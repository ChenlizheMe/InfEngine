"""Document-bound snapshot commands for non-graph authoring panels."""

from __future__ import annotations

import copy
from typing import Any

from Infernux.engine.interaction import DocumentLocator, DocumentRegistry
from Infernux.engine.undo._base import UndoCommand


class DocumentRevisionCommand(UndoCommand):
    """Bind one already-defined command to an authoritative document revision.

    The wrapped command owns model mutation.  This adapter owns only the
    corresponding DocumentRegistry cursor, so Undo/Redo never has to infer
    dirty state from journal position or active-panel focus.
    """

    def __init__(
        self,
        command: UndoCommand,
        locator: DocumentLocator,
        before_revision: int,
        after_revision: int,
    ) -> None:
        if not isinstance(command, UndoCommand):
            raise TypeError("document revision adapter requires an UndoCommand")
        if not isinstance(locator, DocumentLocator):
            raise TypeError("document revision adapter requires a DocumentLocator")
        super().__init__(command.description)
        self._command = command
        self._locator = locator
        self._before_revision = int(before_revision)
        self._after_revision = int(after_revision)
        if self._before_revision < 0 or self._after_revision < 0:
            raise ValueError("document revisions must be non-negative")
        self.marks_dirty = bool(command.marks_dirty)
        self._is_property_edit = bool(command._is_property_edit)
        self.before_selection_snapshot = command.before_selection_snapshot
        self.after_selection_snapshot = command.after_selection_snapshot
        self.timestamp = command.timestamp
        self.operation_id = command.operation_id

    @property
    def document_locator(self) -> DocumentLocator:
        return self._locator

    @property
    def inner_command(self) -> UndoCommand:
        return self._command

    @property
    def diff(self):
        """Expose a typed domain diff for journal diagnostics and tooling."""
        return self._command.diff

    @property
    def before_revision(self) -> int:
        return self._before_revision

    @property
    def after_revision(self) -> int:
        return self._after_revision

    def _restore_revision(self, revision: int) -> None:
        registry = DocumentRegistry.instance()
        document = registry.resolve_locator(self._locator)
        if document is None:
            raise RuntimeError(
                "document revision replay could not resolve "
                f"{self._locator.stable_id!r}"
            )
        if document.stable_id != self._locator.stable_id:
            raise RuntimeError("document revision replay resolved a different document")
        registry.restore_content_revision(document.document_id, revision)

    def execute(self) -> None:
        self._command.execute()
        self._restore_revision(self._after_revision)

    def undo(self) -> None:
        self._command.undo()
        self._restore_revision(self._before_revision)

    def redo(self) -> None:
        self._command.redo()
        self._restore_revision(self._after_revision)

    def dispose(self) -> None:
        self._command.dispose()

    def bind_operation_id(self, operation_id: str) -> None:
        super().bind_operation_id(operation_id)
        self._command.bind_operation_id(self.operation_id)

    def can_merge(self, other: UndoCommand) -> bool:
        if (
            not isinstance(other, DocumentRevisionCommand)
            or self._locator.stable_id != other._locator.stable_id
            or self._after_revision != other._before_revision
            or not self._command.can_merge(other._command)
        ):
            return False
        registry = DocumentRegistry.instance()
        document = registry.resolve_locator(self._locator)
        if document is None or document.stable_id != self._locator.stable_id:
            return False
        if document.saved_revision == self._after_revision:
            return False
        ticket = registry.active_save_ticket(document.document_id)
        return ticket is None or ticket.captured_revision != self._after_revision

    def merge(self, other: "DocumentRevisionCommand") -> None:
        self._command.merge(other._command)
        self._after_revision = other._after_revision
        self.after_selection_snapshot = other.after_selection_snapshot
        self.timestamp = other.timestamp


class AuthoringDocumentSnapshotCommand(UndoCommand):
    """Replay one panel-owned authoring snapshot through its live controller."""

    MERGE_WINDOW = 0.3
    marks_dirty = False

    def __init__(
        self,
        description: str,
        document_id: str,
        before_snapshot: Any,
        after_snapshot: Any,
        before_revision: int,
        after_revision: int,
        *,
        merge_key: str = "",
    ) -> None:
        super().__init__(description)
        self._document_id = str(document_id or "")
        if not self._document_id:
            raise ValueError("authoring snapshot command requires a document id")
        self._before_snapshot = copy.deepcopy(before_snapshot)
        self._after_snapshot = copy.deepcopy(after_snapshot)
        self._before_revision = int(before_revision)
        self._after_revision = int(after_revision)
        self._merge_key = str(merge_key or "")

    def _apply(self, snapshot: Any, revision: int) -> None:
        registry = DocumentRegistry.instance()
        document = registry.require(self._document_id)
        restore = getattr(document.controller, "restore_authoring_snapshot", None)
        if not callable(restore):
            raise RuntimeError(
                f"document {self._document_id!r} has no authoring snapshot adapter"
            )
        restore(copy.deepcopy(snapshot))
        registry.restore_content_revision(self._document_id, revision)

    def execute(self) -> None:
        self._apply(self._after_snapshot, self._after_revision)

    def undo(self) -> None:
        self._apply(self._before_snapshot, self._before_revision)

    def redo(self) -> None:
        self.execute()

    def can_merge(self, other: UndoCommand) -> bool:
        return (
            bool(self._merge_key)
            and isinstance(other, AuthoringDocumentSnapshotCommand)
            and self._document_id == other._document_id
            and self._merge_key == other._merge_key
            and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW
        )

    def merge(self, other: "AuthoringDocumentSnapshotCommand") -> None:
        self._after_snapshot = copy.deepcopy(other._after_snapshot)
        self._after_revision = other._after_revision
        self.after_selection_snapshot = other.after_selection_snapshot
        self.timestamp = other.timestamp


__all__ = ["AuthoringDocumentSnapshotCommand", "DocumentRevisionCommand"]
