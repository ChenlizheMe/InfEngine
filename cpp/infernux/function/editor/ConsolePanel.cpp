#include "ConsolePanel.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstring>
#include <iomanip>
#include <sstream>

namespace infernux
{

// ════════════════════════════════════════════════════════════════════
// Construction / Destruction
// ════════════════════════════════════════════════════════════════════

ConsolePanel::ConsolePanel() : EditorPanel("Console", "console")
{
    // Subscribe to INXLOG — receives ALL C++ log messages.
    m_sinkId = InxLog::GetInstance().AddSink(
        [this](LogLevel level, const char *file, int line, const std::string &message, bool internalOnly) {
            OnLogMessage(level, file, line, message, internalOnly);
        });
}

ConsolePanel::~ConsolePanel()
{
    InxLog::GetInstance().RemoveSink(m_sinkId);
}

std::unordered_map<std::string, double> ConsolePanel::ConsumeSubTimings()
{
    std::unordered_map<std::string, double> result{
        {"flush", m_subFlush}, {"cache", m_subCache}, {"toolbar", m_subToolbar}, {"body", m_subBody}};
    m_subFlush = m_subCache = m_subToolbar = m_subBody = 0.0;
    return result;
}

// ════════════════════════════════════════════════════════════════════
// INXLOG sink callback (may be called from ANY thread)
// ════════════════════════════════════════════════════════════════════

void ConsolePanel::OnLogMessage(LogLevel level, const char *file, int line, const std::string &message,
                                bool internalOnly)
{
    if (internalOnly || IsInternalNoise(message))
        return;

    LogEntry entry;
    entry.level = level;
    entry.message = message;
    entry.sourceFile = file ? file : "";
    entry.sourceLine = line;
    entry.timestamp = CurrentTimestamp();

    // Cache first line for display
    auto nl = entry.message.find('\n');
    entry.firstLine = (nl != std::string::npos) ? entry.message.substr(0, nl) : entry.message;

    {
        std::lock_guard<std::mutex> lock(m_logMutex);
        entry.uid = m_nextUid++;
        switch (entry.level) {
        case LOG_WARN:
            ++m_warnCount;
            break;
        case LOG_ERROR:
        case LOG_FATAL:
            ++m_errorCount;
            break;
        default:
            ++m_infoCount;
            break;
        }
        m_pendingLogs.push_back(std::move(entry));
        m_revision.fetch_add(1, std::memory_order_release);
    }
}

// ════════════════════════════════════════════════════════════════════
// Public API
// ════════════════════════════════════════════════════════════════════

void ConsolePanel::LogFromPython(LogLevel level, const std::string &message, const std::string &stackTrace,
                                 const std::string &sourceFile, int sourceLine)
{
    LogEntry entry;
    entry.level = level;
    entry.message = message;
    entry.stackTrace = stackTrace;
    entry.sourceFile = sourceFile;
    entry.sourceLine = sourceLine;
    entry.timestamp = CurrentTimestamp();

    auto nl = entry.message.find('\n');
    entry.firstLine = (nl != std::string::npos) ? entry.message.substr(0, nl) : entry.message;

    {
        std::lock_guard<std::mutex> lock(m_logMutex);
        entry.uid = m_nextUid++;
        switch (entry.level) {
        case LOG_WARN:
            ++m_warnCount;
            break;
        case LOG_ERROR:
        case LOG_FATAL:
            ++m_errorCount;
            break;
        default:
            ++m_infoCount;
            break;
        }
        m_pendingLogs.push_back(std::move(entry));
        m_revision.fetch_add(1, std::memory_order_release);
    }
}

void ConsolePanel::Clear()
{
    std::lock_guard<std::mutex> lock(m_logMutex);
    m_logs.clear();
    m_pendingLogs.clear();
    m_infoCount = 0;
    m_warnCount = 0;
    m_errorCount = 0;
    m_selectedUid = 0;
    m_requestedUid = 0;
    m_followTail = true;
    m_scrollToBottom = false;
    m_nextUid = 1;
    m_cacheDirty = true;
    m_cachedInfoCount = 0;
    m_cachedWarnCount = 0;
    m_cachedErrorCount = 0;
    m_visible.clear();
    m_collapseLookup.clear();
    m_revision.fetch_add(1, std::memory_order_release);
}

int ConsolePanel::GetInfoCount() const
{
    int infoCount = 0;
    int warnCount = 0;
    int errorCount = 0;
    GetCountSnapshot(infoCount, warnCount, errorCount);
    return infoCount;
}

int ConsolePanel::GetWarningCount() const
{
    int infoCount = 0;
    int warnCount = 0;
    int errorCount = 0;
    GetCountSnapshot(infoCount, warnCount, errorCount);
    return warnCount;
}

int ConsolePanel::GetErrorCount() const
{
    int infoCount = 0;
    int warnCount = 0;
    int errorCount = 0;
    GetCountSnapshot(infoCount, warnCount, errorCount);
    return errorCount;
}

void ConsolePanel::SelectLatestEntry()
{
    FlushPendingLogs();
    if (m_logs.empty()) {
        m_isOpen = true;
        if (onRequestFocus)
            onRequestFocus();
        ImGui::SetWindowFocus((m_title + "###" + m_windowId).c_str());
        return;
    }
    SelectUid(m_logs.back().uid, true);
}

void ConsolePanel::SelectEntry(uint64_t uid)
{
    FlushPendingLogs();
    if (uid == 0) {
        SelectLatestEntry();
        return;
    }
    SelectUid(uid, true);
}

void ConsolePanel::GetStatusBarSnapshot(std::string &outMsg, std::string &outLevel, int &outInfoCount,
                                        int &outWarnCount, int &outErrorCount, uint64_t &outUid)
{
    // The status bar is always rendered, even when the Console window is
    // closed. Flushing here keeps the native queue bounded and gives the
    // displayed message a stable UID that the subsequent click can select.
    FlushPendingLogs();
    outMsg.clear();
    outLevel = "info";
    outUid = 0;
    GetCountSnapshot(outInfoCount, outWarnCount, outErrorCount);
    LogEntry pendingLatest;
    bool hasPending = false;
    {
        std::lock_guard<std::mutex> lock(m_logMutex);
        if (!m_pendingLogs.empty()) {
            pendingLatest = m_pendingLogs.back();
            hasPending = true;
        }
    }
    if (!hasPending && m_logs.empty())
        return;
    const LogEntry &log = hasPending ? pendingLatest : m_logs.back();
    outMsg = log.firstLine;
    outUid = log.uid;
    switch (log.level) {
    case LOG_WARN:
        outLevel = "warning";
        break;
    case LOG_ERROR:
    case LOG_FATAL:
        outLevel = "error";
        break;
    default:
        outLevel = "info";
        break;
    }
}

uint64_t ConsolePanel::GetRevision() const noexcept
{
    return m_revision.load(std::memory_order_acquire);
}

uint64_t ConsolePanel::GetSelectedUid() const noexcept
{
    return m_selectedUid;
}

// ════════════════════════════════════════════════════════════════════
// Render
// ════════════════════════════════════════════════════════════════════

void ConsolePanel::OnRenderContent(InxGUIContext *ctx)
{
    const auto toolbarStart = std::chrono::steady_clock::now();
    RenderToolbar(ctx);
    const auto bodyStart = std::chrono::steady_clock::now();
    RenderBody(ctx);
    const auto bodyEnd = std::chrono::steady_clock::now();
    m_subToolbar += std::chrono::duration<double, std::milli>(bodyStart - toolbarStart).count();
    m_subBody += std::chrono::duration<double, std::milli>(bodyEnd - bodyStart).count();
}

void ConsolePanel::PreRender(InxGUIContext * /*ctx*/)
{
    const auto flushStart = std::chrono::steady_clock::now();
    FlushPendingLogs();
    const auto cacheStart = std::chrono::steady_clock::now();
    EnsureCache();
    const auto cacheEnd = std::chrono::steady_clock::now();
    m_subFlush += std::chrono::duration<double, std::milli>(cacheStart - flushStart).count();
    m_subCache += std::chrono::duration<double, std::milli>(cacheEnd - cacheStart).count();
}

// ════════════════════════════════════════════════════════════════════
// Flush pending logs (main thread only)
// ════════════════════════════════════════════════════════════════════

void ConsolePanel::FlushPendingLogs()
{
    std::vector<LogEntry> incoming;
    {
        std::lock_guard<std::mutex> lock(m_logMutex);
        if (m_pendingLogs.empty())
            return;
        incoming.swap(m_pendingLogs);
    }

    DetectFilterChange();
    const bool appendToVisible = !m_cacheDirty && !m_filterDirty;
    bool receivedError = false;
    for (auto &entry : incoming) {
        const size_t logIndex = m_logs.size();
        if (appendToVisible && MatchesCurrentFilters(entry)) {
            if (collapse) {
                const std::string key = CollapseKey(entry);
                const auto it = m_collapseLookup.find(key);
                if (it != m_collapseLookup.end()) {
                    VisibleEntry &visible = m_visible[it->second];
                    ++visible.count;
                    visible.latestUid = entry.uid;
                } else {
                    m_collapseLookup.emplace(key, m_visible.size());
                    m_visible.push_back({logIndex, 1, entry.uid, entry.uid});
                }
            } else {
                m_visible.push_back({logIndex, 1, entry.uid, entry.uid});
            }
        }
        switch (entry.level) {
        case LOG_WARN:
            ++m_cachedWarnCount;
            break;
        case LOG_ERROR:
        case LOG_FATAL:
            ++m_cachedErrorCount;
            receivedError = true;
            break;
        default:
            ++m_cachedInfoCount;
            break;
        }
        m_logs.push_back(std::move(entry));
    }

    bool trimmed = false;
    while (m_logs.size() > MAX_LOGS) {
        switch (m_logs.front().level) {
        case LOG_WARN:
            --m_warnCount;
            break;
        case LOG_ERROR:
        case LOG_FATAL:
            --m_errorCount;
            break;
        default:
            --m_infoCount;
            break;
        }
        m_logs.pop_front();
        trimmed = true;
    }

    // The common non-collapse path can extend the visible cache in O(new logs).
    // Filtering/collapse changes and deque trimming still use the full rebuild.
    if (!appendToVisible || trimmed)
        m_cacheDirty = true;
    if (autoScroll && m_followTail)
        m_scrollToBottom = true;
    if (receivedError && errorPause && onErrorPause)
        onErrorPause();
}

void ConsolePanel::GetCountSnapshot(int &infoCount, int &warnCount, int &errorCount) const
{
    infoCount = m_infoCount.load(std::memory_order_relaxed);
    warnCount = m_warnCount.load(std::memory_order_relaxed);
    errorCount = m_errorCount.load(std::memory_order_relaxed);
}

// ════════════════════════════════════════════════════════════════════
// Filter cache
// ════════════════════════════════════════════════════════════════════

void ConsolePanel::DetectFilterChange()
{
    const std::string search = m_search.data();
    bool changed = (showInfo != m_prevShowInfo || showWarnings != m_prevShowWarnings ||
                    showErrors != m_prevShowErrors || collapse != m_prevCollapse || search != m_prevSearch);
    if (changed) {
        m_prevShowInfo = showInfo;
        m_prevShowWarnings = showWarnings;
        m_prevShowErrors = showErrors;
        m_prevCollapse = collapse;
        m_prevSearch = search;
        m_filterDirty = true;
    }
}

bool ConsolePanel::MatchesCurrentFilters(const LogEntry &entry) const
{
    bool severityMatches = showInfo;
    if (entry.level == LOG_WARN)
        severityMatches = showWarnings;
    else if (entry.level == LOG_ERROR || entry.level == LOG_FATAL)
        severityMatches = showErrors;
    if (!severityMatches)
        return false;

    const std::string needle = m_search.data();
    if (needle.empty())
        return true;

    auto containsCaseInsensitive = [&needle](const std::string &value) {
        return std::search(value.begin(), value.end(), needle.begin(), needle.end(), [](char lhs, char rhs) {
                   return std::tolower(static_cast<unsigned char>(lhs)) ==
                          std::tolower(static_cast<unsigned char>(rhs));
               }) != value.end();
    };
    return containsCaseInsensitive(entry.message) || containsCaseInsensitive(entry.stackTrace) ||
           containsCaseInsensitive(entry.sourceFile);
}

std::string ConsolePanel::CollapseKey(const LogEntry &entry) const
{
    return std::to_string(static_cast<int>(entry.level)) + "|" + entry.message;
}

int ConsolePanel::FindVisibleIndexByUid(uint64_t uid) const
{
    if (uid == 0)
        return -1;
    for (int index = 0; index < static_cast<int>(m_visible.size()); ++index) {
        const VisibleEntry &entry = m_visible[index];
        if (entry.uid == uid || entry.latestUid == uid)
            return index;
    }
    if (!collapse)
        return -1;

    const auto target =
        std::find_if(m_logs.begin(), m_logs.end(), [uid](const LogEntry &entry) { return entry.uid == uid; });
    if (target == m_logs.end())
        return -1;
    const std::string targetKey = CollapseKey(*target);
    const auto group = m_collapseLookup.find(targetKey);
    return group == m_collapseLookup.end() ? -1 : static_cast<int>(group->second);
}

void ConsolePanel::SelectUid(uint64_t uid, bool focusWindow)
{
    m_isOpen = true;
    m_requestedUid = uid;
    m_selectedUid = uid;
    m_followTail = false;
    m_scrollToBottom = false;
    m_search[0] = '\0';

    const auto target =
        std::find_if(m_logs.begin(), m_logs.end(), [uid](const LogEntry &entry) { return entry.uid == uid; });
    if (target != m_logs.end()) {
        if (target->level == LOG_WARN)
            showWarnings = true;
        else if (target->level == LOG_ERROR || target->level == LOG_FATAL)
            showErrors = true;
        else
            showInfo = true;
    }
    if (focusWindow) {
        if (onRequestFocus)
            onRequestFocus();
        ImGui::SetWindowFocus((m_title + "###" + m_windowId).c_str());
    }
}

void ConsolePanel::EnsureCache()
{
    DetectFilterChange();
    if (!m_cacheDirty && !m_filterDirty)
        return;

    // Rebuild counts
    int ic = 0, wc = 0, ec = 0;
    GetCountSnapshot(ic, wc, ec);
    m_cachedInfoCount = ic;
    m_cachedWarnCount = wc;
    m_cachedErrorCount = ec;

    // Rebuild visible list
    m_visible.clear();
    m_collapseLookup.clear();

    for (size_t i = 0; i < m_logs.size(); ++i) {
        const auto &log = m_logs[i];

        // Apply filters
        if (!MatchesCurrentFilters(log))
            continue;

        if (collapse) {
            // Build collapse key: level + message
            const std::string key = CollapseKey(log);
            auto it = m_collapseLookup.find(key);
            if (it != m_collapseLookup.end()) {
                m_visible[it->second].count++;
                m_visible[it->second].latestUid = log.uid;
                continue;
            }
            m_collapseLookup[key] = m_visible.size();
        }

        VisibleEntry ve;
        ve.logIndex = i;
        ve.count = 1;
        ve.uid = log.uid;
        ve.latestUid = log.uid;
        m_visible.push_back(ve);
    }

    if (m_selectedUid > 0 && FindVisibleIndexByUid(m_selectedUid) < 0)
        m_selectedUid = 0;

    m_cacheDirty = false;
    m_filterDirty = false;
}

// ════════════════════════════════════════════════════════════════════
// Toolbar
// ════════════════════════════════════════════════════════════════════

void ConsolePanel::RenderToolbar(InxGUIContext *ctx)
{
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(EditorTheme::CONSOLE_FRAME_PAD_X, EditorTheme::CONSOLE_FRAME_PAD_Y));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(EditorTheme::CONSOLE_ITEM_SPC_X, EditorTheme::CONSOLE_ITEM_SPC_Y));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, EditorTheme::TOOLBAR_FRAME_BRD);

    const float availableWidth = ImGui::GetContentRegionAvail().x;
    const bool wrapOptions = availableWidth < 500.0f;

    if (ImGui::Button("Clear", ImVec2(54.0f, 0.0f)))
        Clear();
    ctx->RecordSemanticItem("console_action", "Clear", true, "console.clear");

    ImGui::SameLine();
    ImGui::Checkbox("Collapse", &collapse);
    ctx->RecordSemanticItem("checkbox", "Collapse", true, "console.collapse", collapse);

    if (wrapOptions)
        ImGui::NewLine();
    else
        ImGui::SameLine();
    ImGui::Checkbox("Clear on Play", &clearOnPlay);
    ctx->RecordSemanticItem("checkbox", "Clear on Play", true, "console.clear_on_play", clearOnPlay);

    ImGui::SameLine();
    ImGui::Checkbox("Error Pause", &errorPause);
    ctx->RecordSemanticItem("checkbox", "Error Pause", true, "console.error_pause", errorPause);

    ImGui::SameLine();
    bool follow = autoScroll && m_followTail;
    if (ImGui::Checkbox("Follow", &follow)) {
        autoScroll = follow;
        m_followTail = follow;
        if (follow) {
            m_selectedUid = 0;
            m_requestedUid = 0;
            m_scrollToBottom = true;
        }
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Keep the view pinned to incoming messages");
    ctx->RecordSemanticItem("checkbox", "Follow", true, "console.follow", follow);

    // Search and severity filters use a dedicated row, matching the Console's
    // two distinct jobs: controlling capture and inspecting messages.
    ImGui::NewLine();
    constexpr float segmentWidth = 78.0f;
    constexpr float segmentGap = 3.0f;
    constexpr float severityWidth = segmentWidth * 3.0f + segmentGap * 2.0f;
    const bool stackSeverity = availableWidth < severityWidth + 120.0f;
    const float searchWidth =
        stackSeverity ? availableWidth : (std::max)(100.0f, availableWidth - severityWidth - 8.0f);
    ImGui::SetNextItemWidth(searchWidth);
    ImGui::InputTextWithHint("##ConsoleSearch", "Search messages, files, and stack traces", m_search.data(),
                             m_search.size());
    ctx->RecordSemanticItem("text_input", "Search messages, files, and stack traces", true, "console.search",
                            std::nullopt, std::nullopt, std::string(m_search.data()));
    if (ImGui::IsItemEdited())
        m_followTail = false;

    auto severitySegment = [&](const char *id, const char *name, int count, bool &enabled, const ImVec4 &color) {
        char label[64];
        if (count > 999)
            snprintf(label, sizeof(label), "%s 999+###%s", name, id);
        else
            snprintf(label, sizeof(label), "%s %d###%s", name, count, id);

        ImGui::PushStyleColor(ImGuiCol_Text, enabled ? color : EditorTheme::LOG_DIM);
        ImGui::PushStyleColor(ImGuiCol_Button, enabled ? EditorTheme::CONSOLE_SEGMENT_ACTIVE : EditorTheme::BTN_GHOST);
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, EditorTheme::BTN_GHOST_HOVERED);
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, EditorTheme::BTN_GHOST_ACTIVE);
        if (ImGui::Button(label, ImVec2(segmentWidth, 0.0f)))
            enabled = !enabled;
        ctx->RecordSemanticItem("console_filter", name, true, std::string("console.filter.") + id, enabled,
                                static_cast<double>(count));
        ImGui::PopStyleColor(4);
    };

    if (stackSeverity)
        ImGui::NewLine();
    else
        ImGui::SameLine(0.0f, 6.0f);
    severitySegment("ConsoleFilterInfo", "Log", m_cachedInfoCount, showInfo, EditorTheme::LOG_INFO);
    ImGui::SameLine(0.0f, segmentGap);
    severitySegment("ConsoleFilterWarn", "Warn", m_cachedWarnCount, showWarnings, EditorTheme::LOG_WARNING);
    ImGui::SameLine(0.0f, segmentGap);
    severitySegment("ConsoleFilterError", "Error", m_cachedErrorCount, showErrors, EditorTheme::LOG_ERROR);

    ImGui::PopStyleVar(3);
}

// ════════════════════════════════════════════════════════════════════
// Body (log list + detail pane)
// ════════════════════════════════════════════════════════════════════

void ConsolePanel::RenderBody(InxGUIContext *ctx)
{
    float availH = ImGui::GetContentRegionAvail().y;
    int selectedIndex = FindVisibleIndexByUid(m_selectedUid);
    bool hasDetail = selectedIndex >= 0;

    float splitterH = 3.0f;
    float listH;
    if (hasDetail) {
        m_detailHeight = (std::max)(40.0f, (std::min)(m_detailHeight, availH - 60.0f));
        listH = (std::max)(availH - m_detailHeight - splitterH, 40.0f);
    } else {
        listH = 0.0f; // 0 = use remaining space
    }

    int total = static_cast<int>(m_visible.size());
    float rowH = m_rowHeight;

    // ── Log list (virtual-scrolled) ──
    ImGui::PushStyleColor(ImGuiCol_Border, EditorTheme::BORDER_TRANSPARENT);
    if (ImGui::BeginChild("##ConsoleLogList", ImVec2(0, listH), ImGuiChildFlags_Borders)) {
        // Freeze follow on mouse-down, before Selectable resolves on release.
        // New messages continue entering the model without moving the target
        // row out from under the pointer.
        if (ImGui::IsWindowHovered() &&
            (ImGui::IsMouseDown(ImGuiMouseButton_Left) || ImGui::GetIO().MouseWheel != 0.0f)) {
            m_followTail = false;
            m_scrollToBottom = false;
        }

        if (m_requestedUid > 0) {
            selectedIndex = FindVisibleIndexByUid(m_requestedUid);
            if (selectedIndex >= 0) {
                const float targetY = selectedIndex * rowH;
                const float currentY = ImGui::GetScrollY();
                const float viewH = ImGui::GetContentRegionAvail().y;
                if (targetY < currentY || targetY + rowH > currentY + viewH)
                    ImGui::SetScrollY((std::max)(0.0f, targetY - viewH * 0.35f));
            }
            m_requestedUid = 0;
        }

        float scrollY = ImGui::GetScrollY();
        float viewportH = ImGui::GetContentRegionAvail().y;
        int firstVis = (rowH > 0.0f) ? (std::max)(static_cast<int>(scrollY / rowH), 0) : 0;
        int lastVis = (total > 0) ? (std::min)(firstVis + static_cast<int>(viewportH / rowH) + 2, total - 1) : -1;

        // Top spacer
        if (firstVis > 0) {
            float w = ImGui::GetContentRegionAvail().x;
            ImGui::Dummy(ImVec2(w, firstVis * rowH));
        }

        // Render visible rows
        for (int idx = (std::max)(firstVis, 0); idx <= lastVis; ++idx) {
            if (!m_rowHeightMeasured) {
                float y0 = ImGui::GetCursorPosY();
                RenderRow(ctx, idx, m_visible[idx], idx == selectedIndex);
                float y1 = ImGui::GetCursorPosY();
                float measured = y1 - y0;
                if (measured > 1.0f) {
                    m_rowHeight = measured;
                    rowH = measured;
                    m_rowHeightMeasured = true;
                }
            } else {
                RenderRow(ctx, idx, m_visible[idx], idx == selectedIndex);
            }
        }

        // Bottom spacer
        int remaining = total - (lastVis + 1);
        if (remaining > 0) {
            float w = ImGui::GetContentRegionAvail().x;
            ImGui::Dummy(ImVec2(w, remaining * rowH));
        }

        // Ctrl+C: copy selected entry
        selectedIndex = FindVisibleIndexByUid(m_selectedUid);
        if (selectedIndex >= 0 && selectedIndex < total) {
            if (ImGui::GetIO().KeyCtrl && ImGui::IsKeyPressed(ImGuiKey_C)) {
                const auto &ve = m_visible[selectedIndex];
                const auto &log = m_logs[ve.logIndex];
                std::string copyText = log.message;
                if (!log.stackTrace.empty())
                    copyText += "\n" + log.stackTrace;
                ImGui::SetClipboardText(copyText.c_str());
            }
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                m_selectedUid = 0;
                selectedIndex = -1;
            }
        }

        if (m_scrollToBottom && !m_visible.empty()) {
            ImGui::SetScrollHereY(1.0f);
            m_scrollToBottom = false;
        }

        // Wheel/scrollbar interaction owns follow state. Appending messages
        // never changes it, so selecting a row remains stable under log floods.
        if (ImGui::IsWindowHovered() &&
            (ImGui::GetIO().MouseWheel != 0.0f || ImGui::IsMouseDragging(ImGuiMouseButton_Left))) {
            scrollY = ImGui::GetScrollY();
            const float scrollMax = ImGui::GetScrollMaxY();
            const bool atBottom = scrollMax <= 0.0f || (scrollMax - scrollY) < 20.0f;
            m_followTail = autoScroll && atBottom && m_selectedUid == 0;
        }
    }
    ImGui::EndChild();
    ImGui::PopStyleColor(); // Border

    // ── Draggable splitter ──
    if (hasDetail) {
        float availW = ImGui::GetContentRegionAvail().x;
        ImGui::PushStyleColor(ImGuiCol_Button, EditorTheme::BTN_GHOST);
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, EditorTheme::SPLITTER_HOVER);
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, EditorTheme::SPLITTER_ACTIVE);
        ImGui::InvisibleButton("##ConsoleSplitter", ImVec2(availW, splitterH));
        if (ImGui::IsItemActive()) {
            float dy = ImGui::GetMouseDragDelta(0).y;
            if (std::abs(dy) > 0.5f) {
                m_detailHeight = (std::max)(40.0f, m_detailHeight - dy);
                ImGui::ResetMouseDragDelta(0);
            }
            ImGui::SetMouseCursor(ImGuiMouseCursor_ResizeNS);
        } else if (ImGui::IsItemHovered()) {
            ImGui::SetMouseCursor(ImGuiMouseCursor_ResizeNS);
        }
        ImGui::PopStyleColor(3);
    }

    // ── Detail pane ──
    selectedIndex = FindVisibleIndexByUid(m_selectedUid);
    if (hasDetail && selectedIndex >= 0 && selectedIndex < static_cast<int>(m_visible.size())) {
        const auto &ve = m_visible[selectedIndex];
        const auto &log = m_logs[ve.logIndex];
        const ImVec4 &clr = LevelColor(log.level);

        std::string detailText = "[" + log.timestamp + "]  " + log.message;
        if (!log.stackTrace.empty())
            detailText += "\n\n" + log.stackTrace;

        ImGui::PushStyleColor(ImGuiCol_Text, clr);
        ImGui::PushStyleColor(ImGuiCol_WindowBg, EditorTheme::ROW_NONE);
        ImGui::PushStyleColor(ImGuiCol_FrameBg, EditorTheme::ROW_NONE);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);

        // Read-only multiline input — supports text selection & Ctrl+C
        ImGui::InputTextMultiline("##ConsoleDetail", const_cast<char *>(detailText.c_str()), detailText.size() + 1,
                                  ImVec2(-1, -1), ImGuiInputTextFlags_ReadOnly);
        ctx->RecordSemanticItem("console_detail", log.firstLine, true, "console.detail", std::nullopt, std::nullopt,
                                detailText);

        ImGui::PopStyleVar();
        ImGui::PopStyleColor(3);
    }
}

// ════════════════════════════════════════════════════════════════════
// Single row
// ════════════════════════════════════════════════════════════════════

void ConsolePanel::RenderRow(InxGUIContext *ctx, int visIdx, const VisibleEntry &ve, bool isSel)
{
    const auto &log = m_logs[ve.logIndex];
    const ImVec4 &clr = LevelColor(log.level);
    // Console selection is intentionally neutral. The blue default header
    // color reads like an active editor selection and overwhelms log levels.
    const ImVec4 selectedRow(59.0f / 255.0f, 59.0f / 255.0f, 64.0f / 255.0f, 1.0f);
    // Row background
    if (isSel)
        ImGui::PushStyleColor(ImGuiCol_Header, selectedRow);
    else if (visIdx % 2 == 1)
        ImGui::PushStyleColor(ImGuiCol_Header, EditorTheme::ROW_ALT);
    else
        ImGui::PushStyleColor(ImGuiCol_Header, EditorTheme::ROW_NONE);

    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, selectedRow);
    ImGui::PushStyleColor(ImGuiCol_HeaderActive, selectedRow);
    ImGui::PushStyleColor(ImGuiCol_Text, clr);

    // Unique ID to avoid ImGui ID conflicts
    char label[512];
    snprintf(label, sizeof(label), "%s##clog_%llu_%d", log.firstLine.c_str(), static_cast<unsigned long long>(ve.uid),
             visIdx);

    if (ImGui::Selectable(label, isSel, ImGuiSelectableFlags_SpanAllColumns | ImGuiSelectableFlags_AllowDoubleClick)) {
        m_selectedUid = ve.uid;
        m_requestedUid = 0;
        m_followTail = false;
        m_scrollToBottom = false;
        // Double-click: navigate to source
        if (ImGui::IsMouseDoubleClicked(0) && onDoubleClickEntry && !log.sourceFile.empty()) {
            onDoubleClickEntry(log.sourceFile, log.sourceLine);
        }
    }
    ctx->RecordSemanticItem("console_entry", log.firstLine, true,
                            "console.entry." + std::to_string(static_cast<unsigned long long>(ve.uid)), isSel,
                            static_cast<double>(ve.count));

    // Collapse count badge
    if (ve.count > 1) {
        ImGui::SameLine(ImGui::GetContentRegionAvail().x - 20.0f);
        ImGui::PushStyleColor(ImGuiCol_Text, EditorTheme::LOG_BADGE);
        ImGui::Text("%d", ve.count);
        ImGui::PopStyleColor();
    }

    ImGui::PopStyleColor(4);
}

// ════════════════════════════════════════════════════════════════════
// Utilities
// ════════════════════════════════════════════════════════════════════

const ImVec4 &ConsolePanel::LevelColor(LogLevel lv) const
{
    switch (lv) {
    case LOG_ERROR:
    case LOG_FATAL:
        return EditorTheme::LOG_ERROR;
    case LOG_WARN:
        return EditorTheme::LOG_WARNING;
    default:
        return EditorTheme::LOG_INFO;
    }
}

std::string ConsolePanel::CurrentTimestamp()
{
    auto now = std::chrono::system_clock::now();
    auto time = std::chrono::system_clock::to_time_t(now);
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()) % 1000;

    std::tm tm{};
#ifdef _WIN32
    localtime_s(&tm, &time);
#else
    localtime_r(&time, &tm);
#endif

    char buf[32];
    snprintf(buf, sizeof(buf), "%02d:%02d:%02d.%03d", tm.tm_hour, tm.tm_min, tm.tm_sec, static_cast<int>(ms.count()));
    return buf;
}

bool ConsolePanel::IsInternalNoise(const std::string &msg)
{
    if (msg.find("DEAR IMGUI") != std::string::npos)
        return true;
    if (msg.find("PushID") != std::string::npos)
        return true;
    if (msg.find("conflicting ID") != std::string::npos)
        return true;
    return false;
}

} // namespace infernux
