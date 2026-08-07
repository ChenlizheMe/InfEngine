"""Chronological evidence and cursor for editor undo/redo."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from contextlib import contextmanager
from contextvars import ContextVar
import time
import uuid
from typing import Any, Optional

from .contexts import FocusSnapshot
from .descriptors import SelectionSnapshot
from .documents import DocumentLocator
from .windows import WindowLocator


class ActionOrigin(str, Enum):
    USER = "user"
    AUTOMATION = "automation"
    SYSTEM = "system"
    EXTERNAL = "external"


_ACTION_ORIGIN = ContextVar("infernux_editor_action_origin", default=ActionOrigin.USER)


def current_action_origin() -> ActionOrigin:
    """Return the source of commands created in the current execution context."""
    return ActionOrigin(_ACTION_ORIGIN.get())


@contextmanager
def action_origin_scope(origin: ActionOrigin):
    """Propagate one human/automation/system source through nested services."""
    token = _ACTION_ORIGIN.set(ActionOrigin(origin))
    try:
        yield
    finally:
        _ACTION_ORIGIN.reset(token)


class ContextRestoreStatus(str, Enum):
    """Progress of restoring the editor context required by history replay."""

    READY = "ready"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EditorContextSnapshot:
    focus: FocusSnapshot = FocusSnapshot()
    selection: SelectionSnapshot = SelectionSnapshot()
    document: Optional[DocumentLocator] = None
    window: Optional[WindowLocator] = None
    scene: Optional[DocumentLocator] = None

    def with_selection(self, selection: SelectionSnapshot) -> "EditorContextSnapshot":
        if not isinstance(selection, SelectionSnapshot):
            raise TypeError("editor context selection must be a SelectionSnapshot")
        return EditorContextSnapshot(
            self.focus,
            selection,
            self.document,
            self.window,
            self.scene,
        )


@dataclass(slots=True)
class JournalEntry:
    action: Any
    before_context: Optional[EditorContextSnapshot]
    after_context: Optional[EditorContextSnapshot]
    origin: ActionOrigin = ActionOrigin.USER
    operation_id: str = ""
    transaction_id: str = ""
    command_id: str = ""
    timestamp: float = 0.0
    revision: int = 0

    def __post_init__(self) -> None:
        if not self.operation_id:
            self.operation_id = uuid.uuid4().hex
        self.command_id = str(self.command_id or "").strip()
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
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

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

    def record(
        self,
        action: Any,
        *,
        before_context: Optional[EditorContextSnapshot] = None,
        after_context: Optional[EditorContextSnapshot] = None,
        origin: ActionOrigin = ActionOrigin.USER,
        transaction_id: str = "",
        operation_id: str = "",
        command_id: str = "",
    ) -> JournalPushResult:
        if origin is ActionOrigin.EXTERNAL:
            self._dispose_action(action)
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
                if command_id:
                    previous.command_id = str(command_id).strip()
                self._revision += 1
                self._dispose_action(action)
                self._dispose_entries(discarded_redo)
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
            command_id=str(command_id or ""),
            operation_id=str(operation_id or getattr(action, "operation_id", "")),
        )
        self._entries.append(entry)
        self._cursor += 1
        self._revision += 1

        dropped: tuple[JournalEntry, ...] = ()
        overflow = len(self._entries) - self.max_entries
        if overflow > 0:
            dropped = tuple(self._entries[:overflow])
            del self._entries[:overflow]
            self._cursor = max(0, self._cursor - overflow)

        self._dispose_entries(discarded_redo)
        self._dispose_entries(dropped)

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
        self._revision += 1

    def commit_redo(self, entry: JournalEntry) -> None:
        current = self.peek_redo()
        if current is not entry:
            raise RuntimeError("redo journal cursor changed during replay")
        self._cursor += 1
        self._revision += 1

    def operation_entries(self, operation_id: str) -> tuple[JournalEntry, ...]:
        """Return the contiguous applied tail owned by one user operation."""
        operation_id = str(operation_id or "").strip()
        if not operation_id:
            return ()
        result: list[JournalEntry] = []
        for entry in reversed(self._entries[: self._cursor]):
            if entry.operation_id != operation_id:
                break
            result.append(entry)
        return tuple(reversed(result))

    def replace_operation(
        self,
        operation_id: str,
        action: Any,
        *,
        before_context: Optional[EditorContextSnapshot],
        after_context: Optional[EditorContextSnapshot],
        origin: ActionOrigin,
        transaction_id: str = "",
        command_id: str = "",
    ) -> JournalEntry:
        """Collapse one operation's applied tail without disposing its children."""
        entries = self.operation_entries(operation_id)
        if not entries:
            raise RuntimeError("journal operation has no applied entries")
        if self._cursor != len(self._entries):
            raise RuntimeError("cannot replace an operation while redo entries exist")
        start = self._cursor - len(entries)
        del self._entries[start : self._cursor]
        entry = JournalEntry(
            action=action,
            before_context=before_context,
            after_context=after_context,
            origin=ActionOrigin(origin),
            operation_id=str(operation_id),
            transaction_id=str(transaction_id or ""),
            command_id=str(command_id or ""),
            timestamp=entries[-1].timestamp,
            revision=max(entry.revision for entry in entries),
        )
        self._entries.append(entry)
        self._cursor = len(self._entries)
        self._revision += 1
        return entry

    def clear(self) -> None:
        entries = tuple(self._entries)
        self._entries.clear()
        self._cursor = 0
        self._revision += 1
        self._dispose_entries(entries)

    @staticmethod
    def _dispose_action(action: Any) -> None:
        dispose = getattr(action, "dispose", None)
        if not callable(dispose):
            return
        try:
            dispose()
        except Exception as exc:
            from Infernux.debug import Debug

            Debug.log_suppressed("EditorActionJournal.dispose", exc)

    @classmethod
    def _dispose_entries(cls, entries: tuple[JournalEntry, ...]) -> None:
        for entry in entries:
            cls._dispose_action(entry.action)
