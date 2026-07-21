#include "StatusBarPanel.h"
#include "ConsolePanel.h"
#include <function/renderer/gui/InxGUISemantics.h>

#include <algorithm>
#include <cmath>
#include <cstring>

namespace infernux
{

// ════════════════════════════════════════════════════════════════════
// Construction
// ════════════════════════════════════════════════════════════════════

StatusBarPanel::StatusBarPanel() = default;

// ════════════════════════════════════════════════════════════════════
// Public API
// ════════════════════════════════════════════════════════════════════

void StatusBarPanel::SetConsolePanel(ConsolePanel *panel)
{
    m_console = panel;
}

void StatusBarPanel::SetEngineStatus(const std::string &text, float progress, const std::string &kind)
{
    m_statusText = text;
    m_statusProgress = progress;
    m_statusKind = kind;
}

// ════════════════════════════════════════════════════════════════════
// Render
// ════════════════════════════════════════════════════════════════════

void StatusBarPanel::OnRender(InxGUIContext *ctx)
{
    float x0, y0, dispW, dispH;
    ctx->GetMainViewportBounds(&x0, &y0, &dispW, &dispH);
    if (dispW <= 0.0f || dispH <= 0.0f)
        return;

    float dpi = ctx->GetDpiScale();
    float height = EditorTheme::STATUS_BAR_BASE_HEIGHT * dpi;

    ctx->SetNextWindowPos(x0, y0 + dispH - height, ImGuiCond_Always, 0.0f, 0.0f);
    ctx->SetNextWindowSize(dispW, height, ImGuiCond_Always);

    // Style overrides (before Begin)
    ImGui::PushStyleColor(ImGuiCol_WindowBg, EditorTheme::STATUS_BAR_BG);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, EditorTheme::STATUS_BAR_WIN_PAD);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, EditorTheme::STATUS_BAR_ITEM_SPC);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, EditorTheme::STATUS_BAR_FRAME_PAD);

    constexpr ImGuiWindowFlags flags = ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
                                       ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse |
                                       ImGuiWindowFlags_NoDocking | ImGuiWindowFlags_NoSavedSettings;

    if (ImGui::Begin("##InxStatusBar", nullptr, flags)) {
        RenderContent(ctx, dispW);
    }
    ImGui::End();

    ImGui::PopStyleVar(4);
    ImGui::PopStyleColor(1);
}

// ════════════════════════════════════════════════════════════════════
// Content rendering
// ════════════════════════════════════════════════════════════════════

void StatusBarPanel::RenderContent(InxGUIContext *ctx, float dispW)
{
    const bool statusActive = !m_statusText.empty();
    const float dpi = ctx->GetDpiScale();
    const float barHeight = EditorTheme::STATUS_BAR_BASE_HEIGHT * dpi - 8.0f;
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const float availableWidth = (std::max)(0.0f, ImGui::GetContentRegionAvail().x);
    const float preferredStatusWidth = (std::min)((std::max)(210.0f, availableWidth * 0.36f), 430.0f);
    const float statusWidth =
        statusActive ? (std::min)(preferredStatusWidth, (std::max)(0.0f, availableWidth - 80.0f)) : 0.0f;
    const float consoleWidth = availableWidth - statusWidth;

    if (m_console) {
        const uint64_t revision = m_console->GetRevision();
        if (revision != m_consoleRevision) {
            m_console->GetStatusBarSnapshot(m_latestMessage, m_latestLevel, m_infoCount, m_warningCount, m_errorCount,
                                            m_latestUid);
            m_consoleRevision = m_console->GetRevision();
        }
    }

    ImGui::SetCursorScreenPos(origin);
    ImGui::InvisibleButton("##StatusBarConsole", ImVec2(consoleWidth, barHeight));
    const bool consoleHovered = ImGui::IsItemHovered();
    if (ImGui::IsItemClicked() && m_console)
        m_console->SelectEntry(m_latestUid);
    if (consoleHovered && !m_latestMessage.empty())
        ImGui::SetTooltip("%s", m_latestMessage.c_str());
    if (InxGUISemantics::IsCaptureEnabled())
        ctx->RecordSemanticItem("status_console", "Console", true, "status.console");

    ImDrawList *draw = ImGui::GetWindowDrawList();
    const float right = origin.x + availableWidth;
    const float consoleRight = origin.x + consoleWidth;
    const float textY = origin.y + (barHeight - ImGui::GetTextLineHeight()) * 0.5f;
    draw->AddLine(ImVec2(origin.x, origin.y - 4.0f), ImVec2(right, origin.y - 4.0f),
                  ImGui::ColorConvertFloat4ToU32(EditorTheme::STATUS_BAR_BORDER));
    if (consoleHovered)
        draw->AddRectFilled(origin, ImVec2(consoleRight, origin.y + barHeight),
                            ImGui::ColorConvertFloat4ToU32(EditorTheme::BTN_SB_HOVERED));

    char infoText[48];
    char warningText[48];
    char errorText[48];
    snprintf(infoText, sizeof(infoText), "Log %d", m_infoCount);
    snprintf(warningText, sizeof(warningText), "Warn %d", m_warningCount);
    snprintf(errorText, sizeof(errorText), "Error %d", m_errorCount);
    const float countGap = 13.0f;
    const float infoWidth = ImGui::CalcTextSize(infoText).x;
    const float warningWidth = ImGui::CalcTextSize(warningText).x;
    const float errorWidth = ImGui::CalcTextSize(errorText).x;
    const float countsWidth = infoWidth + warningWidth + errorWidth + countGap * 2.0f;
    const float countX = (std::max)(origin.x + 48.0f, consoleRight - countsWidth - 9.0f);

    float messageX = origin.x + 9.0f;
    const ImVec4 &messageColor = LevelColorForString(m_latestLevel);
    if (m_latestLevel == "warning" || m_latestLevel == "error") {
        const char *marker = m_latestLevel == "error" ? "!" : "!";
        draw->AddText(ImVec2(messageX, textY), ImGui::ColorConvertFloat4ToU32(messageColor), marker);
        messageX += 13.0f;
    }
    const std::string &summary = m_latestMessage.empty() ? std::string("Console") : m_latestMessage;
    draw->PushClipRect(ImVec2(messageX, origin.y), ImVec2((std::max)(messageX, countX - 9.0f), origin.y + barHeight),
                       true);
    draw->AddText(ImVec2(messageX, textY), ImGui::ColorConvertFloat4ToU32(messageColor), summary.c_str());
    draw->PopClipRect();

    float x = countX;
    draw->AddText(ImVec2(x, textY),
                  ImGui::ColorConvertFloat4ToU32(m_infoCount > 0 ? EditorTheme::LOG_INFO : EditorTheme::LOG_DIM),
                  infoText);
    x += infoWidth + countGap;
    draw->AddText(ImVec2(x, textY),
                  ImGui::ColorConvertFloat4ToU32(m_warningCount > 0 ? EditorTheme::LOG_WARNING : EditorTheme::LOG_DIM),
                  warningText);
    x += warningWidth + countGap;
    draw->AddText(ImVec2(x, textY),
                  ImGui::ColorConvertFloat4ToU32(m_errorCount > 0 ? EditorTheme::LOG_ERROR : EditorTheme::LOG_DIM),
                  errorText);

    if (statusActive)
        RenderEngineStatus(consoleRight, origin.y, statusWidth, barHeight, m_statusText, m_statusProgress,
                           m_statusKind);

    ImGui::SetCursorScreenPos(ImVec2(origin.x, origin.y + barHeight));
    // SetCursorScreenPos alone is not a layout item. Submit a zero-sized item
    // so current ImGui versions can account for the explicit bar boundary.
    ImGui::Dummy(ImVec2(0.0f, 0.0f));
}

void StatusBarPanel::RenderEngineStatus(float x, float y, float width, float height, const std::string &text,
                                        float progress, const std::string &kind)
{
    const bool determinate = kind == "progress" && progress >= 0.0f && progress < 1.0f;
    ImVec4 statusColor = EditorTheme::STATUS_PROGRESS_LABEL_CLR;
    const char *statusIcon = "";
    if (kind == "success") {
        statusColor = ImVec4(0.42f, 0.78f, 0.48f, 1.0f);
        statusIcon = "OK";
    } else if (kind == "warning") {
        statusColor = EditorTheme::LOG_WARNING;
        statusIcon = "!";
    } else if (kind == "error") {
        statusColor = EditorTheme::LOG_ERROR;
        statusIcon = "!";
    } else if (kind == "activity") {
        const float pulse = 0.68f + 0.22f * static_cast<float>(std::sin(ImGui::GetTime() * 4.0));
        statusColor = ImVec4(0.55f, 0.72f, 0.90f, pulse);
        statusIcon = "*";
    }

    ImDrawList *draw = ImGui::GetWindowDrawList();
    draw->AddLine(ImVec2(x, y), ImVec2(x, y + height), ImGui::ColorConvertFloat4ToU32(EditorTheme::STATUS_BAR_BORDER));
    draw->AddRectFilled(ImVec2(x + 1.0f, y), ImVec2(x + width, y + height),
                        ImGui::ColorConvertFloat4ToU32(EditorTheme::STATUS_ACTIVITY_BG));

    const float textY = y + (height - ImGui::GetTextLineHeight()) * 0.5f;
    float textX = x + 11.0f;
    if (*statusIcon != '\0') {
        draw->AddText(ImVec2(textX, textY), ImGui::ColorConvertFloat4ToU32(statusColor), statusIcon);
        textX += ImGui::CalcTextSize(statusIcon).x + 7.0f;
    }

    char percentage[16] = {};
    float percentageWidth = 0.0f;
    if (determinate) {
        snprintf(percentage, sizeof(percentage), "%d%%",
                 static_cast<int>(std::round((std::min)((std::max)(progress, 0.0f), 1.0f) * 100.0f)));
        percentageWidth = ImGui::CalcTextSize(percentage).x;
        draw->AddText(ImVec2(x + width - percentageWidth - 10.0f, textY),
                      ImGui::ColorConvertFloat4ToU32(EditorTheme::STATUS_PROGRESS_LABEL_CLR), percentage);
    }

    const float labelRight = x + width - (determinate ? percentageWidth + 20.0f : 9.0f);
    draw->PushClipRect(ImVec2(textX, y), ImVec2((std::max)(textX, labelRight), y + height), true);
    draw->AddText(ImVec2(textX, textY), ImGui::ColorConvertFloat4ToU32(statusColor), text.c_str());
    draw->PopClipRect();

    const float lineHeight = 2.0f;
    const float lineY = y + height - lineHeight;
    if (determinate) {
        const float clamped = (std::min)((std::max)(progress, 0.0f), 1.0f);
        draw->AddRectFilled(ImVec2(x + 1.0f, lineY), ImVec2(x + width, y + height),
                            ImGui::ColorConvertFloat4ToU32(EditorTheme::STATUS_PROGRESS_BG));
        draw->AddRectFilled(ImVec2(x + 1.0f, lineY), ImVec2(x + 1.0f + (width - 1.0f) * clamped, y + height),
                            ImGui::ColorConvertFloat4ToU32(EditorTheme::STATUS_PROGRESS_CLR));
    } else if (kind == "activity") {
        const float trackWidth = (std::max)(40.0f, width * 0.28f);
        const float travel = (std::max)(1.0f, width - trackWidth - 2.0f);
        const float phase = static_cast<float>(std::fmod(ImGui::GetTime() * 92.0, static_cast<double>(travel)));
        draw->AddRectFilled(ImVec2(x + 1.0f + phase, lineY), ImVec2(x + 1.0f + phase + trackWidth, y + height),
                            ImGui::ColorConvertFloat4ToU32(statusColor));
    }
}

const ImVec4 &StatusBarPanel::LevelColorForString(const std::string &level) const
{
    if (level == "error")
        return EditorTheme::LOG_ERROR;
    if (level == "warning")
        return EditorTheme::LOG_WARNING;
    return EditorTheme::LOG_INFO;
}

} // namespace infernux
