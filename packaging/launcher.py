import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYTHON_DIR = _REPO_ROOT / "python"
if str(_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(_PYTHON_DIR))

try:
    from Infernux.runtime_utf8 import configure_process_utf8

    configure_process_utf8()
except Exception:
    if sys.platform == "win32":
        os.environ.setdefault("PYTHONUTF8", "1")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")

sys.dont_write_bytecode = True

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QMessageBox, QDialog,
    QHBoxLayout, QVBoxLayout, QSizePolicy, QStackedWidget,
    QGraphicsOpacityEffect,
)
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QIcon, QFontDatabase

from ui_project_list import ProjectListPane
from database import ProjectDatabase
from style import StyleManager
from hub_resources import ICON_PATH, FONT_PATH
from hub_utils import HubLaunchContext, get_app_dir, is_frozen
from python_runtime import PythonRuntimeManager
from android_support import AndroidSupportManager
from version_manager import VersionManager

from model.project_model import ProjectModel
from viewmodel.control_pane_viewmodel import ControlPaneViewModel
from view.control_pane_view import ControlPane
from view.sidebar_view import SidebarView
from view.installs_view import InstallsView
from installer_safety import can_remove_install_dir
from i18n import configure_language, tr
from view.hover_widgets import ensure_hover_animation_filter
import logging


class GameEngineLauncher(QMainWindow):
    def __init__(self, launch_context: HubLaunchContext | None = None) -> None:
        self.launch_context = launch_context or HubLaunchContext.current()
        self._own_app = False
        if QApplication.instance() is None:
            self._own_app = True
            self.app = QApplication(sys.argv)
        else:
            self.app = QApplication.instance()

        super().__init__()

        # Configure localization before constructing any visible widget.
        self.db = ProjectDatabase()
        configure_language(self.db.get_setting("language", "system"))

        # Load custom engine font
        font_id = QFontDatabase.addApplicationFont(FONT_PATH)
        if font_id >= 0:
            QFontDatabase.applicationFontFamilies(font_id)

        # Apply the persisted Hub theme before constructing visible pages.
        self.app.is_dark_theme = self.db.get_setting("theme", "dark") != "light"
        self.app.setStyleSheet(StyleManager.get_stylesheet(self.app.is_dark_theme))
        ensure_hover_animation_filter(self.app)

        self.setWindowTitle("Infernux Hub")
        self.setWindowIcon(QIcon(ICON_PATH))
        self.resize(1080, 720)

        # Version and runtime managers
        self.runtime_manager = PythonRuntimeManager()
        self.android_support_manager = AndroidSupportManager()
        self.android_support_manager.activate_environment()
        self.version_manager = VersionManager(self.runtime_manager)

        # ── Root layout: sidebar | content ───────────────────────────
        central = QWidget(self)
        central.setObjectName("central")
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Sidebar
        self.sidebar = SidebarView(parent=central)
        root_layout.addWidget(self.sidebar)

        # Stacked pages
        self.pages = QStackedWidget()
        root_layout.addWidget(self.pages, 1)

        # ── Page 0: Projects ─────────────────────────────────────────
        projects_page = QWidget()
        projects_layout = QVBoxLayout(projects_page)
        projects_layout.setContentsMargins(28, 24, 28, 24)
        projects_layout.setSpacing(16)

        self.project_list = ProjectListPane(
            self.db, self.version_manager, parent=projects_page,
        )
        model = ProjectModel(self.db, self.version_manager, self.runtime_manager)
        viewmodel = ControlPaneViewModel(
            model,
            self.project_list,
            self.version_manager,
            self.runtime_manager,
            launch_context=self.launch_context,
        )
        self.viewmodel = viewmodel
        self.project_list.remove_requested.connect(self._remove_project_from_card)
        self.controls = ControlPane(viewmodel, parent=projects_page)

        self.controls.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.project_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        projects_layout.addWidget(self.controls, 0)
        projects_layout.addWidget(self.project_list, 1)

        self.pages.addWidget(projects_page)

        # ── Page 1: Installs ─────────────────────────────────────────
        installs_page = QWidget()
        installs_layout = QVBoxLayout(installs_page)
        installs_layout.setContentsMargins(28, 24, 28, 24)
        installs_layout.setSpacing(0)

        self.installs_view = InstallsView(
            self.version_manager,
            self.runtime_manager,
            self.android_support_manager,
            parent=installs_page,
        )
        installs_layout.addWidget(self.installs_view)

        self.pages.addWidget(installs_page)

        # ── Page 2: Settings ─────────────────────────────────────────
        from view.settings_view import SettingsView

        settings_page = QWidget()
        settings_layout = QVBoxLayout(settings_page)
        settings_layout.setContentsMargins(32, 30, 32, 30)
        self.settings_view = SettingsView(self.db, parent=settings_page)
        settings_layout.addWidget(self.settings_view)
        self.pages.addWidget(settings_page)

        from view.update_dialog import UpdateController
        self.update_controller = UpdateController(self)
        self.update_controller.check_finished.connect(
            self._on_startup_update_check_finished
        )
        self._startup_update_pending = False
        self.settings_view.update_check_requested.connect(
            lambda: self.update_controller.check(silent=False)
        )
        self.settings_view.language_changed.connect(self._on_language_changed)

        from view.notification_dialog import HubNotificationController

        self.notification_controller = HubNotificationController(
            self,
            self.db,
            open_installs=lambda: self.sidebar.select_page(1),
        )

        # ── Page 3: Discussion ──────────────────────────────────────
        from view.discussion_view import DiscussionView

        self.discussion_view = DiscussionView(parent=self.pages)
        self.pages.addWidget(self.discussion_view)

        # ── Sidebar → page switching ─────────────────────────────────
        self.sidebar.page_changed.connect(self._on_page_changed)

        # Cleanup on close
        self.app.aboutToQuit.connect(self._on_close)

    def _on_page_changed(self, index: int):
        self.pages.setCurrentIndex(index)
        effect = self.pages.graphicsEffect()
        if effect is None:
            effect = QGraphicsOpacityEffect(self.pages)
            self.pages.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        self._page_transition = QPropertyAnimation(effect, b"opacity", self)
        self._page_transition.setDuration(180)
        self._page_transition.setStartValue(0.0)
        self._page_transition.setEndValue(1.0)
        self._page_transition.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._page_transition.start()
        # Refresh installs when switching to that page
        if index == 1:
            self.installs_view.refresh()
        elif index == 2:
            self.settings_view.refresh()

    def _remove_project_from_card(self, project_id: str):
        self.project_list.select_project(project_id)
        self.viewmodel.remove_project(self)

    def _on_language_changed(self, _mode: str):
        """Rebuild visible widgets in the new language without restarting the process."""
        replacement = GameEngineLauncher()
        replacement.setGeometry(self.geometry())
        replacement.show()
        # Keep the replacement alive while the old window finishes its event turn.
        self._language_replacement = replacement
        self.hide()
        self.db.close()

    def run(self):
        self.show()
        if is_frozen():
            QTimer.singleShot(0, self._bootstrap_hub)
        if self._own_app:
            sys.exit(self.app.exec())

    def _bootstrap_hub(self):
        # Resolve application updates before presenting runtime migration.
        # This keeps an older Hub from offering engine/runtime actions that a
        # newer release has made incompatible.
        self._startup_update_pending = True
        self.update_controller.check(silent=True)

    def _on_startup_update_check_finished(self):
        if not self._startup_update_pending:
            return
        self._startup_update_pending = False
        self._bootstrap_python_runtime()

    def _bootstrap_python_runtime(self):
        # A fresh installer provisions the default runtime before launching the
        # Hub. An in-place upgrade from an older Hub deliberately does not
        # mutate Python behind the user's back, so make the missing requirement
        # explicit and take the user directly to the runtime controls.
        default_version = self.runtime_manager.default_version
        if not self.runtime_manager.has_runtime(default_version):
            QMessageBox.warning(
                self,
                tr("Python Runtime Required"),
                tr(
                    "This Infernux Hub requires Python {version} for current "
                    "engine releases. Install Python {version} from Installs "
                    "before installing or creating a project with them.\n\n"
                    "Older engine versions continue to use their own matching "
                    "Python runtime.",
                    version=default_version,
                ),
            )
            self.sidebar.select_page(1)
        QTimer.singleShot(0, self._finish_startup)

    def _finish_startup(self):
        self.installs_view.refresh()
        self.notification_controller.show_pending()

    def _on_close(self):
        self.db.close()


def _handle_uninstall() -> int:
    """Remove registry entries, Start Menu shortcut, and optionally the install directory."""
    if sys.platform == "darwin":
        return _handle_uninstall_macos()
    if sys.platform.startswith("linux"):
        return _handle_uninstall_linux()
    if sys.platform != "win32":
        return 1
    import winreg

    # Read install location from registry before removing the key.
    install_dir = ""
    reg_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\InfernuxHub"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key) as key:
            install_dir, _ = winreg.QueryValueEx(key, "InstallLocation")
    except OSError as _exc:
        logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
        pass

    # Remove registry entry
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, reg_key)
    except OSError as _exc:
        logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
        pass

    # Remove Start Menu shortcut
    try:
        import ctypes.wintypes
        buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x0002, None, 0, buf)
        if buf.value:
            import shutil as _shutil
            _shutil.rmtree(os.path.join(buf.value, "Infernux Hub"), ignore_errors=True)
    except Exception as _exc:
        logging.getLogger(__name__).debug("[Suppressed] %s: %s", type(_exc).__name__, _exc)
        pass

    # Ask user if they want to remove install files
    app = QApplication.instance() or QApplication(sys.argv)
    answer = QMessageBox.question(
        None,
        tr("Uninstall Infernux Hub"),
        tr("Registry entries and shortcuts have been removed.\n\nDo you also want to delete the installation folder?\n{path}", path=install_dir),
    )
    if answer == QMessageBox.Yes and install_dir and os.path.isdir(install_dir):
        import shutil as _shutil
        if can_remove_install_dir(install_dir):
            _shutil.rmtree(install_dir, ignore_errors=True)
        else:
            QMessageBox.warning(
                None,
                tr("Install Folder Preserved"),
                tr("The installation folder was not deleted because it is not marked as a safe Infernux Hub install directory.\n\n"
                "Your projects and downloaded engine versions are preserved. Remove application files manually only if "
                "you are sure this folder does not contain user data."),
            )

    QMessageBox.information(None, tr("Uninstall Complete"), tr("Infernux Hub has been uninstalled."))
    return 0


def _handle_uninstall_macos() -> int:
    """Remove Infernux Hub from macOS."""
    import shutil as _shutil

    app = QApplication.instance() or QApplication(sys.argv)

    # Typical macOS install / config locations
    config_dir = os.path.expanduser("~/.config/Infernux")
    app_link = os.path.expanduser("~/Applications/Infernux Hub")
    dirs_to_remove = [d for d in (config_dir, app_link) if os.path.exists(d)]

    if dirs_to_remove:
        answer = QMessageBox.question(
            None,
            "Uninstall Infernux Hub",
            "Do you want to remove Infernux Hub application configuration?\n\n"
            + "\n".join(dirs_to_remove),
        )
        if answer == QMessageBox.Yes:
            for d in dirs_to_remove:
                _shutil.rmtree(d, ignore_errors=True)

    QMessageBox.information(None, "Uninstall Complete", "Infernux Hub has been uninstalled.")
    return 0


def _handle_uninstall_linux() -> int:
    """Remove the Linux application while preserving Hub user data."""
    import shutil as _shutil

    app = QApplication.instance() or QApplication(sys.argv)

    desktop_entry = os.path.expanduser("~/.local/share/applications/infernux-hub.desktop")
    install_dir = get_app_dir()
    targets = [p for p in (desktop_entry, install_dir) if os.path.exists(p)]

    if targets:
        answer = QMessageBox.question(
            None,
            "Uninstall Infernux Hub",
            "Do you want to remove the Infernux Hub application?\n\n"
            + "\n".join(targets)
            + "\n\nProjects, downloaded engines, Python runtimes, and the shared "
            "plugin library are preserved.",
        )
        if answer == QMessageBox.Yes:
            for p in targets:
                if os.path.isdir(p):
                    if p == install_dir and not can_remove_install_dir(p):
                        raise RuntimeError(
                            "The Hub application directory is not a recognized install: "
                            f"{p}"
                        )
                    _shutil.rmtree(p)
                else:
                    os.remove(p)

    QMessageBox.information(None, "Uninstall Complete", "Infernux Hub has been uninstalled.")
    return 0


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        raise SystemExit(_handle_uninstall())
    launcher = GameEngineLauncher(HubLaunchContext.current())
    launcher.run()
