"""Thread-safe file-system event coalescing for the asset import pipeline."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable

from Infernux.engine.path_utils import path_key, portable_path, resolved_path


class AssetFsEventKind(str, Enum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED = "moved"
    META_DELETED = "meta_deleted"


@dataclass(frozen=True, slots=True)
class AssetFsEvent:
    kind: AssetFsEventKind
    path: str
    destination: str = ""
    guid_hint: str = ""
    observed_at: float = 0.0
    attempt: int = 0


@dataclass(slots=True)
class _PendingEvent:
    event: AssetFsEvent
    ready_at: float


def _absolute_path(path: str) -> str:
    if not path:
        raise ValueError("asset event path cannot be empty")
    return resolved_path(path)


def _path_key(path: str) -> str:
    return path_key(path)


_DOCUMENT_STORE_TEMP_PATTERN = re.compile(r"\.tmp\.\d+\.\d+$")


def is_document_store_temporary_path(path: str) -> bool:
    """Return whether *path* uses AtomicFile's exact temporary-file suffix."""
    if not path:
        return False
    normalized = portable_path(path)
    return _DOCUMENT_STORE_TEMP_PATTERN.search(normalized) is not None


def _event_references_temporary_path(event: AssetFsEvent) -> bool:
    return is_document_store_temporary_path(event.path) or is_document_store_temporary_path(event.destination)


class ImportCoordinator:
    """Accept watcher-thread events and publish coalesced main-thread work."""

    def __init__(
        self,
        *,
        debounce_seconds: float = 0.12,
        delete_grace_seconds: float = 0.75,
        meta_delete_grace_seconds: float = 0.6,
        retry_delay_seconds: float = 0.15,
        max_attempts: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(debounce_seconds, delete_grace_seconds, meta_delete_grace_seconds, retry_delay_seconds) < 0:
            raise ValueError("coordinator delays must be non-negative")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._debounce_seconds = debounce_seconds
        self._delete_grace_seconds = delete_grace_seconds
        self._meta_delete_grace_seconds = meta_delete_grace_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._max_attempts = max_attempts
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingEvent] = {}

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def submit(
        self,
        kind: AssetFsEventKind,
        path: str,
        *,
        destination: str = "",
        guid_hint: str = "",
        observed_at: float | None = None,
        debounce_seconds: float | None = None,
    ) -> None:
        if not isinstance(kind, AssetFsEventKind):
            raise TypeError("kind must be AssetFsEventKind")
        if debounce_seconds is not None and debounce_seconds < 0:
            raise ValueError("debounce_seconds must be non-negative or None")
        now = self._clock() if observed_at is None else observed_at
        normalized_path = _absolute_path(path)
        normalized_destination = _absolute_path(destination) if destination else ""
        if kind is AssetFsEventKind.MOVED and is_document_store_temporary_path(normalized_path):
            if not normalized_destination or is_document_store_temporary_path(normalized_destination):
                return
            kind = AssetFsEventKind.MODIFIED
            normalized_path = normalized_destination
            normalized_destination = ""
        elif is_document_store_temporary_path(normalized_path) or is_document_store_temporary_path(
            normalized_destination
        ):
            return
        event = AssetFsEvent(
            kind=kind,
            path=normalized_path,
            destination=normalized_destination,
            guid_hint=guid_hint.strip(),
            observed_at=now,
        )
        if kind is AssetFsEventKind.MOVED and not event.destination:
            raise ValueError("moved event requires a destination")
        with self._lock:
            self._submit_locked(event, now, debounce_seconds=debounce_seconds)

    def _delay_for(
        self,
        kind: AssetFsEventKind,
        debounce_seconds: float | None = None,
    ) -> float:
        if kind is AssetFsEventKind.DELETED:
            return self._delete_grace_seconds
        if kind is AssetFsEventKind.META_DELETED:
            return self._meta_delete_grace_seconds
        return self._debounce_seconds if debounce_seconds is None else debounce_seconds

    def _event_key(self, event: AssetFsEvent) -> str:
        if event.kind is AssetFsEventKind.META_DELETED:
            return "meta:" + _path_key(event.path)
        target = event.destination if event.kind is AssetFsEventKind.MOVED else event.path
        return "asset:" + _path_key(target)

    def _find_guid_pair_locked(self, event: AssetFsEvent) -> tuple[str, _PendingEvent] | None:
        if not event.guid_hint or event.kind not in (AssetFsEventKind.CREATED, AssetFsEventKind.DELETED):
            return None
        opposite = (
            AssetFsEventKind.DELETED if event.kind is AssetFsEventKind.CREATED else AssetFsEventKind.CREATED
        )
        for key, pending in self._pending.items():
            candidate = pending.event
            if candidate.kind is opposite and candidate.guid_hint == event.guid_hint:
                return key, pending
        return None

    def _submit_locked(
        self,
        event: AssetFsEvent,
        now: float,
        *,
        debounce_seconds: float | None = None,
    ) -> None:
        debounce = (
            self._debounce_seconds
            if debounce_seconds is None
            else debounce_seconds
        )
        pair = self._find_guid_pair_locked(event)
        if pair is not None:
            pair_key, pending = pair
            del self._pending[pair_key]
            deleted = event if event.kind is AssetFsEventKind.DELETED else pending.event
            created = event if event.kind is AssetFsEventKind.CREATED else pending.event
            if _path_key(deleted.path) == _path_key(created.path):
                event = AssetFsEvent(
                    AssetFsEventKind.MODIFIED,
                    created.path,
                    guid_hint=event.guid_hint,
                    observed_at=max(deleted.observed_at, created.observed_at),
                )
            else:
                event = AssetFsEvent(
                    AssetFsEventKind.MOVED,
                    deleted.path,
                    destination=created.path,
                    guid_hint=event.guid_hint,
                    observed_at=max(deleted.observed_at, created.observed_at),
                )

        if event.kind is AssetFsEventKind.MOVED:
            source_key = "asset:" + _path_key(event.path)
            destination_key = "asset:" + _path_key(event.destination)
            for key, pending in list(self._pending.items()):
                previous = pending.event
                if previous.kind is AssetFsEventKind.MOVED and _path_key(previous.destination) == _path_key(event.path):
                    event = replace(event, path=previous.path, guid_hint=event.guid_hint or previous.guid_hint)
                    del self._pending[key]
                    source_key = "asset:" + _path_key(event.path)
                    break
            self._pending.pop(source_key, None)
            self._pending.pop(destination_key, None)
            self._pending[self._event_key(event)] = _PendingEvent(
                event,
                now + self._delay_for(
                    AssetFsEventKind.MOVED,
                    debounce_seconds,
                ),
            )
            return

        key = self._event_key(event)
        previous_pending = self._pending.get(key)
        if previous_pending is None:
            self._pending[key] = _PendingEvent(
                event,
                now + self._delay_for(event.kind, debounce_seconds),
            )
            return

        previous = previous_pending.event
        if previous.kind is AssetFsEventKind.META_DELETED or event.kind is AssetFsEventKind.META_DELETED:
            self._pending[key] = _PendingEvent(
                event,
                now + self._delay_for(event.kind, debounce_seconds),
            )
            return

        if previous.kind is AssetFsEventKind.MOVED:
            if event.kind is AssetFsEventKind.DELETED:
                collapsed = AssetFsEvent(
                    AssetFsEventKind.DELETED,
                    previous.path,
                    guid_hint=previous.guid_hint or event.guid_hint,
                    observed_at=event.observed_at,
                )
                self._pending.pop(key, None)
                self._pending[self._event_key(collapsed)] = _PendingEvent(
                    collapsed, now + self._delete_grace_seconds
                )
            else:
                previous_pending.ready_at = now + debounce
            return

        if previous.kind is AssetFsEventKind.CREATED and event.kind is AssetFsEventKind.DELETED:
            del self._pending[key]
            return
        if previous.kind is AssetFsEventKind.DELETED and event.kind is AssetFsEventKind.CREATED:
            event = replace(event, kind=AssetFsEventKind.MODIFIED)
        elif previous.kind is AssetFsEventKind.CREATED and event.kind is AssetFsEventKind.MODIFIED:
            event = replace(previous, observed_at=event.observed_at, guid_hint=event.guid_hint or previous.guid_hint)
        elif event.kind is AssetFsEventKind.DELETED:
            pass
        elif previous.kind is AssetFsEventKind.DELETED:
            # Atomic replacement on Windows can report the old target as
            # deleted before the DocumentStore temp file is moved into place.
            # That temp move is normalized to MODIFIED above, so it must revive
            # the pending asset instead of allowing the stale delete to win.
            if event.kind is not AssetFsEventKind.MODIFIED:
                event = previous
        elif previous.kind is AssetFsEventKind.MODIFIED and event.kind is AssetFsEventKind.CREATED:
            event = replace(event, kind=AssetFsEventKind.MODIFIED)

        self._pending[key] = _PendingEvent(
            event,
            now + self._delay_for(event.kind, debounce_seconds),
        )

    def drain(
        self,
        *,
        force: bool = False,
        now: float | None = None,
        max_events: int | None = None,
    ) -> list[AssetFsEvent]:
        if max_events is not None and max_events < 1:
            raise ValueError("max_events must be positive or None")
        current = self._clock() if now is None else now
        with self._lock:
            ready = [
                (key, pending)
                for key, pending in self._pending.items()
                if force or pending.ready_at <= current
            ]
            ready.sort(key=lambda item: item[1].event.observed_at)
            if not force and max_events is not None:
                ready = ready[:max_events]
            for key, _pending in ready:
                del self._pending[key]
        return [pending.event for _key, pending in ready]

    def retry(self, event: AssetFsEvent, *, now: float | None = None) -> bool:
        if _event_references_temporary_path(event):
            return False
        if event.attempt + 1 >= self._max_attempts:
            return False
        current = self._clock() if now is None else now
        retried = replace(event, attempt=event.attempt + 1, observed_at=current)
        with self._lock:
            self._pending[self._event_key(retried)] = _PendingEvent(
                retried, current + self._retry_delay_seconds
            )
        return True

    def defer(self, event: AssetFsEvent, *, now: float | None = None) -> None:
        """Requeue known in-flight owner work without consuming retry budget.

        A DocumentStore write has its own completion ticket.  Watcher echoes
        that arrive before that ticket completes are not failed imports, so
        counting them against the bounded importer retry budget can turn a
        healthy asynchronous save into a false terminal error.
        """
        if _event_references_temporary_path(event):
            return
        current = self._clock() if now is None else now
        deferred = replace(event, observed_at=current)
        with self._lock:
            self._pending[self._event_key(deferred)] = _PendingEvent(
                deferred, current + self._retry_delay_seconds
            )

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
