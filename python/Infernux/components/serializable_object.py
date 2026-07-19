"""
SerializableObject — Base class for custom serializable data objects.

Similar to Unity's ``[Serializable]`` attribute on plain C# classes.
Supports the same ``serialized_field()`` descriptors as InxComponent, but
without lifecycle methods or undo/dirty tracking.  Can be nested inside
InxComponent fields or other SerializableObjects.

Example::

    from Infernux.components import SerializableObject, serialized_field

    class Stats(SerializableObject):
        hp: int = serialized_field(default=100)
        mp: float = serialized_field(default=50.0)
        name: str = serialized_field(default="default")

    class Enemy(InxComponent):
        stats: Stats = serialized_field(default=Stats())
        allies: list = list_field(element_type=FieldType.SERIALIZABLE_OBJECT, element_class=Stats)
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional, Type, TYPE_CHECKING

if TYPE_CHECKING:
    from .serialized_field import FieldMetadata

# Global registry: module:qualname → class. Populated by __init_subclass__.
_SERIALIZABLE_REGISTRY: Dict[str, Type["SerializableObject"]] = {}


def get_serializable_type_id(value: type | "SerializableObject") -> str:
    """Return the stable current-format identity for a serializable type."""
    value_type = value if isinstance(value, type) else type(value)
    explicit_id = value_type.__dict__.get("__serialized_type_id__", "")
    if explicit_id:
        if not isinstance(explicit_id, str):
            raise TypeError("__serialized_type_id__ must be a string")
        return explicit_id
    return f"{value_type.__module__}:{value_type.__qualname__}"


def get_serializable_class(type_id: str) -> Optional[Type["SerializableObject"]]:
    """Look up a registered SerializableObject subclass by module:qualname."""
    return _SERIALIZABLE_REGISTRY.get(type_id)


class SerializableObject:
    """Lightweight data container with serialized-field metadata.

    Subclass this to create custom serializable data types that can be used
    as InxComponent field values (scalars or list elements).

    * Field declarations follow the same syntax as InxComponent
      (``serialized_field()``, plain values, type annotations).
    * Instances use the current typed value-document identity for
      polymorphic deserialization.
    * **No undo/dirty tracking** — that is handled at the InxComponent level.
    """

    _serialized_fields_: Dict[str, "FieldMetadata"] = {}
    __serialized_type_id__ = ""

    # ------------------------------------------------------------------
    # Metaclass-style auto-registration
    # ------------------------------------------------------------------

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        cls._serialized_fields_ = {}
        current_type_id = get_serializable_type_id(cls)
        if not isinstance(current_type_id, str) or not current_type_id:
            raise ValueError("serialized type ID must be a non-empty string")
        # Last definition wins so editor script hot-reload can replace a class.
        _SERIALIZABLE_REGISTRY[current_type_id] = cls

        own_annotations = cls.__dict__.get('__annotations__', {})

        from .serialized_field import (
            FieldMetadata,
            SerializedFieldDescriptor,
            infer_field_type_from_value,
            resolve_annotation,
            HiddenField,
        )

        for attr_name in list(cls.__dict__):
            if attr_name.startswith("_"):
                continue

            attr = cls.__dict__[attr_name]

            if callable(attr) or isinstance(attr, (property, classmethod, staticmethod)):
                continue

            if isinstance(attr, HiddenField):
                continue

            if isinstance(attr, SerializedFieldDescriptor):
                meta = attr.metadata
                meta.name = attr_name
                cls._serialized_fields_[attr_name] = meta
                # Replace the heavy descriptor with None; __init__ will set
                # plain instance attributes from defaults.
                setattr(cls, attr_name, None)

            elif isinstance(attr, FieldMetadata):
                attr.name = attr_name
                cls._serialized_fields_[attr_name] = attr
                setattr(cls, attr_name, None)

            elif attr is None:
                ann = own_annotations.get(attr_name)
                if ann is not None:
                    meta = resolve_annotation(ann)
                    if meta is not None:
                        meta.name = attr_name
                        cls._serialized_fields_[attr_name] = meta
                        setattr(cls, attr_name, None)

            else:
                from enum import Enum as _Enum

                field_type = infer_field_type_from_value(attr)
                enum_type = type(attr) if isinstance(attr, _Enum) else None
                meta = FieldMetadata(
                    name=attr_name,
                    field_type=field_type,
                    default=attr,
                    enum_type=enum_type,
                )
                cls._serialized_fields_[attr_name] = meta
                # Leave the class attribute as-is (plain default value)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __getattribute__(self, name: str):
        if not name.startswith("_"):
            cls = object.__getattribute__(self, "__class__")
            fields = getattr(cls, "_serialized_fields_", {})
            meta = fields.get(name)
            if meta is not None:
                from .serialized_field import resolve_runtime_field_value
                data = object.__getattribute__(self, "__dict__")
                raw = data.get(name, meta.default)
                return resolve_runtime_field_value(raw, meta)
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value):
        cls = type(self)
        fields = getattr(cls, "_serialized_fields_", {})
        meta = fields.get(name)
        if meta is not None:
            from .serialized_field import normalize_runtime_field_value
            value = normalize_runtime_field_value(value, meta)
        object.__setattr__(self, name, value)

    def __init__(self, **kwargs):
        from .serialized_field import get_serialized_fields

        fields = get_serialized_fields(self.__class__)
        for name, meta in fields.items():
            if name in kwargs:
                setattr(self, name, kwargs[name])
            else:
                try:
                    setattr(self, name, copy.deepcopy(meta.default))
                except Exception:
                    setattr(self, name, meta.default)

    # ------------------------------------------------------------------
    # Serialization helpers (used by InxComponent._serialize_value)
    # ------------------------------------------------------------------

    def _serialize(self) -> dict:
        """Serialize this object to a JSON-friendly dict."""
        from .serialized_field import get_serialized_fields

        fields_document: Dict[str, Any] = {}
        fields = get_serialized_fields(self.__class__)
        from .serialized_field import get_raw_field_value
        for name, meta in fields.items():
            value = get_raw_field_value(self, name)
            fields_document[name] = _serialize_so_value(
                value, f"{get_serializable_type_id(self)}.{name}"
            )
        from .value_document import make_serializable_object
        return make_serializable_object(get_serializable_type_id(self), fields_document)

    @classmethod
    def _validate_document(cls, data: dict, path: str = "SerializableObject"):
        """Validate a complete object graph without constructing an instance."""
        from .serialized_field import get_serialized_fields

        if not isinstance(data, dict):
            raise TypeError(f"{path}: SerializableObject document must be an object")
        from .value_document import TYPE_KEY, SERIALIZABLE_OBJECT
        expected_keys = {TYPE_KEY, "type_id", "fields"}
        if set(data) != expected_keys or data.get(TYPE_KEY) != SERIALIZABLE_OBJECT:
            raise ValueError(f"{path}: invalid SerializableObject typed document")
        type_id = data.get("type_id")
        if not isinstance(type_id, str) or not type_id:
            raise ValueError(f"{path}: SerializableObject document requires type_id")
        actual_cls = _SERIALIZABLE_REGISTRY.get(type_id)
        if actual_cls is None:
            raise ValueError(f"{path}: unknown SerializableObject type_id {type_id!r}")

        fields = get_serialized_fields(actual_cls)
        fields_document = data.get("fields")
        if not isinstance(fields_document, dict):
            raise TypeError(f"{path}: SerializableObject fields must be an object")

        from .serialized_field import validate_serialized_field_document
        validate_serialized_field_document(fields_document, fields, owner_name=type_id)

        from .value_codec import VALUE_CODECS
        for name, meta in fields.items():
            VALUE_CODECS.validate(fields_document[name], meta, f"{path}.{name}")
        return actual_cls, fields

    @classmethod
    def _deserialize(cls, data: dict) -> "SerializableObject":
        """Validate, decode, and construct one current-schema object."""
        actual_cls, fields = cls._validate_document(data)

        fields_document = data["fields"]
        decoded = {
            name: _deserialize_so_value(
                fields_document[name],
                meta,
                f"{get_serializable_type_id(actual_cls)}.{name}"
            )
            for name, meta in fields.items()
        }
        instance = actual_cls.__new__(actual_cls)
        for name, value in decoded.items():
            setattr(instance, name, value)
        return instance

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __eq__(self, other):
        if type(self) is not type(other):
            return NotImplemented
        from .serialized_field import get_serialized_fields

        fields = get_serialized_fields(self.__class__)
        return all(
            getattr(self, n, None) == getattr(other, n, None)
            for n in fields
        )

    def __repr__(self):
        from .serialized_field import get_serialized_fields

        fields = get_serialized_fields(self.__class__)
        parts = [f"{n}={getattr(self, n, 'N/A')!r}" for n in fields]
        return f"{self.__class__.__name__}({', '.join(parts)})"

    def __deepcopy__(self, memo):
        from .serialized_field import get_serialized_fields

        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        fields = get_serialized_fields(cls)
        for name in fields:
            value = getattr(self, name, None)
            setattr(result, name, copy.deepcopy(value, memo))
        return result


def _serialize_so_value(value: Any, path: str = "SerializableObject.value") -> Any:
    """Encode a SerializableObject field with the shared strict registry."""
    from .value_codec import VALUE_CODECS

    return VALUE_CODECS.encode(value, path)


def _deserialize_so_value(
    value: Any, field_meta: Any, path: str = "SerializableObject.value"
) -> Any:
    """Decode a SerializableObject field with the shared strict registry."""
    from .value_codec import VALUE_CODECS

    return VALUE_CODECS.decode(value, field_meta, path)
