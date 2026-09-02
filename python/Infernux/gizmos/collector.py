"""
GizmosCollector — per-frame scene walk that invokes gizmo callbacks and uploads.

Called once per frame (before ``SubmitCulling()``) to:
1. Reset Gizmos per-frame state.
2. [OPTIMISED] Iterate Python components via ``InxComponent._active_instances``
   (populated by ``_set_game_object()``); zero pybind11 calls when no Python
   components exist in the scene.
3. [OPTIMISED] For built-in C++ components that define gizmo methods or icons,
   use a per-type cached GO list rebuilt lazily after scene changes.
"""

from __future__ import annotations

from typing import Dict, List, TYPE_CHECKING

from Infernux.gizmos.gizmos import Gizmos, ICON_KIND_DEFAULT
from Infernux.components.fields import SerializedFieldDescriptor
from Infernux.debug import Debug
from Infernux.engine.editor_visibility import (
    component_owner_is_active_in_hierarchy,
    game_object_is_active_in_hierarchy,
)

if TYPE_CHECKING:
    from Infernux.engine.engine import Engine


# ---------------------------------------------------------------------------
# Module-level scene-change notification flag
#
# Call ``notify_scene_changed()`` from play_mode.py / scene_manager.py when
# objects are added or removed from the scene.  The next collect() call will
# rebuild the per-type icon cache.
# ---------------------------------------------------------------------------
_scene_dirty: bool = True   # start dirty so the cache is populated on first frame

# Rate-limited warning logger for per-frame gizmo issues (avoids console spam)
_gizmo_warn_count: int = 0
_GIZMO_WARN_LIMIT: int = 5


def _log_gizmo_warning(msg: str) -> None:
    """Log a gizmo-related warning, suppressed after a per-session limit."""
    global _gizmo_warn_count
    if _gizmo_warn_count >= _GIZMO_WARN_LIMIT:
        return
    _gizmo_warn_count += 1
    suffix = "" if _gizmo_warn_count < _GIZMO_WARN_LIMIT else " (further warnings suppressed)"
    Debug.log_warning(f"{msg}{suffix}")


def notify_scene_changed() -> None:
    """Signal that the scene has changed; next frame rebuilds the icon cache."""
    global _scene_dirty
    _scene_dirty = True


class GizmosCollector:
    """Stateful per-frame collector.

    Keep a single long-lived instance per Engine; call ``invalidate_cache()``
    whenever objects are added/removed (scene reload, play-mode transitions).

    Performance characteristics for a scene with N purely-C++ objects and K
    Python-component objects:
      * Pass 1: O(K) — zero pybind11 calls when K=0
      * Pass 2: O(icon_instances) per frame after cache warm-up; O(N) once on
        cache miss (per icon type, e.g. first frame or after scene change)

    Usage::

        collector = GizmosCollector()
        # on scene change / play-mode exit:
        collector.invalidate_cache()
        # each frame:
        collector.collect_and_upload(engine)
    """

    def __init__(self):
        # per-type GO cache: type_name -> list of live GO pybind11 objects
        # that carry that component.  Rebuilt lazily when _cache_built doesn't
        # contain the type_name.
        self._icon_cache: Dict[str, list] = {}
        self._cache_built: set = set()   # type_names whose cache entry is filled
        self._last_structure_version: int = -1  # track Scene.structure_version
        self._last_logged_icon_count: int = -1
        self._builtin_registry = None
        # The selected-object subtree is stable until the scene structure or
        # selection changes.  Keep only its IDs: component state and Gizmo
        # geometry are still evaluated every frame.
        self._selection_cache_key = None
        self._selected_ancestor_ids = frozenset()
        self._cache_generation = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        """Invalidate the GO-to-type cache (call on scene change / play mode exit)."""
        self._icon_cache.clear()
        self._cache_built.clear()
        self._cache_generation += 1
        self._selection_cache_key = None
        self._selected_ancestor_ids = frozenset()

    def collect_and_upload(self, engine: 'Engine') -> None:
        """Walk the scene, invoke callbacks, upload geometry to C++."""
        global _scene_dirty

        from Infernux.lib import SceneManager as _SM
        # Ensure built-in wrapper classes (Camera, Light, etc.) have run their
        # BuiltinComponent.__init_subclass__ registration before we snapshot
        # _builtin_registry. Without this prewarm, icon-only gizmos can appear
        # to "do nothing" if no earlier code path imported the wrappers yet.
        import Infernux.components.builtin  # noqa: F401
        from Infernux.components.builtin_component import BuiltinComponent
        from Infernux.components.component import InxComponent

        native = engine.get_native_engine()
        if native is None:
            return

        scene = _SM.instance().get_active_scene()
        if scene is None:
            if native:
                native.clear_component_gizmos()
            return

        # Use the C++ structure_version counter to detect scene mutations cheaply.
        # Falls back to the manual _scene_dirty flag when the binding is unavailable.
        ver = scene.structure_version
        if ver is not None:
            if ver != self._last_structure_version:
                self.invalidate_cache()
                self._last_structure_version = ver
                _scene_dirty = False
        elif _scene_dirty:
            self.invalidate_cache()
            _scene_dirty = False

        selected_id: int = engine.get_selected_object_id()

        # Build the selected subtree only when its inputs changed.  The set is
        # used for membership tests below, while all component enabled checks,
        # callbacks, transforms, and uploads remain per-frame.
        selected_ancestors = self._get_selected_ancestor_ids(scene, selected_id)

        # Builtin wrapper registration is process-stable after the import above;
        # copying this dictionary for every scene frame was needless work.
        if self._builtin_registry is None:
            self._builtin_registry = dict(BuiltinComponent._builtin_registry)
        builtin_registry = self._builtin_registry

        # Begin frame: clear Gizmos accumulation buffers
        Gizmos._begin_frame()

        # ====================================================================
        # Pass 1: Python component gizmos
        #
        # ``InxComponent._active_instances`` is a {go_id: [InxComponent, ...]}
        # dict populated by ``_set_game_object()`` — no pybind11 calls needed
        # to discover which objects have Python components.
        # For a scene with zero Python components this pass costs nothing.
        # ====================================================================
        active = InxComponent._active_instances
        if active:
            # Snapshot to avoid issues if the dict mutates during iteration
            for go_id, components in list(active.items()):
                is_selected = go_id in selected_ancestors
                for comp in components:
                    if isinstance(comp, BuiltinComponent):
                        continue
                    if not component_owner_is_active_in_hierarchy(comp):
                        continue
                    if not getattr(comp, 'enabled', True):
                        continue

                    # Scene icon — Python components opt in the same way the
                    # C++ wrappers do, by declaring _gizmo_icon_color.
                    icon_color = getattr(comp, '_gizmo_icon_color', None)
                    if icon_color is not None:
                        self._register_python_component_icon(comp, go_id, icon_color)

                    always_show = getattr(comp, '_always_show', True)
                    should_draw = always_show or is_selected
                    has_gizmos = (
                        type(comp).on_draw_gizmos is not InxComponent.on_draw_gizmos
                        or type(comp).on_draw_gizmos_selected is not InxComponent.on_draw_gizmos_selected
                    )
                    if should_draw and has_gizmos:
                        comp._call_on_draw_gizmos()

                    if is_selected and has_gizmos:
                        comp._call_on_draw_gizmos_selected()

        # ====================================================================
        # Pass 2: Built-in C++ component gizmos (Camera, Light, BoxCollider…)
        #
        # Optimisations applied:
        #   a) Skip types that have neither icon colour nor custom gizmo methods
        #      (e.g. Rigidbody, MeshRenderer) — they contribute nothing.
        #   b) Skip selection-only types (always_show=False, no icon) when
        #      nothing is selected; only visit selected descendants for these.
        #   c) For icon types (Camera, Light): use _get_icon_instances() which
        #      builds a per-type GO list once and reuses it every subsequent
        #      frame (~O(1) after first build, O(N) on cache miss).
        # ====================================================================
        for type_name, wrapper_cls in builtin_registry.items():

            # --- a) Pre-filter: does this type contribute anything visible? ---
            icon_color = self._resolve_class_value(wrapper_cls, '_gizmo_icon_color', None)
            has_gizmos = (
                wrapper_cls.on_draw_gizmos is not InxComponent.on_draw_gizmos
                or wrapper_cls.on_draw_gizmos_selected is not InxComponent.on_draw_gizmos_selected
            )
            if icon_color is None and not has_gizmos:
                continue  # Nothing to draw (Rigidbody, MeshRenderer, …)

            always_show_cls = bool(self._resolve_class_value(wrapper_cls, '_always_show', True))

            # --- b) Skip selection-only types when nothing is selected ---
            if icon_color is None and not always_show_cls and not selected_ancestors:
                continue  # Colliders etc. — nothing to show, nothing selected

            # --- c) Determine which GOs to iterate ---
            if icon_color is not None:
                # Icon types: use the per-type GO cache (O(1) after first frame)
                matching = self._get_icon_instances(scene, type_name)
            elif not always_show_cls and selected_ancestors:
                # Selection-only gizmos: only visit selected + descendant objects
                matching = [
                    scene.find_by_id(gid)
                    for gid in selected_ancestors if gid
                ]
                matching = [go for go in matching if go is not None]
            else:
                # Always-visible gizmos require the complete object list.
                matching = scene.get_all_objects()

            for go in matching:
                # Gizmos follow GameObject.activeInHierarchy, not activeSelf.
                # Keep this gate before component lookup, icon registration,
                # wrapper creation, and callbacks so every built-in component
                # shares the same hierarchy visibility contract.
                if not game_object_is_active_in_hierarchy(go):
                    continue
                try:
                    go_id = go.id
                    is_selected = go_id in selected_ancestors
                    # Always use C++ lookup — _type_map stores Python wrappers
                    # which can become stale after component removal.
                    cpp_comp = go.get_cpp_component(type_name)
                except Exception as exc:
                    _log_gizmo_warning(f"Gizmo: failed to query component '{type_name}': {exc}")
                    continue
                if cpp_comp is None:
                    continue

                try:
                    enabled = cpp_comp.enabled
                except Exception as exc:
                    _log_gizmo_warning(f"Gizmo: failed to read enabled on '{type_name}': {exc}")
                    continue
                if not enabled:
                    continue

                # ---- Icon registration (always, regardless of selection) ----
                if icon_color is not None:
                    transform = go.get_transform()
                    if transform is not None:
                        pos = transform.position
                        icon_kind = self._resolve_class_value(wrapper_cls, '_gizmo_icon_kind', ICON_KIND_DEFAULT)
                        Gizmos.draw_icon(
                            (pos.x, pos.y, pos.z), go_id, icon_color, icon_kind=icon_kind)

                # ---- Gizmo lifecycle ----
                if not has_gizmos:
                    continue

                try:
                    wrapper = wrapper_cls._get_or_create_wrapper(cpp_comp, go)
                except Exception as exc:
                    _log_gizmo_warning(f"Gizmo: failed to create wrapper for '{type_name}': {exc}")
                    continue
                if wrapper is None:
                    continue

                try:
                    always_show_inst = getattr(wrapper, '_always_show', True)
                    should_draw = always_show_inst or is_selected
                    if should_draw:
                        wrapper._call_on_draw_gizmos()

                    if is_selected:
                        wrapper._call_on_draw_gizmos_selected()
                except Exception as exc:
                    _log_gizmo_warning(f"Gizmo callback failed for '{type_name}': {exc}")
                    wrapper._invalidate_native_binding()

        # ---- Pack and upload line gizmo data ----
        packed = Gizmos._get_packed_data()
        if packed is not None:
            vert_buf, vert_count, idx_buf, desc_buf, desc_count = packed
            native.upload_component_gizmos(
                vert_buf, vert_count, idx_buf, desc_buf, desc_count)
        else:
            native.clear_component_gizmos()

        # ---- Pack and upload icon data ----
        icon_packed = Gizmos._get_packed_icon_data()
        if icon_packed is not None:
            pos_color_buf, id_buf, kind_buf, icon_count = icon_packed
            native.upload_component_gizmo_icons(
                pos_color_buf, id_buf, kind_buf, icon_count)
            if icon_count != self._last_logged_icon_count:
                Debug.log_internal(f"[Gizmos] uploaded {icon_count} component icon(s)")
                self._last_logged_icon_count = icon_count
        else:
            native.clear_component_gizmo_icons()
            if self._last_logged_icon_count != 0:
                Debug.log_internal("[Gizmos] uploaded 0 component icons")
                self._last_logged_icon_count = 0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_selected_ancestor_ids(self, scene, selected_id: int):
        """Return selected + descendant IDs, reusing the stable scene relation."""
        selected_id = int(selected_id or 0)
        key = (self._cache_generation, selected_id)
        if key != self._selection_cache_key:
            self._selected_ancestor_ids = (
                frozenset(self._build_ancestor_set(scene, selected_id))
                if selected_id
                else frozenset()
            )
            self._selection_cache_key = key
        return self._selected_ancestor_ids

    @staticmethod
    def _register_python_component_icon(comp, go_id: int, icon_color) -> None:
        """Queue the scene billboard icon for a Python component instance."""
        try:
            transform = comp.game_object.get_transform()
        except Exception as exc:
            _log_gizmo_warning(f"Gizmo: failed to read transform for icon: {exc}")
            return
        if transform is None:
            return
        pos = transform.position
        Gizmos.draw_icon(
            (pos.x, pos.y, pos.z), go_id, icon_color,
            icon_kind=getattr(comp, '_gizmo_icon_kind', ICON_KIND_DEFAULT),
        )

    def _get_icon_instances(self, scene, type_name: str) -> list:
        """Return the cached list of GOs that carry *type_name*.

        On the first call for a given type (or after ``invalidate_cache()``),
        walks all scene objects once to find matching GOs.  Subsequent calls
        return the cached list in O(1).
        """
        if type_name in self._cache_built:
            return self._icon_cache.get(type_name, [])

        # Build this type's cache entry via a one-time scene walk
        result: List = []
        all_objs = scene.get_all_objects()
        for go in all_objs:
            if go.get_cpp_component(type_name) is not None:
                result.append(go)

        self._icon_cache[type_name] = result
        self._cache_built.add(type_name)
        return result

    @staticmethod
    def _build_ancestor_set(scene, selected_id: int) -> set:
        """Build a set containing the selected object ID and all its descendant IDs."""
        result = set()
        if not selected_id:
            return result

        selected_go = scene.find_by_id(selected_id)
        if selected_go is None:
            result.add(selected_id)
            return result

        # Iterative DFS to collect selected GO + all descendants
        stack = [selected_go]
        while stack:
            go = stack.pop()
            gid = go.id
            result.add(gid)
            children = go.get_children()
            if children:
                stack.extend(children)

        return result

    @staticmethod
    def _resolve_class_value(cls_obj, name: str, default):
        value = getattr(cls_obj, name, default)
        if isinstance(value, SerializedFieldDescriptor):
            return value.metadata.default
        return value
