"""Top-level modal presentation for the global command palette."""

from __future__ import annotations

from Infernux.engine.i18n import t
from Infernux.engine.interaction.command_palette import (
    COMMAND_PALETTE_MODAL_ID,
    CommandPaletteService,
)

from .editor_modal import begin_editor_modal, end_editor_modal
from .dpi import scaled_editor_metric


class CommandPalettePresenter:
    def __init__(self, service: CommandPaletteService, modal_service) -> None:
        if not isinstance(service, CommandPaletteService):
            raise TypeError("command palette presenter requires CommandPaletteService")
        self._service = service
        modal_service.register(
            COMMAND_PALETTE_MODAL_ID,
            is_active=lambda: self._service.is_active,
            render=self.render,
            cancel=self._service.close,
        )

    def render(self, ctx) -> None:
        request_focus = self._service.request_search_focus
        if not begin_editor_modal(
            ctx,
            popup_id=t("command_palette.title") + "###editor_command_palette",
            title=t("command_palette.title"),
            semantic_id="editor.command_palette",
            request_open=request_focus,
            width=720.0,
            height=520.0,
        ):
            return

        if request_focus:
            ctx.set_keyboard_focus_here()
        query = ctx.text_input(
            t("command_palette.search") + "##command_palette_search",
            self._service.query,
            256,
        )
        self._service.set_query(query)
        ctx.record_semantic_item(
            "text_input",
            t("command_palette.search"),
            True,
            "command_palette.search",
        )
        ctx.spacing()

        entries = self._service.entries
        if not entries:
            ctx.text_wrapped(t("command_palette.empty"))
        else:
            child_visible = ctx.begin_child(
                "##command_palette_results", 0.0, 0.0, False
            )
            if child_visible:
                for index, entry in enumerate(entries):
                    if not entry.enabled:
                        ctx.begin_disabled(True)
                    category = entry.category or t("command_palette.uncategorized")
                    shortcut = f"    {entry.shortcut}" if entry.shortcut else ""
                    clicked = ctx.selectable(
                        f"{entry.display_name}    [{category}]{shortcut}##palette_{entry.command_id}",
                        index == self._service.selected_index,
                        width=0.0,
                        height=scaled_editor_metric(ctx, 30.0),
                    )
                    ctx.record_semantic_item(
                        "command",
                        entry.display_name,
                        entry.enabled,
                        f"command_palette.command.{entry.command_id}",
                    )
                    if not entry.enabled:
                        ctx.end_disabled()
                        if entry.disabled_reason and ctx.is_item_hovered():
                            ctx.set_tooltip(entry.disabled_reason)
                    if clicked:
                        self._service.select(index)
                        self._service.execute_selected()
                        break
            ctx.end_child()

        end_editor_modal(ctx)


__all__ = ["CommandPalettePresenter"]
