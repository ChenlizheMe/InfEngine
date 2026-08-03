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
        return scene, items, native_map, py_map

    def _get_cached_maps(obj_id):
        scene, scene_ver, struct_ver = _versions()
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
        transform = obj.get_transform()
        info.transform_component_id = int(
            getattr(transform, 'component_id', 0) or 0
        )
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


def _restore_native_components_after_failed_paste(
    obj,
    before_ids: set[int],
    before_documents: dict,
) -> None:
    for component in reversed(list(obj.get_components() or ())):
        component_id = int(getattr(component, "component_id", 0) or 0)
        if component_id and component_id not in before_ids:
            obj.remove_component(component)
    for component_id, (component, document) in before_documents.items():
        if component_id not in before_ids:
            continue
        if not component.deserialize_document(copy.deepcopy(document)):
            raise RuntimeError(
                f"Failed to roll back native component {component_id}"
            )


def _paste_native_component_as_new(obj, type_name: str, document: dict) -> bool:
    from Infernux.engine.ui._inspector_undo import (
        _get_component_ids,
        _get_native_component_documents,
        _record_add_component_compound,
    )

    before_ids = _get_component_ids(obj)
    before_documents = _get_native_component_documents(obj)
    try:
        result = obj.add_component(type_name)
        if result is None:
            raise RuntimeError(f"Failed to add native component '{type_name}'")
        if int(getattr(result, "component_id", 0) or 0) in before_ids:
            raise RuntimeError(
                f"Native component '{type_name}' does not allow another instance"
            )
        if not result.deserialize_document(copy.deepcopy(document)):
            raise RuntimeError(f"Failed to paste native component '{type_name}'")
    except Exception as exc:
        try:
            _restore_native_components_after_failed_paste(
                obj,
                before_ids,
                before_documents,
            )
        except Exception as rollback_exc:
            Debug.log_error(
                f"Cannot roll back failed component paste '{type_name}': {rollback_exc}"
            )
        Debug.log_error(f"Cannot paste native component '{type_name}': {exc}")
        return False

    _record_add_component_compound(
        obj,
        type_name,
        result,
        before_ids,
        before_documents=before_documents,
    )
    return True


def _paste_native_component_values(comp, document: dict) -> bool:
    old_document = comp.serialize_document()
    new_document = copy.deepcopy(document)
    if old_document == new_document:
        return False
    from Infernux.engine.undo import GenericComponentCommand, UndoManager

    manager = UndoManager.instance()
    if manager is not None:
        return manager.execute(
            GenericComponentCommand(
                comp,
                old_document,
                new_document,
                f"Paste {getattr(comp, 'type_name', 'Component')} Properties",
                mergeable=False,
            )
        )
    if not comp.deserialize_document(new_document):
        return False
    from Infernux.engine.ui._inspector_undo import _notify_scene_modified

    _notify_scene_modified()
    return True

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
            from Infernux.engine.ui._inspector_undo import (
                _get_component_ids,
                _get_native_component_documents,
                _record_add_component_compound,
            )
            before_ids = _get_component_ids(obj)
            before_documents = _get_native_component_documents(obj)
            attached = obj.add_py_component(instance)
            if attached is None:
                instance._call_on_destroy()
                try:
                    _restore_native_components_after_failed_paste(
                        obj,
                        before_ids,
                        before_documents,
                    )
                except Exception as rollback_exc:
                    Debug.log_error(
                        f"Cannot roll back failed component paste '{tn}': "
                        f"{rollback_exc}"
                    )
                Debug.log_error(f"Cannot paste: failed to attach '{tn}'")
                return False
            try:
                attached._call_on_after_deserialize()
            except Exception as exc:
                obj.remove_py_component(attached)
                try:
                    _restore_native_components_after_failed_paste(
                        obj,
                        before_ids,
                        before_documents,
                    )
                except Exception as rollback_exc:
                    Debug.log_error(
                        f"Cannot roll back failed component paste '{tn}': "
                        f"{rollback_exc}"
                    )
                Debug.log_error(f"Cannot finish pasting '{tn}': {exc}")
                return False
            _record_add_component_compound(
                obj,
                tn,
                attached,
                before_ids,
                is_py=True,
                before_documents=before_documents,
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
                old_document = _python_component_clipboard_document(comp)
                new_document = copy.deepcopy(payload)
                new_document["__type_name__"] = type(comp).__name__
                from Infernux.engine.undo import (
                    PythonComponentDocumentCommand,
                    UndoManager,
                )
                manager = UndoManager.instance()
                if manager:
                    command = PythonComponentDocumentCommand(
                        comp,
                        old_document,
                        new_document,
                        f"Paste {type(comp).__name__} Properties",
                        edit_key=f"paste_component:{_time.time_ns()}",
                    )
                    if not manager.execute(command):
                        return False
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

    def _selected_component_entries():
        from Infernux.engine.interaction import SelectionDomain, SelectionService

        snapshot = SelectionService.instance().snapshot
        if snapshot.domain is not SelectionDomain.COMPONENT:
            return []
        scene = SceneManager.instance().get_active_scene()
        if scene is None:
            return []
        entries = []
        for target in snapshot.targets:
            object_id, component_id = target.component_ids()
            obj = scene.find_by_id(object_id) if object_id else None
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

    def _primary_component_entry():
        entries = _selected_component_entries()
        if not entries:
            return None
        from Infernux.engine.interaction import SelectionService

        primary = SelectionService.instance().snapshot.primary
        return next((entry for entry in entries if entry[0] == primary), entries[-1])

    def _can_copy_selected_component():
        return _primary_component_entry() is not None

    def _copy_selected_component():
        entry = _primary_component_entry()
        if entry is None:
            return False
        _target, _obj, comp, type_name, is_native = entry
        return _copy_to_clipboard(comp, type_name, is_native)

    def _can_paste_selected_values():
        entry = _primary_component_entry()
        if entry is None:
            return False
        _target, _obj, comp, type_name, is_native = entry
        return _can_paste_values(comp, type_name, is_native)

    def _paste_selected_values():
        entry = _primary_component_entry()
        if entry is None:
            return False
        _target, _obj, comp, _type_name, is_native = entry
        return _paste_values(comp, is_native)

    def _can_paste_selected_as_new():
        return _primary_component_entry() is not None and _has_clip()

    def _paste_selected_as_new():
        entry = _primary_component_entry()
        if entry is None:
            return False
        return _paste_as_new(entry[1])

    def _paste_selected_default():
        if _can_paste_selected_values():
            return _paste_selected_values()
        return _paste_selected_as_new()

    def _can_remove_selected_components():
        entries = _selected_component_entries()
        return bool(entries) and all(
            _can_remove_component(obj, comp, type_name, is_native)
            for _target, obj, comp, type_name, is_native in entries
        )

    def _remove_selected_components():
        entries = _selected_component_entries()
        if not entries or not _can_remove_selected_components():
            return False
        from Infernux.engine.interaction import (
            SelectionService,
            SelectionSnapshot,
            SelectionTarget,
        )
        from Infernux.engine.undo import (
            RemoveComponentsCommand,
            RemoveNativeComponentCommand,
            RemovePyComponentCommand,
            UndoManager,
        )

        before_selection = SelectionService.instance().snapshot
        object_ids = list(dict.fromkeys(int(entry[1].id) for entry in entries))
        after_targets = tuple(
            SelectionTarget.scene_object(object_id) for object_id in object_ids
        )
        primary_object_id = (
            before_selection.primary.component_ids()[0]
            if before_selection.primary is not None
            else object_ids[-1]
        )
        primary_target = SelectionTarget.scene_object(primary_object_id)
        after_selection = SelectionSnapshot.create(
            after_targets,
            owner_id="inspector",
            primary=primary_target,
            anchor=after_targets[0],
        )
        commands = [
            (
                RemoveNativeComponentCommand(obj.id, type_name, comp)
                if is_native
                else RemovePyComponentCommand(obj.id, comp)
            )
            for _target, obj, comp, type_name, is_native in entries
        ]
        description = (
            commands[0].description
            if len(commands) == 1
            else f"Remove {len(commands)} Components"
        )
        command = RemoveComponentsCommand(
            commands,
            before_selection,
            after_selection,
            description,
        )
        manager = UndoManager.instance()
        if manager is not None:
            if not manager.execute(command):
                return False
        else:
            command.execute()
            command.dispose()
        _invalidate()
        return True

    def _can_open_selected_script():
        entry = _primary_component_entry()
        return bool(entry is not None and not entry[4] and _get_script_path(entry[2]))

    def _open_selected_script():
        entry = _primary_component_entry()
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
        can_open_script=_can_open_selected_script,
        open_script=_open_selected_script,
    )

    def _render_component_context_menu(ctx_arg, obj_id, type_name, comp_id, is_native):
        del obj_id, type_name, comp_id, is_native
        from Infernux.engine.interaction import CommandSource

        registry = ctx.bs.interaction_core.commands

        def _command_item(label, command_id):
            context = registry.context(CommandSource.CONTEXT_MENU)
            enabled = registry.can_execute(command_id, context)
            if not enabled:
                ctx_arg.begin_disabled()
            clicked = ctx_arg.selectable(label)
            if not enabled:
                ctx_arg.end_disabled()
            if not clicked:
                return False
            result = registry.execute(
                command_id,
                source=CommandSource.CONTEXT_MENU,
            )
            if result.accepted:
                ctx_arg.close_current_popup()
            return result.accepted

        if registry.can_execute(
            "component.open_script",
            registry.context(CommandSource.CONTEXT_MENU),
        ):
            if _command_item(_t("inspector.show_script"), "component.open_script"):
                return False
            ctx_arg.separator()

        if _command_item(
            _t("inspector.copy_properties"),
            "component.copy_properties",
        ):
            return False
        if _command_item(
            _t("inspector.paste_as_new"),
            "component.paste_as_new",
        ):
            return False
        if _command_item(
            _t("inspector.paste_properties"),
            "component.paste_properties",
        ):
            return False

        ctx_arg.separator()
        if _command_item(_t("inspector.remove"), "component.remove"):
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
                            f"only one per GameObject is allowed")
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
                from Infernux.engine.interaction import (
                    SelectionService,
                    SelectionTarget,
                )

                SelectionService.instance().select(
                    SelectionTarget.scene_object(root.id),
                    owner_id="inspector",
                    reason="prefab_revert",
                    record_history=False,
                )
                hierarchy.set_pending_expand_id(root.id)
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
