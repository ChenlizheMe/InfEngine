"""Shared presentation for unsaved editor documents."""

from __future__ import annotations

from typing import Optional

from Infernux.engine.i18n import t
from .editor_modal import (
    EditorModalAction,
    begin_editor_modal,
    end_editor_modal,
    render_editor_modal_actions,
)


def render_unsaved_changes_dialog(
    ctx,
    *,
    popup_id: str,
    semantic_prefix: str,
    document_title: str,
    action: str,
    error: str = "",
    request_open: bool = False,
) -> Optional[str]:
    """Render the standard unsaved-document modal and return a chosen action."""
    dialog_title = t("editor.unsaved.title")
    if not begin_editor_modal(
        ctx,
        popup_id=popup_id,
        title=dialog_title,
        semantic_id=f"{semantic_prefix}.dialog",
        request_open=request_open,
    ):
        return None

    ctx.text_wrapped(t("editor.unsaved.message").format(document=document_title))
    question_key = "editor.unsaved.before_exit" if action == "exit" else "editor.unsaved.before_close"
    ctx.text_wrapped(t(question_key))
    if error:
        ctx.spacing()
        ctx.text_wrapped(error)
    selected: Optional[str] = None

    def _choose(value: str) -> None:
        nonlocal selected
        selected = value
        ctx.close_current_popup()

    save_label = t("editor.unsaved.save")
    discard_label = t("editor.unsaved.dont_save")
    cancel_label = t("editor.unsaved.cancel")
    render_editor_modal_actions(
        ctx,
        [
            EditorModalAction(save_label, "save", lambda: _choose("save")),
            EditorModalAction(discard_label, "discard", lambda: _choose("discard")),
            EditorModalAction(cancel_label, "cancel", lambda: _choose("cancel")),
        ],
        semantic_prefix=semantic_prefix,
    )
    end_editor_modal(ctx)
    return selected
