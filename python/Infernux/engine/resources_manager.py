import os
import threading

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
from Infernux.engine.path_utils import path_key, portable_path
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


def _is_particle_script_path(file_path: str) -> bool:
    return str(file_path or "").lower().endswith(".particle.py")


class ResourceChangeHandler(FileSystemEventHandler):

    def __init__(self, engine: Infernux):
        self._engine = engine
        self._script_compiler = get_script_compiler()
        self._coordinator = ImportCoordinator()
        self._shader_cache_invalidation_callbacks = []
        self._asset_database = engine.get_asset_database()
        if self._asset_database is None:
            raise RuntimeError("ResourceChangeHandler requires an initialized AssetDatabase")

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
        self._coordinator.submit(
            AssetFsEventKind.CREATED,
            event.src_path,
            guid_hint=read_meta_guid(event.src_path),
        )

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
        self._coordinator.submit(AssetFsEventKind.MODIFIED, event.src_path)

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
        )

    @property
    def pending_count(self) -> int:
        return self._coordinator.pending_count

    def process_pending_reloads(self, *, force: bool = False) -> int:
        """Commit coalesced events on the AssetDatabase owner thread."""
        events = self._coordinator.drain(force=force)
        for event in events:
            try:
                self._dispatch_event(event)
            except _AssetImportNotReady as exc:
                if not self._coordinator.retry(event):
                    Debug.log_error(f"Asset event exhausted retries: {event}: {exc}")
            except Exception as exc:
                Debug.log_error(f"Asset event failed: {event}: {exc}")
        return len(events)

    def _dispatch_event(self, event: AssetFsEvent) -> None:
        from Infernux.core.assets import AssetManager
        if event.kind is AssetFsEventKind.META_DELETED:
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
                if not os.path.isfile(event.path + ".meta"):
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
            raise _AssetImportNotReady(
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
        was_registered = self._asset_database.contains_path(path)
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
        if path.lower().endswith(".py") and not _is_particle_script_path(path):
            self._check_script(path, catalog_event="modified")
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
            unregister_component_script(path)
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
            from Infernux.components.script_loader import clear_deleted_script_errors
            from Infernux.components.registry import unregister_component_script
            clear_deleted_script_errors(old_path)
            unregister_component_script(old_path)
            manager = ResourcesManager.instance()
            if manager is not None:
                manager.notify_script_catalog_changed(new_path, "moved")
            self._check_script(new_path, catalog_event=None)
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

    def _check_script(self, file_path: str, *, catalog_event: str | None = "modified"):
        """Check a Python script for syntax errors and hot-reload components."""
        # Verify file exists and is readable (ensures write is complete)
        if not os.path.exists(file_path):
            return

        errors = self._script_compiler.check_file(file_path)
        if errors:
            from Infernux.components.script_loader import set_script_error
            from Infernux.components.registry import unregister_component_script
            from Infernux.engine.play_mode import PlayModeManager
            play_mode = PlayModeManager.instance()
            # A syntax error must not remove the last valid component class
            # while Play/Pause is using it. Edit mode retains its established
            # catalog behavior for invalid scripts.
            if play_mode is None or not play_mode.is_playing:
                unregister_component_script(file_path)
            combined = "\n".join(
                f"{os.path.basename(e.file_path)}:{e.line_number}  {e.message}"
                for e in errors
            )
            set_script_error(file_path, combined)
            for error in errors:
                Debug.log_error(
                    f"Script Error in {os.path.basename(error.file_path)}:{error.line_number}\n{error.message}",
                    source_file=error.file_path,
                    source_line=error.line_number)
        else:
            from Infernux.components.script_loader import _clear_script_error
            from Infernux.components.registry import register_component_script
            _clear_script_error(file_path)
            register_component_script(file_path)
            Debug.log_internal(f"[OK] Script OK: {os.path.basename(file_path)}")
            rm = ResourcesManager.instance()
            if rm is not None and catalog_event is not None:
                rm.notify_script_catalog_changed(file_path, catalog_event)
            # Notify registered per-file callbacks (e.g. RenderStack pipeline reload)
            # Callbacks are stored on ResourcesManager to avoid handler-init races
            abs_path = path_key(file_path)
            if rm is not None:
                for cb in list(rm._script_reload_callbacks.get(abs_path, [])):
                    cb(file_path)
            # Hot-reload InxComponents from this script
            from Infernux.engine.play_mode import PlayModeManager
            play_mode = PlayModeManager.instance()
            if play_mode:
                play_mode.reload_components_from_script(file_path)

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

class ResourcesManager:
    _instance: 'ResourcesManager | None' = None

    @classmethod
    def instance(cls) -> 'ResourcesManager | None':
        """Return the active ResourcesManager singleton, or None."""
        return cls._instance

    def __init__(self, project_path: str, engine: Infernux):
        ResourcesManager._instance = self
        self._engine = engine
        self._assets_path = os.path.join(project_path, "Assets")
        self._observer = None
        self._observer_lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()
        self._event_handler = None
        self._script_reload_callbacks = {}  # file_path -> [callbacks]
        self._script_catalog_callbacks = []  # [callback(file_path, event_type)]
        self._initial_scan_lock = threading.Lock()
        self._initial_scan_artifact = None

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

    def start(self):
        """
        Start to scan the project directory for resources in a sub-thread.
        """
        if not _HAS_WATCHDOG:
            return  # watchdog not available (standalone player build)
        if self._thread and self._thread.is_alive():
            Debug.log_warning("ResourcesManager is already running")
            return
            
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._scan_resources,
            daemon=False,
            name="InfernuxResourceWatcher",
        )
        self._thread.start()

    def _scan_resources(self):
        """
        Use watchdog to monitor file changes in _assets_path.
        """
        if not os.path.exists(self._assets_path):
            Debug.log_warning(f"Assets path not found: {self._assets_path}")
            return
        if self._stop_event.is_set():
            return

        self._event_handler = ResourceChangeHandler(self._engine)
        observer = Observer()
        try:
            observer.daemon = False
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")

        try:
            observer.schedule(self._event_handler, self._assets_path, recursive=True)
            with self._observer_lock:
                if self._stop_event.is_set():
                    return
                self._observer = observer
                try:
                    observer.start()
                except Exception:
                    self._observer = None
                    raise

            # Initial full scan: check every .py file in Assets/ so that
            # pre-existing script errors are detected on engine startup.
            self._initial_script_scan()

            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=0.25)  # wake quickly on shutdown
        finally:
            self._shutdown_observer(join_timeout=5.0)

    def _initial_script_scan(self):
        """Walk Assets/ and syntax-check every .py file.

        Called once from the watchdog thread right after the observer
        starts so that errors present *before* the engine was opened
        are detected immediately.
        """
        from Infernux.engine.script_compiler import ScriptCompiler

        compiler = ScriptCompiler()
        results = []
        for root, dirs, files in os.walk(self._assets_path):
            if self._stop_event.is_set():
                return
            dirs[:] = [d for d in dirs if d != '__pycache__']
            for fname in files:
                if not fname.endswith('.py') or _is_particle_script_path(fname):
                    continue
                fpath = os.path.join(root, fname)
                errors = tuple(compiler.check_file(fpath))
                results.append((fpath, errors))
                from Infernux.components.registry import (
                    register_component_script,
                    unregister_component_script,
                )
                if errors:
                    unregister_component_script(fpath)
                else:
                    register_component_script(fpath)

        with self._initial_scan_lock:
            if not self._stop_event.is_set():
                self._initial_scan_artifact = tuple(results)

    def _commit_initial_script_scan(self) -> int:
        with self._initial_scan_lock:
            artifact = self._initial_scan_artifact
            self._initial_scan_artifact = None
        if artifact is None:
            return 0

        from Infernux.components.script_loader import set_script_error, _clear_script_error

        error_count = 0
        for file_path, errors in artifact:
            if not errors:
                _clear_script_error(file_path)
                continue
            combined = "\n".join(
                f"{os.path.basename(error.file_path)}:{error.line_number}  {error.message}"
                for error in errors
            )
            set_script_error(file_path, combined)
            error_count += 1
            for error in errors:
                Debug.log_error(
                    f"Script Error in {os.path.basename(error.file_path)}:{error.line_number}\n{error.message}",
                    source_file=error.file_path,
                    source_line=error.line_number,
                )
        if error_count:
            Debug.log_warning(f"Startup scan: {error_count} script(s) with errors")
        else:
            Debug.log_internal("All scripts passed startup validation")
        return len(artifact)

    def process_pending_reloads(self, *, force: bool = False) -> int:
        """Commit worker artifacts and asset events on the main thread."""
        processed = self._commit_initial_script_scan()
        if self._event_handler:
            processed += self._event_handler.process_pending_reloads(force=force)
        return processed

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
        for cb in list(self._script_catalog_callbacks):
            try:
                cb(file_path, event_type)
            except Exception as e:
                Debug.log_error(f"Script catalog callback failed: {e}")

    def reload_moved_script(self, old_path: str, new_path: str) -> None:
        """Publish and hot-reload a GUID-stable script move on the main thread."""
        if self._event_handler is None:
            return
        from Infernux.components.script_loader import clear_deleted_script_errors
        from Infernux.components.registry import unregister_component_script

        clear_deleted_script_errors(old_path)
        unregister_component_script(old_path)
        self.notify_script_catalog_changed(new_path, "moved")
        self._event_handler._check_script(new_path, catalog_event=None)

    def register_shader_cache_callback(self, callback):
        """Register a callback to be called when shader cache should be invalidated."""
        if self._event_handler:
            self._event_handler.register_shader_cache_callback(callback)

    def stop(self):
        """
        Stop the resource monitoring and clean up resources.
        """
        self._stop_event.set()

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

        # Reset internal state
        self._observer = None
        self._thread = None
        self._engine = None
        self._event_handler = None
        self._script_reload_callbacks.clear()
        self._script_catalog_callbacks.clear()
        if ResourcesManager._instance is self:
            ResourcesManager._instance = None
        
        Debug.log_internal("ResourcesManager cleanup completed")
