"""Main wiring function for the C++ InspectorPanel."""
from __future__ import annotations

import copy
import time as _time
from typing import TYPE_CHECKING

from Infernux.debug import Debug
from Infernux.engine.bootstrap_inspector._helpers import (
    _can_remove_component,
    _get_add_component_entries,
    _get_components_safe,
    _get_py_components_safe,
    _load_script_component,
    _remove_component_impl,
)

if TYPE_CHECKING:
    from Infernux.engine.bootstrap import EditorBootstrap


class _Ctx:
    """Thin namespace shared across inspector wiring helpers."""


# ═══════ Cache initialisation ═══════════════════════════════════

def _wire_cache_init(ctx):
    """Create component and material caches, invalidation helpers."""
    _component_cache = {
        "object_id": 0, "scene_version": -1, "structure_version": -1,
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
            items=[], native_map={}, py_map={})
        _invalidate_material_section_cache()

    ctx.invalidate_component_cache = _invalidate_component_cache

    def _current_scene_and_versions():
        scene = ctx.SceneManager.instance().get_active_scene()
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

    def _native_wrapper_is_dead(component):
        """Cheaply reject wrappers invalidated by a scene/component rebuild.

        Calling ``is_valid`` here would resolve a native handle for every
        component on every Inspector frame.  The invalidation path already
        clears ``_cpp_component`` and marks the wrapper destroyed, so these
        local fields are sufficient to decide whether the cached map must be
        rebuilt.
        """
        return (
            bool(getattr(component, "_is_builtin_component_wrapper", False))
            and (
                getattr(component, "_cpp_component", None) is None
                or bool(getattr(component, "_is_destroyed", False))
            )
        )

    def _is_py_entry(component):
        return isinstance(component, InxComponent) or hasattr(component, 'get_py_component')

    def _get_component_payload(obj_id):
        scene, scene_ver, struct_ver = _versions()
        if (
            _component_cache["object_id"] == obj_id
            and _component_cache["scene_version"] == scene_ver
            and _component_cache["structure_version"] == struct_ver
        ):
            items = _component_cache["items"]
            native_map = _component_cache["native_map"]
            py_map = _component_cache["py_map"]
            stale = False
            for item in items:
                comp = native_map.get(item.component_id) if item.is_native else py_map.get(item.component_id)
                if comp is None or (item.is_native and _native_wrapper_is_dead(comp)):
                    stale = True
                    break
                item.enabled = bool(getattr(comp, 'enabled', True))
                if not item.is_native:
                    item.is_broken = bool(getattr(comp, '_is_broken', False))
                    item.broken_error = (
                        getattr(comp, '_broken_error', '') or ''
                        if item.is_broken else ''
                    )
            if not stale:
                return scene, items, native_map, py_map

        obj = scene.find_by_id(obj_id) if scene else None
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
                # CastToPython already resolved PyComponentProxy to the
                # actual Python instance, so *comp* IS the Python component.
                py_comp = comp
                cid = getattr(py_comp, 'component_id', id(py_comp))
                ci = InspectorComponentInfo()
                ci.type_name = getattr(py_comp, 'type_name', type(py_comp).__name__)
                ci.component_id = cid
                ci.enabled = bool(getattr(py_comp, 'enabled', True))
                ci.is_native = False
                ci.is_script = True
                ci.is_broken = bool(getattr(py_comp, '_is_broken', False))
                ci.broken_error = (
                    getattr(py_comp, '_broken_error', '') or ''
                    if ci.is_broken else ''
                )
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
            items=items, native_map=native_map, py_map=py_map)
        return scene, items, native_map, py_map

    def _get_cached_maps(obj_id):
        scene, scene_ver, struct_ver = _versions()
        if (
            _component_cache["object_id"] == obj_id
            and _component_cache["scene_version"] == scene_ver
            and _component_cache["structure_version"] == struct_ver
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
    """Wire get_object_info and set_object_property."""
    SceneManager = ctx.SceneManager
    InspectorObjectInfo = ctx.InspectorObjectInfo
    ip = ctx.ip
    _bump = ctx._bump_inspector_values

    def _get_object_info(obj_id):
        info = InspectorObjectInfo()
        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(obj_id) if scene else None
        if obj is None:
            return info
        info.name = obj.name
        info.active = obj.active
        info.tag = getattr(obj, 'tag', 'Untagged')
        info.layer = getattr(obj, 'layer', 0)
        info.prefab_guid = getattr(obj, 'prefab_guid', '') or ''
        info.hide_transform = getattr(obj, 'hide_transform', False)
        return info

    ip.get_object_info = _get_object_info

    def _set_object_property(obj_id, prop_name, value_str):
        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(obj_id) if scene else None
        if obj is None:
            return
        from Infernux.engine.undo import UndoManager, SetPropertyCommand
        mgr = UndoManager.instance()
        old_val = getattr(obj, prop_name, None)
        if prop_name == "active":
            new_val = value_str.lower() in ("true", "1")
        elif prop_name in ("name", "tag"):
            new_val = value_str
        elif prop_name == "layer":
            new_val = int(value_str)
        else:
            new_val = value_str
        if mgr:
            mgr.execute(SetPropertyCommand(obj, prop_name, old_val, new_val,
                                           f"Set {prop_name}"))
        else:
            setattr(obj, prop_name, new_val)
            _bump()
        if prop_name == "active":
            actual = getattr(obj, prop_name, None)
            if actual != new_val:
                Debug.log_warning(
                    f"[Inspector] SetActive failed: old={old_val}, "
                    f"requested={new_val}, actual={actual}, obj={obj_id}")

    ip.set_object_property = _set_object_property


# ═══════ Transform ═════════════════════════════════════════════

def _wire_transform(ctx):
    """Wire transform get/set callbacks."""
    SceneManager = ctx.SceneManager
    ip = ctx.ip
    _bump = ctx._bump_inspector_values

    from Infernux.lib import InspectorTransformData

    def _get_transform_data(obj_id):
        td = InspectorTransformData()
        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(obj_id) if scene else None
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

    from Infernux.engine.undo import (
        UndoManager, InspectorSnapshotCommand,
        snapshot_live_transform, restore_live_transform,
    )

    def _set_transform_data(obj_id, td):
        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(obj_id) if scene else None
        if obj is None:
            return
        trans = obj.get_transform()
        if trans is None:
            return

        mgr = UndoManager.instance()
        old_snap = None
        if mgr and mgr.enabled:
            try:
                old_snap = snapshot_live_transform(obj_id)
            except Exception:
                old_snap = None

        from Infernux.lib import Vector3
        trans.local_position = Vector3(td.px, td.py_, td.pz)
        trans.local_euler_angles = Vector3(td.rx, td.ry, td.rz)
        trans.local_scale = Vector3(td.sx, td.sy, td.sz)

        if mgr and mgr.enabled and old_snap is not None:
            try:
                new_snap = snapshot_live_transform(obj_id)
            except Exception:
                new_snap = None
            if new_snap is not None and new_snap != old_snap:
                def _restore(snap, _oid=obj_id):
                    restore_live_transform(_oid, snap)
                    _bump()
                cmd = InspectorSnapshotCommand(
                    f"transform:{obj_id}", old_snap, new_snap,
                    _restore, "Edit Transform")
                mgr.record(cmd)

        _bump()

    ip.set_transform_data = _set_transform_data


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
            if tid != 0:
                _icon_cache[key] = tid
            else:
                all_ready = False
        _icons_loaded[0] = all_ready

    def _get_component_icon_id(type_name, is_script):
        _ensure_icons()
        tid = _icon_cache.get(type_name.lower(), 0)
        if tid == 0 and is_script:
            tid = _icon_cache.get("script", 0)
        return tid

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
        if _profile_enabled:
            _record_count("bodyPyCheck_count")
        _t0 = _time.perf_counter() if _profile_enabled else 0.0
        _script_err = None
        if getattr(comp, '_is_broken', False):
            _script_err = getattr(comp, '_broken_error', '') or 'Script failed to load'
        else:
            _py_guid = getattr(comp, '_script_guid', None)
            adb = engine.get_asset_database()
            if _py_guid and adb:
                from Infernux.components.script_loader import get_script_error_by_path
                _py_path = adb.get_path_from_guid(_py_guid)
                if _py_path:
                    _script_err = get_script_error_by_path(_py_path)
        if _profile_enabled:
            _record_timing("bodyPyCheck", (_time.perf_counter() - _t0) * 1000.0)
        if _script_err:
            from Infernux.engine.ui.theme import Theme, ImGuiCol
            ctx_arg.push_style_color(ImGuiCol.Text, *Theme.ERROR_TEXT)
            ctx_arg.text_wrapped(_script_err)
            ctx_arg.pop_style_color(1)
        else:
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

    def _set_component_enabled(obj_id, comp_id, new_enabled, is_native):
        comp = ctx.resolve_component(obj_id, comp_id, is_native)
        if comp is None:
            return
        from Infernux.engine.undo import UndoManager, SetPropertyCommand
        mgr = UndoManager.instance()
        old_val = comp.enabled
        if mgr:
            mgr.execute(SetPropertyCommand(
                comp, "enabled", old_val, new_enabled,
                f"Toggle {getattr(comp, 'type_name', '?')}"))
        else:
            comp.enabled = new_enabled
            _bump()
        for item in _component_cache["items"]:
            if item.component_id == comp_id:
                item.enabled = bool(new_enabled)
                break

    ip.set_component_enabled = _set_component_enabled


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

def _wire_clipboard_and_context(ctx):
    """Wire clipboard operations and component context menu."""
    ip = ctx.ip
    engine = ctx.engine
    _resolve = ctx.resolve_component
    _invalidate = ctx.invalidate_component_cache
    _bump = ctx._bump_inspector_values
    _t = ctx._t
    SceneManager = ctx.SceneManager

    _comp_clipboard = {
        "type_name": "", "is_native": True, "script_guid": "", "type_guid": "", "payload": None,
    }

    def _copy_to_clipboard(comp, type_name, is_native):
        _comp_clipboard["type_name"] = type_name
        _comp_clipboard["is_native"] = is_native
        _comp_clipboard["script_guid"] = getattr(comp, '_script_guid', '') or ''
        _comp_clipboard["type_guid"] = comp.__class__._get_type_guid() if not is_native else ""
        try:
            if is_native and hasattr(comp, "serialize_document"):
                _comp_clipboard["payload"] = comp.serialize_document()
            elif hasattr(comp, "_serialize_fields_document"):
                _comp_clipboard["payload"] = _python_component_clipboard_document(comp)
            else:
                _comp_clipboard["payload"] = None
        except Exception:
            _comp_clipboard["payload"] = None

    def _has_clip():
        return bool(_comp_clipboard["type_name"] and _comp_clipboard["payload"] is not None)

    def _can_paste_values(comp, type_name, is_native):
        if not _has_clip():
            return False
        if not (_comp_clipboard["type_name"] == type_name and
                _comp_clipboard["is_native"] == is_native):
            return False
        if is_native:
            return True
        return (
            _comp_clipboard["script_guid"] == (getattr(comp, "_script_guid", "") or "")
            and _comp_clipboard["type_guid"] == comp.__class__._get_type_guid()
        )

    def _paste_as_new(obj):
        tn = _comp_clipboard["type_name"]
        native = _comp_clipboard["is_native"]
        payload = _comp_clipboard["payload"]
        guid = _comp_clipboard["script_guid"]
        type_guid = _comp_clipboard["type_guid"]
        if native:
            result = obj.add_component(tn)
            if result and payload is not None and hasattr(result, "deserialize_document"):
                try:
                    if not result.deserialize_document(payload):
                        raise RuntimeError(f"Failed to paste native component '{tn}'")
                except Exception as _exc:
                    Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
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
            before_ids = {getattr(item, "component_id", 0) for item in obj.get_components()}
            attached = obj.add_py_component(instance)
            if attached is None:
                instance._call_on_destroy()
                Debug.log_error(f"Cannot paste: failed to attach '{tn}'")
                return False
            attached._call_on_after_deserialize()
            from Infernux.engine.ui._inspector_undo import _record_add_component_compound
            _record_add_component_compound(
                obj,
                tn,
                attached,
                before_ids,
                is_py=True,
            )
        _invalidate()
        return True

    def _paste_values(comp, is_native):
        payload = _comp_clipboard["payload"]
        if payload is None:
            return
        if is_native and hasattr(comp, "deserialize_document"):
            try:
                if not comp.deserialize_document(payload):
                    raise RuntimeError("Failed to paste native component values")
            except Exception as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        elif hasattr(comp, "_deserialize_fields_document"):
            try:
                old_document = _python_component_clipboard_document(comp)
                new_document = copy.deepcopy(payload)
                new_document["__type_name__"] = type(comp).__name__
                from Infernux.engine.undo import (
                    PythonComponentDocumentCommand,
                    UndoManager,
                )
                manager = UndoManager.instance()
                if manager:
                    manager.execute(PythonComponentDocumentCommand(
                        comp,
                        old_document,
                        new_document,
                        f"Paste {type(comp).__name__} Properties",
                    ))
                else:
                    _apply_python_component_clipboard_document(comp, new_document)
                    from Infernux.engine.ui._inspector_undo import _notify_scene_modified
                    _notify_scene_modified()
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

    _project_path = ctx.project_path

    def _render_component_context_menu(ctx_arg, obj_id, type_name, comp_id, is_native):
        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(obj_id) if scene else None
        if obj is None:
            return False
        comp = _resolve(obj_id, comp_id, is_native)
        if comp is None:
            return False

        if not is_native:
            script_path = _get_script_path(comp)
            if script_path:
                if ctx_arg.selectable(_t("inspector.show_script")):
                    from Infernux.engine.ui import project_utils
                    project_utils.open_file_with_system(
                        script_path, project_root=_project_path)
                    ctx_arg.close_current_popup()
                    return False
                ctx_arg.separator()

        if ctx_arg.selectable(_t("inspector.copy_properties")):
            _copy_to_clipboard(comp, type_name, is_native)
            ctx_arg.close_current_popup()
            return False

        has_clip = _has_clip()
        if not has_clip:
            ctx_arg.begin_disabled()
        if ctx_arg.selectable(_t("inspector.paste_as_new")):
            _paste_as_new(obj)
            ctx_arg.close_current_popup()
            if not has_clip:
                ctx_arg.end_disabled()
            return False
        if not has_clip:
            ctx_arg.end_disabled()

        can_paste_v = _can_paste_values(comp, type_name, is_native)
        if not can_paste_v:
            ctx_arg.begin_disabled()
        if ctx_arg.selectable(_t("inspector.paste_properties")):
            _paste_values(comp, is_native)
            ctx_arg.close_current_popup()
            if not can_paste_v:
                ctx_arg.end_disabled()
            return False
        if not can_paste_v:
            ctx_arg.end_disabled()

        ctx_arg.separator()

        if ctx_arg.selectable(_t("inspector.remove")):
            if not _can_remove_component(obj, comp, type_name, is_native):
                return False
            if is_native:
                from Infernux.engine.undo import UndoManager, RemoveNativeComponentCommand
                mgr = UndoManager.instance()
                if mgr:
                    mgr.execute(RemoveNativeComponentCommand(obj.id, type_name, comp))
                    _invalidate()
                elif obj.remove_component(comp) is False:
                    return False
                else:
                    _invalidate()
            else:
                from Infernux.engine.undo import UndoManager, RemovePyComponentCommand
                mgr = UndoManager.instance()
                if mgr:
                    mgr.execute(RemovePyComponentCommand(obj.id, comp))
                    _invalidate()
                elif obj.remove_py_component(comp) is False:
                    return False
                else:
                    _invalidate()
            ctx_arg.close_current_popup()
            return True
        return False

    ip.render_component_context_menu = _render_component_context_menu


# ═══════ Add / remove / script-drop ════════════════════════════

def _wire_add_remove_and_drop(ctx):
    """Wire add_component, remove_component, and handle_script_drop."""
    ip = ctx.ip
    engine = ctx.engine
    SelectionManager = ctx.SelectionManager
    SceneManager = ctx.SceneManager
    _invalidate = ctx.invalidate_component_cache
    _bump = ctx._bump_inspector_values
    _resolve = ctx.resolve_component

    ip.get_add_component_entries = _get_add_component_entries

    def _add_component(type_name_or_path, is_native, script_path):
        sel = SelectionManager.instance()
        primary = sel.get_primary()
        if not primary:
            return
        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(primary) if scene else None
        if obj is None:
            return
        from Infernux.engine.ui.inspector_components import (
            _record_add_component_compound, _get_component_ids,
            _get_native_component_documents,
        )
        if is_native:
            # Block adding MeshRenderer when SpriteRenderer manages it.
            if type_name_or_path == "MeshRenderer":
                for c in _get_components_safe(obj):
                    if getattr(c, 'type_name', '') == 'SpriteRenderer':
                        Debug.log_warning(
                            "Cannot add MeshRenderer — "
                            "SpriteRenderer already manages the renderer.")
                        return
            # Block adding SpriteRenderer when MeshRenderer exists.
            if type_name_or_path == "SpriteRenderer":
                for c in _get_components_safe(obj):
                    if getattr(c, 'type_name', '') == 'MeshRenderer':
                        Debug.log_warning(
                            "Cannot add SpriteRenderer — "
                            "a MeshRenderer already exists. Remove it first.")
                        return
            before_documents = _get_native_component_documents(obj)
            before_ids = _get_component_ids(obj)
            result = obj.add_component(type_name_or_path)
            if result is not None:
                Debug.log_internal(f"Added component: {type_name_or_path}")
                _record_add_component_compound(
                    obj, type_name_or_path, result, before_ids, is_py=False,
                    before_documents=before_documents)
                _invalidate()
                _bump()
            else:
                Debug.log_error(f"Failed to add component: {type_name_or_path}")
        elif not script_path:
            _engine_py_map = {"RenderStack": None}
            try:
                from Infernux.renderstack.render_stack import RenderStack as _RS
                _engine_py_map["RenderStack"] = _RS
            except ImportError as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            comp_cls = _engine_py_map.get(type_name_or_path)
            if comp_cls is None:
                # Try engine Python-only components via registry
                from Infernux.components.registry import get_type
                comp_cls = get_type(type_name_or_path)
            if comp_cls is None:
                Debug.log_error(f"Unknown engine component: {type_name_or_path}")
                return
            if getattr(comp_cls, '_disallow_multiple_', False):
                for pc in _get_py_components_safe(obj):
                    if isinstance(pc, comp_cls):
                        Debug.log_warning(
                            f"Cannot add another '{comp_cls.__name__}' — "
                            f"only one per scene is allowed")
                        return
            instance = comp_cls()
            before_documents = _get_native_component_documents(obj)
            before_ids = _get_component_ids(obj)
            obj.add_py_component(instance)
            _record_add_component_compound(
                obj, comp_cls.__name__, instance, before_ids, is_py=True,
                before_documents=before_documents)
            _invalidate()
            _bump()
            Debug.log_internal(f"Added component {comp_cls.__name__}")
        else:
            adb = engine.get_asset_database()
            instance = _load_script_component(script_path, adb)
            if instance is None:
                return
            before_documents = _get_native_component_documents(obj)
            before_ids = _get_component_ids(obj)
            obj.add_py_component(instance)
            _record_add_component_compound(
                obj, instance.type_name, instance, before_ids, is_py=True,
                before_documents=before_documents)
            _invalidate()
            _bump()
            Debug.log_internal(f"Added component {instance.type_name}")

    ip.add_component = _add_component

    def _remove_component(obj_id, type_name, comp_id, is_native):
        return _remove_component_impl(
            obj_id, type_name, comp_id, is_native,
            _resolve, _can_remove_component, _invalidate, _bump)

    ip.remove_component = _remove_component

    def _handle_script_drop(script_path):
        sel = SelectionManager.instance()
        primary = sel.get_primary()
        if not primary:
            return
        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(primary) if scene else None
        if obj is None:
            return
        adb = engine.get_asset_database()
        instance = _load_script_component(script_path, adb)
        if instance is None:
            return
        from Infernux.engine.ui.inspector_components import (
            _record_add_component_compound, _get_component_ids,
            _get_native_component_documents,
        )
        before_documents = _get_native_component_documents(obj)
        before_ids = _get_component_ids(obj)
        obj.add_py_component(instance)
        _record_add_component_compound(
            obj, instance.type_name, instance, before_ids, is_py=True,
            before_documents=before_documents)
        _invalidate()
        _bump()

    ip.handle_script_drop = _handle_script_drop


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
    SelectionManager = ctx.SelectionManager
    _bump = ctx._bump_inspector_values
    _invalidate = ctx.invalidate_component_cache

    from Infernux.lib import InspectorPrefabInfo

    def _get_prefab_info(obj_id):
        pinfo = InspectorPrefabInfo()
        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(obj_id) if scene else None
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

    def _prefab_action(obj_id, action):
        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(obj_id) if scene else None
        if obj is None:
            return
        guid = getattr(obj, 'prefab_guid', '') or ''
        if not guid:
            return
        from Infernux.engine.prefab_overrides import resolve_prefab_instance_root
        root = resolve_prefab_instance_root(obj)
        if root is None:
            return
        adb = engine.get_asset_database()
        path = adb.get_path_from_guid(guid) if adb else ""
        if action == "select":
            if path:
                bs.project_panel.set_current_path(
                    __import__('os').path.dirname(path))
        elif action == "open":
            from Infernux.engine.scene_manager import SceneFileManager
            sfm = SceneFileManager.instance()
            if sfm and path:
                sfm.open_prefab_mode_with_undo(path)
        elif action == "apply":
            from Infernux.engine.prefab_overrides import apply_overrides_to_prefab
            if path:
                apply_overrides_to_prefab(root, path, adb)
        elif action == "revert":
            from Infernux.engine.prefab_overrides import revert_overrides_with_undo
            if path and revert_overrides_with_undo(root, path, adb):
                hierarchy = bs.hierarchy
                hierarchy.invalidate_scene_structure_cache()
                hierarchy.set_selected_object_by_id(root.id, True)
                hierarchy.set_pending_expand_id(root.id)
                if hierarchy.on_selection_changed:
                    hierarchy.on_selection_changed(root.id)
        _invalidate()
        _bump()

    ip.prefab_action = _prefab_action

    from Infernux.lib import TagLayerManager
    ip.get_all_tags = lambda: TagLayerManager.instance().get_all_tags()
    ip.get_all_layers = lambda: TagLayerManager.instance().get_all_layers()

    wm = bs.window_manager
    ip.open_window = lambda win_id: wm.open_window(win_id) if wm else None


# ═══════ Main entry point ══════════════════════════════════════

def wire_inspector_callbacks(bs: EditorBootstrap) -> None:
    """Wire C++ InspectorPanel callbacks to Python managers."""
    ip = bs.inspector_panel
    engine = bs.engine
    from Infernux.engine.i18n import t as _t
    from Infernux.engine.ui import inspector_support as _inspector_support
    from Infernux.engine.ui.selection_manager import SelectionManager
    from Infernux.lib import SceneManager, InspectorObjectInfo, InspectorComponentInfo
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
    ctx.SelectionManager = SelectionManager
    ctx.project_path = bs.project_path

    ip.translate = _t

    sel = SelectionManager.instance()
    ip.is_multi_selection = lambda: sel.is_multi()
    ip.get_selected_ids = lambda: sel.get_ids()
    ip.get_value_generation = _inspector_support.get_inspector_value_generation
    ip.consume_component_body_profile = _inspector_support.consume_inspector_profile_metrics

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
