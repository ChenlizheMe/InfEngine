#include "MenuBarPanel.h"
#include <function/renderer/gui/InxGUISemantics.h>

#include <algorithm>
#include <cctype>

namespace infernux
{
namespace
{

bool BeginSemanticMenu(InxGUIContext *ctx, const std::string &label, const std::string &semanticId, bool enabled = true)
{
    const bool open = ImGui::BeginMenu(label.c_str(), enabled);
    if (ctx && InxGUISemantics::IsCaptureEnabled())
        ctx->RecordSemanticItem("menu", label, enabled, semanticId, open);
    return open;
}

bool SemanticMenuItem(InxGUIContext *ctx, const std::string &label, const std::string &shortcut, bool selected,
                      bool enabled, const std::string &semanticId)
{
    const bool clicked =
        ImGui::MenuItem(label.c_str(), shortcut.empty() ? nullptr : shortcut.c_str(), selected, enabled);
    if (ctx && InxGUISemantics::IsCaptureEnabled())
        ctx->RecordSemanticItem("menu_item", label, enabled, semanticId);
    return clicked;
}

} // namespace

// ════════════════════════════════════════════════════════════════════
// Construction
// ════════════════════════════════════════════════════════════════════

MenuBarPanel::MenuBarPanel() = default;

void MenuBarPanel::InvalidateWindowTypeCache()
{
    m_windowTypesDirty = true;
}

// ════════════════════════════════════════════════════════════════════
// Translation helper
// ════════════════════════════════════════════════════════════════════

std::string MenuBarPanel::T(const std::string &key) const
{
    if (translate)
        return translate(key);
    auto dot = key.rfind('.');
    return (dot != std::string::npos) ? key.substr(dot + 1) : key;
}

// ════════════════════════════════════════════════════════════════════
// Render
// ════════════════════════════════════════════════════════════════════

void MenuBarPanel::OnRender(InxGUIContext *ctx)
{
    // Handle global shortcuts before menu logic
    HandleShortcuts(ctx);

    // Check for window close request (SDL_EVENT_QUIT intercepted by C++)
    if (isCloseRequested && onRequestClose) {
        if (isCloseRequested())
            onRequestClose();
    }

    // Style overrides
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, EditorTheme::TOOLBAR_FRAME_PAD);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, EditorTheme::TOOLBAR_ITEM_SPC);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, EditorTheme::TOOLBAR_WIN_PAD);
    ImGui::PushStyleColor(ImGuiCol_MenuBarBg, EditorTheme::MENU_BAR_BG);
    ImGui::PushStyleColor(ImGuiCol_PopupBg, EditorTheme::POPUP_BG);
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, EditorTheme::HEADER_HOVERED);
    ImGui::PushStyleColor(ImGuiCol_HeaderActive, EditorTheme::HEADER_ACTIVE);

    if (ImGui::BeginMainMenuBar()) {
        if (InxGUISemantics::IsCaptureEnabled())
            ctx->RecordSemanticWindow("menu_bar", "Main Menu", "menu_bar");
        RenderProjectMenu(ctx);
        RenderSceneMenu(ctx);
        RenderDynamicMenus(ctx);
        RenderWindowMenu(ctx);
        ImGui::EndMainMenuBar();
    }

    ImGui::PopStyleColor(4);
    ImGui::PopStyleVar(3);

    // Note: floating sub-panels (BuildSettings, Preferences, PhysicsLayerMatrix)
    // and save-confirmation popup are rendered from Python side.
}

// ════════════════════════════════════════════════════════════════════
// Keyboard shortcuts
// ════════════════════════════════════════════════════════════════════

void MenuBarPanel::HandleShortcuts(InxGUIContext *ctx)
{
    const int frame = ImGui::GetFrameCount();
    if (m_lastShortcutFrame == frame)
        return;
    m_lastShortcutFrame = frame;

    bool ctrl = ctx->IsKeyDown(KEY_LEFT_CTRL) || ctx->IsKeyDown(KEY_RIGHT_CTRL);
    if (!ctrl)
        return;
    const bool shift = ctx->IsKeyDown(KEY_LEFT_SHIFT) || ctx->IsKeyDown(KEY_RIGHT_SHIFT);
    const auto pressedOnce = [](int key) { return ImGui::IsKeyPressed(static_cast<ImGuiKey>(key), false); };

    if (pressedOnce(KEY_S)) {
        if (shift && onSaveFocusedAs)
            onSaveFocusedAs();
        else if (!shift && onSaveFocused)
            onSaveFocused();
    }

    if (!shift && pressedOnce(KEY_N) && onNewScene)
        onNewScene();

    if (pressedOnce(KEY_Z)) {
        if (canUndo && canUndo() && onUndo)
            onUndo();
    }

    if (pressedOnce(KEY_Y)) {
        if (canRedo && canRedo() && onRedo)
            onRedo();
    }
}

// ════════════════════════════════════════════════════════════════════
// Project menu
// ════════════════════════════════════════════════════════════════════

void MenuBarPanel::RenderProjectMenu(InxGUIContext *ctx)
{
    if (!BeginSemanticMenu(ctx, T("menu.project"), "menu.project"))
        return;

    if (SemanticMenuItem(ctx, T("menu.new_scene"), "Ctrl+N", false, true, "menu.project.new_scene")) {
        if (onNewScene)
            onNewScene();
    }
    if (SemanticMenuItem(ctx, T("menu.save_scene"), "Ctrl+S", false, true, "menu.project.save_scene")) {
        if (onSave)
            onSave();
    }
    if (SemanticMenuItem(ctx, T("menu.save_scene_as"), "Ctrl+Shift+S", false, true, "menu.project.save_scene_as")) {
        if (onSaveAs)
            onSaveAs();
    }

    ImGui::Separator();

    // Build Settings toggle
    bool bsOpen = isBuildSettingsOpen ? isBuildSettingsOpen() : false;
    if (SemanticMenuItem(ctx, T("menu.build_settings"), "", bsOpen, true, "menu.project.build_settings")) {
        if (toggleBuildSettings)
            toggleBuildSettings();
    }

    // Physics Layer Matrix toggle
    bool plOpen = isPhysicsLayerMatrixOpen ? isPhysicsLayerMatrixOpen() : false;
    if (SemanticMenuItem(ctx, T("menu.physics_layer_matrix"), "", plOpen, true, "menu.project.physics_layer_matrix")) {
        if (togglePhysicsLayerMatrix)
            togglePhysicsLayerMatrix();
    }

    ImGui::Separator();

    // Preferences toggle
    bool prefOpen = isPreferencesOpen ? isPreferencesOpen() : false;
    if (SemanticMenuItem(ctx, T("menu.preferences"), "", prefOpen, true, "menu.project.preferences")) {
        if (togglePreferences)
            togglePreferences();
    }

    ImGui::EndMenu();
}

// ════════════════════════════════════════════════════════════════════
// Scene menu — per-scene settings (environment / lighting)
// ════════════════════════════════════════════════════════════════════

void MenuBarPanel::RenderSceneMenu(InxGUIContext *ctx)
{
    if (!BeginSemanticMenu(ctx, T("menu.scene"), "menu.scene"))
        return;

    // Environment (skybox + ambient) settings toggle
    bool envOpen = isEnvironmentSettingsOpen ? isEnvironmentSettingsOpen() : false;
    if (SemanticMenuItem(ctx, T("menu.environment_settings"), "", envOpen, true, "menu.scene.environment_settings")) {
        if (toggleEnvironmentSettings)
            toggleEnvironmentSettings();
    }

    ImGui::EndMenu();
}

// ════════════════════════════════════════════════════════════════════
// Window menu
// ════════════════════════════════════════════════════════════════════

void MenuBarPanel::RenderWindowMenu(InxGUIContext *ctx)
{
    if (!BeginSemanticMenu(ctx, T("menu.window"), "menu.window"))
        return;

    RefreshWindowTypeCache();
    if (!m_cachedWindowTypes.empty() && getOpenWindows) {
        auto openWins = getOpenWindows();

        bool hasItems = false;
        for (const auto &info : m_cachedWindowTypes) {
            if (info.menuPath != "Window")
                continue;
            hasItems = true;

            bool isOpen = false;
            auto it = openWins.find(info.typeId);
            if (it != openWins.end())
                isOpen = it->second;

            if (SemanticMenuItem(ctx, info.displayName, "", isOpen, true, "window." + info.typeId)) {
                // A checked entry represents an already-open tool window.
                // Opening it again asks WindowManager to bring its dock tab
                // to the foreground rather than unexpectedly closing it.
                if (openWindow)
                    openWindow(info.typeId);
            }
        }

        if (!hasItems) {
            SemanticMenuItem(ctx, T("menu.no_windows"), "", false, false, "menu.window.none");
        }
    } else {
        SemanticMenuItem(ctx, T("menu.no_wm"), "", false, false, "menu.window.unavailable");
    }

    ImGui::Separator();

    if (SemanticMenuItem(ctx, T("menu.reset_layout"), "", false, true, "menu.window.reset_layout")) {
        if (resetLayout)
            resetLayout();
    }

    ImGui::EndMenu();
}

// ════════════════════════════════════════════════════════════════════
// Dynamic menus (everything between Project and Window)
// ════════════════════════════════════════════════════════════════════

void MenuBarPanel::RenderDynamicMenus(InxGUIContext *ctx)
{
    RefreshWindowTypeCache();
    if (m_cachedWindowTypes.empty())
        return;

    // Render each top-level menu.
    for (const auto &top : m_cachedTopMenus) {
        // Build i18n key: "Animation" -> "menu.animation"
        std::string key = "menu." + top;
        for (auto &c : key)
            if (c == ' ')
                c = '_';
            else
                c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));

        std::string label = T(key);
        // Fallback: if T() returned the key tail, use original name
        if (label == key.substr(key.rfind('.') + 1))
            label = top;

        RenderMenuGroup(ctx, top, label, m_cachedWindowTypes);
    }
}

void MenuBarPanel::RefreshWindowTypeCache()
{
    if (!m_windowTypesDirty)
        return;
    if (!getRegisteredTypes)
        return;

    m_windowTypesDirty = false;
    m_cachedWindowTypes = getRegisteredTypes();
    m_cachedTopMenus.clear();
    for (const auto &info : m_cachedWindowTypes) {
        if (info.menuPath == "Window")
            continue;
        std::string top = info.menuPath;
        const auto slash = top.find('/');
        if (slash != std::string::npos)
            top.resize(slash);
        if (std::find(m_cachedTopMenus.begin(), m_cachedTopMenus.end(), top) == m_cachedTopMenus.end())
            m_cachedTopMenus.push_back(std::move(top));
    }
}

void MenuBarPanel::RenderMenuGroup(InxGUIContext *ctx, const std::string &topMenu, const std::string &translatedLabel,
                                   const std::vector<WindowTypeInfo> &types)
{
    if (!BeginSemanticMenu(ctx, translatedLabel, "menu." + topMenu))
        return;

    // Collect entries belonging to this top-level menu.
    struct Entry
    {
        std::string subMenu; // "" = direct child, else sub-menu label
        std::string typeId;
        std::string displayName;
        bool singleton;
    };

    std::vector<Entry> entries;
    std::vector<std::string> subMenuOrder;

    const size_t topLen = topMenu.size();

    for (const auto &info : types) {
        // Must start with topMenu
        if (info.menuPath.rfind(topMenu, 0) != 0)
            continue;
        // Must be exactly topMenu or topMenu/...
        if (info.menuPath.size() > topLen && info.menuPath[topLen] != '/')
            continue;

        Entry e;
        e.typeId = info.typeId;
        e.displayName = info.displayName;
        e.singleton = info.singleton;

        if (info.menuPath.size() > topLen + 1)
            e.subMenu = info.menuPath.substr(topLen + 1);

        entries.push_back(e);

        if (!e.subMenu.empty()) {
            bool found = false;
            for (const auto &s : subMenuOrder)
                if (s == e.subMenu) {
                    found = true;
                    break;
                }
            if (!found)
                subMenuOrder.push_back(e.subMenu);
        }
    }

    const auto openWins = getOpenWindows ? getOpenWindows() : std::map<std::string, bool>{};

    // Lambda: render a single menu-item toggle
    auto renderItem = [&](const Entry &e) {
        bool isOpen = false;
        auto it = openWins.find(e.typeId);
        if (it != openWins.end())
            isOpen = it->second;
        if (SemanticMenuItem(ctx, e.displayName, "", isOpen, true, "window." + e.typeId)) {
            // WindowManager treats an already-visible window as a focus
            // request, which makes this menu work like a normal tool picker.
            if (openWindow)
                openWindow(e.typeId);
        }
    };

    // Top-level items (menuPath == topMenu exactly)
    for (const auto &e : entries) {
        if (e.subMenu.empty())
            renderItem(e);
    }

    // Sub-menus
    for (const auto &sm : subMenuOrder) {
        // Build i18n key: e.g. "Animation" + "2D Animation" -> "menu.animation_2d_animation"
        std::string smKey = "menu." + topMenu + "_" + sm;
        for (auto &c : smKey)
            if (c == ' ')
                c = '_';
            else
                c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));

        std::string smLabel = T(smKey);
        if (smLabel == smKey.substr(smKey.rfind('.') + 1))
            smLabel = sm;

        if (BeginSemanticMenu(ctx, smLabel, "menu." + topMenu + "." + sm)) {
            for (const auto &e : entries) {
                if (e.subMenu == sm)
                    renderItem(e);
            }
            ImGui::EndMenu();
        }
    }

    ImGui::EndMenu();
}

} // namespace infernux
