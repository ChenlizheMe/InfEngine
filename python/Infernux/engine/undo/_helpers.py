"""Scene-graph helpers shared by undo commands."""

from __future__ import annotations

from typing import Any, List, Optional

from Infernux.debug import Debug


def _get_active_scene():
    from Infernux.lib import SceneManager
    return SceneManager.instance().get_active_scene()


def _safe_attr(target: Any, name: str, default=None):
    try:
        return getattr(target, name, default)
    except (AttributeError, ReferenceError, RuntimeError):
        return default


def _game_object_id_of(target: Any) -> int:
    goid = _safe_attr(target, 'game_object_id') or 0
    if not goid:
        go = _safe_attr(target, 'game_object')
        if go is not None:
            goid = _safe_attr(go, 'id', 0) or 0
    if not goid:
        goid = _safe_attr(target, 'id', 0) or 0
    return goid


def _comp_type_name_of(target: Any) -> str:
    tn = _safe_attr(target, 'type_name')
    if tn:
        return str(tn)
    if (_safe_attr(target, 'id', 0)
            and not _safe_attr(target, 'component_id', 0)
            and _safe_attr(target, 'game_object') is None):
        return "GameObject"
    return type(target).__name__


def _stable_target_id(target: Any) -> int:
    for attr in ("component_id", "id"):
        val = getattr(target, attr, None)
        if val is not None and val != 0:
            return int(val)
    return id(target)


def _resolve_target(stored_ref: Any, game_object_id: int,
                    comp_type_name: str) -> Any:
    if not game_object_id or not comp_type_name:
        return stored_ref
    scene = _get_active_scene()
    if not scene:
        return None
    obj = scene.find_by_id(game_object_id)
    if obj is None:
        return None
    if comp_type_name == "GameObject":
        return obj
    if comp_type_name == "Transform":
        return getattr(obj, "transform", None)
    component_id = int(getattr(stored_ref, "component_id", 0) or 0)
    if component_id:
        for component in (obj.get_components() or ()):
            if int(getattr(component, "component_id", 0) or 0) == component_id:
                return component
        for component in (obj.get_py_components() or ()):
            if int(getattr(component, "component_id", 0) or 0) == component_id:
                return component
    live = obj.get_component(comp_type_name)
    if live is not None:
        return live
    for pc in obj.get_py_components():
        if type(pc).__name__ == comp_type_name:
            return pc
    return None


_resolve_live_ref = _resolve_target


def _find_live_native_component(obj, type_name: str):
    if hasattr(obj, 'get_component'):
        c = obj.get_component(type_name)
        if c is not None:
            return c
    for c in obj.get_components():
        if getattr(c, 'type_name', None) == type_name:
            return c
    return None


def _bump_inspector_structure():
    try:
        from Infernux.engine.ui.inspector_support import bump_component_structure_version
        bump_component_structure_version()
    except ImportError:
        # Inspector module not available (e.g. headless / player mode) — no-op.
        pass


def _inspector_snapshot_revision() -> int:
    try:
        from Infernux.engine.ui.inspector_snapshot import InspectorSnapshotService

        return InspectorSnapshotService.instance().revision()
    except ImportError:
        return 0


def _bump_inspector_values(snapshot_baseline: int | None = None):
    try:
        from Infernux.engine.ui.inspector_support import bump_inspector_value_generation
        from Infernux.engine.ui.inspector_snapshot import InspectorSnapshotService

        publish_snapshot = (
            snapshot_baseline is None
            or InspectorSnapshotService.instance().revision() == snapshot_baseline
        )
        bump_inspector_value_generation(publish_snapshot=publish_snapshot)
    except ImportError:
        # Inspector module not available (e.g. headless / player mode) — no-op.
        pass


def _require_scene_object(object_id: int, label: str):
    scene = _get_active_scene()
    if not scene:
        raise RuntimeError(f"[Undo] {label}: no scene")
    obj = scene.find_by_id(object_id)
    if not obj:
        raise RuntimeError(f"[Undo] {label}: object {object_id} not found")
    return scene, obj


def _notify_gizmos_scene_changed():
    from Infernux.gizmos.collector import notify_scene_changed
    notify_scene_changed()


def _invalidate_builtin_wrapper(comp_ref):
    if not comp_ref:
        # Already-destroyed native component: nothing left to invalidate.
        return
    comp_id = comp_ref.component_id
    from Infernux.components.builtin_component import BuiltinComponent
    wrapper = BuiltinComponent._wrapper_cache.get(comp_id)
    if wrapper is not None:
        wrapper._invalidate_native_binding()


def _invalidate_builtin_wrappers_for_tree(obj):
    try:
        from Infernux.components.builtin_component import BuiltinComponent
    except ImportError:
        # BuiltinComponent module not yet available — wrappers will lazily
        # rebuild themselves the next time they are queried.
        return
    cache = BuiltinComponent._wrapper_cache
    pending = [obj]
    while pending:
        current = pending.pop()
        if not current:
            # Skip natives already destroyed while walking the tree.
            continue
        for comp in current.get_components():
            comp_id = getattr(comp, "component_id", 0) or 0
            if comp_id:
                wrapper = cache.get(comp_id)
                if wrapper is not None:
                    wrapper._invalidate_native_binding()
        pending.extend(current.get_children())


def _destroy_game_object_immediately(scene, obj):
    if scene is None or obj is None:
        return
    _invalidate_builtin_wrappers_for_tree(obj)
    scene.destroy_game_object(obj)
    if hasattr(scene, "process_pending_destroys"):
        scene.process_pending_destroys()
    _bump_inspector_structure()
    _notify_gizmos_scene_changed()


def _invalidate_canvas_caches(go):
    if go is None:
        return
    from Infernux.ui import UICanvas
    cur = go
    while cur is not None:
        for comp in cur.get_py_components():
            if isinstance(comp, UICanvas):
                comp.invalidate_element_cache()
                return
        cur = cur.get_parent()


def _preserve_ui_world_position(obj, new_parent):
    from Infernux.ui.inx_ui_screen_component import InxUIScreenComponent, clear_rect_cache
    from Infernux.ui import UICanvas

    ui_comp = None
    for comp in obj.get_py_components():
        if isinstance(comp, InxUIScreenComponent):
            ui_comp = comp
            break
    if ui_comp is None:
        return

    def _find_canvas(go):
        while go is not None:
            for c in go.get_py_components():
                if isinstance(c, UICanvas):
                    return c
            go = go.get_parent()
        return None

    old_canvas = _find_canvas(obj.get_parent() or obj)
    if old_canvas is None:
        return
    old_cw = float(old_canvas.reference_width)
    old_ch = float(old_canvas.reference_height)
    old_abs_x, old_abs_y, _w, _h = ui_comp.get_rect(old_cw, old_ch)

    new_canvas = _find_canvas(new_parent) if new_parent is not None else None
    ncw = float(new_canvas.reference_width) if new_canvas is not None else old_cw
    nch = float(new_canvas.reference_height) if new_canvas is not None else old_ch

    if new_parent is not None:
        new_parent_ui = None
        for c in new_parent.get_py_components():
            if isinstance(c, InxUIScreenComponent):
                new_parent_ui = c
                break
        if new_parent_ui is not None:
            npx, npy, npw, nph = new_parent_ui.get_rect(ncw, nch)
        else:
            npx, npy, npw, nph = 0.0, 0.0, ncw, nch
    else:
        npx, npy, npw, nph = 0.0, 0.0, ncw, nch

    anchor_x, anchor_y = ui_comp._anchor_origin(npw, nph)
    ui_comp.x = old_abs_x - npx - anchor_x
    ui_comp.y = old_abs_y - npy - anchor_y
    clear_rect_cache(-1)
