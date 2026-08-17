"""PlayerGUI — main GUI renderable for standalone player mode.

Handles game viewport rendering, screen-space UI, and input processing
without any editor chrome.
"""

from __future__ import annotations

from Infernux.lib import InxGUIRenderable, InxGUIContext


class PlayerGUI(InxGUIRenderable):
    """Full-screen game renderer for player builds."""

    def __init__(
        self,
        engine: object,
        *,
        splash_items: list | None = None,
        data_root: str = "",
        control_channel: object | None = None,
    ) -> None: ...

    def on_render(self, ctx: InxGUIContext) -> None: ...
