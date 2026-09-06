from types import SimpleNamespace

import pytest
from PySide6.QtGui import QCloseEvent, QPalette, QCursor, QEnterEvent
from PySide6.QtCore import QPoint, QPointF, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QScrollArea, QWidget

from install_queue import InstallQueue
from launcher import GameEngineLauncher
from style import StyleManager
from view.install_queue_panel import InstallQueuePanel
from view.sidebar_view import SidebarView
from i18n import tr


def test_sidebar_groups_all_installations_under_one_entry():
    app = QApplication.instance() or QApplication([])
    sidebar = SidebarView()
    assert [button.text() for button in sidebar._nav_buttons] == [
        tr("Projects"), tr("Installs"), tr("Settings"), tr("Community"),
    ]
    selected = []
    sidebar.page_changed.connect(selected.append)
    sidebar._nav_buttons[1].click()
    assert selected == [1]
    assert sidebar._nav_buttons[1].property("active")
    sidebar.close()


def test_required_runtime_opens_install_subpage_before_installing():
    observed = []
    runtime_view = SimpleNamespace(install=lambda version: observed.append(("install", version)))
    window = SimpleNamespace(
        python_view=runtime_view,
        install_tabs=SimpleNamespace(setCurrentWidget=lambda widget: observed.append(("tab", widget))),
        sidebar=SimpleNamespace(select_page=lambda index: observed.append(("page", index))),
    )
    window._show_runtime_installs = lambda: GameEngineLauncher._show_runtime_installs(window)
    GameEngineLauncher._install_required_runtime(window, "3.13")
    assert observed == [("tab", runtime_view), ("page", 1), ("install", "3.13")]


@pytest.mark.parametrize("tray_available", [False, True])
def test_close_keeps_active_installation_reachable(tray_available):
    app = QApplication.instance() or QApplication([])
    window = GameEngineLauncher.__new__(GameEngineLauncher)
    QMainWindow.__init__(window)
    window.tray = SimpleNamespace(isVisible=lambda: tray_available)
    window.install_queue = SimpleNamespace(busy=True)
    window.show()
    event = QCloseEvent()
    GameEngineLauncher.closeEvent(window, event)
    assert not event.isAccepted()
    if tray_available:
        assert window.isHidden()
    else:
        assert window.isMinimized()
    window._restore_window()
    assert window.isVisible() and not window.isMinimized()
    window.install_queue.busy = False
    window.tray.isVisible = lambda: False
    window.close()


@pytest.mark.parametrize("dark", [False, True])
def test_queue_scroll_palette_tracks_hub_theme(dark, monkeypatch):
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(StyleManager.get_stylesheet(dark))
    queue = InstallQueue(app)
    job = queue.submit("fixture", "Fixture", lambda report: None)
    queue.cancel_queued(job)
    container = QWidget()
    container.resize(500, 450)
    panel = InstallQueuePanel(queue, container)
    panel.setFixedWidth(320)
    panel.move(20, 380)
    container.show()
    panel.show()
    cursor = panel.mapToGlobal(QPoint(-100, -100))
    monkeypatch.setattr(QCursor, "pos", lambda: cursor)
    assert panel.height() == 40
    cursor = panel.mapToGlobal(QPoint(15, 20))
    app.sendEvent(panel, QEnterEvent(QPointF(15, 20), QPointF(15, 20), QPointF(cursor)))
    QTest.qWait(50)
    assert panel._details.isVisible()
    assert panel.height() == 40
    app.processEvents()
    cursor = panel._details.mapToGlobal(QPoint(15, 20))
    app.sendEvent(panel, QEvent(QEvent.Type.Leave))
    app.sendEvent(panel._details, QEnterEvent(QPointF(15, 20), QPointF(15, 20), QPointF(cursor)))
    QTest.qWait(200)
    assert panel._details.isVisible()
    scroll = panel._details.findChild(QScrollArea)
    assert scroll.viewport().palette().color(QPalette.ColorRole.Window).name() == StyleManager.palette(dark).bg_base
    cursor = panel.mapToGlobal(QPoint(-100, -100))
    app.sendEvent(panel._details, QEvent(QEvent.Type.Leave))
    QTest.qWait(200)
    assert panel.height() == 40
    assert not panel._details.isVisible()
    panel.close()
    container.close()
