from __future__ import annotations

import sys
from pathlib import Path

import pytest


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from python_runtime_catalog import (
    DEFAULT_PYTHON_RUNTIME,
    PythonRuntimeId,
    SUPPORTED_PYTHON_RUNTIMES,
    runtime_directory_name,
    runtime_release,
)


def test_python_runtime_identity_derives_all_abi_names() -> None:
    runtime = PythonRuntimeId.parse("3.13")

    assert runtime.series == "3.13"
    assert runtime.directory_name == "python313"
    assert runtime.cp_tag == "cp313"
    assert runtime.windows_library_stem == "python313"
    assert runtime.unix_library_stem == "python3.13"


def test_patch_version_normalizes_to_minor_runtime_identity() -> None:
    assert PythonRuntimeId.parse("3.13.15") == PythonRuntimeId(3, 13)


def test_runtime_directory_layout_keeps_versions_as_siblings() -> None:
    assert runtime_directory_name("3.12") == "python312"
    assert runtime_directory_name("3.13") == "python313"


def test_python_313_is_default_but_python_312_remains_available() -> None:
    assert DEFAULT_PYTHON_RUNTIME == PythonRuntimeId(3, 13)
    assert SUPPORTED_PYTHON_RUNTIMES == (
        PythonRuntimeId(3, 13),
        PythonRuntimeId(3, 12),
    )


def test_catalog_pins_immutable_runtime_archives() -> None:
    release = runtime_release("3.13")

    assert release.patch_version == "3.13.15"
    assert release.build_release == "20260825"
    assert len(release.archive_sha256["x86_64-pc-windows-msvc"]) == 64


def test_unknown_python_runtime_is_rejected() -> None:
    with pytest.raises(ValueError, match="not in the Hub runtime catalog"):
        runtime_release("3.11")
