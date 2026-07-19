"""Save-time Particle AOT artifact registry with last-known-good publication."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .asset import ParticleGraphAsset
from .hir import ParticleGraphCompiler, ParticleProgramHIR
from .kernel_ir import ParticleKernelLowerer, ParticleKernelProgram
from .gpu_glsl_backend import (
    GpuParticleGlslLowerer,
    compile_gpu_particle_spirv,
    validate_gpu_particle_spirv,
)
from .script import ParticleScriptCompiler


PARTICLE_ARTIFACT_SCHEMA = "infernux.particle_artifact"
PARTICLE_ARTIFACT_VERSION = 11


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
        return cls._artifacts.get(cls._source_key(path, guid))

    @classmethod
    def compile_path(cls, path: str, *, guid: str = "") -> ParticleArtifact:
        source_path = os.path.abspath(path)
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
            "$version": PARTICLE_ARTIFACT_VERSION,
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
        return identity or os.path.normcase(os.path.abspath(path))

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
                or payload.get("$schema") != PARTICLE_ARTIFACT_SCHEMA
                or payload.get("$version") != PARTICLE_ARTIFACT_VERSION
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
                }
                for operation in value.operations
            ],
        }

    return {
        "stable_id": program.stable_id,
        "name": program.name,
        "semantic_hash": program.semantic_hash,
        "behavior_hash": program.behavior_hash,
        "schedule": list(program.schedule.emitter_ids),
        "emitters": [
            {
                "stable_id": emitter.stable_id,
                "name": emitter.name,
                "settings": emitter.settings.to_dict(),
                "attributes": [attribute.to_dict() for attribute in emitter.attributes],
                "data_interfaces": [
                    interface.to_dict() for interface in emitter.data_interfaces
                ],
                "init": stage(emitter.init),
                "update": stage(emitter.update),
                "rendering": stage(emitter.rendering),
                "render_plan": [
                    {
                        "output_id": output.output_id,
                        "output_type": output.output_type,
                        "material": output.material.to_dict(),
                        "receive_scene_lighting": output.receive_scene_lighting,
                        "receive_shadows": output.receive_shadows,
                        "sort_mode": output.sort_mode,
                    }
                    for output in emitter.render_plan.outputs
                ],
            }
            for emitter in program.emitters
        ],
    }


__all__ = [
    "PARTICLE_ARTIFACT_SCHEMA",
    "PARTICLE_ARTIFACT_VERSION",
    "ParticleArtifact",
    "ParticleArtifactError",
    "ParticleArtifactRegistry",
]
