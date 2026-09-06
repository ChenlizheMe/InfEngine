"""Strict audit and manifest generation for exported Player products.

The audit is deliberately the last gate of the packaging pipeline.  It only
recognises the current native InxPack reader and the final single-entry layout.
Unrecognised containers, source files and metadata are rejected as unsupported
payloads; the audit does not classify or migrate historical formats.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import json
import os
import re
import struct
import sys
import time
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

from .path_utils import resolved_path
from .player_package_native import (
    ASSET_CATALOG_ARCHIVE_FILENAME,
    ASSET_CATALOG_ENTRY_PATH,
    BUILD_MANIFEST_ENTRY_PATH,
    extract_pack,
    read_entry,
    read_manifest,
)
from .player_service_graph import (
    PLAYER_MANIFEST_SCHEMA,
    RuntimeFeatureSet,
    RuntimeFlavor,
    RuntimeProductManifest,
    forbidden_player_service_modules,
    player_runtime_contract_sections,
)
from .runtime_artifact_catalog import (
    CATALOG_SCHEMA,
    RUNTIME_ARTIFACT_REASONS,
    RUNTIME_AUTHORING_DOCUMENT_SUFFIXES,
    RUNTIME_DOCUMENT_PAYLOAD_KINDS,
    logical_type_for_path,
    package_kind,
    payload_kind_for,
    runtime_artifact_id,
    runtime_artifact_reason_for,
)
from .python_abi import (
    BOOTSTRAP_NATIVE_MANIFEST_FILENAME,
    BOOTSTRAP_NATIVE_MANIFEST_SCHEMA,
    LINUX_PYTHON_SHARED_PREFIX,
    PYTHON_VERSION,
    WINDOWS_PYTHON_DLL,
    is_windows_libffi_dll,
    player_native_library_filenames,
)


MANIFEST_FILENAME = "Player.inxmanifest"
MANIFEST_SCHEMA = PLAYER_MANIFEST_SCHEMA

# These are the native files that the current single-process Player must be
# able to resolve before Runtime.inxrt has been extracted.  The allowlist is
# deliberately explicit rather than suffix-based: a random Nuitka DLL/PYD is
# not a bootstrap dependency merely because it has a familiar extension.
# Bootstrap files are intentionally inside Bootstrap.inxrt. The visible
# package root is only the game host executable plus its Data directory.
BOOTSTRAP_NATIVE_ROOT_ALLOWLIST: dict[str, dict[str, str]] = {}

if sys.platform == "win32":
    RUNTIME_REQUIRED_NATIVE_FILES = frozenset({
        "Infernux/lib/_Infernux.pyd",
        *(f"Infernux/lib/{name}" for name in player_native_library_filenames()),
    })
    RUNTIME_CONDITIONAL_NATIVE_FILES = frozenset({"Infernux/lib/zlib.dll"})
    BOOTSTRAP_REQUIRED_ARCHIVE_FILES = frozenset({
        BOOTSTRAP_NATIVE_MANIFEST_FILENAME,
        WINDOWS_PYTHON_DLL,
        "_ctypes.pyd",
        "_InfernuxBootstrap.pyd",
        "Infernux/lib/InfernuxFoundation.dll",
        "stdlib/encodings/__init__.pyc",
        "stdlib/encodings/aliases.pyc",
        "stdlib/encodings/utf_8.pyc",
    })
else:
    RUNTIME_REQUIRED_NATIVE_FILES = frozenset({
        "Infernux/lib/_Infernux.so",
        *(f"Infernux/lib/{name}" for name in player_native_library_filenames()),
    })
    RUNTIME_CONDITIONAL_NATIVE_FILES = frozenset({"Infernux/lib/libz.so"})
    BOOTSTRAP_REQUIRED_ARCHIVE_FILES = frozenset({
        BOOTSTRAP_NATIVE_MANIFEST_FILENAME,
        "libInfernuxFoundation.so",
        "stdlib/encodings/__init__.pyc",
        "stdlib/encodings/aliases.pyc",
        "stdlib/encodings/utf_8.pyc",
    })
BOOTSTRAP_REQUIRED_ROOT_FILES = frozenset(
    {}
)
EDITOR_I18N_PREFIX = "Infernux/engine/locales/"

PLAYER_FORBIDDEN_RUNTIME_MODULES = forbidden_player_service_modules() | frozenset(
    {
        "Infernux/engine/_play_mode_serialization.pyc",
        "Infernux/engine/deferred_task.pyc",
        "Infernux/engine/scene_document_transaction.pyc",
        "Infernux/engine/scene_manager.pyc",
    }
)
PLAYER_FORBIDDEN_RUNTIME_PREFIXES = frozenset(
    {
        "Infernux/engine/interaction/",
        "Infernux/engine/undo/",
        "Infernux/gizmos/",
    }
)

AUTHOR_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".pyx",
        ".particle.py",
        ".glsl",
        ".vert",
        ".frag",
        ".comp",
        ".geom",
        ".tesc",
        ".tese",
        ".hlsl",
        ".shader",
        ".particlegraph",
        ".shadingmodel",
        ".lua",
        ".cpp",
        ".c",
        ".cc",
        ".h",
        ".hpp",
    }
)
# Runtime.inxrt contains the engine's own shader programs.  This is the only
# author-source exception: project Content remains subject to the source gate.
RUNTIME_BUILTIN_SHADER_SUFFIXES = frozenset(
    {".glsl", ".vert", ".frag", ".shadingmodel"}
)
RUNTIME_BUILTIN_SHADER_PREFIX = "Infernux/resources/shaders/"
# Authoring documents with these suffixes are forbidden as direct Player
# payloads. Cooked copies may retain the suffix only under the GUID-addressed
# Library/Artifacts/Document namespace.
RUNTIME_DOCUMENT_SUFFIXES = RUNTIME_AUTHORING_DOCUMENT_SUFFIXES
PLAYER_RUNTIME_PROJECT_SETTINGS = frozenset(
    {
        "ProjectSettings/BuildSettings.json",
        "ProjectSettings/InxPlugins.json",
        "ProjectSettings/PhysicsSettings.json",
        "ProjectSettings/TagLayerSettings.json",
    }
)
TEXT_SUFFIXES = AUTHOR_SOURCE_SUFFIXES | RUNTIME_DOCUMENT_SUFFIXES | frozenset(
    {".json", ".yaml", ".yml", ".txt"}
)
NATIVE_SUFFIXES = frozenset({".exe", ".dll", ".pyd", ".so", ".dylib"})
NATIVE_ARCHIVE_SUFFIXES = frozenset({".inxrt", ".inxpkg", ".inxmod", ".inxcat"})
ABSOLUTE_PATH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9+.-])[A-Za-z]:[\\/]|\\\\|/(?:Users|home|workspace|mnt|var|tmp)/)"
)
_PE_SIGNATURE = b"PE\0\0"
_PE_MACHINES = frozenset({0x014C, 0x8664, 0xAA64})
_IMAGE_FILE_EXECUTABLE_IMAGE = 0x0002
_IMAGE_FILE_DLL = 0x2000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: object) -> None:
    """Publish audit evidence only after the complete JSON is durable."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _portable(relative: str) -> str:
    return relative.replace("\\", "/")


def _data_root(package_root: Path, executable_stem: str | None = None) -> Path:
    candidates = sorted(
        path for path in package_root.glob("*_Data") if path.is_dir()
    )
    if len(candidates) != 1:
        raise RuntimeError(
            "Player layout is incomplete: expected exactly one <Game>_Data directory"
        )
    data_root = candidates[0]
    if executable_stem is not None:
        expected_name = f"{executable_stem}_Data"
        if data_root.name.casefold() != expected_name.casefold():
            raise RuntimeError(
                "Player layout is incomplete: the unique data directory must be "
                f"named {expected_name!r} to match native host {executable_stem!r}"
            )
    return data_root


def _player_executable_stem(package_root: Path) -> str | None:
    if sys.platform == "win32":
        candidates = sorted(
            path
            for path in package_root.iterdir()
            if path.is_file() and path.suffix.casefold() == ".exe"
        )
    else:
        candidates = sorted(
            path
            for path in package_root.iterdir()
            if path.is_file() and not path.suffix
        )
    if len(candidates) != 1:
        return None
    return candidates[0].stem if sys.platform == "win32" else candidates[0].name


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _contains_absolute_author_path(text: str) -> bool:
    return ABSOLUTE_PATH_RE.search(text) is not None


def _is_safe_native_entry_path(entry_name: str) -> bool:
    """Return whether a native package TOC path is safe to extract."""

    if not entry_name or "\x00" in entry_name:
        return False
    if entry_name.startswith(("/", "\\")):
        return False
    if re.match(r"^[A-Za-z]:[\\/]?", entry_name):
        return False
    parts = entry_name.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _is_builtin_runtime_shader_entry(relative_archive: str, entry_name: str) -> bool:
    """Return whether an entry is the explicitly controlled built-in shader path."""

    return (
        relative_archive.endswith("/Runtime.inxrt")
        and entry_name.startswith(RUNTIME_BUILTIN_SHADER_PREFIX)
        and Path(entry_name).suffix.casefold() in RUNTIME_BUILTIN_SHADER_SUFFIXES
    )


def _is_project_runtime_shader_entry(relative_archive: str, entry_name: str) -> bool:
    """Allow packed project GLSL consumed by the runtime shader linker."""

    normalized = entry_name.replace("\\", "/")
    return (
        relative_archive.endswith("/Content.inxpkg")
        and normalized.casefold().startswith("library/artifacts/blob/")
        and Path(normalized).suffix.casefold() in RUNTIME_BUILTIN_SHADER_SUFFIXES
    )


def _has_player_host_identity(executable_path: Path) -> bool:
    """Validate the executable's native image structure without string scans."""

    try:
        with executable_path.open("rb") as executable_file:
            prefix = executable_file.read(64)
            if prefix.startswith(b"MZ"):
                if len(prefix) < 64:
                    return False
                pe_offset = struct.unpack_from("<I", prefix, 0x3C)[0]
                if pe_offset < 64 or pe_offset > 16 * 1024 * 1024:
                    return False
                executable_file.seek(pe_offset)
                if executable_file.read(4) != _PE_SIGNATURE:
                    return False
                file_header = executable_file.read(20)
                if len(file_header) != 20:
                    return False
                machine, _sections, _timestamp, _symbols, _symbol_count, optional_size, characteristics = struct.unpack(
                    "<HHIIIHH", file_header
                )
                if machine not in _PE_MACHINES:
                    return False
                if not characteristics & _IMAGE_FILE_EXECUTABLE_IMAGE:
                    return False
                if characteristics & _IMAGE_FILE_DLL:
                    return False
                optional_header = executable_file.read(optional_size)
                if len(optional_header) < 2:
                    return False
                return struct.unpack_from("<H", optional_header, 0)[0] in {0x010B, 0x020B}

            # Keep the structural check useful for non-Windows package tests
            # and future native Player targets without accepting text files.
            return prefix.startswith(b"\x7fELF") or prefix[:4] in {
                b"\xfe\xed\xfa\xce",
                b"\xce\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf",
                b"\xcf\xfa\xed\xfe",
            }
    except OSError:
        return False


def _archive_entry_records(
    archive_path: Path,
    relative_archive: str,
    *,
    payload_candidates: defaultdict[
        int, list[tuple[str, Path | tuple[Path, str], str | None]]
    ],
    archive_entries: list[dict[str, object]],
    forbidden: list[str],
    author_sources: list[str],
    meta_files: list[str],
    absolute_paths: list[str],
    native_files: list[str],
    hidden_executables: list[str],
    authoring_tree_files: list[str],
    unknown_author_documents: list[str],
    unsafe_entry_paths: list[str],
) -> dict[str, object] | None:
    """Read one native package through the native bridge and index its TOC."""

    try:
        manifest = read_manifest(archive_path)
    except Exception as exc:  # native bridge errors are an audit failure
        forbidden.append(f"{relative_archive}: native InxPack validation failed ({exc})")
        return None

    records = manifest.get("files", [])
    if not isinstance(records, list):
        forbidden.append(f"{relative_archive}: native InxPack manifest has no file list")
        return None

    # ReadEntry validates the complete archive before returning one entry.
    # Repeating it for every shader/document turns a package audit into dozens
    # of complete archive scans. Extract once into an isolated temporary tree;
    # the native extractor performs the same integrity and path validation and
    # lets the text audit read each payload without rescanning the container.
    text_payloads: dict[str, bytes] = {}
    text_entry_names = [
        _portable(str(item.get("path", "")))
        for item in records
        if isinstance(item, dict)
        and Path(str(item.get("path", ""))).suffix.casefold() in TEXT_SUFFIXES
    ]
    if text_entry_names:
        try:
            with TemporaryDirectory(prefix="infernux-player-audit-") as temporary:
                extracted_root = Path(temporary)
                extract_pack(archive_path, extracted_root)
                for entry_name in text_entry_names:
                    if not _is_safe_native_entry_path(entry_name):
                        continue
                    extracted_path = extracted_root.joinpath(*entry_name.split("/"))
                    text_payloads[entry_name] = extracted_path.read_bytes()
        except Exception as exc:
            forbidden.append(
                f"{relative_archive}: native text payload extraction failed ({exc})"
            )

    seen_paths: set[str] = set()
    raw_bytes_total = 0
    stored_bytes_total = 0
    for item in records:
        if not isinstance(item, dict):
            forbidden.append(f"{relative_archive}: malformed native InxPack entry record")
            continue
        entry_name = _portable(str(item.get("path", "")))
        entry_relative = f"{relative_archive}::{entry_name}"
        if not _is_safe_native_entry_path(entry_name):
            unsafe_entry_paths.append(entry_relative)
            forbidden.append(f"{entry_relative}: unsafe native entry path")
            continue
        if not entry_name or entry_name in seen_paths:
            forbidden.append(f"{entry_relative}: empty or duplicate native entry path")
            continue
        seen_paths.add(entry_name)
        try:
            entry_bytes = int(item["raw_bytes"])
            stored_bytes = int(item["stored_bytes"])
        except (KeyError, TypeError, ValueError):
            forbidden.append(f"{entry_relative}: native entry size fields are invalid")
            continue
        if entry_bytes < 0 or stored_bytes < 0:
            forbidden.append(f"{entry_relative}: native entry size is negative")
            continue
        raw_bytes_total += entry_bytes
        stored_bytes_total += stored_bytes
        archive_entries.append({"path": entry_relative, "bytes": entry_bytes})
        payload_candidates[entry_bytes].append(
            (entry_relative, (archive_path, entry_name), None)
        )
        entry_suffix = Path(entry_name).suffix.casefold()

        payload = None
        if entry_suffix in TEXT_SUFFIXES:
            payload = text_payloads.get(entry_name)
            if payload is None:
                forbidden.append(
                    f"{entry_relative}: native text payload is unavailable after extraction"
                )
            if payload is not None:
                if len(payload) != entry_bytes:
                    forbidden.append(
                        f"{entry_relative}: raw payload size mismatch "
                        f"(manifest={entry_bytes}, actual={len(payload)})"
                    )
        if entry_suffix == ".meta":
            meta_files.append(entry_relative)
        if (
            entry_suffix in AUTHOR_SOURCE_SUFFIXES
            and not _is_builtin_runtime_shader_entry(relative_archive, entry_name)
            and not _is_project_runtime_shader_entry(relative_archive, entry_name)
        ):
            author_sources.append(entry_relative)
        if entry_suffix in NATIVE_SUFFIXES:
            native_files.append(entry_relative)
        if entry_suffix == ".exe":
            hidden_executables.append(entry_relative)
        if (
            entry_name.startswith("ProjectSettings/")
            and entry_name not in PLAYER_RUNTIME_PROJECT_SETTINGS
        ):
            unknown_author_documents.append(entry_relative)
        if entry_suffix in TEXT_SUFFIXES and payload is not None:
            try:
                text = payload.decode("utf-8", errors="replace")
            except Exception as exc:
                forbidden.append(f"{entry_relative}: native entry decode failed ({exc})")
            else:
                if _contains_absolute_author_path(text):
                    absolute_paths.append(entry_relative)

    try:
        if int(manifest.get("file_count", -1)) != len(records):
            forbidden.append(f"{relative_archive}: native file count does not match its TOC")
        if int(manifest.get("raw_bytes", -1)) != raw_bytes_total:
            forbidden.append(f"{relative_archive}: native raw byte total does not match its TOC")
        if int(manifest.get("stored_bytes", -1)) != stored_bytes_total:
            forbidden.append(f"{relative_archive}: native stored byte total does not match its TOC")
    except (TypeError, ValueError):
        forbidden.append(f"{relative_archive}: native aggregate size fields are invalid")
    return manifest


def _payload_category(qualified_path: str) -> str:
    archive_path, separator, entry_path = qualified_path.partition("::")
    path = entry_path if separator else archive_path
    suffix = Path(path).suffix.casefold()
    if suffix in NATIVE_SUFFIXES:
        return "native"
    if suffix in {".pyc", ".pyo"}:
        return "python"
    if separator and archive_path.endswith("/Content.inxpkg"):
        return "asset"
    if path.startswith("Infernux/resources/"):
        return "resource"
    return "runtime_support"


def _payload_category_report(
    archive_entries: list[dict[str, object]],
) -> dict[str, dict[str, int]]:
    report: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {"count": 0, "bytes": 0}
    )
    for entry in archive_entries:
        category = _payload_category(str(entry["path"]))
        report[category]["count"] += 1
        report[category]["bytes"] += int(entry["bytes"])
    return dict(sorted(report.items()))


def _is_logically_distinct_asset_payload(
    paths: list[str],
    data_relative: str,
) -> bool:
    """Return whether equal bytes belong to distinct compiled asset identities."""

    content_prefix = f"{data_relative}/Content.inxpkg::Library/Artifacts/"
    return bool(paths) and all(
        path.replace("\\", "/").startswith(content_prefix) for path in paths
    )


def audit_player_package(
    package_root: str | Path,
    *,
    write_manifest: bool = True,
) -> dict[str, object]:
    """Audit a final native host plus ``<Game>_Data`` Player directory."""

    root = Path(resolved_path(package_root))
    if not root.is_dir():
        raise RuntimeError(f"Player package directory not found: {root}")
    data_root = _data_root(root)
    executable_stem = _player_executable_stem(root)
    if executable_stem is None:
        executable_stem = data_root.name[: -len("_Data")]
    data_root = _data_root(root, executable_stem)
    data_relative = data_root.name
    expected = {
        f"{data_relative}/{MANIFEST_FILENAME}",
        f"{data_relative}/Bootstrap.inxrt",
        f"{data_relative}/Runtime.inxrt",
        f"{data_relative}/Content.inxpkg",
        f"{data_relative}/{ASSET_CATALOG_ARCHIVE_FILENAME}",
        f"{data_relative}/PackageIndex.inxmanifest",
    }
    optional = {f"{data_relative}/Modules/Parallel.inxmod"}

    # Audit the direct root surface before recursively inspecting payloads.
    # This makes unknown Nuitka output fail closed, including empty folders.
    expected_executable = (
        f"{executable_stem}.exe" if sys.platform == "win32" else executable_stem
    )
    root_surface: list[dict[str, str]] = []
    root_surface_gaps: list[str] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if child.is_dir() and child.name.casefold() == data_relative.casefold():
            root_surface.append(
                {
                    "path": child.name,
                    "category": "player_data_directory",
                    "owner": "Player bootstrap",
                    "reason": "Contains the private Runtime.inxrt and Content.inxpkg payloads",
                }
            )
            continue
        if child.is_file() and child.name == expected_executable:
            root_surface.append(
                {
                    "path": child.name,
                    "category": "player_executable",
                    "owner": "Player bootstrap",
                    "reason": "Single visible process entry point",
                }
            )
            continue
        native_policy = next(
            (
                policy
                for name, policy in BOOTSTRAP_NATIVE_ROOT_ALLOWLIST.items()
                if name.casefold() == child.name.casefold()
            ),
            None,
        )
        if child.is_file() and native_policy is not None:
            root_surface.append(
                {
                    "path": child.name,
                    "category": native_policy["category"],
                    "owner": native_policy["owner"],
                    "reason": native_policy["reason"],
                }
            )
            continue
        relative = child.relative_to(root).as_posix()
        root_surface_gaps.append(relative)
    present_root_files = {
        child.name.casefold() for child in root.iterdir() if child.is_file()
    }
    root_surface_gaps.extend(
        f"missing required bootstrap file: {name}"
        for name in sorted(BOOTSTRAP_REQUIRED_ROOT_FILES)
        if name.casefold() not in present_root_files
    )

    files: list[dict[str, object]] = []
    hashes: defaultdict[str, list[str]] = defaultdict(list)
    payload_candidates: defaultdict[
        int, list[tuple[str, Path | tuple[Path, str], str | None]]
    ] = defaultdict(list)
    forbidden: list[str] = []
    author_sources: list[str] = []
    meta_files: list[str] = []
    absolute_paths: list[str] = []
    native_files: list[str] = []
    hidden_executables: list[str] = []
    authoring_tree_files: list[str] = []
    unknown_author_documents: list[str] = []
    unsafe_entry_paths: list[str] = []
    editor_i18n_files: list[str] = []
    data_surface_gaps: list[str] = []
    archive_entries: list[dict[str, object]] = []
    archive_manifests: dict[str, dict[str, object]] = {}
    allowed_data_files = expected | optional

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(f"{data_relative}/") and relative not in allowed_data_files:
            data_surface_gaps.append(relative)
        if relative == f"{data_relative}/{MANIFEST_FILENAME}":
            continue
        if relative.casefold().startswith(EDITOR_I18N_PREFIX.casefold()):
            editor_i18n_files.append(relative)
            forbidden.append(f"{relative}: Editor i18n data is not a Player payload")
        size = path.stat().st_size
        suffix = path.suffix.casefold()
        payload_candidates[size].append((relative, path, None))
        files.append({"path": relative, "bytes": size})
        if suffix in NATIVE_SUFFIXES:
            native_files.append(relative)
        if suffix == ".meta":
            meta_files.append(relative)
        if suffix in AUTHOR_SOURCE_SUFFIXES:
            author_sources.append(relative)
        if suffix in {".pyc", ".pyo"}:
            # Compiled user scripts are allowed only inside Content.inxpkg;
            # a loose bytecode file is still redundant package payload.
            forbidden.append(f"{relative}: loose compiled script is not supported")
        if suffix in RUNTIME_DOCUMENT_SUFFIXES:
            forbidden.append(f"{relative}: loose runtime document is not supported")
        if relative == f"{data_relative}/Assets" or relative.startswith(
            f"{data_relative}/Assets/"
        ):
            authoring_tree_files.append(relative)
        if relative.startswith(f"{data_relative}/ProjectSettings/"):
            project_setting = relative[len(f"{data_relative}/") :]
            if project_setting not in PLAYER_RUNTIME_PROJECT_SETTINGS:
                unknown_author_documents.append(relative)
        expected_here = relative in expected or relative in optional
        if suffix in NATIVE_ARCHIVE_SUFFIXES:
            if not expected_here:
                forbidden.append(f"{relative}: unexpected native package path")
            else:
                archive_manifest = _archive_entry_records(
                    path,
                    relative,
                    payload_candidates=payload_candidates,
                    archive_entries=archive_entries,
                    forbidden=forbidden,
                    author_sources=author_sources,
                    meta_files=meta_files,
                    absolute_paths=absolute_paths,
                    native_files=native_files,
                    hidden_executables=hidden_executables,
                    authoring_tree_files=authoring_tree_files,
                    unknown_author_documents=unknown_author_documents,
                    unsafe_entry_paths=unsafe_entry_paths,
                )
                if archive_manifest is not None:
                    archive_manifests[relative] = archive_manifest
        elif suffix in TEXT_SUFFIXES:
            if _contains_absolute_author_path(_read_text(path)):
                absolute_paths.append(relative)

    # Duplicate detection needs content hashes only when at least two payloads
    # have the same byte count. Unique-sized files cannot be byte-identical, so
    # hashing every Player file would add a complete second package scan without
    # strengthening the audit.
    for candidates in payload_candidates.values():
        if len(candidates) < 2:
            continue
        for relative, payload_source, known_digest in candidates:
            digest = known_digest
            if digest is None:
                if isinstance(payload_source, tuple):
                    digest = hashlib.sha256(
                        read_entry(payload_source[0], payload_source[1])
                    ).hexdigest()
                else:
                    digest = _sha256(payload_source)
            hashes[digest].append(relative)

    for archive_entry in archive_entries:
        archive_path, entry_path = str(archive_entry["path"]).split("::", 1)
        if entry_path.casefold().startswith(EDITOR_I18N_PREFIX.casefold()):
            editor_i18n_files.append(f"{archive_path}::{entry_path}")
            forbidden.append(
                f"{archive_path}::{entry_path}: Editor i18n data is not a Player payload"
            )

    missing = sorted(expected - {str(item["path"]) for item in files})
    # Player.inxmanifest is generated by this function, so it is not a
    # prerequisite on the first audit pass.
    missing_without_manifest = [
        path for path in missing if not path.endswith(f"/{MANIFEST_FILENAME}")
    ]
    if missing_without_manifest:
        forbidden.extend(f"{path}: required Player artifact is missing" for path in missing_without_manifest)

    executables = [
        str(item["path"])
        for item in files
        if str(item["path"]) == expected_executable
    ]
    if len(executables) != 1:
        forbidden.append("Player package must contain exactly one visible native host")
    elif "/" in executables[0]:
        forbidden.append("Player executable must be at the package root")

    foundation_name = (
        "InfernuxFoundation.dll"
        if sys.platform == "win32"
        else "libInfernuxFoundation.so"
    )
    expected_foundation_archive_paths = {
        (
            f"{data_relative}/Bootstrap.inxrt::"
            f"{foundation_name if sys.platform.startswith('linux') else f'Infernux/lib/{foundation_name}'}"
        ).casefold(),
        (
            f"{data_relative}/Runtime.inxrt::"
            f"Infernux/lib/{foundation_name}"
        ).casefold(),
    }

    def _is_required_bootstrap_duplicate(paths: list[str]) -> bool:
        # Foundation is intentionally present in both extraction phases: the
        # bootstrap extension needs it before Runtime.inxrt can be mounted,
        # and the full runtime needs it in its own independent warm cache.
        # Keep this exception exact so no other duplicate can hide here.
        return (
            len(paths) == 2
            and {path.replace("\\", "/").casefold() for path in paths}
            == expected_foundation_archive_paths
        )

    def _is_linux_soname_alias_group(paths: list[str]) -> bool:
        if not sys.platform.startswith("linux") or len(paths) < 2:
            return False
        archives = {path.partition("::")[0] for path in paths}
        if len(archives) != 1 or not all("::" in path for path in paths):
            return False
        bases: set[str] = set()
        for path in paths:
            name = Path(path.split("::", 1)[1]).name
            match = re.fullmatch(r"(?P<base>.+\.so)(?:\..+)?", name)
            if match is None:
                return False
            bases.add(match.group("base").casefold())
        return len(bases) == 1

    duplicate_asset_payloads = sorted(
        sorted(paths)
        for paths in hashes.values()
        if len(paths) > 1
        and _is_logically_distinct_asset_payload(paths, data_relative)
    )

    duplicate_payloads = sorted(
        sorted(paths)
        for paths in hashes.values()
        if len(paths) > 1
        and not _is_required_bootstrap_duplicate(paths)
        and not _is_linux_soname_alias_group(paths)
        and not _is_logically_distinct_asset_payload(paths, data_relative)
    )
    duplicate_native = sorted(
        group
        for group in duplicate_payloads
        if any(Path(path.split("::", 1)[-1]).suffix.casefold() in NATIVE_SUFFIXES for path in group)
    )
    if hidden_executables:
        forbidden.extend(
            f"{path}: executable found inside native package TOC"
            for path in hidden_executables
        )
    if authoring_tree_files:
        forbidden.extend(
            f"{path}: raw Assets authoring tree is not a Player payload"
            for path in authoring_tree_files
        )
    if unknown_author_documents:
        forbidden.extend(
            f"{path}: unknown authoring document path"
            for path in unknown_author_documents
        )

    runtime_prefix = f"{data_relative}/Runtime.inxrt::"
    runtime_entry_paths = {
        str(entry["path"])[len(runtime_prefix) :]
        for entry in archive_entries
        if str(entry["path"]).startswith(runtime_prefix)
    }
    runtime_entries_by_casefold = {path.casefold(): path for path in runtime_entry_paths}
    build_manifest_document: dict[str, object] = {}
    runtime_contract_gaps: list[str] = []
    try:
        build_manifest_document = json.loads(
            read_entry(
                data_root / ASSET_CATALOG_ARCHIVE_FILENAME,
                BUILD_MANIFEST_ENTRY_PATH,
            ).decode("utf-8")
        )
        if not isinstance(build_manifest_document, dict):
            raise RuntimeError("BuildManifest root is not an object")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
        runtime_contract_gaps.append(f"sealed BuildManifest is unreadable: {exc}")
        build_manifest_document = {}

    declared_contract = build_manifest_document.get("runtime_contract")
    runtime_flavor = RuntimeFlavor.PLAYER_RELEASE
    runtime_features = RuntimeFeatureSet()
    if not isinstance(declared_contract, dict):
        runtime_contract_gaps.append(
            "BuildManifest has no authoritative runtime_contract"
        )
    else:
        product = declared_contract.get("product")
        try:
            runtime_flavor = RuntimeFlavor(
                str(product.get("flavor", "")) if isinstance(product, dict) else ""
            )
            if not runtime_flavor.is_player:
                raise ValueError("EditorDevelopment is not a Player product")
            runtime_features = RuntimeFeatureSet.from_manifest(
                declared_contract.get("features")
            )
            expected_contract = player_runtime_contract_sections(
                runtime_flavor,
                runtime_features,
            )
            if declared_contract != expected_contract:
                raise RuntimeError(
                    "runtime_contract differs from the authoritative service graph"
                )
            expected_debug = runtime_flavor is RuntimeFlavor.PLAYER_DEBUG
            if bool(build_manifest_document.get("debug_build", False)) != expected_debug:
                raise RuntimeError(
                    "debug_build disagrees with the declared RuntimeFlavor"
                )
        except (RuntimeError, TypeError, ValueError) as exc:
            runtime_contract_gaps.append(str(exc))

    runtime_contract = player_runtime_contract_sections(
        runtime_flavor,
        runtime_features,
    )
    runtime_service_graph = runtime_contract["services"]["graph"]
    runtime_editor_services = sorted(
        path
        for path in runtime_entry_paths
        if path in PLAYER_FORBIDDEN_RUNTIME_MODULES
        or any(path.startswith(prefix) for prefix in PLAYER_FORBIDDEN_RUNTIME_PREFIXES)
    )
    forbidden.extend(
        f"{data_relative}/Runtime.inxrt::{path}: editor service is not a Player payload"
        for path in runtime_editor_services
    )
    runtime_required_native_files = set(RUNTIME_REQUIRED_NATIVE_FILES)
    runtime_required_native_files.update(
        conditional
        for conditional in RUNTIME_CONDITIONAL_NATIVE_FILES
        if conditional.casefold() in runtime_entries_by_casefold
    )
    runtime_payload_gap = [
        f"missing required runtime native file: {required}"
        for required in sorted(runtime_required_native_files)
        if required.casefold() not in runtime_entries_by_casefold
    ]
    if any(path.casefold().startswith("numpy/") for path in runtime_entry_paths):
        for required in ("numpy/__init__.pyc", "numpy/_core/__init__.pyc"):
            if required.casefold() not in runtime_entries_by_casefold:
                runtime_payload_gap.append(
                    f"incomplete NumPy runtime package: {required} is missing"
                )
    runtime_payload_gap.extend(
        f"missing required Player service module: {record['module']}"
        for record in runtime_service_graph
        if (
            (
                str(record["module"]).startswith("Modules/")
                and not (data_root / str(record["module"])).is_file()
            )
            or (
                not str(record["module"]).startswith("Modules/")
                and str(record["module"]) not in runtime_entry_paths
            )
        )
    )
    parallel_present = (data_root / "Modules" / "Parallel.inxmod").is_file()
    if parallel_present != runtime_features.parallel:
        runtime_payload_gap.append(
            "Parallel.inxmod presence disagrees with RuntimeManifest features"
        )
    runtime_payload_gap.extend(runtime_contract_gaps)
    bootstrap_prefix = f"{data_relative}/Bootstrap.inxrt::"
    bootstrap_entry_paths = {
        str(entry["path"])[len(bootstrap_prefix) :]
        for entry in archive_entries
        if str(entry["path"]).startswith(bootstrap_prefix)
    }
    bootstrap_payload_gap = [
        f"missing required bootstrap archive file: {required}"
        for required in sorted(BOOTSTRAP_REQUIRED_ARCHIVE_FILES)
        if required.casefold() not in {path.casefold() for path in bootstrap_entry_paths}
    ]
    bootstrap_names = {Path(path).name for path in bootstrap_entry_paths}
    if BOOTSTRAP_NATIVE_MANIFEST_FILENAME in bootstrap_entry_paths:
        try:
            bootstrap_native_document = json.loads(
                read_entry(
                    data_root / "Bootstrap.inxrt",
                    BOOTSTRAP_NATIVE_MANIFEST_FILENAME,
                ).decode("utf-8")
            )
        except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            bootstrap_payload_gap.append(
                f"bootstrap native manifest is unreadable: {exc}"
            )
        else:
            if (
                bootstrap_native_document.get("$schema")
                != BOOTSTRAP_NATIVE_MANIFEST_SCHEMA
            ):
                bootstrap_payload_gap.append(
                    "bootstrap native manifest has an unsupported schema"
                )
            bootstrap_native_files = bootstrap_native_document.get("files")
            if not isinstance(bootstrap_native_files, list):
                bootstrap_payload_gap.append(
                    "bootstrap native manifest contains no file list"
                )
            else:
                bootstrap_names_casefold = {
                    name.casefold() for name in bootstrap_names
                }
                bootstrap_payload_gap.extend(
                    f"missing declared bootstrap native file: {name}"
                    for name in bootstrap_native_files
                    if not isinstance(name, str)
                    or not name
                    or Path(name).name != name
                    or name.casefold() not in bootstrap_names_casefold
                )
    if sys.platform == "win32":
        if not any(is_windows_libffi_dll(name) for name in bootstrap_names):
            bootstrap_payload_gap.append(
                "missing required bootstrap archive file: Windows libffi DLL"
            )
    elif sys.platform.startswith("linux"):
        linux_bootstrap_requirements = {
            f"CPython {PYTHON_VERSION} shared library": lambda name: name.startswith(
                LINUX_PYTHON_SHARED_PREFIX
            ),
            "_InfernuxBootstrap module": lambda name: name.startswith("_InfernuxBootstrap")
            and name.endswith(".so"),
        }
        bootstrap_payload_gap.extend(
            f"missing required bootstrap archive file: {label}"
            for label, predicate in linux_bootstrap_requirements.items()
            if not any(predicate(name) for name in bootstrap_names)
        )
    if not any(
        Path(path).name.startswith("_InfernuxPlayer")
        and Path(path).name.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES))
        for path in bootstrap_entry_paths
    ):
        bootstrap_payload_gap.append(
            "missing required bootstrap archive file: ABI-named _InfernuxPlayer extension module"
        )
    runtime_bootstrap_leaks = sorted(
        path
        for path in runtime_entry_paths
        if Path(path).name.casefold().startswith("_infernuxbootstrap")
    )
    runtime_payload_gap.extend(
        f"bootstrap module must not be stored in Runtime.inxrt: {path}"
        for path in runtime_bootstrap_leaks
    )

    player_host_gap: list[str] = []
    if len(executables) == 1 and "/" not in executables[0]:
        executable_path = root / executables[0]
        if not _has_player_host_identity(executable_path):
            player_host_gap.append(
                "root executable has no verifiable Infernux PlayerHost identity"
            )
    else:
        player_host_gap.append("root executable is unavailable for PlayerHost identity validation")

    library_artifact_gap: list[str] = []
    catalog_path = data_root / ASSET_CATALOG_ARCHIVE_FILENAME
    catalog: dict[str, object] = {}
    catalog_artifacts: list[dict[str, object]] = []
    catalog_packages: list[dict[str, object]] = []
    if not catalog_path.is_file():
        library_artifact_gap.append(f"{ASSET_CATALOG_ARCHIVE_FILENAME} is missing")
    else:
        try:
            catalog = json.loads(
                read_entry(catalog_path, ASSET_CATALOG_ENTRY_PATH).decode("utf-8")
            )
            sealed_build_manifest = json.loads(
                read_entry(catalog_path, BUILD_MANIFEST_ENTRY_PATH).decode("utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError):
            catalog = {}
            sealed_build_manifest = None
        if sealed_build_manifest != build_manifest_document:
            library_artifact_gap.append(
                f"{ASSET_CATALOG_ARCHIVE_FILENAME} build manifest is unstable"
            )
        if (
            catalog.get("$schema") != CATALOG_SCHEMA
            or set(catalog) != {"$schema", "player_host", "packages", "artifacts"}
        ):
            library_artifact_gap.append(
                f"{ASSET_CATALOG_ARCHIVE_FILENAME} has no current catalog schema"
            )
        raw_packages = catalog.get("packages", [])
        if not isinstance(raw_packages, list):
            library_artifact_gap.append(
                f"{ASSET_CATALOG_ARCHIVE_FILENAME} has no package entry list"
            )
        else:
            catalog_packages = [item for item in raw_packages if isinstance(item, dict)]
            if len(catalog_packages) != len(raw_packages):
                library_artifact_gap.append(
                    f"{ASSET_CATALOG_ARCHIVE_FILENAME} contains malformed package entries"
                )
        raw_artifacts = catalog.get("artifacts", [])
        if not isinstance(raw_artifacts, list):
            library_artifact_gap.append(
                f"{ASSET_CATALOG_ARCHIVE_FILENAME} has no artifact entry list"
            )
        else:
            catalog_artifacts = [item for item in raw_artifacts if isinstance(item, dict)]
            if len(catalog_artifacts) != len(raw_artifacts):
                library_artifact_gap.append(
                    f"{ASSET_CATALOG_ARCHIVE_FILENAME} contains malformed artifact entries"
                )

    # Bootstrap.inxrt is a host startup closure, not a game runtime asset
    # package. It is fully audited above, but deliberately excluded from the
    # RuntimeAssetCatalog contract shared with GameBuilder.
    catalog_package_paths = {
        f"{data_relative}/Runtime.inxrt",
        f"{data_relative}/Content.inxpkg",
        f"{data_relative}/Modules/Parallel.inxmod",
    }
    actual_artifacts: dict[str, dict[str, object]] = {}
    for archive_entry in archive_entries:
        archive_name, entry_name = str(archive_entry["path"]).split("::", 1)
        if archive_name not in catalog_package_paths:
            continue
        actual_id = runtime_artifact_id(archive_name, entry_name)
        actual_artifacts[actual_id] = {
            "runtime_artifact_id": actual_id,
            "logical_type": logical_type_for_path(entry_name),
            "payload_kind": payload_kind_for(logical_type_for_path(entry_name)),
            "package": archive_name,
            "runtime_path": entry_name,
            "content_bytes": int(archive_entry["bytes"]),
        }
    actual_direct_assets = sorted(
        artifact_id
        for artifact_id, artifact in actual_artifacts.items()
        if artifact.get("payload_kind") in RUNTIME_DOCUMENT_PAYLOAD_KINDS
    )
    if actual_direct_assets:
        library_artifact_gap.extend(
            "native Player package contains a direct or serialized runtime "
            f"payload: {artifact_id}"
            for artifact_id in actual_direct_assets
        )

    actual_packages = {
        path: {
            "path": path,
            "archive_bytes": manifest.get("archive_bytes"),
            "file_count": manifest.get("file_count"),
            "raw_bytes": manifest.get("raw_bytes"),
            "stored_bytes": manifest.get("stored_bytes"),
            "codec": manifest.get("codec"),
        }
        for path, manifest in archive_manifests.items()
        if path in catalog_package_paths
    }
    if not any(
        artifact.get("runtime_path") == "Library/RuntimeTypeRegistry.json"
        for artifact in actual_artifacts.values()
    ):
        library_artifact_gap.append("Library/RuntimeTypeRegistry.json is missing")
    catalog_packages_by_path = {
        str(package.get("path")): package
        for package in catalog_packages
        if isinstance(package.get("path"), str) and package.get("path")
    }
    if len(catalog_packages_by_path) != len(catalog_packages):
        library_artifact_gap.append("catalog contains duplicate package paths")
    if set(catalog_packages_by_path) != set(actual_packages):
        library_artifact_gap.append("catalog package entries do not match native package set")
    for package_path, expected_package in actual_packages.items():
        actual_package = catalog_packages_by_path.get(package_path)
        if actual_package is None:
            continue
        if set(actual_package) != {
            "path", "archive_bytes", "file_count", "raw_bytes", "stored_bytes", "codec"
        }:
            library_artifact_gap.append(
                f"catalog package does not match the current schema: {package_path}"
            )
        for field in (
            "archive_bytes",
            "file_count",
            "raw_bytes",
            "stored_bytes",
            "codec",
        ):
            if actual_package.get(field) != expected_package[field]:
                library_artifact_gap.append(
                    f"catalog package field mismatch for {package_path}: {field}"
                )

    catalog_by_id: dict[str, dict[str, object]] = {}
    for artifact in catalog_artifacts:
        artifact_id = artifact.get("runtime_artifact_id")
        package = artifact.get("package")
        runtime_path = artifact.get("runtime_path")
        if not all(isinstance(value, str) and value for value in (artifact_id, package, runtime_path)):
            library_artifact_gap.append("catalog artifact is missing stable identity fields")
            continue
        expected_id = runtime_artifact_id(package, runtime_path)
        if artifact_id != expected_id:
            library_artifact_gap.append(
                f"catalog artifact id mismatch for {package}::{runtime_path}"
            )
        if artifact_id in catalog_by_id:
            library_artifact_gap.append(f"duplicate catalog artifact id: {artifact_id}")
        catalog_by_id[artifact_id] = artifact
        allowed_artifact_fields = {
            "runtime_artifact_id", "logical_type", "payload_kind", "package",
            "runtime_path", "content_bytes", "dependencies", "unresolved_dependencies",
        }
        if frozenset(artifact) not in {
            frozenset(allowed_artifact_fields),
            frozenset(allowed_artifact_fields | {"source_asset", "asset_guid"}),
        }:
            library_artifact_gap.append(
                f"catalog artifact does not match the current schema: {artifact_id}"
            )
        expected = actual_artifacts.get(artifact_id)
        if expected is None:
            library_artifact_gap.append(f"catalog artifact is not present in package TOC: {artifact_id}")
            continue
        for field in ("logical_type", "payload_kind", "package", "runtime_path", "content_bytes"):
            if artifact.get(field) != expected[field]:
                library_artifact_gap.append(
                    f"catalog artifact field mismatch for {artifact_id}: {field}"
                )
    missing_catalog_ids = sorted(set(actual_artifacts) - set(catalog_by_id))
    if missing_catalog_ids:
        library_artifact_gap.append(
            f"catalog is missing {len(missing_catalog_ids)} package artifact entries"
        )
    catalog_ids = set(catalog_by_id)
    for artifact_id, artifact in catalog_by_id.items():
        dependencies = artifact.get("dependencies", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) or item not in catalog_ids for item in dependencies
        ):
            library_artifact_gap.append(f"catalog dependencies are invalid for {artifact_id}")
        unresolved = artifact.get("unresolved_dependencies", [])
        if not isinstance(unresolved, list) or unresolved:
            library_artifact_gap.append(
                f"catalog has unresolved runtime dependencies for {artifact_id}"
            )

    artifact_ids_by_guid: dict[str, set[str]] = defaultdict(set)
    for artifact_id, artifact in catalog_by_id.items():
        asset_guid = artifact.get("asset_guid")
        if asset_guid is None:
            continue
        if not isinstance(asset_guid, str) or not asset_guid:
            library_artifact_gap.append(
                f"catalog artifact has an invalid asset GUID: {artifact_id}"
            )
            continue
        artifact_ids_by_guid[asset_guid].add(artifact_id)

    runtime_asset_records: dict[str, object] = {}
    try:
        runtime_asset_records = json.loads(
            read_entry(
                data_root / "Content.inxpkg",
                "Library/RuntimeAssetRecords.json",
            ).decode("utf-8")
        )
    except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
        library_artifact_gap.append(
            "Content.inxpkg has no readable Library/RuntimeAssetRecords.json"
        )
    raw_asset_records = runtime_asset_records.get("entries", [])
    if (
        runtime_asset_records.get("$schema") != "infernux.runtime_asset_records"
        or set(runtime_asset_records) != {"$schema", "entries"}
        or not isinstance(raw_asset_records, list)
    ):
        library_artifact_gap.append("runtime asset records use an unsupported schema")
        raw_asset_records = []
    seen_asset_guids: set[str] = set()
    for record in raw_asset_records:
        if not isinstance(record, dict):
            library_artifact_gap.append("runtime asset records contain a malformed entry")
            continue
        guid = record.get("guid")
        primary_id = record.get("primary_runtime_artifact_id")
        runtime_ids = record.get("runtime_artifact_ids")
        dependencies = record.get("dependencies", [])
        record_reason = record.get("runtime_artifact_reason", "")
        if (
            not isinstance(guid, str)
            or not guid
            or guid in seen_asset_guids
            or not isinstance(primary_id, str)
            or not isinstance(runtime_ids, list)
            or primary_id not in runtime_ids
        ):
            library_artifact_gap.append("runtime asset record has incomplete identity")
            continue
        if any(not isinstance(value, str) for value in runtime_ids):
            library_artifact_gap.append(
                f"runtime asset record has invalid artifact IDs: {guid}"
            )
            continue
        if set(runtime_ids) != artifact_ids_by_guid.get(guid, set()):
            library_artifact_gap.append(
                f"runtime asset record disagrees with catalog artifacts: {guid}"
            )
        if not isinstance(dependencies, list) or any(
            dependency not in catalog_ids for dependency in dependencies
        ):
            library_artifact_gap.append(
                f"runtime asset record has invalid dependencies: {guid}"
            )
        catalog_reasons = {
            str(catalog_by_id[artifact_id].get("runtime_artifact_reason", ""))
            for artifact_id in runtime_ids
            if artifact_id in catalog_by_id
            and catalog_by_id[artifact_id].get("payload_kind")
            in {"serialized_runtime_document", "direct_runtime_asset"}
        }
        catalog_reasons.discard("")
        if catalog_reasons:
            if len(catalog_reasons) != 1 or record_reason not in catalog_reasons:
                library_artifact_gap.append(
                    f"runtime asset record reason disagrees with catalog: {guid}"
                )
        elif record_reason:
            library_artifact_gap.append(
                f"compiled runtime asset record has a pass-through reason: {guid}"
            )
        seen_asset_guids.add(guid)
    missing_asset_records = sorted(set(artifact_ids_by_guid) - seen_asset_guids)
    if missing_asset_records:
        library_artifact_gap.append(
            f"runtime asset records omit {len(missing_asset_records)} catalog GUIDs"
        )

    source_replacement_gaps: list[str] = []
    compiled_asset_types = {
        "animation_clip_2d_artifact",
        "animation_clip_3d_artifact",
        "animation_clip_artifact",
        "animation_fsm_artifact",
        "animation_timeline_artifact",
        "audio_artifact",
        "texture_artifact",
        "mesh_artifact",
        "skinned_mesh_artifact",
        "particle_graph_artifact",
        "material_artifact",
        "prefab_artifact",
        "render_effect_artifact",
        "render_effect_group_artifact",
        "scene_artifact",
        "timeline_artifact",
        "timeline_fsm_artifact",
        "project_runtime_document_artifact",
        "project_runtime_blob_artifact",
    }
    for artifact_id, artifact in catalog_by_id.items():
        if artifact.get("logical_type") not in compiled_asset_types:
            continue
        if artifact.get("payload_kind") != "compiled_artifact":
            source_replacement_gaps.append(
                f"compiled artifact has an invalid payload kind: {artifact_id}"
            )
            continue
        source_asset = artifact.get("source_asset")
        if not isinstance(source_asset, dict):
            source_replacement_gaps.append(
                f"compiled artifact has no source AssetIndex binding: {artifact_id}"
            )
            continue
        required = (
            "source_guid",
            "source_path",
            "source_fingerprint",
            "artifact_source_hash",
            "artifact_path",
        )
        if any(not isinstance(source_asset.get(field), (str, dict)) for field in required):
            source_replacement_gaps.append(
                f"compiled artifact source binding is incomplete: {artifact_id}"
            )
        if any(
            not isinstance(source_asset.get(field), str) or not source_asset[field]
            for field in ("source_guid", "source_path", "artifact_source_hash", "artifact_path")
        ):
            source_replacement_gaps.append(
                f"compiled artifact source binding has empty identity: {artifact_id}"
            )
        fingerprint = source_asset.get("source_fingerprint")
        if not isinstance(fingerprint, dict) or not all(
            isinstance(fingerprint.get(field), int)
            for field in ("size", "modified_ns")
        ):
            source_replacement_gaps.append(
                f"compiled artifact source fingerprint is invalid: {artifact_id}"
            )
        source_path = source_asset.get("source_path")
        if isinstance(source_path, str) and any(
            str(entry.get("runtime_path", "")) == source_path
            for entry in actual_artifacts.values()
        ):
            source_replacement_gaps.append(
                f"compiled artifact source is still present in Player payload: {source_path}"
            )

    residual_direct_assets = sorted(
        {
            artifact_id
            for artifact_id, artifact in catalog_by_id.items()
            if artifact.get("payload_kind")
            in {"serialized_runtime_document", "direct_runtime_asset"}
        }
    )
    direct_payload_reason_gaps: list[str] = []
    if residual_direct_assets:
        direct_payload_reason_gaps.extend(
            "direct or serialized runtime payload is forbidden in the Player "
            f"package: {artifact_id}"
            for artifact_id in residual_direct_assets
        )
    for artifact_id in residual_direct_assets:
        artifact = catalog_by_id[artifact_id]
        reason = artifact.get("runtime_artifact_reason")
        expected_reason = runtime_artifact_reason_for(
            str(artifact.get("logical_type", ""))
        )
        if reason not in RUNTIME_ARTIFACT_REASONS or reason != expected_reason:
            direct_payload_reason_gaps.append(
                f"direct runtime payload has no auditable reason: {artifact_id}"
            )
        if not isinstance(artifact.get("asset_guid"), str) or not artifact.get(
            "asset_guid"
        ):
            direct_payload_reason_gaps.append(
                f"direct runtime payload has no AssetIndex GUID: {artifact_id}"
            )

    layout = "single_executable_native_packages"
    runtime_policy = runtime_contract["runtime_policy"]
    reachability_gaps = []
    if missing_without_manifest:
        reachability_gaps.append("required native runtime/content artifact is missing")
    reachability_gaps.extend(player_host_gap)
    reachability_gaps.extend(library_artifact_gap)
    reachability_gaps.extend(source_replacement_gaps)
    reachability_gaps.extend(direct_payload_reason_gaps)
    reachability_gaps.extend(runtime_payload_gap)
    reachability_gaps.extend(bootstrap_payload_gap)
    single_entry_point = len(executables) == 1 and "/" not in executables[0]
    audit_passed = not (
        forbidden
        or author_sources
        or meta_files
        or absolute_paths
        or duplicate_payloads
        or hidden_executables
        or authoring_tree_files
        or unknown_author_documents
        or unsafe_entry_paths
        or root_surface_gaps
        or data_surface_gaps
        or editor_i18n_files
        or not single_entry_point
        or player_host_gap
        or library_artifact_gap
        or source_replacement_gaps
        or direct_payload_reason_gaps
        or runtime_payload_gap
        or bootstrap_payload_gap
    )

    result = {
        "$schema": MANIFEST_SCHEMA,
        "product": {
            "layout": layout,
            **runtime_contract["product"],
            "entry_points": executables,
            "single_entry_point": len(executables) == 1 and "/" not in executables[0],
        },
        "features": runtime_contract["features"],
        "bootstrap_surface": {
            "policy": "phase_a_strict_root_surface",
            "allowed": root_surface,
            "gaps": sorted(set(root_surface_gaps)),
            "native_allowlist": [
                {"path": name, **policy}
                for name, policy in sorted(BOOTSTRAP_NATIVE_ROOT_ALLOWLIST.items())
            ],
        },
        "runtime_native_surface": {
            "owner": "Infernux Runtime",
            "reason": "Loaded package-qualified only after Runtime.inxrt extraction and search-path activation",
            "required": sorted(runtime_required_native_files),
            "conditional": sorted(RUNTIME_CONDITIONAL_NATIVE_FILES),
            "gaps": sorted(set(runtime_payload_gap)),
        },
        "services": runtime_contract["services"],
        "runtime_policy": runtime_policy,
        "reachability": {
            "build_manifest": (
                f"{data_relative}/{ASSET_CATALOG_ARCHIVE_FILENAME}::"
                f"{BUILD_MANIFEST_ENTRY_PATH}"
            ),
            "runtime_artifacts": sorted(archive_manifests),
            "content_entries": sorted(
                entry["path"] for entry in archive_entries
                if str(entry["path"]).startswith(
                    f"{data_relative}/Content.inxpkg::"
                )
            ),
            "gaps": reachability_gaps,
            "residual_direct_assets": residual_direct_assets,
            "source_replacement_gaps": sorted(source_replacement_gaps),
            "direct_payload_reason_gaps": sorted(direct_payload_reason_gaps),
        },
        "audit": {
            "passed": audit_passed,
            "forbidden_files": sorted(set(forbidden)),
            "author_source_files": sorted(set(author_sources)),
            "meta_files": sorted(set(meta_files)),
            "absolute_author_paths": sorted(set(absolute_paths)),
            "duplicate_native_payloads": duplicate_native,
            "duplicate_payload_groups": duplicate_payloads,
            "duplicate_asset_payload_groups": duplicate_asset_payloads,
            "hidden_executables": sorted(set(hidden_executables)),
            "authoring_tree_files": sorted(set(authoring_tree_files)),
            "unknown_author_documents": sorted(set(unknown_author_documents)),
            "unsafe_entry_paths": sorted(set(unsafe_entry_paths)),
            "editor_i18n_files": sorted(set(editor_i18n_files)),
            "bootstrap_surface_gaps": sorted(set(root_surface_gaps)),
            "data_surface_gaps": sorted(set(data_surface_gaps)),
            "runtime_payload_gaps": sorted(set(runtime_payload_gap)),
            "bootstrap_payload_gaps": sorted(set(bootstrap_payload_gap)),
            "source_replacement_gaps": sorted(set(source_replacement_gaps)),
            "direct_payload_reason_gaps": sorted(set(direct_payload_reason_gaps)),
            "player_host_gap": sorted(set(player_host_gap)),
            "library_artifact_gap": sorted(set(library_artifact_gap)),
            "layout_gaps": sorted(set(reachability_gaps)),
            "residual_direct_assets": residual_direct_assets,
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
            "payload_categories": _payload_category_report(archive_entries),
            "archives": archive_entries,
        },
    }
    if result["audit"]["passed"]:
        try:
            RuntimeProductManifest.from_document(result)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Generated Player runtime manifest is invalid: {exc}"
            ) from exc
    if not result["audit"]["passed"]:
        raise RuntimeError(
            "Player package audit failed: "
            + json.dumps(result["audit"], ensure_ascii=False, sort_keys=True)
        )
    if write_manifest:
        manifest_path = data_root / MANIFEST_FILENAME
        # The detailed audit enumerates package entries, including author-side
        # logical aliases.  It is build evidence, not runtime content.  Ship
        # only the small product contract required by PlayerBootstrap.
        _write_json_atomic(
            manifest_path,
            {
                key: result[key]
                for key in (
                    "$schema",
                    "product",
                    "features",
                    "services",
                    "runtime_policy",
                )
            },
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="final Player output directory")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    audit_player_package(args.root, write_manifest=not args.no_write)
    print(f"Player package audit passed: {resolved_path(args.root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
