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
    ParticleSuspensionKind,
)
from .data_interface import (
    ParticleDataInterface,
    SdfVolume,
    VectorField,
    particle_data_interface_from_dict,
)
from .nodes import ATTRIBUTE_OPERATION_SPECS, particle_event_payload_port_id
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
    source_name: str = ""
    line: int = 0
    column: int = 0
    end_line: int = 0
    end_column: int = 0

    def __post_init__(self) -> None:
        if not all(
            type(value) is str
            for value in (
                self.node_uid,
                self.port_id,
                self.operation,
                self.source_name,
            )
        ):
            raise KernelCompileError("kernel source fields must be strings")
        coordinates = (self.line, self.column, self.end_line, self.end_column)
        if any(type(value) is not int or value < 0 for value in coordinates):
            raise KernelCompileError(
                "kernel source coordinates must be non-negative integers"
            )
        if self.line == 0 and any(coordinates[1:]):
            raise KernelCompileError("kernel source coordinates require a start line")

    def describe(self) -> str:
        location = self.source_name
        if self.line:
            location = f"{location or '<particle-source>'}:{self.line}"
            if self.column:
                location += f":{self.column}"
        identity = self.node_uid + (f".{self.port_id}" if self.port_id else "")
        if identity:
            location += f"{' ' if location else ''}[{identity}]"
        return location

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_uid": self.node_uid,
            "port_id": self.port_id,
            "operation": self.operation,
            "source_name": self.source_name,
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelSourceRef":
        _exact_dict(
            value,
            {
                "node_uid",
                "port_id",
                "operation",
                "source_name",
                "line",
                "column",
                "end_line",
                "end_column",
            },
            "kernel source",
        )
        if not all(
            type(value[name]) is str
            for name in ("node_uid", "port_id", "operation", "source_name")
        ):
            raise KernelCompileError("kernel source fields must be strings")
        return cls(
            value["node_uid"],
            value["port_id"],
            value["operation"],
            value["source_name"],
            value["line"],
            value["column"],
            value["end_line"],
            value["end_column"],
        )


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
class KernelExecutionLane:
    stable_id: str
    index: int
    parent_index: int
    source_node_uid: str
    source_port_id: str

    def __post_init__(self) -> None:
        if (
            type(self.stable_id) is not str
            or not self.stable_id
            or type(self.index) is not int
            or self.index < 0
            or type(self.parent_index) is not int
            or type(self.source_node_uid) is not str
            or not self.source_node_uid
            or type(self.source_port_id) is not str
        ):
            raise KernelCompileError("kernel execution lane is invalid")

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id if include_source else "",
            "index": self.index,
            "parent_index": self.parent_index,
            "source_node_uid": self.source_node_uid if include_source else "",
            "source_port_id": self.source_port_id if include_source else "",
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelExecutionLane":
        _exact_dict(
            value,
            {
                "stable_id",
                "index",
                "parent_index",
                "source_node_uid",
                "source_port_id",
            },
            "kernel execution lane",
        )
        return cls(
            value["stable_id"],
            value["index"],
            value["parent_index"],
            value["source_node_uid"],
            value["source_port_id"],
        )


@dataclass(frozen=True)
class KernelFlowBlock:
    source_node_uid: str
    lane_index: int
    instruction_begin: int
    instruction_end: int

    def __post_init__(self) -> None:
        if (
            type(self.source_node_uid) is not str
            or not self.source_node_uid
            or type(self.lane_index) is not int
            or self.lane_index < 0
            or type(self.instruction_begin) is not int
            or type(self.instruction_end) is not int
            or self.instruction_begin < 0
            or self.instruction_end < self.instruction_begin
        ):
            raise KernelCompileError("kernel flow block is invalid")

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        return {
            "source_node_uid": self.source_node_uid if include_source else "",
            "lane_index": self.lane_index,
            "instruction_begin": self.instruction_begin,
            "instruction_end": self.instruction_end,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelFlowBlock":
        _exact_dict(
            value,
            {
                "source_node_uid",
                "lane_index",
                "instruction_begin",
                "instruction_end",
            },
            "kernel flow block",
        )
        return cls(
            value["source_node_uid"],
            value["lane_index"],
            value["instruction_begin"],
            value["instruction_end"],
        )


@dataclass(frozen=True)
class KernelJoinAll:
    source_node_uid: str
    input_lane_indices: tuple[int, ...]
    output_lane_index: int

    def __post_init__(self) -> None:
        if (
            type(self.source_node_uid) is not str
            or not self.source_node_uid
            or len(self.input_lane_indices) < 2
            or not all(type(value) is int and value >= 0 for value in self.input_lane_indices)
            or len(set(self.input_lane_indices)) != len(self.input_lane_indices)
            or type(self.output_lane_index) is not int
            or self.output_lane_index < 0
        ):
            raise KernelCompileError("kernel Join All descriptor is invalid")

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        return {
            "source_node_uid": self.source_node_uid if include_source else "",
            "input_lane_indices": list(self.input_lane_indices),
            "output_lane_index": self.output_lane_index,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelJoinAll":
        _exact_dict(
            value,
            {"source_node_uid", "input_lane_indices", "output_lane_index"},
            "kernel Join All",
        )
        if type(value["input_lane_indices"]) is not list:
            raise KernelCompileError("kernel Join All input lanes must be an array")
        return cls(
            value["source_node_uid"],
            tuple(value["input_lane_indices"]),
            value["output_lane_index"],
        )


@dataclass(frozen=True)
class KernelLifecycleFlow:
    lifecycle_stage: ParticleStage
    kernel_stage: KernelStage
    entry_node_uid: str
    lanes: tuple[KernelExecutionLane, ...]
    blocks: tuple[KernelFlowBlock, ...]
    joins: tuple[KernelJoinAll, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle_stage", ParticleStage(self.lifecycle_stage))
        object.__setattr__(self, "kernel_stage", KernelStage(self.kernel_stage))
        expected_kernel_stage = {
            ParticleStage.INIT: KernelStage.INIT,
            ParticleStage.UPDATE: KernelStage.UPDATE,
            ParticleStage.COLLISION_ENTER: KernelStage.UPDATE,
            ParticleStage.COLLISION_STAY: KernelStage.UPDATE,
            ParticleStage.COLLISION_EXIT: KernelStage.UPDATE,
            ParticleStage.RENDERING: KernelStage.RENDERING,
        }[self.lifecycle_stage]
        if self.kernel_stage is not expected_kernel_stage:
            raise KernelCompileError("kernel lifecycle flow targets the wrong function stage")
        if type(self.entry_node_uid) is not str or not self.entry_node_uid:
            raise KernelCompileError("kernel lifecycle flow entry cannot be empty")
        if not self.lanes or not all(isinstance(item, KernelExecutionLane) for item in self.lanes):
            raise KernelCompileError("kernel lifecycle flow requires execution lanes")
        if tuple(lane.index for lane in self.lanes) != tuple(range(len(self.lanes))):
            raise KernelCompileError("kernel execution lane indices must be dense")
        if len({lane.stable_id for lane in self.lanes}) != len(self.lanes):
            raise KernelCompileError("kernel execution lane identities must be unique")
        for lane in self.lanes:
            if lane.index == 0:
                if lane.parent_index != -1:
                    raise KernelCompileError("kernel root lane cannot have a parent")
            elif not 0 <= lane.parent_index < lane.index:
                raise KernelCompileError("kernel execution lane parent is invalid")
        if not self.blocks or not all(isinstance(item, KernelFlowBlock) for item in self.blocks):
            raise KernelCompileError("kernel lifecycle flow requires blocks")
        if len({block.source_node_uid for block in self.blocks}) != len(self.blocks):
            raise KernelCompileError("kernel flow block source nodes must be unique")
        if self.blocks[0].source_node_uid != self.entry_node_uid:
            raise KernelCompileError("kernel lifecycle flow root block is invalid")
        if any(not 0 <= block.lane_index < len(self.lanes) for block in self.blocks):
            raise KernelCompileError("kernel flow block lane is invalid")
        block_by_node = {block.source_node_uid: block for block in self.blocks}
        previous_end = None
        for block in self.blocks:
            if previous_end is not None and block.instruction_begin < previous_end:
                raise KernelCompileError(
                    "kernel flow instruction ranges do not follow block order"
                )
            previous_end = block.instruction_end
        if not all(isinstance(item, KernelJoinAll) for item in self.joins):
            raise KernelCompileError("kernel lifecycle Join All metadata is invalid")
        if len({join.source_node_uid for join in self.joins}) != len(self.joins):
            raise KernelCompileError("kernel lifecycle Join All nodes must be unique")
        for join in self.joins:
            block = block_by_node.get(join.source_node_uid)
            if block is None or block.lane_index != join.output_lane_index:
                raise KernelCompileError("kernel Join All output block is inconsistent")
            if any(value >= len(self.lanes) for value in join.input_lane_indices):
                raise KernelCompileError("kernel Join All input lane is invalid")
            if join.output_lane_index >= len(self.lanes):
                raise KernelCompileError("kernel Join All output lane is invalid")

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        return {
            "lifecycle_stage": self.lifecycle_stage.value,
            "kernel_stage": self.kernel_stage.value,
            "entry_node_uid": self.entry_node_uid if include_source else "",
            "lanes": [
                lane.to_dict(include_source=include_source) for lane in self.lanes
            ],
            "blocks": [
                block.to_dict(include_source=include_source) for block in self.blocks
            ],
            "joins": [
                join.to_dict(include_source=include_source) for join in self.joins
            ],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelLifecycleFlow":
        _exact_dict(
            value,
            {
                "lifecycle_stage",
                "kernel_stage",
                "entry_node_uid",
                "lanes",
                "blocks",
                "joins",
            },
            "kernel lifecycle flow",
        )
        for name in ("lanes", "blocks", "joins"):
            if type(value[name]) is not list:
                raise KernelCompileError(f"kernel lifecycle flow {name} must be an array")
        return cls(
            value["lifecycle_stage"],
            value["kernel_stage"],
            value["entry_node_uid"],
            tuple(KernelExecutionLane.from_dict(item) for item in value["lanes"]),
            tuple(KernelFlowBlock.from_dict(item) for item in value["blocks"]),
            tuple(KernelJoinAll.from_dict(item) for item in value["joins"]),
        )


@dataclass(frozen=True)
class KernelSuspensionPoint:
    lifecycle_stage: ParticleStage
    kind: ParticleSuspensionKind
    lane_index: int
    lane_stable_id: str
    resume_program_counter: int
    stage_resume_program_counter: int
    resume_instruction_index: int
    suspend_instruction_index: int
    source_node_uid: str
    resume_node_uid: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle_stage", ParticleStage(self.lifecycle_stage))
        object.__setattr__(self, "kind", ParticleSuspensionKind(self.kind))
        terminal = (
            self.resume_instruction_index == -1
            and self.resume_node_uid == ""
        )
        if (
            type(self.lane_index) is not int
            or self.lane_index < 0
            or type(self.lane_stable_id) is not str
            or not self.lane_stable_id
            or type(self.resume_program_counter) is not int
            or self.resume_program_counter <= 0
            or type(self.stage_resume_program_counter) is not int
            or self.stage_resume_program_counter <= 0
            or type(self.resume_instruction_index) is not int
            or type(self.suspend_instruction_index) is not int
            or self.suspend_instruction_index < 0
            or type(self.source_node_uid) is not str
            or not self.source_node_uid
            or type(self.resume_node_uid) is not str
            or (
                not terminal
                and (
                    self.resume_instruction_index < 0
                    or not self.resume_node_uid
                )
            )
            or (
                self.resume_node_uid == ""
                and not terminal
            )
        ):
            raise KernelCompileError("kernel suspension point is invalid")

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        return {
            "lifecycle_stage": self.lifecycle_stage.value,
            "kind": self.kind.value,
            "lane_index": self.lane_index,
            "lane_stable_id": self.lane_stable_id,
            "resume_program_counter": self.resume_program_counter,
            "stage_resume_program_counter": self.stage_resume_program_counter,
            "resume_instruction_index": self.resume_instruction_index,
            "suspend_instruction_index": self.suspend_instruction_index,
            "source_node_uid": self.source_node_uid if include_source else "",
            "resume_node_uid": self.resume_node_uid if include_source else "",
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelSuspensionPoint":
        _exact_dict(
            value,
            {
                "lifecycle_stage",
                "kind",
                "lane_index",
                "lane_stable_id",
                "resume_program_counter",
                "stage_resume_program_counter",
                "resume_instruction_index",
                "suspend_instruction_index",
                "source_node_uid",
                "resume_node_uid",
            },
            "kernel suspension point",
        )
        return cls(
            value["lifecycle_stage"],
            value["kind"],
            value["lane_index"],
            value["lane_stable_id"],
            value["resume_program_counter"],
            value["stage_resume_program_counter"],
            value["resume_instruction_index"],
            value["suspend_instruction_index"],
            value["source_node_uid"],
            value["resume_node_uid"],
        )


@dataclass(frozen=True)
class ParticleEmitterKernelIR:
    stable_id: str
    random_seed: int
    attributes: tuple[tuple[str, TypeRef, Any], ...]
    init: ParticleKernelFunction
    update: ParticleKernelFunction
    rendering: ParticleKernelFunction
    flows: tuple[KernelLifecycleFlow, ...]
    data_interfaces: tuple[ParticleDataInterface, ...] = ()
    suspensions: tuple[KernelSuspensionPoint, ...] = ()

    def __post_init__(self) -> None:
        interfaces = tuple(self.data_interfaces)
        if type(self.stable_id) is not str or not self.stable_id:
            raise KernelCompileError("kernel emitter stable_id cannot be empty")
        if type(self.random_seed) is not int or not 0 <= self.random_seed <= 0xFFFFFFFF:
            raise KernelCompileError("kernel emitter random_seed must be an unsigned 32-bit integer")
        if len({stable_id for stable_id, _type, _default in self.attributes}) != len(self.attributes):
            raise KernelCompileError("kernel emitter attribute stable ids must be unique")
        if not all(
            isinstance(interface, (VectorField, SdfVolume))
            for interface in interfaces
        ):
            raise KernelCompileError("kernel emitter data interfaces are invalid")
        interfaces = tuple(sorted(interfaces, key=lambda value: value.stable_id))
        object.__setattr__(self, "data_interfaces", interfaces)
        flows = tuple(self.flows)
        object.__setattr__(self, "flows", flows)
        suspensions = tuple(self.suspensions)
        object.__setattr__(self, "suspensions", suspensions)
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
        if not all(isinstance(item, KernelLifecycleFlow) for item in flows):
            raise KernelCompileError("kernel emitter lifecycle flow metadata is invalid")
        flow_by_stage = {flow.lifecycle_stage: flow for flow in flows}
        if len(flow_by_stage) != len(flows):
            raise KernelCompileError("kernel emitter lifecycle stages must be unique")
        mandatory_stages = {
            ParticleStage.INIT,
            ParticleStage.UPDATE,
            ParticleStage.RENDERING,
        }
        if not mandatory_stages.issubset(flow_by_stage):
            raise KernelCompileError("kernel emitter is missing a mandatory lifecycle flow")
        lifecycle_order = {
            ParticleStage.INIT: 0,
            ParticleStage.UPDATE: 1,
            ParticleStage.COLLISION_ENTER: 2,
            ParticleStage.COLLISION_STAY: 3,
            ParticleStage.COLLISION_EXIT: 4,
            ParticleStage.RENDERING: 5,
        }
        if tuple(flow.lifecycle_stage for flow in flows) != tuple(
            sorted(flow_by_stage, key=lifecycle_order.__getitem__)
        ):
            raise KernelCompileError("kernel emitter lifecycle flows are not canonically ordered")
        function_by_kernel_stage = {
            KernelStage.INIT: self.init,
            KernelStage.UPDATE: self.update,
            KernelStage.RENDERING: self.rendering,
        }
        for flow in flows:
            function = function_by_kernel_stage[flow.kernel_stage]
            for block in flow.blocks:
                if block.instruction_end > len(function.instructions):
                    raise KernelCompileError(
                        "kernel flow block instruction range exceeds its function"
                    )
        if not all(isinstance(item, KernelSuspensionPoint) for item in suspensions):
            raise KernelCompileError("kernel emitter suspension metadata is invalid")
        if len({item.resume_program_counter for item in suspensions}) != len(suspensions):
            raise KernelCompileError(
                "kernel emitter continuation program counters must be unique"
            )
        function_by_stage = {
            ParticleStage.INIT: self.init,
            ParticleStage.UPDATE: self.update,
            ParticleStage.COLLISION_ENTER: self.update,
            ParticleStage.COLLISION_STAY: self.update,
            ParticleStage.COLLISION_EXIT: self.update,
            ParticleStage.RENDERING: self.rendering,
        }
        for suspension in suspensions:
            function = function_by_stage.get(suspension.lifecycle_stage)
            flow = flow_by_stage.get(suspension.lifecycle_stage)
            if (
                function is None
                or flow is None
                or suspension.suspend_instruction_index >= len(function.instructions)
            ):
                raise KernelCompileError("kernel suspension instruction target is invalid")
            instruction = function.instructions[suspension.suspend_instruction_index]
            expected_opcode = {
                ParticleSuspensionKind.FRAMES: "suspend_frames",
                ParticleSuspensionKind.SECONDS: "suspend_seconds",
                ParticleSuspensionKind.UNTIL_FRAMES: "until_frames",
                ParticleSuspensionKind.UNTIL_SECONDS: "until_seconds",
            }[suspension.kind]
            if (
                instruction.opcode != expected_opcode
                or instruction.source.node_uid != suspension.source_node_uid
                or instruction.immediate_dict()["resume_program_counter"]
                != suspension.resume_program_counter
            ):
                raise KernelCompileError(
                    "kernel suspension metadata does not match its instruction"
                )
            block_by_node = {
                block.source_node_uid: block
                for block in flow.blocks
            }
            source_block = next(
                (
                    block
                    for block in flow.blocks
                    if block.source_node_uid == suspension.source_node_uid
                ),
                None,
            )
            resume_block = block_by_node.get(suspension.resume_node_uid)
            terminal = (
                suspension.resume_instruction_index == -1
                and suspension.resume_node_uid == ""
            )
            if (
                source_block is None
                or source_block.lane_index != suspension.lane_index
                or flow.lanes[suspension.lane_index].stable_id
                != suspension.lane_stable_id
                or not (
                    source_block.instruction_begin
                    <= suspension.suspend_instruction_index
                    < source_block.instruction_end
                )
                or (
                    not terminal
                    and (
                        resume_block is None
                        or resume_block.source_node_uid
                        != suspension.resume_node_uid
                        or resume_block.instruction_begin
                        != suspension.resume_instruction_index
                    )
                )
            ):
                raise KernelCompileError(
                    "kernel suspension metadata is inconsistent with lifecycle flow"
                )

    def _validate_data_interface_access(self, function: ParticleKernelFunction) -> None:
        interfaces = {interface.stable_id: interface for interface in self.data_interfaces}
        for instruction in function.instructions:
            if instruction.opcode not in {
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
                VectorField
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
            if instruction.opcode == "collide_scene":
                immediate = instruction.immediate_dict()
                writes.update(
                    stable_id
                    for stable_id in (
                        immediate["position_attribute"],
                        immediate["velocity_attribute"],
                        immediate["hit_attribute"],
                        immediate["normal_attribute"],
                    )
                    if stable_id
                )
                continue
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
            "flows": [
                flow.to_dict(include_source=include_source) for flow in self.flows
            ],
            "suspensions": [
                suspension.to_dict(include_source=include_source)
                for suspension in self.suspensions
            ],
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
                "flows",
                "suspensions",
            },
            "kernel emitter",
        )
        if (
            type(value["attributes"]) is not list
            or type(value["data_interfaces"]) is not list
            or type(value["flows"]) is not list
            or type(value["suspensions"]) is not list
        ):
            raise KernelCompileError(
                "kernel emitter attributes, data interfaces, flows, and suspensions must be arrays"
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
            tuple(KernelLifecycleFlow.from_dict(item) for item in value["flows"]),
            tuple(
                particle_data_interface_from_dict(
                    item, f"kernel emitter data_interfaces[{index}]"
                )
                for index, item in enumerate(value["data_interfaces"])
            ),
            tuple(
                KernelSuspensionPoint.from_dict(item)
                for item in value["suspensions"]
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
class KernelParameter:
    stable_id: str
    name: str
    value_type: TypeRef
    default: Any
    exposed: bool
    slot: int

    def __post_init__(self) -> None:
        if type(self.stable_id) is not str or not self.stable_id:
            raise KernelCompileError("kernel parameter stable_id cannot be empty")
        if type(self.name) is not str or not self.name:
            raise KernelCompileError("kernel parameter name cannot be empty")
        if not isinstance(self.value_type, TypeRef):
            raise KernelCompileError("kernel parameter type is invalid")
        if type(self.exposed) is not bool or type(self.slot) is not int or self.slot < 0:
            raise KernelCompileError("kernel parameter runtime metadata is invalid")
        _finite_json(self.default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "name": self.name,
            "type": self.value_type.to_dict(),
            "default": self.default,
            "exposed": self.exposed,
            "slot": self.slot,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "KernelParameter":
        _exact_dict(
            value,
            {"stable_id", "name", "type", "default", "exposed", "slot"},
            "kernel parameter",
        )
        return cls(
            value["stable_id"],
            value["name"],
            TypeRef.from_dict(value["type"]),
            value["default"],
            value["exposed"],
            value["slot"],
        )


@dataclass(frozen=True)
class ParticleKernelProgram:
    source_behavior_hash: str
    kernel_hash: str
    parameters: tuple[KernelParameter, ...]
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
        if not all(isinstance(parameter, KernelParameter) for parameter in self.parameters):
            raise KernelCompileError("particle kernel parameters are invalid")
        if tuple(parameter.slot for parameter in self.parameters) != tuple(range(len(self.parameters))):
            raise KernelCompileError("particle kernel parameter slots must be dense and ordered")
        if len({parameter.stable_id for parameter in self.parameters}) != len(self.parameters):
            raise KernelCompileError("particle kernel parameter ids must be unique")
        if len({emitter.stable_id for emitter in self.emitters}) != len(self.emitters):
            raise KernelCompileError("particle kernel emitter stable ids must be unique")
        if not isinstance(self.events, KernelEventABI):
            raise KernelCompileError("particle kernel event ABI is invalid")
        if not isinstance(self.contract, KernelRuntimeContract):
            raise KernelCompileError("particle kernel runtime contract is invalid")
        self._validate_parameter_access()
        self._validate_event_access()
        if self.kernel_hash != _kernel_semantic_hash(
            self.parameters, self.emitters, self.events, self.contract
        ):
            raise KernelCompileError("particle kernel hash does not match its semantic payload")

    def _validate_parameter_access(self) -> None:
        parameters = {
            parameter.stable_id: parameter.value_type
            for parameter in self.parameters
        }
        for emitter in self.emitters:
            for function in (emitter.init, emitter.update, emitter.rendering):
                for instruction in function.instructions:
                    if instruction.opcode != "load_parameter":
                        continue
                    stable_id = instruction.immediate_dict()["parameter"]
                    if parameters.get(stable_id) != instruction.result_type:
                        raise KernelCompileError(
                            f"kernel references unknown or mismatched parameter {stable_id!r}"
                        )

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
            "parameters": [parameter.to_dict() for parameter in self.parameters],
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
                "parameters",
                "contract",
                "events",
                "emitters",
            },
            "particle kernel program",
        )
        if value["$schema"] != KERNEL_IR_SCHEMA:
            raise KernelCompileError("particle kernel schema is unsupported")
        if type(value["emitters"]) is not list or type(value["parameters"]) is not list:
            raise KernelCompileError("particle kernel collections must be arrays")
        try:
            contract = KernelRuntimeContract.from_dict(value["contract"])
        except KernelSemanticError as exc:
            raise KernelCompileError(str(exc)) from exc
        return cls(
            value["source_behavior_hash"],
            value["kernel_hash"],
            tuple(KernelParameter.from_dict(item) for item in value["parameters"]),
            tuple(ParticleEmitterKernelIR.from_dict(item) for item in value["emitters"]),
            KernelEventABI.from_dict(value["events"]),
            contract,
        )


def _authored_source_ref(
    stage: ParticleStageHIR,
    node_uid: str,
    port_id: str = "",
    operation: str = "",
) -> KernelSourceRef:
    location = stage.source_location(node_uid)
    return KernelSourceRef(
        node_uid,
        port_id,
        operation,
        location.source_name,
        location.line,
        location.column,
        location.end_line,
        location.end_column,
    )


class ParticleKernelLowerer:
    """Lower Particle Program HIR into backend-neutral executable kernels."""

    def lower(self, program: ParticleProgramHIR) -> ParticleKernelProgram:
        parameters = tuple(
            KernelParameter(
                parameter.stable_id,
                parameter.name,
                parameter.value_type,
                parameter.default,
                parameter.exposed,
                parameter.slot,
            )
            for parameter in program.parameters
        )
        parameter_types = {
            parameter.stable_id: parameter.value_type
            for parameter in parameters
        }
        routes = {
            route.stable_id: (index, route)
            for index, route in enumerate(program.events.routes)
        }
        event_types = {
            event_type.type_index: event_type
            for event_type in program.events.event_types
        }
        emitters = tuple(
            self._lower_emitter(emitter, routes, event_types, parameter_types)
            for emitter in program.emitters
        )
        events = _lower_event_abi(program.events)
        contract = KernelRuntimeContract()
        return ParticleKernelProgram(
            program.behavior_hash,
            _kernel_semantic_hash(parameters, emitters, events, contract),
            parameters,
            emitters,
            events,
            contract,
        )

    def _lower_emitter(
        self,
        emitter: ParticleEmitterHIR,
        routes: Mapping[str, tuple[int, ParticleEventRouteHIR]],
        event_types: Mapping[int, ParticleEventTypeHIR],
        parameter_types: Mapping[str, TypeRef],
    ) -> ParticleEmitterKernelIR:
        schema = tuple(
            (attribute.stable_id, attribute.value_type, attribute.default)
            for attribute in emitter.attributes
        )
        types = {stable_id: value_type for stable_id, value_type, _default in schema}
        defaults = {stable_id: default for stable_id, _value_type, default in schema}
        lifecycle_stages = tuple(
            stage
            for stage in (
                emitter.init,
                emitter.update,
                emitter.collision_enter,
                emitter.collision_stay,
                emitter.collision_exit,
                emitter.rendering,
            )
            if stage is not None
        )
        continuation_program_counters = {}
        suspension_sources = []
        for program_counter, (stage, suspension) in enumerate(
            (
                (stage, item)
                for stage in lifecycle_stages
                for item in stage.flow.suspensions
            ),
            start=1,
        ):
            key = (stage.stage, suspension.node_uid)
            continuation_program_counters[key] = program_counter
            suspension_sources.append((stage, suspension, program_counter))
        operation_instruction_ranges = {}
        init = self._lower_init(
            emitter,
            types,
            defaults,
            routes,
            event_types,
            parameter_types,
            continuation_program_counters,
            operation_instruction_ranges,
        )
        update = self._lower_update(
            emitter,
            types,
            routes,
            event_types,
            parameter_types,
            continuation_program_counters,
            operation_instruction_ranges,
        )
        rendering = self._lower_rendering(
            emitter,
            types,
            routes,
            event_types,
            parameter_types,
            continuation_program_counters,
            operation_instruction_ranges,
        )
        flows = tuple(
            self._lower_lifecycle_flow(
                stage,
                {
                    ParticleStage.INIT: init,
                    ParticleStage.UPDATE: update,
                    ParticleStage.COLLISION_ENTER: update,
                    ParticleStage.COLLISION_STAY: update,
                    ParticleStage.COLLISION_EXIT: update,
                    ParticleStage.RENDERING: rendering,
                }[stage.stage],
                operation_instruction_ranges,
            )
            for stage in lifecycle_stages
        )
        flow_by_stage = {flow.lifecycle_stage: flow for flow in flows}
        instruction_locations = {}
        for function in (init, update, rendering):
            for instruction_index, instruction in enumerate(function.instructions):
                if instruction.opcode not in {
                    "suspend_frames",
                    "suspend_seconds",
                    "until_frames",
                    "until_seconds",
                }:
                    continue
                lifecycle_stage = ParticleStage(
                    instruction.immediate_dict()["lifecycle_stage"]
                )
                instruction_locations[(lifecycle_stage, instruction.source.node_uid)] = (
                    instruction_index
                )
        suspensions = tuple(
            KernelSuspensionPoint(
                stage.stage,
                suspension.kind,
                suspension.lane_index,
                suspension.lane_stable_id,
                program_counter,
                suspension.resume_program_counter,
                (
                    next(
                        block.instruction_begin
                        for block in flow_by_stage[stage.stage].blocks
                        if block.source_node_uid == suspension.resume_node_uid
                    )
                    if suspension.resume_node_uid
                    else -1
                ),
                instruction_locations[(stage.stage, suspension.node_uid)],
                suspension.node_uid,
                suspension.resume_node_uid,
            )
            for stage, suspension, program_counter in suspension_sources
        )
        return ParticleEmitterKernelIR(
            emitter.stable_id,
            emitter.settings.seed,
            schema,
            init,
            update,
            rendering,
            flows,
            emitter.data_interfaces,
            suspensions,
        )

    @staticmethod
    def _lower_lifecycle_flow(
        stage_hir: ParticleStageHIR,
        function: ParticleKernelFunction,
        operation_instruction_ranges: Mapping[
            tuple[ParticleStage, str], tuple[int, int]
        ],
    ) -> KernelLifecycleFlow:
        block_ranges: list[tuple[int, int] | None] = []
        for block in stage_hir.flow.blocks:
            ranges = []
            for operation in block.operations:
                instruction_range = operation_instruction_ranges.get(
                    (stage_hir.stage, operation.source_node_uid)
                )
                if instruction_range is None:
                    raise KernelCompileError(
                        "kernel lifecycle operation "
                        f"{stage_hir.stage.value}:{operation.source_node_uid!r} "
                        "has no instruction range"
                    )
                ranges.append(instruction_range)
            block_ranges.append(
                (ranges[0][0], ranges[-1][1]) if ranges else None
            )
        next_instruction = len(function.instructions)
        resolved_ranges = [None] * len(block_ranges)
        for index in range(len(block_ranges) - 1, -1, -1):
            instruction_range = block_ranges[index]
            if instruction_range is None:
                resolved_ranges[index] = (next_instruction, next_instruction)
            else:
                resolved_ranges[index] = instruction_range
                next_instruction = instruction_range[0]
        blocks = tuple(
            KernelFlowBlock(
                block.node_uid,
                block.lane_index,
                resolved_ranges[index][0],
                resolved_ranges[index][1],
            )
            for index, block in enumerate(stage_hir.flow.blocks)
        )
        return KernelLifecycleFlow(
            stage_hir.stage,
            function.stage,
            stage_hir.flow.entry_node_uid,
            tuple(
                KernelExecutionLane(
                    lane.stable_id,
                    lane.index,
                    lane.parent_index,
                    lane.source_node_uid,
                    lane.source_port_id,
                )
                for lane in stage_hir.flow.lanes
            ),
            blocks,
            tuple(
                KernelJoinAll(
                    join.node_uid,
                    join.input_lane_indices,
                    join.output_lane_index,
                )
                for join in stage_hir.flow.joins
            ),
        )

    @staticmethod
    def _lower_suspension(
        builder,
        stage_hir,
        operation,
        expression_values,
        source,
        continuation_program_counters,
    ) -> None:
        suspension = next(
            (
                item
                for item in stage_hir.flow.suspensions
                if item.node_uid == operation.source_node_uid
            ),
            None,
        )
        if suspension is None:
            raise KernelCompileError(
                f"Wait operation {operation.source_node_uid!r} has no HIR suspension descriptor"
            )
        program_counter = continuation_program_counters.get(
            (stage_hir.stage, operation.source_node_uid)
        )
        if program_counter is None:
            raise KernelCompileError(
                f"Wait operation {operation.source_node_uid!r} has no emitter continuation program counter"
            )
        parameter_name = "frames" if operation.opcode.endswith("_frames") else "seconds"
        value_type = TypeRef(
            ValueType.I32
            if operation.opcode.endswith("_frames")
            else ValueType.F32
        )
        duration = builder.operation_value(
            parameter_name,
            dict(operation.value_bindings),
            expression_values,
            operation.parameter_dict(),
            value_type,
            source,
        )
        builder.emit_void(
            {
                "control.wait_frames": "suspend_frames",
                "control.wait_seconds": "suspend_seconds",
                "control.until_frames": "until_frames",
                "control.until_seconds": "until_seconds",
            }[operation.opcode],
            (duration,),
            {
                "lifecycle_stage": stage_hir.stage.value,
                "lane_index": suspension.lane_index,
                "lane_stable_id": suspension.lane_stable_id,
                "resume_program_counter": program_counter,
            },
            source,
        )

    @staticmethod
    def _lower_operation_expressions(builder, stage, operation) -> dict[str, str]:
        required_outputs = {
            result_id
            for _property_id, result_id in operation.value_bindings
        }
        required_outputs.update(
            predicate.value_id
            for predicate in operation.execution_predicates
            if predicate.value_id
        )
        return builder.lower_expressions(stage, required_outputs)

    @staticmethod
    def _lower_attribute_modification(
        builder,
        operation,
        expression_values,
        attribute_types,
        source,
    ) -> None:
        parameters = operation.parameter_dict()
        if operation.opcode == "attribute.modify_cache":
            stable_id = str(parameters["attribute"])
            property_name = "value"
            degrees_input = False
        else:
            stable_id, property_name, degrees_input = ATTRIBUTE_OPERATION_SPECS[
                operation.opcode
            ]
        value = builder.operation_value(
            property_name,
            dict(operation.value_bindings),
            expression_values,
            parameters,
            attribute_types[stable_id],
            source,
        )
        if degrees_input:
            value = builder.emit(
                "multiply",
                attribute_types[stable_id],
                (
                    value,
                    builder.constant(TypeRef(ValueType.F32), math.pi / 180.0, source),
                ),
                {},
                source,
            )
        composition = str(parameters.get("composition", "set"))
        if composition != "set":
            current = builder.load(stable_id, source)
            value = builder.emit(
                {"add": "add", "multiply": "multiply"}[composition],
                attribute_types[stable_id],
                (current, value),
                {},
                source,
            )
        builder.store(stable_id, value, source)

    @staticmethod
    def _integrate_update_position(builder, attribute_types, delta_time) -> None:
        source = KernelSourceRef(operation="update.integrate_position")
        position = builder.load("builtin.position", source)
        velocity = builder.load("builtin.velocity", source)
        displacement = builder.emit(
            "multiply",
            attribute_types["builtin.position"],
            (velocity, delta_time),
            {},
            source,
        )
        position = builder.emit(
            "add",
            attribute_types["builtin.position"],
            (position, displacement),
            {},
            source,
        )
        builder.store("builtin.position", position, source)

    @staticmethod
    def _lower_update_collision(
        builder,
        operation,
        expression_values,
        attribute_types,
        source,
    ) -> None:
        parameters = operation.parameter_dict()
        bindings = dict(operation.value_bindings)
        position = builder.load("builtin.position", source)
        velocity = builder.load("builtin.velocity", source)
        if operation.opcode == "collision.scene":
            particle_radius = builder.operation_value(
                "particle_radius",
                bindings,
                expression_values,
                parameters,
                TypeRef(ValueType.F32),
                source,
            )
            layer_mask = builder.operation_value(
                "layer_mask",
                bindings,
                expression_values,
                parameters,
                TypeRef(ValueType.U32),
                source,
            )
            include_triggers = builder.operation_value(
                "include_triggers",
                bindings,
                expression_values,
                parameters,
                TypeRef(ValueType.BOOL),
                source,
            )
            restitution_scale = builder.operation_value(
                "restitution_scale",
                bindings,
                expression_values,
                parameters,
                TypeRef(ValueType.F32),
                source,
            )
            friction_scale = builder.operation_value(
                "friction_scale",
                bindings,
                expression_values,
                parameters,
                TypeRef(ValueType.F32),
                source,
            )
            builder.emit_void(
                "collide_scene",
                (
                    position,
                    velocity,
                    particle_radius,
                    layer_mask,
                    include_triggers,
                    restitution_scale,
                    friction_scale,
                ),
                {
                    "position_attribute": "builtin.position",
                    "velocity_attribute": "builtin.velocity",
                    "hit_attribute": (
                        "builtin.collision_hit"
                        if "builtin.collision_hit" in attribute_types
                        else ""
                    ),
                    "normal_attribute": (
                        "builtin.collision_normal"
                        if "builtin.collision_normal" in attribute_types
                        else ""
                    ),
                },
                source,
            )
            builder.written_attributes.update(
                {"builtin.position", "builtin.velocity"}
            )
            return
        if operation.opcode == "collision.plane":
            point = builder.operation_value(
                "point",
                bindings,
                expression_values,
                parameters,
                attribute_types["builtin.position"],
                source,
            )
            normal = builder.operation_value(
                "normal",
                bindings,
                expression_values,
                parameters,
                attribute_types["builtin.position"],
                source,
            )
            radius = builder.operation_value(
                "radius",
                bindings,
                expression_values,
                parameters,
                TypeRef(ValueType.F32),
                source,
            )
            collision_operands = (position, velocity, point, normal, radius)
        elif operation.opcode == "collision.sphere":
            center = builder.operation_value(
                "center",
                bindings,
                expression_values,
                parameters,
                attribute_types["builtin.position"],
                source,
            )
            sphere_radius = builder.operation_value(
                "sphere_radius",
                bindings,
                expression_values,
                parameters,
                TypeRef(ValueType.F32),
                source,
            )
            particle_radius = builder.operation_value(
                "particle_radius",
                bindings,
                expression_values,
                parameters,
                TypeRef(ValueType.F32),
                source,
            )
            collision_operands = (
                position,
                velocity,
                center,
                sphere_radius,
                particle_radius,
            )
        elif operation.opcode == "collision.sdf":
            particle_radius = builder.operation_value(
                "particle_radius",
                bindings,
                expression_values,
                parameters,
                TypeRef(ValueType.F32),
                source,
            )
            collision_operands = (position, velocity, particle_radius)
        else:
            raise KernelCompileError(
                f"unsupported Update collision operation {operation.opcode!r}"
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

    @staticmethod
    def _prepare_collision_lifecycle(builder, attribute_types) -> None:
        source = KernelSourceRef(operation="update.collision_lifecycle")
        previous_active = builder.load("builtin.collision_active", source)
        position = builder.load("builtin.position", source)
        velocity = builder.load("builtin.velocity", source)
        size = builder.load("builtin.size", source)
        half = builder.constant(TypeRef(ValueType.F32), 0.5, source)
        radius = builder.emit(
            "multiply", TypeRef(ValueType.F32), (size, half), {}, source
        )
        layer_mask = builder.constant(TypeRef(ValueType.U32), 0xFFFFFFFF, source)
        include_triggers = builder.constant(TypeRef(ValueType.BOOL), False, source)
        one = builder.constant(TypeRef(ValueType.F32), 1.0, source)
        builder.emit_void(
            "collide_scene",
            (
                position,
                velocity,
                radius,
                layer_mask,
                include_triggers,
                one,
                one,
            ),
            {
                "position_attribute": "builtin.position",
                "velocity_attribute": "builtin.velocity",
                "hit_attribute": "builtin.collision_hit",
                "normal_attribute": "builtin.collision_normal",
            },
            source,
        )
        builder.written_attributes.update(
            {
                "builtin.position",
                "builtin.velocity",
                "builtin.collision_hit",
                "builtin.collision_normal",
            }
        )
        current_active = builder.load("builtin.collision_hit", source)
        not_previous = builder.emit(
            "logical_not",
            TypeRef(ValueType.BOOL),
            (previous_active,),
            {},
            source,
        )
        entered = builder.emit(
            "logical_and",
            TypeRef(ValueType.BOOL),
            (current_active, not_previous),
            {},
            source,
        )
        stayed = builder.emit(
            "logical_and",
            TypeRef(ValueType.BOOL),
            (current_active, previous_active),
            {},
            source,
        )
        not_current = builder.emit(
            "logical_not",
            TypeRef(ValueType.BOOL),
            (current_active,),
            {},
            source,
        )
        exited = builder.emit(
            "logical_and",
            TypeRef(ValueType.BOOL),
            (not_current, previous_active),
            {},
            source,
        )
        builder.set_runtime_predicate("collision_enter", entered)
        builder.set_runtime_predicate("collision_stay", stayed)
        builder.set_runtime_predicate("collision_exit", exited)
        builder.store("builtin.collision_active", current_active, source)

    def _lower_init(
        self,
        emitter,
        attribute_types,
        defaults,
        routes,
        event_types,
        parameter_types,
        continuation_program_counters,
        operation_instruction_ranges,
    ) -> ParticleKernelFunction:
        builder = _KernelBuilder(KernelStage.INIT, attribute_types, parameter_types)
        for stable_id in sorted(defaults):
            if stable_id == "builtin.id":
                continue
            value = builder.constant(
                attribute_types[stable_id],
                defaults[stable_id],
                KernelSourceRef(operation="attribute.default"),
            )
            builder.store(stable_id, value, KernelSourceRef(operation="attribute.default"))

        for operation in emitter.init.flow.iter_operations():
            instruction_begin = len(builder.instructions)
            source = _authored_source_ref(
                emitter.init,
                operation.source_node_uid,
                operation=f"init.{operation.opcode}",
            )
            expression_values = self._lower_operation_expressions(
                builder, emitter.init, operation
            )
            parameters = operation.parameter_dict()
            bindings = dict(operation.value_bindings)
            if operation.opcode in {"control.if", "control.join_all"}:
                operation_instruction_ranges[
                    (emitter.init.stage, operation.source_node_uid)
                ] = (instruction_begin, len(builder.instructions))
                continue
            guard = builder.execution_guard(operation, expression_values, source)
            if operation.opcode != "event.emit":
                builder.begin_guard(guard, source)
            if operation.opcode in {
                "control.wait_frames",
                "control.wait_seconds",
                "control.until_frames",
                "control.until_seconds",
            }:
                self._lower_suspension(
                    builder,
                    emitter.init,
                    operation,
                    expression_values,
                    source,
                    continuation_program_counters,
                )
            elif operation.opcode == "emitter.sample_shape":
                shape_parameters = {
                    "shape": parameters["shape"],
                    "shape_space": parameters["shape_space"],
                    "radius": parameters["shape_radius"],
                    "angle_degrees": parameters["shape_angle_degrees"],
                    "dimensions": parameters["shape_dimensions"],
                    "mesh": parameters["shape_mesh"],
                    "mesh_mode": parameters["shape_mesh_mode"],
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
            elif operation.opcode in ATTRIBUTE_OPERATION_SPECS or operation.opcode == "attribute.modify_cache":
                self._lower_attribute_modification(
                    builder,
                    operation,
                    expression_values,
                    attribute_types,
                    source,
                )
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
            if operation.opcode != "event.emit":
                builder.end_guard(guard, source)
            operation_instruction_ranges[
                (emitter.init.stage, operation.source_node_uid)
            ] = (instruction_begin, len(builder.instructions))
        return builder.finish()

    def _lower_update(
        self,
        emitter,
        attribute_types,
        routes,
        event_types,
        parameter_types,
        continuation_program_counters,
        operation_instruction_ranges,
    ) -> ParticleKernelFunction:
        builder = _KernelBuilder(KernelStage.UPDATE, attribute_types, parameter_types)
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
        if "builtin.collision_hit" in attribute_types:
            builder.store(
                "builtin.collision_hit",
                builder.constant(
                    TypeRef(ValueType.BOOL),
                    False,
                    KernelSourceRef(operation="update.reset_collision"),
                ),
                KernelSourceRef(operation="update.reset_collision"),
            )
        if "builtin.collision_normal" in attribute_types:
            builder.store(
                "builtin.collision_normal",
                builder.constant(
                    attribute_types["builtin.collision_normal"],
                    [0.0, 0.0, 0.0],
                    KernelSourceRef(operation="update.reset_collision"),
                ),
                KernelSourceRef(operation="update.reset_collision"),
            )
        collision_opcodes = {
            "collision.plane",
            "collision.sphere",
            "collision.sdf",
            "collision.scene",
        }
        operation_stream = tuple(
            (emitter.update, operation)
            for operation in emitter.update.flow.iter_operations()
        ) + tuple(
            (lifecycle, operation)
            for lifecycle in (
                emitter.collision_enter,
                emitter.collision_stay,
                emitter.collision_exit,
            )
            if lifecycle is not None
            for operation in lifecycle.flow.iter_operations()
        )
        position_integrated = False
        collision_lifecycle_ready = False

        for stage_hir, operation in operation_stream:
            if operation.opcode in collision_opcodes and not position_integrated:
                self._integrate_update_position(builder, attribute_types, delta_time)
                position_integrated = True
            uses_collision_lifecycle = any(
                predicate.runtime_condition.startswith("collision_")
                for predicate in operation.execution_predicates
            )
            if uses_collision_lifecycle and not collision_lifecycle_ready:
                if not position_integrated:
                    self._integrate_update_position(builder, attribute_types, delta_time)
                    position_integrated = True
                self._prepare_collision_lifecycle(builder, attribute_types)
                collision_lifecycle_ready = True
            instruction_begin = len(builder.instructions)
            source = _authored_source_ref(
                stage_hir,
                operation.source_node_uid,
                operation=f"{stage_hir.stage.value}.{operation.opcode}",
            )
            expression_values = self._lower_operation_expressions(
                builder, stage_hir, operation
            )
            parameters = operation.parameter_dict()
            bindings = dict(operation.value_bindings)
            if operation.opcode in {"control.if", "control.join_all"}:
                operation_instruction_ranges[
                    (stage_hir.stage, operation.source_node_uid)
                ] = (instruction_begin, len(builder.instructions))
                continue
            guard = builder.execution_guard(operation, expression_values, source)
            if operation.opcode != "event.emit":
                builder.begin_guard(guard, source)
            if operation.opcode in {
                "control.wait_frames",
                "control.wait_seconds",
                "control.until_frames",
                "control.until_seconds",
            }:
                self._lower_suspension(
                    builder,
                    stage_hir,
                    operation,
                    expression_values,
                    source,
                    continuation_program_counters,
                )
            elif operation.opcode in ATTRIBUTE_OPERATION_SPECS or operation.opcode == "attribute.modify_cache":
                self._lower_attribute_modification(
                    builder,
                    operation,
                    expression_values,
                    attribute_types,
                    source,
                )
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
            elif operation.opcode in collision_opcodes:
                self._lower_update_collision(
                    builder,
                    operation,
                    expression_values,
                    attribute_types,
                    source,
                )
            else:
                raise KernelCompileError(f"unsupported Update operation {operation.opcode!r}")
            if operation.opcode != "event.emit":
                builder.end_guard(guard, source)
            operation_instruction_ranges[
                (stage_hir.stage, operation.source_node_uid)
            ] = (instruction_begin, len(builder.instructions))

        if emitter.settings.collision_enabled and not collision_lifecycle_ready:
            if not position_integrated:
                self._integrate_update_position(builder, attribute_types, delta_time)
                position_integrated = True
            self._prepare_collision_lifecycle(builder, attribute_types)
            collision_lifecycle_ready = True
        if not position_integrated:
            self._integrate_update_position(builder, attribute_types, delta_time)
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
        parameter_types,
        continuation_program_counters,
        operation_instruction_ranges,
    ) -> ParticleKernelFunction:
        builder = _KernelBuilder(KernelStage.RENDERING, attribute_types, parameter_types)
        for operation in emitter.rendering.flow.iter_operations():
            instruction_begin = len(builder.instructions)
            source = _authored_source_ref(
                emitter.rendering,
                operation.source_node_uid,
                operation=f"rendering.{operation.opcode}",
            )
            expression_values = self._lower_operation_expressions(
                builder, emitter.rendering, operation
            )
            if operation.opcode in {"control.if", "control.join_all"} or operation.opcode.startswith(
                "render."
            ):
                operation_instruction_ranges[
                    (emitter.rendering.stage, operation.source_node_uid)
                ] = (instruction_begin, len(builder.instructions))
                continue
            guard = builder.execution_guard(operation, expression_values, source)
            if operation.opcode != "event.emit":
                builder.begin_guard(guard, source)
            if operation.opcode in {
                "control.wait_frames",
                "control.wait_seconds",
                "control.until_frames",
                "control.until_seconds",
            }:
                self._lower_suspension(
                    builder,
                    emitter.rendering,
                    operation,
                    expression_values,
                    source,
                    continuation_program_counters,
                )
            elif operation.opcode in ATTRIBUTE_OPERATION_SPECS or operation.opcode == "attribute.modify_cache":
                self._lower_attribute_modification(
                    builder,
                    operation,
                    expression_values,
                    attribute_types,
                    source,
                )
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
                raise KernelCompileError(
                    f"unsupported Rendering operation {operation.opcode!r}"
                )
            if operation.opcode != "event.emit":
                builder.end_guard(guard, source)
            operation_instruction_ranges[
                (emitter.rendering.stage, operation.source_node_uid)
            ] = (instruction_begin, len(builder.instructions))
        for stable_id in (
            "builtin.position",
            "builtin.velocity",
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
        guard = builder.execution_guard(operation, expression_values, source)
        if guard:
            condition = builder.emit(
                "logical_and",
                TypeRef(ValueType.BOOL),
                (condition, guard),
                {},
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
    def __init__(
        self,
        stage: KernelStage,
        attribute_types: Mapping[str, TypeRef],
        parameter_types: Mapping[str, TypeRef],
    ) -> None:
        self.stage = stage
        self.attribute_types = dict(attribute_types)
        self.parameter_types = dict(parameter_types)
        self.instructions: list[KernelInstruction] = []
        self.read_attributes: set[str] = set()
        self.written_attributes: set[str] = set()
        self._value_types: dict[str, TypeRef] = {}
        self._runtime_predicates: dict[str, str] = {}
        self._random_slot = 0

    def set_runtime_predicate(self, name: str, value: str) -> None:
        if self._value_types.get(value) != TypeRef(ValueType.BOOL):
            raise KernelCompileError(
                f"runtime predicate {name!r} must reference a bool value"
            )
        self._runtime_predicates[str(name)] = value

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

    def execution_guard(
        self,
        operation,
        expression_values: Mapping[str, str],
        source: KernelSourceRef,
    ) -> str:
        values = []
        for predicate in operation.execution_predicates:
            if predicate.runtime_condition:
                try:
                    value = self._runtime_predicates[predicate.runtime_condition]
                except KeyError as exc:
                    raise KernelCompileError(
                        f"runtime execution predicate {predicate.runtime_condition!r} "
                        "is unavailable"
                    ) from exc
            elif predicate.value_id:
                try:
                    value = expression_values[predicate.value_id]
                except KeyError as exc:
                    raise KernelCompileError(
                        f"execution predicate {predicate.value_id!r} is unavailable"
                    ) from exc
            else:
                value = self.constant(
                    TypeRef(ValueType.BOOL), predicate.literal, source
                )
            if self._value_types[value] != TypeRef(ValueType.BOOL):
                raise KernelCompileError("particle execution predicate must be bool")
            if not predicate.expected:
                value = self.emit(
                    "logical_not",
                    TypeRef(ValueType.BOOL),
                    (value,),
                    {},
                    source,
                )
            values.append(value)
        if not values:
            return ""
        result = values[0]
        for value in values[1:]:
            result = self.emit(
                "logical_and",
                TypeRef(ValueType.BOOL),
                (result, value),
                {},
                source,
            )
        return result

    def begin_guard(self, guard: str, source: KernelSourceRef) -> None:
        if guard:
            self.emit_void("begin_if", (guard,), {}, source)

    def end_guard(self, guard: str, source: KernelSourceRef) -> None:
        if guard:
            self.emit_void("end_if", (), {}, source)

    def lower_expressions(
        self,
        stage: ParticleStageHIR,
        required_outputs: set[str] | None = None,
    ) -> dict[str, str]:
        instructions = stage.expressions.instructions
        if required_outputs is not None:
            by_result = {
                instruction.result_id: instruction
                for instruction in instructions
            }
            required = set()

            def include(result_id: str) -> None:
                if result_id in required:
                    return
                instruction = by_result.get(result_id)
                if instruction is None:
                    raise KernelCompileError(
                        f"expression output {result_id!r} is unavailable"
                    )
                required.add(result_id)
                for operand in instruction.operands:
                    if operand.value_id:
                        include(operand.value_id)

            for result_id in required_outputs:
                include(result_id)
            instructions = tuple(
                instruction
                for instruction in instructions
                if instruction.result_id in required
            )
        lowered: dict[str, str] = {}
        for instruction in instructions:
            source = _authored_source_ref(
                stage,
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
            elif instruction.opcode == "load_parameter":
                stable_id = str(instruction.immediate_dict()["parameter"])
                value_type = self.parameter_types.get(stable_id)
                if value_type is None or value_type != instruction.result_type:
                    raise KernelCompileError(
                        f"expression parameter type mismatch for {stable_id!r}"
                    )
                value = self.emit(
                    "load_parameter",
                    value_type,
                    (),
                    {"parameter": stable_id},
                    source,
                )
            elif instruction.opcode == "normalized_age":
                if instruction.operands or instruction.immediate_dict():
                    raise KernelCompileError(
                        "normalized age expression cannot define authored inputs"
                    )
                age = self.load("builtin.age", source)
                lifetime = self.load("builtin.lifetime", source)
                value = self.emit(
                    "normalized_age",
                    TypeRef(ValueType.F32),
                    (age, lifetime),
                    {},
                    source,
                )
            elif instruction.opcode == "delta_time":
                if instruction.operands or instruction.immediate_dict():
                    raise KernelCompileError(
                        "delta time expression cannot define authored inputs"
                    )
                if self.stage is not KernelStage.UPDATE:
                    raise KernelCompileError(
                        "delta time is only valid in Update kernels"
                    )
                value = self.emit(
                    "load_uniform",
                    TypeRef(ValueType.F32),
                    (),
                    {"name": "delta_time"},
                    source,
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
    parameters: tuple[KernelParameter, ...],
    emitters: tuple[ParticleEmitterKernelIR, ...],
    events: KernelEventABI,
    contract: KernelRuntimeContract,
) -> str:
    semantic = {
        "$schema": KERNEL_IR_SCHEMA,
        "parameters": [
            {
                "stable_id": parameter.stable_id,
                "type": parameter.value_type.to_dict(),
                "slot": parameter.slot,
            }
            for parameter in parameters
        ],
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
    "KernelExecutionLane",
    "KernelEventABI",
    "KernelEventField",
    "KernelEventRoute",
    "KernelEventType",
    "KernelOperand",
    "KernelParameter",
    "KernelFlowBlock",
    "KernelJoinAll",
    "KernelLifecycleFlow",
    "KernelSourceRef",
    "KernelSuspensionPoint",
    "KernelStage",
    "ParticleEmitterKernelIR",
    "ParticleKernelFunction",
    "ParticleKernelLowerer",
    "ParticleKernelProgram",
]
