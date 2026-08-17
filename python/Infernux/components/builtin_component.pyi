"""Type stubs for Infernux.components.builtin_component."""

from __future__ import annotations

import weakref
from typing import Any, Dict, Optional, Tuple, Type

from .fields import FieldMetadata, FieldType
from .component import InxComponent


class CppProperty:
    """Descriptor that delegates get/set to a C++ component property.

    Used by BuiltinComponent subclasses to expose C++ properties as
    serialized fields within the InxComponent system.
    """

    _is_cpp_property: bool
    cpp_attr: str
    metadata: FieldMetadata
    get_converter: Any
    set_converter: Any
    native_getter: Any
    native_setter: Any

    def __init__(
        self,
        cpp_attr: str,
        field_type: FieldType = ...,
        default: Any = ...,
        *,
        readonly: bool = ...,
        tooltip: str = ...,
        header: str = ...,
        range: Optional[tuple] = ...,
        enum_type: Any = ...,
        enum_labels: Optional[list] = ...,
        visible_when: Any = ...,
        asset_type: Optional[str] = ...,
        get_converter: Any = ...,
        set_converter: Any = ...,
        native_getter: Any = ...,
        native_setter: Any = ...,
        hdr: bool = ...,
        slider: bool = ...,
    ) -> None: ...
    def __set_name__(self, owner: type, name: str) -> None: ...
    def __get__(self, instance: Optional[Any], owner: type) -> Any: ...
    def __set__(self, instance: Any, value: Any) -> None: ...


class BuiltinComponent(InxComponent):
    """Base class for Python wrappers around C++ built-in components.

    Subclasses MUST set ``_cpp_type_name`` to the C++ component's registered
    type name (e.g. ``"Light"``, ``"MeshRenderer"``, ``"Camera"``).
    """

    _cpp_type_name: str
    _cpp_component: Optional[Any]
    _builtin_registry: Dict[str, Type[BuiltinComponent]]
    _wrapper_cache: weakref.WeakValueDictionary

    def _require_cpp_component(self) -> Any: ...

    def _bind_cpp(self, cpp_component: Any, game_object: Any) -> None:
        """Bind this Python wrapper to an existing C++ component."""
        ...

    @classmethod
    def _get_or_create_wrapper(
        cls, cpp_component: Any, game_object: Any,
    ) -> BuiltinComponent:
        """Return an existing wrapper or create a new one."""
        ...

    @classmethod
    def _clear_cache(cls) -> None:
        """Clear the wrapper cache (call on scene change / play-mode stop)."""
        ...

    @property
    def enabled(self) -> bool:
        """Whether the component is enabled."""
        ...
    @enabled.setter
    def enabled(self, value: bool) -> None: ...
    @property
    def is_valid(self) -> bool:
        """Whether the underlying C++ component is still alive."""
        ...
    @property
    def component_id(self) -> int:
        """The unique ID of this component instance."""
        ...

    def serialize(self) -> str:
        """Serialize via the C++ component's own serializer."""
        ...
    def deserialize(self, json_str: str) -> bool:
        """Deserialize via the C++ component."""
        ...
    def serialize_document(self) -> dict: ...
    def deserialize_document(self, document: dict) -> bool: ...
    def _serialize_fields(self) -> str: ...
    def _deserialize_fields(self, json_str: str) -> None: ...
    def __repr__(self) -> str: ...
