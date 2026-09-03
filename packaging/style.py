"""Centralized product theme for Infernux Hub."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThemePalette:
    bg_base: str
    bg_surface: str
    bg_surface_hover: str
    bg_surface_selected: str
    bg_input: str
    text_primary: str
    text_secondary: str
    text_muted: str
    border: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str
    danger: str
    sidebar_bg: str
    sidebar_border: str
    nav_hover: str
    nav_active: str
    border_hover: str
    button_surface: str
    button_hover: str
    button_pressed: str
    disabled_surface: str
    disabled_text: str


_DARK_PALETTE = ThemePalette(
    bg_base="#191919",
    bg_surface="#252525",
    bg_surface_hover="#2d2d2d",
    bg_surface_selected="#353535",
    bg_input="#191919",
    text_primary="#f2f2f2",
    text_secondary="#bdbdbd",
    text_muted="#8f8f8f",
    border="#363636",
    accent="#eb5757",
    accent_hover="#f26a6a",
    accent_pressed="#b83f3f",
    accent_text="#ffffff",
    danger="#eb5757",
    sidebar_bg="#232323",
    sidebar_border="#303030",
    nav_hover="#2c2c2c",
    nav_active="#414141",
    border_hover="#4a4a4a",
    button_surface="#3a3a3a",
    button_hover="#454545",
    button_pressed="#303030",
    disabled_surface="#212121",
    disabled_text="#666666",
)

_LIGHT_PALETTE = ThemePalette(
    bg_base="#eeeeee",
    bg_surface="#f2f2f2",
    bg_surface_hover="#e6e6e6",
    bg_surface_selected="#dcdcdc",
    bg_input="#f2f2f2",
    text_primary="#202020",
    text_secondary="#5f5f5f",
    text_muted="#858585",
    border="#cfcfcf",
    accent="#eb5757",
    accent_hover="#d83b46",
    accent_pressed="#b8313b",
    accent_text="#ffffff",
    danger="#b8313b",
    sidebar_bg="#e8e8e8",
    sidebar_border="#cfcfcf",
    nav_hover="#dedede",
    nav_active="#d5d5d5",
    border_hover="#999999",
    button_surface="#dddddd",
    button_hover="#d2d2d2",
    button_pressed="#c8c8c8",
    disabled_surface="#e4e4e4",
    disabled_text="#999999",
)

class StyleManager:
    """Provides dynamic QSS stylesheets for light/dark modes."""

    @staticmethod
    def palette(is_dark: bool) -> ThemePalette:
        return _DARK_PALETTE if is_dark else _LIGHT_PALETTE

    @staticmethod
    def get_stylesheet(is_dark: bool) -> str:
        palette = StyleManager.palette(is_dark)
        (
            bg_base,
            bg_surface,
            bg_surface_hover,
            bg_surface_selected,
            bg_input,
            text_primary,
            text_secondary,
            text_muted,
            border,
            accent,
            accent_hover,
            accent_pressed,
            accent_text,
            danger,
            sidebar_bg,
            sidebar_border,
            nav_hover,
            nav_active,
            border_hover,
            button_surface,
            button_hover,
            button_pressed,
            disabled_surface,
            disabled_text,
        ) = (
            palette.bg_base,
            palette.bg_surface,
            palette.bg_surface_hover,
            palette.bg_surface_selected,
            palette.bg_input,
            palette.text_primary,
            palette.text_secondary,
            palette.text_muted,
            palette.border,
            palette.accent,
            palette.accent_hover,
            palette.accent_pressed,
            palette.accent_text,
            palette.danger,
            palette.sidebar_bg,
            palette.sidebar_border,
            palette.nav_hover,
            palette.nav_active,
            palette.border_hover,
            palette.button_surface,
            palette.button_hover,
            palette.button_pressed,
            palette.disabled_surface,
            palette.disabled_text,
        )

        return f"""
            * {{
                font-family: "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
                font-size: 13px;
                font-weight: 400;
                color: {text_primary};
            }}
            QMainWindow, QWidget#central, QDialog {{
                background-color: {bg_base};
            }}
            QToolTip {{
                background-color: {bg_surface};
                color: {text_primary};
                border: none;
                padding: 4px 8px;
                border-radius: 1px;
            }}

            /* ── Dialog content ── */
            QLabel#dialogTitle {{
                color: {text_primary};
                font-size: 22px;
                font-weight: 600;
            }}
            QLabel#dialogSubtitle {{
                color: {text_muted};
                font-size: 13px;
            }}
            QFrame#newProjectForm {{
                background-color: {bg_surface};
                border: none;
                border-radius: 4px;
            }}
            QLabel#fieldLabel {{
                color: {text_secondary};
                font-size: 13px;
                font-weight: 600;
            }}
            QLabel#dialogErrorHint {{
                color: {danger};
                font-size: 12px;
                padding-top: 2px;
            }}
            /* ── Common prompts ──
               Qt stylesheet pixels are device-independent under high-DPI
               scaling, keeping these shared dimensions readable everywhere. */
            QMessageBox {{
                padding: 18px;
            }}
            QMessageBox QLabel {{
                font-size: 15px;
                min-width: 360px;
                padding: 6px 4px 10px 4px;
            }}
            QMessageBox QPushButton {{
                min-width: 88px;
                min-height: 34px;
                padding: 0 16px;
                font-size: 14px;
                font-weight: 600;
            }}
            QMenu {{
                background-color: {bg_surface};
                color: {text_primary};
                border: 1px solid {border};
                padding: 5px;
            }}
            QMenu::item {{
                background: transparent;
                border-radius: 3px;
                padding: 7px 22px 7px 10px;
                margin: 1px;
            }}
            QMenu::item:selected {{
                background-color: {bg_surface_selected};
                color: {text_primary};
            }}

            /* ── Sidebar ── */
            QWidget#sidebar {{
                background-color: {sidebar_bg};
                border-right: 1px solid {sidebar_border};
            }}
            QWidget#sidebarHeader {{
                background: transparent;
            }}
            QLabel#sidebarTitle {{
                font-size: 23px;
                font-weight: 600;
                color: {text_primary};
            }}
            QLabel#sidebarSubtitle {{
                font-size: 13px;
                font-weight: 400;
                color: {text_muted};
            }}
            QPushButton#navItem {{
                background: transparent;
                border: none;
                border-radius: 4px;
                margin: 2px 12px;
                text-align: left;
                padding: 0 14px;
                font-size: 14px;
                font-weight: 500;
                color: {text_secondary};
            }}
            QPushButton#navItem:hover {{
                background-color: transparent;
                color: {text_secondary};
            }}
            QPushButton#navItem[active="true"] {{
                background-color: {nav_active};
                color: {text_primary};
                font-weight: 600;
            }}

            /* ── ScrollBar ── */
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 0;
                border-radius: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {border};
                min-height: 30px;
                border-radius: 2px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {text_muted};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
            QScrollArea {{ border: none; background: transparent; }}
            QScrollArea#projectScrollArea,
            QWidget#projectViewport,
            QWidget#projectListContainer,
            QScrollArea#installScrollArea,
            QWidget#installViewport,
            QWidget#installListContainer {{
                background-color: {bg_base};
            }}

            /* ── Buttons ── */
            QPushButton {{
                background-color: {button_surface};
                color: {text_primary};
                border: none;
                border-radius: 3px;
                padding: 0 14px;
                font-size: 13px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {button_surface};
            }}
            QPushButton:pressed {{
                background-color: {button_pressed};
            }}
            QPushButton:focus {{
                border: none;
            }}
            QPushButton:disabled {{
                background-color: {disabled_surface};
                color: {disabled_text};
                border: none;
            }}
            QPushButton#primaryBtn, QPushButton#createBtn {{
                background-color: {accent};
                color: {accent_text};
                border: none;
                font-weight: 600;
            }}
            QPushButton#primaryBtn:hover, QPushButton#createBtn:hover {{
                background-color: {accent};
                border: none;
            }}
            QPushButton#primaryBtn:pressed, QPushButton#createBtn:pressed {{
                background-color: {accent_pressed};
                border: none;
            }}
            QPushButton#dangerBtn {{
                color: {danger};
                border: none;
                background-color: {button_surface};
            }}
            QPushButton#dangerBtn:hover {{
                background-color: {button_surface};
                color: {danger};
            }}
            QPushButton#dangerBtn:pressed {{
                background-color: {accent_pressed};
                color: #ffffff;
            }}
            QPushButton#iconBtn {{
                background: transparent;
                border: none;
                font-size: 18px;
                padding: 4px;
            }}
            QPushButton#iconBtn:hover {{
                background: {bg_surface_hover};
            }}

            /* ── Theme Label ── */
            QLabel#themeLabel {{
                font-size: 14px;
                color: {text_secondary};
            }}

            /* ── Inputs ── */
            QLineEdit {{
                background-color: {bg_input};
                color: {text_primary};
                border: 1px solid {border};
                border-radius: 2px;
                padding: 9px 13px;
                font-size: 14px;
            }}
            QLineEdit:focus {{
                border-color: {accent};
                background-color: {bg_surface};
            }}
            QLineEdit:disabled {{
                background-color: {disabled_surface};
                color: {disabled_text};
            }}
            QLineEdit::placeholder {{
                color: {text_muted};
            }}

            /* ── ComboBox ── */
            QComboBox {{
                background-color: {bg_input};
                color: {text_primary};
                border: 1px solid {border};
                border-radius: 2px;
                padding: 8px 12px;
                font-size: 14px;
            }}
            QComboBox:hover {{
                border-color: {border};
            }}
            QComboBox:focus {{
                border-color: {accent};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 24px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {bg_surface};
                color: {text_primary};
                border: 1px solid {border};
                selection-background-color: {bg_surface_selected};
            }}

            /* ── Project Card ── */
            QFrame#projectCard {{
                background: transparent;
                border: none;
                border-radius: 4px;
            }}
            QFrame#projectCard:hover {{
                background: transparent;
            }}
            QFrame#projectCard[selected="true"] {{
                background: transparent;
                border: none;
            }}
            QPushButton#cardAvatar {{
                background-color: {bg_surface_selected};
                color: {accent};
                border-radius: 3px;
                font-size: 13px;
                font-weight: bold;
                border: none;
                padding: 0;
                margin: 0;
            }}
            QLabel#cardName {{
                font-size: 15px;
                font-weight: 700;
            }}
            QLabel#cardPath {{
                font-size: 12px;
                color: {text_secondary};
            }}
            QLabel#cardVersion {{
                font-size: 11px;
                color: {text_muted};
            }}
            QLabel#sidebarLogo {{
                background: transparent;
            }}
            QLabel#cardDate {{
                font-size: 12px;
                color: {text_muted};
            }}
            QPushButton#cardOpenBtn {{
                background-color: {button_surface};
                color: {text_secondary};
                border: none;
                border-radius: 3px;
                font-size: 12px;
                padding: 0;
            }}
            QPushButton#cardOpenBtn:hover {{
                background-color: {button_hover};
                color: {text_primary};
            }}
            QLabel#projectStatus {{
                padding: 4px 9px;
                border-radius: 1px;
                font-size: 11px;
                font-weight: 600;
                color: {text_secondary};
                background-color: {bg_surface_selected};
            }}
            QLabel#projectStatus[kind="ready"] {{ color: {text_secondary}; background-color: rgba(130, 130, 130, 0.20); }}
            QLabel#projectStatus[kind="warning"] {{ color: {text_secondary}; background-color: rgba(130, 130, 130, 0.20); }}
            QLabel#projectStatus[kind="error"] {{ color: {danger}; background-color: rgba(160, 42, 62, 0.22); }}
            QLabel#projectStatus[kind="active"] {{ color: {accent_hover}; background-color: rgba(184, 49, 59, 0.24); }}
            QLabel#projectVersion {{
                padding: 6px 10px;
                border-radius: 3px;
                font-size: 12px;
                font-weight: 500;
                color: {text_secondary};
                background-color: {bg_surface_selected};
            }}
            QLabel#projectVersion[kind="warning"] {{ color: {accent_hover}; }}
            QLabel#projectVersion[kind="active"] {{ color: {accent}; }}

            /* ── Version Card (Installs page) ── */
            QFrame#versionCard {{
                background: transparent;
                border: none;
                border-radius: 4px;
            }}
            QFrame#versionCard:hover {{
                background: transparent;
            }}
            QLabel#versionBadge {{
                font-size: 16px;
                font-weight: 700;
                color: {text_primary};
                padding: 2px 10px;
                background-color: {bg_surface_selected};
                border-radius: 1px;
            }}

            /* ── Version Row (Install Editor dialog) ── */
            QFrame#versionRow {{
                background: transparent;
                border: none;
                border-radius: 3px;
            }}
            QFrame#versionRow:hover {{
                background: transparent;
            }}
            QFrame#versionRow[selected="true"] {{
                background: transparent;
                border: none;
            }}
            QLabel#installedBadge {{
                font-size: 12px;
                color: {text_muted};
                font-style: italic;
            }}

            /* ── Page Title ── */
            QLabel#pageTitle {{
                font-size: 27px;
                font-weight: 600;
            }}
            QLabel#pageSubtitle {{
                font-size: 13px;
                color: {text_secondary};
            }}

            /* ── Empty hint ── */
            QLabel#emptyHint {{
                font-size: 14px;
                color: {text_muted};
                padding: 4px;
            }}
            QFrame#emptyState {{
                background-color: {bg_surface};
                border: none;
                border-radius: 4px;
                min-height: 210px;
            }}
            QLabel#emptyTitle {{
                font-size: 18px;
                font-weight: 600;
            }}
            QFrame#settingsCard {{
                background: transparent;
                border: none;
                border-radius: 4px;
            }}
            QLabel#settingsLabel {{
                font-size: 15px;
                font-weight: 600;
            }}
            QLabel#settingsDescription {{
                font-size: 12px;
                color: {text_secondary};
            }}

            /* ── Discussion ── */
            QFrame#discussionHero {{
                background: transparent;
                border: none;
                border-radius: 4px;
            }}
            QLabel#discussionEyebrow {{
                color: {accent};
                font-size: 11px;
                font-weight: 600;
            }}
            QLabel#discussionHeading {{
                color: {text_primary};
                font-size: 22px;
                font-weight: 600;
            }}
            QLabel#discussionDescription {{
                color: {text_secondary};
                font-size: 13px;
            }}
            QLabel#discussionAddress {{
                color: {text_muted};
                font-size: 12px;
            }}
            QLabel#communityFeedTitle {{
                color: {text_primary};
                font-size: 16px;
                font-weight: 600;
            }}
            QFrame#communityTopic {{
                background-color: {bg_surface};
                border: 1px solid {border};
                border-radius: 4px;
            }}
            QLabel#communityTopicTitle {{
                color: {text_primary};
                font-size: 14px;
                font-weight: 600;
            }}
            QLabel#communityTopicStats, QLabel#communityFeedStatus {{
                color: {text_muted};
                font-size: 12px;
            }}
            QLabel#communityFeedStatus[kind="error"] {{
                color: {danger};
            }}

            /* ── Header ── */
            QLabel#mainTitle {{
                font-size: 28px;
                font-weight: 700;
            }}
            QLabel#subTitle {{
                font-size: 14px;
                color: {text_muted};
            }}

            /* ── Progress ── */
            QProgressBar {{
                background-color: {button_surface};
                border: none;
                border-radius: 0;
                height: 6px;
            }}
            QProgressBar::chunk {{
                background-color: {accent};
                border-radius: 0;
            }}
        """
