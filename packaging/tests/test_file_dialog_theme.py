import pytest

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QFileDialog, QTreeView

from style import StyleManager


@pytest.mark.parametrize("dark", [True, False])
def test_qt_file_dialog_list_uses_the_same_theme_as_its_text(tmp_path, dark):
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(StyleManager.get_stylesheet(dark))
    dialog = QFileDialog(directory=str(tmp_path))
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
    dialog.show()
    app.processEvents()
    try:
        view = dialog.findChild(QTreeView, "treeView")
        colors = view.viewport().palette()
        theme = StyleManager.palette(dark)
        assert colors.color(QPalette.ColorRole.Base).name() == theme.bg_input
        assert colors.color(QPalette.ColorRole.Text).name() == theme.text_primary
        assert colors.color(QPalette.ColorRole.Highlight).name() == theme.accent
        assert colors.color(QPalette.ColorRole.HighlightedText).name() == theme.accent_text
    finally:
        dialog.close()
