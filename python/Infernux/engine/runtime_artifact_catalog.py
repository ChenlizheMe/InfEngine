"""Deterministic runtime artifact catalog primitives.

The catalog is deliberately small and package-oriented.  It describes the
payload that a Player can actually reach after native package extraction; it
does not pretend that serialized authoring documents are compiled artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any, Iterable

from Infernux.core.asset_types import AUDIO_EXTENSIONS

from .path_utils import relative_path, resolved_path


CATALOG_SCHEMA = "infernux.runtime_asset_catalog"
# Windows FILETIME is measured in 100 ns ticks since 1601-01-01 UTC.
WINDOWS_FILETIME_EPOCH_OFFSET_TICKS = 116444736000000000

_DOCUMENT_TYPES = {
    ".scene": "scene",
    ".prefab": "prefab",
    ".mat": "material",
    ".effect": "render_effect",
    ".effectgroup": "render_effect_group",
    ".timeline": "timeline",
    ".timelinefsm": "timeline_fsm",
    ".animclip": "animation_clip",
    ".animclip2d": "animation_clip_2d",
    ".animclip3d": "animation_clip_3d",
    ".animfsm": "animation_fsm",
    ".animtimeline": "animation_timeline",
}

# One source of truth for authoring documents that must be cooked into
# Library artifacts before entering a Player package.  Keep the JSON subset
# separate: GameBuilder may rewrite project-owned absolute paths only in
# formats whose parser contract is JSON.
RUNTIME_AUTHORING_DOCUMENT_SUFFIXES = frozenset(_DOCUMENT_TYPES) | frozenset(
    {".graph", ".particlegraph"}
)
RUNTIME_JSON_DOCUMENT_SUFFIXES = frozenset(_DOCUMENT_TYPES) | frozenset(
    {".graph", ".particlegraph", ".json"}
)
_AUDIO_TYPES = {extension: "audio" for extension in AUDIO_EXTENSIONS}
_DIRECT_TEXTURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".hdr", ".exr"}
_DIRECT_MODEL_SUFFIXES = {".fbx", ".obj", ".gltf", ".glb", ".dae"}
_BINARY_ARTIFACT_MAGIC = {
    ".inxtex": b"INXTEXTURE",
    ".inxmesh": b"INXMESHART",
    ".inxskin": b"INXSKINAR",
}
_ARTIFACT_SUFFIXES = frozenset(_BINARY_ARTIFACT_MAGIC) | frozenset(
    {".inxparticle", ".inxeffect"}
)

# These reasons describe the Library artifact that a source type must produce.
# They remain part of the build diagnostics, but no longer authorize a direct
# authoring payload in a Player package.
RUNTIME_ARTIFACT_REASONS = frozenset(
    {
        "runtime_loader_requires_serialized_document",
        "runtime_audio_backend_requires_encoded_stream",
        "runtime_loader_requires_opaque_project_payload",
    }
)
_RUNTIME_ARTIFACT_REASON_BY_LOGICAL_TYPE = {
    "scene": "runtime_loader_requires_serialized_document",
    "prefab": "runtime_loader_requires_serialized_document",
    "material": "runtime_loader_requires_serialized_document",
    "render_effect": "runtime_loader_requires_serialized_document",
    "render_effect_group": "runtime_loader_requires_serialized_document",
    "timeline": "runtime_loader_requires_serialized_document",
    "timeline_fsm": "runtime_loader_requires_serialized_document",
    "animation_clip": "runtime_loader_requires_serialized_document",
    "animation_clip_2d": "runtime_loader_requires_serialized_document",
    "animation_clip_3d": "runtime_loader_requires_serialized_document",
    "animation_fsm": "runtime_loader_requires_serialized_document",
    "animation_timeline": "runtime_loader_requires_serialized_document",
    "audio": "runtime_audio_backend_requires_encoded_stream",
    "project_runtime_document": "runtime_loader_requires_opaque_project_payload",
    "project_runtime_blob": "runtime_loader_requires_opaque_project_payload",
}
RUNTIME_DOCUMENT_PAYLOAD_KINDS = frozenset(
    {"serialized_runtime_document", "direct_runtime_asset"}
)


class RuntimeArtifactError(RuntimeError):
    """Raised when a Library artifact cannot be proven current."""


def unix_ns_to_filetime_ticks(unix_ns: int) -> int:
    """Convert Unix nanoseconds to Windows FILETIME 100 ns ticks."""

    return int(unix_ns) // 100 + WINDOWS_FILETIME_EPOCH_OFFSET_TICKS


def _source_content_hash(path: str) -> str:
    """Return the native AssetIndex FNV-1a source fingerprint."""

    try:
        with Path(path).open("rb") as stream:
            value = 14695981039346656037
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                for byte in chunk:
                    value ^= byte
                    value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
            return f"{value:016x}"
    except OSError as exc:
        raise RuntimeArtifactError(f"Asset source cannot be fingerprinted: {path}") from exc


def _metadata_value(entry: dict[str, Any], key: str, default: Any = None) -> Any:
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict):
        return default
    metadata = metadata.get("metadata", metadata)
    if not isinstance(metadata, dict):
        return default
    item = metadata.get(key)
    if isinstance(item, dict) and "value" in item:
        return item["value"]
    return item if item is not None else default


def load_asset_index(project_root: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load the current native AssetIndex without rebuilding it.

    The index is an editor-produced derived artifact.  This helper deliberately
    does not silently rescan Assets: a Player build must fail when the index is
    missing or malformed instead of inventing a different source of truth.
    """

    root = Path(project_root)
    path = root / "Library" / "AssetIndex.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeArtifactError(f"Library AssetIndex is unreadable: {path}") from exc
    entries = document.get("entries") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise RuntimeArtifactError("Library AssetIndex.entries must be an array")
    normalized: list[dict[str, Any]] = []
    seen_guids: set[str] = set()
    seen_paths: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeArtifactError(f"AssetIndex.entries[{index}] is not an object")
        required = ("guid", "normalized_path", "source", "content_hash", "dependencies")
        if any(key not in entry for key in required):
            raise RuntimeArtifactError(
                f"AssetIndex.entries[{index}] is missing a required artifact field"
            )
        if not isinstance(entry["guid"], str) or not entry["guid"]:
            raise RuntimeArtifactError(f"AssetIndex.entries[{index}].guid is invalid")
        if entry["guid"] in seen_guids:
            raise RuntimeArtifactError(f"AssetIndex contains duplicate GUID: {entry['guid']}")
        seen_guids.add(entry["guid"])
        normalized_path = entry["normalized_path"]
        if not isinstance(normalized_path, str) or not normalized_path.strip():
            raise RuntimeArtifactError(
                f"AssetIndex.entries[{index}].normalized_path is invalid"
            )
        normalized_key = normalized_path.replace("\\", "/").casefold()
        if normalized_key in seen_paths:
            raise RuntimeArtifactError(
                f"AssetIndex contains duplicate normalized path: {normalized_path}"
            )
        seen_paths.add(normalized_key)
        source = entry["source"]
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("size"), int)
            or not isinstance(source.get("modified_ns"), int)
        ):
            raise RuntimeArtifactError(f"AssetIndex.entries[{index}].source is invalid")
        if not isinstance(entry.get("content_hash"), str) or not re.fullmatch(
            r"[0-9a-fA-F]{16}", entry["content_hash"]
        ):
            raise RuntimeArtifactError(f"AssetIndex.entries[{index}].content_hash is invalid")
        if "artifact_path" in entry and not isinstance(entry["artifact_path"], str):
            raise RuntimeArtifactError(f"AssetIndex.entries[{index}].artifact_path is invalid")
        if not isinstance(entry["dependencies"], list) or any(
            not isinstance(value, str) or not value for value in entry["dependencies"]
        ):
            raise RuntimeArtifactError(f"AssetIndex.entries[{index}].dependencies is invalid")
        normalized.append(entry)
    return normalized


def source_path_for_entry(project_root: str | os.PathLike[str], entry: dict[str, Any]) -> str:
    """Resolve an AssetIndex path while rejecting paths outside the project."""

    root = resolved_path(project_root)
    raw = str(entry.get("normalized_path", "")).replace("\\", "/")
    try:
        candidate = resolved_path(raw if os.path.isabs(raw) else os.path.join(root, raw.removeprefix("./")))
        relative_path(candidate, root)
    except (OSError, ValueError) as exc:
        raise RuntimeArtifactError(
            f"AssetIndex source path escapes the project: {raw!r}"
        ) from exc
    return candidate


def source_fingerprint(project_root: str | os.PathLike[str], entry: dict[str, Any]) -> dict[str, Any]:
    """Return and verify the current filesystem fingerprint for an index entry."""

    source = source_path_for_entry(project_root, entry)
    try:
        stat = os.stat(source)
    except OSError as exc:
        raise RuntimeArtifactError(f"Asset source is missing: {source}") from exc
    expected = entry["source"]
    expected_size = int(expected["size"])
    expected_modified = int(expected["modified_ns"])
    content_hash = str(entry.get("content_hash", "")).strip()
    if not content_hash:
        raise RuntimeArtifactError(f"AssetIndex content_hash is missing for {source}")

    # std::filesystem::file_time_type has no cross-platform epoch or tick
    # contract. Windows indexes currently resemble FILETIME while Linux
    # indexes use the implementation's native clock, and copying a project
    # between filesystems can also change mtime without changing its bytes.
    # Size + mtime remain the fast path; an mtime mismatch is resolved by the
    # native content hash instead of rejecting an otherwise identical source.
    current = {
        "size": int(stat.st_size),
        "modified_ns": unix_ns_to_filetime_ticks(stat.st_mtime_ns),
    }
    if current["size"] != expected_size:
        raise RuntimeArtifactError(
            f"Asset source fingerprint is stale for {source}: "
            f"expected={expected!r}, current={current!r}"
        )
    if current["modified_ns"] != expected_modified:
        actual_hash = _source_content_hash(source)
        if actual_hash.casefold() != content_hash.casefold():
            raise RuntimeArtifactError(
                f"Asset source fingerprint is stale for {source}: "
                f"expected={expected!r}, current={current!r}, "
                f"expected_content_hash={content_hash!r}, actual_content_hash={actual_hash!r}"
            )
        current["content_hash"] = actual_hash
    else:
        current["content_hash"] = content_hash
    return current


def _particle_artifact_source_key(path: str | os.PathLike[str]) -> str:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    owner = payload.get("source_key")
    return owner if isinstance(owner, str) else ""


def artifact_source_hash(path: str | os.PathLike[str]) -> str:
    """Read the source hash embedded by the current Library artifact writers."""

    artifact = Path(path)
    suffix = artifact.suffix.casefold()
    try:
        if suffix in {".inxparticle", ".inxeffect"}:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            value = payload.get("source_hash")
            if not isinstance(value, str) or not value or any(
                character not in "0123456789abcdefABCDEF" for character in value
            ):
                raise RuntimeArtifactError(f"JSON artifact has no source_hash: {artifact}")
            return value
        raw = artifact.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeArtifactError(f"Library artifact is unreadable: {artifact}") from exc
    magic = _BINARY_ARTIFACT_MAGIC.get(suffix)
    if magic is None or len(raw) < len(magic) + 8:
        raise RuntimeArtifactError(f"unsupported or truncated Library artifact: {artifact}")
    if not raw.startswith(magic):
        raise RuntimeArtifactError(f"Library artifact has an invalid header: {artifact}")
    marker_offset = len(magic)
    if raw[marker_offset : marker_offset + 4] != b"\x04\x03\x02\x01":
        raise RuntimeArtifactError(f"Library artifact has an invalid endian marker: {artifact}")
    hash_size_offset = marker_offset + 4
    hash_offset = hash_size_offset + 4
    hash_size = int.from_bytes(
        raw[hash_size_offset:hash_offset], "little", signed=False
    )
    if hash_size <= 0 or hash_offset + hash_size > len(raw):
        raise RuntimeArtifactError(f"Library artifact has an invalid source hash: {artifact}")
    value = raw[hash_offset : hash_offset + hash_size].decode("ascii", errors="ignore")
    if len(value) != hash_size or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise RuntimeArtifactError(f"Library artifact has no current source hash: {artifact}")
    return value


def expected_artifact_source_hash(
    project_root: str | os.PathLike[str],
    entry: dict[str, Any],
    artifact_path: str | os.PathLike[str],
) -> str:
    """Return the source hash format expected by a concrete artifact kind."""

    suffix = Path(artifact_path).suffix.casefold()
    if suffix == ".inxeffect":
        source = source_path_for_entry(project_root, entry)
        try:
            return hashlib.sha256(Path(source).read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeArtifactError(
                f"RenderEffect source cannot be fingerprinted: {source}"
            ) from exc
    if suffix != ".inxparticle":
        return str(entry["content_hash"])
    source = source_path_for_entry(project_root, entry)
    try:
        from Infernux.particle.asset import ParticleGraphAsset

        graph = ParticleGraphAsset.from_json(
            Path(source).read_text(encoding="utf-8")
        )
        return hashlib.sha256(graph.canonical_json().encode("utf-8")).hexdigest()
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeArtifactError(
            f"ParticleGraph source cannot produce a current artifact fingerprint: {source}"
        ) from exc


def validate_artifact(
    project_root: str | os.PathLike[str],
    entry: dict[str, Any],
    artifact_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Validate one Library artifact against its current AssetIndex entry."""

    if entry.get("import_succeeded") is False:
        raise RuntimeArtifactError(
            f"AssetIndex reports an unsuccessful import for {entry.get('guid', '')}"
        )
    source_fp = source_fingerprint(project_root, entry)
    root = resolved_path(project_root)
    artifact = resolved_path(artifact_path)
    try:
        artifact_relative = relative_path(artifact, root)
    except (OSError, ValueError) as exc:
        raise RuntimeArtifactError(
            f"Library artifact path escapes the project: {artifact}"
        ) from exc
    if not os.path.isfile(artifact):
        raise RuntimeArtifactError(f"Library artifact is missing: {artifact}")
    if Path(artifact).suffix.casefold() == ".inxparticle":
        owner = _particle_artifact_source_key(artifact)
        guid = str(entry.get("guid", ""))
        if (
            owner
            and guid
            and re.fullmatch(r"[0-9a-fA-F]{32}", owner)
            and owner.casefold() != guid.casefold()
        ):
            raise RuntimeArtifactError(
                f"Library particle artifact belongs to {owner}, not {guid}: "
                f"{artifact}"
            )
    expected_hash = expected_artifact_source_hash(project_root, entry, artifact)
    embedded_hash = artifact_source_hash(artifact)
    if embedded_hash.casefold() != expected_hash.casefold():
        raise RuntimeArtifactError(
            f"Library artifact is stale for {entry['guid']}: {artifact} "
            f"embedded={embedded_hash!r}, expected={expected_hash!r}"
        )
    source_path = source_path_for_entry(project_root, entry)
    return {
        "source_guid": str(entry["guid"]),
        "source_path": relative_path(source_path, root),
        "source_fingerprint": source_fp,
        "artifact_source_hash": expected_hash,
        "artifact_path": artifact_relative,
    }


def logical_asset_type(entry: dict[str, Any]) -> str:
    value = str(_metadata_value(entry, "resource_type", ""))
    if value:
        return value.casefold()
    suffix = Path(str(entry.get("normalized_path", ""))).suffix.casefold()
    if suffix in _DIRECT_TEXTURE_SUFFIXES:
        return "texture"
    if suffix in _DIRECT_MODEL_SUFFIXES:
        return "mesh"
    if suffix == ".particlegraph":
        return "particlegraph"
    return ""


def package_kind(package_path: str) -> str:
    name = PurePosixPath(package_path.replace("\\", "/")).name.casefold()
    if name == "runtime.inxrt":
        return "runtime"
    if name == "content.inxpkg":
        return "content"
    if name == "parallel.inxmod":
        return "parallel"
    return PurePosixPath(name).suffix.removeprefix(".") or "package"


def normalize_runtime_path(path: str) -> str:
    value = str(path).replace("\\", "/").lstrip("/")
    if not value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"unsafe runtime artifact path: {path!r}")
    return value


def logical_type_for_path(path: str) -> str:
    normalized = normalize_runtime_path(path)
    lower = normalized.casefold()
    suffix = PurePosixPath(normalized).suffix.casefold()
    if lower.startswith("infernux/resources/shaders/"):
        return "builtin_shader"
    if lower.startswith("infernux/resources/"):
        return "builtin_resource"
    if lower.startswith(("branding/", "splash/")):
        return "player_branding"
    if lower.startswith("library/artifacts/document/"):
        document_type = _DOCUMENT_TYPES.get(suffix, "project_runtime_document")
        return f"{document_type}_artifact"
    if lower.startswith("library/artifacts/audio/"):
        return "audio_artifact"
    if lower.startswith("library/artifacts/blob/"):
        return "project_runtime_blob_artifact"
    if suffix in _DOCUMENT_TYPES:
        return _DOCUMENT_TYPES[suffix]
    if suffix in _AUDIO_TYPES:
        return _AUDIO_TYPES[suffix]
    if suffix in _DIRECT_TEXTURE_SUFFIXES:
        return "texture_source"
    if suffix in _DIRECT_MODEL_SUFFIXES:
        return "model_source"
    if suffix == ".pyc":
        return "compiled_script"
    if suffix == ".inxparticle":
        return "particle_graph_artifact"
    if suffix == ".inxmesh":
        return "mesh_artifact"
    if suffix == ".inxskin":
        return "skinned_mesh_artifact"
    if suffix == ".inxtex":
        return "texture_artifact"
    if suffix == ".inxeffect":
        return "render_effect_artifact"
    if suffix in {".json", ".yaml", ".yml"}:
        if lower.startswith("assets/"):
            return "project_runtime_document"
        return "runtime_metadata"
    if lower.startswith("assets/"):
        return "project_runtime_blob"
    return "runtime_binary"


def payload_kind_for(logical_type: str) -> str:
    if logical_type in {
        "scene",
        "prefab",
        "material",
        "render_effect",
        "render_effect_group",
        "timeline",
        "timeline_fsm",
        "animation_clip",
        "animation_clip_2d",
        "animation_clip_3d",
        "animation_timeline",
        "animation_fsm",
    }:
        return "serialized_runtime_document"
    if logical_type == "compiled_script":
        return "compiled_script"
    if logical_type in {
        "audio",
        "texture_source",
        "model_source",
        "project_runtime_document",
        "project_runtime_blob",
    }:
        return "direct_runtime_asset"
    if logical_type.endswith("_artifact") or logical_type == "builtin_shader":
        return "compiled_artifact"
    return "runtime_binary"


def runtime_artifact_reason_for(logical_type: str) -> str | None:
    return _RUNTIME_ARTIFACT_REASON_BY_LOGICAL_TYPE.get(str(logical_type))


def runtime_artifact_id(package: str, runtime_path: str) -> str:
    """Return the stable ID for a package entry.

    The ID intentionally excludes the project name, absolute paths and
    content bytes.  Editing an asset changes its digest, while its runtime
    identity remains stable as long as its package namespace and path stay
    stable.
    """

    canonical = f"{package_kind(package)}:{normalize_runtime_path(runtime_path)}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"ra_{digest[:32]}"


_COMPACT_ASSET_GUID = re.compile(r"[0-9a-fA-F]{32}")


_SCALAR_ASSET_GUID_FIELDS = frozenset(
    {
        "dependencies",
        "materials",
        "material_guids",
        "mesh_guids",
        "shader_guids",
    }
)
_NON_ASSET_GUID_FIELDS = frozenset({"type_guid", "stable_id"})


def _asset_refs(value: Any, field_name: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        # Python serialized fields use the explicit asset_ref marker, while
        # several native/current documents (materials, effect groups and
        # renderer components) store the same GUID/path pair without it.
        # Both are durable asset identities and must contribute to the Player
        # catalog dependency graph.
        if value.get("$type") == "asset_ref" or (
            "guid" in value and ("path_hint" in value or "asset_type" in value)
        ):
            guid = value.get("guid")
            path_hint = value.get("path_hint")
            if not isinstance(guid, str):
                guid = ""
            if not isinstance(path_hint, str):
                path_hint = ""
            if guid or path_hint:
                yield guid, path_hint
        for key, item in value.items():
            yield from _asset_refs(item, str(key))
    elif isinstance(value, list):
        for item in value:
            yield from _asset_refs(item, field_name)
    elif (
        isinstance(value, str)
        and _COMPACT_ASSET_GUID.fullmatch(value)
        and (
            (
                field_name.casefold().endswith("guid")
                and field_name.casefold() not in _NON_ASSET_GUID_FIELDS
            )
            or field_name.casefold() in _SCALAR_ASSET_GUID_FIELDS
        )
    ):
        # Native scene serializers keep common references such as material,
        # mesh and shader GUIDs as compact scalar strings. Type GUIDs and
        # particle stable IDs are identities, not asset dependencies.
        yield value, ""


def _dependencies(
    payload: bytes | None,
    path_index: dict[str, str],
    guid_index: dict[str, str],
) -> tuple[list[str], list[dict[str, str]]]:
    if not payload:
        return [], []
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [], []
    ids: set[str] = set()
    unresolved: set[tuple[str, str]] = set()
    for guid, path_hint in _asset_refs(value):
        target = guid_index.get(guid) if guid else None
        if target is None and path_hint:
            normalized = path_hint.replace("\\", "/").lstrip("./").casefold()
            target = path_index.get(normalized)
            if target is None and normalized.startswith("assets/"):
                target = path_index.get(normalized[7:])
        if target is not None:
            ids.add(target)
            continue
        if guid:
            unresolved.add(("guid", guid))
        if path_hint:
            unresolved.add(("path", path_hint))
    return sorted(ids), [
        {"kind": kind, "value": value}
        for kind, value in sorted(unresolved)
    ]


def build_catalog(
    package_entries: Iterable[dict[str, Any]],
    *,
    player_host: dict[str, Any],
    package_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic catalog from native package TOC entries.

    Each item must contain ``package``, ``runtime_path`` and ``bytes``.
    ``payload`` is optional and is used only to derive dependency IDs; it is
    never copied into the catalog. Package identity remains owned by the
    container and is deliberately not duplicated here.
    """

    prepared: list[dict[str, Any]] = []
    path_index: dict[str, str] = {}
    guid_index: dict[str, str] = {}
    guid_alias_candidates: dict[str, list[dict[str, Any]]] = {}
    source_alias_candidates: dict[str, list[dict[str, Any]]] = {}
    seen_artifact_ids: set[str] = set()
    for item in package_entries:
        package = str(item["package"]).replace("\\", "/")
        runtime_path = normalize_runtime_path(str(item["runtime_path"]))
        logical_type = logical_type_for_path(runtime_path)
        artifact_id = runtime_artifact_id(package, runtime_path)
        if artifact_id in seen_artifact_ids:
            raise RuntimeArtifactError(
                f"duplicate runtime artifact identity: {package}::{runtime_path}"
            )
        seen_artifact_ids.add(artifact_id)
        record = {
            "runtime_artifact_id": artifact_id,
            "logical_type": logical_type,
            "payload_kind": payload_kind_for(logical_type),
            "package": package,
            "runtime_path": runtime_path,
            "content_bytes": int(item["bytes"]),
            "dependencies": [],
            "unresolved_dependencies": [],
        }
        if record["payload_kind"] in RUNTIME_DOCUMENT_PAYLOAD_KINDS:
            raise RuntimeArtifactError(
                "direct or serialized runtime payload is forbidden in the Player "
                f"package; expected a Library artifact: {package}::{runtime_path}"
            )
        binding = item.get("asset_binding")
        if binding is not None:
            if not isinstance(binding, dict):
                raise RuntimeArtifactError(
                    f"asset binding for {package}::{runtime_path} is not an object"
                )
            record["source_asset"] = json.loads(
                json.dumps(binding, ensure_ascii=False, sort_keys=True)
            )
            source_guid = binding.get("source_guid")
            if not isinstance(source_guid, str) or not source_guid:
                raise RuntimeArtifactError(
                    f"asset binding for {package}::{runtime_path} has no source GUID"
                )
            record["asset_guid"] = source_guid
            # One source asset may emit several artifacts, such as the mesh
            # and skin payloads produced by a single GLB. Resolve their public
            # GUID/path aliases after every record is known so input iteration
            # order cannot choose a different primary artifact.
            guid_alias_candidates.setdefault(source_guid, []).append(record)
            reason = binding.get("runtime_artifact_reason")
            if reason is not None:
                raise RuntimeArtifactError(
                    "compiled runtime payload must not declare a direct-payload reason: "
                    f"{package}::{runtime_path}"
                )
        prepared.append(record | {"_payload": item.get("payload")})
        runtime_key = runtime_path.casefold()
        existing_path_artifact = path_index.get(runtime_key)
        if existing_path_artifact is not None and existing_path_artifact != artifact_id:
            raise RuntimeArtifactError(
                f"runtime artifact path is ambiguous: {runtime_path}"
            )
        path_index[runtime_key] = artifact_id
        if runtime_path.casefold().startswith("assets/"):
            path_index.setdefault(runtime_path[7:].casefold(), artifact_id)
        else:
            path_index.setdefault(f"assets/{runtime_path.casefold()}", artifact_id)
        if isinstance(binding, dict):
            source_path = binding.get("source_path")
            if isinstance(source_path, str) and source_path:
                source_runtime_path = normalize_runtime_path(source_path)
                source_key = source_runtime_path.casefold()
                source_alias_candidates.setdefault(source_key, []).append(record)

    def _primary_alias_record(records: list[dict[str, Any]]) -> dict[str, Any]:
        priority = {
            "mesh_artifact": 0,
            "texture_artifact": 0,
            "particle_graph_artifact": 0,
            "render_effect_artifact": 0,
            "skinned_mesh_artifact": 1,
        }
        return min(
            records,
            key=lambda value: (
                priority.get(str(value["logical_type"]), 0),
                str(value["runtime_path"]).casefold(),
                str(value["runtime_artifact_id"]),
            ),
        )

    for source_guid, records in guid_alias_candidates.items():
        guid_index[source_guid] = str(
            _primary_alias_record(records)["runtime_artifact_id"]
        )

    for source_key, records in source_alias_candidates.items():
        source_guids = {
            str(record.get("asset_guid", ""))
            for record in records
            if record.get("asset_guid")
        }
        if len(source_guids) != 1:
            source_path = str(
                records[0].get("source_asset", {}).get("source_path", source_key)
            )
            raise RuntimeArtifactError(
                f"runtime source alias is ambiguous: {source_path}"
            )
        primary_id = str(_primary_alias_record(records)["runtime_artifact_id"])
        existing_source_artifact = path_index.get(source_key)
        if (
            existing_source_artifact is not None
            and existing_source_artifact != primary_id
        ):
            source_path = str(
                records[0].get("source_asset", {}).get("source_path", source_key)
            )
            raise RuntimeArtifactError(
                f"runtime source alias is ambiguous: {source_path}"
            )
        path_index[source_key] = primary_id
        if source_key.startswith("assets/"):
            path_index.setdefault(source_key[7:], primary_id)

    for record in prepared:
        dependencies, unresolved = _dependencies(
            record.pop("_payload"),
            path_index,
            guid_index,
        )
        binding = record.get("source_asset")
        if isinstance(binding, dict):
            bound_dependencies = binding.get("dependencies", [])
            if not isinstance(bound_dependencies, list) or any(
                not isinstance(value, str) for value in bound_dependencies
            ):
                raise RuntimeArtifactError(
                    f"asset binding dependencies are invalid for {record['runtime_artifact_id']}"
                )
            dependencies = sorted(set(dependencies).union(bound_dependencies))
        record["unresolved_dependencies"] = unresolved
        record["dependencies"] = dependencies

    artifacts = sorted(prepared, key=lambda value: value["runtime_artifact_id"])
    return {
        "$schema": CATALOG_SCHEMA,
        "player_host": player_host,
        "packages": sorted(package_records, key=lambda value: value["path"]),
        "artifacts": artifacts,
    }


__all__ = [
    "CATALOG_SCHEMA",
    "build_catalog",
    "artifact_source_hash",
    "expected_artifact_source_hash",
    "load_asset_index",
    "logical_type_for_path",
    "logical_asset_type",
    "normalize_runtime_path",
    "package_kind",
    "payload_kind_for",
    "RUNTIME_ARTIFACT_REASONS",
    "RUNTIME_DOCUMENT_PAYLOAD_KINDS",
    "runtime_artifact_id",
    "runtime_artifact_reason_for",
    "RuntimeArtifactError",
    "source_fingerprint",
    "source_path_for_entry",
    "unix_ns_to_filetime_ticks",
    "WINDOWS_FILETIME_EPOCH_OFFSET_TICKS",
    "validate_artifact",
]
