import hashlib
import os
import threading
import time
import types
import uuid

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _HAS_WATCHDOG = True
except ImportError:
    # Standalone player builds exclude watchdog; ResourcesManager
    # still importable but .start() becomes a no-op.
    Observer = None
    FileSystemEventHandler = object
    _HAS_WATCHDOG = False

from Infernux.lib import Infernux
from Infernux.engine.path_utils import (
    is_path_within,
    path_key,
    portable_path,
    resolved_path,
)
from Infernux.engine.script_change_collector import (
    ScriptChangeCollector,
    ScriptChangeResult,
)
from Infernux.engine.script_dependency_graph import ScriptDependencyGraph
from Infernux.engine.script_compiler import get_script_compiler
from Infernux.engine.import_coordinator import (
    AssetFsEvent,
    AssetFsEventKind,
    ImportCoordinator,
    is_document_store_temporary_path,
)
from Infernux.core.asset_types import read_meta_guid
from Infernux.debug import Debug


class _AssetImportNotReady(RuntimeError):
    pass


class _AssetLocalWritePending(_AssetImportNotReady):
    """A watcher echo is waiting for an editor-owned persistence ticket."""

    pass


class _ScriptPublicationTransaction:
    """Owner-thread state for one dependency-aware script publication.

    Frontend results are immutable and may arrive in any order.  This small
    mutable record is deliberately owned by ``ResourceChangeHandler`` so no
    worker can publish a partial dependency closure.
    """

    def __init__(
        self,
        transaction_id: str,
        paths=(),
        *,
        retire_paths=(),
        initial_scan: bool = False,
    ) -> None:
        self.transaction_id = transaction_id
        self.expected_paths: list[str] = []
        self._expected_keys: set[str] = set()
        # Initial-scan members need their collector generation recorded at
        # submission time.  A later revision may remove the old result before
        # the owner drains it, leaving no result object to compare against.
        self.expected_generations: dict[str, int] = {}
        self.results: dict[str, ScriptChangeResult] = {}
        self.failed = False
        self.initial_scan = bool(initial_scan)
        self.closure_seeded = bool(initial_scan)
        self.graph_stage = None
        # Once the collector has advanced LKG, live publication must never be
        # replayed or rolled back.  Native CDS finalization is deliberately a
        # retryable post-durable step so a transient owner/finalize failure
        # cannot strand an active schema transaction or publish twice.
        self.durable_token = None
        self.durable_ordered: tuple[ScriptChangeResult, ...] = ()
        self.durable_finalized = False
        self.durable_move_finalized = False
        self.retire_paths: tuple[str, ...] = tuple(
            dict.fromkeys(os.fspath(path) for path in retire_paths if path)
        )
        self.add_paths(paths)

    def add_paths(self, paths) -> None:
        for value in paths:
            display = os.fspath(value)
            key = path_key(display)
            if key in self._expected_keys:
                continue
            self._expected_keys.add(key)
            self.expected_paths.append(display)

    def add_retire_paths(self, paths) -> None:
        """Record paths retired by this transaction without mutating live state."""
        values = list(self.retire_paths)
        seen = {path_key(path) for path in values}
        for value in paths:
            display = os.fspath(value)
            key = path_key(display)
            if not display or key in seen:
                continue
            seen.add(key)
            values.append(display)
        self.retire_paths = tuple(values)

    def has_path(self, path: str) -> bool:
        return path_key(path) in self._expected_keys

    def complete(self) -> bool:
        return all(path_key(path) in self.results for path in self.expected_paths)


class _ScriptPublicationRollback:
    """Rollback token retained by ``publish_ready_batch``."""

    def __init__(
        self,
        *,
        play_batch=None,
        edit_batch=None,
        registry_snapshot=None,
        graph_transaction=None,
        graph_committed=False,
    ):
        self.play_batch = play_batch
        self.edit_batch = edit_batch
        self.registry_snapshot = registry_snapshot
        self.graph_transaction = graph_transaction
        self.graph_committed = bool(graph_committed)


def _is_particle_script_path(file_path: str) -> bool:
    return str(file_path or "").lower().endswith(".particle.py")


class ResourceChangeHandler(FileSystemEventHandler):

    def __init__(
        self,
        engine: Infernux,
        *,
        project_path: str | None = None,
        frontend_wake=None,
    ):
        self._engine = engine
        # Kept as a compatibility inspection point for existing integrations;
        # _check_script never invokes it on the owner thread anymore.
        self._script_compiler = get_script_compiler()
        self._script_change_collector = ScriptChangeCollector()
        self._frontend_wake = frontend_wake
        self._frontend_worker_running = False
        self._dependency_graph = (
            ScriptDependencyGraph(project_path)
            if project_path is not None
            else None
        )
        self._last_dependency_affected = ()
        self._stale_script_revisions = set()
        # Every entry is owner-thread state.  A frontend worker can only add
        # immutable results to the collector; it can never mutate this map.
        self._script_transactions: dict[str, _ScriptPublicationTransaction] = {}
        # Frontend work already queued for a retired initial barrier may still
        # finish after a deleted member was removed from the replacement
        # barrier.  Consume such results without recreating the dead state.
        self._retired_script_transactions: set[str] = set()
        self._initial_scan_transaction_id: str | None = None
        self._coordinator = ImportCoordinator()
        self._shader_cache_invalidation_callbacks = []
        self._asset_database = engine.get_asset_database()
        if self._asset_database is None:
            raise RuntimeError("ResourceChangeHandler requires an initialized AssetDatabase")

    @property
    def _script_revision_journal(self):
        """Read-only compatibility alias for the collector journal."""
        return self._script_change_collector.journal

    @property
    def dependency_graph(self):
        return self._dependency_graph

    def dependency_graph_snapshot(self):
        if self._dependency_graph is None:
            return None
        return self._dependency_graph.snapshot()

    def dependency_affected(self, changed=None):
        if self._dependency_graph is None:
            return tuple(self._last_dependency_affected)
        if changed is None:
            return tuple(self._last_dependency_affected)
        return self._dependency_graph.affected_closure(changed)

    def set_frontend_worker_running(self, running: bool) -> None:
        self._frontend_worker_running = bool(running)

    def _wake_editor(self) -> None:
        request_wake = getattr(self._engine, "request_editor_wake", None)
        if callable(request_wake):
            request_wake()

    def begin_script_transaction(
        self,
        paths=(),
        *,
        retire_paths=(),
        transaction_id: str | None = None,
        initial_scan: bool = False,
    ) -> str:
        """Register an owner-side batch before frontend work is submitted."""
        transaction_id = transaction_id or uuid.uuid4().hex
        if transaction_id in self._script_transactions:
            state = self._script_transactions[transaction_id]
            state.add_paths(paths)
            state.add_retire_paths(retire_paths)
            return transaction_id
        self._script_transactions[transaction_id] = _ScriptPublicationTransaction(
            transaction_id,
            paths,
            retire_paths=retire_paths,
            initial_scan=initial_scan,
        )
        if initial_scan:
            self._initial_scan_transaction_id = transaction_id
        return transaction_id

    def process_script_worker(self, max_items: int | None = None) -> int:
        """Pump the collector frontend on the caller-owned worker thread.

        This method never creates a thread and never publishes AssetDatabase,
        registry, or live component state.  ``force=True`` callers may invoke
        it on the owner thread when the watcher is unavailable.
        """
        started = time.perf_counter()
        processed = len(self._script_change_collector.process_worker_batch(max_items))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if processed and elapsed_ms >= 25.0:
            Debug.log_internal(
                f"[ScriptReloadProfile] frontend={elapsed_ms:.2f}ms "
                f"members={processed}"
            )
        if processed:
            self._wake_editor()
        return processed

    @staticmethod
    def _is_meta_sidecar_path(file_path: str) -> bool:
        lower = portable_path(file_path).lower()
        return lower.endswith(".meta") and not lower.endswith(".meta.tmp")

    @staticmethod
    def _owner_path_for_meta_sidecar(meta_path: str) -> str:
        return meta_path[:-5]

    def _should_ignore(self, file_path: str) -> bool:
        """Ignore meta/temp/cache files to avoid GUID churn and noisy events."""
        lower = portable_path(file_path).lower()
        if (
            lower.endswith(".meta")
            or lower.endswith(".meta.tmp")
            or lower.endswith(".tmp")
            or is_document_store_temporary_path(file_path)
        ):
            return True
        if "/__pycache__/" in lower or lower.endswith(".pyc"):
            return True
        basename = lower.rsplit("/", 1)[-1]
        if basename == "imgui.ini":
            return True
        return False

    def on_created(self, event):
        if event.is_directory or self._should_ignore(event.src_path):
            return
        lower = str(event.src_path or "").lower()
        script_change = lower.endswith(".py") and not _is_particle_script_path(lower)
        self._coordinator.submit(
            AssetFsEventKind.CREATED,
            event.src_path,
            guid_hint=read_meta_guid(event.src_path),
            debounce_seconds=0.0 if script_change else None,
        )
        if script_change:
            self._wake_editor()

    def on_deleted(self, event):
        if event.is_directory:
            return
        if self._is_meta_sidecar_path(event.src_path):
            owner_path = self._owner_path_for_meta_sidecar(event.src_path)
            if self._should_ignore(owner_path):
                return
            self._coordinator.submit(
                AssetFsEventKind.META_DELETED,
                owner_path,
            )
            return
        if self._should_ignore(event.src_path):
            return
        self._coordinator.submit(
            AssetFsEventKind.DELETED,
            event.src_path,
            guid_hint=self._asset_database.get_guid_from_path(event.src_path),
        )

    def on_modified(self, event):
        if event.is_directory or self._should_ignore(event.src_path):
            return
        lower = str(event.src_path or "").lower()
        script_change = lower.endswith(".py") and not _is_particle_script_path(lower)
        self._coordinator.submit(
            AssetFsEventKind.MODIFIED,
            event.src_path,
            debounce_seconds=0.0 if script_change else None,
        )
        if script_change:
            self._wake_editor()

    def on_moved(self, event):
        if event.is_directory:
            return
        source_is_document_temp = is_document_store_temporary_path(event.src_path)
        destination_is_document_temp = is_document_store_temporary_path(event.dest_path)
        if source_is_document_temp:
            if destination_is_document_temp or self._should_ignore(event.dest_path):
                return
            self._coordinator.submit(
                AssetFsEventKind.MOVED,
                event.src_path,
                destination=event.dest_path,
                guid_hint=self._asset_database.get_guid_from_path(event.dest_path),
            )
            return
        if self._is_meta_sidecar_path(event.src_path) and self._is_meta_sidecar_path(event.dest_path):
            return
        if self._should_ignore(event.src_path) or self._should_ignore(event.dest_path):
            return
        self._coordinator.submit(
            AssetFsEventKind.MOVED,
            event.src_path,
            destination=event.dest_path,
            guid_hint=self._asset_database.get_guid_from_path(event.src_path),
            debounce_seconds=(
                0.0
                if str(event.dest_path or "").lower().endswith(".py")
                and not _is_particle_script_path(event.dest_path)
                else None
            ),
        )
        destination_lower = str(event.dest_path or "").lower()
        if destination_lower.endswith(".py") and not _is_particle_script_path(
            destination_lower
        ):
            self._wake_editor()

    @property
    def pending_count(self) -> int:
        return (
            self._coordinator.pending_count
            + self._script_change_collector.pending_count
            + self._script_change_collector.completed_count
        )

    def process_pending_reloads(self, *, force: bool = False) -> int:
        """Drain frontend results and commit events on the owner thread.

        The frontend is pumped here only when forced or when no manager-owned
        watcher worker is attached.  Normal frames consume completed frontend
        results only; all AssetDatabase, registry, and live publication work
        remains on this owner thread.
        """
        if force or not self._frontend_worker_running:
            self.process_script_worker()

        # Asset import and live GPU publication are owner-thread operations.
        # A watcher burst must not turn one editor frame into an unbounded
        # import transaction; preserve FIFO order and advance it frame by frame.
        events = self._coordinator.drain(
            force=force,
            max_events=None if force else 1,
        )
        if events:
            request_full_speed = getattr(self._engine, "request_full_speed_frame", None)
            if callable(request_full_speed):
                request_full_speed()
        for event in events:
            event_started = time.perf_counter()
            try:
                # A full AssetDatabase refresh owns the mutation transaction
                # until its prepared commit is published on this thread.
                # Watcher events observed during that interval are valid work,
                # not failed imports.  Requeue them without consuming the
                # bounded importer retry budget or emitting user-facing errors.
                if bool(getattr(self._asset_database, "refresh_pending", False)):
                    self._coordinator.defer(event)
                    continue
                self._dispatch_event(event)
            except _AssetLocalWritePending:
                self._coordinator.defer(event)
            except _AssetImportNotReady as exc:
                if not self._coordinator.retry(event):
                    Debug.log_error(f"Asset event exhausted retries: {event}: {exc}")
            except Exception as exc:
                Debug.log_error(f"Asset event failed: {event}: {exc}")
            finally:
                elapsed_ms = (time.perf_counter() - event_started) * 1000.0
                if elapsed_ms >= 25.0:
                    Debug.log_internal(
                        f"[AssetReloadProfile] event={event.kind.value} "
                        f"elapsed={elapsed_ms:.2f}ms pending={self._coordinator.pending_count} "
                        f"path={event.path}"
                    )

        # Dispatch first: a script event can submit its immutable snapshot to
        # the frontend worker, whose result may already be ready by the time
        # this frame reaches publication. This also avoids always charging a
        # full extra editor frame after every save.
        processed = self._drain_script_results()

        # Asset dispatch may have submitted a script snapshot.  A forced or
        # worker-less call must be deterministic, so finish that same batch.
        if force or not self._frontend_worker_running:
            self.process_script_worker()
            processed += self._drain_script_results()
        return processed + len(events)

    def _record_frontend_failure(self, result: ScriptChangeResult) -> None:
        from Infernux.components.script_loader import set_script_error

        diagnostic = result.diagnostic
        if diagnostic is None:
            message = "script frontend rejected the source"
            line = None
        else:
            location = ""
            if diagnostic.line is not None:
                location = f":{diagnostic.line}"
            message = diagnostic.message
            line = diagnostic.line
            message = f"{os.path.basename(result.path)}{location}  {message}"
        set_script_error(result.path, message)
        Debug.log_error(
            f"Script frontend failed for {result.path} "
            f"(generation={result.generation}, hash={result.content_hash}): {message}",
            source_file=result.path,
            source_line=line,
        )

    def _update_dependency_graph(self, result: ScriptChangeResult):
        graph = self._dependency_graph
        if graph is None or not os.path.isfile(result.path):
            return None
        try:
            mutation = graph.upsert(result.path, source=result.source)
        except Exception as exc:
            Debug.log_error(
                f"Script dependency graph update failed for {result.path} "
                f"(generation={result.generation}): {exc}"
            )
            return None
        self._last_dependency_affected = tuple(mutation.affected)
        return mutation

    @staticmethod
    def _script_source_matches_disk(result: ScriptChangeResult) -> bool:
        try:
            with open(result.path, "rb") as source_file:
                current = source_file.read()
        except OSError:
            return False
        return (
            current == result.source
            and hashlib.sha256(current).hexdigest() == result.content_hash
        )

    def _queue_dependency_changes(self, result: ScriptChangeResult, mutation) -> None:
        """Force affected dependents through the same collector transaction.

        Dependency results are deliberately tagged as such and are never fed
        back into this method.  The graph is already committed before this is
        called, so a rejected or stale source cannot create a false cascade.
        """
        if (
            mutation is None
            or result.change.change_kind in {"initial_scan", "dependency"}
            or result.change.origin == "dependency"
        ):
            return
        graph = self._dependency_graph
        if graph is None:
            return
        source_key = result.change.identity_key
        for module_id in mutation.affected:
            if module_id.path_key == source_key:
                continue
            record = graph.module_for_path(module_id.path_key)
            if record is None:
                continue
            dependent_path = record.source_path
            lower = dependent_path.lower()
            if not lower.endswith(".py") or _is_particle_script_path(dependent_path):
                continue
            self._check_script(
                dependent_path,
                catalog_event=None,
                origin="dependency",
                change_kind="dependency",
                transaction_id=result.change.transaction_id,
                force=True,
            )

    def _transaction_for_result(self, result: ScriptChangeResult):
        transaction_id = result.change.transaction_id
        state = self._script_transactions.get(transaction_id)
        if state is None:
            state = _ScriptPublicationTransaction(
                transaction_id,
                (result.path,),
                initial_scan=result.change.change_kind == "initial_scan",
            )
            self._script_transactions[transaction_id] = state
        elif not state.has_path(result.path):
            # A transaction may discover additional members while its root is
            # being processed.  The owner records them before publication.
            state.add_paths((result.path,))
        # The collector intentionally coalesces identical source snapshots and
        # keeps the first transaction as their publication owner. Internal
        # editor/MCP writes can therefore carry a later watchdog transaction
        # ID as provenance. That is an echo, not a mixed publication batch:
        # only the canonical transaction can claim or commit this revision.
        # Rejecting the canonical owner here made every internally-created
        # script fail as soon as its filesystem echo arrived.
        return state

    def _initial_scan_superseded(
        self,
        state: _ScriptPublicationTransaction,
    ) -> bool:
        """Return whether an initial member moved to a newer revision.

        ``ScriptChangeCollector`` intentionally removes stale results when a
        newer generation is submitted.  Therefore checking only
        ``state.results`` cannot detect this race; the journal's latest
        generation is the durable source of truth.
        """
        for path in state.expected_paths:
            expected_generation = state.expected_generations.get(path_key(path))
            latest = self._script_change_collector.latest(path)
            if latest is None:
                # A deleted member is removed from the replacement barrier;
                # it is not allowed to keep startup blocked forever.
                continue
            if (
                expected_generation is not None
                and latest.generation != expected_generation
            ):
                return True
        return False

    def _abort_initial_scan_transaction(
        self,
        state: _ScriptPublicationTransaction,
    ) -> None:
        """Atomically retire an obsolete initial barrier.

        Only still-current old-generation results are passed to the collector;
        superseded members have already been removed by the collector and
        must not make cleanup fail.  The collector keeps diagnostics while
        discarding pending/failed publication candidates.
        """
        self._retired_script_transactions.add(state.transaction_id)
        surviving_paths: list[str] = []
        for path in state.expected_paths:
            result = state.results.get(path_key(path))
            latest = self._script_change_collector.latest(path)
            if (
                result is not None
                and latest is not None
                and latest.generation == result.generation
                and result.change.transaction_id == state.transaction_id
            ):
                surviving_paths.append(path)
        if surviving_paths:
            discarded = self._script_change_collector.abort_transaction(
                surviving_paths,
                transaction_id=state.transaction_id,
            )
            if not discarded:
                Debug.log_error(
                    "Failed to retire superseded initial script scan "
                    f"(transaction={state.transaction_id})"
                )
        self._remove_script_transaction(state)

    def _restart_initial_scan_transaction(
        self,
        state: _ScriptPublicationTransaction,
    ) -> None:
        """Restart one initial barrier from the current disk snapshot."""
        paths = tuple(
            path
            for path in state.expected_paths
            if os.path.isfile(path)
        )
        self._abort_initial_scan_transaction(state)
        if not paths:
            return
        transaction_id = self.begin_script_transaction(
            paths,
            initial_scan=True,
        )
        submitted = 0
        for path in paths:
            # Force a fresh generation even if the journal still contains the
            # superseded initial source.  _check_script reads the bytes at the
            # point of restart, so the user's newest edit is in this barrier.
            if self._check_script(
                path,
                catalog_event=None,
                origin="initial_scan",
                change_kind="initial_scan",
                transaction_id=transaction_id,
                force=True,
            ) is not None:
                submitted += 1
        if not submitted:
            self._remove_script_transaction(
                self._script_transactions[transaction_id]
            )
            return
        Debug.log_internal(
            "Restarted superseded startup script scan "
            f"(transaction={transaction_id}, members={submitted})"
        )

    def _seed_script_dependency_closure(
        self,
        state: _ScriptPublicationTransaction,
        result: ScriptChangeResult,
    ) -> None:
        """Stage the root graph and enqueue its complete affected closure."""
        if (
            self._dependency_graph is None
            or state.initial_scan
            or state.closure_seeded
            or result.change.change_kind == "dependency"
            or result.change.origin == "dependency"
        ):
            return
        try:
            stage = self._dependency_graph.stage_transaction(
                {result.path: result.source},
                removals=state.retire_paths,
            )
        except Exception as exc:
            state.failed = True
            Debug.log_error(
                f"Script dependency transaction staging failed for {result.path}: {exc}"
            )
            return

        state.graph_stage = stage
        state.closure_seeded = True
        retired_keys = {path_key(path) for path in state.retire_paths}
        dependent_paths = []
        for module_id in stage.affected:
            record = self._dependency_graph.module_for_path(module_id.path_key)
            if record is None:
                continue
            dependent_path = record.source_path
            if (
                path_key(dependent_path) == result.change.identity_key
                or not dependent_path.lower().endswith(".py")
                or _is_particle_script_path(dependent_path)
                or path_key(dependent_path) in retired_keys
            ):
                continue
            if not state.has_path(dependent_path):
                dependent_paths.append(dependent_path)
        state.add_paths(dependent_paths)

        for dependent_path in dependent_paths:
            if not os.path.isfile(dependent_path):
                state.failed = True
                Debug.log_error(
                    f"Script dependency transaction member disappeared: {dependent_path}"
                )
                continue
            self._check_script(
                dependent_path,
                catalog_event=None,
                origin="dependency",
                change_kind="dependency",
                transaction_id=state.transaction_id,
                force=True,
            )

    def _publish_script_registry_member(self, result: ScriptChangeResult) -> None:
        """Publish the rollbackable component registry entry."""
        from Infernux.components.registry import register_component_script

        register_component_script(result.path, source=result.source)

    def _publish_script_post_commit_member(self, result: ScriptChangeResult) -> None:
        """Publish diagnostics and callbacks after collector LKG commit."""
        from Infernux.components.script_loader import _clear_script_error

        started = time.perf_counter()
        marks: list[tuple[str, float]] = []

        def mark(label: str) -> None:
            marks.append((label, time.perf_counter()))

        _clear_script_error(result.path)
        mark("diagnostic_registry")
        Debug.clear_source_entries(result.path)
        mark("console")
        Debug.log_internal(f"[OK] Script OK: {os.path.basename(result.path)}")
        rm = ResourcesManager.instance()
        if rm is not None:
            catalog_event = result.change.effective_catalog_event
            if catalog_event is not None:
                rm.notify_script_catalog_changed(result.path, catalog_event)
            mark("catalog_callbacks")
            abs_path = path_key(result.path)
            for callback in list(rm._script_reload_callbacks.get(abs_path, [])):
                callback(result.path)
            mark("file_callbacks")
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms >= 10.0:
            previous = started
            pieces = []
            for label, current in marks:
                pieces.append(f"{label}={(current - previous) * 1000.0:.2f}ms")
                previous = current
            Debug.log_internal(
                f"[ScriptReloadProfile] post_commit={elapsed_ms:.2f}ms "
                f"file={os.path.basename(result.path)} " + " ".join(pieces)
            )

    def _rollback_script_publication(self, token: _ScriptPublicationRollback) -> None:
        rollback_errors: list[str] = []
        if token.play_batch is not None or token.edit_batch is not None:
            play_mode = __import__(
                "Infernux.engine.play_mode",
                fromlist=("PlayModeManager",),
            ).PlayModeManager.instance()
            if play_mode is not None:
                try:
                    if token.edit_batch is not None:
                        play_mode.rollback_edit_script_reload_batch(token.edit_batch)
                    elif token.play_batch is not None:
                        play_mode.rollback_script_reload_batch(token.play_batch)
                except Exception as exc:
                    rollback_errors.append(f"live script rollback failed: {exc}")
        if token.registry_snapshot is not None:
            from Infernux.components.registry import restore_component_registry_state
            try:
                restore_component_registry_state(token.registry_snapshot)
            except Exception as exc:
                rollback_errors.append(f"component registry rollback failed: {exc}")
        graph = self._dependency_graph
        rollback = getattr(graph, "rollback_transaction", None)
        if (
            graph is not None
            and token.graph_transaction is not None
            and token.graph_committed
            and callable(rollback)
        ):
            try:
                rollback(token.graph_transaction)
            except Exception as exc:
                rollback_errors.append(f"dependency graph rollback failed: {exc}")
        if rollback_errors:
            raise RuntimeError("; ".join(rollback_errors))

    @staticmethod
    def _finalize_script_publication(token: _ScriptPublicationRollback) -> None:
        if token.play_batch is None:
            return
        play_mode = __import__(
            "Infernux.engine.play_mode",
            fromlist=("PlayModeManager",),
        ).PlayModeManager.instance()
        if play_mode is None:
            raise RuntimeError("script publication owner disappeared before finalization")
        play_mode.finalize_script_reload_batch(token.play_batch)

    def _complete_durable_script_publication(
        self,
        state: _ScriptPublicationTransaction,
    ) -> bool:
        """Finish irreversible work after collector LKG has advanced once."""
        token = state.durable_token
        if token is None:
            return False
        if not state.durable_finalized:
            try:
                self._finalize_script_publication(token)
            except Exception as exc:
                Debug.log_error(
                    f"Script publication finalization failed and will retry "
                    f"(transaction={state.transaction_id}): {exc}"
                )
                return False
            state.durable_finalized = True
        if state.retire_paths and not state.durable_move_finalized:
            try:
                self._finalize_script_move(state)
            except Exception as exc:
                Debug.log_error(
                    f"Script move finalization failed and will retry "
                    f"(transaction={state.transaction_id}): {exc}"
                )
                return False
            state.durable_move_finalized = True
        for result in state.durable_ordered:
            try:
                self._publish_script_post_commit_member(result)
            except Exception as exc:
                # Catalog/UI observers are post-commit notifications.  A bad
                # observer must not roll back a durable script commit.
                Debug.log_error(
                    f"Script post-commit callback failed for {result.path}: {exc}"
                )
        self._remove_script_transaction(state)
        return True

    def _ordered_script_results(
        self,
        state: _ScriptPublicationTransaction,
        stage,
    ) -> tuple[ScriptChangeResult, ...]:
        """Map final dependency-first graph order back to frontend results."""
        by_key = {
            path_key(result.path): result
            for result in state.results.values()
        }
        ordered: list[ScriptChangeResult] = []
        seen: set[str] = set()
        if stage is not None:
            for module_id in stage.ordered_modules:
                key = module_id.path_key
                result = by_key.get(key)
                if result is not None and key not in seen:
                    seen.add(key)
                    ordered.append(result)
        for path in state.expected_paths:
            key = path_key(path)
            result = by_key.get(key)
            if result is not None and key not in seen:
                seen.add(key)
                ordered.append(result)
        return tuple(ordered)

    def _edit_script_has_live_targets(self, play_mode, result: ScriptChangeResult) -> bool:
        """Preflight edit-mode targets before allowing a multi-file reload."""
        scene_getter = getattr(play_mode, "_get_scene_manager", None)
        if not callable(scene_getter):
            return False
        scene_manager = scene_getter()
        scene = scene_manager.get_active_scene() if scene_manager else None
        if scene is None:
            return False
        guid = self._asset_database.get_guid_from_path(result.path) or ""
        for obj in scene.get_all_objects():
            for component in getattr(obj, "get_py_components", lambda: ())() or ():
                if str(getattr(component, "_script_guid", "") or "") == str(guid):
                    return True
        return False

    def _publish_script_transaction(
        self,
        state: _ScriptPublicationTransaction,
        ready: tuple[ScriptChangeResult, ...],
    ):
        """Publish one complete ready closure under the collector lock."""
        from Infernux.components.registry import (
            snapshot_component_registry_state,
        )
        from Infernux.engine.play_mode import (
            PlayModeManager,
            ScriptReloadBatchInput,
        )

        profile_started = time.perf_counter()
        profile_marks: list[tuple[str, float]] = []

        def mark(label: str) -> None:
            profile_marks.append((label, time.perf_counter()))

        # This is intentionally inside the collector publication callback:
        # the source/hash check is repeated after claim and immediately before
        # any live, registry, or graph mutation.
        if any(not self._script_source_matches_disk(result) for result in ready):
            return False

        graph = self._dependency_graph
        ready_stage = None
        if graph is not None:
            ready_stage = graph.stage_transaction(
                {result.path: result.source for result in ready},
                removals=state.retire_paths,
            )
        mark("dependency_graph")

        registry_snapshot = snapshot_component_registry_state()
        mark("registry_snapshot")
        play_mode = PlayModeManager.instance()
        prepare_stable_batch = (
            getattr(play_mode, "prepare_script_reload_batch", None)
            if play_mode is not None
            else None
        )
        play_batch = None
        token = _ScriptPublicationRollback(
            registry_snapshot=registry_snapshot,
            graph_transaction=ready_stage,
        )
        try:
            ordered = self._ordered_script_results(state, ready_stage)
            mark("order")
            move_owner_key = (
                path_key(state.expected_paths[0])
                if state.expected_paths
                else ""
            )
            if callable(prepare_stable_batch):
                revisions = tuple(
                    ScriptReloadBatchInput(
                        file_path=result.path,
                        script_guid=(
                            self._asset_database.get_guid_from_path(result.path) or ""
                        ),
                        source=result.source,
                        code=(
                            result.artifact.code
                            if result.artifact is not None
                            else None
                        ),
                        retire_script_paths=(
                            state.retire_paths
                            if path_key(result.path) == move_owner_key
                            else ()
                        ),
                    )
                    for result in ordered
                )
                play_batch = prepare_stable_batch(revisions)
                mark("prepare_live_batch")
                token.play_batch = play_batch
                outcome = play_mode.commit_script_reload_batch(play_batch)
                mark("commit_live_batch")
                if not outcome.success:
                    play_mode.rollback_script_reload_batch(play_batch)
                    token.play_batch = None
                    return False
                for result in ordered:
                    self._publish_script_registry_member(result)
                mark("publish_registry")
            else:
                prepare_edit_batch = getattr(
                    play_mode,
                    "prepare_edit_script_reload_batch",
                    None,
                )
                if play_mode is None or not callable(prepare_edit_batch):
                    # Tooling can construct a resource handler without an
                    # active editor owner. A legacy single-script test/tool
                    # owner may also expose only the old reload entry point.
                    # Never emulate a multi-file live transaction through it.
                    if play_mode is not None and len(ordered) > 1:
                        raise RuntimeError(
                            "edit-mode multi-script reload requires the Edit batch owner API"
                        )
                    for result in ordered:
                        if not self._publish_valid_script(
                            result.path,
                            source=result.source,
                            code=(
                                result.artifact.code
                                if result.artifact is not None
                                else None
                            ),
                            catalog_event=result.change.effective_catalog_event,
                            _defer_post_commit=True,
                        ):
                            raise RuntimeError(
                                f"headless script publication rejected for {result.path}"
                            )
                else:
                    revisions = tuple(
                        ScriptReloadBatchInput(
                            file_path=result.path,
                            script_guid=(
                                self._asset_database.get_guid_from_path(result.path) or ""
                            ),
                            source=result.source,
                            code=(
                                result.artifact.code
                                if result.artifact is not None
                                else None
                            ),
                            retire_script_paths=(
                                state.retire_paths
                                if path_key(result.path) == move_owner_key
                                else ()
                            ),
                        )
                        for result in ordered
                    )
                    edit_batch = prepare_edit_batch(revisions)
                    token.edit_batch = edit_batch
                    play_mode.commit_edit_script_reload_batch(edit_batch)
                    if not edit_batch.committed:
                        return False

            if ready_stage is not None:
                mutation = graph.commit_transaction(ready_stage)
                token.graph_committed = True
                self._last_dependency_affected = tuple(mutation.affected)
            mark("commit_dependency_graph")
            total_ms = (time.perf_counter() - profile_started) * 1000.0
            if total_ms >= 25.0:
                previous = profile_started
                pieces = []
                for label, current in profile_marks:
                    pieces.append(f"{label}={(current - previous) * 1000.0:.2f}ms")
                    previous = current
                Debug.log_internal(
                    f"[ScriptReloadProfile] publish_detail total={total_ms:.2f}ms "
                    + " ".join(pieces)
                )
            return token
        except Exception as exc:
            self._rollback_script_publication(token)
            Debug.log_error(
                f"Script transaction publication failed "
                f"(transaction={state.transaction_id}): {exc}"
            )
            return False

    def _remove_script_transaction(self, state: _ScriptPublicationTransaction) -> None:
        self._script_transactions.pop(state.transaction_id, None)
        if self._initial_scan_transaction_id == state.transaction_id:
            self._initial_scan_transaction_id = None

    def _discard_failed_script_transaction(
        self,
        state: _ScriptPublicationTransaction,
    ) -> bool:
        if not state.complete():
            return False
        # A member can be superseded while another member of this transaction
        # is still current.  Discard only the surviving old-generation subset;
        # the collector owns the newer generation and must keep it pending.
        surviving_paths = []
        for path in state.expected_paths:
            result = state.results.get(path_key(path))
            latest = self._script_change_collector.latest(path)
            if (
                result is not None
                and latest is not None
                and latest.generation == result.generation
                and result.change.transaction_id == state.transaction_id
                and all(
                    item == state.transaction_id
                    for item in result.change.merged_transaction_ids
                )
            ):
                surviving_paths.append(path)
        discarded = True
        if surviving_paths:
            discarded = self._script_change_collector.abort_transaction(
                surviving_paths,
                transaction_id=state.transaction_id,
            )
            if not discarded:
                Debug.log_error(
                    f"Failed to discard surviving script transaction members "
                    f"(transaction={state.transaction_id})"
                )
        self._remove_script_transaction(state)
        return discarded

    def _try_publish_script_transaction(
        self,
        state: _ScriptPublicationTransaction,
    ) -> bool:
        if state.durable_token is not None:
            return self._complete_durable_script_publication(state)
        if state.failed:
            return self._discard_failed_script_transaction(state)
        if not state.complete():
            return False
        if any(not result.succeeded for result in state.results.values()):
            state.failed = True
            return self._discard_failed_script_transaction(state)
        publish_started = time.perf_counter()
        try:
            published = self._script_change_collector.publish_ready_batch(
                state.expected_paths,
                state.transaction_id,
                lambda ready: self._publish_script_transaction(state, ready),
                rollback=self._rollback_script_publication,
            )
        except Exception as exc:
            state.failed = True
            Debug.log_error(
                f"Script transaction publication raised "
                f"(transaction={state.transaction_id}): {exc}"
            )
            self._discard_failed_script_transaction(state)
            return False
        publish_elapsed_ms = (time.perf_counter() - publish_started) * 1000.0
        if publish_elapsed_ms >= 25.0:
            Debug.log_internal(
                f"[ScriptReloadProfile] publish={publish_elapsed_ms:.2f}ms "
                f"members={len(state.expected_paths)}"
            )
        if published is False:
            state.failed = True
            self._discard_failed_script_transaction(state)
            return False
        if isinstance(published, _ScriptPublicationRollback):
            # publish_ready_batch has durably advanced LKG at this point.  It
            # is now safe to close the native CDS rollback window.  Store the
            # token first: finalization may be retried, but publication may
            # never be replayed after this edge.
            state.durable_token = published
            state.durable_ordered = self._ordered_script_results(
                state,
                published.graph_transaction,
            )
            return self._complete_durable_script_publication(state)
        self._remove_script_transaction(state)
        return True

    def _finalize_script_move(self, state: _ScriptPublicationTransaction) -> None:
        """Retire old script identities only after the collector commit edge."""
        from Infernux.components.script_loader import (
            clear_deleted_script_errors,
            retire_script_module,
        )

        if not state.expected_paths:
            return
        new_path = state.expected_paths[0]
        manager = ResourcesManager.instance()
        callbacks = []
        for old_path in state.retire_paths:
            clear_deleted_script_errors(old_path)
            Debug.clear_source_entries(old_path)
            retire_script_module(old_path)
            if manager is not None:
                callbacks.extend(
                    manager._script_reload_callbacks.pop(path_key(old_path), [])
                )
        if manager is not None and callbacks:
            manager._script_reload_callbacks.setdefault(path_key(new_path), []).extend(
                callback
                for callback in callbacks
                if callback not in manager._script_reload_callbacks.get(path_key(new_path), [])
            )

    def _drain_script_results(self) -> int:
        """Consume frontend output and publish complete dependency closures."""
        completed = self._script_change_collector.drain_completed()
        initial_scan_count = 0
        for result in completed:
            if result.change.transaction_id in self._retired_script_transactions:
                continue
            if result.change.change_kind == "initial_scan":
                initial_scan_count += 1
            state = self._transaction_for_result(result)
            state.results[result.change.identity_key] = result
            if not result.succeeded:
                self._record_frontend_failure(result)
                state.failed = True
                continue
            # An initial scan owns the dependency graph baseline.  Results
            # arriving from the watcher during that window are retained but
            # cannot calculate a closure against the empty graph.
            if self._initial_scan_transaction_id is None:
                self._seed_script_dependency_closure(state, result)

        # A normal revision can supersede one member of the large startup
        # barrier before that member's old frontend result is drained.  Retire
        # and rebuild the barrier as one owner-thread operation, using the
        # current bytes for every still-existing original member.  This is
        # deliberately checked once per drain round so a burst of edits causes
        # one restart rather than a restart storm.
        initial_state = None
        if self._initial_scan_transaction_id is not None:
            initial_state = self._script_transactions.get(
                self._initial_scan_transaction_id
            )
        if (
            initial_state is not None
            and self._initial_scan_superseded(initial_state)
        ):
            self._restart_initial_scan_transaction(initial_state)

        # Finish the initial transaction first.  Once its graph/LKG barrier is
        # gone, seed any ordinary transactions that arrived concurrently.
        for state in tuple(self._script_transactions.values()):
            if state.initial_scan:
                self._try_publish_script_transaction(state)
        if self._initial_scan_transaction_id is None:
            for state in tuple(self._script_transactions.values()):
                if state.initial_scan or state.failed or state.closure_seeded:
                    continue
                roots = tuple(
                    result
                    for result in state.results.values()
                    if result.succeeded
                    and result.change.change_kind != "dependency"
                    and result.change.origin != "dependency"
                )
                if roots:
                    self._seed_script_dependency_closure(state, roots[0])
        # Ordinary transactions may accumulate while the startup barrier is
        # active, but they cannot publish against a partially built baseline.
        # They are revisited on the owner safe point after the barrier clears.
        if self._initial_scan_transaction_id is None:
            for state in tuple(self._script_transactions.values()):
                if not state.initial_scan:
                    self._try_publish_script_transaction(state)
        return initial_scan_count

    def _dispatch_event(self, event: AssetFsEvent) -> None:
        from Infernux.core.assets import AssetManager
        if event.kind is AssetFsEventKind.META_DELETED:
            # META_DELETED is advisory: watchdog may deliver it after the
            # owning asset was renamed/deleted or after an atomic sidecar
            # replace has already completed.  Do not enter the main-thread
            # import transaction for either stale case.
            if not os.path.isfile(event.path) or os.path.isfile(event.path + ".meta"):
                return
            if AssetManager.is_meta_watcher_suppressed(event.path):
                Debug.log_internal(f"[AssetManager] suppressed meta watcher echo: {event}")
                return
        elif AssetManager.is_watcher_echo_suppressed(
            event.kind.value,
            event.path,
            event.destination,
        ):
            Debug.log_internal(f"[AssetManager] suppressed watcher echo: {event}")
            return
        from Infernux.engine.interaction import ActionOrigin, action_origin_scope

        with action_origin_scope(ActionOrigin.EXTERNAL):
            if event.kind is AssetFsEventKind.CREATED:
                self._commit_created(event.path)
            elif event.kind is AssetFsEventKind.MODIFIED:
                self._commit_modified(event.path)
            elif event.kind is AssetFsEventKind.DELETED:
                self._commit_deleted(event.path, guid_hint=event.guid_hint)
            elif event.kind is AssetFsEventKind.MOVED:
                self._commit_moved(event.path, event.destination)
            elif event.kind is AssetFsEventKind.META_DELETED:
                self._process_meta_missing_rebuild(event.path)
            else:
                raise RuntimeError(f"Unhandled asset event kind: {event.kind}")

    def _commit_created(self, path: str) -> None:
        if not os.path.isfile(path):
            raise _AssetImportNotReady(f"created file is not ready: {path}")
        from Infernux.core.assets import AssetManager
        try:
            result = AssetManager.import_asset(
                path,
                database=self._asset_database,
                suppress_watcher_echo=False,
            )
            if not result:
                detail = str(getattr(result, "error", "") or "unknown import error")
                raise _AssetImportNotReady(f"import failed: {path}: {detail}")
        except RuntimeError as exc:
            raise _AssetImportNotReady(str(exc)) from exc
        if path.lower().endswith(".py") and not _is_particle_script_path(path):
            self._check_script(path, catalog_event="created")
        elif path.lower().endswith((".vert", ".frag")):
            # First import only writes metadata. Shader GPU modules are
            # published by reimport, the same edge effect dependencies use.
            published = AssetManager.reimport_asset(
                path,
                database=self._asset_database,
                suppress_watcher_echo=False,
            )
            if not published:
                detail = str(
                    getattr(published, "error", "") or "unknown reimport error"
                )
                raise _AssetImportNotReady(f"shader publish failed: {path}: {detail}")
            self._notify_shader_reloaded(path)

    def _commit_modified(self, path: str) -> None:
        if not os.path.isfile(path):
            raise _AssetImportNotReady(f"modified file is not ready: {path}")
        from Infernux.core.assets import AssetManager
        from Infernux.engine.interaction import DocumentRegistry

        documents = DocumentRegistry.instance()
        # A self-write watcher event may arrive before AssetManager has polled
        # its ticket. Acknowledge an exact committed fingerprint and defer an
        # incomplete write; neither path is an external edit.
        local_write_state = AssetManager.local_write_event_state(path)
        if local_write_state == "ack":
            return
        if local_write_state == "pending":
            raise _AssetLocalWritePending(
                f"local document write is still pending: {path}"
            )
        durable_change = documents.durable_resource_content_changed(path)
        if durable_change is None:
            raise _AssetImportNotReady(
                f"durable resource identity is not ready: {path}"
            )
        if durable_change is False:
            return
        if not documents.preflight_external_resource_change(path):
            return
        script_change = path.lower().endswith(".py") and not _is_particle_script_path(path)
        profile_started = time.perf_counter() if script_change else 0.0
        was_registered = self._asset_database.contains_path(path)
        import_started = time.perf_counter() if script_change else 0.0
        try:
            if was_registered:
                result = AssetManager.reimport_asset(
                    path,
                    database=self._asset_database,
                    suppress_watcher_echo=False,
                )
                if not result:
                    detail = str(
                        getattr(result, "error", "") or "unknown reimport error"
                    )
                    raise _AssetImportNotReady(
                        f"reimport failed: {path}: {detail}"
                    )
            else:
                result = AssetManager.import_asset(
                    path,
                    database=self._asset_database,
                    suppress_watcher_echo=False,
                )
                if not result:
                    detail = str(
                        getattr(result, "error", "") or "unknown import error"
                    )
                    raise _AssetImportNotReady(
                        f"import failed: {path}: {detail}"
                    )
            # AssetMutationService normally consumes this preflight while
            # publishing a registered reimport. If it did not, finalize the
            # same successful external change here for both branches.
            if documents.has_pending_external_change_preflight(path):
                documents.publish_external_resource_change(path)
        except Exception as exc:
            documents.fail_external_resource_change(path, message=str(exc))
            raise
        if script_change:
            import_elapsed_ms = (time.perf_counter() - import_started) * 1000.0
            self._check_script(path, catalog_event="modified")
            elapsed_ms = (time.perf_counter() - profile_started) * 1000.0
            if elapsed_ms >= 25.0:
                Debug.log_internal(
                    f"[ScriptReloadProfile] asset_reimport={import_elapsed_ms:.2f}ms "
                    f"owner_submit={elapsed_ms:.2f}ms file={os.path.basename(path)}"
                )
        elif path.lower().endswith((".vert", ".frag")):
            self._notify_shader_reloaded(path)

    def _commit_deleted(self, path: str, *, guid_hint: str = "") -> None:
        from Infernux.core.assets import AssetManager
        # A replace-in-place write may surface as a delete followed by a move
        # or create. Watcher delivery can be reordered, so never let a stale
        # delete event remove a newly published asset from disk/database.
        if os.path.isfile(path):
            self._commit_modified(path)
            return
        from Infernux.engine.interaction import DocumentRegistry

        documents = DocumentRegistry.instance()
        durable_change = documents.durable_resource_content_changed(
            path,
            deleted=True,
        )
        if durable_change is None:
            raise _AssetImportNotReady(
                f"durable resource identity is not ready: {path}"
            )
        if not durable_change or not documents.preflight_external_resource_change(
            path,
            deleted=True,
        ):
            return
        if not AssetManager.delete_asset(
            path,
            database=self._asset_database,
            suppress_watcher_echo=False,
            guid_hint=guid_hint,
        ):
            raise RuntimeError(f"asset deletion failed: {path}")
        if path.lower().endswith(".py") and not _is_particle_script_path(path):
            from Infernux.components.script_loader import clear_deleted_script_errors
            from Infernux.components.registry import unregister_component_script
            clear_deleted_script_errors(path)
            Debug.clear_source_entries(path)
            unregister_component_script(path)
            if self._dependency_graph is not None:
                mutation = self._dependency_graph.remove(path)
                self._last_dependency_affected = tuple(mutation.affected)
            manager = ResourcesManager.instance()
            if manager is not None:
                manager.notify_script_catalog_changed(path, "deleted")

    def _commit_moved(self, old_path: str, new_path: str) -> None:
        if not os.path.isfile(new_path):
            raise _AssetImportNotReady(f"moved file is not ready: {new_path}")
        from Infernux.core.assets import AssetManager
        if not AssetManager.move_asset(
            old_path,
            new_path,
            database=self._asset_database,
            suppress_watcher_echo=False,
            origin="external",
        ):
            raise RuntimeError(f"asset move failed: {old_path} -> {new_path}")
        if new_path.lower().endswith(".py") and not _is_particle_script_path(new_path):
            self._submit_moved_script(old_path, new_path, origin="watchdog")
        elif new_path.lower().endswith((".vert", ".frag")):
            if not AssetManager.reimport_asset(
                new_path,
                database=self._asset_database,
                suppress_watcher_echo=False,
            ):
                raise RuntimeError(f"moved shader reimport failed: {new_path}")
            self._notify_shader_reloaded(new_path)

    def _process_meta_missing_rebuild(self, owner_path: str):
        """Handle a deleted .meta sidecar (watchdog-driven, main thread).

        Deleting a .meta while the engine runs should immediately:
          1. Reimport (or import) through the canonical AssetDatabase transaction,
             which regenerates the sidecar while preserving the in-memory GUID.
          2. Reload the live resource + GPU caches so the change takes effect now.
        """
        Debug.log_internal(f"[Meta Missing] regenerate + reload for {owner_path}")
        if not owner_path or not os.path.isfile(owner_path):
            return
        if os.path.isfile(owner_path + ".meta"):
            return

        from Infernux.core.assets import AssetManager
        if AssetManager.is_meta_watcher_suppressed(owner_path):
            Debug.log_internal(f"[Meta Missing] suppressed rebuild echo for {owner_path}")
            return

        if self._asset_database.contains_path(owner_path):
            result = AssetManager.reimport_asset(
                owner_path,
                database=self._asset_database,
                suppress_watcher_echo=False,
            )
        else:
            result = AssetManager.import_asset(
                owner_path,
                database=self._asset_database,
                suppress_watcher_echo=False,
            )
        if not result:
            # DocumentStore may have already republished the sidecar even when
            # a subsequent runtime reload failed; treat that as recovered.
            if os.path.isfile(owner_path + ".meta"):
                AssetManager.invalidate_project_panel_cache()
                return
            detail = getattr(result, "error", "") or "asset mutation failed"
            raise _AssetImportNotReady(
                f"failed to rebuild missing metadata: {owner_path}: {detail}"
            )
        AssetManager.invalidate_project_panel_cache()

    def _check_script(
        self,
        file_path: str,
        *,
        catalog_event: str | None = "modified",
        origin: str = "watchdog",
        change_kind: str | None = None,
        transaction_id: str | None = None,
        force: bool | None = None,
    ):
        """Capture exact bytes and submit one immutable frontend revision."""
        if not os.path.exists(file_path):
            return None

        with open(file_path, "rb") as source_file:
            source = source_file.read()
        if change_kind is None:
            change_kind = catalog_event if catalog_event in {
                "created",
                "modified",
                "deleted",
                "moved",
                "renamed",
            } else "modified"
        change = self._script_change_collector.submit(
            file_path,
            source,
            origin=origin,
            transaction_id=transaction_id,
            catalog_event=catalog_event,
            change_kind=change_kind,
            force=force,
        )
        if change is not None and callable(self._frontend_wake):
            self._frontend_wake()
        if change is not None and transaction_id is not None:
            state = self._script_transactions.get(transaction_id)
            if state is not None:
                state.expected_generations[path_key(file_path)] = change.generation
        return change

    def _submit_moved_script(
        self,
        old_path: str,
        new_path: str,
        *,
        origin: str = "watchdog",
    ):
        """Queue a moved script as one candidate transaction.

        The durable move has already succeeded when this method is called.
        The old path is metadata only until the new candidate, dependency
        graph, live reload, registry, and collector journal commit together.
        """
        transaction_id = self.begin_script_transaction(
            (new_path,),
            retire_paths=(old_path,),
        )
        change = self._check_script(
            new_path,
            origin=origin,
            catalog_event="moved",
            change_kind="moved",
            transaction_id=transaction_id,
            force=True,
        )
        if change is None:
            state = self._script_transactions.get(transaction_id)
            if state is not None:
                self._remove_script_transaction(state)
        return change

    def _publish_script_revisions(self) -> int:
        """Compatibility safe point for callers that do not own a watcher."""
        if not self._frontend_worker_running:
            self.process_script_worker()
        return self._drain_script_results()

    def _publish_valid_script(
        self,
        file_path: str,
        *,
        source: bytes,
        code: types.CodeType | None = None,
        catalog_event: str | None,
        _defer_post_commit: bool = False,
    ) -> bool:
        """Apply the existing registry/reload behavior for one LKG candidate."""
        from Infernux.components.script_loader import _clear_script_error
        from Infernux.components.registry import register_component_script

        from Infernux.engine.play_mode import PlayModeManager
        play_mode = PlayModeManager.instance()
        if play_mode:
            outcome = play_mode.reload_components_from_script_result(
                file_path,
                source=source,
                code=code,
            )
            if not outcome.success:
                detail = outcome.error or "live component reload was rejected"
                raise RuntimeError(
                    f"Script reload did not publish for {os.path.basename(file_path)}: {detail}"
                )

        # Publish catalog/error state only after every live target accepted the
        # candidate. A rejected body must leave the previous registry and
        # diagnostic state intact alongside the previous runtime revision.
        register_component_script(file_path, source=source)
        if _defer_post_commit:
            return True
        _clear_script_error(file_path)
        Debug.log_internal(f"[OK] Script OK: {os.path.basename(file_path)}")
        rm = ResourcesManager.instance()
        if rm is not None and catalog_event is not None:
            rm.notify_script_catalog_changed(file_path, catalog_event)
        abs_path = path_key(file_path)
        if rm is not None:
            for cb in list(rm._script_reload_callbacks.get(abs_path, [])):
                cb(file_path)
        return True

    def _notify_shader_reloaded(self, file_path: str):
        """Invalidate editor shader caches after the canonical reimport succeeds."""
        # Invalidate shader caches in UI
        for callback in self._shader_cache_invalidation_callbacks:
            callback()
        Debug.log_internal(f"[OK] Shader reloaded: {os.path.basename(file_path)}")
    
    def register_shader_cache_callback(self, callback):
        """Register a callback to be called when shader cache should be invalidated."""
        if callback not in self._shader_cache_invalidation_callbacks:
            self._shader_cache_invalidation_callbacks.append(callback)

    def cleanup(self) -> None:
        """Release collector and event queues after the owner has drained them."""
        self._frontend_worker_running = False
        self._script_change_collector.shutdown()
        self._coordinator.clear()
        self._stale_script_revisions.clear()
        self._script_transactions.clear()
        self._retired_script_transactions.clear()
        self._initial_scan_transaction_id = None

class ResourcesManager:
    _instance: 'ResourcesManager | None' = None

    @classmethod
    def instance(cls) -> 'ResourcesManager | None':
        """Return the active ResourcesManager singleton, or None."""
        return cls._instance

    def __init__(self, project_path: str, engine: Infernux):
        ResourcesManager._instance = self
        self._engine = engine
        self._project_path = resolved_path(project_path)
        self._assets_path = resolved_path(os.path.join(self._project_path, "Assets"))
        self._observer = None
        self._observer_lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()
        self._frontend_wake_event = threading.Event()
        self._event_handler = None
        self._frontend_worker_running = False
        self._script_reload_callbacks = {}  # file_path -> [callbacks]
        self._script_catalog_callbacks = []  # [callback(file_path, event_type)]
        self._initial_scan_lock = threading.Lock()
        self._initial_scan_artifact = None
        self._startup_prepared = False
        self._skip_initial_scan = False

    def _shutdown_observer(self, *, join_timeout: float = 5.0) -> bool:
        """Stop and join the currently published watchdog observer."""
        with self._observer_lock:
            observer = self._observer
            self._observer = None
        if observer is None:
            return True

        try:
            observer.stop()
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")

        try:
            observer.unschedule_all()
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")

        try:
            observer.join(timeout=join_timeout)
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")

        if getattr(observer, "is_alive", lambda: False)():
            Debug.log_warning("ResourcesManager observer did not stop cleanly before timeout")
            with self._observer_lock:
                if self._observer is None:
                    self._observer = observer
            return False
        return True

    def start(self, *, skip_initial_scan: bool = False):
        """
        Start to scan the project directory for resources in a sub-thread.
        """
        if not _HAS_WATCHDOG:
            return  # watchdog not available (standalone player build)
        if self._thread and self._thread.is_alive():
            Debug.log_warning("ResourcesManager is already running")
            return
        self._skip_initial_scan = bool(skip_initial_scan or self._startup_prepared)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._scan_resources,
            daemon=False,
            name="InfernuxResourceWatcher",
        )
        self._thread.start()

    def _ensure_event_handler(self):
        if self._event_handler is None:
            self._event_handler = ResourceChangeHandler(
                self._engine,
                project_path=self._project_path,
                frontend_wake=self._frontend_wake_event.set,
            )
        return self._event_handler

    def _startup_work_pending(self) -> bool:
        handler = self._event_handler
        if handler is None:
            return False
        if getattr(handler, "_initial_scan_transaction_id", None):
            return True
        if handler.pending_count:
            return True
        collector = getattr(handler, "_script_change_collector", None)
        if collector is not None and (
            collector.pending_count or collector.completed_count
        ):
            return True
        return False

    def _wait_for_startup_idle(self) -> None:
        deadline = time.monotonic() + 180.0
        idle_rounds = 0
        while time.monotonic() < deadline:
            processed = self.process_pending_reloads(force=True)
            if not self._startup_work_pending() and processed == 0:
                idle_rounds += 1
                if idle_rounds >= 2:
                    return
            else:
                idle_rounds = 0
            time.sleep(0)
        raise RuntimeError(
            "startup resource refresh did not finish before the engine window opened"
        )

    def prepare_startup(self, on_progress=None) -> None:
        """Finish the startup script refresh before the engine window is shown.

        ``start()`` used to run this scan after ``Engine.show()``. The owner
        thread then published the barrier one poll at a time and the first
        minutes of the session hitch. The window must not appear until the
        ready-barrier has been published.
        """
        if self._startup_prepared:
            if not self.is_running():
                self.start(skip_initial_scan=True)
            return
        if on_progress:
            on_progress("Scanning project scripts...")
        self._ensure_event_handler()
        self._initial_script_scan()
        if on_progress:
            on_progress("Publishing project scripts...")
        self._wait_for_startup_idle()
        self._startup_prepared = True
        self.start(skip_initial_scan=True)

    def _scan_resources(self):
        """
        Use watchdog to monitor file changes in _assets_path.
        """
        if not os.path.exists(self._assets_path):
            Debug.log_warning(f"Assets path not found: {self._assets_path}")
            return
        if self._stop_event.is_set():
            return

        handler = self._ensure_event_handler()
        self._frontend_worker_running = True
        handler.set_frontend_worker_running(True)
        observer = Observer()
        try:
            observer.daemon = False
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")

        try:
            observer.schedule(handler, self._assets_path, recursive=True)
            with self._observer_lock:
                if self._stop_event.is_set():
                    return
                self._observer = observer
                try:
                    observer.start()
                except Exception:
                    self._observer = None
                    raise

            # Initial full scan submits exact snapshots to the same collector
            # used by watchdog events.  The watcher owns the frontend budget
            # unless prepare_startup() already published that barrier.
            if not self._skip_initial_scan:
                self._initial_script_scan()

            while not self._stop_event.is_set():
                self._frontend_wake_event.wait(timeout=1.0)
                self._frontend_wake_event.clear()
                handler.process_script_worker(max_items=32)
        finally:
            self._frontend_worker_running = False
            if self._event_handler is not None:
                self._event_handler.set_frontend_worker_running(False)
            self._shutdown_observer(join_timeout=5.0)

    def _initial_script_scan(self):
        """Walk Assets/ and syntax-check every .py file.

        ``prepare_startup()`` runs this on the owner thread before the
        engine window is shown. The watcher thread only repeats it when
        that barrier was not already published.
        """
        self._ensure_event_handler()
        script_paths = []
        for root, dirs, files in os.walk(self._assets_path):
            if self._stop_event.is_set():
                return
            dirs[:] = [d for d in dirs if d != '__pycache__']
            for fname in files:
                if not fname.endswith('.py') or _is_particle_script_path(fname):
                    continue
                fpath = os.path.join(root, fname)
                if self._event_handler is None:
                    continue
                script_paths.append(fpath)
        transaction_id = None
        if script_paths and self._event_handler is not None:
            transaction_id = self._event_handler.begin_script_transaction(
                script_paths,
                initial_scan=True,
            )
        submitted = 0
        for fpath in script_paths:
            if self._event_handler is None:
                continue
            if self._event_handler._check_script(
                fpath,
                catalog_event=None,
                origin="initial_scan",
                change_kind="initial_scan",
                transaction_id=transaction_id,
            ) is not None:
                submitted += 1
        if submitted:
            Debug.log_internal(f"Startup scan queued {submitted} script revision(s)")

    def process_pending_reloads(self, *, force: bool = False) -> int:
        """Commit worker artifacts and asset events on the main thread."""
        if self._event_handler is None:
            return 0
        if force or not self._frontend_worker_running:
            self._event_handler.process_script_worker()
        processed = 0
        processed += self._event_handler.process_pending_reloads(force=force)
        return processed

    def submit_script_change(
        self,
        file_path: str,
        *,
        origin: str,
        catalog_event: str | None = "modified",
        change_kind: str | None = None,
        transaction_id: str | None = None,
        force: bool | None = None,
    ):
        """Submit an internal script ingress without bypassing the collector.

        Internal asset mutations are project-scoped.  The manager wrapper
        keeps callers from accidentally submitting a script outside Assets and
        preserves the same owner/worker split as watchdog changes.
        """
        if not is_path_within(file_path, self._assets_path, allow_root=False):
            return None
        if self._event_handler is None:
            self._event_handler = ResourceChangeHandler(
                self._engine,
                project_path=self._project_path,
                frontend_wake=self._frontend_wake_event.set,
            )
        return self._event_handler._check_script(
            file_path,
            origin=origin,
            catalog_event=catalog_event,
            change_kind=change_kind,
            transaction_id=transaction_id,
            force=force,
        )

    def drain_pending_events(self) -> int:
        """Force all bounded retries to finish after the observer has stopped."""
        processed = 0
        for _ in range(16):
            processed += self.process_pending_reloads(force=True)
            if self._event_handler is None or self._event_handler.pending_count == 0:
                return processed
        raise RuntimeError("asset event queue did not drain after observer shutdown")

    def register_script_reload_callback(self, file_path: str, callback) -> None:
        """Subscribe *callback(file_path)* to be called when *file_path* is saved.

        Called on the main thread after a successful syntax check.
        Safe to call multiple times (duplicates are ignored).
        """
        abs_path = path_key(file_path)
        cbs = self._script_reload_callbacks.setdefault(abs_path, [])
        if callback not in cbs:
            cbs.append(callback)

    def unregister_script_reload_callback(self, callback) -> None:
        """Remove *callback* from all file-path subscriptions."""
        for cbs in self._script_reload_callbacks.values():
            if callback in cbs:
                cbs.remove(callback)

    def register_script_catalog_callback(self, callback) -> None:
        """Subscribe to global Python script catalog changes.

        Callback signature: ``callback(file_path, event_type)`` where
        ``event_type`` is one of ``modified``, ``deleted``, ``moved``.
        """
        if callback not in self._script_catalog_callbacks:
            self._script_catalog_callbacks.append(callback)

    def unregister_script_catalog_callback(self, callback) -> None:
        """Unsubscribe from global Python script catalog changes."""
        if callback in self._script_catalog_callbacks:
            self._script_catalog_callbacks.remove(callback)

    def notify_script_catalog_changed(self, file_path: str, event_type: str) -> None:
        """Notify listeners that Python script catalog may have changed."""
        started = time.perf_counter()
        for cb in list(self._script_catalog_callbacks):
            try:
                cb(file_path, event_type)
            except Exception as e:
                Debug.log_error(f"Script catalog callback failed: {e}")
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms >= 10.0:
            Debug.log_internal(
                f"[ScriptReloadProfile] catalog_callbacks={elapsed_ms:.2f}ms "
                f"listeners={len(self._script_catalog_callbacks)} "
                f"file={os.path.basename(file_path)} event={event_type}"
            )

    def reload_moved_script(self, old_path: str, new_path: str) -> None:
        """Queue a GUID-stable script move after the durable asset move."""
        if self._event_handler is None:
            return
        self._event_handler._submit_moved_script(
            old_path,
            new_path,
            origin="editor",
        )

    def register_shader_cache_callback(self, callback):
        """Register a callback to be called when shader cache should be invalidated."""
        if self._event_handler:
            self._event_handler.register_shader_cache_callback(callback)

    def stop(self):
        """
        Stop the resource monitoring and clean up resources.
        """
        self._stop_event.set()
        self._frontend_wake_event.set()

        # Stop watchdog immediately from the calling thread as well. This makes
        # shutdown robust even if the worker thread is blocked or delayed.
        self._shutdown_observer(join_timeout=5.0)

        # Join the scanning thread (its finally block handles observer teardown).
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                raise RuntimeError("ResourcesManager thread did not stop before timeout")

        if not self._shutdown_observer(join_timeout=5.0):
            raise RuntimeError("ResourcesManager observer did not stop before timeout")

    def is_running(self):
        """
        Check if the ResourcesManager is currently running.
        
        Returns:
            bool: True if the manager is running, False otherwise.
        """
        return (self._thread is not None and 
                self._thread.is_alive() and 
                not self._stop_event.is_set())

    def cleanup(self):
        """
        Clean up all resources and stop monitoring.
        This method ensures complete cleanup of the ResourcesManager.
        """
        self.stop()
        self.drain_pending_events()

        if self._event_handler is not None:
            self._event_handler.cleanup()

        # Reset internal state
        self._observer = None
        self._thread = None
        self._engine = None
        self._event_handler = None
        self._frontend_worker_running = False
        self._startup_prepared = False
        self._skip_initial_scan = False
        self._script_reload_callbacks.clear()
        self._script_catalog_callbacks.clear()
        if ResourcesManager._instance is self:
            ResourcesManager._instance = None
        
        Debug.log_internal("ResourcesManager cleanup completed")
