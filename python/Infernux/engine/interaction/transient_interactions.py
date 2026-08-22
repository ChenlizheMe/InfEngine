"""LIFO ownership for cancellable, non-document editor interactions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Optional
import uuid

from Infernux.debug import Debug

from .contexts import FocusService, FocusSnapshot, InputContext


CancelCallback = Callable[[], object]


@dataclass(frozen=True, slots=True)
class TransientInteraction:
    """One temporary editor interaction that may consume Escape."""

    token_id: str
    owner_id: str
    kind: str
    priority: int
    sequence: int


@dataclass(slots=True)
class _TransientEntry:
    descriptor: TransientInteraction
    cancel: CancelCallback


class TransientInteractionService:
    """Single authority for cancelling the top-most temporary interaction.

    Text edits, popup-owned operations, graph renames, drags, and future modal
    subflows register only while they are active.  The service projects one
    blocking child input context into :class:`FocusService`, allowing the
    normal shortcut router to give Escape deterministic precedence without a
    second per-panel key polling path.
    """

    CONTEXT_ID = "editor.transient"
    _instance: Optional["TransientInteractionService"] = None

    def __init__(self, focus: Optional[FocusService] = None) -> None:
        self._focus = focus
        self._entries: dict[str, _TransientEntry] = {}
        self._sequence = 0
        self._revision = 0
        self._restore_panel_id = ""
        self._restore_child_context_id = ""
        TransientInteractionService._instance = self

    @classmethod
    def instance(cls) -> "TransientInteractionService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def active(self) -> Optional[TransientInteraction]:
        entry = self._top_entry()
        return entry.descriptor if entry is not None else None

    @property
    def can_cancel(self) -> bool:
        return bool(self._entries)

    def persistent_focus_snapshot(
        self,
        snapshot: Optional[FocusSnapshot] = None,
    ) -> FocusSnapshot:
        """Remove pointer/inline interaction state before journaling context."""
        value = snapshot or self._focus_service().snapshot
        child_context_id = value.child_context_id
        if child_context_id == self.CONTEXT_ID:
            child_context_id = (
                self._restore_child_context_id
                if value.active_panel_id == self._restore_panel_id
                else ""
            )
        if (
            child_context_id == value.child_context_id
            and not value.capture_owner_id
        ):
            return value
        return replace(
            value,
            child_context_id=child_context_id,
            capture_owner_id="",
        )

    def begin(
        self,
        owner_id: str,
        cancel: CancelCallback,
        *,
        kind: str,
        priority: int = 0,
        token_id: str = "",
    ) -> str:
        owner_id = str(owner_id or "").strip()
        kind = str(kind or "").strip()
        if not owner_id:
            raise ValueError("transient interaction requires an owner_id")
        if not kind:
            raise ValueError("transient interaction requires a kind")
        if not callable(cancel):
            raise TypeError("transient interaction cancel callback must be callable")

        token = str(token_id or "").strip() or uuid.uuid4().hex
        was_empty = not self._entries
        if was_empty:
            snapshot = self._focus_service().snapshot
            self._restore_panel_id = snapshot.active_panel_id
            self._restore_child_context_id = snapshot.child_context_id

        self._sequence += 1
        descriptor = TransientInteraction(
            token,
            owner_id,
            kind,
            int(priority),
            self._sequence,
        )
        self._entries[token] = _TransientEntry(descriptor, cancel)
        self._revision += 1
        self._publish_context()
        return token

    def end(self, token_id: str) -> bool:
        token = str(token_id or "").strip()
        if not token or self._entries.pop(token, None) is None:
            return False
        self._revision += 1
        self._publish_context()
        return True

    def cancel_active(self) -> bool:
        entry = self._top_entry()
        if entry is None:
            return False
        token = entry.descriptor.token_id
        self._entries.pop(token, None)
        self._revision += 1
        self._publish_context()
        try:
            return entry.cancel() is not False
        except Exception as exc:
            Debug.log_suppressed(
                f"TransientInteraction.cancel[{entry.descriptor.kind}]",
                exc,
            )
            return False

    def cancel_owner(self, owner_id: str) -> int:
        owner = str(owner_id or "").strip()
        matching = sorted(
            (
                entry
                for entry in self._entries.values()
                if entry.descriptor.owner_id == owner
            ),
            key=self._entry_rank,
            reverse=True,
        )
        cancelled = 0
        for entry in matching:
            token = entry.descriptor.token_id
            current = self._entries.pop(token, None)
            if current is None:
                continue
            self._revision += 1
            try:
                current.cancel()
            except Exception as exc:
                Debug.log_suppressed(
                    f"TransientInteraction.cancel_owner[{current.descriptor.kind}]",
                    exc,
                )
            cancelled += 1
        if cancelled:
            self._publish_context()
        return cancelled

    def refresh_context(self) -> None:
        self._publish_context()

    def clear(self) -> None:
        if self._entries:
            self._entries.clear()
            self._revision += 1
        self._publish_context()

    def _focus_service(self) -> FocusService:
        return self._focus or FocusService.instance()

    @staticmethod
    def _entry_rank(entry: _TransientEntry) -> tuple[int, int]:
        descriptor = entry.descriptor
        return descriptor.priority, descriptor.sequence

    def _top_entry(self) -> Optional[_TransientEntry]:
        if not self._entries:
            return None
        return max(self._entries.values(), key=self._entry_rank)

    def _publish_context(self) -> None:
        focus = self._focus_service()
        focus.input_contexts.remove(self.CONTEXT_ID)
        top = self._top_entry()
        if top is not None:
            owner_id = top.descriptor.owner_id
            if focus.snapshot.active_panel_id != owner_id:
                return
            focus.input_contexts.push(
                InputContext(
                    self.CONTEXT_ID,
                    owner_id,
                    priority=10_000,
                    blocks_lower=True,
                )
            )
            focus.set_child_context(
                owner_id,
                self.CONTEXT_ID,
                reason="transient_interaction_begin",
                record_history=False,
            )
            return

        active_panel = focus.snapshot.active_panel_id
        if active_panel and focus.snapshot.child_context_id == self.CONTEXT_ID:
            restore = (
                self._restore_child_context_id
                if active_panel == self._restore_panel_id
                else ""
            )
            focus.set_child_context(
                active_panel,
                restore,
                reason="transient_interaction_end",
                record_history=False,
            )
        self._restore_panel_id = ""
        self._restore_child_context_id = ""


__all__ = ["TransientInteraction", "TransientInteractionService"]
