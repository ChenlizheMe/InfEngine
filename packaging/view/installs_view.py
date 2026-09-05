"""Installs page — lists installed engine versions, install from GitHub or locate .whl."""

from __future__ import annotations

import os
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QScrollArea, QFrame, QDialog, QFileDialog, QMessageBox,
)
from PySide6.QtCore import Qt, QThread, Signal, QObject

from version_manager import VersionManager, EngineVersion
from install_queue import InstallQueue
from android_support import AndroidSupportManager
from i18n import tr
from view.hover_widgets import AnimatedSurfaceFrame


def _update_install_buttons(view, queue):
    for button in view.findChildren(QPushButton):
        key = button.property("installationKey")
        if key:
            pending = queue.is_pending(key)
            button.setEnabled(not pending)
            button.setText(tr("In queue") if pending else button.property("idleText"))


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
        button.setProperty("installationKey", f"python:{version}")
        button.setProperty("idleText", button.text())
        button.clicked.connect(lambda: self.install_clicked.emit(version))
        layout.addWidget(button)


class _AndroidSupportCard(AnimatedSurfaceFrame):
    install_clicked = Signal()

    def __init__(self, manager: AndroidSupportManager, parent=None):
        super().__init__("versionCard", parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(86)
        status = manager.status()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)
        info = QVBoxLayout()
        info.setSpacing(3)
        title = QLabel(tr("Android compatibility"))
        title.setObjectName("cardName")
        info.addWidget(title)
        if status.installed:
            detail_text = tr(
                "SDK, NDK, JDK, Gradle and Android CPython are managed once for every project.\n{path}",
                path=str(status.root),
            )
        elif status.error:
            detail_text = tr("Installed files need repair: {message}", message=status.error)
        else:
            detail_text = tr(
                "Required before the Android platform plugin can be imported. "
                "Large toolchains are installed once and shared by every project."
            )
        detail = QLabel(detail_text)
        detail.setObjectName("cardPath")
        detail.setWordWrap(True)
        info.addWidget(detail)
        layout.addLayout(info, 1)

        install = QPushButton(tr("Repair") if status.installed or status.error else tr("Install"))
        install.setObjectName("normalBtn" if status.installed else "primaryBtn")
        install.setFixedHeight(34)
        install.setMinimumWidth(96)
        install.setProperty("installationKey", "android")
        install.setProperty("idleText", install.text())
        install.clicked.connect(self.install_clicked.emit)
        layout.addWidget(install)


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

    runtime_install_requested = Signal(str)

    def __init__(self, version_manager: VersionManager, queue: InstallQueue, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Install Engine Version"))
        self.setMinimumSize(520, 420)
        self._vm = version_manager
        self._selected: EngineVersion | None = None
        self._rows: list[tuple[EngineVersion, _VersionRow]] = []
        self._queue = queue
        self._queue.job_finished.connect(self._refresh_selection)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        self._status = QLabel(tr("Fetching available versions..."))
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._btn_runtime = QPushButton()
        self._btn_runtime.setObjectName("primaryBtn")
        self._btn_runtime.setMinimumHeight(34)
        self._btn_runtime.hide()
        self._btn_runtime.clicked.connect(self._on_install_runtime)
        layout.addWidget(self._btn_runtime)

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
        self._btn_runtime.hide()
        block_reason = self._vm.installation_block_reason(ev)
        if block_reason:
            self._btn_install.setEnabled(False)
            if (
                ev.python_version and ev.wheel_url
                and not ev.compatibility_error
                and not self._vm.is_python_runtime_installed(ev.python_version)
            ):
                self._status.setText(
                    tr(
                        "Infernux {engine} needs its managed runtime (Python {version}). "
                        "Install it here; no Python or Conda setup is required.",
                        engine=ev.version,
                        version=ev.python_version,
                    )
                )
                self._btn_runtime.setText(
                    tr("Install required runtime (Python {version})", version=ev.python_version)
                )
                self._btn_runtime.setEnabled(not self._queue.is_pending(f"python:{ev.python_version}"))
                self._btn_runtime.show()
            else:
                self._status.setText(block_reason)
            self._status.show()
            for candidate, row in self._rows:
                row.set_selected(candidate is ev)
            return
        self._btn_install.setEnabled(
            not ev.installed and bool(ev.wheel_url)
            and not self._queue.is_pending(f"engine:{ev.version}")
        )
        self._status.hide()
        for v, row in self._rows:
            row.set_selected(v is ev)

    def _on_install_runtime(self):
        engine = self._selected
        self.runtime_install_requested.emit(engine.python_version)
        self._select(engine)

    def _refresh_selection(self, _job):
        if self._selected is not None:
            self._select(self._selected)

    def _on_install(self):
        engine = self._selected
        if engine is None or not self._btn_install.isEnabled():
            return
        manager = self._vm
        self._queue.submit(
            f"engine:{engine.version}", f"Infernux {engine.version}",
            lambda report: manager.download_version(
                engine.version,
                on_progress=lambda done, total: report(tr("Downloading"), done, total),
            ),
        )
        self.accept()


# ─── Main Installs page ─────────────────────────────────────────────

class InstallsView(QWidget):
    """Engine installations only; all writes are owned by the shared queue."""

    runtime_install_requested = Signal(str)

    def __init__(self, version_manager, queue: InstallQueue, parent=None):
        super().__init__(parent)
        self._vm = version_manager
        self._queue = queue
        self._install_dialog = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        header = QHBoxLayout()
        title = QLabel(tr("Engine versions"))
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch()
        self.btn_locate = QPushButton(tr("Locate"))
        self.btn_locate.setObjectName("normalBtn")
        self.btn_locate.setMinimumHeight(36)
        self.btn_locate.clicked.connect(self._on_locate)
        header.addWidget(self.btn_locate)
        self.btn_install = QPushButton(tr("Install Editor"))
        self.btn_install.setObjectName("primaryBtn")
        self.btn_install.setMinimumHeight(36)
        self.btn_install.clicked.connect(self._on_install_editor)
        header.addWidget(self.btn_install)
        layout.addLayout(header)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll, 1)
        self._container = QWidget()
        _configure_install_scroll_area(scroll, self._container)
        self._card_layout = QVBoxLayout(self._container)
        self._card_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self._container)
        queue.job_finished.connect(self._on_job_finished)
        self.refresh()

    def _on_job_finished(self, _job):
        self.refresh()

    def refresh(self):
        while self._card_layout.count():
            item = self._card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        versions = self._vm.installed_versions()
        if not versions:
            label = QLabel(tr("No engine versions installed.\nClick 'Install Editor' or 'Locate' to add one."))
            label.setObjectName("emptyHint")
            self._card_layout.addWidget(label)
        for version in versions:
            card = _VersionCard(version, self._vm.get_wheel_path(version) or "")
            card.remove_clicked.connect(self._on_remove_version)
            self._card_layout.addWidget(card)

    def _on_install_editor(self):
        if self._install_dialog is None:
            self._install_dialog = InstallEditorDialog(self._vm, self._queue, self)
            self._install_dialog.runtime_install_requested.connect(self.runtime_install_requested)
        self._install_dialog.show()
        self._install_dialog.raise_()
        self._install_dialog.activateWindow()

    def _on_locate(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Select Infernux Wheel"), "", "Wheel files (*.whl)",
        )
        if path:
            manager = self._vm
            self._queue.submit(
                f"engine-file:{os.path.normcase(os.path.abspath(path))}",
                os.path.basename(path),
                lambda report: manager.install_local_wheel(path),
            )

    def _on_remove_version(self, version):
        if self._queue.busy:
            QMessageBox.information(self, tr("Installation in progress"),
                                    tr("Wait for installations to finish before removing an engine."))
            return
        if QMessageBox.question(
            self, tr("Remove Version"),
            f"Infernux {version}\n" + tr("This deletes the cached wheel. Projects using this version will need to reinstall it."),
        ) == QMessageBox.Yes:
            self._vm.remove_version(version)
            self.refresh()


class PythonRuntimesView(QWidget):
    def __init__(self, manager, queue: InstallQueue, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._queue = queue
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel(tr("Runtime environment"))
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        description = QLabel(tr("Required to run the editor. Hub installs and manages Python for you; no programming or environment setup is needed."))
        description.setWordWrap(True)
        description.setObjectName("pageSubtitle")
        layout.addWidget(description)
        self._cards = QVBoxLayout()
        layout.addLayout(self._cards)
        layout.addStretch()
        queue.job_finished.connect(self._on_job_finished)
        queue.changed.connect(self._update_actions)
        self.refresh()

    def _update_actions(self):
        _update_install_buttons(self, self._queue)

    def _on_job_finished(self, _job):
        self.refresh()

    def refresh(self):
        while self._cards.count():
            self._cards.takeAt(0).widget().deleteLater()
        for version in self._manager.supported_versions():
            card = _RuntimeCard(
                version, self._manager.get_runtime_path(version) or "",
                default=version == self._manager.default_version,
            )
            card.install_clicked.connect(self.install)
            self._cards.addWidget(card)
        self._update_actions()

    def install(self, version):
        manager = self._manager
        reinstall = manager.has_runtime(version)
        def prepare(report):
            report(tr("Preparing runtime"), 0, 0)
            status = lambda text: report(text, 0, 0)
            if reinstall:
                return manager.reinstall_runtime(version, on_status=status)
            return manager.ensure_runtime(version=version, on_status=status)
        return self._queue.submit(f"python:{version}", f"Python {version}", prepare)


class AndroidSupportView(QWidget):
    def __init__(self, manager, queue: InstallQueue, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._queue = queue
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel(tr("Android support"))
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        description = QLabel(tr("Only needed to build Android games. Install from the official channel; Hub manages the shared build tools for all projects."))
        description.setWordWrap(True)
        description.setObjectName("pageSubtitle")
        layout.addWidget(description)
        self._cards = QVBoxLayout()
        layout.addLayout(self._cards)
        layout.addStretch()
        queue.job_finished.connect(self._on_job_finished)
        queue.changed.connect(self._update_actions)
        self.refresh()

    def _update_actions(self):
        _update_install_buttons(self, self._queue)

    def _on_job_finished(self, _job):
        self.refresh()

    def refresh(self):
        while self._cards.count():
            self._cards.takeAt(0).widget().deleteLater()
        card = _AndroidSupportCard(self._manager)
        card.install_clicked.connect(self.install)
        self._cards.addWidget(card)
        self._update_actions()

    def install(self):
        manager = self._manager
        return self._queue.submit(
            "android", tr("Android support"),
            lambda report: manager.install(
                on_progress=lambda done, total: report(tr("Downloading"), done, total),
            ),
        )
