"""Visible, modal Player-build asset catalog preparation.

The native asset database can scan source files on worker threads.  A Player
build must wait for that scan to publish its immutable AssetIndex, but doing a
blocking ``refresh()`` directly from the Build Settings click callback makes
the editor look dead.  This service deliberately presents a global modal
first, then advances the scan between rendered frames.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from Infernux.debug import Debug

from .editor_modal import (
    EditorModalAction,
    begin_editor_modal,
    end_editor_modal,
    render_editor_modal_actions,
)


@dataclass(slots=True)
class _BuildPreflightTransaction:
    begin_scan: Callable[[], object]
    poll_scan: Callable[[object], object | None]
    complete: Callable[[bool, object, str], None]
    phase: str = "opening"
    message: str = "Preparing Player build..."
    progress: float = 0.02
    presented_phase: str = ""
    cancelled: bool = False
    result: object = None


class BuildPreflightProgressService:
    """Own the editor-blocking catalog transaction before a Player build."""

    MODAL_ID = "editor.build_preflight"
    _instance: Optional["BuildPreflightProgressService"] = None

    @classmethod
    def instance(cls) -> "BuildPreflightProgressService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._transaction: Optional[_BuildPreflightTransaction] = None
        self._registered_service = None

    @property
    def is_active(self) -> bool:
        return self._transaction is not None

    def _ensure_registered(self):
        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is None:
            raise RuntimeError("build preflight requires EditorInteractionCore")
        modals = core.modals
        if self._registered_service is modals:
            return modals
        modals.register(
            self.MODAL_ID,
            is_active=lambda: self.is_active,
            render=self.render,
            cancel=self.cancel,
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
        begin_scan: Callable[[], object],
        poll_scan: Callable[[object], object | None],
        complete: Callable[[bool, object, str], None],
    ) -> bool:
        if self._transaction is not None:
            return False
        modals = self._ensure_registered()
        self._transaction = _BuildPreflightTransaction(
            begin_scan=begin_scan,
            poll_scan=poll_scan,
            complete=complete,
        )
        if not modals.activate(self.MODAL_ID, owner_id="build_settings"):
            self._transaction = None
            return False
        return True

    def cancel(self) -> None:
        transaction = self._transaction
        if transaction is not None:
            transaction.cancelled = True

    def post_present_tick(self) -> None:
        """Advance only a phase that was visibly presented to the user."""
        transaction = self._transaction
        if transaction is None or transaction.presented_phase != transaction.phase:
            return
        if transaction.cancelled:
            self._finish(False, None, "Build preparation cancelled")
            return
        try:
            if transaction.phase == "opening":
                transaction.phase = "starting_scan"
                transaction.message = "Publishing pending asset changes..."
                transaction.progress = 0.12
                return
            if transaction.phase == "starting_scan":
                transaction.result = transaction.begin_scan()
                transaction.phase = "scanning"
                transaction.message = "Scanning project resources..."
                transaction.progress = 0.28
                return
            if transaction.phase == "scanning":
                result = transaction.poll_scan(transaction.result)
                if result is None:
                    stage = ""
                    if isinstance(transaction.result, dict):
                        stage = str(transaction.result.get("stage", "") or "")
                    if stage == "writes":
                        transaction.message = "Saving pending asset changes..."
                    elif stage == "index":
                        transaction.message = "Publishing the Player asset catalog..."
                    else:
                        transaction.message = "Scanning and importing project resources..."
                    transaction.progress = min(0.88, transaction.progress + 0.035)
                    return
                transaction.result = result
                transaction.phase = "complete"
                transaction.message = "Asset catalog is ready. Starting build..."
                transaction.progress = 1.0
                return
            if transaction.phase == "complete":
                self._finish(True, transaction.result, "")
        except Exception as exc:
            Debug.log_error(f"Player build preparation failed: {exc}")
            self._finish(False, None, str(exc))

    def _finish(self, ok: bool, result: object, message: str) -> None:
        transaction = self._transaction
        if transaction is None:
            return
        self._transaction = None
        try:
            transaction.complete(bool(ok), result, str(message or ""))
        finally:
            if self._registered_service is not None:
                self._registered_service.deactivate(self.MODAL_ID)

    def render(self, ctx) -> bool:
        transaction = self._transaction
        if transaction is None:
            return False
        if not begin_editor_modal(
            ctx,
            popup_id="Preparing Player Build##build_preflight_progress",
            title="Preparing Player Build",
            semantic_id=self.MODAL_ID,
            request_open=not transaction.presented_phase,
            width=560.0,
            height=205.0,
        ):
            return False
        ctx.text_wrapped(transaction.message)
        ctx.spacing()
        ctx.progress_bar(float(transaction.progress), -1.0, 22.0, "")
        ctx.spacing()
        ctx.text_wrapped("The editor is temporarily locked while the asset catalog is made consistent.")
        render_editor_modal_actions(
            ctx,
            (
                EditorModalAction(
                    "Cancel",
                    "build_preflight.cancel",
                    self.cancel,
                    enabled=transaction.phase not in {"complete"},
                ),
            ),
            semantic_prefix=self.MODAL_ID,
        )
        end_editor_modal(ctx)
        transaction.presented_phase = transaction.phase
        return True


__all__ = ["BuildPreflightProgressService"]
