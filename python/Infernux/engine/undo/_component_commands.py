"""Component add/remove undo commands."""

from __future__ import annotations

import copy
from typing import Any, Callable, Optional

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
    restored_id = int(getattr(instance, "component_id", 0) or 0)
    attached = obj.add_py_component(instance)
    if attached is None:
        raise RuntimeError(f"[Undo] {label}: attach failed")
    if restored_id:
        native_component = getattr(attached, "_cpp_component", None)
        if native_component is None:
            obj.remove_py_component(attached)
            raise RuntimeError(f"[Undo] {label}: native proxy is unavailable")
        native_component._set_component_id(restored_id)
        attached._bind_native_component(native_component, obj)
    _restore_component_index(
        obj,
        int(getattr(attached, "component_id", 0) or 0),
        component_index,
        label,
    )
    if hasattr(attached, '_call_on_after_deserialize'):
        try:
            attached._call_on_after_deserialize()
        except Exception as exc:
            Debug.log_suppressed("undo._component_commands._add_py_from_snapshot.on_after_deserialize", exc)
    _bump_inspector_structure()
    return attached


def _capture_component_documents(obj) -> dict:
    """Capture the exact component structure needed by an add transaction."""
    python_components = {}
    for component in obj.get_py_components() or ():
        component_id = int(getattr(component, "component_id", 0) or 0)
        if not component_id:
            raise RuntimeError("Python component has no stable component identity")
        python_components[component_id] = {
            "ref": component,
            "type_name": _comp_type_name_of(component),
            "document": copy.deepcopy(component._serialize_fields_document()),
        }

    native_components = {}
    for component in obj.get_components() or ():
        component_id = int(getattr(component, "component_id", 0) or 0)
        if not component_id or component_id in python_components:
            continue
        type_name = _comp_type_name_of(component)
        if type_name == "Transform":
            continue
        serializer = getattr(component, "serialize_document", None)
        if not callable(serializer):
            raise RuntimeError(
                f"native component '{type_name}' has no document serializer"
            )
        native_components[component_id] = {
            "ref": component,
            "type_name": type_name,
            "document": copy.deepcopy(serializer()),
        }

    order = tuple(int(value) for value in obj.get_component_order())
    captured_ids = set(native_components) | set(python_components)
    if set(order) != captured_ids:
        missing = sorted(set(order) - captured_ids)
        extra = sorted(captured_ids - set(order))
        raise RuntimeError(
            "component document capture disagrees with native order: "
            f"missing={missing}, extra={extra}"
        )
    return {
        "order": order,
        "native": native_components,
        "python": python_components,
    }


def _restore_component_documents(obj, state: dict, label: str) -> None:
    """Roll a failed first execution back without creating an undo action."""
    before_ids = set(state["order"])
    current = _capture_component_documents(obj)
    for component_id in reversed(current["order"]):
        if component_id in before_ids:
            continue
        if component_id in current["python"]:
            component = current["python"][component_id]["ref"]
            removed = obj.remove_py_component(component)
        else:
            component = current["native"][component_id]["ref"]
            removed = obj.remove_component(component)
            _invalidate_builtin_wrapper(component)
        if removed is False:
            raise RuntimeError(
                f"[Undo] {label}: failed to remove component {component_id}"
            )

    restored = _capture_component_documents(obj)
    if set(restored["order"]) != before_ids:
        raise RuntimeError(f"[Undo] {label}: component set rollback is incomplete")
    for component_id, record in state["native"].items():
        component = restored["native"][component_id]["ref"]
        if not component.deserialize_document(copy.deepcopy(record["document"])):
            raise RuntimeError(
                f"[Undo] {label}: native component {component_id} rollback failed"
            )
    for component_id, record in state["python"].items():
        component = restored["python"][component_id]["ref"]
        component._deserialize_fields_document(copy.deepcopy(record["document"]))
    if tuple(restored["order"]) != tuple(state["order"]):
        if not obj.set_component_order(list(state["order"])):
            raise RuntimeError(f"[Undo] {label}: component order rollback failed")
    _bump_inspector_structure()
    _notify_gizmos_scene_changed()


# -- Command classes --

class AddComponentTransactionCommand(UndoCommand):
    """Create one component and own every structural side effect atomically.

    The first ``execute`` performs the requested add and derives precise child
    commands for automatically-created dependencies and modified existing
    components. Later undo/redo never rerun Inspector code.
    """

    def __init__(
        self,
        object_id: int,
        type_name: str,
        *,
        native_document: Optional[dict] = None,
        python_instance: Any = None,
        initializer: Optional[Callable[[Any], None]] = None,
        invoke_after_deserialize: bool = False,
        description: str = "",
    ):
        super().__init__(description or f"Add {type_name}")
        if python_instance is not None and native_document is not None:
            raise ValueError("component add cannot be both native and Python")
        self._object_id = int(object_id)
        self._type_name = str(type_name)
        self._native_document = copy.deepcopy(native_document)
        self._python_instance = python_instance
        self._initializer = initializer
        self._invoke_after_deserialize = bool(invoke_after_deserialize)
        self._result_component = None
        self._result_component_id = 0
        self._compound: Optional[CompoundCommand] = None

    @property
    def result_component(self):
        return self._result_component

    def _perform_initial_add(self, obj):
        if self._python_instance is not None:
            attached = obj.add_py_component(self._python_instance)
            if attached is None:
                raise RuntimeError(
                    f"failed to attach Python component '{self._type_name}'"
                )
            if self._initializer is not None:
                self._initializer(attached)
            if self._invoke_after_deserialize:
                attached._call_on_after_deserialize()
            return attached

        component = obj.add_component(self._type_name)
        if component is None:
            raise RuntimeError(f"failed to add native component '{self._type_name}'")
        if self._native_document is not None:
            document = copy.deepcopy(self._native_document)
            document.pop("component_id", None)
            if not component.deserialize_document(document):
                raise RuntimeError(
                    f"failed to apply '{self._type_name}' component document"
                )
        if self._initializer is not None:
            self._initializer(component)
        return component

    def _build_compound(self, before: dict, after: dict) -> CompoundCommand:
        from Infernux.engine.undo._property_commands import (
            GenericComponentCommand,
            PythonComponentDocumentCommand,
        )

        before_ids = set(before["order"])
        after_ids = set(after["order"])
        missing = before_ids - after_ids
        if missing:
            raise RuntimeError(
                "component add unexpectedly removed existing components: "
                f"{sorted(missing)}"
            )

        commands = []
        for component_id in before["order"]:
            if component_id in before["native"]:
                old_record = before["native"][component_id]
                new_record = after["native"][component_id]
                if old_record["document"] != new_record["document"]:
                    commands.append(
                        GenericComponentCommand(
                            new_record["ref"],
                            old_record["document"],
                            new_record["document"],
                            f"Update {new_record['type_name']} for {self._type_name}",
                            mergeable=False,
                        )
                    )
                continue
            old_record = before["python"][component_id]
            new_record = after["python"][component_id]
            if old_record["document"] != new_record["document"]:
                commands.append(
                    PythonComponentDocumentCommand(
                        new_record["ref"],
                        old_record["document"],
                        new_record["document"],
                        f"Update {new_record['type_name']} for {self._type_name}",
                        edit_key=f"component_add:{self._object_id}:{component_id}",
                    )
                )

        for component_id in after["order"]:
            if component_id in before_ids:
                continue
            if component_id in after["native"]:
                record = after["native"][component_id]
                commands.append(
                    AddNativeComponentCommand(
                        self._object_id,
                        record["type_name"],
                        record["ref"],
                        f"Add {record['type_name']}",
                    )
                )
            else:
                record = after["python"][component_id]
                commands.append(
                    AddPyComponentCommand(
                        self._object_id,
                        record["ref"],
                        f"Add {record['type_name']}",
                    )
                )

        if not commands or before_ids == after_ids:
            raise RuntimeError(
                f"component add '{self._type_name}' produced no new component"
            )
        return CompoundCommand(commands, self.description)

    def execute(self) -> None:
        if self._compound is not None:
            self._compound.redo()
            return
        _scene, obj = _require_scene_object(
            self._object_id, f"AddComponent('{self._type_name}').execute"
        )
        before = _capture_component_documents(obj)
        try:
            self._result_component = self._perform_initial_add(obj)
            self._result_component_id = int(
                getattr(self._result_component, "component_id", 0) or 0
            )
            after = _capture_component_documents(obj)
            self._compound = self._build_compound(before, after)
        except Exception:
            _restore_component_documents(
                obj, before, f"AddComponent('{self._type_name}').rollback"
            )
            raise
        finally:
            self._initializer = None
        _bump_inspector_structure()
        _notify_gizmos_scene_changed()

    def undo(self) -> None:
        if self._compound is None:
            raise RuntimeError("component add transaction has not executed")
        self._compound.undo()
        self._result_component = None

    def redo(self) -> None:
        if self._compound is None:
            raise RuntimeError("component add transaction has not executed")
        self._compound.redo()
        _scene, obj = _require_scene_object(
            self._object_id, f"AddComponent('{self._type_name}').redo.resolve"
        )
        state = _capture_component_documents(obj)
        record = state["native"].get(self._result_component_id)
        if record is None:
            record = state["python"].get(self._result_component_id)
        if record is None:
            raise RuntimeError(
                f"component add redo lost stable component {self._result_component_id}"
            )
        self._result_component = record["ref"]

    def dispose(self) -> None:
        if self._compound is not None:
            self._compound.dispose()
            self._compound = None
        elif self._python_instance is not None and not bool(
            getattr(self._python_instance, "_is_destroyed", False)
        ):
            self._python_instance._call_on_destroy()

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
