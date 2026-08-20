#pragma once

#include <function/renderer/gui/InxGUIRenderable.h>

#include <functional>
#include <string>

namespace infernux
{

/// Native physical-input adapter for the Editor Interaction Core.
///
/// This object owns no command semantics. It samples ImGui key edges once per
/// frame and forwards a normalized chord to the Python ShortcutRouter only
/// when an edge occurs. Panels and the menu bar must not duplicate this pump.
class EditorShortcutInput final : public InxGUIRenderable
{
  public:
    std::function<bool(const std::string &, bool, bool)> routeShortcut;

    void OnRender(InxGUIContext *ctx) override;

  private:
    int m_lastFrame = -1;
    bool m_popupActivePreviousFrame = false;
    bool m_modalActivePreviousFrame = false;
};

} // namespace infernux
