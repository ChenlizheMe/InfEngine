#include "MenuBarPanel.h"
#include <function/renderer/gui/InxGUISemantics.h>

#include <algorithm>
#include <cctype>
#include <tuple>

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
        RenderEditMenu(ctx);
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

    const auto isDown = [ctx](ImGuiKey key) { return ctx->IsKeyDown(static_cast<int>(key)); };
    const bool ctrl = isDown(ImGuiKey_LeftCtrl) || isDown(ImGuiKey_RightCtrl);
    const bool shift = isDown(ImGuiKey_LeftShift) || isDown(ImGuiKey_RightShift);
    const bool alt = isDown(ImGuiKey_LeftAlt) || isDown(ImGuiKey_RightAlt);
    const bool super = isDown(ImGuiKey_LeftSuper) || isDown(ImGuiKey_RightSuper);
    const auto pressedOnce = [](ImGuiKey key) { return ImGui::IsKeyPressed(key, false); };
    const bool textInputActive = ImGui::GetIO().WantTextInput;
    const bool popupActive = ImGui::IsPopupOpen("", ImGuiPopupFlags_AnyPopupId);
    const auto dispatch = [&](const char *chord) {
        if (routeShortcut)
            routeShortcut(chord, textInputActive, popupActive);
    };

    if (ctrl && !alt && !super) {
        if (pressedOnce(ImGuiKey_S))
            dispatch(shift ? "Ctrl+Shift+S" : "Ctrl+S");

        if (pressedOnce(ImGuiKey_N))
            dispatch(shift ? "Ctrl+Shift+N" : "Ctrl+N");

        if (pressedOnce(ImGuiKey_Z))
            dispatch(shift ? "Ctrl+Shift+Z" : "Ctrl+Z");

        if (!shift && pressedOnce(ImGuiKey_Y))
            dispatch("Ctrl+Y");

        if (!shift && pressedOnce(ImGuiKey_C))
            dispatch("Ctrl+C");
        if (!shift && pressedOnce(ImGuiKey_X))
            dispatch("Ctrl+X");
        if (!shift && pressedOnce(ImGuiKey_V))
            dispatch("Ctrl+V");
        return;
    }

    if (!shift && !alt && !super) {
        if (pressedOnce(ImGuiKey_F2))
            dispatch("F2");
        if (pressedOnce(ImGuiKey_Delete))
            dispatch("Delete");
    }
}

// ════════════════════════════════════════════════════════════════════
// Project menu
// ════════════════════════════════════════════════════════════════════

void MenuBarPanel::RenderProjectMenu(InxGUIContext *ctx)
{
    if (!BeginSemanticMenu(ctx, T("menu.project"), "menu.project"))
        return;

    const bool canNew = CanExecuteCommand("file.new_scene");
    if (SemanticMenuItem(ctx, T("menu.new_scene"), "Ctrl+N", false, canNew, "menu.project.new_scene"))
        ExecuteCommand("file.new_scene", "menu");
    const bool canSave = CanExecuteCommand("file.save");
    if (SemanticMenuItem(ctx, T("menu.save_scene"), "Ctrl+S", false, canSave, "menu.project.save_scene"))
        ExecuteCommand("file.save", "menu");
    const bool canSaveAs = CanExecuteCommand("file.save_as");
    if (SemanticMenuItem(ctx, T("menu.save_scene_as"), "Ctrl+Shift+S", false, canSaveAs, "menu.project.save_scene_as"))
        ExecuteCommand("file.save_as", "menu");

    ImGui::Separator();

    // Build Settings toggle
    const bool canBuildSettings = CanExecuteCommand("window.toggle.build_settings");
    const bool buildSettingsOpen = IsCommandChecked("window.toggle.build_settings");
    if (SemanticMenuItem(ctx, T("menu.build_settings"), "", buildSettingsOpen, canBuildSettings,
                         "menu.project.build_settings"))
        ExecuteCommand("window.toggle.build_settings", "menu");

    // Physics Layer Matrix toggle
    const bool canPhysicsLayers = CanExecuteCommand("window.toggle.physics_layers");
    const bool physicsLayersOpen = IsCommandChecked("window.toggle.physics_layers");
    if (SemanticMenuItem(ctx, T("menu.physics_layer_matrix"), "", physicsLayersOpen, canPhysicsLayers,
                         "menu.project.physics_layer_matrix"))
        ExecuteCommand("window.toggle.physics_layers", "menu");

    ImGui::Separator();

    // Preferences toggle
    const bool canPreferences = CanExecuteCommand("window.toggle.preferences");
    const bool preferencesOpen = IsCommandChecked("window.toggle.preferences");
    if (SemanticMenuItem(ctx, T("menu.preferences"), "", preferencesOpen, canPreferences, "menu.project.preferences"))
        ExecuteCommand("window.toggle.preferences", "menu");

    ImGui::EndMenu();
}

void MenuBarPanel::RenderEditMenu(InxGUIContext *ctx)
{
    if (!BeginSemanticMenu(ctx, T("menu.edit"), "menu.edit"))
        return;

    const bool canUndoCommand = CanExecuteCommand("edit.undo");
    if (SemanticMenuItem(ctx, T("menu.undo"), "Ctrl+Z", false, canUndoCommand, "menu.edit.undo"))
        ExecuteCommand("edit.undo", "menu");
    const bool canRedoCommand = CanExecuteCommand("edit.redo");
    if (SemanticMenuItem(ctx, T("menu.redo"), "Ctrl+Shift+Z", false, canRedoCommand, "menu.edit.redo"))
        ExecuteCommand("edit.redo", "menu");

    ImGui::Separator();

    for (const auto &[commandId, labelKey, shortcut, semanticId] :
         {std::tuple{"edit.copy", "menu.copy", "Ctrl+C", "menu.edit.copy"},
          std::tuple{"edit.cut", "menu.cut", "Ctrl+X", "menu.edit.cut"},
          std::tuple{"edit.paste", "menu.paste", "Ctrl+V", "menu.edit.paste"},
          std::tuple{"edit.rename", "menu.rename", "F2", "menu.edit.rename"},
          std::tuple{"edit.delete", "menu.delete", "Delete", "menu.edit.delete"}}) {
        const bool enabled = CanExecuteCommand(commandId);
        if (SemanticMenuItem(ctx, T(labelKey), shortcut, false, enabled, semanticId))
            ExecuteCommand(commandId, "menu");
    }

    ImGui::EndMenu();
}

bool MenuBarPanel::ExecuteCommand(const std::string &commandId, const std::string &source,
                                  const std::string &argument) const
{
    return executeCommand && executeCommand(commandId, source, argument);
}

bool MenuBarPanel::CanExecuteCommand(const std::string &commandId, const std::string &argument) const
{
    return canExecuteCommand && canExecuteCommand(commandId, argument);
}

bool MenuBarPanel::IsCommandChecked(const std::string &commandId, const std::string &argument) const
{
    return isCommandChecked && isCommandChecked(commandId, argument);
}

// ════════════════════════════════════════════════════════════════════
// Scene menu — per-scene settings (environment / lighting)
// ════════════════════════════════════════════════════════════════════

void MenuBarPanel::RenderSceneMenu(InxGUIContext *ctx)
{
    if (!BeginSemanticMenu(ctx, T("menu.scene"), "menu.scene"))
        return;

    const bool canEnvironment = CanExecuteCommand("window.toggle.environment");
    const bool environmentOpen = IsCommandChecked("window.toggle.environment");
    if (SemanticMenuItem(ctx, T("menu.environment_settings"), "", environmentOpen, canEnvironment,
                         "menu.scene.environment_settings"))
        ExecuteCommand("window.toggle.environment", "menu");

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
    if (!m_cachedWindowTypes.empty()) {
        bool hasItems = false;
        for (const auto &info : m_cachedWindowTypes) {
            if (info.menuPath != "Window")
                continue;
            hasItems = true;

            const bool canOpen = CanExecuteCommand("window.open", info.typeId);
            const bool isOpen = IsCommandChecked("window.open", info.typeId);
            if (SemanticMenuItem(ctx, info.displayName, "", isOpen, canOpen, "window." + info.typeId))
                ExecuteCommand("window.open", "menu", info.typeId);
        }

        if (!hasItems) {
            SemanticMenuItem(ctx, T("menu.no_windows"), "", false, false, "menu.window.none");
        }
    } else {
        SemanticMenuItem(ctx, T("menu.no_wm"), "", false, false, "menu.window.unavailable");
    }

    ImGui::Separator();

    const bool canResetLayout = CanExecuteCommand("window.reset_layout");
    if (SemanticMenuItem(ctx, T("menu.reset_layout"), "", false, canResetLayout, "menu.window.reset_layout"))
        ExecuteCommand("window.reset_layout", "menu");

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

    // Lambda: render a single command-backed menu item.
    auto renderItem = [&](const Entry &e) {
        const bool canOpen = CanExecuteCommand("window.open", e.typeId);
        const bool isOpen = IsCommandChecked("window.open", e.typeId);
        if (SemanticMenuItem(ctx, e.displayName, "", isOpen, canOpen, "window." + e.typeId))
            ExecuteCommand("window.open", "menu", e.typeId);
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
