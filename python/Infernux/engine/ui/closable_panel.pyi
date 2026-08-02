"""ClosablePanel — base class for dockable ImGui windows.

Provides open/close state management, window-manager integration,
and focus tracking. Subclassed by :class:`EditorPanel`.
"""

from __future__ import annotations

from typing import Optional

from Infernux.lib import InxGUIRenderable, InxGUIContext


class ClosablePanel(InxGUIRenderable):
    """A dockable ImGui window with open/close lifecycle."""

    WINDOW_TYPE_ID: Optional[str]
    WINDOW_DISPLAY_NAME: Optional[str]
    WINDOW_TITLE_KEY: Optional[str]

    def __init__(self, title: str, window_id: Optional[str] = None) -> None: ...

    @property
    def window_id(self) -> str:
        """Unique window identifier used by the window manager."""
        ...

    @property
    def is_open(self) -> bool:
        """Whether the panel is currently visible."""
        ...

    @property
    def document_id(self) -> str: ...

    def bind_document(self, document_id: str) -> None: ...
    def unbind_document(self) -> None: ...

    def set_window_manager(self, window_manager: object) -> None: ...

    def open(self) -> None:
        """Show the panel (registers with window manager if needed)."""
        ...

    def request_focus(self, ctx: InxGUIContext) -> None:
        """Request ImGui keyboard focus for this panel."""
        ...

    @classmethod
    def get_active_panel_id(cls) -> Optional[str]:
        """Return the ``window_id`` of the currently focused panel."""
        ...

    @classmethod
    def focus_panel_by_id(cls, panel_id: str) -> None:
        """Programmatically focus a panel by its ``window_id``."""
        ...
