"""Backend-independent Particle Program HIR and Graph frontend compiler."""

from __future__ import annotations

from dataclasses import dataclass
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
from Infernux.graph.types import AssetReference, TypeRef, ValueType

from .asset import EmitterSettings, ParticleAttribute, ParticleGraphAsset
from .data_interface import ParticleDataInterface, SdfVolume
from .nodes import particle_event_output_type_id, particle_graph_node_definitions


class ParticleStage(str, Enum):
    INIT = "init"
    UPDATE = "update"
    RENDERING = "rendering"


class ParticleCompileError(ValueError):
    pass


@dataclass(frozen=True)
class ParticleOperation:
    opcode: str
    parameters: tuple[tuple[str, Any], ...]
    source_node_uid: str
    value_bindings: tuple[tuple[str, str], ...] = ()

    def parameter_dict(self) -> dict[str, Any]:
        return dict(self.parameters)


@dataclass(frozen=True)
class ParticleStageHIR:
    stage: ParticleStage
    root_uid: str
    expressions: ExpressionProgram
    operations: tuple[ParticleOperation, ...]


@dataclass(frozen=True)
class ParticleOutputDescriptor:
    output_id: str
    output_type: str
    mesh: AssetReference
    material: AssetReference
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
    rendering: ParticleStageHIR
    render_plan: ParticleRenderPlan
    data_interfaces: tuple[ParticleDataInterface, ...] = ()


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
    capacity_per_step: int
    payload_stride_words: int
    fields: tuple[ParticleEventFieldHIR, ...]


@dataclass(frozen=True)
class ParticleEventRouteHIR:
    stable_id: str
    event_type_id: str
    event_type_index: int
    source_emitter_id: str
    source_emitter_index: int
    source_stage: ParticleStage
    target_emitter_id: str
    target_emitter_index: int
    spawn_count: int
    capacity: int
    payload_stride_words: int


@dataclass(frozen=True)
class ParticleEventSchedule:
    event_abi_hash: str
    event_types: tuple[ParticleEventTypeHIR, ...]
    routes: tuple[ParticleEventRouteHIR, ...]

    @property
    def event_abi_u64(self) -> int:
        value = int(self.event_abi_hash[:16], 16)
        return value or 1


@dataclass(frozen=True)
class ParticleProgramHIR:
    stable_id: str
    name: str
    semantic_hash: str
    behavior_hash: str
    emitters: tuple[ParticleEmitterHIR, ...]
    schedule: ParticleSystemSchedule
    events: ParticleEventSchedule


class ParticleGraphCompiler:
    """Lower strict ParticleGraph assets into backend-independent HIR."""

    def compile(self, asset: ParticleGraphAsset) -> ParticleProgramHIR:
        events = self._compile_events(asset)
        definitions = particle_graph_node_definitions(asset)
        emitters = tuple(
            self._compile_emitter(emitter, events, definitions)
            for emitter in asset.emitters
        )
        return ParticleProgramHIR(
            asset.stable_id,
            asset.name,
            asset.semantic_hash(),
            self._behavior_hash(emitters, events),
            emitters,
            ParticleSystemSchedule(tuple(emitter.stable_id for emitter in emitters)),
            events,
        )

    @staticmethod
    def _behavior_hash(emitters, events: ParticleEventSchedule) -> str:
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
            operations = [
                {
                    "opcode": operation.opcode,
                    "parameters": list(operation.parameters),
                    "value_bindings": [
                        (name, canonical_ids.get(value_id, value_id))
                        for name, value_id in operation.value_bindings
                    ],
                }
                for operation in stage_hir.operations
            ]
            return {"expressions": expressions, "operations": operations}

        payload = []
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
                    "stages": {
                        stage_name: stage_payload(getattr(emitter, stage_name))
                        for stage_name in ("init", "update", "rendering")
                    },
                    "outputs": [
                        {
                            "type": output.output_type,
                            "mesh": output.mesh.to_dict(),
                            "material": output.material.to_dict(),
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
                        "capacity": value.capacity_per_step,
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
                "event_routes": [
                    {
                        "stable_id": route.stable_id,
                        "event_type": route.event_type_id,
                        "source_emitter": route.source_emitter_id,
                        "source_stage": route.source_stage.value,
                        "target_emitter": route.target_emitter_id,
                        "spawn_count": route.spawn_count,
                    }
                    for route in events.routes
                ],
            }
        )
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _compile_events(asset: ParticleGraphAsset) -> ParticleEventSchedule:
        emitter_indices = {
            emitter.stable_id: index for index, emitter in enumerate(asset.emitters)
        }
        event_types = []
        event_type_by_id = {}
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
                event_type.capacity_per_step,
                word_offset,
                tuple(fields),
            )
            event_types.append(lowered)
            event_type_by_id[event_type.stable_id] = lowered

        routes = []
        for route in asset.event_routes:
            event_type = event_type_by_id[route.event_type_id]
            routes.append(
                ParticleEventRouteHIR(
                    route.stable_id,
                    event_type.stable_id,
                    event_type.type_index,
                    route.source_emitter_id,
                    emitter_indices[route.source_emitter_id],
                    ParticleStage(route.source_stage),
                    route.target_emitter_id,
                    emitter_indices[route.target_emitter_id],
                    route.spawn_count,
                    event_type.capacity_per_step,
                    event_type.payload_stride_words,
                )
            )

        abi_payload = {
            "emitters": [emitter.stable_id for emitter in asset.emitters],
            "event_types": [
                {
                    "stable_id": value.stable_id,
                    "stable_type_hash": value.stable_type_hash,
                    "capacity": value.capacity_per_step,
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
            "routes": [
                {
                    "stable_id": route.stable_id,
                    "event_type_index": route.event_type_index,
                    "source_emitter_index": route.source_emitter_index,
                    "source_stage": route.source_stage.value,
                    "target_emitter_index": route.target_emitter_index,
                    "spawn_count": route.spawn_count,
                }
                for route in routes
            ],
        }
        encoded = json.dumps(abi_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return ParticleEventSchedule(
            hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            tuple(event_types),
            tuple(routes),
        )

    def _compile_emitter(
        self,
        emitter,
        events: ParticleEventSchedule,
        definitions,
    ) -> ParticleEmitterHIR:
        init = self._compile_stage(
            ParticleStage.INIT,
            emitter.init,
            emitter.settings,
            definitions,
            events,
            emitter,
        )
        update = self._compile_stage(
            ParticleStage.UPDATE,
            emitter.update,
            emitter.settings,
            definitions,
            events,
            emitter,
        )
        rendering = self._compile_stage(
            ParticleStage.RENDERING,
            emitter.rendering,
            emitter.settings,
            definitions,
            events,
            emitter,
        )
        routes_by_id = {route.stable_id: route for route in events.routes}
        stage_documents = {
            ParticleStage.INIT: emitter.init,
            ParticleStage.UPDATE: emitter.update,
            ParticleStage.RENDERING: emitter.rendering,
        }
        for stage in (init, update, rendering):
            for operation in stage.operations:
                if operation.opcode != "event.emit":
                    continue
                route_id = str(operation.parameter_dict()["route"])
                route = routes_by_id.get(route_id)
                if route is None:
                    raise ParticleCompileError(
                        f"Event Output {operation.source_node_uid!r} references unknown route {route_id!r}"
                    )
                if route.source_emitter_id != emitter.stable_id:
                    raise ParticleCompileError(
                        f"Event Output route {route_id!r} belongs to emitter "
                        f"{route.source_emitter_id!r}, not {emitter.stable_id!r}"
                    )
                if route.source_stage is not stage.stage:
                    raise ParticleCompileError(
                        f"Event Output route {route_id!r} belongs to the "
                        f"{route.source_stage.value} stage, not {stage.stage.value}"
                    )
                expected_type_id = particle_event_output_type_id(
                    route.stable_id, route.source_stage.value
                )
                source_node = next(
                    (
                        node
                        for node in stage_documents[stage.stage].nodes
                        if node.uid == operation.source_node_uid
                    ),
                    None,
                )
                if source_node is None or source_node.type_id != expected_type_id:
                    actual = source_node.type_id if source_node is not None else ""
                    raise ParticleCompileError(
                        f"Event Output route {route_id!r} does not match node type {actual!r}"
                    )
        outputs = tuple(
            self._compile_output(operation)
            for operation in rendering.operations
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
                and (
                    output.soft_particles
                    or output.sort_mode != "none"
                )
            ),
            None,
        )
        if unsupported_mesh_semantics is not None:
            raise ParticleCompileError(
                f"particle mesh output {unsupported_mesh_semantics.output_id!r} currently "
                "supports unsorted, non-soft rendering only"
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
        collision_opcodes = {"collision.plane", "collision.sphere", "collision.sdf"}
        collision_indices = [
            index
            for index, operation in enumerate(update.operations)
            if operation.opcode in collision_opcodes
        ]
        if collision_indices and any(
            operation.opcode not in collision_opcodes | {"event.emit"}
            for operation in update.operations[collision_indices[0] :]
        ):
            raise ParticleCompileError(
                "Collision nodes must form the final operation group in the Update stream"
            )
        for operation in (update.operations[index] for index in collision_indices):
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
                if name in bindings:
                    continue
                value = float(parameters[name])
                if name in radius_names and value < 0.0:
                    raise ParticleCompileError(f"{label} {name} must be non-negative")
                if name not in radius_names and not 0.0 <= value <= 1.0:
                    raise ParticleCompileError(
                        f"{label} {name} must be between 0 and 1"
                    )
        orientation_opcodes = {
            "attribute.set_orientation",
            "integrate.angular_velocity_3d",
        }
        scale_opcodes = {"attribute.set_scale"}
        needs_orientation = any(output.output_type == "mesh" for output in outputs) or any(
            operation.opcode in orientation_opcodes
            for stage in (init, update, rendering)
            for operation in stage.operations
        )
        needs_scale = any(output.output_type == "mesh" for output in outputs) or any(
            operation.opcode in scale_opcodes
            for stage in (init, update, rendering)
            for operation in stage.operations
        )
        needs_flipbook_frame = any(
            operation.opcode == "attribute.set_flipbook_frame"
            for stage in (init, update, rendering)
            for operation in stage.operations
        )
        attributes = emitter.attributes
        if needs_orientation:
            attributes = (
                *attributes,
                ParticleAttribute(
                    "builtin.orientation",
                    "orientation",
                    TypeRef(ValueType.VEC3),
                    [0.0, 0.0, 0.0],
                ),
            )
        if needs_scale:
            attributes = (
                *attributes,
                ParticleAttribute(
                    "builtin.scale",
                    "scale",
                    TypeRef(ValueType.VEC3),
                    [1.0, 1.0, 1.0],
                ),
            )
        if needs_flipbook_frame:
            attributes = (
                *attributes,
                ParticleAttribute(
                    "builtin.flipbook_frame",
                    "flipbook_frame",
                    TypeRef(ValueType.F32),
                    0.0,
                ),
            )
        if any(output.output_type == "ribbon" for output in outputs):
            attributes = (
                *attributes,
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
            )
        return ParticleEmitterHIR(
            emitter.stable_id,
            emitter.name,
            emitter.settings,
            attributes,
            init,
            update,
            rendering,
            ParticleRenderPlan(outputs),
            emitter.data_interfaces,
        )

    @staticmethod
    def _compile_output(operation: ParticleOperation) -> ParticleOutputDescriptor:
        parameters = operation.parameter_dict()
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
            AssetReference.from_dict(parameters["material"]),
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
        )

    def _compile_stage(
        self,
        stage: ParticleStage,
        document,
        settings: EmitterSettings,
        definitions,
        events: ParticleEventSchedule,
        emitter,
    ) -> ParticleStageHIR:
        registry = definitions.registry
        root_type = f"particle.root.{stage.value}"
        root = next(node for node in document.nodes if node.type_id == root_type)
        value_links = sorted(
            (link for link in document.links if link.kind is PortKind.VALUE),
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
        try:
            expressions = ExpressionCompiler(
                registry,
                definition_fingerprint=definitions.abi_fingerprint,
            ).compile(
                document, expression_outputs
            )
        except ExpressionCompileError as exc:
            raise ParticleCompileError(str(exc)) from exc
        expressions = self._resolve_event_payload_expressions(
            expressions,
            document,
            stage,
            emitter.stable_id,
            definitions,
            events,
        )
        value_bindings = {
            (link.target_node, link.target_port): result_id
            for link, (result_id, _value_type) in zip(value_links, expressions.outputs)
        }
        operations = list(self._settings_operations(stage, settings))
        operations.extend(
            self._graph_operations(document, root.uid, value_bindings, definitions)
        )
        return ParticleStageHIR(stage, root.uid, expressions, tuple(operations))

    @staticmethod
    def _resolve_event_payload_expressions(
        expressions: ExpressionProgram,
        document,
        stage: ParticleStage,
        emitter_id: str,
        definitions,
        events: ParticleEventSchedule,
    ) -> ExpressionProgram:
        if not any(
            instruction.opcode == "event_payload"
            for instruction in expressions.instructions
        ):
            return expressions
        if stage is not ParticleStage.INIT:
            raise ParticleCompileError("Event Payload is only valid in the Init stage")

        node_types = {node.uid: node.type_id for node in document.nodes}
        routes = {
            route.stable_id: (index, route)
            for index, route in enumerate(events.routes)
        }
        event_types = {
            event_type.type_index: event_type for event_type in events.event_types
        }
        resolved = []
        for instruction in expressions.instructions:
            if instruction.opcode != "event_payload":
                resolved.append(instruction)
                continue
            type_id = node_types.get(instruction.source_node_uid, "")
            route_id = definitions.event_input_route_by_type_id.get(type_id, "")
            field_id = definitions.event_field_by_port.get(
                (type_id, instruction.source_port_id), ""
            )
            route_entry = routes.get(route_id)
            if route_entry is None or not field_id:
                raise ParticleCompileError("Event Payload node does not match this event ABI")
            channel_index, route = route_entry
            if route.target_emitter_id != emitter_id:
                raise ParticleCompileError(
                    f"Event Payload route {route_id!r} belongs to target emitter "
                    f"{route.target_emitter_id!r}, not {emitter_id!r}"
                )
            event_type = event_types[route.event_type_index]
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
                        ("channel_index", channel_index),
                        ("word_offset", field.word_offset),
                        ("word_count", field.word_count),
                        ("default", field.default),
                    ),
                )
            )
        return ExpressionProgram(
            tuple(resolved), expressions.outputs, expressions.semantic_hash
        )

    def _graph_operations(self, document, root_uid: str, value_bindings, definitions):
        registry = definitions.registry
        by_uid = {node.uid: node for node in document.nodes}
        outgoing: dict[str, list[tuple[str, str]]] = {}
        for link in document.links:
            if link.kind is not PortKind.STREAM:
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
                or source_port.kind is not PortKind.STREAM
                or target_port is None
                or target_port.direction is not PortDirection.INPUT
                or target_port.kind is not PortKind.STREAM
            ):
                raise ParticleCompileError(f"invalid particle stream link {link.uid!r}")
            outgoing.setdefault(link.source_node, []).append((link.uid, link.target_node))

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
            raise ParticleCompileError("particle stream graph contains a cycle")

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
            if node.type_id in definitions.event_route_by_type_id:
                properties["route"] = definitions.event_route_by_type_id[node.type_id]
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
        return tuple(result)

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
    "ParticleEmitterHIR",
    "ParticleEventFieldHIR",
    "ParticleEventRouteHIR",
    "ParticleEventSchedule",
    "ParticleEventTypeHIR",
    "ParticleGraphCompiler",
    "ParticleOperation",
    "ParticleOutputDescriptor",
    "ParticleProgramHIR",
    "ParticleRenderPlan",
    "ParticleStage",
    "ParticleStageHIR",
    "ParticleSystemSchedule",
]
