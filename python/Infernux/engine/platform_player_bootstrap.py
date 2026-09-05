"""Bootstrap a cooked Player inside a platform-owned Python/native host."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from Infernux.engine.path_utils import resolved_path
from Infernux.engine.player_package_native import (
    ASSET_CATALOG_ARCHIVE_FILENAME,
    ASSET_CATALOG_ENTRY_PATH,
    BUILD_MANIFEST_ENTRY_PATH,
    extract_pack,
    read_entry,
    read_manifest,
)


_PACKAGE_INDEX_HEADER = "INFERNUX_PLAYER_PACKAGE_INDEX"
_CONTENT_ROOTS = {
    "Assets",
    "Branding",
    "Infernux",
    "Library",
    "Packages",
    "ProjectSettings",
    "Splash",
    "_script_guid_map.json",
}
_CONTENT_CACHE_PREFIX = "content-"
_CONTENT_CACHE_DIGEST_LENGTH = 24


def _package_index(data_root: Path) -> dict[str, tuple[str, int]]:
    index_path = data_root / "PackageIndex.inxmanifest"
    records: dict[str, tuple[str, int]] = {}
    try:
        lines = index_path.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Player package index is unreadable: {index_path}") from exc
    if not lines or lines[0] != _PACKAGE_INDEX_HEADER:
        raise RuntimeError("Player package index uses an unsupported format")
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != 3:
            raise RuntimeError("Player package index contains an invalid record")
        kind, digest, raw_size = parts
        if (
            kind not in {"runtime", "content", "catalog", "parallel"}
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RuntimeError("Player package index contains an invalid identity")
        try:
            archive_size = int(raw_size)
        except ValueError as exc:
            raise RuntimeError("Player package index contains an invalid size") from exc
        if archive_size < 0 or kind in records:
            raise RuntimeError("Player package index contains an invalid record")
        records[kind] = (digest, archive_size)
    return records


def _validate_indexed_archive(
    data_root: Path,
    kind: str,
    filename: str,
) -> tuple[Path, dict[str, object]]:
    expected = _package_index(data_root).get(kind)
    if expected is None:
        raise RuntimeError(f"Player package index does not declare {filename}")
    expected_hash, expected_size = expected
    archive = data_root / filename
    manifest = read_manifest(archive)
    if (
        manifest["archive_sha256"] != expected_hash
        or manifest["archive_bytes"] != expected_size
    ):
        raise RuntimeError(f"Player {filename} identity disagrees with its package index")
    return archive, manifest


def read_player_asset_catalog(data_root: str | Path) -> dict[str, object]:
    """Read the boot-validated catalog without exposing a Library directory."""

    root = Path(resolved_path(data_root))
    archive, _manifest = _validate_indexed_archive(
        root,
        "catalog",
        ASSET_CATALOG_ARCHIVE_FILENAME,
    )
    try:
        document = json.loads(
            read_entry(archive, ASSET_CATALOG_ENTRY_PATH).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Player asset catalog is unreadable") from exc
    if not isinstance(document, dict):
        raise RuntimeError("Player asset catalog root must be an object")
    return document


def read_player_build_manifest(data_root: str | Path) -> dict[str, object]:
    """Read the build presentation contract from the sealed catalog."""

    root = Path(resolved_path(data_root))
    archive, _manifest = _validate_indexed_archive(
        root,
        "catalog",
        ASSET_CATALOG_ARCHIVE_FILENAME,
    )
    try:
        document = json.loads(
            read_entry(archive, BUILD_MANIFEST_ENTRY_PATH).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Player build manifest is unreadable") from exc
    if not isinstance(document, dict):
        raise RuntimeError("Player build manifest root must be an object")
    return document


def _content_cache(data_root: Path, cache_root: Path) -> Path:
    archive, manifest = _validate_indexed_archive(
        data_root,
        "content",
        "Content.inxpkg",
    )
    expected_hash = str(manifest["archive_sha256"])
    expected_size = int(manifest["archive_bytes"])

    os.environ["_INFERNUX_PLAYER_CONTENT_ARCHIVE_SHA256"] = expected_hash
    os.environ["_INFERNUX_PLAYER_CONTENT_ARCHIVE_BYTES"] = str(expected_size)
    destination = cache_root / f"content-{expected_hash[:24]}"
    ready = destination / ".ready"
    try:
        if ready.read_text(encoding="ascii").strip() == expected_hash:
            _prune_content_caches(cache_root, destination)
            return destination
    except OSError:
        pass

    cache_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}-",
            dir=cache_root,
        )
    )
    try:
        extract_pack(
            archive,
            temporary,
            allowed_roots=_CONTENT_ROOTS,
        )
        ready_path = temporary / ".ready"
        ready_path.write_text(expected_hash + "\n", encoding="ascii", newline="\n")
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    _prune_content_caches(cache_root, destination)
    return destination


def _prune_content_caches(
    cache_root: Path,
    active: Path,
) -> None:
    """Keep only the active engine-owned content generation."""

    for child in tuple(cache_root.iterdir()):
        if child == active or not child.is_dir():
            continue
        name = child.name
        if not name.startswith(_CONTENT_CACHE_PREFIX):
            continue
        digest = name[len(_CONTENT_CACHE_PREFIX) :]
        if (
            len(digest) != _CONTENT_CACHE_DIGEST_LENGTH
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            continue
        shutil.rmtree(child)


def prepare_platform_player(package_root: str, cache_root: str) -> str:
    """Validate/extract a platform package and return its cooked project root."""

    package = Path(resolved_path(package_root))
    cache = Path(resolved_path(cache_root))
    if not package.is_dir():
        raise RuntimeError(f"Platform Player package root is missing: {package}")
    if not (package / "Player.inxmanifest").is_file():
        raise RuntimeError(
            "Platform Player package root must be the cooked Data directory"
        )
    manifest_path = package / "Player.inxmanifest"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        flavor = manifest["product"]["flavor"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Platform Player runtime manifest is unreadable") from exc
    if flavor not in {"PlayerDebug", "PlayerRelease"}:
        raise RuntimeError("Platform Player runtime manifest has an invalid flavor")
    build_manifest_document = read_player_build_manifest(package)
    project_root = _content_cache(package, cache)
    target_manifest = project_root / "BuildManifest.json"
    payload = (
        json.dumps(
            build_manifest_document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if not target_manifest.is_file() or target_manifest.read_bytes() != payload:
        temporary = target_manifest.with_name(target_manifest.name + ".tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, target_manifest)

    os.environ["_INFERNUX_PLAYER_MODE"] = "1"
    os.environ["_INFERNUX_PLAYER_DATA_ROOT"] = str(package)
    persistent_root = os.environ.get(
        "_INFERNUX_PLAYER_PERSISTENT_DATA_ROOT", ""
    ).strip()
    if not persistent_root:
        persistent_root = str(cache.parent / "Data")
        os.environ["_INFERNUX_PLAYER_PERSISTENT_DATA_ROOT"] = persistent_root
    Path(persistent_root).mkdir(parents=True, exist_ok=True)
    os.environ["_INFERNUX_PLAYER_DEBUG_BUILD"] = (
        "1" if flavor == "PlayerDebug" else "0"
    )
    log_directory = cache / "Logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    os.environ["_INFERNUX_PLAYER_LOG"] = str(log_directory / "player.log")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    return str(project_root)


def run_platform_player(package_root: str, cache_root: str) -> None:
    """Enter the shared standalone Player from a platform-native host."""

    project_root = prepare_platform_player(package_root, cache_root)
    from Infernux.engine import run_player
    from Infernux.lib import LogLevel

    run_player(project_root, engine_log_level=LogLevel.Debug)


__all__ = [
    "prepare_platform_player",
    "read_player_asset_catalog",
    "read_player_build_manifest",
    "run_platform_player",
]
