"""The single CPython ABI targeted by this Infernux engine version."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path


PYTHON_MAJOR = 3
PYTHON_MINOR = 13
PYTHON_VERSION = f"{PYTHON_MAJOR}.{PYTHON_MINOR}"
CPYTHON_TAG = f"cp{PYTHON_MAJOR}{PYTHON_MINOR}"
PYTHON_RUNTIME_DIRECTORY = f"python{PYTHON_MAJOR}{PYTHON_MINOR}"
WINDOWS_PYTHON_DLL = f"{PYTHON_RUNTIME_DIRECTORY}.dll"
WINDOWS_LIBFFI_DLL_PATTERNS = ("ffi.dll", "ffi-*.dll", "libffi*.dll")
LINUX_PYTHON_SHARED_PREFIX = f"libpython{PYTHON_VERSION}.so"
BOOTSTRAP_NATIVE_MANIFEST_FILENAME = "_infernux_bootstrap_native.json"
BOOTSTRAP_NATIVE_MANIFEST_SCHEMA = "infernux.player-bootstrap-native.v1"


def current_interpreter_matches() -> bool:
    import sys

    return sys.version_info[:2] == (PYTHON_MAJOR, PYTHON_MINOR)


def is_windows_libffi_dll(filename: str) -> bool:
    """Return whether *filename* is a supported Windows libffi runtime name."""

    name = filename.casefold()
    return (
        name == "ffi.dll"
        or (name.startswith("ffi-") and name.endswith(".dll"))
        or (name.startswith("libffi") and name.endswith(".dll"))
    )


def stdlib_extension_module_sources() -> dict[str, Path]:
    """Resolve physical native modules owned by the active CPython stdlib."""

    extension_suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    sources: dict[str, Path] = {}
    for module_name in sorted(sys.stdlib_module_names):
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, AttributeError, ValueError):
            continue
        origin = getattr(spec, "origin", None) if spec is not None else None
        if not origin or not str(origin).endswith(extension_suffixes):
            continue
        source = Path(origin).resolve()
        if source.is_file():
            sources.setdefault(source.name, source)
    return sources


__all__ = [
    "BOOTSTRAP_NATIVE_MANIFEST_FILENAME",
    "BOOTSTRAP_NATIVE_MANIFEST_SCHEMA",
    "CPYTHON_TAG",
    "LINUX_PYTHON_SHARED_PREFIX",
    "PYTHON_MAJOR",
    "PYTHON_MINOR",
    "PYTHON_RUNTIME_DIRECTORY",
    "PYTHON_VERSION",
    "WINDOWS_LIBFFI_DLL_PATTERNS",
    "WINDOWS_PYTHON_DLL",
    "current_interpreter_matches",
    "is_windows_libffi_dll",
    "stdlib_extension_module_sources",
]
