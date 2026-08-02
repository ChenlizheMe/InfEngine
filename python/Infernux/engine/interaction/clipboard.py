"""Typed, single-authority editor clipboard state."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Iterable, Optional


class ClipboardDomain(str, Enum):
    """Editor model encoded by a clipboard payload."""

    SCENE_OBJECT = "scene_object"
    ASSET = "asset"
    COMPONENT = "component"
    GRAPH_ELEMENT = "graph_element"
    TIMELINE_ELEMENT = "timeline_element"
    UI_ELEMENT = "ui_element"


class ClipboardOperation(str, Enum):
    COPY = "copy"
    CUT = "cut"


@dataclass(frozen=True, slots=True)
class ClipboardItem:
    """One stable clipboard item plus optional domain-owned serialized data."""

    target_id: str
    document_id: str = ""
    sub_kind: str = ""
    data: object = None

    def __post_init__(self) -> None:
        target_id = str(self.target_id or "").strip()
        if not target_id:
            raise ValueError("clipboard item target_id must not be empty")
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "document_id", str(self.document_id or ""))
        object.__setattr__(self, "sub_kind", str(self.sub_kind or ""))


@dataclass(frozen=True, slots=True)
class ClipboardPayload:
    """Immutable typed clipboard snapshot.

    ``revision`` is assigned by :class:`ClipboardService`. Consumers use it
    when consuming a cut payload so a newer user copy can never be cleared by
    an older asynchronous paste completion.
    """

    domain: ClipboardDomain
    operation: ClipboardOperation
    items: tuple[ClipboardItem, ...]
    source_owner_id: str = ""
    revision: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.domain, ClipboardDomain):
            object.__setattr__(self, "domain", ClipboardDomain(self.domain))
        if not isinstance(self.operation, ClipboardOperation):
            object.__setattr__(self, "operation", ClipboardOperation(self.operation))
        items = tuple(self.items)
        if not items:
            raise ValueError("clipboard payload must contain at least one item")
        if not all(isinstance(item, ClipboardItem) for item in items):
            raise TypeError("clipboard payload items must be ClipboardItem values")
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "source_owner_id", str(self.source_owner_id or ""))
        object.__setattr__(self, "revision", int(self.revision))


@dataclass(frozen=True, slots=True)
class ClipboardChange:
    before: Optional[ClipboardPayload]
    after: Optional[ClipboardPayload]
    reason: str


class ClipboardService:
    """Own the one editor clipboard across all selection domains."""

    _instance: Optional["ClipboardService"] = None

    def __init__(self) -> None:
        self._payload: Optional[ClipboardPayload] = None
        self._revision = 0
        self._listeners: list[Callable[[ClipboardChange], None]] = []
        ClipboardService._instance = self

    @classmethod
    def instance(cls) -> "ClipboardService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def install(cls, service: "ClipboardService") -> None:
        if not isinstance(service, cls):
            raise TypeError("clipboard service must be a ClipboardService")
        cls._instance = service

    @property
    def revision(self) -> int:
        return self._revision

    def add_listener(self, callback: Callable[[ClipboardChange], None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[ClipboardChange], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    @staticmethod
    def _clone(payload: Optional[ClipboardPayload]) -> Optional[ClipboardPayload]:
        if payload is None:
            return None
        return ClipboardPayload(
            payload.domain,
            payload.operation,
            tuple(
                ClipboardItem(
                    item.target_id,
                    item.document_id,
                    item.sub_kind,
                    copy.deepcopy(item.data),
                )
                for item in payload.items
            ),
            payload.source_owner_id,
            payload.revision,
        )

    def peek(self, domain: Optional[ClipboardDomain] = None) -> Optional[ClipboardPayload]:
        payload = self._payload
        if payload is None or (domain is not None and payload.domain is not domain):
            return None
        return self._clone(payload)

    def has_payload(self, domain: Optional[ClipboardDomain] = None) -> bool:
        return self._payload is not None and (
            domain is None or self._payload.domain is domain
        )

    def publish(self, payload: ClipboardPayload, *, reason: str = "copy") -> ClipboardPayload:
        if not isinstance(payload, ClipboardPayload):
            raise TypeError("payload must be a ClipboardPayload")
        before = self._clone(self._payload)
        self._revision += 1
        stored = self._clone(replace(payload, revision=self._revision))
        assert stored is not None
        self._payload = stored
        self._notify(ClipboardChange(before, self._clone(stored), str(reason)))
        result = self._clone(stored)
        assert result is not None
        return result

    def write(
        self,
        domain: ClipboardDomain,
        items: Iterable[ClipboardItem],
        *,
        operation: ClipboardOperation = ClipboardOperation.COPY,
        source_owner_id: str = "",
        reason: str = "copy",
    ) -> ClipboardPayload:
        return self.publish(
            ClipboardPayload(domain, operation, tuple(items), source_owner_id),
            reason=reason,
        )

    def clear(
        self,
        *,
        expected_revision: Optional[int] = None,
        reason: str = "clear",
    ) -> bool:
        if self._payload is None:
            return False
        if expected_revision is not None and self._payload.revision != int(expected_revision):
            return False
        before = self._clone(self._payload)
        self._revision += 1
        self._payload = None
        self._notify(ClipboardChange(before, None, str(reason)))
        return True

    def consume_cut(self, revision: int) -> bool:
        payload = self._payload
        if (
            payload is None
            or payload.revision != int(revision)
            or payload.operation is not ClipboardOperation.CUT
        ):
            return False
        return self.clear(expected_revision=revision, reason="consume_cut")

    def _notify(self, change: ClipboardChange) -> None:
        for callback in tuple(self._listeners):
            try:
                callback(change)
            except Exception as exc:
                from Infernux.debug import Debug

                Debug.log_suppressed("ClipboardService.listener", exc)
