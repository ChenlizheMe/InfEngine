"""Confirmation flow for deleting GameObjects from the active scene."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from Infernux.engine.i18n import t

from .editor_modal import (
    EditorModalAction,
    begin_editor_modal,
    end_editor_modal,
    render_editor_modal_actions,
)


class SceneDeleteConfirmationCoordinator:
    _instance = None

    def __init__(self) -> None:
        self._names: tuple[str, ...] = ()
        self._delete_handler: Callable[[], None] | None = None
        self._requested = False

    @classmethod
    def instance(cls) -> "SceneDeleteConfirmationCoordinator":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def is_active(self) -> bool:
        return self._delete_handler is not None

    def request(self, names: Sequence[str], delete_handler: Callable[[], None]) -> bool:
        if self.is_active or not callable(delete_handler):
            return False
        self._names = tuple(str(name or t("hierarchy.unnamed_object")) for name in names)
        if not self._names:
            return False
        self._delete_handler = delete_handler
        self._requested = True
        return True

    def render(self, ctx) -> None:
        if not self.is_active:
            return

        popup_id = f"{t('hierarchy.delete_confirm_title')}###scene_delete_confirm"
        request_open = self._requested
        self._requested = False
        if not begin_editor_modal(
            ctx,
            popup_id=popup_id,
            title=t("hierarchy.delete_confirm_title"),
            semantic_id="hierarchy.delete.dialog",
            request_open=request_open,
        ):
            return

        if len(self._names) == 1:
            message = t("hierarchy.delete_confirm_msg").format(name=self._names[0])
        else:
            message = t("hierarchy.delete_confirm_multi_msg").format(count=len(self._names))
        ctx.text_wrapped(message)
        render_editor_modal_actions(
            ctx,
            [
                EditorModalAction(t("editor.modal.delete"), "confirm", lambda: self._confirm(ctx)),
                EditorModalAction(t("editor.modal.cancel"), "cancel", lambda: self._cancel(ctx)),
            ],
            semantic_prefix="hierarchy.delete",
        )
        end_editor_modal(ctx)

    def _clear(self) -> None:
        self._names = ()
        self._delete_handler = None
        self._requested = False

    def _confirm(self, ctx) -> None:
        handler = self._delete_handler
        self._clear()
        ctx.close_current_popup()
        if handler is not None:
            handler()

    def _cancel(self, ctx) -> None:
        self._clear()
        ctx.close_current_popup()
