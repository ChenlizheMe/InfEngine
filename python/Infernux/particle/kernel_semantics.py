"""Portable particle-kernel opcode and runtime semantics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Mapping, Sequence

from Infernux.graph.types import CoordinateSpace, TypeRef, ValueType
from Infernux.graph.ramp import Curve, Gradient


class KernelSemanticError(ValueError):
    pass


class KernelCapability(str, Enum):
    PORTABLE = "portable"
    TARGET_LIMITED = "target_limited"
    APPROXIMATE = "approximate"


class KernelStage(str, Enum):
    INIT = "init"
    UPDATE = "update"
    RENDERING = "rendering"


@dataclass(frozen=True)
class KernelOpcodeSpec:
    result_required: bool
    operand_count: int
    immediate_names: frozenset[str] = frozenset()
    stages: frozenset[KernelStage] = frozenset(KernelStage)
    capability: KernelCapability = KernelCapability.PORTABLE


_ALL_STAGES = frozenset(KernelStage)
_INIT_ONLY = frozenset({KernelStage.INIT})
_UPDATE_ONLY = frozenset({KernelStage.UPDATE})
_RENDER_ONLY = frozenset({KernelStage.RENDERING})
_SHAPE_IMMEDIATES = frozenset(
    {"shape", "shape_space", "radius", "angle_degrees", "dimensions", "random_slots"}
)

KERNEL_OPCODE_SPECS: Mapping[str, KernelOpcodeSpec] = {
    "constant": KernelOpcodeSpec(True, 0, frozenset({"value"})),
    "load_attribute": KernelOpcodeSpec(True, 0, frozenset({"attribute"})),
    "store_attribute": KernelOpcodeSpec(False, 1, frozenset({"attribute"})),
    "load_uniform": KernelOpcodeSpec(True, 0, frozenset({"name"})),
    "add": KernelOpcodeSpec(True, 2),
    "subtract": KernelOpcodeSpec(True, 2),
    "multiply": KernelOpcodeSpec(True, 2),
    "divide": KernelOpcodeSpec(True, 2),
    "lerp": KernelOpcodeSpec(True, 3),
    "normalize": KernelOpcodeSpec(True, 1),
    "random_f32": KernelOpcodeSpec(True, 3, frozenset({"random_slot"})),
    "sample_curve": KernelOpcodeSpec(True, 1, frozenset({"curve"})),
    "sample_gradient": KernelOpcodeSpec(True, 1, frozenset({"gradient"})),
    "sample_shape_position": KernelOpcodeSpec(True, 0, _SHAPE_IMMEDIATES, _INIT_ONLY),
    "sample_shape_direction": KernelOpcodeSpec(True, 0, _SHAPE_IMMEDIATES, _INIT_ONLY),
    "sample_point_cache": KernelOpcodeSpec(
        True,
        1,
        frozenset({"interface", "channel", "lookup", "semantic"}),
        _ALL_STAGES,
    ),
    "sample_vector_field": KernelOpcodeSpec(
        True,
        1,
        frozenset({"interface"}),
        _ALL_STAGES,
    ),
    "less_than": KernelOpcodeSpec(True, 2),
    "set_alive": KernelOpcodeSpec(False, 1, stages=_UPDATE_ONLY),
    "export_attribute": KernelOpcodeSpec(
        False, 1, frozenset({"attribute"}), _RENDER_ONLY
    ),
    "convert_space": KernelOpcodeSpec(
        True, 1, frozenset({"from", "to", "semantic"}), _ALL_STAGES
    ),
}

KERNEL_RUNTIME_UNIFORMS: Mapping[str, TypeRef] = {
    "delta_time": TypeRef(ValueType.F32),
}

RANDOM_ALGORITHM = "inx_hash32"
RANDOM_FLOAT_MAPPING = "high24_div_2pow24"
RANDOM_KEY_FIELDS = (
    "system_seed",
    "emitter_seed",
    "node_seed",
    "particle_id",
    "spawn_generation",
    "simulation_step",
    "random_slot",
)


@dataclass(frozen=True)
class KernelRuntimeContract:
    float_mode: str = "ieee754_f32"
    non_finite_policy: str = "kill_particle"
    normalize_zero_policy: str = "return_zero"
    lifecycle_order: tuple[str, ...] = ("spawn", "init", "update", "kill", "rendering")
    delta_time_policy: str = "finite_non_negative_f32"
    pause_policy: str = "no_spawn_no_update_no_step_increment"
    capacity_policy: str = "drop_newest"
    unwritten_attribute_policy: str = "schema_default"
    shape_sampling: str = "infernux_shape"
    random_algorithm: str = RANDOM_ALGORITHM
    random_float_mapping: str = RANDOM_FLOAT_MAPPING
    random_key_fields: tuple[str, ...] = RANDOM_KEY_FIELDS

    def __post_init__(self) -> None:
        if self.float_mode != "ieee754_f32":
            raise KernelSemanticError("unsupported particle kernel float mode")
        if self.non_finite_policy != "kill_particle":
            raise KernelSemanticError("unsupported particle non-finite policy")
        if self.normalize_zero_policy != "return_zero":
            raise KernelSemanticError("unsupported particle normalize-zero policy")
        if tuple(self.lifecycle_order) != ("spawn", "init", "update", "kill", "rendering"):
            raise KernelSemanticError("particle lifecycle order is part of the ABI")
        if self.delta_time_policy != "finite_non_negative_f32":
            raise KernelSemanticError("unsupported particle delta-time policy")
        if self.pause_policy != "no_spawn_no_update_no_step_increment":
            raise KernelSemanticError("unsupported particle pause policy")
        if self.capacity_policy != "drop_newest":
            raise KernelSemanticError("unsupported particle capacity policy")
        if self.unwritten_attribute_policy != "schema_default":
            raise KernelSemanticError("unsupported particle unwritten-attribute policy")
        if self.shape_sampling != "infernux_shape":
            raise KernelSemanticError("unsupported particle shape-sampling contract")
        if self.random_algorithm != RANDOM_ALGORITHM:
            raise KernelSemanticError("unsupported particle random algorithm")
        if self.random_float_mapping != RANDOM_FLOAT_MAPPING:
            raise KernelSemanticError("unsupported particle random float mapping")
        if tuple(self.random_key_fields) != RANDOM_KEY_FIELDS:
            raise KernelSemanticError("particle random key fields are part of the ABI")

    def to_dict(self) -> dict[str, Any]:
        return {
            "float_mode": self.float_mode,
            "non_finite_policy": self.non_finite_policy,
            "normalize_zero_policy": self.normalize_zero_policy,
            "lifecycle_order": list(self.lifecycle_order),
            "delta_time_policy": self.delta_time_policy,
            "pause_policy": self.pause_policy,
            "capacity_policy": self.capacity_policy,
            "unwritten_attribute_policy": self.unwritten_attribute_policy,
            "shape_sampling": self.shape_sampling,
            "random_algorithm": self.random_algorithm,
            "random_float_mapping": self.random_float_mapping,
            "random_key_fields": list(self.random_key_fields),
            "uniforms": {
                name: value_type.to_dict()
                for name, value_type in sorted(KERNEL_RUNTIME_UNIFORMS.items())
            },
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelRuntimeContract":
        expected = {
            "float_mode",
            "non_finite_policy",
            "normalize_zero_policy",
            "lifecycle_order",
            "delta_time_policy",
            "pause_policy",
            "capacity_policy",
            "unwritten_attribute_policy",
            "shape_sampling",
            "random_algorithm",
            "random_float_mapping",
            "random_key_fields",
            "uniforms",
        }
        if type(value) is not dict or set(value) != expected:
            raise KernelSemanticError("particle kernel runtime contract shape is invalid")
        if value["uniforms"] != {
            name: value_type.to_dict()
            for name, value_type in sorted(KERNEL_RUNTIME_UNIFORMS.items())
        }:
            raise KernelSemanticError("particle kernel uniform ABI does not match this runtime")
        if (
            type(value["random_key_fields"]) is not list
            or type(value["lifecycle_order"]) is not list
        ):
            raise KernelSemanticError("particle lifecycle and random key fields must be arrays")
        return cls(
            value["float_mode"],
            value["non_finite_policy"],
            value["normalize_zero_policy"],
            tuple(value["lifecycle_order"]),
            value["delta_time_policy"],
            value["pause_policy"],
            value["capacity_policy"],
            value["unwritten_attribute_policy"],
            value["shape_sampling"],
            value["random_algorithm"],
            value["random_float_mapping"],
            tuple(value["random_key_fields"]),
        )


def validate_instruction_semantics(
    opcode: str,
    result_type: TypeRef | None,
    operand_types: Sequence[TypeRef],
    immediates: Mapping[str, Any],
    capability: KernelCapability,
    *,
    stage: KernelStage | None = None,
) -> None:
    spec = KERNEL_OPCODE_SPECS.get(opcode)
    if spec is None:
        raise KernelSemanticError(f"unknown particle kernel opcode {opcode!r}")
    if (result_type is not None) != spec.result_required:
        mode = "requires" if spec.result_required else "must not have"
        raise KernelSemanticError(f"kernel opcode {opcode!r} {mode} a result")
    if len(operand_types) != spec.operand_count:
        raise KernelSemanticError(
            f"kernel opcode {opcode!r} requires {spec.operand_count} operands"
        )
    if set(immediates) != set(spec.immediate_names):
        raise KernelSemanticError(
            f"kernel opcode {opcode!r} immediates must be {sorted(spec.immediate_names)}"
        )
    if KernelCapability(capability) is not spec.capability:
        raise KernelSemanticError(
            f"kernel opcode {opcode!r} capability must be {spec.capability.value}"
        )
    if stage is not None and KernelStage(stage) not in spec.stages:
        raise KernelSemanticError(
            f"kernel opcode {opcode!r} is not valid in the {KernelStage(stage).value} stage"
        )
    _validate_opcode_types(opcode, result_type, tuple(operand_types), immediates)


def _validate_opcode_types(
    opcode: str,
    result_type: TypeRef | None,
    operands: tuple[TypeRef, ...],
    immediates: Mapping[str, Any],
) -> None:
    f32 = TypeRef(ValueType.F32)
    bool_type = TypeRef(ValueType.BOOL)
    if opcode == "constant":
        _validate_literal(result_type, immediates["value"])
    elif opcode == "load_uniform":
        expected = KERNEL_RUNTIME_UNIFORMS.get(immediates["name"])
        if expected is None or result_type != expected:
            raise KernelSemanticError("kernel uniform name or result type is invalid")
    elif opcode in {"add", "subtract", "divide"}:
        if result_type is None or operands != (result_type, result_type):
            raise KernelSemanticError(
                f"kernel {opcode} requires two operands matching its result"
            )
    elif opcode == "multiply":
        if result_type is None or not (
            operands == (result_type, result_type)
            or operands == (result_type, f32)
            or operands == (f32, result_type)
        ):
            raise KernelSemanticError(
                "kernel multiply requires matching operands or one f32 scalar"
            )
    elif opcode == "lerp":
        if result_type is None or operands != (result_type, result_type, f32):
            raise KernelSemanticError(
                "kernel lerp requires two operands matching its result and one f32 factor"
            )
    elif opcode == "normalize":
        if result_type is None or operands != (result_type,) or result_type.value_type not in {
            ValueType.VEC2,
            ValueType.VEC3,
            ValueType.VEC4,
        }:
            raise KernelSemanticError("kernel normalize requires one matching vector operand")
    elif opcode == "random_f32":
        if result_type != f32 or operands != (f32, f32, TypeRef(ValueType.U32)):
            raise KernelSemanticError(
                "kernel random_f32 requires f32 bounds, a u32 node seed, and an f32 result"
            )
        _validate_u32(immediates["random_slot"], "random_slot")
    elif opcode == "sample_curve":
        if result_type != f32 or operands != (f32,):
            raise KernelSemanticError("curve sampling requires one f32 input and an f32 result")
        try:
            Curve.from_dict(immediates["curve"])
        except (TypeError, ValueError) as exc:
            raise KernelSemanticError(f"invalid curve literal: {exc}") from exc
    elif opcode == "sample_gradient":
        if result_type != TypeRef(ValueType.COLOR) or operands != (f32,):
            raise KernelSemanticError(
                "gradient sampling requires one f32 input and a color result"
            )
        try:
            Gradient.from_dict(immediates["gradient"])
        except (TypeError, ValueError) as exc:
            raise KernelSemanticError(f"invalid gradient literal: {exc}") from exc
    elif opcode.startswith("sample_shape_"):
        if result_type is None or result_type.value_type is not ValueType.VEC3:
            raise KernelSemanticError("kernel shape sampling requires a vec3 result")
        try:
            shape_space = CoordinateSpace(immediates["shape_space"])
        except ValueError as exc:
            raise KernelSemanticError("kernel shape space is invalid") from exc
        if shape_space not in {CoordinateSpace.EMITTER_LOCAL, CoordinateSpace.WORLD}:
            raise KernelSemanticError("kernel shape space must be emitter_local or world")
        if result_type.space is not shape_space:
            raise KernelSemanticError("kernel shape result must retain its authored space")
        if immediates["shape"] not in {"point", "sphere", "box", "cone"}:
            raise KernelSemanticError("kernel shape kind is invalid")
        _validate_non_negative(immediates["radius"], "shape radius")
        angle = _finite_number(immediates["angle_degrees"], "shape angle")
        if not 0.0 <= angle <= 180.0:
            raise KernelSemanticError("kernel shape angle must be between 0 and 180")
        dimensions = immediates["dimensions"]
        if not isinstance(dimensions, (list, tuple)) or len(dimensions) != 3:
            raise KernelSemanticError("kernel shape dimensions require three values")
        for value in dimensions:
            _validate_non_negative(value, "shape dimension")
        random_slots = immediates["random_slots"]
        if not isinstance(random_slots, (list, tuple)) or len(random_slots) != 3:
            raise KernelSemanticError("kernel shape sampling requires three random slots")
        for random_slot in random_slots:
            _validate_u32(random_slot, "random_slot")
        if len(set(random_slots)) != len(random_slots):
            raise KernelSemanticError("kernel shape sampling random slots must be unique")
    elif opcode == "sample_point_cache":
        if operands != (TypeRef(ValueType.U32),):
            raise KernelSemanticError("point cache sampling requires one u32 index or stable ID")
        if result_type is None or result_type.value_type not in {
            ValueType.F32,
            ValueType.U32,
            ValueType.VEC2,
            ValueType.VEC3,
            ValueType.VEC4,
            ValueType.COLOR,
        }:
            raise KernelSemanticError("point cache sampling has an unsupported result type")
        if any(
            type(immediates[name]) is not str or not immediates[name].strip()
            for name in ("interface", "channel")
        ):
            raise KernelSemanticError("point cache interface and channel names cannot be empty")
        if immediates["lookup"] not in {"index", "stable_id"}:
            raise KernelSemanticError("point cache lookup must use index or stable_id")
        if immediates["semantic"] not in {
            "raw",
            "position",
            "direction",
            "vector",
            "normal",
        }:
            raise KernelSemanticError("point cache sample semantic is invalid")
        if immediates["semantic"] == "raw":
            if result_type.space is not CoordinateSpace.NONE:
                raise KernelSemanticError("raw point cache samples cannot carry a coordinate space")
        elif result_type != TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION):
            raise KernelSemanticError(
                "transformed point cache samples must produce a simulation-space vec3"
            )
    elif opcode == "sample_vector_field":
        simulation_vector = TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION)
        if operands != (simulation_vector,) or result_type != simulation_vector:
            raise KernelSemanticError(
                "vector field sampling requires and produces a simulation-space vec3"
            )
        if type(immediates["interface"]) is not str or not immediates["interface"].strip():
            raise KernelSemanticError("vector field interface cannot be empty")
    elif opcode == "less_than":
        if result_type != bool_type or operands[0] != operands[1] or operands[0].value_type not in {
            ValueType.I32,
            ValueType.U32,
            ValueType.F32,
        }:
            raise KernelSemanticError("kernel less_than requires matching scalar operands")
    elif opcode == "set_alive":
        if operands != (bool_type,):
            raise KernelSemanticError("kernel set_alive requires one bool operand")
    elif opcode == "convert_space":
        if result_type is None or operands[0].value_type != result_type.value_type:
            raise KernelSemanticError("kernel space conversion must preserve value type")
        if operands[0].space.value != immediates["from"] or result_type.space.value != immediates["to"]:
            raise KernelSemanticError("kernel space conversion metadata does not match its types")
        if operands[0].space is result_type.space:
            raise KernelSemanticError("kernel space conversion must change coordinate space")
        if immediates["semantic"] not in {"position", "direction", "vector"}:
            raise KernelSemanticError("kernel space conversion semantic is invalid")


def _validate_literal(value_type: TypeRef | None, value: Any) -> None:
    if value_type is None:
        raise KernelSemanticError("kernel constant requires a result type")
    kind = value_type.value_type
    if kind is ValueType.BOOL:
        valid = type(value) is bool
    elif kind in {ValueType.I32, ValueType.U32}:
        valid = type(value) is int and (kind is not ValueType.U32 or value >= 0)
    elif kind is ValueType.F32:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))
    else:
        count = {
            ValueType.VEC2: 2,
            ValueType.VEC3: 3,
            ValueType.VEC4: 4,
            ValueType.COLOR: 4,
            ValueType.MAT3: 9,
            ValueType.MAT4: 16,
        }.get(kind)
        valid = count is not None and isinstance(value, (list, tuple)) and len(value) == count and all(
            not isinstance(item, bool)
            and isinstance(item, (int, float))
            and math.isfinite(float(item))
            for item in value
        )
    if not valid:
        raise KernelSemanticError(f"kernel constant does not match {value_type}")


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise KernelSemanticError(f"kernel {label} must be finite")
    return float(value)


def _validate_non_negative(value: Any, label: str) -> None:
    if _finite_number(value, label) < 0.0:
        raise KernelSemanticError(f"kernel {label} must be non-negative")


def _validate_u32(value: Any, label: str) -> None:
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise KernelSemanticError(f"kernel {label} must be an unsigned 32-bit integer")


def particle_random_u32(
    system_seed: int,
    emitter_seed: int,
    node_seed: int,
    particle_id: int,
    spawn_generation: int,
    simulation_step: int,
    random_slot: int,
) -> int:
    """Reference implementation of the exact integer RNG shared by all backends."""

    values = (
        system_seed,
        emitter_seed,
        node_seed,
        particle_id,
        spawn_generation,
        simulation_step,
        random_slot,
    )
    state = 0x811C9DC5
    for value in values:
        _validate_u32(value, "random key field")
        state ^= value
        state = (state * 0x01000193) & 0xFFFFFFFF
        state ^= state >> 16
    state ^= state >> 16
    state = (state * 0x7FEB352D) & 0xFFFFFFFF
    state ^= state >> 15
    state = (state * 0x846CA68B) & 0xFFFFFFFF
    state ^= state >> 16
    return state & 0xFFFFFFFF


def particle_random_f32(*key: int) -> float:
    """Map the high 24 random bits to the portable half-open range [0, 1)."""

    return float(particle_random_u32(*key) >> 8) * (1.0 / 16777216.0)


__all__ = [
    "KERNEL_OPCODE_SPECS",
    "KERNEL_RUNTIME_UNIFORMS",
    "KernelCapability",
    "KernelOpcodeSpec",
    "KernelRuntimeContract",
    "KernelSemanticError",
    "KernelStage",
    "RANDOM_ALGORITHM",
    "RANDOM_FLOAT_MAPPING",
    "RANDOM_KEY_FIELDS",
    "particle_random_f32",
    "particle_random_u32",
    "validate_instruction_semantics",
]
