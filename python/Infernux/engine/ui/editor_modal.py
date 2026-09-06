"""Shared presentation primitives for Editor-owned modal dialogs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .dpi import editor_dpi_scale
from .theme import Theme


@dataclass(frozen=True)
class EditorModalAction:
    label: str
    semantic_id: str
    callback: Callable[[], None]
    enabled: bool = True


DEFAULT_MODAL_WIDTH = 560.0
DEFAULT_MODAL_HEIGHT = 220.0
DEFAULT_ACTION_WIDTH = 112.0
DEFAULT_ACTION_HEIGHT = 34.0


def begin_editor_modal(
    ctx,
    *,
    popup_id: str,
    title: str,
    semantic_id: str,
    request_open: bool = False,
    width: float = DEFAULT_MODAL_WIDTH,
    height: float = DEFAULT_MODAL_HEIGHT,
) -> bool:
    """Open and begin a consistently centred Editor modal."""
    dpi = editor_dpi_scale(ctx)
    if request_open:
        ctx.open_popup(popup_id)

    get_viewport = getattr(ctx, "get_main_viewport_bounds", None)
    set_position = getattr(ctx, "set_next_window_pos", None)
    if callable(get_viewport) and callable(set_position):
        viewport_x, viewport_y, viewport_w, viewport_h = get_viewport()
        set_position(
            viewport_x + viewport_w * 0.5,
            viewport_y + viewport_h * 0.5,
            Theme.COND_ALWAYS,
            0.5,
            0.5,
        )
    set_size = getattr(ctx, "set_next_window_size", None)
    if callable(set_size):
        set_size(float(width) * dpi, float(height) * dpi, Theme.COND_ALWAYS)
    if not ctx.begin_popup_modal(popup_id, 0):
        return False
    ctx.record_semantic_window("modal", title, semantic_id)
    return True


def render_editor_modal_actions(
    ctx,
    actions: Iterable[EditorModalAction],
    *,
    semantic_prefix: str,
) -> None:
    """Render the standard bottom-anchored command-button row."""
    dpi = editor_dpi_scale(ctx)
    action_list = list(actions)
    get_avail_h = getattr(ctx, "get_content_region_avail_height", None)
    get_cursor_y = getattr(ctx, "get_cursor_pos_y", None)
    set_cursor_y = getattr(ctx, "set_cursor_pos_y", None)
    if callable(get_avail_h) and callable(get_cursor_y) and callable(set_cursor_y):
        action_block_h = (DEFAULT_ACTION_HEIGHT + 24.0) * dpi
        remaining = float(get_avail_h())
        if remaining > action_block_h:
            set_cursor_y(float(get_cursor_y()) + remaining - action_block_h)

    ctx.spacing()
    ctx.separator()
    ctx.spacing()

    button_w = DEFAULT_ACTION_WIDTH * dpi
    button_h = DEFAULT_ACTION_HEIGHT * dpi
    gap = 8.0 * dpi
    total_w = len(action_list) * button_w + max(0, len(action_list) - 1) * gap
    get_avail = getattr(ctx, "get_content_region_avail_width", None)
    get_cursor = getattr(ctx, "get_cursor_pos_x", None)
    set_cursor = getattr(ctx, "set_cursor_pos_x", None)
    if callable(get_avail) and callable(get_cursor) and callable(set_cursor):
        available = float(get_avail())
        if available > total_w:
            set_cursor(float(get_cursor()) + available - total_w)

    for index, action in enumerate(action_list):
        if index:
            ctx.same_line()
        if not action.enabled:
            ctx.begin_disabled(True)
        suffix = action.semantic_id.replace(".", "_")
        ctx.button(
            f"{action.label}##{suffix}",
            action.callback,
            width=button_w,
            height=button_h,
        )
        ctx.record_semantic_item(
            "button", action.label, action.enabled,
            f"{semantic_prefix}.{action.semantic_id.rsplit('.', 1)[-1]}",
        )
        if not action.enabled:
            ctx.end_disabled()


def end_editor_modal(ctx) -> None:
    ctx.end_popup()
