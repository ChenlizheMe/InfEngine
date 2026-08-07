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
from Infernux.engine.interaction.object_fields import (
    ASSET_REFERENCE_CLEAR_COMMAND,
    ASSET_REFERENCE_COPY_COMMAND,
    ASSET_REFERENCE_OPEN_COMMAND,
    ASSET_REFERENCE_PASTE_COMMAND,
    ASSET_REFERENCE_REVEAL_COMMAND,
    AssetReferenceFieldModel,
    ObjectFieldGesture,
    ObjectReferenceFieldModel,
    asset_reference_command_payload,
    object_picker_model,
)
from Infernux.engine.interaction.context_menus import (
    ContextMenuBuilder,
    ContextMenuCommand,
)
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
        on_open: Optional[Callable[[], None]] = None,
        ping_path: Optional[str] = None,
        semantic_id: str = "",
        has_value: Optional[bool] = None,
    ) -> bool:
        """Render a Unity-style object-reference field with optional drop target
        and picker popup.

        *picker_scene_items* / *picker_asset_items*: ``filter_text -> [(label, value), ...]``
        *on_pick*: called with the selected value when user picks an item.
        *on_clear*: called when user picks "None" to clear the field.
        *on_ping*: called on body single-click (e.g. reveal asset in Project).
        *on_open*: called on body double-click. When omitted, double-click uses
        the same locate action as a single-click.
        *ping_path*: when *on_ping* is omitted, auto-reveals this asset path in
        Project from the body navigation action.

        Returns True if the field selectable was clicked.
        """
        model = ObjectReferenceFieldModel(
            field_id=field_id,
            display_text=display_text,
            type_hint=type_hint,
            selected=selected,
            clickable=clickable,
            accept=accept,
            scene_items=picker_scene_items,
            asset_items=picker_asset_items,
            on_drop=on_drop,
            on_pick=on_pick,
            on_clear=on_clear,
            on_locate=on_ping,
            on_open=on_open,
            ping_path=ping_path,
            semantic_id=semantic_id,
            has_value=has_value,
        )
        return IGUI.object_field_model(ctx, model)

    @staticmethod
    def object_field_model(
        ctx: InxGUIContext,
        model: ObjectReferenceFieldModel,
    ) -> bool:
        """Render an ObjectField from its shared interaction model."""

        has_picker = model.has_picker
        picker_texture = EditorIcons.get_cached(Theme.ICON_IMG_PICKER) if has_picker else 0
        interaction = int(ctx.render_object_field_chrome(
            model.field_id,
            model.display_text,
            model.type_hint,
            model.selected,
            model.clickable,
            has_picker,
            int(picker_texture or 0),
            model.semantic_id,
        ))

        # Drag/drop must bind to the ObjectField group before rendering either
        # popup, because popup rows become ImGui's last submitted item.
        if model.accept and model.can_accept_drop:
            if isinstance(model.accept, str):
                IGUI.drop_target(ctx, model.accept, model.dispatch_drop)
            else:
                IGUI.multi_drop_target(
                    ctx,
                    list(model.accept),
                    lambda _drop_type, payload: model.dispatch_drop(payload),
                )

        IGUI.process_object_field_interaction(ctx, model, interaction)

        return bool(interaction & int(ObjectFieldGesture.LOCATE))

    @staticmethod
    def asset_reference_field(
        ctx: InxGUIContext,
        field_id: str,
        display_text: str,
        type_hint: str,
        *,
        selected: bool = False,
        clickable: bool = True,
        accept: Optional[Union[str, Sequence[str]]] = None,
        on_assign: Optional[Callable[[Any], None]] = None,
        picker_scene_items: Optional[Callable[[str], Sequence[tuple]]] = None,
        additional_asset_items: Optional[Callable[[str], Sequence[tuple]]] = None,
        on_clear: Optional[Callable[[], None]] = None,
        on_ping: Optional[Callable[[], None]] = None,
        on_open: Optional[Callable[[], None]] = None,
        ping_path: Optional[str] = None,
        semantic_id: str = "",
        has_value: Optional[bool] = None,
        asset_type: str = "",
        on_rejected: Optional[Callable[[str], None]] = None,
        reference_value: Any = None,
        transaction=None,
        alternate_compatibility: Optional[Callable[[Any], str]] = None,
        read_only: bool = False,
    ) -> bool:
        """Render an asset reference through the shared asset-field model."""

        from Infernux.engine.interaction import property_drawer_registry

        return IGUI.object_field_model(
            ctx,
            property_drawer_registry.create(
                "asset_reference",
                field_id=field_id,
                display_text=display_text,
                type_hint=type_hint,
                selected=selected,
                clickable=clickable,
                accept=accept,
                scene_items=picker_scene_items,
                additional_asset_items=additional_asset_items,
                on_assign=on_assign,
                on_clear=on_clear,
                on_locate=on_ping,
                on_open=on_open,
                ping_path=ping_path,
                semantic_id=semantic_id,
                has_value=has_value,
                asset_type=asset_type,
                on_rejected=on_rejected,
                reference_value=reference_value,
                transaction=transaction,
                alternate_compatibility=alternate_compatibility,
                field_read_only=read_only,
            ),
        )

    @staticmethod
    def _render_asset_reference_context_menu(
        ctx: InxGUIContext,
        model: AssetReferenceFieldModel,
    ):
        """Render and execute the shared command menu after popup scopes close."""

        semantic_root = str(model.semantic_id or "").strip() or (
            f"object_field.{model.field_id}"
        )
        specs = (
            ContextMenuCommand(
                ASSET_REFERENCE_OPEN_COMMAND,
                label=t("asset_reference.open"),
                semantic_id=f"{semantic_root}.context.open",
            ),
            ContextMenuCommand(
                ASSET_REFERENCE_REVEAL_COMMAND,
                label=t("asset_reference.reveal"),
                semantic_id=f"{semantic_root}.context.reveal",
            ),
            ContextMenuCommand(
                ASSET_REFERENCE_COPY_COMMAND,
                label=t("asset_reference.copy"),
                separator_before=True,
                semantic_id=f"{semantic_root}.context.copy",
            ),
            ContextMenuCommand(
                ASSET_REFERENCE_PASTE_COMMAND,
                label=t("asset_reference.paste"),
                semantic_id=f"{semantic_root}.context.paste",
            ),
            ContextMenuCommand(
                ASSET_REFERENCE_CLEAR_COMMAND,
                label=t("asset_reference.clear"),
                separator_before=True,
                semantic_id=f"{semantic_root}.context.clear",
            ),
        )
        builder = ContextMenuBuilder()
        request = None
        ctx.push_id_str(model.field_id)
        try:
            if not ctx.begin_popup("##asset_reference_context"):
                return None
            try:
                request = builder.render_deferred(
                    ctx,
                    specs,
                    payload=asset_reference_command_payload(
                        model,
                        clipboard_text=str(ctx.get_clipboard_text() or ""),
                        clipboard_writer=ctx.set_clipboard_text,
                    ),
                )
            finally:
                ctx.end_popup()
        finally:
            ctx.pop_id()
        return builder.execute_resolved(request) if request is not None else None

    @staticmethod
    def process_object_field_interaction(
        ctx: InxGUIContext,
        model: ObjectReferenceFieldModel,
        interaction: int,
        *,
        picker_open: bool = False,
        poll_picker: bool = True,
        context_open: bool = False,
        poll_context: bool = True,
    ) -> None:
        """Apply native chrome/picker output through one shared contract."""

        gesture = model.dispatch_chrome(interaction)
        if gesture & ObjectFieldGesture.OPEN_PICKER:
            object_picker_model.request_open(model.field_id)

        if isinstance(model, AssetReferenceFieldModel):
            context_requested = bool(gesture & ObjectFieldGesture.CONTEXT_MENU)
            if context_requested:
                ctx.push_id_str(model.field_id)
                try:
                    ctx.open_popup("##asset_reference_context")
                finally:
                    ctx.pop_id()
            if poll_context or context_open or context_requested:
                IGUI._render_asset_reference_context_menu(ctx, model)

        if not model.has_picker:
            return
        if not poll_picker and not (
            picker_open or gesture & ObjectFieldGesture.OPEN_PICKER
        ):
            return

        picker_intent = None
        ctx.push_id_str(model.field_id)
        try:
            picker_intent = IGUI._render_object_picker_popup(
                ctx,
                model.field_id,
                model.scene_items,
                model.asset_items,
                (
                    model.can_clear
                    if isinstance(model, AssetReferenceFieldModel)
                    else model.on_clear is not None
                ),
            )
        finally:
            ctx.pop_id()
        model.dispatch_picker(picker_intent)

    # ------------------------------------------------------------------
    #  Picker popup (internal)
    # ------------------------------------------------------------------

    @staticmethod
    def _render_object_picker_popup(
        ctx: InxGUIContext,
        field_id: str,
        scene_items: Optional[Callable[[str], Sequence[tuple]]],
        asset_items: Optional[Callable[[str], Sequence[tuple]]],
        allow_clear: bool,
    ) -> Optional[tuple[str, Any]]:
        """Render the picker and return an intent after all ImGui scopes close."""
        if not ctx.begin_popup("##obj_picker"):
            return None

        intent = None
        try:
            if object_picker_model.consume_focus_request(field_id):
                ctx.set_keyboard_focus_here()

            prev_filter = object_picker_model.query(field_id)
            new_filter = ctx.input_text_with_hint(
                "##filter", t("igui.search_hint"), prev_filter, 256
            )
            object_picker_model.set_query(field_id, new_filter)
            ctx.separator()

            if allow_clear and ctx.selectable(t("igui.none"), False):
                intent = ("clear", None)
                ctx.close_current_popup()

            picker_height = 300.0
            has_scene = scene_items is not None
            has_assets = asset_items is not None

            if intent is None and has_scene and has_assets:
                if ctx.begin_tab_bar("##picker_tabs"):
                    try:
                        if ctx.begin_tab_item(t("igui.tab_scene")):
                            try:
                                visible = ctx.begin_child(
                                    "##picker_list_scene", 0, picker_height, False
                                )
                                try:
                                    if visible:
                                        intent = IGUI._render_picker_items(
                                            ctx, scene_items, new_filter
                                        )
                                finally:
                                    ctx.end_child()
                            finally:
                                ctx.end_tab_item()
                        if intent is None and ctx.begin_tab_item(t("igui.tab_assets")):
                            try:
                                visible = ctx.begin_child(
                                    "##picker_list_assets", 0, picker_height, False
                                )
                                try:
                                    if visible:
                                        intent = IGUI._render_picker_items(
                                            ctx, asset_items, new_filter
                                        )
                                finally:
                                    ctx.end_child()
                            finally:
                                ctx.end_tab_item()
                    finally:
                        ctx.end_tab_bar()
            elif intent is None and has_scene:
                visible = ctx.begin_child("##picker_list", 0, picker_height, False)
                try:
                    if visible:
                        intent = IGUI._render_picker_items(ctx, scene_items, new_filter)
                finally:
                    ctx.end_child()
            elif intent is None and has_assets:
                visible = ctx.begin_child("##picker_list", 0, picker_height, False)
                try:
                    if visible:
                        intent = IGUI._render_picker_items(ctx, asset_items, new_filter)
                finally:
                    ctx.end_child()
        finally:
            ctx.end_popup()
        return intent

    @staticmethod
    def _render_picker_items(
        ctx: InxGUIContext,
        items_fn: Callable[[str], Sequence[tuple]],
        filter_text: str,
    ) -> Optional[tuple[str, Any]]:
        """Render picker rows and return the selected value without mutating it."""
        items = items_fn(filter_text)
        for idx, (label, value) in enumerate(items):
            ctx.push_id(idx)
            try:
                if ctx.selectable(label, False):
                    ctx.close_current_popup()
                    return ("pick", value)
            finally:
                ctx.pop_id()
        return None

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
