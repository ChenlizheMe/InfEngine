"""Small update prompt and progress window for Infernux Hub."""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QProgressBar, QVBoxLayout

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


class _CheckWorker(QObject):
    finished = Signal(object)
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
            self.finished.emit(result)
        except Exception as exc:
            _write_update_trace({"status": "failed", "error": str(exc)})
            self.failed.emit(str(exc))


class _DownloadWorker(QObject):
    progress = Signal(int)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, update):
        super().__init__()
        self.update = update

    def run(self):
        try:
            def report(received, total):
                self.progress.emit(int(received * 100 / total) if total else 0)
            self.finished.emit(str(stage_update(self.update, report)))
        except Exception as exc:
            self.failed.emit(str(exc))


class UpdateProgressDialog(QDialog):
    def __init__(self, update, parent=None):
        super().__init__(parent)
        self.update = update
        self.setWindowTitle(tr("Updating Infernux Hub"))
        self.setFixedSize(480, 170)
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        title = QLabel(tr("INSTALLING HUB UPDATE {version}", version=update.target_version))
        title.setObjectName("settingsLabel")
        layout.addWidget(title)
        self.status = QLabel(tr("Downloading the Hub update..."))
        self.status.setObjectName("settingsDescription")
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)

        self.thread = QThread(self)
        self.worker = _DownloadWorker(update)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.finished.connect(self._ready)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.start()

    def reject(self):
        if self.thread.isRunning():
            return
        super().reject()

    def _ready(self, staged_root: str):
        self.thread.quit()
        self.thread.wait(2000)
        self.progress.setValue(100)
        self.status.setText(tr("Closing Hub and installing the update..."))
        try:
            application = QApplication.instance()
            launch_external_updater(
                staged_root,
                is_dark=bool(getattr(application, "is_dark_theme", True)),
            )
        except Exception as exc:
            self._failed(str(exc))
            return
        self.accept()

    def _failed(self, message: str):
        self.thread.quit()
        self.thread.wait(2000)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status.setText(tr("Update failed"))
        QMessageBox.critical(self, tr("Hub Update Failed"), message)
        super().reject()


class UpdateController(QObject):
    """Own worker lifetime and present an update without blocking Hub startup."""

    check_finished = Signal()

    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.thread = None
        self.worker = None
        self._silent_check = True
        self._completion_pending = False

    def check(self, *, silent: bool = True):
        if self.thread and self.thread.isRunning():
            return
        self._silent_check = silent
        self._completion_pending = True
        self.thread = QThread(self)
        self.worker = _CheckWorker()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        # Connect to QObject-bound slots, not lambdas.  Lambdas have no Qt
        # receiver affinity and therefore run in the worker thread, which
        # caused QMessageBox to create children for the main window across
        # threads and left the Hub stuck after a manual check.
        self.worker.finished.connect(self._checked)
        self.worker.failed.connect(self._check_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
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
        dialog = UpdateProgressDialog(update, self.main_window)
        result = dialog.exec()
        if result == QDialog.Accepted:
            self.main_window.hide()
            self.main_window.app.quit()
            return
        self._finish_check()

    def _check_failed(self, message: str):
        if not self._silent_check:
            QMessageBox.warning(self.main_window, tr("Update Check Failed"), message)
        self._finish_check()

    def _finish_check(self) -> None:
        if not self._completion_pending:
            return
        self._completion_pending = False
        self.check_finished.emit()


__all__ = ["UpdateController", "UpdateProgressDialog"]
