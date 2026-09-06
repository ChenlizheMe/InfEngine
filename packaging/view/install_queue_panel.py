"""Compact non-modal download/install activity, shared by every Hub page."""
from PySide6.QtCore import Qt, Signal, QTimer, QEvent
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget, QSizePolicy

from i18n import tr
from view.installs_view import _configure_install_scroll_area


class InstallQueuePanel(QFrame):
    layout_changed = Signal()

    def __init__(self, queue, parent):
        super().__init__(parent)
        self.setObjectName("installQueuePanel")
        self._queue = queue
        self._expanded = False
        self._rows = {}
        self._leave_timer = QTimer(self)
        self._leave_timer.setSingleShot(True)
        self._leave_timer.setInterval(150)
        self._leave_timer.timeout.connect(self._collapse)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(4)
        self._details = QFrame(parent)
        self._details.setObjectName("installQueuePopup")
        self._details.installEventFilter(self)
        details_layout = QVBoxLayout(self._details)
        details_layout.setContentsMargins(10, 8, 10, 8)
        header = QHBoxLayout()
        header.addWidget(QLabel(tr("Downloads and installs")), 1)
        clear = QPushButton(tr("Clear finished"))
        clear.setObjectName("normalBtn")
        clear.setMinimumHeight(28)
        clear.clicked.connect(queue.clear_finished)
        header.addWidget(clear)
        details_layout.addLayout(header)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        content = QWidget()
        _configure_install_scroll_area(self._scroll, content)
        self._items = QVBoxLayout(content)
        self._items.setContentsMargins(0, 0, 0, 0)
        self._items.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(content)
        details_layout.addWidget(self._scroll)
        self._bar = QWidget()
        self._bar.setFixedHeight(28)
        bar_layout = QVBoxLayout(self._bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(4)
        self._summary = QLabel()
        self._summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bar_layout.addWidget(self._summary)
        self._progress = QProgressBar()
        self._progress.setFixedHeight(3)
        self._progress.setTextVisible(False)
        bar_layout.addWidget(self._progress)
        layout.addWidget(self._bar)
        queue.changed.connect(self.refresh)
        self.refresh()

    def enterEvent(self, event):
        self._leave_timer.stop()
        self._expanded = True
        self.refresh()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._leave_timer.start()
        super().leaveEvent(event)

    def _collapse(self):
        cursor = QCursor.pos()
        if self.rect().contains(self.mapFromGlobal(cursor)) or (
            self._details.isVisible() and self._details.rect().contains(self._details.mapFromGlobal(cursor))
        ):
            return
        self._expanded = False
        self.refresh()

    def eventFilter(self, watched, event):
        if watched is self._details:
            if event.type() == QEvent.Type.Enter:
                self._leave_timer.stop()
            elif event.type() == QEvent.Type.Leave:
                self._leave_timer.start()
        return super().eventFilter(watched, event)

    def position_details(self):
        self._details.setFixedWidth(self.width())
        self._details.move(self.x(), max(0, self.y() - self._details.height() - 6))
        self._details.raise_()

    def hideEvent(self, event):
        self._expanded = False
        self._details.hide()
        super().hideEvent(event)

    def refresh(self):
        jobs = self._queue.jobs
        current = {id(job) for job in jobs}
        for key in tuple(self._rows):
            if key not in current:
                row = self._rows.pop(key)[0]
                self._items.removeWidget(row)
                row.deleteLater()
        labels = {
            "queued": tr("Queued"), "running": tr("Installing"),
            "succeeded": tr("Completed"), "failed": tr("Failed"),
            "cancelled": tr("Cancelled"),
        }
        for job in jobs:
            key = id(job)
            if key not in self._rows:
                row = QFrame()
                row_layout = QVBoxLayout(row)
                line = QHBoxLayout()
                title = QLabel(job.title)
                title.setObjectName("cardName")
                line.addWidget(title, 1)
                cancel = QPushButton(tr("Cancel"))
                cancel.setObjectName("normalBtn")
                cancel.clicked.connect(lambda _checked=False, item=job: self._queue.cancel_queued(item))
                line.addWidget(cancel)
                row_layout.addLayout(line)
                status = QLabel()
                status.setWordWrap(True)
                status.setObjectName("cardPath")
                row_layout.addWidget(status)
                progress = QProgressBar()
                progress.setFixedHeight(6)
                progress.setTextVisible(False)
                row_layout.addWidget(progress)
                self._items.addWidget(row)
                self._rows[key] = row, status, progress, cancel
            _row, status, progress, cancel = self._rows[key]
            detail = job.error if job.state == "failed" else job.message if job.state == "running" else ""
            status.setText(labels[job.state] + (f" — {detail}" if detail else ""))
            cancel.setVisible(job.state == "queued")
            progress.setVisible(job.state == "running")
            progress.setRange(0, 100 if job.total else 0)
            if job.total:
                progress.setValue(int(job.completed * 100 / job.total))
        active = next((job for job in jobs if job.state == "running"), None)
        queued = sum(job.state == "queued" for job in jobs)
        failed = sum(job.state == "failed" for job in jobs)
        summary = active.title if active else tr("Downloads and installs")
        if active and active.total:
            summary += f" · {int(active.completed * 100 / active.total)}%"
        if queued:
            summary += " · " + tr("{count} queued", count=queued)
        elif not active:
            summary = tr("{count} failed", count=failed) if failed else tr("Installations complete")
        self._summary.setText(summary)
        self._summary.setToolTip(summary)
        self._progress.setVisible(active is not None)
        self._progress.setRange(0, 100 if active and active.total else 0)
        if active and active.total:
            self._progress.setValue(int(active.completed * 100 / active.total))
        self._details.setFixedHeight(44 + min(260, len(jobs) * 85))
        self._details.setVisible(self._expanded and bool(jobs))
        self.setFixedHeight(40)
        self.position_details()
        self.setVisible(bool(jobs))
        self.layout_changed.emit()
