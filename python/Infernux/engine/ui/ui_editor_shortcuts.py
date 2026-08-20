"""UI Editor panel shortcuts / input helpers.

Centralises all keyboard and mouse shortcut logic for the Figma-style
UI editor so that ui_editor_panel.py stays focused on rendering.

Usage (inside UIEditorPanel)::

    from .ui_editor_shortcuts import UIEditorInput

    # once per frame, after getting area_hovered / ctx
    inp = UIEditorInput(ctx, area_hovered)
    if inp.wants_pan:
        ...
    if inp.ctrl_down:
        ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Infernux.lib import InxGUIContext

from .imgui_keys import (
    KEY_SPACE,
    KEY_LEFT_CTRL,
    KEY_RIGHT_CTRL,
    KEY_LEFT_SHIFT,
    KEY_RIGHT_SHIFT,
    KEY_LEFT_ALT,
    KEY_RIGHT_ALT,
)


class UIEditorInput:
    """Snapshot of all keyboard / mouse state relevant to the UI editor.

    Create a fresh instance each frame *after* the invisible-button call
    so that ``area_hovered`` is up-to-date.
    """

    __slots__ = (
        "ctx",
        "area_hovered",
        # modifier flags
        "space_down",
        "ctrl_down",
        "shift_down",
        "alt_down",
        # composite queries
        "wants_pan_space",
        "wants_pan_mmb",
        "wants_pan",
        # mouse helpers
        "mouse_x",
        "mouse_y",
        "wheel_delta",
        "lmb_clicked",
        "lmb_double_clicked",
        "lmb_down",
    )

    def __init__(self, ctx: "InxGUIContext", area_hovered: bool):
        self.ctx = ctx
        self.area_hovered = area_hovered

        # ── Modifiers ──
        self.space_down: bool = ctx.is_key_down(KEY_SPACE)
        self.ctrl_down: bool = (
            ctx.is_key_down(KEY_LEFT_CTRL) or ctx.is_key_down(KEY_RIGHT_CTRL)
        )
        self.shift_down: bool = (
            ctx.is_key_down(KEY_LEFT_SHIFT) or ctx.is_key_down(KEY_RIGHT_SHIFT)
        )
        self.alt_down: bool = (
            ctx.is_key_down(KEY_LEFT_ALT) or ctx.is_key_down(KEY_RIGHT_ALT)
        )

        # ── Mouse ──
        self.mouse_x: float = ctx.get_mouse_pos_x()
        self.mouse_y: float = ctx.get_mouse_pos_y()
        self.wheel_delta: float = ctx.get_mouse_wheel_delta() if area_hovered else 0.0
        self.lmb_clicked: bool = area_hovered and ctx.is_mouse_button_clicked(0)
        self.lmb_double_clicked: bool = area_hovered and ctx.is_mouse_double_clicked(0)
        self.lmb_down: bool = ctx.is_mouse_button_down(0)

        # ── Pan intent ──
        self.wants_pan_space = area_hovered and self.space_down and self.lmb_down
        self.wants_pan_mmb = area_hovered and ctx.is_mouse_button_down(2)
        self.wants_pan = self.wants_pan_space or self.wants_pan_mmb

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def pan_drag_button(self) -> int:
        """Return the mouse button index used for the current pan gesture."""
        return 0 if self.wants_pan_space else 2
