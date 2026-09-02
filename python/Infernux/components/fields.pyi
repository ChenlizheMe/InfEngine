"""Type stubs for Infernux.components.fields."""

from __future__ import annotations

from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, TYPE_CHECKING, TypeVar, Union
from dataclasses import dataclass

if TYPE_CHECKING:
    from .component import InxComponent

_T = TypeVar("_T")


class FieldType(Enum):
    """Supported field types for serialization and inspector rendering."""
    INT = auto()
    FLOAT = auto()
    BOOL = auto()
    STRING = auto()
    VEC2 = auto()
    VEC3 = auto()
    VEC4 = auto()
    COLOR = auto()
    GAME_OBJECT = auto()
    COMPONENT = auto()
    MATERIAL = auto()
    TEXTURE = auto()
    SHADER = auto()
    ASSET = auto()
    ENUM = auto()
    LIST = auto()
    SERIALIZABLE_OBJECT = auto()
    ANIMATION_CURVE = auto()
    GRADIENT = auto()
    UNKNOWN = auto()


@dataclass(frozen=True)
class Range:
    lo: float
    hi: float
    slider: bool = ...

@dataclass(frozen=True)
class Tooltip:
    text: str

@dataclass(frozen=True)
class Header:
    text: str

@dataclass(frozen=True)
class Space:
    height: float = ...

@dataclass(frozen=True)
class Group:
    name: str

@dataclass(frozen=True)
class InfoText:
    text: str

@dataclass(frozen=True)
class DragSpeed:
    speed: float

@dataclass(frozen=True)
class RequiredComponent:
    type_name: str

@dataclass(frozen=True)
class FormerlySerializedAs:
    name: str

class Multiline: ...
class ReadOnly: ...
class HideInInspector: ...
class NonSerialized: ...
class HDR: ...

class Color:
    def __new__(
        cls,
        r: float = ...,
        g: float = ...,
        b: float = ...,
        a: float = ...,
    ) -> list[float]: ...


@dataclass
class FieldMetadata:
    """Metadata for a serialized field."""
    name: str
    field_type: FieldType
    default: Any
    range: Optional[Tuple[float, float]] = ...
    tooltip: str = ...
    display_name_key: str = ...
    readonly: bool = ...
    header: str = ...
    space: float = ...
    enum_type: Optional[Type[Enum]] = ...
    enum_labels: Optional[list] = ...
    element_type: Optional[FieldType] = ...
    group: str = ...
    info_text: str = ...
    multiline: bool = ...
    slider: bool = ...
    drag_speed: Optional[float] = ...
    required_component: Optional[str] = ...
    visible_when: Optional[Callable] = ...
    element_class: Optional[Type] = ...
    serializable_class: Optional[Type] = ...
    component_type: Optional[str] = ...
    hdr: bool = ...
    curve_non_negative: bool = ...
    asset_type: Optional[str] = ...
    python_type: Optional[Type] = ...
    getter: Optional[Callable] = ...
    setter: Optional[Callable] = ...
    hidden: bool = ...
    former_names: tuple[str, ...] = ...


class SerializedFieldDescriptor:
    """Descriptor that handles get/set for serialized fields."""
    metadata: FieldMetadata
    def __init__(self, metadata: FieldMetadata) -> None: ...
    def __set_name__(self, owner: Type, name: str) -> None: ...
    def get_raw(self, instance: InxComponent) -> Any:
        """Get the raw stored value without auto-resolution."""
        ...
    def __get__(self, instance: Optional[InxComponent], owner: Type) -> Any: ...
    def __set__(self, instance: InxComponent, value: Any) -> None: ...
    def __delete__(self, instance: InxComponent) -> None: ...


class HiddenField:
    """Marker class for fields hidden from serialization and Inspector."""
    default: Any
    def __init__(self, default: Any = ...) -> None: ...
    def __set_name__(self, owner: type, name: str) -> None: ...
    def __get__(self, obj: Any, objtype: Any = ...) -> Any: ...
    def __set__(self, obj: Any, value: Any) -> None: ...


def infer_field_type_from_value(value: Any) -> FieldType:
    """Infer FieldType from a runtime value."""
    ...

def get_raw_field_value(component: InxComponent, field_name: str) -> Any:
    """Get the raw stored value of a serialized field (bypasses auto-resolve).

    For COMPONENT fields this returns the underlying ``ComponentRef``
    instead of the resolved component.
    """
    ...

def resolve_annotation(annotation: Any) -> Optional[FieldMetadata]:
    """Resolve a type annotation to a FieldMetadata with an appropriate default.

    Handles single reference types (``Camera``, ``GameObject``, etc.)
    and list generics (``list[Camera]``, ``list[GameObject]``, etc.).
    """
    ...


def serialized_field(
    default: _T = ...,
    *,
    field_type: Optional[FieldType] = ...,
    element_type: Optional[FieldType] = ...,
    element_class: Optional[Type] = ...,
    serializable_class: Optional[Type] = ...,
    component_type: Optional[str] = ...,
    asset_type: Optional[str] = ...,
    range: Optional[Tuple[float, float]] = ...,
    tooltip: str = ...,
    display_name_key: str = ...,
    enum_labels: Optional[List[str]] = ...,
    readonly: bool = ...,
    header: str = ...,
    space: float = ...,
    group: str = ...,
    info_text: str = ...,
    multiline: bool = ...,
    slider: bool = ...,
    drag_speed: Optional[float] = ...,
    required_component: Optional[str] = ...,
    visible_when: Optional[Callable] = ...,
    hdr: bool = ...,
    curve_non_negative: bool = ...,
    hidden: bool = ...,
) -> _T:
    """Mark a field as serialized and inspector-visible.

    Args:
        default: Default value for the field.
        field_type: Explicit field type (auto-detected if not provided).
        element_type: For LIST fields, the element FieldType.
        element_class: For LIST fields, the SerializableObject subclass for elements.
        serializable_class: For SERIALIZABLE_OBJECT fields, the concrete class.
        component_type: For COMPONENT fields, the target component type name.
        asset_type: For ASSET fields, the registered asset type name.
        range: ``(min, max)`` tuple for numeric sliders / bounded drag.
        tooltip: Hover text shown in inspector.
        display_name_key: Optional i18n key used for the Inspector label.
        readonly: If ``True``, field is read-only in inspector.
        header: Group header text shown above this field.
        space: Vertical spacing before this field in inspector.
        group: Collapsible group name.
        info_text: Non-editable description line (dimmed) below the field.
        multiline: Use multiline text input for STRING fields.
        slider: Widget style when range is set (True = slider, False = drag).
        drag_speed: Override default drag speed for numeric fields.
        required_component: For GAME_OBJECT fields only.
        visible_when: ``fn(component) -> bool``; hides field when False.
        hdr: For COLOR fields only.  Allow HDR values (> 1.0).
        curve_non_negative: For ANIMATION_CURVE fields, constrain key values to >= 0.
        hidden: Serialize without showing the field in the Inspector.

    Example::

        class MyComponent(InxComponent):
            speed: float = serialized_field(default=5.0, range=(0, 100))
    """
    ...

def validate_serialized_field_document(
    document: Dict[str, Any],
    fields: Dict[str, FieldMetadata],
    *,
    owner_name: str,
    metadata_keys: set[str] | frozenset[str] = ...,
    allow_missing: bool = False,
    allow_unknown: bool = False,
) -> None: ...


def hide_field(default: Any = ...) -> Any:
    """Mark a class-level field as hidden (not serialized, not in Inspector)."""
    ...


def set_field_change_hooks(
    will_change: Optional[Callable] = None,
    did_change: Optional[Callable] = None,
) -> None:
    """Set global hooks called before/after a serialized field changes."""
    ...

def resolve_runtime_field_value(value: Any, field_meta_or_type: Any) -> Any:
    """Resolve a runtime field value (e.g. dereference refs)."""
    ...

def normalize_runtime_field_value(value: Any, field_meta_or_type: Any) -> Any:
    """Normalize a runtime field value for serialization."""
    ...

def coerce_serialized_field_input(
    value: Any, field_meta_or_type: Any, path: str = ...
) -> Any:
    """Convert JSON-friendly editor input into a valid runtime field value."""
    ...

def get_annotation_default(annotation: Any) -> Any:
    """Return the default value for a type annotation."""
    ...

def clear_serialized_fields_cache(component_class: Optional[type] = None) -> None:
    """Clear the serialized-fields metadata cache."""
    ...


def int_field(
    default: int = ...,
    *,
    range: Optional[Tuple[float, float]] = ...,
    tooltip: str = ...,
    readonly: bool = ...,
    header: str = ...,
    space: float = ...,
    group: str = ...,
    info_text: str = ...,
    slider: bool = ...,
    drag_speed: Optional[float] = ...,
) -> Any:
    """Shortcut for creating an integer serialized field."""
    ...


def list_field(
    *,
    element_type: FieldType,
    element_class: Optional[Type] = ...,
    component_type: Optional[str] = ...,
    default: Optional[list] = ...,
    tooltip: str = ...,
    readonly: bool = ...,
    header: str = ...,
    space: float = ...,
    group: str = ...,
    info_text: str = ...,
) -> Any:
    """Create a LIST serialized field.

    Args:
        element_type: FieldType of each list element.
        element_class: For SERIALIZABLE_OBJECT elements, the concrete class.
        component_type: For COMPONENT elements, the target type name.
    """
    ...


def component_field(
    component_type: str = ...,
    default: Any = ...,
    **kwargs: Any,
) -> Any:
    """Create a ComponentRef field.

    Args:
        component_type: Optional filter — only accept components of this
            type name in the Inspector drag-drop slot.
    """
    ...


def component_list_field(
    component_type: str = ...,
    default: Optional[list] = ...,
    **kwargs: Any,
) -> Any:
    """Create a list field whose elements are ComponentRef instances.

    Args:
        component_type: Optional type filter shown in the Inspector.
    """
    ...


def get_serialized_fields(component_class: Type[InxComponent]) -> Dict[str, FieldMetadata]:
    """Get all serialized fields from a component class (including inherited)."""
    ...


def get_field_value(component: InxComponent, field_name: str) -> Any:
    """Get the value of a serialized field."""
    ...


def set_field_value(component: InxComponent, field_name: str, value: Any) -> None:
    """Set the value of a serialized field."""
    ...
