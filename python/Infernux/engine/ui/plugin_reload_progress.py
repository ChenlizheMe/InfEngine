"""Visible, frame-stepped progress for explicit plugin reloads."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from Infernux.debug import Debug
from Infernux.engine.i18n import t

from .editor_modal import (
    EditorModalAction,
    begin_editor_modal,
    end_editor_modal,
    render_editor_modal_actions,
)


@dataclass(slots=True)
class _PluginReloadTransaction:
    manager: object
    references: tuple[str, ...]
    complete: Callable[[bool, tuple[object, ...], str], None]
    phase: str = "opening"
    message: str = ""
    progress: float = 0.02
    presented_phase: str = ""
    index: int = 0
    states: list[object] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    phase_started_at: float = field(default_factory=time.monotonic)
    last_step_at: float = field(default_factory=time.monotonic)


class PluginReloadProgressService:
    """Reload installed plugins between presented frames.

    A preload entry can perform meaningful startup work.  Running it directly
    inside an ImGui button callback makes the Editor appear frozen and prevents
    the user from ever seeing progress.  This service first presents a modal,
    then reloads one plugin per post-present tick.
    """

    MODAL_ID = "editor.plugin_reload_progress"
    OPENING_MIN_SECONDS = 0.16
    STEP_MIN_SECONDS = 0.10
    COMPLETE_MIN_SECONDS = 0.30
    _instance: Optional["PluginReloadProgressService"] = None

    @classmethod
    def instance(cls) -> "PluginReloadProgressService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._transaction: Optional[_PluginReloadTransaction] = None
        self._registered_service = None

    @property
    def is_active(self) -> bool:
        return self._transaction is not None

    def _ensure_registered(self):
        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is None:
            raise RuntimeError("plugin reload progress requires EditorInteractionCore")
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
        manager,
        references: tuple[str, ...],
        complete: Callable[[bool, tuple[object, ...], str], None],
    ) -> bool:
        if self._transaction is not None or not references:
            return False
        modals = self._ensure_registered()
        self._transaction = _PluginReloadTransaction(
            manager=manager,
            references=tuple(str(item) for item in references),
            complete=complete,
            message=t("plugins.reload_progress.preparing"),
        )
        if not modals.activate(self.MODAL_ID, owner_id="plugins"):
            self._transaction = None
            return False
        return True

    def cancel(self) -> None:
        transaction = self._transaction
        if transaction is not None:
            transaction.cancelled = True

    def post_present_tick(self) -> None:
        transaction = self._transaction
        if transaction is None or transaction.presented_phase != transaction.phase:
            return
        now = time.monotonic()
        if transaction.cancelled:
            self._finish(False, t("plugins.reload_progress.cancelled"))
            return
        try:
            if transaction.phase == "opening":
                if now - transaction.phase_started_at < self.OPENING_MIN_SECONDS:
                    return
                transaction.phase = "reloading"
                transaction.progress = 0.08
                transaction.message = t("plugins.reload_progress.starting")
                transaction.phase_started_at = now
                transaction.last_step_at = now
                return
            if transaction.phase == "reloading":
                if now - transaction.last_step_at < self.STEP_MIN_SECONDS:
                    return
                reference = transaction.references[transaction.index]
                transaction.message = t("plugins.reload_progress.current").format(
                    reference=reference,
                    current=transaction.index + 1,
                    total=len(transaction.references),
                )
                state = transaction.manager.reload(reference)
                transaction.states.append(state)
                error = str(getattr(state, "error", "") or "")
                if error:
                    transaction.errors.append(f"{reference}: {error}")
                transaction.index += 1
                transaction.last_step_at = now
                transaction.progress = transaction.index / len(transaction.references)
                if transaction.index >= len(transaction.references):
                    transaction.phase = "complete"
                    transaction.phase_started_at = now
                    transaction.message = (
                        t("plugins.reload_progress.failed").format(count=len(transaction.errors))
                        if transaction.errors
                        else t("plugins.reload_progress.complete")
                    )
                return
            if transaction.phase == "complete":
                if now - transaction.phase_started_at < self.COMPLETE_MIN_SECONDS:
                    return
                self._finish(not transaction.errors, "\n".join(transaction.errors))
        except Exception as exc:
            Debug.log_error(f"Plugin reload failed: {exc}")
            self._finish(False, f"{type(exc).__name__}: {exc}")

    def _finish(self, success: bool, message: str) -> None:
        transaction = self._transaction
        if transaction is None:
            return
        self._transaction = None
        try:
            transaction.complete(bool(success), tuple(transaction.states), str(message or ""))
        finally:
            if self._registered_service is not None:
                self._registered_service.deactivate(self.MODAL_ID)

    def render(self, ctx) -> bool:
        transaction = self._transaction
        if transaction is None:
            return False
        title = t("plugins.reload_progress.title")
        if not begin_editor_modal(
            ctx,
            popup_id=f"{title}##plugin_reload_progress",
            title=title,
            semantic_id=self.MODAL_ID,
            request_open=not transaction.presented_phase,
            width=540.0,
            height=205.0,
        ):
            return False
        ctx.text_wrapped(transaction.message)
        ctx.spacing()
        overlay = f"{transaction.index} / {len(transaction.references)}"
        ctx.progress_bar(float(transaction.progress), -1.0, 22.0, overlay)
        ctx.spacing()
        ctx.text_wrapped(t("plugins.reload_progress.locked"))
        render_editor_modal_actions(
            ctx,
            (
                EditorModalAction(
                    t("plugins.reload_progress.cancel"),
                    "plugin_reload_progress.cancel",
                    self.cancel,
                    enabled=transaction.phase != "complete",
                ),
            ),
            semantic_prefix=self.MODAL_ID,
        )
        end_editor_modal(ctx)
        transaction.presented_phase = transaction.phase
        return True


__all__ = ["PluginReloadProgressService"]
