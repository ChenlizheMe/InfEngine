#include "EditorThemeRegistry.h"

#include <stdexcept>

namespace infernux
{
namespace
{

struct ThemeData
{
    std::unordered_map<std::string, ImVec4> colors;
    std::unordered_map<std::string, ImVec2> vec2s;
    std::unordered_map<std::string, float> floats;
};

ThemeData BuildBaseTheme()
{
    ThemeData t;
#define INX_THEME_COLOR(name, r, g, b, a) t.colors.emplace(#name, ImVec4(r, g, b, a));
#define INX_THEME_VEC2(name, x, y) t.vec2s.emplace(#name, ImVec2(x, y));
#define INX_THEME_FLOAT(name, v) t.floats.emplace(#name, v);
#include "EditorThemeTable.inl"
#undef INX_THEME_COLOR
#undef INX_THEME_VEC2
#undef INX_THEME_FLOAT
    return t;
}

// A theme is fully described by a small set of semantic ROLE_* colors; the
// ImGui global palette is composed from them (see ApplyImGuiColors). Switching
// themes therefore only needs to override these roles.
ThemeData MakeAccentVariant(const ThemeData &base, const ImVec4 &accent)
{
    ThemeData v = base;
    v.colors["ROLE_ACCENT"] = accent;
    v.colors["APPLY_BUTTON"] = accent; // keep custom-drawn accent in sync
    v.colors["LOG_ERROR"] = accent;
    v.colors["PREFAB_TEXT"] = accent;
    v.colors["STATUS_PROGRESS_CLR"] = accent;
    return v;
}

struct Registry
{
    std::unordered_map<std::string, ThemeData> themes;
    std::vector<std::string> order;
    std::string active;
    unsigned long generation = 1;

    Registry()
    {
        ThemeData base = BuildBaseTheme();
        Add("infernux", base);                                                       // theme red (default)
        Add("graphite", MakeAccentVariant(base, ImVec4(0.30f, 0.56f, 0.86f, 1.0f))); // steel blue
        Add("amber", MakeAccentVariant(base, ImVec4(0.92f, 0.62f, 0.22f, 1.0f)));    // instrument amber
        active = "infernux";
    }

    void Add(const std::string &name, const ThemeData &data)
    {
        if (!themes.count(name))
            order.push_back(name);
        themes[name] = data;
    }

    ThemeData &Active()
    {
        return themes[active];
    }
};

Registry &Reg()
{
    // Engine-lifetime singleton (intentionally leaked, like SceneManager).
    static Registry *r = new Registry();
    return *r;
}

inline ImVec4 Mix(const ImVec4 &a, const ImVec4 &b, float t)
{
    return ImVec4(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t, a.w + (b.w - a.w) * t);
}

inline ImVec4 Alpha(const ImVec4 &c, float a)
{
    return ImVec4(c.x, c.y, c.z, a);
}

} // namespace

const std::unordered_map<std::string, ImVec4> &EditorThemeRegistry::Colors()
{
    return Reg().Active().colors;
}
const std::unordered_map<std::string, ImVec2> &EditorThemeRegistry::Vec2s()
{
    return Reg().Active().vec2s;
}
const std::unordered_map<std::string, float> &EditorThemeRegistry::Floats()
{
    return Reg().Active().floats;
}

ImVec4 EditorThemeRegistry::Color(const std::string &name)
{
    const auto &m = Reg().Active().colors;
    const auto it = m.find(name);
    if (it == m.end())
        throw std::out_of_range("Editor theme is missing required color token: " + name);
    return it->second;
}

ImVec2 EditorThemeRegistry::Vec2(const std::string &name)
{
    const auto &m = Reg().Active().vec2s;
    const auto it = m.find(name);
    if (it == m.end())
        throw std::out_of_range("Editor theme is missing required vec2 token: " + name);
    return it->second;
}

float EditorThemeRegistry::Float(const std::string &name)
{
    const auto &m = Reg().Active().floats;
    const auto it = m.find(name);
    if (it == m.end())
        throw std::out_of_range("Editor theme is missing required float token: " + name);
    return it->second;
}

std::vector<std::string> EditorThemeRegistry::ThemeNames()
{
    return Reg().order;
}
const std::string &EditorThemeRegistry::ActiveTheme()
{
    return Reg().active;
}

bool EditorThemeRegistry::SetActiveTheme(const std::string &name)
{
    auto &r = Reg();
    if (!r.themes.count(name))
        return false;
    if (r.active != name) {
        r.active = name;
        ++r.generation;
    }
    return true;
}

unsigned long EditorThemeRegistry::Generation()
{
    return Reg().generation;
}

void EditorThemeRegistry::ApplyImGuiColors()
{
    ImGuiStyle &style = ImGui::GetStyle();
    ImVec4 *c = style.Colors;

    const ImVec4 accent = Color("ROLE_ACCENT");
    const ImVec4 bg = Color("ROLE_BG_BASE");
    const ImVec4 surf = Color("ROLE_BG_SURFACE");
    const ImVec4 raised = Color("ROLE_BG_RAISED");
    const ImVec4 hover = Color("ROLE_BG_HOVER");
    const ImVec4 text = Color("ROLE_TEXT");
    const ImVec4 dim = Color("ROLE_TEXT_DIM");
    const ImVec4 border = Color("ROLE_BORDER");

    const ImVec4 transparent(0.0f, 0.0f, 0.0f, 0.0f);

    // Text
    c[ImGuiCol_Text] = text;
    c[ImGuiCol_TextDisabled] = Mix(text, bg, 0.62f);
    c[ImGuiCol_TextSelectedBg] = Alpha(accent, 0.35f);

    // Backgrounds
    c[ImGuiCol_WindowBg] = bg;
    c[ImGuiCol_ChildBg] = surf;
    c[ImGuiCol_PopupBg] = Alpha(raised, 0.98f);
    c[ImGuiCol_FrameBg] = surf;
    c[ImGuiCol_FrameBgHovered] = Mix(hover, accent, 0.12f);
    c[ImGuiCol_FrameBgActive] = Mix(surf, accent, 0.22f);

    // Title / menu
    c[ImGuiCol_TitleBg] = bg;
    c[ImGuiCol_TitleBgActive] = bg;
    c[ImGuiCol_TitleBgCollapsed] = Alpha(bg, 0.75f);
    c[ImGuiCol_MenuBarBg] = bg;

    // Scrollbar
    c[ImGuiCol_ScrollbarBg] = transparent;
    c[ImGuiCol_ScrollbarGrab] = Mix(surf, text, 0.14f);
    c[ImGuiCol_ScrollbarGrabHovered] = Mix(surf, text, 0.26f);
    c[ImGuiCol_ScrollbarGrabActive] = Alpha(accent, 0.70f);

    // Accent widgets
    c[ImGuiCol_CheckMark] = accent;
    c[ImGuiCol_SliderGrab] = Alpha(accent, 0.88f);
    c[ImGuiCol_SliderGrabActive] = accent;
    c[ImGuiCol_NavHighlight] = transparent;

    // Buttons — surface with an accent-tinted hover/active for clear feedback.
    c[ImGuiCol_Button] = surf;
    c[ImGuiCol_ButtonHovered] = Mix(surf, accent, 0.26f);
    c[ImGuiCol_ButtonActive] = Mix(surf, accent, 0.40f);

    // Headers / selectables — the primary "pick an item" feedback across the
    // editor (Hierarchy tree, Inspector headers, lists), so keep it clearly read.
    c[ImGuiCol_Header] = hover;
    c[ImGuiCol_HeaderHovered] = Mix(hover, accent, 0.28f);
    c[ImGuiCol_HeaderActive] = Mix(hover, accent, 0.42f);

    // Borders / separators
    c[ImGuiCol_Border] = border;
    c[ImGuiCol_BorderShadow] = transparent;
    c[ImGuiCol_Separator] = border;
    c[ImGuiCol_SeparatorHovered] = Alpha(accent, 0.60f);
    c[ImGuiCol_SeparatorActive] = Alpha(accent, 0.80f);

    // Resize grip
    c[ImGuiCol_ResizeGrip] = transparent;
    c[ImGuiCol_ResizeGripHovered] = Alpha(accent, 0.30f);
    c[ImGuiCol_ResizeGripActive] = Alpha(accent, 0.50f);

    // Tabs
    c[ImGuiCol_Tab] = bg;
    c[ImGuiCol_TabHovered] = hover;
    c[ImGuiCol_TabSelected] = surf;
    c[ImGuiCol_TabSelectedOverline] = accent;
    c[ImGuiCol_TabDimmed] = bg;
    c[ImGuiCol_TabDimmedSelected] = surf;
    c[ImGuiCol_TabDimmedSelectedOverline] = Alpha(accent, 0.60f);

    // Docking
    c[ImGuiCol_DockingPreview] = Alpha(accent, 0.25f);
    c[ImGuiCol_DockingEmptyBg] = Mix(bg, ImVec4(0, 0, 0, 1), 0.4f);

    // Plots
    c[ImGuiCol_PlotLines] = Mix(surf, text, 0.45f);
    c[ImGuiCol_PlotHistogram] = text;

    // Drag-drop
    c[ImGuiCol_DragDropTarget] = ImVec4(1.0f, 1.0f, 1.0f, 1.0f);
    c[ImGuiCol_DragDropTargetBg] = transparent;

    // Modal
    c[ImGuiCol_ModalWindowDimBg] = ImVec4(0.0f, 0.0f, 0.0f, 0.56f);

    // Tables
    c[ImGuiCol_TableHeaderBg] = surf;
    c[ImGuiCol_TableBorderStrong] = border;
    c[ImGuiCol_TableBorderLight] = Mix(bg, border, 0.5f);
    c[ImGuiCol_TableRowBg] = transparent;
    c[ImGuiCol_TableRowBgAlt] = ImVec4(1.0f, 1.0f, 1.0f, 0.02f);
}

} // namespace infernux
