"""Project-level Python ABI binding used by Infernux Hub."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from python_runtime_catalog import PythonRuntimeId


PROJECT_RUNTIME_SETTINGS = os.path.join(
    "ProjectSettings", "PythonRuntime.json"
)
_RUNTIME_DIRECTORY_PATTERN = re.compile(r"^python(\d)(\d{1,2})$")


def project_runtime_settings_path(project_dir: str | os.PathLike[str]) -> Path:
    return Path(project_dir) / PROJECT_RUNTIME_SETTINGS


def write_project_python_version(
    project_dir: str | os.PathLike[str], version: str | PythonRuntimeId
) -> str:
    runtime_id = PythonRuntimeId.parse(version)
    path = project_runtime_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "pythonVersion": runtime_id.series,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return runtime_id.series


def _read_explicit_binding(project_dir: str | os.PathLike[str]) -> str:
    path = project_runtime_settings_path(project_dir)
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schemaVersion") != 1:
            raise RuntimeError(
                f"Unsupported project Python runtime settings schema: {path}"
            )
        return PythonRuntimeId.parse(payload.get("pythonVersion", "")).series
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid project Python runtime settings: {path}") from exc


def _detect_private_runtime_binding(project_dir: str | os.PathLike[str]) -> str:
    runtime_root = Path(project_dir) / ".runtime"
    if not runtime_root.is_dir():
        return ""
    detected: set[str] = set()
    for child in runtime_root.iterdir():
        if not child.is_dir():
            continue
        match = _RUNTIME_DIRECTORY_PATTERN.fullmatch(child.name)
        if match is None:
            continue
        detected.add(f"{int(match.group(1))}.{int(match.group(2))}")
    if len(detected) > 1:
        versions = ", ".join(sorted(detected))
        raise RuntimeError(
            "This project contains multiple Python runtimes but has no explicit "
            f"PythonRuntime.json binding: {versions}."
        )
    return next(iter(detected), "")


def _detect_venv_binding(project_dir: str | os.PathLike[str]) -> str:
    config = Path(project_dir) / ".venv" / "pyvenv.cfg"
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() == "version":
            try:
                return PythonRuntimeId.parse(value.strip()).series
            except ValueError:
                return ""
    return ""


def read_project_python_version(
    project_dir: str | os.PathLike[str], *, required: bool = True
) -> str:
    version = (
        _read_explicit_binding(project_dir)
        or _detect_private_runtime_binding(project_dir)
        or _detect_venv_binding(project_dir)
    )
    if version or not required:
        return version
    raise RuntimeError(
        "The project does not declare a Python version and no existing project "
        "runtime could be identified. Open the project in Infernux Hub and choose "
        "a Python runtime before launching it."
    )


def project_runtime_directory(
    project_dir: str | os.PathLike[str], version: str | PythonRuntimeId
) -> str:
    runtime_id = PythonRuntimeId.parse(version)
    return os.path.join(str(project_dir), ".runtime", runtime_id.directory_name)


__all__ = [
    "PROJECT_RUNTIME_SETTINGS",
    "project_runtime_directory",
    "project_runtime_settings_path",
    "read_project_python_version",
    "write_project_python_version",
]
