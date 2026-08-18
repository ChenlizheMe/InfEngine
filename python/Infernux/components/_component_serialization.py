"""Strict serialization methods shared by Python-defined components."""
from __future__ import annotations

from typing import Any


class ComponentSerializationMixin:
    """ComponentSerializationMixin method group for InxComponent."""

    def _serialize_fields_document(self) -> dict[str, Any]:
        """Encode all serialized fields into the current typed document."""
        from .fields import (
            copy_serialized_field_default,
            get_raw_field_value,
            get_serialized_fields,
        )
        from .value_codec import VALUE_CODECS
        
        # Call on_before_serialize hook
        self._call_on_before_serialize()
        
        fields = get_serialized_fields(self.__class__)
        data = {
            "__type_name__": self.__class__.__name__,
            "__component_id__": self._component_id,
        }
        for name, metadata in fields.items():
            path = f"{self.__class__.__name__}.{name}"
            value = get_raw_field_value(self, name)
            try:
                data[name] = VALUE_CODECS.encode(value, path)
            except ValueError:
                # Non-finite CDS leftovers from a schema hot-reload must not
                # brick every Inspector edit. Fall back to the authored default
                # so sibling fields can still be committed as one document.
                data[name] = VALUE_CODECS.encode(
                    copy_serialized_field_default(metadata), path
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
        repair: bool = False,
    ) -> None:
        """Restore fields from a typed document, transactionally per component.

        ``repair=True`` is for scene and snapshot documents. The live editor
        class is authoritative: unknown keys are dropped, and invalid values
        keep the already-initialized field default instead of failing the load.
        """
        from .fields import (
            get_raw_field_value,
            get_serialized_fields,
        )

        if not isinstance(data, dict):
            raise TypeError("Python component fields document must be an object")

        type_name = data.get("__type_name__")
        if type_name is not None and type_name != self.__class__.__name__:
            if not repair:
                raise ValueError(
                    f"component fields type mismatch: expected {self.__class__.__name__!r}, "
                    f"got {type_name!r}"
                )

        fields = get_serialized_fields(self.__class__)
        metadata_keys = {"__type_name__"}
        if "__component_id__" in data:
            metadata_keys.add("__component_id__")
        from .fields import validate_serialized_field_document
        if repair:
            unknown = sorted(
                set(data) - set(fields) - metadata_keys
            )
            if unknown:
                from Infernux.debug import Debug
                Debug.log_warning(
                    f"{self.__class__.__name__}: ignoring stale scene fields {unknown}; "
                    "live editor schema is authoritative"
                )
        validate_serialized_field_document(
            data,
            fields,
            owner_name=self.__class__.__name__,
            metadata_keys=metadata_keys,
            allow_missing=True,
            allow_unknown=repair,
        )

        saved_id = data.get("__component_id__")
        if saved_id is not None and (type(saved_id) is not int or saved_id <= 0):
            if not repair:
                raise ValueError("__component_id__ must be a positive integer when present")
            saved_id = None

        from .value_codec import VALUE_CODECS
        present_fields = {
            name: meta for name, meta in fields.items() if name in data
        }
        accepted = {}
        for name, meta in present_fields.items():
            path = f"{self.__class__.__name__}.{name}"
            try:
                VALUE_CODECS.validate(data[name], meta, path)
            except (TypeError, ValueError):
                if not repair:
                    raise
                from Infernux.debug import Debug
                Debug.log_warning(
                    f"{path}: scene value is invalid; keeping the editor default"
                )
                continue
            accepted[name] = meta

        decoded = {}
        for name, meta in accepted.items():
            path = f"{self.__class__.__name__}.{name}"
            try:
                decoded[name] = self._deserialize_value(data[name], meta)
            except (TypeError, ValueError):
                if not repair:
                    raise
                from Infernux.debug import Debug
                Debug.log_warning(
                    f"{path}: scene value is invalid; keeping the editor default"
                )

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

    def _deserialize_fields(self, json_str: str, *, _skip_on_after_deserialize: bool = False, repair: bool = False) -> None:
        """Restore fields at an explicit JSON text boundary."""
        import json

        self._deserialize_fields_document(
            json.loads(json_str),
            _skip_on_after_deserialize=_skip_on_after_deserialize,
            repair=repair,
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

