"""Static discovery and GUID-keyed runtime for :class:`InxPreload`."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import os
import sys
import time
import types
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping

from Infernux.debug import Debug
from Infernux.engine.path_utils import (
    is_path_within,
    path_key,
    portable_path,
    relative_path,
    resolved_path,
)
from Infernux.lifecycle import InxPreload, PreloadContext

from .registry import PluginRegistry
from .project_index import project_guid_paths


_SCRIPT_NAMESPACE = uuid.UUID("90c16393-d740-4fa3-a45f-8003e2459753")
_TYPE_NAMESPACE = uuid.UUID("11a563a9-a178-45b4-b5bc-93c50b8da675")
_SKIPPED_DIRECTORIES = frozenset(
    {".git", "__pycache__", ".venv", "venv", "build", "dist", ".runtime"}
)


@dataclass(frozen=True, slots=True)
class _ClassDeclaration:
    path: str
    module: str
    name: str
    bases: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.module}.{self.name}"


@dataclass(slots=True)
class PreloadState:
    identity: str
    script_guid: str
    type_id: str
    type_name: str
    source_path: str
    package_reference: str = ""
    loaded: bool = False
    error: str = ""
    import_ms: float = 0.0
    preload_ms: float = 0.0
    unload_ms: float = 0.0
    module_name: str = ""
    module_names: tuple[str, ...] = ()
    restart_required: bool = False
    restart_reason: str = ""
    contribution_owner: str = ""
    instance: InxPreload | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "script_guid": self.script_guid,
            "type_id": self.type_id,
            "type_name": self.type_name,
            "source_path": self.source_path,
            "package_reference": self.package_reference,
            "loaded": self.loaded,
            "error": self.error,
            "import_ms": self.import_ms,
            "preload_ms": self.preload_ms,
            "unload_ms": self.unload_ms,
            "module_names": list(self.module_names),
            "restart_required": self.restart_required,
            "restart_reason": self.restart_reason,
            "contribution_owner": self.contribution_owner,
        }


class PreloadManager:
    """Project-scoped authority for early-import lifecycle candidates."""

    def __init__(
        self,
        project_root: str,
        *,
        engine: Any = None,
        runtime: bool = False,
        registry: PluginRegistry | None = None,
    ) -> None:
        self.project_root = resolved_path(project_root)
        if not self.project_root:
            raise ValueError("PreloadManager requires a project root")
        self.engine = engine
        self.runtime = bool(runtime)
        self.registry = registry or PluginRegistry(self.project_root)
        self.states: dict[str, PreloadState] = {}
        self.failures: dict[str, str] = {}
        self._ownership_cache: dict[str, dict[str, object]] | None = None
        self._declarations_by_path: dict[str, tuple[_ClassDeclaration, ...]] = {}
        self._declaration_stamps: dict[str, tuple[int, int]] = {}
        self._catalog_initialized = False

    def reload_all(self) -> tuple[PreloadState, ...]:
        failures = self.unload_all()
        # A failed unload can mean a live server, thread, callback, or native
        # object still owns the old module.  Never load a second instance on
        # top of it; expose the failed state until the owner can stop or the
        # process is restarted.
        if failures:
            return tuple(self.states.values())
        self._ownership_cache = None
        self.failures.clear()
        self._refresh_declaration_catalog()
        declarations = self._catalog_declarations()
        candidates = self._candidate_declarations(declarations)
        ordered_paths = self._ordered_candidate_paths(candidates)
        result: list[PreloadState] = []
        declarations_by_path: dict[str, set[str]] = {}
        for declaration in candidates:
            declarations_by_path.setdefault(path_key(declaration.path), set()).add(
                declaration.name
            )
        for path in ordered_paths:
            result.extend(
                self._load_path(path, declarations_by_path.get(path_key(path), set()))
            )
        return tuple(result)

    def reload_path(self, file_path: str) -> tuple[PreloadState, ...]:
        target = resolved_path(file_path)
        if not target or not is_path_within(target, self.project_root, allow_root=False):
            return ()
        if not self._catalog_initialized:
            self._refresh_declaration_catalog()
        normalized = path_key(target)
        previous = self._declarations_by_path.get(normalized, ())
        previous_candidates = self._candidate_declarations(
            self._catalog_declarations()
        )
        self.failures.pop(normalized, None)
        if os.path.isfile(target):
            try:
                current = _read_declarations(target, self.project_root)
            except (OSError, SyntaxError, ValueError) as exc:
                self.failures[normalized] = f"{type(exc).__name__}: {exc}"
                current = ()
            self._declarations_by_path[normalized] = current
            self._declaration_stamps[normalized] = _file_stamp(target)
        else:
            current = ()
            self._declarations_by_path.pop(normalized, None)
            self._declaration_stamps.pop(normalized, None)
        current_candidates = self._candidate_declarations(
            self._catalog_declarations()
        )
        package_reference = self._package_for_path(target)
        affected_paths = self._affected_candidate_paths(
            target,
            previous,
            current,
            previous_candidates,
            current_candidates,
        )
        if package_reference:
            affected_paths.update(
                path_key(item.path)
                for item in (*previous_candidates, *current_candidates)
                if self._package_for_path(item.path).casefold()
                == package_reference.casefold()
            )
        affected_states = [
            state
            for state in self.states.values()
            if path_key(state.source_path) in affected_paths
            or (
                package_reference
                and state.package_reference.casefold() == package_reference.casefold()
            )
        ]
        if not affected_states and not affected_paths:
            return ()
        for state in reversed(affected_states):
            if not self._unload_state(state):
                return (state,)
            self.states.pop(state.identity, None)
        candidates_by_path: dict[str, set[str]] = {}
        for declaration in current_candidates:
            key = path_key(declaration.path)
            if key in affected_paths:
                candidates_by_path.setdefault(key, set()).add(declaration.name)
        loaded: list[PreloadState] = []
        ordered = self._ordered_candidate_paths(
            item
            for item in current_candidates
            if path_key(item.path) in candidates_by_path
        )
        for path in ordered:
            loaded.extend(
                self._load_path(path, candidates_by_path[path_key(path)])
            )
        return tuple(loaded)

    def unload_all(self) -> tuple[PreloadState, ...]:
        failures: list[PreloadState] = []
        for state in reversed(tuple(self.states.values())):
            if self._unload_state(state):
                self.states.pop(state.identity, None)
            else:
                failures.append(state)
        return tuple(failures)

    def unload_package(self, reference: str) -> tuple[PreloadState, ...]:
        """Unload only lifecycle instances owned by one package."""

        key = str(reference).casefold()
        failures: list[PreloadState] = []
        targets = [
            state
            for state in self.states.values()
            if state.package_reference.casefold() == key
        ]
        for state in reversed(targets):
            if self._unload_state(state):
                self.states.pop(state.identity, None)
            else:
                failures.append(state)
        return tuple(failures)

    def reload_package(self, reference: str) -> tuple[PreloadState, ...]:
        """Refresh and reload the declaration slice owned by one package."""

        failures = self.unload_package(reference)
        if failures:
            return failures
        if not self._catalog_initialized:
            self._refresh_declaration_catalog()
        self._ownership_cache = None
        paths = self._package_source_paths(reference)
        active_keys = {path_key(path) for path in paths}
        for key, declarations in tuple(self._declarations_by_path.items()):
            path = declarations[0].path if declarations else key
            if key not in active_keys and self._path_belongs_to_package(
                path, reference
            ):
                self._declarations_by_path.pop(key, None)
                self._declaration_stamps.pop(key, None)
                self.failures.pop(key, None)
        for path in paths:
            key = path_key(path)
            self.failures.pop(key, None)
            try:
                self._declarations_by_path[key] = _read_declarations(
                    path, self.project_root
                )
                self._declaration_stamps[key] = _file_stamp(path)
            except (OSError, SyntaxError, ValueError) as exc:
                self._declarations_by_path[key] = ()
                try:
                    self._declaration_stamps[key] = _file_stamp(path)
                except OSError:
                    self._declaration_stamps.pop(key, None)
                self.failures[key] = f"{type(exc).__name__}: {exc}"
        candidates = [
            item
            for item in self._candidate_declarations(
                self._catalog_declarations()
            )
            if self._path_belongs_to_package(item.path, reference)
        ]
        expected: dict[str, set[str]] = {}
        for declaration in candidates:
            expected.setdefault(path_key(declaration.path), set()).add(
                declaration.name
            )
        loaded: list[PreloadState] = []
        for path in self._ordered_candidate_paths(candidates):
            loaded.extend(self._load_path(path, expected[path_key(path)]))
        return tuple(loaded)

    def forget_package(self, reference: str) -> None:
        """Drop cached declarations after a package has been removed."""

        for key, declarations in tuple(self._declarations_by_path.items()):
            path = declarations[0].path if declarations else key
            if self._path_belongs_to_package(path, reference):
                self._declarations_by_path.pop(key, None)
                self._declaration_stamps.pop(key, None)
                self.failures.pop(key, None)
        self._ownership_cache = None

    def snapshots(self) -> tuple[dict[str, object], ...]:
        return tuple(state.snapshot() for state in self.states.values())

    def _refresh_declaration_catalog(self) -> None:
        declarations: dict[str, tuple[_ClassDeclaration, ...]] = {}
        stamps: dict[str, tuple[int, int]] = {}
        for path in self._source_paths():
            key = path_key(path)
            try:
                stamp = _file_stamp(path)
                stamps[key] = stamp
                if self._declaration_stamps.get(key) == stamp:
                    declarations[key] = self._declarations_by_path.get(key, ())
                    continue
                declarations[key] = _read_declarations(path, self.project_root)
                self.failures.pop(key, None)
            except (OSError, SyntaxError, ValueError) as exc:
                declarations[key] = ()
                self.failures[key] = f"{type(exc).__name__}: {exc}"
        self._declarations_by_path = declarations
        self._declaration_stamps = stamps
        self._catalog_initialized = True

    def _catalog_declarations(self) -> list[_ClassDeclaration]:
        return [
            declaration
            for values in self._declarations_by_path.values()
            for declaration in values
        ]

    @staticmethod
    def _affected_candidate_paths(
        target: str,
        previous: Iterable[_ClassDeclaration],
        current: Iterable[_ClassDeclaration],
        previous_candidates: Iterable[_ClassDeclaration],
        current_candidates: Iterable[_ClassDeclaration],
    ) -> set[str]:
        old_values = list(previous)
        new_values = list(current)
        old_candidates = list(previous_candidates)
        new_candidates = list(current_candidates)
        target_key = path_key(target)
        old_keys = {item.key for item in old_candidates}
        new_keys = {item.key for item in new_candidates}
        affected = {
            target_key
            for item in (*old_values, *new_values)
            if item.key in old_keys or item.key in new_keys
        }
        symbols = {
            value
            for item in (*old_values, *new_values)
            for value in (item.key, item.name)
        }
        changed = True
        all_candidates = (*old_candidates, *new_candidates)
        while changed:
            changed = False
            for item in all_candidates:
                if path_key(item.path) in affected:
                    symbols.update((item.key, item.name))
                    continue
                if not any(
                    base in symbols or base.rsplit(".", 1)[-1] in symbols
                    for base in item.bases
                ):
                    continue
                affected.add(path_key(item.path))
                symbols.update((item.key, item.name))
                changed = True
        return affected

    @staticmethod
    def _candidate_declarations(
        declarations: Iterable[_ClassDeclaration],
    ) -> list[_ClassDeclaration]:
        values = list(declarations)
        by_key = {item.key: item for item in values}
        simple: dict[str, set[str]] = {}
        for item in values:
            simple.setdefault(item.name, set()).add(item.key)
        known = {"Infernux.lifecycle.InxPreload", "InxPreload"}
        selected: set[str] = set()
        changed = True
        while changed:
            changed = False
            for item in values:
                if item.key in selected:
                    continue
                resolved: set[str] = set()
                for base in item.bases:
                    resolved.add(base)
                    if "." not in base:
                        resolved.add(f"{item.module}.{base}")
                        if len(simple.get(base, ())) == 1:
                            resolved.update(simple[base])
                if not any(base in known or base in selected for base in resolved):
                    continue
                selected.add(item.key)
                known.add(item.key)
                changed = True
        return [item for item in values if item.key in selected]

    def _source_paths(self) -> Iterator[str]:
        guid_paths, native = project_guid_paths(
            self.project_root,
            engine=self.engine,
        )
        if native:
            candidates = {
                path_key(path): path
                for path in guid_paths.values()
                if path.casefold().endswith(".py")
            }
            for package in self.registry.installed():
                for item in package.get("files", []):
                    if not isinstance(item, Mapping):
                        continue
                    hint = portable_path(str(item.get("path_hint", ""))).strip("/")
                    if not hint.casefold().endswith(".py"):
                        continue
                    path = resolved_path(
                        os.path.join(self.project_root, *hint.split("/"))
                    )
                    if os.path.isfile(path):
                        candidates[path_key(path)] = path
            ownership = self._path_ownership()
            for path in sorted(candidates.values(), key=path_key):
                owner = ownership.get(path_key(path))
                if owner is not None and not bool(owner.get("enabled", True)):
                    continue
                if self.runtime and _is_editor_source(
                    path, self.project_root, owner
                ):
                    continue
                yield path
            return
        roots = [
            os.path.join(self.project_root, "Assets"),
            os.path.join(self.project_root, "Packages"),
        ]
        ownership = self._path_ownership()
        for root in roots:
            if not os.path.isdir(root):
                continue
            for walk_root, dirs, names in os.walk(root):
                dirs[:] = sorted(
                    name
                    for name in dirs
                    if name not in _SKIPPED_DIRECTORIES and not name.startswith(".")
                )
                for name in sorted(names):
                    if not name.endswith(".py") or name.startswith("."):
                        continue
                    path = os.path.join(walk_root, name)
                    owner = ownership.get(path_key(path))
                    if owner is not None and not bool(owner.get("enabled", True)):
                        continue
                    if self.runtime and _is_editor_source(path, self.project_root, owner):
                        continue
                    yield path

    def _package_source_paths(self, reference: str) -> tuple[str, ...]:
        record = self.registry.installed_record(reference)
        if record is None or not bool(record.get("enabled", True)):
            return ()
        guid_paths, _native = project_guid_paths(
            self.project_root,
            engine=self.engine,
        )
        result: dict[str, str] = {}
        for item in record.get("files", []):
            if not isinstance(item, Mapping):
                continue
            hint = portable_path(str(item.get("path_hint", ""))).strip("/")
            if not hint.casefold().endswith(".py"):
                continue
            guid = str(item.get("guid", "")).casefold()
            path = guid_paths.get(guid)
            if not path and hint:
                path = resolved_path(
                    os.path.join(self.project_root, *hint.split("/"))
                )
            if path and os.path.isfile(path):
                result[path_key(path)] = path
        return tuple(sorted(result.values(), key=path_key))

    def _path_belongs_to_package(self, path: str, reference: str) -> bool:
        if self._package_for_path(path).casefold() == str(reference).casefold():
            return True
        roots = (
            os.path.join(
                self.project_root, "Packages", *str(reference).split("/")
            ),
            os.path.join(
                self.project_root,
                "Assets",
                "Plugins",
                *str(reference).split("/"),
            ),
        )
        return any(is_path_within(path, root, allow_root=False) for root in roots)

    def _path_ownership(self) -> dict[str, dict[str, object]]:
        if self._ownership_cache is not None:
            return self._ownership_cache
        result: dict[str, dict[str, object]] = {}
        guid_paths, _native = project_guid_paths(
            self.project_root,
            engine=self.engine,
        )
        for package in self.registry.installed():
            for item in package.get("files", []):
                if not isinstance(item, Mapping):
                    continue
                guid = str(item.get("guid", "")).casefold()
                path = guid_paths.get(guid)
                if not path:
                    destination = str(item.get("path_hint", ""))
                    path = (
                        resolved_path(
                            os.path.join(
                                self.project_root,
                                *portable_path(destination).split("/"),
                            )
                        )
                        if destination
                        else ""
                    )
                if not path:
                    continue
                result[path_key(path)] = package
        self._ownership_cache = result
        return result

    def _package_for_path(self, path: str) -> str:
        owner = self._path_ownership().get(path_key(path))
        return str(owner.get("reference", "")) if owner is not None else ""

    def _ordered_candidate_paths(
        self, candidates: Iterable[_ClassDeclaration]
    ) -> tuple[str, ...]:
        unique = {path_key(item.path): item.path for item in candidates}
        ranks = self._package_ranks()
        return tuple(
            path
            for _key, path in sorted(
                unique.items(),
                key=lambda item: (
                    ranks.get(self._package_for_path(item[1]).casefold(), -1),
                    portable_path(relative_path(item[1], self.project_root)).casefold(),
                ),
            )
        )

    def _package_ranks(self) -> dict[str, int]:
        installed = {
            str(item.get("reference", "")).casefold(): item
            for item in self.registry.installed()
        }
        ranks: dict[str, int] = {}
        visiting: set[str] = set()

        def visit(key: str) -> int:
            if key in ranks:
                return ranks[key]
            if key in visiting:
                raise RuntimeError(f"Circular InxPackage dependency: {key}")
            visiting.add(key)
            record = installed.get(key, {})
            dependencies = [str(item).casefold() for item in record.get("dependencies", [])]
            rank = 1 + max((visit(item) for item in dependencies if item in installed), default=-1)
            visiting.remove(key)
            ranks[key] = rank
            return rank

        for key in installed:
            visit(key)
        return ranks

    def _load_path(self, path: str, expected_classes: set[str]) -> list[PreloadState]:
        script_guid = _ensure_script_guid(path, self.project_root)
        package_reference = self._package_for_path(path)
        # Package-owned lifecycle scripts keep their real import identity so
        # normal Python package semantics (including ``from . import ...``)
        # work during preload. Loose Assets scripts retain a GUID namespace to
        # avoid colliding with gameplay modules that share a filename.
        module_name = (
            _module_name(path, self.project_root)
            if package_reference
            else f"_infernux_preload_{script_guid}"
        )
        contribution_owner = f"preload:{script_guid}"
        modules_before = set(sys.modules)
        started = time.perf_counter()
        try:
            from Infernux.engine.ui.panel_registry import PanelRegistry

            with PanelRegistry.contribution_scope(contribution_owner):
                module = _load_module(
                    module_name, path, self.project_root, package_reference
                )
        except Exception as exc:
            try:
                PanelRegistry.remove_owner(contribution_owner)
            except Exception:
                pass
            self.failures[path_key(path)] = f"{type(exc).__name__}: {exc}"
            Debug.log_error(f"InxPreload import failed [{path}]: {exc}")
            return []
        import_ms = (time.perf_counter() - started) * 1000.0
        states: list[PreloadState] = []
        classes = sorted(
            (
                value
                for value in vars(module).values()
                if inspect.isclass(value)
                and value is not InxPreload
                and issubclass(value, InxPreload)
                and value.__module__ == module.__name__
                and not inspect.isabstract(value)
                and value.__name__ in expected_classes
            ),
            key=lambda value: value.__qualname__,
        )
        for preload_type in classes:
            type_id = uuid.uuid5(
                _TYPE_NAMESPACE, f"{script_guid}:{preload_type.__qualname__}"
            ).hex
            identity = f"{script_guid}:{type_id}"
            state = PreloadState(
                identity,
                script_guid,
                type_id,
                preload_type.__qualname__,
                path,
                package_reference,
                import_ms=import_ms,
                module_name=module_name,
                contribution_owner=contribution_owner,
            )
            try:
                instance = preload_type()
                context = PreloadContext(
                    self.project_root,
                    path,
                    script_guid,
                    type_id,
                    package_reference,
                    self.engine,
                    self.runtime,
                    lambda reason, target=state: _mark_restart_required(
                        target, reason
                    ),
                )
                started = time.perf_counter()
                with PanelRegistry.contribution_scope(contribution_owner):
                    with _temporary_import_paths(
                        path, self.project_root, package_reference
                    ):
                        instance.preload(context)
                state.preload_ms = (time.perf_counter() - started) * 1000.0
                state.instance = instance
                state.loaded = True
            except Exception as exc:
                state.error = f"{type(exc).__name__}: {exc}"
                Debug.log_error(
                    f"InxPreload.preload failed [{path}:{preload_type.__qualname__}]: {exc}"
                )
            self.states[identity] = state
            states.append(state)
        imported_project_modules = _new_project_modules(
            self.project_root, modules_before
        )
        native_modules = _new_native_modules(modules_before)
        for state in states:
            state.module_names = imported_project_modules
            if native_modules and not state.restart_required:
                _mark_restart_required(
                    state,
                    "Loaded native extension modules: " + ", ".join(native_modules),
                )
        if not classes:
            sys.modules.pop(module_name, None)
        return states

    def _unload_state(self, state: PreloadState) -> bool:
        if state.instance is not None:
            started = time.perf_counter()
            try:
                with _temporary_import_paths(
                    state.source_path,
                    self.project_root,
                    state.package_reference,
                ):
                    state.instance.unload()
            except Exception as exc:
                state.error = f"unload failed: {type(exc).__name__}: {exc}"
                _mark_restart_required(state, state.error)
                Debug.log_error(
                    f"InxPreload.unload failed [{state.identity}]: {exc}"
                )
                state.unload_ms = (time.perf_counter() - started) * 1000.0
                return False
            state.unload_ms = (time.perf_counter() - started) * 1000.0
        if state.contribution_owner:
            from Infernux.engine.ui.panel_registry import PanelRegistry

            if not PanelRegistry.remove_owner(state.contribution_owner):
                state.error = "unload failed: contributed editor panel refused to close"
                _mark_restart_required(state, state.error)
                return False
        state.instance = None
        state.loaded = False
        if state.module_name:
            sys.modules.pop(state.module_name, None)
        module_names = set(state.module_names)
        if state.package_reference:
            module_names.update(
                _package_module_names(
                    self.project_root, state.package_reference
                )
            )
        for name in reversed(sorted(module_names)):
            sys.modules.pop(name, None)
        return True


def _file_stamp(path: str) -> tuple[int, int]:
    stat = os.stat(path)
    return stat.st_mtime_ns, stat.st_size


def _read_declarations(path: str, project_root: str) -> tuple[_ClassDeclaration, ...]:
    with open(path, "r", encoding="utf-8") as stream:
        tree = ast.parse(stream.read(), filename=path)
    module = _module_name(path, project_root)
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            imported_module = _resolve_import_module(module, node.module or "", node.level)
            for alias in node.names:
                if alias.name == "*":
                    continue
                target = f"{imported_module}.{alias.name}" if imported_module else alias.name
                aliases[alias.asname or alias.name] = target
    result = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = tuple(
            value
            for base in node.bases
            if (value := _expression_name(base, aliases))
        )
        result.append(_ClassDeclaration(path, module, node.name, bases))
    return tuple(result)


def _expression_name(node: ast.AST, aliases: Mapping[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = _expression_name(node.value, aliases)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Subscript):
        return _expression_name(node.value, aliases)
    return ""


def _module_name(path: str, project_root: str) -> str:
    relative = portable_path(relative_path(path, project_root))
    parts = relative.split("/")
    if parts and parts[0] == "Assets":
        relative = "/".join(parts[1:])
    elif parts and parts[0] == "Packages":
        boundary = next(
            (
                index
                for index, part in enumerate(parts)
                if part in {"Runtime", "Editor"}
            ),
            -1,
        )
        if boundary >= 0:
            relative = "/".join(parts[boundary + 1 :])
    stem = relative.rsplit(".", 1)[0]
    parts = [part for part in stem.split("/") if part != "__init__"]
    if parts and all(part.isidentifier() for part in parts):
        return ".".join(parts)
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
    return f"infernux_project_script_{digest}"


def _resolve_import_module(current: str, target: str, level: int) -> str:
    if level <= 0:
        return target
    parts = current.split(".")[:-1]
    trim = max(0, level - 1)
    if trim:
        parts = parts[:-trim]
    if target:
        parts.extend(target.split("."))
    return ".".join(parts)


def _ensure_script_guid(path: str, project_root: str) -> str:
    meta_path = path + ".meta"
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as stream:
                document = json.load(stream)
            guid = str(document["metadata"]["guid"]["value"]).strip()
            if len(guid) == 32:
                return guid
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass
    relative = portable_path(relative_path(path, project_root))
    guid = uuid.uuid5(_SCRIPT_NAMESPACE, relative.casefold()).hex
    document = {"metadata": {"guid": {"type": "string", "value": guid}}}
    with open(meta_path, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=4)
        stream.write("\n")
    return guid


def _is_editor_source(
    path: str, project_root: str, owner: Mapping[str, object] | None
) -> bool:
    relative = portable_path(relative_path(path, project_root))
    if relative.startswith("Packages/"):
        if owner is None:
            return "/Editor/" in f"/{relative}/"
        for item in owner.get("files", []):
            if not isinstance(item, Mapping):
                continue
            if path_key(
                os.path.join(project_root, *portable_path(str(item.get("path_hint", ""))).split("/"))
            ) == path_key(path):
                return str(item.get("role", "")) == "editor"
    return False


def _load_module(
    module_name: str, path: str, project_root: str, package_reference: str
) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load InxPreload candidate: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        with _temporary_import_paths(path, project_root, package_reference):
            with open(path, "rb") as stream:
                code = compile(stream.read(), path, "exec")
            exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _new_project_modules(
    project_root: str, modules_before: set[str]
) -> tuple[str, ...]:
    result: list[str] = []
    for name in set(sys.modules) - modules_before:
        module = sys.modules.get(name)
        path = str(getattr(module, "__file__", "") or "")
        if path and is_path_within(path, project_root, allow_root=False):
            result.append(name)
    return tuple(sorted(result))


def _package_module_names(
    project_root: str, package_reference: str
) -> tuple[str, ...]:
    roots = (
        os.path.join(project_root, "Packages", *package_reference.split("/")),
        os.path.join(
            project_root,
            "Assets",
            "Plugins",
            *package_reference.split("/"),
        ),
    )
    result: list[str] = []
    for name, module in tuple(sys.modules.items()):
        path = str(getattr(module, "__file__", "") or "")
        if path and any(
            is_path_within(path, root, allow_root=False) for root in roots
        ):
            result.append(name)
    return tuple(sorted(result))


def _new_native_modules(modules_before: set[str]) -> tuple[str, ...]:
    suffixes = (".pyd", ".so", ".dylib")
    result = []
    for name in set(sys.modules) - modules_before:
        module = sys.modules.get(name)
        path = str(getattr(module, "__file__", "") or "").casefold()
        if path.endswith(suffixes):
            result.append(name)
    return tuple(sorted(result))


def _mark_restart_required(state: PreloadState, reason: str) -> None:
    state.restart_required = True
    state.restart_reason = str(reason)


@contextmanager
def _temporary_import_paths(
    path: str, project_root: str, package_reference: str
) -> Iterator[None]:
    candidates = [
        os.path.dirname(path),
        os.path.join(project_root, "Assets"),
        os.path.join(project_root, "Packages"),
    ]
    if package_reference:
        package_root = os.path.join(
            project_root, "Packages", *package_reference.split("/")
        )
        candidates.extend(
            [package_root, os.path.join(package_root, "Runtime"), os.path.join(package_root, "Editor")]
        )
    before = list(sys.path)
    try:
        for candidate in reversed(candidates):
            candidate = resolved_path(candidate)
            if candidate and os.path.isdir(candidate) and candidate not in sys.path:
                sys.path.insert(0, candidate)
        yield
    finally:
        sys.path[:] = before


__all__ = ["PreloadManager", "PreloadState"]
