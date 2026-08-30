"""Transactional InxPackage installation and project lifecycle authority."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote, urlsplit

from packaging.specifiers import SpecifierSet
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from Infernux.debug import Debug
from Infernux.engine.path_utils import (
    is_path_within,
    portable_path,
    relative_path,
    resolved_path,
)
from Infernux.engine.player_package_native import read_entry
from Infernux.engine.python_abi import PYTHON_RUNTIME_DIRECTORY
from Infernux.version import ENGINE_VERSION

from .content import (
    discover_plugin_pages,
    localized_intro as select_plugin_intro,
    read_plugin_pages,
    resolve_plugin_page_asset,
)
from .package import (
    InxPackage,
    InxPackagePreview,
    PACKAGE_EXTENSION,
    PACKAGE_MANIFEST,
    SOURCE_MANIFEST,
    package_control_root,
    package_destination,
    validate_reference,
)
from .preload import PreloadManager
from .project_index import project_guid_paths
from .registry import PluginRegistry


_URL_PACKAGE_CHUNK_BYTES = 1024 * 1024
_InstallProgress = Callable[[str, float], None]


def _report_progress(
    callback: _InstallProgress | None,
    stage: str,
    progress: float,
) -> None:
    if callback is not None:
        callback(str(stage), max(0.0, min(1.0, float(progress))))


def _scaled_progress(
    callback: _InstallProgress | None,
    start: float,
    end: float,
) -> _InstallProgress | None:
    if callback is None:
        return None
    span = float(end) - float(start)
    return lambda stage, value: callback(stage, float(start) + span * float(value))


class PackageConflictError(RuntimeError):
    """Raised when installation would overwrite a different durable asset."""


@dataclass(slots=True)
class PluginState:
    reference: str
    root: str
    enabled: bool = True
    loaded: bool = False
    error: str = ""
    resources: dict[str, str] = field(default_factory=dict)
    lifecycle: tuple[dict[str, object], ...] = ()
    restart_required: bool = False

    def snapshot(self) -> dict[str, object]:
        return {
            "reference": self.reference,
            "root": self.root,
            "enabled": self.enabled,
            "loaded": self.loaded,
            "error": self.error,
            "resources": sorted(self.resources),
            "preload_instances": sum(
                1 for item in self.lifecycle if bool(item.get("loaded"))
            ),
            "lifecycle": list(self.lifecycle),
            "restart_required": self.restart_required,
        }


@dataclass(frozen=True, slots=True)
class _PlannedFile:
    logical_path: str
    destination: str
    destination_relative: str
    guid: str
    sha256: str
    role: str
    payload: bytes
    meta_payload: bytes
    owned: bool


@dataclass(slots=True)
class _PipInstallEffect:
    before: dict[str, str]
    after: dict[str, str]
    requirements: tuple[dict[str, str], ...]
    command: tuple[str, ...]
    output: str
    rolled_back: bool = False

    @property
    def changes(self) -> tuple[dict[str, str], ...]:
        names = sorted(set(self.before) | set(self.after))
        return tuple(
            {
                "name": name,
                "before": self.before.get(name, ""),
                "after": self.after.get(name, ""),
            }
            for name in names
            if self.before.get(name, "") != self.after.get(name, "")
        )


class PluginManager:
    """One project-scoped authority for InxPackage plugins and dependencies."""

    _instance: "PluginManager | None" = None

    def __init__(
        self, project_root: str, *, engine: Any = None, runtime: bool = False
    ) -> None:
        self.project_root = resolved_path(project_root)
        if not self.project_root:
            raise ValueError("PluginManager requires a project root")
        self.engine = engine
        self.runtime = bool(runtime)
        self.official_catalog_error = ""
        self.registry = PluginRegistry(self.project_root)
        self.preloads = PreloadManager(
            self.project_root,
            engine=engine,
            runtime=runtime,
            registry=self.registry,
        )
        self.states: dict[str, PluginState] = {}
        self._resource_manager = None
        self._installing: set[str] = set()

    @classmethod
    def instance(cls) -> "PluginManager | None":
        return cls._instance

    @classmethod
    def startup(
        cls, project_root: str, *, engine: Any = None, runtime: bool = False
    ) -> "PluginManager":
        current = cls._instance
        normalized = resolved_path(project_root)
        if (
            current is not None
            and current.project_root == normalized
            and current.runtime == bool(runtime)
        ):
            current.engine = engine
            current.preloads.engine = engine
            current.reload_all()
            return current
        if current is not None:
            current.shutdown()
        manager = cls(normalized, engine=engine, runtime=runtime)
        cls._instance = manager
        if not runtime:
            mirrored_resources = os.path.join(normalized, "Library", "Resources")
            from .official import (
                OfficialCatalogError,
                install_bundled_packages,
                sync_official_registry,
            )

            try:
                sync_official_registry(normalized, resources_root=mirrored_resources)
            except OfficialCatalogError as exc:
                manager.official_catalog_error = str(exc)
                Debug.log_warning(
                    "Official plugin catalog is unavailable; continuing with "
                    f"installed, local, Git, and pip sources: {exc}"
                )
            install_bundled_packages(normalized, manager=manager)
        manager.registry.save(manager.registry.load())
        manager.reload_all()
        if not runtime:
            manager._attach_resource_events()
        return manager

    def shutdown(self) -> None:
        if self._resource_manager is not None:
            self._resource_manager.unregister_script_catalog_callback(
                self._on_script_catalog_changed
            )
        self._resource_manager = None
        failures = self.preloads.unload_all()
        if failures:
            raise RuntimeError(
                "Plugin lifecycle shutdown failed; process restart required: "
                + "; ".join(state.error for state in failures)
            )
        self.states.clear()
        if PluginManager._instance is self:
            PluginManager._instance = None

    def reload_all(self) -> tuple[PluginState, ...]:
        self.preloads.reload_all()
        return self._rebuild_states()

    def _rebuild_states(self) -> tuple[PluginState, ...]:
        snapshots = self.preloads.snapshots()
        installed = self.registry.installed()
        self.states.clear()
        for record in installed:
            state = self._state_for_record(record, snapshots)
            self.states[state.reference.casefold()] = state
        return tuple(self.states.values())

    def reload(self, reference: str) -> PluginState:
        reference = validate_reference(reference)
        if self.registry.installed_record(reference) is None:
            raise KeyError(f"Plugin is not installed: {reference}")
        self.preloads.reload_package(reference)
        self._rebuild_states()
        return self.states[reference.casefold()]

    def content_pages(
        self,
        record: Mapping[str, object],
        *,
        locale: str | None = None,
    ) -> tuple[dict[str, str], ...]:
        reference = str(record.get("reference", ""))
        if locale is None:
            try:
                from Infernux.engine.i18n import get_locale

                selected_locale = get_locale()
            except ImportError:
                selected_locale = "en"
        else:
            selected_locale = locale
        try:
            control_root = package_control_root(self.project_root, reference)
            descriptors = record.get("pages", [])
            if not descriptors:
                descriptors = list(discover_plugin_pages(control_root))
            pages = read_plugin_pages(
                control_root,
                descriptors,
                locale=selected_locale,
            )
        except (OSError, ValueError):
            pages = ()
        if pages:
            return pages
        intro = select_plugin_intro(record, selected_locale)
        if not intro:
            return ()
        return (
            {
                "id": "intro",
                "title": "Description",
                "path": "",
                "format": "text",
                "content": intro,
            },
        )

    def content_asset_path(
        self,
        record: Mapping[str, object],
        page: Mapping[str, object],
        source: str,
    ) -> str:
        reference = str(record.get("reference", ""))
        try:
            control_root = package_control_root(self.project_root, reference)
            path = resolve_plugin_page_asset(
                control_root, str(page.get("path", "")), source
            )
            if path:
                return path
        except ValueError:
            pass
        content_root = os.path.join(
            self.project_root, "Assets", "Plugins", *reference.split("/")
        )
        try:
            return resolve_plugin_page_asset(
                content_root, str(page.get("path", "")), source
            )
        except ValueError:
            return ""

    def install_package(
        self,
        package_path: str,
        *,
        selected: Iterable[str] | None = None,
        install_dependencies: bool = True,
        source: Mapping[str, object] | None = None,
        progress: _InstallProgress | None = None,
    ) -> PluginState:
        _report_progress(progress, "inspect_package", 0.36)
        package_path = resolved_path(package_path)
        expected_sha256 = str((source or {}).get("sha256", "")).strip().casefold()
        package_sha256 = _file_hash(package_path)
        if expected_sha256 and package_sha256 != expected_sha256:
            raise RuntimeError(f"InxPackage source hash mismatch: {package_path}")
        preview = InxPackage.inspect(package_path)
        compatibility = str(preview.metadata.get("engine", "")).strip()
        if compatibility and Version(ENGINE_VERSION) not in SpecifierSet(compatibility):
            raise RuntimeError(
                f"InxPackage requires Infernux {compatibility}, current engine is {ENGINE_VERSION}"
            )
        reference = validate_reference(str(preview.metadata["reference"]))
        key = reference.casefold()
        if key in self._installing:
            raise RuntimeError(f"Circular plugin dependency: {reference}")
        current = self.registry.installed_record(reference)
        if current is not None:
            if _same_install(current, preview):
                return self.reload(reference)
            raise RuntimeError(
                f"Plugin is already installed with different content: {reference}; "
                "uninstall it before reinstalling"
            )
        installed_before = {
            str(item.get("reference", "")).casefold()
            for item in self.registry.installed()
        }
        pip_effect: _PipInstallEffect | None = None
        self._installing.add(key)
        try:
            cache_path, cache_relative = self._cache_package(
                package_path, package_sha256
            )
            resolved_source = dict(source or {})
            resolved_source.setdefault("type", "local")
            resolved_source.setdefault("location", package_path)
            resolved_source["sha256"] = package_sha256
            resolved_source["cache_location"] = cache_relative
            dependencies: list[str] = []
            if install_dependencies:
                _report_progress(progress, "resolve_dependencies", 0.48)
                dependencies.extend(
                    self._install_manifest_dependencies(
                        preview,
                        progress=_scaled_progress(progress, 0.48, 0.57),
                    )
                )
                requirement_dependencies, pip_effect = self._install_requirements(
                    preview,
                    progress=_scaled_progress(progress, 0.57, 0.66),
                )
                dependencies.extend(requirement_dependencies)
                dependencies = list(
                    dict.fromkeys(
                        str(value)
                        for value in dependencies
                        if str(value).casefold() != key
                    )
                )
            _report_progress(progress, "plan_assets", 0.68)
            planned, control = self._plan_install(preview, selected)
            transaction = _InstallTransaction(self.project_root)
            registry_before = self.registry.load()
            registry_changed = False
            try:
                _report_progress(progress, "write_assets", 0.76)
                for item in planned:
                    if not item.owned:
                        continue
                    transaction.write(item.destination, item.payload)
                    transaction.write(item.destination + ".meta", item.meta_payload)
                control_payload = (
                    json.dumps(preview.metadata, ensure_ascii=False, indent=2) + "\n"
                ).encode("utf-8")
                if bool(control["owned"]):
                    transaction.write(str(control["absolute_path"]), control_payload)
                    transaction.write(
                        str(control["absolute_path"]) + ".meta",
                        _minimal_meta(str(control["guid"])),
                    )
                file_records = [
                    {
                        "logical_path": item.logical_path,
                        "path_hint": item.destination_relative,
                        "guid": item.guid,
                        "sha256": item.sha256,
                        "role": item.role,
                        "owned": item.owned,
                    }
                    for item in planned
                ]
                control_record = {
                    "logical_path": PACKAGE_MANIFEST,
                    "path_hint": str(control["path_hint"]),
                    "guid": str(control["guid"]),
                    "sha256": hashlib.sha256(control_payload).hexdigest(),
                    "role": "control",
                    "owned": bool(control["owned"]),
                }
                _report_progress(progress, "update_registry", 0.84)
                registry_changed = True
                self.registry.record_install(
                    preview.metadata,
                    files=file_records,
                    control=control_record,
                    package_path=cache_path,
                    source=resolved_source,
                    dependencies=dependencies,
                    transaction_id=transaction.id,
                    package_sha256=package_sha256,
                    python_requirements=(
                        pip_effect.requirements if pip_effect else ()
                    ),
                    python_changes=(pip_effect.changes if pip_effect else ()),
                    python_install=(
                        {
                            "syntax": "requirements.txt",
                            "command": list(pip_effect.command),
                            "output": pip_effect.output[-4000:],
                        }
                        if pip_effect else None
                    ),
                )
                transaction.commit()
            except BaseException:
                transaction.rollback()
                if registry_changed:
                    self.registry.save(registry_before)
                self._rollback_pip_effect(pip_effect)
                raise
            _report_progress(progress, "refresh_assets", 0.90)
            self._refresh_editor_assets()
            _report_progress(progress, "preload_plugin", 0.95)
            state = self.reload(reference)
            _report_progress(progress, "complete", 1.0)
            return state
        except BaseException as install_error:
            self._rollback_pip_effect(pip_effect)
            rollback_errors = self._rollback_new_plugin_dependencies(
                installed_before,
                excluding=reference,
            )
            if rollback_errors:
                raise RuntimeError(
                    f"Plugin install failed and dependency rollback was incomplete for "
                    f"{reference}: {'; '.join(rollback_errors)}"
                ) from install_error
            raise
        finally:
            self._installing.discard(key)

    def _rollback_new_plugin_dependencies(
        self,
        installed_before: set[str],
        *,
        excluding: str,
    ) -> tuple[str, ...]:
        """Remove plugin dependencies introduced by one rejected install."""

        excluded = str(excluding).casefold()
        pending = {
            str(item.get("reference", "")).casefold(): str(
                item.get("reference", "")
            )
            for item in self.registry.installed()
            if str(item.get("reference", "")).casefold() not in installed_before
            and str(item.get("reference", "")).casefold() != excluded
        }
        failures: list[str] = []
        while pending:
            records = {
                str(item.get("reference", "")).casefold(): item
                for item in self.registry.installed()
                if str(item.get("reference", "")).casefold() in pending
            }
            removable = [
                key
                for key in pending
                if not any(
                    pending_key != key
                    and pending[key].casefold()
                    in {
                        str(value).casefold()
                        for value in records.get(pending_key, {}).get(
                            "dependencies", []
                        )
                    }
                    for pending_key in pending
                )
            ]
            if not removable:
                failures.append(
                    "dependency cycle prevented rollback: "
                    + ", ".join(sorted(pending.values()))
                )
                break
            for dependency_key in sorted(removable, reverse=True):
                dependency = pending.pop(dependency_key)
                try:
                    self.uninstall(dependency)
                except Exception as exc:
                    failures.append(
                        f"{dependency}: {type(exc).__name__}: {exc}"
                    )
        return tuple(failures)

    def install_reference(
        self,
        reference: str,
        *,
        install_dependencies: bool = True,
        progress: _InstallProgress | None = None,
    ) -> PluginState:
        record = self.registry.find(reference)
        if record is None:
            raise KeyError(
                f"Plugin reference was not found in the official registry: {reference}"
            )
        source = record.get("source")
        if not isinstance(source, Mapping):
            raise ValueError(f"Plugin registry source is invalid: {reference}")
        descriptor = dict(source)
        cache_location = portable_path(
            str(descriptor.get("cache_location", ""))
        ).strip("/")
        if cache_location:
            cache_path = resolved_path(
                os.path.join(self.project_root, *cache_location.split("/"))
            )
            if os.path.isfile(cache_path):
                cached = dict(descriptor)
                cached["type"] = "local"
                cached["location"] = cache_path
                return self.install_source(
                    cached,
                    install_dependencies=install_dependencies,
                    progress=progress,
                )
        return self.install_source(
            descriptor,
            install_dependencies=install_dependencies,
            progress=progress,
        )

    def install_source(
        self,
        source: Mapping[str, object] | str,
        *,
        install_dependencies: bool = True,
        progress: _InstallProgress | None = None,
    ) -> PluginState:
        _report_progress(progress, "resolve_source", 0.04)
        descriptor = self._source_descriptor(source)
        source_type = str(descriptor["type"])
        location = str(descriptor["location"])
        if source_type == "local":
            _report_progress(progress, "read_local_source", 0.16)
            local = resolved_path(
                location
                if os.path.isabs(location)
                else os.path.join(self.project_root, location)
            )
            return self._install_local(
                local,
                descriptor,
                install_dependencies=install_dependencies,
                progress=progress,
            )
        if source_type == "url":
            with tempfile.TemporaryDirectory(prefix="infernux-plugin-url-") as workspace:
                target = os.path.join(workspace, "download.inxpkg")
                _report_progress(progress, "download_package", 0.08)
                if progress is None:
                    _download_url_package(location, target)
                else:
                    _download_url_package(location, target, progress=progress)
                return self.install_package(
                    target,
                    install_dependencies=install_dependencies,
                    source=descriptor,
                    progress=progress,
                )
        with tempfile.TemporaryDirectory(prefix="infernux-plugin-git-") as workspace:
            command = ["git", "clone", "--depth", "1"]
            revision = str(descriptor.get("revision", "")).strip()
            if revision:
                command.extend(["--branch", revision])
            command.extend([location, workspace])
            _report_progress(progress, "clone_repository", 0.08)
            self._run_process(command)
            _report_progress(progress, "read_repository", 0.28)
            subdirectory = portable_path(str(descriptor.get("subdirectory", ""))).strip(
                "/"
            )
            root = (
                resolved_path(os.path.join(workspace, *subdirectory.split("/")))
                if subdirectory
                else workspace
            )
            if not is_path_within(root, workspace, allow_root=True):
                raise ValueError("Git plugin subdirectory escapes the checkout")
            package_path = portable_path(str(descriptor.get("package", ""))).strip("/")
            target = os.path.join(root, *package_path.split("/")) if package_path else root
            return self._install_local(
                target,
                descriptor,
                install_dependencies=install_dependencies,
                progress=progress,
            )

    def install_pip(
        self,
        syntax: str,
        *,
        progress: _InstallProgress | None = None,
    ) -> dict[str, object]:
        _report_progress(progress, "parse_pip", 0.08)
        raw = str(syntax or "").strip()
        if not raw:
            raise ValueError("pip syntax cannot be empty")
        tokens = [token.strip("\"'") for token in shlex.split(raw, posix=os.name != "nt")]
        lowered = [token.casefold() for token in tokens]
        if len(tokens) >= 2 and os.path.basename(lowered[0]).startswith("pip") and lowered[1] == "install":
            arguments = tokens[2:]
        elif len(tokens) >= 4 and lowered[1:4] == ["-m", "pip", "install"]:
            arguments = tokens[4:]
        elif lowered[:3] == ["-m", "pip", "install"]:
            arguments = tokens[3:]
        else:
            arguments = tokens
        if not arguments:
            raise ValueError("pip install syntax contains no arguments")
        executable = self._project_python_executable()
        _report_progress(progress, "inspect_python_environment", 0.22)
        before = self._python_environment_snapshot(executable)
        command = [executable, "-m", "pip", "install", *arguments]
        try:
            _report_progress(progress, "install_python_dependencies", 0.46)
            result = self._run_process(command, cwd=self.project_root)
            _report_progress(progress, "verify_python_environment", 0.86)
            after = self._python_environment_snapshot(executable)
            requirements = _pip_requirement_targets(arguments)
            changes = _python_environment_changes(before, after)
            self.registry.record_python_install(
                syntax=raw,
                command=command,
                output=result.stdout,
                requirements=arguments,
                dependency_requirements=requirements,
                changes=changes,
                owner="@project",
            )
        except BaseException as install_error:
            try:
                self._restore_python_environment(before, executable=executable)
            except Exception as rollback_error:
                raise RuntimeError(
                    "pip install failed and Python environment rollback was "
                    f"incomplete: {rollback_error}"
                ) from install_error
            raise
        _report_progress(progress, "complete", 1.0)
        return {
            "ok": True,
            "command": command,
            "output": result.stdout[-4000:],
        }

    def _install_pip_lines(
        self, lines: Iterable[str]
    ) -> _PipInstallEffect:
        values = tuple(str(line) for line in lines)
        executable = self._project_python_executable()
        before = self._python_environment_snapshot(executable)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", encoding="utf-8", delete=False
        ) as stream:
            stream.writelines(values)
            filtered = stream.name
        try:
            command = (executable, "-m", "pip", "install", "-r", filtered)
            result = self._run_process(list(command), cwd=self.project_root)
            after = self._python_environment_snapshot(executable)
            requirements = _pip_requirement_targets(values)
            return _PipInstallEffect(
                before,
                after,
                requirements,
                command,
                result.stdout,
            )
        except BaseException as install_error:
            try:
                self._restore_python_environment(before, executable=executable)
            except Exception as rollback_error:
                raise RuntimeError(
                    "Plugin requirement installation failed and Python environment "
                    f"rollback was incomplete: {rollback_error}"
                ) from install_error
            raise
        finally:
            try:
                os.remove(filtered)
            except FileNotFoundError:
                pass

    def _python_environment_snapshot(
        self, executable: str | None = None
    ) -> dict[str, str]:
        python = executable or self._project_python_executable()
        result = self._run_process(
            [python, "-m", "pip", "list", "--format=json"],
            cwd=self.project_root,
        )
        try:
            values = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("pip list returned invalid JSON") from exc
        if not isinstance(values, list):
            raise RuntimeError("pip list returned a non-list document")
        return {
            canonicalize_name(str(item.get("name", ""))): str(
                item.get("version", "")
            )
            for item in values
            if isinstance(item, Mapping)
            and str(item.get("name", "")).strip()
            and str(item.get("version", "")).strip()
        }

    def _rollback_pip_effect(self, effect: _PipInstallEffect | None) -> None:
        if effect is None or effect.rolled_back:
            return
        effect.rolled_back = True
        self._restore_python_environment(effect.before)

    def _restore_python_environment(
        self,
        baseline: Mapping[str, str],
        *,
        executable: str | None = None,
    ) -> None:
        python = executable or self._project_python_executable()
        current = self._python_environment_snapshot(python)
        added = sorted(set(current) - set(baseline))
        if added:
            self._run_process(
                [python, "-m", "pip", "uninstall", "-y", *added],
                cwd=self.project_root,
            )
        restore = [
            f"{name}=={version}"
            for name, version in sorted(baseline.items())
            if current.get(name) != version
        ]
        if restore:
            self._run_process(
                [python, "-m", "pip", "install", *restore],
                cwd=self.project_root,
            )

    def _release_python_dependencies(
        self, reference: str
    ) -> dict[str, str] | None:
        plan = self.registry.python_release_plan(reference)
        actionable = [
            item
            for item in plan
            if item.get("remaining_owners") or bool(item.get("managed", False))
        ]
        if not actionable:
            return None
        executable = self._project_python_executable()
        baseline = self._python_environment_snapshot(executable)
        remaining_requirements: list[str] = []
        uninstall: list[str] = []
        restore: list[str] = []
        for item in actionable:
            owners = item.get("remaining_owners", [])
            if owners:
                for owner in owners:
                    if isinstance(owner, Mapping):
                        remaining_requirements.extend(
                            str(value)
                            for value in owner.get("requirements", [])
                            if str(value).strip()
                        )
                continue
            if not bool(item.get("managed", False)):
                continue
            name = canonicalize_name(str(item.get("name", "")))
            previous = str(item.get("baseline_version", "")).strip()
            if previous:
                restore.append(f"{name}=={previous}")
            elif name:
                uninstall.append(name)
        try:
            if remaining_requirements and not _requirements_satisfied(
                remaining_requirements, baseline
            ):
                self._run_pip_requirement_file(
                    remaining_requirements,
                    executable=executable,
                )
            if uninstall:
                self._run_process(
                    [executable, "-m", "pip", "uninstall", "-y", *sorted(set(uninstall))],
                    cwd=self.project_root,
                )
            if restore:
                self._run_process(
                    [executable, "-m", "pip", "install", *sorted(set(restore))],
                    cwd=self.project_root,
                )
        except BaseException:
            self._restore_python_environment(baseline, executable=executable)
            raise
        return baseline

    def _run_pip_requirement_file(
        self, requirements: Iterable[str], *, executable: str
    ) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", encoding="utf-8", delete=False
        ) as stream:
            for requirement in requirements:
                stream.write(str(requirement).rstrip("\r\n") + "\n")
            path = stream.name
        try:
            self._run_process(
                [executable, "-m", "pip", "install", "-r", path],
                cwd=self.project_root,
            )
        finally:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def set_enabled(self, reference: str, enabled: bool) -> PluginState:
        reference = validate_reference(reference)
        record = self.registry.installed_record(reference)
        if record is None:
            raise KeyError(f"Plugin is not installed: {reference}")
        requested = bool(enabled)
        if bool(record.get("enabled", True)) == requested:
            return self.states.get(reference.casefold()) or self.reload(reference)
        installed = self.registry.installed()
        reference_key = reference.casefold()
        if not requested:
            blockers = [
                str(item.get("reference", ""))
                for item in installed
                if bool(item.get("enabled", True))
                and any(
                    str(dependency).casefold() == reference_key
                    for dependency in item.get("dependencies", [])
                )
            ]
            if blockers:
                raise RuntimeError(
                    f"Plugin {reference} is required by enabled plugins: "
                    + ", ".join(sorted(blockers))
                )
            failures = self.preloads.unload_package(reference)
            if failures:
                raise RuntimeError(
                    "Plugin lifecycle could not be stopped; restart required: "
                    + "; ".join(state.error for state in failures)
                )
            try:
                self.registry.set_enabled(reference, False)
            except BaseException:
                self.preloads.reload_package(reference)
                self._rebuild_states()
                raise
            self._rebuild_states()
            return self.states[reference_key]
        dependencies = {
            str(item.get("reference", "")).casefold(): item for item in installed
        }
        unavailable = [
            str(dependency)
            for dependency in record.get("dependencies", [])
            if str(dependency).casefold() not in dependencies
            or not bool(
                dependencies[str(dependency).casefold()].get("enabled", True)
            )
        ]
        if unavailable:
            raise RuntimeError(
                f"Plugin {reference} requires enabled plugins: "
                + ", ".join(sorted(unavailable))
            )
        self.registry.set_enabled(reference, True)
        return self.reload(reference)

    def uninstall(self, reference: str) -> dict[str, object]:
        reference = validate_reference(reference)
        record = self.registry.installed_record(reference)
        if record is None:
            raise KeyError(f"Plugin is not installed: {reference}")
        reference_key = reference.casefold()
        blockers = [
            str(item.get("reference", ""))
            for item in self.registry.installed()
            if any(
                str(dependency).casefold() == reference_key
                for dependency in item.get("dependencies", [])
            )
        ]
        if blockers:
            raise RuntimeError(
                f"Plugin {reference} is required by: {', '.join(sorted(blockers))}"
            )
        failures = self.preloads.unload_package(reference)
        if failures:
            raise RuntimeError(
                "Plugin lifecycle could not be stopped; uninstall aborted and restart required: "
                + "; ".join(state.error for state in failures)
            )
        python_baseline: dict[str, str] | None = None
        try:
            python_baseline = self._release_python_dependencies(reference)
        except BaseException:
            self.preloads.reload_package(reference)
            self._rebuild_states()
            raise
        removed: list[str] = []
        preserved: list[str] = []
        guid_index = self._guid_index()
        transaction = _InstallTransaction(self.project_root)
        registry_before = self.registry.load()
        registry_changed = False
        try:
            for item in [*record.get("files", []), record.get("control")]:
                if not isinstance(item, Mapping) or not bool(item.get("owned", True)):
                    continue
                guid = str(item.get("guid", "")).casefold()
                if self.registry.users_for_guid(guid, excluding=reference):
                    preserved.append(guid_index.get(guid, str(item.get("path_hint", ""))))
                    continue
                path = guid_index.get(guid)
                if not path:
                    continue
                expected_hash = str(item.get("sha256", ""))
                if _file_hash(path) != expected_hash:
                    preserved.append(path)
                    continue
                transaction.remove(path)
                transaction.remove(path + ".meta")
                removed.append(path)
            registry_changed = True
            self.registry.remove_install(reference)
            transaction.commit()
        except BaseException as uninstall_error:
            transaction.rollback()
            if registry_changed:
                self.registry.save(registry_before)
            python_rollback_error = None
            if python_baseline is not None:
                try:
                    self._restore_python_environment(python_baseline)
                except Exception as python_error:
                    python_rollback_error = python_error
            try:
                self.reload(reference)
            except Exception as reload_error:
                Debug.log_suppressed("PluginManager.uninstall.rollback_reload", reload_error)
            if python_rollback_error is not None:
                raise RuntimeError(
                    "Plugin uninstall failed and Python environment rollback "
                    f"was incomplete: {python_rollback_error}"
                ) from uninstall_error
            raise
        self._prune_package_directories(reference)
        self.preloads.forget_package(reference)
        self._rebuild_states()
        self._refresh_editor_assets()
        return {**record, "removed_files": removed, "preserved_modified_files": preserved}

    def _install_local(
        self,
        path: str,
        source: Mapping[str, object],
        *,
        install_dependencies: bool,
        progress: _InstallProgress | None = None,
    ) -> PluginState:
        if os.path.isfile(path):
            if not path.casefold().endswith(PACKAGE_EXTENSION):
                raise ValueError("A local plugin file must be an .inxpkg")
            return self.install_package(
                path,
                install_dependencies=install_dependencies,
                source=source,
                progress=progress,
            )
        if not os.path.isdir(path):
            raise FileNotFoundError(path)
        configured = portable_path(str(source.get("package", ""))).strip("/")
        if configured:
            return self.install_package(
                os.path.join(path, *configured.split("/")),
                install_dependencies=install_dependencies,
                source=source,
                progress=progress,
            )
        packages = sorted(Path(path).glob(f"*{PACKAGE_EXTENSION}"))
        if len(packages) == 1 and not os.path.isfile(os.path.join(path, SOURCE_MANIFEST)):
            return self.install_package(
                str(packages[0]),
                install_dependencies=install_dependencies,
                source=source,
                progress=progress,
            )
        if not os.path.isfile(os.path.join(path, SOURCE_MANIFEST)):
            raise ValueError("Plugin source must contain InxPackage.json or one .inxpkg")
        with tempfile.TemporaryDirectory(prefix="infernux-plugin-source-") as workspace:
            package = os.path.join(workspace, "Source.inxpkg")
            _report_progress(progress, "build_source_package", 0.28)
            InxPackage.export_source(path, package)
            return self.install_package(
                package,
                install_dependencies=install_dependencies,
                source=source,
                progress=progress,
            )

    def _install_requirements(
        self,
        preview: InxPackagePreview,
        *,
        progress: _InstallProgress | None = None,
    ) -> tuple[tuple[str, ...], _PipInstallEffect | None]:
        requirement_name = portable_path(
            str(preview.metadata.get("requirements", "requirements.txt"))
        ).strip("/")
        if not requirement_name:
            return (), None
        requirement_record = next(
            (
                item
                for item in preview.file_records
                if str(item.get("logical_path", "")) == requirement_name
            ),
            None,
        )
        if requirement_record is None:
            return (), None
        text = read_entry(
            preview.package_path, str(requirement_record["archive_path"])
        ).decode("utf-8")
        dependencies: list[str] = []
        pip_lines: list[str] = []
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                pip_lines.append(line)
                continue
            nested = self._nested_requirement(preview, requirement_name, stripped)
            if nested is not None:
                with tempfile.TemporaryDirectory(prefix="infernux-nested-package-") as workspace:
                    path = os.path.join(workspace, "nested.inxpkg")
                    Path(path).write_bytes(nested)
                    state = self.install_package(path, progress=progress)
                    dependencies.append(state.reference)
                continue
            reference = self._registry_reference_for_requirement(stripped)
            if reference is not None:
                self.install_reference(reference, progress=progress)
                dependencies.append(reference)
            else:
                pip_lines.append(line)
        effect = None
        if any(line.strip() and not line.lstrip().startswith("#") for line in pip_lines):
            _report_progress(progress, "install_python_dependencies", 0.58)
            effect = self._install_pip_lines(pip_lines)
        return tuple(dict.fromkeys(dependencies)), effect

    def _install_manifest_dependencies(
        self,
        preview: InxPackagePreview,
        *,
        progress: _InstallProgress | None = None,
    ) -> tuple[str, ...]:
        """Resolve the package dependency manifest through the plugin registry."""

        resolved: list[str] = []
        for value in preview.metadata.get("dependencies", []):
            reference = validate_reference(str(value))
            installed = self.registry.installed_record(reference)
            if installed is None:
                state = self.install_reference(reference, progress=progress)
                resolved.append(state.reference)
                continue
            installed_reference = str(installed.get("reference", reference))
            if not bool(installed.get("enabled", True)):
                self.set_enabled(installed_reference, True)
            resolved.append(installed_reference)
        unique: dict[str, str] = {}
        for value in resolved:
            unique.setdefault(value.casefold(), value)
        return tuple(unique.values())

    def _nested_requirement(
        self, preview: InxPackagePreview, requirement_name: str, requirement: str
    ) -> bytes | None:
        if requirement.startswith(("-e ", "--editable ")):
            return None
        raw = portable_path(requirement)
        if not raw.casefold().endswith(PACKAGE_EXTENSION):
            return None
        base = posixpath.dirname(requirement_name)
        logical = posixpath.normpath(posixpath.join(base, raw)).strip("/")
        if logical == ".." or logical.startswith("../"):
            raise ValueError(f"Nested InxPackage requirement escapes package: {requirement}")
        record = next(
            (
                item
                for item in preview.file_records
                if str(item.get("logical_path", "")) == logical
            ),
            None,
        )
        if record is None:
            raise FileNotFoundError(logical)
        return read_entry(preview.package_path, str(record["archive_path"]))

    def _registry_reference_for_requirement(self, requirement: str) -> str | None:
        candidate = requirement.removeprefix("inx:").strip()
        try:
            direct = self.registry.find(candidate)
        except ValueError:
            direct = None
        if direct is not None:
            return str(direct["reference"])
        lowered = candidate.casefold()
        if (
            candidate.startswith(("-", ".", "/", "\\"))
            or " @ " in candidate
            or lowered.startswith(("git+", "http://", "https://", "file:"))
            or lowered.endswith((".whl", ".tar.gz", ".zip", PACKAGE_EXTENSION))
            or (len(candidate) >= 2 and candidate[1] == ":")
        ):
            return None
        try:
            parsed = Requirement(candidate)
            if parsed.url or (parsed.marker is not None and not parsed.marker.evaluate()):
                return None
            name = parsed.name
        except Exception:
            parsed = None
            name = candidate.split(";", 1)[0].split("@", 1)[0].strip()
            for marker in ("===", "==", ">=", "<=", "~=", "!=", ">", "<", "["):
                name = name.split(marker, 1)[0].strip()
        matched = self.registry.find(name)
        if matched is not None and parsed is not None and parsed.specifier:
            version = str(matched.get("version", "")).strip()
            if version and not parsed.specifier.contains(version, prereleases=True):
                raise RuntimeError(
                    f"Official InxPackage {matched['reference']} {version} "
                    f"does not satisfy {parsed.specifier}"
                )
        return str(matched["reference"]) if matched is not None else None

    def _plan_install(
        self, preview: InxPackagePreview, selected: Iterable[str] | None
    ) -> tuple[list[_PlannedFile], dict[str, object]]:
        selected_set = None if selected is None else {
            portable_path(str(item)).strip("/") for item in selected
        }
        reference = str(preview.metadata["reference"])
        guid_index = self._guid_index()
        planned: list[_PlannedFile] = []
        for record in preview.file_records:
            logical = str(record["logical_path"])
            destination_relative = package_destination(reference, logical)
            if selected_set is not None and not (
                logical in selected_set or destination_relative in selected_set
            ):
                continue
            destination = resolved_path(
                os.path.join(self.project_root, *destination_relative.split("/"))
            )
            payload = read_entry(preview.package_path, str(record["archive_path"]))
            meta_payload = read_entry(
                preview.package_path, str(record["meta_archive_path"])
            )
            owned, actual_destination = self._preflight_file(
                destination,
                str(record["guid"]),
                str(record["sha256"]),
                guid_index,
            )
            planned.append(
                _PlannedFile(
                    logical,
                    actual_destination,
                    portable_path(relative_path(actual_destination, self.project_root)),
                    str(record["guid"]),
                    str(record["sha256"]),
                    str(record["role"]),
                    payload,
                    meta_payload,
                    owned,
                )
            )
        control_root = package_control_root(self.project_root, reference)
        control_path = os.path.join(control_root, PACKAGE_MANIFEST)
        control_guid = str(preview.metadata["control_guid"])
        manifest_payload = (
            json.dumps(preview.metadata, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        owned, actual_control = self._preflight_file(
            control_path,
            control_guid,
            hashlib.sha256(manifest_payload).hexdigest(),
            guid_index,
        )
        control = {
            "guid": control_guid,
            "owned": owned,
            "absolute_path": actual_control,
            "path_hint": portable_path(relative_path(actual_control, self.project_root)),
        }
        return planned, control

    def _preflight_file(
        self,
        destination: str,
        guid: str,
        digest: str,
        guid_index: Mapping[str, str],
    ) -> tuple[bool, str]:
        existing_guid_path = guid_index.get(guid.casefold())
        if existing_guid_path:
            if _file_hash(existing_guid_path) != digest:
                raise PackageConflictError(
                    f"GUID {guid} already exists with different content: {existing_guid_path}"
                )
            return False, existing_guid_path
        if os.path.exists(destination):
            target_guid = _meta_guid(destination)
            if target_guid.casefold() != guid.casefold():
                # Asset importers may have assigned a project-local identity to an
                # otherwise byte-identical package payload before the package was
                # installed (or while an older development package was detached).
                # Re-identifying that exact payload is safe and keeps the package
                # identity stable.  Different bytes remain a hard conflict so an
                # install can never claim or overwrite user-authored content.
                if _file_hash(destination) == digest:
                    return True, destination
                raise PackageConflictError(
                    f"Destination is occupied by another GUID: {destination}"
                )
            if _file_hash(destination) != digest:
                raise PackageConflictError(
                    f"Destination contains modified content: {destination}"
                )
            return False, destination
        return True, destination

    def _guid_index(self) -> dict[str, str]:
        try:
            result, _native = project_guid_paths(
                self.project_root,
                engine=self.engine,
            )
            for package in self.registry.installed():
                for item in [*package.get("files", []), package.get("control")]:
                    if not isinstance(item, Mapping):
                        continue
                    guid = str(item.get("guid", "")).casefold()
                    hint = portable_path(str(item.get("path_hint", ""))).strip("/")
                    if not guid or not hint or guid in result:
                        continue
                    path = resolved_path(
                        os.path.join(self.project_root, *hint.split("/"))
                    )
                    if os.path.isfile(path):
                        result[guid] = path
            return result
        except ValueError as exc:
            raise PackageConflictError(str(exc)) from exc

    def _state_for_record(
        self,
        record: Mapping[str, object],
        lifecycle: tuple[dict[str, object], ...],
    ) -> PluginState:
        reference = str(record.get("reference", ""))
        package_lifecycle = tuple(
            item
            for item in lifecycle
            if str(item.get("package_reference", "")).casefold()
            == reference.casefold()
        )
        errors = [str(item.get("error", "")) for item in package_lifecycle if item.get("error")]
        enabled = bool(record.get("enabled", True))
        resources = {
            str(item.get("logical_path", "")): resolved_path(
                os.path.join(
                    self.project_root,
                    *portable_path(str(item.get("path_hint", ""))).split("/"),
                )
            )
            for item in record.get("files", [])
            if isinstance(item, Mapping)
        }
        return PluginState(
            reference,
            f"Packages/{reference}",
            enabled,
            loaded=enabled and not errors,
            error="; ".join(errors),
            resources=resources,
            lifecycle=package_lifecycle,
            restart_required=any(
                bool(item.get("restart_required")) for item in package_lifecycle
            ),
        )

    def _attach_resource_events(self) -> None:
        try:
            from Infernux.engine.resources_manager import ResourcesManager

            manager = ResourcesManager.instance()
        except Exception:
            manager = None
        if manager is not None:
            manager.register_script_catalog_callback(self._on_script_catalog_changed)
            self._resource_manager = manager

    def _on_script_catalog_changed(self, file_path: str, event_type: str) -> None:
        if not str(file_path).lower().endswith(".py"):
            return
        self.preloads.reload_path(file_path)
        self._rebuild_states()

    def _refresh_editor_assets(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        database = getattr(self.engine, "get_asset_database", lambda: None)()
        if database is None:
            try:
                from Infernux.lib import AssetRegistry

                database = AssetRegistry.instance().get_asset_database()
            except Exception:
                database = None
        if database is not None:
            try:
                database.refresh()
            except Exception as exc:
                Debug.log_suppressed("PluginManager.refresh_assets", exc)

    def _prune_package_directories(self, reference: str) -> None:
        roots = [
            os.path.join(self.project_root, "Packages", *reference.split("/")),
            os.path.join(
                self.project_root, "Assets", "Plugins", *reference.split("/")
            ),
        ]
        boundaries = [
            os.path.join(self.project_root, "Packages"),
            os.path.join(self.project_root, "Assets", "Plugins"),
        ]
        for root, boundary in zip(roots, boundaries):
            current = root
            while is_path_within(current, boundary, allow_root=False):
                try:
                    os.rmdir(current)
                except OSError:
                    break
                current = os.path.dirname(current)

    def _project_python_executable(self) -> str:
        candidates = (
            os.path.join(
                self.project_root,
                ".runtime",
                PYTHON_RUNTIME_DIRECTORY,
                "python.exe",
            ),
            os.path.join(self.project_root, ".venv", "Scripts", "python.exe"),
            os.path.join(self.project_root, ".venv", "bin", "python"),
        )
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        if is_path_within(sys.executable, self.project_root, allow_root=False):
            return sys.executable
        raise RuntimeError(
            "Project Python environment is unavailable; create/install the project runtime before pip dependencies"
        )

    def _cache_package(self, package_path: str, digest: str) -> tuple[str, str]:
        relative = f"Library/InxPackageCache/{digest}{PACKAGE_EXTENSION}"
        destination = resolved_path(
            os.path.join(self.project_root, *relative.split("/"))
        )
        if not os.path.isfile(destination) or _file_hash(destination) != digest:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            temporary = destination + f".tmp.{uuid.uuid4().hex}"
            try:
                shutil.copy2(package_path, temporary)
                if _file_hash(temporary) != digest:
                    raise RuntimeError("InxPackage cache copy hash mismatch")
                os.replace(temporary, destination)
            finally:
                try:
                    os.remove(temporary)
                except FileNotFoundError:
                    pass
        return destination, relative

    def _source_descriptor(
        self, source: Mapping[str, object] | str
    ) -> dict[str, object]:
        if isinstance(source, str):
            value = source.strip()
            lower = value.casefold()
            if lower.startswith(("http://", "https://")) and lower.endswith(
                PACKAGE_EXTENSION
            ):
                source_type = "url"
            elif lower.startswith(("http://", "https://", "git@")) or lower.endswith(
                ".git"
            ):
                source_type = "github" if "github.com" in lower else "git"
            else:
                source_type = "local"
            return {"type": source_type, "location": value}
        descriptor = dict(source)
        source_type = str(descriptor.get("type", "")).strip().casefold()
        location = str(descriptor.get("location", "")).strip()
        if source_type not in {"local", "github", "git", "url"} or not location:
            raise ValueError(
                "Plugin source must be local, git, github, or url and include location"
            )
        descriptor["type"] = source_type
        descriptor["location"] = location
        return descriptor

    @staticmethod
    def _run_process(
        command: list[str], *, cwd: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"Command failed ({result.returncode}): {detail[-4000:]}"
            )
        return result


class _InstallTransaction:
    def __init__(self, project_root: str) -> None:
        self.project_root = project_root
        self.id = uuid.uuid4().hex
        self.root = os.path.join(
            project_root, "Library", "InxPackageStaging", self.id
        )
        self.backups: list[tuple[str, str | None]] = []
        self.committed = False
        os.makedirs(self.root, exist_ok=True)

    def write(self, destination: str, payload: bytes) -> None:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        backup = None
        if os.path.exists(destination):
            backup = os.path.join(self.root, f"backup-{len(self.backups)}")
            shutil.copy2(destination, backup)
        temporary = os.path.join(self.root, f"write-{len(self.backups)}")
        with open(temporary, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        self.backups.append((destination, backup))

    def remove(self, destination: str) -> None:
        if not os.path.exists(destination):
            return
        backup = os.path.join(self.root, f"backup-{len(self.backups)}")
        shutil.copy2(destination, backup)
        self.backups.append((destination, backup))
        os.remove(destination)

    def commit(self) -> None:
        self.committed = True
        shutil.rmtree(self.root, ignore_errors=True)

    def rollback(self) -> None:
        if self.committed:
            return
        for destination, backup in reversed(self.backups):
            if backup and os.path.isfile(backup):
                os.replace(backup, destination)
            else:
                try:
                    os.remove(destination)
                except FileNotFoundError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)


def _python_environment_changes(
    before: Mapping[str, str], after: Mapping[str, str]
) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "name": name,
            "before": str(before.get(name, "")),
            "after": str(after.get(name, "")),
        }
        for name in sorted(set(before) | set(after))
        if before.get(name, "") != after.get(name, "")
    )


def _download_url_package(
    location: str,
    destination: str,
    *,
    progress: _InstallProgress | None = None,
) -> None:
    request = urllib.request.Request(
        str(location),
        headers={"User-Agent": f"Infernux/{ENGINE_VERSION}"},
    )
    partial = f"{destination}.{uuid.uuid4().hex}.part"
    try:
        with urllib.request.urlopen(request) as response:
            total = int(response.headers.get("Content-Length", "0") or 0)
            received = 0
            with open(partial, "wb") as stream:
                while True:
                    chunk = response.read(_URL_PACKAGE_CHUNK_BYTES)
                    if not chunk:
                        break
                    stream.write(chunk)
                    received += len(chunk)
                    fraction = min(1.0, received / total) if total else 0.0
                    _report_progress(
                        progress,
                        "download_package",
                        0.08 + 0.22 * fraction,
                    )
        os.replace(partial, destination)
    finally:
        try:
            os.remove(partial)
        except FileNotFoundError:
            pass


def _pip_requirement_targets(
    values: Iterable[str],
) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in values:
        requirement = str(raw).strip()
        if not requirement or requirement.startswith("#"):
            continue
        candidate = requirement
        if candidate.startswith(("-e ", "--editable ")):
            candidate = candidate.split(None, 1)[1].strip()
        if candidate.startswith("-"):
            continue
        name = ""
        try:
            parsed = Requirement(candidate)
            if parsed.marker is not None and not parsed.marker.evaluate():
                continue
            name = parsed.name
        except Exception:
            marker = candidate.rsplit("#egg=", 1)
            if len(marker) == 2:
                name = marker[1].split("&", 1)[0].strip()
            else:
                basename = os.path.basename(
                    unquote(urlsplit(candidate).path or candidate)
                )
                if basename.casefold().endswith(".whl"):
                    try:
                        name = str(parse_wheel_filename(basename)[0])
                    except Exception:
                        name = ""
        canonical = canonicalize_name(name) if name else ""
        key = (canonical, requirement)
        if canonical and key not in seen:
            result.append({"name": canonical, "requirement": requirement})
            seen.add(key)
    return tuple(result)


def _requirements_satisfied(
    requirements: Iterable[str], environment: Mapping[str, str]
) -> bool:
    for value in requirements:
        targets = _pip_requirement_targets((value,))
        if len(targets) != 1:
            return False
        target = targets[0]
        version = environment.get(target["name"], "")
        if not version:
            return False
        try:
            parsed = Requirement(target["requirement"])
        except Exception:
            return False
        if parsed.specifier and not parsed.specifier.contains(
            version, prereleases=True
        ):
            return False
    return True


def _same_install(record: Mapping[str, object], preview: InxPackagePreview) -> bool:
    current = {
        (str(item.get("logical_path", "")), str(item.get("guid", "")), str(item.get("sha256", "")))
        for item in record.get("files", [])
        if isinstance(item, Mapping)
    }
    incoming = {
        (str(item.get("logical_path", "")), str(item.get("guid", "")), str(item.get("sha256", "")))
        for item in preview.file_records
    }
    return current == incoming and str(record.get("version", "")) == str(
        preview.metadata.get("version", "")
    )


def _meta_guid(asset_path: str) -> str:
    try:
        with open(asset_path + ".meta", "r", encoding="utf-8") as stream:
            document = json.load(stream)
        return str(document["metadata"]["guid"]["value"]).strip()
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return ""


def _file_hash(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _minimal_meta(guid: str) -> bytes:
    return (
        json.dumps(
            {"metadata": {"guid": {"type": "string", "value": guid}}},
            ensure_ascii=False,
            indent=4,
        )
        + "\n"
    ).encode("utf-8")


__all__ = ["PackageConflictError", "PluginManager", "PluginState"]
