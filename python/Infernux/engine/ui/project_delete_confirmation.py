"""Editor-owned confirmation for Project asset deletion."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from typing import Optional

from Infernux.debug import Debug
from Infernux.engine.i18n import t
from Infernux.engine.path_utils import path_key, resolved_path
from .editor_modal import (
    EditorModalAction,
    begin_editor_modal,
    end_editor_modal,
    render_editor_modal_actions,
)


class ProjectDeleteConfirmationCoordinator:
    """Confirm destructive Project operations without opening an OS dialog."""

    _instance: Optional["ProjectDeleteConfirmationCoordinator"] = None

    def __init__(self) -> None:
        self._paths: tuple[str, ...] = ()
        self._delete_handler: Optional[Callable[[list[str]], bool]] = None
        self._requested = False
        self._error = ""

    @classmethod
    def instance(cls) -> "ProjectDeleteConfirmationCoordinator":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def is_active(self) -> bool:
        return bool(self._paths)

    @property
    def paths(self) -> tuple[str, ...]:
        return self._paths

    def request(
        self,
        paths: Iterable[str],
        delete_handler: Callable[[list[str]], bool],
    ) -> bool:
        if self.is_active or not callable(delete_handler):
            return False
        unique: list[str] = []
        seen: set[str] = set()
        for value in paths:
            path = resolved_path(str(value or ""))
            key = path_key(path)
            if not value or key in seen or not os.path.exists(path):
                continue
            seen.add(key)
            unique.append(path)
        if not unique:
            return False
        self._paths = tuple(unique)
        self._delete_handler = delete_handler
        self._requested = True
        self._error = ""
        return True

    def render(self, ctx) -> None:
        if not self.is_active:
            return

        popup_id = f"{t('project.delete_confirm_title')}###project_delete_confirm"
        request_open = self._requested
        self._requested = False
        if not begin_editor_modal(
            ctx,
            popup_id=popup_id,
            title=t("project.delete_confirm_title"),
            semantic_id="project.delete.dialog",
            request_open=request_open,
        ):
            return

        if len(self._paths) == 1:
            message = t("project.delete_confirm_msg").format(
                name=os.path.basename(self._paths[0])
            )
        else:
            message = t("project.delete_confirm_multi_msg").format(count=len(self._paths))
        ctx.text_wrapped(message)
        if self._error:
            ctx.spacing()
            ctx.text_wrapped(t(self._error))
        render_editor_modal_actions(
            ctx,
            [
                EditorModalAction(t("editor.modal.delete"), "confirm", lambda: self._confirm(ctx)),
                EditorModalAction(t("editor.modal.cancel"), "cancel", lambda: self._cancel(ctx)),
            ],
            semantic_prefix="project.delete",
        )
        end_editor_modal(ctx)

    def _confirm(self, ctx) -> None:
        handler = self._delete_handler
        if handler is None:
            self._error = "project.delete_unavailable"
            return
        try:
            deleted = bool(handler(list(self._paths)))
        except Exception as exc:
            Debug.log_error(f"Project asset deletion failed: {exc}")
            deleted = False
        if not deleted:
            self._error = "project.delete_failed"
            return
        self._close(ctx)

    def _cancel(self, ctx) -> None:
        self._close(ctx)

    def _close(self, ctx) -> None:
        self._paths = ()
        self._delete_handler = None
        self._requested = False
        self._error = ""
        ctx.close_current_popup()
