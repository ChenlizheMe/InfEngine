from enum import IntFlag
from typing import Any, Callable, Optional, Sequence
from .search import SearchQueryModel
from .serialized_properties import PropertyTransaction

class ObjectFieldGesture(IntFlag):
    NONE: ObjectFieldGesture
    LOCATE: ObjectFieldGesture
    OPEN_PICKER: ObjectFieldGesture
    OPEN: ObjectFieldGesture
    KEYBOARD_OPEN: ObjectFieldGesture
    CLEAR: ObjectFieldGesture
    CONTEXT_MENU: ObjectFieldGesture

PickerProvider = Callable[[str], Sequence[tuple[str, Any]]]

ASSET_REFERENCE_OPEN_COMMAND: str
ASSET_REFERENCE_REVEAL_COMMAND: str
ASSET_REFERENCE_COPY_COMMAND: str
ASSET_REFERENCE_PASTE_COMMAND: str
ASSET_REFERENCE_CLEAR_COMMAND: str

class AssetReferenceCatalog:
    def invalidate(self) -> None: ...
    def items(self, asset_type: str, query: str) -> tuple[tuple[str, str], ...]: ...
    def provider(self, asset_type: str) -> PickerProvider: ...

class ObjectPickerModel:
    def request_open(self, field_id: str) -> None: ...
    def open_requested(self, field_id: str) -> bool: ...
    def confirm_open(self, field_id: str) -> None: ...
    def consume_focus_request(self, field_id: str) -> bool: ...
    def query(self, field_id: str) -> str: ...
    def set_query(self, field_id: str, value: str) -> bool: ...
    def query_model(self, field_id: str) -> SearchQueryModel: ...
    def close(self, field_id: str) -> None: ...

class ObjectReferenceFieldModel:
    field_id: str
    display_text: str
    type_hint: str
    selected: bool
    clickable: bool
    accept: Optional[str | Sequence[str]]
    scene_items: Optional[PickerProvider]
    asset_items: Optional[PickerProvider]
    on_drop: Optional[Callable[[Any], None]]
    on_pick: Optional[Callable[[Any], None]]
    on_clear: Optional[Callable[[], None]]
    on_locate: Optional[Callable[[], None]]
    on_open: Optional[Callable[[], None]]
    ping_path: Optional[str]
    semantic_id: str
    has_value: Optional[bool]
    def __init__(self, field_id: str, display_text: str, type_hint: str, selected: bool = ..., clickable: bool = ..., accept: Optional[str | Sequence[str]] = ..., scene_items: Optional[PickerProvider] = ..., asset_items: Optional[PickerProvider] = ..., on_drop: Optional[Callable[[Any], None]] = ..., on_pick: Optional[Callable[[Any], None]] = ..., on_clear: Optional[Callable[[], None]] = ..., on_locate: Optional[Callable[[], None]] = ..., on_open: Optional[Callable[[], None]] = ..., ping_path: Optional[str] = ..., semantic_id: str = ..., has_value: Optional[bool] = ...) -> None: ...
    @property
    def has_picker(self) -> bool: ...
    @property
    def can_accept_drop(self) -> bool: ...
    def dispatch_chrome(self, flags: int) -> ObjectFieldGesture: ...
    def dispatch_picker(self, intent: Optional[tuple[str, Any]]) -> None: ...
    def dispatch_drop(self, value: Any) -> None: ...
    def locate(self) -> None: ...

class AssetReferenceFieldModel(ObjectReferenceFieldModel):
    asset_type: str
    on_assign: Optional[Callable[[Any], None]]
    on_rejected: Optional[Callable[[str], None]]
    additional_asset_items: Optional[PickerProvider]
    reference_value: Any
    transaction: Optional[PropertyTransaction]
    alternate_compatibility: Optional[Callable[[Any], str]]
    field_read_only: bool
    read_only: bool
    mixed: bool
    can_clear: bool
    can_accept_drop: bool
    def __init__(self, field_id: str, display_text: str, type_hint: str, selected: bool = ..., clickable: bool = ..., accept: Optional[str | Sequence[str]] = ..., scene_items: Optional[PickerProvider] = ..., on_clear: Optional[Callable[[], None]] = ..., on_locate: Optional[Callable[[], None]] = ..., on_open: Optional[Callable[[], None]] = ..., ping_path: Optional[str] = ..., semantic_id: str = ..., has_value: Optional[bool] = ..., asset_type: str = ..., on_assign: Optional[Callable[[Any], None]] = ..., on_rejected: Optional[Callable[[str], None]] = ..., additional_asset_items: Optional[PickerProvider] = ..., reference_value: Any = ..., transaction: Optional[PropertyTransaction] = ..., alternate_compatibility: Optional[Callable[[Any], str]] = ..., field_read_only: bool = ...) -> None: ...
    def dispatch_chrome(self, flags: int) -> ObjectFieldGesture: ...
    def dispatch_picker(self, intent: Optional[tuple[str, Any]]) -> None: ...
    def dispatch_drop(self, value: Any) -> None: ...
    def copy_reference_text(self) -> str: ...
    def can_paste_reference(self, text: str) -> bool: ...
    def dispatch_paste_reference(self, text: str) -> bool: ...
    def clear_reference(self) -> bool: ...

class AssetReferenceCommandTarget:
    model: AssetReferenceFieldModel
    clipboard_text: str
    clipboard_writer: Optional[Callable[[str], None]]
    def __init__(self, model: AssetReferenceFieldModel, clipboard_text: str = "", clipboard_writer: Optional[Callable[[str], None]] = ...) -> None: ...

def asset_reference_command_payload(model: AssetReferenceFieldModel, *, clipboard_text: str = "", clipboard_writer: Optional[Callable[[str], None]] = ...) -> dict[str, Any]: ...
def register_asset_reference_commands(registry: Any = ...) -> None: ...

asset_reference_catalog: AssetReferenceCatalog
object_picker_model: ObjectPickerModel
