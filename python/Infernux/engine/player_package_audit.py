"""Deterministic audit and manifest generation for exported Player products."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .player_package_format import read_manifest


MANIFEST_FILENAME = "PlayerRuntimeManifest.json"
MANIFEST_SCHEMA = "infernux.player_runtime_manifest"

# These are author/runtime source formats, rather than runtime asset formats.
# Scene/material/model files are intentionally reported as pending artifact
# migration for R9; the current player still consumes some of those documents.
AUTHOR_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".pyx",
        ".glsl",
        ".vert",
        ".frag",
        ".comp",
        ".geom",
        ".tesc",
        ".tese",
        ".hlsl",
        ".shader",
        ".lua",
        ".cpp",
        ".c",
        ".h",
    }
)
NATIVE_SUFFIXES = frozenset({".exe", ".dll", ".pyd", ".so", ".dylib"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable(relative: str) -> str:
    return relative.replace("\\", "/").lstrip("./")


def _data_root(package_root: Path) -> Path:
    candidates = [
        package_root / f"{package_root.name}_Data",
        package_root / "Data",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise RuntimeError(f"Player Data directory not found below {package_root}")


def _iter_archive_entries(path: Path) -> Iterable[tuple[str, int, str]]:
    if path.suffix == ".inxpack" or path.name.endswith(".inxpack"):
        header, _payload = read_manifest(path)
        for entry in header["files"]:
            yield str(entry["path"]), int(entry["raw_bytes"]), str(entry["sha256"])
        return
    # ZIP is accepted only as a diagnostic for legacy products.  It is never
    # emitted by the new Player packer and therefore becomes a pending R9 item.
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                if not entry.is_dir():
                    with archive.open(entry) as stream:
                        digest = hashlib.sha256(stream.read()).hexdigest()
                    yield entry.filename.replace("\\", "/"), entry.file_size, digest


def _runtime_service_ids() -> list[str]:
    return [
        "player_bootstrap",
        "engine",
        "scene_file_manager",
        "play_mode_manager",
        "player_gui",
        "game_camera",
    ]


def audit_player_package(
    package_root: str | os.PathLike[str],
    *,
    write_manifest: bool = True,
) -> dict[str, object]:
    """Audit a final Player directory and optionally write its manifest.

    The audit is intentionally strict for source, metadata and duplicate
    native payloads.  Legacy ZIP containers and the current launcher/runtime
    split are reported as explicit migration gaps instead of being silently
    treated as a finished R9 implementation.
    """

    root = Path(package_root).resolve()
    if not root.is_dir():
        raise RuntimeError(f"Player package directory not found: {root}")
    data_root = _data_root(root)
    files: list[dict[str, object]] = []
    hashes: defaultdict[str, list[str]] = defaultdict(list)
    forbidden: list[str] = []
    author_sources: list[str] = []
    meta_files: list[str] = []
    native_files: list[str] = []
    archive_entries: list[dict[str, object]] = []
    legacy_zips: list[str] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_FILENAME:
            continue
        relative = path.relative_to(root).as_posix()
        digest = _sha256(path)
        size = path.stat().st_size
        suffix = path.suffix.casefold()
        hashes[digest].append(relative)
        files.append({"path": relative, "bytes": size, "sha256": digest})
        if suffix in NATIVE_SUFFIXES:
            native_files.append(relative)
        if suffix == ".meta":
            meta_files.append(relative)
        # Author sources are forbidden in the visible payload.  Runtime code
        # compiled by Nuitka is represented by binaries, not these files.
        if suffix in AUTHOR_SOURCE_SUFFIXES and (
            relative.startswith("Data/Assets/")
            or "::Assets/" in relative
        ):
            author_sources.append(relative)
        if suffix == ".zip":
            legacy_zips.append(relative)
        if suffix in {".inxpack", ".inxpkg", ".zip"}:
            try:
                for entry_name, raw_bytes, entry_hash in _iter_archive_entries(path):
                    archive_relative = f"{relative}::{entry_name}"
                    archive_entries.append(
                        {
                            "path": archive_relative,
                            "bytes": raw_bytes,
                            "sha256": entry_hash,
                        }
                    )
                    hashes[entry_hash].append(archive_relative)
                    entry_suffix = Path(entry_name).suffix.casefold()
                    if entry_suffix == ".meta":
                        meta_files.append(archive_relative)
                    if entry_suffix in AUTHOR_SOURCE_SUFFIXES and (
                        entry_name.startswith("Assets/")
                        or entry_name.startswith("ProjectSettings/")
                    ):
                        author_sources.append(archive_relative)
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                forbidden.append(f"{relative}: invalid container ({exc})")

    duplicate_payloads = [
        paths for paths in hashes.values() if len(paths) > 1
    ]
    duplicate_native = [
        paths
        for paths in duplicate_payloads
        if any(Path(path).suffix.casefold() in NATIVE_SUFFIXES for path in paths)
    ]
    executables = [path for path in files if Path(str(path["path"])).suffix.casefold() == ".exe"]
    dual_entry_point = len(executables) != 1
    layout = "infernux-windows-player" if (root / f"{root.name}_Data").is_dir() else "infernux-player-directory"
    content_manifest_path = data_root / "Content.json"
    content_manifest: dict[str, object] = {}
    if content_manifest_path.is_file():
        try:
            content_manifest = json.loads(content_manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            forbidden.append("Content.json: invalid JSON")

    result = {
        "$schema": MANIFEST_SCHEMA,
        "manifest_version": 1,
        "product": {
            "layout": layout,
            "flavor": "debug" if "debug" in root.name.casefold() else "release",
            "entry_points": [item["path"] for item in executables],
            "single_entry_point": len(executables) == 1,
        },
        "services": {
            "kind": "player",
            "declared": _runtime_service_ids(),
            "editor_services": [],
        },
        "reachability": {
            "build_manifest": "Data/BuildManifest.json"
            if layout == "infernux-player-directory"
            else f"{data_root.name}/BuildManifest.json",
            "content_manifest": content_manifest,
            "runtime_artifacts": [
                item["path"]
                for item in files
                if "/Artifacts/" in str(item["path"])
            ],
        },
        "audit": {
            "passed": not (
                forbidden
                or author_sources
                or meta_files
                or duplicate_payloads
                or legacy_zips
                or dual_entry_point
            ),
            "forbidden_files": sorted(set(forbidden)),
            "author_source_files": sorted(set(author_sources)),
            "meta_files": sorted(set(meta_files)),
            "duplicate_native_payloads": sorted(duplicate_native),
            "duplicate_payload_groups": sorted(duplicate_payloads),
            "legacy_zip_files": sorted(legacy_zips),
            "legacy_dual_entry_point": dual_entry_point,
        },
        "files": {
            "count": len(files),
            "bytes": sum(int(item["bytes"]) for item in files),
            "native_binary_count": len(native_files),
            "native_binary_bytes": sum(
                int(item["bytes"])
                for item in files
                if Path(str(item["path"])).suffix.casefold() in NATIVE_SUFFIXES
            ),
            "archive_entry_count": len(archive_entries),
            "archives": archive_entries,
        },
    }
    if not result["audit"]["passed"]:
        raise RuntimeError(
            "Player package audit failed: "
            + json.dumps(result["audit"], ensure_ascii=False, sort_keys=True)
        )
    if write_manifest:
        manifest_path = data_root / MANIFEST_FILENAME
        manifest_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="final Player output directory")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    audit_player_package(args.root, write_manifest=not args.no_write)
    print(f"Player package audit passed: {Path(args.root).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
