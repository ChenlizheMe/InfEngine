"""Static, project-scoped dependency graph for Python scripts.

This module deliberately has no connection to the live script loader.  It is a
small foundation for R1.2: in the Editor it indexes only Python source files
under the project's ``Assets`` directory,
resolves the imports that can be resolved without executing user code, and
records everything else as an explicit external, unresolved, or dynamic edge.

The graph is mutable internally but every value crossing its public API is an
immutable dataclass or tuple.  A single re-entrant lock protects the complete
index, so readers never observe a half-updated module map.  R1.3 staged
transactions build a complete candidate state before the owner swaps it in.
"""

from __future__ import annotations

import ast
import hashlib
import heapq
import io
import keyword
import os
import sys
import threading
import tokenize
from dataclasses import dataclass, replace
from enum import Enum
from collections.abc import Mapping
from typing import Iterable, Optional, Union

from Infernux.engine.path_utils import (
    is_path_within,
    path_key,
    portable_path,
    relative_path,
    resolved_path,
)


PathLike = Union[str, os.PathLike[str]]


class ScriptDependencyGraphError(ValueError):
    """Base error raised for invalid project graph operations."""


class ModuleIdentityError(ScriptDependencyGraphError):
    """Raised when an Assets path cannot have a unique Python module identity."""


class StaleDependencyGraphTransaction(ScriptDependencyGraphError):
    """Raised when a staged transaction no longer targets the live revision."""


class DependencyGraphRollbackError(ScriptDependencyGraphError):
    """Raised when a transaction cannot safely restore its base graph state."""


class DependencyKind(str, Enum):
    """Classification of an import edge."""

    PROJECT = "project"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"
    DYNAMIC = "dynamic"


@dataclass(frozen=True, order=True)
class ModuleId:
    """Stable identity of one project-owned Python module."""

    project_id: str
    module_name: str
    path_key: str


@dataclass(frozen=True)
class DependencyEdge:
    """One import occurrence and its statically resolved target, if any."""

    source: ModuleId
    imported_name: str
    target: Optional[ModuleId]
    kind: DependencyKind
    import_form: str
    line: int
    column: int
    external_name: str = ""
    external_origin: str = ""
    dynamic: bool = False


@dataclass(frozen=True)
class ModuleRecord:
    """Immutable indexed description of one Assets Python file."""

    id: ModuleId
    source_path: str
    is_package: bool
    source_hash: str
    revision: int
    dependencies: tuple[DependencyEdge, ...] = ()
    parse_error: str = ""


@dataclass(frozen=True)
class DependencyGraphSnapshot:
    """Consistent immutable view of the complete graph."""

    revision: int
    modules: tuple[ModuleRecord, ...]
    edges: tuple[DependencyEdge, ...]


@dataclass(frozen=True)
class GraphMutation:
    """Result of an index mutation, including the reload closure it affects."""

    operation: str
    revision: int
    changed: tuple[ModuleId, ...] = ()
    removed: tuple[ModuleId, ...] = ()
    affected: tuple[ModuleId, ...] = ()


@dataclass(frozen=True)
class _GraphStateSnapshot:
    """Complete immutable graph state captured for one staged commit."""

    revision: int
    modules: tuple[tuple[str, ModuleRecord], ...]
    module_by_name: tuple[tuple[str, ModuleId], ...]
    sources: tuple[tuple[str, bytes], ...]
    raw_imports: tuple[tuple[str, tuple["_RawImport", ...]], ...]
    commit_token: Optional[object]


class StagedDependencyGraphTransaction:
    """Opaque, immutable candidate state owned by one dependency graph.

    The graph keeps the owner token and complete candidate snapshot private.
    Callers can inspect the immutable operation summary, but can only make the
    candidate live through :meth:`ScriptDependencyGraph.commit_transaction`.
    """

    __slots__ = (
        "_owner_token",
        "_base_revision",
        "_revision",
        "_upserts",
        "_removals",
        "_mutation",
        "_dependency_batches",
        "_ordered_modules",
        "_commit_token",
        "_base_snapshot",
        "_snapshot",
    )

    def __init__(
        self,
        owner_token: object,
        base_revision: int,
        revision: int,
        upserts: tuple[ModuleId, ...],
        removals: tuple[ModuleId, ...],
        mutation: GraphMutation,
        dependency_batches: tuple[tuple[ModuleId, ...], ...],
        commit_token: object,
        base_snapshot: _GraphStateSnapshot,
        snapshot: _GraphStateSnapshot,
    ) -> None:
        object.__setattr__(self, "_owner_token", owner_token)
        object.__setattr__(self, "_base_revision", base_revision)
        object.__setattr__(self, "_revision", revision)
        object.__setattr__(self, "_upserts", upserts)
        object.__setattr__(self, "_removals", removals)
        object.__setattr__(self, "_mutation", mutation)
        object.__setattr__(self, "_dependency_batches", dependency_batches)
        object.__setattr__(
            self,
            "_ordered_modules",
            tuple(module for batch in dependency_batches for module in batch),
        )
        object.__setattr__(self, "_commit_token", commit_token)
        object.__setattr__(self, "_base_snapshot", base_snapshot)
        object.__setattr__(self, "_snapshot", snapshot)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("StagedDependencyGraphTransaction is immutable")

    @property
    def base_revision(self) -> int:
        return self._base_revision

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def upserts(self) -> tuple[ModuleId, ...]:
        return self._upserts

    @property
    def removals(self) -> tuple[ModuleId, ...]:
        return self._removals

    @property
    def mutation(self) -> GraphMutation:
        return self._mutation

    @property
    def affected(self) -> tuple[ModuleId, ...]:
        return self._mutation.affected

    @property
    def strongly_connected_components(self) -> tuple[tuple[ModuleId, ...], ...]:
        """Affected candidate SCCs in dependency-first staging order."""

        return self._dependency_batches

    @property
    def dependency_batches(self) -> tuple[tuple[ModuleId, ...], ...]:
        """Affected candidate modules grouped into dependency-first batches."""

        return self._dependency_batches

    @property
    def ordered_modules(self) -> tuple[ModuleId, ...]:
        """Dependency-first modules with cyclic batches flattened stably."""

        return self._ordered_modules


@dataclass(frozen=True)
class _RawImport:
    form: str
    module: str
    names: tuple[str, ...]
    level: int
    line: int
    column: int
    dynamic: bool = False


def _valid_segment(value: str) -> bool:
    return bool(value) and value.isidentifier() and not keyword.iskeyword(value)


def _decode_source(payload: bytes) -> str:
    """Decode Python source using its PEP 263 encoding declaration."""

    encoding, _ = tokenize.detect_encoding(io.BytesIO(payload).readline)
    return payload.decode(encoding)


def _stdlib_root(name: str) -> bool:
    root = name.split(".", 1)[0]
    known = getattr(sys, "stdlib_module_names", frozenset())
    return root in known or root in {"__future__", "builtins"}


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.items: list[_RawImport] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.items.append(
                _RawImport(
                    "import",
                    alias.name,
                    (),
                    0,
                    node.lineno,
                    node.col_offset,
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.items.append(
            _RawImport(
                "from",
                node.module or "",
                tuple(alias.name for alias in node.names),
                int(node.level or 0),
                node.lineno,
                node.col_offset,
            )
        )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = ""
        function = node.func
        if isinstance(function, ast.Name) and function.id in {"__import__", "import_module"}:
            name = function.id
        elif isinstance(function, ast.Attribute) and function.attr in {"import_module", "__import__"}:
            name = function.attr
        if name:
            imported = ""
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                imported = node.args[0].value
            self.items.append(
                _RawImport(
                    "dynamic",
                    imported,
                    (),
                    0,
                    node.lineno,
                    node.col_offset,
                    True,
                )
            )
        self.generic_visit(node)


class ScriptDependencyGraph:
    """Thread-safe static dependency graph rooted at one project's Assets."""

    def __init__(self, project_root: PathLike, assets_root: Optional[PathLike] = None) -> None:
        project_path = resolved_path(project_root)
        assets_path = resolved_path(assets_root or os.path.join(project_path, "Assets"))
        self._project_root = project_path
        self._assets_root = assets_path
        self._project_id = path_key(project_path)
        self._lock = threading.RLock()
        self._owner_token = object()
        self._owner_thread_id: Optional[int] = None
        self._live_transaction_token: Optional[object] = None
        self._revision = 0
        self._modules: dict[str, ModuleRecord] = {}
        self._module_by_name: dict[str, ModuleId] = {}
        self._sources: dict[str, bytes] = {}
        self._raw_imports: dict[str, tuple[_RawImport, ...]] = {}

    @property
    def project_root(self) -> str:
        return self._project_root

    @property
    def assets_root(self) -> str:
        return self._assets_root

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def bind_owner_thread(self) -> None:
        """Bind live graph mutations to the calling thread.

        Staging and all read operations remain thread-safe and may run from
        worker threads.  Only operations that publish or replace the live
        graph use this binding.  The first explicit bind is also the default
        used by the first live mutation, which lets a watcher construct the
        graph while the editor later takes ownership on its main thread.
        """

        with self._lock:
            current = threading.get_ident()
            if self._owner_thread_id is None:
                self._owner_thread_id = current
                return
            if self._owner_thread_id != current:
                raise ScriptDependencyGraphError(
                    "live dependency graph mutations belong to the owner thread"
                )

    def snapshot(self) -> DependencyGraphSnapshot:
        with self._lock:
            modules = tuple(sorted(self._modules.values(), key=lambda item: item.id.module_name))
            edges = tuple(
                edge
                for module in modules
                for edge in module.dependencies
            )
            return DependencyGraphSnapshot(self._revision, modules, edges)

    def index_assets(self) -> GraphMutation:
        """Atomically index every Python file currently under Assets."""
        with self._lock:
            self._assert_live_mutation_thread_locked()
            paths: list[str] = []
            if os.path.isdir(self._assets_root):
                for directory, subdirectories, filenames in os.walk(self._assets_root):
                    subdirectories[:] = sorted(
                        name for name in subdirectories
                        if name != "__pycache__" and not name.startswith(".")
                    )
                    for filename in sorted(filenames):
                        # Editor dependency identity is source-based.  Never
                        # index bytecode beside its source counterpart.
                        if filename.endswith(".py"):
                            paths.append(os.path.join(directory, filename))
            paths.sort(key=path_key)

            staged: dict[str, tuple[ModuleRecord, bytes, tuple[_RawImport, ...]]] = {}
            names: dict[str, str] = {}
            next_revision = self._revision + 1
            for path in paths:
                display = resolved_path(path)
                key = path_key(display)
                with open(display, "rb") as stream:
                    payload = stream.read()
                record, raw = self._make_record(display, payload, next_revision)
                previous_key = names.get(record.id.module_name)
                if previous_key is not None and previous_key != key:
                    raise ModuleIdentityError(
                        f"module name collision for '{record.id.module_name}': "
                        f"'{staged[previous_key][0].source_path}' and '{display}'"
                    )
                names[record.id.module_name] = key
                staged[key] = (record, payload, raw)

            old_ids = {record.id for record in self._modules.values()}
            self._modules = {key: item[0] for key, item in staged.items()}
            self._sources = {key: item[1] for key, item in staged.items()}
            self._raw_imports = {key: item[2] for key, item in staged.items()}
            self._module_by_name = {record.id.module_name: record.id for record, _, _ in staged.values()}
            self._revision = next_revision
            self._live_transaction_token = None
            self._refresh_edges_locked()
            new_ids = {record.id for record in self._modules.values()}
            changed = tuple(sorted(old_ids | new_ids, key=self._module_sort_key))
            removed = tuple(sorted(old_ids - new_ids, key=self._module_sort_key))
            affected = self._affected_for_ids_locked(new_ids | old_ids)
            return GraphMutation("index", self._revision, changed, removed, affected)

    rebuild = index_assets

    def stage_transaction(
        self,
        upserts: Mapping[PathLike, Optional[bytes | str]]
        | Iterable[PathLike | tuple[PathLike, Optional[bytes | str]]],
        removals: Iterable[PathLike | ModuleId | str] | PathLike | ModuleId | str = (),
    ) -> StagedDependencyGraphTransaction:
        """Build a complete candidate graph without changing the live graph.

        ``upserts`` accepts a mapping of path to source, a collection of paths,
        or a collection of ``(path, source)`` pairs.  A source of ``None``
        reads the current file.  Removals are applied before upserts, so a
        rename can be represented by removing the old path and upserting the
        new path in one transaction.
        """

        return self._stage_transaction(upserts, removals, operation="transaction")

    def commit_transaction(self, transaction: StagedDependencyGraphTransaction) -> GraphMutation:
        """Atomically publish a transaction if it still targets this graph."""

        if not isinstance(transaction, StagedDependencyGraphTransaction):
            raise TypeError("transaction must be a StagedDependencyGraphTransaction")
        with self._lock:
            self._assert_live_mutation_thread_locked()
            if transaction._owner_token is not self._owner_token:
                raise ScriptDependencyGraphError("transaction belongs to another graph")
            if transaction.base_revision != self._revision:
                raise StaleDependencyGraphTransaction(
                    f"staged graph revision {transaction.base_revision} is stale; "
                    f"live revision is {self._revision}"
                )
            if not self._state_matches_snapshot_locked(transaction._base_snapshot):
                raise StaleDependencyGraphTransaction(
                    "live graph no longer matches the transaction base snapshot"
                )
            candidate = transaction._snapshot
            if candidate.commit_token is not transaction._commit_token:
                raise ScriptDependencyGraphError("transaction candidate token is invalid")
            self._restore_state_snapshot_locked(candidate)
            return transaction.mutation

    def rollback_transaction(
        self,
        transaction: StagedDependencyGraphTransaction,
    ) -> GraphMutation:
        """Atomically restore the base of the current committed transaction.

        Rollback is intentionally narrow: the transaction must belong to this
        graph, be the most recent commit, and the complete live state must
        still match its candidate snapshot.  It can therefore never overwrite
        a later graph mutation.
        """

        if not isinstance(transaction, StagedDependencyGraphTransaction):
            raise TypeError("transaction must be a StagedDependencyGraphTransaction")
        with self._lock:
            self._assert_live_mutation_thread_locked()
            if transaction._owner_token is not self._owner_token:
                raise DependencyGraphRollbackError("transaction belongs to another graph")
            if self._live_transaction_token is not transaction._commit_token:
                raise DependencyGraphRollbackError(
                    "transaction is not the current committed graph state"
                )
            if self._revision != transaction.revision:
                raise StaleDependencyGraphTransaction(
                    f"committed graph revision {transaction.revision} is stale; "
                    f"live revision is {self._revision}"
                )
            if not self._state_matches_snapshot_locked(transaction._snapshot):
                raise DependencyGraphRollbackError(
                    "live graph no longer matches the committed transaction"
                )

            base = transaction._base_snapshot
            candidate_modules = dict(transaction._snapshot.modules)
            base_modules = dict(base.modules)
            # ``changed`` describes the state restored into the live graph:
            # a modified candidate record is restored, and a candidate
            # deletion restores its base record.  ``removed`` is reserved for
            # candidate-only records that disappear during rollback.
            shared_keys = base_modules.keys() & candidate_modules.keys()
            changed = {
                base_modules[key].id
                for key in shared_keys
                if candidate_modules[key] != base_modules[key]
            }
            changed.update(
                base_modules[key].id
                for key in base_modules.keys() - candidate_modules.keys()
            )
            removed = {
                candidate_modules[key].id
                for key in candidate_modules.keys() - base_modules.keys()
            }
            self._restore_state_snapshot_locked(base)
            return GraphMutation(
                "rollback",
                base.revision,
                tuple(sorted(changed, key=self._module_sort_key)),
                tuple(sorted(removed, key=self._module_sort_key)),
                transaction.affected,
            )

    def abort_transaction(self, transaction: StagedDependencyGraphTransaction) -> None:
        """Discard a candidate; staged transactions own no live resources."""

        if not isinstance(transaction, StagedDependencyGraphTransaction):
            raise TypeError("transaction must be a StagedDependencyGraphTransaction")

    def _stage_transaction(
        self,
        upserts: Mapping[PathLike, Optional[bytes | str]]
        | Iterable[PathLike | tuple[PathLike, Optional[bytes | str]]],
        removals: Iterable[PathLike | ModuleId | str] | PathLike | ModuleId | str,
        *,
        operation: str,
    ) -> StagedDependencyGraphTransaction:
        upsert_specs = self._normalize_upserts(upserts)
        removal_specs = self._normalize_removals(removals)
        with self._lock:
            base_revision = self._revision
            base_snapshot = self._state_snapshot_locked()
            commit_token = object()
            next_revision = base_revision + 1
            old_modules = dict(self._modules)
            old_module_by_name = dict(self._module_by_name)

            modules = dict(old_modules)
            sources = dict(self._sources)
            raw_imports = dict(self._raw_imports)
            module_by_name = dict(old_module_by_name)
            removed_records: dict[str, ModuleRecord] = {}

            for selector in removal_specs:
                key = self._selector_key_for_maps(selector, modules, module_by_name)
                record = modules.get(key) if key else None
                if record is None:
                    continue
                removed_records.setdefault(key, record)
                modules.pop(key, None)
                sources.pop(key, None)
                raw_imports.pop(key, None)
                if module_by_name.get(record.id.module_name) == record.id:
                    module_by_name.pop(record.id.module_name, None)

            upsert_records: dict[str, ModuleRecord] = {}
            for path, source in upsert_specs:
                display = resolved_path(path)
                key = path_key(display)
                self._assert_asset_file(display)
                payload = self._read_source(display, source)
                record, raw = self._make_record(display, payload, next_revision)
                existing = module_by_name.get(record.id.module_name)
                if existing is not None and existing.path_key != key:
                    raise ModuleIdentityError(
                        f"module name collision for '{record.id.module_name}': "
                        f"'{modules[existing.path_key].source_path}' and '{display}'"
                    )
                previous = modules.get(key)
                if previous is not None and previous.id.module_name != record.id.module_name:
                    raise ModuleIdentityError(
                        f"path already belongs to module '{previous.id.module_name}': '{display}'"
                    )
                modules[key] = record
                sources[key] = payload
                raw_imports[key] = raw
                module_by_name[record.id.module_name] = record.id
                upsert_records[key] = record

            changed_ids = {record.id for record in upsert_records.values()}
            removed_ids = {record.id for record in removed_records.values()}
            has_changes = bool(changed_ids or removed_ids)
            candidate_revision = next_revision if has_changes else base_revision
            if has_changes and candidate_revision != next_revision:
                raise AssertionError("candidate revision must advance exactly once")
            refreshed = self._refresh_edges_for_maps(modules, module_by_name, raw_imports)
            modules = refreshed
            module_by_name = {
                record.id.module_name: record.id for record in modules.values()
            }

            seeds = changed_ids | removed_ids
            old_affected = self._affected_for_maps(old_modules, seeds)
            new_affected = self._affected_for_maps(modules, seeds)
            affected = tuple(
                sorted(set(old_affected) | set(new_affected), key=self._module_sort_key)
            )
            mutation = GraphMutation(
                operation,
                candidate_revision,
                tuple(sorted(changed_ids, key=self._module_sort_key)),
                tuple(sorted(removed_ids, key=self._module_sort_key)),
                affected,
            )
            dependency_batches = self._dependency_batches_for_maps(modules, affected)
            snapshot = _GraphStateSnapshot(
                candidate_revision,
                tuple(sorted(modules.items())),
                tuple(sorted(module_by_name.items())),
                tuple(sorted(sources.items())),
                tuple(sorted(raw_imports.items())),
                commit_token,
            )
            return StagedDependencyGraphTransaction(
                self._owner_token,
                base_revision,
                candidate_revision,
                tuple(sorted(changed_ids, key=self._module_sort_key)),
                tuple(sorted(removed_ids, key=self._module_sort_key)),
                mutation,
                dependency_batches,
                commit_token,
                base_snapshot,
                snapshot,
            )

    def _assert_live_mutation_thread_locked(self) -> None:
        """Reject live mutation from any thread other than the owner."""

        current = threading.get_ident()
        if self._owner_thread_id is None:
            self._owner_thread_id = current
            return
        if self._owner_thread_id != current:
            raise ScriptDependencyGraphError(
                "live dependency graph mutations belong to the owner thread"
            )

    def upsert(self, path: PathLike, source: Optional[bytes | str] = None) -> GraphMutation:
        """Insert or replace one Assets module and refresh all import edges."""
        transaction = self._stage_transaction((path, source), (), operation="upsert")
        return self.commit_transaction(transaction)

    def remove(self, path_or_module: PathLike | ModuleId) -> GraphMutation:
        """Remove one module; the returned mutation preserves its old identity."""
        transaction = self._stage_transaction((), (path_or_module,), operation="remove")
        return self.commit_transaction(transaction)

    def module_for_path(self, path: PathLike) -> Optional[ModuleRecord]:
        key = path_key(path)
        with self._lock:
            return self._modules.get(key)

    def module_for_name(self, module_name: str) -> Optional[ModuleRecord]:
        with self._lock:
            module_id = self._module_by_name.get(str(module_name))
            return self._modules.get(module_id.path_key) if module_id else None

    def resolve_module(
        self,
        module_name: str,
        *,
        from_module: Optional[ModuleId] = None,
        level: int = 0,
    ) -> Optional[ModuleId]:
        with self._lock:
            absolute = self._resolve_import_name_locked(module_name, from_module, level)
            return self._module_by_name.get(absolute)

    def dependencies_of(self, selector: PathLike | ModuleId | str) -> tuple[DependencyEdge, ...]:
        with self._lock:
            record = self._record_for_selector_locked(selector)
            return tuple(record.dependencies) if record else ()

    def dependents_of(self, selector: PathLike | ModuleId | str) -> tuple[ModuleId, ...]:
        with self._lock:
            record = self._record_for_selector_locked(selector)
            if record is None:
                return ()
            reverse = self._reverse_edges_locked()
            return tuple(sorted(reverse.get(record.id, ()), key=self._module_sort_key))

    def external_dependencies(
        self,
        selector: Optional[PathLike | ModuleId | str] = None,
    ) -> tuple[DependencyEdge, ...]:
        with self._lock:
            if selector is None:
                records = tuple(self._modules.values())
            else:
                record = self._record_for_selector_locked(selector)
                records = (record,) if record else ()
            return tuple(
                edge
                for record in sorted(records, key=lambda item: item.id.module_name)
                for edge in record.dependencies
                if edge.kind is not DependencyKind.PROJECT
            )

    def affected_closure(
        self,
        changed: Iterable[PathLike | ModuleId | str] | PathLike | ModuleId | str,
    ) -> tuple[ModuleId, ...]:
        with self._lock:
            values = (changed,) if isinstance(changed, (str, os.PathLike, ModuleId)) else tuple(changed)
            ids = {
                record.id
                for value in values
                if (record := self._record_for_selector_locked(value)) is not None
            }
            return self._affected_for_ids_locked(ids)

    def strongly_connected_components(self) -> tuple[tuple[ModuleId, ...], ...]:
        """Return deterministic Tarjan SCCs over project-to-project edges."""

        with self._lock:
            adjacency = self._project_adjacency_locked()
            index = 0
            indices: dict[ModuleId, int] = {}
            lowlinks: dict[ModuleId, int] = {}
            stack: list[ModuleId] = []
            on_stack: set[ModuleId] = set()
            components: list[tuple[ModuleId, ...]] = []

            def visit(node: ModuleId) -> None:
                nonlocal index
                indices[node] = index
                lowlinks[node] = index
                index += 1
                stack.append(node)
                on_stack.add(node)
                for target in adjacency.get(node, ()):
                    if target not in indices:
                        visit(target)
                        lowlinks[node] = min(lowlinks[node], lowlinks[target])
                    elif target in on_stack:
                        lowlinks[node] = min(lowlinks[node], indices[target])
                if lowlinks[node] == indices[node]:
                    component: list[ModuleId] = []
                    while True:
                        candidate = stack.pop()
                        on_stack.remove(candidate)
                        component.append(candidate)
                        if candidate == node:
                            break
                    components.append(tuple(sorted(component, key=self._module_sort_key)))

            for node in sorted(adjacency, key=self._module_sort_key):
                if node not in indices:
                    visit(node)
            return tuple(sorted(components, key=lambda item: self._module_sort_key(item[0])))

    def _make_record(
        self,
        display: str,
        payload: bytes,
        revision: int,
    ) -> tuple[ModuleRecord, tuple[_RawImport, ...]]:
        module_name, is_package = self._module_name_for_path(display)
        module_id = ModuleId(self._project_id, module_name, path_key(display))
        parse_error = ""
        raw: tuple[_RawImport, ...] = ()
        try:
            tree = ast.parse(_decode_source(payload), filename=display)
            collector = _ImportCollector()
            collector.visit(tree)
            raw = tuple(collector.items)
        except (SyntaxError, UnicodeError, ValueError) as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
        record = ModuleRecord(
            module_id,
            display,
            is_package,
            hashlib.sha256(payload).hexdigest(),
            revision,
            (),
            parse_error,
        )
        return record, raw

    def _state_snapshot_locked(self) -> _GraphStateSnapshot:
        return _GraphStateSnapshot(
            self._revision,
            tuple(sorted(self._modules.items())),
            tuple(sorted(self._module_by_name.items())),
            tuple(sorted(self._sources.items())),
            tuple(sorted(self._raw_imports.items())),
            self._live_transaction_token,
        )

    def _state_matches_snapshot_locked(self, snapshot: _GraphStateSnapshot) -> bool:
        return (
            self._revision == snapshot.revision
            and self._live_transaction_token is snapshot.commit_token
            and tuple(sorted(self._modules.items())) == snapshot.modules
            and tuple(sorted(self._module_by_name.items())) == snapshot.module_by_name
            and tuple(sorted(self._sources.items())) == snapshot.sources
            and tuple(sorted(self._raw_imports.items())) == snapshot.raw_imports
        )

    def _restore_state_snapshot_locked(self, snapshot: _GraphStateSnapshot) -> None:
        modules = dict(snapshot.modules)
        module_by_name = dict(snapshot.module_by_name)
        sources = dict(snapshot.sources)
        raw_imports = dict(snapshot.raw_imports)
        self._modules = modules
        self._module_by_name = module_by_name
        self._sources = sources
        self._raw_imports = raw_imports
        self._revision = snapshot.revision
        self._live_transaction_token = snapshot.commit_token

    @staticmethod
    def _normalize_upserts(
        upserts: Mapping[PathLike, Optional[bytes | str]]
        | Iterable[PathLike | tuple[PathLike, Optional[bytes | str]]],
    ) -> tuple[tuple[PathLike, Optional[bytes | str]], ...]:
        if isinstance(upserts, Mapping):
            values = tuple(upserts.items())
        elif isinstance(upserts, (str, os.PathLike)):
            values = (upserts,)
        elif (
            isinstance(upserts, tuple)
            and len(upserts) == 2
            and isinstance(upserts[0], (str, os.PathLike))
            and (upserts[1] is None or isinstance(upserts[1], (bytes, str)))
        ):
            values = (upserts,)
        else:
            values = tuple(upserts)

        normalized: list[tuple[PathLike, Optional[bytes | str]]] = []
        for item in values:
            if isinstance(item, (str, os.PathLike)):
                normalized.append((item, None))
                continue
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise TypeError("upserts must contain paths or (path, source) pairs")
            path, source = item
            if not isinstance(path, (str, os.PathLike)):
                raise TypeError("upsert path must be path-like")
            if source is not None and not isinstance(source, (bytes, str)):
                raise TypeError("upsert source must be bytes, str, or None")
            normalized.append((path, source))
        return tuple(normalized)

    @staticmethod
    def _normalize_removals(
        removals: Iterable[PathLike | ModuleId | str] | PathLike | ModuleId | str,
    ) -> tuple[PathLike | ModuleId | str, ...]:
        if isinstance(removals, (str, os.PathLike, ModuleId)):
            return (removals,)
        return tuple(removals)

    def _refresh_edges_for_maps(
        self,
        modules: dict[str, ModuleRecord],
        module_by_name: dict[str, ModuleId],
        raw_imports: dict[str, tuple[_RawImport, ...]],
    ) -> dict[str, ModuleRecord]:
        refreshed: dict[str, ModuleRecord] = {}
        for key, record in modules.items():
            edges: list[DependencyEdge] = []
            for raw in raw_imports.get(key, ()):
                edges.extend(
                    self._resolve_raw_import_locked(
                        record,
                        raw,
                        modules=modules,
                        module_by_name=module_by_name,
                    )
                )
            refreshed[key] = replace(record, dependencies=tuple(self._dedupe_edges(edges)))
        return refreshed

    def _refresh_edges_locked(self) -> None:
        self._modules = self._refresh_edges_for_maps(
            self._modules,
            self._module_by_name,
            self._raw_imports,
        )

    def _resolve_raw_import_locked(
        self,
        source: ModuleRecord,
        raw: _RawImport,
        *,
        modules: Optional[dict[str, ModuleRecord]] = None,
        module_by_name: Optional[dict[str, ModuleId]] = None,
    ) -> list[DependencyEdge]:
        modules = self._modules if modules is None else modules
        module_by_name = self._module_by_name if module_by_name is None else module_by_name
        if raw.form == "dynamic":
            if not raw.module:
                return [self._edge(source, "<dynamic>", None, DependencyKind.DYNAMIC, raw)]
            return self._resolve_named_edges(
                source,
                raw.module,
                raw,
                dynamic=True,
                module_by_name=module_by_name,
            )

        if raw.form == "import":
            return self._resolve_named_edges(
                source,
                raw.module,
                raw,
                module_by_name=module_by_name,
            )

        base = self._resolve_import_name_locked(
            raw.module,
            source.id,
            raw.level,
            modules=modules,
        )
        if not base:
            return [self._edge(source, raw.module, None, DependencyKind.UNRESOLVED, raw)]
        edges = self._resolve_named_edges(
            source,
            base,
            raw,
            dynamic=raw.dynamic,
            module_by_name=module_by_name,
        )
        for imported in raw.names:
            if imported == "*":
                continue
            child = f"{base}.{imported}" if base else imported
            target = module_by_name.get(child)
            if target is not None:
                edges.append(self._edge(source, child, target, DependencyKind.PROJECT, raw, dynamic=raw.dynamic))
        return edges

    def _resolve_named_edges(
        self,
        source: ModuleRecord,
        name: str,
        raw: _RawImport,
        *,
        dynamic: bool = False,
        module_by_name: Optional[dict[str, ModuleId]] = None,
    ) -> list[DependencyEdge]:
        module_by_name = self._module_by_name if module_by_name is None else module_by_name
        if not name:
            return [self._edge(source, name, None, DependencyKind.UNRESOLVED, raw, dynamic=dynamic)]
        targets = self._project_prefix_targets_locked(name, module_by_name=module_by_name)
        edges: list[DependencyEdge] = [
            self._edge(source, target.module_name, target, DependencyKind.DYNAMIC if dynamic else DependencyKind.PROJECT, raw, dynamic=dynamic)
            for target in targets
        ]
        exact = module_by_name.get(name)
        if exact is None and targets:
            edges.append(self._edge(source, name, None, DependencyKind.UNRESOLVED, raw, dynamic=dynamic))
        elif not targets:
            kind = DependencyKind.DYNAMIC if dynamic else DependencyKind.EXTERNAL
            edges.append(self._edge(source, name, None, kind, raw, dynamic=dynamic))
        return edges

    def _edge(
        self,
        source: ModuleRecord,
        imported_name: str,
        target: Optional[ModuleId],
        kind: DependencyKind,
        raw: _RawImport,
        *,
        dynamic: bool = False,
    ) -> DependencyEdge:
        external_name = (
            imported_name
            if target is None and kind in {DependencyKind.EXTERNAL, DependencyKind.DYNAMIC}
            else ""
        )
        origin = ""
        if external_name:
            root = external_name.split(".", 1)[0]
            origin = "engine" if root == "Infernux" else ("stdlib" if _stdlib_root(external_name) else "third_party")
        return DependencyEdge(
            source.id,
            imported_name,
            target,
            kind,
            raw.form,
            raw.line,
            raw.column,
            external_name,
            origin,
            dynamic or raw.dynamic,
        )

    def _resolve_import_name_locked(
        self,
        name: str,
        from_module: Optional[ModuleId],
        level: int,
        *,
        modules: Optional[dict[str, ModuleRecord]] = None,
    ) -> str:
        if level <= 0:
            return name
        if from_module is None:
            return ""
        modules = self._modules if modules is None else modules
        source = modules.get(from_module.path_key)
        if source is None:
            return ""
        package = source.id.module_name if source.is_package else source.id.module_name.rsplit(".", 1)[0] if "." in source.id.module_name else ""
        parts = [part for part in package.split(".") if part]
        if not parts:
            return ""
        trim = level - 1
        if trim > len(parts):
            return ""
        base = parts[: len(parts) - trim]
        if name:
            base.extend(name.split("."))
        return ".".join(base)

    def _project_prefix_targets_locked(
        self,
        name: str,
        *,
        module_by_name: Optional[dict[str, ModuleId]] = None,
    ) -> tuple[ModuleId, ...]:
        module_by_name = self._module_by_name if module_by_name is None else module_by_name
        targets: list[ModuleId] = []
        parts = name.split(".")
        for count in range(1, len(parts) + 1):
            target = module_by_name.get(".".join(parts[:count]))
            if target is not None:
                targets.append(target)
        return tuple(targets)

    def _project_adjacency_locked(self) -> dict[ModuleId, tuple[ModuleId, ...]]:
        adjacency: dict[ModuleId, set[ModuleId]] = {record.id: set() for record in self._modules.values()}
        for record in self._modules.values():
            for edge in record.dependencies:
                if edge.target is not None and edge.kind in {DependencyKind.PROJECT, DependencyKind.DYNAMIC}:
                    adjacency[record.id].add(edge.target)
        return {key: tuple(sorted(value, key=self._module_sort_key)) for key, value in adjacency.items()}

    def _dependency_batches_for_maps(
        self,
        modules: dict[str, ModuleRecord],
        affected: Iterable[ModuleId],
    ) -> tuple[tuple[ModuleId, ...], ...]:
        """Return candidate-resident affected SCCs in dependency-first order."""

        candidate_ids = {record.id for record in modules.values()}
        included = set(affected) & candidate_ids
        adjacency: dict[ModuleId, tuple[ModuleId, ...]] = {}
        for record in modules.values():
            if record.id not in included:
                continue
            dependencies = {
                edge.target
                for edge in record.dependencies
                if edge.target in included
                and edge.kind in {DependencyKind.PROJECT, DependencyKind.DYNAMIC}
            }
            adjacency[record.id] = tuple(sorted(dependencies, key=self._module_sort_key))

        index = 0
        indices: dict[ModuleId, int] = {}
        lowlinks: dict[ModuleId, int] = {}
        stack: list[ModuleId] = []
        on_stack: set[ModuleId] = set()
        components: list[tuple[ModuleId, ...]] = []

        def visit(node: ModuleId) -> None:
            nonlocal index
            indices[node] = index
            lowlinks[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)
            for dependency in adjacency.get(node, ()):
                if dependency not in indices:
                    visit(dependency)
                    lowlinks[node] = min(lowlinks[node], lowlinks[dependency])
                elif dependency in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[dependency])
            if lowlinks[node] != indices[node]:
                return
            component: list[ModuleId] = []
            while True:
                candidate = stack.pop()
                on_stack.remove(candidate)
                component.append(candidate)
                if candidate == node:
                    break
            components.append(tuple(sorted(component, key=self._module_sort_key)))

        for node in sorted(adjacency, key=self._module_sort_key):
            if node not in indices:
                visit(node)

        component_for: dict[ModuleId, int] = {
            module: component_index
            for component_index, component in enumerate(components)
            for module in component
        }
        dependents: dict[int, set[int]] = {
            component_index: set() for component_index in range(len(components))
        }
        dependency_count = [0] * len(components)
        for source, dependencies in adjacency.items():
            source_component = component_for[source]
            for dependency in dependencies:
                dependency_component = component_for[dependency]
                if source_component == dependency_component:
                    continue
                if source_component not in dependents[dependency_component]:
                    dependents[dependency_component].add(source_component)
                    dependency_count[source_component] += 1

        def component_sort_key(component_index: int) -> tuple[tuple[str, str], ...]:
            return tuple(self._module_sort_key(module) for module in components[component_index])

        ready = [
            (component_sort_key(component_index), component_index)
            for component_index, count in enumerate(dependency_count)
            if count == 0
        ]
        heapq.heapify(ready)
        ordered: list[tuple[ModuleId, ...]] = []
        while ready:
            _, component_index = heapq.heappop(ready)
            ordered.append(components[component_index])
            for dependent in sorted(
                dependents[component_index],
                key=component_sort_key,
            ):
                dependency_count[dependent] -= 1
                if dependency_count[dependent] == 0:
                    heapq.heappush(
                        ready,
                        (component_sort_key(dependent), dependent),
                    )

        if len(ordered) != len(components):
            raise AssertionError("SCC condensation graph must be acyclic")
        return tuple(ordered)

    def _reverse_edges_locked(self) -> dict[ModuleId, set[ModuleId]]:
        return self._reverse_edges_for_maps(self._modules)

    @staticmethod
    def _reverse_edges_for_maps(
        modules: dict[str, ModuleRecord],
    ) -> dict[ModuleId, set[ModuleId]]:
        reverse: dict[ModuleId, set[ModuleId]] = {}
        for record in modules.values():
            for edge in record.dependencies:
                if edge.target is not None and edge.kind in {DependencyKind.PROJECT, DependencyKind.DYNAMIC}:
                    reverse.setdefault(edge.target, set()).add(record.id)
        return reverse

    def _affected_for_ids_locked(self, seeds: Iterable[ModuleId]) -> tuple[ModuleId, ...]:
        return self._affected_for_maps(self._modules, seeds)

    def _affected_for_maps(
        self,
        modules: dict[str, ModuleRecord],
        seeds: Iterable[ModuleId],
    ) -> tuple[ModuleId, ...]:
        reverse = self._reverse_edges_for_maps(modules)
        result = set(seeds)
        pending = list(result)
        while pending:
            current = pending.pop()
            for dependent in reverse.get(current, ()):
                if dependent not in result:
                    result.add(dependent)
                    pending.append(dependent)
        return tuple(sorted(result, key=self._module_sort_key))

    def _module_name_for_path(self, path: str) -> tuple[str, bool]:
        if not is_path_within(path, self._assets_root):
            raise ModuleIdentityError(f"script is outside Assets: {path}")
        relative = portable_path(relative_path(path, self._assets_root))
        parts = relative.split("/")
        filename = parts.pop()
        stem, extension = os.path.splitext(filename)
        if extension != ".py" or not stem:
            raise ModuleIdentityError(f"not a Python module path: {path}")
        is_package = stem == "__init__"
        if is_package:
            if not parts:
                raise ModuleIdentityError("Assets/__init__.py has no project module name")
        else:
            parts.append(stem)
        if any(not _valid_segment(part) for part in parts):
            raise ModuleIdentityError(f"invalid Python module path: {path}")
        return ".".join(parts), is_package

    def _assert_asset_file(self, path: str) -> None:
        if not is_path_within(path, self._assets_root):
            raise ModuleIdentityError(f"script is outside Assets: {path}")
        if os.path.splitext(path)[1] != ".py":
            raise ModuleIdentityError(f"not a Python module path: {path}")

    @staticmethod
    def _read_source(path: str, source: Optional[bytes | str]) -> bytes:
        if source is None:
            with open(path, "rb") as stream:
                return stream.read()
        return source.encode("utf-8") if isinstance(source, str) else bytes(source)

    def _selector_key_locked(self, selector: PathLike | ModuleId) -> Optional[str]:
        return self._selector_key_for_maps(selector, self._modules, self._module_by_name)

    def _selector_key_for_maps(
        self,
        selector: PathLike | ModuleId | str,
        modules: dict[str, ModuleRecord],
        module_by_name: dict[str, ModuleId],
    ) -> Optional[str]:
        if isinstance(selector, ModuleId):
            return selector.path_key if selector.path_key in modules else selector.path_key
        value = os.fspath(selector)
        if value in module_by_name:
            return module_by_name[value].path_key
        if not os.path.isabs(value):
            value = os.path.join(self._assets_root, value)
        return path_key(value)

    def _record_for_selector_locked(self, selector: PathLike | ModuleId | str) -> Optional[ModuleRecord]:
        if isinstance(selector, str) and selector in self._module_by_name:
            module_id = self._module_by_name[selector]
            return self._modules.get(module_id.path_key)
        key = self._selector_key_locked(selector)
        return self._modules.get(key) if key else None

    @staticmethod
    def _module_sort_key(module_id: ModuleId) -> tuple[str, str]:
        return module_id.module_name, module_id.path_key

    @staticmethod
    def _dedupe_edges(edges: Iterable[DependencyEdge]) -> tuple[DependencyEdge, ...]:
        seen: set[tuple[object, ...]] = set()
        result: list[DependencyEdge] = []
        for edge in edges:
            key = (
                edge.source,
                edge.imported_name,
                edge.target,
                edge.kind,
                edge.import_form,
                edge.line,
                edge.column,
                edge.dynamic,
            )
            if key not in seen:
                seen.add(key)
                result.append(edge)
        return tuple(result)


__all__ = [
    "DependencyEdge",
    "DependencyGraphRollbackError",
    "DependencyGraphSnapshot",
    "DependencyKind",
    "GraphMutation",
    "ModuleId",
    "ModuleIdentityError",
    "ModuleRecord",
    "ScriptDependencyGraph",
    "ScriptDependencyGraphError",
    "StagedDependencyGraphTransaction",
    "StaleDependencyGraphTransaction",
]
