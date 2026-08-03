"""Component add/remove undo commands."""

from __future__ import annotations

from typing import Any, Optional

from Infernux.debug import Debug
from Infernux.engine.undo._base import CompoundCommand, UndoCommand
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


def _find_native_component(obj, type_name: str, component_id: int = 0):
    if component_id:
        for component in obj.get_components() or ():
            if int(getattr(component, "component_id", 0) or 0) == int(component_id):
                return component
        return None
    return _find_live_native_component(obj, type_name)


def _component_index(object_id: int, component_id: int) -> int:
    if not component_id:
        return -1
    _scene, obj = _require_scene_object(object_id, "ComponentIndex")
    try:
        return tuple(int(value) for value in obj.get_component_order()).index(
            int(component_id)
        )
    except ValueError:
        return -1


def _restore_component_index(obj, component_id: int, component_index: int,
                             label: str) -> None:
    if component_index < 0:
        return
    order = [int(value) for value in obj.get_component_order()]
    component_id = int(component_id)
    if component_id not in order:
        raise RuntimeError(f"[Undo] {label}: restored component is missing")
    order.remove(component_id)
    order.insert(min(component_index, len(order)), component_id)
    if not obj.set_component_order(order):
        raise RuntimeError(f"[Undo] {label}: failed to restore component order")


def _snapshot_and_remove_native(object_id: int, type_name: str,
                                label: str, component_id: int = 0) -> dict:
    _scene, obj = _require_scene_object(object_id, label)
    live = _find_native_component(obj, type_name, component_id)
    if live is None:
        identity = f" id={component_id}" if component_id else ""
        raise RuntimeError(
            f"[Undo] {label}: component '{type_name}'{identity} not found"
        )
    document = live.serialize_document()
    if obj.remove_component(live) is False:
        raise RuntimeError(f"[Undo] {label}: native component removal failed")
    _invalidate_builtin_wrapper(live)
    _bump_inspector_structure()
    _notify_gizmos_scene_changed()
    return document


def _add_native_from_snapshot(object_id: int, type_name: str,
                              document: Optional[dict],
                              label: str, component_index: int = -1):
    _scene, obj = _require_scene_object(object_id, label)
    result = obj.add_component(type_name)
    if not result:
        raise RuntimeError(f"[Undo] {label}: add '{type_name}' failed")
    if document is not None and not result.deserialize_document(document):
        obj.remove_component(result)
        raise RuntimeError(f"[Undo] {label}: component document restore failed")
    restored_id = int((document or {}).get("component_id", 0) or 0)
    if restored_id:
        live = _find_native_component(obj, type_name, restored_id)
        if live is None:
            raise RuntimeError(
                f"[Undo] {label}: restored component id={restored_id} is not live"
            )
        result = live
    _restore_component_index(
        obj,
        int(getattr(result, "component_id", 0) or 0),
        component_index,
        label,
    )
    _bump_inspector_structure()
    _notify_gizmos_scene_changed()
    return result


def _snapshot_and_remove_py(object_id: int, type_name: str, script_guid: str, type_guid: str,
                            ordinal: int, py_comp_ref: Any, label: str):
    _scene, obj = _require_scene_object(object_id, label)
    live = _resolve_live_py(obj, type_name, script_guid, type_guid, ordinal, py_comp_ref)
    if live is None:
        raise RuntimeError(f"[Undo] {label}: component not found")
    fields_json = _snapshot_py_fields(live)
    enabled = _snapshot_py_enabled(live)
    if obj.remove_py_component(live) is False:
        raise RuntimeError(f"[Undo] {label}: Python component removal failed")
    _bump_inspector_structure()
    return fields_json, enabled, live


def _add_py_from_snapshot(object_id: int, type_name: str, script_guid: str, type_guid: str,
                          fields_json, enabled, label: str, component_index: int = -1):
    _scene, obj = _require_scene_object(object_id, label)
    instance = _instantiate_py_snapshot(
        type_name, script_guid, type_guid, fields_json, enabled, description=label)
    if instance is None:
        raise RuntimeError(f"[Undo] {label}: recreate failed")
    obj.add_py_component(instance)
    _restore_component_index(
        obj,
        int(getattr(instance, "component_id", 0) or 0),
        component_index,
        label,
    )
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
        self._component_ref = comp_ref
        self._component_id = int(getattr(comp_ref, "component_id", 0) or 0)
        self._component_index = _component_index(object_id, self._component_id)

    def execute(self) -> None:
        pass

    def undo(self) -> None:
        self._document = _snapshot_and_remove_native(
            self._object_id, self._type_name,
            f"AddNative('{self._type_name}').undo",
            self._component_id,
        )
        self._component_ref = None
        self._component_id = 0

    def redo(self) -> None:
        self._component_ref = _add_native_from_snapshot(
            self._object_id, self._type_name, self._document,
            f"AddNative('{self._type_name}').redo", self._component_index)
        self._component_id = int(
            getattr(self._component_ref, "component_id", 0) or 0
        )


class RemoveNativeComponentCommand(UndoCommand):
    """Undo re-adds the C++ component from a document; redo re-removes."""

    def __init__(self, object_id: int, type_name: str, comp_ref: Any = None,
                 description: str = ""):
        super().__init__(description or f"Remove {type_name}")
        self._object_id = object_id
        self._type_name = type_name
        self._document: Optional[dict] = comp_ref.serialize_document() if comp_ref is not None else None
        self._component_ref = comp_ref
        self._component_id = int(getattr(comp_ref, "component_id", 0) or 0)
        self._component_index = _component_index(object_id, self._component_id)

    def execute(self) -> None:
        self._do_remove()

    def undo(self) -> None:
        self._component_ref = _add_native_from_snapshot(
            self._object_id, self._type_name, self._document,
            f"RemoveNative('{self._type_name}').undo", self._component_index)
        self._component_id = int(
            getattr(self._component_ref, "component_id", 0) or 0
        )

    def redo(self) -> None:
        self._do_remove()

    def _do_remove(self) -> None:
        self._document = _snapshot_and_remove_native(
            self._object_id, self._type_name,
            f"RemoveNative('{self._type_name}')",
            self._component_id,
        )
        self._component_ref = None
        self._component_id = 0


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
        self._component_index = _component_index(
            object_id, int(getattr(py_comp_ref, "component_id", 0) or 0)
        )

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
            f"AddPy('{self._type_name_str}').redo", self._component_index)


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
        self._component_index = _component_index(
            object_id, int(getattr(py_comp_ref, "component_id", 0) or 0)
        )

    def execute(self) -> None:
        self._do_remove()

    def undo(self) -> None:
        self._py_comp_ref = _add_py_from_snapshot(
            self._object_id, self._type_name_str, self._script_guid,
            self._type_guid,
            self._fields_json, self._enabled,
            f"RemovePy('{self._type_name_str}').undo", self._component_index)

    def redo(self) -> None:
        self._do_remove()

    def _do_remove(self) -> None:
        fj, en, live = _snapshot_and_remove_py(
            self._object_id, self._type_name_str, self._script_guid,
            self._type_guid,
            self._ordinal, self._py_comp_ref,
            f"RemovePy('{self._type_name_str}')")
        self._fields_json, self._enabled, self._py_comp_ref = fj, en, live


class RemoveComponentsCommand(CompoundCommand):
    """Remove one or more selected components as one structural action."""

    def __init__(
        self,
        commands,
        before_selection,
        after_selection,
        description: str = "Remove Components",
    ):
        from Infernux.engine.interaction import SelectionSnapshot

        if not isinstance(before_selection, SelectionSnapshot):
            raise TypeError("before_selection must be a SelectionSnapshot")
        if not isinstance(after_selection, SelectionSnapshot):
            raise TypeError("after_selection must be a SelectionSnapshot")
        commands = list(commands)
        if not commands:
            raise ValueError("component removal requires at least one command")
        super().__init__(commands, description)
        self._before_orders = {}
        for command in commands:
            object_id = int(getattr(command, "_object_id", 0) or 0)
            if not object_id or object_id in self._before_orders:
                continue
            _scene, obj = _require_scene_object(
                object_id, "RemoveComponents.capture_order"
            )
            self._before_orders[object_id] = tuple(
                int(value) for value in obj.get_component_order()
            )
        self.before_selection_snapshot = before_selection
        self.after_selection_snapshot = after_selection

    def _restore_original_orders(self, label: str) -> None:
        for object_id, order in self._before_orders.items():
            _scene, obj = _require_scene_object(object_id, label)
            current = tuple(int(value) for value in obj.get_component_order())
            if current == order:
                continue
            if set(current) != set(order) or not obj.set_component_order(list(order)):
                raise RuntimeError(
                    f"[Undo] {label}: failed to restore component order for "
                    f"object {object_id}"
                )

    @staticmethod
    def _apply_selection(snapshot, reason: str) -> None:
        from Infernux.engine.interaction import SelectionService

        SelectionService.instance().apply_snapshot(
            snapshot,
            reason=reason,
            record_history=False,
        )

    def execute(self) -> None:
        try:
            super().execute()
        except Exception:
            self._restore_original_orders("RemoveComponents.execute.rollback")
            raise
        self._apply_selection(
            self.after_selection_snapshot,
            "remove_components",
        )

    def undo(self) -> None:
        super().undo()
        self._restore_original_orders("RemoveComponents.undo")
        self._apply_selection(
            self.before_selection_snapshot,
            "undo_remove_components",
        )

    def redo(self) -> None:
        super().redo()
        self._apply_selection(
            self.after_selection_snapshot,
            "redo_remove_components",
        )


class ReorderComponentsCommand(UndoCommand):
    """Apply one or more exact component-order permutations atomically."""

    def __init__(self, changes, description: str = "Reorder Components"):
        super().__init__(description)
        normalized = []
        for object_id, before_order, after_order in changes:
            before = tuple(int(value) for value in before_order)
            after = tuple(int(value) for value in after_order)
            if not before or len(before) != len(after):
                raise ValueError("component orders must be non-empty and equally sized")
            if len(set(before)) != len(before) or set(before) != set(after):
                raise ValueError("component orders must be exact stable-ID permutations")
            if before != after:
                normalized.append((int(object_id), before, after))
        if not normalized:
            raise ValueError("component reorder must change at least one object")
        self._changes = tuple(normalized)

    @staticmethod
    def _set_order(object_id: int, order: tuple[int, ...], label: str) -> None:
        _scene, obj = _require_scene_object(object_id, label)
        current = tuple(int(value) for value in obj.get_component_order())
        if current == order:
            return
        if set(current) != set(order) or not obj.set_component_order(list(order)):
            raise RuntimeError(
                f"[Undo] {label}: component order no longer matches object {object_id}"
            )

    def _apply(self, order_index: int, label: str) -> None:
        applied = []
        try:
            for change in self._changes:
                object_id = change[0]
                _scene, obj = _require_scene_object(object_id, label)
                previous = tuple(int(value) for value in obj.get_component_order())
                self._set_order(object_id, change[order_index], label)
                applied.append((object_id, previous))
        except Exception:
            for object_id, previous in reversed(applied):
                try:
                    self._set_order(object_id, previous, f"{label}.rollback")
                except Exception as rollback_exc:
                    Debug.log_error(
                        f"[Undo] {label}: component-order rollback failed: {rollback_exc}"
                    )
            raise
        _bump_inspector_structure()

    def execute(self) -> None:
        self._apply(2, "ReorderComponents.execute")

    def undo(self) -> None:
        self._apply(1, "ReorderComponents.undo")

    def redo(self) -> None:
        self._apply(2, "ReorderComponents.redo")
