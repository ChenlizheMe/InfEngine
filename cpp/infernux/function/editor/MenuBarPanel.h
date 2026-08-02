#pragma once

#include "EditorTheme.h"
#include <function/renderer/gui/InxGUIContext.h>
#include <function/renderer/gui/InxGUIRenderable.h>

#include <imgui.h>

#include <functional>
#include <map>
#include <string>
#include <vector>

namespace infernux
{

/// Info about a registered window type (mirrors Python WindowManager data).
struct WindowTypeInfo
{
    std::string typeId;
    std::string displayName;
    std::string menuPath = "Window";
    bool singleton = true;
};

/// C++ native menu bar — Project / dynamic menus / Window + keyboard shortcuts.
/// Not dockable (inherits InxGUIRenderable directly).
///
/// Menus between Project and Window are built dynamically from panel
/// `menuPath` values.  For example, a Python panel decorated with
///   @editor_panel(menu_path="Animation/2D Animation")
/// will automatically appear under an "Animation" top-level menu
/// in a "2D Animation" sub-menu — no C++ changes needed.
///
/// Floating sub-panels (BuildSettings, Preferences, PhysicsLayerMatrix) and
/// the save-confirmation popup are still rendered from Python; this panel
/// delegates to Python callbacks for operations that touch Python-only managers.
class MenuBarPanel : public InxGUIRenderable
{
  public:
    MenuBarPanel();
    ~MenuBarPanel() override = default;
    void InvalidateWindowTypeCache();

    // ── Callbacks set from Python ────────────────────────────────────

    // Scene file operations
    std::function<void()> onSave;
    std::function<void()> onSaveAs;
    std::function<void()> onSaveFocused;
    std::function<void()> onSaveFocusedAs;
    std::function<void()> onNewScene;
    std::function<void()> onRequestClose;

    // Undo
    std::function<void()> onUndo;
    std::function<void()> onRedo;
    std::function<bool()> canUndo;
    std::function<bool()> canRedo;

    // Window management
    std::function<std::vector<WindowTypeInfo>()> getRegisteredTypes;
    std::function<std::map<std::string, bool>()> getOpenWindows;
    std::function<void(const std::string &)> openWindow;
    std::function<void(const std::string &)> closeWindow;
    std::function<void()> resetLayout;

    // Close request check (C++ engine)
    std::function<bool()> isCloseRequested;

    // Toggle floating sub-panels — rendered from Python
    std::function<void()> toggleBuildSettings;
    std::function<void()> togglePreferences;
    std::function<void()> togglePhysicsLayerMatrix;
    std::function<void()> toggleEnvironmentSettings;
    std::function<bool()> isBuildSettingsOpen;
    std::function<bool()> isPreferencesOpen;
    std::function<bool()> isPhysicsLayerMatrixOpen;
    std::function<bool()> isEnvironmentSettingsOpen;

    // i18n
    std::function<std::string(const std::string &)> translate;

    // ── InxGUIRenderable ─────────────────────────────────────────────
    void OnRender(InxGUIContext *ctx) override;

  private:
    void HandleShortcuts(InxGUIContext *ctx);
    void RenderProjectMenu(InxGUIContext *ctx);
    void RenderSceneMenu(InxGUIContext *ctx);
    void RenderDynamicMenus(InxGUIContext *ctx);
    void RefreshWindowTypeCache();
    void RenderWindowMenu(InxGUIContext *ctx);

    /// Render a single top-level menu for panels whose menuPath starts with
    /// @p topMenu.  Panels with exact match become top-level items; those
    /// with a '/' suffix become sub-menus (e.g. "Animation/2D Animation").
    void RenderMenuGroup(InxGUIContext *ctx, const std::string &topMenu, const std::string &translatedLabel,
                         const std::vector<WindowTypeInfo> &types);

    std::string T(const std::string &key) const;

    // ImGuiKey constants
    static constexpr int KEY_S = 564;
    static constexpr int KEY_N = 559;
    static constexpr int KEY_Z = 571;
    static constexpr int KEY_Y = 570;
    static constexpr int KEY_LEFT_CTRL = 527;
    static constexpr int KEY_RIGHT_CTRL = 531;
    static constexpr int KEY_LEFT_SHIFT = ImGuiKey_LeftShift;
    static constexpr int KEY_RIGHT_SHIFT = ImGuiKey_RightShift;

    int m_lastShortcutFrame = -1;
    std::vector<WindowTypeInfo> m_cachedWindowTypes;
    std::vector<std::string> m_cachedTopMenus;
    bool m_windowTypesDirty = true;
};

} // namespace infernux
