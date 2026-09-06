"""Thread-safe, owner-driven collection of Python script source changes.

The collector deliberately does not own a worker thread.  A caller submits
immutable source snapshots from any thread, then explicitly calls
``process_worker_batch`` from the worker it owns.  The default front-end only
parses and compiles Python source; it never imports or executes user code.
"""

from __future__ import annotations

import ast
import io
import os
import threading
import tokenize
import types
import uuid
from collections import deque
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from Infernux.engine.path_utils import path_key
from Infernux.engine.runtime_script_revision import (
    ScriptRevision,
    ScriptRevisionJournal,
    ScriptRevisionRequest,
)
from Infernux.engine.script_candidate_policy import (
    ScriptCandidatePolicyReport,
    analyze_script_candidate_tree,
)


ORIGINS = frozenset(
    {"watchdog", "automation", "editor", "rollback", "initial_scan", "dependency"}
)
CHANGE_KINDS = frozenset(
    {"created", "modified", "deleted", "moved", "renamed", "initial_scan", "dependency"}
)
_CATALOG_EVENT_PRIORITY = {
    "created": 2,
    "moved": 2,
    "renamed": 2,
    "deleted": 2,
    "modified": 1,
    "initial": 0,
    "initial_scan": 0,
    "dependency": 0,
}


def _source_bytes(source: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(source, str):
        return source.encode("utf-8")
    return bytes(source)


def _decode_source(source: bytes) -> str:
    """Decode source using the same encoding discovery Python uses."""
    encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
    return source.decode(encoding)


@dataclass(frozen=True, slots=True)
class ScriptChange:
    """One immutable source snapshot and its change provenance."""

    revision: ScriptRevision
    origin: str
    transaction_id: str
    catalog_event: str | None
    change_kind: str
    merged_count: int = 1
    merged_origins: tuple[str, ...] = ()
    merged_transaction_ids: tuple[str, ...] = ()
    merged_catalog_events: tuple[str, ...] = ()
    merged_change_kinds: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return self.revision.path

    @property
    def identity_key(self) -> str:
        return self.revision.identity_key

    @property
    def generation(self) -> int:
        return self.revision.generation

    @property
    def content_hash(self) -> str:
        return self.revision.content_hash

    @property
    def source(self) -> bytes:
        return self.revision.source

    @property
    def request(self) -> ScriptRevisionRequest:
        return ScriptRevisionRequest(self.revision)

    @property
    def kind(self) -> str:
        return self.change_kind

    @property
    def effective_catalog_event(self) -> str | None:
        """Return the strongest event after duplicate-source coalescing."""
        events = (self.catalog_event, *self.merged_catalog_events)
        selected: str | None = None
        selected_priority = -1
        for event in events:
            if event is None:
                continue
            priority = _CATALOG_EVENT_PRIORITY.get(event, 0)
            if selected is None or priority > selected_priority:
                selected = event
                selected_priority = priority
        return selected


@dataclass(frozen=True, slots=True)
class ScriptFrontendImport:
    """A static import reference retained for a future dependency graph."""

    module: str
    level: int
    imported: tuple[str, ...]
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class ScriptFrontendDiagnostic:
    path: str
    generation: int
    content_hash: str
    message: str
    phase: str
    severity: str = "error"
    code: str = ""
    operation: str = ""
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None


@dataclass(frozen=True, slots=True)
class ScriptFrontendArtifact:
    """Immutable front-end output; it contains no loaded module state."""

    path: str
    identity_key: str
    generation: int
    content_hash: str
    source: bytes
    code: types.CodeType | None
    imports: tuple[ScriptFrontendImport, ...] = ()
    payload: object | None = None
    policy_report: ScriptCandidatePolicyReport = ScriptCandidatePolicyReport()


@dataclass(frozen=True, slots=True)
class ScriptChangeResult:
    change: ScriptChange
    status: str
    artifact: ScriptFrontendArtifact | None = None
    diagnostics: tuple[ScriptFrontendDiagnostic, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status in {"completed", "ready"}

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def request(self) -> ScriptRevisionRequest:
        return self.change.request

    @property
    def path(self) -> str:
        return self.change.path

    @property
    def generation(self) -> int:
        return self.change.generation

    @property
    def content_hash(self) -> str:
        return self.change.content_hash

    @property
    def source(self) -> bytes:
        return self.change.source

    @property
    def diagnostic(self) -> ScriptFrontendDiagnostic | None:
        return self.diagnostics[0] if self.diagnostics else None


@dataclass(frozen=True, slots=True)
class _QueuedChange:
    change: ScriptChange
    epoch: int


class ScriptChangeCollector:
    """Collect, front-end compile, and owner-publish script revisions.

    ``process_worker_batch`` is intentionally synchronous and must be called
    by the owner of the worker budget.  It may be called from any caller-owned
    thread, but the collector never creates one and never touches
    ``sys.path``, ``sys.modules``, or the component registry.
    """

    def __init__(
        self,
        journal: ScriptRevisionJournal | None = None,
        *,
        compile_source: Callable[[bytes], object] | None = None,
        frontend: Callable[[bytes], object] | None = None,
    ) -> None:
        if compile_source is not None and frontend is not None:
            raise TypeError("pass either compile_source or frontend, not both")
        self._lock = threading.RLock()
        self._journal = journal or ScriptRevisionJournal()
        self._compile_source = compile_source or frontend
        self._pending: deque[_QueuedChange] = deque()
        self._inflight: dict[tuple[str, int], _QueuedChange] = {}
        self._completed: deque[ScriptChangeResult] = deque()
        self._results: dict[tuple[str, int], ScriptChangeResult] = {}
        self._claimed: dict[tuple[str, int], ScriptChangeResult] = {}
        self._epoch = 0
        self._shutdown = False
        self._publication_active = False

    @property
    def journal(self) -> ScriptRevisionJournal:
        return self._journal

    @staticmethod
    def _validate_metadata(
        origin: str,
        change_kind: str,
        transaction_id: str | None,
    ) -> str:
        if origin not in ORIGINS:
            raise ValueError(f"unsupported change origin: {origin!r}")
        if not isinstance(change_kind, str) or change_kind not in CHANGE_KINDS:
            raise ValueError(f"unsupported change kind: {change_kind!r}")
        if transaction_id is not None and not str(transaction_id):
            raise ValueError("transaction_id must not be empty")
        return str(transaction_id) if transaction_id is not None else uuid.uuid4().hex

    @staticmethod
    def _key(change: ScriptChange) -> tuple[str, int]:
        return change.identity_key, change.generation

    def _merge_duplicate_locked(
        self,
        existing: ScriptChange,
        *,
        origin: str,
        transaction_id: str,
        catalog_event: str | None,
        change_kind: str,
    ) -> ScriptChange:
        catalog_events = existing.merged_catalog_events
        if catalog_event is not None:
            catalog_events += (catalog_event,)
        return replace(
            existing,
            merged_count=existing.merged_count + 1,
            merged_origins=existing.merged_origins + (origin,),
            merged_transaction_ids=existing.merged_transaction_ids + (transaction_id,),
            merged_catalog_events=catalog_events,
            merged_change_kinds=existing.merged_change_kinds + (change_kind,),
        )

    def _discard_superseded_locked(self, identity_key: str, generation: int) -> None:
        """Discard all older publish candidates for the same path."""
        self._pending = deque(
            item
            for item in self._pending
            if item.change.identity_key != identity_key or item.change.generation == generation
        )
        for key in tuple(self._claimed):
            if key[0] != identity_key or key[1] == generation:
                continue
            result = self._claimed.pop(key)
            self._journal.release_claim(result.request)
            self._results.pop(key, None)
        for key in tuple(self._results):
            if key[0] == identity_key and key[1] != generation:
                self._results.pop(key, None)
        self._completed = deque(
            result
            for result in self._completed
            if result.change.identity_key != identity_key
            or result.change.generation == generation
        )

    def _replace_queued_locked(self, merged: ScriptChange) -> None:
        key = self._key(merged)
        self._pending = deque(
            _QueuedChange(merged if item.change == merged or self._key(item.change) == key else item.change, item.epoch)
            for item in self._pending
        )
        item = self._inflight.get(key)
        if item is not None:
            self._inflight[key] = _QueuedChange(merged, item.epoch)

    def submit(
        self,
        path: str,
        source: bytes | bytearray | memoryview | str,
        *,
        origin: str,
        transaction_id: str | None = None,
        catalog_event: str | None = None,
        change_kind: str = "modified",
        force_new_generation: bool = False,
        force: bool | None = None,
    ) -> ScriptChange | None:
        """Record a change and enqueue it for explicit front-end processing.

        Identical content for the same path is coalesced by the journal and
        never creates a second worker item.  Metadata from duplicates is kept
        on the queued immutable change through the ``merged_*`` fields.
        """
        transaction_id = self._validate_metadata(origin, change_kind, transaction_id)
        if catalog_event is not None:
            catalog_event = str(catalog_event)
        with self._lock:
            if self._shutdown:
                raise RuntimeError("script change collector is shut down")
            if self._publication_active:
                raise RuntimeError("cannot submit during an active script publication")
            request = self._journal.request(
                os.fspath(path),
                _source_bytes(source),
                force_new_generation=force_new_generation,
                force=force,
            )
            if request is None:
                latest = self._journal.latest(os.fspath(path))
                if latest is not None:
                    key = (latest.identity_key, latest.generation)
                    existing = self._inflight.get(key)
                    if existing is not None:
                        merged = self._merge_duplicate_locked(
                            existing.change,
                            origin=origin,
                            transaction_id=transaction_id,
                            catalog_event=catalog_event,
                            change_kind=change_kind,
                        )
                        self._inflight[key] = _QueuedChange(merged, existing.epoch)
                    else:
                        existing_result = self._results.get(key)
                        if existing_result is not None and existing_result.status == "completed":
                            merged = self._merge_duplicate_locked(
                                existing_result.change,
                                origin=origin,
                                transaction_id=transaction_id,
                                catalog_event=catalog_event,
                                change_kind=change_kind,
                            )
                            merged_result = replace(existing_result, change=merged)
                            self._results[key] = merged_result
                            self._completed = deque(
                                merged_result if self._key(item.change) == key else item
                                for item in self._completed
                            )
                        for item in self._pending:
                            if self._key(item.change) == key:
                                merged = self._merge_duplicate_locked(
                                    item.change,
                                    origin=origin,
                                    transaction_id=transaction_id,
                                    catalog_event=catalog_event,
                                    change_kind=change_kind,
                                )
                                self._replace_queued_locked(merged)
                                break
                return None
            change = ScriptChange(
                revision=request.revision,
                origin=origin,
                transaction_id=transaction_id,
                catalog_event=catalog_event,
                change_kind=change_kind,
            )
            self._discard_superseded_locked(change.identity_key, change.generation)
            self._pending.append(_QueuedChange(change, self._epoch))
            return change

    @staticmethod
    def _imports(tree: ast.AST) -> tuple[ScriptFrontendImport, ...]:
        entries: list[ScriptFrontendImport] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    entries.append(
                        ScriptFrontendImport(
                            alias.name,
                            0,
                            (alias.asname or alias.name,),
                            node.lineno,
                            node.col_offset,
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                entries.append(
                    ScriptFrontendImport(
                        node.module or "",
                        node.level,
                        tuple(alias.name for alias in node.names),
                        node.lineno,
                        node.col_offset,
                    )
                )
        entries.sort(key=lambda entry: (entry.line, entry.column))
        return tuple(entries)

    @staticmethod
    def _diagnostic(change: ScriptChange, exc: BaseException, phase: str) -> ScriptFrontendDiagnostic:
        if isinstance(exc, SyntaxError):
            line = exc.lineno
            column = exc.offset
            end_line = getattr(exc, "end_lineno", None)
            end_column = getattr(exc, "end_offset", None)
            message = exc.msg
        else:
            line = column = end_line = end_column = None
            message = str(exc) or exc.__class__.__name__
        return ScriptFrontendDiagnostic(
            path=change.path,
            generation=change.generation,
            content_hash=change.content_hash,
            message=message,
            phase=phase,
            line=line,
            column=column,
            end_line=end_line,
            end_column=end_column,
        )

    def _default_frontend(self, change: ScriptChange) -> ScriptFrontendArtifact:
        # Built-in compile and AST parsing are front-end operations only.
        tree = ast.parse(_decode_source(change.source), filename=change.path, mode="exec")
        policy_report = analyze_script_candidate_tree(tree)
        code = None
        if not policy_report.is_rejected:
            code = compile(tree, change.path, "exec", dont_inherit=True)
        return ScriptFrontendArtifact(
            path=change.path,
            identity_key=change.identity_key,
            generation=change.generation,
            content_hash=change.content_hash,
            source=change.source,
            code=code,
            imports=self._imports(tree),
            policy_report=policy_report,
        )

    def _compile_change(self, change: ScriptChange) -> ScriptFrontendArtifact:
        tree = ast.parse(_decode_source(change.source), filename=change.path, mode="exec")
        policy_report = analyze_script_candidate_tree(tree)
        if self._compile_source is None:
            code = None
            if not policy_report.is_rejected:
                code = compile(tree, change.path, "exec", dont_inherit=True)
            payload = None
        else:
            # Policy must run before a caller-owned candidate compiler/importer.
            # A rejected source is returned as a diagnostic artifact only; it
            # never reaches the candidate import boundary.
            payload = None
            if not policy_report.is_rejected:
                payload = self._compile_source(change.source)
            code = None
        return ScriptFrontendArtifact(
            path=change.path,
            identity_key=change.identity_key,
            generation=change.generation,
            content_hash=change.content_hash,
            source=change.source,
            code=code,
            imports=self._imports(tree),
            payload=payload,
            policy_report=policy_report,
        )

    @staticmethod
    def _policy_diagnostics(
        change: ScriptChange,
        report: ScriptCandidatePolicyReport,
    ) -> tuple[ScriptFrontendDiagnostic, ...]:
        return tuple(
            ScriptFrontendDiagnostic(
                path=change.path,
                generation=change.generation,
                content_hash=change.content_hash,
                message=issue.message,
                phase="candidate_policy",
                severity="error",
                code=issue.code,
                operation=issue.operation,
                line=issue.line,
                column=issue.column,
                end_line=issue.end_line,
                end_column=issue.end_column,
            )
            for issue in (*report.blocked, *report.runtime_guard_required)
        )

    def process_worker_batch(self, max_items: int | None = None) -> tuple[ScriptChangeResult, ...]:
        """Synchronously front-end a bounded batch on the caller-owned worker."""
        if max_items is not None and max_items < 0:
            raise ValueError("max_items must be non-negative or None")
        with self._lock:
            if self._shutdown:
                return ()
            limit = len(self._pending) if max_items is None else min(max_items, len(self._pending))
            batch: list[_QueuedChange] = []
            for _ in range(limit):
                item = self._pending.popleft()
                self._inflight[self._key(item.change)] = item
                batch.append(item)

        results: list[ScriptChangeResult] = []
        for item in batch:
            change = item.change
            artifact: ScriptFrontendArtifact | None = None
            diagnostics: tuple[ScriptFrontendDiagnostic, ...] = ()
            status = "completed"
            try:
                artifact = self._compile_change(change)
                if artifact.policy_report.is_rejected:
                    diagnostics = self._policy_diagnostics(change, artifact.policy_report)
                    status = "failed"
            except Exception as exc:  # front-end errors become diagnostics
                diagnostics = (self._diagnostic(change, exc, "front_end"),)
                status = "failed"

            with self._lock:
                current = self._inflight.pop(self._key(change), None)
                if current is not None:
                    change = current.change
                epoch_stale = item.epoch != self._epoch
                if epoch_stale or self._shutdown:
                    status = "stale"
                    artifact = None
                    diagnostics = ()
                elif status == "completed":
                    if not self._journal.complete(change.request, succeeded=True):
                        status = "stale"
                        artifact = None
                elif not self._journal.complete(
                    change.request,
                    succeeded=False,
                    messages=tuple(diagnostic.message for diagnostic in diagnostics),
                    phase=(diagnostics[0].phase if diagnostics else "front_end"),
                ):
                    status = "stale"
                    diagnostics = ()
                result = ScriptChangeResult(change, status, artifact, diagnostics)
                key = self._key(change)
                if status == "stale":
                    self._results.pop(key, None)
                else:
                    self._results[key] = result
                if not self._shutdown and not epoch_stale:
                    self._completed.append(result)
                results.append(result)
        return tuple(results)

    def drain_completed(self, path: str | None = None) -> tuple[ScriptChangeResult, ...]:
        with self._lock:
            if path is None:
                values = tuple(self._completed)
                self._completed.clear()
                return values
            key = path_key(path)
            selected: list[ScriptChangeResult] = []
            retained: deque[ScriptChangeResult] = deque()
            for result in self._completed:
                if result.change.identity_key == key:
                    selected.append(result)
                else:
                    retained.append(result)
            self._completed = retained
            return tuple(selected)

    @staticmethod
    def _batch_paths(paths: Iterable[str]) -> tuple[str, ...]:
        values = tuple(paths)
        if not values:
            return ()
        keys: list[str] = []
        seen: set[str] = set()
        for path in values:
            key = path_key(os.fspath(path))
            if key in seen:
                raise ValueError(f"batch contains duplicate path: {path!r}")
            seen.add(key)
            keys.append(key)
        return tuple(keys)

    def _current_result_locked(self, identity_key: str) -> ScriptChangeResult | None:
        latest = self._journal.latest(identity_key)
        if latest is None:
            return None
        return self._results.get((identity_key, latest.generation))

    @staticmethod
    def _transaction_matches(result: ScriptChangeResult, transaction_id: str) -> bool:
        """Return whether ``transaction_id`` owns this canonical revision.

        Duplicate editor/watchdog observations are provenance metadata only.
        The first submission creates the revision and therefore remains its
        publication owner; merged transaction IDs must never participate in
        the ownership check.  This also prevents a later echo transaction
        from claiming or publishing the canonical revision.
        """
        change = result.change
        return change.transaction_id == transaction_id

    def claim_ready_batch(
        self,
        expected_paths: Iterable[str],
        *,
        transaction_id: str,
    ) -> tuple[ScriptChangeResult, ...]:
        """Atomically claim completed successful results for one transaction."""
        if not isinstance(transaction_id, str) or not transaction_id:
            raise ValueError("transaction_id must not be empty")
        paths = self._batch_paths(expected_paths)
        if not paths:
            return ()
        with self._lock:
            candidates: list[ScriptChangeResult] = []
            for key in paths:
                result = self._current_result_locked(key)
                if (
                    result is None
                    or result.status != "completed"
                    or not result.succeeded
                    or not self._transaction_matches(result, transaction_id)
                    or self._key(result.change) in self._claimed
                    or self._journal.latest(result.path) != result.change.revision
                ):
                    return ()
                candidates.append(result)
            journal_results = self._journal.claim_ready_batch(
                result.path for result in candidates
            )
            if len(journal_results) != len(candidates):
                return ()
            ready = tuple(replace(result, status="ready") for result in candidates)
            for result in ready:
                self._claimed[self._key(result.change)] = result
            return ready

    def publish_ready_batch(
        self,
        expected_paths: Iterable[str],
        transaction_id: str,
        publisher: Callable[[tuple[ScriptChangeResult, ...]], object],
        *,
        rollback: Callable[[object], object] | None = None,
    ) -> object:
        """Publish one ready transaction while holding the collector lock.

        Claiming, the owner-thread callback, and journal commit form one
        collector-owned critical section.  A callback result of exactly
        ``False`` is a failed publication; ``None`` is treated as successful
        completion and normalized to ``True``.  Any exception releases the
        claim and is re-raised after cleanup.  A non-``None`` callback result
        is returned to the caller as its optional rollback token.
        """
        if not callable(publisher):
            raise TypeError("publisher must be callable")
        if rollback is not None and not callable(rollback):
            raise TypeError("rollback must be callable or None")
        with self._lock:
            if self._publication_active:
                raise RuntimeError("nested script publication is not allowed")
            self._publication_active = True
            try:
                ready = self.claim_ready_batch(
                    expected_paths,
                    transaction_id=transaction_id,
                )
                if not ready:
                    return False
                try:
                    token = publisher(ready)
                except BaseException:
                    self.release_claim_batch(ready, transaction_id=transaction_id)
                    raise
                if token is False:
                    self.release_claim_batch(ready, transaction_id=transaction_id)
                    return False
                if not self.commit_published_batch(
                    ready,
                    transaction_id=transaction_id,
                ):
                    try:
                        if rollback is not None and token is not None:
                            rollback(token)
                    finally:
                        self.release_claim_batch(ready, transaction_id=transaction_id)
                    return False
                return True if token is None else token
            finally:
                self._publication_active = False

    @staticmethod
    def _request(value: ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision) -> ScriptRevisionRequest:
        if isinstance(value, ScriptChangeResult):
            return value.request
        if isinstance(value, ScriptChange):
            return value.request
        if isinstance(value, ScriptRevisionRequest):
            return value
        return ScriptRevisionRequest(value)

    def commit_published(
        self,
        value: ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision | Iterable[ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision],
        *,
        transaction_id: str | None = None,
    ) -> bool:
        if not isinstance(value, (ScriptChangeResult, ScriptChange, ScriptRevisionRequest, ScriptRevision)):
            return self.commit_published_batch(value, transaction_id=transaction_id)
        request = self._request(value)
        with self._lock:
            committed = self._journal.commit_published(request)
            if committed:
                key = (request.revision.identity_key, request.revision.generation)
                self._claimed.pop(key, None)
                self._results.pop(key, None)
                self._completed = deque(
                    value for value in self._completed if self._key(value.change) != key
                )
            return committed

    def commit_published_batch(
        self,
        values: Iterable[ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision],
        *,
        transaction_id: str | None = None,
    ) -> bool:
        """Atomically commit a collector claim and its journal LKG updates."""
        values = tuple(values)
        if not values:
            return False
        if transaction_id is not None and (not isinstance(transaction_id, str) or not transaction_id):
            raise ValueError("transaction_id must not be empty")
        with self._lock:
            requests = tuple(self._request(value) for value in values)
            keys: set[tuple[str, int]] = set()
            for value, request in zip(values, requests):
                key = (request.revision.identity_key, request.revision.generation)
                if key in keys or self._claimed.get(key) is None:
                    return False
                keys.add(key)
                claimed = self._claimed[key]
                if transaction_id is not None and not self._transaction_matches(claimed, transaction_id):
                    return False
            if not self._journal.commit_published_batch(requests):
                return False
            for key in keys:
                self._claimed.pop(key, None)
                self._results.pop(key, None)
            self._completed = deque(
                value for value in self._completed if self._key(value.change) not in keys
            )
            return True

    def release_claim(
        self,
        value: ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision | Iterable[ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision],
        *,
        transaction_id: str | None = None,
    ) -> bool:
        if not isinstance(value, (ScriptChangeResult, ScriptChange, ScriptRevisionRequest, ScriptRevision)):
            return self.release_claim_batch(value, transaction_id=transaction_id)
        request = self._request(value)
        with self._lock:
            released = self._journal.release_claim(request)
            if released:
                key = (request.revision.identity_key, request.revision.generation)
                self._claimed.pop(key, None)
                self._results.pop(key, None)
            return released

    def release_claim_batch(
        self,
        values: Iterable[ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision],
        *,
        transaction_id: str | None = None,
    ) -> bool:
        """Atomically release a collector claim without changing the LKG."""
        values = tuple(values)
        if not values:
            return False
        if transaction_id is not None and (not isinstance(transaction_id, str) or not transaction_id):
            raise ValueError("transaction_id must not be empty")
        with self._lock:
            requests = tuple(self._request(value) for value in values)
            keys: set[tuple[str, int]] = set()
            for request in requests:
                key = (request.revision.identity_key, request.revision.generation)
                if key in keys or self._claimed.get(key) is None:
                    return False
                keys.add(key)
                if transaction_id is not None and not self._transaction_matches(self._claimed[key], transaction_id):
                    return False
            if not self._journal.release_claim_batch(requests):
                return False
            for key in keys:
                self._claimed.pop(key, None)
                self._results.pop(key, None)
            self._completed = deque(
                value for value in self._completed if self._key(value.change) not in keys
            )
            return True

    def discard_batch(
        self,
        expected_paths: Iterable[str],
        *,
        transaction_id: str,
    ) -> bool:
        """Discard one failed transaction without advancing any LKG.

        Every expected path must have a current completed or failed result
        belonging to the transaction.  The journal is validated and cleared
        first; collector maps and completion notifications are only removed
        after that all-or-nothing operation succeeds.
        """
        if not isinstance(transaction_id, str) or not transaction_id:
            raise ValueError("transaction_id must not be empty")
        paths = self._batch_paths(expected_paths)
        if not paths:
            return False
        with self._lock:
            results: list[ScriptChangeResult] = []
            keys: set[tuple[str, int]] = set()
            for path_key_value in paths:
                result = self._current_result_locked(path_key_value)
                if result is None:
                    return False
                key = self._key(result.change)
                if (
                    key in keys
                    or result.status not in {"completed", "failed"}
                    or not self._transaction_matches(result, transaction_id)
                    or self._journal.latest(result.path) != result.change.revision
                    or key in self._inflight
                ):
                    return False
                keys.add(key)
                results.append(result)
            if not self._journal.discard_batch(result.path for result in results):
                return False
            self._claimed = {
                key: value for key, value in self._claimed.items() if key not in keys
            }
            self._results = {
                key: value for key, value in self._results.items() if key not in keys
            }
            self._completed = deque(
                value for value in self._completed if self._key(value.change) not in keys
            )
            return True

    def abort_transaction(
        self,
        expected_paths: Iterable[str],
        *,
        transaction_id: str,
    ) -> bool:
        """Alias for :meth:`discard_batch` for transaction-oriented callers."""
        return self.discard_batch(expected_paths, transaction_id=transaction_id)

    def latest(self, path: str) -> ScriptRevision | None:
        return self._journal.latest(path)

    def last_known_good(self, path: str) -> ScriptRevision | None:
        return self._journal.last_known_good(path)

    def diagnostic(self, path: str):
        return self._journal.diagnostic(path)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending) + len(self._inflight)

    @property
    def completed_count(self) -> int:
        with self._lock:
            return len(self._completed)

    @property
    def is_shutdown(self) -> bool:
        with self._lock:
            return self._shutdown

    def clear(self) -> None:
        """Drop queued/claimed results and start a fresh journal epoch."""
        with self._lock:
            for result in tuple(self._claimed.values()):
                self._journal.release_claim(result.request)
            self._pending.clear()
            self._completed.clear()
            self._results.clear()
            self._claimed.clear()
            self._inflight.clear()
            self._epoch += 1
            self._journal = ScriptRevisionJournal()

    def shutdown(self) -> None:
        """Stop accepting work and discard all collector-owned queues."""
        with self._lock:
            if self._shutdown:
                return
            for result in tuple(self._claimed.values()):
                self._journal.release_claim(result.request)
            self._shutdown = True
            self._epoch += 1
            self._pending.clear()
            self._completed.clear()
            self._results.clear()
            self._claimed.clear()
            self._inflight.clear()


__all__ = [
    "CHANGE_KINDS",
    "ORIGINS",
    "ScriptChange",
    "ScriptChangeCollector",
    "ScriptChangeResult",
    "ScriptFrontendArtifact",
    "ScriptFrontendDiagnostic",
    "ScriptFrontendImport",
]
