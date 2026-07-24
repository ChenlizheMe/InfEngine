"""Particle data-interface descriptors shared by both frontends."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, TypeAlias
import uuid

from Infernux.graph.types import AssetReference, CoordinateSpace


class ParticleDataInterfaceError(ValueError):
    pass


class VectorFieldBoundary(str, Enum):
    ZERO = "zero"
    CLAMP = "clamp"
    REPEAT = "repeat"


class VectorFieldFilter(str, Enum):
    NEAREST = "nearest"
    LINEAR = "linear"


class SdfFilter(str, Enum):
    NEAREST = "nearest"
    LINEAR = "linear"


def _identity_matrix() -> tuple[float, ...]:
    return (
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def _validate_identity(stable_id: str, name: str, label: str) -> None:
    if type(stable_id) is not str or not stable_id.strip():
        raise ParticleDataInterfaceError(f"{label} stable_id cannot be empty")
    if type(name) is not str or not name.strip():
        raise ParticleDataInterfaceError(f"{label} name cannot be empty")


def _validate_space(value: CoordinateSpace, label: str) -> CoordinateSpace:
    try:
        space = CoordinateSpace(value)
    except (TypeError, ValueError) as exc:
        raise ParticleDataInterfaceError(f"{label} space is invalid") from exc
    if space not in {CoordinateSpace.EMITTER_LOCAL, CoordinateSpace.WORLD}:
        raise ParticleDataInterfaceError(
            f"{label} space must be emitter_local or world"
        )
    return space


def _validate_matrix(value, label: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != 16:
        raise ParticleDataInterfaceError(f"{label} transform requires 16 values")
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ParticleDataInterfaceError(
            f"{label} transform values must be numeric"
        ) from exc
    if not all(math.isfinite(item) for item in result):
        raise ParticleDataInterfaceError(f"{label} transform must be finite")
    return result


@dataclass(frozen=True)
class VectorField:
    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Vector Field"
    texture: AssetReference = AssetReference()
    space: CoordinateSpace = CoordinateSpace.WORLD
    field_to_space: tuple[float, ...] = field(default_factory=_identity_matrix)
    vector_scale: float = 1.0
    boundary: VectorFieldBoundary = VectorFieldBoundary.ZERO
    filtering: VectorFieldFilter = VectorFieldFilter.LINEAR

    kind = "vector_field"

    def __post_init__(self) -> None:
        _validate_identity(self.stable_id, self.name, "vector field")
        if not isinstance(self.texture, AssetReference):
            raise ParticleDataInterfaceError("vector field texture is invalid")
        object.__setattr__(self, "stable_id", self.stable_id.strip())
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "space", _validate_space(self.space, "vector field"))
        object.__setattr__(
            self,
            "field_to_space",
            _validate_matrix(self.field_to_space, "vector field"),
        )
        try:
            scale = float(self.vector_scale)
        except (TypeError, ValueError) as exc:
            raise ParticleDataInterfaceError(
                "vector field scale must be numeric"
            ) from exc
        if not math.isfinite(scale):
            raise ParticleDataInterfaceError("vector field scale must be finite")
        object.__setattr__(self, "vector_scale", scale)
        try:
            boundary = VectorFieldBoundary(self.boundary)
            filtering = VectorFieldFilter(self.filtering)
        except (TypeError, ValueError) as exc:
            raise ParticleDataInterfaceError(
                "vector field boundary or filtering mode is invalid"
            ) from exc
        object.__setattr__(self, "boundary", boundary)
        object.__setattr__(self, "filtering", filtering)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "stable_id": self.stable_id,
            "name": self.name,
            "texture": self.texture.to_dict(),
            "space": self.space.value,
            "field_to_space": list(self.field_to_space),
            "vector_scale": self.vector_scale,
            "boundary": self.boundary.value,
            "filtering": self.filtering.value,
        }


@dataclass(frozen=True)
class SdfVolume:
    """A signed-distance volume sampled by particle collision kernels.

    Source field coordinates use the canonical ``[-0.5, 0.5]^3`` domain. A
    ``+0.5`` offset maps that domain to texture UVW coordinates. The imported
    texture bake basis and ``field_to_space`` place the field in emitter-local
    or world space; samples outside the field never collide.
    """

    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "SDF Volume"
    texture: AssetReference = AssetReference()
    space: CoordinateSpace = CoordinateSpace.WORLD
    field_to_space: tuple[float, ...] = field(default_factory=_identity_matrix)
    distance_scale: float = 1.0
    filtering: SdfFilter = SdfFilter.LINEAR

    kind = "sdf_volume"

    def __post_init__(self) -> None:
        _validate_identity(self.stable_id, self.name, "SDF volume")
        if not isinstance(self.texture, AssetReference):
            raise ParticleDataInterfaceError("SDF volume texture is invalid")
        object.__setattr__(self, "stable_id", self.stable_id.strip())
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "space", _validate_space(self.space, "SDF volume"))
        object.__setattr__(
            self,
            "field_to_space",
            _validate_matrix(self.field_to_space, "SDF volume"),
        )
        try:
            scale = float(self.distance_scale)
            filtering = SdfFilter(self.filtering)
        except (TypeError, ValueError) as exc:
            raise ParticleDataInterfaceError(
                "SDF volume distance scale or filtering mode is invalid"
            ) from exc
        if not math.isfinite(scale) or scale <= 0.0:
            raise ParticleDataInterfaceError(
                "SDF volume distance scale must be finite and positive"
            )
        object.__setattr__(self, "distance_scale", scale)
        object.__setattr__(self, "filtering", filtering)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "stable_id": self.stable_id,
            "name": self.name,
            "texture": self.texture.to_dict(),
            "space": self.space.value,
            "field_to_space": list(self.field_to_space),
            "distance_scale": self.distance_scale,
            "filtering": self.filtering.value,
        }


@dataclass(frozen=True)
class PointCache:
    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Point Cache"
    cache: AssetReference = AssetReference()
    space: CoordinateSpace = CoordinateSpace.WORLD
    cache_to_space: tuple[float, ...] = field(default_factory=_identity_matrix)
    position_channel: str = "position"
    normal_channel: str = "normal"
    color_channel: str = "color"
    id_channel: str = "id"

    kind = "point_cache"

    def __post_init__(self) -> None:
        _validate_identity(self.stable_id, self.name, "point cache")
        if not isinstance(self.cache, AssetReference):
            raise ParticleDataInterfaceError("point cache asset is invalid")
        channels = (
            self.position_channel,
            self.normal_channel,
            self.color_channel,
            self.id_channel,
        )
        if not all(type(value) is str for value in channels):
            raise ParticleDataInterfaceError("point cache channel names must be strings")
        if not self.position_channel.strip() or not self.id_channel.strip():
            raise ParticleDataInterfaceError(
                "point cache position and stable id channels cannot be empty"
            )
        object.__setattr__(self, "stable_id", self.stable_id.strip())
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "space", _validate_space(self.space, "point cache"))
        object.__setattr__(
            self,
            "cache_to_space",
            _validate_matrix(self.cache_to_space, "point cache"),
        )
        object.__setattr__(self, "position_channel", self.position_channel.strip())
        object.__setattr__(self, "normal_channel", self.normal_channel.strip())
        object.__setattr__(self, "color_channel", self.color_channel.strip())
        object.__setattr__(self, "id_channel", self.id_channel.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "stable_id": self.stable_id,
            "name": self.name,
            "cache": self.cache.to_dict(),
            "space": self.space.value,
            "cache_to_space": list(self.cache_to_space),
            "position_channel": self.position_channel,
            "normal_channel": self.normal_channel,
            "color_channel": self.color_channel,
            "id_channel": self.id_channel,
        }


ParticleDataInterface: TypeAlias = VectorField | SdfVolume | PointCache


def particle_data_interface_from_dict(
    value: Any, location: str = "particle data interface"
) -> ParticleDataInterface:
    if type(value) is not dict or type(value.get("kind")) is not str:
        raise ParticleDataInterfaceError(f"{location} must be a typed object")
    kind = value["kind"]
    if kind == VectorField.kind:
        expected = {
            "kind",
            "stable_id",
            "name",
            "texture",
            "space",
            "field_to_space",
            "vector_scale",
            "boundary",
            "filtering",
        }
        if set(value) != expected:
            raise ParticleDataInterfaceError(f"{location} keys do not match VectorField")
        return VectorField(
            stable_id=value["stable_id"],
            name=value["name"],
            texture=AssetReference.from_dict(value["texture"]),
            space=value["space"],
            field_to_space=tuple(value["field_to_space"]),
            vector_scale=value["vector_scale"],
            boundary=value["boundary"],
            filtering=value["filtering"],
        )
    if kind == PointCache.kind:
        expected = {
            "kind",
            "stable_id",
            "name",
            "cache",
            "space",
            "cache_to_space",
            "position_channel",
            "normal_channel",
            "color_channel",
            "id_channel",
        }
        if set(value) != expected:
            raise ParticleDataInterfaceError(f"{location} keys do not match PointCache")
        return PointCache(
            stable_id=value["stable_id"],
            name=value["name"],
            cache=AssetReference.from_dict(value["cache"]),
            space=value["space"],
            cache_to_space=tuple(value["cache_to_space"]),
            position_channel=value["position_channel"],
            normal_channel=value["normal_channel"],
            color_channel=value["color_channel"],
            id_channel=value["id_channel"],
        )
    if kind == SdfVolume.kind:
        expected = {
            "kind",
            "stable_id",
            "name",
            "texture",
            "space",
            "field_to_space",
            "distance_scale",
            "filtering",
        }
        if set(value) != expected:
            raise ParticleDataInterfaceError(f"{location} keys do not match SdfVolume")
        return SdfVolume(
            stable_id=value["stable_id"],
            name=value["name"],
            texture=AssetReference.from_dict(value["texture"]),
            space=value["space"],
            field_to_space=tuple(value["field_to_space"]),
            distance_scale=value["distance_scale"],
            filtering=value["filtering"],
        )
    raise ParticleDataInterfaceError(f"{location} kind {kind!r} is unsupported")


__all__ = [
    "ParticleDataInterface",
    "ParticleDataInterfaceError",
    "PointCache",
    "SdfFilter",
    "SdfVolume",
    "VectorField",
    "VectorFieldBoundary",
    "VectorFieldFilter",
    "particle_data_interface_from_dict",
]
