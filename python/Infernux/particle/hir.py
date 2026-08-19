"""Backend-independent Particle Program HIR and Graph frontend compiler."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import math
from typing import Any, Mapping

from Infernux.graph.registry import PortDirection, PortKind
from Infernux.graph.expression_ir import (
    ExpressionCompileError,
    ExpressionCompiler,
    ExpressionInstruction,
    ExpressionProgram,
)
from Infernux.graph.document import (
    GraphDocument,
    GraphLinkRecord,
    GraphNodeRecord,
    GraphSourceLocation,
)
from Infernux.graph.parameters import graph_parameter_allows_hdr
from Infernux.graph.types import AssetReference, CoordinateSpace, TypeRef, ValueType

from .asset import (
    EmitterSettings,
    ParticleAttribute,
    ParticleGraphAsset,
    ParticleParameter,
    particle_attribute_cache_id,
    particle_attribute_capture_id,
    particle_attribute_catalog,
    particle_attribute_zero,
)
from .data_interface import (
    MeshResourceBinding,
    ParticleRuntimeResource,
    SdfVolume,
)
from .nodes import (
    ATTRIBUTE_OPERATION_SPECS,
    particle_graph_node_definitions,
    specialize_particle_event_document,
    specialize_particle_output_document,
)


def _merge_active_event_flows(event_id, flows, definitions) -> GraphDocument:
    """Compile all Active Event roots for one event as one parallel handler flow."""
    root_type = definitions.event_active_compile_type_by_id[event_id]
    nodes = [
        GraphNodeRecord("root.event", root_type, (0.0, 0.0), {"event": event_id})
    ]
    links = []
    source_locations = {}
    for flow in flows:
        document = specialize_particle_event_document(flow.graph, definitions)
        prefix = f"{flow.stable_id}."
        for node in document.nodes:
            if node.uid == "root.event":
                continue
            uid = prefix + node.uid
            nodes.append(replace(node, uid=uid))
            location = document.source_location(node.uid)
            if location != GraphSourceLocation():
                source_locations[uid] = location
        for link in document.links:
            links.append(
                GraphLinkRecord(
                    prefix + link.uid,
                    "root.event" if link.source_node == "root.event" else prefix + link.source_node,
                    link.source_port,
                    "root.event" if link.target_node == "root.event" else prefix + link.target_node,
                    link.target_port,
                    link.kind,
                )
            )
    return GraphDocument(
        "particle.event",
        tuple(nodes),
        tuple(links),
        source_locations=source_locations,
    )


class ParticleStage(str, Enum):
    INIT = "init"
    UPDATE = "update"
    COLLISION_ENTER = "collision_enter"
    COLLISION_STAY = "collision_stay"
    COLLISION_EXIT = "collision_exit"
    EVENT = "event"
    RENDERING = "rendering"


class ParticleCompileError(ValueError):
    pass


@dataclass(frozen=True)
class ParticleExecutionPredicate:
    source_node_uid: str
    value_id: str
    literal: bool
    expected: bool
    runtime_condition: str = ""


@dataclass(frozen=True)
class ParticleOperation:
    opcode: str
    parameters: tuple[tuple[str, Any], ...]
    source_node_uid: str
    value_bindings: tuple[tuple[str, str], ...] = ()
    execution_predicates: tuple[ParticleExecutionPredicate, ...] = ()

    def parameter_dict(self) -> dict[str, Any]:
        return dict(self.parameters)


@dataclass(frozen=True)
class ParticleExecEdge:
    link_uid: str
    source_node_uid: str
    source_port_id: str
    target_node_uid: str
    target_port_id: str
    predicate_node_uid: str = ""
    predicate_expected: bool | None = None
    lane_index: int = 0


@dataclass(frozen=True)
class ParticleFlowBlock:
    node_uid: str
    operations: tuple[ParticleOperation, ...]
    incoming_edges: tuple[str, ...]
    outgoing_edges: tuple[str, ...]
    lane_index: int = 0


@dataclass(frozen=True)
class ParticleExecutionLane:
    stable_id: str
    index: int
    parent_index: int
    source_node_uid: str
    source_port_id: str


@dataclass(frozen=True)
class ParticleJoinDescriptor:
    node_uid: str
    input_lane_indices: tuple[int, ...]
    output_lane_index: int


class ParticleSuspensionKind(str, Enum):
    FRAMES = "frames"
    SECONDS = "seconds"
    UNTIL_FRAMES = "until_frames"
    UNTIL_SECONDS = "until_seconds"


@dataclass(frozen=True)
class ParticleSuspensionPoint:
    node_uid: str
    kind: ParticleSuspensionKind
    lane_index: int
    lane_stable_id: str
    resume_program_counter: int
    resume_node_uid: str
    value_id: str = ""
    literal: int | float = 0


@dataclass(frozen=True)
class ParticleFlowProgram:
    entry_node_uid: str
    blocks: tuple[ParticleFlowBlock, ...]
    edges: tuple[ParticleExecEdge, ...]
    lanes: tuple[ParticleExecutionLane, ...] = ()
    joins: tuple[ParticleJoinDescriptor, ...] = ()
    suspensions: tuple[ParticleSuspensionPoint, ...] = ()

    def __post_init__(self) -> None:
        block_ids = {block.node_uid for block in self.blocks}
        if self.entry_node_uid not in block_ids:
            raise ParticleCompileError("particle flow entry block is missing")
        if len(block_ids) != len(self.blocks):
            raise ParticleCompileError("particle flow block ids must be unique")
        operations = tuple(
            operation
            for block in self.blocks
            for operation in block.operations
        )
        if len({operation.source_node_uid for operation in operations}) != len(operations):
            raise ParticleCompileError("particle flow operation source ids must be unique")
        for block in self.blocks:
            if block.node_uid == self.entry_node_uid:
                continue
            if (
                len(block.operations) != 1
                or block.operations[0].source_node_uid != block.node_uid
            ):
                raise ParticleCompileError(
                    "particle non-entry flow blocks must own exactly one matching operation"
                )
        edge_ids = {edge.link_uid for edge in self.edges}
        if len(edge_ids) != len(self.edges):
            raise ParticleCompileError("particle flow edge ids must be unique")
        if any(
            edge.source_node_uid not in block_ids or edge.target_node_uid not in block_ids
            for edge in self.edges
        ):
            raise ParticleCompileError("particle flow edge references an unknown block")
        if not self.lanes:
            raise ParticleCompileError("particle flow requires at least one execution lane")
        if tuple(lane.index for lane in self.lanes) != tuple(range(len(self.lanes))):
            raise ParticleCompileError("particle execution lane indices must be dense")
        if len({lane.stable_id for lane in self.lanes}) != len(self.lanes):
            raise ParticleCompileError("particle execution lane identities must be unique")
        for lane in self.lanes:
            if lane.index == 0:
                if lane.parent_index != -1:
                    raise ParticleCompileError("particle root lane cannot have a parent")
            elif not 0 <= lane.parent_index < lane.index:
                raise ParticleCompileError("particle execution lane parent is invalid")
        if any(not 0 <= edge.lane_index < len(self.lanes) for edge in self.edges):
            raise ParticleCompileError("particle flow edge lane is invalid")
        if any(not 0 <= block.lane_index < len(self.lanes) for block in self.blocks):
            raise ParticleCompileError("particle flow block lane is invalid")
        join_nodes = set()
        for join in self.joins:
            if join.node_uid in join_nodes or join.node_uid not in block_ids:
                raise ParticleCompileError("particle Join All descriptor is invalid")
            join_nodes.add(join.node_uid)
            if len(join.input_lane_indices) < 2 or len(set(join.input_lane_indices)) != len(
                join.input_lane_indices
            ):
                raise ParticleCompileError("particle Join All input lanes are invalid")
            if any(not 0 <= value < len(self.lanes) for value in join.input_lane_indices):
                raise ParticleCompileError("particle Join All references an unknown input lane")
            if not 0 <= join.output_lane_index < len(self.lanes):
                raise ParticleCompileError("particle Join All output lane is invalid")
        suspension_nodes = set()
        resume_program_counters = set()
        for suspension in self.suspensions:
            terminal = suspension.resume_node_uid == ""
            if (
                suspension.node_uid in suspension_nodes
                or suspension.node_uid not in block_ids
                or (
                    not terminal
                    and suspension.resume_node_uid not in block_ids
                )
            ):
                raise ParticleCompileError("particle suspension descriptor is invalid")
            suspension_nodes.add(suspension.node_uid)
            if (
                suspension.resume_program_counter <= 0
                or suspension.resume_program_counter in resume_program_counters
            ):
                raise ParticleCompileError("particle suspension resume target is invalid")
            resume_program_counters.add(suspension.resume_program_counter)
            if not 0 <= suspension.lane_index < len(self.lanes):
                raise ParticleCompileError("particle suspension lane is invalid")
            if self.lanes[suspension.lane_index].stable_id != suspension.lane_stable_id:
                raise ParticleCompileError("particle suspension lane identity is inconsistent")

    def iter_operations(self):
        for block in self.blocks:
            yield from block.operations


@dataclass(frozen=True)
class ParticleStageHIR:
    stage: ParticleStage
    root_uid: str
    expressions: ExpressionProgram
    flow: ParticleFlowProgram
    flow_id: str = ""
    source_locations: Mapping[str, GraphSourceLocation] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )

    def source_location(self, node_uid: str) -> GraphSourceLocation:
        return self.source_locations.get(node_uid, GraphSourceLocation())


@dataclass(frozen=True)
class ParticleOutputShaderProperty:
    name: str
    value_type: TypeRef
    default: Any
    parameter_id: str = ""


@dataclass(frozen=True)
class ParticleOutputDescriptor:
    output_id: str
    output_type: str
    mesh: AssetReference
    shader: str
    shader_properties: tuple[ParticleOutputShaderProperty, ...]
    receive_scene_lighting: bool
    receive_shadows: bool
    cast_shadows: bool
    soft_particles: bool
    soft_distance: float
    sort_mode: str
    ribbon_uv_mode: str = "stretch"
    ribbon_uv_scale: float = 1.0
    flipbook_columns: int = 1
    flipbook_rows: int = 1
    sprite_alignment: str = "camera_plane"
    alignment_axis: tuple[float, float, float] = (0.0, 1.0, 0.0)
    mesh_parameter: str = ""


@dataclass(frozen=True)
class ParticleRenderPlan:
    outputs: tuple[ParticleOutputDescriptor, ...]


@dataclass(frozen=True)
class ParticleEmitterHIR:
    stable_id: str
    name: str
    settings: EmitterSettings
    attributes: tuple[ParticleAttribute, ...]
    init: ParticleStageHIR
    update: ParticleStageHIR
    collision_enter: ParticleStageHIR | None
    collision_stay: ParticleStageHIR | None
    collision_exit: ParticleStageHIR | None
    event_flows: tuple[ParticleStageHIR, ...]
    rendering: ParticleStageHIR
    render_plan: ParticleRenderPlan
    data_interfaces: tuple[ParticleRuntimeResource, ...] = ()

    def lifecycle_stages(self) -> tuple[ParticleStageHIR, ...]:
        """Return active lifecycle stages in their deterministic execution order."""
        return (
            self.init,
            self.update,
            *(
                stage
                for stage in (
                    self.collision_enter,
                    self.collision_stay,
                    self.collision_exit,
                )
                if stage is not None
            ),
            *self.event_flows,
            self.rendering,
        )


@dataclass(frozen=True)
class ParticleSystemSchedule:
    emitter_ids: tuple[str, ...]


@dataclass(frozen=True)
class ParticleEventFieldHIR:
    stable_id: str
    name: str
    value_type: TypeRef
    word_offset: int
    word_count: int
    default: Any


@dataclass(frozen=True)
class ParticleEventTypeHIR:
    stable_id: str
    name: str
    type_index: int
    stable_type_hash: int
    queue_capacity: int
    payload_stride_words: int
    fields: tuple[ParticleEventFieldHIR, ...]


@dataclass(frozen=True)
class ParticleEventSchedule:
    event_abi_hash: str
    event_types: tuple[ParticleEventTypeHIR, ...]

    @property
    def event_abi_u64(self) -> int:
        value = int(self.event_abi_hash[:16], 16)
        return value or 1


@dataclass(frozen=True)
class ParticleParameterHIR:
    stable_id: str
    name: str
    value_type: TypeRef
    default: Any
    exposed: bool
    writable: bool
    slot: int
    category: str
    tooltip: str
    hdr: bool = False

    @classmethod
    def from_asset(cls, parameter: ParticleParameter, slot: int) -> "ParticleParameterHIR":
        return cls(
            parameter.stable_id,
            parameter.name,
            parameter.value_type,
            parameter.default,
            parameter.exposed,
            parameter.writable,
            slot,
            parameter.category,
            parameter.tooltip,
            graph_parameter_allows_hdr(parameter),
        )


@dataclass(frozen=True)
class ParticleProgramHIR:
    stable_id: str
    name: str
    semantic_hash: str
    behavior_hash: str
    parameters: tuple[ParticleParameterHIR, ...]
    emitters: tuple[ParticleEmitterHIR, ...]
    schedule: ParticleSystemSchedule
    events: ParticleEventSchedule


class ParticleGraphCompiler:
    """Lower strict ParticleGraph assets into backend-independent HIR."""

    def compile(
        self,
        asset: ParticleGraphAsset,
        *,
        source_name: str = "",
    ) -> ParticleProgramHIR:
        events = self._compile_events(asset)
        definitions = particle_graph_node_definitions(asset)
        parameters = tuple(
            ParticleParameterHIR.from_asset(parameter, slot)
            for slot, parameter in enumerate(
                sorted(asset.parameters, key=lambda item: item.stable_id)
            )
        )
        emitters = tuple(
            self._compile_emitter(emitter, events, definitions, source_name)
            for emitter in asset.emitters
        )
        return ParticleProgramHIR(
            asset.stable_id,
            asset.name,
            asset.semantic_hash(),
            self._behavior_hash(parameters, emitters, events),
            parameters,
            emitters,
            ParticleSystemSchedule(tuple(emitter.stable_id for emitter in emitters)),
            events,
        )

    @staticmethod
    def _behavior_hash(parameters, emitters, events: ParticleEventSchedule) -> str:
        def stage_payload(stage_hir):
            canonical_ids = {
                instruction.result_id: f"%{index}"
                for index, instruction in enumerate(stage_hir.expressions.instructions)
            }
            expressions = []
            for instruction in stage_hir.expressions.instructions:
                expressions.append(
                    {
                        "result": canonical_ids[instruction.result_id],
                        "opcode": instruction.opcode,
                        "type": instruction.result_type.to_dict(),
                        "immediates": list(instruction.immediates),
                        "operands": [
                            {
                                "type": operand.value_type.to_dict(),
                                "value": canonical_ids.get(operand.value_id, operand.value_id),
                                "literal": operand.literal,
                            }
                            for operand in instruction.operands
                        ],
                    }
                )
            block_indices = {
                block.node_uid: index
                for index, block in enumerate(stage_hir.flow.blocks)
            }

            def operation_payload(operation):
                return {
                    "opcode": operation.opcode,
                    "parameters": list(operation.parameters),
                    "value_bindings": [
                        (name, canonical_ids.get(value_id, value_id))
                        for name, value_id in operation.value_bindings
                    ],
                    "execution_predicates": [
                        {
                            "value": canonical_ids.get(
                                predicate.value_id, predicate.value_id
                            ),
                            "literal": predicate.literal,
                            "expected": predicate.expected,
                            "runtime_condition": predicate.runtime_condition,
                        }
                        for predicate in operation.execution_predicates
                    ],
                }

            def flow_node(node_uid: str):
                if not node_uid:
                    return "terminal"
                if node_uid == stage_hir.flow.entry_node_uid:
                    return "entry"
                return f"block:{block_indices[node_uid]}"

            flow_edges = sorted(
                (
                    flow_node(edge.source_node_uid),
                    edge.source_port_id,
                    flow_node(edge.target_node_uid),
                    edge.target_port_id,
                    edge.lane_index,
                )
                for edge in stage_hir.flow.edges
            )
            return {
                "expressions": expressions,
                "blocks": [
                    {
                        "node": flow_node(block.node_uid),
                        "operations": [
                            operation_payload(operation)
                            for operation in block.operations
                        ],
                    }
                    for block in stage_hir.flow.blocks
                ],
                "flow_edges": flow_edges,
                "lanes": [
                    {
                        "stable_id": lane.stable_id,
                        "index": lane.index,
                        "parent": lane.parent_index,
                        "source": flow_node(lane.source_node_uid),
                        "port": lane.source_port_id,
                    }
                    for lane in stage_hir.flow.lanes
                ],
                "joins": [
                    {
                        "node": flow_node(join.node_uid),
                        "inputs": list(join.input_lane_indices),
                        "output": join.output_lane_index,
                    }
                    for join in stage_hir.flow.joins
                ],
                "suspensions": [
                    {
                        "node": flow_node(suspension.node_uid),
                        "kind": suspension.kind.value,
                        "lane": suspension.lane_index,
                        "lane_stable_id": suspension.lane_stable_id,
                        "resume_pc": suspension.resume_program_counter,
                        "resume_node": flow_node(suspension.resume_node_uid),
                        "value": canonical_ids.get(
                            suspension.value_id, suspension.value_id
                        ),
                        "literal": suspension.literal,
                    }
                    for suspension in stage_hir.flow.suspensions
                ],
            }

        payload = [
            {
                "parameters": [
                    {
                        "stable_id": parameter.stable_id,
                        "type": parameter.value_type.to_dict(),
                        "slot": parameter.slot,
                        "writable": parameter.writable,
                    }
                    for parameter in parameters
                ]
            }
        ]
        for emitter in emitters:
            payload.append(
                {
                    "stable_id": emitter.stable_id,
                    "settings": emitter.settings.to_dict(),
                    "attributes": [attribute.to_dict() for attribute in emitter.attributes],
                    "data_interfaces": [
                        interface.to_dict()
                        for interface in sorted(
                            emitter.data_interfaces,
                            key=lambda value: value.stable_id,
                        )
                    ],
                    "lifecycles": {
                        stage_name: (
                            stage_payload(stage)
                            if (stage := getattr(emitter, stage_name)) is not None
                            else None
                        )
                        for stage_name in (
                            "init",
                            "update",
                            "collision_enter",
                            "collision_stay",
                            "collision_exit",
                            "rendering",
                        )
                    },
                    "events": {
                        stage.flow_id: stage_payload(stage)
                        for stage in emitter.event_flows
                    },
                    "outputs": [
                        {
                            "type": output.output_type,
                            "mesh": output.mesh.to_dict(),
                            "mesh_parameter": output.mesh_parameter,
                            "shader": output.shader,
                            "shader_properties": [
                                {
                                    "name": item.name,
                                    "type": item.value_type.to_dict(),
                                    "default": item.default,
                                    "parameter_id": item.parameter_id,
                                }
                                for item in output.shader_properties
                            ],
                            "receive_scene_lighting": output.receive_scene_lighting,
                            "receive_shadows": output.receive_shadows,
                            "cast_shadows": output.cast_shadows,
                            "soft_particles": output.soft_particles,
                            "soft_distance": output.soft_distance,
                            "sort": output.sort_mode,
                            "ribbon_uv_mode": output.ribbon_uv_mode,
                            "ribbon_uv_scale": output.ribbon_uv_scale,
                            "flipbook_columns": output.flipbook_columns,
                            "flipbook_rows": output.flipbook_rows,
                            "sprite_alignment": output.sprite_alignment,
                            "alignment_axis": list(output.alignment_axis),
                        }
                        for output in emitter.render_plan.outputs
                    ],
                }
            )
        payload.append(
            {
                "event_abi_hash": events.event_abi_hash,
                "event_types": [
                    {
                        "stable_id": value.stable_id,
                        "queue_capacity": value.queue_capacity,
                        "payload_stride_words": value.payload_stride_words,
                        "fields": [
                            {
                                "stable_id": field.stable_id,
                                "type": field.value_type.to_dict(),
                                "word_offset": field.word_offset,
                                "word_count": field.word_count,
                                "default": field.default,
                            }
                            for field in value.fields
                        ],
                    }
                    for value in events.event_types
                ],
            }
        )
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _compile_events(asset: ParticleGraphAsset) -> ParticleEventSchedule:
        event_types = []
        for type_index, event_type in enumerate(asset.event_types):
            fields = []
            word_offset = 0
            for field in event_type.fields:
                word_count = _event_word_count(field.value_type)
                fields.append(
                    ParticleEventFieldHIR(
                        field.stable_id,
                        field.name,
                        field.value_type,
                        word_offset,
                        word_count,
                        field.default,
                    )
                )
                word_offset += word_count
            stable_type_hash = _stable_u64(event_type.stable_id)
            lowered = ParticleEventTypeHIR(
                event_type.stable_id,
                event_type.name,
                type_index,
                stable_type_hash,
                event_type.queue_capacity,
                word_offset,
                tuple(fields),
            )
            event_types.append(lowered)

        abi_payload = {
            "emitters": [emitter.stable_id for emitter in asset.emitters],
            "event_types": [
                {
                    "stable_id": value.stable_id,
                    "stable_type_hash": value.stable_type_hash,
                    "queue_capacity": value.queue_capacity,
                    "payload_stride_words": value.payload_stride_words,
                    "fields": [
                        {
                            "stable_id": field.stable_id,
                            "type": field.value_type.to_dict(),
                            "word_offset": field.word_offset,
                            "word_count": field.word_count,
                        }
                        for field in value.fields
                    ],
                }
                for value in event_types
            ],
        }
        encoded = json.dumps(abi_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return ParticleEventSchedule(
            hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            tuple(event_types),
        )

    def _compile_emitter(
        self,
        emitter,
        events: ParticleEventSchedule,
        definitions,
        source_name: str,
    ) -> ParticleEmitterHIR:
        init = self._compile_stage(
            ParticleStage.INIT,
            emitter.init,
            emitter.settings,
            definitions,
            events,
            emitter,
            source_name=source_name,
        )
        update = self._compile_stage(
            ParticleStage.UPDATE,
            emitter.update,
            emitter.settings,
            definitions,
            events,
            emitter,
            source_name=source_name,
        )
        def compile_collision_lifecycle(stage: ParticleStage) -> ParticleStageHIR | None:
            document = getattr(emitter, stage.value)
            if not emitter.settings.collision_enabled or document is None:
                return None
            return self._compile_stage(
                stage,
                document,
                emitter.settings,
                definitions,
                events,
                emitter,
                source_name=source_name,
            )

        collision_enter = compile_collision_lifecycle(ParticleStage.COLLISION_ENTER)
        collision_stay = compile_collision_lifecycle(ParticleStage.COLLISION_STAY)
        collision_exit = compile_collision_lifecycle(ParticleStage.COLLISION_EXIT)
        flows_by_event = {
            event_type.stable_id: tuple(
                flow
                for flow in emitter.event_flows
                if flow.event_id == event_type.stable_id
            )
            for event_type in events.event_types
        }
        event_flows = tuple(
            self._compile_stage(
                ParticleStage.EVENT,
                _merge_active_event_flows(event_id, flows, definitions),
                emitter.settings,
                definitions,
                events,
                emitter,
                root_type=definitions.event_active_compile_type_by_id[event_id],
                flow_id=event_id,
                source_name=source_name,
            )
            for event_id, flows in flows_by_event.items()
            if flows
        )
        rendering = self._compile_stage(
            ParticleStage.RENDERING,
            emitter.rendering,
            emitter.settings,
            definitions,
            events,
            emitter,
            source_name=source_name,
        )
        outputs = tuple(
            self._compile_output(
                operation,
                rendering.expressions,
                definitions.output_definition_by_node.get(
                    (emitter.stable_id, operation.source_node_uid)
                ),
            )
            for operation in rendering.flow.iter_operations()
            if operation.opcode in {"render.sprite", "render.mesh", "render.ribbon"}
        )
        if not outputs:
            raise ParticleCompileError(
                f"emitter {emitter.stable_id!r} Rendering stage requires at least one output"
            )
        invalid_sort = next(
            (
                output
                for output in outputs
                if output.sort_mode not in {"none", "back_to_front", "front_to_back"}
            ),
            None,
        )
        if invalid_sort is not None:
            raise ParticleCompileError(
                f"particle output {invalid_sort.output_id!r} has unsupported sort mode "
                f"{invalid_sort.sort_mode!r}"
            )
        invalid_shadows = next(
            (
                output
                for output in outputs
                if output.receive_shadows and not output.receive_scene_lighting
            ),
            None,
        )
        if invalid_shadows is not None:
            raise ParticleCompileError(
                f"particle output {invalid_shadows.output_id!r} cannot receive shadows "
                "while scene lighting is disabled"
            )
        invalid_soft_distance = next(
            (
                output
                for output in outputs
                if not math.isfinite(output.soft_distance) or output.soft_distance <= 0.0
            ),
            None,
        )
        if invalid_soft_distance is not None:
            raise ParticleCompileError(
                f"particle output {invalid_soft_distance.output_id!r} soft distance must be finite and positive"
            )
        invalid_flipbook = next(
            (
                output
                for output in outputs
                if output.output_type == "sprite"
                and (
                    type(output.flipbook_columns) is not int
                    or type(output.flipbook_rows) is not int
                    or not 1 <= output.flipbook_columns <= 4096
                    or not 1 <= output.flipbook_rows <= 4096
                    or output.flipbook_columns * output.flipbook_rows > 65536
                )
            ),
            None,
        )
        if invalid_flipbook is not None:
            raise ParticleCompileError(
                f"particle sprite output {invalid_flipbook.output_id!r} flipbook grid must use "
                "1..4096 columns/rows and contain at most 65536 frames"
            )
        invalid_alignment = next(
            (
                output
                for output in outputs
                if output.output_type == "sprite"
                and output.sprite_alignment
                not in {"camera_plane", "camera_position", "axis", "velocity"}
            ),
            None,
        )
        if invalid_alignment is not None:
            raise ParticleCompileError(
                f"particle sprite output {invalid_alignment.output_id!r} alignment must be "
                "'camera_plane', 'camera_position', 'axis', or 'velocity'"
            )
        invalid_alignment_axis = next(
            (
                output
                for output in outputs
                if output.output_type == "sprite"
                and (
                    len(output.alignment_axis) != 3
                    or not all(math.isfinite(value) for value in output.alignment_axis)
                    or (
                        output.sprite_alignment == "axis"
                        and sum(value * value for value in output.alignment_axis) <= 1.0e-12
                    )
                )
            ),
            None,
        )
        if invalid_alignment_axis is not None:
            raise ParticleCompileError(
                f"particle sprite output {invalid_alignment_axis.output_id!r} alignment axis "
                "must contain three finite values and be non-zero for axis alignment"
            )
        missing_mesh = next(
            (
                output
                for output in outputs
                if output.output_type == "mesh"
                and not output.mesh_parameter
                and not (output.mesh.guid or output.mesh.path_hint)
            ),
            None,
        )
        if missing_mesh is not None:
            raise ParticleCompileError(
                f"particle mesh output {missing_mesh.output_id!r} requires a mesh asset"
            )
        unsupported_mesh_semantics = next(
            (
                output
                for output in outputs
                if output.output_type == "mesh"
                and output.soft_particles
            ),
            None,
        )
        if unsupported_mesh_semantics is not None:
            raise ParticleCompileError(
                f"particle mesh output {unsupported_mesh_semantics.output_id!r} currently "
                "does not support soft-particle depth fading"
            )
        unsupported_ribbon_semantics = next(
            (
                output
                for output in outputs
                if output.output_type == "ribbon" and output.sort_mode != "none"
            ),
            None,
        )
        if unsupported_ribbon_semantics is not None:
            raise ParticleCompileError(
                f"particle ribbon output {unsupported_ribbon_semantics.output_id!r} uses stable "
                "strip topology ordering and therefore requires sort='none'"
            )
        invalid_ribbon_uv = next(
            (
                output
                for output in outputs
                if output.output_type == "ribbon"
                and (
                    output.ribbon_uv_mode not in {"stretch", "repeat"}
                    or not math.isfinite(output.ribbon_uv_scale)
                    or output.ribbon_uv_scale <= 0.0
                )
            ),
            None,
        )
        if invalid_ribbon_uv is not None:
            raise ParticleCompileError(
                f"particle ribbon output {invalid_ribbon_uv.output_id!r} requires uv_mode "
                "'stretch' or 'repeat' and a finite positive uv_scale"
            )
        all_lifecycles = (
            init,
            update,
            *(
                stage
                for stage in (collision_enter, collision_stay, collision_exit)
                if stage
            ),
            *event_flows,
            rendering,
        )
        lifecycle_operations = tuple(
            operation
            for stage in all_lifecycles
            for operation in stage.flow.iter_operations()
        )
        collision_opcodes = {
            "collision.plane",
            "collision.sphere",
            "collision.sdf",
        }
        for operation in lifecycle_operations:
            if operation.opcode != "motion.target_position":
                continue
            parameters = operation.parameter_dict()
            bindings = dict(operation.value_bindings)
            for name in ("speed", "responsiveness", "arrival_radius"):
                if name in bindings:
                    continue
                value = float(parameters[name])
                if not math.isfinite(value) or value < 0.0:
                    raise ParticleCompileError(
                        f"Target Position {name} must be finite and non-negative"
                    )
        for operation in lifecycle_operations:
            if operation.opcode not in collision_opcodes:
                continue
            parameters = operation.parameter_dict()
            bindings = dict(operation.value_bindings)
            label = {
                "collision.plane": "Plane Collision",
                "collision.sphere": "Sphere Collision",
                "collision.sdf": "SDF Collision",
            }[operation.opcode]
            if operation.opcode == "collision.plane":
                normal = parameters["normal"]
                if "normal" not in bindings and (
                    not isinstance(normal, (list, tuple))
                    or len(normal) != 3
                    or sum(float(value) * float(value) for value in normal) <= 1.0e-12
                ):
                    raise ParticleCompileError("Plane Collision normal must be non-zero")
                radius_names = ("radius",)
            elif operation.opcode == "collision.sphere":
                radius_names = ("sphere_radius", "particle_radius")
            else:
                interface_id = parameters["interface"]
                interface = next(
                    (
                        value
                        for value in emitter.data_interfaces
                        if value.stable_id == interface_id
                    ),
                    None,
                )
                if not isinstance(interface, SdfVolume):
                    raise ParticleCompileError(
                        f"SDF Collision references unknown SdfVolume interface {interface_id!r}"
                    )
                if type(parameters["inverted"]) is not bool:
                    raise ParticleCompileError("SDF Collision inverted must be a boolean")
                radius_names = ("particle_radius",)
            for name in (*radius_names, "restitution", "friction"):
                if name not in parameters:
                    continue
                if name in bindings:
                    continue
                value = float(parameters[name])
                if name in radius_names and value < 0.0:
                    raise ParticleCompileError(f"{label} {name} must be non-negative")
                if name not in radius_names and not 0.0 <= value <= 1.0:
                    raise ParticleCompileError(
                        f"{label} {name} must be between 0 and 1"
                    )
        orientation_opcodes = {"attribute.modify_orientation"}
        scale_opcodes = {"attribute.modify_scale"}
        event_flow_ids = {stage.flow_id for stage in event_flows}
        for stage_hir in all_lifecycles:
            for operation in stage_hir.flow.iter_operations():
                if operation.opcode != "event.trigger":
                    continue
                event_id = str(operation.parameter_dict().get("event", ""))
                if not event_id:
                    continue
                if event_id not in event_flow_ids:
                    raise ParticleCompileError(
                        f"Trigger Event {operation.source_node_uid!r} requires event "
                        f"{event_id!r} to be dragged into emitter {emitter.name!r} first"
                    )
        needs_orientation = any(output.output_type == "mesh" for output in outputs) or any(
            operation.opcode in orientation_opcodes
            for stage in all_lifecycles
            for operation in stage.flow.iter_operations()
        )
        needs_scale = any(output.output_type == "mesh" for output in outputs) or any(
            operation.opcode in scale_opcodes
            for stage in all_lifecycles
            for operation in stage.flow.iter_operations()
        )
        needs_flipbook_frame = any(
            operation.opcode == "attribute.modify_flipbook_frame"
            for stage in all_lifecycles
            for operation in stage.flow.iter_operations()
        )
        catalog = {
            attribute.stable_id: attribute
            for attribute in particle_attribute_catalog(emitter)
        }
        for stage_hir in all_lifecycles:
            for operation in stage_hir.flow.iter_operations():
                if operation.opcode != "attribute.capture":
                    continue
                parameters = operation.parameter_dict()
                source_id = str(parameters["attribute"])
                source = catalog.get(source_id)
                if source is None:
                    raise ParticleCompileError(
                        f"Get Attribute capture references unknown attribute {source_id!r}"
                    )
                snapshot_id = str(parameters["snapshot"])
                snapshot = ParticleAttribute(
                    snapshot_id,
                    f"{operation.source_node_uid}_sample",
                    source.value_type,
                    particle_attribute_zero(source.value_type),
                )
                existing = catalog.get(snapshot_id)
                if existing is not None and existing != snapshot:
                    raise ParticleCompileError(
                        f"Get Attribute capture identity collision for {snapshot_id!r}"
                    )
                catalog[snapshot_id] = snapshot
        for stage in all_lifecycles:
            for operation in stage.flow.iter_operations():
                if operation.opcode != "attribute.modify_cache":
                    continue
                stable_id = str(operation.parameter_dict().get("attribute", ""))
                if stable_id.startswith("builtin.") or stable_id not in catalog:
                    raise ParticleCompileError(
                        "Attribute Cache storage must be owned by its graph node, got "
                        f"unknown slot {stable_id!r}"
                    )
        attributes = list(emitter.attributes)
        allocated = {attribute.stable_id for attribute in attributes}

        def allocate(stable_id: str) -> None:
            if stable_id in allocated:
                return
            attribute = catalog.get(stable_id)
            if attribute is None:
                raise ParticleCompileError(
                    f"particle graph references unknown attribute {stable_id!r}"
                )
            attributes.append(attribute)
            allocated.add(stable_id)

        for stage_hir in all_lifecycles:
            for operation in stage_hir.flow.iter_operations():
                if operation.opcode == "attribute.modify_cache":
                    allocate(str(operation.parameter_dict()["attribute"]))
                if operation.opcode == "attribute.capture":
                    allocate(str(operation.parameter_dict()["snapshot"]))
            for instruction in stage_hir.expressions.instructions:
                if instruction.opcode == "load_attribute":
                    allocate(str(instruction.immediate_dict()["attribute"]))
        if needs_orientation:
            allocate("builtin.orientation")
        if needs_scale:
            allocate("builtin.scale")
        if needs_flipbook_frame:
            allocate("builtin.flipbook_frame")
        if emitter.settings.collision_enabled or any(
            operation.opcode in collision_opcodes
            for operation in lifecycle_operations
        ):
            allocate("builtin.collision_hit")
            allocate("builtin.collision_normal")
            allocate("builtin.collision_point")
            allocate("builtin.collision_relative_velocity")
            allocate("builtin.collision_penetration")
            allocate("builtin.collision_is_trigger")
            allocate("builtin.collision_material")
            allocate("builtin.collision_collider_id_low")
            allocate("builtin.collision_collider_id_high")
        if any(output.output_type == "ribbon" for output in outputs):
            allocate("builtin.ribbon_strip_id")
            allocate("builtin.ribbon_order")
            allocate("builtin.ribbon_break")
        compiled_interfaces = list(emitter.data_interfaces)
        interface_ids = {
            interface.stable_id for interface in compiled_interfaces
        }
        for stage_hir in all_lifecycles:
            for instruction in stage_hir.expressions.instructions:
                if instruction.opcode != "sample_mesh":
                    continue
                immediate = instruction.immediate_dict()
                stable_id = str(immediate["interface"])
                if stable_id in interface_ids:
                    continue
                compiled_interfaces.append(
                    MeshResourceBinding(
                        stable_id=stable_id,
                        name="Sample Mesh Resource",
                        mesh=AssetReference.from_dict(immediate["mesh"]),
                        mesh_parameter=str(immediate["mesh_parameter"]),
                        space=CoordinateSpace.EMITTER_LOCAL,
                    )
                )
                interface_ids.add(stable_id)
        return ParticleEmitterHIR(
            emitter.stable_id,
            emitter.name,
            emitter.settings,
            tuple(attributes),
            init,
            update,
            collision_enter,
            collision_stay,
            collision_exit,
            event_flows,
            rendering,
            ParticleRenderPlan(outputs),
            tuple(compiled_interfaces),
        )

    @staticmethod
    def _compile_output(
        operation: ParticleOperation,
        expressions: ExpressionProgram,
        definition,
    ) -> ParticleOutputDescriptor:
        parameters = operation.parameter_dict()
        value_bindings = dict(operation.value_bindings)
        instructions = {
            instruction.result_id: instruction
            for instruction in expressions.instructions
        }

        def direct_parameter_id(value_id: str) -> str:
            visited = set()
            while value_id and value_id not in visited:
                visited.add(value_id)
                instruction = instructions.get(value_id)
                if instruction is None:
                    return ""
                if instruction.opcode == "load_parameter":
                    return str(instruction.immediate_dict().get("parameter", ""))
                if instruction.opcode not in {"numeric_resize", "convert_space"}:
                    return ""
                operands = [item for item in instruction.operands if item.value_id]
                if len(operands) != 1:
                    return ""
                value_id = operands[0].value_id
            return ""

        missing_literal = object()

        def resize_literal(value, source_type: TypeRef, target_type: TypeRef):
            dimensions = {
                ValueType.I32: 1,
                ValueType.U32: 1,
                ValueType.F32: 1,
                ValueType.VEC2: 2,
                ValueType.VEC3: 3,
                ValueType.VEC4: 4,
                ValueType.COLOR: 4,
            }
            source_dimension = dimensions.get(source_type.value_type)
            target_dimension = dimensions.get(target_type.value_type)
            if source_dimension is None or target_dimension is None:
                return missing_literal
            source = list(value) if source_dimension > 1 else [value]
            if target_dimension == 1:
                scalar = source[0]
                return (
                    float(scalar)
                    if target_type.value_type is ValueType.F32
                    else int(scalar)
                )
            if source_dimension == 1:
                scalar = float(source[0])
                return [scalar] * target_dimension
            return [
                float(source[index]) if index < source_dimension else 0.0
                for index in range(target_dimension)
            ]

        def compile_time_literal(value_id: str, visited=None):
            """Resolve immutable output bindings without inventing runtime state."""
            visited = set(visited or ())
            if not value_id or value_id in visited:
                return missing_literal
            visited.add(value_id)
            instruction = instructions.get(value_id)
            if instruction is None:
                return missing_literal
            if instruction.opcode == "constant":
                operands = tuple(instruction.operands)
                if (
                    len(operands) == 1
                    and not operands[0].value_id
                    and operands[0].literal is not None
                ):
                    return copy.deepcopy(operands[0].literal)
                return missing_literal
            if instruction.opcode != "numeric_resize":
                return missing_literal
            operands = tuple(item for item in instruction.operands if item.value_id)
            if len(operands) != 1:
                return missing_literal
            value = compile_time_literal(operands[0].value_id, visited)
            if value is missing_literal:
                return missing_literal
            return resize_literal(value, operands[0].value_type, instruction.result_type)

        shader_properties = []
        if definition is not None:
            for port in definition.ports:
                if not port.id.startswith("shader.") or port.value_type is None:
                    continue
                property_name = port.id.removeprefix("shader.")
                value_id = value_bindings.get(port.id, "")
                parameter_id = direct_parameter_id(value_id)
                default = parameters.get(port.id, port.default)
                if value_id and not parameter_id:
                    literal = compile_time_literal(value_id)
                    if literal is missing_literal:
                        raise ParticleCompileError(
                            f"particle output shader property {property_name!r} must "
                            "be driven by a ParticleGraph Parameter or a compile-time "
                            "constant expression"
                        )
                    default = literal
                shader_properties.append(
                    ParticleOutputShaderProperty(
                        property_name,
                        port.value_type,
                        default,
                        parameter_id,
                    )
                )
        mesh_parameter = ""
        if operation.opcode == "render.mesh":
            mesh_value_id = value_bindings.get("mesh", "")
            mesh_parameter = direct_parameter_id(mesh_value_id)
            if mesh_value_id and not mesh_parameter:
                raise ParticleCompileError(
                    "particle mesh output Mesh input currently requires a direct "
                    "ParticleGraph Mesh Parameter connection"
                )
        output_type = {
            "render.sprite": "sprite",
            "render.mesh": "mesh",
            "render.ribbon": "ribbon",
        }[operation.opcode]
        return ParticleOutputDescriptor(
            operation.source_node_uid,
            output_type,
            AssetReference.from_dict(
                parameters.get("mesh", AssetReference().to_dict())
            ),
            str(parameters["shader"]),
            tuple(shader_properties),
            bool(parameters["receive_scene_lighting"]),
            bool(parameters["receive_shadows"]),
            bool(parameters["cast_shadows"]) if output_type == "mesh" else False,
            bool(parameters.get("soft_particles", False)),
            float(parameters.get("soft_distance", 1.0)),
            str(parameters["sort"]),
            str(parameters.get("uv_mode", "stretch")),
            float(parameters.get("uv_scale", 1.0)),
            int(parameters.get("flipbook_columns", 1)),
            int(parameters.get("flipbook_rows", 1)),
            str(parameters.get("alignment", "camera_plane")),
            tuple(float(value) for value in parameters.get("alignment_axis", (0.0, 1.0, 0.0))),
            mesh_parameter,
        )

    @staticmethod
    def _reachable_exec_nodes(document, root_uid: str) -> set[str]:
        """Return Exec-reachable nodes so unused value graphs cannot fail a stage."""
        outgoing: dict[str, list[str]] = {}
        for link in document.links:
            if link.kind is not PortKind.EXEC:
                continue
            outgoing.setdefault(link.source_node, []).append(link.target_node)
        reachable = set()
        pending = [root_uid]
        while pending:
            uid = pending.pop(0)
            if uid in reachable:
                continue
            reachable.add(uid)
            pending.extend(outgoing.get(uid, ()))
        return reachable

    def _compile_stage(
        self,
        stage: ParticleStage,
        document,
        settings: EmitterSettings,
        definitions,
        events: ParticleEventSchedule,
        emitter,
        *,
        entry_condition: str = "",
        root_type: str = "",
        flow_id: str = "",
        source_name: str = "",
    ) -> ParticleStageHIR:
        document = specialize_particle_event_document(document, definitions)
        if stage is ParticleStage.RENDERING:
            document = specialize_particle_output_document(
                document, definitions, emitter.stable_id
            )
        registry = definitions.registry
        root_type = root_type or f"particle.root.{stage.value}"
        root = next(node for node in document.nodes if node.type_id == root_type)
        reachable = self._reachable_exec_nodes(document, root.uid)
        value_links = sorted(
            (
                link
                for link in document.links
                if link.kind is PortKind.VALUE and link.target_node in reachable
            ),
            key=lambda link: (
                link.target_node,
                link.target_port,
                link.source_node,
                link.source_port,
            ),
        )
        expression_outputs = tuple(
            (link.source_node, link.source_port)
            for link in value_links
        )
        attribute_types = {
            attribute.stable_id: attribute.value_type
            for attribute in particle_attribute_catalog(emitter)
        }

        def _resolve_property_type(property_name: str, selected) -> TypeRef | None:
            if property_name == "attribute":
                return attribute_types.get(str(selected))
            if property_name == "value_type":
                try:
                    return TypeRef(ValueType(str(selected)))
                except ValueError:
                    return None
            if property_name == "target_space":
                try:
                    return TypeRef(ValueType.VEC3, CoordinateSpace(str(selected)))
                except ValueError:
                    return None
            if property_name == "parameter":
                return definitions.parameter_type_by_id.get(str(selected))
            return None

        def _source_location(node_uid: str) -> GraphSourceLocation:
            location = document.source_location(node_uid)
            if location.source_name or not source_name:
                return location
            return GraphSourceLocation(
                source_name=source_name,
                line=location.line,
                column=location.column,
                end_line=location.end_line,
                end_column=location.end_column,
            )

        try:
            expressions = ExpressionCompiler(
                registry,
                definition_fingerprint=definitions.abi_fingerprint,
                property_type_resolver=_resolve_property_type,
            ).compile(
                document, expression_outputs
            )
        except ExpressionCompileError as exc:
            diagnostics = []
            links_by_uid = {link.uid: link for link in document.links}
            for diagnostic in exc.diagnostics:
                node_uid = diagnostic.node_uid
                if not node_uid and diagnostic.link_uid:
                    link = links_by_uid.get(diagnostic.link_uid)
                    node_uid = link.target_node if link is not None else ""
                location = _source_location(node_uid)
                prefix = location.describe(node_uid)
                diagnostics.append(
                    f"{prefix + ': ' if prefix else ''}{diagnostic.message}"
                )
            raise ParticleCompileError("; ".join(diagnostics)) from exc
        expressions = self._resolve_event_payload_expressions(
            expressions,
            document,
            stage,
            flow_id,
            definitions,
            events,
        )
        expressions = self._resolve_mesh_sample_expressions(
            expressions,
            stage,
            flow_id,
            definitions,
        )
        sampled_gets = {
            link.target_node
            for link in document.links
            if link.kind is PortKind.EXEC
            and link.target_port == "in"
            and (node := next((item for item in document.nodes if item.uid == link.target_node), None))
            is not None
            and node.type_id == "particle.attribute.get"
        }
        if sampled_gets:
            rewritten = []
            for instruction in expressions.instructions:
                if (
                    instruction.opcode == "load_attribute"
                    and instruction.source_node_uid in sampled_gets
                ):
                    rewritten.append(
                        replace(
                            instruction,
                            immediates=((
                                "attribute",
                                particle_attribute_capture_id(
                                    stage.value, instruction.source_node_uid
                                ),
                            ),),
                        )
                    )
                else:
                    rewritten.append(instruction)
            expressions = replace(expressions, instructions=tuple(rewritten))
        value_bindings = {
            (link.target_node, link.target_port): result_id
            for link, (result_id, _value_type) in zip(value_links, expressions.outputs)
        }
        settings_operations = tuple(self._settings_operations(stage, settings))
        flow = self._graph_operations(
            stage,
            document,
            root.uid,
            value_bindings,
            definitions,
            expressions,
            str(emitter.stable_id),
            entry_operations=settings_operations,
            entry_condition=entry_condition,
        )
        return ParticleStageHIR(
            stage,
            root.uid,
            expressions,
            flow,
            flow_id,
            {node.uid: _source_location(node.uid) for node in document.nodes},
        )

    @staticmethod
    def _resolve_event_payload_expressions(
        expressions: ExpressionProgram,
        document,
        stage: ParticleStage,
        event_id: str,
        definitions,
        events: ParticleEventSchedule,
    ) -> ExpressionProgram:
        if not any(
            instruction.opcode == "event_payload"
            for instruction in expressions.instructions
        ):
            return expressions
        if stage is not ParticleStage.EVENT or not event_id:
            raise ParticleCompileError("Event payload values are only valid in their Event flow")

        node_types = {node.uid: node.type_id for node in document.nodes}
        event_type = next(
            (value for value in events.event_types if value.stable_id == event_id),
            None,
        )
        if event_type is None:
            raise ParticleCompileError(f"Event flow references unknown event {event_id!r}")
        resolved = []
        for instruction in expressions.instructions:
            if instruction.opcode != "event_payload":
                resolved.append(instruction)
                continue
            type_id = node_types.get(instruction.source_node_uid, "")
            root_event_id = definitions.event_id_by_compile_type.get(type_id, "")
            field_id = definitions.event_field_by_port.get(
                (event_id, instruction.source_port_id), ""
            )
            if root_event_id != event_id or not field_id:
                raise ParticleCompileError("Event Payload node does not match this event ABI")
            field = next(
                (value for value in event_type.fields if value.stable_id == field_id),
                None,
            )
            if field is None or field.value_type != instruction.result_type:
                raise ParticleCompileError(
                    f"Event Payload field {field_id!r} does not match its node port"
                )
            resolved.append(
                ExpressionInstruction(
                    instruction.result_id,
                    "event_payload",
                    instruction.result_type,
                    instruction.operands,
                    instruction.source_node_uid,
                    instruction.source_port_id,
                    (
                        ("event_type_index", event_type.type_index),
                        ("field_stable_id", field.stable_id),
                        ("word_offset", field.word_offset),
                        ("word_count", field.word_count),
                        ("default", field.default),
                    ),
                )
            )
        return ExpressionProgram(
            tuple(resolved), expressions.outputs, expressions.semantic_hash
        )

    @staticmethod
    def _resolve_mesh_sample_expressions(
        expressions: ExpressionProgram,
        stage: ParticleStage,
        flow_id: str,
        definitions,
    ) -> ExpressionProgram:
        if not any(
            instruction.opcode == "sample_mesh"
            for instruction in expressions.instructions
        ):
            return expressions

        by_result = {
            instruction.result_id: instruction
            for instruction in expressions.instructions
        }
        resolved = []
        for instruction in expressions.instructions:
            if instruction.opcode != "sample_mesh":
                resolved.append(instruction)
                continue
            if len(instruction.operands) != 2:
                raise ParticleCompileError(
                    "Sample Mesh requires one Mesh and one sample-coordinate input"
                )
            mesh_operand, sample_operand = instruction.operands
            mesh_parameter = ""
            if mesh_operand.value_id:
                source = by_result.get(mesh_operand.value_id)
                if (
                    source is None
                    or source.opcode != "load_parameter"
                    or source.result_type.value_type is not ValueType.MESH
                ):
                    raise ParticleCompileError(
                        "Sample Mesh Mesh input requires a direct Mesh parameter connection"
                    )
                mesh_parameter = str(
                    source.immediate_dict().get("parameter", "")
                )
                parameter = definitions.parameter_by_id.get(mesh_parameter)
                if (
                    parameter is None
                    or parameter.value_type.value_type is not ValueType.MESH
                ):
                    raise ParticleCompileError(
                        f"Sample Mesh references unknown Mesh parameter {mesh_parameter!r}"
                    )
                reference = AssetReference.from_dict(parameter.default)
            else:
                reference = AssetReference.from_dict(mesh_operand.literal)

            identity = (
                f"parameter:{mesh_parameter}"
                if mesh_parameter
                else "constant:"
                + json.dumps(
                    reference.to_dict(), sort_keys=True, separators=(",", ":")
                )
            )
            interface_id = (
                "sample.mesh."
                + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            )
            immediates = instruction.immediate_dict()
            immediates.update(
                interface=interface_id,
                mesh=reference.to_dict(),
                mesh_parameter=mesh_parameter,
                seeded_sample=not bool(sample_operand.value_id),
            )
            resolved.append(
                replace(
                    instruction,
                    operands=(sample_operand,) if sample_operand.value_id else (),
                    immediates=tuple(sorted(immediates.items())),
                )
            )
        return replace(expressions, instructions=tuple(resolved))

    def _graph_operations(
        self,
        stage: ParticleStage,
        document,
        root_uid: str,
        value_bindings,
        definitions,
        expressions,
        owner_emitter_id: str,
        *,
        entry_operations: tuple[ParticleOperation, ...],
        entry_condition: str = "",
    ):
        registry = definitions.registry
        by_uid = {node.uid: node for node in document.nodes}
        outgoing: dict[str, list[tuple[str, str]]] = {}
        incoming: dict[str, list[str]] = {}
        exec_links = []
        for link in document.links:
            if link.kind is not PortKind.EXEC:
                continue
            source = by_uid.get(link.source_node)
            target = by_uid.get(link.target_node)
            source_def = registry.get(source.type_id) if source else None
            target_def = registry.get(target.type_id) if target else None
            source_port = source_def.port(link.source_port) if source_def else None
            target_port = target_def.port(link.target_port) if target_def else None
            if (
                source_port is None
                or source_port.direction is not PortDirection.OUTPUT
                or source_port.kind is not PortKind.EXEC
                or target_port is None
                or target_port.direction is not PortDirection.INPUT
                or target_port.kind is not PortKind.EXEC
            ):
                raise ParticleCompileError(f"invalid particle Exec link {link.uid!r}")
            outgoing.setdefault(link.source_node, []).append((link.uid, link.target_node))
            incoming.setdefault(link.target_node, []).append(link.uid)
            exec_links.append(link)

        multiple_inputs = {
            node_uid: tuple(sorted(link_ids))
            for node_uid, link_ids in incoming.items()
            if len(link_ids) > 1
        }
        for node_uid in sorted(multiple_inputs):
            node = by_uid.get(node_uid)
            definition = registry.get(node.type_id) if node else None
            opcode = definition.target_opcodes.get("particle_hir", "") if definition else ""
            if opcode != "control.join_all":
                raise ParticleCompileError(
                    f"particle Exec node {node_uid!r} has multiple inputs; use an explicit Join All node"
                )
        for node in document.nodes:
            if (
                node.type_id == "particle.attribute.get"
                and outgoing.get(node.uid)
                and not incoming.get(node.uid)
            ):
                raise ParticleCompileError(
                    f"Get Attribute node {node.uid!r} cannot use its Exec output "
                    "until its Exec input is connected"
                )

        reachable = set()
        pending = [root_uid]
        while pending:
            uid = pending.pop(0)
            if uid in reachable:
                continue
            reachable.add(uid)
            pending.extend(target for _, target in outgoing.get(uid, ()))

        ordered = []
        local_indegree = {uid: 0 for uid in reachable}
        for source_uid in reachable:
            for _, target_uid in outgoing.get(source_uid, ()):
                if target_uid in reachable:
                    local_indegree[target_uid] += 1
        ready = sorted(uid for uid in reachable if local_indegree[uid] == 0)
        while ready:
            uid = ready.pop(0)
            ordered.append(uid)
            for _, target in sorted(outgoing.get(uid, ())):
                local_indegree[target] -= 1
                if local_indegree[target] == 0:
                    ready.append(target)
                    ready.sort()
        if len(ordered) != len(reachable):
            raise ParticleCompileError("particle Exec graph contains a cycle")

        result = []
        for uid in ordered:
            if uid == root_uid:
                continue
            node = by_uid[uid]
            definition = registry.get(node.type_id)
            if definition is None:
                raise ParticleCompileError(f"unknown particle node type {node.type_id!r}")
            opcode = definition.target_opcodes.get("particle_hir", "")
            if not opcode:
                raise ParticleCompileError(f"node {node.type_id!r} has no Particle HIR target")
            properties = {item.id: item.default for item in definition.properties}
            editable_inputs = {
                port.id
                for port in definition.ports
                if port.direction is PortDirection.INPUT
                and port.kind is PortKind.VALUE
                and not port.required
            }
            properties.update(
                {
                    port.id: port.default
                    for port in definition.ports
                    if port.id in editable_inputs
                }
            )
            unknown = set(node.properties) - (set(properties) | editable_inputs)
            if unknown:
                raise ParticleCompileError(
                    f"node {node.type_id!r} has unknown properties: {sorted(unknown)}"
                )
            properties.update(node.properties)
            if opcode == "attribute.modify_cache":
                properties["attribute"] = particle_attribute_cache_id(
                    stage.value, node.uid
                )
            if opcode == "attribute.capture":
                properties["snapshot"] = particle_attribute_capture_id(stage.value, node.uid)
            specialized_event_id = definitions.event_id_by_compile_type.get(
                node.type_id, ""
            )
            if specialized_event_id and opcode == "event.trigger":
                properties["event"] = specialized_event_id
            if opcode == "emitter.burst":
                target_id = str(properties["emitter"])
                target_index = definitions.emitter_index_by_id.get(target_id)
                target_capacity = definitions.emitter_capacity_by_id.get(target_id)
                if target_index is None or target_capacity is None:
                    raise ParticleCompileError(
                        f"Burst node {node.uid!r} references unknown emitter {target_id!r}"
                    )
                properties["target_emitter_index"] = target_index
                properties["target_capacity"] = target_capacity
            if opcode == "emitter.set_playing":
                target_id = str(properties["emitter"])
                target_index = definitions.emitter_index_by_id.get(target_id)
                if target_index is None:
                    raise ParticleCompileError(
                        f"Set Emitter Playing node {node.uid!r} references unknown "
                        f"emitter {target_id!r}"
                    )
                if target_id == owner_emitter_id:
                    raise ParticleCompileError(
                        "Set Emitter Playing cannot target its owning emitter"
                    )
                properties["target_emitter_index"] = target_index
            if opcode == "parameter.store":
                parameter_id = str(properties["parameter"])
                parameter = definitions.parameter_by_id.get(parameter_id)
                if parameter is None:
                    raise ParticleCompileError(
                        f"Set Parameter node {node.uid!r} references unknown parameter "
                        f"{parameter_id!r}"
                    )
                if not bool(parameter.writable):
                    raise ParticleCompileError(
                        f"Set Parameter node {node.uid!r} cannot write read-only parameter "
                        f"{parameter.name!r}"
                    )
            from Infernux.graph.expression_ir import ExpressionCompiler

            for property_def in definition.properties:
                error = ExpressionCompiler._literal_error(
                    property_def.value_type,
                    properties[property_def.id],
                )
                if error:
                    raise ParticleCompileError(
                        f"node {node.type_id!r}.{property_def.id} {error}"
                    )
                if property_def.choices and properties[property_def.id] not in {
                    value for _label, value in property_def.choices
                }:
                    raise ParticleCompileError(
                        f"node {node.type_id!r}.{property_def.id} must be one of "
                        f"{[value for _label, value in property_def.choices]}"
                    )
            result.append(
                ParticleOperation(
                    opcode,
                    tuple(sorted(properties.items())),
                    node.uid,
                    tuple(
                        sorted(
                            (property_id, value_id)
                            for (target_uid, property_id), value_id in value_bindings.items()
                            if target_uid == node.uid
                        )
                    ),
                )
            )

        link_by_uid = {link.uid: link for link in exec_links}
        operation_by_uid = {operation.source_node_uid: operation for operation in result}
        predicates_by_uid: dict[str, tuple[ParticleExecutionPredicate, ...]] = {
            root_uid: ()
        }
        predicated_result = []
        for uid in ordered:
            if uid == root_uid:
                continue
            input_link_ids = incoming.get(uid, ())
            operation = operation_by_uid[uid]
            if operation.opcode == "control.join_all":
                if len(input_link_ids) < 2:
                    raise ParticleCompileError(
                        f"Join All node {uid!r} requires at least two execution inputs"
                    )
                joined_predicates = []
                for link_id in sorted(input_link_ids):
                    source_uid = link_by_uid[link_id].source_node
                    predicates = predicates_by_uid.get(source_uid)
                    if predicates is None:
                        raise ParticleCompileError(
                            f"Join All node {uid!r} has an input from unreachable "
                            f"Exec node {source_uid!r}"
                        )
                    joined_predicates.append(predicates)
                joined_predicates = tuple(joined_predicates)
                predicates = joined_predicates[0]
                if any(value != predicates for value in joined_predicates[1:]):
                    raise ParticleCompileError(
                        f"Join All node {uid!r} cannot join mutually exclusive or differently predicated branches"
                    )
                input_link = None
            elif len(input_link_ids) != 1:
                raise ParticleCompileError(
                    f"particle Exec node {uid!r} must have exactly one execution input"
                )
            else:
                input_link = link_by_uid[input_link_ids[0]]
                predicates = predicates_by_uid[input_link.source_node]
                source_operation = operation_by_uid.get(input_link.source_node)
                if source_operation is not None and source_operation.opcode == "control.if":
                    if input_link.source_port not in {"true", "false"}:
                        raise ParticleCompileError(
                            f"If node {input_link.source_node!r} has invalid Exec output "
                            f"{input_link.source_port!r}"
                        )
                    source_bindings = dict(source_operation.value_bindings)
                    predicates = predicates + (
                        ParticleExecutionPredicate(
                            source_operation.source_node_uid,
                            source_bindings.get("condition", ""),
                            bool(source_operation.parameter_dict().get("condition", False)),
                            input_link.source_port == "true",
                        ),
                    )
            if entry_condition:
                predicates = predicates + (
                    ParticleExecutionPredicate(
                        root_uid,
                        "",
                        False,
                        True,
                        entry_condition,
                    ),
                )
            predicates_by_uid[uid] = predicates
            operation = ParticleOperation(
                operation.opcode,
                operation.parameters,
                operation.source_node_uid,
                operation.value_bindings,
                predicates,
            )
            predicated_result.append(operation)
            operation_by_uid[uid] = operation
        result = predicated_result
        descendants: dict[str, set[str]] = {uid: set() for uid in reachable}
        for uid in reversed(ordered):
            for _link_uid, target_uid in outgoing.get(uid, ()):
                if target_uid not in reachable:
                    continue
                descendants[uid].add(target_uid)
                descendants[uid].update(descendants[target_uid])
        operation_uids = [uid for uid in ordered if uid != root_uid]
        expression_reads = self._expression_attribute_reads(expressions)
        for index, left_uid in enumerate(operation_uids):
            left = operation_by_uid[left_uid]
            left_writes = self._operation_writes(left)
            left_reads = self._operation_reads(left, expression_reads)
            if not left_writes and not left_reads:
                continue
            for right_uid in operation_uids[index + 1 :]:
                if right_uid in descendants[left_uid] or left_uid in descendants[right_uid]:
                    continue
                right = operation_by_uid[right_uid]
                left_conditions = {
                    predicate.source_node_uid: predicate.expected
                    for predicate in left.execution_predicates
                }
                right_conditions = {
                    predicate.source_node_uid: predicate.expected
                    for predicate in right.execution_predicates
                }
                if any(
                    source_uid in right_conditions
                    and right_conditions[source_uid] != expected
                    for source_uid, expected in left_conditions.items()
                ):
                    continue
                right_writes = self._operation_writes(right)
                right_reads = self._operation_reads(right, expression_reads)
                conflicts = (
                    (left_writes & right_writes)
                    | (left_writes & right_reads)
                    | (right_writes & left_reads)
                )
                if conflicts:
                    joined = ", ".join(sorted(conflicts))
                    raise ParticleCompileError(
                        "parallel particle Exec branches have an unordered state dependency "
                        f"({joined}): {left_uid!r} and {right_uid!r}; "
                        "connect them in sequence or use an explicit Join All"
                    )

        reachable_links_without_lanes = tuple(
            sorted(
                (
                    ParticleExecEdge(
                        link.uid,
                        link.source_node,
                        link.source_port,
                        link.target_node,
                        link.target_port,
                        (
                            link.source_node
                            if operation_by_uid.get(link.source_node) is not None
                            and operation_by_uid[link.source_node].opcode == "control.if"
                            else ""
                        ),
                        (
                            link.source_port == "true"
                            if operation_by_uid.get(link.source_node) is not None
                            and operation_by_uid[link.source_node].opcode == "control.if"
                            else None
                        ),
                    )
                    for link in exec_links
                    if link.source_node in reachable and link.target_node in reachable
                ),
                key=lambda edge: (
                    edge.source_node_uid,
                    edge.source_port_id,
                    edge.target_node_uid,
                    edge.target_port_id,
                ),
            )
        )
        lanes, joins, edge_lanes, block_lanes = self._execution_lane_layout(
            root_uid,
            ordered,
            operation_by_uid,
            reachable_links_without_lanes,
        )
        reachable_links = tuple(
            ParticleExecEdge(
                edge.link_uid,
                edge.source_node_uid,
                edge.source_port_id,
                edge.target_node_uid,
                edge.target_port_id,
                edge.predicate_node_uid,
                edge.predicate_expected,
                edge_lanes[edge.link_uid],
            )
            for edge in reachable_links_without_lanes
        )
        suspensions = []
        suspension_operations = [
            operation
            for operation in result
            if operation.opcode
            in {
                "control.wait_frames",
                "control.wait_seconds",
                "control.until_frames",
                "control.until_seconds",
            }
        ]
        for resume_program_counter, operation in enumerate(
            suspension_operations, start=1
        ):
            is_until = operation.opcode.startswith("control.until_")
            outgoing_edges = tuple(
                edge
                for edge in reachable_links
                if edge.source_node_uid == operation.source_node_uid
            )
            if len(outgoing_edges) > 1:
                raise ParticleCompileError(
                    f"Wait/Until node {operation.source_node_uid!r} requires at most one Exec output"
                )
            edge = outgoing_edges[0] if outgoing_edges else None
            parameter_name = (
                "frames"
                if operation.opcode.endswith("_frames")
                else "seconds"
            )
            literal = operation.parameter_dict()[parameter_name]
            if float(literal) < 0.0:
                raise ParticleCompileError(
                    f"Wait/Until node {operation.source_node_uid!r} duration cannot be negative"
                )
            value_id = dict(operation.value_bindings).get(parameter_name, "")
            lane_index = block_lanes[operation.source_node_uid]
            if is_until:
                input_link_ids = incoming.get(operation.source_node_uid, ())
                if len(input_link_ids) != 1:
                    raise ParticleCompileError(
                        f"Until node {operation.source_node_uid!r} requires exactly one Exec input"
                    )
                resume_node_uid = link_by_uid[input_link_ids[0]].source_node
                if resume_node_uid == root_uid:
                    raise ParticleCompileError(
                        f"Until node {operation.source_node_uid!r} requires an operation before it"
                    )
            else:
                resume_node_uid = edge.target_node_uid if edge is not None else ""
            suspensions.append(
                ParticleSuspensionPoint(
                    operation.source_node_uid,
                    (
                        ParticleSuspensionKind.UNTIL_FRAMES
                        if operation.opcode == "control.until_frames"
                        else ParticleSuspensionKind.UNTIL_SECONDS
                        if operation.opcode == "control.until_seconds"
                        else ParticleSuspensionKind.FRAMES
                        if operation.opcode == "control.wait_frames"
                        else ParticleSuspensionKind.SECONDS
                    ),
                    lane_index,
                    lanes[lane_index].stable_id,
                    resume_program_counter,
                    resume_node_uid,
                    value_id,
                    literal,
                )
            )
        flow = ParticleFlowProgram(
            root_uid,
            tuple(
                ParticleFlowBlock(
                    uid,
                    (
                        entry_operations
                        if uid == root_uid
                        else (operation_by_uid[uid],)
                    ),
                    tuple(
                        edge.link_uid
                        for edge in reachable_links
                        if edge.target_node_uid == uid
                    ),
                    tuple(
                        edge.link_uid
                        for edge in reachable_links
                        if edge.source_node_uid == uid
                    ),
                    block_lanes[uid],
                )
                for uid in ordered
            ),
            reachable_links,
            lanes,
            joins,
            tuple(suspensions),
        )
        return flow

    @staticmethod
    def _execution_lane_layout(
        root_uid: str,
        ordered: list[str],
        operation_by_uid: Mapping[str, ParticleOperation],
        edges: tuple[ParticleExecEdge, ...],
    ):
        def stable_id(*parts: str) -> str:
            encoded = "\x1f".join(parts).encode("utf-8")
            return hashlib.sha256(encoded).hexdigest()[:32]

        outgoing: dict[str, list[ParticleExecEdge]] = {}
        incoming: dict[str, list[ParticleExecEdge]] = {}
        for edge in edges:
            outgoing.setdefault(edge.source_node_uid, []).append(edge)
            incoming.setdefault(edge.target_node_uid, []).append(edge)
        for values in (*outgoing.values(), *incoming.values()):
            values.sort(
                key=lambda edge: (
                    edge.source_node_uid,
                    edge.source_port_id,
                    edge.target_node_uid,
                    edge.target_port_id,
                )
            )

        lanes = [
            ParticleExecutionLane(
                stable_id("root", root_uid),
                0,
                -1,
                root_uid,
                "",
            )
        ]
        block_lanes = {root_uid: 0}
        edge_lanes: dict[str, int] = {}
        joins = []

        def add_lane(parent: int, source_uid: str, source_port: str, target_uid: str) -> int:
            index = len(lanes)
            lanes.append(
                ParticleExecutionLane(
                    stable_id(
                        "lane",
                        lanes[parent].stable_id,
                        source_uid,
                        source_port,
                        target_uid,
                    ),
                    index,
                    parent,
                    source_uid,
                    source_port,
                )
            )
            return index

        for uid in ordered:
            operation = operation_by_uid.get(uid)
            if operation is not None and operation.opcode == "control.join_all":
                input_lanes = tuple(
                    edge_lanes[edge.link_uid]
                    for edge in incoming.get(uid, ())
                )
                parent = min(input_lanes)
                lane_index = add_lane(parent, uid, "out", uid)
                block_lanes[uid] = lane_index
                joins.append(ParticleJoinDescriptor(uid, input_lanes, lane_index))
            lane_index = block_lanes[uid]
            outgoing_edges = outgoing.get(uid, ())
            fork = len(outgoing_edges) > 1
            for edge in outgoing_edges:
                edge_lane = (
                    add_lane(
                        lane_index,
                        uid,
                        edge.source_port_id,
                        edge.target_node_uid,
                    )
                    if fork
                    else lane_index
                )
                edge_lanes[edge.link_uid] = edge_lane
                target_operation = operation_by_uid.get(edge.target_node_uid)
                if target_operation is None or target_operation.opcode != "control.join_all":
                    block_lanes[edge.target_node_uid] = edge_lane

        return tuple(lanes), tuple(joins), edge_lanes, block_lanes

    @staticmethod
    def _operation_writes(operation: ParticleOperation) -> frozenset[str]:
        opcode = operation.opcode
        writes = {
            "emitter.sample_shape": {"builtin.position"},
            "motion.target_position": {"builtin.velocity"},
            "collision.plane": {
                "builtin.position",
                "builtin.velocity",
                "builtin.collision_hit",
                "builtin.collision_normal",
            },
            "collision.sphere": {
                "builtin.position",
                "builtin.velocity",
                "builtin.collision_hit",
                "builtin.collision_normal",
            },
            "collision.sdf": {
                "builtin.position",
                "builtin.velocity",
                "builtin.collision_hit",
                "builtin.collision_normal",
            },
        }
        if opcode in ATTRIBUTE_OPERATION_SPECS:
            return frozenset({ATTRIBUTE_OPERATION_SPECS[opcode][0]})
        if opcode == "attribute.modify_cache":
            return frozenset({str(operation.parameter_dict()["attribute"])})
        if opcode == "attribute.capture":
            return frozenset({str(operation.parameter_dict()["snapshot"])})
        return frozenset(writes.get(opcode, ()))

    @staticmethod
    def _expression_attribute_reads(expressions) -> dict[str, frozenset[str]]:
        reads: dict[str, frozenset[str]] = {}
        for instruction in expressions.instructions:
            inherited = set()
            for operand in instruction.operands:
                if operand.value_id:
                    inherited.update(reads.get(operand.value_id, ()))
            if instruction.opcode == "load_attribute":
                inherited.add(str(instruction.immediate_dict()["attribute"]))
            reads[instruction.result_id] = frozenset(inherited)
        return reads

    @staticmethod
    def _operation_reads(
        operation: ParticleOperation,
        expression_reads: Mapping[str, frozenset[str]],
    ) -> frozenset[str]:
        reads = {
            "motion.target_position": {"builtin.position", "builtin.velocity"},
            "collision.plane": {"builtin.position", "builtin.velocity"},
            "collision.sphere": {"builtin.position", "builtin.velocity"},
            "collision.sdf": {"builtin.position", "builtin.velocity"},
        }
        result = set(reads.get(operation.opcode, ()))
        if (
            operation.opcode in ATTRIBUTE_OPERATION_SPECS
            and operation.parameter_dict().get("composition", "set") != "set"
        ):
            result.add(ATTRIBUTE_OPERATION_SPECS[operation.opcode][0])
        if (
            operation.opcode == "attribute.modify_cache"
            and operation.parameter_dict().get("composition", "set") != "set"
        ):
            result.add(str(operation.parameter_dict()["attribute"]))
        if operation.opcode == "attribute.capture":
            result.add(str(operation.parameter_dict()["attribute"]))
        for _property_id, value_id in operation.value_bindings:
            result.update(expression_reads.get(value_id, ()))
        for predicate in operation.execution_predicates:
            if predicate.value_id:
                result.update(expression_reads.get(predicate.value_id, ()))
        return frozenset(result)

    @staticmethod
    def _settings_operations(stage: ParticleStage, settings: EmitterSettings):
        if stage is ParticleStage.INIT:
            return (
                ParticleOperation(
                    "emitter.sample_shape",
                    tuple(
                        sorted(
                            {
                                "seed": settings.seed,
                                "shape": settings.shape.kind.value,
                                "shape_space": settings.shape.space.value,
                                "shape_radius": settings.shape.radius,
                                "shape_angle_degrees": settings.shape.angle_degrees,
                                "shape_dimensions": list(settings.shape.dimensions),
                                "shape_mesh": settings.shape.mesh.to_dict(),
                                "shape_mesh_mode": settings.shape.mesh_mode.value,
                                "shape_sdf_interface": settings.shape.sdf_interface,
                                "shape_sdf_mode": settings.shape.sdf_mode.value,
                            }.items()
                        )
                    ),
                    "settings.init",
                ),
            )
        return ()


def _stable_u64(value: str) -> int:
    result = int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")
    return result or 1


def _event_word_count(value_type: TypeRef) -> int:
    widths = {
        ValueType.BOOL: 1,
        ValueType.I32: 1,
        ValueType.U32: 1,
        ValueType.F32: 1,
        ValueType.VEC2: 2,
        ValueType.VEC3: 3,
        ValueType.VEC4: 4,
        ValueType.COLOR: 4,
        # Mat3 columns retain their std430 padding so a payload can be copied
        # directly into generated GPU code without backend-specific repacking.
        ValueType.MAT3: 12,
        ValueType.MAT4: 16,
    }
    try:
        return widths[value_type.value_type]
    except KeyError as exc:
        raise ParticleCompileError(
            f"particle event field type {value_type.value_type.value!r} is not GPU portable"
        ) from exc


__all__ = [
    "ParticleCompileError",
    "ParticleExecutionPredicate",
    "ParticleExecEdge",
    "ParticleExecutionLane",
    "ParticleFlowBlock",
    "ParticleFlowProgram",
    "ParticleJoinDescriptor",
    "ParticleEmitterHIR",
    "ParticleEventFieldHIR",
    "ParticleEventSchedule",
    "ParticleEventTypeHIR",
    "ParticleGraphCompiler",
    "ParticleOperation",
    "ParticleOutputDescriptor",
    "ParticleOutputShaderProperty",
    "ParticleProgramHIR",
    "ParticleRenderPlan",
    "ParticleStage",
    "ParticleStageHIR",
    "ParticleSuspensionKind",
    "ParticleSuspensionPoint",
    "ParticleSystemSchedule",
]
