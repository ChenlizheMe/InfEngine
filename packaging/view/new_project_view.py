"""Infernux Hub 'Create New Project' dialog."""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QFileDialog, QComboBox, QFrame,
)
from PySide6.QtCore import Qt, QSettings

from model.new_project_model import NewProjectModel
from viewmodel.new_project_viewmodel import NewProjectViewModel
from hub_utils import is_frozen
from i18n import tr


class NewProjectView(QDialog):
    SETTINGS_GROUP = "NewProjectDialog"
    LAST_PATH_KEY = "lastProjectPath"

    def __init__(self, version_manager=None, runtime_manager=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Create New Project"))
        self.setObjectName("newProjectDialog")
        self.setMinimumWidth(560)
        self.setModal(True)
        self._version_manager = version_manager
        self._has_installed_versions = False

        self.settings = QSettings("InfernuxEngine", "InfernuxEngine")

        # MVVM setup
        self.model = NewProjectModel()
        self.viewmodel = NewProjectViewModel(self.model)

        layout = QVBoxLayout(self)
        layout.setSpacing(20)
        layout.setContentsMargins(28, 26, 28, 24)

        header = QVBoxLayout()
        header.setSpacing(4)
        title = QLabel(tr("Create New Project"))
        title.setObjectName("dialogTitle")
        header.addWidget(title)
        subtitle = QLabel(tr("Set the project name, location and Infernux version."))
        subtitle.setObjectName("dialogSubtitle")
        header.addWidget(subtitle)
        layout.addLayout(header)

        form_card = QFrame()
        form_card.setObjectName("newProjectForm")
        form_layout = QVBoxLayout(form_card)
        form_layout.setContentsMargins(20, 18, 20, 20)
        form_layout.setSpacing(16)

        # Project name input
        name_layout = QVBoxLayout()
        name_layout.setSpacing(7)
        name_label = QLabel(tr("Project Name:"))
        name_label.setObjectName("fieldLabel")
        self.name_edit = QLineEdit()
        self.name_edit.setFixedHeight(38)
        self.name_edit.setPlaceholderText(tr("Enter a name for your project"))
        self.name_edit.textChanged.connect(self.viewmodel.set_name)
        self.name_edit.textChanged.connect(self._update_create_button_state)
        name_layout.addWidget(name_label)
        name_layout.addWidget(self.name_edit)
        form_layout.addLayout(name_layout)

        # Path chooser
        path_layout = QVBoxLayout()
        path_layout.setSpacing(7)
        path_label = QLabel(tr("Project Location:"))
        path_label.setObjectName("fieldLabel")
        chooser_row = QHBoxLayout()
        chooser_row.setSpacing(8)
        self.path_edit = QLineEdit()
        self.path_edit.setFixedHeight(38)
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText(tr("No path selected"))
        path_button = QPushButton(tr("Browse..."))
        path_button.setObjectName("normalBtn")
        path_button.setFixedSize(92, 38)
        path_button.clicked.connect(self._on_choose_path)
        chooser_row.addWidget(self.path_edit)
        chooser_row.addWidget(path_button)
        path_layout.addWidget(path_label)
        path_layout.addLayout(chooser_row)
        form_layout.addLayout(path_layout)

        # Load last path if available
        last_path = self.settings.value(f"{self.SETTINGS_GROUP}/{self.LAST_PATH_KEY}", "")
        if last_path:
            self.path_edit.setText(last_path)
            self.viewmodel.set_path(last_path)

        # Engine version selector
        ver_layout = QVBoxLayout()
        ver_layout.setSpacing(7)
        ver_label = QLabel(tr("Engine Version:"))
        ver_label.setObjectName("fieldLabel")
        self.version_combo = QComboBox()
        self.version_combo.setFixedHeight(38)
        self._no_version_hint = QLabel(tr(
            "No engine versions installed. Go to the Installs tab to download one first."
        ))
        self._no_version_hint.setWordWrap(True)
        self._no_version_hint.setVisible(False)
        self._no_version_hint.setObjectName("dialogErrorHint")
        ver_layout.addWidget(ver_label)
        ver_layout.addWidget(self.version_combo)
        ver_layout.addWidget(self._no_version_hint)
        form_layout.addLayout(ver_layout)
        layout.addWidget(form_card)
        self.version_combo.currentIndexChanged.connect(self._update_create_button_state)

        # Button row
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)
        btn_layout.addStretch()

        btn_cancel = QPushButton(tr("Cancel"))
        btn_cancel.setObjectName("normalBtn")
        btn_cancel.setFixedSize(88, 36)
        btn_cancel.clicked.connect(self.reject)

        btn_create = QPushButton(tr("Create"))
        btn_create.setObjectName("primaryBtn")
        btn_create.setFixedSize(88, 36)
        btn_create.clicked.connect(self.accept)
        self._create_btn = btn_create

        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_create)
        layout.addLayout(btn_layout)

        # Populate versions AFTER the create button exists
        self._populate_versions()

    def _populate_versions(self):
        """Fill the version combo box."""
        self.version_combo.clear()
        installed: list[str] = []
        dev_mode = not is_frozen()
        if self._version_manager is not None:
            installed = self._version_manager.installed_versions()
            self._has_installed_versions = bool(installed)
            if installed:
                for v in installed:
                    self.version_combo.addItem(v, v)
            elif not dev_mode:
                self.version_combo.addItem("(no versions installed)", "")

        if dev_mode:
            # Dev mode: add a "dev (current env)" option at the top
            self.version_combo.insertItem(0, tr("dev (current environment)"), "")
            self.version_combo.setCurrentIndex(0)

        self._update_create_button_state()

    def _has_selected_version(self) -> bool:
        if not is_frozen():
            return self.version_combo.currentIndex() >= 0
        return bool(self.version_combo.currentData())

    def _update_create_button_state(self):
        has_version = self._has_selected_version()
        if is_frozen():
            self._no_version_hint.setText(tr(
                "No engine versions installed. Go to the Installs tab to download one first."
                if not self._has_installed_versions
                else "Select an installed engine version before creating a project."
            ))
            self._no_version_hint.setVisible(not has_version)
        else:
            self._no_version_hint.setVisible(False)
        self._create_btn.setEnabled(self.viewmodel.is_valid() and has_version)

    def _on_choose_path(self):
        current_path = self.path_edit.text() or ""
        folder = QFileDialog.getExistingDirectory(self, tr("Choose Project Location"), current_path)
        if folder:
            self.path_edit.setText(folder)
            self.viewmodel.set_path(folder)
            self.settings.setValue(f"{self.SETTINGS_GROUP}/{self.LAST_PATH_KEY}", folder)
            self._update_create_button_state()

    def accept(self):
        self._update_create_button_state()
        if not self._create_btn.isEnabled():
            return
        super().accept()

    def get_data(self):
        name, path = self.viewmodel.get_data()
        version = self.version_combo.currentData() or ""
        return name, path, version
