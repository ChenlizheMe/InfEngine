"""Main wiring function for the C++ InspectorPanel."""
from __future__ import annotations

import copy
import time as _time
from typing import TYPE_CHECKING

from Infernux.debug import Debug
from Infernux.engine.i18n import t
from Infernux.engine.bootstrap_inspector._helpers import (
    _can_remove_component,
    _get_add_component_entries,
    _get_components_safe,
    _get_py_components_safe,
    _load_script_component,
)

if TYPE_CHECKING:
    from Infernux.engine.bootstrap import EditorBootstrap


class _Ctx:
    """Thin namespace shared across inspector wiring helpers."""


def _component_commands():
    """Return the sole authority for Inspector component mutations."""
    from Infernux.engine.interaction import ComponentCommandService

    return ComponentCommandService.require()


# ═══════ Cache initialisation ═══════════════════════════════════

def _wire_cache_init(ctx):
    """Create component and material caches, invalidation helpers."""
    _component_cache = {
        "object_id": 0, "scene_version": -1, "structure_version": -1,
        "script_error_revision": -1,
        "items": [], "native_map": {}, "py_map": {},
    }
    _material_section_cache = {
        "object_id": 0, "scene_version": -1, "structure_version": -1,
        "signature": (), "entries": [],
    }
    ctx.component_cache = _component_cache
    ctx.material_section_cache = _material_section_cache

    def _invalidate_material_section_cache():
        _material_section_cache.update(
            object_id=0, scene_version=-1, structure_version=-1,
            signature=(), entries=[])

    def _invalidate_component_cache():
        _component_cache.update(
            object_id=0, scene_version=-1, structure_version=-1,
            script_error_revision=-1,
            items=[], native_map={}, py_map={})
        _invalidate_material_section_cache()

    ctx.invalidate_component_cache = _invalidate_component_cache

    def _resolve_scene_object(object_id):
        manager = ctx.SceneManager.instance()
        resolver = getattr(manager, "find_runtime_object_by_id", None)
        if callable(resolver):
            obj = resolver(int(object_id or 0))
        else:
            scene = manager.get_active_scene()
            obj = scene.find_by_id(int(object_id or 0)) if scene else None
        scene = getattr(obj, "scene", None) if obj is not None else None
        if scene is None:
            scene = manager.get_active_scene()
        return scene, obj

    ctx.resolve_scene_object = _resolve_scene_object

    def _current_scene_and_versions(object_id=0):
        scene, _obj = _resolve_scene_object(object_id) if object_id else (
            ctx.SceneManager.instance().get_active_scene(),
            None,
        )
        scene_version = getattr(scene, 'structure_version', -1) if scene else -1
        structure_version = ctx._inspector_support.get_component_structure_version()
        return scene, scene_version, structure_version

    ctx.current_scene_and_versions = _current_scene_and_versions


# ═══════ Component enumeration ═════════════════════════════════

def _wire_component_list(ctx):
    """Wire get_component_list and helper resolvers."""
    SceneManager = ctx.SceneManager
    InspectorComponentInfo = ctx.InspectorComponentInfo
    InxComponent = ctx.InxComponent
    _component_cache = ctx.component_cache
    _invalidate = ctx.invalidate_component_cache
    _versions = ctx.current_scene_and_versions
    snapshot_service = getattr(ctx, "inspector_snapshot_service", None)
    inspector_target_type = getattr(ctx, "InspectorTarget", None)
    if snapshot_service is None or inspector_target_type is None:
        from Infernux.engine.ui.inspector_snapshot import (
            InspectorSnapshotService,
            InspectorTarget,
        )

        snapshot_service = snapshot_service or InspectorSnapshotService.instance()
        inspector_target_type = inspector_target_type or InspectorTarget

    def _register_target_components(obj_id, native_map, py_map):
        snapshot_service.register_target_components(
            inspector_target_type.scene_object(obj_id),
            (*native_map.values(), *py_map.values()),
        )

    def _script_error_revision():
        from Infernux.components.script_loader import get_script_error_revision
        return get_script_error_revision()

    def _script_error(component):
        from ._helpers import _get_component_script_error
        return _get_component_script_error(component, ctx.engine.get_asset_database())

    def _native_wrapper_is_dead(component):
        """Cheaply reject wrappers invalidated by a scene/component rebuild.

        Calling ``is_valid`` here would resolve a native handle for every
        component on every Inspector frame.  The invalidation path already
        clears ``_cpp_component`` and marks the wrapper destroyed, so these
        local fields are sufficient to decide whether the cached map must be
        rebuilt.
        """
        if not bool(getattr(component, "_is_builtin_component_wrapper", False)):
            return False
        stale = getattr(component, "_is_native_binding_stale", None)
        if callable(stale):
            return bool(stale())
        return (
            getattr(component, "_cpp_component", None) is None
            or bool(getattr(component, "_is_destroyed", False))
        )

    def _is_py_entry(component):
        return isinstance(component, InxComponent) or hasattr(component, 'get_py_component')

    def _get_component_payload(obj_id):
        scene, scene_ver, struct_ver = _versions(obj_id)
        error_revision = _script_error_revision()
        if (
            _component_cache["object_id"] == obj_id
            and _component_cache["scene_version"] == scene_ver
            and _component_cache["structure_version"] == struct_ver
        ):
            items = _component_cache["items"]
            native_map = _component_cache["native_map"]
            py_map = _component_cache["py_map"]
            errors_changed = _component_cache["script_error_revision"] != error_revision
            stale = False
            for item in items:
                comp = native_map.get(item.component_id) if item.is_native else py_map.get(item.component_id)
                if comp is None or (item.is_native and _native_wrapper_is_dead(comp)):
                    stale = True
                    break
                item.enabled = bool(getattr(comp, 'enabled', True))
                if not item.is_native and errors_changed:
                    error = _script_error(comp)
                    item.is_broken = bool(error)
                    item.broken_error = error or ''
            if not stale:
                _component_cache["script_error_revision"] = error_revision
                _register_target_components(obj_id, native_map, py_map)
                return scene, items, native_map, py_map

        scene, obj = ctx.resolve_scene_object(obj_id)
        if obj is None:
            _invalidate()
            return scene, [], {}, {}

        items, native_map, py_map = [], {}, {}
        from Infernux.components.builtin_component import BuiltinComponent

        # Single pass over get_components() preserves the actual insertion
        # order (C++ m_components vector) so the Inspector shows components
        # in chronological add-order.
        for comp in _get_components_safe(obj):
            tn = getattr(comp, 'type_name', type(comp).__name__)
            if tn == "Transform":
                continue

            if _is_py_entry(comp):
                # The public GameObject component facade has already unwrapped
                # any PyComponentProxy, so *comp* is the Python component.
                py_comp = comp
                cid = getattr(py_comp, 'component_id', id(py_comp))
                ci = InspectorComponentInfo()
                ci.type_name = getattr(py_comp, 'type_name', type(py_comp).__name__)
                display_key = str(getattr(type(py_comp), '_display_name_key', '') or '')
                ci.display_name = t(display_key) if display_key else ci.type_name
                ci.component_id = cid
                ci.enabled = bool(getattr(py_comp, 'enabled', True))
                ci.is_native = False
                ci.is_script = True
                error = _script_error(py_comp)
                ci.is_broken = bool(error)
                ci.broken_error = error or ''
                ci.icon_id = ctx.get_component_icon_id(ci.type_name, True)
                items.append(ci)
                py_map[cid] = py_comp
            else:
                cid = getattr(comp, 'component_id', id(comp))
                ci = InspectorComponentInfo()
                ci.type_name = tn
                ci.component_id = cid
                ci.enabled = bool(getattr(comp, 'enabled', True))
                ci.is_native = True
                ci.is_script = False
                ci.is_broken = False
                ci.icon_id = ctx.get_component_icon_id(tn, False)
                items.append(ci)
                wrapper_cls = BuiltinComponent._builtin_registry.get(tn)
                if wrapper_cls is not None and not isinstance(comp, BuiltinComponent):
                    try:
                        comp = wrapper_cls._get_or_create_wrapper(comp, obj)
                    except (AttributeError, ReferenceError, RuntimeError, TypeError) as exc:
                        Debug.log_suppressed(
                            f"Inspector.wrap_component[{tn}:{cid}]", exc
                        )
                native_map[cid] = comp

        _component_cache.update(
            object_id=obj_id, scene_version=scene_ver,
            structure_version=struct_ver,
            script_error_revision=error_revision,
            items=items, native_map=native_map, py_map=py_map)
        _register_target_components(obj_id, native_map, py_map)
        return scene, items, native_map, py_map

    def _get_cached_maps(obj_id):
        scene, scene_ver, struct_ver = _versions(obj_id)
        error_revision = _script_error_revision()
        if (
            _component_cache["object_id"] == obj_id
            and _component_cache["scene_version"] == scene_ver
            and _component_cache["structure_version"] == struct_ver
            and _component_cache["script_error_revision"] == error_revision
        ):
            native_map = _component_cache["native_map"]
            if not any(_native_wrapper_is_dead(comp) for comp in native_map.values()):
                return (scene, _component_cache["items"],
                        native_map, _component_cache["py_map"])
        return _get_component_payload(obj_id)

    ctx.get_cached_component_maps = _get_cached_maps

    def _resolve_component(obj_id, comp_id, is_native):
        _scene, _items, native_map, py_map = _get_cached_maps(obj_id)
        return native_map.get(comp_id) if is_native else py_map.get(comp_id)

    ctx.resolve_component = _resolve_component
    ctx.ip.get_component_list = lambda obj_id: _get_component_payload(obj_id)[1]


# ═══════ Object info & properties ══════════════════════════════

def _wire_object_info(ctx):
    """Wire the read-only object-info projection."""
    SceneManager = ctx.SceneManager
    InspectorObjectInfo = ctx.InspectorObjectInfo
    ip = ctx.ip
    _bump = ctx._bump_inspector_values

    def _get_object_info(obj_id):
        info = InspectorObjectInfo()
        _scene, obj = ctx.resolve_scene_object(obj_id)
        if obj is None:
            return info
        info.name = obj.name
        info.active = obj.active
        info.tag = getattr(obj, 'tag', 'Untagged')
        info.layer = getattr(obj, 'layer', 0)
        info.prefab_guid = getattr(obj, 'prefab_guid', '') or ''
        info.hide_transform = getattr(obj, 'hide_transform', False)
        transform = obj.get_transform()
        info.transform_component_id = int(
            getattr(transform, 'component_id', 0) or 0
        )
        return info

    ip.get_object_info = _get_object_info

# ═══════ Transform ═════════════════════════════════════════════

def _wire_transform(ctx):
    """Wire the read-only Transform projection."""
    SceneManager = ctx.SceneManager
    ip = ctx.ip
    _bump = ctx._bump_inspector_values

    from Infernux.lib import InspectorTransformData

    def _get_transform_data(obj_id):
        td = InspectorTransformData()
        _scene, obj = ctx.resolve_scene_object(obj_id)
        if obj is None:
            return td
        trans = obj.get_transform()
        if trans is None:
            return td
        lp = trans.local_position
        le = trans.local_euler_angles
        ls = trans.local_scale
        td.px, td.py_, td.pz = lp.x, lp.y, lp.z
        td.rx, td.ry, td.rz = le.x, le.y, le.z
        td.sx, td.sy, td.sz = ls.x, ls.y, ls.z
        return td

    ip.get_transform_data = _get_transform_data

# ═══════ Icons & body rendering ════════════════════════════════

def _wire_icons_and_body(ctx):
    """Wire component icons, body rendering, and enabled toggle."""
    ip = ctx.ip
    engine = ctx.engine
    _bump = ctx._bump_inspector_values
    _record_count = ctx._record_profile_count
    _record_timing = ctx._record_profile_timing
    _component_cache = ctx.component_cache
    _inspector_support = ctx._inspector_support
    _profile_enabled = _inspector_support.is_inspector_profile_enabled()
    from Infernux.engine.texture_task_bridge import texture_stamp, query_or_schedule_texture

    _icon_cache = {}
    _icons_loaded = [False]

    def _ensure_icons():
        if _icons_loaded[0]:
            return
        native_engine = engine.get_native_engine()
        if not native_engine:
            return
        import os
        import Infernux.resources as _resources
        icons_dir = _resources.component_icons_dir
        if not os.path.isdir(icons_dir):
            return
        all_ready = True
        for fname in os.listdir(icons_dir):
            if not fname.startswith("component_") or not fname.endswith(".png"):
                continue
            key = fname[len("component_"):-len(".png")]
            icon_path = os.path.join(icons_dir, fname)
            stamp = texture_stamp(icon_path, "component_icon")
            if stamp == 0:
                all_ready = False
                continue
            tid, _, _ = query_or_schedule_texture(
                native_engine,
                f"compicon|{key}",
                icon_path,
                int(stamp),
                # Inspector draws ~COMPONENT_ICON_SIZE logical px; linear + mips from a 256px
                # atlas reads mushy. Point sampling + smaller CPU downscale matches Unity-like crisp UI.
                nearest=True,
                srgb=False,
            )
            # Overwrite even with 0 so a stale handle never survives eviction
            # or replacement of the underlying texture.
            _icon_cache[key] = tid
            if tid == 0:
                all_ready = False
        _icons_loaded[0] = all_ready

    def _live_component_icon_id(key):
        """Re-resolve the currently-published descriptor for an icon.

        Texture ids are raw Vulkan descriptor handles owned by the native
        preview system, which may replace or evict textures at any time.
        Binding a handle cached across frames can hit a freed VkDescriptorSet
        (validation errors, icons rendering as other textures, crashes), so
        the id is looked up fresh every call. Returns -1 when the native
        getter is unavailable (older builds) so callers can fall back.
        """
        native_engine = engine.get_native_engine()
        getter = getattr(native_engine, "get_texture_preview_texture_id", None) if native_engine else None
        if getter is None:
            return -1
        try:
            return int(getter(f"compicon|{key}") or 0)
        except Exception:
            return 0

    def _get_component_icon_id(type_name, is_script):
        _ensure_icons()
        key = type_name.lower()
        if _icon_cache.get(key, 0) == 0 and is_script:
            key = "script"
        if _icon_cache.get(key, 0) == 0:
            return 0
        live = _live_component_icon_id(key)
        if live == -1:
            return _icon_cache.get(key, 0)
        if live == 0:
            # Evicted or replaced mid-flight: schedule a re-upload on the next
            # lookup and draw nothing this frame rather than a dead handle.
            _icons_loaded[0] = False
            _icon_cache[key] = 0
            return 0
        return live

    ctx.get_component_icon_id = _get_component_icon_id
    ip.get_component_icon_id = _get_component_icon_id

    from Infernux.engine.ui import inspector_components as comp_ui

    _component_body_heights = {}

    def _render_component_body_live(ctx_arg, obj_id, type_name, comp_id, is_native):
        if _profile_enabled:
            _record_count("bodyResolve_count")
        _resolve_t0 = _time.perf_counter() if _profile_enabled else 0.0
        comp = ctx.resolve_component(obj_id, comp_id, is_native)
        if _profile_enabled:
            _record_timing("bodyResolve", (_time.perf_counter() - _resolve_t0) * 1000.0)
        if comp is None:
            return
        if is_native:
            if _profile_enabled:
                _record_count("bodyNativeDispatch_count")
            _t0 = _time.perf_counter() if _profile_enabled else 0.0
            comp_ui.render_component(ctx_arg, comp)
            if _profile_enabled:
                _record_timing("bodyNativeDispatch", (_time.perf_counter() - _t0) * 1000.0)
            return
        from Infernux.engine.ui.inspector_components import render_py_component
        if _profile_enabled:
            _record_count("bodyPyDispatch_count")
        _t0 = _time.perf_counter() if _profile_enabled else 0.0
        render_py_component(ctx_arg, comp)
        if _profile_enabled:
            _record_timing("bodyPyDispatch", (_time.perf_counter() - _t0) * 1000.0)

    def _render_component_body(ctx_arg, obj_id, type_name, comp_id, is_native):
        cache_key = (obj_id, comp_id, bool(is_native), type_name)
        cached_height = _component_body_heights.get(cache_key, 0.0)
        visibility_query = getattr(ctx_arg, "is_virtualized_region_visible", None)
        if cached_height > 0.0 and callable(visibility_query) and not visibility_query(cached_height):
            ctx_arg.dummy(0.0, cached_height)
            if _profile_enabled:
                _record_count("bodyVirtualizedSkip_count")
            return

        start_y = ctx_arg.get_cursor_pos_y()
        try:
            _render_component_body_live(ctx_arg, obj_id, type_name, comp_id, is_native)
        finally:
            measured_height = max(0.0, ctx_arg.get_cursor_pos_y() - start_y)
            if measured_height > 0.0:
                _component_body_heights[cache_key] = measured_height
            else:
                _component_body_heights.pop(cache_key, None)

    ip.render_component_body = _render_component_body

    def _render_multi_component_body(ctx_arg, obj_ids, type_name, comp_ids, is_native):
        comps = []
        for obj_id, comp_id in zip(obj_ids, comp_ids):
            comp = ctx.resolve_component(obj_id, comp_id, is_native)
            if comp is not None:
                comps.append(comp)
        if not comps:
            return
        if _profile_enabled:
            _record_count("bodyMultiDispatch_count")
        _t0 = _time.perf_counter() if _profile_enabled else 0.0
        comp_ui.render_multi_component(ctx_arg, comps, is_native=is_native)
        if _profile_enabled:
            _record_timing("bodyMultiDispatch", (_time.perf_counter() - _t0) * 1000.0)

    ip.render_multi_component_body = _render_multi_component_body

    def _enabled_components(obj_ids, comp_ids, is_native):
        if not obj_ids or len(obj_ids) != len(comp_ids):
            return ()
        components = tuple(
            ctx.resolve_component(obj_id, comp_id, is_native)
            for obj_id, comp_id in zip(obj_ids, comp_ids)
        )
        return components if all(comp is not None for comp in components) else ()

    def _can_set_component_enabled(obj_ids, comp_ids, new_enabled, is_native):
        del new_enabled
        return bool(_enabled_components(obj_ids, comp_ids, is_native))

    def _set_component_enabled(obj_ids, comp_ids, new_enabled, is_native):
        components = _enabled_components(obj_ids, comp_ids, is_native)
        if not components:
            return False
        changes = [
            (
                comp,
                "enabled",
                bool(comp.enabled),
                bool(new_enabled),
                f"Toggle {getattr(comp, 'type_name', type(comp).__name__)}",
            )
            for comp in components
            if bool(comp.enabled) != bool(new_enabled)
        ]
        if not changes:
            return False
        type_name = getattr(components[0], "type_name", type(components[0]).__name__)
        if not _component_commands().execute_property_changes(
            changes,
            description=f"Toggle {type_name}",
        ):
            return False
        changed_ids = {int(comp_id) for comp_id in comp_ids}
        for item in _component_cache["items"]:
            if int(item.component_id) in changed_ids:
                item.enabled = bool(new_enabled)
        # The native component headers cache enabled state as structural UI
        # metadata.  Refresh only the owning objects; a coarse value bump here
        # used to invalidate every Inspector target and every sibling body.
        for obj_id in obj_ids:
            ctx.inspector_snapshot_service.invalidate_schema(
                ctx.InspectorTarget.scene_object(int(obj_id)),
                domain="component_enabled",
            )
        return True

    # The command adapter is assembled by ``_wire_clipboard_and_context``.
    # Publish these callbacks through the shared wiring context instead of
    # relying on local closures from this wiring phase.
    ctx.can_set_component_enabled = _can_set_component_enabled
    ctx.set_component_enabled = _set_component_enabled


# ═══════ Clipboard & context menu ══════════════════════════════

def _python_component_clipboard_document(comp) -> dict:
    """Capture fields without copying the source component's identity."""
    document = copy.deepcopy(comp._serialize_fields_document())
    document.pop("__component_id__", None)
    return document


def _apply_python_component_clipboard_document(
    comp,
    document: dict,
    *,
    invoke_after_deserialize: bool = True,
) -> None:
    """Apply a clipboard field snapshot while preserving target identity."""
    if not isinstance(document, dict):
        raise TypeError("Python component clipboard payload must be an object")
    payload = copy.deepcopy(document)
    payload.pop("__component_id__", None)
    payload["__type_name__"] = type(comp).__name__
    comp._deserialize_fields_document(
        payload,
        _skip_on_after_deserialize=not invoke_after_deserialize,
    )


def _publish_component_selection(bs, object_ids, component_ids, is_native):
    """Publish a native Inspector header gesture into the global authority."""
    from Infernux.engine.interaction import SelectionService, SelectionTarget

    document_id = ""
    scene_file_manager = getattr(bs, "scene_file_manager", None)
    if scene_file_manager is not None:
        document_id = str(getattr(scene_file_manager, "document_id", "") or "")
    targets = tuple(
        SelectionTarget.component(
            int(object_id),
            int(component_id),
            document_id=document_id,
            sub_kind="native" if is_native else "script",
        )
        for object_id, component_id in zip(object_ids, component_ids)
        if int(object_id) > 0 and int(component_id) > 0
    )
    if not targets:
        return False
    return SelectionService.instance().replace(
        targets,
        owner_id="inspector",
        primary=targets[-1],
        anchor=targets[0],
        reason="inspector_component_select",
        record_history=True,
    )


def _component_clipboard_data() -> dict | None:
    from Infernux.engine.interaction import ClipboardDomain, ClipboardService

    payload = ClipboardService.instance().peek(ClipboardDomain.COMPONENT)
    if payload is None or len(payload.items) != 1:
        return None
    data = payload.items[0].data
    if not isinstance(data, dict) or data.get("schema") != "component_document":
        return None
    if not isinstance(data.get("type_name"), str) or not data["type_name"]:
        return None
    if not isinstance(data.get("is_native"), bool):
        return None
    if not isinstance(data.get("script_guid"), str):
        return None
    if not isinstance(data.get("type_guid"), str):
        return None
    document = data.get("document")
    if not isinstance(document, dict):
        return None
    return data


def _publish_component_clipboard(comp, type_name: str, is_native: bool) -> bool:
    try:
        if is_native and hasattr(comp, "serialize_document"):
            document = comp.serialize_document()
            document.pop("component_id", None)
        elif hasattr(comp, "_serialize_fields_document"):
            document = _python_component_clipboard_document(comp)
        else:
            return False
    except Exception as exc:
        Debug.log_error(f"Cannot copy component properties: {exc}")
        return False

    from Infernux.engine.interaction import (
        ClipboardDomain,
        ClipboardItem,
        ClipboardOperation,
        ClipboardService,
    )

    component_id = int(getattr(comp, "component_id", 0) or 0)
    ClipboardService.instance().write(
        ClipboardDomain.COMPONENT,
        (
            ClipboardItem(
                str(component_id or type_name),
                sub_kind=str(type_name),
                data={
                    "schema": "component_document",
                    "type_name": str(type_name),
                    "is_native": bool(is_native),
                    "script_guid": getattr(comp, "_script_guid", "") or "",
                    "type_guid": (
                        "" if is_native else comp.__class__._get_type_guid()
                    ),
                    "document": copy.deepcopy(document),
                },
            ),
        ),
        operation=ClipboardOperation.COPY,
        source_owner_id="inspector",
        reason="copy_component_properties",
    )
    return True


def _paste_native_component_as_new(obj, type_name: str, document: dict) -> bool:
    try:
        _component_commands().add(
            obj,
            type_name,
            native_document=document,
            description=f"Paste {type_name} As New",
        )
        return True
    except RuntimeError:
        return False


def _paste_native_component_values(comp, document: dict) -> bool:
    return _component_commands().restore_document(
        comp,
        copy.deepcopy(document),
        description=f"Paste {getattr(comp, 'type_name', 'Component')} Properties",
        edit_key=f"paste_component:{_time.time_ns()}",
    )

def _wire_clipboard_and_context(ctx):
    """Wire clipboard operations and component context menu."""
    ip = ctx.ip
    engine = ctx.engine
    _resolve = ctx.resolve_component
    _invalidate = ctx.invalidate_component_cache
    _bump = ctx._bump_inspector_values

    ip.on_component_selection_changed = lambda object_ids, component_ids, is_native: (
        _publish_component_selection(ctx.bs, object_ids, component_ids, is_native)
    )
    _t = ctx._t
    SceneManager = ctx.SceneManager

    def _copy_to_clipboard(comp, type_name, is_native):
        return _publish_component_clipboard(comp, type_name, is_native)

    def _has_clip():
        return _component_clipboard_data() is not None

    def _can_paste_values(comp, type_name, is_native):
        data = _component_clipboard_data()
        if data is None:
            return False
        if not (data["type_name"] == type_name and
                data["is_native"] == is_native):
            return False
        if is_native:
            return True
        return (
            data["script_guid"] == (getattr(comp, "_script_guid", "") or "")
            and data["type_guid"] == comp.__class__._get_type_guid()
        )

    def _paste_as_new(obj):
        data = _component_clipboard_data()
        if data is None:
            return False
        tn = data["type_name"]
        native = data["is_native"]
        payload = data["document"]
        guid = data["script_guid"]
        type_guid = data["type_guid"]
        if native:
            if not _paste_native_component_as_new(obj, tn, payload):
                return False
        else:
            from Infernux.engine.component_restore import create_component_instance
            from Infernux.engine.scene_manager import SceneFileManager
            sfm = SceneFileManager.instance()
            asset_db = sfm._asset_database if sfm else None
            instance, _sp = create_component_instance(
                guid,
                type_guid,
                tn,
                asset_database=asset_db,
                prefer_loaded_type=True,
            )
            if instance is None:
                Debug.log_warning(f"Cannot paste: failed to create '{tn}'")
                return
            if payload:
                try:
                    _apply_python_component_clipboard_document(
                        instance,
                        payload,
                        invoke_after_deserialize=False,
                    )
                except Exception as _exc:
                    instance._call_on_destroy()
                    Debug.log_error(f"Cannot paste '{tn}' fields: {_exc}")
                    return False
            if guid:
                try:
                    instance._script_guid = guid
                except Exception as _exc:
                    Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            _component_commands().add(
                obj,
                tn,
                python_instance=instance,
                invoke_after_deserialize=True,
                description=f"Paste {tn} As New",
            )
        _invalidate()
        return True

    def _paste_values(comp, is_native):
        data = _component_clipboard_data()
        if data is None:
            return False
        payload = data["document"]
        if is_native and hasattr(comp, "deserialize_document"):
            if not _paste_native_component_values(comp, payload):
                return False
        elif hasattr(comp, "_deserialize_fields_document"):
            try:
                new_document = copy.deepcopy(payload)
                new_document["__type_name__"] = type(comp).__name__
                if not _component_commands().restore_document(
                    comp,
                    new_document,
                    description=f"Paste {type(comp).__name__} Properties",
                    edit_key=f"paste_component:{_time.time_ns()}",
                ):
                    return False
            except Exception as _exc:
                Debug.log_error(f"Cannot paste component properties: {_exc}")
                return False
        _bump()
        return True

    def _get_script_path(comp):
        guid = getattr(comp, '_script_guid', None)
        if not guid:
            return ''
        adb = engine.get_asset_database()
        if adb:
            path = adb.get_path_from_guid(guid)
            if path:
                return path
        return ''

    def _component_entry(object_id, component_id, is_native):
        from Infernux.engine.interaction import SelectionTarget

        _scene, obj = ctx.resolve_scene_object(int(object_id))
        if obj is None or int(component_id) <= 0:
            return None
        comp = _resolve(int(object_id), int(component_id), bool(is_native))
        if comp is None:
            return None
        type_name = str(
            getattr(comp, "type_name", type(comp).__name__) or type(comp).__name__
        )
        target = SelectionTarget.component(
            int(object_id),
            int(component_id),
            sub_kind="native" if is_native else "script",
        )
        return target, obj, comp, type_name, bool(is_native)

    def _selected_component_entries(explicit_target=None):
        from Infernux.engine.interaction import SelectionDomain, SelectionService

        if explicit_target is not None:
            entry = _component_entry(*explicit_target)
            return [entry] if entry is not None else []

        snapshot = SelectionService.instance().snapshot
        if snapshot.domain is not SelectionDomain.COMPONENT:
            return []
        entries = []
        for target in snapshot.targets:
            object_id, component_id = target.component_ids()
            _scene, obj = ctx.resolve_scene_object(object_id) if object_id else (None, None)
            if obj is None or component_id <= 0:
                continue
            native_hint = target.sub_kind != "script"
            comp = _resolve(object_id, component_id, native_hint)
            is_native = native_hint
            if comp is None and not target.sub_kind:
                comp = _resolve(object_id, component_id, False)
                is_native = False
            if comp is None:
                continue
            type_name = str(
                getattr(comp, "type_name", type(comp).__name__) or type(comp).__name__
            )
            entries.append((target, obj, comp, type_name, is_native))
        return entries

    def _primary_component_entry(explicit_target=None):
        entries = _selected_component_entries(explicit_target)
        if not entries:
            return None
        if explicit_target is not None:
            return entries[0]
        from Infernux.engine.interaction import SelectionService

        primary = SelectionService.instance().snapshot.primary
        return next((entry for entry in entries if entry[0] == primary), entries[-1])

    def _can_copy_selected_component(explicit_target=None):
        return _primary_component_entry(explicit_target) is not None

    def _copy_selected_component(explicit_target=None):
        entry = _primary_component_entry(explicit_target)
        if entry is None:
            return False
        _target, _obj, comp, type_name, is_native = entry
        return _copy_to_clipboard(comp, type_name, is_native)

    def _can_paste_selected_values(explicit_target=None):
        entry = _primary_component_entry(explicit_target)
        if entry is None:
            return False
        _target, _obj, comp, type_name, is_native = entry
        return _can_paste_values(comp, type_name, is_native)

    def _paste_selected_values(explicit_target=None):
        entry = _primary_component_entry(explicit_target)
        if entry is None:
            return False
        _target, _obj, comp, _type_name, is_native = entry
        return _paste_values(comp, is_native)

    def _can_paste_selected_as_new(explicit_target=None):
        entry = _primary_component_entry(explicit_target)
        return bool(entry is not None and entry[3] != "Transform" and _has_clip())

    def _paste_selected_as_new(explicit_target=None):
        entry = _primary_component_entry(explicit_target)
        if entry is None:
            return False
        return _paste_as_new(entry[1])

    def _paste_selected_default(explicit_target=None):
        if _can_paste_selected_values(explicit_target):
            return _paste_selected_values(explicit_target)
        return _paste_selected_as_new(explicit_target)

    def _can_remove_selected_components(explicit_target=None):
        entries = _selected_component_entries(explicit_target)
        return bool(entries) and all(
            _can_remove_component(obj, comp, type_name, is_native)
            for _target, obj, comp, type_name, is_native in entries
        )

    def _can_reset_selected_components(explicit_target=None):
        return bool(_selected_component_entries(explicit_target))

    def _reset_selected_components(explicit_target=None):
        entries = _selected_component_entries(explicit_target)
        if not entries:
            return False
        edits = []
        for _target, obj, comp, type_name, is_native in entries:
            if is_native:
                old_document = comp.serialize_document()
                native_component = (
                    comp._require_cpp_component()
                    if hasattr(comp, "_require_cpp_component")
                    else comp
                )
                new_document = obj.get_component_default_document(native_component)
                if old_document != new_document:
                    edits.append(
                        (comp, new_document, f"Reset {type_name}", "")
                    )
                continue

            old_document = _python_component_clipboard_document(comp)
            fresh = type(comp)()
            if hasattr(fresh, "_call_reset"):
                fresh._call_reset()
            new_document = _python_component_clipboard_document(fresh)
            if old_document != new_document:
                edits.append(
                    (
                        comp,
                        new_document,
                        f"Reset {type_name}",
                        f"reset_component:{int(getattr(comp, 'component_id', 0) or 0)}",
                    )
                )

        if not edits:
            return False
        if not _component_commands().restore_many(
            edits,
            description=f"Reset {len(edits)} Components",
        ):
            return False
        _bump()
        return True

    def _component_reorder_changes(direction: int, explicit_target=None):
        if direction not in {-1, 1}:
            raise ValueError("component move direction must be -1 or 1")
        grouped = {}
        for _target, obj, comp, _type_name, _is_native in _selected_component_entries(
            explicit_target
        ):
            component_id = int(getattr(comp, "component_id", 0) or 0)
            if component_id:
                grouped.setdefault(int(obj.id), (obj, set()))[1].add(component_id)

        changes = []
        for object_id, (obj, selected_ids) in grouped.items():
            before = [int(value) for value in obj.get_component_order()]
            selected_ids.intersection_update(before)
            if not selected_ids:
                continue
            after = list(before)
            indices = (
                range(1, len(after))
                if direction < 0
                else range(len(after) - 2, -1, -1)
            )
            for index in indices:
                neighbor = index - 1 if direction < 0 else index + 1
                if after[index] in selected_ids and after[neighbor] not in selected_ids:
                    after[index], after[neighbor] = after[neighbor], after[index]
            if after != before:
                changes.append((object_id, tuple(before), tuple(after)))
        return changes

    def _can_move_selected_components(direction: int, explicit_target=None):
        return bool(_component_reorder_changes(direction, explicit_target))

    def _execute_component_reorder(changes, description: str):
        if not changes:
            return False
        if not _component_commands().reorder(changes, description=description):
            return False
        _invalidate()
        return True

    def _move_selected_components(direction: int, explicit_target=None):
        return _execute_component_reorder(
            _component_reorder_changes(direction, explicit_target),
            "Move Components Up" if direction < 0 else "Move Components Down",
        )

    def _component_drag_reorder_changes(
        object_ids,
        dragged_component_ids,
        target_component_ids,
        insert_after: bool,
    ):
        try:
            object_ids = tuple(int(value) for value in object_ids)
            dragged_component_ids = tuple(int(value) for value in dragged_component_ids)
            target_component_ids = tuple(int(value) for value in target_component_ids)
        except (TypeError, ValueError):
            return []
        if (
            not object_ids
            or len(object_ids) != len(dragged_component_ids)
            or len(object_ids) != len(target_component_ids)
            or len(set(object_ids)) != len(object_ids)
        ):
            return []

        changes = []
        for object_id, dragged_component_id, target_component_id in zip(
            object_ids, dragged_component_ids, target_component_ids
        ):
            _scene, obj = ctx.resolve_scene_object(object_id)
            if obj is None:
                return []
            before = [int(value) for value in obj.get_component_order()]
            if (
                dragged_component_id not in before
                or target_component_id not in before
                or dragged_component_id == target_component_id
            ):
                return []
            remaining = [
                component_id for component_id in before
                if component_id != dragged_component_id
            ]
            target_index = remaining.index(target_component_id)
            insertion_index = target_index + (1 if insert_after else 0)
            after = (
                remaining[:insertion_index]
                + [dragged_component_id]
                + remaining[insertion_index:]
            )
            if after != before:
                changes.append((object_id, tuple(before), tuple(after)))
        return changes

    def _can_reorder_selected_components(
        object_ids,
        dragged_component_ids,
        target_component_ids,
        insert_after: bool,
    ):
        return bool(
            _component_drag_reorder_changes(
                object_ids,
                dragged_component_ids,
                target_component_ids,
                insert_after,
            )
        )

    def _reorder_selected_components(
        object_ids,
        dragged_component_ids,
        target_component_ids,
        insert_after: bool,
    ):
        return _execute_component_reorder(
            _component_drag_reorder_changes(
                object_ids,
                dragged_component_ids,
                target_component_ids,
                insert_after,
            ),
            "Reorder Components",
        )

    def _remove_selected_components(explicit_target=None):
        entries = _selected_component_entries(explicit_target)
        if not entries or not _can_remove_selected_components(explicit_target):
            return False
        from Infernux.engine.interaction import (
            SelectionService,
            SelectionSnapshot,
            SelectionTarget,
        )
        before_selection = SelectionService.instance().snapshot
        object_ids = list(dict.fromkeys(int(entry[1].id) for entry in entries))
        removed_targets = {entry[0] for entry in entries}
        if explicit_target is not None and not any(
            target in removed_targets for target in before_selection.targets
        ):
            after_selection = before_selection
        else:
            after_targets = tuple(
                SelectionTarget.scene_object(object_id) for object_id in object_ids
            )
            primary_object_id = (
                before_selection.primary.component_ids()[0]
                if before_selection.primary in removed_targets
                else object_ids[-1]
            )
            primary_target = SelectionTarget.scene_object(primary_object_id)
            after_selection = SelectionSnapshot.create(
                after_targets,
                owner_id="inspector",
                primary=primary_target,
                anchor=after_targets[0],
            )
        if not _component_commands().remove_many(
            [(obj, comp) for _target, obj, comp, _type_name, _is_native in entries],
            before_selection=before_selection,
            after_selection=after_selection,
            description=f"Remove {len(entries)} Components",
        ):
            return False
        _invalidate()
        return True

    def _can_open_selected_script(explicit_target=None):
        entry = _primary_component_entry(explicit_target)
        return bool(entry is not None and not entry[4] and _get_script_path(entry[2]))

    def _open_selected_script(explicit_target=None):
        entry = _primary_component_entry(explicit_target)
        if entry is None or entry[4]:
            return False
        script_path = _get_script_path(entry[2])
        if not script_path:
            return False
        from Infernux.engine.ui import project_utils

        project_utils.open_file_with_system(
            script_path,
            project_root=ctx.project_path,
        )
        return True

    from types import SimpleNamespace

    ctx.bs._inspector_component_actions = SimpleNamespace(
        can_copy=_can_copy_selected_component,
        copy=_copy_selected_component,
        can_paste_values=_can_paste_selected_values,
        paste_values=_paste_selected_values,
        can_paste_as_new=_can_paste_selected_as_new,
        paste_as_new=_paste_selected_as_new,
        paste_default=_paste_selected_default,
        can_remove=_can_remove_selected_components,
        remove=_remove_selected_components,
        can_reset=_can_reset_selected_components,
        reset=_reset_selected_components,
        can_move_up=lambda target=None: _can_move_selected_components(-1, target),
        move_up=lambda target=None: _move_selected_components(-1, target),
        can_move_down=lambda target=None: _can_move_selected_components(1, target),
        move_down=lambda target=None: _move_selected_components(1, target),
        can_reorder=_can_reorder_selected_components,
        reorder=_reorder_selected_components,
        can_open_script=_can_open_selected_script,
        open_script=_open_selected_script,
        can_set_enabled=ctx.can_set_component_enabled,
        set_enabled=ctx.set_component_enabled,
    )

    def _render_component_context_menu(ctx_arg, obj_id, type_name, comp_id, is_native):
        from Infernux.engine.interaction import (
            ContextMenuBuilder,
            ContextMenuCommand,
        )

        registry = ctx.bs.interaction_core.commands
        payload = {
            "object_id": int(obj_id),
            "component_id": int(comp_id),
            "type_name": str(type_name or ""),
            "is_native": bool(is_native),
        }
        result = ContextMenuBuilder(registry).render(
            ctx_arg,
            (
                ContextMenuCommand(
                    "component.open_script",
                    label=_t("inspector.show_script"),
                    hide_when_disabled=True,
                ),
                ContextMenuCommand(
                    "component.reset",
                    label=_t("inspector.reset"),
                    separator_before=True,
                ),
                ContextMenuCommand(
                    "component.move_up",
                    label=_t("inspector.move_up"),
                ),
                ContextMenuCommand(
                    "component.move_down",
                    label=_t("inspector.move_down"),
                ),
                ContextMenuCommand(
                    "component.copy_properties",
                    label=_t("inspector.copy_properties"),
                    separator_before=True,
                ),
                ContextMenuCommand(
                    "component.paste_as_new",
                    label=_t("inspector.paste_as_new"),
                ),
                ContextMenuCommand(
                    "component.paste_properties",
                    label=_t("inspector.paste_properties"),
                ),
                ContextMenuCommand(
                    "component.remove",
                    label=_t("inspector.remove"),
                    separator_before=True,
                ),
            ),
            payload=payload,
        )
        return bool(
            result is not None
            and result.result.accepted
            and result.command.spec.command_id == "component.remove"
        )

    ip.render_component_context_menu = _render_component_context_menu


# ═══════ Add / remove / script-drop ════════════════════════════

def _wire_add_remove_and_drop(ctx):
    """Wire add_component and handle_script_drop."""
    ip = ctx.ip
    engine = ctx.engine
    SelectionService = ctx.SelectionService
    SceneManager = ctx.SceneManager
    _invalidate = ctx.invalidate_component_cache
    _bump = ctx._bump_inspector_values

    ip.get_add_component_entries = _get_add_component_entries

    def _selected_object():
        primary = SelectionService.instance().primary_scene_object_id()
        if not primary:
            return None
        _scene, obj = ctx.resolve_scene_object(primary)
        return obj

    def _execute_add_transaction(
        obj,
        type_name,
        *,
        python_instance=None,
        target_component_id=0,
        insert_after=False,
        insert_at_start=False,
    ):
        try:
            _component_commands().add(
                obj,
                type_name,
                python_instance=python_instance,
                target_component_id=int(target_component_id or 0),
                insert_after=bool(insert_after),
                insert_at_start=bool(insert_at_start),
                description=f"Add {type_name}",
            )
        except ValueError as exc:
            Debug.log_warning(str(exc))
            return False
        _invalidate()
        _bump()
        Debug.log_internal(f"Added component: {type_name}")
        return True

    def _add_component(
        type_name_or_path,
        is_native,
        script_path,
        target_component_id=0,
        insert_after=False,
        insert_at_start=False,
    ):
        obj = _selected_object()
        if obj is None:
            return False
        if is_native:
            return _execute_add_transaction(
                obj,
                type_name_or_path,
                target_component_id=target_component_id,
                insert_after=insert_after,
                insert_at_start=insert_at_start,
            )
        elif not script_path:
            return _execute_add_transaction(
                obj,
                type_name_or_path,
                target_component_id=target_component_id,
                insert_after=insert_after,
                insert_at_start=insert_at_start,
            )
        else:
            adb = engine.get_asset_database()
            instance = _load_script_component(script_path, adb)
            if instance is None:
                return False
            return _execute_add_transaction(
                obj,
                instance.type_name,
                python_instance=instance,
                target_component_id=target_component_id,
                insert_after=insert_after,
                insert_at_start=insert_at_start,
            )

    def _can_add_component(
        type_name_or_path,
        is_native,
        script_path,
        target_component_id=0,
        insert_after=False,
        insert_at_start=False,
    ):
        del script_path
        del insert_after
        obj = _selected_object()
        if obj is None or not str(type_name_or_path or "").strip():
            return False
        target_component_id = int(target_component_id or 0)
        insert_at_start = bool(insert_at_start)
        if target_component_id and insert_at_start:
            return False
        if target_component_id and target_component_id not in {
            int(value) for value in obj.get_component_order()
        }:
            return False
        if is_native:
            return not tuple(obj.get_add_component_blockers(type_name_or_path))
        return True

    actions = ctx.bs._inspector_component_actions
    actions.can_add = _can_add_component
    actions.add = _add_component

# ═══════ Asset / file preview ══════════════════════════════════

def _wire_asset_preview(ctx):
    """Wire asset inspector and file preview callbacks."""
    ip = ctx.ip
    _t = ctx._t

    def _render_asset_inspector(ctx_arg, file_path, category):
        from Infernux.engine.ui.asset_details_renderer import render_asset_inspector
        try:
            render_asset_inspector(ctx_arg, ip, file_path, category)
        except Exception as exc:
            Debug.log_error(f"Asset inspector render failed for '{file_path}': {exc}")

    ip.render_asset_inspector = _render_asset_inspector

    def _render_file_preview(ctx_arg, file_path):
        import os
        if os.path.isdir(file_path):
            ctx_arg.label(_t("inspector.folder_label").format(name=os.path.basename(file_path)))
            ctx_arg.separator()
            ctx_arg.label(_t("inspector.path_label").format(path=file_path))
        else:
            ctx_arg.label(_t("inspector.file_label").format(name=os.path.basename(file_path)))
            ctx_arg.separator()
            ctx_arg.label(_t("inspector.no_previewer"))

    ip.render_file_preview = _render_file_preview


# ═══════ Prefab, tags, window manager ══════════════════════════

def _wire_prefab_and_misc(ctx):
    """Wire prefab info/actions, tags, layers, and window manager."""
    ip = ctx.ip
    engine = ctx.engine
    bs = ctx.bs
    _t = ctx._t
    SceneManager = ctx.SceneManager

    from Infernux.lib import InspectorPrefabInfo

    def _get_prefab_info(obj_id):
        pinfo = InspectorPrefabInfo()
        _scene, obj = ctx.resolve_scene_object(obj_id)
        if obj is None:
            return pinfo
        guid = getattr(obj, 'prefab_guid', '') or ''
        if not guid:
            return pinfo
        from Infernux.engine.prefab_overrides import (
            compute_overrides,
            resolve_prefab_instance_root,
        )
        root = resolve_prefab_instance_root(obj)
        adb = engine.get_asset_database()
        path = adb.get_path_from_guid(guid) if adb else ""
        if root is not None and path:
            pinfo.override_count = len(compute_overrides(root, path, adb))
        return pinfo

    ip.get_prefab_info = _get_prefab_info

    from Infernux.lib import TagLayerManager
    ip.get_all_tags = lambda: TagLayerManager.instance().get_all_tags()
    ip.get_all_layers = lambda: TagLayerManager.instance().get_all_layers()


# ═══════ Main entry point ══════════════════════════════════════

def wire_inspector_callbacks(bs: EditorBootstrap) -> None:
    """Wire C++ InspectorPanel callbacks to Python managers."""
    ip = bs.inspector_panel
    engine = bs.engine
    from Infernux.engine.i18n import t as _t
    from Infernux.engine.ui import inspector_support as _inspector_support
    from Infernux.engine.interaction import SelectionService
    from Infernux.lib import SceneManager, InspectorObjectInfo, InspectorComponentInfo
    from Infernux.engine.ui.inspector_snapshot import (
        InspectorSnapshotService,
        InspectorTarget,
        refresh_visible_play_transforms,
        sync_selected_transforms_from_native_serial,
    )
    from Infernux.components.component import InxComponent

    ctx = _Ctx()
    ctx.ip = ip
    ctx.engine = engine
    ctx.bs = bs
    ctx._t = _t
    ctx._inspector_support = _inspector_support
    ctx._bump_inspector_values = _inspector_support.bump_inspector_value_generation
    ctx._record_profile_count = _inspector_support.record_inspector_profile_count
    ctx._record_profile_timing = _inspector_support.record_inspector_profile_timing
    ctx.SceneManager = SceneManager
    ctx.InspectorObjectInfo = InspectorObjectInfo
    ctx.InspectorComponentInfo = InspectorComponentInfo
    ctx.InxComponent = InxComponent
    ctx.SelectionService = SelectionService
    ctx.InspectorTarget = InspectorTarget
    ctx.inspector_snapshot_service = InspectorSnapshotService.instance()
    ctx.project_path = bs.project_path

    bs.interaction_core.scene_objects.set_change_publisher(
        _inspector_support.bump_inspector_value_generation
    )

    ip.translate = _t

    from Infernux.engine.interaction import CommandSource

    def _command_payload(command_id: str, argument: str):
        if command_id in {
            "scene.set_object_property",
            "scene.set_transforms",
            "component.reorder",
        }:
            import json

            try:
                payload = json.loads(str(argument or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}
        if command_id == "component.add":
            import os
            import json

            try:
                raw = json.loads(str(argument or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                return {}
            if not isinstance(raw, dict):
                return {}
            type_name = str(raw.get("type_name", "") or "").strip()
            script_path = str(raw.get("script_path", "") or "").strip()
            if not type_name and script_path:
                type_name = os.path.splitext(os.path.basename(script_path))[0]
            return {
                "type_name": type_name,
                "is_native": bool(raw.get("is_native", False)),
                "script_path": script_path,
                "target_component_id": raw.get("target_component_id", 0),
                "insert_after": bool(raw.get("insert_after", False)),
                "insert_at_start": bool(raw.get("insert_at_start", False)),
            }
        if command_id == "component.set_enabled":
            import json

            try:
                raw = json.loads(str(argument or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                return {}
            if not isinstance(raw, dict):
                return {}
            return {
                "targets": raw.get("targets", ()),
                "enabled": bool(raw.get("enabled", False)),
                "is_native": bool(raw.get("is_native", False)),
            }
        if command_id.startswith("prefab."):
            try:
                return {"object_id": int(argument)}
            except (TypeError, ValueError):
                return {}
        if command_id == "window.open":
            target_id = str(argument or "").strip()
            return {"target_id": target_id} if target_id else {}
        return {}

    def _execute_inspector_command(command_id, source, argument):
        return bs.interaction_core.commands.execute(
            command_id,
            source=CommandSource(source),
            payload=_command_payload(command_id, argument),
        ).accepted

    def _can_execute_inspector_command(command_id, argument):
        return bs.interaction_core.commands.can_execute(
            command_id,
            bs.interaction_core.commands.context(
                CommandSource.CONTEXT_MENU,
                _command_payload(command_id, argument),
            ),
        )

    ip.execute_command = _execute_inspector_command
    ip.can_execute_command = _can_execute_inspector_command

    selection = SelectionService.instance()
    ip.is_multi_selection = lambda: len(selection.scene_object_ids()) > 1
    ip.get_selected_ids = lambda: list(selection.scene_object_ids())
    ip.get_value_generation = _inspector_support.get_inspector_value_generation
    ip.consume_component_body_profile = _inspector_support.consume_inspector_profile_metrics

    # New native builds consume one immutable four-layer packet per visible
    # Inspector frame. Keep the legacy generation callback above so source
    # Python remains runnable against an older installed extension.
    try:
        from Infernux.lib import InspectorRevisionSnapshot as _NativeRevisionSnapshot
    except ImportError:
        _NativeRevisionSnapshot = None

    if _NativeRevisionSnapshot is not None and hasattr(ip, "get_revision_snapshot"):
        snapshot_service = ctx.inspector_snapshot_service
        from Infernux.engine.runtime_change_journal import runtime_change_journal

        inspector_journal = runtime_change_journal()
        inspector_change_cursor = inspector_journal.create_cursor(
            "editor-inspector-snapshot",
            start_at_current=True,
        )
        inspector_transform_serial = [None]

        def _inspector_is_playing():
            try:
                from Infernux.engine.play_mode import PlayModeManager

                play_manager = PlayModeManager.instance()
                return play_manager is not None and bool(play_manager.is_playing)
            except (ImportError, AttributeError, ReferenceError, RuntimeError):
                return False

        def _get_revision_snapshot():
            snapshot_service.consume_changes(
                inspector_journal.consume(inspector_change_cursor, flush=False)
            )
            object_ids = tuple(int(value) for value in selection.scene_object_ids())
            if _inspector_is_playing():
                refresh_visible_play_transforms(object_ids, playing=True)
            else:
                getter = getattr(SceneManager.instance(), "get_global_transform_serial", None)
                if callable(getter):
                    inspector_transform_serial[0] = (
                        sync_selected_transforms_from_native_serial(
                            int(getter()),
                            object_ids,
                            previous_serial=inspector_transform_serial[0],
                        )
                    )
            selected_file = str(ip.get_selected_file() or "")
            if len(object_ids) == 1:
                target = InspectorTarget.scene_object(object_ids[0])
                snapshot_service.set_active_target(target)
                packet = snapshot_service.snapshot(target)
            elif object_ids:
                targets = tuple(
                    InspectorTarget.scene_object(object_id)
                    for object_id in object_ids
                )
                packet = snapshot_service.aggregate(targets)
                snapshot_service.set_active_target(packet.target)
                packet = snapshot_service.aggregate(targets)
            elif selected_file:
                target = InspectorTarget.asset(selected_file)
                snapshot_service.set_active_target(target)
                packet = snapshot_service.snapshot(target)
            else:
                target = InspectorTarget.none()
                snapshot_service.set_active_target(target)
                packet = snapshot_service.snapshot(target)

            native = _NativeRevisionSnapshot()
            native.target = packet.target_revision
            native.schema = packet.schema_revision
            native.value = packet.value_revision
            native.preview = packet.preview_revision
            return native

        ip.get_revision_snapshot = _get_revision_snapshot

    ip.on_panel_focused = bs.window_manager.native_panel_focus_callback(
        "inspector",
        view_id="inspector",
        source_instance=ip,
    )

    _wire_cache_init(ctx)
    _wire_component_list(ctx)
    _wire_icons_and_body(ctx)
    _wire_object_info(ctx)
    _wire_transform(ctx)
    _wire_clipboard_and_context(ctx)
    _wire_add_remove_and_drop(ctx)
    _wire_asset_preview(ctx)

    from Infernux.engine.bootstrap_inspector._materials import wire_material_sections
    wire_material_sections(
        ip, _t, engine, _inspector_support,
        ctx.get_cached_component_maps, ctx.current_scene_and_versions,
        ctx.material_section_cache)

    _wire_prefab_and_misc(ctx)
