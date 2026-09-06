"""Update approval and shared-queue staging for Infernux Hub."""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from hub_updater import HubUpdateStatus, check_for_update, launch_external_updater, stage_update
from i18n import tr


def _write_update_trace(payload: dict) -> None:
    """Write an opt-in packaged-update diagnostic for release verification."""
    destination = os.environ.get("INFERNUX_HUB_UPDATE_TRACE", "").strip()
    if not destination:
        return
    try:
        path = Path(destination).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


class _CheckWorker(QThread):
    checked = Signal(object)
    failed = Signal(str)

    def run(self):
        try:
            result = check_for_update()
            update = result.update
            _write_update_trace(
                {
                    "status": result.status.value,
                    "current_version": result.current_version,
                    "target_version": result.latest_version,
                    "asset_name": update.asset_name if update is not None else "",
                    "detail": result.detail,
                }
            )
            self.checked.emit(result)
        except Exception as exc:
            _write_update_trace({"status": "failed", "error": str(exc)})
            self.failed.emit(str(exc))


class UpdateController(QObject):
    """Own worker lifetime and present an update without blocking Hub startup."""

    check_finished = Signal()

    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.queue = main_window.install_queue
        self._update_job = None
        self.queue.idle.connect(self._apply_staged_update)
        self.thread = None
        self._silent_check = True
        self._completion_pending = False
        QApplication.instance().aboutToQuit.connect(self._wait_for_check)

    def _wait_for_check(self):
        if self.thread is not None:
            self.thread.wait()

    def check(self, *, silent: bool = True):
        if self.thread and self.thread.isRunning():
            return
        self._silent_check = silent
        self._completion_pending = True
        if self.thread is not None:
            self.thread.deleteLater()
        self.thread = _CheckWorker(self)
        # Connect to QObject-bound slots, not lambdas.  Lambdas have no Qt
        # receiver affinity and therefore run in the worker thread, which
        # caused QMessageBox to create children for the main window across
        # threads and left the Hub stuck after a manual check.
        self.thread.checked.connect(self._checked)
        self.thread.failed.connect(self._check_failed)
        self.thread.start()

    def _checked(self, result):
        if result.status is HubUpdateStatus.UP_TO_DATE:
            if not self._silent_check:
                QMessageBox.information(
                    self.main_window, tr("Hub Update"), tr("Infernux Hub is up to date."),
                )
            self._finish_check()
            return
        if result.status is HubUpdateStatus.NETWORK_UNAVAILABLE:
            if not self._silent_check:
                QMessageBox.warning(
                    self.main_window,
                    tr("Update Check Unavailable"),
                    tr("The Hub update catalog could not be reached.\n\n{message}", message=result.detail),
                )
            self._finish_check()
            return
        if result.status is HubUpdateStatus.CATALOG_INVALID:
            if not self._silent_check:
                QMessageBox.warning(
                    self.main_window,
                    tr("Update Catalog Invalid"),
                    tr("The Hub update catalog is invalid.\n\n{message}", message=result.detail),
                )
            self._finish_check()
            return
        if result.status is HubUpdateStatus.UNSUPPORTED_CURRENT_VERSION:
            answer = QMessageBox.question(
                self.main_window,
                tr("Full Hub Install Required"),
                tr(
                    "This Hub is too old for the current in-app update path. "
                    "Open the {version} installer download now?",
                    version=result.latest_version,
                ),
            )
            if answer == QMessageBox.Yes:
                QDesktopServices.openUrl(QUrl(result.installer_url))
            self._finish_check()
            return
        update = result.update
        if result.status is not HubUpdateStatus.UPDATE_AVAILABLE or update is None:
            raise RuntimeError(f"Unhandled Hub update status: {result.status}")
        answer = QMessageBox.question(
            self.main_window,
            tr("Hub Update Available"),
            tr(
                "Infernux Hub {version} is available. Update now?\n\n"
                "Hub will close, install the update, and restart automatically.",
                version=update.target_version,
            ),
        )
        if answer != QMessageBox.Yes:
            self._finish_check()
            return
        self._update_job = self.queue.submit(
            f"hub-update:{update.target_version}",
            tr("Hub update {version}", version=update.target_version),
            lambda report: str(stage_update(
                update, lambda done, total: report(tr("Downloading"), done, total),
            )),
        )
        self._finish_check()

    def _apply_staged_update(self):
        job = self._update_job
        if job is None or job.state != "succeeded":
            return
        self._update_job = None
        try:
            application = QApplication.instance()
            launch_external_updater(
                job.result, is_dark=bool(getattr(application, "is_dark_theme", True)),
            )
        except Exception as exc:
            job.state, job.error = "failed", str(exc)
            self.queue.changed.emit()
            return
        self.main_window.hide()
        self.main_window.app.quit()

    def _check_failed(self, message: str):
        if not self._silent_check:
            QMessageBox.warning(self.main_window, tr("Update Check Failed"), message)
        self._finish_check()

    def _finish_check(self) -> None:
        if not self._completion_pending:
            return
        self._completion_pending = False
        self.check_finished.emit()


__all__ = ["UpdateController"]
