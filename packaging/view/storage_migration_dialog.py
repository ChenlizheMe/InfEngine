"""Modal Hub migration progress; disk copying never blocks the GUI thread."""

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout

from i18n import tr
from shared_storage_migration import migrate_legacy_storage


class _MigrationThread(QThread):
    progress = Signal(str)

    def __init__(self, plan, project_roots, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.project_roots = project_roots
        self.result = ()
        self.error = ""

    def run(self):
        try:
            self.result = migrate_legacy_storage(
                self.plan, self.project_roots, progress=self.progress.emit
            )
        except Exception as exc:
            self.error = str(exc)


class StorageMigrationDialog(QDialog):
    def __init__(self, plan, project_roots, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Migrate Legacy Resources"))
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        self.status = QLabel(tr("Moving resources. Keep Hub open until this finishes."))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(progress)
        self.worker = _MigrationThread(plan, project_roots, self)
        self.worker.progress.connect(self.status.setText)
        self.worker.finished.connect(self.accept)

    def exec(self):
        self.worker.start()
        return super().exec()

    def reject(self):
        if not self.worker.isRunning():
            super().reject()

    def closeEvent(self, event):
        if self.worker.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)
