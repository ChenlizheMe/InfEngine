from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QScrollArea, QWidget

from style import StyleManager
from view.installs_view import _configure_install_scroll_area


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
