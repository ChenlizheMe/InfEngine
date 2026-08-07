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
    ModalService,
)


class DirtyPanelConfirmationCoordinator:
    """Present one shared save/discard/cancel modal for CloseCoordinator."""

    _instance: Optional["DirtyPanelConfirmationCoordinator"] = None
    MODAL_ID = "editor.unsaved_changes"

    def __init__(
        self,
        close_coordinator: Optional[CloseCoordinator] = None,
        modal_service: Optional[ModalService] = None,
    ) -> None:
        if close_coordinator is None:
            from Infernux.engine.interaction import EditorInteractionCore

            core = EditorInteractionCore.instance()
            if core is None:
                raise RuntimeError(
                    "DirtyPanelConfirmationCoordinator requires EditorInteractionCore"
                )
            close_coordinator = core.close_coordinator
            modal_service = modal_service or core.modals
        elif modal_service is None:
            raise ValueError(
                "modal_service is required with an explicit close_coordinator"
            )
        self._close = close_coordinator
        self._modals = modal_service
        self._scope = ""
        self._panel_id = ""
        self._show_popup = False
        self._defer_panel_popup_once = False
        self._presented_document_id = ""
        self._modals.register(
            self.MODAL_ID,
            is_active=lambda: self.is_active,
            render=self.render,
            cancel=self.choose_cancel,
        )

    @classmethod
    def instance(cls) -> "DirtyPanelConfirmationCoordinator":
        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is None:
            if cls._instance is not None:
                return cls._instance
            raise RuntimeError(
                "DirtyPanelConfirmationCoordinator requires EditorInteractionCore"
            )
        if cls._instance is None or cls._instance._modals is not core.modals:
            if cls._instance is not None and cls._instance.is_active:
                cls._instance.choose_cancel()
            cls._instance = cls(core.close_coordinator, core.modals)
        return cls._instance

    @property
    def is_active(self) -> bool:
        return self._close.is_active

    @property
    def active_panel_id(self) -> str:
        if self._scope == "panel":
            return self._panel_id
        document = self._close.active_document
        return self._preferred_document_view(document)

    @property
    def active_document_id(self) -> str:
        document = self._close.active_document
        return document.document_id if document is not None else ""

    @property
    def waiting_for_save(self) -> bool:
        return self._close.state is CloseState.WAITING_FOR_SAVE

    @property
    def waiting_for_conflict(self) -> bool:
        return self._close.state is CloseState.WAITING_FOR_CONFLICT

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

    def request_reset_layout(
        self,
        document_ids: tuple[str, ...],
        on_complete: Callable[[], None],
        on_cancel: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Resolve documents that lose their final View before resetting layout."""
        if self.is_active:
            return False
        return self._request(
            "reset_layout",
            CloseIntent(
                CloseIntentKind.RESET_LAYOUT,
                document_ids=tuple(document_ids),
            ),
            on_complete,
            on_cancel,
        )

    def request_document_replace(
        self,
        document_id: str,
        on_complete: Callable[[], None],
        on_cancel: Optional[Callable[[], None]] = None,
        *,
        owner_id: str = "",
    ) -> bool:
        """Resolve one dirty document before replacing its in-memory content."""
        identifier = str(document_id or "").strip()
        if not identifier or self.is_active:
            return False
        owner = str(owner_id or "").strip()
        if not owner:
            from Infernux.engine.interaction import DocumentRegistry

            document = DocumentRegistry.instance().get(identifier)
            if document is not None and document.view_ids:
                owner = sorted(document.view_ids)[0]
        return self._request(
            "replace",
            CloseIntent(
                CloseIntentKind.REPLACE_DOCUMENT,
                document_ids=(identifier,),
            ),
            on_complete,
            on_cancel,
            panel_id=owner,
        )

    def render(self, ctx) -> None:
        """Poll saves and render through the global modal portal."""
        if not self.is_active:
            return

        if self.waiting_for_conflict:
            self._close.poll()
            if not self.is_active or self.waiting_for_conflict:
                return
            self._show_popup = True

        self._present_active_document()

        # ImGui consumes a dock tab's close request before the panel can veto
        # it. Give WindowManager one modal-free frame to restore the source
        # tab, then open the shared confirmation from that visible panel on
        # the following frame. Opening the modal immediately leaves whatever
        # neighboring tab ImGui selected underneath it.
        if self._defer_panel_popup_once:
            self._defer_panel_popup_once = False
            self._show_popup = self.is_active
            return

        if self.waiting_for_save:
            self._close.poll()
            if not self.is_active:
                return
            if self.waiting_for_save:
                return
            if self._present_active_document():
                self._defer_panel_popup_once = True
                self._show_popup = False
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
        if not self._modals.activate(
            self.MODAL_ID,
            owner_id=panel_id,
        ):
            return False
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
        self._present_active_document(force=True)
        self._defer_panel_popup_once = self.is_active
        self._show_popup = self.is_active and not self._defer_panel_popup_once
        return True

    def _sync_presentation_after_decision(self) -> None:
        if not self.is_active:
            return
        changed = self._present_active_document()
        self._defer_panel_popup_once = changed
        self._show_popup = (
            self._close.state is CloseState.AWAITING_DECISION and not changed
        )

    @staticmethod
    def _preferred_document_view(document) -> str:
        if document is None:
            return ""
        owners = sorted(document.dirty_owner_view_ids())
        for view_id in owners:
            if view_id in document.view_ids:
                return view_id
        if document.kind.value == "scene" and "scene_view" in document.view_ids:
            return "scene_view"
        return sorted(document.view_ids)[0] if document.view_ids else ""

    def _present_active_document(self, *, force: bool = False) -> bool:
        document = self._close.active_document
        document_id = document.document_id if document is not None else ""
        if not force and document_id == self._presented_document_id:
            return False
        self._presented_document_id = document_id
        view_id = (
            self._panel_id
            if self._scope == "panel" and self._panel_id
            else self._preferred_document_view(document)
        )
        if not view_id:
            return bool(document_id)

        from Infernux.engine.interaction import FocusService
        from .window_manager import WindowManager

        FocusService.instance().request_panel_focus(view_id)
        manager = WindowManager.instance()
        if manager is not None and manager.is_window_open(view_id):
            manager.restore_close_confirmation_source(view_id)
        return True

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
        self._modals.deactivate(self.MODAL_ID)
        self._scope = ""
        self._panel_id = ""
        self._show_popup = False
        self._defer_panel_popup_once = False
        self._presented_document_id = ""
