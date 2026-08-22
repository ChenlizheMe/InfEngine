"""Dockable read-only view of the global editor action journal."""

from __future__ import annotations

from Infernux.engine.i18n import t
from Infernux.engine.interaction import EditorInteractionCore, PanelInteractionDescriptor

from .editor_panel import EditorPanel
from .panel_registry import editor_panel


@editor_panel(
    "History",
    type_id="history",
    title_key="panel.history",
    menu_path="Window",
    interaction=PanelInteractionDescriptor(),
)
class HistoryPanel(EditorPanel):
    """Search journal evidence without creating a second history cursor."""

    def __init__(self) -> None:
        super().__init__(t("panel.history"), "history")

    def _initial_size(self) -> tuple[float, float]:
        return 560.0, 420.0

    def on_render_content(self, ctx) -> None:
        core = EditorInteractionCore.instance()
        if core is None:
            ctx.text_wrapped(t("history.unavailable"))
            return

        model = core.history
        query = ctx.text_input(
            t("history.search") + "##history_search",
            model.query,
            256,
        )
        model.set_query(query)
        ctx.record_semantic_item(
            "text_input",
            t("history.search"),
            True,
            "history.search",
        )

        snapshot = model.snapshot
        ctx.label(
            t("history.summary").format(
                applied=snapshot.cursor,
                total=snapshot.total,
                visible=len(snapshot.entries),
            )
        )
        ctx.separator()

        if not snapshot.entries:
            ctx.text_wrapped(t("history.empty"))
            return

        visible = ctx.begin_child("##history_entries", 0.0, 0.0, False)
        if visible:
            for row in snapshot.entries:
                state = (
                    t("history.state.applied")
                    if row.state == "applied"
                    else t("history.state.redo")
                )
                marker = " >" if row.is_next_undo or row.is_next_redo else ""
                context = f"  [{row.context}]" if row.context else ""
                ctx.label(f"{row.sequence:03d}  {state}{marker}  {row.description}{context}")
                ctx.record_semantic_item(
                    "history_entry",
                    row.description,
                    True,
                    f"history.entry.{row.operation_id}",
                )
                if ctx.is_item_hovered():
                    details = [
                        f"{t('history.command')}: {row.command_id}",
                        f"{t('history.target')}: {row.target or '-'}",
                        f"{t('history.document')}: {row.document or '-'}",
                        f"{t('history.origin')}: {row.origin}",
                    ]
                    ctx.set_tooltip("\n".join(details))
        ctx.end_child()


__all__ = ["HistoryPanel"]
