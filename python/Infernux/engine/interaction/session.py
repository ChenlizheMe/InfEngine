"""Lifetime owner for editor interaction services."""

from __future__ import annotations

from typing import Optional

from .contexts import FocusService
from .action_journal import EditorActionJournal, EditorContextSnapshot
from .documents import DocumentRegistry
from .close_coordinator import CloseCoordinator
from .selection import SelectionService


class EditorInteractionCore:
    """Project-session owner for shared editor interaction state."""

    _instance: Optional["EditorInteractionCore"] = None

    def __init__(self) -> None:
        self.selection = SelectionService()
        self.focus = FocusService()
        self.documents = DocumentRegistry()
        self.close_coordinator = CloseCoordinator(self.documents)
        self.action_journal = EditorActionJournal()
        EditorInteractionCore._instance = self

    @classmethod
    def instance(cls) -> Optional["EditorInteractionCore"]:
        return cls._instance

    def shutdown(self) -> None:
        self.close_coordinator.cancel()
        self.selection.clear(reason="session_shutdown", record_history=False)
        self.documents.clear()
        self.action_journal.clear()
        active_panel_id = self.focus.snapshot.active_panel_id
        if active_panel_id:
            self.focus.deactivate_panel(active_panel_id)
        if EditorInteractionCore._instance is self:
            EditorInteractionCore._instance = None

    def capture_context(self) -> EditorContextSnapshot:
        return EditorContextSnapshot(self.focus.snapshot, self.selection.snapshot)
