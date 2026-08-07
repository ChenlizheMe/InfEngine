"""Small deterministic container used by exported Player packages.

This is deliberately independent from the Editor asset system.  It is a
linear block container with a JSON table of contents followed by payload
blocks.  LZMA is used only when it makes a block smaller; already compressed
or native files are stored as-is.  The format has no ZIP central directory or
Deflate dependency, and every extracted block is hash checked.
"""

from __future__ import annotations

import hashlib
import json
import lzma
import os
import struct
from pathlib import Path
from typing import Iterable


MAGIC = b"INXPCK1\0"
FORMAT_VERSION = 1
_HEADER_LENGTH = struct.Struct("<Q")


def _portable_name(name: str) -> str:
    normalized = str(name).replace("\\", "/").strip("/")
    parts = normalized.split("/") if normalized else []
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"invalid Player package path: {name!r}")
    if ":" in parts[0]:
        raise ValueError(f"absolute Player package path is forbidden: {name!r}")
    return "/".join(parts)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_pack(
    files: Iterable[tuple[str, str | os.PathLike[str]]],
    destination: str | os.PathLike[str],
    *,
    compression_preset: int = 6,
) -> dict[str, object]:
    """Write a deterministic Player container and return its manifest."""

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, source in files:
        portable = _portable_name(name)
        if portable in seen:
            raise ValueError(f"duplicate Player package path: {portable}")
        source_path = str(source)
        if not os.path.isfile(source_path):
            raise FileNotFoundError(source_path)
        seen.add(portable)
        entries.append((portable, source_path))
    entries.sort(key=lambda item: item[0])
    if not entries:
        raise ValueError("Player package cannot be empty")

    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(
        f".{destination_path.name}.{os.getpid()}.tmp"
    )
    table: list[dict[str, object]] = []
    payload_offset = 0
    raw_bytes = 0
    payload_temporary = destination_path.with_name(
        f".{destination_path.name}.{os.getpid()}.payload.tmp"
    )
    try:
        # Stage payload blocks separately.  Rebuilding the prefix must not
        # read the complete archive back into memory for large game packages.
        with payload_temporary.open("wb") as payload_output:
            for portable, source_path in entries:
                raw = Path(source_path).read_bytes()
                compressed = lzma.compress(raw, preset=compression_preset)
                if len(compressed) + 16 < len(raw):
                    stored = compressed
                    codec = "lzma"
                else:
                    stored = raw
                    codec = "store"
                table.append(
                    {
                        "path": portable,
                        "offset": payload_offset,
                        "stored_bytes": len(stored),
                        "raw_bytes": len(raw),
                        "codec": codec,
                        "sha256": _sha256(raw),
                    }
                )
                payload_output.write(stored)
                payload_offset += len(stored)
                raw_bytes += len(raw)

            header = {
                "format": "infernux-player-pack",
                "version": FORMAT_VERSION,
                "codec": "lzma-or-store",
                "file_count": len(table),
                "raw_bytes": raw_bytes,
                "stored_bytes": payload_offset,
                "files": table,
            }
            encoded_header = json.dumps(
                header, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        with temporary.open("wb") as output:
            output.write(MAGIC)
            output.write(_HEADER_LENGTH.pack(len(encoded_header)))
            output.write(encoded_header)
            with payload_temporary.open("rb") as payload_input:
                for block in iter(lambda: payload_input.read(1024 * 1024), b""):
                    output.write(block)
        os.replace(temporary, destination_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        try:
            payload_temporary.unlink()
        except FileNotFoundError:
            pass

    result = dict(header)
    result["archive"] = destination_path.name
    result["archive_bytes"] = destination_path.stat().st_size
    digest = hashlib.sha256()
    with destination_path.open("rb") as archive:
        for block in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(block)
    result["archive_sha256"] = digest.hexdigest()
    return result


def read_manifest(path: str | os.PathLike[str]) -> tuple[dict[str, object], int]:
    """Read and validate a container table of contents."""

    with open(path, "rb") as source:
        if source.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"not an Infernux Player package: {path}")
        raw_length = source.read(_HEADER_LENGTH.size)
        if len(raw_length) != _HEADER_LENGTH.size:
            raise ValueError("truncated Player package header")
        header_length = _HEADER_LENGTH.unpack(raw_length)[0]
        if header_length > 128 * 1024 * 1024:
            raise ValueError("Player package header is unreasonably large")
        try:
            header = json.loads(source.read(header_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid Player package manifest") from exc
    if not isinstance(header, dict) or header.get("version") != FORMAT_VERSION:
        raise ValueError("unsupported Player package format")
    files = header.get("files")
    if not isinstance(files, list) or len(files) != header.get("file_count"):
        raise ValueError("Player package file table is invalid")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("Player package file entry is invalid")
        name = _portable_name(str(entry.get("path", "")))
        if name in seen:
            raise ValueError(f"duplicate Player package entry: {name}")
        seen.add(name)
    return header, len(MAGIC) + _HEADER_LENGTH.size + header_length


def extract_pack(
    path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    allowed_roots: set[str] | None = None,
) -> dict[str, object]:
    """Extract a package after validating every block and path."""

    header, payload_base = read_manifest(path)
    package_size = os.path.getsize(path)
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    with open(path, "rb") as source:
        for entry in header["files"]:
            name = _portable_name(str(entry["path"]))
            if allowed_roots is not None and name.split("/", 1)[0] not in allowed_roots:
                raise ValueError(f"unexpected Player package entry: {name}")
            offset = int(entry["offset"])
            stored_size = int(entry["stored_bytes"])
            raw_size = int(entry["raw_bytes"])
            if offset < 0 or stored_size < 0 or payload_base + offset + stored_size > package_size:
                raise ValueError(f"out-of-range Player package entry: {name}")
            source.seek(payload_base + offset)
            data = source.read(stored_size)
            if entry.get("codec") == "lzma":
                data = lzma.decompress(data)
            elif entry.get("codec") != "store":
                raise ValueError(f"unknown Player package codec: {entry.get('codec')}")
            if len(data) != raw_size or _sha256(data) != entry.get("sha256"):
                raise ValueError(f"Player package checksum mismatch: {name}")
            output = target.joinpath(*name.split("/"))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
    return header
