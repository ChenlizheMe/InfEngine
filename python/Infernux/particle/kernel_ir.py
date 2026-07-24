"""Portable typed SSA IR shared by particle execution backends."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping

from Infernux.graph.types import CoordinateSpace, TypeRef, ValueType

from .hir import (
    ParticleEmitterHIR,
    ParticleEventRouteHIR,
    ParticleEventSchedule,
    ParticleEventTypeHIR,
    ParticleProgramHIR,
    ParticleStage,
    ParticleStageHIR,
)
from .data_interface import (
    ParticleDataInterface,
    PointCache,
    SdfVolume,
    VectorField,
    particle_data_interface_from_dict,
)
from .nodes import particle_event_payload_port_id
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
            isinstance(interface, (VectorField, SdfVolume, PointCache))
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
            if instruction.opcode not in {
                "sample_point_cache",
                "sample_vector_field",
                "collide_sdf_position",
                "collide_sdf_velocity",
            }:
                continue
            stable_id = instruction.immediate_dict()["interface"]
            interface = interfaces.get(stable_id)
            if interface is None:
                raise KernelCompileError(
                    f"kernel references unknown data interface {stable_id!r}"
                )
            expected_type = (
                PointCache
                if instruction.opcode == "sample_point_cache"
                else VectorField
                if instruction.opcode == "sample_vector_field"
                else SdfVolume
            )
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
class KernelEventField:
    stable_id: str
    value_type: TypeRef
    word_offset: int
    word_count: int
    default: Any

    def __post_init__(self) -> None:
        if type(self.stable_id) is not str or not self.stable_id:
            raise KernelCompileError("kernel event field stable_id cannot be empty")
        if not isinstance(self.value_type, TypeRef):
            raise KernelCompileError("kernel event field type is invalid")
        if type(self.word_offset) is not int or self.word_offset < 0:
            raise KernelCompileError("kernel event field word_offset is invalid")
        if type(self.word_count) is not int or self.word_count <= 0:
            raise KernelCompileError("kernel event field word_count is invalid")
        _finite_json(self.default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "type": self.value_type.to_dict(),
            "word_offset": self.word_offset,
            "word_count": self.word_count,
            "default": self.default,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelEventField":
        _exact_dict(
            value,
            {"stable_id", "type", "word_offset", "word_count", "default"},
            "kernel event field",
        )
        try:
            value_type = TypeRef.from_dict(value["type"])
        except (TypeError, ValueError) as exc:
            raise KernelCompileError("kernel event field type is invalid") from exc
        return cls(
            value["stable_id"],
            value_type,
            value["word_offset"],
            value["word_count"],
            value["default"],
        )


@dataclass(frozen=True)
class KernelEventType:
    stable_id: str
    type_index: int
    stable_type_hash: int
    capacity_per_step: int
    payload_stride_words: int
    fields: tuple[KernelEventField, ...]

    def __post_init__(self) -> None:
        if type(self.stable_id) is not str or not self.stable_id:
            raise KernelCompileError("kernel event type stable_id cannot be empty")
        if type(self.type_index) is not int or self.type_index < 0:
            raise KernelCompileError("kernel event type index is invalid")
        if (
            type(self.stable_type_hash) is not int
            or not 0 <= self.stable_type_hash <= 0xFFFFFFFFFFFFFFFF
        ):
            raise KernelCompileError("kernel event stable type hash is invalid")
        if type(self.capacity_per_step) is not int or self.capacity_per_step <= 0:
            raise KernelCompileError("kernel event capacity is invalid")
        if type(self.payload_stride_words) is not int or self.payload_stride_words < 0:
            raise KernelCompileError("kernel event payload stride is invalid")
        if not all(isinstance(field, KernelEventField) for field in self.fields):
            raise KernelCompileError("kernel event fields are invalid")
        if len({field.stable_id for field in self.fields}) != len(self.fields):
            raise KernelCompileError("kernel event field stable ids must be unique")
        cursor = 0
        for field in self.fields:
            if field.word_offset != cursor:
                raise KernelCompileError("kernel event fields must use a contiguous payload layout")
            cursor += field.word_count
        if cursor != self.payload_stride_words:
            raise KernelCompileError("kernel event payload stride does not match its fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "type_index": self.type_index,
            "stable_type_hash": self.stable_type_hash,
            "capacity_per_step": self.capacity_per_step,
            "payload_stride_words": self.payload_stride_words,
            "fields": [field.to_dict() for field in self.fields],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelEventType":
        _exact_dict(
            value,
            {
                "stable_id",
                "type_index",
                "stable_type_hash",
                "capacity_per_step",
                "payload_stride_words",
                "fields",
            },
            "kernel event type",
        )
        if type(value["fields"]) is not list:
            raise KernelCompileError("kernel event fields must be an array")
        return cls(
            value["stable_id"],
            value["type_index"],
            value["stable_type_hash"],
            value["capacity_per_step"],
            value["payload_stride_words"],
            tuple(KernelEventField.from_dict(field) for field in value["fields"]),
        )


@dataclass(frozen=True)
class KernelEventRoute:
    stable_id: str
    event_type_index: int
    source_emitter_index: int
    source_stage: KernelStage
    target_emitter_index: int
    spawn_count: int

    def __post_init__(self) -> None:
        if type(self.stable_id) is not str or not self.stable_id:
            raise KernelCompileError("kernel event route stable_id cannot be empty")
        for label, value in (
            ("event type", self.event_type_index),
            ("source emitter", self.source_emitter_index),
            ("target emitter", self.target_emitter_index),
        ):
            if type(value) is not int or value < 0:
                raise KernelCompileError(f"kernel event route {label} index is invalid")
        object.__setattr__(self, "source_stage", KernelStage(self.source_stage))
        if type(self.spawn_count) is not int or self.spawn_count <= 0:
            raise KernelCompileError("kernel event route spawn_count is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "event_type_index": self.event_type_index,
            "source_emitter_index": self.source_emitter_index,
            "source_stage": self.source_stage.value,
            "target_emitter_index": self.target_emitter_index,
            "spawn_count": self.spawn_count,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelEventRoute":
        _exact_dict(
            value,
            {
                "stable_id",
                "event_type_index",
                "source_emitter_index",
                "source_stage",
                "target_emitter_index",
                "spawn_count",
            },
            "kernel event route",
        )
        return cls(
            value["stable_id"],
            value["event_type_index"],
            value["source_emitter_index"],
            value["source_stage"],
            value["target_emitter_index"],
            value["spawn_count"],
        )


@dataclass(frozen=True)
class KernelEventABI:
    abi_hash: str
    event_types: tuple[KernelEventType, ...]
    routes: tuple[KernelEventRoute, ...]

    def __post_init__(self) -> None:
        if type(self.abi_hash) is not str or len(self.abi_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.abi_hash
        ):
            raise KernelCompileError("kernel event ABI hash must be a lowercase SHA-256")
        if not all(isinstance(value, KernelEventType) for value in self.event_types):
            raise KernelCompileError("kernel event types are invalid")
        if not all(isinstance(value, KernelEventRoute) for value in self.routes):
            raise KernelCompileError("kernel event routes are invalid")
        if tuple(value.type_index for value in self.event_types) != tuple(
            range(len(self.event_types))
        ):
            raise KernelCompileError("kernel event type indices must be dense and ordered")
        if len({value.stable_id for value in self.event_types}) != len(self.event_types):
            raise KernelCompileError("kernel event type stable ids must be unique")
        if len({value.stable_id for value in self.routes}) != len(self.routes):
            raise KernelCompileError("kernel event route stable ids must be unique")
        for route in self.routes:
            if route.event_type_index >= len(self.event_types):
                raise KernelCompileError("kernel event route references an unknown event type")

    def to_dict(self) -> dict[str, Any]:
        return {
            "abi_hash": self.abi_hash,
            "event_types": [value.to_dict() for value in self.event_types],
            "routes": [value.to_dict() for value in self.routes],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelEventABI":
        _exact_dict(value, {"abi_hash", "event_types", "routes"}, "kernel event ABI")
        if type(value["event_types"]) is not list or type(value["routes"]) is not list:
            raise KernelCompileError("kernel event ABI types and routes must be arrays")
        return cls(
            value["abi_hash"],
            tuple(KernelEventType.from_dict(item) for item in value["event_types"]),
            tuple(KernelEventRoute.from_dict(item) for item in value["routes"]),
        )


@dataclass(frozen=True)
class ParticleKernelProgram:
    source_behavior_hash: str
    kernel_hash: str
    emitters: tuple[ParticleEmitterKernelIR, ...]
    events: KernelEventABI
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
        if not isinstance(self.events, KernelEventABI):
            raise KernelCompileError("particle kernel event ABI is invalid")
        if not isinstance(self.contract, KernelRuntimeContract):
            raise KernelCompileError("particle kernel runtime contract is invalid")
        self._validate_event_access()
        if self.kernel_hash != _kernel_semantic_hash(self.emitters, self.events, self.contract):
            raise KernelCompileError("particle kernel hash does not match its semantic payload")

    def _validate_event_access(self) -> None:
        for route in self.events.routes:
            if (
                route.source_emitter_index >= len(self.emitters)
                or route.target_emitter_index >= len(self.emitters)
            ):
                raise KernelCompileError("kernel event route references an unknown emitter")
        for emitter_index, emitter in enumerate(self.emitters):
            for function in (emitter.init, emitter.update, emitter.rendering):
                for instruction in function.instructions:
                    if instruction.opcode == "event_payload":
                        channel_index = instruction.immediate_dict()["channel_index"]
                        if (
                            type(channel_index) is not int
                            or not 0 <= channel_index < len(self.events.routes)
                        ):
                            raise KernelCompileError(
                                "kernel event_payload references an unknown channel"
                            )
                        route = self.events.routes[channel_index]
                        if (
                            route.target_emitter_index != emitter_index
                            or function.stage is not KernelStage.INIT
                        ):
                            raise KernelCompileError(
                                "kernel event_payload does not match its target route"
                            )
                        event_type = self.events.event_types[route.event_type_index]
                        immediate = instruction.immediate_dict()
                        field = next(
                            (
                                value
                                for value in event_type.fields
                                if value.word_offset == immediate["word_offset"]
                            ),
                            None,
                        )
                        if (
                            field is None
                            or field.word_count != immediate["word_count"]
                            or field.value_type != instruction.result_type
                            or field.default != immediate["default"]
                        ):
                            raise KernelCompileError(
                                "kernel event_payload does not match its event field"
                            )
                        continue
                    if instruction.opcode != "event_append":
                        continue
                    channel_index = instruction.immediate_dict()["channel_index"]
                    if type(channel_index) is not int or not 0 <= channel_index < len(
                        self.events.routes
                    ):
                        raise KernelCompileError("kernel event_append references an unknown channel")
                    route = self.events.routes[channel_index]
                    if (
                        route.source_emitter_index != emitter_index
                        or route.source_stage is not function.stage
                    ):
                        raise KernelCompileError("kernel event_append does not match its source route")
                    event_type = self.events.event_types[route.event_type_index]
                    payload_types = tuple(
                        operand.value_type for operand in instruction.operands[1:]
                    )
                    if payload_types != tuple(field.value_type for field in event_type.fields):
                        raise KernelCompileError("kernel event_append payload does not match its event type")

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        return {
            "$schema": KERNEL_IR_SCHEMA,
            "source_behavior_hash": self.source_behavior_hash,
            "kernel_hash": self.kernel_hash,
            "contract": self.contract.to_dict(),
            "events": self.events.to_dict(),
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
                "events",
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
            KernelEventABI.from_dict(value["events"]),
            contract,
        )


class ParticleKernelLowerer:
    """Lower Particle Program HIR into backend-neutral executable kernels."""

    def lower(self, program: ParticleProgramHIR) -> ParticleKernelProgram:
        routes = {
            route.stable_id: (index, route)
            for index, route in enumerate(program.events.routes)
        }
        event_types = {
            event_type.type_index: event_type
            for event_type in program.events.event_types
        }
        emitters = tuple(
            self._lower_emitter(emitter, routes, event_types)
            for emitter in program.emitters
        )
        events = _lower_event_abi(program.events)
        contract = KernelRuntimeContract()
        return ParticleKernelProgram(
            program.behavior_hash,
            _kernel_semantic_hash(emitters, events, contract),
            emitters,
            events,
            contract,
        )

    def _lower_emitter(
        self,
        emitter: ParticleEmitterHIR,
        routes: Mapping[str, tuple[int, ParticleEventRouteHIR]],
        event_types: Mapping[int, ParticleEventTypeHIR],
    ) -> ParticleEmitterKernelIR:
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
            self._lower_init(emitter, types, defaults, routes, event_types),
            self._lower_update(emitter, types, routes, event_types),
            self._lower_rendering(emitter, types, routes, event_types),
            emitter.data_interfaces,
        )

    def _lower_init(
        self,
        emitter,
        attribute_types,
        defaults,
        routes,
        event_types,
    ) -> ParticleKernelFunction:
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
            if operation.opcode == "emitter.sample_shape":
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
            elif operation.opcode in {
                "attribute.set_flipbook_frame",
                "attribute.set_color",
                "attribute.set_size",
                "attribute.set_scale",
                "attribute.set_rotation",
                "attribute.set_orientation",
                "attribute.set_strip_id",
                "attribute.set_ribbon_order",
                "attribute.set_ribbon_break",
            }:
                stable_id = {
                    "attribute.set_flipbook_frame": "builtin.flipbook_frame",
                    "attribute.set_color": "builtin.color",
                    "attribute.set_size": "builtin.size",
                    "attribute.set_scale": "builtin.scale",
                    "attribute.set_rotation": "builtin.rotation",
                    "attribute.set_orientation": "builtin.orientation",
                    "attribute.set_strip_id": "builtin.ribbon_strip_id",
                    "attribute.set_ribbon_order": "builtin.ribbon_order",
                    "attribute.set_ribbon_break": "builtin.ribbon_break",
                }[operation.opcode]
                property_name = "degrees" if operation.opcode == "attribute.set_orientation" else "value"
                value = builder.operation_value(
                    property_name,
                    bindings,
                    expression_values,
                    parameters,
                    attribute_types[stable_id],
                    source,
                )
                if operation.opcode == "attribute.set_orientation":
                    radians_per_degree = builder.constant(
                        TypeRef(ValueType.F32),
                        math.pi / 180.0,
                        source,
                    )
                    value = builder.emit(
                        "multiply",
                        attribute_types[stable_id],
                        (value, radians_per_degree),
                        {},
                        source,
                    )
                builder.store(stable_id, value, source)
            elif operation.opcode == "event.emit":
                self._lower_event_output(
                    builder,
                    operation,
                    expression_values,
                    routes,
                    event_types,
                    source,
                )
            else:
                raise KernelCompileError(f"unsupported Init operation {operation.opcode!r}")
        return builder.finish()

    def _lower_update(
        self,
        emitter,
        attribute_types,
        routes,
        event_types,
    ) -> ParticleKernelFunction:
        builder = _KernelBuilder(KernelStage.UPDATE, attribute_types)
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
        expression_values = builder.lower_expressions(emitter.update)

        for operation in emitter.update.operations:
            source = KernelSourceRef(operation.source_node_uid, operation=f"update.{operation.opcode}")
            parameters = operation.parameter_dict()
            bindings = dict(operation.value_bindings)
            if operation.opcode in {
                "attribute.set_velocity",
                "attribute.set_lifetime",
                "attribute.set_flipbook_frame",
            }:
                stable_id = {
                    "attribute.set_velocity": "builtin.velocity",
                    "attribute.set_lifetime": "builtin.lifetime",
                    "attribute.set_flipbook_frame": "builtin.flipbook_frame",
                }[operation.opcode]
                value = builder.operation_value(
                    "value",
                    bindings,
                    expression_values,
                    parameters,
                    attribute_types[stable_id],
                    source,
                )
                builder.store(stable_id, value, source)
            elif operation.opcode == "integrate.acceleration":
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
            elif operation.opcode in {
                "attribute.set_color",
                "attribute.set_size",
                "attribute.set_scale",
                "attribute.set_rotation",
                "attribute.set_orientation",
                "attribute.set_strip_id",
                "attribute.set_ribbon_order",
                "attribute.set_ribbon_break",
            }:
                stable_id = {
                    "attribute.set_color": "builtin.color",
                    "attribute.set_size": "builtin.size",
                    "attribute.set_scale": "builtin.scale",
                    "attribute.set_rotation": "builtin.rotation",
                    "attribute.set_orientation": "builtin.orientation",
                    "attribute.set_strip_id": "builtin.ribbon_strip_id",
                    "attribute.set_ribbon_order": "builtin.ribbon_order",
                    "attribute.set_ribbon_break": "builtin.ribbon_break",
                }[operation.opcode]
                property_name = "degrees" if operation.opcode == "attribute.set_orientation" else "value"
                value = builder.operation_value(
                    property_name,
                    bindings,
                    expression_values,
                    parameters,
                    attribute_types[stable_id],
                    source,
                )
                if operation.opcode == "attribute.set_orientation":
                    radians_per_degree = builder.constant(
                        TypeRef(ValueType.F32),
                        math.pi / 180.0,
                        source,
                    )
                    value = builder.emit(
                        "multiply",
                        attribute_types[stable_id],
                        (value, radians_per_degree),
                        {},
                        source,
                    )
                builder.store(stable_id, value, source)
            elif operation.opcode == "integrate.angular_velocity":
                degrees_per_second = builder.operation_value(
                    "degrees_per_second",
                    bindings,
                    expression_values,
                    parameters,
                    attribute_types["builtin.rotation"],
                    source,
                )
                radians_per_degree = builder.constant(
                    attribute_types["builtin.rotation"],
                    math.pi / 180.0,
                    source,
                )
                radians_per_second = builder.emit(
                    "multiply",
                    attribute_types["builtin.rotation"],
                    (degrees_per_second, radians_per_degree),
                    {},
                    source,
                )
                delta_rotation = builder.emit(
                    "multiply",
                    attribute_types["builtin.rotation"],
                    (radians_per_second, delta_time),
                    {},
                    source,
                )
                rotation = builder.load("builtin.rotation", source)
                rotation = builder.emit(
                    "add",
                    attribute_types["builtin.rotation"],
                    (rotation, delta_rotation),
                    {},
                    source,
                )
                builder.store("builtin.rotation", rotation, source)
            elif operation.opcode == "integrate.angular_velocity_3d":
                degrees_per_second = builder.operation_value(
                    "degrees_per_second",
                    bindings,
                    expression_values,
                    parameters,
                    attribute_types["builtin.orientation"],
                    source,
                )
                radians_per_degree = builder.constant(
                    TypeRef(ValueType.F32),
                    math.pi / 180.0,
                    source,
                )
                radians_per_second = builder.emit(
                    "multiply",
                    attribute_types["builtin.orientation"],
                    (degrees_per_second, radians_per_degree),
                    {},
                    source,
                )
                delta_orientation = builder.emit(
                    "multiply",
                    attribute_types["builtin.orientation"],
                    (radians_per_second, delta_time),
                    {},
                    source,
                )
                orientation = builder.load("builtin.orientation", source)
                orientation = builder.emit(
                    "add",
                    attribute_types["builtin.orientation"],
                    (orientation, delta_orientation),
                    {},
                    source,
                )
                builder.store("builtin.orientation", orientation, source)
            elif operation.opcode == "lifecycle.kill_if":
                condition = builder.operation_value(
                    "condition",
                    bindings,
                    expression_values,
                    parameters,
                    TypeRef(ValueType.BOOL),
                    source,
                )
                builder.emit_void("kill_if", (condition,), {}, source)
            elif operation.opcode == "event.emit":
                self._lower_event_output(
                    builder,
                    operation,
                    expression_values,
                    routes,
                    event_types,
                    source,
                )
            elif operation.opcode in {"collision.plane", "collision.sphere", "collision.sdf"}:
                # Collisions are lowered after the implicit position integration below.
                continue
            else:
                raise KernelCompileError(f"unsupported Update operation {operation.opcode!r}")

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
        for operation in emitter.update.operations:
            if operation.opcode not in {"collision.plane", "collision.sphere", "collision.sdf"}:
                continue
            source = KernelSourceRef(
                operation.source_node_uid,
                operation=f"update.{operation.opcode}",
            )
            parameters = operation.parameter_dict()
            bindings = dict(operation.value_bindings)
            position = builder.load("builtin.position", source)
            velocity = builder.load("builtin.velocity", source)
            if operation.opcode == "collision.plane":
                point = builder.operation_value(
                    "point", bindings, expression_values, parameters,
                    attribute_types["builtin.position"], source,
                )
                normal = builder.operation_value(
                    "normal", bindings, expression_values, parameters,
                    attribute_types["builtin.position"], source,
                )
                radius = builder.operation_value(
                    "radius", bindings, expression_values, parameters,
                    TypeRef(ValueType.F32), source,
                )
                collision_operands = (
                    position,
                    velocity,
                    point,
                    normal,
                    radius,
                )
            elif operation.opcode == "collision.sphere":
                center = builder.operation_value(
                    "center", bindings, expression_values, parameters,
                    attribute_types["builtin.position"], source,
                )
                sphere_radius = builder.operation_value(
                    "sphere_radius", bindings, expression_values, parameters,
                    TypeRef(ValueType.F32), source,
                )
                particle_radius = builder.operation_value(
                    "particle_radius", bindings, expression_values, parameters,
                    TypeRef(ValueType.F32), source,
                )
                collision_operands = (
                    position,
                    velocity,
                    center,
                    sphere_radius,
                    particle_radius,
                )
            else:
                particle_radius = builder.operation_value(
                    "particle_radius", bindings, expression_values, parameters,
                    TypeRef(ValueType.F32), source,
                )
                collision_operands = (
                    position,
                    velocity,
                    particle_radius,
                )
            restitution = builder.operation_value(
                "restitution",
                bindings,
                expression_values,
                parameters,
                TypeRef(ValueType.F32),
                source,
            )
            friction = builder.operation_value(
                "friction",
                bindings,
                expression_values,
                parameters,
                TypeRef(ValueType.F32),
                source,
            )
            collision_operands += (restitution, friction)
            opcode_prefix = operation.opcode.replace("collision.", "collide_")
            immediates = (
                {
                    "interface": parameters["interface"],
                    "inverted": parameters["inverted"],
                }
                if operation.opcode == "collision.sdf"
                else {}
            )
            resolved_position = builder.emit(
                f"{opcode_prefix}_position",
                attribute_types["builtin.position"],
                collision_operands,
                immediates,
                source,
            )
            resolved_velocity = builder.emit(
                f"{opcode_prefix}_velocity",
                attribute_types["builtin.velocity"],
                collision_operands,
                immediates,
                source,
            )
            builder.store("builtin.position", resolved_position, source)
            builder.store("builtin.velocity", resolved_velocity, source)
        lifetime = builder.load("builtin.lifetime", KernelSourceRef(operation="update.kill_expired"))
        alive = builder.emit(
            "less_than",
            TypeRef(ValueType.BOOL),
            (new_age, lifetime),
            {},
            KernelSourceRef(operation="update.kill_expired"),
        )
        expired = builder.emit(
            "logical_not",
            TypeRef(ValueType.BOOL),
            (alive,),
            {},
            KernelSourceRef(operation="update.kill_expired"),
        )
        builder.emit_void(
            "kill_if",
            (expired,),
            {},
            KernelSourceRef(operation="update.kill_expired"),
        )
        return builder.finish()

    def _lower_rendering(
        self,
        emitter,
        attribute_types,
        routes,
        event_types,
    ) -> ParticleKernelFunction:
        builder = _KernelBuilder(KernelStage.RENDERING, attribute_types)
        expression_values = builder.lower_expressions(emitter.rendering)
        for operation in emitter.rendering.operations:
            if operation.opcode != "event.emit":
                continue
            self._lower_event_output(
                builder,
                operation,
                expression_values,
                routes,
                event_types,
                KernelSourceRef(
                    operation.source_node_uid,
                    operation=f"rendering.{operation.opcode}",
                ),
            )
        for stable_id in (
            "builtin.position",
            "builtin.size",
            "builtin.scale",
            "builtin.color",
            "builtin.rotation",
            "builtin.orientation",
            "builtin.age",
            "builtin.lifetime",
            "builtin.flipbook_frame",
            "builtin.id",
            "builtin.ribbon_strip_id",
            "builtin.ribbon_order",
            "builtin.ribbon_break",
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

    @staticmethod
    def _lower_event_output(
        builder,
        operation,
        expression_values,
        routes,
        event_types,
        source,
    ) -> None:
        parameters = operation.parameter_dict()
        bindings = dict(operation.value_bindings)
        route_id = str(parameters["route"])
        try:
            channel_index, route = routes[route_id]
            event_type = event_types[route.event_type_index]
        except KeyError as exc:
            raise KernelCompileError(
                f"Event Output references unavailable route {route_id!r}"
            ) from exc
        condition = builder.operation_value(
            "condition",
            bindings,
            expression_values,
            parameters,
            TypeRef(ValueType.BOOL),
            source,
        )
        payload_values = []
        payload_layout = []
        for field in event_type.fields:
            port_id = particle_event_payload_port_id(field.stable_id)
            payload_values.append(
                builder.operation_value(
                    port_id,
                    bindings,
                    expression_values,
                    parameters,
                    field.value_type,
                    source,
                )
            )
            payload_layout.append(
                {
                    "stable_id": field.stable_id,
                    "type": field.value_type.to_dict(),
                    "word_offset": field.word_offset,
                    "word_count": field.word_count,
                }
            )
        builder.emit_void(
            "event_append",
            (condition, *payload_values),
            {
                "channel_index": channel_index,
                "payload_layout": payload_layout,
            },
            source,
        )


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


def _lower_event_abi(events: ParticleEventSchedule) -> KernelEventABI:
    return KernelEventABI(
        events.event_abi_hash,
        tuple(
            KernelEventType(
                event_type.stable_id,
                event_type.type_index,
                event_type.stable_type_hash,
                event_type.capacity_per_step,
                event_type.payload_stride_words,
                tuple(
                    KernelEventField(
                        field.stable_id,
                        field.value_type,
                        field.word_offset,
                        field.word_count,
                        field.default,
                    )
                    for field in event_type.fields
                ),
            )
            for event_type in events.event_types
        ),
        tuple(
            KernelEventRoute(
                route.stable_id,
                route.event_type_index,
                route.source_emitter_index,
                route.source_stage.value,
                route.target_emitter_index,
                route.spawn_count,
            )
            for route in events.routes
        ),
    )


def _kernel_semantic_hash(
    emitters: tuple[ParticleEmitterKernelIR, ...],
    events: KernelEventABI,
    contract: KernelRuntimeContract,
) -> str:
    semantic = {
        "$schema": KERNEL_IR_SCHEMA,
        "contract": contract.to_dict(),
        "events": events.to_dict(),
        "emitters": [emitter.to_dict(include_source=False) for emitter in emitters],
    }
    encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "KERNEL_IR_SCHEMA",
    "KernelCapability",
    "KernelCompileError",
    "KernelInstruction",
    "KernelEventABI",
    "KernelEventField",
    "KernelEventRoute",
    "KernelEventType",
    "KernelOperand",
    "KernelSourceRef",
    "KernelStage",
    "ParticleEmitterKernelIR",
    "ParticleKernelFunction",
    "ParticleKernelLowerer",
    "ParticleKernelProgram",
]
