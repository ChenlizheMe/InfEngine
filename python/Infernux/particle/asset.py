"""Strict multi-emitter ParticleGraph authoring asset."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
from typing import Any, Mapping
import uuid
from pathlib import Path

from Infernux.graph.document import (
    GraphDocument,
    GraphDocumentError,
    GraphLinkRecord,
    GraphNodeRecord,
)
from Infernux.graph.registry import PortKind
from Infernux.graph.types import AssetReference, CoordinateSpace, TypeRef, ValueType

from . import nodes as _particle_nodes  # noqa: F401
from .data_interface import (
    ParticleDataInterface,
    SdfVolume,
    VectorField,
    particle_data_interface_from_dict,
)


PARTICLE_GRAPH_SCHEMA = "infernux.particle_graph"
_PARTICLE_STORAGE_TYPES = frozenset(
    {
        ValueType.BOOL,
        ValueType.I32,
        ValueType.U32,
        ValueType.F32,
        ValueType.VEC2,
        ValueType.VEC3,
        ValueType.VEC4,
        ValueType.COLOR,
        ValueType.MAT3,
        ValueType.MAT4,
    }
)


class ParticleGraphSchemaError(ValueError):
    pass


class SimulationSpace(str, Enum):
    LOCAL = "local"
    WORLD = "world"


class EmitterShapeKind(str, Enum):
    POINT = "point"
    SPHERE = "sphere"
    BOX = "box"
    CONE = "cone"
    MESH = "mesh"
    SDF = "sdf"


class MeshEmissionMode(str, Enum):
    VERTEX = "vertex"
    EDGE = "edge"
    SURFACE = "surface"


class SdfEmissionMode(str, Enum):
    SURFACE = "surface"
    VOLUME = "volume"


@dataclass(frozen=True)
class ScalarRange:
    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        minimum = float(self.minimum)
        maximum = float(self.maximum)
        if not math.isfinite(minimum) or not math.isfinite(maximum) or maximum < minimum:
            raise ParticleGraphSchemaError("scalar range must be finite and ordered")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)

    def to_dict(self) -> dict[str, float]:
        return {"min": self.minimum, "max": self.maximum}

    @classmethod
    def from_dict(cls, value, location: str) -> "ScalarRange":
        _exact_object(value, {"min", "max"}, location)
        return cls(value["min"], value["max"])


@dataclass(frozen=True)
class ParticleBurst:
    time: float
    count: int
    cycles: int = 1
    interval: float = 0.0
    probability: float = 1.0

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.time)) or float(self.time) < 0.0:
            raise ParticleGraphSchemaError("burst time must be finite and non-negative")
        if type(self.count) is not int or self.count < 0:
            raise ParticleGraphSchemaError("burst count must be a non-negative integer")
        if type(self.cycles) is not int or self.cycles <= 0:
            raise ParticleGraphSchemaError("burst cycles must be a positive integer")
        if not math.isfinite(float(self.interval)) or float(self.interval) < 0.0:
            raise ParticleGraphSchemaError("burst interval must be finite and non-negative")
        if (
            not math.isfinite(float(self.probability))
            or not 0.0 <= float(self.probability) <= 1.0
        ):
            raise ParticleGraphSchemaError("burst probability must be between zero and one")

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": float(self.time),
            "count": self.count,
            "cycles": self.cycles,
            "interval": float(self.interval),
            "probability": float(self.probability),
        }

    @classmethod
    def from_dict(cls, value, location: str) -> "ParticleBurst":
        _exact_object(
            value,
            {"time", "count", "cycles", "interval", "probability"},
            location,
        )
        return cls(
            value["time"],
            value["count"],
            value["cycles"],
            value["interval"],
            value["probability"],
        )


@dataclass(frozen=True)
class EmitterShape:
    kind: EmitterShapeKind = EmitterShapeKind.POINT
    space: CoordinateSpace = CoordinateSpace.EMITTER_LOCAL
    radius: float = 0.0
    angle_degrees: float = 25.0
    dimensions: tuple[float, float, float] = (1.0, 1.0, 1.0)
    mesh: AssetReference = AssetReference()
    mesh_mode: MeshEmissionMode = MeshEmissionMode.SURFACE
    sdf_interface: str = ""
    sdf_mode: SdfEmissionMode = SdfEmissionMode.SURFACE

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EmitterShapeKind(self.kind))
        object.__setattr__(self, "space", CoordinateSpace(self.space))
        object.__setattr__(self, "mesh_mode", MeshEmissionMode(self.mesh_mode))
        object.__setattr__(self, "sdf_mode", SdfEmissionMode(self.sdf_mode))
        if self.space not in {CoordinateSpace.EMITTER_LOCAL, CoordinateSpace.WORLD}:
            raise ParticleGraphSchemaError("emitter shape space must be emitter_local or world")
        if not math.isfinite(float(self.radius)) or float(self.radius) < 0.0:
            raise ParticleGraphSchemaError("emitter shape radius must be finite and non-negative")
        if not math.isfinite(float(self.angle_degrees)) or not 0.0 <= float(self.angle_degrees) <= 180.0:
            raise ParticleGraphSchemaError("emitter cone angle must be between 0 and 180 degrees")
        if len(self.dimensions) != 3 or any(
            not math.isfinite(float(value)) or float(value) < 0.0 for value in self.dimensions
        ):
            raise ParticleGraphSchemaError("emitter shape dimensions require three non-negative values")
        object.__setattr__(self, "dimensions", tuple(float(value) for value in self.dimensions))
        if not isinstance(self.mesh, AssetReference):
            raise ParticleGraphSchemaError("emitter shape mesh must be an AssetReference")
        if type(self.sdf_interface) is not str:
            raise ParticleGraphSchemaError("emitter shape SDF interface must be a string")
        object.__setattr__(self, "sdf_interface", self.sdf_interface.strip())
        if self.kind is EmitterShapeKind.SDF and not self.sdf_interface:
            raise ParticleGraphSchemaError(
                "SDF emitter shape requires a Signed Distance Volume interface"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "space": self.space.value,
            "radius": float(self.radius),
            "angle_degrees": float(self.angle_degrees),
            "dimensions": list(self.dimensions),
            "mesh": self.mesh.to_dict(),
            "mesh_mode": self.mesh_mode.value,
            "sdf_interface": self.sdf_interface,
            "sdf_mode": self.sdf_mode.value,
        }

    @classmethod
    def from_dict(cls, value, location: str) -> "EmitterShape":
        _exact_object(
            value,
            {
                "kind",
                "space",
                "radius",
                "angle_degrees",
                "dimensions",
                "mesh",
                "mesh_mode",
                "sdf_interface",
                "sdf_mode",
            },
            location,
        )
        if type(value["dimensions"]) is not list:
            raise ParticleGraphSchemaError(f"{location}.dimensions must be an array")
        return cls(
            value["kind"],
            value["space"],
            value["radius"],
            value["angle_degrees"],
            tuple(value["dimensions"]),
            AssetReference.from_dict(value["mesh"]),
            value["mesh_mode"],
            value["sdf_interface"],
            value["sdf_mode"],
        )


@dataclass(frozen=True)
class EmitterSettings:
    capacity: int = 1000
    simulation_space: SimulationSpace = SimulationSpace.WORLD
    seed: int = 1
    spawn_rate: float = 10.0
    spawn_rate_over_distance: float = 0.0
    duration: float = 5.0
    loop: bool = True
    start_delay: float = 0.0
    collision_enabled: bool = False
    collision_layer_mask: int = 0xFFFFFFFF
    collision_include_triggers: bool = True
    collision_bounce_scale: float = 1.0
    collision_friction_scale: float = 1.0
    bursts: tuple[ParticleBurst, ...] = ()
    shape: EmitterShape = EmitterShape()

    def __post_init__(self) -> None:
        if type(self.capacity) is not int or self.capacity <= 0:
            raise ParticleGraphSchemaError("emitter capacity must be a positive integer")
        object.__setattr__(self, "simulation_space", SimulationSpace(self.simulation_space))
        if type(self.seed) is not int or not 0 <= self.seed <= 0xFFFFFFFF:
            raise ParticleGraphSchemaError("emitter seed must be an unsigned 32-bit integer")
        if not math.isfinite(float(self.spawn_rate)) or float(self.spawn_rate) < 0.0:
            raise ParticleGraphSchemaError("spawn rate must be finite and non-negative")
        if (
            not math.isfinite(float(self.spawn_rate_over_distance))
            or float(self.spawn_rate_over_distance) < 0.0
        ):
            raise ParticleGraphSchemaError(
                "spawn rate over distance must be finite and non-negative"
            )
        if not math.isfinite(float(self.duration)) or float(self.duration) <= 0.0:
            raise ParticleGraphSchemaError("emitter duration must be finite and positive")
        if type(self.loop) is not bool:
            raise ParticleGraphSchemaError("emitter loop must be a boolean")
        if not math.isfinite(float(self.start_delay)) or float(self.start_delay) < 0.0:
            raise ParticleGraphSchemaError(
                "emitter start delay must be finite and non-negative"
            )
        if type(self.collision_enabled) is not bool:
            raise ParticleGraphSchemaError("emitter collision_enabled must be a boolean")
        if (
            type(self.collision_layer_mask) is not int
            or not 0 <= self.collision_layer_mask <= 0xFFFFFFFF
        ):
            raise ParticleGraphSchemaError(
                "emitter collision_layer_mask must be an unsigned 32-bit integer"
            )
        if type(self.collision_include_triggers) is not bool:
            raise ParticleGraphSchemaError(
                "emitter collision_include_triggers must be a boolean"
            )
        if (
            not math.isfinite(float(self.collision_bounce_scale))
            or float(self.collision_bounce_scale) < 0.0
        ):
            raise ParticleGraphSchemaError(
                "emitter collision_bounce_scale must be finite and non-negative"
            )
        if (
            not math.isfinite(float(self.collision_friction_scale))
            or float(self.collision_friction_scale) < 0.0
        ):
            raise ParticleGraphSchemaError(
                "emitter collision_friction_scale must be finite and non-negative"
            )
        bursts = tuple(self.bursts)
        if not all(isinstance(value, ParticleBurst) for value in bursts):
            raise ParticleGraphSchemaError("emitter bursts must contain ParticleBurst values")
        if any(
            burst.time + (burst.cycles - 1) * burst.interval > float(self.duration)
            for burst in bursts
        ):
            raise ParticleGraphSchemaError(
                "emitter burst events must fit within the emitter duration"
            )
        if not isinstance(self.shape, EmitterShape):
            raise ParticleGraphSchemaError("emitter shape must be an EmitterShape")
        object.__setattr__(self, "bursts", bursts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "simulation_space": self.simulation_space.value,
            "seed": self.seed,
            "spawn_rate": float(self.spawn_rate),
            "spawn_rate_over_distance": float(self.spawn_rate_over_distance),
            "duration": float(self.duration),
            "loop": self.loop,
            "start_delay": float(self.start_delay),
            "collision_enabled": self.collision_enabled,
            "collision_layer_mask": self.collision_layer_mask,
            "collision_include_triggers": self.collision_include_triggers,
            "collision_bounce_scale": float(self.collision_bounce_scale),
            "collision_friction_scale": float(self.collision_friction_scale),
            "bursts": [burst.to_dict() for burst in self.bursts],
            "shape": self.shape.to_dict(),
        }

    @classmethod
    def from_dict(cls, value, location: str) -> "EmitterSettings":
        expected = {
            "capacity", "simulation_space", "seed", "spawn_rate",
            "spawn_rate_over_distance", "duration", "loop", "start_delay",
            "collision_enabled", "collision_layer_mask",
            "collision_include_triggers", "collision_bounce_scale",
            "collision_friction_scale", "bursts", "shape",
        }
        _exact_object(value, expected, location)
        if type(value["bursts"]) is not list:
            raise ParticleGraphSchemaError(f"{location}.bursts must be an array")
        return cls(
            capacity=value["capacity"],
            simulation_space=value["simulation_space"],
            seed=value["seed"],
            spawn_rate=value["spawn_rate"],
            spawn_rate_over_distance=value["spawn_rate_over_distance"],
            duration=value["duration"],
            loop=value["loop"],
            start_delay=value["start_delay"],
            collision_enabled=value["collision_enabled"],
            collision_layer_mask=value["collision_layer_mask"],
            collision_include_triggers=value["collision_include_triggers"],
            collision_bounce_scale=value["collision_bounce_scale"],
            collision_friction_scale=value["collision_friction_scale"],
            bursts=tuple(
                ParticleBurst.from_dict(item, f"{location}.bursts[{index}]")
                for index, item in enumerate(value["bursts"])
            ),
            shape=EmitterShape.from_dict(value["shape"], f"{location}.shape"),
        )


@dataclass(frozen=True)
class ParticleAttribute:
    stable_id: str
    name: str
    value_type: TypeRef
    default: Any

    def __post_init__(self) -> None:
        if type(self.stable_id) is not str or not self.stable_id:
            raise ParticleGraphSchemaError("particle attribute stable_id cannot be empty")
        if type(self.name) is not str or not self.name:
            raise ParticleGraphSchemaError("particle attribute name cannot be empty")
        if not isinstance(self.value_type, TypeRef):
            raise ParticleGraphSchemaError("particle attribute type must be a TypeRef")
        if self.value_type.value_type not in _PARTICLE_STORAGE_TYPES:
            raise ParticleGraphSchemaError(
                f"particle attribute {self.name!r} must use a GPU-storable type"
            )
        from Infernux.graph.expression_ir import ExpressionCompiler

        error = ExpressionCompiler._literal_error(self.value_type, self.default)
        if error:
            raise ParticleGraphSchemaError(f"particle attribute {self.name!r} default {error}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "name": self.name,
            "type": self.value_type.to_dict(),
            "default": self.default,
        }

    @classmethod
    def from_dict(cls, value, location: str) -> "ParticleAttribute":
        _exact_object(value, {"stable_id", "name", "type", "default"}, location)
        if type(value["stable_id"]) is not str or type(value["name"]) is not str:
            raise ParticleGraphSchemaError(f"{location} identity must be strings")
        return cls(value["stable_id"], value["name"], TypeRef.from_dict(value["type"]), value["default"])


_PARTICLE_PARAMETER_TYPES = frozenset(
    {
        ValueType.BOOL,
        ValueType.I32,
        ValueType.U32,
        ValueType.F32,
        ValueType.VEC2,
        ValueType.VEC3,
        ValueType.VEC4,
        ValueType.COLOR,
        ValueType.CURVE,
        ValueType.GRADIENT,
        ValueType.TEXTURE2D,
        ValueType.MESH,
    }
)


@dataclass(frozen=True)
class ParticleParameter:
    """A graph-level value with a stable runtime ABI identity.

    Parameters are not particle attributes: one value belongs to a ParticleGraph
    instance and may be read by every particle without consuming per-particle
    state.  The stable id is the compiled/runtime key; the display name may be
    changed freely without breaking overrides.
    """

    stable_id: str
    name: str
    value_type: TypeRef
    default: Any
    exposed: bool = True
    category: str = ""
    tooltip: str = ""
    writable: bool = False

    def __post_init__(self) -> None:
        if type(self.stable_id) is not str or not self.stable_id:
            raise ParticleGraphSchemaError("particle parameter stable_id cannot be empty")
        if type(self.name) is not str or not self.name:
            raise ParticleGraphSchemaError("particle parameter name cannot be empty")
        if not isinstance(self.value_type, TypeRef):
            raise ParticleGraphSchemaError("particle parameter type must be a TypeRef")
        if self.value_type.space is not CoordinateSpace.NONE and not (
            self.value_type.value_type is ValueType.VEC3
            and self.value_type.space is CoordinateSpace.WORLD
        ):
            raise ParticleGraphSchemaError(
                "particle parameters may only carry world-space vec3 data"
            )
        if self.value_type.value_type not in _PARTICLE_PARAMETER_TYPES:
            raise ParticleGraphSchemaError(
                f"particle parameter {self.name!r} uses an unsupported type"
            )
        if type(self.exposed) is not bool:
            raise ParticleGraphSchemaError("particle parameter exposed must be a bool")
        if type(self.writable) is not bool:
            raise ParticleGraphSchemaError("particle parameter writable must be a bool")
        if self.writable and self.value_type.value_type in {
            ValueType.CURVE,
            ValueType.GRADIENT,
            ValueType.TEXTURE2D,
            ValueType.MESH,
        }:
            raise ParticleGraphSchemaError(
                f"particle parameter {self.name!r} cannot be writable because "
                f"{self.value_type.value_type.value} is a resource or structured lookup"
            )
        if type(self.category) is not str or type(self.tooltip) is not str:
            raise ParticleGraphSchemaError("particle parameter metadata must be strings")
        from Infernux.graph.expression_ir import ExpressionCompiler

        error = ExpressionCompiler._literal_error(self.value_type, self.default)
        if error:
            raise ParticleGraphSchemaError(
                f"particle parameter {self.name!r} default {error}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "name": self.name,
            "type": self.value_type.to_dict(),
            "default": self.default,
            "exposed": self.exposed,
            "writable": self.writable,
            "category": self.category,
            "tooltip": self.tooltip,
        }

    @classmethod
    def from_dict(cls, value, location: str) -> "ParticleParameter":
        _exact_object(
            value,
            {
                "stable_id",
                "name",
                "type",
                "default",
                "exposed",
                "writable",
                "category",
                "tooltip",
            },
            location,
        )
        if type(value["stable_id"]) is not str or type(value["name"]) is not str:
            raise ParticleGraphSchemaError(f"{location} identity must be strings")
        return cls(
            value["stable_id"],
            value["name"],
            TypeRef.from_dict(value["type"]),
            value["default"],
            value["exposed"],
            value["category"],
            value["tooltip"],
            value["writable"],
        )


_ROOTS = {
    "init": ("particle.init", "root.init", "particle.root.init"),
    "update": ("particle.update", "root.update", "particle.root.update"),
    "collision_enter": (
        "particle.collision_enter",
        "root.collision_enter",
        "particle.root.collision_enter",
    ),
    "collision_stay": (
        "particle.collision_stay",
        "root.collision_stay",
        "particle.root.collision_stay",
    ),
    "collision_exit": (
        "particle.collision_exit",
        "root.collision_exit",
        "particle.root.collision_exit",
    ),
    "rendering": ("particle.rendering", "root.rendering", "particle.root.rendering"),
}


def default_stage_graph(stage: str) -> GraphDocument:
    domain, root_uid, root_type = _ROOTS[stage]
    nodes = [GraphNodeRecord(root_uid, root_type, (0.0, 0.0))]
    links = []
    if stage == "init":
        nodes.extend(
            (
                GraphNodeRecord(
                    "init.lifetime",
                    "particle.attribute.lifetime",
                    (280.0, -70.0),
                    properties={"composition": "set", "value": 5.0},
                ),
                GraphNodeRecord(
                    "init.velocity",
                    "particle.attribute.velocity",
                    (560.0, -70.0),
                    properties={
                        "composition": "set",
                        "value": [0.0, 1.0, 0.0],
                    },
                ),
            )
        )
        links.extend(
            (
                GraphLinkRecord(
                    "root-to-lifetime", root_uid, "out", "init.lifetime", "in", PortKind.EXEC
                ),
                GraphLinkRecord(
                    "lifetime-to-velocity",
                    "init.lifetime",
                    "out",
                    "init.velocity",
                    "in",
                    PortKind.EXEC,
                ),
            )
        )
    elif stage == "update":
        nodes.append(
            GraphNodeRecord(
                "update.velocity",
                "particle.attribute.velocity",
                (280.0, -70.0),
                properties={
                    "composition": "add",
                    "value": [0.0, -0.01, 0.0],
                },
            )
        )
        links.append(
            GraphLinkRecord(
                "root-to-velocity",
                root_uid,
                "out",
                "update.velocity",
                "in",
                PortKind.EXEC,
            )
        )
    elif stage == "rendering":
        nodes.append(GraphNodeRecord("output.sprite", "particle.output.sprite", (280.0, 0.0)))
        links.append(
            GraphLinkRecord(
                "root-to-sprite",
                root_uid,
                "out",
                "output.sprite",
                "in",
                PortKind.EXEC,
            )
        )
    return GraphDocument(domain, tuple(nodes), tuple(links))


def standard_particle_attributes() -> tuple[ParticleAttribute, ...]:
    return (
        ParticleAttribute("builtin.position", "position", TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION), [0.0, 0.0, 0.0]),
        # Keep the scalar beside position so std430 uses vec3's trailing word instead of growing the state stride.
        ParticleAttribute("builtin.rotation", "rotation", TypeRef(ValueType.F32), 0.0),
        ParticleAttribute("builtin.velocity", "velocity", TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION), [0.0, 0.0, 0.0]),
        ParticleAttribute("builtin.color", "color", TypeRef(ValueType.COLOR), [1.0, 1.0, 1.0, 1.0]),
        ParticleAttribute("builtin.size", "size", TypeRef(ValueType.F32), 1.0),
        ParticleAttribute("builtin.age", "age", TypeRef(ValueType.F32), 0.0),
        ParticleAttribute("builtin.lifetime", "lifetime", TypeRef(ValueType.F32), 5.0),
        ParticleAttribute("builtin.id", "particle_id", TypeRef(ValueType.U32), 0),
    )


def optional_particle_attributes() -> tuple[ParticleAttribute, ...]:
    """Builtin attributes allocated only when a graph or output requires them."""
    return (
        ParticleAttribute(
            "builtin.orientation",
            "orientation",
            TypeRef(ValueType.VEC3),
            [0.0, 0.0, 0.0],
        ),
        ParticleAttribute(
            "builtin.scale",
            "scale",
            TypeRef(ValueType.VEC3),
            [1.0, 1.0, 1.0],
        ),
        ParticleAttribute(
            "builtin.flipbook_frame",
            "flipbook_frame",
            TypeRef(ValueType.F32),
            0.0,
        ),
        ParticleAttribute(
            "builtin.ribbon_strip_id",
            "ribbon_strip_id",
            TypeRef(ValueType.U32),
            0,
        ),
        ParticleAttribute(
            "builtin.ribbon_order",
            "ribbon_order",
            TypeRef(ValueType.U32),
            0,
        ),
        ParticleAttribute(
            "builtin.ribbon_break",
            "ribbon_break",
            TypeRef(ValueType.BOOL),
            False,
        ),
        ParticleAttribute(
            "builtin.collision_hit",
            "collision_hit",
            TypeRef(ValueType.BOOL),
            False,
        ),
        ParticleAttribute(
            "builtin.collision_normal",
            "collision_normal",
            TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
            [0.0, 0.0, 0.0],
        ),
        ParticleAttribute(
            "builtin.collision_point",
            "collision_point",
            TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
            [0.0, 0.0, 0.0],
        ),
        ParticleAttribute(
            "builtin.collision_relative_velocity",
            "collision_relative_velocity",
            TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
            [0.0, 0.0, 0.0],
        ),
        ParticleAttribute(
            "builtin.collision_penetration",
            "collision_penetration",
            TypeRef(ValueType.F32),
            0.0,
        ),
        ParticleAttribute(
            "builtin.collision_is_trigger",
            "collision_is_trigger",
            TypeRef(ValueType.BOOL),
            False,
        ),
        ParticleAttribute(
            "builtin.collision_material",
            "collision_material",
            TypeRef(ValueType.VEC4),
            [0.0, 0.0, 0.0, 0.0],
        ),
        # Exact collider identity stays split into uint words so the GPU path
        # does not require optional 64-bit shader integer support.
        ParticleAttribute(
            "builtin.collision_collider_id_low",
            "collision_collider_id_low",
            TypeRef(ValueType.U32),
            0,
        ),
        ParticleAttribute(
            "builtin.collision_collider_id_high",
            "collision_collider_id_high",
            TypeRef(ValueType.U32),
            0,
        ),
    )


def particle_attribute_capture_id(stage: str, node_uid: str) -> str:
    """Return the stable per-particle slot used by an Exec-bound Get Attribute."""
    return f"capture.{str(stage)}.{str(node_uid)}"


def particle_attribute_cache_id(stage: str, node_uid: str) -> str:
    """Return the stable storage identity owned by one Attribute Cache node."""
    return f"cache.{str(stage)}.{str(node_uid)}"


def _particle_storage_stage(stage: str) -> str:
    """Map an authored flow path to its runtime lifecycle storage domain."""
    return str(stage)


def particle_attribute_zero(value_type: TypeRef):
    kind = value_type.value_type
    if kind is ValueType.BOOL:
        return False
    if kind in {ValueType.I32, ValueType.U32}:
        return 0
    if kind is ValueType.F32:
        return 0.0
    width = {
        ValueType.VEC2: 2,
        ValueType.VEC3: 3,
        ValueType.VEC4: 4,
        ValueType.COLOR: 4,
        ValueType.MAT3: 9,
        ValueType.MAT4: 16,
    }.get(kind)
    if width is None:
        raise ParticleGraphSchemaError(
            f"particle capture cannot store {kind.value!r}"
        )
    return [0.0] * width


def particle_cache_attributes(emitter) -> tuple[ParticleAttribute, ...]:
    """Derive persistent per-particle storage directly from cache nodes."""
    caches: list[ParticleAttribute] = []
    known: dict[str, ParticleAttribute] = {}
    documents = [
        (stage, getattr(emitter, stage))
        for stage in (
            "init", "update", "collision_enter", "collision_stay",
            "collision_exit", "rendering",
        )
    ]
    documents.extend(
        (f"event.{flow.stable_id}", flow.graph) for flow in emitter.event_flows
    )
    for stage, document in documents:
        if document is None:
            continue
        for node in document.nodes:
            if node.type_id != "particle.attribute.cache":
                continue
            try:
                value_type = TypeRef(
                    ValueType(str(node.properties.get("value_type", "f32"))),
                    CoordinateSpace(str(node.properties.get("value_space", "none"))),
                )
                name = str(node.properties.get("name", "Attribute Cache")).strip()
                if not name:
                    raise ValueError("name must not be empty")
                attribute = ParticleAttribute(
                    particle_attribute_cache_id(
                        _particle_storage_stage(stage), node.uid
                    ),
                    name,
                    value_type,
                    particle_attribute_zero(value_type),
                )
            except (TypeError, ValueError) as exc:
                raise ParticleGraphSchemaError(
                    f"invalid Attribute Cache node {node.uid!r} in {stage}: {exc}"
                ) from exc
            existing = known.get(attribute.stable_id)
            if existing is not None and existing != attribute:
                raise ParticleGraphSchemaError(
                    f"particle cache identity collision for {attribute.stable_id!r}"
                )
            if existing is None:
                known[attribute.stable_id] = attribute
                caches.append(attribute)
    return tuple(caches)


def captured_particle_attributes(emitter) -> tuple[ParticleAttribute, ...]:
    """Derive sample-and-hold slots from Exec-bound Get Attribute nodes."""
    attributes = {
        attribute.stable_id: attribute
        for attribute in (
            *emitter.attributes,
            *optional_particle_attributes(),
            *particle_cache_attributes(emitter),
        )
    }
    captures: list[ParticleAttribute] = []
    pending = []
    documents = [
        (stage, getattr(emitter, stage))
        for stage in (
            "init", "update", "collision_enter", "collision_stay",
            "collision_exit", "rendering",
        )
    ]
    documents.extend(
        (f"event.{flow.stable_id}", flow.graph) for flow in emitter.event_flows
    )
    for stage, document in documents:
        if document is None:
            continue
        sampled = {
            link.target_node
            for link in document.links
            if link.kind is PortKind.EXEC and link.target_port == "in"
        }
        pending.extend(
            (stage, node)
            for node in document.nodes
            if node.type_id == "particle.attribute.get" and node.uid in sampled
        )

    while pending:
        progressed = False
        deferred = []
        for stage, node in pending:
            source_id = str(node.properties.get("attribute", "builtin.position"))
            source = attributes.get(source_id)
            if source is None:
                deferred.append((stage, node))
                continue
            stable_id = particle_attribute_capture_id(
                _particle_storage_stage(stage), node.uid
            )
            capture = ParticleAttribute(
                stable_id,
                f"{node.uid}_sample",
                source.value_type,
                particle_attribute_zero(source.value_type),
            )
            existing = attributes.get(stable_id)
            if existing is not None and existing != capture:
                raise ParticleGraphSchemaError(
                    f"particle capture identity collision for {stable_id!r}"
                )
            if existing is None:
                attributes[stable_id] = capture
                captures.append(capture)
            progressed = True
        if not progressed:
            break
        pending = deferred
    return tuple(captures)


def particle_attribute_catalog(emitter) -> tuple[ParticleAttribute, ...]:
    """Return every attribute an emitter graph may read, without allocating it."""
    attributes = list(emitter.attributes)
    known = {attribute.stable_id for attribute in attributes}
    attributes.extend(
        attribute
        for attribute in optional_particle_attributes()
        if attribute.stable_id not in known
    )
    known.update(attribute.stable_id for attribute in attributes)
    attributes.extend(
        attribute
        for attribute in particle_cache_attributes(emitter)
        if attribute.stable_id not in known
    )
    known.update(attribute.stable_id for attribute in attributes)
    attributes.extend(
        attribute
        for attribute in captured_particle_attributes(emitter)
        if attribute.stable_id not in known
    )
    return tuple(attributes)


@dataclass(frozen=True)
class ParticleEventFlow:
    stable_id: str
    graph: GraphDocument

    def __post_init__(self) -> None:
        if type(self.stable_id) is not str or not self.stable_id:
            raise ParticleGraphSchemaError("particle event flow stable_id cannot be empty")
        if not isinstance(self.graph, GraphDocument):
            raise ParticleGraphSchemaError("particle event flow graph is invalid")
        roots = [
            node
            for node in self.graph.nodes
            if node.type_id == _particle_nodes.PARTICLE_EVENT_ACTIVE_TYPE_ID
        ]
        if (
            self.graph.domain != "particle.event"
            or len(roots) != 1
            or roots[0].uid != "root.event"
        ):
            raise ParticleGraphSchemaError(
                "particle event flow requires exactly one immutable Active Event root"
            )
        event_id = roots[0].properties.get("event", "")
        if type(event_id) is not str:
            raise ParticleGraphSchemaError("Active Event selection must be a string")

    @property
    def event_id(self) -> str:
        root = next(node for node in self.graph.nodes if node.uid == "root.event")
        return str(root.properties.get("event", ""))

    def to_dict(self) -> dict[str, Any]:
        return {"stable_id": self.stable_id, "graph": self.graph.to_dict()}

    @classmethod
    def from_dict(cls, value, location: str) -> "ParticleEventFlow":
        _exact_object(value, {"stable_id", "graph"}, location)
        return cls(
            value["stable_id"],
            _parse_stage_document(value["graph"], f"{location}.graph"),
        )


def default_event_graph(event_id: str = "") -> GraphDocument:
    return GraphDocument(
        "particle.event",
        (
            GraphNodeRecord(
                "root.event",
                _particle_nodes.PARTICLE_EVENT_ACTIVE_TYPE_ID,
                (0.0, 0.0),
                {"event": str(event_id)},
            ),
        ),
        (),
    )


@dataclass(frozen=True)
class ParticleEmitterAsset:
    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Emitter"
    settings: EmitterSettings = EmitterSettings()
    attributes: tuple[ParticleAttribute, ...] = field(default_factory=standard_particle_attributes)
    data_interfaces: tuple[ParticleDataInterface, ...] = ()
    init: GraphDocument = field(default_factory=lambda: default_stage_graph("init"))
    update: GraphDocument = field(default_factory=lambda: default_stage_graph("update"))
    collision_enter: GraphDocument | None = None
    collision_stay: GraphDocument | None = None
    collision_exit: GraphDocument | None = None
    rendering: GraphDocument = field(default_factory=lambda: default_stage_graph("rendering"))
    event_flows: tuple[ParticleEventFlow, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.stable_id) is not str
            or type(self.name) is not str
            or not self.stable_id
            or not self.name
        ):
            raise ParticleGraphSchemaError("particle emitter requires stable_id and name")
        if not isinstance(self.settings, EmitterSettings):
            raise ParticleGraphSchemaError("particle emitter settings are invalid")
        if not all(
            isinstance(value, GraphDocument)
            for value in (self.init, self.update, self.rendering)
        ) or not all(
            value is None or isinstance(value, GraphDocument)
            for value in (
                self.collision_enter,
                self.collision_stay,
                self.collision_exit,
            )
        ):
            raise ParticleGraphSchemaError("particle emitter stages must be GraphDocument values")
        collision_stages = {
            "collision_enter": self.collision_enter,
            "collision_stay": self.collision_stay,
            "collision_exit": self.collision_exit,
        }
        attributes = tuple(self.attributes)
        data_interfaces = tuple(self.data_interfaces)
        event_flows = tuple(self.event_flows)
        if not all(isinstance(value, ParticleAttribute) for value in attributes):
            raise ParticleGraphSchemaError("particle emitter attributes are invalid")
        if not all(isinstance(value, (VectorField, SdfVolume)) for value in data_interfaces):
            raise ParticleGraphSchemaError("particle emitter data interfaces are invalid")
        if not all(isinstance(value, ParticleEventFlow) for value in event_flows):
            raise ParticleGraphSchemaError("particle emitter event flows are invalid")
        if len({value.stable_id for value in event_flows}) != len(event_flows):
            raise ParticleGraphSchemaError("particle event flow stable ids must be unique")
        object.__setattr__(self, "attributes", attributes)
        object.__setattr__(self, "data_interfaces", data_interfaces)
        object.__setattr__(self, "event_flows", event_flows)
        _validate_stage_root("init", self.init)
        _validate_stage_root("update", self.update)
        for stage, document in collision_stages.items():
            if document is not None:
                _validate_stage_root(stage, document)
        _validate_stage_root("rendering", self.rendering)
        if len({attribute.stable_id for attribute in self.attributes}) != len(self.attributes):
            raise ParticleGraphSchemaError("particle attribute stable ids must be unique")
        standards = standard_particle_attributes()
        if (
            len(self.attributes) != len(standards)
            or any(
                (actual.stable_id, actual.name, actual.value_type)
                != (expected.stable_id, expected.name, expected.value_type)
                for actual, expected in zip(self.attributes, standards)
            )
        ):
            raise ParticleGraphSchemaError(
                "particle emitter attributes are fixed builtins; Attribute Cache storage "
                "must be declared by graph nodes"
            )
        if len({interface.stable_id for interface in data_interfaces}) != len(data_interfaces):
            raise ParticleGraphSchemaError("particle data-interface stable ids must be unique")
        if self.settings.shape.kind is EmitterShapeKind.SDF:
            interface = next(
                (
                    item
                    for item in data_interfaces
                    if item.stable_id == self.settings.shape.sdf_interface
                ),
                None,
            )
            if not isinstance(interface, SdfVolume):
                raise ParticleGraphSchemaError(
                    "SDF emitter shape must reference a Signed Distance Volume "
                    "Data Interface owned by the same emitter"
                )

    def to_dict(self) -> dict[str, Any]:
        standard_defaults = {
            attribute.stable_id: attribute.default
            for attribute in standard_particle_attributes()
        }
        return {
            "stable_id": self.stable_id,
            "name": self.name,
            "settings": self.settings.to_dict(),
            "attribute_defaults": {
                attribute.stable_id: attribute.default
                for attribute in self.attributes
                if attribute.stable_id.startswith("builtin.")
                and attribute.default != standard_defaults[attribute.stable_id]
            },
            "data_interfaces": [interface.to_dict() for interface in self.data_interfaces],
            "events": [value.to_dict() for value in self.event_flows],
            "stages": {
                "init": self.init.to_dict(),
                "update": self.update.to_dict(),
                "collision_enter": (
                    self.collision_enter.to_dict()
                    if self.collision_enter is not None
                    else None
                ),
                "collision_stay": (
                    self.collision_stay.to_dict()
                    if self.collision_stay is not None
                    else None
                ),
                "collision_exit": (
                    self.collision_exit.to_dict()
                    if self.collision_exit is not None
                    else None
                ),
                "rendering": self.rendering.to_dict(),
            },
        }

    @classmethod
    def from_dict(cls, value, location: str) -> "ParticleEmitterAsset":
        _exact_object(
            value,
            {
                "stable_id",
                "name",
                "settings",
                "attribute_defaults",
                "data_interfaces",
                "events",
                "stages",
            },
            location,
        )
        _exact_object(
            value["stages"],
            {
                "init",
                "update",
                "collision_enter",
                "collision_stay",
                "collision_exit",
                "rendering",
            },
            f"{location}.stages",
        )
        if (
            type(value["attribute_defaults"]) is not dict
            or type(value["data_interfaces"]) is not list
            or type(value["events"]) is not list
        ):
            raise ParticleGraphSchemaError(
                f"{location}.attribute_defaults must be an object and data_interfaces must be an array"
            )
        standards = standard_particle_attributes()
        standard_by_id = {attribute.stable_id: attribute for attribute in standards}
        unknown_defaults = set(value["attribute_defaults"]) - set(standard_by_id)
        if unknown_defaults:
            raise ParticleGraphSchemaError(
                f"{location}.attribute_defaults contains unknown builtin attributes: {sorted(unknown_defaults)}"
            )
        builtin_attributes = tuple(
            ParticleAttribute(
                attribute.stable_id,
                attribute.name,
                attribute.value_type,
                value["attribute_defaults"].get(attribute.stable_id, attribute.default),
            )
            for attribute in standards
        )
        return cls(
            stable_id=value["stable_id"],
            name=value["name"],
            settings=EmitterSettings.from_dict(value["settings"], f"{location}.settings"),
            attributes=builtin_attributes,
            data_interfaces=tuple(
                particle_data_interface_from_dict(
                    item, f"{location}.data_interfaces[{index}]"
                )
                for index, item in enumerate(value["data_interfaces"])
            ),
            event_flows=tuple(
                ParticleEventFlow.from_dict(item, f"{location}.events[{index}]")
                for index, item in enumerate(value["events"])
            ),
            init=_parse_stage_document(value["stages"]["init"], f"{location}.stages.init"),
            update=_parse_stage_document(value["stages"]["update"], f"{location}.stages.update"),
            collision_enter=_parse_optional_stage_document(
                value["stages"]["collision_enter"],
                f"{location}.stages.collision_enter",
            ),
            collision_stay=_parse_optional_stage_document(
                value["stages"]["collision_stay"],
                f"{location}.stages.collision_stay",
            ),
            collision_exit=_parse_optional_stage_document(
                value["stages"]["collision_exit"],
                f"{location}.stages.collision_exit",
            ),
            rendering=_parse_stage_document(
                value["stages"]["rendering"],
                f"{location}.stages.rendering",
            ),
        )


@dataclass(frozen=True)
class ParticleEventField:
    stable_id: str
    name: str
    value_type: TypeRef
    default: Any

    def __post_init__(self) -> None:
        # Event payload fields intentionally share the portable numeric type
        # contract with particle attributes, but remain a distinct authored
        # identity so the event ABI can evolve independently of emitter state.
        ParticleAttribute(self.stable_id, self.name, self.value_type, self.default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "name": self.name,
            "type": self.value_type.to_dict(),
            "default": self.default,
        }

    @classmethod
    def from_dict(cls, value, location: str) -> "ParticleEventField":
        _exact_object(value, {"stable_id", "name", "type", "default"}, location)
        if type(value["stable_id"]) is not str or type(value["name"]) is not str:
            raise ParticleGraphSchemaError(f"{location} identity must be strings")
        return cls(
            value["stable_id"],
            value["name"],
            TypeRef.from_dict(value["type"]),
            value["default"],
        )


@dataclass(frozen=True)
class ParticleEventType:
    stable_id: str
    name: str
    queue_capacity: int = 8
    fields: tuple[ParticleEventField, ...] = ()

    def __post_init__(self) -> None:
        if type(self.stable_id) is not str or not self.stable_id:
            raise ParticleGraphSchemaError("particle event type stable_id cannot be empty")
        if type(self.name) is not str or not self.name:
            raise ParticleGraphSchemaError("particle event type name cannot be empty")
        if type(self.queue_capacity) is not int or not 1 <= self.queue_capacity <= 64:
            raise ParticleGraphSchemaError("particle event queue_capacity must be between 1 and 64")
        fields = tuple(self.fields)
        if not all(isinstance(value, ParticleEventField) for value in fields):
            raise ParticleGraphSchemaError("particle event fields are invalid")
        if len({value.stable_id for value in fields}) != len(fields):
            raise ParticleGraphSchemaError("particle event field stable ids must be unique")
        object.__setattr__(self, "fields", fields)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "name": self.name,
            "queue_capacity": self.queue_capacity,
            "fields": [value.to_dict() for value in self.fields],
        }

    @classmethod
    def from_dict(cls, value, location: str) -> "ParticleEventType":
        _exact_object(value, {"stable_id", "name", "queue_capacity", "fields"}, location)
        if type(value["fields"]) is not list:
            raise ParticleGraphSchemaError(f"{location}.fields must be an array")
        return cls(
            value["stable_id"],
            value["name"],
            value["queue_capacity"],
            tuple(
                ParticleEventField.from_dict(item, f"{location}.fields[{index}]")
                for index, item in enumerate(value["fields"])
            ),
        )


@dataclass(frozen=True)
class ParticleGraphAsset:
    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "New Particle Graph"
    emitters: tuple[ParticleEmitterAsset, ...] = field(default_factory=lambda: (ParticleEmitterAsset(),))
    parameters: tuple[ParticleParameter, ...] = ()
    event_types: tuple[ParticleEventType, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.stable_id) is not str
            or type(self.name) is not str
            or not self.stable_id
            or not self.name
        ):
            raise ParticleGraphSchemaError("particle graph requires stable_id and name")
        emitters = tuple(self.emitters)
        parameters = tuple(self.parameters)
        event_types = tuple(self.event_types)
        if not emitters:
            raise ParticleGraphSchemaError("particle graph requires at least one emitter")
        if not all(isinstance(value, ParticleEmitterAsset) for value in emitters):
            raise ParticleGraphSchemaError("particle graph emitters are invalid")
        if not all(isinstance(value, ParticleParameter) for value in parameters):
            raise ParticleGraphSchemaError("particle graph parameters are invalid")
        if not all(isinstance(value, ParticleEventType) for value in event_types):
            raise ParticleGraphSchemaError("particle graph event types are invalid")
        if len({emitter.stable_id for emitter in emitters}) != len(emitters):
            raise ParticleGraphSchemaError("particle emitter stable ids must be unique")
        if len({parameter.stable_id for parameter in parameters}) != len(parameters):
            raise ParticleGraphSchemaError("particle parameter stable ids must be unique")
        if len({parameter.name for parameter in parameters}) != len(parameters):
            raise ParticleGraphSchemaError("particle parameter names must be unique")
        if len({value.stable_id for value in event_types}) != len(event_types):
            raise ParticleGraphSchemaError("particle event type stable ids must be unique")
        event_type_ids = {value.stable_id for value in event_types}
        for emitter in emitters:
            unknown_event_flows = {
                value.event_id for value in emitter.event_flows if value.event_id
            } - event_type_ids
            if unknown_event_flows:
                raise ParticleGraphSchemaError(
                    f"particle emitter {emitter.stable_id!r} has flows for unknown events: "
                    f"{sorted(unknown_event_flows)}"
                )
        object.__setattr__(self, "emitters", emitters)
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "event_types", event_types)

    def to_dict(self) -> dict[str, Any]:
        return {
            "$schema": PARTICLE_GRAPH_SCHEMA,
            "stable_id": self.stable_id,
            "name": self.name,
            "emitters": [emitter.to_dict() for emitter in self.emitters],
            "parameters": [parameter.to_dict() for parameter in self.parameters],
            "event_types": [value.to_dict() for value in self.event_types],
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def save(self, path: str) -> None:
        if not path:
            raise ParticleGraphSchemaError("particle graph save path cannot be empty")
        from .artifact import ParticleArtifactRegistry

        ParticleArtifactRegistry.save_graph_asset(self, path)

    def semantic_hash(self) -> str:
        value = self.to_dict()
        for emitter_value, emitter_asset in zip(value["emitters"], self.emitters):
            emitter_value["stages"] = {
                stage: getattr(emitter_asset, stage).semantic_hash()
                for stage in ("init", "update", "rendering")
            }
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value) -> "ParticleGraphAsset":
        expected = {
            "$schema", "stable_id", "name", "emitters", "parameters", "event_types",
        }
        _exact_object(value, expected, "$")
        if value["$schema"] != PARTICLE_GRAPH_SCHEMA:
            raise ParticleGraphSchemaError("unsupported particle graph schema")
        if any(type(value[name]) is not list for name in ("emitters", "parameters", "event_types")):
            raise ParticleGraphSchemaError("particle graph collections must be arrays")
        return cls(
            stable_id=value["stable_id"],
            name=value["name"],
            emitters=tuple(
                ParticleEmitterAsset.from_dict(item, f"$.emitters[{index}]")
                for index, item in enumerate(value["emitters"])
            ),
            parameters=tuple(
                ParticleParameter.from_dict(item, f"$.parameters[{index}]")
                for index, item in enumerate(value["parameters"])
            ),
            event_types=tuple(
                ParticleEventType.from_dict(item, f"$.event_types[{index}]")
                for index, item in enumerate(value["event_types"])
            ),
        )

    @classmethod
    def from_json(cls, text: str | bytes) -> "ParticleGraphAsset":
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        return cls.from_dict(json.loads(text))

    @classmethod
    def load(cls, path: str) -> "ParticleGraphAsset":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


def _validate_stage_root(stage: str, document: GraphDocument) -> None:
    domain, root_uid, root_type = _ROOTS[stage]
    roots = [node for node in document.nodes if node.type_id.startswith("particle.root.")]
    mandatory = [node for node in roots if node.type_id == root_type]
    if document.domain != domain or len(mandatory) != 1:
        raise ParticleGraphSchemaError(f"{stage} stage requires exactly one mandatory root")
    root = mandatory[0]
    if root.uid != root_uid or root.type_id != root_type:
        raise ParticleGraphSchemaError(f"{stage} stage root identity is immutable")
    if len(roots) != 1:
        raise ParticleGraphSchemaError(
            f"{stage} stage must contain only its own lifecycle root"
        )


def _parse_stage_document(value: Any, location: str) -> GraphDocument:
    try:
        return GraphDocument.from_dict(value)
    except (GraphDocumentError, TypeError, ValueError) as exc:
        raise ParticleGraphSchemaError(f"{location}: {exc}") from exc


def _parse_optional_stage_document(
    value: Any, location: str
) -> GraphDocument | None:
    if value is None:
        return None
    return _parse_stage_document(value, location)


def _exact_object(value: Any, expected: set[str], location: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise ParticleGraphSchemaError(f"{location} must be an object")
    actual = set(value)
    if actual != expected:
        raise ParticleGraphSchemaError(
            f"{location} keys mismatch; missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )
    return value


__all__ = [
    "EmitterSettings",
    "EmitterShape",
    "EmitterShapeKind",
    "MeshEmissionMode",
    "SdfEmissionMode",
    "PARTICLE_GRAPH_SCHEMA",
    "ParticleAttribute",
    "ParticleBurst",
    "ParticleEmitterAsset",
    "ParticleEventField",
    "ParticleEventFlow",
    "ParticleEventType",
    "ParticleGraphAsset",
    "ParticleGraphSchemaError",
    "ParticleParameter",
    "ScalarRange",
    "SimulationSpace",
    "VectorField",
    "default_stage_graph",
    "default_event_graph",
    "optional_particle_attributes",
    "captured_particle_attributes",
    "particle_attribute_cache_id",
    "particle_attribute_capture_id",
    "particle_attribute_catalog",
    "particle_attribute_zero",
    "particle_cache_attributes",
    "standard_particle_attributes",
]
