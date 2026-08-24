"""Shared mixin for auto-collecting serialized fields via __init_subclass__."""

from __future__ import annotations

import sys
from typing import Any, Dict, FrozenSet


class SerializedFieldCollectorMixin:
    """Mixin that auto-collects class-level serialized fields.

    Subclasses that use this mixin get automatic ``_serialized_fields_``
    population via ``__init_subclass__``.  Override ``_reserved_attrs_``
    in each class hierarchy to exclude certain attribute names.
    """

    _reserved_attrs_: FrozenSet[str] = frozenset()
    _serialized_fields_: Dict[str, Any] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        cls._serialized_fields_ = {}

        reserved = set()
        for klass in cls.__mro__:
            ra = getattr(klass, "_reserved_attrs_", None)
            if ra:
                reserved.update(ra)

        own_annotations = dict(cls.__dict__.get("__annotations__", {}))
        resolved_annotations: dict[str, Any] = {}
        module = sys.modules.get(cls.__module__)
        globalns = getattr(module, "__dict__", {})
        for name, annotation in own_annotations.items():
            if isinstance(annotation, str):
                try:
                    annotation = eval(annotation, globalns, dict(vars(cls)))  # noqa: S307
                except Exception:
                    pass
            resolved_annotations[name] = annotation

        for attr_name in list(cls.__dict__):
            if attr_name.startswith("_"):
                continue
            if attr_name in reserved:
                continue

            attr = cls.__dict__[attr_name]

            if callable(attr) and not isinstance(attr, (int, float, bool, str)):
                continue
            if isinstance(attr, (property, classmethod, staticmethod)):
                continue
            if attr is None:
                continue

            from Infernux.components.fields import (
                FieldMetadata,
                HiddenField,
                SerializedFieldDescriptor,
                infer_field_type_from_value,
            )

            if isinstance(attr, HiddenField):
                continue

            if isinstance(attr, SerializedFieldDescriptor):
                cls._serialized_fields_[attr_name] = attr.metadata
            elif isinstance(attr, FieldMetadata):
                cls._serialized_fields_[attr_name] = attr
            else:
                from enum import Enum as _Enum
                from Infernux.components.fields import (
                    NON_SERIALIZED_FIELD,
                    build_field_from_annotation,
                )

                annotation = resolved_annotations.get(attr_name)
                metadata = None
                if annotation is not None:
                    metadata = build_field_from_annotation(annotation, default=attr)
                    if metadata is NON_SERIALIZED_FIELD:
                        continue
                if metadata is None:
                    field_type = infer_field_type_from_value(attr)
                    enum_type = type(attr) if isinstance(attr, _Enum) else None
                    metadata = FieldMetadata(
                        name=attr_name,
                        field_type=field_type,
                        default=attr,
                        enum_type=enum_type,
                    )
                metadata.name = attr_name
                descriptor = SerializedFieldDescriptor(metadata)
                descriptor.__set_name__(cls, attr_name)
                setattr(cls, attr_name, descriptor)
                cls._serialized_fields_[attr_name] = metadata
