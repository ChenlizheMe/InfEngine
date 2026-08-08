"""ImGui presenter for the Interaction Core external-conflict service."""

from __future__ import annotations

from typing import Optional

from Infernux.engine.i18n import t
from Infernux.engine.interaction import (
    DocumentActionStatus,
    ExternalDocumentConflictService,
    ModalService,
)

from .editor_modal import (
    EditorModalAction,
    begin_editor_modal,
    end_editor_modal,
    render_editor_modal_actions,
)


class ExternalDocumentConflictCoordinator:
    """Present conflicts while all state transitions remain in Interaction Core."""

    _instance: Optional["ExternalDocumentConflictCoordinator"] = None
    MODAL_ID = "editor.external_document_conflict"
    _UNSAVED_MODAL_ID = "editor.unsaved_changes"

    def __init__(
        self,
        conflict_service=None,
        modal_service: Optional[ModalService] = None,
    ) -> None:
        # Keep the former (DocumentRegistry, ModalService) test construction
        # shape usable while routing it through the new core service.
        if conflict_service is not None and not isinstance(
            conflict_service,
            ExternalDocumentConflictService,
        ):
            conflict_service = ExternalDocumentConflictService(conflict_service)
        if conflict_service is None or modal_service is None:
            from Infernux.engine.interaction import EditorInteractionCore

            core = EditorInteractionCore.instance()
            if core is None:
                raise RuntimeError(
                    "ExternalDocumentConflictCoordinator requires EditorInteractionCore"
                )
            conflict_service = conflict_service or core.external_conflicts
            modal_service = modal_service or core.modals
        self._service = conflict_service
        self._modals = modal_service
        self._show_popup = False
        self._presented_document_id = ""
        self._modals.register(
            self.MODAL_ID,
            is_active=lambda: self.is_active,
            render=self.render,
            cancel=self._restore_cancelled_modal,
            allowed_parent_ids=(self._UNSAVED_MODAL_ID,),
        )

    @classmethod
    def instance(cls) -> "ExternalDocumentConflictCoordinator":
        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is None:
            raise RuntimeError(
                "ExternalDocumentConflictCoordinator requires EditorInteractionCore"
            )
        if (
            cls._instance is None
            or cls._instance._service is not core.external_conflicts
            or cls._instance._modals is not core.modals
        ):
            cls._instance = cls(core.external_conflicts, core.modals)
        return cls._instance

    @property
    def is_active(self) -> bool:
        return self._service.is_active

    @property
    def active_document_id(self) -> str:
        conflict = self._service.active
        return conflict.document_id if conflict is not None else ""

    @property
    def waiting_for_save_copy(self) -> bool:
        return self._service.waiting_for_save_copy

    def poll(self) -> None:
        previous_id = self.active_document_id
        self._service.poll()
        conflict = self._service.active
        if conflict is None:
            if previous_id:
                self._modals.deactivate(self.MODAL_ID)
            self._presented_document_id = ""
            self._show_popup = False
            return

        document = self._document()
        if document is None:
            return
        if conflict.document_id != self._presented_document_id:
            self._presented_document_id = conflict.document_id
            self._present_document(document)
            self._show_popup = True

        if any(
            entry.modal_id == self.MODAL_ID
            for entry in self._modals.active_stack
        ):
            return
        parent_id = (
            self._UNSAVED_MODAL_ID
            if self._modals.active_modal_id == self._UNSAVED_MODAL_ID
            else ""
        )
        if self._modals.active_modal_id and not parent_id:
            return
        if self._modals.activate(
            self.MODAL_ID,
            owner_id=self._preferred_view(document),
            parent_id=parent_id,
        ):
            self._show_popup = True

    def render(self, ctx) -> bool:
        conflict = self._service.active
        document = self._document()
        if conflict is None or document is None:
            return False
        popup_id = (
            f"{t('editor.external_conflict.title')}"
            "###editor_external_document_conflict"
        )
        request_open = self._show_popup
        self._show_popup = False
        if not begin_editor_modal(
            ctx,
            popup_id=popup_id,
            title=t("editor.external_conflict.title"),
            semantic_id="editor.external_conflict.dialog",
            request_open=request_open,
            height=250.0,
        ):
            # ImGui may retire the popup because focus/docking/play-mode
            # changed underneath us. Keep the domain conflict active, but ask
            # for a fresh popup next frame and release the invisible modal's
            # shortcut barrier through ModalService's presentation heartbeat.
            self._show_popup = True
            return False

        ctx.text_wrapped(
            t("editor.external_conflict.message").format(document=document.title)
        )
        ctx.text_wrapped(t("editor.external_conflict.question"))
        error = self._localized_error()
        if error:
            ctx.spacing()
            ctx.text_wrapped(error)
        render_editor_modal_actions(
            ctx,
            [
                EditorModalAction(
                    t("editor.external_conflict.reload"),
                    "reload",
                    lambda: self._reload(ctx),
                ),
                EditorModalAction(
                    t("editor.external_conflict.keep_local"),
                    "keep_local",
                    lambda: self._keep_local(ctx),
                ),
                EditorModalAction(
                    t("editor.external_conflict.save_copy"),
                    "save_copy",
                    lambda: self._save_copy(ctx),
                ),
            ],
            semantic_prefix="editor.external_conflict",
        )
        end_editor_modal(ctx)
        return True

    def choose_reload(self) -> bool:
        conflict = self._service.active
        if conflict is None:
            return False
        result = self._service.reload(conflict.conflict_id)
        return self._finish_choice(result)

    def choose_keep_local(self) -> bool:
        conflict = self._service.active
        if conflict is None:
            return False
        result = self._service.keep_local(conflict.conflict_id)
        return self._finish_choice(result)

    def choose_save_copy(self) -> bool:
        conflict = self._service.active
        if conflict is None:
            return False
        result = self._service.save_copy(conflict.conflict_id)
        return result.status in {
            DocumentActionStatus.PENDING,
            DocumentActionStatus.APPLIED,
            DocumentActionStatus.NO_OP,
        }

    def _finish_choice(self, result) -> bool:
        if result.status not in {
            DocumentActionStatus.APPLIED,
            DocumentActionStatus.NO_OP,
        }:
            self._show_popup = True
            return False
        self._modals.deactivate(self.MODAL_ID)
        return True

    def _reload(self, ctx) -> None:
        if self.choose_reload():
            ctx.close_current_popup()

    def _keep_local(self, ctx) -> None:
        if self.choose_keep_local():
            ctx.close_current_popup()

    def _save_copy(self, ctx) -> None:
        if self.choose_save_copy():
            ctx.close_current_popup()

    def _document(self):
        return self._service.active_document

    def _localized_error(self) -> str:
        error = self._service.error
        if not error:
            return ""
        if error == "save_copy_failed":
            return t("editor.external_conflict.save_copy_failed")
        return error

    @staticmethod
    def _preferred_view(document) -> str:
        owners = sorted(document.dirty_owner_view_ids())
        for view_id in owners:
            if view_id in document.view_ids:
                return view_id
        return sorted(document.view_ids)[0] if document.view_ids else ""

    def _present_document(self, document) -> None:
        view_id = self._preferred_view(document)
        if not view_id:
            return
        from Infernux.engine.interaction import FocusService
        from .window_manager import WindowManager

        FocusService.instance().request_panel_focus(view_id)
        manager = WindowManager.instance()
        if manager is not None and manager.is_window_open(view_id):
            manager.restore_close_confirmation_source(view_id)

    def _restore_cancelled_modal(self) -> None:
        if self.is_active:
            self._show_popup = True


__all__ = ["ExternalDocumentConflictCoordinator"]
