"""Transactional engine-version migration for Infernux Hub projects."""

from __future__ import annotations

import datetime as _datetime
import os
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from hub_utils import is_frozen
from project_paths import inspect_existing_project
from project_python_runtime import (
    project_runtime_settings_path,
    write_project_python_version,
)


@dataclass(frozen=True)
class MigrationResult:
    project_path: str
    source_version: str
    target_version: str
    backup_path: str


class ProjectMigrationService:
    """Upgrade a project's pinned engine and runtime with rollback support."""

    _SKIP_DIRS = {".git", ".infernux-backups", ".runtime", ".venv", "Library", "Logs"}

    def __init__(self, project_model, version_manager) -> None:
        self.project_model = project_model
        self.version_manager = version_manager

    def migrate(self, project_path: str, target_version: str, *, on_status=None) -> MigrationResult:
        info = inspect_existing_project(project_path)
        source_version = info.engine_version
        target_version = str(target_version or "").strip()
        if not target_version:
            raise RuntimeError("A target engine version is required.")
        if target_version == source_version:
            raise RuntimeError(f"The project already uses Infernux {target_version}.")
        if self.version_manager is None or not self.version_manager.is_installed(target_version):
            raise RuntimeError(f"Infernux {target_version} is not installed in Hub.")
        target_python = self.version_manager.python_version_for_engine(target_version)
        if not target_python:
            raise RuntimeError(
                f"Infernux {target_version} has no valid Python ABI binding."
            )
        if is_frozen() and not self.project_model.runtime_manager.has_runtime(
            target_python
        ):
            raise RuntimeError(
                f"Infernux {target_version} requires Python {target_python}, which "
                "is not installed in Hub. Install that Python runtime first."
            )

        self._emit(on_status, "Creating project backup...")
        backup_path = self._create_backup(info.path, source_version, target_version)

        runtime_name = ".runtime" if is_frozen() else ".venv"
        runtime_path = os.path.join(info.path, runtime_name)
        saved_runtime = os.path.join(info.path, f".infernux-runtime-rollback-{uuid.uuid4().hex}")
        had_runtime = os.path.exists(runtime_path)
        pin_path = os.path.join(info.path, ".infernux-version")
        pin_bytes = Path(pin_path).read_bytes() if os.path.isfile(pin_path) else None
        requirements_path = os.path.join(info.path, "ProjectSettings", "requirements.txt")
        requirements_bytes = Path(requirements_path).read_bytes() if os.path.isfile(requirements_path) else None
        python_settings_path = str(project_runtime_settings_path(info.path))
        python_settings_bytes = (
            Path(python_settings_path).read_bytes()
            if os.path.isfile(python_settings_path)
            else None
        )

        if had_runtime:
            os.replace(runtime_path, saved_runtime)

        temp_requirements = requirements_path + f".migrate-{uuid.uuid4().hex}"
        try:
            self._emit(on_status, "Preparing target engine runtime...")
            write_project_python_version(info.path, target_python)
            self.project_model._create_project_runtime(info.path, on_status=on_status)
            self.project_model._install_infernux_in_runtime(
                info.path, target_version, on_status=on_status,
            )
            self.project_model.validate_project_runtime(info.path)

            self.project_model._copy_bundled_requirements(temp_requirements, target_version)
            if os.path.isfile(temp_requirements):
                os.replace(temp_requirements, requirements_path)

            self.version_manager.write_project_version(info.path, target_version)
            self.project_model._create_vscode_workspace(info.path)
            if had_runtime:
                self._remove_tree(saved_runtime)
        except Exception:
            self._remove_tree(temp_requirements)
            self._remove_tree(runtime_path)
            if had_runtime and os.path.exists(saved_runtime):
                os.replace(saved_runtime, runtime_path)
            self._restore_file(pin_path, pin_bytes)
            self._restore_file(requirements_path, requirements_bytes)
            self._restore_file(python_settings_path, python_settings_bytes)
            raise

        return MigrationResult(info.path, source_version, target_version, backup_path)

    @classmethod
    def _create_backup(cls, project_path: str, source: str, target: str) -> str:
        backup_dir = os.path.join(project_path, ".infernux-backups")
        os.makedirs(backup_dir, exist_ok=True)
        stamp = _datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_source = source or "unversioned"
        archive_path = os.path.join(backup_dir, f"{stamp}-{safe_source}-to-{target}.zip")
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for root, dirs, files in os.walk(project_path, followlinks=False):
                dirs[:] = [name for name in dirs if name not in cls._SKIP_DIRS]
                for filename in files:
                    full_path = os.path.join(root, filename)
                    if os.path.islink(full_path):
                        continue
                    relative = os.path.relpath(full_path, project_path)
                    archive.write(full_path, relative)
        return archive_path

    @staticmethod
    def _restore_file(path: str, content: bytes | None) -> None:
        if content is None:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        Path(path).write_bytes(content)

    @staticmethod
    def _remove_tree(path: str) -> None:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _emit(callback, message: str) -> None:
        if callback:
            callback(message)


__all__ = ["MigrationResult", "ProjectMigrationService"]
