"""Chronological evidence and cursor for editor undo/redo."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time
import uuid
from typing import Any, Optional

from .contexts import FocusSnapshot
from .descriptors import SelectionSnapshot


class ActionOrigin(str, Enum):
    USER = "user"
    AUTOMATION = "automation"
    SYSTEM = "system"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class EditorContextSnapshot:
    focus: FocusSnapshot = FocusSnapshot()
    selection: SelectionSnapshot = SelectionSnapshot()

    def with_selection(self, selection: SelectionSnapshot) -> "EditorContextSnapshot":
        return EditorContextSnapshot(self.focus, selection)


@dataclass(slots=True)
class JournalEntry:
    action: Any
    before_context: Optional[EditorContextSnapshot]
    after_context: Optional[EditorContextSnapshot]
    origin: ActionOrigin = ActionOrigin.USER
    operation_id: str = ""
    transaction_id: str = ""
    timestamp: float = 0.0
    revision: int = 0

    def __post_init__(self) -> None:
        if not self.operation_id:
            self.operation_id = uuid.uuid4().hex
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass(frozen=True, slots=True)
class JournalPushResult:
    recorded: bool
    merged: bool = False
    dropped: tuple[JournalEntry, ...] = ()
    discarded_redo: tuple[JournalEntry, ...] = ()


class EditorActionJournal:
    """One ordered action stream and one global undo/redo cursor."""

    def __init__(self, max_entries: int = 200) -> None:
        self.max_entries = max(1, int(max_entries))
        self._entries: list[JournalEntry] = []
        self._cursor = 0

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        return tuple(self._entries)

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def can_undo(self) -> bool:
        return self._cursor > 0

    @property
    def can_redo(self) -> bool:
        return self._cursor < len(self._entries)

    def applied_entries(self) -> tuple[JournalEntry, ...]:
        return tuple(self._entries[: self._cursor])

    def redo_entries(self) -> tuple[JournalEntry, ...]:
        # Compatibility order: the next redo action is the final element.
        return tuple(reversed(self._entries[self._cursor :]))

    def dirty_signature(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            (entry.operation_id, entry.revision)
            for entry in self._entries[: self._cursor]
            if bool(getattr(entry.action, "marks_dirty", True))
        )

    def record(
        self,
        action: Any,
        *,
        before_context: Optional[EditorContextSnapshot] = None,
        after_context: Optional[EditorContextSnapshot] = None,
        origin: ActionOrigin = ActionOrigin.USER,
        transaction_id: str = "",
    ) -> JournalPushResult:
        if origin is ActionOrigin.EXTERNAL:
            return JournalPushResult(False)

        discarded_redo = tuple(self._entries[self._cursor :])
        if discarded_redo:
            del self._entries[self._cursor :]

        if self._entries and self._cursor == len(self._entries):
            previous = self._entries[-1]
            if (
                previous.origin is origin
                and previous.transaction_id == str(transaction_id or "")
                and bool(previous.action.can_merge(action))
            ):
                previous.action.merge(action)
                previous.after_context = after_context
                previous.timestamp = time.time()
                previous.revision += 1
                return JournalPushResult(
                    True,
                    merged=True,
                    discarded_redo=discarded_redo,
                )

        entry = JournalEntry(
            action=action,
            before_context=before_context,
            after_context=after_context,
            origin=origin,
            transaction_id=str(transaction_id or ""),
        )
        self._entries.append(entry)
        self._cursor += 1

        dropped: tuple[JournalEntry, ...] = ()
        overflow = len(self._entries) - self.max_entries
        if overflow > 0:
            dropped = tuple(self._entries[:overflow])
            del self._entries[:overflow]
            self._cursor = max(0, self._cursor - overflow)

        return JournalPushResult(
            True,
            dropped=dropped,
            discarded_redo=discarded_redo,
        )

    def peek_undo(self) -> Optional[JournalEntry]:
        return self._entries[self._cursor - 1] if self.can_undo else None

    def peek_redo(self) -> Optional[JournalEntry]:
        return self._entries[self._cursor] if self.can_redo else None

    def commit_undo(self, entry: JournalEntry) -> None:
        current = self.peek_undo()
        if current is not entry:
            raise RuntimeError("undo journal cursor changed during replay")
        self._cursor -= 1

    def commit_redo(self, entry: JournalEntry) -> None:
        current = self.peek_redo()
        if current is not entry:
            raise RuntimeError("redo journal cursor changed during replay")
        self._cursor += 1

    def clear(self) -> None:
        self._entries.clear()
        self._cursor = 0
