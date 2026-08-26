"""Modal, serial asset import transactions for explicit Inspector Apply."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from Infernux.debug import Debug
from Infernux.engine.i18n import t

from .editor_modal import begin_editor_modal, end_editor_modal


@dataclass(slots=True)
class _ImportTransaction:
    title: str
    path: str
    work: Callable[[], bool]
    is_published: Callable[[], bool]
    complete: Callable[[bool, str], None]
    owner_id: str = "asset_inspector"
    preparing_message: str = ""
    processing_message: str = ""
    publishing_message: str = ""
    complete_message: str = ""
    phase: str = "opening"
    progress: float = 0.05
    message: str = ""
    presented_phase: str = ""


class AssetImportProgressService:
    """Own one editor-blocking import until its GPU publication is visible.

    Expensive source processing remains serial and deterministic from the
    user's point of view. The first modal frame is presented before the work
    begins, then the final resource becomes available atomically before the
    modal is released.
    """

    MODAL_ID = "editor.asset_import_progress"
    _instance: Optional["AssetImportProgressService"] = None

    @classmethod
    def instance(cls) -> "AssetImportProgressService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._transaction: Optional[_ImportTransaction] = None
        self._registered_service = None

    @property
    def is_active(self) -> bool:
        return self._transaction is not None

    def _ensure_registered(self):
        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is None:
            raise RuntimeError("asset import progress requires EditorInteractionCore")
        modals = core.modals
        if self._registered_service is modals:
            return modals
        modals.register(
            self.MODAL_ID,
            is_active=lambda: self.is_active,
            render=self.render,
            cancel=lambda: None,
            allowed_parent_ids={
                "editor.unsaved_changes",
                "editor.external_document_conflict",
            },
        )
        self._registered_service = modals
        return modals

    def begin(
        self,
        *,
        title: str,
        path: str,
        work: Callable[[], bool],
        is_published: Callable[[], bool],
        complete: Callable[[bool, str], None],
        owner_id: str = "asset_inspector",
        preparing_message: str = "",
        processing_message: str = "",
        publishing_message: str = "",
        complete_message: str = "",
    ) -> bool:
        if self._transaction is not None:
            return False
        modals = self._ensure_registered()
        parent = modals.active_modal_id
        self._transaction = _ImportTransaction(
            title=str(title),
            path=str(path),
            work=work,
            is_published=is_published,
            complete=complete,
            owner_id=str(owner_id or "asset_inspector"),
            preparing_message=str(
                preparing_message or t("asset.import_progress.preparing")
            ),
            processing_message=str(
                processing_message or t("asset.import_progress.processing")
            ),
            publishing_message=str(
                publishing_message or t("asset.import_progress.publishing")
            ),
            complete_message=str(
                complete_message or t("asset.import_progress.complete")
            ),
        )
        self._transaction.message = self._transaction.preparing_message
        if not modals.activate(
            self.MODAL_ID,
            owner_id=self._transaction.owner_id,
            parent_id=parent,
        ):
            self._transaction = None
            return False
        return True

    def post_present_tick(self) -> None:
        """Advance only a phase that was visible in the presented frame.

        This is called from Engine's post-draw callback, after GPU submit and
        presentation.  Keeping it out of ImGui rendering prevents a blocking
        importer from running before the progress modal reaches the screen.
        """
        transaction = self._transaction
        if transaction is None or transaction.presented_phase != transaction.phase:
            return

        modals = self._ensure_registered()
        if self.MODAL_ID not in {entry.modal_id for entry in modals.active_stack}:
            parent = modals.active_modal_id
            modals.activate(
                self.MODAL_ID,
                owner_id=transaction.owner_id,
                parent_id=parent,
            )

        try:
            if transaction.phase == "opening":
                transaction.phase = "importing"
                transaction.progress = 0.2
                transaction.message = transaction.processing_message
                # Present the processing state before entering blocking work.
                return

            if transaction.phase == "importing":
                if not transaction.work():
                    self._finish(False, "asset importer rejected the new settings")
                    return
                transaction.phase = "publishing"
                transaction.progress = 0.82
                transaction.message = transaction.publishing_message

            if transaction.phase == "publishing" and transaction.is_published():
                transaction.phase = "complete"
                transaction.progress = 1.0
                transaction.message = transaction.complete_message
                return

            if transaction.phase == "complete":
                self._finish(True, "")
        except Exception as exc:
            Debug.log_error(f"Asset import failed for '{transaction.path}': {exc}")
            self._finish(False, str(exc))

    def _finish(self, success: bool, message: str) -> None:
        transaction = self._transaction
        if transaction is None:
            return
        self._transaction = None
        try:
            transaction.complete(bool(success), str(message or ""))
        finally:
            if self._registered_service is not None:
                self._registered_service.deactivate(self.MODAL_ID)

    def render(self, ctx) -> bool:
        transaction = self._transaction
        if transaction is None:
            return False
        title = transaction.title or t("asset.import_progress.title")
        if not begin_editor_modal(
            ctx,
            popup_id=f"{title}##asset_import_progress",
            title=title,
            semantic_id=self.MODAL_ID,
            request_open=not transaction.presented_phase,
            width=520.0,
            height=170.0,
        ):
            return False
        ctx.text_wrapped(transaction.message)
        ctx.spacing()
        ctx.progress_bar(float(transaction.progress), -1.0, 22.0, "")
        ctx.spacing()
        ctx.text_wrapped(transaction.path)
        end_editor_modal(ctx)
        transaction.presented_phase = transaction.phase
        return True
