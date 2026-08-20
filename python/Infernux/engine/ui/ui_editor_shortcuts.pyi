"""ui_editor_shortcuts — keyboard/mouse state snapshot for the UI Editor."""

from __future__ import annotations

from Infernux.lib import InxGUIContext


class UIEditorInput:
    """Snapshot of keyboard/mouse state relevant to the UI Editor.

    Usage::

        inp = UIEditorInput(ctx, area_hovered=True)
        if inp.wants_pan:
            ...
    """

    __slots__ = (
        "ctx",
        "area_hovered",
        "space_down",
        "ctrl_down",
        "shift_down",
        "alt_down",
        "wants_pan_space",
        "wants_pan_mmb",
        "wants_pan",
        "mouse_x",
        "mouse_y",
        "wheel_delta",
        "lmb_clicked",
        "lmb_double_clicked",
        "lmb_down",
    )

    ctx: InxGUIContext
    area_hovered: bool
    space_down: bool
    ctrl_down: bool
    shift_down: bool
    alt_down: bool
    wants_pan_space: bool
    wants_pan_mmb: bool
    wants_pan: bool
    mouse_x: float
    mouse_y: float
    wheel_delta: float
    lmb_clicked: bool
    lmb_double_clicked: bool
    lmb_down: bool

    def __init__(self, ctx: InxGUIContext, area_hovered: bool) -> None: ...

    @property
    def pan_drag_button(self) -> int: ...
