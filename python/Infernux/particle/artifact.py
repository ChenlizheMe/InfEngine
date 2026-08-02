"""Save-time Particle AOT artifact registry with last-known-good publication."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Mapping

from Infernux.engine.path_utils import (
    path_key,
    portable_path,
    relative_path,
    resolved_path,
)
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
PARTICLE_RUNTIME_INDEX_SCHEMA = "infernux.particle_runtime_index"
PARTICLE_RUNTIME_INDEX_FILENAME = "RuntimeIndex.json"


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


@dataclass(frozen=True)
class _CompiledParticleArtifact:
    source_hash: str
    source_kind: str
    stable_id: str
    semantic_hash: str
    behavior_hash: str
    hir: Mapping[str, Any]
    kernel_ir: Mapping[str, Any]
    gpu_glsl: Mapping[str, Any]
    gpu_spirv: Mapping[str, Any]


class ParticleArtifactRegistry:
    _artifacts: dict[str, ParticleArtifact] = {}
    _revision = 0
    _request_generation: dict[str, int] = {}
    _lock = threading.RLock()

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._artifacts.clear()
            cls._request_generation.clear()
            cls._revision = 0

    @classmethod
    def get(cls, path: str = "", *, guid: str = "") -> ParticleArtifact | None:
        with cls._lock:
            return cls._current_unlocked(path, guid)

    @classmethod
    def load_runtime_reference(
        cls, path: str = "", *, guid: str = ""
    ) -> ParticleArtifact | None:
        """Load a shipped AOT artifact without requiring its authoring source."""
        from Infernux.engine.project_context import get_project_root

        project_root = get_project_root()
        if not project_root:
            return None
        artifact_root = os.path.join(
            project_root, "Library", "Artifacts", "Particle"
        )
        index_path = os.path.join(artifact_root, PARTICLE_RUNTIME_INDEX_FILENAME)
        if not os.path.isfile(index_path):
            return None
        try:
            index = json.loads(Path(index_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ParticleArtifactError(
                f"particle runtime index cannot be read: {index_path}"
            ) from exc
        if (
            type(index) is not dict
            or set(index) != {"$schema", "entries"}
            or index.get("$schema") != PARTICLE_RUNTIME_INDEX_SCHEMA
            or type(index.get("entries")) is not list
        ):
            raise ParticleArtifactError("particle runtime index is not current")

        def reference_key(value: str) -> str:
            candidate = str(value or "").strip()
            if os.path.isabs(candidate):
                try:
                    candidate = relative_path(
                        candidate,
                        project_root,
                        resolve=False,
                    )
                except ValueError:
                    pass
            return portable_path(candidate).casefold()

        wanted_guid = str(guid or "").strip()
        wanted_path = reference_key(path)
        selected = None
        for entry in index["entries"]:
            if (
                type(entry) is not dict
                or set(entry) != {"guid", "path_hint", "stable_id"}
                or any(type(entry.get(key)) is not str for key in entry)
            ):
                raise ParticleArtifactError("particle runtime index entry is not current")
            if wanted_guid and entry["guid"] == wanted_guid:
                selected = entry
                break
            if not wanted_guid and wanted_path and reference_key(entry["path_hint"]) == wanted_path:
                selected = entry
                break
        if selected is None and wanted_path:
            selected = next(
                (
                    entry
                    for entry in index["entries"]
                    if reference_key(entry["path_hint"]) == wanted_path
                ),
                None,
            )
        if selected is None:
            return None

        stable_id = selected["stable_id"]
        if not stable_id or not all(
            character.isalnum() or character in "-_" for character in stable_id
        ):
            raise ParticleArtifactError(
                f"particle runtime index has an invalid stable_id: {stable_id!r}"
            )
        artifact_path = os.path.join(artifact_root, stable_id + ".inxparticle")
        try:
            payload = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
            source_hash = payload["source_hash"]
            source_kind = payload["source_kind"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ParticleArtifactError(
                f"shipped particle artifact cannot be read: {artifact_path}"
            ) from exc
        key = cls._source_key(path, guid)
        artifact = cls._load_persisted(
            artifact_path,
            key=key,
            source_hash=source_hash,
            source_kind=source_kind,
        )
        if artifact is None:
            raise ParticleArtifactError(
                f"shipped particle artifact is invalid: {artifact_path}"
            )
        with cls._lock:
            cls._register_unlocked(artifact, key, cls._source_key(path))
            cls._revision = max(cls._revision, artifact.revision)
        return artifact

    @classmethod
    def save_graph_asset(
        cls,
        asset: ParticleGraphAsset,
        path: str,
        *,
        guid: str = "",
    ) -> ParticleArtifact:
        """Compile, atomically save, and publish one exact graph snapshot."""
        if not isinstance(asset, ParticleGraphAsset):
            raise ParticleArtifactError("particle graph must be a ParticleGraphAsset")
        source_path = resolved_path(path)
        if not source_path:
            raise ParticleArtifactError("particle graph save path cannot be empty")
        source_hash = cls._graph_source_hash(asset)
        path_identity = cls._source_key(source_path)
        key = cls._source_key(source_path, guid)
        ticket = cls._begin_request(path_identity)

        with cls._lock:
            existing = cls._current_unlocked(source_path, guid)
        if existing is not None and existing.source_hash == source_hash:
            compiled = cls._compiled_from_artifact(existing, stable_id=asset.stable_id)
        else:
            try:
                compiled = cls._compile_graph_asset(asset, source_path)
            except (TypeError, ValueError) as exc:
                raise ParticleArtifactError(
                    f"particle graph AOT compile failed: {exc}"
                ) from exc

        artifact_path = cls._artifact_path(asset.stable_id)
        source_text = (
            json.dumps(asset.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        return cls._publish_compiled(
            compiled,
            source_path=source_path,
            key=key,
            path_identity=path_identity,
            ticket=ticket,
            artifact_path=artifact_path,
            source_text=source_text,
        )

    @classmethod
    def compile_path(cls, path: str, *, guid: str = "") -> ParticleArtifact:
        source_path = resolved_path(path)
        try:
            source = Path(source_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ParticleArtifactError(f"failed to read particle source: {exc}") from exc
        source_kind = "script" if source_path.lower().endswith(".particle.py") else "graph"
        path_identity = cls._source_key(source_path)
        key = cls._source_key(source_path, guid)
        ticket = cls._begin_request(path_identity)

        try:
            graph_asset = (
                ParticleGraphAsset.from_json(source) if source_kind == "graph" else None
            )
            source_hash = (
                cls._graph_source_hash(graph_asset)
                if graph_asset is not None
                else cls._text_source_hash(source)
            )
            stable_id = (
                graph_asset.stable_id
                if graph_asset is not None
                else ParticleScriptCompiler()
                .parse(source, source_name=source_path)
                .stable_id
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ParticleArtifactError(f"particle AOT compile failed: {exc}") from exc

        with cls._lock:
            existing = cls._current_unlocked(source_path, guid)
            if (
                cls._request_generation.get(path_identity) == ticket
                and existing is not None
                and existing.source_hash == source_hash
                and bool(existing.artifact_path)
            ):
                alias = replace(existing, source_key=key)
                cls._register_unlocked(alias, key, path_identity)
                return alias

        artifact_path = cls._artifact_path(stable_id)
        if existing is None and artifact_path:
            persisted = cls._load_persisted(
                artifact_path,
                key=key,
                source_hash=source_hash,
                source_kind=source_kind,
            )
            if persisted is not None:
                with cls._lock:
                    if cls._request_generation.get(path_identity) != ticket:
                        return cls._superseded_unlocked(source_path, guid)
                    cls._register_unlocked(persisted, key, path_identity)
                    cls._revision = max(cls._revision, persisted.revision)
                    return persisted

        if existing is not None and existing.source_hash == source_hash:
            compiled = cls._compiled_from_artifact(existing, stable_id=stable_id)
        else:
            try:
                compiled = (
                    cls._compile_script_source(source, source_path)
                    if graph_asset is None
                    else cls._compile_graph_asset(graph_asset, source_path)
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ParticleArtifactError(f"particle AOT compile failed: {exc}") from exc
        return cls._publish_compiled(
            compiled,
            source_path=source_path,
            key=key,
            path_identity=path_identity,
            ticket=ticket,
            artifact_path=artifact_path,
        )

    @classmethod
    def _compile_graph_asset(
        cls, asset: ParticleGraphAsset, source_path: str
    ) -> _CompiledParticleArtifact:
        program = ParticleGraphCompiler().compile(asset, source_name=source_path)
        return cls._lower_program(
            program,
            source_hash=cls._graph_source_hash(asset),
            source_kind="graph",
            stable_id=asset.stable_id,
        )

    @classmethod
    def _compile_script_source(
        cls, source: str, source_path: str
    ) -> _CompiledParticleArtifact:
        program = ParticleScriptCompiler().compile(source, source_name=source_path)
        return cls._lower_program(
            program,
            source_hash=cls._text_source_hash(source),
            source_kind="script",
            stable_id=program.stable_id,
        )

    @staticmethod
    def _lower_program(
        program: ParticleProgramHIR,
        *,
        source_hash: str,
        source_kind: str,
        stable_id: str,
    ) -> _CompiledParticleArtifact:
        hir = _program_to_dict(program)
        kernel_program = ParticleKernelLowerer().lower(program)
        kernel_ir = kernel_program.to_dict()
        gpu_program = GpuParticleGlslLowerer().lower(kernel_program)
        gpu_glsl = gpu_program.to_dict()
        gpu_spirv = compile_gpu_particle_spirv(gpu_program)
        return _CompiledParticleArtifact(
            source_hash,
            source_kind,
            stable_id,
            program.semantic_hash,
            program.behavior_hash,
            hir,
            kernel_ir,
            gpu_glsl,
            gpu_spirv,
        )

    @staticmethod
    def _compiled_from_artifact(
        artifact: ParticleArtifact, *, stable_id: str
    ) -> _CompiledParticleArtifact:
        return _CompiledParticleArtifact(
            artifact.source_hash,
            artifact.source_kind,
            stable_id,
            artifact.semantic_hash,
            artifact.behavior_hash,
            artifact.hir,
            artifact.kernel_ir,
            artifact.gpu_glsl,
            artifact.gpu_spirv,
        )

    @classmethod
    def _publish_compiled(
        cls,
        compiled: _CompiledParticleArtifact,
        *,
        source_path: str,
        key: str,
        path_identity: str,
        ticket: int,
        artifact_path: str,
        source_text: str | None = None,
    ) -> ParticleArtifact:
        with cls._lock:
            if cls._request_generation.get(path_identity) != ticket:
                return cls._superseded_unlocked(source_path, key)

            current = cls._artifacts.get(key) or cls._artifacts.get(path_identity)
            if current is not None and current.source_hash == compiled.source_hash:
                revision = current.revision
            else:
                revision = cls._revision + 1

            artifact = ParticleArtifact(
                key,
                compiled.source_hash,
                compiled.source_kind,
                revision,
                compiled.semantic_hash,
                compiled.behavior_hash,
                artifact_path,
                compiled.hir,
                compiled.kernel_ir,
                compiled.gpu_glsl,
                compiled.gpu_spirv,
            )

            from Infernux.core.document_store import write_document_text

            if source_text is not None:
                os.makedirs(os.path.dirname(source_path), exist_ok=True)
                write_document_text(source_path, source_text)
            if artifact_path:
                os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
                write_document_text(
                    artifact_path,
                    json.dumps(
                        cls._artifact_payload(artifact),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                )
            if compiled.source_kind == "graph":
                cls._publish_runtime_index_entry(
                    source_path,
                    stable_id=compiled.stable_id,
                )

            cls._revision = max(cls._revision, revision)
            cls._register_unlocked(artifact, key, path_identity)
            return artifact

    @staticmethod
    def _publish_runtime_index_entry(source_path: str, *, stable_id: str) -> None:
        from Infernux.core.document_store import write_document_text
        from Infernux.engine.project_context import get_project_root

        project_root = get_project_root()
        if not project_root:
            return
        try:
            path_hint = portable_path(
                relative_path(source_path, project_root, resolve=False)
            )
        except ValueError:
            return

        guid = ""
        try:
            meta = json.loads(Path(source_path + ".meta").read_text(encoding="utf-8"))
            value = meta["metadata"]["guid"]["value"]
            if type(value) is str:
                guid = value.strip()
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            pass

        artifact_root = os.path.join(
            project_root, "Library", "Artifacts", "Particle"
        )
        index_path = os.path.join(artifact_root, PARTICLE_RUNTIME_INDEX_FILENAME)
        entries: list[dict[str, str]] = []
        try:
            current = json.loads(Path(index_path).read_text(encoding="utf-8"))
            if (
                type(current) is dict
                and current.get("$schema") == PARTICLE_RUNTIME_INDEX_SCHEMA
                and type(current.get("entries")) is list
            ):
                entries = [
                    entry
                    for entry in current["entries"]
                    if type(entry) is dict
                    and set(entry) == {"guid", "path_hint", "stable_id"}
                    and all(type(entry.get(key)) is str for key in entry)
                ]
        except (OSError, json.JSONDecodeError):
            pass

        path_identity = portable_path(path_hint).casefold()
        entries = [
            entry
            for entry in entries
            if portable_path(entry["path_hint"]).casefold() != path_identity
            and entry["stable_id"] != stable_id
            and (not guid or entry["guid"] != guid)
        ]
        entries.append(
            {"guid": guid, "path_hint": path_hint, "stable_id": stable_id}
        )
        entries.sort(key=lambda entry: portable_path(entry["path_hint"]).casefold())
        os.makedirs(artifact_root, exist_ok=True)
        write_document_text(
            index_path,
            json.dumps(
                {"$schema": PARTICLE_RUNTIME_INDEX_SCHEMA, "entries": entries},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    @classmethod
    def _begin_request(cls, path_identity: str) -> int:
        with cls._lock:
            ticket = cls._request_generation.get(path_identity, 0) + 1
            cls._request_generation[path_identity] = ticket
            return ticket

    @classmethod
    def _current_unlocked(
        cls, path: str, guid: str = ""
    ) -> ParticleArtifact | None:
        return cls._artifacts.get(cls._source_key(path, guid)) or cls._artifacts.get(
            cls._source_key(path)
        )

    @classmethod
    def _register_unlocked(
        cls, artifact: ParticleArtifact, key: str, path_identity: str
    ) -> None:
        cls._artifacts[key] = artifact
        cls._artifacts[path_identity] = artifact

    @classmethod
    def _superseded_unlocked(
        cls, source_path: str, guid_or_key: str = ""
    ) -> ParticleArtifact:
        current = cls._artifacts.get(guid_or_key) or cls._current_unlocked(source_path)
        if current is not None:
            return current
        raise ParticleArtifactError(
            f"particle compile for {source_path!r} was superseded by a newer request"
        )

    @staticmethod
    def _graph_source_hash(asset: ParticleGraphAsset) -> str:
        return hashlib.sha256(asset.canonical_json().encode("utf-8")).hexdigest()

    @staticmethod
    def _text_source_hash(source: str) -> str:
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    @staticmethod
    def _artifact_payload(artifact: ParticleArtifact) -> dict[str, Any]:
        return {
            "$schema": PARTICLE_ARTIFACT_SCHEMA,
            "source_key": artifact.source_key,
            "source_hash": artifact.source_hash,
            "source_kind": artifact.source_kind,
            "revision": artifact.revision,
            "semantic_hash": artifact.semantic_hash,
            "behavior_hash": artifact.behavior_hash,
            "hir": artifact.hir,
            "kernel_ir": artifact.kernel_ir,
            "gpu_glsl": artifact.gpu_glsl,
            "gpu_spirv": artifact.gpu_spirv,
        }

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
                or type(payload.get("source_key")) is not str
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
            "flow_id": value.flow_id,
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
            "flow": {
                "entry_node_uid": value.flow.entry_node_uid,
                "blocks": [
                    {
                        "node_uid": block.node_uid,
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
                            for operation in block.operations
                        ],
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
                "writable": parameter.writable,
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
                    "queue_capacity": event_type.queue_capacity,
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
        },
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
                "events": [stage(event_flow) for event_flow in emitter.event_flows],
                "rendering": stage(emitter.rendering),
                "render_plan": [
                    {
                        "output_id": output.output_id,
                        "output_type": output.output_type,
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
