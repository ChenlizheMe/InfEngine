"""Early Hub settings page."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from about_content import ABOUT_DESCRIPTION, ABOUT_TITLE
from hub_updater import current_hub_version
from i18n import current_language, detect_system_locale, tr
from plugin_library import inspect_plugin_library, prune_unreferenced_packages
from hub_utils import get_hub_shared_data_dir
from shared_storage_migration import inspect_legacy_storage
from view.storage_migration_dialog import StorageMigrationDialog
from view.sidebar_view import ToggleSwitch, apply_theme
from view.hover_widgets import AnimatedSurfaceFrame


class SettingsView(QWidget):
    update_check_requested = Signal()
    language_changed = Signal(str)

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self._db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        title = QLabel(tr("Settings"))
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        subtitle = QLabel(tr("Hub preferences, updates and project-independent information."))
        subtitle.setObjectName("pageSubtitle")
        layout.addWidget(subtitle)

        card = AnimatedSurfaceFrame("settingsCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(12)

        row = QHBoxLayout()
        label_block = QVBoxLayout()
        language_label = QLabel(tr("Language"))
        language_label.setObjectName("settingsLabel")
        label_block.addWidget(language_label)
        detected = QLabel(
            f"{tr('Current language')}: {'中文' if current_language() == 'zh' else 'English'} "
            f"({detect_system_locale()})"
        )
        detected.setObjectName("settingsDescription")
        label_block.addWidget(detected)
        row.addLayout(label_block, 1)

        self.language_combo = QComboBox()
        self.language_combo.addItem(tr("System"), "system")
        self.language_combo.addItem(tr("Chinese"), "zh")
        self.language_combo.addItem(tr("English"), "en")
        saved = self._db.get_setting("language", "system") if self._db else "system"
        index = self.language_combo.findData(saved)
        self.language_combo.setCurrentIndex(max(index, 0))
        self.language_combo.currentIndexChanged.connect(self._save_language)
        row.addWidget(self.language_combo)
        card_layout.addLayout(row)

        hint = QLabel(tr("Language changes apply immediately."))
        hint.setObjectName("settingsDescription")
        card_layout.addWidget(hint)
        layout.addWidget(card)

        appearance_card = AnimatedSurfaceFrame("settingsCard")
        appearance_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        appearance_layout = QHBoxLayout(appearance_card)
        appearance_layout.setContentsMargins(20, 16, 20, 16)
        appearance_text = QVBoxLayout()
        appearance_label = QLabel(tr("Appearance"))
        appearance_label.setObjectName("settingsLabel")
        appearance_text.addWidget(appearance_label)
        appearance_hint = QLabel(tr("Switch between the neutral dark and light Hub themes."))
        appearance_hint.setObjectName("settingsDescription")
        appearance_text.addWidget(appearance_hint)
        appearance_layout.addLayout(appearance_text, 1)
        self.theme_toggle = ToggleSwitch()
        self.theme_toggle.setChecked(bool(getattr(QApplication.instance(), "is_dark_theme", True)))
        self.theme_toggle.stateChanged.connect(self._toggle_theme)
        appearance_layout.addWidget(self.theme_toggle)
        layout.addWidget(appearance_card)

        storage_card = AnimatedSurfaceFrame("settingsCard")
        storage_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        storage_layout = QHBoxLayout(storage_card)
        storage_layout.setContentsMargins(20, 18, 20, 18)
        storage_text = QVBoxLayout()
        storage_label = QLabel(tr("Plugin Library"))
        storage_label.setObjectName("settingsLabel")
        storage_text.addWidget(storage_label)
        self.storage_description = QLabel()
        self.storage_description.setObjectName("settingsDescription")
        self.storage_description.setWordWrap(True)
        self.storage_description.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        storage_text.addWidget(self.storage_description)
        storage_layout.addLayout(storage_text, 1)
        self.clean_plugins_button = QPushButton(tr("Clean Unused Packages"))
        self.clean_plugins_button.setObjectName("normalBtn")
        self.clean_plugins_button.setFixedHeight(34)
        self.clean_plugins_button.clicked.connect(self._clean_plugin_library)
        storage_layout.addWidget(self.clean_plugins_button)
        layout.addWidget(storage_card)
        self._refresh_plugin_library()

        migration_card = AnimatedSurfaceFrame("settingsCard")
        migration_layout = QVBoxLayout(migration_card)
        migration_layout.setContentsMargins(20, 18, 20, 18)
        shared_path = QLabel(tr("Shared resources: {path}", path=get_hub_shared_data_dir()))
        shared_path.setWordWrap(True)
        shared_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        migration_layout.addWidget(shared_path)
        self.migrate_storage_button = QPushButton(tr("Migrate Legacy Resources"))
        self.migrate_storage_button.setObjectName("normalBtn")
        self.migrate_storage_button.setFixedHeight(34)
        self.migrate_storage_button.clicked.connect(self._migrate_legacy_storage)
        migration_layout.addWidget(self.migrate_storage_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(migration_card)

        update_card = AnimatedSurfaceFrame("settingsCard")
        update_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        update_layout = QHBoxLayout(update_card)
        update_layout.setContentsMargins(20, 18, 20, 18)
        update_text = QVBoxLayout()
        update_label = QLabel(tr("Hub Update"))
        update_label.setObjectName("settingsLabel")
        update_text.addWidget(update_label)
        update_description = QLabel(tr("Check the Infernux release catalog for a Hub update."))
        update_description.setObjectName("settingsDescription")
        update_text.addWidget(update_description)
        update_layout.addLayout(update_text, 1)
        update_button = QPushButton(tr("Check for Updates"))
        update_button.setObjectName("normalBtn")
        update_button.setFixedHeight(34)
        update_button.setMinimumWidth(118)
        update_button.clicked.connect(self.update_check_requested)
        update_layout.addWidget(update_button)
        layout.addWidget(update_card)

        about_card = AnimatedSurfaceFrame("settingsCard")
        about_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        about_layout = QVBoxLayout(about_card)
        about_layout.setContentsMargins(20, 18, 20, 18)
        about_layout.setSpacing(6)
        about_title = QLabel(tr(ABOUT_TITLE))
        about_title.setObjectName("settingsLabel")
        about_layout.addWidget(about_title)
        about_text = QLabel(tr(ABOUT_DESCRIPTION))
        about_text.setObjectName("settingsDescription")
        about_text.setWordWrap(True)
        about_layout.addWidget(about_text)
        version = QLabel(tr("Hub version: {version}", version=current_hub_version()))
        version.setObjectName("settingsDescription")
        about_layout.addWidget(version)
        layout.addWidget(about_card)
        layout.addStretch()

    def _save_language(self):
        if not self._db:
            return
        mode = self.language_combo.currentData()
        if mode == self._db.get_setting("language", "system"):
            return
        self._db.set_setting("language", mode)
        from i18n import configure_language
        configure_language(mode)
        self.language_changed.emit(mode)

    def refresh(self):
        """Refresh state owned outside the Hub process."""

        self._refresh_plugin_library()

    def _toggle_theme(self, state: int):
        if self._db:
            self._db.set_setting("theme", "dark" if state else "light")
        apply_theme(self.window(), bool(state))

    def _project_roots(self) -> tuple[str, ...]:
        if not self._db:
            return ()
        return tuple(record.path for record in self._db.all_projects())

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, int(value)))
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        unit = units[0]
        for unit in units:
            if amount < 1024.0 or unit == units[-1]:
                break
            amount /= 1024.0
        return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"

    def _refresh_plugin_library(self):
        try:
            stats = inspect_plugin_library(self._project_roots())
        except (OSError, RuntimeError, ValueError) as exc:
            self.storage_description.setText(
                tr("Plugin library cleanup is unavailable: {message}", message=str(exc))
            )
            self.clean_plugins_button.setEnabled(False)
            return
        self.storage_description.setText(
            tr(
                "{count} packages · {size} · {path}",
                count=stats.package_count,
                size=self._format_bytes(stats.total_bytes),
                path=str(stats.root),
            )
        )
        self.clean_plugins_button.setEnabled(bool(stats.removable))
        self.clean_plugins_button.setToolTip(
            tr(
                "{count} unused packages can release {size}.",
                count=len(stats.removable),
                size=self._format_bytes(stats.removable_bytes),
            )
            if stats.removable
            else tr("Every downloaded package is still referenced by a Hub project.")
        )

    def _clean_plugin_library(self):
        try:
            before = inspect_plugin_library(self._project_roots())
        except (OSError, RuntimeError, ValueError) as exc:
            QMessageBox.critical(self, tr("Plugin Library"), str(exc))
            self._refresh_plugin_library()
            return
        if not before.removable:
            self._refresh_plugin_library()
            return
        answer = QMessageBox.question(
            self,
            tr("Clean Unused Packages"),
            tr(
                "Delete {count} unreferenced plugin packages and release {size}?",
                count=len(before.removable),
                size=self._format_bytes(before.removable_bytes),
            ),
        )
        if answer != QMessageBox.Yes:
            return
        try:
            prune_unreferenced_packages(self._project_roots())
        except (OSError, RuntimeError, ValueError) as exc:
            QMessageBox.critical(self, tr("Plugin Library"), str(exc))
        self._refresh_plugin_library()

    def _migrate_legacy_storage(self):
        try:
            plan = inspect_legacy_storage()
        except (OSError, RuntimeError, ValueError) as exc:
            QMessageBox.critical(self, tr("Migrate Legacy Resources"), str(exc))
            return
        preview = QMessageBox(self)
        preview.setWindowTitle(tr("Migrate Legacy Resources"))
        preview.setText(tr(
            "Move {count} complete resources from {source} to {destination}?\n"
            "Close all Editors, builds and downloads first. Existing targets ({conflicts}) "
            "will be skipped and retained at the old location. Projects, settings and "
            "unfinished downloads are not moved. See details for the exact list.",
            count=len(plan.items), source=str(plan.source), destination=str(plan.destination),
            conflicts=len(plan.conflicts),
        ))
        preview.setDetailedText(
            tr("Move:") + "\n" + "\n".join(path.as_posix() for path in plan.items)
            + "\n\n" + tr("Keep at old location (target exists):") + "\n"
            + "\n".join(path.as_posix() for path in plan.conflicts)
        )
        preview.setStandardButtons(
            QMessageBox.Yes | QMessageBox.No if plan.items else QMessageBox.Ok
        )
        preview.setDefaultButton(QMessageBox.No if plan.items else QMessageBox.Ok)
        if preview.exec() != QMessageBox.Yes or not plan.items:
            return
        dialog = StorageMigrationDialog(plan, self._project_roots(), self)
        dialog.exec()
        if dialog.worker.error:
            QMessageBox.critical(self, tr("Migrate Legacy Resources"), dialog.worker.error)
        else:
            QMessageBox.information(self, tr("Migrate Legacy Resources"), tr(
                "Moved {count} resources. {conflicts} existing targets were skipped; "
                "their old copies have not been deleted.",
                count=len(dialog.worker.result), conflicts=len(plan.conflicts),
            ))
        self._refresh_plugin_library()


__all__ = ["SettingsView"]
