from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from project_python_runtime import (
    project_runtime_directory,
    read_project_python_version,
    write_project_python_version,
)


def test_project_python_binding_round_trips(tmp_path: Path) -> None:
    write_project_python_version(tmp_path, "3.13")

    settings = json.loads(
        (tmp_path / "ProjectSettings" / "PythonRuntime.json").read_text(
            encoding="utf-8"
        )
    )
    assert settings == {"pythonVersion": "3.13", "schemaVersion": 1}
    assert read_project_python_version(tmp_path) == "3.13"


def test_legacy_python312_project_is_detected_without_rewriting_it(
    tmp_path: Path,
) -> None:
    (tmp_path / ".runtime" / "python312").mkdir(parents=True)

    assert read_project_python_version(tmp_path) == "3.12"
    assert not (tmp_path / "ProjectSettings" / "PythonRuntime.json").exists()


def test_unbound_project_with_multiple_runtimes_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".runtime" / "python312").mkdir(parents=True)
    (tmp_path / ".runtime" / "python313").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="multiple Python runtimes"):
        read_project_python_version(tmp_path)


def test_explicit_binding_selects_one_of_multiple_runtime_directories(
    tmp_path: Path,
) -> None:
    (tmp_path / ".runtime" / "python312").mkdir(parents=True)
    (tmp_path / ".runtime" / "python313").mkdir(parents=True)
    write_project_python_version(tmp_path, "3.13")

    assert read_project_python_version(tmp_path) == "3.13"
    assert project_runtime_directory(tmp_path, "3.13").endswith(
        str(Path(".runtime") / "python313")
    )


def test_missing_project_python_binding_has_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="choose a Python runtime"):
        read_project_python_version(tmp_path)
