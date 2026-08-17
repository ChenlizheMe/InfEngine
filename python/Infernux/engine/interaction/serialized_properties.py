"""Authoritative serialized-property schemas, drawers, and transactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import copy
from typing import Any, Callable, Mapping, Optional, Sequence

from Infernux.engine.undo._base import CompoundCommand, UndoCommand


class PropertyTransactionStatus(str, Enum):
    APPLIED = "applied"
    NO_CHANGE = "no_change"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class FieldSchema:
    """Static structure for one serialized property."""

    property_path: str
    value_type: str
    read_only: bool = False
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        path = str(self.property_path or "").strip()
        value_type = str(self.value_type or "").strip()
        if not path:
            raise ValueError("serialized property path must not be empty")
        if not value_type:
            raise ValueError("serialized property type must not be empty")
        object.__setattr__(self, "property_path", path)
        object.__setattr__(self, "value_type", value_type)
        object.__setattr__(self, "attributes", dict(self.attributes))


@dataclass(frozen=True, slots=True)
class SerializedObjectView:
    """Stable identity and revision for one or more edited objects."""

    target_ids: tuple[str, ...]
    revision: int = 0

    def __post_init__(self) -> None:
        identities = tuple(str(value or "").strip() for value in self.target_ids)
        if not identities or any(not value for value in identities):
            raise ValueError("serialized object view requires stable target ids")
        if len(set(identities)) != len(identities):
            raise ValueError("serialized object view target ids must be unique")
        object.__setattr__(self, "target_ids", identities)
        object.__setattr__(self, "revision", max(0, int(self.revision)))


PropertyValidator = Callable[[Any], str]
PropertyNormalizer = Callable[[Any], Any]
PropertyComparator = Callable[[Any, Any], bool]
PropertyCommandFactory = Callable[[Any, Any, str], UndoCommand]
SnapshotCapture = Callable[[], Any]
SnapshotRestore = Callable[[Any], None]


def _publish_inspector_fields(targets: Sequence[Any], field_name: str) -> None:
    """Publish precise Editor invalidation without making Runtime depend on UI."""
    try:
        from Infernux.engine.ui.inspector_snapshot import invalidate_component_field
    except ImportError:
        return
    for target in targets:
        invalidate_component_field(target, field_name)


def _compose_field_publisher(
    targets: Sequence[Any],
    field_name: str,
    publisher: Optional[Callable[[], None]],
) -> Callable[[], None]:
    def _publish() -> None:
        if publisher is not None:
            publisher()
        _publish_inspector_fields(targets, field_name)

    return _publish


@dataclass(frozen=True, slots=True)
class SerializedPropertyBinding:
    """One target-specific endpoint behind a serialized property handle."""

    target_id: str
    read: Callable[[], Any]
    command_factory: PropertyCommandFactory
    normalize: PropertyNormalizer = lambda value: value
    validate: PropertyValidator = lambda _value: ""
    equivalent: PropertyComparator = lambda left, right: left == right

    def __post_init__(self) -> None:
        if not str(self.target_id or "").strip():
            raise ValueError("serialized property binding requires a target id")
        if not callable(self.read) or not callable(self.command_factory):
            raise TypeError("serialized property binding requires read and command factory")


@dataclass(frozen=True, slots=True)
class SerializedPropertyHandle:
    """Stable multi-target property endpoint used by every drawer."""

    schema: FieldSchema
    object_view: SerializedObjectView
    bindings: tuple[SerializedPropertyBinding, ...]
    publish: Optional[Callable[[], None]] = None
    marks_dirty: bool = True

    def __post_init__(self) -> None:
        bindings = tuple(self.bindings)
        if not bindings:
            raise ValueError("serialized property handle requires at least one binding")
        binding_ids = tuple(str(binding.target_id) for binding in bindings)
        if binding_ids != self.object_view.target_ids:
            raise ValueError("serialized property bindings must match object-view order")
        object.__setattr__(self, "bindings", bindings)

    @property
    def property_path(self) -> str:
        return self.schema.property_path

    @property
    def values(self) -> tuple[Any, ...]:
        return tuple(binding.read() for binding in self.bindings)

    @property
    def value(self) -> Any:
        return self.values[0]

    @property
    def mixed(self) -> bool:
        values = self.values
        first = values[0]
        return any(
            not binding.equivalent(first, value)
            for binding, value in zip(self.bindings[1:], values[1:])
        )

    def prepare(self, candidate: Any) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        before = self.values
        after = []
        errors = []
        for binding in self.bindings:
            try:
                value = binding.normalize(candidate)
                error = str(binding.validate(value) or "")
            except Exception as exc:
                errors.append(f"{binding.target_id}: {exc}")
                continue
            if error:
                errors.append(f"{binding.target_id}: {error}")
            after.append(value)
        if errors:
            raise ValueError("; ".join(errors))
        return before, tuple(after)


class _PropertyTransactionCommand(UndoCommand):
    """One journal entry containing all target writes and one publication."""

    _is_property_edit = True
    MERGE_WINDOW = 0.3

    def __init__(
        self,
        commands: Sequence[UndoCommand],
        description: str,
        *,
        publish: Optional[Callable[[], None]],
        marks_dirty: bool,
        merge_key: tuple[str, tuple[str, ...]],
    ) -> None:
        super().__init__(description)
        self._inner = CompoundCommand(list(commands), description)
        self._publish = publish
        self._merge_key = merge_key
        self.marks_dirty = bool(marks_dirty and self._inner.marks_dirty)

    def _notify(self) -> None:
        if self._publish is not None:
            self._publish()

    def execute(self) -> None:
        self._inner.execute()
        try:
            self._notify()
        except Exception:
            self._inner.undo()
            raise

    def undo(self) -> None:
        self._inner.undo()
        self._notify()

    def redo(self) -> None:
        self._inner.redo()
        self._notify()

    def dispose(self) -> None:
        self._inner.dispose()

    def bind_operation_id(self, operation_id: str) -> None:
        super().bind_operation_id(operation_id)
        self._inner.bind_operation_id(operation_id)

    def can_merge(self, other: UndoCommand) -> bool:
        if not isinstance(other, _PropertyTransactionCommand):
            return False
        if self._merge_key != other._merge_key:
            return False
        if (other.timestamp - self.timestamp) > self.MERGE_WINDOW:
            return False
        current = self._inner._commands
        incoming = other._inner._commands
        return (
            len(current) == len(incoming)
            and all(left.can_merge(right) for left, right in zip(current, incoming))
        )

    def merge(self, other: UndoCommand) -> None:
        if not isinstance(other, _PropertyTransactionCommand):
            raise TypeError("serialized-property transactions can only merge together")
        for current, incoming in zip(
            self._inner._commands, other._inner._commands
        ):
            current.merge(incoming)
        self.timestamp = other.timestamp


@dataclass(slots=True)
class PropertyTransaction:
    """Validate and commit one property candidate through the Action Journal."""

    handle: SerializedPropertyHandle
    description: str = ""
    clear_value: Any = None
    on_rejected: Optional[Callable[[str], None]] = None

    @property
    def value_type(self) -> str:
        return self.handle.schema.value_type

    @property
    def value(self) -> Any:
        return self.handle.value

    @property
    def read_only(self) -> bool:
        return self.handle.schema.read_only

    @property
    def mixed(self) -> bool:
        return self.handle.mixed

    def commit(self, candidate: Any) -> PropertyTransactionStatus:
        if self.handle.schema.read_only:
            return self._reject(f"{self.handle.property_path} is read-only")
        try:
            before, after = self.handle.prepare(candidate)
        except Exception as exc:
            return self._reject(str(exc))

        commands: list[UndoCommand] = []
        description = self.description or f"Set {self.handle.property_path}"
        try:
            for binding, old_value, new_value in zip(
                self.handle.bindings, before, after
            ):
                if binding.equivalent(old_value, new_value):
                    continue
                commands.append(
                    binding.command_factory(old_value, new_value, description)
                )
        except Exception as exc:
            for command in commands:
                command.dispose()
            return self._reject(str(exc))
        if not commands:
            return PropertyTransactionStatus.NO_CHANGE

        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        if manager is None:
            return self._reject("property edit requires an active UndoManager")
        command = _PropertyTransactionCommand(
            commands,
            description,
            publish=self.handle.publish,
            marks_dirty=self.handle.marks_dirty,
            merge_key=(
                self.handle.property_path,
                self.handle.object_view.target_ids,
            ),
        )
        if not manager.execute(command):
            return self._reject(f"property edit was rejected: {description}")
        return PropertyTransactionStatus.APPLIED

    def clear(self) -> PropertyTransactionStatus:
        return self.commit(self.clear_value)

    def commit_or_raise(self, candidate: Any) -> PropertyTransactionStatus:
        errors: list[str] = []
        previous = self.on_rejected

        def _capture(message: str) -> None:
            errors.append(str(message))
            if previous is not None:
                previous(str(message))

        self.on_rejected = _capture
        try:
            status = self.commit(candidate)
        finally:
            self.on_rejected = previous
        if status is PropertyTransactionStatus.REJECTED:
            raise RuntimeError(
                errors[-1] if errors else "serialized property edit was rejected"
            )
        return status

    def clear_or_raise(self) -> PropertyTransactionStatus:
        return self.commit_or_raise(self.clear_value)

    def _reject(self, message: str) -> PropertyTransactionStatus:
        if self.on_rejected is not None:
            self.on_rejected(str(message))
        return PropertyTransactionStatus.REJECTED


class _SnapshotPropertyCommand(UndoCommand):
    """Replay one explicitly captured property aggregate."""

    _is_property_edit = True
    MERGE_WINDOW = 0.3

    def __init__(
        self,
        target_key: str,
        before: Any,
        after: Any,
        restore: SnapshotRestore,
        description: str,
        *,
        marks_dirty: bool,
        mergeable: bool,
        gesture_id: str = "",
    ) -> None:
        super().__init__(description)
        self._target_key = str(target_key)
        self._before = copy.deepcopy(before)
        self._after = copy.deepcopy(after)
        self._restore = restore
        self._mergeable = bool(mergeable)
        self._gesture_id = str(gesture_id or "")
        self.marks_dirty = bool(marks_dirty)

    def execute(self) -> None:
        self._restore(copy.deepcopy(self._after))

    def undo(self) -> None:
        self._restore(copy.deepcopy(self._before))

    def redo(self) -> None:
        self.execute()

    def can_merge(self, other: UndoCommand) -> bool:
        if not (
            self._mergeable
            and isinstance(other, _SnapshotPropertyCommand)
            and other._mergeable
            and self._target_key == other._target_key
        ):
            return False

        # Pointer gestures have an explicit press/release boundary. Once either
        # command carries one, the legacy timing heuristic must not merge two
        # separate drags merely because the user started the next one quickly.
        if self._gesture_id or other._gesture_id:
            return bool(self._gesture_id) and self._gesture_id == other._gesture_id

        return (other.timestamp - self.timestamp) <= self.MERGE_WINDOW

    def merge(self, other: UndoCommand) -> None:
        if not isinstance(other, _SnapshotPropertyCommand):
            raise TypeError("snapshot property commands can only merge together")
        self._after = copy.deepcopy(other._after)
        self.timestamp = other.timestamp


@dataclass(slots=True)
class SnapshotPropertyTransaction:
    """Atomic transaction for an aggregate that cannot be addressed per field.

    This is the shared escape hatch for values such as a multi-object Transform
    edit.  The renderer supplies stable capture/restore endpoints; the
    Interaction Core still owns no-op detection, journal insertion, replay,
    merging, and dirty semantics.
    """

    target_key: str
    capture: SnapshotCapture
    restore: SnapshotRestore
    description: str = "Edit properties"
    value_type: str = ""
    normalize: PropertyNormalizer = lambda value: value
    clear_value: Any = None
    equivalent: PropertyComparator = lambda left, right: left == right
    marks_dirty: bool = True
    mergeable: bool = True
    gesture_id: str = ""
    on_rejected: Optional[Callable[[str], None]] = None

    def __post_init__(self) -> None:
        self.target_key = str(self.target_key or "").strip()
        self.value_type = str(self.value_type or "").strip()
        self.gesture_id = str(self.gesture_id or "").strip()
        if not self.target_key:
            raise ValueError("snapshot property transaction requires a target key")
        if not callable(self.capture) or not callable(self.restore):
            raise TypeError("snapshot property transaction requires capture and restore")

    @property
    def value(self) -> Any:
        return self.capture()

    @property
    def read_only(self) -> bool:
        return False

    @property
    def mixed(self) -> bool:
        return False

    def _commit(
        self,
        candidate: Any,
        *,
        mergeable: bool,
    ) -> PropertyTransactionStatus:
        try:
            before = copy.deepcopy(self.capture())
            after = copy.deepcopy(self.normalize(candidate))
            if self.equivalent(before, after):
                return PropertyTransactionStatus.NO_CHANGE
        except Exception as exc:
            return self._reject(str(exc))

        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        if manager is None:
            return self._reject("property edit requires an active UndoManager")
        command = _SnapshotPropertyCommand(
            self.target_key,
            before,
            after,
            self.restore,
            self.description or "Edit properties",
            marks_dirty=self.marks_dirty,
            mergeable=bool(mergeable and self.mergeable),
            gesture_id=self.gesture_id,
        )
        if not manager.execute(command, transaction_id=self.gesture_id):
            return self._reject(
                f"property edit was rejected: {self.description or self.target_key}"
            )
        return PropertyTransactionStatus.APPLIED

    def commit(self, candidate: Any) -> PropertyTransactionStatus:
        return self._commit(candidate, mergeable=True)

    def clear(self) -> PropertyTransactionStatus:
        # Clear is a discrete command, not another sample in a continuous edit.
        # Keeping it separate lets one Undo restore the value that was visible
        # immediately before the user pressed Clear.
        return self._commit(self.clear_value, mergeable=False)

    def commit_or_raise(self, candidate: Any) -> PropertyTransactionStatus:
        errors: list[str] = []
        previous = self.on_rejected

        def _capture(message: str) -> None:
            errors.append(str(message))
            if previous is not None:
                previous(str(message))

        self.on_rejected = _capture
        try:
            status = self.commit(candidate)
        finally:
            self.on_rejected = previous
        if status is PropertyTransactionStatus.REJECTED:
            raise RuntimeError(
                errors[-1] if errors else "snapshot property edit was rejected"
            )
        return status

    def clear_or_raise(self) -> PropertyTransactionStatus:
        errors: list[str] = []
        previous = self.on_rejected

        def _capture(message: str) -> None:
            errors.append(str(message))
            if previous is not None:
                previous(str(message))

        self.on_rejected = _capture
        try:
            status = self.clear()
        finally:
            self.on_rejected = previous
        if status is PropertyTransactionStatus.REJECTED:
            raise RuntimeError(
                errors[-1] if errors else "snapshot property clear was rejected"
            )
        return status

    def _reject(self, message: str) -> PropertyTransactionStatus:
        if self.on_rejected is not None:
            self.on_rejected(str(message))
        return PropertyTransactionStatus.REJECTED


def _serialized_target_id(target: Any) -> str:
    """Return the stable object identity used by serialized-property handles."""
    game_object = None
    resolver = getattr(target, "_try_get_game_object", None)
    if callable(resolver):
        try:
            game_object = resolver()
        except Exception:
            game_object = None
    if game_object is None:
        try:
            game_object = getattr(target, "game_object", None)
        except Exception:
            game_object = None
    game_object_id = int(getattr(game_object, "id", 0) or 0)
    component_id = int(getattr(target, "component_id", 0) or 0)
    type_name = str(
        getattr(target, "type_name", "") or type(target).__name__
    )
    if game_object_id and component_id:
        return f"component:{game_object_id}:{component_id}:{type_name}"
    if game_object_id:
        return f"object:{game_object_id}:{type_name}"
    try:
        from Infernux.engine.undo._helpers import _stable_target_id

        stable_id = int(_stable_target_id(target))
    except Exception:
        stable_id = id(target)
    return f"object:{stable_id}:{type_name}"


def make_attribute_property_transaction(
    targets: Sequence[Any],
    attribute: str,
    *,
    property_path: str = "",
    value_type: str = "Any",
    description: str = "",
    read_only: bool = False,
    normalize: PropertyNormalizer = lambda value: value,
    validate: PropertyValidator = lambda _value: "",
    equivalent: PropertyComparator = lambda left, right: left == right,
    publish: Optional[Callable[[], None]] = None,
    clear_value: Any = None,
    on_rejected: Optional[Callable[[str], None]] = None,
    marks_dirty: bool = True,
) -> PropertyTransaction:
    """Create the authoritative transaction for one attribute on N targets."""
    edited_targets = tuple(targets)
    attr = str(attribute or "").strip()
    if not edited_targets:
        raise ValueError("attribute property transaction requires at least one target")
    if not attr:
        raise ValueError("attribute property transaction requires an attribute")

    from Infernux.engine.undo import SetPropertyCommand

    ids = tuple(_serialized_target_id(target) for target in edited_targets)
    bindings = tuple(
        SerializedPropertyBinding(
            target_id=target_id,
            read=lambda target=target: getattr(target, attr),
            command_factory=(
                lambda old, new, text, target=target: SetPropertyCommand(
                    target, attr, old, new, text
                )
            ),
            normalize=normalize,
            validate=validate,
            equivalent=equivalent,
        )
        for target, target_id in zip(edited_targets, ids)
    )
    path = str(property_path or "").strip() or f"{type(edited_targets[0]).__name__}.{attr}"
    return PropertyTransaction(
        SerializedPropertyHandle(
            FieldSchema(path, str(value_type or "Any"), read_only=read_only),
            SerializedObjectView(ids),
            bindings,
            publish=_compose_field_publisher(edited_targets, attr, publish),
            marks_dirty=marks_dirty,
        ),
        description=description or f"Set {attr}",
        clear_value=clear_value,
        on_rejected=on_rejected,
    )


def make_python_component_property_transaction(
    components: Sequence[Any],
    field_name: str,
    *,
    value_type: str = "Any",
    description: str = "",
    read_only: bool = False,
    normalize: PropertyNormalizer = lambda value: value,
    validate: PropertyValidator = lambda _value: "",
    equivalent: PropertyComparator = lambda left, right: left == right,
    clear_value: Any = None,
    on_rejected: Optional[Callable[[str], None]] = None,
) -> PropertyTransaction:
    """Create one atomic typed-document edit for Python component fields."""
    edited_components = tuple(components)
    field = str(field_name or "").strip()
    if not edited_components:
        raise ValueError("Python component property transaction requires targets")
    if not field:
        raise ValueError("Python component property transaction requires a field")

    from Infernux.components.value_codec import VALUE_CODECS
    from Infernux.engine.undo import PythonComponentDocumentCommand

    ids = tuple(_serialized_target_id(comp) for comp in edited_components)
    bindings = []
    for component, target_id in zip(edited_components, ids):
        serializer = getattr(component, "_serialize_fields_document", None)
        if not callable(serializer):
            raise TypeError(
                f"{type(component).__name__} does not expose a serialized document"
            )
        try:
            from Infernux.components.fields import get_serialized_fields

            field_metadata = get_serialized_fields(type(component)).get(field)
        except Exception:
            field_metadata = None

        def _validate_candidate(
            candidate,
            component=component,
            field_metadata=field_metadata,
        ):
            message = str(validate(candidate) or "")
            if message:
                return message
            encoded = VALUE_CODECS.encode(
                candidate, f"{type(component).__name__}.{field}"
            )
            if field_metadata is not None:
                VALUE_CODECS.validate(
                    encoded,
                    field_metadata,
                    f"{type(component).__name__}.{field}",
                )
            return ""

        def _command_factory(old, new, text, component=component):
            old_document = component._serialize_fields_document()
            new_document = copy.deepcopy(old_document)
            new_document[field] = VALUE_CODECS.encode(
                new, f"{type(component).__name__}.{field}"
            )
            return PythonComponentDocumentCommand(
                component,
                old_document,
                new_document,
                text,
                edit_key=field,
            )

        bindings.append(
            SerializedPropertyBinding(
                target_id=target_id,
                read=lambda component=component: getattr(component, field),
                command_factory=_command_factory,
                normalize=normalize,
                validate=_validate_candidate,
                equivalent=equivalent,
            )
        )

    def _publish() -> None:
        for component in edited_components:
            callback = getattr(component, "_call_on_validate", None)
            if callable(callback):
                callback()
        _publish_inspector_fields(edited_components, field)

    return PropertyTransaction(
        SerializedPropertyHandle(
            FieldSchema(
                f"{type(edited_components[0]).__name__}.{field}",
                str(value_type or "Any"),
                read_only=read_only,
            ),
            SerializedObjectView(ids),
            tuple(bindings),
            publish=_publish,
        ),
        description=description or f"Set {field}",
        clear_value=clear_value,
        on_rejected=on_rejected,
    )


def make_native_document_property_transaction(
    components: Sequence[Any],
    field_name: str,
    *,
    value_type: str = "Any",
    description: str = "",
    normalize: PropertyNormalizer = lambda value: value,
    validate: PropertyValidator = lambda _value: "",
    equivalent: PropertyComparator = lambda left, right: left == right,
    on_rejected: Optional[Callable[[str], None]] = None,
) -> PropertyTransaction:
    """Create one atomic native component document-field transaction."""
    edited_components = tuple(components)
    field = str(field_name or "").strip()
    if not edited_components or not field:
        raise ValueError("native document property transaction requires targets and field")

    from Infernux.engine.undo import GenericComponentCommand

    ids = tuple(_serialized_target_id(comp) for comp in edited_components)
    bindings = []
    for component, target_id in zip(edited_components, ids):
        serializer = getattr(component, "serialize_document", None)
        if not callable(serializer):
            raise TypeError(
                f"{type(component).__name__} does not expose serialize_document()"
            )

        def _read(component=component):
            return component.serialize_document().get(field)

        def _command_factory(old, new, text, component=component):
            old_document = component.serialize_document()
            new_document = copy.deepcopy(old_document)
            new_document[field] = copy.deepcopy(new)
            return GenericComponentCommand(
                component, old_document, new_document, text
            )

        bindings.append(
            SerializedPropertyBinding(
                target_id=target_id,
                read=_read,
                command_factory=_command_factory,
                normalize=normalize,
                validate=validate,
                equivalent=equivalent,
            )
        )

    return PropertyTransaction(
        SerializedPropertyHandle(
            FieldSchema(
                f"{type(edited_components[0]).__name__}.{field}",
                str(value_type or "Any"),
            ),
            SerializedObjectView(ids),
            tuple(bindings),
            publish=_compose_field_publisher(edited_components, field, None),
        ),
        description=description or f"Set {field}",
        on_rejected=on_rejected,
    )


class PropertyDrawerRegistry:
    """Single factory registry for serialized-property drawers."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., Any]] = {}

    def register(
        self,
        drawer_id: str,
        factory: Callable[..., Any],
        *,
        replace: bool = False,
    ) -> None:
        key = str(drawer_id or "").strip().casefold()
        if not key:
            raise ValueError("property drawer id must not be empty")
        if not callable(factory):
            raise TypeError("property drawer factory must be callable")
        if key in self._factories and not replace:
            raise ValueError(f"property drawer is already registered: {drawer_id}")
        self._factories[key] = factory

    def require(self, drawer_id: str) -> Callable[..., Any]:
        key = str(drawer_id or "").strip().casefold()
        factory = self._factories.get(key)
        if factory is None:
            raise KeyError(f"unknown property drawer: {drawer_id}")
        return factory

    def create(self, drawer_id: str, **kwargs) -> Any:
        return self.require(drawer_id)(**kwargs)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


property_drawer_registry = PropertyDrawerRegistry()
