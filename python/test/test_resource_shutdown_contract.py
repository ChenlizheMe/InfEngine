from __future__ import annotations

from types import SimpleNamespace

import pytest

from Infernux.engine.resources_manager import ResourcesManager


class _Database:
    def __init__(self, *, refresh_pending: bool) -> None:
        self.refresh_pending = refresh_pending
        self.complete_calls = 0

    def complete_pending_refresh(self) -> None:
        self.complete_calls += 1
        if not self.refresh_pending:
            raise AssertionError("completion requires pending refresh work")
        self.refresh_pending = False


class _Handler:
    def __init__(self, *, retained: bool = False) -> None:
        self.pending_count = 1
        self.calls = 0
        self.retained = retained

    def process_script_worker(self) -> int:
        return 0

    def process_pending_reloads(self, *, force: bool = False) -> int:
        assert force is True
        self.calls += 1
        if not self.retained:
            self.pending_count = 0
        return 1


def _manager(database: _Database, handler: _Handler) -> ResourcesManager:
    manager = ResourcesManager.__new__(ResourcesManager)
    manager._engine = SimpleNamespace(get_asset_database=lambda: database)
    manager._event_handler = handler
    manager._frontend_worker_running = False
    manager._startup_prepared = False
    return manager


def test_shutdown_completes_native_refresh_before_draining_events():
    database = _Database(refresh_pending=True)
    handler = _Handler()
    manager = _manager(database, handler)

    assert manager.drain_pending_events() == 1

    assert database.complete_calls == 1
    assert handler.calls == 1


def test_shutdown_does_not_start_or_complete_absent_refresh_work():
    database = _Database(refresh_pending=False)
    handler = _Handler()
    manager = _manager(database, handler)

    manager.drain_pending_events()

    assert database.complete_calls == 0
    assert handler.calls == 1


def test_shutdown_rejects_retained_events_without_guessing_retry_rounds():
    database = _Database(refresh_pending=False)
    handler = _Handler(retained=True)
    manager = _manager(database, handler)

    with pytest.raises(RuntimeError, match="retained work"):
        manager.drain_pending_events()

    assert handler.calls == 1
