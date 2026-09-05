"""Project list pane with compact Unity Hub-inspired rows and folder-open."""

import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
    QSizePolicy, QFrame, QPushButton, QLineEdit, QMenu,
)
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from database import ProjectDatabase
from hub_utils import is_project_open
from i18n import tr
from version_manager import VersionManager
from view.hover_widgets import AnimatedSurfaceFrame


class _ProjectCard(AnimatedSurfaceFrame):
    """A compact project row with identity, path, version and state."""

    def __init__(self, project_id: str, name: str, created_at: str, path: str,
                 version_manager=None, on_remove_requested=None, parent=None,
                 *, on_migrate_requested=None):
        super().__init__("projectCard", parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)
        self.project_name = name
        self.project_id = project_id
        self.project_path = path
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(72)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 10, 8)
        layout.setSpacing(12)

        # --- Text block ---
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.setContentsMargins(0, 0, 0, 0)

        name_label = QLabel(name)
        name_label.setObjectName("cardName")
        text_col.addWidget(name_label)

        path_label = QLabel(path)
        path_label.setObjectName("cardPath")
        path_label.setToolTip(path)
        text_col.addWidget(path_label)

        version = VersionManager.read_project_version(path) if os.path.isdir(path) else ""
        layout.addLayout(text_col, 1)

        if not os.path.isdir(path):
            status_label = QLabel(tr("Project path missing"))
            status_label.setObjectName("projectStatus")
            status_label.setProperty("kind", "error")
            layout.addWidget(
                status_label,
                alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
        else:
            version_label = QLabel(f"Infernux {version}" if version else tr("Unversioned"))
            version_label.setObjectName("projectVersion")
            if version and version_manager is not None and not version_manager.is_installed(version):
                version_label.setProperty("kind", "warning")
                version_label.setToolTip(tr("Engine Version Not Installed"))
            elif is_project_open(path):
                version_label.setProperty("kind", "active")
                version_label.setToolTip(tr("Project Already Open"))
            else:
                version_label.setProperty("kind", "ready")
            layout.addWidget(version_label, alignment=Qt.AlignmentFlag.AlignVCenter)

        # --- Project action menu ---
        open_btn = QPushButton("...")
        open_btn.setObjectName("cardOpenBtn")
        open_btn.setFixedSize(38, 32)
        open_btn.setToolTip(tr("Project actions"))
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._actions_button = open_btn
        self._actions_menu = QMenu(open_btn)
        show_action = self._actions_menu.addAction(tr("Show in Explorer"))
        if on_migrate_requested is not None:
            migrate_action = self._actions_menu.addAction(tr("Migrate Project"))
            migrate_action.triggered.connect(
                lambda _checked=False: on_migrate_requested(project_id)
            )
        remove_action = self._actions_menu.addAction(tr("Remove from Hub"))
        show_action.triggered.connect(
            lambda _checked=False: QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        )
        remove_action.triggered.connect(
            lambda _checked=False: (
                on_remove_requested(project_id)
                if on_remove_requested is not None else None
            )
        )
        open_btn.clicked.connect(self._show_actions_menu)
        layout.addWidget(open_btn)

    def _show_actions_menu(self):
        anchor = self._actions_button.mapToGlobal(self._actions_button.rect().bottomLeft())
        self._actions_menu.exec(anchor)

    # --- Selection state ---
    def set_selected(self, selected: bool):
        self.setProperty("selected", selected)
        self.set_selected_animated(selected)


class ProjectListPane(QWidget):
    """Scrollable list of project cards with a search bar."""

    remove_requested = Signal(str)
    migrate_requested = Signal(str)

    def __init__(self, db: ProjectDatabase, version_manager=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.version_manager = version_manager
        self.selected_project_id = None
        self.project_cards: dict[str, _ProjectCard] = {}
        self._all_projects = []

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(10)

        # --- Search bar ---
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("searchBox")
        self.search_edit.setPlaceholderText(tr("Search projects..."))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedHeight(36)
        self.search_edit.textChanged.connect(self._apply_filter)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.addStretch()
        self.search_edit.setFixedWidth(300)
        search_row.addWidget(self.search_edit)
        main_layout.addLayout(search_row)

        # --- Scrollable card area ---
        scroll_area = QScrollArea()
        scroll_area.setObjectName("projectScrollArea")
        scroll_area.viewport().setObjectName("projectViewport")
        scroll_area.setWidgetResizable(True)
        main_layout.addWidget(scroll_area)

        self.container = QWidget()
        self.container.setObjectName("projectListContainer")
        self.card_layout = QVBoxLayout(self.container)
        self.card_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.card_layout.setSpacing(6)
        self.card_layout.setContentsMargins(0, 0, 4, 0)
        scroll_area.setWidget(self.container)

        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self):
        previous_selection = self.selected_project_id
        self.project_cards.clear()
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        self._all_projects = self.db.all_projects()
        for record in self._all_projects:
            card = _ProjectCard(
                record.project_id, record.name, record.created_at, record.path,
                self.version_manager, self.remove_requested.emit,
                on_migrate_requested=self.migrate_requested.emit,
            )
            card.mousePressEvent = lambda _ev, pid=record.project_id: self._on_select(pid)
            card.mouseDoubleClickEvent = lambda _ev, pid=record.project_id: self._on_double_click(pid)
            self.card_layout.addWidget(card)
            self.project_cards[record.project_id] = card

        if not self._all_projects:
            empty = QFrame()
            empty.setObjectName("emptyState")
            empty_layout = QVBoxLayout(empty)
            empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_title = QLabel(tr("No projects yet"))
            empty_title.setObjectName("emptyTitle")
            empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_detail = QLabel(tr("Create a new project or open an existing project to get started."))
            empty_detail.setObjectName("emptyHint")
            empty_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_layout.addWidget(empty_title)
            empty_layout.addWidget(empty_detail)
            self.card_layout.addWidget(empty)
        self.card_layout.addStretch()
        self._apply_filter(self.search_edit.text())
        if previous_selection in self.project_cards:
            self._on_select(previous_selection)
        else:
            self.selected_project_id = None

    # ------------------------------------------------------------------
    def _apply_filter(self, text: str):
        needle = text.strip().lower()
        for card in self.project_cards.values():
            searchable = f"{card.project_name} {card.project_path}".lower()
            card.setVisible(needle in searchable if needle else True)

    def _on_select(self, project_id: str):
        self.selected_project_id = project_id
        for candidate_id, card in self.project_cards.items():
            card.set_selected(candidate_id == project_id)

    def _on_double_click(self, project_id: str):
        """Select + auto-launch on double-click (handled by parent)."""
        self._on_select(project_id)

    # ------------------------------------------------------------------
    # Public API (unchanged)
    # ------------------------------------------------------------------
    def get_selected_project(self):
        record = self.get_selected_record()
        return record.name if record else None

    def get_selected_project_id(self):
        return self.selected_project_id

    def get_selected_record(self):
        if self.selected_project_id:
            return self.db.get_project(self.selected_project_id)
        return None

    def get_selected_project_path(self):
        record = self.get_selected_record()
        return record.path if record else None

    def select_project(self, project_id: str):
        if project_id in self.project_cards:
            self._on_select(project_id)
