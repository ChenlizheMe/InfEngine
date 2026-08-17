from __future__ import annotations

import threading

from Infernux.engine.import_coordinator import (
    AssetFsEvent,
    AssetFsEventKind,
    ImportCoordinator,
    is_document_store_temporary_path,
)


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _coordinator(clock):
    return ImportCoordinator(
        debounce_seconds=0.1,
        delete_grace_seconds=1.0,
        meta_delete_grace_seconds=0.6,
        retry_delay_seconds=0.2,
        max_attempts=3,
        clock=clock,
    )


def test_repeated_modifications_coalesce_to_one_event(tmp_path):
    clock = _Clock()
    coordinator = _coordinator(clock)
    path = tmp_path / "asset.txt"
    for _ in range(20):
        coordinator.submit(AssetFsEventKind.MODIFIED, str(path))
    assert coordinator.drain() == []
    clock.advance(0.11)
    events = coordinator.drain()
    assert len(events) == 1
    assert events[0].kind is AssetFsEventKind.MODIFIED


def test_one_submission_can_use_a_shorter_debounce(tmp_path):
    clock = _Clock()
    coordinator = _coordinator(clock)
    path = tmp_path / "fast.py"

    coordinator.submit(
        AssetFsEventKind.MODIFIED,
        str(path),
        debounce_seconds=0.025,
    )
    clock.advance(0.024)
    assert coordinator.drain() == []
    clock.advance(0.002)
    events = coordinator.drain()
    assert [event.kind for event in events] == [AssetFsEventKind.MODIFIED]


def test_create_modify_stays_created_and_ephemeral_create_delete_disappears(tmp_path):
    clock = _Clock()
    coordinator = _coordinator(clock)
    kept = tmp_path / "kept.txt"
    transient = tmp_path / "transient.txt"
    coordinator.submit(AssetFsEventKind.CREATED, str(kept))
    coordinator.submit(AssetFsEventKind.MODIFIED, str(kept))
    coordinator.submit(AssetFsEventKind.CREATED, str(transient))
    coordinator.submit(AssetFsEventKind.DELETED, str(transient))
    events = coordinator.drain(force=True)
    assert [(event.kind, event.path) for event in events] == [
        (AssetFsEventKind.CREATED, str(kept.resolve())),
    ]


def test_guid_matched_delete_create_becomes_move_and_chains(tmp_path):
    clock = _Clock()
    coordinator = _coordinator(clock)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    third = tmp_path / "third.txt"
    coordinator.submit(AssetFsEventKind.DELETED, str(first), guid_hint="same-guid")
    coordinator.submit(AssetFsEventKind.CREATED, str(second), guid_hint="same-guid")
    coordinator.submit(AssetFsEventKind.MOVED, str(second), destination=str(third), guid_hint="same-guid")
    events = coordinator.drain(force=True)
    assert len(events) == 1
    assert events[0].kind is AssetFsEventKind.MOVED
    assert events[0].path == str(first.resolve())
    assert events[0].destination == str(third.resolve())


def test_delete_and_meta_delete_have_independent_grace_periods(tmp_path):
    clock = _Clock()
    coordinator = _coordinator(clock)
    asset = tmp_path / "asset.txt"
    owner = tmp_path / "owner.txt"
    coordinator.submit(AssetFsEventKind.DELETED, str(asset))
    coordinator.submit(AssetFsEventKind.META_DELETED, str(owner))
    clock.advance(0.61)
    events = coordinator.drain()
    assert [event.kind for event in events] == [AssetFsEventKind.META_DELETED]
    clock.advance(0.4)
    assert [event.kind for event in coordinator.drain()] == [AssetFsEventKind.DELETED]


def test_retry_is_non_blocking_and_bounded(tmp_path):
    clock = _Clock()
    coordinator = _coordinator(clock)
    path = tmp_path / "asset.txt"
    coordinator.submit(AssetFsEventKind.CREATED, str(path))
    event = coordinator.drain(force=True)[0]
    assert coordinator.retry(event)
    assert coordinator.drain() == []
    clock.advance(0.21)
    event = coordinator.drain()[0]
    assert event.attempt == 1
    assert coordinator.retry(event)
    clock.advance(0.21)
    event = coordinator.drain()[0]
    assert event.attempt == 2
    assert not coordinator.retry(event)


def test_defer_preserves_import_retry_budget(tmp_path):
    clock = _Clock()
    coordinator = _coordinator(clock)
    path = tmp_path / "asset.mat"
    coordinator.submit(AssetFsEventKind.MODIFIED, str(path))
    event = coordinator.drain(force=True)[0]

    for _ in range(8):
        coordinator.defer(event)
        clock.advance(0.21)
        event = coordinator.drain()[0]
        assert event.attempt == 0

    assert coordinator.retry(event)


def test_document_store_temporary_events_are_filtered_and_atomic_move_becomes_modified(tmp_path):
    clock = _Clock()
    coordinator = _coordinator(clock)
    target = tmp_path / "atomic.mat"
    temporary = tmp_path / "atomic.mat.tmp.123456.7"

    assert is_document_store_temporary_path(str(temporary))
    assert not is_document_store_temporary_path(str(tmp_path / "design.tmp.notes"))

    for kind in (
        AssetFsEventKind.CREATED,
        AssetFsEventKind.MODIFIED,
        AssetFsEventKind.DELETED,
    ):
        coordinator.submit(kind, str(temporary))
    assert coordinator.pending_count == 0

    coordinator.submit(AssetFsEventKind.MOVED, str(temporary), destination=str(target))
    events = coordinator.drain(force=True)
    assert len(events) == 1
    assert events[0].kind is AssetFsEventKind.MODIFIED
    assert events[0].path == str(target.resolve())
    assert events[0].destination == ""

    leaked_temporary_event = AssetFsEvent(AssetFsEventKind.MODIFIED, str(temporary.resolve()))
    assert not coordinator.retry(leaked_temporary_event)
    assert coordinator.pending_count == 0


def test_concurrent_submit_is_thread_safe(tmp_path):
    clock = _Clock()
    coordinator = _coordinator(clock)
    path = tmp_path / "shared.txt"

    def submit_many():
        for _ in range(250):
            coordinator.submit(AssetFsEventKind.MODIFIED, str(path))

    workers = [threading.Thread(target=submit_many) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert coordinator.pending_count == 1
    assert len(coordinator.drain(force=True)) == 1
