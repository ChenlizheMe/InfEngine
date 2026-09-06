import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea

from i18n import configure_language, language_mode
from style import StyleManager
from view.settings_view import SettingsView


@pytest.mark.parametrize("dark", [True, False])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_settings_scroll_without_squeezing_text(tmp_path, monkeypatch, dark, language):
    monkeypatch.setenv("INFERNUX_DATA_ROOT", str(tmp_path / "User"))
    monkeypatch.setenv("INFERNUX_SHARED_DATA_ROOT", str(tmp_path / "Long Hub directory" / "Shared"))
    app = QApplication.instance() or QApplication([])
    previous_style = app.styleSheet()
    previous_language = language_mode()
    configure_language(language)
    app.setStyleSheet(StyleManager.get_stylesheet(dark))
    view = SettingsView(None)
    try:
        view.resize(760, 500)
        view.show()
        app.processEvents()
        assert view.height() == 500, "Settings must not force a taller Hub window"
        scroll = view.findChild(QScrollArea)
        assert scroll is not None
        assert scroll.verticalScrollBar().maximum() > 0
        assert scroll.horizontalScrollBar().maximum() == 0, "\n".join(
            f"{label.text()}: min={label.minimumSizeHint().width()} actual={label.width()} font={label.font().toString()}"
            for label in view.findChildren(QLabel)
        )
        assert scroll.viewport().palette().color(QPalette.ColorRole.Window).name() == StyleManager.palette(dark).bg_base
        for label in view.findChildren(QLabel):
            if label.wordWrap():
                assert label.height() >= label.heightForWidth(label.width()), label.text()
        scroll.ensureWidgetVisible(view.migrate_storage_button)
        app.processEvents()
        button = view.migrate_storage_button
        point = button.mapTo(scroll.viewport(), QPoint(0, 0))
        assert point.y() >= 0
        assert point.y() + button.height() <= scroll.viewport().height()
    finally:
        view.close()
        app.setStyleSheet(previous_style)
        configure_language(previous_language)
