"""Early Hub settings page."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from about_content import ABOUT_DESCRIPTION, ABOUT_TITLE
from hub_updater import current_hub_version
from i18n import current_language, detect_system_locale, tr
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

        update_card = AnimatedSurfaceFrame("settingsCard")
        update_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        update_layout = QHBoxLayout(update_card)
        update_layout.setContentsMargins(20, 18, 20, 18)
        update_text = QVBoxLayout()
        update_label = QLabel(tr("Hub Update"))
        update_label.setObjectName("settingsLabel")
        update_text.addWidget(update_label)
        update_description = QLabel(tr("Check GitHub Releases for a verified Hub update."))
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

    def _toggle_theme(self, state: int):
        if self._db:
            self._db.set_setting("theme", "dark" if state else "light")
        apply_theme(self.window(), bool(state))


__all__ = ["SettingsView"]
