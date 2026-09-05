from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea, QWidget

from android_support import AndroidSupportManager
from style import StyleManager
from view.installs_view import _AndroidSupportCard, _configure_install_scroll_area


def _app():
    return QApplication.instance() or QApplication([])


def test_install_dialog_scroll_surface_uses_dark_hub_palette():
    app = _app()
    scroll = QScrollArea()
    container = QWidget()
    _configure_install_scroll_area(scroll, container)
    scroll.setWidget(container)

    app.setStyleSheet(StyleManager.get_stylesheet(True))
    scroll.show()
    app.processEvents()

    assert scroll.objectName() == "installScrollArea"
    assert scroll.viewport().objectName() == "installViewport"
    assert container.objectName() == "installListContainer"
    assert scroll.viewport().palette().color(QPalette.ColorRole.Window).name() == "#191919"
    assert container.palette().color(QPalette.ColorRole.Window).name() == "#191919"

    scroll.close()


def test_common_message_boxes_use_shared_readable_metrics():
    stylesheet = StyleManager.get_stylesheet(True)

    assert "QMessageBox QLabel" in stylesheet
    assert "font-size: 15px" in stylesheet
    assert "min-width: 360px" in stylesheet
    assert "QMessageBox QPushButton" in stylesheet
    assert "min-height: 34px" in stylesheet


def test_android_support_card_exposes_install_before_any_project_plugin(
    tmp_path,
):
    _app()
    card = _AndroidSupportCard(AndroidSupportManager(tmp_path / "missing"))

    labels = [label.text() for label in card.findChildren(QLabel)]
    buttons = [button.text() for button in card.findChildren(QPushButton)]
    assert "Android compatibility" in labels
    assert any("Required before" in label for label in labels)
    assert buttons == ["Install"]

    card.close()
