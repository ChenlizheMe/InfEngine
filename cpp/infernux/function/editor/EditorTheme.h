#pragma once

#include <imgui.h>

#include <algorithm>

namespace infernux
{

/// Lightweight theme constants for C++ editor panels.
/// Matches the Python Theme class values from theme.py.
/// Colors are in linear sRGB, RGBA order (matching ImGui's convention).
namespace EditorTheme
{

// ── Accent / brand color (Infernux red #EB5757) ─────────────────────
constexpr float ACCENT_R = 0.922f;
constexpr float ACCENT_G = 0.341f;
constexpr float ACCENT_B = 0.341f;

// ── Console log level colors ─────────────────────────────────────────
constexpr ImVec4 LOG_INFO{0.82f, 0.82f, 0.85f, 1.0f};
constexpr ImVec4 LOG_WARNING{0.890f, 0.710f, 0.300f, 1.0f};
constexpr ImVec4 LOG_ERROR{ACCENT_R, ACCENT_G, ACCENT_B, 1.0f};
constexpr ImVec4 LOG_TRACE{0.50f, 0.50f, 0.50f, 1.0f};
constexpr ImVec4 LOG_BADGE{0.55f, 0.55f, 0.55f, 1.0f};
constexpr ImVec4 LOG_DIM{0.133f, 0.133f, 0.133f, 0.6f};

// ── Selection / Row colors ───────────────────────────────────────────
constexpr ImVec4 SELECTION_BG{ACCENT_R, ACCENT_G, ACCENT_B, 1.0f};
constexpr ImVec4 ROW_ALT{0.0f, 0.0f, 0.0f, 0.06f};
constexpr ImVec4 ROW_NONE{0.0f, 0.0f, 0.0f, 0.0f};
constexpr ImVec4 CONSOLE_SEGMENT_ACTIVE{0.23f, 0.23f, 0.25f, 1.0f};

// ── Transparent / ghost ──────────────────────────────────────────────
constexpr ImVec4 BTN_GHOST{0.0f, 0.0f, 0.0f, 0.0f};
constexpr ImVec4 BTN_GHOST_HOVERED{1.0f, 1.0f, 1.0f, 0.06f};
constexpr ImVec4 BTN_GHOST_ACTIVE{1.0f, 1.0f, 1.0f, 0.10f};
constexpr ImVec4 BORDER_TRANSPARENT{0.0f, 0.0f, 0.0f, 0.0f};

// ── Status bar button ────────────────────────────────────────────────
constexpr ImVec4 BTN_SB_HOVERED{0.20f, 0.20f, 0.20f, 0.6f};
constexpr ImVec4 BTN_SB_ACTIVE{0.25f, 0.25f, 0.25f, 0.8f};

// ── Toolbar play-mode buttons ────────────────────────────────────────
constexpr ImVec4 PLAY_ACTIVE{0.20f, 0.45f, 0.30f, 1.0f};
constexpr ImVec4 PAUSE_ACTIVE{0.50f, 0.40f, 0.15f, 1.0f};
constexpr ImVec4 BTN_IDLE{0.18f, 0.18f, 0.18f, 1.0f};
constexpr ImVec4 BTN_DISABLED{0.15f, 0.15f, 0.15f, 0.4f};

// ── Headers / Selectables ────────────────────────────────────────────
constexpr ImVec4 HEADER_HOVERED{0.28f, 0.24f, 0.24f, 1.0f};
constexpr ImVec4 HEADER_ACTIVE{0.32f, 0.25f, 0.25f, 1.0f};

// ── Panel backgrounds ────────────────────────────────────────────────
constexpr ImVec4 STATUS_BAR_BG{0.13f, 0.13f, 0.13f, 1.0f};
constexpr ImVec4 MENU_BAR_BG{0.16f, 0.16f, 0.16f, 1.0f};
constexpr ImVec4 POPUP_BG{0.24f, 0.24f, 0.24f, 0.96f};

// ── Status bar progress ──────────────────────────────────────────────
constexpr ImVec4 STATUS_PROGRESS_CLR{235.0f / 255.0f, 87.0f / 255.0f, 87.0f / 255.0f, 1.0f};
constexpr ImVec4 STATUS_PROGRESS_BG{0.10f, 0.10f, 0.10f, 1.0f};
constexpr ImVec4 STATUS_PROGRESS_LABEL_CLR{0.65f, 0.65f, 0.65f, 1.0f};
constexpr ImVec4 STATUS_ACTIVITY_BG{0.155f, 0.155f, 0.165f, 1.0f};
constexpr ImVec4 STATUS_BAR_BORDER{0.26f, 0.26f, 0.28f, 1.0f};
constexpr float STATUS_PROGRESS_FRACTION = 0.25f;

// ── Splitter ─────────────────────────────────────────────────────────
constexpr ImVec4 SPLITTER_HOVER{0.35f, 0.25f, 0.25f, 0.6f};
constexpr ImVec4 SPLITTER_ACTIVE{0.40f, 0.28f, 0.28f, 0.8f};

// ── Console toolbar compact spacing ──────────────────────────────────
constexpr float CONSOLE_FRAME_PAD_X = 4.0f;
constexpr float CONSOLE_FRAME_PAD_Y = 3.0f;
constexpr float CONSOLE_ITEM_SPC_X = 6.0f;
constexpr float CONSOLE_ITEM_SPC_Y = 4.0f;
constexpr float TOOLBAR_FRAME_BRD = 0.0f;

// ── Toolbar spacing ──────────────────────────────────────────────────
constexpr ImVec2 TOOLBAR_WIN_PAD{4.0f, 4.0f};
constexpr ImVec2 TOOLBAR_FRAME_PAD{6.0f, 4.0f};
constexpr ImVec2 TOOLBAR_ITEM_SPC{6.0f, 4.0f};
constexpr float TOOLBAR_FRAME_RND = 3.0f;

// ── Menu bar spacing ─────────────────────────────────────────────────
// (reuses TOOLBAR_FRAME_PAD / TOOLBAR_ITEM_SPC / TOOLBAR_WIN_PAD)

// ── Popup spacing ────────────────────────────────────────────────────
constexpr ImVec2 POPUP_WIN_PAD{12.0f, 10.0f};
constexpr ImVec2 POPUP_ITEM_SPC{10.0f, 8.0f};
constexpr ImVec2 POPUP_FRAME_PAD{8.0f, 6.0f};

// ── Status bar spacing ───────────────────────────────────────────────
constexpr ImVec2 STATUS_BAR_WIN_PAD{6.0f, 4.0f};
constexpr ImVec2 STATUS_BAR_ITEM_SPC{8.0f, 0.0f};
constexpr ImVec2 STATUS_BAR_FRAME_PAD{0.0f, 0.0f};
constexpr float STATUS_BAR_BASE_HEIGHT = 24.0f;

// ── Hierarchy / Tree ─────────────────────────────────────────────────
constexpr ImVec2 TREE_ITEM_SPC{0.0f, 2.0f};  // ItemSpacing
constexpr ImVec2 TREE_FRAME_PAD{2.0f, 3.0f}; // FramePadding
constexpr float TREE_INDENT = 14.0f;         // IndentSpacing per level
constexpr ImVec4 PREFAB_TEXT{235.0f / 255.0f, 87.0f / 255.0f, 87.0f / 255.0f, 1.0f};
constexpr ImVec4 TEXT_DISABLED{0.40f, 0.40f, 0.40f, 1.0f};
constexpr ImVec4 DND_REORDER_LINE{1.0f, 1.0f, 1.0f, 0.90f};
constexpr ImVec4 DND_PARENT_OUTLINE{1.0f, 1.0f, 1.0f, 0.95f};

// ── Project panel — model expand strip & embedded sub-assets ─────────
constexpr ImVec4 PROJECT_EXPAND_STRIP_BG{0.22f, 0.22f, 0.24f, 1.0f};
constexpr ImVec4 PROJECT_EXPAND_STRIP_HOVER{0.32f, 0.28f, 0.28f, 1.0f};
constexpr ImVec4 PROJECT_SUBASSET_CELL_BG{0.17f, 0.17f, 0.19f, 0.94f};
constexpr ImVec4 PROJECT_SUBASSET_ACCENT{ACCENT_R * 0.70f, ACCENT_G * 0.70f, ACCENT_B * 0.70f, 0.90f};
constexpr float PROJECT_SUBASSET_ACCENT_W = 3.0f;

constexpr float DND_REORDER_LINE_THICKNESS = 2.0f;
constexpr float DND_PARENT_OUTLINE_THICKNESS = 1.5f;
constexpr float DND_REORDER_SEPARATOR_H = 6.0f;
constexpr const char *PREFAB_ICON = "\xe2\x97\x86"; // ◆ U+25C6

// ── Icons (Unicode glyphs) ───────────────────────────────────────────
constexpr const char *ICON_WARNING = "\xe2\x96\xb2"; // ▲ U+25B2
constexpr const char *ICON_ERROR = "\xe2\x97\x8f";   // ● U+25CF
// ── Inspector panel ──────────────────────────────────────────────
constexpr ImVec4 TEXT{0.82f, 0.82f, 0.85f, 1.0f};
constexpr ImVec4 TEXT_DIM2{0.55f, 0.55f, 0.55f, 1.0f};
constexpr ImVec4 WARNING_TEXT{0.90f, 0.60f, 0.20f, 1.0f};
constexpr ImVec4 ERROR_TEXT{0.90f, 0.30f, 0.30f, 1.0f};
constexpr ImVec4 PREFAB_TEXT2{ACCENT_R, ACCENT_G, ACCENT_B, 1.0f};

constexpr ImVec4 PREFAB_HEADER_BG{0.235f, 0.235f, 0.235f, 1.0f};
constexpr float PREFAB_HEADER_H = 28.0f;
constexpr float PREFAB_HEADER_BTN_GAP = 4.0f;

constexpr ImVec2 INSPECTOR_INIT_SIZE{300.0f, 500.0f};
constexpr float INSPECTOR_MIN_PROPS_H = 100.0f;
constexpr float INSPECTOR_MIN_RAWDATA_H = 100.0f;
constexpr float INSPECTOR_SPLITTER_H = 8.0f;
constexpr float INSPECTOR_DEFAULT_RATIO = 0.4f;
constexpr float INSPECTOR_LABEL_PAD = 12.0f;
constexpr float INSPECTOR_MIN_LABEL_WIDTH = 132.0f;
constexpr ImVec2 INSPECTOR_FRAME_PAD{4.0f, 2.0f};
constexpr ImVec2 INSPECTOR_ITEM_SPC{4.0f, 2.0f};
constexpr float INSPECTOR_SECTION_GAP = 6.0f;
constexpr float INSPECTOR_TITLE_GAP = 10.0f;
constexpr float INSPECTOR_ACTION_ALIGN_X = 0.0f;
constexpr float INSPECTOR_HEADER_CONTENT_INDENT = 34.0f;
constexpr float COMPONENT_ICON_SIZE = 16.0f;

constexpr ImVec2 INSPECTOR_HEADER_PRIMARY_FRAME_PAD{4.0f, 2.0f};
constexpr ImVec2 INSPECTOR_HEADER_SECONDARY_FRAME_PAD{4.0f, 2.0f};
constexpr ImVec2 INSPECTOR_HEADER_ITEM_SPC{4.0f, 2.0f};
constexpr float INSPECTOR_HEADER_BORDER_SIZE = 0.0f;
constexpr float INSPECTOR_HEADER_RIGHT_MARGIN = 0.0f;
constexpr float INSPECTOR_HEADER_PRIMARY_FONT_SCALE = 1.0f;
constexpr float INSPECTOR_HEADER_SECONDARY_FONT_SCALE = 1.0f;

constexpr ImVec4 INSPECTOR_HEADER_PRIMARY{0.235f, 0.235f, 0.235f, 1.0f};
constexpr ImVec4 INSPECTOR_HEADER_PRIMARY_HOVERED{0.28f, 0.24f, 0.24f, 1.0f};
constexpr ImVec4 INSPECTOR_HEADER_PRIMARY_ACTIVE{0.32f, 0.25f, 0.25f, 1.0f};
constexpr ImVec4 INSPECTOR_HEADER_SELECTED{0.40f, 0.22f, 0.22f, 1.0f};
constexpr ImVec4 INSPECTOR_HEADER_SELECTED_HOVERED{0.46f, 0.24f, 0.24f, 1.0f};
constexpr ImVec4 INSPECTOR_HEADER_SELECTED_ACTIVE{0.50f, 0.26f, 0.26f, 1.0f};
constexpr ImVec4 INSPECTOR_HEADER_SECONDARY{0.20f, 0.20f, 0.20f, 1.0f};
constexpr ImVec4 INSPECTOR_HEADER_SECONDARY_HOVERED{0.25f, 0.22f, 0.22f, 1.0f};
constexpr ImVec4 INSPECTOR_HEADER_SECONDARY_ACTIVE{0.28f, 0.24f, 0.24f, 1.0f};

constexpr ImVec2 INSPECTOR_CHECKBOX_FRAME_PAD{3.0f, 1.5f};
/// Scales only the checkbox square (label text stays at ambient font size).
constexpr float INSPECTOR_CHECKBOX_BOX_SCALE = 0.75f;
constexpr float INSPECTOR_CHECKBOX_SLOT_W = 16.5f;
/// Fixed side length (px) of every checkbox square — one size everywhere.
constexpr float INSPECTOR_CHECKBOX_BOX_PX = 14.0f;
/// Fixed width of the numeric input box beside a ranged slider row.
constexpr float INSPECTOR_RANGED_INPUT_WIDTH = 52.0f;
constexpr float INSPECTOR_SLIDER_LINE_PAD = 2.0f;
constexpr float INSPECTOR_SLIDER_TRACK_HEIGHT = 3.0f;
/// Negative values move the track line upward within the slider row.
constexpr float INSPECTOR_SLIDER_TRACK_Y_OFFSET = -2.0f;

constexpr ImVec4 INSPECTOR_INLINE_BTN_IDLE{0.20f, 0.20f, 0.20f, 1.0f};
constexpr ImVec4 INSPECTOR_INLINE_BTN_HOVER{0.28f, 0.24f, 0.24f, 1.0f};
constexpr ImVec4 INSPECTOR_INLINE_BTN_ACTIVE{ACCENT_R, ACCENT_G, ACCENT_B, 1.0f};
constexpr float INSPECTOR_INLINE_BTN_GAP = 4.0f;

constexpr float ADD_COMP_SEARCH_W = 240.0f;
constexpr ImVec2 POPUP_ADD_COMP_PAD{10.0f, 8.0f};
constexpr ImVec2 POPUP_ADD_COMP_SPC{6.0f, 4.0f};
constexpr ImVec2 ADD_COMP_FRAME_PAD{6.0f, 6.0f};
// ── Helper: push flat-color button style (3 colors) ─────────────────
inline void PushFlatButtonStyle(const ImVec4 &base)
{
    ImGui::PushStyleColor(ImGuiCol_Button, base);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
                          ImVec4{(std::min)(base.x + 0.06f, 1.0f), (std::min)(base.y + 0.06f, 1.0f),
                                 (std::min)(base.z + 0.06f, 1.0f), base.w});
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,
                          ImVec4{(std::min)(base.x + 0.12f, 1.0f), (std::min)(base.y + 0.12f, 1.0f),
                                 (std::min)(base.z + 0.12f, 1.0f), base.w});
}

inline void PushGhostButtonStyle()
{
    ImGui::PushStyleColor(ImGuiCol_Button, BTN_GHOST);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, BTN_GHOST_HOVERED);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, BTN_GHOST_ACTIVE);
}

} // namespace EditorTheme

} // namespace infernux
