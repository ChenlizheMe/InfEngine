"""Transactional InxPackage installation and project lifecycle authority."""

from __future__ import annotations

import importlib.util
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
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
    path_key,
    portable_path,
    relative_path,
    resolved_path,
    same_path,
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
from .cache import SharedPackageCache
from .package import (
    InxPackage,
    InxPackagePreview,
    PACKAGE_EXTENSION,
    PACKAGE_MANIFEST,
    REPOSITORY_PACKAGE_DIRECTORY,
    SOURCE_MANIFEST,
    current_meta_bytes,
    package_control_root,
    package_destination,
    validate_reference,
)
from .preload import PreloadManager
from .project_index import project_guid_paths
from .registry import PluginRegistry


_URL_PACKAGE_CHUNK_BYTES = 1024 * 1024
_InstallProgress = Callable[[str, float, str], None]
_GIT_PROGRESS_PERCENT = re.compile(r"(?<!\d)(\d{1,3})%(?!\d)")
_SOURCE_DESCRIPTOR_FIELDS = frozenset(
    {
        "type",
        "location",
        "reference",
        "revision",
        "subdirectory",
        "official",
        "builtin",
        "repository",
        "cache_scope",
        "cache_location",
        "release_tag",
        "release_url",
        "version",
        "acquisition",
        "commit",
        "source_snapshot",
    }
)


def _report_progress(
    callback: _InstallProgress | None,
    stage: str,
    progress: float,
    detail: str = "",
) -> None:
    if callback is not None:
        callback(
            str(stage),
            max(0.0, min(1.0, float(progress))),
            str(detail or ""),
        )


def _scaled_progress(
    callback: _InstallProgress | None,
    start: float,
    end: float,
) -> _InstallProgress | None:
    if callback is None:
        return None
    span = float(end) - float(start)
    return lambda stage, value, detail="": callback(
        stage,
        float(start) + span * float(value),
        detail,
    )


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
        if not self.runtime:
            from .platform_support import activate_android_support_environment

            activate_android_support_environment()
        self.official_catalog_error = ""
        self.python_requirement_error = ""
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
        self._deferred_catalog_changes: set[str] = set()

    def _package_cache(self) -> SharedPackageCache:
        return SharedPackageCache(
            staging_root=os.path.join(
                self.project_root, "Cache", "Plugins", ".staging"
            )
        )

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
            if not runtime:
                current._reconcile_python_requirements_for_startup()
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
            # An existing built-in package may be reloaded by
            # install_bundled_packages(). Its Python dependencies therefore
            # have to exist before that call; restoring them afterwards leaves
            # the current Editor process with a failed preload even if pip later
            # succeeds.
            manager._reconcile_python_requirements_for_startup()
            install_bundled_packages(normalized, manager=manager)
        # Bundled packages load their own lifecycle exactly once while being
        # synchronized.  Catch up the remaining project/plugin declarations
        # without unloading those live instances and importing them again.
        manager.preloads.catch_up()
        manager._rebuild_states()
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

    def catch_up_preloads(self) -> tuple[PreloadState, ...]:
        """Load lifecycles that entered the GUID catalog after startup preload.

        Called once the startup AssetDatabase refresh has committed, so
        scripts created without a ``.meta`` sidecar still preload on their
        first editor session instead of waiting for the next restart.
        """
        loaded = self.preloads.catch_up()
        if loaded:
            self._rebuild_states()
        return loaded

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
        content_root = os.path.join(self.project_root, "Assets", "Plugins")
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
        preview = InxPackage.inspect(package_path)
        compatibility = str(preview.metadata.get("engine", "")).strip()
        if compatibility and Version(ENGINE_VERSION) not in SpecifierSet(compatibility):
            raise RuntimeError(
                f"InxPackage requires Infernux {compatibility}, current engine is {ENGINE_VERSION}"
            )
        reference = validate_reference(str(preview.metadata["reference"]))
        from .platform_support import require_plugin_support

        require_plugin_support(reference)
        key = reference.casefold()
        if key in self._installing:
            raise RuntimeError(f"Circular plugin dependency: {reference}")
        current = self.registry.installed_record(reference)
        if current is not None:
            if _same_install(current, preview):
                return self.reload(reference)
            control = current.get("control", {})
            control_path = self._guid_index().get(str(control.get("guid", "")).casefold())
            if control_path:
                with open(control_path, "r", encoding="utf-8") as stream:
                    original = json.load(stream)
                if (
                    str(current.get("version", "")) == str(preview.metadata.get("version", ""))
                    and _package_file_identities(original.get("files", ()))
                    == _package_file_identities(preview.file_records)
                ):
                    return self._extend_package_install(current, preview, selected, progress)
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
                package_path,
                reference,
                str(preview.metadata.get("version", "")),
            )
            resolved_source = dict(source or {})
            resolved_source.setdefault("type", "local")
            resolved_source.setdefault("location", package_path)
            resolved_source["cache_location"] = cache_relative
            resolved_source["cache_scope"] = "hub"
            dependencies: list[str] = []
            if install_dependencies:
                _report_progress(progress, "resolve_dependencies", 0.48)
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
                        current_meta_bytes(str(control["guid"]), control_payload),
                    )
                file_records = [
                    {
                        "logical_path": item.logical_path,
                        "path_hint": item.destination_relative,
                        "guid": item.guid,
                        "role": item.role,
                        "owned": item.owned,
                    }
                    for item in planned
                ]
                control_record = {
                    "logical_path": PACKAGE_MANIFEST,
                    "path_hint": str(control["path_hint"]),
                    "guid": str(control["guid"]),
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
            self._acknowledge_installed_scripts(planned)
            if threading.current_thread() is threading.main_thread():
                _report_progress(progress, "refresh_assets", 0.90)
                self._refresh_editor_assets()
                self._publish_package_runtime_scripts(reference)
                _report_progress(progress, "preload_plugin", 0.95)
                state = self.reload(reference)
            else:
                state = PluginState(
                    reference=reference,
                    root=package_control_root(self.project_root, reference),
                )
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

    def _extend_package_install(
        self,
        current: Mapping[str, object],
        preview: InxPackagePreview,
        selected: Iterable[str] | None,
        progress: _InstallProgress | None,
    ) -> PluginState:
        """Add previously unchecked members of the same package, not an upgrade."""
        reference = str(current["reference"])
        installed = {str(item["logical_path"]) for item in current["files"]}
        selected_paths = None if selected is None else {
            portable_path(str(path)).strip("/") for path in selected
        }
        additions = tuple(
            str(item["logical_path"])
            for item in preview.file_records
            if str(item["logical_path"]) not in installed
            and (
                selected_paths is None
                or str(item["logical_path"]) in selected_paths
                or package_destination(reference, str(item["logical_path"])) in selected_paths
            )
        )
        if not additions:
            return self.reload(reference)

        self._installing.add(reference.casefold())
        try:
            planned, _control = self._plan_install(preview, additions)
            transaction = _InstallTransaction(self.project_root)
            registry_before = self.registry.load()
            registry_changed = False
            try:
                _report_progress(progress, "write_assets", 0.76)
                for item in planned:
                    if item.owned:
                        transaction.write(item.destination, item.payload)
                        transaction.write(item.destination + ".meta", item.meta_payload)
                document = self.registry.load()
                record = next(
                    item for item in document["installed"]
                    if str(item["reference"]).casefold() == reference.casefold()
                )
                record["files"].extend({
                    "logical_path": item.logical_path,
                    "path_hint": item.destination_relative,
                    "guid": item.guid,
                    "role": item.role,
                    "owned": item.owned,
                } for item in planned)
                record["transaction_id"] = transaction.id
                _report_progress(progress, "update_registry", 0.84)
                registry_changed = True
                self.registry.save(document)
                transaction.commit()
            except BaseException:
                transaction.rollback()
                if registry_changed:
                    self.registry.save(registry_before)
                raise
            self._acknowledge_installed_scripts(planned)
            if threading.current_thread() is threading.main_thread():
                self._refresh_editor_assets()
                self._publish_package_runtime_scripts(reference)
                state = self.reload(reference)
            else:
                state = PluginState(reference=reference, root=package_control_root(self.project_root, reference))
            _report_progress(progress, "complete", 1.0)
            return state
        finally:
            self._installing.discard(reference.casefold())

    @staticmethod
    def _acknowledge_installed_scripts(planned: Iterable[_PlannedFile]) -> None:
        """The install publication owns these writes, not their delayed echoes."""
        from Infernux.core.assets import AssetManager

        for item in planned:
            # Bare content scripts in Assets/Plugins still use the normal asset
            # watcher. Only package Editor/Runtime scripts are published here
            # (or by finalize_background_install on the editor thread).
            if (
                item.owned
                and item.role in {"editor", "runtime"}
                and item.destination.casefold().endswith(".py")
            ):
                for event in ("created", "modified", "deleted"):
                    AssetManager._suppress_watcher_echo(event, item.destination)

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
        if (
            str(descriptor.get("type", "")).strip().casefold() == "local"
            and descriptor.get("builtin") is True
        ):
            return self.install_source(
                descriptor,
                install_dependencies=install_dependencies,
                progress=progress,
            )
        cache_path = self.cached_reference_path(reference)
        if cache_path:
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

    def cached_reference_path(self, reference: str) -> str:
        record = self.registry.find(reference)
        if record is None:
            return ""
        cache = self._package_cache()
        source = record.get("source")
        if isinstance(source, Mapping):
            location = portable_path(
                str(source.get("cache_location", ""))
            ).strip("/")
            if location:
                candidate = resolved_path(
                    os.path.join(cache.root, *location.split("/"))
                )
                if os.path.isfile(candidate):
                    return candidate
        version = str(record.get("version", "")).strip()
        return cache.resolve(reference, version) if version else ""

    def download_reference(
        self,
        reference: str,
        *,
        force: bool = False,
        progress: _InstallProgress | None = None,
    ) -> dict[str, object]:
        """Download one registry package into the shared Hub library."""

        record = self.registry.find(reference)
        if record is None:
            raise KeyError(f"Plugin reference was not found: {reference}")
        cached = self.cached_reference_path(reference)
        if cached and not force:
            return {"reference": reference, "path": cached, "cached": True}
        source = record.get("source")
        if not isinstance(source, Mapping):
            raise ValueError(f"Plugin registry source is invalid: {reference}")
        descriptor = self._source_descriptor(source)
        descriptor.update(
            {
                key: value
                for key, value in source.items()
                if key not in {"cache_location", "cache_scope"}
            }
        )
        with self._package_cache().workspace("download") as workspace:
            package_path, acquired_source = self._materialize_source(
                descriptor,
                workspace,
                progress=progress,
            )
            preview = InxPackage.inspect(package_path)
            actual_reference = validate_reference(
                str(preview.metadata.get("reference", ""))
            )
            if actual_reference.casefold() != validate_reference(reference).casefold():
                raise RuntimeError(
                    f"Downloaded plugin reference mismatch: expected {reference}, "
                    f"found {actual_reference}"
                )
            compatibility = str(preview.metadata.get("engine", "")).strip()
            if compatibility and Version(ENGINE_VERSION) not in SpecifierSet(
                compatibility
            ):
                raise RuntimeError(
                    f"InxPackage requires Infernux {compatibility}, current engine "
                    f"is {ENGINE_VERSION}"
                )
            version = str(preview.metadata.get("version", ""))
            cache = self._package_cache()
            destination = cache.store(
                package_path,
                reference=actual_reference,
                version=version,
            )
            cached_source = dict(acquired_source)
            cached_source.update(
                {
                    "cache_scope": "hub",
                    "cache_location": cache.relative_path(actual_reference, version),
                }
            )
            self.registry.add_package(
                actual_reference,
                name=str(preview.metadata.get("name", record.get("name", ""))),
                version=str(preview.metadata.get("version", "")),
                engine=compatibility,
                dependencies=(),
                intro=str(preview.metadata.get("intro", record.get("intro", ""))),
                intros=preview.metadata.get("intros", record.get("intros", {})),
                pages=preview.metadata.get("pages", record.get("pages", ())),
                category=str(record.get("category", "Other")),
                targets=record.get("targets", ()),
                source=cached_source,
            )
            _report_progress(progress, "download_complete", 1.0)
            return {
                "reference": actual_reference,
                "path": destination,
                "cached": False,
            }

    def install_source(
        self,
        source: Mapping[str, object] | str,
        *,
        install_dependencies: bool = True,
        progress: _InstallProgress | None = None,
    ) -> PluginState:
        _report_progress(progress, "resolve_source", 0.04)
        descriptor = self._source_descriptor(source)
        descriptor.update(dict(source) if isinstance(source, Mapping) else {})
        with self._package_cache().workspace("source") as workspace:
            package_path, acquired_source = self._materialize_source(
                descriptor,
                workspace,
                progress=progress,
            )
            return self.install_package(
                package_path,
                install_dependencies=install_dependencies,
                source=acquired_source,
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

    def finalize_background_install(self, reference: str) -> PluginState:
        """Publish worker-written plugin assets and preload them on the editor thread."""

        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Plugin installation must be finalized on the editor thread")
        normalized = validate_reference(reference)
        self._refresh_editor_assets()
        self._publish_package_runtime_scripts(normalized)
        self.preloads.catch_up()
        self._deferred_catalog_changes.clear()
        self._rebuild_states()
        return self.states[normalized.casefold()]

    def _reconcile_python_requirements_for_startup(self) -> tuple[str, ...]:
        """Restore enabled plugin requirements before any plugin preload runs.

        The project registry travels with a project, while its Python environment
        may be recreated or moved to another host.  Installation-time dependency
        records therefore need to be checked against the active project runtime
        on every editor startup.
        """

        self.python_requirement_error = ""
        requirements_by_plugin: dict[str, tuple[str, ...]] = {}
        all_requirements: list[str] = []
        for record in self.registry.installed():
            if not bool(record.get("enabled", True)):
                continue
            reference = str(record.get("reference", "")).strip()
            requirements = tuple(
                str(item.get("requirement", "")).strip()
                for item in record.get("python_requirements", [])
                if isinstance(item, Mapping)
                and str(item.get("requirement", "")).strip()
            )
            if requirements:
                requirements_by_plugin[reference] = requirements
                all_requirements.extend(requirements)
        if not all_requirements:
            return ()

        try:
            executable = self._project_python_executable()
            before = self._python_environment_snapshot(executable)
            missing = tuple(
                dict.fromkeys(
                    requirement
                    for requirement in all_requirements
                    if not _requirements_satisfied((requirement,), before)
                )
            )
            if not missing:
                return ()
            self._run_pip_requirement_file(missing, executable=executable)
            after = self._python_environment_snapshot(executable)
            unresolved = tuple(
                requirement
                for requirement in all_requirements
                if not _requirements_satisfied((requirement,), after)
            )
            if unresolved:
                raise RuntimeError(
                    "pip completed but plugin requirements remain unresolved: "
                    + ", ".join(unresolved)
                )
            changes = _python_environment_changes(before, after)
            self.registry.record_python_reconciliation(
                requirements=missing,
                changes=changes,
                owners={
                    reference: _pip_requirement_targets(requirements)
                    for reference, requirements in requirements_by_plugin.items()
                },
            )
        except Exception as exc:
            self.python_requirement_error = str(exc)
            raise RuntimeError(
                "Installed plugin Python requirements could not be restored: "
                f"{exc}"
            ) from exc

        return tuple(
            reference
            for reference, requirements in requirements_by_plugin.items()
            if any(requirement in missing for requirement in requirements)
        )

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
        # When the project environment is the interpreter we are already
        # running in, enumerate installed distributions in-process instead of
        # spawning `python -m pip list` (which costs seconds on every editor
        # startup, under the "Preloading project plugins…" splash step).
        same_interpreter = same_path(python, sys.executable)
        if same_interpreter:
            import importlib.metadata

            snapshot: dict[str, str] = {}
            for distribution in importlib.metadata.distributions():
                try:
                    name = canonicalize_name(str(distribution.metadata["Name"] or ""))
                    version = str(distribution.version or "")
                except Exception:
                    continue
                if name and version and name not in snapshot:
                    snapshot[name] = version
            return snapshot
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
                self._retire_package_runtime_scripts(record)
            except BaseException:
                self.registry.set_enabled(reference, True)
                self._publish_package_runtime_scripts(reference)
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
        self._publish_package_runtime_scripts(reference)
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
        removed_caches: list[str] = []
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
                for cache in _script_bytecode_paths(path):
                    transaction.remove(cache)
                    removed_caches.append(cache)
                transaction.remove(path)
                transaction.remove(path + ".meta")
                removed.append(path)
            registry_changed = True
            self.registry.remove_install(reference)
            self._retire_package_runtime_scripts(record)
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
                self._publish_package_runtime_scripts(reference)
                self.reload(reference)
            except Exception as reload_error:
                Debug.log_suppressed("PluginManager.uninstall.rollback_reload", reload_error)
            if python_rollback_error is not None:
                raise RuntimeError(
                    "Plugin uninstall failed and Python environment rollback "
                    f"was incomplete: {python_rollback_error}"
                ) from uninstall_error
            raise
        self._prune_package_directories([*removed, *removed_caches])
        from Infernux.core.assets import AssetManager

        for path in removed:
            if path.endswith(".py") and is_path_within(
                path, os.path.join(self.project_root, "Packages"), allow_root=False
            ):
                # Lifecycle/runtime retirement was committed above. A delayed
                # delete echo must not restart a replacement package at this
                # reference (including a legacy Editor -> editor migration).
                AssetManager._suppress_watcher_echo("deleted", path)
        self.preloads.forget_package(reference)
        self._rebuild_states()
        self._refresh_editor_assets()
        return {**record, "removed_files": removed, "preserved_shared_files": preserved}

    def _materialize_source(
        self,
        source: Mapping[str, object],
        workspace: str,
        *,
        progress: _InstallProgress | None = None,
    ) -> tuple[str, dict[str, object]]:
        descriptor = dict(source)
        source_type = str(descriptor["type"])
        location = str(descriptor["location"])
        if source_type == "local":
            _report_progress(progress, "read_local_source", 0.16)
            local = resolved_path(
                location
                if os.path.isabs(location)
                else os.path.join(self.project_root, location)
            )
            return self._materialize_local(local, descriptor, workspace, progress)
        if source_type == "url":
            target = os.path.join(workspace, "download.inxpkg")
            _report_progress(progress, "download_package", 0.08)
            if progress is None:
                _download_url_package(location, target)
            else:
                _download_url_package(location, target, progress=progress)
            return target, descriptor
        revision = str(descriptor.get("revision", "")).strip()
        if source_type == "github":
            from .github_releases import (
                download_github_source,
                resolve_github_release,
            )

            _report_progress(progress, "resolve_releases", 0.06)
            released = resolve_github_release(
                location,
                workspace,
                expected_reference=str(descriptor.get("reference", "")),
                progress=progress,
            )
            if released is not None:
                released_source = dict(descriptor)
                released_source.update(
                    {
                        key: value
                        for key, value in released.source.items()
                        if key not in {"type", "location"}
                    }
                )
                released_source["acquisition"] = "github-release"
                return released.path, released_source

        subdirectory = portable_path(
            str(descriptor.get("subdirectory", ""))
        ).strip("/")
        if source_type == "github":
            _report_progress(progress, "download_source", 0.08)
            snapshot = download_github_source(
                location,
                workspace,
                revision=revision,
                subdirectory=subdirectory,
                progress=progress,
            )
            descriptor["commit"] = snapshot.commit
            descriptor["source_snapshot"] = True
            return self._materialize_local(
                snapshot.root,
                descriptor,
                workspace,
                progress,
            )

        checkout = os.path.join(workspace, "checkout")
        command = ["git", "clone", "--depth", "1", "--progress"]
        if subdirectory:
            command.extend(["--filter=blob:none", "--sparse"])
        if revision:
            command.extend(["--branch", revision])
        command.extend([location, checkout])
        _report_progress(progress, "clone_repository", 0.08)
        self._run_process_with_progress(
            command,
            stage="clone_repository",
            progress=progress,
            start=0.08,
            end=0.24,
        )
        if subdirectory:
            self._run_process(
                ["git", "sparse-checkout", "set", subdirectory],
                cwd=checkout,
            )
        commit = self._run_process(
            ["git", "rev-parse", "HEAD"], cwd=checkout
        ).stdout.strip()
        if len(commit) != 40 or any(
            character not in "0123456789abcdefABCDEF" for character in commit
        ):
            raise RuntimeError("Git plugin source did not resolve to a commit SHA")
        descriptor["commit"] = commit.casefold()
        descriptor["source_snapshot"] = True
        _report_progress(progress, "read_repository", 0.28)
        root = (
            resolved_path(os.path.join(checkout, *subdirectory.split("/")))
            if subdirectory
            else checkout
        )
        if not is_path_within(root, checkout, allow_root=True):
            raise ValueError("Git plugin subdirectory escapes the checkout")
        if not os.path.isdir(root):
            source_revision = revision or "the repository default branch"
            raise RuntimeError(
                f"Git plugin source {location} at {source_revision} does not contain "
                f"the package directory {subdirectory}"
            )
        return self._materialize_local(root, descriptor, workspace, progress)

    def _materialize_local(
        self,
        path: str,
        source: Mapping[str, object],
        workspace: str,
        progress: _InstallProgress | None,
    ) -> tuple[str, dict[str, object]]:
        if os.path.isfile(path):
            if not path.casefold().endswith(PACKAGE_EXTENSION):
                raise ValueError("A local plugin file must be an .inxpkg")
            return path, dict(source)
        if not os.path.isdir(path):
            raise FileNotFoundError(path)
        configured = portable_path(str(source.get("package", ""))).strip("/")
        if configured:
            package_path = resolved_path(os.path.join(path, *configured.split("/")))
            if not is_path_within(package_path, path, allow_root=False):
                raise ValueError("Plugin package path escapes the source root")
            if not os.path.isfile(package_path):
                raise FileNotFoundError(package_path)
            return package_path, dict(source)
        repository_root = os.path.join(path, REPOSITORY_PACKAGE_DIRECTORY)
        repository_manifest = os.path.isfile(os.path.join(repository_root, SOURCE_MANIFEST))
        if bool(source.get("source_snapshot")) and not repository_manifest:
            raise ValueError(
                "Repository plugin source must put distributable files under "
                "package/inx_package.json"
            )
        source_name = Path(path).name
        if bool(source.get("source_snapshot")):
            location_path = urlsplit(str(source.get("location", ""))).path.rstrip("/")
            source_name = Path(location_path).stem or source_name
        package = os.path.join(workspace, f"{source_name}.inxpkg")
        _report_progress(progress, "build_source_package", 0.28)
        InxPackage.export_source(path, package)
        return package, dict(source)

    def _install_requirements(
        self,
        preview: InxPackagePreview,
        *,
        progress: _InstallProgress | None = None,
    ) -> tuple[tuple[str, ...], _PipInstallEffect | None]:
        requirement_name = "requirements.txt"
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
                with self._package_cache().workspace("nested") as workspace:
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
            physical_relative = package_destination(
                reference, logical, project_root=self.project_root
            )
            destination = resolved_path(
                os.path.join(self.project_root, *physical_relative.split("/"))
            )
            payload = read_entry(preview.package_path, str(record["archive_path"]))
            meta_payload = read_entry(
                preview.package_path, str(record["meta_archive_path"])
            )
            owned, actual_destination = self._preflight_file(
                destination,
                str(record["guid"]),
                guid_index,
            )
            planned.append(
                _PlannedFile(
                    logical,
                    actual_destination,
                    portable_path(relative_path(actual_destination, self.project_root)),
                    str(record["guid"]),
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
        guid_index: Mapping[str, str],
    ) -> tuple[bool, str]:
        existing_guid_path = guid_index.get(guid.casefold())
        if existing_guid_path:
            return False, existing_guid_path
        if os.path.exists(destination):
            target_guid = _meta_guid(destination)
            if target_guid.casefold() != guid.casefold():
                raise PackageConflictError(
                    f"Destination is occupied by another GUID: {destination}"
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
        if self._installing:
            self._deferred_catalog_changes.add(resolved_path(file_path))
            return
        self.preloads.reload_path(file_path)
        self._rebuild_states()

    def _publish_package_runtime_scripts(self, reference: str) -> None:
        """Publish installed Runtime components through the resource authority."""

        record = self.registry.installed_record(reference)
        if record is None:
            raise KeyError(f"Plugin is not installed: {reference}")
        try:
            from Infernux.engine.resources_manager import ResourcesManager

            manager = ResourcesManager.instance()
        except ImportError:
            manager = None
        if manager is None:
            return
        if not same_path(getattr(manager, "_project_path", ""), self.project_root):
            raise RuntimeError(
                "Active ResourcesManager belongs to a different project during plugin install"
            )

        runtime_paths = self._package_runtime_script_paths(record)
        if not runtime_paths:
            return

        transaction_id = manager.begin_script_transaction(runtime_paths)
        submitted = False
        for path in runtime_paths:
            change = manager.submit_script_change(
                path,
                origin="editor",
                catalog_event="created",
                change_kind="created",
                transaction_id=transaction_id,
                force=True,
            )
            submitted = submitted or change is not None
        if submitted:
            manager.process_pending_reloads(force=True)

    def _retire_package_runtime_scripts(self, record: Mapping[str, object]) -> None:
        """Remove package gameplay types and modules from the active runtime."""

        try:
            from Infernux.engine.resources_manager import ResourcesManager

            manager = ResourcesManager.instance()
        except ImportError:
            manager = None
        if manager is None:
            return
        if not same_path(getattr(manager, "_project_path", ""), self.project_root):
            raise RuntimeError(
                "Active ResourcesManager belongs to a different project during plugin retirement"
            )
        paths = self._package_runtime_script_paths(record, retiring=True)
        if paths:
            manager.retire_script_paths(paths)

    def _package_runtime_script_paths(
        self, record: Mapping[str, object], *, retiring: bool = False
    ) -> list[str]:
        """Current Runtime scripts, plus historical module paths for retirement."""

        from Infernux.engine.project_context import package_script_reference

        guid_paths = self._guid_index()
        paths: dict[str, str] = {}
        if retiring:
            for item in record.get("files", []):
                if not isinstance(item, Mapping) or item.get("role") != "runtime":
                    continue
                hint = portable_path(str(item.get("path_hint", ""))).strip("/")
                if hint.casefold().endswith(".py"):
                    # Removed or moved files can still have live modules at
                    # their old paths. Never reinterpret the current Editor
                    # destination of that GUID as a gameplay module to retire.
                    path = resolved_path(os.path.join(self.project_root, *hint.split("/")))
                    paths[path_key(path)] = path
        reference = str(record["reference"])
        root = package_control_root(self.project_root, reference)
        for path in guid_paths.values():
            if not path.casefold().endswith(".py") or not is_path_within(path, root, allow_root=False):
                continue
            logical = portable_path(relative_path(path, root))
            if logical.partition("/")[0].casefold() != "runtime":
                continue
            current_reference = package_script_reference(path, self.project_root)
            # A nested package is independent. During uninstall the removed
            # control file may already be gone; the transaction's record still
            # defines which surviving, user-authored scripts must be retired.
            if current_reference and current_reference.casefold() != reference.casefold():
                continue
            paths[path_key(path)] = path
        return [paths[key] for key in sorted(paths)]

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

    def _prune_package_directories(self, removed_paths: Iterable[str]) -> None:
        boundaries = (
            os.path.join(self.project_root, "Packages"),
            os.path.join(self.project_root, "Assets", "Plugins"),
        )
        roots = {
            resolved_path(os.path.dirname(path))
            for path in removed_paths
            if path
        }
        for root in sorted(roots, key=lambda path: len(Path(path).parts), reverse=True):
            boundary = next(
                (
                    candidate
                    for candidate in boundaries
                    if is_path_within(root, candidate, allow_root=False)
                ),
                "",
            )
            if not boundary:
                continue
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
        return sys.executable

    def _cache_package(
        self,
        package_path: str,
        reference: str,
        version: str,
    ) -> tuple[str, str]:
        cache = self._package_cache()
        destination = cache.store(
            package_path,
            reference=reference,
            version=version,
        )
        return destination, cache.relative_path(reference, version)

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
        unknown = set(descriptor).difference(_SOURCE_DESCRIPTOR_FIELDS)
        if unknown:
            fields = ", ".join(sorted(map(str, unknown)))
            raise ValueError(f"Plugin source contains unknown fields: {fields}")
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

    def _run_process_with_progress(
        self,
        command: list[str],
        *,
        stage: str,
        progress: _InstallProgress | None,
        start: float,
        end: float,
        cwd: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if progress is None:
            return self._run_process(command, cwd=cwd)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        output: list[str] = []
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            output.append(line)
            matches = _GIT_PROGRESS_PERCENT.findall(line)
            fraction = int(matches[-1]) / 100.0 if matches else 0.0
            _report_progress(
                progress,
                stage,
                start + (end - start) * fraction,
                line,
            )
        return_code = process.wait()
        combined = "\n".join(output)
        if return_code:
            raise RuntimeError(f"Command failed ({return_code}): {combined[-4000:]}")
        return subprocess.CompletedProcess(command, return_code, combined, "")


def _script_bytecode_paths(source: str) -> tuple[str, ...]:
    """Derived caches of this owned source, never other author files."""

    if not source.endswith(".py"):
        return ()
    directory = os.path.join(os.path.dirname(source), "__pycache__")
    if not os.path.isdir(directory) or os.path.islink(directory):
        return ()
    result = []
    for entry in os.scandir(directory):
        if entry.is_symlink() or not entry.is_file() or not entry.name.endswith(".pyc"):
            continue
        try:
            cached_source = importlib.util.source_from_cache(entry.path)
        except ValueError:
            continue
        if same_path(cached_source, source):
            result.append(entry.path)
    return tuple(sorted(result))


class _InstallTransaction:
    def __init__(self, project_root: str) -> None:
        self.project_root = project_root
        self.id = uuid.uuid4().hex
        self.root = os.path.join(
            project_root, "Cache", "Plugins", ".transactions", self.id
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
    # Repository acquisition is an Editor/package-manager concern.  Keep the
    # HTTP client out of the module import closure so cooked runtimes can use
    # PluginManager startup/preload without shipping urllib.request.
    import urllib.request

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
                        _download_size_text(received, total),
                    )
        os.replace(partial, destination)
    finally:
        try:
            os.remove(partial)
        except FileNotFoundError:
            pass


def _download_size_text(received: int, total: int) -> str:
    def size(value: int) -> str:
        amount = float(value)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if amount < 1024.0 or unit == "GiB":
                return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
            amount /= 1024.0
        raise AssertionError("unreachable")

    return f"{size(received)} / {size(total)}" if total else size(received)


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


def _package_file_identities(files: Iterable[Mapping[str, object]]) -> set[tuple[str, str]]:
    return {
        (str(item.get("logical_path", "")), str(item.get("guid", "")))
        for item in files
        if isinstance(item, Mapping)
    }


def _same_install(record: Mapping[str, object], preview: InxPackagePreview) -> bool:
    current = _package_file_identities(record.get("files", []))
    incoming = _package_file_identities(preview.file_records)
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


__all__ = ["PackageConflictError", "PluginManager", "PluginState"]
