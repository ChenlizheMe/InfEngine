"""One application-owned installation queue; workers never own Hub windows."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal


Progress = Callable[[str, int, int], None]


@dataclass
class InstallJob:
    key: str
    title: str
    operation: Callable[[Progress], object] = field(repr=False)
    state: str = "queued"
    message: str = ""
    completed: int = 0
    total: int = 0
    result: object = None
    error: str = ""


class _InstallThread(QThread):
    # Android downloads exceed Qt's signed 32-bit int signal payload.
    progress = Signal(str, object, object)

    def __init__(self, job: InstallJob, parent: QObject):
        super().__init__(parent)
        self.job = job
        self.result = None
        self.error = ""

    def run(self):
        try:
            self.result = self.job.operation(self.progress.emit)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"


class InstallQueue(QObject):
    changed = Signal()
    job_finished = Signal(object)
    idle = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs: list[InstallJob] = []
        self._pending: deque[InstallJob] = deque()
        self._thread: _InstallThread | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None or bool(self._pending)

    def submit(self, key: str, title: str, operation: Callable[[Progress], object]) -> InstallJob:
        for job in self.jobs:
            if job.key == key and job.state in {"queued", "running"}:
                return job
        job = InstallJob(key, title, operation)
        self.jobs.append(job)
        self._pending.append(job)
        self.changed.emit()
        QTimer.singleShot(0, self._start_next)
        return job

    def cancel_queued(self, job: InstallJob) -> None:
        if job.state != "queued":
            return
        self._pending.remove(job)
        job.state = "cancelled"
        self.changed.emit()

    def clear_finished(self) -> None:
        self.jobs[:] = [job for job in self.jobs if job.state in {"queued", "running"}]
        self.changed.emit()

    def is_pending(self, key: str) -> bool:
        return any(job.key == key and job.state in {"queued", "running"} for job in self.jobs)

    def _start_next(self):
        if self._thread is not None:
            return
        if not self._pending:
            self.idle.emit()
            return
        job = self._pending.popleft()
        job.state = "running"
        self._thread = _InstallThread(job, self)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished.connect(self._on_finished)
        self._thread.start()
        self.changed.emit()

    def _on_progress(self, message: str, completed: int, total: int):
        job = self._thread.job
        job.message, job.completed, job.total = message, completed, total
        self.changed.emit()

    def _on_finished(self):
        thread = self._thread
        thread.wait()
        self._thread = None
        job = thread.job
        job.result, job.error = thread.result, thread.error
        job.state = "failed" if job.error else "succeeded"
        thread.deleteLater()
        self.changed.emit()
        self.job_finished.emit(job)
        QTimer.singleShot(0, self._start_next)
