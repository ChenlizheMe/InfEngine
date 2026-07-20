"""Component add/remove undo commands."""

from __future__ import annotations

from typing import Any, Optional

from Infernux.debug import Debug
from Infernux.engine.undo._base import UndoCommand
from Infernux.engine.undo._helpers import (
    _get_active_scene, _comp_type_name_of,
    _require_scene_object, _find_live_native_component,
    _invalidate_builtin_wrapper,
    _bump_inspector_structure, _notify_gizmos_scene_changed,
)
from Infernux.engine.undo._snapshots import _get_nth_live_py_component


# -- Helper functions --

def _snapshot_py_fields(py_comp: Any) -> str:
    if py_comp is None or not hasattr(py_comp, '_serialize_fields'):
        return ""
    try:
        return py_comp._serialize_fields()
    except Exception as exc:
        Debug.log_suppressed("undo._component_commands._snapshot_py_fields", exc)
        return ""


def _snapshot_py_enabled(py_comp: Any) -> bool:
    try:
        return bool(getattr(py_comp, 'enabled', True))
    except Exception:
        return True


def _find_py_ordinal(object_id: int, py_comp: Any) -> int:
    scene = _get_active_scene()
    if not scene:
        return 0
    obj = scene.find_by_id(object_id)
    if obj is None or not hasattr(obj, 'get_py_components'):
        return 0
    target_type = _comp_type_name_of(py_comp)
    target_guid = getattr(py_comp, '_script_guid', '') or ''
    target_type_guid = py_comp.__class__._get_type_guid()
    ordinal = 0
    try:
        for current in obj.get_py_components():
            try:
                ct = _comp_type_name_of(current)
                cg = getattr(current, '_script_guid', '') or ''
                ctg = current.__class__._get_type_guid()
            except Exception as exc:
                Debug.log_suppressed("undo._component_commands._find_py_ordinal.read_meta", exc)
                continue
            if ct != target_type or cg != target_guid or ctg != target_type_guid:
                continue
            if current is py_comp:
                return ordinal
            ordinal += 1
    except Exception as exc:
        Debug.log_suppressed("undo._component_commands._find_py_ordinal.iter", exc)
    return 0


def _resolve_live_py(obj, type_name: str, script_guid: str, type_guid: str,
                     ordinal: int, fallback: Any = None):
    live = _get_nth_live_py_component(obj.id, type_name, ordinal, script_guid, type_guid)
    if live is not None:
        return live
    if fallback is None:
        return None
    try:
        for current in obj.get_py_components():
            if current is fallback:
                return current
    except Exception as exc:
        Debug.log_suppressed("undo._component_commands._resolve_live_py.fallback_lookup", exc)
    return None


def _instantiate_py_snapshot(type_name: str, script_guid: str, type_guid: str,
                             fields_json: str, enabled: bool,
                             description: str = "") -> Any:
    from Infernux.engine.scene_manager import SceneFileManager
    from Infernux.engine.component_restore import create_component_instance

    sfm = SceneFileManager.instance()
    asset_db = sfm._asset_database if sfm else None
    instance, script_path = create_component_instance(
        script_guid,
        type_guid,
        type_name,
        asset_database=asset_db,
        prefer_loaded_type=True,
    )

    if instance is None:
        location = script_path or script_guid or "<unresolved>"
        raise RuntimeError(
            f"[Undo] Cannot recreate Python component '{type_name}' from "
            f"{location} during {description or 'undo/redo'}"
        )

    if fields_json:
        instance._deserialize_fields(fields_json, _skip_on_after_deserialize=True)

    instance.enabled = enabled
    if script_guid:
        instance._script_guid = script_guid
    return instance


def _snapshot_and_remove_native(object_id: int, type_name: str,
                                label: str) -> dict:
    _scene, obj = _require_scene_object(object_id, label)
    live = _find_live_native_component(obj, type_name)
    if live is None:
        raise RuntimeError(f"[Undo] {label}: component '{type_name}' not found")
    document = live.serialize_document()
    obj.remove_component(live)
    _invalidate_builtin_wrapper(live)
    _bump_inspector_structure()
    _notify_gizmos_scene_changed()
    return document


def _add_native_from_snapshot(object_id: int, type_name: str,
                              document: Optional[dict],
                              label: str) -> None:
    _scene, obj = _require_scene_object(object_id, label)
    result = obj.add_component(type_name)
    if not result:
        raise RuntimeError(f"[Undo] {label}: add '{type_name}' failed")
    if document is not None and not result.deserialize_document(document):
        obj.remove_component(result)
        raise RuntimeError(f"[Undo] {label}: component document restore failed")
    _bump_inspector_structure()
    _notify_gizmos_scene_changed()


def _snapshot_and_remove_py(object_id: int, type_name: str, script_guid: str, type_guid: str,
                            ordinal: int, py_comp_ref: Any, label: str):
    _scene, obj = _require_scene_object(object_id, label)
    live = _resolve_live_py(obj, type_name, script_guid, type_guid, ordinal, py_comp_ref)
    if live is None:
        raise RuntimeError(f"[Undo] {label}: component not found")
    fields_json = _snapshot_py_fields(live)
    enabled = _snapshot_py_enabled(live)
    obj.remove_py_component(live)
    _bump_inspector_structure()
    return fields_json, enabled, live


def _add_py_from_snapshot(object_id: int, type_name: str, script_guid: str, type_guid: str,
                          fields_json, enabled, label: str):
    _scene, obj = _require_scene_object(object_id, label)
    instance = _instantiate_py_snapshot(
        type_name, script_guid, type_guid, fields_json, enabled, description=label)
    if instance is None:
        raise RuntimeError(f"[Undo] {label}: recreate failed")
    obj.add_py_component(instance)
    if hasattr(instance, '_call_on_after_deserialize'):
        try:
            instance._call_on_after_deserialize()
        except Exception as exc:
            Debug.log_suppressed("undo._component_commands._add_py_from_snapshot.on_after_deserialize", exc)
    _bump_inspector_structure()
    return instance


# -- Command classes --

class AddNativeComponentCommand(UndoCommand):
    """Undo removes the C++ component; redo re-adds from a document snapshot."""

    def __init__(self, object_id: int, type_name: str, comp_ref: Any = None,
                 description: str = ""):
        super().__init__(description or f"Add {type_name}")
        self._object_id = object_id
        self._type_name = type_name
        self._document: Optional[dict] = None

    def execute(self) -> None:
        pass

    def undo(self) -> None:
        self._document = _snapshot_and_remove_native(
            self._object_id, self._type_name,
            f"AddNative('{self._type_name}').undo")

    def redo(self) -> None:
        _add_native_from_snapshot(
            self._object_id, self._type_name, self._document,
            f"AddNative('{self._type_name}').redo")


class RemoveNativeComponentCommand(UndoCommand):
    """Undo re-adds the C++ component from a document; redo re-removes."""

    def __init__(self, object_id: int, type_name: str, comp_ref: Any = None,
                 description: str = ""):
        super().__init__(description or f"Remove {type_name}")
        self._object_id = object_id
        self._type_name = type_name
        self._document: Optional[dict] = comp_ref.serialize_document() if comp_ref is not None else None

    def execute(self) -> None:
        self._do_remove()

    def undo(self) -> None:
        _add_native_from_snapshot(
            self._object_id, self._type_name, self._document,
            f"RemoveNative('{self._type_name}').undo")

    def redo(self) -> None:
        self._do_remove()

    def _do_remove(self) -> None:
        self._document = _snapshot_and_remove_native(
            self._object_id, self._type_name,
            f"RemoveNative('{self._type_name}')")


class AddPyComponentCommand(UndoCommand):
    """Undo removes the Python component; redo recreates from snapshot."""

    def __init__(self, object_id: int, py_comp_ref: Any,
                 description: str = ""):
        self._type_name_str = getattr(py_comp_ref, 'type_name', 'Script')
        super().__init__(description or f"Add {self._type_name_str}")
        self._object_id = object_id
        self._py_comp_ref = py_comp_ref
        self._script_guid = getattr(py_comp_ref, '_script_guid', '') or ''
        self._type_guid = py_comp_ref.__class__._get_type_guid()
        self._fields_json = _snapshot_py_fields(py_comp_ref)
        self._enabled = _snapshot_py_enabled(py_comp_ref)
        self._ordinal = _find_py_ordinal(object_id, py_comp_ref)

    def execute(self) -> None:
        pass

    def undo(self) -> None:
        fj, en, live = _snapshot_and_remove_py(
            self._object_id, self._type_name_str, self._script_guid,
            self._type_guid,
            self._ordinal, self._py_comp_ref,
            f"AddPy('{self._type_name_str}').undo")
        self._fields_json, self._enabled, self._py_comp_ref = fj, en, live

    def redo(self) -> None:
        self._py_comp_ref = _add_py_from_snapshot(
            self._object_id, self._type_name_str, self._script_guid,
            self._type_guid,
            self._fields_json, self._enabled,
            f"AddPy('{self._type_name_str}').redo")


class RemovePyComponentCommand(UndoCommand):
    """Undo recreates the Python component from snapshot; redo re-removes."""

    def __init__(self, object_id: int, py_comp_ref: Any,
                 description: str = ""):
        self._type_name_str = getattr(py_comp_ref, 'type_name', 'Script')
        super().__init__(description or f"Remove {self._type_name_str}")
        self._object_id = object_id
        self._py_comp_ref = py_comp_ref
        self._script_guid = getattr(py_comp_ref, '_script_guid', '') or ''
        self._type_guid = py_comp_ref.__class__._get_type_guid()
        self._fields_json = _snapshot_py_fields(py_comp_ref)
        self._enabled = _snapshot_py_enabled(py_comp_ref)
        self._ordinal = _find_py_ordinal(object_id, py_comp_ref)

    def execute(self) -> None:
        self._do_remove()

    def undo(self) -> None:
        self._py_comp_ref = _add_py_from_snapshot(
            self._object_id, self._type_name_str, self._script_guid,
            self._type_guid,
            self._fields_json, self._enabled,
            f"RemovePy('{self._type_name_str}').undo")

    def redo(self) -> None:
        self._do_remove()

    def _do_remove(self) -> None:
        fj, en, live = _snapshot_and_remove_py(
            self._object_id, self._type_name_str, self._script_guid,
            self._type_guid,
            self._ordinal, self._py_comp_ref,
            f"RemovePy('{self._type_name_str}')")
        self._fields_json, self._enabled, self._py_comp_ref = fj, en, live
