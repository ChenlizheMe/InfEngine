#pragma once

#include "EditorTheme.h"
#include <function/renderer/gui/InxGUIContext.h>
#include <function/renderer/gui/InxGUIRenderable.h>

#include <imgui.h>

#include <cstdint>
#include <functional>
#include <string>

namespace infernux
{

class ConsolePanel;

/// C++ native status bar — fixed bar at the bottom of the editor window.
/// Not dockable; inherits InxGUIRenderable directly.
///
/// Reads log counts from ConsolePanel.
/// Engine-status text/progress is fed from Python via SetEngineStatus().
class StatusBarPanel : public InxGUIRenderable
{
  public:
    StatusBarPanel();
    ~StatusBarPanel() override = default;

    // ── Public API ───────────────────────────────────────────────────

    /// Wire to C++ ConsolePanel for count queries and "select latest" action.
    void SetConsolePanel(ConsolePanel *panel);

    /// Update engine-status indicator (called from Python every frame).
    void SetEngineStatus(const std::string &text, float progress, const std::string &kind = "activity");

    /// Unified command entry used by status-bar actions.
    std::function<bool(const std::string &, const std::string &, const std::string &)> executeCommand;

    // ── InxGUIRenderable ─────────────────────────────────────────────
    void OnRender(InxGUIContext *ctx) override;

  private:
    void RenderContent(InxGUIContext *ctx, float dispW);
    void RenderEngineStatus(float x, float y, float width, float height, const std::string &text, float progress,
                            const std::string &kind);

    const ImVec4 &LevelColorForString(const std::string &level) const;

    ConsolePanel *m_console = nullptr;

    uint64_t m_consoleRevision = 0;
    uint64_t m_latestUid = 0;
    std::string m_latestMessage;
    std::string m_latestLevel{"info"};
    int m_infoCount = 0;
    int m_warningCount = 0;
    int m_errorCount = 0;

    // Engine status
    std::string m_statusText;
    float m_statusProgress = -1.0f;
    std::string m_statusKind{"activity"};
};

} // namespace infernux
