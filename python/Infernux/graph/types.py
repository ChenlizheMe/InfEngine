"""Backend-neutral value and coordinate-space types for authored graphs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from Infernux.engine.path_utils import portable_path


class ValueType(str, Enum):
    BOOL = "bool"
    I32 = "i32"
    U32 = "u32"
    F32 = "f32"
    VEC2 = "vec2"
    VEC3 = "vec3"
    VEC4 = "vec4"
    COLOR = "color"
    MAT3 = "mat3"
    MAT4 = "mat4"
    STRING = "string"
    ASSET_REF = "asset_ref"
    CURVE = "curve"
    GRADIENT = "gradient"


class CoordinateSpace(str, Enum):
    NONE = "none"
    EMITTER_LOCAL = "emitter_local"
    SIMULATION = "simulation"
    WORLD = "world"
    VIEW = "view"
    BILLBOARD = "billboard"
    BAKE_BASIS = "bake_basis"


@dataclass(frozen=True)
class AssetReference:
    guid: str = ""
    path_hint: str = ""

    def __post_init__(self) -> None:
        if type(self.guid) is not str or type(self.path_hint) is not str:
            raise TypeError("asset reference guid and path_hint must be strings")
        object.__setattr__(self, "guid", self.guid.strip())
        object.__setattr__(self, "path_hint", portable_path(self.path_hint.strip()))

    def to_dict(self) -> dict[str, str]:
        return {"guid": self.guid, "path_hint": self.path_hint}

    @classmethod
    def from_dict(cls, value) -> "AssetReference":
        if type(value) is not dict or set(value) != {"guid", "path_hint"}:
            raise ValueError("asset reference requires exactly guid and path_hint")
        return cls(value["guid"], value["path_hint"])


@dataclass(frozen=True, order=True)
class TypeRef:
    value_type: ValueType
    space: CoordinateSpace = CoordinateSpace.NONE

    def __post_init__(self) -> None:
        object.__setattr__(self, "value_type", ValueType(self.value_type))
        object.__setattr__(self, "space", CoordinateSpace(self.space))
        if self.space is not CoordinateSpace.NONE and self.value_type not in {
            ValueType.VEC2,
            ValueType.VEC3,
            ValueType.VEC4,
        }:
            raise ValueError("coordinate spaces are only valid on vector values")

    def to_dict(self) -> dict[str, str]:
        return {"value_type": self.value_type.value, "space": self.space.value}

    @classmethod
    def from_dict(cls, value) -> "TypeRef":
        if type(value) is not dict or set(value) != {"value_type", "space"}:
            raise ValueError("type reference requires value_type and space")
        return cls(ValueType(value["value_type"]), CoordinateSpace(value["space"]))


class TypeSystem:
    """Portable connection and numeric-unification rules."""

    _NUMERIC = frozenset(
        {
            ValueType.I32,
            ValueType.U32,
            ValueType.F32,
            ValueType.VEC2,
            ValueType.VEC3,
            ValueType.VEC4,
            ValueType.COLOR,
        }
    )

    def can_connect(self, source: TypeRef, target: TypeRef) -> bool:
        if source == target:
            return True
        if source.space != target.space:
            return False
        return source.value_type in {ValueType.I32, ValueType.U32} and target.value_type is ValueType.F32

    def unify_numeric(self, left: TypeRef, right: TypeRef) -> TypeRef:
        if left.value_type not in self._NUMERIC or right.value_type not in self._NUMERIC:
            raise TypeError(f"numeric operation cannot use {left} and {right}")
        if (
            left.space is not CoordinateSpace.NONE
            and right.space is not CoordinateSpace.NONE
            and left.space != right.space
        ):
            raise TypeError(
                f"numeric operation cannot mix {left.space.value} and {right.space.value}"
            )
        result_space = (
            left.space
            if left.space is not CoordinateSpace.NONE
            else right.space
        )
        if left == right:
            return left
        scalar = {ValueType.I32, ValueType.U32, ValueType.F32}
        if left.value_type in scalar and right.value_type in scalar:
            if ValueType.F32 in {left.value_type, right.value_type}:
                return TypeRef(ValueType.F32, result_space)
            if left.value_type != right.value_type:
                raise TypeError("signed and unsigned integers require an explicit cast")
            return TypeRef(left.value_type, result_space)
        if left.value_type == right.value_type:
            return TypeRef(left.value_type, result_space)
        if left.value_type is ValueType.COLOR and right.value_type is ValueType.VEC4:
            return TypeRef(ValueType.VEC4, result_space)
        if left.value_type is ValueType.VEC4 and right.value_type is ValueType.COLOR:
            return TypeRef(ValueType.VEC4, result_space)
        raise TypeError(f"numeric operation requires matching shapes, got {left} and {right}")


PORTABLE_TYPE_SYSTEM = TypeSystem()


__all__ = [
    "AssetReference",
    "CoordinateSpace",
    "PORTABLE_TYPE_SYSTEM",
    "TypeRef",
    "TypeSystem",
    "ValueType",
]
