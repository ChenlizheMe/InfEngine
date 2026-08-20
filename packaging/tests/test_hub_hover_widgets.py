from PySide6.QtCore import QEvent, QPointF
from PySide6.QtGui import QEnterEvent
from PySide6.QtWidgets import QApplication, QLineEdit
from PySide6.QtTest import QTest

from view.hover_widgets import (
    AnimatedSurfaceFrame,
    HoverAnimationFilter,
    ensure_hover_animation_filter,
    release_hover_animation_filter,
)
from view.sidebar_view import SidebarView


def _app():
    return QApplication.instance() or QApplication([])


def _send_hover_enter(widget):
    center = QPointF(widget.rect().center())
    QApplication.sendEvent(widget, QEnterEvent(center, center, center))


def test_surface_hover_and_selection_are_animated():
    _app()
    frame = AnimatedSurfaceFrame("projectCard")
    frame.resize(300, 72)
    frame.show()

    frame._animate_hover(1.0)
    frame.set_selected_animated(True)
    QTest.qWait(260)

    assert frame.hoverProgress > 0.95
    assert frame.selectionProgress > 0.95


def test_search_hover_uses_the_global_transition_filter():
    app = _app()
    ensure_hover_animation_filter(app)
    search = QLineEdit()
    search.resize(260, 36)
    search.show()

    _send_hover_enter(search)
    QTest.qWait(220)

    assert getattr(search, "_hub_hover_progress", 0.0) > 0.95


def test_hover_filter_is_singleton_owned_by_the_live_qt_application():
    app = _app()

    first = ensure_hover_animation_filter(app)
    second = ensure_hover_animation_filter(app)

    assert first is second
    assert first.parent() is app
    assert app.findChild(HoverAnimationFilter, "infernuxHubHoverAnimationFilter") is first
    assert first._installed is True

    release_hover_animation_filter(app)
    assert first._installed is False

    # Re-acquiring after an explicit host/test teardown restores the same
    # owned filter and registers it exactly once again.
    restored = ensure_hover_animation_filter(app)
    assert restored is first
    assert restored._installed is True


def test_sidebar_switch_clears_stale_hover_style():
    _app()
    sidebar = SidebarView()
    old_button, new_button = sidebar._nav_buttons[:2]
    old_button._hub_hover_progress = 1.0
    old_button.setStyleSheet("background-color: #333333;")

    sidebar._switch_page(1)

    assert old_button.styleSheet() == ""
    assert old_button._hub_hover_progress == 0.0
    assert old_button.property("active") is False
    assert new_button.property("active") is True


def test_sidebar_leave_restores_transparent_qss_state():
    app = _app()
    ensure_hover_animation_filter(app)
    sidebar = SidebarView()
    sidebar.resize(232, 720)
    sidebar.show()
    button = sidebar._nav_buttons[1]

    _send_hover_enter(button)
    QTest.qWait(220)
    assert button.styleSheet() != ""

    QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
    QTest.qWait(220)

    assert button._hub_hover_progress == 0.0
    assert button.styleSheet() == ""
