#pragma once

#include "EditorTheme.h"
#include <function/renderer/gui/InxGUIContext.h>
#include <function/renderer/gui/InxGUIRenderable.h>

#include <imgui.h>

#include <functional>
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

    // Unified editor command and shortcut entry points.
    std::function<bool(const std::string &, const std::string &, const std::string &)> executeCommand;
    std::function<bool(const std::string &, const std::string &)> canExecuteCommand;
    std::function<bool(const std::string &, const std::string &)> isCommandChecked;
    std::function<bool(const std::string &, bool, bool)> routeShortcut;
    std::function<void()> onRequestClose;

    // Window management
    std::function<std::vector<WindowTypeInfo>()> getRegisteredTypes;

    // Close request check (C++ engine)
    std::function<bool()> isCloseRequested;

    // i18n
    std::function<std::string(const std::string &)> translate;

    // ── InxGUIRenderable ─────────────────────────────────────────────
    void OnRender(InxGUIContext *ctx) override;

  private:
    void HandleShortcuts(InxGUIContext *ctx);
    void RenderProjectMenu(InxGUIContext *ctx);
    void RenderEditMenu(InxGUIContext *ctx);
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
    bool ExecuteCommand(const std::string &commandId, const std::string &source,
                        const std::string &argument = "") const;
    bool CanExecuteCommand(const std::string &commandId, const std::string &argument = "") const;
    bool IsCommandChecked(const std::string &commandId, const std::string &argument = "") const;

    int m_lastShortcutFrame = -1;
    std::vector<WindowTypeInfo> m_cachedWindowTypes;
    std::vector<std::string> m_cachedTopMenus;
    bool m_windowTypesDirty = true;
};

} // namespace infernux
