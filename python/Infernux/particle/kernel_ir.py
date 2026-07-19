"""Portable typed SSA IR shared by particle execution backends."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from Infernux.graph.types import CoordinateSpace, TypeRef, ValueType

from .hir import ParticleEmitterHIR, ParticleProgramHIR, ParticleStage, ParticleStageHIR
from .data_interface import (
    ParticleDataInterface,
    PointCache,
    VectorField,
    particle_data_interface_from_dict,
)
from .kernel_semantics import (
    KernelCapability,
    KernelRuntimeContract,
    KernelSemanticError,
    KernelStage,
    validate_instruction_semantics,
)


KERNEL_IR_SCHEMA = "infernux.particle_kernel_ir"


class KernelCompileError(ValueError):
    pass


@dataclass(frozen=True)
class KernelSourceRef:
    node_uid: str = ""
    port_id: str = ""
    operation: str = ""

    def __post_init__(self) -> None:
        if not all(type(value) is str for value in (self.node_uid, self.port_id, self.operation)):
            raise KernelCompileError("kernel source fields must be strings")

    def to_dict(self) -> dict[str, str]:
        return {
            "node_uid": self.node_uid,
            "port_id": self.port_id,
            "operation": self.operation,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelSourceRef":
        _exact_dict(value, {"node_uid", "port_id", "operation"}, "kernel source")
        if not all(type(value[name]) is str for name in value):
            raise KernelCompileError("kernel source fields must be strings")
        return cls(value["node_uid"], value["port_id"], value["operation"])


@dataclass(frozen=True)
class KernelOperand:
    value_type: TypeRef
    value_id: str = ""
    literal: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.value_type, TypeRef):
            raise KernelCompileError("kernel operand requires a TypeRef")
        if type(self.value_id) is not str:
            raise KernelCompileError("kernel operand value_id must be a string")
        if bool(self.value_id) == (self.literal is not None):
            raise KernelCompileError("kernel operand requires exactly one value_id or literal")
        if not self.value_id:
            _finite_json(self.literal)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.value_type.to_dict(),
            "value_id": self.value_id,
            "literal": self.literal,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelOperand":
        _exact_dict(value, {"type", "value_id", "literal"}, "kernel operand")
        try:
            value_type = TypeRef.from_dict(value["type"])
        except (TypeError, ValueError) as exc:
            raise KernelCompileError("kernel operand type is invalid") from exc
        if type(value["value_id"]) is not str:
            raise KernelCompileError("kernel operand value_id must be a string")
        return cls(value_type, value["value_id"], value["literal"])


@dataclass(frozen=True)
class KernelInstruction:
    opcode: str
    result_id: str = ""
    result_type: TypeRef | None = None
    operands: tuple[KernelOperand, ...] = ()
    immediates: tuple[tuple[str, Any], ...] = ()
    capability: KernelCapability = KernelCapability.PORTABLE
    source: KernelSourceRef = KernelSourceRef()

    def __post_init__(self) -> None:
        if type(self.opcode) is not str or not self.opcode:
            raise KernelCompileError("kernel instruction opcode cannot be empty")
        if type(self.result_id) is not str:
            raise KernelCompileError("kernel instruction result_id must be a string")
        if bool(self.result_id) != (self.result_type is not None):
            raise KernelCompileError("kernel instruction result id and type must appear together")
        if self.result_type is not None and not isinstance(self.result_type, TypeRef):
            raise KernelCompileError("kernel instruction result type is invalid")
        if not all(isinstance(item, KernelOperand) for item in self.operands):
            raise KernelCompileError("kernel instruction operands are invalid")
        object.__setattr__(self, "capability", KernelCapability(self.capability))
        if not isinstance(self.source, KernelSourceRef):
            raise KernelCompileError("kernel instruction source map is invalid")
        names = [name for name, _value in self.immediates]
        if len(names) != len(set(names)) or any(type(name) is not str or not name for name in names):
            raise KernelCompileError("kernel instruction immediates require unique names")
        for _name, value in self.immediates:
            _finite_json(value)
        try:
            validate_instruction_semantics(
                self.opcode,
                self.result_type,
                tuple(operand.value_type for operand in self.operands),
                self.immediate_dict(),
                self.capability,
            )
        except KernelSemanticError as exc:
            raise KernelCompileError(str(exc)) from exc

    def immediate_dict(self) -> dict[str, Any]:
        return dict(self.immediates)

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        result = {
            "opcode": self.opcode,
            "result_id": self.result_id,
            "result_type": self.result_type.to_dict() if self.result_type is not None else None,
            "operands": [operand.to_dict() for operand in self.operands],
            "immediates": [[name, value] for name, value in self.immediates],
            "capability": self.capability.value,
        }
        if include_source:
            result["source"] = self.source.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: Any) -> "KernelInstruction":
        _exact_dict(
            value,
            {
                "opcode",
                "result_id",
                "result_type",
                "operands",
                "immediates",
                "capability",
                "source",
            },
            "kernel instruction",
        )
        if type(value["operands"]) is not list or type(value["immediates"]) is not list:
            raise KernelCompileError("kernel instruction operands and immediates must be arrays")
        result_type = None
        if value["result_type"] is not None:
            try:
                result_type = TypeRef.from_dict(value["result_type"])
            except (TypeError, ValueError) as exc:
                raise KernelCompileError("kernel instruction result type is invalid") from exc
        immediates = []
        for item in value["immediates"]:
            if type(item) is not list or len(item) != 2 or type(item[0]) is not str:
                raise KernelCompileError("kernel instruction immediate entries are invalid")
            immediates.append((item[0], item[1]))
        return cls(
            value["opcode"],
            value["result_id"],
            result_type,
            tuple(KernelOperand.from_dict(item) for item in value["operands"]),
            tuple(immediates),
            value["capability"],
            KernelSourceRef.from_dict(value["source"]),
        )


@dataclass(frozen=True)
class ParticleKernelFunction:
    stage: KernelStage
    instructions: tuple[KernelInstruction, ...]
    read_attributes: tuple[str, ...]
    written_attributes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", KernelStage(self.stage))
        if not all(isinstance(item, KernelInstruction) for item in self.instructions):
            raise KernelCompileError("kernel function instructions are invalid")
        if not all(type(item) is str for item in self.read_attributes + self.written_attributes):
            raise KernelCompileError("kernel function attribute summaries must contain strings")
        defined: set[str] = set()
        for instruction in self.instructions:
            try:
                validate_instruction_semantics(
                    instruction.opcode,
                    instruction.result_type,
                    tuple(operand.value_type for operand in instruction.operands),
                    instruction.immediate_dict(),
                    instruction.capability,
                    stage=self.stage,
                )
            except KernelSemanticError as exc:
                raise KernelCompileError(str(exc)) from exc
            for operand in instruction.operands:
                if operand.value_id and operand.value_id not in defined:
                    raise KernelCompileError(
                        f"kernel instruction {instruction.opcode!r} reads undefined SSA value {operand.value_id!r}"
                    )
            if instruction.result_id:
                if instruction.result_id in defined:
                    raise KernelCompileError(f"duplicate SSA result {instruction.result_id!r}")
                defined.add(instruction.result_id)
        object.__setattr__(self, "read_attributes", tuple(sorted(set(self.read_attributes))))
        object.__setattr__(self, "written_attributes", tuple(sorted(set(self.written_attributes))))

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "instructions": [
                instruction.to_dict(include_source=include_source)
                for instruction in self.instructions
            ],
            "read_attributes": list(self.read_attributes),
            "written_attributes": list(self.written_attributes),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ParticleKernelFunction":
        _exact_dict(
            value,
            {"stage", "instructions", "read_attributes", "written_attributes"},
            "kernel function",
        )
        for name in ("instructions", "read_attributes", "written_attributes"):
            if type(value[name]) is not list:
                raise KernelCompileError(f"kernel function {name} must be an array")
        if not all(type(item) is str for item in value["read_attributes"] + value["written_attributes"]):
            raise KernelCompileError("kernel function attribute summaries must contain strings")
        return cls(
            value["stage"],
            tuple(KernelInstruction.from_dict(item) for item in value["instructions"]),
            tuple(value["read_attributes"]),
            tuple(value["written_attributes"]),
        )


@dataclass(frozen=True)
class ParticleEmitterKernelIR:
    stable_id: str
    random_seed: int
    attributes: tuple[tuple[str, TypeRef, Any], ...]
    init: ParticleKernelFunction
    update: ParticleKernelFunction
    rendering: ParticleKernelFunction
    data_interfaces: tuple[ParticleDataInterface, ...] = ()

    def __post_init__(self) -> None:
        interfaces = tuple(self.data_interfaces)
        if type(self.stable_id) is not str or not self.stable_id:
            raise KernelCompileError("kernel emitter stable_id cannot be empty")
        if type(self.random_seed) is not int or not 0 <= self.random_seed <= 0xFFFFFFFF:
            raise KernelCompileError("kernel emitter random_seed must be an unsigned 32-bit integer")
        if len({stable_id for stable_id, _type, _default in self.attributes}) != len(self.attributes):
            raise KernelCompileError("kernel emitter attribute stable ids must be unique")
        if not all(
            isinstance(interface, (VectorField, PointCache))
            for interface in interfaces
        ):
            raise KernelCompileError("kernel emitter data interfaces are invalid")
        interfaces = tuple(sorted(interfaces, key=lambda value: value.stable_id))
        object.__setattr__(self, "data_interfaces", interfaces)
        if len({interface.stable_id for interface in interfaces}) != len(interfaces):
            raise KernelCompileError("kernel emitter data-interface stable ids must be unique")
        for stable_id, value_type, default in self.attributes:
            if type(stable_id) is not str or not stable_id or not isinstance(value_type, TypeRef):
                raise KernelCompileError("kernel emitter attribute schema is invalid")
            _finite_json(default)
        expected_stages = (
            (self.init, KernelStage.INIT),
            (self.update, KernelStage.UPDATE),
            (self.rendering, KernelStage.RENDERING),
        )
        for function, expected_stage in expected_stages:
            if function.stage is not expected_stage:
                raise KernelCompileError(
                    f"kernel emitter {expected_stage.value} function has stage {function.stage.value}"
                )
            self._validate_attribute_access(function)
            self._validate_data_interface_access(function)

    def _validate_data_interface_access(self, function: ParticleKernelFunction) -> None:
        interfaces = {interface.stable_id: interface for interface in self.data_interfaces}
        for instruction in function.instructions:
            if instruction.opcode not in {"sample_point_cache", "sample_vector_field"}:
                continue
            stable_id = instruction.immediate_dict()["interface"]
            interface = interfaces.get(stable_id)
            if interface is None:
                raise KernelCompileError(
                    f"kernel references unknown data interface {stable_id!r}"
                )
            expected_type = PointCache if instruction.opcode == "sample_point_cache" else VectorField
            if not isinstance(interface, expected_type):
                raise KernelCompileError(
                    f"kernel data interface {stable_id!r} is not a {expected_type.__name__}"
                )

    def _validate_attribute_access(self, function: ParticleKernelFunction) -> None:
        schema = {stable_id: value_type for stable_id, value_type, _default in self.attributes}
        reads: set[str] = set()
        writes: set[str] = set()
        for instruction in function.instructions:
            if instruction.opcode not in {
                "load_attribute",
                "store_attribute",
                "export_attribute",
            }:
                continue
            stable_id = instruction.immediate_dict()["attribute"]
            expected = schema.get(stable_id)
            if expected is None:
                raise KernelCompileError(f"kernel references unknown attribute {stable_id!r}")
            if instruction.opcode == "load_attribute":
                reads.add(stable_id)
                actual = instruction.result_type
            else:
                actual = instruction.operands[0].value_type
                if instruction.opcode == "store_attribute":
                    writes.add(stable_id)
            if actual != expected:
                raise KernelCompileError(
                    f"kernel attribute {stable_id!r} expected {expected}, got {actual}"
                )
        if reads != set(function.read_attributes):
            raise KernelCompileError("kernel read attribute summary does not match instructions")
        if writes != set(function.written_attributes):
            raise KernelCompileError("kernel written attribute summary does not match instructions")

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "random_seed": self.random_seed,
            "attributes": [
                {"stable_id": stable_id, "type": value_type.to_dict(), "default": default}
                for stable_id, value_type, default in self.attributes
            ],
            "data_interfaces": [
                interface.to_dict() for interface in self.data_interfaces
            ],
            "init": self.init.to_dict(include_source=include_source),
            "update": self.update.to_dict(include_source=include_source),
            "rendering": self.rendering.to_dict(include_source=include_source),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ParticleEmitterKernelIR":
        _exact_dict(
            value,
            {
                "stable_id",
                "random_seed",
                "attributes",
                "data_interfaces",
                "init",
                "update",
                "rendering",
            },
            "kernel emitter",
        )
        if type(value["attributes"]) is not list or type(value["data_interfaces"]) is not list:
            raise KernelCompileError(
                "kernel emitter attributes and data interfaces must be arrays"
            )
        attributes = []
        for item in value["attributes"]:
            _exact_dict(item, {"stable_id", "type", "default"}, "kernel attribute")
            try:
                value_type = TypeRef.from_dict(item["type"])
            except (TypeError, ValueError) as exc:
                raise KernelCompileError("kernel attribute type is invalid") from exc
            attributes.append((item["stable_id"], value_type, item["default"]))
        return cls(
            value["stable_id"],
            value["random_seed"],
            tuple(attributes),
            ParticleKernelFunction.from_dict(value["init"]),
            ParticleKernelFunction.from_dict(value["update"]),
            ParticleKernelFunction.from_dict(value["rendering"]),
            tuple(
                particle_data_interface_from_dict(
                    item, f"kernel emitter data_interfaces[{index}]"
                )
                for index, item in enumerate(value["data_interfaces"])
            ),
        )


@dataclass(frozen=True)
class ParticleKernelProgram:
    source_behavior_hash: str
    kernel_hash: str
    emitters: tuple[ParticleEmitterKernelIR, ...]
    contract: KernelRuntimeContract = KernelRuntimeContract()

    def __post_init__(self) -> None:
        for label, value in (
            ("source_behavior_hash", self.source_behavior_hash),
            ("kernel_hash", self.kernel_hash),
        ):
            if type(value) is not str or len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise KernelCompileError(f"particle kernel {label} must be a lowercase SHA-256")
        if not all(isinstance(emitter, ParticleEmitterKernelIR) for emitter in self.emitters):
            raise KernelCompileError("particle kernel emitters are invalid")
        if len({emitter.stable_id for emitter in self.emitters}) != len(self.emitters):
            raise KernelCompileError("particle kernel emitter stable ids must be unique")
        if not isinstance(self.contract, KernelRuntimeContract):
            raise KernelCompileError("particle kernel runtime contract is invalid")
        if self.kernel_hash != _kernel_semantic_hash(self.emitters, self.contract):
            raise KernelCompileError("particle kernel hash does not match its semantic payload")

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        return {
            "$schema": KERNEL_IR_SCHEMA,
            "source_behavior_hash": self.source_behavior_hash,
            "kernel_hash": self.kernel_hash,
            "contract": self.contract.to_dict(),
            "emitters": [
                emitter.to_dict(include_source=include_source)
                for emitter in self.emitters
            ],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ParticleKernelProgram":
        _exact_dict(
            value,
            {
                "$schema",
                "source_behavior_hash",
                "kernel_hash",
                "contract",
                "emitters",
            },
            "particle kernel program",
        )
        if value["$schema"] != KERNEL_IR_SCHEMA:
            raise KernelCompileError("particle kernel schema is unsupported")
        if type(value["emitters"]) is not list:
            raise KernelCompileError("particle kernel emitters must be an array")
        try:
            contract = KernelRuntimeContract.from_dict(value["contract"])
        except KernelSemanticError as exc:
            raise KernelCompileError(str(exc)) from exc
        return cls(
            value["source_behavior_hash"],
            value["kernel_hash"],
            tuple(ParticleEmitterKernelIR.from_dict(item) for item in value["emitters"]),
            contract,
        )


class ParticleKernelLowerer:
    """Lower Particle Program HIR into backend-neutral executable kernels."""

    def lower(self, program: ParticleProgramHIR) -> ParticleKernelProgram:
        emitters = tuple(self._lower_emitter(emitter) for emitter in program.emitters)
        contract = KernelRuntimeContract()
        return ParticleKernelProgram(
            program.behavior_hash,
            _kernel_semantic_hash(emitters, contract),
            emitters,
            contract,
        )

    def _lower_emitter(self, emitter: ParticleEmitterHIR) -> ParticleEmitterKernelIR:
        schema = tuple(
            (attribute.stable_id, attribute.value_type, attribute.default)
            for attribute in emitter.attributes
        )
        types = {stable_id: value_type for stable_id, value_type, _default in schema}
        defaults = {stable_id: default for stable_id, _value_type, default in schema}
        return ParticleEmitterKernelIR(
            emitter.stable_id,
            emitter.settings.seed,
            schema,
            self._lower_init(emitter, types, defaults),
            self._lower_update(emitter, types),
            self._lower_rendering(emitter, types),
            emitter.data_interfaces,
        )

    def _lower_init(self, emitter, attribute_types, defaults) -> ParticleKernelFunction:
        builder = _KernelBuilder(KernelStage.INIT, attribute_types)
        for stable_id in sorted(defaults):
            if stable_id == "builtin.id":
                continue
            value = builder.constant(
                attribute_types[stable_id],
                defaults[stable_id],
                KernelSourceRef(operation="attribute.default"),
            )
            builder.store(stable_id, value, KernelSourceRef(operation="attribute.default"))

        expression_values = builder.lower_expressions(emitter.init)
        for operation in emitter.init.operations:
            source = KernelSourceRef(operation.source_node_uid, operation=f"init.{operation.opcode}")
            parameters = operation.parameter_dict()
            bindings = dict(operation.value_bindings)
            if operation.opcode == "settings.initialize":
                lifetime = builder.random_range(
                    float(parameters["lifetime_min"]),
                    float(parameters["lifetime_max"]),
                    source,
                )
                builder.store("builtin.lifetime", lifetime, source)
                speed = builder.random_range(
                    float(parameters["initial_speed_min"]),
                    float(parameters["initial_speed_max"]),
                    source,
                )
                shape_parameters = {
                    "shape": parameters["shape"],
                    "shape_space": parameters["shape_space"],
                    "radius": parameters["shape_radius"],
                    "angle_degrees": parameters["shape_angle_degrees"],
                    "dimensions": parameters["shape_dimensions"],
                    "random_slots": list(builder.next_random_slots(3)),
                }
                shape_space = CoordinateSpace(parameters["shape_space"])
                shape_type = TypeRef(ValueType.VEC3, shape_space)
                position = builder.emit(
                    "sample_shape_position",
                    shape_type,
                    (),
                    shape_parameters,
                    source,
                )
                if shape_type != attribute_types["builtin.position"]:
                    position = builder.emit(
                        "convert_space",
                        attribute_types["builtin.position"],
                        (position,),
                        {
                            "from": shape_type.space.value,
                            "to": attribute_types["builtin.position"].space.value,
                            "semantic": "position",
                        },
                        source,
                    )
                builder.store("builtin.position", position, source)
                shape_parameters["random_slots"] = list(builder.next_random_slots(3))
                direction = builder.emit(
                    "sample_shape_direction",
                    shape_type,
                    (),
                    shape_parameters,
                    source,
                )
                if shape_type != attribute_types["builtin.velocity"]:
                    direction = builder.emit(
                        "convert_space",
                        attribute_types["builtin.velocity"],
                        (direction,),
                        {
                            "from": shape_type.space.value,
                            "to": attribute_types["builtin.velocity"].space.value,
                            "semantic": "direction",
                        },
                        source,
                    )
                velocity = builder.emit(
                    "multiply",
                    attribute_types["builtin.velocity"],
                    (direction, speed),
                    {},
                    source,
                )
                builder.store("builtin.velocity", velocity, source)
            elif operation.opcode == "attribute.set_velocity":
                value = builder.operation_value(
                    "value",
                    bindings,
                    expression_values,
                    parameters,
                    attribute_types["builtin.velocity"],
                    source,
                )
                builder.store("builtin.velocity", value, source)
            elif operation.opcode == "attribute.set_lifetime":
                value = builder.operation_value(
                    "value",
                    bindings,
                    expression_values,
                    parameters,
                    attribute_types["builtin.lifetime"],
                    source,
                )
                builder.store("builtin.lifetime", value, source)
            else:
                raise KernelCompileError(f"unsupported Init operation {operation.opcode!r}")
        return builder.finish()

    def _lower_update(self, emitter, attribute_types) -> ParticleKernelFunction:
        builder = _KernelBuilder(KernelStage.UPDATE, attribute_types)
        expression_values = builder.lower_expressions(emitter.update)
        delta_time = builder.emit(
            "load_uniform",
            TypeRef(ValueType.F32),
            (),
            {"name": "delta_time"},
            KernelSourceRef(operation="update.delta_time"),
        )
        age = builder.load("builtin.age", KernelSourceRef(operation="update.age"))
        new_age = builder.emit(
            "add", TypeRef(ValueType.F32), (age, delta_time), {}, KernelSourceRef(operation="update.age")
        )
        builder.store("builtin.age", new_age, KernelSourceRef(operation="update.age"))

        for operation in emitter.update.operations:
            source = KernelSourceRef(operation.source_node_uid, operation=f"update.{operation.opcode}")
            parameters = operation.parameter_dict()
            bindings = dict(operation.value_bindings)
            if operation.opcode not in {"settings.gravity", "integrate.acceleration"}:
                raise KernelCompileError(f"unsupported Update operation {operation.opcode!r}")
            acceleration = builder.operation_value(
                "value",
                bindings,
                expression_values,
                parameters,
                attribute_types["builtin.velocity"],
                source,
            )
            velocity = builder.load("builtin.velocity", source)
            delta_velocity = builder.emit(
                "multiply", attribute_types["builtin.velocity"], (acceleration, delta_time), {}, source
            )
            velocity = builder.emit(
                "add", attribute_types["builtin.velocity"], (velocity, delta_velocity), {}, source
            )
            builder.store("builtin.velocity", velocity, source)

        position = builder.load("builtin.position", KernelSourceRef(operation="update.integrate_position"))
        velocity = builder.load("builtin.velocity", KernelSourceRef(operation="update.integrate_position"))
        displacement = builder.emit(
            "multiply",
            attribute_types["builtin.position"],
            (velocity, delta_time),
            {},
            KernelSourceRef(operation="update.integrate_position"),
        )
        position = builder.emit(
            "add",
            attribute_types["builtin.position"],
            (position, displacement),
            {},
            KernelSourceRef(operation="update.integrate_position"),
        )
        builder.store("builtin.position", position, KernelSourceRef(operation="update.integrate_position"))
        lifetime = builder.load("builtin.lifetime", KernelSourceRef(operation="update.kill_expired"))
        alive = builder.emit(
            "less_than",
            TypeRef(ValueType.BOOL),
            (new_age, lifetime),
            {},
            KernelSourceRef(operation="update.kill_expired"),
        )
        builder.emit_void(
            "set_alive",
            (alive,),
            {},
            KernelSourceRef(operation="update.kill_expired"),
        )
        return builder.finish()

    def _lower_rendering(self, emitter, attribute_types) -> ParticleKernelFunction:
        builder = _KernelBuilder(KernelStage.RENDERING, attribute_types)
        builder.lower_expressions(emitter.rendering)
        for stable_id in (
            "builtin.position",
            "builtin.size",
            "builtin.color",
            "builtin.age",
            "builtin.lifetime",
            "builtin.id",
        ):
            if stable_id not in attribute_types:
                continue
            value = builder.load(stable_id, KernelSourceRef(operation="render.export"))
            builder.emit_void(
                "export_attribute",
                (value,),
                {"attribute": stable_id},
                KernelSourceRef(operation="render.export"),
            )
        return builder.finish()


class _KernelBuilder:
    def __init__(self, stage: KernelStage, attribute_types: Mapping[str, TypeRef]) -> None:
        self.stage = stage
        self.attribute_types = dict(attribute_types)
        self.instructions: list[KernelInstruction] = []
        self.read_attributes: set[str] = set()
        self.written_attributes: set[str] = set()
        self._value_types: dict[str, TypeRef] = {}
        self._random_slot = 0

    def emit(
        self,
        opcode: str,
        result_type: TypeRef,
        values: tuple[str, ...],
        immediates: Mapping[str, Any],
        source: KernelSourceRef,
    ) -> str:
        result_id = f"%{len(self._value_types)}"
        operands = tuple(KernelOperand(self._value_types[value], value_id=value) for value in values)
        instruction = KernelInstruction(
            opcode,
            result_id,
            result_type,
            operands,
            tuple(sorted(immediates.items())),
            KernelCapability.PORTABLE,
            source,
        )
        self.instructions.append(instruction)
        self._value_types[result_id] = result_type
        return result_id

    def emit_void(
        self,
        opcode: str,
        values: tuple[str, ...],
        immediates: Mapping[str, Any],
        source: KernelSourceRef,
    ) -> None:
        self.instructions.append(
            KernelInstruction(
                opcode,
                operands=tuple(KernelOperand(self._value_types[value], value_id=value) for value in values),
                immediates=tuple(sorted(immediates.items())),
                source=source,
            )
        )

    def constant(self, value_type: TypeRef, literal: Any, source: KernelSourceRef) -> str:
        return self.emit("constant", value_type, (), {"value": literal}, source)

    def load(self, stable_id: str, source: KernelSourceRef) -> str:
        value_type = self.attribute_types.get(stable_id)
        if value_type is None:
            raise KernelCompileError(f"kernel reads unknown attribute {stable_id!r}")
        self.read_attributes.add(stable_id)
        return self.emit("load_attribute", value_type, (), {"attribute": stable_id}, source)

    def store(self, stable_id: str, value: str, source: KernelSourceRef) -> None:
        expected = self.attribute_types.get(stable_id)
        actual = self._value_types.get(value)
        if expected is None or actual != expected:
            raise KernelCompileError(
                f"kernel store type mismatch for {stable_id!r}: expected {expected}, got {actual}"
            )
        self.written_attributes.add(stable_id)
        self.emit_void("store_attribute", (value,), {"attribute": stable_id}, source)

    def random_range(
        self,
        minimum: float,
        maximum: float,
        source: KernelSourceRef,
    ) -> str:
        if minimum == maximum:
            return self.constant(TypeRef(ValueType.F32), minimum, source)
        low = self.constant(TypeRef(ValueType.F32), minimum, source)
        high = self.constant(TypeRef(ValueType.F32), maximum, source)
        node_seed = self.constant(TypeRef(ValueType.U32), 0, source)
        return self.emit(
            "random_f32",
            TypeRef(ValueType.F32),
            (low, high, node_seed),
            {"random_slot": self.next_random_slot()},
            source,
        )

    def next_random_slot(self) -> int:
        result = self._random_slot
        self._random_slot += 1
        return result

    def next_random_slots(self, count: int) -> tuple[int, ...]:
        return tuple(self.next_random_slot() for _index in range(count))

    def lower_expressions(self, stage: ParticleStageHIR) -> dict[str, str]:
        lowered: dict[str, str] = {}
        for instruction in stage.expressions.instructions:
            source = KernelSourceRef(
                instruction.source_node_uid,
                instruction.source_port_id,
                f"expression.{instruction.opcode}",
            )
            if instruction.opcode == "constant":
                value = self.constant(instruction.result_type, instruction.operands[0].literal, source)
            elif instruction.opcode == "load_attribute":
                value = self.load(instruction.immediate_dict()["attribute"], source)
                if self._value_types[value] != instruction.result_type:
                    raise KernelCompileError(
                        f"expression attribute type mismatch for "
                        f"{instruction.immediate_dict()['attribute']!r}"
                    )
            else:
                values = []
                for operand in instruction.operands:
                    if operand.value_id:
                        try:
                            values.append(lowered[operand.value_id])
                        except KeyError as exc:
                            raise KernelCompileError(
                                f"expression reads unavailable value {operand.value_id!r}"
                            ) from exc
                    else:
                        values.append(self.constant(operand.value_type, operand.literal, source))
                immediates = instruction.immediate_dict()
                if instruction.opcode == "random_f32":
                    if immediates:
                        raise KernelCompileError("random expression cannot define authored immediates")
                    immediates = {"random_slot": self.next_random_slot()}
                value = self.emit(
                    instruction.opcode,
                    instruction.result_type,
                    tuple(values),
                    immediates,
                    source,
                )
            lowered[instruction.result_id] = value
        return lowered

    def operation_value(
        self,
        property_id: str,
        bindings: Mapping[str, str],
        expression_values: Mapping[str, str],
        parameters: Mapping[str, Any],
        expected_type: TypeRef,
        source: KernelSourceRef,
    ) -> str:
        expression_id = bindings.get(property_id, "")
        if expression_id:
            try:
                value = expression_values[expression_id]
            except KeyError as exc:
                raise KernelCompileError(f"operation binding {expression_id!r} is unavailable") from exc
            actual = self._value_types[value]
            if actual == expected_type:
                return value
            if actual.value_type == expected_type.value_type:
                return self.emit(
                    "convert_space",
                    expected_type,
                    (value,),
                    {
                        "from": actual.space.value,
                        "to": expected_type.space.value,
                        "semantic": "direction",
                    },
                    source,
                )
            raise KernelCompileError(
                f"operation {property_id!r} expected {expected_type}, got {actual}"
            )
        return self.constant(expected_type, parameters[property_id], source)

    def finish(self) -> ParticleKernelFunction:
        return ParticleKernelFunction(
            self.stage,
            tuple(self.instructions),
            tuple(self.read_attributes),
            tuple(self.written_attributes),
        )


def _finite_json(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise KernelCompileError("kernel values must be finite JSON data") from exc


def _exact_dict(value: Any, expected: set[str], label: str) -> None:
    if type(value) is not dict or set(value) != expected:
        raise KernelCompileError(f"{label} keys do not match the schema")


def _kernel_semantic_hash(
    emitters: tuple[ParticleEmitterKernelIR, ...],
    contract: KernelRuntimeContract,
) -> str:
    semantic = {
        "$schema": KERNEL_IR_SCHEMA,
        "contract": contract.to_dict(),
        "emitters": [emitter.to_dict(include_source=False) for emitter in emitters],
    }
    encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "KERNEL_IR_SCHEMA",
    "KernelCapability",
    "KernelCompileError",
    "KernelInstruction",
    "KernelOperand",
    "KernelSourceRef",
    "KernelStage",
    "ParticleEmitterKernelIR",
    "ParticleKernelFunction",
    "ParticleKernelLowerer",
    "ParticleKernelProgram",
]
