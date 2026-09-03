#!/usr/bin/env python3
"""Build the repository's ``package`` directory as an Infernux package.

This file intentionally uses only the Python standard library.  It is kept
outside ``package`` so repository documentation and build configuration never
enter the resulting archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import struct
import sys
import tempfile
import time
import uuid
from pathlib import Path


PACKAGE_SCHEMA = "infernux.inxpackage"
PACKAGE_MANIFEST = "inx_package.json"
PACKAGE_PREFIX = "package/"
GUID_NAMESPACE = uuid.UUID("2bd3f0e2-0e94-4a61-bfe4-146b96bb66ab")
SOURCE_FIELDS = frozenset(
    {"$schema", "reference", "name", "version", "intro", "intros", "engine", "pages"}
)
CONTROL_DIRECTORIES = frozenset({"plugin_pages"})
CONTROL_NAMES = frozenset({"requirements.txt"})
PAGE_EXTENSIONS = frozenset({".md", ".markdown", ".txt"})
IGNORED_DIRECTORIES = frozenset({".git", "__pycache__"})
MAGIC = b"INXPKG\0\0"
TOC_MAGIC = b"TOC0"
ALIGNMENT = 64
HEADER = struct.Struct("<8sIIIIQQQQQQQ32s16s")
TOC_PREFIX = struct.Struct("<4sIQQ")
ENTRY = struct.Struct("<QIIQQQB7sII8s")


def _align(value: int) -> int:
    return (value + ALIGNMENT - 1) // ALIGNMENT * ALIGNMENT


def _safe_path(value: str) -> str:
    raw = str(value).replace("\\", "/")
    normalized = posixpath.normpath(raw).strip("/")
    parts = normalized.split("/")
    if (
        not normalized
        or raw.startswith(("/", "\\"))
        or normalized == ".."
        or normalized.startswith("../")
        or any(part in {"", ".", ".."} for part in parts)
        or (len(normalized) >= 2 and normalized[1] == ":")
    ):
        raise ValueError(f"unsafe package path: {value}")
    canonical = {name.casefold(): name for name in ("runtime", "editor", *CONTROL_DIRECTORIES)}
    expected = canonical.get(parts[0].casefold())
    if expected is not None and parts[0] != expected:
        raise ValueError(
            f"top-level directory must use canonical casing {expected}: {normalized}"
        )
    return normalized


def _reference(value: str) -> str:
    result = str(value).strip().replace("\\", "/").strip("/")
    if not result or any(
        not part
        or part in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part)
        for part in result.split("/")
    ):
        raise ValueError(f"invalid package reference: {value}")
    return result


def _role(logical_path: str) -> str:
    logical = _safe_path(logical_path)
    first = logical.split("/", 1)[0]
    if first == "runtime":
        return "runtime"
    if first == "editor":
        return "editor"
    if first in CONTROL_DIRECTORIES or posixpath.basename(logical).casefold() in CONTROL_NAMES:
        return "control"
    if logical.casefold().endswith(".inxpkg"):
        return "nested_package"
    return "content"


def _content_hash(payload: bytes) -> str:
    value = 14695981039346656037
    for byte in payload:
        value ^= byte
        value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def _meta_bytes(guid: str, payload: bytes, existing_path: Path) -> bytes:
    if existing_path.is_file():
        document = json.loads(existing_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("metadata"), dict):
            raise ValueError(f"invalid asset metadata: {existing_path}")
    else:
        document = {"metadata": {}}
    document["metadata"]["guid"] = {"type": "string", "value": guid}
    document["metadata"]["content_hash"] = {
        "type": "string",
        "value": _content_hash(payload),
    }
    return (json.dumps(document, ensure_ascii=False, indent=4) + "\n").encode("utf-8")


def _existing_guid(meta_path: Path) -> str:
    if not meta_path.is_file():
        return ""
    try:
        value = json.loads(meta_path.read_text(encoding="utf-8"))["metadata"]["guid"]
        guid = str(value["value"]) if value.get("type") == "string" else ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return ""
    if not re.fullmatch(r"[0-9a-fA-F]{32}", guid):
        raise ValueError(f"invalid GUID in {meta_path}")
    return guid


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-.")
    return result or "page"


def _page_descriptor(relative: str, path: Path) -> dict[str, str]:
    suffix = path.suffix.casefold()
    stem = path.stem
    locale = ""
    if stem.endswith(".zh-CN"):
        stem = stem[: -len(".zh-CN")]
        locale = "zh-CN"
    identity = str(Path(relative).with_name(stem + path.suffix)).replace("\\", "/")
    title = stem
    try:
        first = path.read_text(encoding="utf-8").splitlines()
        heading = next((line.lstrip("#").strip() for line in first if line.startswith("#")), "")
        if heading:
            title = heading
    except (OSError, UnicodeDecodeError):
        pass
    descriptor = {
        "id": _slug(str(Path(identity).with_suffix("")).replace("\\", ".")),
        "title": title,
        "path": relative,
        "format": "markdown" if suffix in {".md", ".markdown"} else "text",
    }
    if locale:
        descriptor["locale"] = locale
    return descriptor


def _normalize_page(value: object) -> dict[str, str]:
    if isinstance(value, str):
        path = _safe_path(value)
        value = {"path": path, "id": _slug(Path(path).stem), "title": Path(path).stem}
    if not isinstance(value, dict):
        raise ValueError("package page must be an object or relative path")
    path = _safe_path(str(value.get("path", "")))
    if Path(path).suffix.casefold() not in PAGE_EXTENSIONS:
        raise ValueError(f"unsupported package page: {path}")
    page_id = str(value.get("id") or _slug(Path(path).stem)).strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", page_id):
        raise ValueError(f"invalid package page id: {page_id}")
    title = str(value.get("title") or Path(path).stem).strip()
    page_format = str(
        value.get("format")
        or ("markdown" if Path(path).suffix.casefold() in {".md", ".markdown"} else "text")
    ).strip().casefold()
    locale = str(value.get("locale", "")).strip()
    if not title or page_format not in {"markdown", "text"} or locale not in {"", "zh-CN"}:
        raise ValueError(f"invalid package page descriptor: {path}")
    result = {"id": page_id, "title": title, "path": path, "format": page_format}
    if locale:
        result["locale"] = locale
    return result


def _pages(package_root: Path, explicit: object) -> list[dict[str, str]]:
    discovered: list[dict[str, str]] = []
    pages_root = package_root / "plugin_pages"
    if pages_root.is_dir():
        for path in sorted(
            (item for item in pages_root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(package_root).as_posix().casefold(),
        ):
            if path.suffix.casefold() in PAGE_EXTENSIONS:
                relative = path.relative_to(package_root).as_posix()
                discovered.append(_page_descriptor(relative, path))
    if explicit is None:
        return discovered
    if not isinstance(explicit, list):
        raise ValueError("package pages must be a list")
    declared = [_normalize_page(item) for item in explicit]
    declared_keys = {(item["id"], item.get("locale", "")) for item in declared}
    return declared + [
        item for item in discovered if (item["id"], item.get("locale", "")) not in declared_keys
    ]


def _source_document(package_root: Path) -> dict[str, object]:
    path = package_root / PACKAGE_MANIFEST
    if not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("inx_package.json must contain an object")
    unknown = sorted(set(document) - SOURCE_FIELDS)
    if unknown:
        raise ValueError("unsupported inx_package.json fields: " + ", ".join(unknown))
    return document


def _files(package_root: Path) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    for walk_root, directories, names in os.walk(package_root):
        directories[:] = sorted(name for name in directories if name not in IGNORED_DIRECTORIES)
        for name in sorted(names):
            path = Path(walk_root) / name
            logical = _safe_path(path.relative_to(package_root).as_posix())
            if name.endswith(".meta") or logical == PACKAGE_MANIFEST:
                continue
            result.append((logical, path))
    result.sort(key=lambda item: item[0].encode("utf-8"))
    if not result:
        raise ValueError("package directory contains no payload files")
    return result


def _write_inxpack(entries: list[tuple[str, bytes]], destination: Path) -> None:
    entries.sort(key=lambda item: item[0].encode("utf-8"))
    if len({path for path, _payload in entries}) != len(entries):
        raise ValueError("duplicate InxPack entry path")
    string_table = b"".join(path.encode("utf-8") for path, _payload in entries)
    toc_bytes = _align(TOC_PREFIX.size + len(entries) * ENTRY.size + len(string_table))
    payload_offset = HEADER.size + toc_bytes
    payload = bytearray()
    records = bytearray()
    path_offset = 0
    raw_bytes = 0
    stored_bytes = 0
    for path, content in entries:
        encoded = path.encode("utf-8")
        entry_offset = len(payload)
        records.extend(
            ENTRY.pack(
                path_offset,
                len(encoded),
                0,
                entry_offset,
                len(content),
                len(content),
                0,
                b"\0" * 7,
                ALIGNMENT,
                0,
                b"\0" * 8,
            )
        )
        payload.extend(content)
        payload.extend(b"\0" * (_align(len(payload)) - len(payload)))
        path_offset += len(encoded)
        raw_bytes += len(content)
        stored_bytes += len(content)
    toc = bytearray(TOC_PREFIX.pack(TOC_MAGIC, 0, len(entries), len(string_table)))
    toc.extend(records)
    toc.extend(string_table)
    toc.extend(b"\0" * (toc_bytes - len(toc)))
    header_values = (
        MAGIC,
        HEADER.size,
        0,
        ENTRY.size,
        0,
        HEADER.size,
        toc_bytes,
        payload_offset,
        len(payload),
        len(entries),
        len(string_table),
        raw_bytes,
    )
    header = HEADER.pack(*header_values, b"\0" * 32, b"\0" * 16)
    archive = bytearray(header + toc + payload)
    digest = hashlib.sha256(archive).digest()
    archive[: HEADER.size] = HEADER.pack(*header_values, digest, b"\0" * 16)

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".tmp.", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(archive)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(64):
            try:
                os.replace(temporary_name, destination)
                break
            except PermissionError:
                if attempt == 63:
                    raise
                time.sleep(0.001)
    finally:
        try:
            os.remove(temporary_name)
        except FileNotFoundError:
            pass


def build(destination: Path) -> Path:
    repository_root = Path(__file__).resolve().parent
    package_root = repository_root / "package"
    if not package_root.is_dir():
        raise FileNotFoundError(f"missing package directory: {package_root}")
    destination = destination.expanduser().resolve()
    if destination.suffix.casefold() != ".inxpkg":
        destination = destination.with_name(destination.name + ".inxpkg")
    source = _source_document(package_root)
    default_reference = re.sub(r"[^A-Za-z0-9._-]+", "-", destination.stem).strip("-.").lower()
    reference = _reference(str(source.get("reference") or default_reference or "package"))
    payload_entries: list[tuple[str, bytes]] = []
    records: list[dict[str, str]] = []
    guids: set[str] = set()
    for logical, path in _files(package_root):
        content = path.read_bytes()
        meta_path = path.with_name(path.name + ".meta")
        guid = _existing_guid(meta_path) or uuid.uuid5(GUID_NAMESPACE, f"{reference}\0{logical}").hex
        if guid.casefold() in guids:
            raise ValueError(f"duplicate package GUID: {guid}")
        guids.add(guid.casefold())
        archive_path = PACKAGE_PREFIX + logical
        records.append(
            {
                "logical_path": logical,
                "guid": guid,
                "role": _role(logical),
                "archive_path": archive_path,
                "meta_archive_path": archive_path + ".meta",
            }
        )
        payload_entries.append((archive_path, content))
        payload_entries.append((archive_path + ".meta", _meta_bytes(guid, content, meta_path)))
    intros = source.get("intros", {})
    if not isinstance(intros, dict) or any(locale not in {"zh", "zh-CN", "en", ""} for locale in intros):
        raise ValueError("package intros must use en or zh-CN locales")
    normalized_intros = {
        ("zh-CN" if locale == "zh" else "" if locale == "en" else locale): str(value)
        for locale, value in intros.items()
    }
    control_guid = uuid.uuid5(GUID_NAMESPACE, f"{reference}\0{PACKAGE_MANIFEST}").hex
    if control_guid.casefold() in guids:
        raise ValueError("package control GUID duplicates a payload GUID")
    manifest = {
        "$schema": PACKAGE_SCHEMA,
        "reference": reference,
        "name": str(source.get("name") or reference.rsplit("/", 1)[-1]),
        "version": str(source.get("version") or "0.0.0"),
        "intro": str(source.get("intro") or ""),
        "intros": normalized_intros,
        "engine": str(source.get("engine") or ""),
        "pages": _pages(package_root, source.get("pages")),
        "control_guid": control_guid,
        "files": records,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_inxpack([(PACKAGE_MANIFEST, manifest_bytes), *payload_entries], destination)
    return destination


def main(argv: list[str] | None = None) -> int:
    repository_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "destination",
        nargs="?",
        type=Path,
        default=repository_root / f"{repository_root.name}.inxpkg",
        help="output .inxpkg path (default: repository name beside this script)",
    )
    arguments = parser.parse_args(argv)
    try:
        output = build(arguments.destination)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"package.py: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

