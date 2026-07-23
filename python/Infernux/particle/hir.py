"""Backend-independent Particle Program HIR and Graph frontend compiler."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from typing import Any, Mapping

from Infernux.graph.registry import COMMON_NODE_REGISTRY, PortDirection, PortKind
from Infernux.graph.expression_ir import ExpressionCompileError, ExpressionCompiler, ExpressionProgram
from Infernux.graph.types import AssetReference, TypeRef, ValueType

from .asset import EmitterSettings, ParticleAttribute, ParticleGraphAsset
from .data_interface import ParticleDataInterface


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
    soft_particles: bool
    soft_distance: float
    sort_mode: str


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
class ParticleProgramHIR:
    stable_id: str
    name: str
    semantic_hash: str
    behavior_hash: str
    emitters: tuple[ParticleEmitterHIR, ...]
    schedule: ParticleSystemSchedule


class ParticleGraphCompiler:
    """Lower strict ParticleGraph assets into backend-independent HIR."""

    def compile(self, asset: ParticleGraphAsset) -> ParticleProgramHIR:
        emitters = tuple(self._compile_emitter(emitter) for emitter in asset.emitters)
        return ParticleProgramHIR(
            asset.stable_id,
            asset.name,
            asset.semantic_hash(),
            self._behavior_hash(emitters),
            emitters,
            ParticleSystemSchedule(tuple(emitter.stable_id for emitter in emitters)),
        )

    @staticmethod
    def _behavior_hash(emitters) -> str:
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
                            "soft_particles": output.soft_particles,
                            "soft_distance": output.soft_distance,
                            "sort": output.sort_mode,
                        }
                        for output in emitter.render_plan.outputs
                    ],
                }
            )
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _compile_emitter(self, emitter) -> ParticleEmitterHIR:
        init = self._compile_stage(ParticleStage.INIT, emitter.init, emitter.settings)
        update = self._compile_stage(ParticleStage.UPDATE, emitter.update, emitter.settings)
        rendering = self._compile_stage(
            ParticleStage.RENDERING,
            emitter.rendering,
            emitter.settings,
        )
        outputs = tuple(
            self._compile_output(operation)
            for operation in rendering.operations
            if operation.opcode in {"render.sprite", "render.mesh"}
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
        orientation_opcodes = {
            "attribute.set_orientation",
            "integrate.angular_velocity_3d",
        }
        needs_orientation = any(output.output_type == "mesh" for output in outputs) or any(
            operation.opcode in orientation_opcodes
            for stage in (init, update)
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
        output_type = "sprite" if operation.opcode == "render.sprite" else "mesh"
        return ParticleOutputDescriptor(
            operation.source_node_uid,
            output_type,
            AssetReference.from_dict(
                parameters.get("mesh", AssetReference().to_dict())
            ),
            AssetReference.from_dict(parameters["material"]),
            bool(parameters["receive_scene_lighting"]),
            bool(parameters["receive_shadows"]),
            bool(parameters.get("soft_particles", False)),
            float(parameters.get("soft_distance", 1.0)),
            str(parameters["sort"]),
        )

    def _compile_stage(
        self,
        stage: ParticleStage,
        document,
        settings: EmitterSettings,
    ) -> ParticleStageHIR:
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
            expressions = ExpressionCompiler().compile(document, expression_outputs)
        except ExpressionCompileError as exc:
            raise ParticleCompileError(str(exc)) from exc
        value_bindings = {
            (link.target_node, link.target_port): result_id
            for link, (result_id, _value_type) in zip(value_links, expressions.outputs)
        }
        operations = list(self._settings_operations(stage, settings))
        operations.extend(self._graph_operations(document, root.uid, value_bindings))
        return ParticleStageHIR(stage, root.uid, expressions, tuple(operations))

    def _graph_operations(self, document, root_uid: str, value_bindings):
        by_uid = {node.uid: node for node in document.nodes}
        outgoing: dict[str, list[tuple[str, str]]] = {}
        for link in document.links:
            if link.kind is not PortKind.STREAM:
                continue
            source = by_uid.get(link.source_node)
            target = by_uid.get(link.target_node)
            source_def = COMMON_NODE_REGISTRY.get(source.type_id) if source else None
            target_def = COMMON_NODE_REGISTRY.get(target.type_id) if target else None
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
            definition = COMMON_NODE_REGISTRY.get(node.type_id)
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
            unknown = set(node.properties) - (set(properties) | editable_inputs)
            if unknown:
                raise ParticleCompileError(
                    f"node {node.type_id!r} has unknown properties: {sorted(unknown)}"
                )
            properties.update(node.properties)
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
                    "settings.initialize",
                    tuple(
                        sorted(
                            {
                                "initial_speed_min": settings.initial_speed.minimum,
                                "initial_speed_max": settings.initial_speed.maximum,
                                "lifetime_min": settings.lifetime.minimum,
                                "lifetime_max": settings.lifetime.maximum,
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
        if stage is ParticleStage.UPDATE:
            return (
                ParticleOperation(
                    "settings.gravity",
                    (("value", list(settings.gravity)),),
                    "settings.update",
                ),
            )
        return ()


__all__ = [
    "ParticleCompileError",
    "ParticleEmitterHIR",
    "ParticleGraphCompiler",
    "ParticleOperation",
    "ParticleOutputDescriptor",
    "ParticleProgramHIR",
    "ParticleRenderPlan",
    "ParticleStage",
    "ParticleStageHIR",
    "ParticleSystemSchedule",
]
