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
from Infernux.graph.types import CoordinateSpace, TypeRef, ValueType

from . import nodes as _particle_nodes  # noqa: F401
from .data_interface import (
    ParticleDataInterface,
    PointCache,
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


class ExecutionTarget(str, Enum):
    AUTO = "auto"
    CPU = "cpu"
    GPU = "gpu"


class EmitterShapeKind(str, Enum):
    POINT = "point"
    SPHERE = "sphere"
    BOX = "box"
    CONE = "cone"


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

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.time)) or float(self.time) < 0.0:
            raise ParticleGraphSchemaError("burst time must be finite and non-negative")
        if type(self.count) is not int or self.count < 0:
            raise ParticleGraphSchemaError("burst count must be a non-negative integer")
        if type(self.cycles) is not int or self.cycles <= 0:
            raise ParticleGraphSchemaError("burst cycles must be a positive integer")
        if not math.isfinite(float(self.interval)) or float(self.interval) < 0.0:
            raise ParticleGraphSchemaError("burst interval must be finite and non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": float(self.time),
            "count": self.count,
            "cycles": self.cycles,
            "interval": float(self.interval),
        }

    @classmethod
    def from_dict(cls, value, location: str) -> "ParticleBurst":
        _exact_object(value, {"time", "count", "cycles", "interval"}, location)
        return cls(value["time"], value["count"], value["cycles"], value["interval"])


@dataclass(frozen=True)
class EmitterShape:
    kind: EmitterShapeKind = EmitterShapeKind.POINT
    space: CoordinateSpace = CoordinateSpace.EMITTER_LOCAL
    radius: float = 0.0
    angle_degrees: float = 25.0
    dimensions: tuple[float, float, float] = (1.0, 1.0, 1.0)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EmitterShapeKind(self.kind))
        object.__setattr__(self, "space", CoordinateSpace(self.space))
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "space": self.space.value,
            "radius": float(self.radius),
            "angle_degrees": float(self.angle_degrees),
            "dimensions": list(self.dimensions),
        }

    @classmethod
    def from_dict(cls, value, location: str) -> "EmitterShape":
        _exact_object(value, {"kind", "space", "radius", "angle_degrees", "dimensions"}, location)
        if type(value["dimensions"]) is not list:
            raise ParticleGraphSchemaError(f"{location}.dimensions must be an array")
        return cls(
            value["kind"],
            value["space"],
            value["radius"],
            value["angle_degrees"],
            tuple(value["dimensions"]),
        )


@dataclass(frozen=True)
class EmitterSettings:
    capacity: int = 1000
    target: ExecutionTarget = ExecutionTarget.AUTO
    simulation_space: SimulationSpace = SimulationSpace.WORLD
    seed: int = 1
    spawn_rate: float = 10.0
    bursts: tuple[ParticleBurst, ...] = ()
    lifetime: ScalarRange = ScalarRange(5.0, 5.0)
    initial_speed: ScalarRange = ScalarRange(1.0, 1.0)
    gravity: tuple[float, float, float] = (0.0, -9.81, 0.0)
    shape: EmitterShape = EmitterShape()

    def __post_init__(self) -> None:
        if type(self.capacity) is not int or self.capacity <= 0:
            raise ParticleGraphSchemaError("emitter capacity must be a positive integer")
        object.__setattr__(self, "target", ExecutionTarget(self.target))
        object.__setattr__(self, "simulation_space", SimulationSpace(self.simulation_space))
        if type(self.seed) is not int or not 0 <= self.seed <= 0xFFFFFFFF:
            raise ParticleGraphSchemaError("emitter seed must be an unsigned 32-bit integer")
        if not math.isfinite(float(self.spawn_rate)) or float(self.spawn_rate) < 0.0:
            raise ParticleGraphSchemaError("spawn rate must be finite and non-negative")
        if len(self.gravity) != 3 or any(not math.isfinite(float(value)) for value in self.gravity):
            raise ParticleGraphSchemaError("gravity requires three finite values")
        object.__setattr__(self, "gravity", tuple(float(value) for value in self.gravity))
        bursts = tuple(self.bursts)
        if not all(isinstance(value, ParticleBurst) for value in bursts):
            raise ParticleGraphSchemaError("emitter bursts must contain ParticleBurst values")
        if not isinstance(self.lifetime, ScalarRange) or not isinstance(self.initial_speed, ScalarRange):
            raise ParticleGraphSchemaError("emitter lifetime and initial_speed must be ScalarRange values")
        if not isinstance(self.shape, EmitterShape):
            raise ParticleGraphSchemaError("emitter shape must be an EmitterShape")
        object.__setattr__(self, "bursts", bursts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "target": self.target.value,
            "simulation_space": self.simulation_space.value,
            "seed": self.seed,
            "spawn_rate": float(self.spawn_rate),
            "bursts": [burst.to_dict() for burst in self.bursts],
            "lifetime": self.lifetime.to_dict(),
            "initial_speed": self.initial_speed.to_dict(),
            "gravity": list(self.gravity),
            "shape": self.shape.to_dict(),
        }

    @classmethod
    def from_dict(cls, value, location: str) -> "EmitterSettings":
        expected = {
            "capacity", "target", "simulation_space", "seed", "spawn_rate", "bursts",
            "lifetime", "initial_speed", "gravity", "shape",
        }
        _exact_object(value, expected, location)
        if type(value["bursts"]) is not list or type(value["gravity"]) is not list:
            raise ParticleGraphSchemaError(f"{location}.bursts and gravity must be arrays")
        return cls(
            capacity=value["capacity"],
            target=value["target"],
            simulation_space=value["simulation_space"],
            seed=value["seed"],
            spawn_rate=value["spawn_rate"],
            bursts=tuple(
                ParticleBurst.from_dict(item, f"{location}.bursts[{index}]")
                for index, item in enumerate(value["bursts"])
            ),
            lifetime=ScalarRange.from_dict(value["lifetime"], f"{location}.lifetime"),
            initial_speed=ScalarRange.from_dict(value["initial_speed"], f"{location}.initial_speed"),
            gravity=tuple(value["gravity"]),
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
                f"particle attribute {self.name!r} must use a numeric storage type"
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


_ROOTS = {
    "init": ("particle.init", "root.init", "particle.root.init"),
    "update": ("particle.update", "root.update", "particle.root.update"),
    "rendering": ("particle.rendering", "root.rendering", "particle.root.rendering"),
}


def default_stage_graph(stage: str) -> GraphDocument:
    domain, root_uid, root_type = _ROOTS[stage]
    nodes = [GraphNodeRecord(root_uid, root_type, (0.0, 0.0))]
    links = []
    if stage == "rendering":
        nodes.append(GraphNodeRecord("output.sprite", "particle.output.sprite", (280.0, 0.0)))
        links.append(
            GraphLinkRecord(
                "root-to-sprite",
                root_uid,
                "out",
                "output.sprite",
                "in",
                PortKind.STREAM,
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


@dataclass(frozen=True)
class ParticleEmitterAsset:
    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Emitter"
    settings: EmitterSettings = EmitterSettings()
    attributes: tuple[ParticleAttribute, ...] = field(default_factory=standard_particle_attributes)
    data_interfaces: tuple[ParticleDataInterface, ...] = ()
    init: GraphDocument = field(default_factory=lambda: default_stage_graph("init"))
    update: GraphDocument = field(default_factory=lambda: default_stage_graph("update"))
    rendering: GraphDocument = field(default_factory=lambda: default_stage_graph("rendering"))

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
        if not all(isinstance(value, GraphDocument) for value in (self.init, self.update, self.rendering)):
            raise ParticleGraphSchemaError("particle emitter stages must be GraphDocument values")
        attributes = tuple(self.attributes)
        data_interfaces = tuple(self.data_interfaces)
        if not all(isinstance(value, ParticleAttribute) for value in attributes):
            raise ParticleGraphSchemaError("particle emitter attributes are invalid")
        if not all(isinstance(value, (VectorField, PointCache)) for value in data_interfaces):
            raise ParticleGraphSchemaError("particle emitter data interfaces are invalid")
        object.__setattr__(self, "attributes", attributes)
        object.__setattr__(self, "data_interfaces", data_interfaces)
        _validate_stage_root("init", self.init)
        _validate_stage_root("update", self.update)
        _validate_stage_root("rendering", self.rendering)
        if len({attribute.stable_id for attribute in self.attributes}) != len(self.attributes):
            raise ParticleGraphSchemaError("particle attribute stable ids must be unique")
        builtins = tuple(
            attribute for attribute in self.attributes if attribute.stable_id.startswith("builtin.")
        )
        standards = standard_particle_attributes()
        if (
            len(builtins) != len(standards)
            or tuple(self.attributes[: len(standards)]) != builtins
            or any(
                (actual.stable_id, actual.name, actual.value_type)
                != (expected.stable_id, expected.name, expected.value_type)
                for actual, expected in zip(builtins, standards)
            )
        ):
            raise ParticleGraphSchemaError(
                "particle emitter must use the complete current builtin attribute set"
            )
        if len({interface.stable_id for interface in data_interfaces}) != len(data_interfaces):
            raise ParticleGraphSchemaError("particle data-interface stable ids must be unique")

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
            "custom_attributes": [
                attribute.to_dict()
                for attribute in self.attributes
                if not attribute.stable_id.startswith("builtin.")
            ],
            "data_interfaces": [interface.to_dict() for interface in self.data_interfaces],
            "stages": {
                "init": self.init.to_dict(),
                "update": self.update.to_dict(),
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
                "custom_attributes",
                "data_interfaces",
                "stages",
            },
            location,
        )
        _exact_object(value["stages"], {"init", "update", "rendering"}, f"{location}.stages")
        if (
            type(value["attribute_defaults"]) is not dict
            or type(value["custom_attributes"]) is not list
            or type(value["data_interfaces"]) is not list
        ):
            raise ParticleGraphSchemaError(
                f"{location}.attribute_defaults must be an object; custom_attributes and data_interfaces must be arrays"
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
            attributes=(
                *builtin_attributes,
                *(
                    ParticleAttribute.from_dict(
                        item, f"{location}.custom_attributes[{index}]"
                    )
                    for index, item in enumerate(value["custom_attributes"])
                ),
            ),
            data_interfaces=tuple(
                particle_data_interface_from_dict(
                    item, f"{location}.data_interfaces[{index}]"
                )
                for index, item in enumerate(value["data_interfaces"])
            ),
            init=_parse_stage_document(value["stages"]["init"], f"{location}.stages.init"),
            update=_parse_stage_document(value["stages"]["update"], f"{location}.stages.update"),
            rendering=_parse_stage_document(
                value["stages"]["rendering"],
                f"{location}.stages.rendering",
            ),
        )


@dataclass(frozen=True)
class ParticleGraphAsset:
    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "New Particle Graph"
    emitters: tuple[ParticleEmitterAsset, ...] = field(default_factory=lambda: (ParticleEmitterAsset(),))
    parameters: tuple[ParticleAttribute, ...] = ()

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
        if not emitters:
            raise ParticleGraphSchemaError("particle graph requires at least one emitter")
        if not all(isinstance(value, ParticleEmitterAsset) for value in emitters):
            raise ParticleGraphSchemaError("particle graph emitters are invalid")
        if not all(isinstance(value, ParticleAttribute) for value in parameters):
            raise ParticleGraphSchemaError("particle graph parameters are invalid")
        if len({emitter.stable_id for emitter in emitters}) != len(emitters):
            raise ParticleGraphSchemaError("particle emitter stable ids must be unique")
        if len({parameter.stable_id for parameter in parameters}) != len(parameters):
            raise ParticleGraphSchemaError("particle parameter stable ids must be unique")
        object.__setattr__(self, "emitters", emitters)
        object.__setattr__(self, "parameters", parameters)

    def to_dict(self) -> dict[str, Any]:
        return {
            "$schema": PARTICLE_GRAPH_SCHEMA,
            "stable_id": self.stable_id,
            "name": self.name,
            "emitters": [emitter.to_dict() for emitter in self.emitters],
            "parameters": [parameter.to_dict() for parameter in self.parameters],
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def save(self, path: str) -> None:
        if not path:
            raise ParticleGraphSchemaError("particle graph save path cannot be empty")
        from Infernux.core.document_store import write_document_text
        from .artifact import ParticleArtifactRegistry

        write_document_text(
            path,
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        ParticleArtifactRegistry.compile_path(path)

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
        expected = {"$schema", "stable_id", "name", "emitters", "parameters"}
        _exact_object(value, expected, "$")
        if value["$schema"] != PARTICLE_GRAPH_SCHEMA:
            raise ParticleGraphSchemaError("unsupported particle graph schema")
        if type(value["emitters"]) is not list or type(value["parameters"]) is not list:
            raise ParticleGraphSchemaError("emitters and parameters must be arrays")
        return cls(
            stable_id=value["stable_id"],
            name=value["name"],
            emitters=tuple(
                ParticleEmitterAsset.from_dict(item, f"$.emitters[{index}]")
                for index, item in enumerate(value["emitters"])
            ),
            parameters=tuple(
                ParticleAttribute.from_dict(item, f"$.parameters[{index}]")
                for index, item in enumerate(value["parameters"])
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
    if document.domain != domain or len(roots) != 1:
        raise ParticleGraphSchemaError(f"{stage} stage requires exactly one mandatory root")
    root = roots[0]
    if root.uid != root_uid or root.type_id != root_type:
        raise ParticleGraphSchemaError(f"{stage} stage root identity is immutable")


def _parse_stage_document(value: Any, location: str) -> GraphDocument:
    try:
        return GraphDocument.from_dict(value)
    except (GraphDocumentError, TypeError, ValueError) as exc:
        raise ParticleGraphSchemaError(f"{location}: {exc}") from exc


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
    "ExecutionTarget",
    "PARTICLE_GRAPH_SCHEMA",
    "ParticleAttribute",
    "ParticleBurst",
    "ParticleEmitterAsset",
    "ParticleGraphAsset",
    "ParticleGraphSchemaError",
    "PointCache",
    "ScalarRange",
    "SimulationSpace",
    "VectorField",
    "default_stage_graph",
    "standard_particle_attributes",
]
