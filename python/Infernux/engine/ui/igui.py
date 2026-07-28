"""
IGUI — Infernux Unified Editor GUI System
===========================================

A single, composable widget library that replaces the scattered rendering
helpers in ``inspector_utils``, ``inspector_components``,
``inspector_renderstack``, and ``inspector_material``.

Design goals
~~~~~~~~~~~~
* **One API** for every property type — components, materials, effects,
  C++ builtins, and future systems all call into the same widget set.
* **Unified drag-drop** — drop-target highlighting (white outline), reorder
  separators (white line), and payload routing in one place.
* **Unified list widget** — ``igui_list()`` renders a header with ``[+]``
  and ``[-]`` buttons, optional drag-to-reorder with per-slot indicators,
  and reference-type drop targets.  Used by serialized-field lists,
  RenderStack mounted effects, Build-Settings scene list, and any future
  ordered collection.
* **Zero duplication** — every widget is defined exactly once.

Usage::

    from Infernux.engine.ui.igui import IGUI

    IGUI.object_field(ctx, "mat_slot", display, "Material",
                      accept="MATERIAL_FILE", on_drop=my_cb)
    IGUI.list_header(ctx, "items", count=5, on_add=..., on_remove=...)
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence, Tuple, Union

from Infernux.lib import InxGUIContext
from Infernux.engine.i18n import t
from .editor_icons import EditorIcons
from .theme import Theme, ImGuiCol, ImGuiStyleVar, ImGuiTreeNodeFlags

# ═══════════════════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════════════════

# White colour for drop-target outline and reorder indicator line
_DROP_OUTLINE_COLOR = Theme.DND_DROP_OUTLINE
_DROP_OUTLINE_THICKNESS = Theme.DND_DROP_OUTLINE_THICKNESS
_REORDER_LINE_COLOR = Theme.DND_REORDER_LINE
_REORDER_LINE_THICKNESS = Theme.DND_REORDER_LINE_THICKNESS
_REORDER_SEPARATOR_H = Theme.DND_REORDER_SEPARATOR_H

# Internal drag-drop type for generic list reordering
_LIST_REORDER_TYPE = "IGUI_LIST_REORDER"

# Button sizes
_INLINE_BTN_W: float = 24.0
_PICKER_DOT_W: float = 20.0
_MINI_ICON_BTN_SIDE: float = _PICKER_DOT_W
_MINI_ICON_DRAW_SIZE: float = 10.0

# Picker popup filter state (keyed by field_id)
_picker_filters: dict = {}

# Track which popups need auto-focus on the search input
_popup_needs_focus: set = set()

# Cache list body heights from previous frame so the fill can be drawn behind
# the next frame's content while the border uses exact bounds every frame.
_list_body_heights: dict[str, float] = {}


# ═══════════════════════════════════════════════════════════════════════════
#  IGUI — the unified editor-GUI namespace
# ═══════════════════════════════════════════════════════════════════════════

class IGUI:
    """Static namespace for all unified editor widgets.

    Every method is ``@staticmethod`` so callers write ``IGUI.xxx(ctx, ...)``.
    """

    # ------------------------------------------------------------------
    #  Drop-target outline (white highlight when hovering a valid target)
    # ------------------------------------------------------------------

    @staticmethod
    def drop_target(
        ctx: InxGUIContext,
        accept_type: str,
        on_drop: Callable[[Any], None],
        *,
        outline: bool = True,
    ) -> bool:
        """Wrap the **last** ImGui item as a drag-drop target.

        If *outline* is True, a white rect is drawn around the item while
        a compatible payload hovers over it.

        Returns True if a payload was accepted this frame.
        """
        accepted = False
        # Push transparent DragDropTarget colour so ImGui's built-in
        # highlight doesn't interfere — we draw our own outline.
        ctx.push_style_color(ImGuiCol.DragDropTarget, 0.0, 0.0, 0.0, 0.0)
        if ctx.begin_drag_drop_target():
            if outline:
                IGUI._draw_item_outline(ctx, *_DROP_OUTLINE_COLOR, _DROP_OUTLINE_THICKNESS)
            payload = ctx.accept_drag_drop_payload(accept_type)
            if payload is not None:
                on_drop(payload)
                accepted = True
            ctx.end_drag_drop_target()
        ctx.pop_style_color(1)
        return accepted

    @staticmethod
    def multi_drop_target(
        ctx: InxGUIContext,
        accept_types: Sequence[str],
        on_drop: Callable[[str, Any], None],
        *,
        outline: bool = True,
    ) -> bool:
        """Like ``drop_target`` but accepts multiple payload types.

        *on_drop* receives ``(type_str, payload)``.
        """
        accepted = False
        ctx.push_style_color(ImGuiCol.DragDropTarget, 0.0, 0.0, 0.0, 0.0)
        if ctx.begin_drag_drop_target():
            if outline:
                IGUI._draw_item_outline(ctx, *_DROP_OUTLINE_COLOR, _DROP_OUTLINE_THICKNESS)
            for dt in accept_types:
                payload = ctx.accept_drag_drop_payload(dt)
                if payload is not None:
                    on_drop(dt, payload)
                    accepted = True
                    break
            ctx.end_drag_drop_target()
        ctx.pop_style_color(1)
        return accepted

    @staticmethod
    def _mini_icon_button(
        ctx: InxGUIContext,
        button_id: str,
        icon_name: str,
        fallback_label: str,
    ) -> bool:
        """Render a shared square mini icon button used by picker and list +/-."""
        btn_side = _MINI_ICON_BTN_SIDE
        color_count = Theme.push_inline_button_style(ctx)
        ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, *Theme.INSPECTOR_SMALL_ICON_BTN_FRAME_PAD)
        clicked = ctx.button(button_id, None, width=btn_side, height=Theme.INSPECTOR_INLINE_BTN_H)
        min_x = ctx.get_item_rect_min_x()
        min_y = ctx.get_item_rect_min_y()
        max_x = ctx.get_item_rect_max_x()
        max_y = ctx.get_item_rect_max_y()
        draw_size = min(
            _MINI_ICON_DRAW_SIZE,
            max(0.0, (max_x - min_x) - 6.0),
            max(0.0, (max_y - min_y) - 4.0),
        )
        draw_x = min_x + max(0.0, ((max_x - min_x) - draw_size) * 0.5)
        draw_y = min_y + max(0.0, ((max_y - min_y) - draw_size) * 0.5)
        tex_id = EditorIcons.get_cached(icon_name)
        if tex_id and draw_size > 0.0:
            ctx.draw_image_rect(tex_id, draw_x, draw_y, draw_x + draw_size, draw_y + draw_size)
        else:
            ctx.draw_text_aligned(min_x, min_y, max_x, max_y, fallback_label,
                                  1.0, 1.0, 1.0, 1.0,
                                  0.5, 0.5)
        ctx.pop_style_var(1)
        ctx.pop_style_color(color_count)
        return clicked

    # ------------------------------------------------------------------
    #  Object field (reference slot: material, texture, shader, etc.)
    # ------------------------------------------------------------------

    @staticmethod
    def object_field(
        ctx: InxGUIContext,
        field_id: str,
        display_text: str,
        type_hint: str,
        *,
        selected: bool = False,
        clickable: bool = True,
        accept: Optional[Union[str, Sequence[str]]] = None,
        on_drop: Optional[Callable[[Any], None]] = None,
        # Picker parameters
        picker_scene_items: Optional[Callable[[str], Sequence[tuple]]] = None,
        picker_asset_items: Optional[Callable[[str], Sequence[tuple]]] = None,
        on_pick: Optional[Callable[[Any], None]] = None,
        on_clear: Optional[Callable[[], None]] = None,
        on_ping: Optional[Callable[[], None]] = None,
        ping_path: Optional[str] = None,
        semantic_id: str = "",
    ) -> bool:
        """Render a Unity-style object-reference field with optional drop target
        and picker popup.

        *picker_scene_items* / *picker_asset_items*: ``filter_text -> [(label, value), ...]``
        *on_pick*: called with the selected value when user picks an item.
        *on_clear*: called when user picks "None" to clear the field.
        *on_ping*: called on body double-click (e.g. reveal asset in Project).
        *ping_path*: when *on_ping* is omitted, auto-reveals this asset path in
        Project on body double-click.

        Returns True if the field selectable was clicked.
        """
        has_picker = (picker_scene_items is not None
                      or picker_asset_items is not None)
        picker_texture = EditorIcons.get_cached(Theme.ICON_IMG_PICKER) if has_picker else 0
        interaction = int(ctx.render_object_field_chrome(
            field_id,
            display_text,
            type_hint,
            selected,
            clickable,
            has_picker,
            int(picker_texture or 0),
            semantic_id,
        ))
        clicked = bool(interaction & 1)
        doubled = bool(interaction & 4)
        # Only the picker button (bit 2) opens the selector — body double-click
        # (bit 4) pings the asset in Project instead.
        if (interaction & 2) and has_picker:
            _popup_needs_focus.add(field_id)
            _picker_filters.pop(f"_igui_filter_{field_id}", None)
        if doubled:
            if on_ping is not None:
                on_ping()
            else:
                path = str(ping_path or "").strip()
                if path and path.lower() not in {"none", "null"}:
                    from ._inspector_references import ping_asset_in_project
                    ping_asset_in_project(path)

        if accept and on_drop:
            if isinstance(accept, str):
                IGUI.drop_target(ctx, accept, on_drop)
            else:
                IGUI.multi_drop_target(ctx, list(accept),
                                       lambda _dt, payload: on_drop(payload))

        if has_picker:
            ctx.push_id_str(field_id)
            try:
                IGUI._render_object_picker_popup(
                    ctx, field_id,
                    picker_scene_items, picker_asset_items,
                    on_pick, on_clear,
                )
            finally:
                ctx.pop_id()

        return clicked

    # ------------------------------------------------------------------
    #  Picker popup (internal)
    # ------------------------------------------------------------------

    @staticmethod
    def _render_object_picker_popup(
        ctx: InxGUIContext,
        field_id: str,
        scene_items: Optional[Callable[[str], Sequence[tuple]]],
        asset_items: Optional[Callable[[str], Sequence[tuple]]],
        on_pick: Optional[Callable[[Any], None]],
        on_clear: Optional[Callable[[], None]],
    ) -> None:
        """Render the object picker popup with Scene / Assets tabs."""
        if not ctx.begin_popup("##obj_picker"):
            return

        # Auto-focus the search input on first frame
        if field_id in _popup_needs_focus:
            ctx.set_keyboard_focus_here()
            _popup_needs_focus.discard(field_id)

        # Filter input
        key = f"_igui_filter_{field_id}"
        prev_filter = _picker_filters.get(key, "")
        new_filter = ctx.input_text_with_hint("##filter", t("igui.search_hint"), prev_filter, 256)
        _picker_filters[key] = new_filter

        ctx.separator()

        # "None" option at top
        if on_clear is not None:
            if ctx.selectable(t("igui.none"), False):
                on_clear()
                ctx.close_current_popup()

        # Constrain the item list height (min 80, max 300)
        _PICKER_MIN_H = 80.0
        _PICKER_MAX_H = 300.0

        # Tabs
        has_scene = scene_items is not None
        has_assets = asset_items is not None

        if has_scene and has_assets:
            if ctx.begin_tab_bar("##picker_tabs"):
                try:
                    if ctx.begin_tab_item(t("igui.tab_scene")):
                        try:
                            visible = ctx.begin_child("##picker_list_scene", 0, _PICKER_MAX_H, False)
                            try:
                                if visible:
                                    IGUI._render_picker_items(ctx, scene_items, new_filter, on_pick)
                            finally:
                                ctx.end_child()
                        finally:
                            ctx.end_tab_item()
                    if ctx.begin_tab_item(t("igui.tab_assets")):
                        try:
                            visible = ctx.begin_child("##picker_list_assets", 0, _PICKER_MAX_H, False)
                            try:
                                if visible:
                                    IGUI._render_picker_items(ctx, asset_items, new_filter, on_pick)
                            finally:
                                ctx.end_child()
                        finally:
                            ctx.end_tab_item()
                finally:
                    ctx.end_tab_bar()
        elif has_scene:
            if ctx.begin_child("##picker_list", 0, _PICKER_MAX_H, False):
                IGUI._render_picker_items(ctx, scene_items, new_filter, on_pick)
            ctx.end_child()
        elif has_assets:
            if ctx.begin_child("##picker_list", 0, _PICKER_MAX_H, False):
                IGUI._render_picker_items(ctx, asset_items, new_filter, on_pick)
            ctx.end_child()

        ctx.end_popup()

    @staticmethod
    def _render_picker_items(
        ctx: InxGUIContext,
        items_fn: Callable[[str], Sequence[tuple]],
        filter_text: str,
        on_pick: Optional[Callable[[Any], None]],
    ) -> None:
        """Render the clickable items list inside the picker."""
        items = items_fn(filter_text)
        for idx, (label, value) in enumerate(items):
            ctx.push_id(idx)
            if ctx.selectable(label, False):
                if on_pick is not None:
                    on_pick(value)
                ctx.close_current_popup()
            ctx.pop_id()

    # ------------------------------------------------------------------
    #  Reorder separator (white-line drop indicator between list items)
    # ------------------------------------------------------------------

    @staticmethod
    def reorder_separator(
        ctx: InxGUIContext,
        sep_id: str,
        accept_type: str,
        on_drop: Callable[[Any], None],
    ) -> bool:
        """Render a thin invisible drop zone with a white insertion line.

        Returns True if a payload was accepted.
        """
        avail_w = ctx.get_content_region_avail_width()
        ctx.invisible_button(sep_id, avail_w, _REORDER_SEPARATOR_H)
        accepted = False
        ctx.push_style_color(ImGuiCol.DragDropTarget, 0.0, 0.0, 0.0, 0.0)
        if ctx.begin_drag_drop_target():
            IGUI._draw_separator_line(ctx, avail_w)
            payload = ctx.accept_drag_drop_payload(accept_type)
            if payload is not None:
                on_drop(payload)
                accepted = True
            ctx.end_drag_drop_target()
        ctx.pop_style_color(1)
        return accepted

    # ------------------------------------------------------------------
    #  List header (unified [+] [-] on the right side)
    # ------------------------------------------------------------------

    @staticmethod
    def list_header(
        ctx: InxGUIContext,
        label: str,
        count: int,
        *,
        on_add: Optional[Callable[[], None]] = None,
        on_remove: Optional[Callable[[], None]] = None,
        accept_drop: Optional[str] = None,
        on_header_drop: Optional[Callable[[Any], None]] = None,
        level: str = "list",
    ) -> bool:
        """Render a collapsing list header: ``▶ label [N]  ... [−][+]``

        * ``on_add`` — callback for the [+] button
        * ``on_remove`` — callback for the [−] button (only if count > 0)
        * ``accept_drop`` / ``on_header_drop`` — drop target on the header

        Returns the collapsing-header expanded state.
        """
        from .inspector_utils import render_compact_section_header

        header_label = f"{label} [{count}]"

        # Determine if we'll have overlapping buttons
        has_btns = bool(on_add) or (bool(on_remove) and count > 0)

        header_open = render_compact_section_header(
            ctx, header_label, level=level, allow_overlap=has_btns,
        )

        # Drop target on the header
        if accept_drop and on_header_drop:
            IGUI.drop_target(ctx, accept_drop, on_header_drop)

        # [−][+] buttons right-aligned on the same row
        btns_w = 0.0
        if on_add:
            btns_w += _MINI_ICON_BTN_SIDE
        if on_remove and count > 0:
            btns_w += _MINI_ICON_BTN_SIDE

        if btns_w > 0:
            ctx.same_line(0, 0)
            avail_w = ctx.get_content_region_avail_width()
            if avail_w >= btns_w:
                ctx.set_cursor_pos_x(ctx.get_cursor_pos_x() + avail_w - btns_w)

            if on_remove and count > 0:
                if IGUI._mini_icon_button(ctx, f"##{label}_remove", Theme.ICON_IMG_MINUS, Theme.ICON_MINUS):
                    on_remove()

            if on_add:
                if on_remove and count > 0:
                    ctx.same_line(0, 0)
                if IGUI._mini_icon_button(ctx, f"##{label}_add", Theme.ICON_IMG_PLUS, Theme.ICON_PLUS):
                    on_add()

        return header_open

    # ------------------------------------------------------------------
    #  Full list widget (header + items + reorder + drop zone)
    # ------------------------------------------------------------------

    @staticmethod
    def begin_list(
        ctx: InxGUIContext,
        list_id: str,
        count: int,
        *,
        on_add: Optional[Callable[[], None]] = None,
        on_remove_last: Optional[Callable[[], None]] = None,
        accept_drop: Optional[str] = None,
        on_header_drop: Optional[Callable[[Any], None]] = None,
        level: str = "list",
    ) -> bool:
        """Render the list header and return True if the body is expanded.

        Caller is responsible for rendering list items between
        ``begin_list()`` and ``end_list()``.
        """
        return IGUI.list_header(
            ctx, list_id, count,
            on_add=on_add,
            on_remove=on_remove_last,
            accept_drop=accept_drop,
            on_header_drop=on_header_drop,
            level=level,
        )

    @staticmethod
    def list_body_begin(ctx: InxGUIContext, list_id: str) -> tuple:
        """Begin the list items body using a cached fill plus exact per-frame border."""
        pad_x = Theme.INSPECTOR_LIST_BODY_PAD_X
        pad_y = Theme.INSPECTOR_LIST_BODY_PAD_Y
        start_x = ctx.get_window_pos_x() + ctx.get_cursor_pos_x() - pad_x
        start_y = ctx.get_window_pos_y() + ctx.get_cursor_pos_y()
        avail_w = ctx.get_content_region_avail_width() + pad_x * 2.0
        cached_h = _list_body_heights.get(list_id, 0.0)
        if cached_h > 0:
            ctx.draw_filled_rect(
                start_x, start_y,
                start_x + avail_w, start_y + cached_h,
                *Theme.INSPECTOR_LIST_BODY_BG,
                Theme.INSPECTOR_LIST_BODY_ROUNDING,
            )
        ctx.begin_group()
        ctx.dummy(0.0, pad_y)
        return (list_id, pad_x, avail_w)

    @staticmethod
    def list_body_end(ctx: InxGUIContext, state: tuple) -> None:
        """End the list items body, update cached fill height, and draw border."""
        list_id, pad_x, _avail_w = state
        pad_y = Theme.INSPECTOR_LIST_BODY_PAD_Y
        ctx.dummy(0.0, pad_y)
        ctx.end_group()
        min_x = ctx.get_item_rect_min_x() - pad_x
        min_y = ctx.get_item_rect_min_y()
        max_x = ctx.get_item_rect_max_x() + pad_x
        max_y = ctx.get_item_rect_max_y()
        _list_body_heights[list_id] = max_y - min_y
        ctx.draw_rect(
            min_x, min_y, max_x, max_y,
            *Theme.INSPECTOR_LIST_BODY_BORDER,
            1.0,
            Theme.INSPECTOR_LIST_BODY_ROUNDING,
        )

    @staticmethod
    def list_item_remove_button(
        ctx: InxGUIContext,
        item_id: str,
    ) -> bool:
        """Render a small ``[-]`` button for removing a single list element.

        Returns True if clicked.  Caller should ``same_line`` after this
        before rendering the item widget.
        """
        return IGUI._mini_icon_button(ctx, f"##{item_id}_rm", Theme.ICON_IMG_MINUS, Theme.ICON_MINUS)

    # ------------------------------------------------------------------
    #  Searchable combo (popup with search box + filtered items)
    # ------------------------------------------------------------------

    @staticmethod
    def searchable_combo(
        ctx: InxGUIContext,
        combo_id: str,
        current_idx: int,
        labels: Sequence[str],
        *,
        width: float = 0.0,
    ) -> int:
        """Render a combo-style widget that opens a searchable popup.

        Returns the new selected index (unchanged if nothing was picked).
        """
        btn_w = width if width > 0.0 else ctx.get_content_region_avail_width()
        color_count = Theme.push_inline_button_style(ctx)
        try:
            return ctx.searchable_combo(
                combo_id,
                current_idx,
                list(labels),
                btn_w,
                8,
                t("igui.search_hint"),
                t("igui.no_results"),
            )
        finally:
            ctx.pop_style_color(color_count)

    # ------------------------------------------------------------------
    #  Internal drawing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _draw_item_outline(
        ctx: InxGUIContext,
        r: float, g: float, b: float, a: float,
        thickness: float = 1.5,
    ) -> None:
        """Draw a rectangle outline around the last ImGui item."""
        min_x = ctx.get_item_rect_min_x()
        min_y = ctx.get_item_rect_min_y()
        max_x = ctx.get_item_rect_max_x()
        max_y = ctx.get_item_rect_max_y()
        ctx.draw_rect(min_x, min_y, max_x, max_y, r, g, b, a, thickness)

    @staticmethod
    def _draw_separator_line(ctx: InxGUIContext, width: float) -> None:
        """Draw a white horizontal line across the current invisible_button."""
        min_y = ctx.get_item_rect_min_y()
        max_y = ctx.get_item_rect_max_y()
        mid_y = (min_y + max_y) * 0.5
        x1 = ctx.get_item_rect_min_x()
        x2 = x1 + width
        r, g, b, a = _REORDER_LINE_COLOR
        ctx.draw_line(x1, mid_y, x2, mid_y, r, g, b, a, _REORDER_LINE_THICKNESS)
