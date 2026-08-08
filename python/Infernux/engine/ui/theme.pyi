"""Type stubs for Infernux.engine.ui.theme."""

from __future__ import annotations

from typing import Iterable, Tuple, Any

RGBA = Tuple[float, float, float, float]

def srgb_to_linear(s: float) -> float: ...
def srgb3(r: float, g: float, b: float, a: float = 1.0) -> RGBA: ...
def hex_to_linear(hex_r: int, hex_g: int, hex_b: int, a: float = 1.0) -> RGBA: ...


class ImGuiCol:
    """ImGui color indices matching the ``imgui.h`` ``ImGuiCol_`` enum."""

    Text: int
    TextDisabled: int
    WindowBg: int
    ChildBg: int
    PopupBg: int
    Border: int
    BorderShadow: int
    FrameBg: int
    FrameBgHovered: int
    FrameBgActive: int
    TitleBg: int
    TitleBgActive: int
    TitleBgCollapsed: int
    MenuBarBg: int
    ScrollbarBg: int
    ScrollbarGrab: int
    ScrollbarGrabHovered: int
    ScrollbarGrabActive: int
    CheckMark: int
    SliderGrab: int
    SliderGrabActive: int
    Button: int
    ButtonHovered: int
    ButtonActive: int
    Header: int
    HeaderHovered: int
    HeaderActive: int
    Separator: int
    SeparatorHovered: int
    SeparatorActive: int
    ResizeGrip: int
    ResizeGripHovered: int
    ResizeGripActive: int
    InputTextCursor: int
    TabHovered: int
    Tab: int
    TabSelected: int
    TabSelectedOverline: int
    TabDimmed: int
    TabDimmedSelected: int
    TabDimmedSelectedOverline: int
    DockingPreview: int
    DockingEmptyBg: int
    PlotLines: int
    PlotLinesHovered: int
    PlotHistogram: int
    PlotHistogramHovered: int
    TableHeaderBg: int
    TableBorderStrong: int
    TableBorderLight: int
    TableRowBg: int
    TableRowBgAlt: int
    TextLink: int
    TextSelectedBg: int
    TreeLines: int
    DragDropTarget: int
    DragDropTargetBg: int
    UnsavedMarker: int
    NavCursor: int
    NavWindowingHighlight: int
    NavWindowingDimBg: int
    ModalWindowDimBg: int


class ImGuiWindowFlags:
    """ImGui window flags matching the ``imgui.h`` ``ImGuiWindowFlags_`` enum."""

    NoTitleBar: int
    NoResize: int
    NoMove: int
    NoScrollbar: int
    NoScrollWithMouse: int
    NoCollapse: int
    AlwaysAutoResize: int
    NoBackground: int
    NoSavedSettings: int
    NoMouseInputs: int
    NoFocusOnAppearing: int
    NoBringToFrontOnFocus: int
    NoNavInputs: int
    NoNavFocus: int
    UnsavedDocument: int
    NoDocking: int
    NoNav: int
    NoDecoration: int
    NoInputs: int


class ImGuiTreeNodeFlags:
    """ImGui tree-node flags matching the ``imgui.h`` ``ImGuiTreeNodeFlags_`` enum."""

    Selected: int
    Framed: int
    AllowOverlap: int
    NoTreePushOnOpen: int
    NoAutoOpenOnLog: int
    DefaultOpen: int
    OpenOnDoubleClick: int
    OpenOnArrow: int
    Leaf: int
    Bullet: int
    FramePadding: int
    SpanAvailWidth: int
    SpanFullWidth: int
    SpanAllColumns: int
    CollapsingHeader: int


class ImGuiMouseCursor:
    """ImGui mouse cursor enum."""

    Arrow: int
    TextInput: int
    ResizeAll: int
    ResizeNS: int
    ResizeEW: int
    ResizeNESW: int
    ResizeNWSE: int
    Hand: int


class ImGuiStyleVar:
    """ImGui style variable indices matching the ``imgui.h`` ``ImGuiStyleVar_`` enum."""

    Alpha: int
    DisabledAlpha: int
    WindowPadding: int
    WindowRounding: int
    WindowBorderSize: int
    WindowMinSize: int
    WindowTitleAlign: int
    ChildRounding: int
    ChildBorderSize: int
    PopupRounding: int
    PopupBorderSize: int
    FramePadding: int
    FrameRounding: int
    FrameBorderSize: int
    ItemSpacing: int
    ItemInnerSpacing: int
    IndentSpacing: int
    CellPadding: int
    ScrollbarSize: int
    ScrollbarRounding: int
    ScrollbarPadding: int
    GrabMinSize: int
    GrabRounding: int
    ImageBorderSize: int
    TabRounding: int
    TabBorderSize: int
    TabMinWidthBase: int
    TabMinWidthShrink: int
    TabBarBorderSize: int
    TabBarOverlineSize: int
    TableAngledHeadersAngle: int
    TableAngledHeadersTextAlign: int
    TreeLinesSize: int
    TreeLinesRounding: int
    ButtonTextAlign: int
    SelectableTextAlign: int
    SeparatorTextBorderSize: int
    SeparatorTextAlign: int
    SeparatorTextPadding: int
    DockingSeparatorSize: int


class Theme:
    """Central theme for the Infernux Editor.

    This class is the single source of truth for editor colors, sizes, icons,
    and layout constants. Modify the values here to customize the editor
    appearance.
    """

    TEXT: RGBA
    TEXT_DISABLED: RGBA
    TEXT_DIM: RGBA
    WINDOW_BG: RGBA
    CHILD_BG: RGBA
    POPUP_BG: RGBA
    MENU_BAR_BG: RGBA
    STATUS_BAR_BG: RGBA
    BORDER: RGBA
    BORDER_TRANSPARENT: RGBA
    BORDER_SHADOW: RGBA
    FRAME_BG: RGBA
    FRAME_BG_HOVERED: RGBA
    FRAME_BG_ACTIVE: RGBA
    BTN_NORMAL: RGBA
    BTN_HOVERED: RGBA
    BTN_ACTIVE: RGBA
    BTN_GHOST: RGBA
    BTN_GHOST_HOVERED: RGBA
    BTN_GHOST_ACTIVE: RGBA
    BTN_SB_HOVERED: RGBA
    BTN_SB_ACTIVE: RGBA
    BTN_SELECTED: RGBA
    BTN_SUBTLE_HOVER: RGBA
    PLAY_ACTIVE: RGBA
    PAUSE_ACTIVE: RGBA
    BTN_IDLE: RGBA
    BTN_DISABLED: RGBA
    APPLY_BUTTON: RGBA
    HEADER: RGBA
    HEADER_HOVERED: RGBA
    HEADER_ACTIVE: RGBA
    SELECTION_BG: RGBA
    SPLITTER_HOVER: RGBA
    SPLITTER_ACTIVE: RGBA
    DRAG_DROP_TARGET: RGBA
    DND_DROP_OUTLINE: RGBA
    DND_DROP_OUTLINE_THICKNESS: float
    DND_REORDER_LINE: RGBA
    DND_REORDER_LINE_THICKNESS: float
    DND_REORDER_SEPARATOR_H: float
    LOG_INFO: RGBA
    LOG_WARNING: RGBA
    LOG_ERROR: RGBA
    LOG_TRACE: RGBA
    LOG_BADGE: RGBA
    LOG_DIM: RGBA
    META_TEXT: RGBA
    SUCCESS_TEXT: RGBA
    WARNING_TEXT: RGBA
    ERROR_TEXT: RGBA
    PREFAB_TEXT: RGBA
    PREFAB_HEADER_BG: RGBA
    PREFAB_HEADER_H: float
    PREFAB_HEADER_BTN_GAP: float
    PREFAB_BTN_NORMAL: RGBA
    PREFAB_BTN_HOVERED: RGBA
    PREFAB_BTN_ACTIVE: RGBA
    ROW_ALT: RGBA
    ROW_NONE: RGBA
    BORDER_PLAY: RGBA
    BORDER_PAUSE: RGBA
    BORDER_THICKNESS: float
    INSPECTOR_INIT_SIZE: Tuple[float, float]
    INSPECTOR_MIN_PROPS_H: int
    INSPECTOR_MIN_RAWDATA_H: int
    INSPECTOR_SPLITTER_H: int
    INSPECTOR_DEFAULT_RATIO: float
    INSPECTOR_LABEL_PAD: float
    INSPECTOR_MIN_LABEL_WIDTH: float
    INSPECTOR_FRAME_PAD: Tuple[float, float]
    INSPECTOR_ITEM_SPC: Tuple[float, float]
    INSPECTOR_SUBITEM_SPC: Tuple[float, float]
    INSPECTOR_SECTION_GAP: float
    INSPECTOR_TITLE_GAP: float
    INSPECTOR_HEADER_PRIMARY_FRAME_PAD: Tuple[float, float]
    INSPECTOR_HEADER_SECONDARY_FRAME_PAD: Tuple[float, float]
    INSPECTOR_HEADER_PRIMARY_FONT_SCALE: float
    INSPECTOR_HEADER_SECONDARY_FONT_SCALE: float
    INSPECTOR_HEADER_ITEM_SPC: Tuple[float, float]
    INSPECTOR_HEADER_BORDER_SIZE: float
    INSPECTOR_ACTION_ALIGN_X: float
    INSPECTOR_HEADER_CONTENT_INDENT: float
    ADD_COMP_SEARCH_W: int
    COMPONENT_ICON_SIZE: int
    COMP_ENABLED_CB_OFFSET: int
    INSPECTOR_CHECKBOX_BOX_SCALE: float
    INSPECTOR_CHECKBOX_FRAME_PAD: Tuple[float, float]
    INSPECTOR_CHECKBOX_SLOT_W: float
    INSPECTOR_HEADER_PRIMARY: RGBA
    INSPECTOR_HEADER_PRIMARY_HOVERED: RGBA
    INSPECTOR_HEADER_PRIMARY_ACTIVE: RGBA
    INSPECTOR_HEADER_SELECTED: RGBA
    INSPECTOR_HEADER_SELECTED_HOVERED: RGBA
    INSPECTOR_HEADER_SELECTED_ACTIVE: RGBA
    INSPECTOR_HEADER_SECONDARY: RGBA
    INSPECTOR_HEADER_SECONDARY_HOVERED: RGBA
    INSPECTOR_HEADER_SECONDARY_ACTIVE: RGBA
    INSPECTOR_INLINE_BTN_IDLE: RGBA
    INSPECTOR_INLINE_BTN_HOVER: RGBA
    INSPECTOR_INLINE_BTN_ACTIVE: RGBA
    INSPECTOR_INLINE_BTN_ON: RGBA
    INSPECTOR_INLINE_BTN_GAP: float
    INSPECTOR_INLINE_BTN_H: float
    COLOR_SWATCH_BORDER: RGBA
    TOOLBAR_WIN_PAD: Tuple[float, float]
    TOOLBAR_FRAME_PAD: Tuple[float, float]
    TOOLBAR_ITEM_SPC: Tuple[float, float]
    TOOLBAR_FRAME_RND: float
    TOOLBAR_FRAME_BRD: float
    POPUP_WIN_PAD: Tuple[float, float]
    POPUP_ITEM_SPC: Tuple[float, float]
    POPUP_FRAME_PAD: Tuple[float, float]
    POPUP_ADD_COMP_PAD: Tuple[float, float]
    POPUP_ADD_COMP_SPC: Tuple[float, float]
    ADD_COMP_FRAME_PAD: Tuple[float, float]
    TREE_ITEM_SPC: Tuple[float, float]
    TREE_FRAME_PAD: Tuple[float, float]
    TREE_INDENT: float
    TREE_ROW_ALT_BG: RGBA
    TREE_DND_LINE_CLR: RGBA
    PREFAB_ICON: str
    CONSOLE_FRAME_PAD: Tuple[float, float]
    CONSOLE_ITEM_SPC: Tuple[float, float]
    STATUS_BAR_WIN_PAD: Tuple[float, float]
    STATUS_BAR_ITEM_SPC: Tuple[float, float]
    STATUS_BAR_FRAME_PAD: Tuple[float, float]
    STATUS_PROGRESS_FRACTION: float
    STATUS_PROGRESS_H: float
    STATUS_PROGRESS_CLR: RGBA
    STATUS_PROGRESS_BG: RGBA
    STATUS_PROGRESS_LABEL_CLR: RGBA
    ICON_BTN_NO_PAD: Tuple[float, float]
    PROJECT_PANEL_PAD: Tuple[float, float]
    SCENE_GIZMO_TOOL_BTN_W: float
    SCENE_GIZMO_TOOL_BTN_H: float
    SCENE_GIZMO_TOOL_BTN_GAP: float
    SCENE_GIZMO_TOOL_BTN_PAD: Tuple[float, float]
    SCENE_COORD_DROPDOWN_W: float
    SCENE_ORIENT_RADIUS: float
    SCENE_ORIENT_MARGIN: float
    SCENE_ORIENT_AXIS_LEN: float
    SCENE_ORIENT_END_RADIUS: float
    SCENE_ORIENT_NEG_RADIUS: float
    SCENE_ORIENT_BG: RGBA
    SCENE_ORIENT_FLY_DURATION: float
    SCENE_OVERLAY_COMBO_BG: RGBA
    SCENE_OVERLAY_COMBO_HOVER: RGBA
    SCENE_OVERLAY_COMBO_ACTIVE: RGBA
    SCENE_OVERLAY_ROUNDING: float
    SCENE_OVERLAY_BORDER_SIZE: float
    UI_EDITOR_CANVAS_BG: RGBA
    UI_EDITOR_CANVAS_BORDER: RGBA
    UI_EDITOR_ELEMENT_HOVER: RGBA
    UI_EDITOR_ELEMENT_SELECT: RGBA
    UI_EDITOR_HANDLE_COLOR: RGBA
    UI_EDITOR_HANDLE_SIZE: float
    UI_EDITOR_TOOLBAR_HEIGHT: float
    UI_EDITOR_MIN_ZOOM: float
    UI_EDITOR_MAX_ZOOM: float
    UI_EDITOR_ZOOM_STEP: float
    UI_EDITOR_LABEL_OFFSET: float
    UI_EDITOR_LABEL_COLOR: RGBA
    UI_EDITOR_ROTATE_DISTANCE: float
    UI_EDITOR_ROTATE_RADIUS: float
    UI_EDITOR_ROTATE_HIT_R: float
    UI_EDITOR_EDGE_HIT_TOL: float
    UI_EDITOR_SELECT_LINE_W: float
    UI_EDITOR_ROTATE_LINE_W: float
    UI_EDITOR_MIN_ELEM_SIZE: float
    UI_EDITOR_PLACEHOLDER_TINT: float
    UI_EDITOR_PLACEHOLDER_ALPHA: float
    UI_EDITOR_FALLBACK_TEXT: RGBA
    UI_EDITOR_INIT_WINDOW_W: float
    UI_EDITOR_INIT_WINDOW_H: float
    UI_EDITOR_FIT_MARGIN: float
    UI_EDITOR_TOOLBAR_GAP: float
    UI_EDITOR_TOOLBAR_SECTION_GAP: float
    UI_EDITOR_CREATE_BTN_W: float
    UI_EDITOR_CREATE_BTN_H: float
    UI_EDITOR_NEW_TEXT_POS: Tuple[float, float]
    UI_EDITOR_NEW_IMAGE_SIZE: Tuple[float, float]
    UI_EDITOR_NEW_IMAGE_POS: Tuple[float, float]
    UI_EDITOR_NEW_BUTTON_SIZE: Tuple[float, float]
    UI_EDITOR_NEW_BUTTON_POS: Tuple[float, float]
    UI_EDITOR_SNAP_TABLE: tuple
    UI_EDITOR_SNAP_DEFAULT: int
    UI_EDITOR_ALIGN_GUIDE: RGBA
    UI_EDITOR_ALIGN_GUIDE_FAINT: RGBA
    UI_EDITOR_ALIGN_GUIDE_W: float
    UI_EDITOR_ALIGN_SNAP_PX: float
    UI_EDITOR_ALIGN_BTN_W: float
    UI_EDITOR_ALIGN_BTN_H: float
    UI_EDITOR_ALIGN_BTN_GAP: float
    UI_DEFAULT_BUTTON_BG: RGBA
    UI_DEFAULT_LABEL_COLOR: RGBA
    UI_DEFAULT_FONT_SIZE: float
    UI_DEFAULT_LINE_HEIGHT: float
    UI_DEFAULT_LETTER_SPACING: float
    BUILD_SETTINGS_ROW_SPC: Tuple[float, float]
    WINDOW_FLAGS_VIEWPORT: int
    WINDOW_FLAGS_NO_SCROLL: int
    WINDOW_FLAGS_NO_DECOR: int
    WINDOW_FLAGS_FLOATING: int
    WINDOW_FLAGS_DIALOG: int
    COND_FIRST_USE_EVER: int
    COND_ALWAYS: int
    BORDER_SIZE_NONE: float
    ICON_PLUS: str
    ICON_MINUS: str
    ICON_REMOVE: str
    ICON_PICKER: str
    ICON_WARNING: str
    ICON_ERROR: str
    ICON_DOT: str
    ICON_CHECK: str
    ICON_IMG_PLUS: str
    ICON_IMG_MINUS: str
    ICON_IMG_REMOVE: str
    ICON_IMG_PICKER: str
    ICON_IMG_WARNING: str
    ICON_IMG_ERROR: str
    ICON_IMG_UI_TEXT: str
    ICON_IMG_UI_IMAGE: str
    ICON_IMG_UI_BUTTON: str
    EDITOR_ICON_SIZE: float
    @staticmethod
    def push_ghost_button_style(ctx: Any) -> int: ...
    @staticmethod
    def push_flat_button_style(ctx: Any, r: float, g: float, b: float, a: float) -> int: ...
    @staticmethod
    def push_toolbar_vars(ctx: Any) -> int: ...
    @staticmethod
    def push_popup_vars(ctx: Any) -> int: ...
    @staticmethod
    def push_status_bar_button_style(ctx: Any) -> int: ...
    @staticmethod
    def push_transparent_border(ctx: Any) -> int: ...
    @staticmethod
    def push_drag_drop_target_style(ctx: Any) -> int: ...
    @staticmethod
    def push_console_toolbar_vars(ctx: Any) -> int: ...
    @staticmethod
    def push_splitter_style(ctx: Any) -> int: ...
    @staticmethod
    def push_selected_icon_style(ctx: Any) -> int: ...
    @staticmethod
    def push_unselected_icon_style(ctx: Any) -> int: ...
    @staticmethod
    def get_play_border_color(is_paused: bool) -> RGBA: ...
    @staticmethod
    def push_inline_button_style(ctx: Any, active: bool) -> int: ...
    @staticmethod
    def render_inline_button_row(ctx: Any, row_id: str, items: Iterable[tuple[str, str]]) -> None: ...

