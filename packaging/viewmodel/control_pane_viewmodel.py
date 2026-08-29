import os
from PySide6.QtWidgets import (
    QMessageBox, QDialog, QVBoxLayout, QLabel, QProgressBar, QFileDialog, QInputDialog
)
from PySide6.QtCore import QThread, Signal, QObject, QTimer, Qt
from model.project_model import ProjectModel
from hub_utils import is_frozen, is_project_open
from project_paths import ProjectPathError
from project_migration import ProjectMigrationService
from i18n import tr
import random


class CustomProgressDialog(QDialog):
    """Indeterminate progress dialog shown during project initialization."""

    def __init__(self, parent=None, title=None):
        super().__init__(parent)
        self.setWindowTitle(title or tr("Initializing"))
        self.setWindowModality(Qt.WindowModal)
        self.setFixedSize(340, 110)

        self.label = QLabel(tr("Preparing project..."), self)
        self.label.setAlignment(Qt.AlignCenter)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)

        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.addWidget(self.progress_bar)
        self.setLayout(layout)

        self.messages = [
            tr("Setting up project structure..."),
            tr("Copying engine libraries..."),
            tr("Setting up Python runtime..."),
            tr("Preparing asset folders..."),
            tr("Almost there..."),
        ]

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._rotate_message)
        self.timer.start(2000)

    def set_status(self, message: str):
        if self.timer.isActive():
            self.timer.stop()
        self.label.setText(tr(message))

    def _rotate_message(self):
        self.label.setText(random.choice(self.messages))


class InitProjectWorker(QObject):
    """Worker that runs project initialization on a background thread."""
    progress = Signal(str)
    finished = Signal()
    error = Signal(str)

    def __init__(self, model, name, path, engine_version=""):
        super().__init__()
        self.model = model
        self.name = name
        self.path = path
        self.engine_version = engine_version
        self.project_dir = ""

    def run(self):
        try:
            self.project_dir = self.model.init_project_folder(
                self.name,
                self.path,
                self.engine_version,
                on_status=self.progress.emit,
            )
        except Exception as exc:
            self.error.emit(str(exc))
            return
        self.finished.emit()


class MigrationWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, service, project_path: str, target_version: str):
        super().__init__()
        self.service = service
        self.project_path = project_path
        self.target_version = target_version

    def run(self):
        try:
            result = self.service.migrate(
                self.project_path,
                self.target_version,
                on_status=self.progress.emit,
            )
        except Exception as exc:
            self.error.emit(str(exc))
            return
        self.finished.emit(result)


class LaunchPreparationWorker(QObject):
    """Prepare a project runtime without blocking the Hub event loop."""

    progress = Signal(str, int)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, model, version_manager, project_path: str):
        super().__init__()
        self.model = model
        self.version_manager = version_manager
        self.project_path = project_path

    def run(self):
        try:
            project_path = self.project_path
            if not os.path.isdir(project_path):
                raise RuntimeError(
                    f"The project folder could not be found:\n{project_path}\n\n"
                    "Remove this entry from Hub, then use Open Existing to register its new location."
                )
            if is_project_open(project_path):
                raise RuntimeError(
                    "The project is already open in Infernux and cannot be opened again:\n"
                    f"{project_path}"
                )

            self.progress.emit("Checking engine version...", 4)
            python_version = ProjectModel.get_project_python_version(project_path)
            pinned_version = (
                self.version_manager.read_project_version(project_path)
                if self.version_manager
                else ""
            )
            if (
                pinned_version
                and self.version_manager
                and not self.version_manager.is_installed(
                    pinned_version, python_version
                )
            ):
                raise RuntimeError(
                    f"Infernux {pinned_version} for Python {python_version} is "
                    f"required by this project. Open {tr('Installs')} and install "
                    "that exact engine/Python combination before launching."
                )

            if is_frozen():
                self.progress.emit("Checking project runtime...", 7)
                python_exe = ProjectModel._get_project_python(project_path)
                if not os.path.isfile(python_exe):
                    raise RuntimeError(
                        "Project Python runtime not found at:\n"
                        f"{os.path.dirname(python_exe)}\n\n"
                        f"Install Python {python_version} in Hub, then repair this "
                        "project runtime."
                    )
                # Starting the editor is the authoritative native import check.
                # A separate smoke-test process here used to double cold-start
                # Python before the splash screen could even become responsive.
            else:
                import sys

                self.progress.emit("Synchronizing development runtime...", 7)
                self.model._install_infernux_in_runtime(
                    project_path,
                    pinned_version,
                    on_status=lambda message: self.progress.emit(message, 8),
                    validate_current=False,
                )
                self.progress.emit("Updating editor integration...", 9)
                self.model._create_vscode_workspace(project_path)
                python_exe = sys.executable
        except Exception as exc:
            self.error.emit(str(exc))
            return
        self.finished.emit(python_exe)


class ControlPaneViewModel:
    def __init__(self, model, project_list, version_manager=None, runtime_manager=None):
        self.model = model
        self.project_list = project_list
        self.version_manager = version_manager
        self.runtime_manager = runtime_manager
        self._launch_thread = None
        self._launch_worker = None
        self._launch_splash = None

    def launch_project(self, parent):
        record = self.project_list.get_selected_record()
        if record is None:
            QMessageBox.warning(parent, tr("No Selection"), tr("Please select a project to launch."))
            return

        if self._launch_thread is not None and self._launch_thread.isRunning():
            if self._launch_splash is not None:
                self._launch_splash.show()
                self._launch_splash.raise_()
            return

        project_name = record.name
        project_path = record.path
        
        script = (
            'import sys;'
            'from Infernux.engine import release_engine;'
            'from Infernux.lib import LogLevel;'
            'release_engine(engine_log_level=LogLevel.Info, project_path=sys.argv[1])'
        )

        from splash_screen import EngineSplashScreen
        from hub_resources import ICON_PATH

        splash = EngineSplashScreen(ICON_PATH, project_name, parent=None)
        splash.show()
        self._splash = splash
        self._launch_splash = splash

        self._launch_python_exe = ""
        self._launch_error = ""
        self._launch_thread = QThread()
        self._launch_worker = LaunchPreparationWorker(
            self.model,
            self.version_manager,
            project_path,
        )
        self._launch_worker.moveToThread(self._launch_thread)

        def store_python_exe(python_exe: str):
            self._launch_python_exe = python_exe

        def store_error(message: str):
            self._launch_error = message

        def finish_preparation():
            error = self._launch_error
            python_exe = self._launch_python_exe
            worker = self._launch_worker
            thread = self._launch_thread
            self._launch_error = ""
            self._launch_python_exe = ""
            self._launch_worker = None
            self._launch_thread = None

            if error:
                splash.show_preparation_failure(error)
            elif python_exe:
                splash.launch(
                    python_exe,
                    script,
                    project_path,
                    detached=is_frozen(),
                )

            if worker is not None:
                worker.deleteLater()
            if thread is not None:
                thread.deleteLater()

        self._launch_cleanup_timer = QTimer()
        self._launch_cleanup_timer.setSingleShot(True)
        self._launch_cleanup_timer.setInterval(0)
        self._launch_cleanup_timer.timeout.connect(finish_preparation)

        self._launch_thread.started.connect(self._launch_worker.run)
        self._launch_worker.progress.connect(splash.set_preparation_status)
        self._launch_worker.finished.connect(store_python_exe)
        self._launch_worker.finished.connect(self._launch_thread.quit)
        self._launch_worker.error.connect(store_error)
        self._launch_worker.error.connect(self._launch_thread.quit)
        self._launch_thread.finished.connect(self._launch_cleanup_timer.start)

        # Let Qt paint the splash before any runtime preparation begins.
        QTimer.singleShot(0, self._launch_thread.start)

    def open_existing_project(self, parent):
        initial_dir = self.model.db.get_setting("last_open_project_dir", "") if self.model.db else ""
        selected_dir = QFileDialog.getExistingDirectory(
            parent, tr("Open Existing Infernux Project"), initial_dir,
        )
        if not selected_dir:
            return

        try:
            record, info = self.model.register_existing_project(selected_dir)
        except (ProjectPathError, RuntimeError) as exc:
            QMessageBox.critical(parent, tr("Cannot Open Project"), str(exc))
            return

        if self.model.db:
            self.model.db.set_setting("last_open_project_dir", os.path.dirname(info.path))
        self.project_list.refresh()
        self.project_list.select_project(record.project_id)

        if info.engine_version and self.version_manager is not None and not self.version_manager.is_installed(info.engine_version):
            QMessageBox.information(
                parent,
                tr("Engine Version Not Installed"),
                f"Project '{info.name}' was added to Hub, but engine version "
                f"{info.engine_version} is not installed yet.\n\nOpen Installs to install it before launching.",
            )

    def remove_project(self, parent):
        record = self.project_list.get_selected_record()
        if record is None:
            QMessageBox.warning(parent, tr("No Selection"), tr("Please select a project to remove from Hub."))
            return

        confirm = QMessageBox.question(
            parent,
            tr("Remove Project from Hub"),
            f"Remove '{record.name}' from Infernux Hub?\n\n"
            f"{tr('Project files will not be deleted.')}\n{record.path}",
        )
        if confirm != QMessageBox.Yes:
            return

        self.model.remove_project(record.project_id)
        self.project_list.refresh()

    def relocate_project(self, parent):
        record = self.project_list.get_selected_record()
        if record is None:
            QMessageBox.warning(parent, tr("No Selection"), tr("Please select a project to relocate."))
            return

        initial_dir = record.path if os.path.isdir(record.path) else os.path.dirname(record.path)
        selected_dir = QFileDialog.getExistingDirectory(
            parent, tr("Relocate Infernux Project"), initial_dir,
        )
        if not selected_dir:
            return

        try:
            relocated, info = self.model.relocate_project(record.project_id, selected_dir)
        except (ProjectPathError, RuntimeError) as exc:
            QMessageBox.critical(parent, tr("Cannot Relocate Project"), str(exc))
            return

        if self.model.db:
            self.model.db.set_setting("last_open_project_dir", os.path.dirname(info.path))
        self.project_list.refresh()
        self.project_list.select_project(relocated.project_id)

    def migrate_project(self, parent):
        record = self.project_list.get_selected_record()
        if record is None:
            QMessageBox.warning(parent, tr("No Selection"), tr("Please select a project to migrate."))
            return
        if not os.path.isdir(record.path):
            QMessageBox.warning(parent, tr("Project Path Missing"), record.path)
            return

        current = self.version_manager.read_project_version(record.path) or ""
        versions = [version for version in self.version_manager.installed_versions() if version != current]
        if not versions:
            QMessageBox.information(
                parent,
                tr("No Other Version"),
                tr("Install another engine version before migrating this project."),
            )
            return

        target, accepted = QInputDialog.getItem(
            parent,
            tr("Migrate Project"),
            tr("Select target engine version:"),
            versions,
            0,
            False,
        )
        if not accepted or not target:
            return

        confirmation = QMessageBox.question(
            parent,
            tr("Confirm Project Migration"),
            f"{record.name}: {current or '(unversioned)'} → {target}\n\n"
            + tr("A backup of Assets and ProjectSettings will be created before the runtime and version pin are changed."),
        )
        if confirmation != QMessageBox.Yes:
            return

        progress = CustomProgressDialog(parent, tr("Migrate Project"))
        progress.show()
        service = ProjectMigrationService(self.model, self.version_manager)
        self._migration_error = ""
        self._migration_result = None
        self._migration_thread = QThread()
        self._migration_worker = MigrationWorker(service, record.path, target)
        self._migration_worker.moveToThread(self._migration_thread)

        def store_result(result):
            self._migration_result = result

        def store_error(message):
            self._migration_error = message

        def cleanup():
            progress.accept()
            if self._migration_error:
                QMessageBox.critical(parent, tr("Project Migration Failed"), self._migration_error)
            elif self._migration_result is not None:
                QMessageBox.information(
                    parent,
                    tr("Project Migration Complete"),
                    tr("Backup created at:\n{path}", path=self._migration_result.backup_path),
                )
            self.project_list.refresh()
            self.project_list.select_project(record.project_id)
            self._migration_worker.deleteLater()
            self._migration_thread.deleteLater()

        self._migration_timer = QTimer()
        self._migration_timer.setSingleShot(True)
        self._migration_timer.timeout.connect(cleanup)
        self._migration_thread.started.connect(self._migration_worker.run)
        self._migration_worker.progress.connect(progress.set_status)
        self._migration_worker.finished.connect(store_result)
        self._migration_worker.finished.connect(self._migration_thread.quit)
        self._migration_worker.error.connect(store_error)
        self._migration_worker.error.connect(self._migration_thread.quit)
        self._migration_thread.finished.connect(self._migration_timer.start)
        self._migration_thread.start()

    def delete_project(self, parent):
        """Compatibility alias for older views."""
        self.remove_project(parent)

    def create_project(self, parent):
        from view.new_project_view import NewProjectView

        dialog = NewProjectView(self.version_manager, self.runtime_manager, parent)
        if dialog.exec() != QDialog.Accepted:
            return

        new_name, project_path, engine_version = dialog.get_data()
        if not new_name:
            QMessageBox.warning(parent, tr("Missing Name"), tr("Please enter a project name."))
            return
        if not project_path:
            QMessageBox.warning(parent, tr("Missing Location"), tr("Please choose a project location."))
            return
        if is_frozen() and not engine_version:
            QMessageBox.warning(parent, tr("Missing Version"), tr("Please select an installed engine version."))
            return
        progress_dialog = CustomProgressDialog(parent)
        progress_dialog.show()

        self._init_error: str = ""

        self.thread = QThread()
        self.worker = InitProjectWorker(
            self.model, new_name, project_path, engine_version
        )
        self.worker.moveToThread(self.thread)

        def _store_error(msg: str):
            # Called from the worker thread — only store the message.
            self._init_error = msg

        def _cleanup():
            # Guaranteed to run on the main thread (QTimer fires in main loop).
            progress_dialog.accept()
            record = None
            error_message = self._init_error
            self._init_error = ""
            if error_message:
                QMessageBox.critical(
                    parent, tr("Project Creation Failed"), error_message,
                )
            elif self.worker.project_dir:
                record = self.model.add_project(new_name, self.worker.project_dir)
                if record is None:
                    QMessageBox.warning(
                        parent,
                        tr("Project Created"),
                        "The project was created successfully, but it is already registered in Hub.\n\n"
                        f"{self.worker.project_dir}",
                    )
            self.project_list.refresh()
            if not error_message and self.worker.project_dir and record is not None:
                self.project_list.select_project(record.project_id)
            self.worker.deleteLater()
            self.thread.deleteLater()

        # QTimer in the main thread — its start() slot is auto-QueuedConnection
        # when invoked from worker thread, so _cleanup always runs on main thread.
        self._cleanup_timer = QTimer()
        self._cleanup_timer.setSingleShot(True)
        self._cleanup_timer.setInterval(0)
        self._cleanup_timer.timeout.connect(_cleanup)

        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(progress_dialog.set_status)
        self.worker.finished.connect(self.thread.quit)
        self.worker.error.connect(_store_error)
        self.worker.error.connect(self.thread.quit)
        self.thread.finished.connect(self._cleanup_timer.start)

        self.thread.start()
