"""Floating graph overlays and ShaderGraph-style workspace list chrome.

Tabs use underline pagination (not pill buttons). Emitter / parameter /
event rows are rounded neutral-gray cards with a type-color accent dot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from Infernux.lib import InxGUIContext

from .theme import ImGuiCol, ImGuiMouseCursor, ImGuiStyleVar, Theme
OVERLAY_PANEL_BG = (0.14, 0.14, 0.145, 1.0)
OVERLAY_PANEL_ROUNDING = 6.0
OVERLAY_RESIZE_GRIP = 8.0
OVERLAY_MIN_HEIGHT = 120.0
OVERLAY_CONTENT_PAD = (8.0, 6.0)
OVERLAY_GRIP_LINE = (0.42, 0.42, 0.44, 0.85)

WORKSPACE_ROW_H = 22.0
WORKSPACE_ROW_INSET_X = 6.0
WORKSPACE_ROW_DOT_RADIUS = 3.0
WORKSPACE_ROW_DOT_X = 9.0
WORKSPACE_ROW_TEXT_X = 18.0
WORKSPACE_ROW_META_PAD = 64.0
# Compact ShaderGraph-style chips (neutral gray).
WORKSPACE_CARD_IDLE = (0.17, 0.17, 0.17, 1.0)
WORKSPACE_CARD_HOVER = (0.22, 0.22, 0.22, 1.0)
WORKSPACE_CARD_SELECTED = (0.27, 0.27, 0.27, 1.0)
WORKSPACE_CARD_BORDER = (0.32, 0.32, 0.32, 0.85)
WORKSPACE_CARD_BORDER_SELECTED = (0.48, 0.48, 0.48, 1.0)
WORKSPACE_CARD_ROUNDING = 4.0
WORKSPACE_ROW_GAP = 2.0

# Pagination tabs — selected accent matches theme red (210, 80, 80).
_TAB_TEXT_IDLE = (0.62, 0.62, 0.64, 1.0)
_TAB_TEXT_HOVER = (0.82, 0.82, 0.84, 1.0)
_TAB_TEXT_ACTIVE = (210.0 / 255.0, 80.0 / 255.0, 80.0 / 255.0, 1.0)
_TAB_UNDERLINE = _TAB_TEXT_ACTIVE
_TAB_BASELINE = (0.28, 0.28, 0.28, 1.0)
_TAB_HOVER_BG = (1.0, 1.0, 1.0, 0.04)
_TAB_FRAME_PAD = (8.0, 3.0)
_TAB_ITEM_SPACING = (2.0, 0.0)
_TAB_ROW_GAP = 6.0
_TAB_BUTTON_H = 20.0
_TAB_UNDERLINE_THICK = 2.0
_ADD_BTN_SIZE = 20.0


@dataclass
class FloatingOverlayState:
    height: float = 0.0
    drag_active: bool = False
    drag_start_y: float = 0.0
    drag_start_h: float = 0.0


def clamp_overlay_height(height: float, *, avail_h: float, margin: float) -> float:
    max_h = max(1.0, avail_h - margin * 2.0)
    min_h = min(OVERLAY_MIN_HEIGHT, max_h)
    return max(min_h, min(float(height), max_h))


def update_overlay_resize_drag(
    ctx: InxGUIContext,
    state: FloatingOverlayState,
    *,
    avail_h: float,
    margin: float,
) -> float:
    if state.drag_active:
        if ctx.is_mouse_button_down(0):
            dy = ctx.get_mouse_pos_y() - state.drag_start_y
            state.height = clamp_overlay_height(
                state.drag_start_h + dy,
                avail_h=avail_h,
                margin=margin,
            )
            ctx.set_mouse_cursor(ImGuiMouseCursor.ResizeNS)
        else:
            state.drag_active = False
    state.height = clamp_overlay_height(state.height, avail_h=avail_h, margin=margin)
    return state.height


def paint_overlay_background(
    ctx: InxGUIContext, *, width: float, height: float
) -> None:
    x0 = ctx.get_window_pos_x()
    y0 = ctx.get_window_pos_y()
    ctx.draw_filled_rect(
        x0,
        y0,
        x0 + width,
        y0 + height,
        *OVERLAY_PANEL_BG,
        OVERLAY_PANEL_ROUNDING,
    )


def render_floating_overlay(
    ctx: InxGUIContext,
    state: FloatingOverlayState,
    *,
    child_id: str,
    x: float,
    y: float,
    width: float,
    render_fn: Callable[[], None],
    max_height: float | None = None,
) -> None:
    grip = OVERLAY_RESIZE_GRIP
    panel_h = max(OVERLAY_MIN_HEIGHT, float(state.height))
    if max_height is not None:
        panel_h = min(panel_h, max(1.0, float(max_height)))
    pad_x, pad_y = OVERLAY_CONTENT_PAD

    ctx.set_cursor_pos_x(x)
    ctx.set_cursor_pos_y(y)
    ctx.push_style_color(ImGuiCol.ChildBg, 0.0, 0.0, 0.0, 0.0)
    ctx.push_style_color(ImGuiCol.Border, 0.0, 0.0, 0.0, 0.0)
    ctx.push_style_var_float(ImGuiStyleVar.ChildRounding, OVERLAY_PANEL_ROUNDING)
    visible = ctx.begin_child(child_id, width, panel_h, False)
    try:
        if visible:
            paint_overlay_background(ctx, width=width, height=panel_h)

            inner_w = max(1.0, width - pad_x * 2.0)
            content_area_h = max(1.0, panel_h - grip - pad_y)
            ctx.set_cursor_pos_x(pad_x)
            ctx.set_cursor_pos_y(pad_y)
            ctx.push_style_color(ImGuiCol.ChildBg, 0.0, 0.0, 0.0, 0.0)
            content_visible = ctx.begin_child(
                f"{child_id}_body", inner_w, content_area_h, False
            )
            try:
                if content_visible:
                    render_fn()
            finally:
                ctx.end_child()
                ctx.pop_style_color(1)

            ctx.set_cursor_pos_x(0.0)
            ctx.set_cursor_pos_y(panel_h - grip)
            ctx.invisible_button(f"{child_id}_resize_grip", width, grip)
            if ctx.is_item_hovered() or ctx.is_item_active():
                ctx.set_mouse_cursor(ImGuiMouseCursor.ResizeNS)
            if ctx.is_item_active() and not state.drag_active:
                state.drag_active = True
                state.drag_start_y = ctx.get_mouse_pos_y()
                state.drag_start_h = panel_h

            x0 = ctx.get_window_pos_x()
            grip_y = ctx.get_window_pos_y() + panel_h - grip * 0.5
            ctx.draw_line(
                x0 + width * 0.38,
                grip_y,
                x0 + width * 0.62,
                grip_y,
                *OVERLAY_GRIP_LINE,
                1.5,
            )
    finally:
        ctx.end_child()
        ctx.pop_style_var(1)
        ctx.pop_style_color(2)


def render_compact_tab_bar(
    ctx: InxGUIContext,
    bar_id: str,
    tabs: Sequence[tuple[str, str]],
    active_index: int,
    *,
    semantic_prefix: str = "",
) -> int:
    """Left-aligned pagination tabs with underline selection (not pill buttons)."""
    selected_index = int(active_index)
    tab_rects: list[tuple[bool, bool, float, float, float, float]] = []

    ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, *_TAB_ITEM_SPACING)
    try:
        for index, (label, suffix) in enumerate(tabs):
            if index > 0:
                ctx.same_line(0.0, _TAB_ITEM_SPACING[0])
            selected = index == selected_index
            pad_x, pad_y = _TAB_FRAME_PAD
            text_w = max(32.0, float(ctx.calc_text_width(label)) + pad_x * 2.0)
            if ctx.invisible_button(f"##{bar_id}_{suffix}", text_w, _TAB_BUTTON_H):
                selected_index = index
                selected = True
            if semantic_prefix and bool(
                getattr(ctx, "semantic_capture_enabled", True)
            ):
                ctx.record_semantic_item(
                    "tab",
                    label,
                    True,
                    f"{semantic_prefix}.{suffix}",
                    bool_value=selected,
                )
            hovered = bool(ctx.is_item_hovered())
            x0 = ctx.get_item_rect_min_x()
            y0 = ctx.get_item_rect_min_y()
            x1 = ctx.get_item_rect_max_x()
            y1 = ctx.get_item_rect_max_y()
            if hovered and not selected:
                ctx.draw_filled_rect(x0, y0, x1, y1, *_TAB_HOVER_BG, 0.0)
            text_color = (
                _TAB_TEXT_ACTIVE
                if selected
                else (_TAB_TEXT_HOVER if hovered else _TAB_TEXT_IDLE)
            )
            ctx.draw_text_aligned(
                x0 + pad_x,
                y0,
                x1 - pad_x,
                y1,
                label,
                *text_color,
                0.0,
                0.5,
                0.0,
                True,
            )
            tab_rects.append((selected, hovered, x0, y0, x1, y1))
    finally:
        ctx.pop_style_var(1)

    if tab_rects:
        strip_x0 = min(r[2] for r in tab_rects)
        strip_x1 = max(r[4] for r in tab_rects)
        baseline_y = max(r[5] for r in tab_rects) - 1.0
        content_right = max(
            strip_x1,
            strip_x0 + max(1.0, ctx.get_content_region_avail_width()),
        )
        ctx.draw_line(
            strip_x0,
            baseline_y,
            content_right,
            baseline_y,
            *_TAB_BASELINE,
            1.0,
        )
        for selected, _hovered, x0, _y0, x1, y1 in tab_rects:
            if not selected:
                continue
            ctx.draw_line(
                x0 + 2.0,
                y1 - 1.0,
                x1 - 2.0,
                y1 - 1.0,
                *_TAB_UNDERLINE,
                _TAB_UNDERLINE_THICK,
            )

    ctx.dummy(0.0, _TAB_ROW_GAP)
    return selected_index


def render_workspace_add_header(
    ctx: InxGUIContext,
    title: str,
    button_id: str,
    *,
    popup_id: str = "",
    on_add: Callable[[], None] | None = None,
    build_popup: Callable[[InxGUIContext], None] | None = None,
    disabled: bool = False,
    semantic_id: str = "",
) -> None:
    from .igui import IGUI

    row_w = max(1.0, ctx.get_content_region_avail_width())
    row_x = ctx.get_cursor_pos_x()
    btn_size = _ADD_BTN_SIZE

    ctx.label(title)
    ctx.same_line(0, 0)
    ctx.set_cursor_pos_x(row_x + max(0.0, row_w - btn_size))
    if disabled:
        ctx.begin_disabled(True)
    clicked = IGUI._mini_icon_button(
        ctx,
        f"##workspace_add_{button_id.lstrip('#')}",
        Theme.ICON_IMG_PLUS,
        Theme.ICON_PLUS,
    )
    recorder = getattr(ctx, "record_semantic_item", None)
    if semantic_id and callable(recorder):
        recorder("button", title, not disabled, semantic_id)
    if disabled:
        ctx.end_disabled()
    if clicked:
        if popup_id:
            ctx.open_popup(popup_id)
        elif on_add is not None:
            on_add()
    if popup_id and build_popup is not None and ctx.begin_popup(popup_id):
        build_popup(ctx)
        ctx.end_popup()
    ctx.separator()


def workspace_entry_rect(ctx: InxGUIContext) -> tuple[float, float, float, float]:
    return (
        ctx.get_item_rect_min_x(),
        ctx.get_item_rect_min_y(),
        ctx.get_item_rect_max_x(),
        ctx.get_item_rect_max_y(),
    )


def begin_workspace_entry(
    ctx: InxGUIContext, entry_id: str, selected: bool
) -> tuple[bool, tuple[float, float, float, float]]:
    inset = WORKSPACE_ROW_INSET_X
    row_x = ctx.get_cursor_pos_x()
    row_w = max(1.0, ctx.get_content_region_avail_width())
    entry_w = max(1.0, row_w - inset * 2.0)
    ctx.set_cursor_pos_x(row_x + inset)
    # Transparent selectable — card chrome is painted in paint_workspace_entry.
    ctx.push_style_color(ImGuiCol.Header, 0.0, 0.0, 0.0, 0.0)
    ctx.push_style_color(ImGuiCol.HeaderHovered, 0.0, 0.0, 0.0, 0.0)
    ctx.push_style_color(ImGuiCol.HeaderActive, 0.0, 0.0, 0.0, 0.0)
    try:
        clicked = ctx.selectable(
            f"##{entry_id}",
            selected,
            width=entry_w,
            height=WORKSPACE_ROW_H,
        )
    finally:
        ctx.pop_style_color(3)
    return clicked, workspace_entry_rect(ctx)


def paint_workspace_entry(
    ctx: InxGUIContext,
    rect: tuple[float, float, float, float],
    *,
    primary: str,
    secondary: str,
    dot_color: tuple[float, float, float, float],
    meta_pad: float = WORKSPACE_ROW_META_PAD,
    selected: bool = False,
) -> None:
    """Paint a ShaderGraph-style rounded gray card over the last selectable.

    This function intentionally submits no ImGui item so context menus and
    drag sources still refer to the selectable created by
    :func:`begin_workspace_entry`. Call :func:`finish_workspace_entry` after
    all item-bound interactions have been handled.
    """
    x0, y0, x1, y1 = rect
    hovered = bool(ctx.is_item_hovered())
    held = hovered and bool(ctx.is_mouse_button_down(0))
    if selected:
        fill = WORKSPACE_CARD_SELECTED
        border = WORKSPACE_CARD_BORDER_SELECTED
    elif held or hovered:
        fill = WORKSPACE_CARD_HOVER
        border = WORKSPACE_CARD_BORDER
    else:
        fill = WORKSPACE_CARD_IDLE
        border = WORKSPACE_CARD_BORDER

    ctx.draw_filled_rect(x0, y0, x1, y1, *fill, WORKSPACE_CARD_ROUNDING)
    ctx.draw_rect(x0, y0, x1, y1, *border, 1.0, WORKSPACE_CARD_ROUNDING)

    mid_y = (y0 + y1) * 0.5
    ctx.draw_filled_circle(
        x0 + WORKSPACE_ROW_DOT_X,
        mid_y,
        WORKSPACE_ROW_DOT_RADIUS,
        *dot_color,
    )
    ctx.draw_text_aligned(
        x0 + WORKSPACE_ROW_TEXT_X,
        y0,
        x1 - meta_pad,
        y1,
        primary,
        0.90,
        0.91,
        0.92,
        1.0,
        0.0,
        0.5,
        0.0,
        True,
    )
    if secondary:
        ctx.draw_text_aligned(
            x0 + 20.0,
            y0,
            x1 - 8.0,
            y1,
            secondary,
            0.58,
            0.59,
            0.61,
            1.0,
            1.0,
            0.5,
            0.0,
            True,
        )


def finish_workspace_entry(ctx: InxGUIContext) -> None:
    """Finish a workspace row and submit its trailing layout gap."""
    ctx.dummy(0.0, WORKSPACE_ROW_GAP)


__all__ = [
    "FloatingOverlayState",
    "OVERLAY_MIN_HEIGHT",
    "WORKSPACE_ROW_H",
    "WORKSPACE_ROW_META_PAD",
    "begin_workspace_entry",
    "clamp_overlay_height",
    "finish_workspace_entry",
    "paint_overlay_background",
    "paint_workspace_entry",
    "render_compact_tab_bar",
    "render_floating_overlay",
    "render_workspace_add_header",
    "update_overlay_resize_drag",
]
