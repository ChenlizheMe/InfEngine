"""The single CPython ABI targeted by this Infernux engine version."""

from __future__ import annotations


PYTHON_MAJOR = 3
PYTHON_MINOR = 13
PYTHON_VERSION = f"{PYTHON_MAJOR}.{PYTHON_MINOR}"
CPYTHON_TAG = f"cp{PYTHON_MAJOR}{PYTHON_MINOR}"
PYTHON_RUNTIME_DIRECTORY = f"python{PYTHON_MAJOR}{PYTHON_MINOR}"
WINDOWS_PYTHON_DLL = f"{PYTHON_RUNTIME_DIRECTORY}.dll"
LINUX_PYTHON_SHARED_PREFIX = f"libpython{PYTHON_VERSION}.so"


def current_interpreter_matches() -> bool:
    import sys

    return sys.version_info[:2] == (PYTHON_MAJOR, PYTHON_MINOR)


__all__ = [
    "CPYTHON_TAG",
    "LINUX_PYTHON_SHARED_PREFIX",
    "PYTHON_MAJOR",
    "PYTHON_MINOR",
    "PYTHON_RUNTIME_DIRECTORY",
    "PYTHON_VERSION",
    "WINDOWS_PYTHON_DLL",
    "current_interpreter_matches",
]
