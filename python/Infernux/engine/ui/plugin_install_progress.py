"""Visible progress for source and pip installation from the Plugins panel."""

from __future__ import annotations

import time
import threading
from queue import Empty, SimpleQueue
from dataclasses import dataclass, field
from typing import Callable, Optional

from Infernux.debug import Debug
from Infernux.engine.i18n import t

from .editor_modal import begin_editor_modal, end_editor_modal


@dataclass(slots=True)
class _PluginInstallTransaction:
    label: str
    work: Callable[[Callable[[str, float, str], None]], object]
    complete: Callable[[bool, object | None, str], None]
    phase: str = "opening"
    stage: str = "preparing"
    progress: float = 0.02
    detail: str = ""
    presented_phase: str = ""
    result: object | None = None
    history: list[str] = field(default_factory=list)
    completed_at: float = 0.0
    events: SimpleQueue = field(default_factory=SimpleQueue)
    worker: threading.Thread | None = None
    worker_done: threading.Event = field(default_factory=threading.Event)
    worker_error: BaseException | None = None


class PluginInstallProgressService:
    """Keep one modal visible from source acquisition through preload."""

    MODAL_ID = "editor.plugin_install_progress"
    COMPLETE_MIN_SECONDS = 0.35
    _instance: Optional["PluginInstallProgressService"] = None

    @classmethod
    def instance(cls) -> "PluginInstallProgressService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._transaction: Optional[_PluginInstallTransaction] = None
        self._registered_service = None

    @property
    def is_active(self) -> bool:
        return self._transaction is not None

    def _ensure_registered(self):
        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is None:
            raise RuntimeError("plugin install progress requires EditorInteractionCore")
        modals = core.modals
        if self._registered_service is modals:
            return modals
        modals.register(
            self.MODAL_ID,
            is_active=lambda: self.is_active,
            render=self.render,
            cancel=lambda: None,
        )
        self._registered_service = modals
        return modals

    def begin(
        self,
        *,
        label: str,
        work: Callable[[Callable[[str, float, str], None]], object],
        complete: Callable[[bool, object | None, str], None],
    ) -> bool:
        if self._transaction is not None:
            return False
        modals = self._ensure_registered()
        transaction = _PluginInstallTransaction(
            label=str(label),
            work=work,
            complete=complete,
            history=[t("plugins.install_progress.preparing")],
        )
        self._transaction = transaction
        if not modals.activate(self.MODAL_ID, owner_id="plugins"):
            self._transaction = None
            return False
        return True

    @staticmethod
    def _apply_report(
        transaction: _PluginInstallTransaction,
        stage: str,
        progress: float,
        detail: str,
    ) -> None:
        key = f"plugins.install_progress.{stage}"
        message = t(key)
        if message == key:
            message = str(stage).replace("_", " ")
        transaction.stage = str(stage)
        transaction.progress = max(0.02, min(1.0, float(progress)))
        transaction.detail = str(detail or "").strip()
        if not transaction.history or transaction.history[-1] != message:
            transaction.history.append(message)

    @staticmethod
    def _run_worker(transaction: _PluginInstallTransaction) -> None:
        def report(stage: str, progress: float, detail: str = "") -> None:
            transaction.events.put((str(stage), float(progress), str(detail or "")))

        try:
            transaction.result = transaction.work(report)
        except BaseException as exc:
            transaction.worker_error = exc
        finally:
            transaction.worker_done.set()

    @staticmethod
    def _drain_reports(transaction: _PluginInstallTransaction) -> None:
        while True:
            try:
                stage, progress, detail = transaction.events.get_nowait()
            except Empty:
                return
            PluginInstallProgressService._apply_report(
                transaction,
                stage,
                progress,
                detail,
            )

    def post_present_tick(self) -> None:
        transaction = self._transaction
        if transaction is None or transaction.presented_phase != transaction.phase:
            return
        try:
            if transaction.phase == "opening":
                transaction.phase = "running"
                transaction.stage = "resolve_source"
                transaction.progress = 0.04
                transaction.worker = threading.Thread(
                    target=self._run_worker,
                    args=(transaction,),
                    name="InfernuxPluginInstall",
                    daemon=True,
                )
                transaction.worker.start()
                return
            if transaction.phase == "running":
                self._drain_reports(transaction)
                if not transaction.worker_done.is_set():
                    return
                if transaction.worker is not None:
                    transaction.worker.join()
                self._drain_reports(transaction)
                if transaction.worker_error is not None:
                    raise transaction.worker_error
                transaction.phase = "complete"
                transaction.stage = "complete"
                transaction.progress = 1.0
                transaction.detail = ""
                transaction.completed_at = time.monotonic()
                if transaction.history[-1] != t("plugins.install_progress.complete"):
                    transaction.history.append(t("plugins.install_progress.complete"))
                return
            if (
                transaction.phase == "complete"
                and time.monotonic() - transaction.completed_at
                >= self.COMPLETE_MIN_SECONDS
            ):
                self._finish(True, "")
        except Exception as exc:
            Debug.log_error(f"Plugin installation failed: {exc}")
            self._finish(False, f"{type(exc).__name__}: {exc}")

    def _finish(self, success: bool, message: str) -> None:
        transaction = self._transaction
        if transaction is None:
            return
        self._transaction = None
        try:
            transaction.complete(success, transaction.result, str(message or ""))
        finally:
            if self._registered_service is not None:
                self._registered_service.deactivate(self.MODAL_ID)

    def render(self, ctx) -> bool:
        transaction = self._transaction
        if transaction is None:
            return False
        title = t("plugins.install_progress.title")
        if not begin_editor_modal(
            ctx,
            popup_id=f"{title}##plugin_install_progress",
            title=title,
            semantic_id=self.MODAL_ID,
            request_open=not transaction.presented_phase,
            width=560.0,
            height=245.0,
        ):
            return False
        current_key = f"plugins.install_progress.{transaction.stage}"
        current = t(current_key)
        if current == current_key:
            current = transaction.stage.replace("_", " ")
        ctx.text_wrapped(current)
        ctx.spacing()
        ctx.progress_bar(float(transaction.progress), -1.0, 22.0, "")
        if transaction.detail:
            ctx.spacing()
            ctx.text_wrapped(transaction.detail)
        ctx.spacing()
        ctx.text_wrapped(transaction.label)
        ctx.spacing()
        for message in transaction.history[-4:]:
            ctx.text_wrapped(f"• {message}")
        end_editor_modal(ctx)
        transaction.presented_phase = transaction.phase
        return True


__all__ = ["PluginInstallProgressService"]
