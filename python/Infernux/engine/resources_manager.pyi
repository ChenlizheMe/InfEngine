"""ResourcesManager — file-system watcher for live asset reloading.

Monitors the project's Assets and Packages directories and triggers
shader / script reloads automatically.

Example::

    mgr = ResourcesManager(project_path, engine)
    mgr.start()
    # … in frame loop:
    mgr.process_pending_reloads()
    # on shutdown:
    mgr.cleanup()
"""

from __future__ import annotations

from types import CodeType
from typing import Callable, Optional


class ResourcesManager:
    """Watches the project file system and dispatches asset reload events."""

    @classmethod
    def instance(cls) -> Optional[ResourcesManager]:
        """Return the singleton, or ``None`` if not yet created."""
        ...

    def __init__(self, project_path: str, engine: object) -> None: ...

    def start(self, *, skip_initial_scan: bool = ...) -> None:
        """Start the file-system observer thread."""
        ...
    def prepare_startup(self, on_progress: Callable[[str], None] | None = ...) -> None:
        """Finish the startup script refresh before the engine window is shown."""
        ...

    def stop(self) -> None:
        """Stop the observer thread."""
        ...

    def is_running(self) -> bool: ...

    def cleanup(self) -> None:
        """Stop the observer and release all resources."""
        ...

    def process_pending_reloads(self, *, force: bool = ...) -> int:
        """Commit worker artifacts and coalesced asset events on the main thread."""
        ...

    def begin_script_transaction(
        self,
        paths: object,
        *,
        retire_paths: object = ...,
    ) -> str: ...

    def retire_script_paths(self, paths: object) -> None: ...

    def submit_script_change(
        self,
        file_path: str,
        *,
        origin: str,
        catalog_event: str | None = ...,
        change_kind: str | None = ...,
        transaction_id: str | None = ...,
        force: bool | None = ...,
    ) -> object | None: ...

    def drain_pending_events(self) -> int: ...

    def register_script_reload_callback(self, file_path: str, callback: Callable) -> None:
        """Register a callback for when *file_path* is modified.

        Args:
            file_path: Absolute path to the script.
            callback: Callable invoked on file change.
        """
        ...

    def unregister_script_reload_callback(self, callback: Callable) -> None: ...

    def register_script_catalog_callback(self, callback: Callable) -> None:
        """Register a callback for script creation/deletion events."""
        ...

    def unregister_script_catalog_callback(self, callback: Callable) -> None: ...

    def notify_script_catalog_changed(self, file_path: str, event_type: str) -> None: ...

    def reload_moved_script(self, old_path: str, new_path: str) -> None: ...

    def register_shader_cache_callback(self, callback: Callable) -> None: ...


class ResourceChangeHandler:
    """File-system event handler that triggers asset reloads."""

    def __init__(self, engine: object, *, project_path: str | None = ...) -> None: ...
    def on_created(self, event: object) -> None: ...
    def on_deleted(self, event: object) -> None: ...
    def on_modified(self, event: object) -> None: ...
    def on_moved(self, event: object) -> None: ...
    @property
    def pending_count(self) -> int: ...
    @property
    def dependency_graph(self) -> object | None: ...
    def dependency_graph_snapshot(self) -> object | None: ...
    def dependency_affected(self, changed: object | None = ...) -> tuple: ...
    def begin_script_transaction(
        self,
        paths: object = ...,
        *,
        retire_paths: object = ...,
        transaction_id: str | None = ...,
        initial_scan: bool = ...,
    ) -> str: ...
    def set_frontend_worker_running(self, running: bool) -> None: ...
    def process_script_worker(self, max_items: int | None = ...) -> int: ...
    def process_pending_reloads(self, *, force: bool = ...) -> int: ...
    def _publish_valid_script(
        self,
        file_path: str,
        *,
        source: bytes,
        code: CodeType | None = ...,
        catalog_event: str | None,
        _defer_post_commit: bool = ...,
    ) -> bool: ...
    def cleanup(self) -> None: ...
    def register_shader_cache_callback(self, callback: Callable) -> None: ...
