"""Small update prompt and progress window for Infernux Hub."""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox, QProgressBar, QVBoxLayout

from hub_updater import check_for_update, launch_external_updater, stage_update
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
            update = check_for_update()
            _write_update_trace(
                {
                    "status": "update_available" if update is not None else "up_to_date",
                    "current_version": update.current_version if update is not None else "",
                    "target_version": update.target_version if update is not None else "",
                    "asset_name": update.asset_name if update is not None else "",
                    "required": bool(update.required) if update is not None else False,
                }
            )
            self.finished.emit(update)
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
        self.status = QLabel(tr("Downloading and verifying update..."))
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
        self.status.setText(tr("Closing Hub and installing verified files..."))
        try:
            launch_external_updater(staged_root)
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

    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.thread = None
        self.worker = None
        self._silent_check = True

    def check(self, *, silent: bool = True):
        if self.thread and self.thread.isRunning():
            return
        self._silent_check = silent
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

    def _checked(self, update):
        if update is None:
            if not self._silent_check:
                QMessageBox.information(
                    self.main_window, tr("Hub Update"), tr("Infernux Hub is up to date."),
                )
            return
        if update.required:
            QMessageBox.information(
                self.main_window,
                tr("Hub Update Required"),
                tr(
                    "Infernux Hub must be updated to {version} before this version of "
                    "the engine can be installed.\n\nThe verified update will be "
                    "downloaded now. Hub will then restart automatically.",
                    version=update.target_version,
                ),
            )
        else:
            answer = QMessageBox.question(
                self.main_window,
                tr("Hub Update Available"),
                tr(
                    "Infernux Hub {version} is available. Update now?\n\n"
                    "Hub will close, install the verified update, and restart automatically.",
                    version=update.target_version,
                ),
            )
            if answer != QMessageBox.Yes:
                return
        dialog = UpdateProgressDialog(update, self.main_window)
        result = dialog.exec()
        # A required update removes the user's decline path, but a failed
        # download must leave Hub open so the user can retry. Engine/runtime
        # installation remains independently gated by VersionManager.
        if result == QDialog.Accepted:
            self.main_window.hide()
            self.main_window.app.quit()

    def _check_failed(self, message: str):
        if not self._silent_check:
            QMessageBox.warning(self.main_window, tr("Update Check Failed"), message)


__all__ = ["UpdateController", "UpdateProgressDialog"]
