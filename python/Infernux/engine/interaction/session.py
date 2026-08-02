"""Lifetime owner for editor interaction services."""

from __future__ import annotations

from typing import Optional

from .contexts import FocusService
from .selection import SelectionService


class EditorInteractionCore:
    """Project-session owner for shared editor interaction state."""

    _instance: Optional["EditorInteractionCore"] = None

    def __init__(self) -> None:
        self.selection = SelectionService()
        self.focus = FocusService()
        EditorInteractionCore._instance = self

    @classmethod
    def instance(cls) -> Optional["EditorInteractionCore"]:
        return cls._instance

    def shutdown(self) -> None:
        self.selection.clear(reason="session_shutdown", record_history=False)
        active_panel_id = self.focus.snapshot.active_panel_id
        if active_panel_id:
            self.focus.deactivate_panel(active_panel_id)
        if EditorInteractionCore._instance is self:
            EditorInteractionCore._instance = None
