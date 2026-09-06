"""
Infernux Infernux Editor Theme
==============================================


This is the **single theme configuration file** for the Infernux Editor.
Change colors and sizes here to restyle the entire editor.

Structure:

All colors are **sRGB-space RGBA tuples** (float, 0-1).

Usage::

    from Infernux.engine.ui.theme import Theme, ImGuiCol

    Theme.push_ghost_button_style(ctx)
    ctx.button("Click me", on_click)
    ctx.pop_style_color(3)
"""

from __future__ import annotations
from typing import Iterable, Optional, Tuple

# Color type alias (R, G, B, A) all float [0..1]
RGBA = Tuple[float, float, float, float]


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  1. Color Space Utilities ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def srgb_to_linear(s: float) -> float:
    """
    Convert a single sRGB [0,1] component to linear space."""
    if s <= 0.04045:
        return s / 12.92
    return ((s + 0.055) / 1.055) ** 2.4


def srgb3(r: float, g: float, b: float, a: float = 1.0) -> RGBA:
    """
    Convert sRGB 0-1 components to linear RGBA tuple."""
    return (srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b), a)


def hex_to_linear(hex_r: int, hex_g: int, hex_b: int, a: float = 1.0) -> RGBA:
    """
    Convert 0-255 sRGB hex components to linear RGBA tuple."""
    return srgb3(hex_r / 255.0, hex_g / 255.0, hex_b / 255.0, a)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  2. ImGui ImGui Enum Mirrors ║
# ║     Must match imgui.h enum order exactly                               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class ImGuiCol:
    Text                       = 0
    TextDisabled               = 1
    WindowBg                   = 2
    ChildBg                    = 3
    PopupBg                    = 4
    Border                     = 5
    BorderShadow               = 6
    FrameBg                    = 7
    FrameBgHovered             = 8
    FrameBgActive              = 9
    TitleBg                    = 10
    TitleBgActive              = 11
    TitleBgCollapsed           = 12
    MenuBarBg                  = 13
    ScrollbarBg                = 14
    ScrollbarGrab              = 15
    ScrollbarGrabHovered       = 16
    ScrollbarGrabActive        = 17
    CheckMark                  = 18
    SliderGrab                 = 19
    SliderGrabActive           = 20
    Button                     = 21
    ButtonHovered              = 22
    ButtonActive               = 23
    Header                     = 24
    HeaderHovered              = 25
    HeaderActive               = 26
    Separator                  = 27
    SeparatorHovered           = 28
    SeparatorActive            = 29
    ResizeGrip                 = 30
    ResizeGripHovered          = 31
    ResizeGripActive           = 32
    InputTextCursor            = 33
    TabHovered                 = 34
    Tab                        = 35
    TabSelected                = 36
    TabSelectedOverline        = 37
    TabDimmed                  = 38
    TabDimmedSelected          = 39
    TabDimmedSelectedOverline  = 40
    DockingPreview             = 41
    DockingEmptyBg             = 42
    PlotLines                  = 43
    PlotLinesHovered           = 44
    PlotHistogram              = 45
    PlotHistogramHovered       = 46
    TableHeaderBg              = 47
    TableBorderStrong          = 48
    TableBorderLight           = 49
    TableRowBg                 = 50
    TableRowBgAlt              = 51
    TextLink                   = 52
    TextSelectedBg             = 53
    TreeLines                  = 54
    DragDropTarget             = 55
    DragDropTargetBg           = 56
    UnsavedMarker              = 57
    NavCursor                  = 58
    NavWindowingHighlight      = 59
    NavWindowingDimBg          = 60
    ModalWindowDimBg           = 61


class ImGuiWindowFlags:
    NoTitleBar                  = 1 << 0
    NoResize                    = 1 << 1
    NoMove                      = 1 << 2
    NoScrollbar                 = 1 << 3
    NoScrollWithMouse           = 1 << 4
    NoCollapse                  = 1 << 5
    AlwaysAutoResize            = 1 << 6
    NoBackground                = 1 << 7
    NoSavedSettings             = 1 << 8
    NoMouseInputs               = 1 << 9
    NoFocusOnAppearing          = 1 << 12
    NoBringToFrontOnFocus       = 1 << 13
    NoNavInputs                 = 1 << 16
    NoNavFocus                  = 1 << 17
    UnsavedDocument             = 1 << 18
    NoDocking                   = 1 << 19
    NoNav                       = (1 << 16) | (1 << 17)
    NoDecoration                = NoTitleBar | NoResize | NoScrollbar | NoCollapse
    NoInputs                    = NoMouseInputs | NoNavInputs | NoNavFocus


class ImGuiTreeNodeFlags:
    Selected                    = 1 << 0
    Framed                      = 1 << 1
    AllowOverlap                = 1 << 2
    NoTreePushOnOpen            = 1 << 3
    NoAutoOpenOnLog             = 1 << 4
    DefaultOpen                 = 1 << 5
    OpenOnDoubleClick           = 1 << 6
    OpenOnArrow                 = 1 << 7
    Leaf                        = 1 << 8
    Bullet                      = 1 << 9
    FramePadding                = 1 << 10
    SpanAvailWidth              = 1 << 11
    SpanFullWidth               = 1 << 12
    SpanAllColumns              = 1 << 13
    CollapsingHeader            = Framed | NoTreePushOnOpen | NoAutoOpenOnLog


class ImGuiMouseCursor:
    Arrow      = 0
    TextInput  = 1
    ResizeAll  = 2
    ResizeNS   = 3
    ResizeEW   = 4
    ResizeNESW = 5
    ResizeNWSE = 6
    Hand       = 7


class ImGuiStyleVar:
    Alpha                       = 0
    DisabledAlpha               = 1
    WindowPadding               = 2
    WindowRounding              = 3
    WindowBorderSize            = 4
    WindowMinSize               = 5
    WindowTitleAlign            = 6
    ChildRounding               = 7
    ChildBorderSize             = 8
    PopupRounding               = 9
    PopupBorderSize             = 10
    FramePadding                = 11
    FrameRounding               = 12
    FrameBorderSize             = 13
    ItemSpacing                 = 14
    ItemInnerSpacing            = 15
    IndentSpacing               = 16
    CellPadding                 = 17
    ScrollbarSize               = 18
    ScrollbarRounding           = 19
    ScrollbarPadding            = 20
    GrabMinSize                 = 21
    GrabRounding                = 22
    ImageBorderSize             = 23
    TabRounding                 = 24
    TabBorderSize               = 25
    TabMinWidthBase             = 26
    TabMinWidthShrink           = 27
    TabBarBorderSize            = 28
    TabBarOverlineSize          = 29
    TableAngledHeadersAngle     = 30
    TableAngledHeadersTextAlign = 31
    TreeLinesSize               = 32
    TreeLinesRounding           = 33
    ButtonTextAlign             = 34
    SelectableTextAlign         = 35
    SeparatorTextBorderSize     = 36
    SeparatorTextAlign          = 37
    SeparatorTextPadding        = 38
    DockingSeparatorSize        = 39


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  3. Theme — Editor Theme Configuration ║
# ║                                                                         ║
# ║  Modify values below to restyle the entire editor.                      ║
# ║  All colors are sRGB-space RGBA (UNORM swapchain, no hw conversion).    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class Theme:
    """
    Central theme for the Infernux Editor — single source of truth for
    all colors, sizes, icons, and layout constants.

    Modify values in this class to customize the editor appearance.
    """

    # ══════════════════════════════════════════════════════════════════════
    #  Base Palette (Unity-style neutral dark theme)
    #  Neutral grays + red accent (#EB5757)
    # ══════════════════════════════════════════════════════════════════════

    # -- Text Colors ------------------------------------------------
    TEXT              : RGBA = (0.84,  0.84,  0.84,  1.0)  # Primary text (neutral light gray)
    TEXT_DISABLED     : RGBA = (0.40,  0.40,  0.40,  1.0)  # Disabled text
    TEXT_DIM          : RGBA = (0.55,  0.55,  0.55,  1.0)  # Secondary/dim text

    # -- Background Colors -------------------------------------------
    WINDOW_BG         : RGBA = (0.22,  0.22,  0.22,  1.0)  # Window background (Unity #383838)
    CHILD_BG          : RGBA = (0.0, 0.0, 0.0, 0.0)  # Child window bg (transparent)
    POPUP_BG          : RGBA = (0.24,  0.24,  0.24,  0.96)  # Popup background (Unity #3E3E3E)
    MENU_BAR_BG       : RGBA = (0.16,  0.16,  0.16,  1.0)  # Menu bar background (#292929)
    STATUS_BAR_BG     : RGBA = (0.13,  0.13,  0.13,  1.0)  # Status bar background (Unity #212121)

    # -- Border Colors ------------------------------------------------
    BORDER            : RGBA = (0.10,  0.10,  0.10,  1.0)  # Standard border (Unity #1A1A1A)
    BORDER_TRANSPARENT: RGBA = (0.0, 0.0, 0.0, 0.0)  # Transparent border
    BORDER_SHADOW     : RGBA = (0.0, 0.0, 0.0, 0.0)  # Border shadow

    # -- Frame Colors (input fields, sliders) ------------------
    FRAME_BG          : RGBA = (0.165, 0.165, 0.165, 1.0)  # Default background (Unity #2A2A2A)
    FRAME_BG_HOVERED  : RGBA = (0.20,  0.20,  0.20,  1.0)  # On hover
    FRAME_BG_ACTIVE   : RGBA = (0.24,  0.17,  0.17,  1.0)  # On active (red tint)

    # ══════════════════════════════════════════════════════════════════════
    #  Button Colors
    # ══════════════════════════════════════════════════════════════════════

    # -- Regular Button -----------------------------------------------
    BTN_NORMAL        : RGBA = (0.25,  0.25,  0.25,  1.0)  # Normal (Unity #404040)
    BTN_HOVERED       : RGBA = (0.30,  0.25,  0.25,  1.0)  # Hovered (red tint)
    BTN_ACTIVE        : RGBA = (0.35,  0.22,  0.22,  1.0)  # Active (deeper red)

    # -- Ghost Button ( )
    #    Transparent background, used in toolbar and status bar
    BTN_GHOST         : RGBA = (0.0, 0.0, 0.0, 0.0)  # Transparent
    BTN_GHOST_HOVERED : RGBA = (0.28, 0.23, 0.23, 1.0)  # Hovered (red warmth)
    BTN_GHOST_ACTIVE  : RGBA = (0.32, 0.25, 0.25, 1.0)  # Active (deeper red)

    # -- Status Bar Ghost Button ( )
    BTN_SB_HOVERED    : RGBA = (0.20, 0.18, 0.18, 1.0)  # Hovered (subtle red)
    BTN_SB_ACTIVE     : RGBA = (0.24, 0.20, 0.20, 1.0)  # Active

    # -- Selection Highlight ( )
    BTN_SELECTED      : RGBA = (0.922, 0.341, 0.341, 0.55)  # Selected state (theme red, semi-transparent overlay)
    BTN_SUBTLE_HOVER  : RGBA = (0.20, 0.18, 0.18, 1.0)  # Subtle hover on icons

    # -- Toolbar Play-Mode Buttons
    PLAY_ACTIVE       : RGBA = (0.20, 0.45, 0.30, 1.0)  # Playing (green tint)
    PAUSE_ACTIVE      : RGBA = (0.50, 0.40, 0.15, 1.0)  # Paused (amber tint)
    BTN_IDLE          : RGBA = (0.18, 0.18, 0.18, 1.0)  # Idle state (neutral dark)
    BTN_DISABLED      : RGBA = (0.15, 0.15, 0.15, 0.4)  # Disabled state

    # -- Accent Button
    APPLY_BUTTON      : RGBA = (0.922, 0.341, 0.341, 1.0)  # Apply/Confirm (theme red #EB5757)

    # ══════════════════════════════════════════════════════════════════════
    #  Headers, Tree Nodes, Selectables
    # ══════════════════════════════════════════════════════════════════════

    HEADER            : RGBA = (0.235, 0.235, 0.235, 1.0)  # Normal (Unity #3C3C3C)
    HEADER_HOVERED    : RGBA = (0.28,  0.24,  0.24,  1.0)  # Hovered (red tint)
    HEADER_ACTIVE     : RGBA = (0.32,  0.25,  0.25,  1.0)  # Active (deeper red)
    SELECTION_BG      : RGBA = (0.922, 0.341, 0.341, 1.0)  # Selection bg (theme accent #EB5757)

    # ══════════════════════════════════════════════════════════════════════
    #  Splitter Colors
    # ══════════════════════════════════════════════════════════════════════

    SPLITTER_HOVER    : RGBA = (0.35, 0.25, 0.25, 0.6)  # Hovered (red tint)
    SPLITTER_ACTIVE   : RGBA = (0.40, 0.28, 0.28, 0.8)  # Active

    # ══════════════════════════════════════════════════════════════════════
    #  Drag & Drop
    # ══════════════════════════════════════════════════════════════════════

    DRAG_DROP_TARGET        : RGBA = (0.0, 0.0, 0.0, 0.0)  # Drop target highlight
    DND_DROP_OUTLINE        : RGBA = (1.0, 1.0, 1.0, 0.85)  # Drop outline color
    DND_DROP_OUTLINE_THICKNESS: float = 1.5  # Outline thickness (px)
    DND_REORDER_LINE        : RGBA = (1.0, 1.0, 1.0, 0.90)  # Reorder indicator line
    DND_REORDER_LINE_THICKNESS: float = 2.0  # Line thickness (px)
    DND_REORDER_SEPARATOR_H : float = 3.0  # Separator height (px)

    # ══════════════════════════════════════════════════════════════════════
    #  Console & Log Colors
    # ══════════════════════════════════════════════════════════════════════

    LOG_INFO          : RGBA = (0.82,  0.82,  0.85,  1.0)  # Info log
    LOG_WARNING       : RGBA = (0.890, 0.710, 0.300, 1.0)  # Warning log (yellow)
    LOG_ERROR         : RGBA = (0.922, 0.341, 0.341, 1.0)  # Error log (red)
    LOG_TRACE         : RGBA = (0.50,  0.50,  0.50,  1.0)  # Trace log (gray)
    LOG_BADGE         : RGBA = (0.55,  0.55,  0.55,  1.0)  # Log badge (count)
    LOG_DIM           : RGBA = (0.133, 0.133, 0.133, 0.6)  # Dimmed log row

    META_TEXT         : RGBA = (1.0, 1.0, 1.0, 1.0)  # Meta text (white)
    SUCCESS_TEXT      : RGBA = (0.70,  0.80,  0.70,  1.0)  # Success text (green)
    WARNING_TEXT      : RGBA = (0.90,  0.60,  0.20,  1.0)  # Warning text (orange)
    ERROR_TEXT        : RGBA = (0.90,  0.30,  0.30,  1.0)  # Error text (red)
    PREFAB_TEXT       : RGBA = (235/255, 87/255, 87/255, 1.0)  # Prefab instance text (theme red)
    PREFAB_HEADER_BG  : RGBA = (0.235, 0.235, 0.235, 1.0)  # Prefab header bg (matches HEADER)
    PREFAB_HEADER_H   : float = 28.0  # Prefab header row height
    PREFAB_HEADER_BTN_GAP : float = 4.0  # Prefab header button gap
    PREFAB_BTN_NORMAL : RGBA = (235.0 / 255.0, 87.0 / 255.0, 87.0 / 255.0, 0.95)  # Prefab button normal (theme red)
    PREFAB_BTN_HOVERED: RGBA = (1.0, 107.0 / 255.0, 107.0 / 255.0, 1.0)  # Prefab button hovered (lighter red)
    PREFAB_BTN_ACTIVE : RGBA = (220.0 / 255.0, 67.0 / 255.0, 67.0 / 255.0, 1.0)  # Prefab button active (deeper red)

    # -- Console Alternating Row Background
    ROW_ALT           : RGBA = (0.0, 0.0, 0.0, 0.06)  # Alternate row bg (subtle)
    ROW_NONE          : RGBA = (0.0,  0.0,  0.0,  0.0)  # No bg

    # ══════════════════════════════════════════════════════════════════════
    #  Play-Mode Viewport Border
    # ══════════════════════════════════════════════════════════════════════

    BORDER_PLAY       : RGBA = (0.012, 0.871, 0.427, 1.0)  # Playing border (green #03DE6D)
    BORDER_PAUSE      : RGBA = (1.0,   0.718, 0.302, 1.0)  # Paused border (amber #FFB74D)
    BORDER_THICKNESS  : float = 2.0  # Border thickness (px)

    # ══════════════════════════════════════════════════════════════════════
    #  Inspector Panel Layout & Colors
    # ══════════════════════════════════════════════════════════════════════

    # -- Layout Sizes
    INSPECTOR_INIT_SIZE        = (300, 500)  # Initial window size (w, h)
    INSPECTOR_MIN_PROPS_H      = 100  # Min properties height
    INSPECTOR_MIN_RAWDATA_H    = 100  # Min raw-data height
    INSPECTOR_SPLITTER_H       = 8  # Splitter bar height
    INSPECTOR_DEFAULT_RATIO    = 0.4  # Properties ratio
    INSPECTOR_LABEL_PAD        = 12.0  # Label padding
    INSPECTOR_MIN_LABEL_WIDTH  = 132.0  # Min label width
    INSPECTOR_FRAME_PAD        = (4.0, 2.0)  # Frame padding
    # Left text inset for object/reference fields so the label vertically and
    # horizontally aligns with neighbouring scalar widgets (Unity ObjectField).
    OBJECT_FIELD_TEXT_INSET_X  : float = 12.0
    INSPECTOR_ITEM_SPC         = (4.0, 2.0)  # Item spacing
    INSPECTOR_SUBITEM_SPC      = (4.0, 2.0)  # Sub-item spacing
    INSPECTOR_SECTION_GAP      = 6.0  # Section gap
    INSPECTOR_TITLE_GAP        = 10.0  # Title gap

    # -- Component Header
    INSPECTOR_HEADER_PRIMARY_FRAME_PAD = (4.0, 2.0)  # Primary header frame padding
    INSPECTOR_HEADER_SECONDARY_FRAME_PAD = (4.0, 2.0)  # Secondary header frame padding
    INSPECTOR_HEADER_TERTIARY_FRAME_PAD = (4.0, 1.0)  # Nested item header frame padding
    INSPECTOR_HEADER_LIST_FRAME_PAD  = (4.0, 2.0)  # List header frame padding
    INSPECTOR_HEADER_PRIMARY_FONT_SCALE= 1.0  # Primary header font scale
    INSPECTOR_HEADER_SECONDARY_FONT_SCALE= 1.0  # Secondary header font scale
    INSPECTOR_HEADER_TERTIARY_FONT_SCALE= 0.96  # Nested item visual hierarchy
    INSPECTOR_HEADER_LIST_FONT_SCALE = 1.0  # List header font scale
    INSPECTOR_HEADER_ITEM_SPC   = (4.0, 2.0)  # Header item spacing
    INSPECTOR_HEADER_BORDER_SIZE = 0.0  # Header border size
    INSPECTOR_HEADER_RIGHT_MARGIN = 1.0  # How far the header bg stops from the right content edge (aligns with field controls)
    INSPECTOR_ACTION_ALIGN_X    = 0.0  # Action button alignment
    INSPECTOR_HEADER_CONTENT_INDENT = 28.0  # Header content indent (px)
    ADD_COMP_SEARCH_W          = 240  # "Search components" input width
    COMPONENT_ICON_SIZE        = 16  # Component icon size (px)
    COMP_ENABLED_CB_OFFSET     = 34  # Enabled checkbox right offset (matches 75% checkbox)

    # -- Checkbox Style (square at 75%; label text stays ambient size)
    INSPECTOR_CHECKBOX_BOX_SCALE = 0.75  # Scales only the checkbox square
    INSPECTOR_CHECKBOX_FRAME_PAD = (3.0, 1.5)  # Checkbox frame padding
    INSPECTOR_CHECKBOX_SLOT_W    = 16.5  # Checkbox slot width
    INSPECTOR_CHECKBOX_BOX_PX    = 14.0  # Fixed square side length (one size everywhere)

    # -- Inspector Header Colors
    INSPECTOR_HEADER_PRIMARY    : RGBA = (0.235, 0.235, 0.235, 1.0)  # Primary (Unity gray)
    INSPECTOR_HEADER_PRIMARY_HOVERED : RGBA = (0.28,  0.24,  0.24,  1.0)  # Primary hovered (red tint)
    INSPECTOR_HEADER_PRIMARY_ACTIVE  : RGBA = (0.32,  0.25,  0.25,  1.0)  # Primary active
    INSPECTOR_HEADER_SELECTED   : RGBA = (0.40,  0.22,  0.22,  1.0)  # Selected component header (theme red tint)
    INSPECTOR_HEADER_SELECTED_HOVERED : RGBA = (0.46,  0.24,  0.24,  1.0)
    INSPECTOR_HEADER_SELECTED_ACTIVE  : RGBA = (0.50,  0.26,  0.26,  1.0)
    INSPECTOR_HEADER_SECONDARY  : RGBA = (0.18,  0.18,  0.18,  1.0)  # Secondary (same scale, darker tone)
    INSPECTOR_HEADER_SECONDARY_HOVERED : RGBA = (0.22,  0.20,  0.20,  1.0)
    INSPECTOR_HEADER_SECONDARY_ACTIVE  : RGBA = (0.26,  0.22,  0.22,  1.0)
    INSPECTOR_HEADER_TERTIARY  : RGBA = (0.125, 0.125, 0.125, 1.0)
    INSPECTOR_HEADER_TERTIARY_HOVERED : RGBA = (0.18, 0.16, 0.16, 1.0)
    INSPECTOR_HEADER_TERTIARY_ACTIVE  : RGBA = (0.22, 0.18, 0.18, 1.0)
    INSPECTOR_HEADER_LIST       : RGBA = (0.16,  0.16,  0.16,  1.0)  # List header (distinct from component header)
    INSPECTOR_HEADER_LIST_HOVERED : RGBA = (0.20,  0.18,  0.18,  1.0)
    INSPECTOR_HEADER_LIST_ACTIVE  : RGBA = (0.24,  0.20,  0.20,  1.0)

    # -- Inspector Inline Buttons
    INSPECTOR_INLINE_BTN_IDLE  : RGBA = (0.20,  0.20,  0.20,  1.0)  # Idle
    INSPECTOR_INLINE_BTN_HOVER : RGBA = (0.28,  0.24,  0.24,  1.0)  # Hover (red tint)
    INSPECTOR_INLINE_BTN_ACTIVE: RGBA = (0.922, 0.341, 0.341, 1.0)  # Active (theme red #EB5757)
    INSPECTOR_INLINE_BTN_ON    : RGBA = (0.80,  0.30,  0.30,  1.0)  # Active (dimmer red)
    INSPECTOR_INLINE_BTN_GAP   : float = 4.0  # Button gap
    INSPECTOR_INLINE_BTN_H     : float = 0.0  # Button height (0=auto)

    # -- List Body (Unity-style boxed area)
    INSPECTOR_LIST_BODY_BG      : RGBA = (0.10,  0.10,  0.10,  0.82)  # Distinct dark bg behind list items
    INSPECTOR_LIST_BODY_BORDER  : RGBA = (0.22,  0.22,  0.22,  1.0)  # Border separating list body from component bg

    # -- Curve editor -------------------------------------------------
    CURVE_EDITOR_BG             : RGBA = (0.13, 0.13, 0.13, 1.0)
    CURVE_EDITOR_GRID           : RGBA = (0.28, 0.28, 0.28, 0.55)
    CURVE_EDITOR_AXIS           : RGBA = (0.48, 0.48, 0.48, 0.72)
    CURVE_EDITOR_LINE           : RGBA = (0.922, 0.341, 0.341, 1.0)
    CURVE_EDITOR_KEY            : RGBA = (0.94, 0.94, 0.94, 1.0)
    CURVE_EDITOR_KEY_SELECTED   : RGBA = (0.922, 0.341, 0.341, 1.0)
    CURVE_EDITOR_TANGENT        : RGBA = (0.72, 0.54, 0.54, 0.90)
    CURVE_EDITOR_PREVIEW_H      : float = 46.0
    CURVE_EDITOR_CANVAS_H       : float = 220.0
    INSPECTOR_LIST_BODY_ROUNDING: float = 0.0   # Bottom corner rounding
    INSPECTOR_LIST_BODY_PAD_X   : float = 4.0   # Horizontal padding inside list body
    INSPECTOR_LIST_BODY_PAD_Y   : float = 2.0   # Vertical padding inside list body
    INSPECTOR_SMALL_ICON_BTN_FRAME_PAD: tuple = (4.0, 2.0)  # Match standard inspector control height

    # -- Color Swatch Border
    COLOR_SWATCH_BORDER       : RGBA = (0.4, 0.4, 0.4, 1.0)

    # ══════════════════════════════════════════════════════════════════════
    #  Toolbar Panel Spacing
    # ══════════════════════════════════════════════════════════════════════

    TOOLBAR_WIN_PAD   = (4.0, 4.0)  # Window padding
    TOOLBAR_FRAME_PAD = (6.0, 4.0)  # Frame padding
    TOOLBAR_ITEM_SPC  = (6.0, 4.0)  # Item spacing
    TOOLBAR_FRAME_RND = 0.0  # Frame rounding
    TOOLBAR_FRAME_BRD = 0.0  # Frame border size

    # ══════════════════════════════════════════════════════════════════════
    #  Popup Spacing ( Gizmos/Camera )
    # ══════════════════════════════════════════════════════════════════════

    POPUP_WIN_PAD     = (12.0, 10.0)  # Popup window padding
    POPUP_ITEM_SPC    = (10.0, 8.0)  # Popup item spacing
    POPUP_FRAME_PAD   = (8.0, 6.0)  # Popup frame padding

    # -- Add Component Popup
    POPUP_ADD_COMP_PAD  = (10.0, 8.0)  # Padding
    POPUP_ADD_COMP_SPC  = (6.0, 4.0)  # Item spacing
    ADD_COMP_FRAME_PAD  = (6.0, 6.0)  # Frame padding

    # ══════════════════════════════════════════════════════════════════════
    #  Hierarchy Panel
    # ══════════════════════════════════════════════════════════════════════

    TREE_ITEM_SPC     = (0.0, 0.0)  # Tree item spacing (Unity: 0)
    TREE_FRAME_PAD    = (2.0, 2.0)  # Tree frame padding (Unity-compact)
    TREE_INDENT       : float = 14.0  # Tree indent per level (Unity: ~14px)
    TREE_ROW_ALT_BG   : RGBA = (0.0, 0.0, 0.0, 0.08)  # Alternating row tint
    TREE_DND_LINE_CLR : RGBA = (0.922, 0.341, 0.341, 1.0)  # Drag insertion line (theme red)
    PREFAB_ICON       : str  = "\u25C6"  # Prefab icon (diamond)

    # ══════════════════════════════════════════════════════════════════════
    #  Console Panel Spacing
    # ══════════════════════════════════════════════════════════════════════

    CONSOLE_FRAME_PAD = (4.0, 3.0)  # Frame padding
    CONSOLE_ITEM_SPC  = (6.0, 4.0)  # Item spacing

    # ══════════════════════════════════════════════════════════════════════
    #  Status Bar Layout
    # ══════════════════════════════════════════════════════════════════════

    STATUS_BAR_WIN_PAD   = (6.0, 4.0)  # Window padding
    STATUS_BAR_ITEM_SPC  = (8.0, 0.0)  # Item spacing
    STATUS_BAR_FRAME_PAD = (0.0, 0.0)  # Frame padding

    # -- Status/Progress Indicator ( )
    STATUS_PROGRESS_FRACTION : float = 0.25  # Right fraction (1/4)
    STATUS_PROGRESS_H        : float = 4.0  # Progress bar height
    STATUS_PROGRESS_CLR      : RGBA = (235/255, 87/255, 87/255, 1.0)  # Progress color (theme red)
    STATUS_PROGRESS_BG       : RGBA = (0.10, 0.10, 0.10, 1.0)  # Progress bg
    STATUS_PROGRESS_LABEL_CLR: RGBA = (0.65, 0.65, 0.65, 1.0)  # Progress label color

    # ══════════════════════════════════════════════════════════════════════
    #  Project Panel
    # ══════════════════════════════════════════════════════════════════════

    ICON_BTN_NO_PAD   = (0.0, 0.0)  # Icon button frame padding (none)
    PROJECT_PANEL_PAD = (12.0, 8.0)  # File grid child window padding

    # ══════════════════════════════════════════════════════════════════════
    #  Scene View Panel
    # ══════════════════════════════════════════════════════════════════════

    # -- Gizmo Gizmo Tool Buttons
    SCENE_GIZMO_TOOL_BTN_W    : float = 20.0  # Button width
    SCENE_GIZMO_TOOL_BTN_H    : float = 20.0  # Button height
    SCENE_GIZMO_TOOL_BTN_GAP  : float = 1.0  # Button gap
    SCENE_GIZMO_TOOL_BTN_PAD  = (2.0, 2.0)  # Frame padding
    SCENE_COORD_DROPDOWN_W    : float = 80.0  # Global/Local dropdown width

    # -- Orientation Gizmo ( )
    SCENE_ORIENT_RADIUS       : float = 40.0  # Circle radius
    SCENE_ORIENT_MARGIN       : float = 12.0  # Margin from corner
    SCENE_ORIENT_AXIS_LEN     : float = 30.0  # Axis line length
    SCENE_ORIENT_END_RADIUS   : float = 7.0  # Axis end circle radius
    SCENE_ORIENT_NEG_RADIUS   : float = 4.0  # Negative axis circle radius
    SCENE_ORIENT_BG           : RGBA = (0.10, 0.10, 0.10, 0.6)  # Background (neutral dark)
    SCENE_ORIENT_FLY_DURATION : float = 0.3  # Fly animation duration (s)

    # -- Scene Overlay Dropdown
    SCENE_OVERLAY_COMBO_BG    : RGBA = (0.14, 0.14, 0.14, 0.85)  # Background (neutral dark)
    SCENE_OVERLAY_COMBO_HOVER : RGBA = (0.22, 0.19, 0.19, 0.90)  # Hover (red tint)
    SCENE_OVERLAY_COMBO_ACTIVE: RGBA = (0.18, 0.16, 0.16, 0.95)  # Active (red tint)
    SCENE_OVERLAY_ROUNDING    : float = 4.0  # Rounding
    SCENE_OVERLAY_BORDER_SIZE : float = 0.0  # Border size

    # ══════════════════════════════════════════════════════════════════════
    #  UI UI Editor Panel
    # ══════════════════════════════════════════════════════════════════════

    # -- Canvas
    UI_EDITOR_CANVAS_BG       : RGBA = (0.12, 0.12, 0.12, 1.0)  # Canvas background (neutral dark)
    UI_EDITOR_CANVAS_BORDER   : RGBA = (0.30, 0.30, 0.30, 1.0)  # Canvas border (neutral gray)

    # -- Multi-Canvas Layout
    UI_EDITOR_CANVAS_HEADER_H      : float = 22.0   # Canvas header bar height (screen px)
    UI_EDITOR_CANVAS_HEADER_BG     : RGBA = (0.18, 0.18, 0.18, 1.0)
    UI_EDITOR_CANVAS_HEADER_BG_FOC : RGBA = (0.35, 0.22, 0.22, 1.0)  # Focused canvas header (theme red tint)
    UI_EDITOR_CANVAS_HEADER_TEXT   : RGBA = (0.85, 0.85, 0.85, 1.0)
    UI_EDITOR_CANVAS_SPACING       : float = 60.0   # Auto-layout gap between canvases (workspace px)
    UI_EDITOR_CANVAS_INACTIVE_ALPHA: float = 0.35    # Alpha multiplier for inactive canvases

    # -- Element Interaction
    UI_EDITOR_ELEMENT_HOVER   : RGBA = (0.922, 0.341, 0.341, 0.12)  # Element hover (red glow)
    UI_EDITOR_ELEMENT_SELECT  : RGBA = (0.922, 0.341, 0.341, 1.0)  # Element selected (theme red)

    # -- Handles
    UI_EDITOR_HANDLE_COLOR    : RGBA = (1.0, 1.0, 1.0, 1.0)  # Handle color
    UI_EDITOR_HANDLE_SIZE     : float = 4.0  # Handle half-size (px)

    # -- Zoom & Viewport
    UI_EDITOR_TOOLBAR_HEIGHT  : float = 32.0  # Toolbar height
    UI_EDITOR_MIN_ZOOM        : float = 0.05  # Min zoom
    UI_EDITOR_MAX_ZOOM        : float = 2.0  # Max zoom (200%)
    UI_EDITOR_ZOOM_STEP       : float = 0.1  # Wheel zoom step

    # -- Labels
    UI_EDITOR_LABEL_OFFSET    : float = 16.0  # Canvas top label offset (px)
    UI_EDITOR_LABEL_COLOR     : RGBA = (0.6, 0.6, 0.6, 0.7)  # Label color

    # -- Rotation Handle
    UI_EDITOR_ROTATE_DISTANCE : float = 22.0  # Offset from top-mid (px)
    UI_EDITOR_ROTATE_RADIUS   : float = 4.0  # Circle radius (px)
    UI_EDITOR_ROTATE_HIT_R    : float = 10.0  # Click radius (px)
    UI_EDITOR_EDGE_HIT_TOL    : float = 6.0  # Edge hit tolerance (px)
    UI_EDITOR_SELECT_LINE_W   : float = 1.5  # Selection border width
    UI_EDITOR_ROTATE_LINE_W   : float = 1.0  # Rotate handle line width
    UI_EDITOR_MIN_ELEM_SIZE   : float = 4.0  # Min element dimension (px)

    # -- Placeholder
    UI_EDITOR_PLACEHOLDER_TINT : float = 0.3  # Placeholder tint multiplier
    UI_EDITOR_PLACEHOLDER_ALPHA: float = 0.5  # Placeholder alpha
    UI_EDITOR_FALLBACK_TEXT   : RGBA = (0.7, 0.7, 0.7, 1.0)  # Fallback text color

    # -- Window & Toolbar Layout
    UI_EDITOR_INIT_WINDOW_W   : float = 800.0  # Initial window width
    UI_EDITOR_INIT_WINDOW_H   : float = 600.0  # Initial window height
    UI_EDITOR_FIT_MARGIN      : float = 40.0  # Fit-zoom padding (px)
    UI_EDITOR_TOOLBAR_GAP     : float = 4.0  # Toolbar button gap
    UI_EDITOR_TOOLBAR_SECTION_GAP : float = 16.0  # Toolbar section gap
    UI_EDITOR_CREATE_BTN_W    : float = 220.0  # "Create Canvas" button width
    UI_EDITOR_CREATE_BTN_H    : float = 28.0  # "Create Canvas" button height

    # -- Default Element Creation Sizes ( )
    UI_EDITOR_NEW_TEXT_POS    = (-80.0, -20.0)
    UI_EDITOR_NEW_IMAGE_SIZE  = (100.0, 100.0)
    UI_EDITOR_NEW_IMAGE_POS   = (-50.0, -50.0)
    UI_EDITOR_NEW_BUTTON_SIZE = (160.0, 40.0)
    UI_EDITOR_NEW_BUTTON_POS  = (-80.0, -20.0)

    # -- Zoom-Adaptive Snap Table (zoom_threshold → grid_step)
    UI_EDITOR_SNAP_TABLE = (
        (1.0,  1),
        (0.75, 2),
        (0.5,  5),
        (0.35, 10),
        (0.2,  20),
        (0.1,  50),
    )
    UI_EDITOR_SNAP_DEFAULT    : int = 100  # Step at smallest zoom

    # -- Alignment Guides
    UI_EDITOR_ALIGN_GUIDE     : RGBA = (0.922, 0.341, 0.341, 0.95)  # Guide line color
    UI_EDITOR_ALIGN_GUIDE_FAINT: RGBA = (0.922, 0.341, 0.341, 0.30)  # Faint guide color
    UI_EDITOR_ALIGN_GUIDE_W   : float = 1.5  # Guide line width
    UI_EDITOR_ALIGN_SNAP_PX   : float = 8.0  # Snap threshold (px)
    UI_EDITOR_ALIGN_BTN_W     : float = 34.0  # Align button width
    UI_EDITOR_ALIGN_BTN_H     : float = 24.0  # Align button height
    UI_EDITOR_ALIGN_BTN_GAP   : float = 4.0  # Align button gap

    # ══════════════════════════════════════════════════════════════════════
    #  UI UI Runtime Defaults (InxScreenUI )
    # ══════════════════════════════════════════════════════════════════════

    UI_DEFAULT_BUTTON_BG      : RGBA = (0.922, 0.341, 0.341, 1.0)  # Default button bg
    UI_DEFAULT_LABEL_COLOR    : RGBA = (1.0, 1.0, 1.0, 1.0)  # Default label color
    UI_DEFAULT_FONT_SIZE      : float = 18.0  # Default font size
    UI_DEFAULT_LINE_HEIGHT    : float = 1.2  # Default line height
    UI_DEFAULT_LETTER_SPACING : float = 0.0  # Default letter spacing

    # ══════════════════════════════════════════════════════════════════════
    #  Build Settings Panel
    # ══════════════════════════════════════════════════════════════════════

    BUILD_SETTINGS_ROW_SPC = (4.0, 6.0)  # Row spacing

    # ══════════════════════════════════════════════════════════════════════
    #  Common Window Flag Combos
    # ══════════════════════════════════════════════════════════════════════

    WINDOW_FLAGS_VIEWPORT  = (ImGuiWindowFlags.NoFocusOnAppearing
                              | ImGuiWindowFlags.NoBringToFrontOnFocus)
    WINDOW_FLAGS_NO_SCROLL = (ImGuiWindowFlags.NoScrollbar
                              | ImGuiWindowFlags.NoScrollWithMouse)
    WINDOW_FLAGS_NO_DECOR  = (ImGuiWindowFlags.NoTitleBar
                              | ImGuiWindowFlags.NoResize
                              | ImGuiWindowFlags.NoMove
                              | ImGuiWindowFlags.NoScrollbar
                              | ImGuiWindowFlags.NoScrollWithMouse
                              | ImGuiWindowFlags.NoSavedSettings
                              | ImGuiWindowFlags.NoFocusOnAppearing
                              | ImGuiWindowFlags.NoDocking
                              | ImGuiWindowFlags.NoInputs)
    WINDOW_FLAGS_FLOATING  = (ImGuiWindowFlags.NoCollapse
                              | ImGuiWindowFlags.NoSavedSettings)
    WINDOW_FLAGS_DIALOG    = (ImGuiWindowFlags.NoCollapse
                              | ImGuiWindowFlags.NoSavedSettings
                              | ImGuiWindowFlags.NoDocking
                              | ImGuiWindowFlags.NoResize
                              | ImGuiWindowFlags.NoMove)

    # ══════════════════════════════════════════════════════════════════════
    #  ImGui ImGui Condition Constants
    # ══════════════════════════════════════════════════════════════════════

    COND_FIRST_USE_EVER = 4  # Set only on first use
    COND_ALWAYS         = 1  # Set every frame

    # ══════════════════════════════════════════════════════════════════════
    #  Border Sizes
    # ══════════════════════════════════════════════════════════════════════

    BORDER_SIZE_NONE    = 0.0  # No border

    # ══════════════════════════════════════════════════════════════════════
    #  Icon Constants
    #  Text icons for fallback; image icon names for EditorIcons.get()
    # ══════════════════════════════════════════════════════════════════════

    # -- Text Icons (Unicode )
    ICON_PLUS          : str = "+"
    ICON_MINUS         : str = "-"
    ICON_REMOVE        : str = "\u00d7"  # Multiplication sign
    ICON_PICKER        : str = "\u2299"  # Circled dot
    ICON_WARNING       : str = "\u25b2"  # Triangle
    ICON_ERROR         : str = "\u25cf"  # Filled circle
    ICON_DOT           : str = "\u00b7"  # Middle dot
    ICON_CHECK         : str = "v"  # Check mark

    # -- Image Icon Names ( EditorIcons.get )
    ICON_IMG_PLUS      : str = "plus"
    ICON_IMG_MINUS     : str = "minus"
    ICON_IMG_REMOVE    : str = "remove"
    ICON_IMG_PICKER    : str = "picker"
    ICON_IMG_WARNING   : str = "warning"
    ICON_IMG_ERROR     : str = "error"
    ICON_IMG_UI_CANVAS : str = "ui_canvas"
    ICON_IMG_UI_TEXT   : str = "ui_text"
    ICON_IMG_UI_IMAGE  : str = "ui_image"
    ICON_IMG_UI_BUTTON : str = "ui_button"
    EDITOR_ICON_SIZE   : float = 16.0  # Default icon size (px)

    # ══════════════════════════════════════════════════════════════════════
    #  4. Style Push/Pop Helpers
    #     These methods bundle multiple push operations into single calls.
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def push_ghost_button_style(ctx) -> int:
        """
        Push transparent button colors. Returns color count pushed (3)."""
        ctx.push_style_color(ImGuiCol.Button,        *Theme.BTN_GHOST)
        ctx.push_style_color(ImGuiCol.ButtonHovered,  *Theme.BTN_GHOST_HOVERED)
        ctx.push_style_color(ImGuiCol.ButtonActive,   *Theme.BTN_GHOST_ACTIVE)
        return 3

    @staticmethod
    def push_flat_button_style(ctx, r: float, g: float, b: float, a: float = 1.0) -> int:
        """
        Push flat solid-color button style. Returns 3."""
        ctx.push_style_color(ImGuiCol.Button,        r, g, b, a)
        ctx.push_style_color(ImGuiCol.ButtonHovered,  min(r + 0.06, 1), min(g + 0.06, 1), min(b + 0.06, 1), a)
        ctx.push_style_color(ImGuiCol.ButtonActive,   min(r + 0.12, 1), min(g + 0.12, 1), min(b + 0.12, 1), a)
        return 3

    @staticmethod
    def push_toolbar_vars(ctx) -> int:
        """
        Push compact toolbar spacing preset. Returns var count (5)."""
        ctx.push_style_var_vec2(ImGuiStyleVar.WindowPadding, *Theme.TOOLBAR_WIN_PAD)
        ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding,  *Theme.TOOLBAR_FRAME_PAD)
        ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing,   *Theme.TOOLBAR_ITEM_SPC)
        ctx.push_style_var_float(ImGuiStyleVar.FrameRounding, Theme.TOOLBAR_FRAME_RND)
        ctx.push_style_var_float(ImGuiStyleVar.FrameBorderSize, Theme.TOOLBAR_FRAME_BRD)
        return 5

    @staticmethod
    def push_popup_vars(ctx) -> int:
        """
        Push popup spacing preset. Returns 3."""
        ctx.push_style_var_vec2(ImGuiStyleVar.WindowPadding, *Theme.POPUP_WIN_PAD)
        ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing,   *Theme.POPUP_ITEM_SPC)
        ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding,  *Theme.POPUP_FRAME_PAD)
        return 3

    @staticmethod
    def push_status_bar_button_style(ctx) -> int:
        """
        Push status bar button style. Returns 3."""
        ctx.push_style_color(ImGuiCol.Button,        *Theme.BTN_GHOST)
        ctx.push_style_color(ImGuiCol.ButtonHovered,  *Theme.BTN_SB_HOVERED)
        ctx.push_style_color(ImGuiCol.ButtonActive,   *Theme.BTN_SB_ACTIVE)
        return 3

    @staticmethod
    def push_transparent_border(ctx) -> int:
        """
        Push transparent border color. Returns 1."""
        ctx.push_style_color(ImGuiCol.Border, *Theme.BORDER_TRANSPARENT)
        return 1

    @staticmethod
    def push_drag_drop_target_style(ctx) -> int:
        """
        Push drag-drop target highlight color. Returns 1."""
        ctx.push_style_color(ImGuiCol.DragDropTarget, *Theme.DRAG_DROP_TARGET)
        return 1

    @staticmethod
    def push_console_toolbar_vars(ctx) -> int:
        """
        Push console toolbar compact spacing. Returns 3."""
        ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, *Theme.CONSOLE_FRAME_PAD)
        ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing,  *Theme.CONSOLE_ITEM_SPC)
        ctx.push_style_var_float(ImGuiStyleVar.FrameBorderSize, Theme.TOOLBAR_FRAME_BRD)
        return 3

    @staticmethod
    def push_splitter_style(ctx) -> int:
        """
        Push splitter button style. Returns 3."""
        ctx.push_style_color(ImGuiCol.Button,        *Theme.BTN_GHOST)
        ctx.push_style_color(ImGuiCol.ButtonHovered,  *Theme.SPLITTER_HOVER)
        ctx.push_style_color(ImGuiCol.ButtonActive,   *Theme.SPLITTER_ACTIVE)
        return 3

    @staticmethod
    def push_selected_icon_style(ctx) -> int:
        """
        Push selected icon button highlight. Returns 2."""
        ctx.push_style_color(ImGuiCol.Button,        *Theme.BTN_SELECTED)
        ctx.push_style_color(ImGuiCol.ButtonHovered,  *Theme.BTN_SELECTED)
        return 2

    @staticmethod
    def push_unselected_icon_style(ctx) -> int:
        """
        Push unselected icon button style. Returns 2 colors (caller also pops 1 var)."""
        ctx.push_style_var_float(ImGuiStyleVar.FrameBorderSize, 0.0)
        ctx.push_style_color(ImGuiCol.Button,        *Theme.BTN_GHOST)
        ctx.push_style_color(ImGuiCol.ButtonHovered,  *Theme.BTN_SUBTLE_HOVER)
        return 2

    @staticmethod
    def get_play_border_color(is_paused: bool) -> RGBA:
        """
        Return the play-mode border color."""
        return Theme.BORDER_PAUSE if is_paused else Theme.BORDER_PLAY

    @staticmethod
    def push_inline_button_style(ctx, active: bool = False) -> int:
        """
        Push inline button style. Returns color count (3)."""
        if active:
            ctx.push_style_color(ImGuiCol.Button, *Theme.INSPECTOR_INLINE_BTN_ON)
            ctx.push_style_color(ImGuiCol.ButtonHovered, *Theme.INSPECTOR_INLINE_BTN_ON)
            ctx.push_style_color(ImGuiCol.ButtonActive, *Theme.INSPECTOR_INLINE_BTN_ACTIVE)
        else:
            ctx.push_style_color(ImGuiCol.Button, *Theme.INSPECTOR_INLINE_BTN_IDLE)
            ctx.push_style_color(ImGuiCol.ButtonHovered, *Theme.INSPECTOR_INLINE_BTN_HOVER)
            ctx.push_style_color(ImGuiCol.ButtonActive, *Theme.INSPECTOR_INLINE_BTN_ACTIVE)
        return 3

    @staticmethod
    def render_inline_button_row(
        ctx,
        row_id: str,
        items: Iterable[tuple[str, str]],
        *,
        active_items: Optional[Iterable[str]] = None,
        height: float = 0.0,
        semantic_base: str = "",
    ):
        """
        Render a row of evenly sized buttons. Returns clicked item id."""
        entries = list(items)
        if not entries:
            return None

        active_set = set(active_items or [])
        spacing = Theme.INSPECTOR_INLINE_BTN_GAP
        button_h = height if height > 0.0 else Theme.INSPECTOR_INLINE_BTN_H
        avail_w = max(0.0, ctx.get_content_region_avail_width())
        total_gap = spacing * max(0, len(entries) - 1)
        button_w = max(1.0, (avail_w - total_gap) / max(1, len(entries)))

        clicked = [None]
        for idx, (item_id, label) in enumerate(entries):
            color_count = Theme.push_inline_button_style(ctx, item_id in active_set)

            def _on_click(iid=item_id):
                clicked[0] = iid

            ctx.button(f"{label}##{row_id}_{item_id}", _on_click, width=button_w, height=button_h)
            recorder = getattr(ctx, "record_semantic_item", None)
            if semantic_base and callable(recorder):
                recorder("inline_button", label, True, f"{semantic_base}.{item_id}")
            ctx.pop_style_color(color_count)
            if idx + 1 < len(entries):
                ctx.same_line(0, spacing)
        return clicked[0]


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  4. C++ single source of truth override                                  ║
# ║                                                                          ║
# ║  The authoritative theme values live in C++                              ║
# ║  (cpp/infernux/function/editor/EditorThemeTable.inl). At import time we  ║
# ║  overwrite every matching class attribute above with the native value,   ║
# ║  so editing this Python file cannot change the engine's look — restyle   ║
# ║  in EditorThemeTable.inl instead. Native registry availability is an     ║
# ║  Editor startup contract; this module does not provide a second theme.    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _apply_native_theme_overrides() -> None:
    from Infernux.lib import (
        get_editor_theme_colors,
        get_editor_theme_floats,
        get_editor_theme_vec2s,
    )

    applied = 0
    for name, value in get_editor_theme_colors().items():
        if hasattr(Theme, name):
            setattr(Theme, name, tuple(value))
            applied += 1
    for name, value in get_editor_theme_vec2s().items():
        if hasattr(Theme, name):
            setattr(Theme, name, tuple(value))
            applied += 1
    for name, value in get_editor_theme_floats().items():
        if hasattr(Theme, name):
            setattr(Theme, name, float(value))
            applied += 1
    Theme._NATIVE_OVERRIDES_APPLIED = applied


def _native_theme_generation() -> int:
    from Infernux.lib import editor_theme_generation

    return int(editor_theme_generation())


def set_editor_theme(name: str) -> bool:
    """Switch the active editor theme.

    The C++ registry re-skins every built-in ImGui widget (C++ and Python
    panels) in one call; we then refresh the Python-side ``Theme`` tokens used
    by custom drawing. Returns ``True`` on success.
    """
    from Infernux.lib import set_editor_theme as _native_set

    ok = bool(_native_set(name))
    if ok:
        _apply_native_theme_overrides()
        Theme._NATIVE_THEME_GENERATION = _native_theme_generation()
    return ok


def list_editor_themes() -> list:
    """Return the names of all registered editor themes."""
    from Infernux.lib import list_editor_themes as _native_list

    return list(_native_list())


def active_editor_theme() -> str:
    """Return the active editor theme name."""
    from Infernux.lib import get_editor_theme as _native_get

    return str(_native_get())


# Convenience API on the Theme class: Theme.set_theme("amber") / Theme.list_themes()
Theme.set_theme = staticmethod(set_editor_theme)
Theme.list_themes = staticmethod(list_editor_themes)
Theme.active_theme = staticmethod(active_editor_theme)
Theme.refresh = staticmethod(_apply_native_theme_overrides)

Theme._NATIVE_OVERRIDES_APPLIED = 0
Theme._NATIVE_THEME_GENERATION = 0
_apply_native_theme_overrides()
Theme._NATIVE_THEME_GENERATION = _native_theme_generation()
