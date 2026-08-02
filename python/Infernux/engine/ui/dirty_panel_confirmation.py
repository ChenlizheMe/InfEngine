"""ImGui presentation for document-aware close transactions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from Infernux.engine.i18n import t
from Infernux.engine.interaction import (
    CloseCoordinator,
    CloseIntent,
    CloseIntentKind,
    CloseIssue,
    CloseState,
)


class DirtyPanelConfirmationCoordinator:
    """Present one shared save/discard/cancel modal for CloseCoordinator."""

    _instance: Optional["DirtyPanelConfirmationCoordinator"] = None

    def __init__(self, close_coordinator: Optional[CloseCoordinator] = None) -> None:
        if close_coordinator is None:
            from Infernux.engine.interaction import EditorInteractionCore, DocumentRegistry

            core = EditorInteractionCore.instance()
            close_coordinator = (
                core.close_coordinator
                if core is not None
                and core.documents is DocumentRegistry.instance()
                else None
            )
        self._close = close_coordinator or CloseCoordinator()
        self._scope = ""
        self._panel_id = ""
        self._show_popup = False

    @classmethod
    def instance(cls) -> "DirtyPanelConfirmationCoordinator":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def is_active(self) -> bool:
        return self._close.is_active

    @property
    def active_panel_id(self) -> str:
        if self._scope == "panel":
            return self._panel_id
        document = self._close.active_document
        return sorted(document.view_ids)[0] if document and document.view_ids else ""

    @property
    def active_document_id(self) -> str:
        document = self._close.active_document
        return document.document_id if document is not None else ""

    @property
    def waiting_for_save(self) -> bool:
        return self._close.state is CloseState.WAITING_FOR_SAVE

    def request_exit(
        self,
        on_complete: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> bool:
        """Begin a global close transaction, superseding a panel-only prompt."""
        if self.is_active:
            if self._scope == "exit":
                return False
            self._close.cancel()
        return self._request(
            "exit",
            CloseIntent(CloseIntentKind.EXIT_EDITOR),
            on_complete,
            on_cancel,
        )

    def request_panel_close(
        self,
        panel_id: str,
        on_complete: Callable[[], None],
        on_cancel: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Begin a titlebar-close transaction for one panel view."""
        identifier = str(panel_id or "").strip()
        if not identifier or self.is_active:
            return False
        return self._request(
            "panel",
            CloseIntent(CloseIntentKind.CLOSE_VIEW, view_id=identifier),
            on_complete,
            on_cancel,
            panel_id=identifier,
        )

    def render(self, ctx, *, panel_host_id: Optional[str] = None) -> None:
        """Poll saves and render from the window that owns the transaction."""
        if not self.is_active:
            return
        if self._scope == "panel":
            if str(panel_host_id or "") != self._panel_id:
                return
        elif panel_host_id is not None:
            return

        if self.waiting_for_save:
            self._close.poll()
            if not self.is_active:
                return
            if self.waiting_for_save:
                return
            self._show_popup = True

        document = self._close.active_document
        if document is None:
            return

        from .unsaved_changes_dialog import render_unsaved_changes_dialog

        choice = render_unsaved_changes_dialog(
            ctx,
            popup_id="Unsaved Changes###editor_dirty_panel_confirm",
            semantic_prefix="editor.dirty_panel",
            document_title=document.title,
            action="exit" if self._scope == "exit" else "close",
            error=self._localized_issue(),
            request_open=self._show_popup,
        )
        self._show_popup = False
        if choice == "save":
            self.choose_save()
        elif choice == "discard":
            self.choose_discard()
        elif choice == "cancel":
            self.choose_cancel()

    def choose_save(self) -> None:
        if not self.is_active:
            return
        self._close.decide_save()
        self._sync_presentation_after_decision()

    def choose_discard(self) -> None:
        if not self.is_active:
            return
        self._close.decide_discard()
        self._sync_presentation_after_decision()

    def choose_cancel(self) -> None:
        self._close.cancel()

    def _request(
        self,
        scope: str,
        intent: CloseIntent,
        on_complete: Callable[[], None],
        on_cancel: Optional[Callable[[], None]],
        *,
        panel_id: str = "",
    ) -> bool:
        self._scope = scope
        self._panel_id = panel_id
        self._show_popup = False

        def _complete() -> None:
            self._reset_presentation()
            on_complete()

        def _cancel() -> None:
            self._reset_presentation()
            if callable(on_cancel):
                on_cancel()

        accepted = self._close.request(intent, _complete, _cancel)
        if not accepted:
            self._reset_presentation()
            return False
        self._show_popup = self.is_active
        return True

    def _sync_presentation_after_decision(self) -> None:
        if not self.is_active:
            return
        self._show_popup = self._close.state is CloseState.AWAITING_DECISION

    def _localized_issue(self) -> str:
        issue = self._close.issue
        if issue is CloseIssue.NONE:
            return ""
        return {
            CloseIssue.SAVE_NOT_SUPPORTED: t("editor.unsaved.no_save_action"),
            CloseIssue.SAVE_CANCELLED: t("editor.unsaved.save_cancelled"),
            CloseIssue.SAVE_FAILED: t("editor.unsaved.save_failed"),
            CloseIssue.DISCARD_NOT_SUPPORTED: t("editor.unsaved.no_discard_action"),
            CloseIssue.DISCARD_FAILED: t("editor.unsaved.discard_failed"),
            CloseIssue.STILL_DIRTY: t("editor.unsaved.still_dirty"),
        }.get(issue, self._close.message)

    def _reset_presentation(self) -> None:
        self._scope = ""
        self._panel_id = ""
        self._show_popup = False
