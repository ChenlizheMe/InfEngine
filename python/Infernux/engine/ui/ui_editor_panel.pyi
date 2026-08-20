"""ui_editor_panel — Figma-style 2D UI canvas editor."""

from __future__ import annotations

from typing import Callable, Optional

from Infernux.lib import InxGUIContext
from Infernux.engine.ui.editor_panel import EditorPanel


class UIEditorPanel(EditorPanel):
    """Figma-style 2D UI editor panel.

    Usage::

        panel = UIEditorPanel()
        panel.set_engine(engine)
        panel.on_render_content(ctx)
    """

    WINDOW_TYPE_ID: str
    WINDOW_DISPLAY_NAME: str

    def __init__(self, title: str = "UI Editor") -> None: ...
    def set_engine(self, engine: object) -> None: ...
    def set_on_request_ui_mode(self, callback: Optional[Callable]) -> None: ...
    def project_global_selection(self, go: object) -> None: ...
    def on_render_content(self, ctx: InxGUIContext) -> None: ...
