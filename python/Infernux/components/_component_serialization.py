"""Strict serialization methods shared by Python-defined components."""
from __future__ import annotations

from typing import Any


class ComponentSerializationMixin:
    """ComponentSerializationMixin method group for InxComponent."""

    def _serialize_fields_document(self) -> dict[str, Any]:
        """Encode all serialized fields into the current typed document."""
        from .serialized_field import get_raw_field_value, get_serialized_fields
        from .value_codec import VALUE_CODECS
        
        # Call on_before_serialize hook
        self._call_on_before_serialize()
        
        fields = get_serialized_fields(self.__class__)
        data = {
            "__type_name__": self.__class__.__name__,
            "__component_id__": self._component_id,
        }
        for name in fields:
            value = get_raw_field_value(self, name)
            data[name] = VALUE_CODECS.encode(
                value, f"{self.__class__.__name__}.{name}"
            )
        
        return data

    def _serialize_fields(self) -> str:
        """Serialize fields at an explicit JSON text boundary."""
        import json

        return json.dumps(self._serialize_fields_document())

    def _deserialize_fields_document(
        self,
        data: dict[str, Any],
        *,
        _skip_on_after_deserialize: bool = False,
    ) -> None:
        """Restore fields from a typed document, transactionally per component."""
        from .serialized_field import (
            get_raw_field_value,
            get_serialized_fields,
        )

        if not isinstance(data, dict):
            raise TypeError("Python component fields document must be an object")

        type_name = data.get("__type_name__")
        if type_name != self.__class__.__name__:
            raise ValueError(
                f"component fields type mismatch: expected {self.__class__.__name__!r}, "
                f"got {type_name!r}"
            )

        fields = get_serialized_fields(self.__class__)
        metadata_keys = {"__type_name__"}
        if "__component_id__" in data:
            metadata_keys.add("__component_id__")
        from .serialized_field import validate_serialized_field_document
        validate_serialized_field_document(
            data,
            fields,
            owner_name=self.__class__.__name__,
            metadata_keys=metadata_keys,
        )

        saved_id = data.get("__component_id__")
        if saved_id is not None and (type(saved_id) is not int or saved_id <= 0):
            raise ValueError("__component_id__ must be a positive integer when present")

        from .value_codec import VALUE_CODECS
        for name, meta in fields.items():
            VALUE_CODECS.validate(data[name], meta, f"{self.__class__.__name__}.{name}")

        decoded = {
            name: self._deserialize_value(data[name], meta)
            for name, meta in fields.items()
        }
        previous_values = {
            name: get_raw_field_value(self, name)
            for name in fields
        }
        previous_component_id = self._component_id

        self._inf_deserializing = True
        try:
            for name, value in decoded.items():
                setattr(self, name, value)
            if saved_id is not None:
                self._component_id = saved_id
                from Infernux.components.component import InxComponent as _InxComp
                with _InxComp._id_lock:
                    if _InxComp._next_component_id <= saved_id:
                        _InxComp._next_component_id = saved_id + 1
        except Exception:
            self._component_id = previous_component_id
            for name, value in previous_values.items():
                setattr(self, name, value)
            raise
        finally:
            self._inf_deserializing = False

        # Call on_after_deserialize hook
        if not _skip_on_after_deserialize:
            self._call_on_after_deserialize()

    def _deserialize_fields(self, json_str: str, *, _skip_on_after_deserialize: bool = False) -> None:
        """Restore fields at an explicit JSON text boundary."""
        import json

        self._deserialize_fields_document(
            json.loads(json_str),
            _skip_on_after_deserialize=_skip_on_after_deserialize,
        )

    def _serialize_value(self, value: Any):
        """Encode a standalone value using the current strict codec schema."""
        from .value_codec import VALUE_CODECS

        return VALUE_CODECS.encode(value)

    def _deserialize_value(self, value: Any, field_meta_or_type):
        """Decode a field value using the current strict codec schema."""
        from .value_codec import VALUE_CODECS

        name = getattr(field_meta_or_type, "name", None)
        path = f"{self.__class__.__name__}.{name}" if name else "value"
        return VALUE_CODECS.decode(value, field_meta_or_type, path)

