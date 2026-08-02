"""Property-edit undo commands."""

from __future__ import annotations

import copy
from typing import Any, Callable, Optional

from Infernux.debug import Debug
from Infernux.engine.undo._base import UndoCommand, _snapshot_value
from Infernux.engine.undo._helpers import (
    _game_object_id_of, _comp_type_name_of, _stable_target_id,
    _resolve_target,
)


class SetPropertyCommand(UndoCommand):
    """Generic property-edit via ``setattr(target, name, value)``."""

    _is_property_edit = True
    MERGE_WINDOW: float = 0.3

    def __init__(self, target: Any, prop_name: str,
                 old_value: Any, new_value: Any,
                 description: str = ""):
        super().__init__(description or f"Set {prop_name}")
        self._target = target
        self._prop_name = prop_name
        self._old_value = _snapshot_value(old_value)
        self._new_value = _snapshot_value(new_value)
        self._target_id: int = _stable_target_id(target)
        self._game_object_id: int = _game_object_id_of(target)
        self._comp_type_name: str = _comp_type_name_of(target) if self._game_object_id else ""
        self._builtin_wrapper_cls = None
        try:
            from Infernux.components.builtin_component import BuiltinComponent

            if isinstance(target, BuiltinComponent):
                self._builtin_wrapper_cls = type(target)
        except ImportError:
            pass

    def _live(self):
        target = _resolve_target(self._target, self._game_object_id, self._comp_type_name)
        wrapper_cls = self._builtin_wrapper_cls
        if target is None or wrapper_cls is None or isinstance(target, wrapper_cls):
            return target
        game_object = getattr(target, "game_object", None)
        if game_object is None:
            return target
        try:
            return wrapper_cls._get_or_create_wrapper(target, game_object)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            return target

    def execute(self) -> None:
        target = self._live()
        if target is None:
            target = self._target
        setattr(target, self._prop_name, self._new_value)

    def undo(self) -> None:
        target = self._live()
        if target is None:
            Debug.log_error(
                f"[Undo] SetProperty('{self._prop_name}').undo: target not found "
                f"(go={self._game_object_id}, type={self._comp_type_name})")
            return
        setattr(target, self._prop_name, self._old_value)

    def redo(self) -> None:
        target = self._live()
        if target is None:
            Debug.log_error(
                f"[Undo] SetProperty('{self._prop_name}').redo: target not found "
                f"(go={self._game_object_id}, type={self._comp_type_name})")
            return
        setattr(target, self._prop_name, self._new_value)

    def can_merge(self, other: UndoCommand) -> bool:
        if not isinstance(other, SetPropertyCommand):
            return False
        return (self._target_id == other._target_id
                and self._prop_name == other._prop_name
                and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW)

    def merge(self, other: SetPropertyCommand) -> None:
        self._new_value = _snapshot_value(other._new_value)
        self.timestamp = other.timestamp


BuiltinPropertyCommand = SetPropertyCommand


class GenericComponentCommand(UndoCommand):
    """Undo/redo for a native component document edit."""

    _is_property_edit = True
    MERGE_WINDOW: float = 0.3

    def __init__(self, comp: Any, old_document: dict, new_document: dict,
                 description: str = "", *, mergeable: bool = True):
        super().__init__(description or f"Edit {getattr(comp, 'type_name', 'Component')}")
        self._comp = comp
        self._old_document = copy.deepcopy(old_document)
        self._new_document = copy.deepcopy(new_document)
        self._comp_id: int = getattr(comp, "component_id", id(comp))
        self._game_object_id: int = _game_object_id_of(comp)
        self._comp_type_name: str = _comp_type_name_of(comp)
        self._mergeable = bool(mergeable)

    def _live(self):
        return _resolve_target(self._comp, self._game_object_id, self._comp_type_name)

    def execute(self) -> None:
        comp = self._live()
        if comp is None or not comp.deserialize_document(self._new_document):
            raise RuntimeError(f"GenericComponent('{self._comp_type_name}').execute failed")

    def undo(self) -> None:
        comp = self._live()
        if comp is None:
            Debug.log_error(
                f"[Undo] GenericComponent('{self._comp_type_name}').undo: not found")
            return
        if not comp.deserialize_document(self._old_document):
            raise RuntimeError(f"GenericComponent('{self._comp_type_name}').undo failed")

    def redo(self) -> None:
        comp = self._live()
        if comp is None:
            Debug.log_error(
                f"[Undo] GenericComponent('{self._comp_type_name}').redo: not found")
            return
        if not comp.deserialize_document(self._new_document):
            raise RuntimeError(f"GenericComponent('{self._comp_type_name}').redo failed")

    def can_merge(self, other: UndoCommand) -> bool:
        if not isinstance(other, GenericComponentCommand):
            return False
        return (self._mergeable
                and other._mergeable
                and self._comp_id == other._comp_id
                and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW)

    def merge(self, other: GenericComponentCommand) -> None:
        self._new_document = copy.deepcopy(other._new_document)
        self.timestamp = other.timestamp


class PythonComponentDocumentCommand(UndoCommand):
    """Undo/redo one complete Python serialized-field document edit."""

    _is_property_edit = True
    MERGE_WINDOW: float = 0.3

    def __init__(self, comp: Any, old_document: dict, new_document: dict,
                 description: str = "", edit_key: str = ""):
        super().__init__(description or f"Edit {_comp_type_name_of(comp)}")
        self._comp = comp
        self._old_document = copy.deepcopy(old_document)
        self._new_document = copy.deepcopy(new_document)
        self._comp_id: int = getattr(comp, "component_id", id(comp))
        self._game_object_id: int = _game_object_id_of(comp)
        self._comp_type_name: str = _comp_type_name_of(comp)
        self._edit_key = str(edit_key)

    def _live(self):
        if self._game_object_id and self._comp_id:
            from Infernux.engine.undo._helpers import _get_active_scene
            scene = _get_active_scene()
            obj = scene.find_by_id(self._game_object_id) if scene else None
            if obj is not None:
                for component in (obj.get_py_components() or ()):
                    if getattr(component, "component_id", 0) == self._comp_id:
                        return component
        return _resolve_target(self._comp, self._game_object_id, self._comp_type_name)

    @staticmethod
    def _apply(comp: Any, document: dict) -> None:
        if comp is None or not hasattr(comp, "_deserialize_fields_document"):
            raise RuntimeError("Python component document target is unavailable")
        comp._deserialize_fields_document(copy.deepcopy(document))

    def execute(self) -> None:
        self._apply(self._live() or self._comp, self._new_document)

    def undo(self) -> None:
        target = self._live()
        if target is None:
            Debug.log_error(
                f"[Undo] PythonComponent('{self._comp_type_name}').undo: not found"
            )
            return
        self._apply(target, self._old_document)

    def redo(self) -> None:
        target = self._live()
        if target is None:
            Debug.log_error(
                f"[Undo] PythonComponent('{self._comp_type_name}').redo: not found"
            )
            return
        self._apply(target, self._new_document)

    def can_merge(self, other: UndoCommand) -> bool:
        return (
            isinstance(other, PythonComponentDocumentCommand)
            and self._comp_id == other._comp_id
            and self._edit_key == other._edit_key
            and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW
        )

    def merge(self, other: PythonComponentDocumentCommand) -> None:
        self._new_document = copy.deepcopy(other._new_document)
        self.timestamp = other.timestamp


class MaterialDocumentCommand(UndoCommand):
    """Undo/redo for typed material edits followed by an atomic save."""

    _is_property_edit = False
    MERGE_WINDOW: float = 0.3
    marks_dirty: bool = False

    def __init__(self, material: Any, old_document: dict, new_document: dict,
                 description: str = "Edit Material",
                 refresh_callback: Optional[Callable[[Any], None]] = None,
                 edit_key: str = ""):
        super().__init__(description)
        self._material = material
        self._old_document = copy.deepcopy(old_document)
        self._new_document = copy.deepcopy(new_document)
        self._refresh_callback = refresh_callback
        self._material_id = self._stable_id(material)
        self._edit_key = edit_key or ""

    @staticmethod
    def _stable_id(material: Any) -> int:
        guid = getattr(material, "guid", "")
        if guid:
            return hash(("material-guid", guid))
        fp = getattr(material, "file_path", "")
        if fp:
            return hash(("material-file", fp))
        return id(material)

    def execute(self) -> None:
        self._apply(self._new_document)

    def undo(self) -> None:
        self._apply(self._old_document)

    def redo(self) -> None:
        self._apply(self._new_document)

    def can_merge(self, other: UndoCommand) -> bool:
        if not isinstance(other, MaterialDocumentCommand):
            return False
        return (self._material_id == other._material_id
                and self._edit_key == other._edit_key
                and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW)

    def merge(self, other: MaterialDocumentCommand) -> None:
        self._new_document = copy.deepcopy(other._new_document)
        self.timestamp = other.timestamp

    def _apply(self, document: dict) -> None:
        if not self._material.deserialize_document(document):
            raise RuntimeError("material document restore failed")
        save = getattr(self._material, "save", None)
        save_ok = False
        if callable(save):
            result = save()
            save_ok = bool(result) if result is not None else True
        if save_ok:
            fp = getattr(self._material, "file_path", "") or ""
            if fp:
                try:
                    from Infernux.core.assets import AssetManager
                    AssetManager.on_material_saved(fp)
                except Exception as exc:
                    Debug.log_suppressed("undo._property_commands.AssetManager.on_material_saved", exc)
        if self._refresh_callback:
            self._refresh_callback(self._material)


class ResourceDocumentCommand(UndoCommand):
    """Undo/redo a strict resource document and publish it through one callback."""

    _is_property_edit = False
    MERGE_WINDOW: float = 0.3
    marks_dirty: bool = False

    def __init__(self, resource: Any, old_document: dict, new_document: dict,
                 description: str = "Edit Resource",
                 publish_callback: Optional[Callable[[Any], None]] = None,
                 edit_key: str = ""):
        super().__init__(description)
        self._resource = resource
        self._old_document = copy.deepcopy(old_document)
        self._new_document = copy.deepcopy(new_document)
        self._publish_callback = publish_callback
        self._resource_id = self._stable_id(resource)
        self._edit_key = edit_key

    @staticmethod
    def _stable_id(resource: Any) -> int:
        guid = getattr(resource, "guid", "")
        if guid:
            return hash((type(resource), "guid", guid))
        native = getattr(resource, "native", None)
        file_path = getattr(resource, "file_path", "") or getattr(native, "file_path", "")
        if file_path:
            return hash((type(resource), "file", file_path))
        return id(resource)

    def execute(self) -> None:
        self._apply(self._new_document)

    def undo(self) -> None:
        self._apply(self._old_document)

    def redo(self) -> None:
        self._apply(self._new_document)

    def can_merge(self, other: UndoCommand) -> bool:
        if not isinstance(other, ResourceDocumentCommand):
            return False
        return (self._resource_id == other._resource_id
                and self._edit_key == other._edit_key
                and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW)

    def merge(self, other: ResourceDocumentCommand) -> None:
        self._new_document = copy.deepcopy(other._new_document)
        self.timestamp = other.timestamp

    def _apply(self, document: dict) -> None:
        result = self._resource.deserialize_document(document)
        if result is False:
            raise RuntimeError("resource document restore failed")
        if self._publish_callback is not None:
            self._publish_callback(self._resource)
            return
        save = getattr(self._resource, "save", None)
        if not callable(save):
            raise RuntimeError("resource document command requires a publish callback or save()")
        save_result = save()
        if save_result is False:
            raise RuntimeError("resource document save failed")


class SetMaterialSlotCommand(UndoCommand):
    """Undo/redo for MeshRenderer material-slot assignment."""

    _is_property_edit = True
    MERGE_WINDOW: float = 0.3

    def __init__(self, renderer, slot: int, old_guid: str, new_guid: str,
                 description: str = ""):
        super().__init__(description or f"Set Material Slot {slot}")
        self._renderer = renderer
        self._slot = slot
        self._old_guid = old_guid or ""
        self._new_guid = new_guid or ""
        self._game_object_id: int = _game_object_id_of(renderer)
        self._comp_type_name: str = _comp_type_name_of(renderer) if self._game_object_id else ""

    def _live(self):
        return _resolve_target(self._renderer, self._game_object_id, self._comp_type_name)

    def execute(self) -> None:
        target = self._live() or self._renderer
        target.set_material(self._slot, self._new_guid)

    def undo(self) -> None:
        target = self._live()
        if target is None:
            Debug.log_error(
                f"[Undo] SetMaterialSlot({self._slot}).undo: renderer not found "
                f"(go={self._game_object_id}, type={self._comp_type_name})")
            return
        target.set_material(self._slot, self._old_guid)

    def redo(self) -> None:
        target = self._live()
        if target is None:
            Debug.log_error(
                f"[Undo] SetMaterialSlot({self._slot}).redo: renderer not found "
                f"(go={self._game_object_id}, type={self._comp_type_name})")
            return
        target.set_material(self._slot, self._new_guid)

    def can_merge(self, other: UndoCommand) -> bool:
        if not isinstance(other, SetMaterialSlotCommand):
            return False
        return (self._game_object_id == other._game_object_id
                and self._comp_type_name == other._comp_type_name
                and self._slot == other._slot
                and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW)

    def merge(self, other: SetMaterialSlotCommand) -> None:
        self._new_guid = other._new_guid
        self.timestamp = other.timestamp
