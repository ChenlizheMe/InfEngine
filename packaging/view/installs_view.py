"""Installs page — lists installed engine versions, install from GitHub or locate .whl."""

from __future__ import annotations

import os
import shutil
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QScrollArea, QFrame, QDialog, QFileDialog, QMessageBox,
    QProgressBar, QApplication,
)
from PySide6.QtCore import Qt, QThread, Signal, QObject

from version_manager import VersionManager, EngineVersion, DownloadCancelled
from i18n import tr
from view.hover_widgets import AnimatedSurfaceFrame


def _configure_install_scroll_area(scroll: QScrollArea, container: QWidget) -> None:
    """Keep install lists on the Hub palette instead of the OS viewport palette."""
    scroll.setObjectName("installScrollArea")
    scroll.viewport().setObjectName("installViewport")
    scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    container.setObjectName("installListContainer")
    container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


# ─── Version card (one per installed version) ────────────────────────

class _VersionCard(AnimatedSurfaceFrame):
    """Card showing a single installed engine version."""

    remove_clicked = Signal(str)  # version string

    def __init__(self, version: str, wheel_path: str, parent=None):
        super().__init__("versionCard", parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(64)
        self._version = version

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(14)

        # Version badge
        badge = QLabel(version)
        badge.setObjectName("versionBadge")
        layout.addWidget(badge)

        # Wheel filename / path
        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        info_col.setContentsMargins(0, 0, 0, 0)

        filename = os.path.basename(wheel_path) if wheel_path else "unknown"
        file_label = QLabel(filename)
        file_label.setObjectName("cardPath")
        file_label.setToolTip(wheel_path)
        info_col.addWidget(file_label)

        size_text = ""
        if wheel_path and os.path.isfile(wheel_path):
            size_mb = os.path.getsize(wheel_path) / (1024 * 1024)
            size_text = f"{size_mb:.1f} MB"
        size_label = QLabel(size_text)
        size_label.setObjectName("cardDate")
        info_col.addWidget(size_label)

        layout.addLayout(info_col, 1)

        # Remove button
        remove_btn = QPushButton(tr("Remove"))
        remove_btn.setObjectName("dangerBtn")
        remove_btn.setFixedHeight(30)
        remove_btn.setFixedWidth(80)
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.clicked.connect(lambda: self.remove_clicked.emit(self._version))
        layout.addWidget(remove_btn)


class _RuntimeCard(AnimatedSurfaceFrame):
    install_clicked = Signal(str)

    def __init__(self, version: str, path: str, *, default: bool, parent=None):
        super().__init__("versionCard", parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(72)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(14)

        info = QVBoxLayout()
        info.setSpacing(3)
        title = QLabel(
            tr("Python {version} (default)", version=version)
            if default
            else tr("Python {version}", version=version)
        )
        title.setObjectName("cardName")
        info.addWidget(title)
        detail = QLabel(
            path
            if path
            else tr("Not installed. Install this runtime before using its engine wheels.")
        )
        detail.setObjectName("cardPath")
        detail.setWordWrap(True)
        info.addWidget(detail)
        layout.addLayout(info, 1)

        button = QPushButton(
            tr("Reinstall") if path else tr("Install")
        )
        button.setObjectName("normalBtn" if path else "primaryBtn")
        button.setFixedHeight(34)
        button.setMinimumWidth(96)
        button.clicked.connect(lambda: self.install_clicked.emit(version))
        layout.addWidget(button)


# ─── Install Editor dialog (pick version from GitHub releases) ───────

class _FetchWorker(QObject):
    """Fetch available versions on a background thread."""
    finished = Signal(list)  # list[EngineVersion]

    def __init__(self, vm: VersionManager):
        super().__init__()
        self._vm = vm

    def run(self):
        versions = self._vm.list_versions(include_prerelease=True)
        self.finished.emit(versions)


class _DownloadWorker(QObject):
    """Download a version wheel on a background thread (cancellable)."""
    progress = Signal(int, int)  # downloaded, total
    finished = Signal(str)  # wheel path
    cancelled = Signal()
    error = Signal(str)

    def __init__(self, vm: VersionManager, version: str):
        super().__init__()
        self._vm = vm
        self._version = version
        self._cancel_requested = False

    def cancel(self):
        """Request cooperative cancellation (checked per 64 KB chunk)."""
        self._cancel_requested = True

    def run(self):
        try:
            path = self._vm.download_version(
                self._version,
                on_progress=lambda d, t: self.progress.emit(d, t),
                should_cancel=lambda: self._cancel_requested,
            )
            self.finished.emit(path)
        except DownloadCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.error.emit(str(exc))


class _RuntimeInstallWorker(QObject):
    finished = Signal(str)
    error = Signal(str)

    def __init__(
        self, runtime_manager, version: str, *, reinstall: bool = False
    ):
        super().__init__()
        self._runtime_manager = runtime_manager
        self._reinstall = reinstall
        self._version = version

    def run(self):
        try:
            if self._reinstall:
                python_exe = self._runtime_manager.reinstall_runtime(
                    self._version
                )
            else:
                python_exe = self._runtime_manager.ensure_runtime(
                    version=self._version
                )
        except Exception as exc:
            self.error.emit(str(exc))
            return
        self.finished.emit(python_exe)


class PythonRuntimeInstallDialog(QDialog):
    def __init__(
        self,
        runtime_manager,
        version: str,
        parent=None,
        *,
        reinstall: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle(tr("Preparing Python {version}", version=version))
        self.setModal(True)
        self.setFixedSize(420, 140)
        self.result_path = ""
        self.error_text = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel(
            tr("Preparing Python {version} for Infernux Hub", version=version)
        )
        title.setObjectName("cardName")
        layout.addWidget(title)

        detail = QLabel(
            tr(
                "A background setup process is extracting an isolated full Python "
                "{version} runtime under the Infernux Hub runtime directory. Projects "
                "targeting this Python version receive their own copy. Your existing "
                "Python installations are not used or changed. This window will close "
                "automatically when setup finishes.",
                version=version,
            )
        )
        detail.setWordWrap(True)
        detail.setObjectName("cardPath")
        layout.addWidget(detail)

        progress = QProgressBar(self)
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setFixedHeight(6)
        layout.addWidget(progress)

        self._thread = QThread(self)
        self._worker = _RuntimeInstallWorker(
            runtime_manager, version, reinstall=reinstall
        )
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_finished(self, python_exe: str):
        self.result_path = python_exe
        self.accept()

    def _on_error(self, message: str):
        self.error_text = message
        self.reject()

    def reject(self):
        if self._thread.isRunning() and not self.error_text:
            return
        super().reject()


class _VersionRow(AnimatedSurfaceFrame):
    """A selectable row inside the Install Editor dialog."""

    def __init__(self, ev: EngineVersion, parent=None):
        super().__init__("versionRow", parent)
        self.ev = ev
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(48)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(10)

        ver_label = QLabel(ev.display_name)
        ver_label.setObjectName("cardName")
        layout.addWidget(ver_label)

        if ev.python_version:
            python_label = QLabel(f"Python {ev.python_version}")
            python_label.setObjectName("cardDate")
            layout.addWidget(python_label)

        if ev.wheel_size:
            size_mb = ev.wheel_size / (1024 * 1024)
            size_label = QLabel(f"{size_mb:.1f} MB")
            size_label.setObjectName("cardDate")
            layout.addWidget(size_label)

        layout.addStretch()

        if ev.installed:
            installed_label = QLabel(tr("Installed"))
            installed_label.setObjectName("installedBadge")
            layout.addWidget(installed_label)

    def set_selected(self, selected: bool):
        self.setProperty("selected", selected)
        self.set_selected_animated(selected)


class InstallEditorDialog(QDialog):
    """Dialog that lists available versions from GitHub for installation."""

    def __init__(self, version_manager: VersionManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Install Engine Version"))
        self.setMinimumSize(520, 420)
        self._vm = version_manager
        self._selected: EngineVersion | None = None
        self._rows: list[tuple[EngineVersion, _VersionRow]] = []
        self._dl_thread: QThread | None = None
        self._dl_worker: _DownloadWorker | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        self._status = QLabel(tr("Fetching available versions..."))
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status)

        # Scroll area for version rows
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.hide()
        self._container = QWidget()
        _configure_install_scroll_area(self._scroll, self._container)
        self._list_layout = QVBoxLayout(self._container)
        self._list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._list_layout.setSpacing(4)
        self._list_layout.setContentsMargins(0, 0, 4, 0)
        self._scroll.setWidget(self._container)
        layout.addWidget(self._scroll, 1)

        # Progress bar (hidden until download starts)
        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedHeight(6)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.hide()
        layout.addWidget(self._progress_bar)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        btn_cancel = QPushButton(tr("Cancel"))
        btn_cancel.setObjectName("normalBtn")
        btn_cancel.setFixedHeight(34)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        self._btn_install = QPushButton(tr("Install"))
        self._btn_install.setObjectName("primaryBtn")
        self._btn_install.setFixedHeight(34)
        self._btn_install.setMinimumWidth(100)
        self._btn_install.setEnabled(False)
        self._btn_install.clicked.connect(self._on_install)
        btn_row.addWidget(self._btn_install)

        layout.addLayout(btn_row)

        # Kick off fetch in background
        self._fetch_thread = QThread()
        self._fetch_worker = _FetchWorker(self._vm)
        self._fetch_worker.moveToThread(self._fetch_thread)
        self._fetch_thread.started.connect(self._fetch_worker.run)
        self._fetch_worker.finished.connect(self._on_versions_loaded)
        self._fetch_worker.finished.connect(self._fetch_thread.quit)
        self._fetch_thread.start()

    # ── Slots ────────────────────────────────────────────────────────

    def _on_versions_loaded(self, versions: list):
        self._status.hide()
        self._scroll.show()

        if not versions:
            self._status.setText(tr("No versions found."))
            self._status.show()
            return

        for ev in versions:
            row = _VersionRow(ev)
            row.mousePressEvent = lambda _e, v=ev: self._select(v)
            self._list_layout.addWidget(row)
            self._rows.append((ev, row))

        self._list_layout.addStretch()

    def _select(self, ev: EngineVersion):
        self._selected = ev
        block_reason = self._vm.installation_block_reason(ev)
        if block_reason:
            self._btn_install.setEnabled(False)
            if (
                ev.python_version
                and not ev.compatibility_error
                and not self._vm.is_python_runtime_installed(ev.python_version)
            ):
                self._status.setText(
                    tr(
                        "Infernux {engine} requires Python {version}. Please "
                        "install Python {version} first.",
                        engine=ev.version,
                        version=ev.python_version,
                    )
                )
            else:
                self._status.setText(block_reason)
            self._status.show()
            for candidate, row in self._rows:
                row.set_selected(candidate is ev)
            return
        self._btn_install.setEnabled(
            not ev.installed and bool(ev.wheel_url)
        )
        self._status.hide()
        for v, row in self._rows:
            row.set_selected(v is ev)

    def _on_install(self):
        if not self._selected or self._selected.installed:
            return
        if self._dl_thread is not None and self._dl_thread.isRunning():
            return  # one download at a time
        # Start download
        self._btn_install.setEnabled(False)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.show()

        self._dl_thread = QThread()
        self._dl_worker = _DownloadWorker(self._vm, self._selected.version)
        self._dl_worker.moveToThread(self._dl_thread)
        self._dl_thread.started.connect(self._dl_worker.run)
        self._dl_worker.progress.connect(self._on_dl_progress)
        self._dl_worker.finished.connect(self._on_dl_finished)
        self._dl_worker.cancelled.connect(self._on_dl_cancelled)
        self._dl_worker.error.connect(self._on_dl_error)
        self._dl_worker.finished.connect(self._dl_thread.quit)
        self._dl_worker.cancelled.connect(self._dl_thread.quit)
        self._dl_worker.error.connect(self._dl_thread.quit)
        self._dl_thread.start()

    def _cancel_active_download(self, *, wait_ms: int = 10000) -> None:
        """Cooperatively stop the in-flight download and wait for the thread.

        The worker deletes its partial temp file before exiting, so closing
        the dialog mid-download can no longer corrupt the version cache
        (issue #43).
        """
        if self._dl_worker is not None:
            self._dl_worker.cancel()
        if self._dl_thread is not None and self._dl_thread.isRunning():
            self._dl_thread.quit()
            self._dl_thread.wait(wait_ms)

    def reject(self):
        self._cancel_active_download()
        super().reject()

    def closeEvent(self, event):
        self._cancel_active_download()
        super().closeEvent(event)

    def _on_dl_progress(self, downloaded: int, total: int):
        if total > 0:
            self._progress_bar.setValue(int(downloaded * 100 / total))

    def _on_dl_finished(self, _path: str):
        self._progress_bar.hide()
        self.accept()

    def _on_dl_cancelled(self):
        self._progress_bar.hide()
        self._btn_install.setEnabled(True)

    def _on_dl_error(self, msg: str):
        self._progress_bar.hide()
        QMessageBox.critical(self, tr("Download Failed"), msg)
        self._btn_install.setEnabled(True)


# ─── Main Installs page ─────────────────────────────────────────────

class InstallsView(QWidget):
    """Page showing installed engine versions with install/locate actions."""

    def __init__(self, version_manager: VersionManager, runtime_manager=None, parent=None):
        super().__init__(parent)
        self._vm = version_manager
        self._runtime_manager = runtime_manager

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        self._runtime_container = QWidget()
        self._runtime_layout = QVBoxLayout(self._runtime_container)
        self._runtime_layout.setContentsMargins(0, 0, 0, 0)
        self._runtime_layout.setSpacing(6)

        # ── Header ───────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(4, 0, 0, 12)

        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title = QLabel(tr("Installs"))
        title.setObjectName("pageTitle")
        title_block.addWidget(title)
        subtitle = QLabel(tr("Installs and managed runtime"))
        subtitle.setObjectName("pageSubtitle")
        title_block.addWidget(subtitle)
        header.addLayout(title_block)
        header.addStretch()

        self.btn_locate = QPushButton(tr("Locate"))
        self.btn_locate.setObjectName("normalBtn")
        self.btn_locate.setFixedHeight(36)
        self.btn_locate.setMinimumWidth(90)
        self.btn_locate.clicked.connect(self._on_locate)
        header.addWidget(self.btn_locate)

        spacer = QLabel("")
        spacer.setFixedWidth(8)
        header.addWidget(spacer)

        self.btn_install = QPushButton(tr("Install Editor"))
        self.btn_install.setObjectName("primaryBtn")
        self.btn_install.setFixedHeight(36)
        self.btn_install.setMinimumWidth(130)
        self.btn_install.clicked.connect(self._on_install_editor)
        header.addWidget(self.btn_install)

        layout.addLayout(header)
        layout.addWidget(self._runtime_container)

        # ── Version list (scrollable) ────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll, 1)

        self._container = QWidget()
        _configure_install_scroll_area(scroll, self._container)
        self._card_layout = QVBoxLayout(self._container)
        self._card_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._card_layout.setSpacing(6)
        self._card_layout.setContentsMargins(0, 0, 4, 0)
        scroll.setWidget(self._container)

        self.refresh()

    # ── Public API ───────────────────────────────────────────────────

    def refresh(self):
        self._refresh_runtime_status()

        # Clear existing cards
        while self._card_layout.count():
            item = self._card_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        versions = self._vm.installed_versions()
        if not versions:
            empty = QLabel(tr("No engine versions installed.\nClick 'Install Editor' or 'Locate' to add one."))
            empty.setObjectName("emptyHint")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._card_layout.addWidget(empty)
        else:
            for ver in versions:
                wheel = self._vm.get_wheel_path(ver) or ""
                card = _VersionCard(ver, wheel)
                card.remove_clicked.connect(self._on_remove_version)
                self._card_layout.addWidget(card)

        self._card_layout.addStretch()

    def _refresh_runtime_status(self):
        while self._runtime_layout.count():
            item = self._runtime_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        if self._runtime_manager is None:
            self._runtime_container.hide()
            return

        self._runtime_container.show()
        for version in self._runtime_manager.supported_versions():
            card = _RuntimeCard(
                version,
                self._runtime_manager.get_runtime_path(version) or "",
                default=version == self._runtime_manager.default_version,
            )
            card.install_clicked.connect(self._on_install_python)
            self._runtime_layout.addWidget(card)

    # ── Actions ──────────────────────────────────────────────────────

    def _on_install_editor(self):
        dlg = InstallEditorDialog(self._vm, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self.refresh()

    def _on_install_python(self, version: str):
        if self._runtime_manager is None:
            return

        dlg = PythonRuntimeInstallDialog(
            self._runtime_manager,
            version,
            self,
            reinstall=self._runtime_manager.has_runtime(version),
        )
        if dlg.exec() == QDialog.Accepted:
            QMessageBox.information(
                self,
                tr("Python Installed"),
                tr(
                    "Python {version} is ready at:\n{path}",
                    version=version,
                    path=dlg.result_path,
                ),
            )
        elif dlg.error_text:
            QMessageBox.critical(self, tr("Python Installation Failed"), dlg.error_text)
        self.refresh()

    def _on_locate(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Select Infernux Wheel"),
            "",
            "Wheel files (*.whl)",
        )
        if not path:
            return

        try:
            version = self._vm.install_local_wheel(path)
            QMessageBox.information(
                self, tr("Version Installed"),
                tr("Infernux {version} has been installed from the selected wheel.", version=version),
            )
            self.refresh()
        except ValueError as exc:
            QMessageBox.critical(self, tr("Invalid Wheel"), str(exc))

    def _on_remove_version(self, version: str):
        confirm = QMessageBox.question(
            self,
            tr("Remove Version"),
            f"Remove Infernux {version}?\n\n"
            + tr("This deletes the cached wheel. Projects using this version will need to reinstall it."),
        )
        if confirm != QMessageBox.Yes:
            return
        self._vm.remove_version(version)
        self.refresh()
