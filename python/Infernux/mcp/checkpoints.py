"""Supervisor-owned project checkpoints and persistence ledgers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any
import uuid

from Infernux.engine.path_utils import is_path_within, portable_path, relative_path, resolved_path, same_path


CHECKPOINT_ROOTS = ("Assets", "ProjectSettings")
_CHECKPOINT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_IGNORED_DIRECTORY_NAMES = frozenset({"__pycache__"})
_IGNORED_FILE_NAMES = frozenset({".infernux-engine-lock.json", "mcp_capabilities.json", ".DS_Store"})
_PRESERVED_PROJECT_PATHS = ("ProjectSettings/EditorSettings.json",)
_IGNORED_PROJECT_PATHS = frozenset(path.lower() for path in _PRESERVED_PROJECT_PATHS)


class CheckpointError(RuntimeError):
    """Raised when a managed checkpoint cannot be proven or restored safely."""


def normalize_checkpoint_id(value: str) -> str:
    checkpoint_id = str(value or "").strip()
    if not _CHECKPOINT_ID.fullmatch(checkpoint_id) or checkpoint_id in {".", ".."}:
        raise ValueError(
            "Checkpoint identifiers must be 1-96 characters using letters, digits, '.', '_' or '-', "
            "and must start with a letter or digit."
        )
    return checkpoint_id


def checkpoint_directory(artifact_root: str, checkpoint_id: str) -> str:
    safe_id = normalize_checkpoint_id(checkpoint_id)
    root = resolved_path(artifact_root)
    return os.path.join(root, "checkpoints", safe_id)


def capture_project_ledger(
    project_root: str,
    *,
    force_include_paths: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    return _capture_project_ledger(project_root, force_include_paths=force_include_paths)


def _capture_project_ledger(
    project_root: str,
    *,
    force_include_paths: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    root = resolved_path(project_root)
    forced = {portable_path(str(path)) for path in force_include_paths}
    entries: list[dict[str, Any]] = []
    roots_present: list[str] = []
    for root_name in CHECKPOINT_ROOTS:
        source_root = os.path.join(root, root_name)
        if not os.path.isdir(source_root):
            continue
        _reject_link(source_root, root)
        roots_present.append(root_name)
        for current_root, directories, files in os.walk(source_root, topdown=True, followlinks=False):
            kept_directories = []
            for directory in sorted(directories):
                path = os.path.join(current_root, directory)
                if directory in _IGNORED_DIRECTORY_NAMES:
                    continue
                _reject_link(path, root)
                kept_directories.append(directory)
            directories[:] = kept_directories
            for filename in sorted(files):
                path = os.path.join(current_root, filename)
                relative = relative_path(path, root)
                if relative not in forced and _ignore_file(filename, relative):
                    continue
                _reject_link(path, root)
                if not os.path.isfile(path):
                    raise CheckpointError(f"Checkpoint source is not a regular file: {relative}")
                size = os.path.getsize(path)
                entries.append({
                    "path": relative,
                    "size": int(size),
                    "sha256": _sha256_file(path),
                    "kind": _entry_kind(relative),
                })
    entries.sort(key=lambda item: item["path"])
    digest = _ledger_digest(entries, roots_present)
    return {
        "kind": "infernux.mcp.project_ledger",
        "captured_at": time.time(),
        "roots": roots_present,
        "file_count": len(entries),
        "total_bytes": sum(int(item["size"]) for item in entries),
        "digest": digest,
        "entries": entries,
    }


def create_checkpoint(
    project_root: str,
    artifact_root: str,
    checkpoint_id: str,
    *,
    session_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_id = normalize_checkpoint_id(checkpoint_id)
    project = resolved_path(project_root)
    destination = checkpoint_directory(artifact_root, safe_id)
    if os.path.exists(destination):
        raise FileExistsError(f"Managed checkpoint already exists: {safe_id}")

    checkpoints_root = os.path.dirname(destination)
    os.makedirs(checkpoints_root, exist_ok=True)
    temporary = os.path.join(checkpoints_root, f".{safe_id}.{uuid.uuid4().hex}.tmp")
    payload_root = os.path.join(temporary, "payload")
    try:
        source_ledger = capture_project_ledger(project)
        _copy_ledger_files(project, payload_root, source_ledger)
        payload_ledger = capture_project_ledger(payload_root)
        if payload_ledger["digest"] != source_ledger["digest"]:
            raise CheckpointError("Checkpoint payload hash does not match the source project ledger.")
        manifest = {
            "kind": "infernux.mcp.checkpoint",
            "checkpoint_id": safe_id,
            "session_id": str(session_id or ""),
            "project_root": project,
            "created_at": time.time(),
            "payload_root": "payload",
            "ledger": source_ledger,
            "metadata": dict(metadata or {}),
        }
        _write_json(os.path.join(temporary, "manifest.json"), manifest)
        os.replace(temporary, destination)
        return manifest | {
            "manifest_path": os.path.join(destination, "manifest.json"),
            "payload_path": os.path.join(destination, "payload"),
        }
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def load_checkpoint(
    project_root: str,
    artifact_root: str,
    checkpoint_id: str,
    *,
    session_id: str = "",
    verify_payload: bool = True,
) -> dict[str, Any]:
    safe_id = normalize_checkpoint_id(checkpoint_id)
    project = resolved_path(project_root)
    directory = checkpoint_directory(artifact_root, safe_id)
    manifest_path = os.path.join(directory, "manifest.json")
    manifest = _read_json(manifest_path)
    if not manifest:
        raise FileNotFoundError(f"Managed checkpoint was not found: {safe_id}")
    if manifest.get("kind") != "infernux.mcp.checkpoint":
        raise CheckpointError(f"Managed checkpoint manifest is unsupported: {safe_id}")
    if str(manifest.get("checkpoint_id", "")) != safe_id:
        raise CheckpointError("Checkpoint manifest identifier does not match its directory.")
    manifest_project = resolved_path(str(manifest.get("project_root", "") or ""))
    if not same_path(manifest_project, project):
        raise CheckpointError("Checkpoint belongs to a different project root.")
    expected_session = str(session_id or "")
    if expected_session and str(manifest.get("session_id", "") or "") != expected_session:
        raise CheckpointError("Checkpoint belongs to a different Supervisor session.")
    ledger = manifest.get("ledger")
    if not isinstance(ledger, dict) or not isinstance(ledger.get("entries"), list):
        raise CheckpointError("Checkpoint manifest has no valid project ledger.")
    payload_path = os.path.join(directory, str(manifest.get("payload_root", "payload") or "payload"))
    if verify_payload:
        recorded_paths = recorded_ledger_paths(ledger)
        payload_ledger = _capture_project_ledger(payload_path, force_include_paths=recorded_paths)
        if payload_ledger["digest"] != str(ledger.get("digest", "")):
            raise CheckpointError("Checkpoint payload no longer matches its manifest hash.")
    return manifest | {
        "manifest_path": manifest_path,
        "payload_path": payload_path,
    }


def checkpoint_status(
    project_root: str,
    artifact_root: str,
    checkpoint_id: str,
    *,
    session_id: str = "",
    include_current: bool = True,
) -> dict[str, Any]:
    safe_id = normalize_checkpoint_id(checkpoint_id)
    try:
        checkpoint = load_checkpoint(
            project_root,
            artifact_root,
            safe_id,
            session_id=session_id,
            verify_payload=True,
        )
    except FileNotFoundError:
        return {
            "checkpoint_id": safe_id,
            "exists": False,
            "payload_valid": False,
            "current_match": False,
        }
    expected = checkpoint["ledger"]
    result = {
        "checkpoint_id": safe_id,
        "exists": True,
        "payload_valid": True,
        "manifest_path": checkpoint["manifest_path"],
        "created_at": checkpoint["created_at"],
        "ledger_digest": expected["digest"],
        "file_count": expected["file_count"],
        "total_bytes": expected["total_bytes"],
    }
    if not include_current:
        return result
    current = capture_project_ledger(project_root, force_include_paths=recorded_ledger_paths(expected))
    delta = diff_ledgers(expected, current)
    return result | {
        "current_match": not _delta_has_changes(delta),
        "current_ledger_digest": current["digest"],
        "delta": compact_delta(delta),
    }


def list_checkpoints(project_root: str, artifact_root: str, *, session_id: str = "") -> list[dict[str, Any]]:
    """List payload-verified checkpoints without scanning the current project."""
    root = os.path.join(resolved_path(artifact_root), "checkpoints")
    if not os.path.isdir(root):
        return []

    results: list[dict[str, Any]] = []
    for checkpoint_id in sorted(os.listdir(root)):
        path = os.path.join(root, checkpoint_id)
        if not os.path.isdir(path) or checkpoint_id.startswith("."):
            continue
        try:
            manifest = load_checkpoint(
                project_root,
                artifact_root,
                checkpoint_id,
                session_id=session_id,
                verify_payload=True,
            )
        except (OSError, ValueError, CheckpointError):
            continue
        ledger = manifest.get("ledger") or {}
        results.append({
            "checkpoint_id": str(manifest.get("checkpoint_id") or checkpoint_id),
            "created_at": float(manifest.get("created_at", 0.0) or 0.0),
            "ledger_digest": str(ledger.get("digest") or ""),
            "file_count": int(ledger.get("file_count", 0) or 0),
            "total_bytes": int(ledger.get("total_bytes", 0) or 0),
            "payload_valid": True,
            "metadata": dict(manifest.get("metadata") or {}),
        })
    return results


def restore_checkpoint(
    project_root: str,
    artifact_root: str,
    checkpoint_id: str,
    *,
    session_id: str,
) -> dict[str, Any]:
    project = resolved_path(project_root)
    checkpoint = load_checkpoint(
        project,
        artifact_root,
        checkpoint_id,
        session_id=session_id,
        verify_payload=True,
    )
    expected = checkpoint["ledger"]
    recorded_paths = recorded_ledger_paths(expected)
    before = capture_project_ledger(project, force_include_paths=recorded_paths)
    journal = os.path.join(
        resolved_path(artifact_root),
        "checkpoint-restores",
        f"restore-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}",
    )
    staged_root = os.path.join(journal, "staged")
    backup_root = os.path.join(journal, "backup")
    os.makedirs(backup_root, exist_ok=True)
    _copy_ledger_files(checkpoint["payload_path"], staged_root, expected)
    _copy_preserved_project_files(project, staged_root, recorded_paths=recorded_paths)

    replaced: list[tuple[str, bool]] = []
    try:
        for root_name in CHECKPOINT_ROOTS:
            current_root = os.path.join(project, root_name)
            staged = os.path.join(staged_root, root_name)
            backup = os.path.join(backup_root, root_name)
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            os.makedirs(staged, exist_ok=True)
            had_current = os.path.exists(current_root)
            if had_current:
                _replace_root(current_root, backup)
            try:
                _replace_root(staged, current_root)
            except Exception:
                if had_current and os.path.exists(backup):
                    _replace_root(backup, current_root)
                raise
            replaced.append((root_name, had_current))

        after = capture_project_ledger(project, force_include_paths=recorded_paths)
        if after["digest"] != expected["digest"]:
            raise CheckpointError("Restored project ledger does not match the checkpoint manifest.")
    except Exception:
        _rollback_roots(project, backup_root, replaced)
        shutil.rmtree(journal, ignore_errors=True)
        raise

    delta = diff_ledgers(before, after)
    proof = {
        "kind": "infernux.mcp.checkpoint_restore",
        "restore_id": os.path.basename(journal),
        "restored_at": time.time(),
        "checkpoint_id": checkpoint["checkpoint_id"],
        "checkpoint_manifest_path": checkpoint["manifest_path"],
        "before_ledger_digest": before["digest"],
        "after_ledger_digest": after["digest"],
        "checkpoint_ledger_digest": expected["digest"],
        "verified": True,
        "delta": delta,
    }
    proof_path = os.path.join(os.path.dirname(journal), f"{os.path.basename(journal)}.json")
    _write_json(proof_path, proof)
    shutil.rmtree(journal, ignore_errors=True)
    return proof | {"proof_path": proof_path}


def diff_ledgers(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_entries = {str(item["path"]): item for item in before.get("entries", [])}
    after_entries = {str(item["path"]): item for item in after.get("entries", [])}
    before_paths = set(before_entries)
    after_paths = set(after_entries)
    added = sorted(after_paths - before_paths)
    deleted = sorted(before_paths - after_paths)
    modified = sorted(
        path for path in before_paths & after_paths
        if str(before_entries[path].get("sha256", "")) != str(after_entries[path].get("sha256", ""))
        or int(before_entries[path].get("size", 0)) != int(after_entries[path].get("size", 0))
    )
    return {
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "added_count": len(added),
        "modified_count": len(modified),
        "deleted_count": len(deleted),
    }


def compact_delta(delta: dict[str, Any], *, path_limit: int = 64) -> dict[str, Any]:
    limit = max(0, int(path_limit))
    result = {
        "added_count": int(delta.get("added_count", 0)),
        "modified_count": int(delta.get("modified_count", 0)),
        "deleted_count": int(delta.get("deleted_count", 0)),
    }
    for key in ("added", "modified", "deleted"):
        values = [str(value) for value in delta.get(key, [])]
        result[key] = values[:limit]
        result[f"{key}_truncated"] = len(values) > limit
    return result


def _delta_has_changes(delta: dict[str, Any]) -> bool:
    return any(int(delta.get(f"{kind}_count", 0)) for kind in ("added", "modified", "deleted"))


def recorded_ledger_paths(ledger: dict[str, Any]) -> set[str]:
    return {
        portable_path(str(entry.get("path", "") or ""))
        for entry in ledger.get("entries", [])
    }


def _copy_ledger_files(source_root: str, destination_root: str, ledger: dict[str, Any]) -> None:
    source = resolved_path(source_root)
    destination = resolved_path(destination_root)
    for root_name in CHECKPOINT_ROOTS:
        os.makedirs(os.path.join(destination, root_name), exist_ok=True)
    for entry in ledger.get("entries", []):
        relative = str(entry.get("path", "") or "")
        source_path = _safe_relative_path(source, relative)
        destination_path = _safe_relative_path(destination, relative)
        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        shutil.copy2(source_path, destination_path)


def _copy_preserved_project_files(
    source_root: str,
    destination_root: str,
    *,
    recorded_paths: set[str],
) -> None:
    recorded = {portable_path(path).lower() for path in recorded_paths}
    for relative in _PRESERVED_PROJECT_PATHS:
        if relative.lower() in recorded:
            continue
        source_path = _safe_relative_path(source_root, relative)
        if not os.path.exists(source_path):
            continue
        _reject_link(source_path, source_root)
        if not os.path.isfile(source_path):
            raise CheckpointError(f"Preserved project setting is not a regular file: {relative}")
        destination_path = _safe_relative_path(destination_root, relative)
        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        shutil.copy2(source_path, destination_path)


def _rollback_roots(project_root: str, backup_root: str, replaced: list[tuple[str, bool]]) -> None:
    failures = []
    for root_name, had_current in reversed(replaced):
        current = os.path.join(project_root, root_name)
        backup = os.path.join(backup_root, root_name)
        try:
            if os.path.isdir(current):
                shutil.rmtree(current)
            elif os.path.exists(current):
                os.remove(current)
            if had_current and os.path.exists(backup):
                _replace_root(backup, current)
        except OSError as exc:
            failures.append(f"{root_name}: {exc}")
    if failures:
        raise CheckpointError("Checkpoint restore rollback failed: " + "; ".join(failures))


def _safe_relative_path(root: str, relative: str) -> str:
    normalized = portable_path(str(relative or "")).strip("/")
    if not normalized or normalized.startswith("../") or "/../" in f"/{normalized}/":
        raise CheckpointError(f"Unsafe checkpoint ledger path: {relative!r}")
    target = resolved_path(os.path.join(root, *normalized.split("/")))
    if not is_path_within(target, root):
        raise CheckpointError(f"Checkpoint path leaves its project root: {relative!r}")
    return target


def _replace_root(source: str, destination: str) -> None:
    os.replace(source, destination)


def _reject_link(path: str, project_root: str) -> None:
    value = Path(path)
    is_junction = bool(getattr(os.path, "isjunction", lambda _path: False)(path))
    if value.is_symlink() or is_junction:
        relative = relative_path(path, project_root)
        raise CheckpointError(f"Managed checkpoints do not follow links or junctions: {relative}")


def _ignore_file(filename: str, relative: str) -> bool:
    if filename in _IGNORED_FILE_NAMES:
        return True
    if portable_path(relative).lower() in _IGNORED_PROJECT_PATHS:
        return True
    lowered = filename.lower()
    return lowered.endswith((".pyc", ".pyo")) or "/__pycache__/" in f"/{relative}/"


def _entry_kind(relative: str) -> str:
    lowered = relative.lower()
    if lowered.startswith("projectsettings/"):
        return "project_setting"
    if lowered.endswith(".scene"):
        return "scene"
    if lowered.endswith(".prefab"):
        return "prefab"
    if lowered.endswith(".meta"):
        return "asset_meta"
    if lowered.endswith(".py"):
        return "project_script"
    return "asset"


def _ledger_digest(entries: list[dict[str, Any]], roots: list[str]) -> str:
    stable = {
        "roots": list(roots),
        "entries": [
            {
                "path": item["path"],
                "size": int(item["size"]),
                "sha256": item["sha256"],
                "kind": item["kind"],
            }
            for item in entries
        ],
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: str, value: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
