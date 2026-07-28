"""Save-time Particle AOT artifact registry with last-known-good publication."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from Infernux.engine.path_utils import path_key, resolved_path
from .asset import ParticleGraphAsset
from .hir import ParticleGraphCompiler, ParticleProgramHIR
from .kernel_ir import ParticleKernelLowerer, ParticleKernelProgram
from .runtime_metadata import decode_particle_runtime_metadata
from .gpu_glsl_backend import (
    GpuParticleGlslLowerer,
    compile_gpu_particle_spirv,
    validate_gpu_particle_spirv,
)
from .script import ParticleScriptCompiler


PARTICLE_ARTIFACT_SCHEMA = "infernux.particle_artifact"


class ParticleArtifactError(ValueError):
    pass


@dataclass(frozen=True)
class ParticleArtifact:
    source_key: str
    source_hash: str
    source_kind: str
    revision: int
    semantic_hash: str
    behavior_hash: str
    artifact_path: str
    hir: Mapping[str, Any]
    kernel_ir: Mapping[str, Any]
    gpu_glsl: Mapping[str, Any]
    gpu_spirv: Mapping[str, Any]


class ParticleArtifactRegistry:
    _artifacts: dict[str, ParticleArtifact] = {}
    _revision = 0

    @classmethod
    def clear(cls) -> None:
        cls._artifacts.clear()
        cls._revision = 0

    @classmethod
    def get(cls, path: str = "", *, guid: str = "") -> ParticleArtifact | None:
        return cls._artifacts.get(cls._source_key(path, guid)) or cls._artifacts.get(
            cls._source_key(path)
        )

    @staticmethod
    def validate_graph_asset(asset: ParticleGraphAsset) -> None:
        """Run the complete portable AOT pipeline without publishing or writing files."""
        if not isinstance(asset, ParticleGraphAsset):
            raise ParticleArtifactError("particle draft must be a ParticleGraphAsset")
        try:
            program = ParticleGraphCompiler().compile(asset)
            kernel_program = ParticleKernelLowerer().lower(program)
            gpu_program = GpuParticleGlslLowerer().lower(kernel_program)
            compile_gpu_particle_spirv(gpu_program)
        except (TypeError, ValueError) as exc:
            raise ParticleArtifactError(f"particle draft compile failed: {exc}") from exc

    @classmethod
    def publish_graph_asset(
        cls,
        asset: ParticleGraphAsset,
        path: str,
        *,
        guid: str = "",
    ) -> ParticleArtifact:
        """Compile and publish an in-memory editor draft without writing it to disk."""
        if not isinstance(asset, ParticleGraphAsset):
            raise ParticleArtifactError("particle draft must be a ParticleGraphAsset")
        source_path = resolved_path(path)
        source = json.dumps(
            asset.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        path_identity = cls._source_key(source_path)
        key = cls._source_key(source_path, guid)
        existing = cls._artifacts.get(key) or cls._artifacts.get(path_identity)
        if existing is not None and existing.source_hash == source_hash:
            cls._artifacts[key] = existing
            cls._artifacts[path_identity] = existing
            return existing

        try:
            program = ParticleGraphCompiler().compile(asset)
            hir = _program_to_dict(program)
            kernel_program = ParticleKernelLowerer().lower(program)
            kernel_ir = kernel_program.to_dict()
            gpu_program = GpuParticleGlslLowerer().lower(kernel_program)
            gpu_glsl = gpu_program.to_dict()
            gpu_spirv = compile_gpu_particle_spirv(gpu_program)
        except (TypeError, ValueError) as exc:
            raise ParticleArtifactError(f"particle draft compile failed: {exc}") from exc

        revision = cls._revision + 1
        artifact = ParticleArtifact(
            key,
            source_hash,
            "graph",
            revision,
            program.semantic_hash,
            program.behavior_hash,
            "",
            hir,
            kernel_ir,
            gpu_glsl,
            gpu_spirv,
        )
        cls._revision = revision
        cls._artifacts[key] = artifact
        cls._artifacts[path_identity] = artifact
        return artifact

    @classmethod
    def compile_path(cls, path: str, *, guid: str = "") -> ParticleArtifact:
        source_path = resolved_path(path)
        try:
            source = Path(source_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ParticleArtifactError(f"failed to read particle source: {exc}") from exc
        source_kind = "script" if source_path.lower().endswith(".particle.py") else "graph"
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        path_key = cls._source_key(source_path)
        key = cls._source_key(source_path, guid)
        existing = cls._artifacts.get(key) or cls._artifacts.get(path_key)
        if (
            existing is not None
            and existing.source_hash == source_hash
            and bool(existing.artifact_path)
            and (not guid or existing.source_key == key)
        ):
            cls._artifacts[key] = existing
            cls._artifacts[path_key] = existing
            return existing

        try:
            stable_id = cls._source_stable_id(source, source_kind, source_path)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ParticleArtifactError(f"particle AOT compile failed: {exc}") from exc
        artifact_path = cls._artifact_path(stable_id)
        if existing is None and artifact_path:
            persisted = cls._load_persisted(
                artifact_path,
                key=key,
                source_hash=source_hash,
                source_kind=source_kind,
            )
            if persisted is not None:
                cls._artifacts[key] = persisted
                cls._artifacts[path_key] = persisted
                cls._revision = max(cls._revision, persisted.revision)
                return persisted

        try:
            program = (
                ParticleScriptCompiler().compile(source, source_name=source_path)
                if source_kind == "script"
                else ParticleGraphCompiler().compile(ParticleGraphAsset.from_json(source))
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ParticleArtifactError(f"particle AOT compile failed: {exc}") from exc
        try:
            hir = _program_to_dict(program)
            kernel_program = ParticleKernelLowerer().lower(program)
            kernel_ir = kernel_program.to_dict()
            gpu_program = GpuParticleGlslLowerer().lower(kernel_program)
            gpu_glsl = gpu_program.to_dict()
            gpu_spirv = compile_gpu_particle_spirv(gpu_program)
        except (TypeError, ValueError) as exc:
            raise ParticleArtifactError(f"particle AOT lowering failed: {exc}") from exc
        revision = cls._revision + 1
        payload = {
            "$schema": PARTICLE_ARTIFACT_SCHEMA,
            "source_key": key,
            "source_hash": source_hash,
            "source_kind": source_kind,
            "revision": revision,
            "semantic_hash": program.semantic_hash,
            "behavior_hash": program.behavior_hash,
            "hir": hir,
            "kernel_ir": kernel_ir,
            "gpu_glsl": gpu_glsl,
            "gpu_spirv": gpu_spirv,
        }
        if artifact_path:
            from Infernux.core.document_store import write_document_text

            os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
            write_document_text(
                artifact_path,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        artifact = ParticleArtifact(
            key,
            source_hash,
            source_kind,
            revision,
            program.semantic_hash,
            program.behavior_hash,
            artifact_path,
            hir,
            kernel_ir,
            gpu_glsl,
            gpu_spirv,
        )
        cls._revision = revision
        cls._artifacts[key] = artifact
        cls._artifacts[path_key] = artifact
        return artifact

    @staticmethod
    def _source_stable_id(source: str, source_kind: str, source_path: str) -> str:
        if source_kind == "graph":
            return ParticleGraphAsset.from_json(source).stable_id
        return ParticleScriptCompiler().parse(source, source_name=source_path).stable_id

    @staticmethod
    def _source_key(path: str, guid: str = "") -> str:
        identity = str(guid or "").strip()
        return identity or path_key(path)

    @staticmethod
    def _artifact_path(stable_id: str) -> str:
        from Infernux.engine.project_context import get_project_root

        project_root = get_project_root()
        if not project_root:
            return ""
        identity = stable_id
        if not identity or not all(character.isalnum() or character in "-_" for character in identity):
            identity = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:32]
        return os.path.join(
            project_root,
            "Library",
            "Artifacts",
            "Particle",
            f"{identity}.inxparticle",
        )

    @staticmethod
    def _load_persisted(
        artifact_path: str,
        *,
        key: str,
        source_hash: str,
        source_kind: str,
    ) -> ParticleArtifact | None:
        try:
            payload = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
            if (
                type(payload) is not dict
                or set(payload) != {
                    "$schema", "source_key", "source_hash", "source_kind", "revision",
                    "semantic_hash", "behavior_hash", "hir", "kernel_ir", "gpu_glsl", "gpu_spirv",
                }
                or payload.get("$schema") != PARTICLE_ARTIFACT_SCHEMA
                or payload.get("source_key") != key
                or payload.get("source_hash") != source_hash
                or payload.get("source_kind") != source_kind
                or type(payload.get("hir")) is not dict
                or type(payload.get("kernel_ir")) is not dict
                or type(payload.get("gpu_glsl")) is not dict
                or type(payload.get("gpu_spirv")) is not dict
            ):
                return None
            revision = payload.get("revision")
            if type(revision) is not int or revision <= 0:
                return None
            runtime_metadata = decode_particle_runtime_metadata(payload["hir"])
            if runtime_metadata.behavior_hash != payload["behavior_hash"]:
                return None
            kernel_program = ParticleKernelProgram.from_dict(payload["kernel_ir"])
            if kernel_program.source_behavior_hash != payload["behavior_hash"]:
                return None
            gpu_program = GpuParticleGlslLowerer().lower(kernel_program)
            gpu_glsl = gpu_program.to_dict()
            if payload["gpu_glsl"] != gpu_glsl:
                return None
            gpu_spirv = validate_gpu_particle_spirv(payload["gpu_spirv"], gpu_program)
            return ParticleArtifact(
                key,
                source_hash,
                source_kind,
                revision,
                str(payload["semantic_hash"]),
                str(payload["behavior_hash"]),
                artifact_path,
                payload["hir"],
                kernel_program.to_dict(),
                gpu_glsl,
                gpu_spirv,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
            return None


def _program_to_dict(program: ParticleProgramHIR) -> dict[str, Any]:
    def operand(value):
        return {
            "type": value.value_type.to_dict(),
            "value_id": value.value_id,
            "literal": value.literal,
        }

    def stage(value):
        return {
            "stage": value.stage.value,
            "root_uid": value.root_uid,
            "expressions": [
                {
                    "result_id": instruction.result_id,
                    "opcode": instruction.opcode,
                    "result_type": instruction.result_type.to_dict(),
                    "operands": [operand(item) for item in instruction.operands],
                    "immediates": list(instruction.immediates),
                    "source_node_uid": instruction.source_node_uid,
                    "source_port_id": instruction.source_port_id,
                }
                for instruction in value.expressions.instructions
            ],
            "operations": [
                {
                    "opcode": operation.opcode,
                    "parameters": list(operation.parameters),
                    "source_node_uid": operation.source_node_uid,
                    "value_bindings": list(operation.value_bindings),
                    "execution_predicates": [
                        {
                            "source_node_uid": predicate.source_node_uid,
                            "value_id": predicate.value_id,
                            "literal": predicate.literal,
                            "expected": predicate.expected,
                            "runtime_condition": predicate.runtime_condition,
                        }
                        for predicate in operation.execution_predicates
                    ],
                }
                for operation in value.operations
            ],
            "flow": {
                "entry_node_uid": value.flow.entry_node_uid,
                "blocks": [
                    {
                        "node_uid": block.node_uid,
                        "operation_index": block.operation_index,
                        "incoming_edges": list(block.incoming_edges),
                        "outgoing_edges": list(block.outgoing_edges),
                    }
                    for block in value.flow.blocks
                ],
                "edges": [
                    {
                        "link_uid": edge.link_uid,
                        "source_node_uid": edge.source_node_uid,
                        "source_port_id": edge.source_port_id,
                        "target_node_uid": edge.target_node_uid,
                        "target_port_id": edge.target_port_id,
                        "predicate_node_uid": edge.predicate_node_uid,
                        "predicate_expected": edge.predicate_expected,
                        "lane_index": edge.lane_index,
                    }
                    for edge in value.flow.edges
                ],
                "operation_schedule": list(value.flow.operation_schedule),
                "lanes": [
                    {
                        "stable_id": lane.stable_id,
                        "index": lane.index,
                        "parent_index": lane.parent_index,
                        "source_node_uid": lane.source_node_uid,
                        "source_port_id": lane.source_port_id,
                    }
                    for lane in value.flow.lanes
                ],
                "joins": [
                    {
                        "node_uid": join.node_uid,
                        "input_lane_indices": list(join.input_lane_indices),
                        "output_lane_index": join.output_lane_index,
                    }
                    for join in value.flow.joins
                ],
                "suspensions": [
                    {
                        "node_uid": suspension.node_uid,
                        "kind": suspension.kind.value,
                        "lane_index": suspension.lane_index,
                        "lane_stable_id": suspension.lane_stable_id,
                        "resume_program_counter": suspension.resume_program_counter,
                        "resume_node_uid": suspension.resume_node_uid,
                        "resume_operation_index": suspension.resume_operation_index,
                        "value_id": suspension.value_id,
                        "literal": suspension.literal,
                    }
                    for suspension in value.flow.suspensions
                ],
            },
        }

    return {
        "stable_id": program.stable_id,
        "name": program.name,
        "semantic_hash": program.semantic_hash,
        "behavior_hash": program.behavior_hash,
        "parameters": [
            {
                "stable_id": parameter.stable_id,
                "name": parameter.name,
                "type": parameter.value_type.to_dict(),
                "default": parameter.default,
                "exposed": parameter.exposed,
                "slot": parameter.slot,
                "category": parameter.category,
                "tooltip": parameter.tooltip,
            }
            for parameter in program.parameters
        ],
        "schedule": list(program.schedule.emitter_ids),
        "events": {
            "event_abi_hash": program.events.event_abi_hash,
            "event_types": [
                {
                    "stable_id": event_type.stable_id,
                    "name": event_type.name,
                    "type_index": event_type.type_index,
                    "stable_type_hash": event_type.stable_type_hash,
                    "capacity_per_step": event_type.capacity_per_step,
                    "payload_stride_words": event_type.payload_stride_words,
                    "fields": [
                        {
                            "stable_id": field.stable_id,
                            "name": field.name,
                            "type": field.value_type.to_dict(),
                            "word_offset": field.word_offset,
                            "word_count": field.word_count,
                            "default": field.default,
                        }
                        for field in event_type.fields
                    ],
                }
                for event_type in program.events.event_types
            ],
            "routes": [
                {
                    "stable_id": route.stable_id,
                    "event_type_id": route.event_type_id,
                    "event_type_index": route.event_type_index,
                    "source_emitter_id": route.source_emitter_id,
                    "source_emitter_index": route.source_emitter_index,
                    "source_stage": route.source_stage.value,
                    "target_emitter_id": route.target_emitter_id,
                    "target_emitter_index": route.target_emitter_index,
                    "spawn_count": route.spawn_count,
                    "capacity": route.capacity,
                    "payload_stride_words": route.payload_stride_words,
                }
                for route in program.events.routes
            ],
        },
        "emitters": [
            {
                "stable_id": emitter.stable_id,
                "name": emitter.name,
                "enabled": emitter.enabled,
                "play_on_start": emitter.play_on_start,
                "settings": emitter.settings.to_dict(),
                "attributes": [attribute.to_dict() for attribute in emitter.attributes],
                "data_interfaces": [
                    interface.to_dict() for interface in emitter.data_interfaces
                ],
                "init": stage(emitter.init),
                "update": stage(emitter.update),
                "collision_enter": (
                    stage(emitter.collision_enter)
                    if emitter.collision_enter is not None
                    else None
                ),
                "collision_stay": (
                    stage(emitter.collision_stay)
                    if emitter.collision_stay is not None
                    else None
                ),
                "collision_exit": (
                    stage(emitter.collision_exit)
                    if emitter.collision_exit is not None
                    else None
                ),
                "rendering": stage(emitter.rendering),
                "render_plan": [
                    {
                        "output_id": output.output_id,
                        "output_type": output.output_type,
                        "mesh": output.mesh.to_dict(),
                        "material": output.material.to_dict(),
                        "receive_scene_lighting": output.receive_scene_lighting,
                        "receive_shadows": output.receive_shadows,
                        "cast_shadows": output.cast_shadows,
                        "soft_particles": output.soft_particles,
                        "soft_distance": output.soft_distance,
                        "sort_mode": output.sort_mode,
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
            for emitter in program.emitters
        ],
    }


__all__ = [
    "PARTICLE_ARTIFACT_SCHEMA",
    "ParticleArtifact",
    "ParticleArtifactError",
    "ParticleArtifactRegistry",
]
