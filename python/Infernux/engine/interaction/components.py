"""Panel-independent command authority for component authoring."""

from __future__ import annotations

from contextlib import contextmanager
import copy
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .action_journal import ActionOrigin


@dataclass(frozen=True, slots=True)
class ComponentDocumentEditResult:
    """Result of one complete serialized component document edit."""

    changed: bool
    value: Any = None


class ComponentCommandService:
    """Own every component mutation independently of Inspector and automation."""

    _instance: Optional["ComponentCommandService"] = None

    def __init__(self) -> None:
        ComponentCommandService._instance = self

    @classmethod
    def instance(cls) -> Optional["ComponentCommandService"]:
        return cls._instance

    @classmethod
    def require(cls) -> "ComponentCommandService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def shutdown(self) -> None:
        if ComponentCommandService._instance is self:
            ComponentCommandService._instance = None

    @contextmanager
    def user_action(
        self,
        description: str,
        *,
        origin: Optional[ActionOrigin] = None,
    ):
        manager = self._manager()
        with manager.user_action(
            str(description or "Edit Components"),
            origin=origin,
        ):
            yield

    def can_record(self) -> bool:
        manager = self._manager_or_none()
        return manager is not None

    @contextmanager
    def suppress_replay(self):
        manager = self._manager()
        with manager.suppress():
            yield

    def add(
        self,
        game_object: Any,
        component_type: str,
        *,
        python_instance: Any = None,
        native_document: Optional[dict] = None,
        initializer: Optional[Callable[[Any], None]] = None,
        invoke_after_deserialize: bool = False,
        target_component_id: int = 0,
        insert_after: bool = False,
        insert_at_start: bool = False,
        description: str = "",
        origin: Optional[ActionOrigin] = None,
    ) -> Any:
        from Infernux.engine.undo import AddComponentTransactionCommand

        type_name = str(component_type or "").strip()
        if not type_name:
            raise ValueError("component type must not be empty")
        object_id = int(getattr(game_object, "id", game_object) or 0)
        if object_id <= 0:
            raise ValueError("component add requires a live GameObject")
        python_instance = self._resolve_add_target(
            game_object,
            type_name,
            python_instance,
        )
        command = AddComponentTransactionCommand(
            object_id,
            type_name,
            native_document=copy.deepcopy(native_document),
            python_instance=python_instance,
            initializer=initializer,
            invoke_after_deserialize=invoke_after_deserialize,
            target_component_id=int(target_component_id or 0),
            insert_after=bool(insert_after),
            insert_at_start=bool(insert_at_start),
            description=description or f"Add {type_name}",
        )
        self._execute(command, origin=origin)
        component = command.result_component
        if component is None:
            raise RuntimeError(f"component add lost its result: {type_name}")
        return component

    @staticmethod
    def _resolve_add_target(
        game_object: Any,
        type_name: str,
        python_instance: Any,
    ) -> Any:
        """Resolve and validate one add request for every editor caller."""
        if python_instance is None:
            from Infernux.components.registry import (
                ensure_engine_component_catalog_loaded,
                get_type,
            )

            ensure_engine_component_catalog_loaded()
            component_class = get_type(type_name)
            if component_class is not None and not bool(
                getattr(component_class, "_cpp_type_name", "")
            ):
                python_instance = component_class()

        if python_instance is not None:
            from Infernux.components.registry import get_python_attachment_blockers

            blockers = get_python_attachment_blockers(
                game_object,
                type(python_instance),
            )
        else:
            blockers = tuple(game_object.get_add_component_blockers(type_name))

        if blockers:
            raise ValueError(
                f"cannot add '{type_name}': " + "; ".join(blockers)
            )
        return python_instance

    def remove(
        self,
        game_object: Any,
        component: Any,
        *,
        description: str = "",
        origin: Optional[ActionOrigin] = None,
    ) -> bool:
        from Infernux.engine.undo import (
            RemoveNativeComponentCommand,
            RemovePyComponentCommand,
        )

        object_id = int(getattr(game_object, "id", game_object) or 0)
        if object_id <= 0 or component is None:
            raise ValueError("component removal requires a live component")
        type_name = str(
            getattr(component, "type_name", "") or type(component).__name__
        )
        if self._is_python_component(component):
            command = RemovePyComponentCommand(
                object_id,
                component,
                description or f"Remove {type_name}",
            )
        else:
            command = RemoveNativeComponentCommand(
                object_id,
                type_name,
                component,
                description or f"Remove {type_name}",
            )
        manager = self._manager()
        return bool(manager.execute(command, origin=origin))

    def set_field(
        self,
        component: Any,
        field_name: str,
        value: Any,
        *,
        description: str = "",
    ) -> bool:
        field = str(field_name or "").strip()
        if component is None or not field:
            raise ValueError("component property edit requires a target and field")
        from .serialized_properties import (
            PropertyTransactionStatus,
            make_attribute_property_transaction,
            make_python_component_property_transaction,
        )

        text = description or f"Set {type(component).__name__}.{field}"
        if self._is_python_serialized_field(component, field):
            transaction = make_python_component_property_transaction(
                (component,),
                field,
                description=text,
            )
        else:
            transaction = make_attribute_property_transaction(
                (component,),
                field,
                property_path=f"{type(component).__name__}.{field}",
                description=text,
            )
        status = transaction.commit_or_raise(value)
        return status is PropertyTransactionStatus.APPLIED

    def execute_property_changes(
        self,
        changes: list[tuple[Any, str, Any, Any, str]],
        *,
        description: str,
        before_selection: Any = None,
        after_selection: Any = None,
        origin: Optional[ActionOrigin] = None,
    ) -> bool:
        """Apply several component properties as one authoritative command."""
        command = self._property_changes_command(changes, description)
        if command is None:
            return False
        if before_selection is not None:
            command.before_selection_snapshot = before_selection
        if after_selection is not None:
            command.after_selection_snapshot = after_selection
        return bool(self._manager().execute(command, origin=origin))

    def record_applied_property_changes(
        self,
        changes: list[tuple[Any, str, Any, Any, str]],
        *,
        description: str,
        before_context: Any = None,
        after_context: Any = None,
        before_selection: Any = None,
        after_selection: Any = None,
        origin: Optional[ActionOrigin] = None,
    ) -> bool:
        """Record a live gesture, rolling the model back if history rejects it."""
        command = self._property_changes_command(changes, description)
        if command is None:
            return False
        if before_selection is not None:
            command.before_selection_snapshot = before_selection
        if after_selection is not None:
            command.after_selection_snapshot = after_selection
        manager = self._manager()
        if manager.record(
            command,
            before_context=before_context,
            after_context=after_context,
            origin=origin,
        ):
            return True
        with manager.suppress():
            for target, field, old_value, _new_value, _item_description in changes:
                setattr(target, str(field), copy.deepcopy(old_value))
        return False

    def edit_document(
        self,
        component: Any,
        edit: Callable[[], Any],
        *,
        description: str,
        edit_key: str = "",
        origin: Optional[ActionOrigin] = None,
    ) -> ComponentDocumentEditResult:
        """Capture, validate, and commit one aggregate component edit."""
        if component is None or not callable(edit):
            raise ValueError("component document edit requires a target and callback")
        manager = self._manager()
        before = self._capture_document(component)
        value = None
        after = None
        try:
            with manager.suppress():
                value = edit()
                after = self._capture_document(component)
        finally:
            self._restore_document(component, before)
        if after is None:
            raise RuntimeError("component edit did not produce a document")
        if before == after:
            return ComponentDocumentEditResult(False, value)
        command = self._document_command(
            component,
            before,
            after,
            description,
            edit_key=edit_key,
        )
        self._execute(command, origin=origin)
        return ComponentDocumentEditResult(True, value)

    def restore_document(
        self,
        component: Any,
        document: dict,
        *,
        description: str = "",
        edit_key: str = "",
        origin: Optional[ActionOrigin] = None,
    ) -> bool:
        if component is None or not isinstance(document, dict):
            raise ValueError("component restore requires a typed document")
        before = self._capture_document(component)
        after = copy.deepcopy(document)
        if before == after:
            return False
        command = self._document_command(
            component,
            before,
            after,
            description or f"Restore {type(component).__name__}",
            edit_key=edit_key,
            mergeable=False,
        )
        self._execute(command, origin=origin)
        return True

    def restore_many(
        self,
        edits: list[tuple[Any, dict, str, str]],
        *,
        description: str = "",
        origin: Optional[ActionOrigin] = None,
    ) -> bool:
        """Restore multiple component documents as one atomic user action."""
        from Infernux.engine.undo import CompoundCommand

        commands = []
        for component, document, item_description, edit_key in edits:
            if component is None or not isinstance(document, dict):
                raise ValueError("component restore requires typed component documents")
            before = self._capture_document(component)
            after = copy.deepcopy(document)
            if before == after:
                continue
            commands.append(
                self._document_command(
                    component,
                    before,
                    after,
                    item_description or f"Restore {type(component).__name__}",
                    edit_key=str(edit_key or ""),
                    mergeable=False,
                )
            )
        if not commands:
            return False
        command = commands[0] if len(commands) == 1 else CompoundCommand(
            commands,
            description or f"Edit {len(commands)} Components",
        )
        self._execute(command, origin=origin)
        return True

    def remove_many(
        self,
        entries: list[tuple[Any, Any]],
        *,
        before_selection: Any,
        after_selection: Any,
        description: str = "",
        origin: Optional[ActionOrigin] = None,
    ) -> bool:
        """Remove components and restore their selection atomically on Undo."""
        from Infernux.engine.undo import (
            RemoveComponentsCommand,
            RemoveNativeComponentCommand,
            RemovePyComponentCommand,
        )

        commands = []
        for game_object, component in entries:
            object_id = int(getattr(game_object, "id", game_object) or 0)
            if object_id <= 0 or component is None:
                raise ValueError("component removal requires live components")
            type_name = str(
                getattr(component, "type_name", "") or type(component).__name__
            )
            if self._is_python_component(component):
                commands.append(RemovePyComponentCommand(object_id, component))
            else:
                commands.append(
                    RemoveNativeComponentCommand(object_id, type_name, component)
                )
        if not commands:
            return False
        command = RemoveComponentsCommand(
            commands,
            before_selection,
            after_selection,
            description
            or (commands[0].description if len(commands) == 1 else f"Remove {len(commands)} Components"),
        )
        return bool(self._manager().execute(command, origin=origin))

    def reorder(
        self,
        changes: list[tuple[int, tuple[int, ...], tuple[int, ...]]],
        *,
        description: str = "Reorder Components",
        origin: Optional[ActionOrigin] = None,
    ) -> bool:
        """Commit one complete component-order change set."""
        if not changes:
            return False
        from Infernux.engine.undo import ReorderComponentsCommand

        return bool(
            self._manager().execute(
                ReorderComponentsCommand(changes, description),
                origin=origin,
            )
        )

    def commit_documents(
        self,
        component: Any,
        before: dict,
        after: dict,
        *,
        description: str = "",
        edit_key: str = "",
        restore_before_execute: bool = False,
        origin: Optional[ActionOrigin] = None,
    ) -> bool:
        """Commit documents already captured by a specialized Inspector drawer."""
        old_document = copy.deepcopy(before)
        new_document = copy.deepcopy(after)
        if old_document == new_document:
            return False
        if restore_before_execute:
            self._restore_document(component, old_document)
        command = self._document_command(
            component,
            old_document,
            new_document,
            description or f"Edit {type(component).__name__}",
            edit_key=edit_key,
        )
        self._execute(command, origin=origin)
        return True

    def set_material_slot(
        self,
        renderer: Any,
        slot: int,
        old_guid: str,
        new_guid: str,
        *,
        description: str = "",
        origin: Optional[ActionOrigin] = None,
    ) -> bool:
        if str(old_guid or "") == str(new_guid or ""):
            return False
        from Infernux.engine.undo import SetMaterialSlotCommand

        self._execute(
            SetMaterialSlotCommand(
                renderer,
                int(slot),
                str(old_guid or ""),
                str(new_guid or ""),
                description or f"Set Material Slot {int(slot)}",
            ),
            origin=origin,
        )
        return True

    @staticmethod
    def _is_python_component(component: Any) -> bool:
        if getattr(component, "_cpp_type_name", ""):
            return False
        try:
            from Infernux.components.component import InxComponent

            if isinstance(component, InxComponent):
                return True
        except ImportError:
            pass
        if getattr(component, "_script_guid", ""):
            return True
        return (
            callable(getattr(component, "_serialize_fields_document", None))
            and not callable(getattr(component, "serialize_document", None))
        )

    @staticmethod
    def _is_cpp_property_field(component: Any, field: str) -> bool:
        for klass in type(component).__mro__:
            desc = klass.__dict__.get(field)
            if desc is not None:
                return bool(getattr(desc, "_is_cpp_property", False))
        return False

    @classmethod
    def _is_python_serialized_field(cls, component: Any, field: str) -> bool:
        if cls._is_cpp_property_field(component, field):
            return False
        if not cls._is_python_component(component):
            return False
        from Infernux.components.fields import get_serialized_fields

        return field in get_serialized_fields(type(component))

    @staticmethod
    def _property_changes_command(
        changes: list[tuple[Any, str, Any, Any, str]],
        description: str,
    ):
        from Infernux.engine.undo import CompoundCommand, SetPropertyCommand

        commands = [
            SetPropertyCommand(
                target,
                str(field),
                copy.deepcopy(old_value),
                copy.deepcopy(new_value),
                str(item_description or description or f"Set {field}"),
            )
            for target, field, old_value, new_value, item_description in changes
            if old_value != new_value
        ]
        if not commands:
            return None
        return commands[0] if len(commands) == 1 else CompoundCommand(
            commands,
            str(description or "Edit Component Properties"),
        )

    @classmethod
    def _capture_document(cls, component: Any) -> dict:
        if cls._is_python_component(component):
            serializer = getattr(component, "_serialize_fields_document", None)
            if not callable(serializer):
                raise TypeError(
                    f"{type(component).__name__} has no Python component document"
                )
            return copy.deepcopy(serializer())
        serializer = getattr(component, "serialize_document", None)
        if callable(serializer):
            return copy.deepcopy(serializer())
        raise TypeError(
            f"{type(component).__name__} does not expose a serialized document"
        )

    @classmethod
    def _restore_document(cls, component: Any, document: dict) -> None:
        if cls._is_python_component(component):
            restore = getattr(component, "_deserialize_fields_document", None)
            if not callable(restore):
                raise TypeError(
                    f"{type(component).__name__} has no Python component document"
                )
            restore(copy.deepcopy(document))
            return
        restore = getattr(component, "deserialize_document", None)
        if callable(restore) and restore(copy.deepcopy(document)) is not False:
            return
        raise RuntimeError(
            f"{type(component).__name__} rejected its serialized document"
        )

    @classmethod
    def _document_command(
        cls,
        component: Any,
        before: dict,
        after: dict,
        description: str,
        *,
        edit_key: str,
        mergeable: bool = True,
    ):
        if cls._is_python_component(component):
            from Infernux.engine.undo import PythonComponentDocumentCommand

            return PythonComponentDocumentCommand(
                component,
                before,
                after,
                description,
                edit_key=edit_key,
            )
        from Infernux.engine.undo import GenericComponentCommand

        return GenericComponentCommand(
            component,
            before,
            after,
            description,
            mergeable=mergeable,
        )

    @staticmethod
    def _manager_or_none():
        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            return None
        return manager

    @classmethod
    def _manager(cls):
        manager = cls._manager_or_none()
        if manager is None:
            raise RuntimeError("Component edit requires the global Action Journal")
        return manager

    @classmethod
    def _execute(cls, command: Any, *, origin: Optional[ActionOrigin]) -> None:
        manager = cls._manager()
        if not manager.execute(command, origin=origin):
            raise RuntimeError(f"Component edit was rejected: {command.description}")
