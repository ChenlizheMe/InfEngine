"""Strict value codecs shared by Python serialization surfaces."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Callable


@dataclass(frozen=True)
class ValueCodecDescriptor:
    """One active value codec contract.

    A codec must own both directions and a side-effect-free validation step.
    ``can_decode`` receives either ``FieldMetadata`` or a field-type token.
    """

    name: str
    can_encode: Callable[[Any], bool]
    can_decode: Callable[[Any], bool]
    encode: Callable[[Any, str, "ValueCodecRegistry"], Any]
    validate: Callable[[Any, Any, str, "ValueCodecRegistry"], None]
    decode: Callable[[Any, Any, str, "ValueCodecRegistry"], Any]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("value codec name must be a non-empty string")
        for field_name in ("can_encode", "can_decode", "encode", "validate", "decode"):
            if not callable(getattr(self, field_name)):
                raise TypeError(f"value codec {self.name!r} {field_name} must be callable")

    @property
    def identity(self) -> str:
        return self.name


class ValueCodecRegistry:
    """Current-schema codec registry with validate-before-decode semantics."""

    BUILTIN_CODEC_NAME = "infernux.current-schema"

    def __init__(self) -> None:
        self._codecs: list[ValueCodecDescriptor] = []
        self._builtin_codec = ValueCodecDescriptor(
            name=self.BUILTIN_CODEC_NAME,
            can_encode=lambda _value: True,
            can_decode=lambda _field: True,
            encode=lambda value, path, registry: registry._encode_builtin(value, path),
            validate=lambda value, field, path, registry: registry._validate_builtin(value, field, path),
            decode=lambda value, field, path, registry: registry._decode_builtin(value, field, path),
        )
        self._codecs.append(self._builtin_codec)

    @property
    def descriptors(self) -> tuple[ValueCodecDescriptor, ...]:
        """Return the active codecs in dispatch order."""
        return tuple(self._codecs)

    def register_codec(self, descriptor: ValueCodecDescriptor) -> None:
        if not isinstance(descriptor, ValueCodecDescriptor):
            raise TypeError("register_codec requires a ValueCodecDescriptor")
        if any(codec.name == descriptor.name for codec in self._codecs):
            raise ValueError(f"value codec {descriptor.name!r} is already registered")
        self._codecs.insert(len(self._codecs) - 1, descriptor)

    def encode(self, value: Any, path: str = "value") -> Any:
        for codec in self._codecs:
            if codec.can_encode(value):
                encoded = codec.encode(value, path, self)
                self._validate_encoded_document(
                    encoded,
                    path,
                    allow_custom_type=codec is not self._builtin_codec,
                )
                return encoded
        raise AssertionError("the built-in codec must be the final encode fallback")

    def validate(self, value: Any, field_meta_or_type: Any, path: str = "value") -> None:
        codec = self._select_decoder(field_meta_or_type)
        codec.validate(value, field_meta_or_type, path, self)

    def decode(self, value: Any, field_meta_or_type: Any, path: str = "value") -> Any:
        codec = self._select_decoder(field_meta_or_type)
        codec.validate(value, field_meta_or_type, path, self)
        return codec.decode(value, field_meta_or_type, path, self)

    def _select_decoder(self, field_meta_or_type: Any) -> ValueCodecDescriptor:
        for codec in self._codecs:
            if codec.can_decode(field_meta_or_type):
                return codec
        raise AssertionError("the built-in codec must be the final decode fallback")

    def _encode_builtin(self, value: Any, path: str) -> Any:
        if isinstance(value, Enum):
            from .value_document import make_enum
            return make_enum(type(value).__qualname__, value.name)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{path}: floating-point values must be finite")
            return value
        if isinstance(value, (bool, int, str, type(None))):
            return value
        if isinstance(value, (list, tuple)):
            return [self.encode(item, f"{path}[{index}]") for index, item in enumerate(value)]
        if isinstance(value, dict):
            if not all(isinstance(key, str) for key in value):
                raise TypeError(f"{path}: serialized dictionary keys must be strings")
            return {key: self.encode(item, f"{path}.{key}") for key, item in value.items()}

        from .serializable_object import SerializableObject
        if isinstance(value, SerializableObject):
            return value._serialize()

        from .ref_wrappers import ComponentRef, GameObjectRef, MaterialRef, PrefabRef
        if isinstance(value, ComponentRef):
            return value._serialize()
        if isinstance(value, GameObjectRef):
            from .value_document import make_game_object_ref
            return make_game_object_ref(value.persistent_id)
        if isinstance(value, PrefabRef):
            return value._serialize()
        if isinstance(value, MaterialRef):
            from .value_document import make_asset_ref
            return make_asset_ref("Material", value.guid, value._path_hint)

        from ._serialize_helpers import _serialize_asset_ref, serialize_vec
        asset_document = _serialize_asset_ref(value)
        if asset_document is not None:
            return asset_document

        if hasattr(value, "id") and hasattr(value, "name") and hasattr(value, "transform"):
            from .value_document import make_game_object_ref
            return make_game_object_ref(int(value.id))

        try:
            from Infernux.core.material import Material
        except ImportError:
            Material = None
        if Material is not None and isinstance(value, Material):
            guid = MaterialRef._extract_guid(value)
            if not guid:
                raise ValueError(f"{path}: Material must have an asset GUID")
            from .value_document import make_asset_ref
            return make_asset_ref("Material", guid)

        vector = serialize_vec(value)
        if vector is not None:
            if not all(math.isfinite(item) for item in vector):
                raise ValueError(f"{path}: vector values must be finite")
            return vector

        value_type = type(value)
        raise TypeError(
            f"{path}: unsupported serialized value type "
            f"'{value_type.__module__}.{value_type.__qualname__}'"
        )

    def _validate_builtin(self, value: Any, field_meta_or_type: Any, path: str) -> None:
        from .serialized_field import FieldType

        field_type, element_type = self._field_parts(field_meta_or_type)
        ref_types = self._reference_field_types()

        if value is None:
            if field_type not in ref_types and field_type not in {
                FieldType.UNKNOWN,
                FieldType.SERIALIZABLE_OBJECT,
            }:
                raise TypeError(f"{path}: {field_type.name} field cannot be null")
            return

        if field_type == FieldType.BOOL:
            if type(value) is not bool:
                raise TypeError(f"{path}: BOOL field requires a boolean")
            return
        if field_type == FieldType.INT:
            if type(value) is not int:
                raise TypeError(f"{path}: INT field requires an integer")
            return
        if field_type == FieldType.FLOAT:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{path}: FLOAT field requires a number")
            if not math.isfinite(float(value)):
                raise ValueError(f"{path}: FLOAT field must be finite")
            return
        if field_type == FieldType.STRING:
            if not isinstance(value, str):
                raise TypeError(f"{path}: STRING field requires a string")
            return

        if field_type == FieldType.SERIALIZABLE_OBJECT:
            if not isinstance(value, dict):
                raise TypeError(f"{path}: SERIALIZABLE_OBJECT field requires an object")
            from .serializable_object import SerializableObject
            SerializableObject._validate_document(value, path)
            return

        if field_type == FieldType.LIST:
            if not isinstance(value, list):
                raise TypeError(f"{path}: LIST field requires an array")
            for index, item in enumerate(value):
                self.validate(item, element_type or FieldType.UNKNOWN, f"{path}[{index}]")
            return

        vector_sizes = {
            FieldType.VEC2: 2,
            FieldType.VEC3: 3,
            FieldType.VEC4: 4,
        }
        if field_type in vector_sizes:
            self._validate_vector(value, vector_sizes[field_type], path)
            return
        if field_type == FieldType.COLOR:
            if not isinstance(value, (list, tuple)) or len(value) != 4:
                raise TypeError(f"{path}: COLOR field requires exactly four numbers")
            numbers = self._require_numbers(value, path, "COLOR")
            if not all(math.isfinite(item) for item in numbers):
                raise ValueError(f"{path}: COLOR values must be finite")
            return
        if field_type == FieldType.ENUM:
            self._validate_enum(value, field_meta_or_type, path)
            return

        if field_type in ref_types:
            if not isinstance(value, dict):
                raise TypeError(f"{path}: {field_type.name} field requires a reference document")
            document_type = self._validate_typed_document(value, path)
            self._require_reference_document_type(document_type, value, field_type, field_meta_or_type, path)
            return

        if field_type != FieldType.UNKNOWN:
            raise TypeError(f"{path}: unsupported value for {field_type.name}")

        if isinstance(value, list):
            for index, item in enumerate(value):
                self.validate(item, FieldType.UNKNOWN, f"{path}[{index}]")
            return
        if isinstance(value, dict):
            if not all(isinstance(key, str) for key in value):
                raise TypeError(f"{path}: serialized dictionary keys must be strings")
            self._reject_legacy_marker_document(value, path)
            document_type = self._validate_typed_document(value, path)
            if document_type is not None:
                return
            for key, item in value.items():
                self.validate(item, FieldType.UNKNOWN, f"{path}.{key}")
            return
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path}: floating-point values must be finite")
        if isinstance(value, (bool, int, float, str)):
            return
        raise TypeError(f"{path}: unsupported value for {field_type.name}")

    def _decode_builtin(self, value: Any, field_meta_or_type: Any, path: str) -> Any:
        from .serialized_field import FieldType, normalize_rgba
        from ._serialize_helpers import deserialize_dict_ref, make_null_ref

        field_type, element_type = self._field_parts(field_meta_or_type)
        if value is None:
            return make_null_ref(field_type, field_meta_or_type)
        if field_type in {FieldType.BOOL, FieldType.INT, FieldType.STRING}:
            return value
        if field_type == FieldType.FLOAT:
            return float(value)
        if field_type == FieldType.SERIALIZABLE_OBJECT:
            from .serializable_object import SerializableObject
            return SerializableObject._deserialize(value)
        if field_type == FieldType.LIST:
            return [
                self.decode(item, element_type or FieldType.UNKNOWN, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]

        from Infernux.math import Vector2, Vector3, vec4f
        vector_types = {
            FieldType.VEC2: Vector2,
            FieldType.VEC3: Vector3,
            FieldType.VEC4: vec4f,
        }
        if field_type in vector_types:
            return vector_types[field_type](*self._require_numbers(value, path, "vector"))
        if field_type == FieldType.COLOR:
            return normalize_rgba(self._require_numbers(value, path, "COLOR"))
        if field_type == FieldType.ENUM:
            return field_meta_or_type.enum_type.__members__[value["name"]]

        if isinstance(value, list):
            return [self.decode(item, FieldType.UNKNOWN, f"{path}[{index}]") for index, item in enumerate(value)]
        if isinstance(value, dict):
            decoded = deserialize_dict_ref(value)
            if decoded is value:
                return {
                    key: self.decode(item, FieldType.UNKNOWN, f"{path}.{key}")
                    for key, item in value.items()
                }
            return decoded
        return value

    @staticmethod
    def _field_parts(field_meta_or_type: Any) -> tuple[Any, Any]:
        if hasattr(field_meta_or_type, "field_type"):
            return field_meta_or_type.field_type, getattr(field_meta_or_type, "element_type", None)
        return field_meta_or_type, None

    @staticmethod
    def _reference_field_types() -> set[Any]:
        from .serialized_field import FieldType
        return {
            FieldType.GAME_OBJECT,
            FieldType.COMPONENT,
            FieldType.MATERIAL,
            FieldType.TEXTURE,
            FieldType.SHADER,
            FieldType.ASSET,
        }

    @staticmethod
    def _validate_vector(value: Any, size: int, path: str) -> None:
        if not isinstance(value, (list, tuple)) or len(value) != size:
            raise TypeError(f"{path}: vector field requires exactly {size} numbers")
        numbers = ValueCodecRegistry._require_numbers(value, path, "vector")
        if not all(math.isfinite(item) for item in numbers):
            raise ValueError(f"{path}: vector field values must be finite")

    @staticmethod
    def _require_numbers(value: Any, path: str, kind: str) -> list[float]:
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
            raise TypeError(f"{path}: {kind} values must be numbers")
        return [float(item) for item in value]

    @staticmethod
    def _validate_enum(value: Any, field_meta: Any, path: str) -> None:
        from .value_document import ENUM

        if not isinstance(value, dict) or ValueCodecRegistry._validate_typed_document(value, path) != ENUM:
            raise TypeError(f"{path}: ENUM field requires an exact enum document")
        enum_type = getattr(field_meta, "enum_type", None)
        if enum_type is None or enum_type.__qualname__ != value["enum_type"]:
            raise ValueError(f"{path}: unknown enum type {value['enum_type']!r}")
        if value["name"] not in enum_type.__members__:
            raise ValueError(f"{path}: unknown {enum_type.__qualname__} member {value['name']!r}")

    @staticmethod
    def _validate_encoded_document(value: Any, path: str, *, allow_custom_type: bool = False) -> None:
        if value is None or type(value) in {bool, int, str}:
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{path}: encoded floating-point values must be finite")
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                ValueCodecRegistry._validate_encoded_document(
                    item,
                    f"{path}[{index}]",
                    allow_custom_type=allow_custom_type,
                )
            return
        if isinstance(value, dict):
            if not all(isinstance(key, str) for key in value):
                raise TypeError(f"{path}: encoded dictionary keys must be strings")
            if not allow_custom_type:
                ValueCodecRegistry._reject_legacy_marker_document(value, path)
            from .value_document import TYPE_KEY
            if TYPE_KEY in value and not allow_custom_type:
                ValueCodecRegistry._validate_typed_document(value, path)
            for key, item in value.items():
                ValueCodecRegistry._validate_encoded_document(
                    item,
                    f"{path}.{key}",
                    allow_custom_type=allow_custom_type,
                )
            return
        raise TypeError(f"{path}: codec produced non-document value {type(value).__qualname__}")

    @staticmethod
    def _reject_legacy_marker_document(value: dict, path: str) -> None:
        legacy_keys = {
            key
            for key in value
            if key in {"__game_object__", "__component_ref__", "__serializable_type__", "__enum__", "__path_hint__"}
            or (key.startswith("__") and key.endswith("_ref__"))
        }
        if legacy_keys:
            raise ValueError(f"{path}: legacy marker documents are not supported: {sorted(legacy_keys)}")

    @staticmethod
    def _validate_typed_document(value: dict, path: str) -> str | None:
        from .value_document import (
            TYPE_KEY,
            ENUM,
            GAME_OBJECT_REF,
            COMPONENT_REF,
            ASSET_REF,
            SERIALIZABLE_OBJECT,
        )

        if TYPE_KEY not in value:
            return None
        document_type = value.get(TYPE_KEY)
        if not isinstance(document_type, str):
            raise TypeError(f"{path}: typed value document requires a string {TYPE_KEY}")
        if document_type == ENUM:
            if set(value) != {TYPE_KEY, "enum_type", "name"}:
                raise ValueError(f"{path}: enum document has unknown or missing fields")
            if not isinstance(value["enum_type"], str) or not isinstance(value["name"], str):
                raise TypeError(f"{path}: enum type and member names must be strings")
            return document_type

        if document_type == GAME_OBJECT_REF:
            if set(value) != {TYPE_KEY, "object_id"}:
                raise ValueError(f"{path}: GameObjectRef document has unknown or missing fields")
            target_id = value["object_id"]
            if type(target_id) is not int or target_id < 0:
                raise TypeError(f"{path}: GameObjectRef id must be a non-negative integer")
            return document_type

        if document_type == COMPONENT_REF:
            if set(value) != {TYPE_KEY, "game_object_id", "component_type"}:
                raise ValueError(f"{path}: ComponentRef document has unknown or missing fields")
            if type(value["game_object_id"]) is not int or value["game_object_id"] < 0:
                raise TypeError(f"{path}: ComponentRef go_id must be a non-negative integer")
            if not isinstance(value["component_type"], str):
                raise TypeError(f"{path}: ComponentRef type_name must be a string")
            return document_type

        if document_type == ASSET_REF:
            if set(value) != {TYPE_KEY, "asset_type", "guid", "path_hint"}:
                raise ValueError(f"{path}: asset reference document has unknown or missing fields")
            if not all(isinstance(value[key], str) for key in ("asset_type", "guid", "path_hint")):
                raise TypeError(f"{path}: asset reference values must be strings")
            from Infernux.core.asset_ref import get_asset_type_config
            if value["asset_type"] not in {"Prefab", "Material", "Texture", "Shader"} and get_asset_type_config(
                value["asset_type"]
            ) is None:
                raise ValueError(f"{path}: unknown asset reference type {value['asset_type']!r}")
            return document_type

        if document_type == SERIALIZABLE_OBJECT:
            from .serializable_object import SerializableObject
            SerializableObject._validate_document(value, path)
            return document_type

        raise ValueError(f"{path}: unknown typed value document {document_type!r}")

    @staticmethod
    def _require_reference_document_type(
        document_type: str | None,
        value: dict,
        field_type: Any,
        field_meta: Any,
        path: str,
    ) -> None:
        from .serialized_field import FieldType
        from .value_document import GAME_OBJECT_REF, COMPONENT_REF, ASSET_REF

        if field_type == FieldType.COMPONENT:
            if document_type != COMPONENT_REF:
                raise TypeError(f"{path}: COMPONENT field contains the wrong reference type")
            return
        if field_type == FieldType.GAME_OBJECT:
            if document_type == GAME_OBJECT_REF:
                return
            if document_type == ASSET_REF and value["asset_type"] == "Prefab":
                return
            raise TypeError(f"{path}: GAME_OBJECT field contains the wrong reference type")
        if document_type != ASSET_REF:
            raise TypeError(f"{path}: {field_type.name} field contains the wrong reference type")

        expected_asset_type = {
            FieldType.MATERIAL: "Material",
            FieldType.TEXTURE: "Texture",
            FieldType.SHADER: "Shader",
        }.get(field_type)
        if field_type == FieldType.ASSET:
            expected_asset_type = getattr(field_meta, "asset_type", None)
            if expected_asset_type is None:
                from Infernux.core.asset_ref import get_asset_type_config
                if get_asset_type_config(value["asset_type"]) is not None:
                    return
        if value["asset_type"] != expected_asset_type:
            raise TypeError(f"{path}: {field_type.name} field requires {expected_asset_type} reference data")


VALUE_CODECS = ValueCodecRegistry()
