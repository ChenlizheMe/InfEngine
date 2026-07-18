"""Type stubs for Infernux.components.serializable_object."""

from __future__ import annotations

from typing import Any, Dict, Optional, Type

from .serialized_field import FieldMetadata


def get_serializable_type_id(value: type | SerializableObject) -> str: ...
def get_serializable_class(type_id: str) -> Optional[Type[SerializableObject]]:
    """Look up a registered SerializableObject subclass by module:qualname."""
    ...


class SerializableObject:
    """Lightweight data container with serialized-field metadata.

    Subclass this to create custom serializable data types that can be
    used as InxComponent field values (scalars or list elements).
    """

    _serialized_fields_: Dict[str, FieldMetadata]
    __serialized_type_id__: str
    __formerly_serialized_type_as__: tuple[str, ...]

    def __init__(self, **kwargs: Any) -> None: ...
    def _serialize(self) -> dict: ...
    @classmethod
    def _deserialize(cls, data: dict) -> SerializableObject: ...
    def __eq__(self, other: object) -> bool: ...
    def __repr__(self) -> str: ...
    def __deepcopy__(self, memo: Any) -> SerializableObject: ...
