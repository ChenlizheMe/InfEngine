"""Deterministic InxPackage authoring and inspection.

An ``.inxpkg`` stores a package-source tree rather than arbitrary project
destinations. Installation routing is derived from the top-level package
layout and every payload file carries a stable GUID. Paths are mutable
locations; GUIDs are the durable identity.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from Infernux.engine.path_utils import (
    is_path_within,
    portable_path,
    relative_path,
    resolved_path,
)
from Infernux.engine.player_package_native import read_entry, read_manifest, write_pack

from .content import (
    discover_plugin_pages,
    intro_from_readme,
    localized_intros_from_readmes,
    merge_plugin_pages,
    normalize_locale,
    normalize_page_descriptor,
)


PACKAGE_EXTENSION = ".inxpkg"
PACKAGE_MANIFEST = "InxPackage.json"
SOURCE_MANIFEST = PACKAGE_MANIFEST
PACKAGE_SCHEMA = "infernux.inxpackage"
PACKAGE_ENTRY_PREFIX = "Package/"
_GUID_NAMESPACE = uuid.UUID("2bd3f0e2-0e94-4a61-bfe4-146b96bb66ab")
_IGNORED_PARTS = frozenset({".git", "__pycache__", ".venv", "venv", "build", "dist"})
_CONTROL_NAMES = frozenset({"requirements.txt"})
_CONTROL_PREFIXES = ("readme", "license", "copying", "notice", "changelog")
_CONTROL_DIRECTORIES = frozenset({"InxPluginPages"})


@dataclass(frozen=True, slots=True)
class InxPackagePreview:
    package_path: str
    metadata: Mapping[str, object]
    entries: tuple[Mapping[str, object], ...]

    @property
    def file_records(self) -> tuple[Mapping[str, object], ...]:
        return tuple(
            dict(item)
            for item in self.metadata.get("files", ())
            if isinstance(item, Mapping)
        )

    @property
    def logical_entries(self) -> tuple[str, ...]:
        return tuple(str(item["logical_path"]) for item in self.file_records)

    @property
    def project_entries(self) -> tuple[str, ...]:
        """Return derived project destinations for import-preview UI."""

        reference = str(self.metadata["reference"])
        return tuple(
            package_destination(reference, str(item["logical_path"]))
            for item in self.file_records
        )


class InxPackage:
    """The only standalone package container understood by Infernux."""

    @staticmethod
    def export(
        project_root: str,
        source_paths: Sequence[str],
        destination: str,
        *,
        metadata: Mapping[str, object] | None = None,
        profile: str = "development",
    ) -> InxPackagePreview:
        """Export selected sources into a deterministic package-source tree.

        Selecting one directory that contains ``InxPackage.json`` treats that
        directory as the package source root. A single ordinary directory
        exports its contents directly under the generated plugin reference;
        multi-selection keeps each selected basename.
        """

        project = resolved_path(project_root)
        if not project:
            raise ValueError("InxPackage export requires a project root")
        sources = InxPackage._resolve_sources(project, source_paths)
        source_root = InxPackage._package_source_root(sources)
        source_document = InxPackage._source_document(source_root, metadata)
        reference = validate_reference(
            str(source_document.get("reference") or InxPackage._default_reference(sources))
        )
        files = InxPackage._collect_logical_files(sources, source_root)
        if not files:
            raise ValueError("InxPackage export selected no files")

        pages = merge_plugin_pages(
            discover_plugin_pages(source_root) if source_root else (),
            source_document.get("pages"),
        )
        intro = str(source_document.get("intro") or "")
        if not intro and source_root:
            intro = intro_from_readme(source_root, pages)
        intros = (
            localized_intros_from_readmes(source_root, pages)
            if source_root else {}
        )
        explicit_intros = source_document.get("intros", {})
        if not isinstance(explicit_intros, Mapping):
            raise ValueError("InxPackage intros must be an object")
        for locale, value in explicit_intros.items():
            normalized_locale = normalize_locale(str(locale))
            if not normalized_locale:
                raise ValueError("InxPackage localized intro requires a locale")
            intros[normalized_locale] = str(value)

        records: list[dict[str, object]] = []
        payloads: list[tuple[str, str, bytes]] = []
        for logical, source in files:
            guid, meta_payload = InxPackage._asset_identity(reference, logical, source)
            role = package_role(logical)
            archive_path = PACKAGE_ENTRY_PREFIX + logical
            meta_archive_path = archive_path + ".meta"
            records.append(
                {
                    "logical_path": logical,
                    "guid": guid,
                    "role": role,
                    "archive_path": archive_path,
                    "meta_archive_path": meta_archive_path,
                }
            )
            payloads.append((archive_path, source, meta_payload))

        raw_dependencies = source_document.get("dependencies", [])
        if not isinstance(raw_dependencies, list):
            raise ValueError("InxPackage source dependencies must be a list")
        document: dict[str, object] = {
            "$schema": PACKAGE_SCHEMA,
            "reference": reference,
            "name": str(source_document.get("name") or reference.rsplit("/", 1)[-1]),
            "version": str(source_document.get("version") or "0.0.0"),
            "intro": intro,
            "intros": intros,
            "requirements": str(source_document.get("requirements") or "requirements.txt"),
            "engine": str(source_document.get("engine") or ""),
            "dependencies": [
                validate_reference(str(item))
                for item in raw_dependencies
            ],
            "pages": pages,
            "control_guid": uuid.uuid5(
                _GUID_NAMESPACE, f"{reference}\0{PACKAGE_MANIFEST}"
            ).hex,
            "files": records,
        }
        InxPackage.validate_metadata(document)
        destination = resolved_path(destination)
        if not destination.casefold().endswith(PACKAGE_EXTENSION):
            destination += PACKAGE_EXTENSION
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="infernux-inxpackage-") as workspace:
            manifest_path = os.path.join(workspace, PACKAGE_MANIFEST)
            with open(manifest_path, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(document, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
            native_sources: list[tuple[str, str]] = [(PACKAGE_MANIFEST, manifest_path)]
            metadata_root = os.path.join(workspace, "metadata")
            for index, (archive_path, source, meta_payload) in enumerate(payloads):
                meta_path = os.path.join(metadata_root, f"{index}.meta")
                os.makedirs(os.path.dirname(meta_path), exist_ok=True)
                with open(meta_path, "wb") as stream:
                    stream.write(meta_payload)
                native_sources.append((archive_path, source))
                native_sources.append((archive_path + ".meta", meta_path))
            write_pack(native_sources, destination, profile=profile)
        return InxPackage.inspect(destination)

    @staticmethod
    def export_source(
        source_root: str,
        destination: str,
        *,
        metadata: Mapping[str, object] | None = None,
        profile: str = "development",
    ) -> InxPackagePreview:
        root = resolved_path(source_root)
        return InxPackage.export(
            root,
            [root],
            destination,
            metadata=metadata,
            profile=profile,
        )

    @staticmethod
    def inspect(package_path: str) -> InxPackagePreview:
        path = resolved_path(package_path)
        native_manifest = read_manifest(path)
        entries = tuple(
            dict(entry)
            for entry in native_manifest.get("files", ())
            if isinstance(entry, Mapping)
        )
        actual_paths = {str(entry.get("path", "")) for entry in entries}
        if PACKAGE_MANIFEST not in actual_paths:
            raise ValueError("InxPackage is missing InxPackage.json")
        try:
            metadata = json.loads(read_entry(path, PACKAGE_MANIFEST).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("InxPackage.json is not valid UTF-8 JSON") from exc
        InxPackage.validate_metadata(metadata)
        for record in metadata["files"]:
            archive_path = str(record["archive_path"])
            meta_archive_path = str(record["meta_archive_path"])
            if archive_path not in actual_paths or meta_archive_path not in actual_paths:
                raise ValueError(
                    f"InxPackage native manifest is missing payload: {record['logical_path']}"
                )
            meta_payload = read_entry(path, meta_archive_path)
            if _guid_from_meta_bytes(meta_payload) != record["guid"]:
                raise ValueError(
                    f"InxPackage GUID disagrees with metadata: {record['logical_path']}"
                )
        return InxPackagePreview(path, metadata, entries)

    @staticmethod
    def extract(
        package_path: str,
        project_root: str,
        *,
        selected: Iterable[str] | None = None,
        overwrite: bool = False,
    ) -> tuple[str, ...]:
        """Extract derived project files without registering package state.

        PluginManager is the normal transactional installation authority. This
        primitive exists for the import-preview UI and focused format tests.
        """

        preview = InxPackage.inspect(package_path)
        project = resolved_path(project_root)
        if not project:
            raise ValueError("InxPackage extraction requires a project root")
        selected_set = None if selected is None else {
            portable_path(str(item)).strip("/") for item in selected
        }
        extracted: list[str] = []
        for record in preview.file_records:
            logical = str(record["logical_path"])
            destination_relative = package_destination(
                str(preview.metadata["reference"]), logical
            )
            if selected_set is not None and not (
                logical in selected_set or destination_relative in selected_set
            ):
                continue
            destination = _safe_project_destination(project, destination_relative)
            if os.path.exists(destination) and not overwrite:
                raise FileExistsError(destination)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            _atomic_write(
                destination,
                read_entry(preview.package_path, str(record["archive_path"])),
                project,
            )
            _atomic_write(
                destination + ".meta",
                read_entry(preview.package_path, str(record["meta_archive_path"])),
                project,
            )
            extracted.append(destination)

        control_root = package_control_root(project, str(preview.metadata["reference"]))
        manifest_destination = os.path.join(control_root, PACKAGE_MANIFEST)
        if selected_set is None:
            os.makedirs(control_root, exist_ok=True)
            manifest_payload = (
                json.dumps(preview.metadata, ensure_ascii=False, indent=2).encode("utf-8")
                + b"\n"
            )
            _atomic_write(
                manifest_destination,
                manifest_payload,
                project,
            )
            _atomic_write(
                manifest_destination + ".meta",
                current_meta_bytes(
                    str(preview.metadata["control_guid"]), manifest_payload
                ),
                project,
            )
            extracted.append(manifest_destination)
        return tuple(extracted)

    @staticmethod
    def validate_metadata(value: object) -> None:
        if not isinstance(value, Mapping):
            raise ValueError("InxPackage metadata must be an object")
        expected_fields = {
            "$schema", "reference", "name", "version", "intro", "intros",
            "requirements", "engine", "dependencies", "pages", "control_guid",
            "files",
        }
        if value.get("$schema") != PACKAGE_SCHEMA or set(value) != expected_fields:
            raise ValueError("Unsupported InxPackage metadata schema")
        reference = validate_reference(str(value.get("reference", "")))
        control_guid = str(value.get("control_guid", ""))
        _validate_guid(control_guid, "control_guid")
        files = value.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("InxPackage metadata requires a non-empty files list")
        logical_paths: set[str] = set()
        guids: set[str] = {control_guid.casefold()}
        destinations: set[str] = set()
        for item in files:
            if not isinstance(item, Mapping):
                raise ValueError("InxPackage file record must be an object")
            if set(item) != {
                "logical_path", "guid", "role", "archive_path", "meta_archive_path"
            }:
                raise ValueError("InxPackage file record does not match the current schema")
            logical = _safe_relative(str(item.get("logical_path", "")))
            _validate_canonical_layout(logical)
            if logical in logical_paths:
                raise ValueError(f"Duplicate InxPackage logical path: {logical}")
            logical_paths.add(logical)
            guid = str(item.get("guid", ""))
            _validate_guid(guid, logical)
            if guid.casefold() in guids:
                raise ValueError(f"Duplicate InxPackage GUID: {guid}")
            guids.add(guid.casefold())
            if item.get("role") != package_role(logical):
                raise ValueError(f"InxPackage role is not canonical: {logical}")
            if item.get("archive_path") != PACKAGE_ENTRY_PREFIX + logical:
                raise ValueError(f"InxPackage archive path is not canonical: {logical}")
            if item.get("meta_archive_path") != PACKAGE_ENTRY_PREFIX + logical + ".meta":
                raise ValueError(f"InxPackage meta archive path is not canonical: {logical}")
            destination = package_destination(reference, logical).casefold()
            if destination in destinations:
                raise ValueError(f"Duplicate InxPackage destination: {logical}")
            destinations.add(destination)
        pages = value.get("pages", [])
        if not isinstance(pages, list):
            raise ValueError("InxPackage pages must be a list")
        normalized_pages = [normalize_page_descriptor(item) for item in pages]
        page_keys = [(item["id"], item.get("locale", "")) for item in normalized_pages]
        if len(page_keys) != len(set(page_keys)):
            raise ValueError("InxPackage page id and locale pairs must be unique")
        intros = value.get("intros", {})
        if not isinstance(intros, Mapping):
            raise ValueError("InxPackage intros must be an object")
        normalized_intro_locales = [normalize_locale(str(item)) for item in intros]
        if any(not item for item in normalized_intro_locales):
            raise ValueError("InxPackage localized intro requires a locale")
        if len(normalized_intro_locales) != len(set(normalized_intro_locales)):
            raise ValueError("InxPackage intro locales must be unique")
        if any(not isinstance(item, str) for item in intros.values()):
            raise ValueError("InxPackage localized intros must be strings")
        if not isinstance(value.get("engine", ""), str):
            raise ValueError("InxPackage engine compatibility must be a string")
        try:
            SpecifierSet(str(value.get("engine", "")))
        except InvalidSpecifier as exc:
            raise ValueError("InxPackage engine compatibility is invalid") from exc
        dependencies = value.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise ValueError("InxPackage dependencies must be a list")
        normalized_dependencies = [validate_reference(str(item)) for item in dependencies]
        if len({item.casefold() for item in normalized_dependencies}) != len(
            normalized_dependencies
        ):
            raise ValueError("InxPackage dependencies must be unique")

    @staticmethod
    def _resolve_sources(project: str, source_paths: Sequence[str]) -> tuple[str, ...]:
        if not source_paths:
            raise ValueError("InxPackage export requires at least one source")
        result: list[str] = []
        for value in source_paths:
            path = resolved_path(value if os.path.isabs(value) else os.path.join(project, value))
            if not is_path_within(path, project, allow_root=True):
                raise ValueError(f"InxPackage source is outside the project: {value}")
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            result.append(path)
        return tuple(result)

    @staticmethod
    def _package_source_root(sources: Sequence[str]) -> str:
        if len(sources) != 1 or not os.path.isdir(sources[0]):
            return ""
        candidate = sources[0]
        return candidate if os.path.isfile(os.path.join(candidate, SOURCE_MANIFEST)) else ""

    @staticmethod
    def _source_document(
        source_root: str, supplied: Mapping[str, object] | None
    ) -> dict[str, object]:
        document: dict[str, object] = {}
        if source_root:
            with open(os.path.join(source_root, SOURCE_MANIFEST), "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if not isinstance(loaded, dict):
                raise ValueError("InxPackage.json must contain an object")
            document.update(loaded)
        if supplied:
            document.update(dict(supplied))
        return document

    @staticmethod
    def _default_reference(sources: Sequence[str]) -> str:
        stem = Path(sources[0]).stem if len(sources) == 1 else "selection"
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.").lower()
        return normalized or "package"

    @staticmethod
    def _collect_logical_files(
        sources: Sequence[str], source_root: str
    ) -> list[tuple[str, str]]:
        collected: dict[str, str] = {}
        unwrap_single_directory = bool(
            not source_root and len(sources) == 1 and os.path.isdir(sources[0])
        )
        for source in sources:
            if os.path.isdir(source):
                # A manifest-backed source owns an explicit package layout.
                # For an ordinary folder exported from Project view, the
                # generated reference already supplies the destination folder;
                # wrapping the selection again would produce
                # Assets/Plugins/materials/Materials/... style duplication.
                root = (
                    source
                    if unwrap_single_directory
                    else source_root or os.path.dirname(source)
                )
                for walk_root, dirs, names in os.walk(source):
                    dirs[:] = sorted(
                        name
                        for name in dirs
                        if name not in _IGNORED_PARTS and not name.startswith(".")
                    )
                    for name in sorted(names):
                        if (
                            name == SOURCE_MANIFEST
                            or name in _IGNORED_PARTS
                            or name.startswith(".")
                            or name.endswith((".pyc", ".pyo", ".meta"))
                        ):
                            continue
                        path = os.path.join(walk_root, name)
                        logical = portable_path(relative_path(path, root))
                        _validate_canonical_layout(logical)
                        collected.setdefault(_safe_relative(logical), path)
            else:
                if source.endswith(".meta"):
                    continue
                logical = _safe_relative(os.path.basename(source))
                _validate_canonical_layout(logical)
                collected.setdefault(logical, source)
        return sorted(collected.items(), key=lambda item: item[0].encode("utf-8"))

    @staticmethod
    def _asset_identity(reference: str, logical: str, source: str) -> tuple[str, bytes]:
        content = Path(source).read_bytes()
        meta_path = source + ".meta"
        if os.path.isfile(meta_path):
            payload = Path(meta_path).read_bytes()
            guid = _guid_from_meta_bytes(payload)
            _validate_guid(guid, logical)
            return guid, current_meta_bytes(guid, content, existing=payload)
        guid = uuid.uuid5(_GUID_NAMESPACE, f"{reference}\0{logical}").hex
        return guid, current_meta_bytes(guid, content)


def validate_reference(value: str) -> str:
    reference = portable_path(str(value).strip()).strip("/")
    if not reference or "\\" in str(value):
        raise ValueError("InxPackage reference is invalid")
    parts = reference.split("/")
    if any(
        not part
        or part in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part)
        for part in parts
    ):
        raise ValueError("InxPackage reference is invalid")
    if len(reference) >= 2 and reference[1] == ":":
        raise ValueError("InxPackage reference cannot contain a drive")
    return reference


def package_role(logical_path: str) -> str:
    logical = _safe_relative(logical_path)
    _validate_canonical_layout(logical)
    first, _, _remainder = logical.partition("/")
    if first.casefold() == "runtime":
        return "runtime"
    if first.casefold() == "editor":
        return "editor"
    basename = posixpath.basename(logical)
    lower = basename.casefold()
    if (
        lower in _CONTROL_NAMES
        or any(lower.startswith(prefix) for prefix in _CONTROL_PREFIXES)
        or first in _CONTROL_DIRECTORIES
    ):
        return "control"
    if lower.endswith(PACKAGE_EXTENSION):
        return "nested_package"
    return "content"


def package_destination(reference: str, logical_path: str) -> str:
    reference = validate_reference(reference)
    logical = _safe_relative(logical_path)
    role = package_role(logical)
    root = "Packages" if role in {"runtime", "editor", "control"} else "Assets/Plugins"
    return posixpath.join(root, reference, logical)


def package_control_root(project_root: str, reference: str) -> str:
    reference = validate_reference(reference)
    root = resolved_path(project_root)
    destination = resolved_path(os.path.join(root, "Packages", *reference.split("/")))
    if not is_path_within(destination, os.path.join(root, "Packages"), allow_root=False):
        raise ValueError("InxPackage control root escapes Packages")
    return destination


def _safe_relative(value: str) -> str:
    raw = portable_path(str(value))
    normalized = posixpath.normpath(raw).strip("/")
    if (
        not normalized
        or raw.startswith(("/", "\\"))
        or normalized == ".."
        or normalized.startswith("../")
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
        or (len(normalized) >= 2 and normalized[1] == ":")
    ):
        raise ValueError(f"Unsafe InxPackage path: {value}")
    return normalized


def _safe_project_destination(project_root: str, relative: str) -> str:
    project = resolved_path(project_root)
    normalized = _safe_relative(relative)
    if normalized.split("/", 1)[0] not in {"Assets", "Packages"}:
        raise ValueError(f"InxPackage destination root is invalid: {relative}")
    destination = resolved_path(os.path.join(project, *normalized.split("/")))
    if not is_path_within(destination, project, allow_root=False):
        raise ValueError(f"InxPackage path escapes the project: {relative}")
    return destination


def _validate_canonical_layout(logical_path: str) -> None:
    """Reject case variants whose routing differs across host filesystems."""

    logical = _safe_relative(logical_path)
    first = logical.split("/", 1)[0]
    canonical = {name.casefold(): name for name in ("Runtime", "Editor", *_CONTROL_DIRECTORIES)}
    expected = canonical.get(first.casefold())
    if expected is not None and first != expected:
        raise ValueError(
            f"InxPackage top-level directory must use canonical casing {expected}: {logical}"
        )


def _validate_guid(value: str, label: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", str(value)):
        raise ValueError(f"Invalid InxPackage GUID for {label}")


def _guid_from_meta_bytes(payload: bytes) -> str:
    try:
        root = json.loads(payload.decode("utf-8"))
        entry = root["metadata"]["guid"]
        if entry.get("type") != "string":
            return ""
        return str(entry.get("value", "")).strip()
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return ""


def _content_hash(payload: bytes) -> str:
    value = 14695981039346656037
    for byte in payload:
        value ^= byte
        value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def current_meta_bytes(
    guid: str,
    content: bytes,
    *,
    existing: bytes | None = None,
) -> bytes:
    if existing is None:
        document: dict[str, object] = {"metadata": {}}
    else:
        try:
            document = json.loads(existing.decode("utf-8"))
            metadata = document["metadata"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError("InxPackage asset metadata is not valid") from exc
        if not isinstance(document, dict) or not isinstance(metadata, dict):
            raise ValueError("InxPackage asset metadata is not valid")

    metadata = document["metadata"]
    assert isinstance(metadata, dict)
    metadata["guid"] = {"type": "string", "value": guid}
    metadata["content_hash"] = {
        "type": "string",
        "value": _content_hash(content),
    }
    return (json.dumps(document, ensure_ascii=False, indent=4) + "\n").encode("utf-8")


def _atomic_write(path: str, payload: bytes, project_root: str) -> None:
    staging = os.path.join(project_root, "Library", "InxPackageStaging")
    os.makedirs(staging, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="inxpackage-", dir=staging)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def normalize_player_rules(value: object = None) -> dict[str, object]:
    """Return the standard directory policy; custom include/exclude is gone."""

    if value not in (None, {}, False):
        raise ValueError(
            "InxPackage no longer accepts player include/exclude rules; use Runtime/Editor layout"
        )
    return {"runtime": True, "editor": False}


def player_file_exported(metadata: Mapping[str, object], relative_path: str) -> bool:
    """Standard Unity-like Player policy used until dependency stripping lands."""

    role = package_role(relative_path)
    return role not in {"editor", "control"}


__all__ = [
    "InxPackage",
    "InxPackagePreview",
    "PACKAGE_EXTENSION",
    "PACKAGE_MANIFEST",
    "PACKAGE_SCHEMA",
    "SOURCE_MANIFEST",
    "normalize_player_rules",
    "package_control_root",
    "package_destination",
    "package_role",
    "player_file_exported",
    "validate_reference",
]
