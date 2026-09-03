"""Build a deterministic manifest for release artifacts and acceptance results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SCHEMA = "infernux.release_evidence"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_record(identifier: str, path: Path, root: Path) -> dict[str, object]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise ValueError(f"Evidence path is outside the evidence root: {resolved}") from error
    if resolved.is_symlink():
        raise ValueError(f"Evidence artifacts cannot be symlinks: {resolved}")
    if resolved.is_file():
        return {
            "id": identifier,
            "path": relative,
            "kind": "file",
            "sha256": sha256_file(resolved),
            "size": resolved.stat().st_size,
            "file_count": 1,
        }
    if not resolved.is_dir():
        raise FileNotFoundError(resolved)

    digest = hashlib.sha256()
    total_size = 0
    file_count = 0
    for candidate in sorted(
        resolved.rglob("*"), key=lambda item: item.relative_to(resolved).as_posix()
    ):
        if candidate.is_symlink():
            raise ValueError(f"Evidence artifacts cannot contain symlinks: {candidate}")
        if not candidate.is_file():
            continue
        item_path = candidate.relative_to(resolved).as_posix()
        item_hash = sha256_file(candidate)
        item_size = candidate.stat().st_size
        digest.update(item_path.encode("utf-8") + b"\0")
        digest.update(item_hash.encode("ascii") + b"\0")
        digest.update(str(item_size).encode("ascii") + b"\0")
        total_size += item_size
        file_count += 1
    return {
        "id": identifier,
        "path": relative,
        "kind": "directory",
        "sha256": digest.hexdigest(),
        "size": total_size,
        "file_count": file_count,
    }


def result_record(identifier: str, path: Path, root: Path) -> dict[str, object]:
    record = artifact_record(identifier, path, root)
    if record["kind"] != "file":
        raise ValueError(f"Acceptance result must be a JSON file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Acceptance result is not valid JSON: {path}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"Acceptance result must contain a JSON object: {path}")
    status = payload.get("status")
    success = payload.get("success")
    record["status"] = str(status) if status is not None else ""
    record["success"] = bool(success) if isinstance(success, bool) else None
    return record


def repository_record(repository: Path, *, require_clean: bool) -> dict[str, object]:
    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
            )
        return completed.stdout.strip()

    status = git("status", "--porcelain", "--untracked-files=normal")
    dirty = bool(status)
    if require_clean and dirty:
        raise RuntimeError("Release evidence requires a clean Git worktree")
    timestamp = int(git("show", "-s", "--format=%ct", "HEAD"))
    generated_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    return {
        "branch": git("branch", "--show-current"),
        "commit": git("rev-parse", "HEAD"),
        "dirty": dirty,
        "generated_at": generated_at,
    }


def parse_named_paths(values: Iterable[str]) -> tuple[tuple[str, Path], ...]:
    parsed: list[tuple[str, Path]] = []
    identifiers: set[str] = set()
    for raw in values:
        identifier, separator, path_text = raw.partition("=")
        identifier = identifier.strip()
        if not separator or not identifier or not path_text.strip():
            raise ValueError(f"Expected ID=PATH, got: {raw!r}")
        if identifier in identifiers:
            raise ValueError(f"Duplicate evidence identifier: {identifier}")
        identifiers.add(identifier)
        parsed.append((identifier, Path(path_text).expanduser()))
    return tuple(parsed)


def build_manifest(
    *,
    release: str,
    root: Path,
    repository: Path,
    artifacts: Iterable[tuple[str, Path]],
    results: Iterable[tuple[str, Path]],
    require_clean: bool,
) -> dict[str, object]:
    artifact_entries = [artifact_record(name, path, root) for name, path in artifacts]
    result_entries = [result_record(name, path, root) for name, path in results]
    all_ids = [str(item["id"]) for item in (*artifact_entries, *result_entries)]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("Artifact and result identifiers must be globally unique")
    return {
        "$schema": SCHEMA,
        "release": release,
        "source": repository_record(repository.resolve(), require_clean=require_clean),
        "environment": {
            "platform": platform.system().casefold(),
            "platform_release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "conda_environment": os.environ.get("CONDA_DEFAULT_ENV", ""),
        },
        "artifacts": sorted(artifact_entries, key=lambda item: str(item["id"])),
        "results": sorted(result_entries, key=lambda item: str(item["id"])),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--artifact", action="append", default=[], metavar="ID=PATH")
    parser.add_argument("--result", action="append", default=[], metavar="ID=PATH")
    parser.add_argument("--require-clean", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    output = arguments.output.expanduser().resolve()
    manifest = build_manifest(
        release=str(arguments.release),
        root=arguments.root.expanduser().resolve(),
        repository=arguments.repository.expanduser().resolve(),
        artifacts=parse_named_paths(arguments.artifact),
        results=parse_named_paths(arguments.result),
        require_clean=bool(arguments.require_clean),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
