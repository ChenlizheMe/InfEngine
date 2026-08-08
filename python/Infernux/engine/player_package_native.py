"""Thin Python bridge to the native InxPack writer/reader.

The package format and all compression/checksum logic live in the C++
filesystem module.  This file only converts Python arguments and deliberately
raises when the native backend is unavailable.  Tests may install a fake
backend with ``set_test_backend``; production code cannot fall back to ZIP,
Deflate, LZMA or an older Python container.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterable
from typing import Any


_test_backend: Any | None = None


def set_test_backend(backend: Any | None) -> None:
    """Install a fake native backend for contract tests only."""

    global _test_backend
    _test_backend = backend


def _backend() -> Any:
    if _test_backend is not None:
        return _test_backend
    candidates = ("Infernux.lib._Infernux", "_Infernux")
    for module_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if all(
            hasattr(module, name)
            for name in ("_inxpack_write", "_inxpack_read_manifest", "_inxpack_extract", "_inxpack_read_entry")
        ):
            return module
    raise RuntimeError(
        "Native InxPack backend is unavailable. This Player/package operation "
        "requires the current native runtime; no Python or legacy-container fallback exists."
    )


def write_pack(
    files: Iterable[tuple[str, str | os.PathLike[str]]],
    destination: str | os.PathLike[str],
    *,
    compression_level: int | None = None,
    profile: str = "development",
) -> dict[str, object]:
    source_pairs = [(str(path), os.fspath(source)) for path, source in files]
    return dict(
        _backend()._inxpack_write(
            source_pairs,
            os.fspath(destination),
            compression_level,
            profile,
        )
    )


def read_manifest(path: str | os.PathLike[str]) -> dict[str, object]:
    return dict(_backend()._inxpack_read_manifest(os.fspath(path)))


def extract_pack(
    path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    allowed_roots: set[str] | None = None,
) -> dict[str, object]:
    roots = None if allowed_roots is None else sorted(str(root) for root in allowed_roots)
    return dict(_backend()._inxpack_extract(os.fspath(path), os.fspath(destination), roots))


def read_entry(path: str | os.PathLike[str], entry_path: str) -> bytes:
    return bytes(_backend()._inxpack_read_entry(os.fspath(path), str(entry_path)))


__all__ = ["extract_pack", "read_entry", "read_manifest", "set_test_backend", "write_pack"]
