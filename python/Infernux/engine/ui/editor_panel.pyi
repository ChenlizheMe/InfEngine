"""EditorPanel — base class for all dockable editor panels.

Subclass this to create custom inspector-like panels with access to
:class:`EditorServices` and the shared interaction services.

Example::

    from Infernux.engine.ui.editor_panel import EditorPanel

    class MyPanel(EditorPanel):
        def on_enable(self):
            SelectionService.instance().add_listener(self._on_sel)

        def on_disable(self):
            SelectionService.instance().remove_listener(self._on_sel)

        def on_render_content(self, ctx):
            ctx.text("Hello from my panel!")
"""

from __future__ import annotations

from typing import Optional

from Infernux.lib import InxGUIContext
from Infernux.engine.ui.closable_panel import ClosablePanel
from Infernux.engine.ui.editor_services import EditorServices
from Infernux.engine.interaction import (
    CommandSource,
    PanelViewStateSchema,
    SelectionService,
)


class EditorPanel(ClosablePanel):
    """Base class for all dockable editor panels.

    Provides lifecycle hooks, service access, and size/style overrides.
    """

    VIEW_STATE_SCHEMA: Optional[PanelViewStateSchema]

    def __init__(self, title: str, window_id: Optional[str] = None) -> None: ...

    def is_content_visible(self) -> bool: ...
    def was_content_visible(self) -> bool: ...
    def is_content_hovered(self) -> bool: ...

    @property
    def services(self) -> EditorServices:
        """Access to all editor subsystems (engine, undo, scenes, etc.)."""
        ...

    def on_enable(self) -> None:
        """Called when the panel becomes visible. Subscribe to events here."""
        ...

    def on_disable(self) -> None:
        """Called when the panel is hidden. Unsubscribe from events here."""
        ...

    def on_render_content(self, ctx: InxGUIContext) -> None:
        """Override to render the panel body.

        Args:
            ctx: The ImGui rendering context.
        """
        ...

    def save_state(self) -> dict:
        """Return a dict of panel state for persistence."""
        ...

    def load_state(self, data: dict) -> None:
        """Restore panel state from a previously saved dict."""
        ...

    def on_render(self, ctx: InxGUIContext) -> None:
        """Full render cycle (framework calls this — override ``on_render_content``)."""
        ...

    def execute_owned_command(
        self,
        command_id: str,
        *,
        source: Optional[CommandSource] = None,
        payload: object = ...,
    ) -> bool: ...
    def publish_interaction_ownership(
        self,
        *,
        reason: str = ...,
        record_history: bool = ...,
    ) -> bool: ...


class FloatingEditorPanel(EditorPanel):
    """Non-dockable utility panel with the standard Editor lifecycle."""

    def __init__(
        self,
        title: str,
        window_id: str,
        *,
        size: tuple[float, float],
    ) -> None: ...
