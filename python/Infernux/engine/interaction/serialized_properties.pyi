from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence
from Infernux.engine.undo._base import UndoCommand

class PropertyTransactionStatus(str, Enum):
    APPLIED: PropertyTransactionStatus
    NO_CHANGE: PropertyTransactionStatus
    REJECTED: PropertyTransactionStatus

class FieldSchema:
    property_path: str
    value_type: str
    read_only: bool
    attributes: Mapping[str, Any]
    def __init__(self, property_path: str, value_type: str, read_only: bool = False, attributes: Mapping[str, Any] = ...) -> None: ...

class SerializedObjectView:
    target_ids: tuple[str, ...]
    revision: int
    def __init__(self, target_ids: tuple[str, ...], revision: int = 0) -> None: ...

class SerializedPropertyBinding:
    target_id: str
    read: Callable[[], Any]
    command_factory: Callable[[Any, Any, str], UndoCommand]
    normalize: Callable[[Any], Any]
    validate: Callable[[Any], str]
    equivalent: Callable[[Any, Any], bool]
    def __init__(self, target_id: str, read: Callable[[], Any], command_factory: Callable[[Any, Any, str], UndoCommand], normalize: Callable[[Any], Any] = ..., validate: Callable[[Any], str] = ..., equivalent: Callable[[Any, Any], bool] = ...) -> None: ...

class SerializedPropertyHandle:
    schema: FieldSchema
    object_view: SerializedObjectView
    bindings: tuple[SerializedPropertyBinding, ...]
    publish: Optional[Callable[[], None]]
    marks_dirty: bool
    property_path: str
    values: tuple[Any, ...]
    value: Any
    mixed: bool
    def __init__(self, schema: FieldSchema, object_view: SerializedObjectView, bindings: tuple[SerializedPropertyBinding, ...], publish: Optional[Callable[[], None]] = ..., marks_dirty: bool = True) -> None: ...
    def prepare(self, candidate: Any) -> tuple[tuple[Any, ...], tuple[Any, ...]]: ...

class PropertyTransaction:
    handle: SerializedPropertyHandle
    description: str
    clear_value: Any
    on_rejected: Optional[Callable[[str], None]]
    value_type: str
    value: Any
    read_only: bool
    mixed: bool
    def __init__(self, handle: SerializedPropertyHandle, description: str = "", clear_value: Any = ..., on_rejected: Optional[Callable[[str], None]] = ...) -> None: ...
    def commit(self, candidate: Any) -> PropertyTransactionStatus: ...
    def clear(self) -> PropertyTransactionStatus: ...
    def commit_or_raise(self, candidate: Any) -> PropertyTransactionStatus: ...
    def clear_or_raise(self) -> PropertyTransactionStatus: ...

class SnapshotPropertyTransaction:
    target_key: str
    capture: Callable[[], Any]
    restore: Callable[[Any], None]
    description: str
    value_type: str
    normalize: Callable[[Any], Any]
    clear_value: Any
    equivalent: Callable[[Any, Any], bool]
    marks_dirty: bool
    mergeable: bool
    on_rejected: Optional[Callable[[str], None]]
    value: Any
    read_only: bool
    mixed: bool
    def __init__(self, target_key: str, capture: Callable[[], Any], restore: Callable[[Any], None], description: str = ..., value_type: str = ..., normalize: Callable[[Any], Any] = ..., clear_value: Any = ..., equivalent: Callable[[Any, Any], bool] = ..., marks_dirty: bool = ..., mergeable: bool = ..., on_rejected: Optional[Callable[[str], None]] = ...) -> None: ...
    def commit(self, candidate: Any) -> PropertyTransactionStatus: ...
    def clear(self) -> PropertyTransactionStatus: ...
    def commit_or_raise(self, candidate: Any) -> PropertyTransactionStatus: ...
    def clear_or_raise(self) -> PropertyTransactionStatus: ...

def make_attribute_property_transaction(targets: Sequence[Any], attribute: str, *, property_path: str = ..., value_type: str = ..., description: str = ..., read_only: bool = ..., normalize: Callable[[Any], Any] = ..., validate: Callable[[Any], str] = ..., equivalent: Callable[[Any, Any], bool] = ..., publish: Optional[Callable[[], None]] = ..., clear_value: Any = ..., on_rejected: Optional[Callable[[str], None]] = ..., marks_dirty: bool = ...) -> PropertyTransaction: ...
def make_python_component_property_transaction(components: Sequence[Any], field_name: str, *, value_type: str = ..., description: str = ..., read_only: bool = ..., normalize: Callable[[Any], Any] = ..., validate: Callable[[Any], str] = ..., equivalent: Callable[[Any, Any], bool] = ..., clear_value: Any = ..., on_rejected: Optional[Callable[[str], None]] = ...) -> PropertyTransaction: ...
def make_native_document_property_transaction(components: Sequence[Any], field_name: str, *, value_type: str = ..., description: str = ..., normalize: Callable[[Any], Any] = ..., validate: Callable[[Any], str] = ..., equivalent: Callable[[Any, Any], bool] = ..., on_rejected: Optional[Callable[[str], None]] = ...) -> PropertyTransaction: ...

class PropertyDrawerRegistry:
    def register(self, drawer_id: str, factory: Callable[..., Any], *, replace: bool = False) -> None: ...
    def require(self, drawer_id: str) -> Callable[..., Any]: ...
    def create(self, drawer_id: str, **kwargs) -> Any: ...
    def ids(self) -> tuple[str, ...]: ...

property_drawer_registry: PropertyDrawerRegistry
