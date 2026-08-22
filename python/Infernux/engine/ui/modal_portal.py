"""Top-level rendering portal for project-session Editor modals."""

from __future__ import annotations

from Infernux.lib import InxGUIRenderable


class ModalPortal(InxGUIRenderable):
    """Render the single modal selected by the interaction core."""

    def __init__(self, modal_service) -> None:
        super().__init__()
        self._modal_service = modal_service

    def on_render(self, ctx) -> None:
        self._modal_service.render(ctx)
