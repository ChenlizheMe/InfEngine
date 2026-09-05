"""Left sidebar navigation for Infernux Hub."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QApplication,
    QGraphicsOpacityEffect, QComboBox, QLineEdit,
)
from PySide6.QtCore import Signal, Qt, QPropertyAnimation, Property, QEasingCurve
from PySide6.QtGui import QPainter, QColor, QBrush, QPaintEvent, QPixmap

from style import StyleManager
from i18n import tr
from hub_resources import ICON_PATH


def apply_theme(window, is_dark: bool) -> None:
    """Apply the one global Hub theme used by the Settings page."""
    app = QApplication.instance()
    if app is None or getattr(app, "is_dark_theme", True) == is_dark:
        return
    pixmap = window.grab()
    overlay = QLabel(window)
    overlay.setPixmap(pixmap)
    overlay.setGeometry(window.rect())
    overlay.show()
    app.is_dark_theme = is_dark
    app.setStyleSheet(StyleManager.get_stylesheet(is_dark))
    for widget_type in (QPushButton, QComboBox, QLineEdit):
        for widget in window.findChildren(widget_type):
            if hasattr(widget, "_hub_hover_progress"):
                widget._hub_hover_progress = 0.0
                widget.setStyleSheet("")
    app.processEvents()
    effect = QGraphicsOpacityEffect(overlay)
    overlay.setGraphicsEffect(effect)
    animation = QPropertyAnimation(effect, b"opacity", overlay)
    animation.setDuration(180)
    animation.setStartValue(1.0)
    animation.setEndValue(0.0)
    animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
    animation.finished.connect(overlay.deleteLater)
    animation.start()
    window._theme_anim = animation


class ToggleSwitch(QWidget):
    """Compact hardware-like toggle used for the theme setting."""
    stateChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(42, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._checked = True
        self._position = 22.0
        self._anim = QPropertyAnimation(self, b"position")
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._anim.setDuration(200)

    @Property(float)
    def position(self):
        return self._position

    @position.setter
    def position(self, pos):
        self._position = pos
        self.update()

    def isChecked(self):
        return self._checked

    def setChecked(self, checked: bool):
        self._checked = checked
        self._position = 22.0 if checked else 2.0
        self.update()

    def mousePressEvent(self, ev):
        self._checked = not self._checked
        self._anim.stop()
        self._anim.setEndValue(22.0 if self._checked else 2.0)
        self._anim.start()
        self.stateChanged.emit(int(self._checked))

    def paintEvent(self, e: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)

        app = QApplication.instance()
        is_dark = getattr(app, "is_dark_theme", True)
        palette = StyleManager.palette(is_dark)

        if self._checked:
            bg_color = QColor(palette.accent)
            thumb_color = QColor(palette.accent_text)
        else:
            bg_color = QColor(palette.border_hover if is_dark else palette.bg_surface_hover)
            thumb_color = QColor(palette.border if is_dark else palette.accent_text)

        p.setBrush(QBrush(bg_color))
        p.drawRect(0, 0, self.width(), self.height())

        p.setBrush(QBrush(thumb_color))
        p.drawRect(int(self._position), 2, 18, 18)
        p.end()


class SidebarView(QWidget):
    """Infernux Hub sidebar with page navigation."""

    page_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(232)
        self.setObjectName("sidebar")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Title ────────────────────────────────────────────────────
        title_container = QWidget()
        title_container.setObjectName("sidebarHeader")
        title_layout = QHBoxLayout(title_container)
        title_layout.setContentsMargins(20, 24, 20, 20)
        title_layout.setSpacing(10)

        logo = QLabel()
        logo.setObjectName("sidebarLogo")
        logo.setFixedSize(38, 38)
        pixmap = QPixmap(ICON_PATH)
        if not pixmap.isNull():
            logo.setPixmap(
                pixmap.scaled(
                    38, 38,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        title_layout.addWidget(logo)

        brand_text = QVBoxLayout()
        brand_text.setSpacing(1)

        title = QLabel("Infernux")
        title.setObjectName("sidebarTitle")
        brand_text.addWidget(title)

        subtitle = QLabel(tr("Hub"))
        subtitle.setObjectName("sidebarSubtitle")
        brand_text.addWidget(subtitle)
        title_layout.addLayout(brand_text, 1)

        layout.addWidget(title_container)

        # ── Navigation ───────────────────────────────────────────────
        self._nav_buttons: list[QPushButton] = []

        for label, index in [
            (tr("Projects"), 0),
            (tr("Installs"), 1),
            (tr("Settings"), 2),
            (tr("Community"), 3),
        ]:
            btn = QPushButton(label)
            btn.setObjectName("navItem")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(46)
            btn.setProperty("active", index == 0)
            btn.setProperty("page", index)
            btn.clicked.connect(lambda _checked, i=index: self._switch_page(i))
            layout.addWidget(btn)
            self._nav_buttons.append(btn)

        layout.addStretch()

    # ── Internal ─────────────────────────────────────────────────────

    def _switch_page(self, index: int):
        for i, btn in enumerate(self._nav_buttons):
            animation = getattr(btn, "_hub_hover_animation", None)
            if animation is not None:
                animation.stop()
            btn._hub_hover_progress = 0.0
            btn.setStyleSheet("")
            btn.setProperty("active", btn.property("page") == index)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self.page_changed.emit(index)

    def select_page(self, index: int) -> None:
        self._switch_page(index)

    def _toggle_theme(self, state):
        apply_theme(self.window(), bool(state))
