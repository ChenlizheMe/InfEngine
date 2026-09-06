#pragma once

#include "EditorPanel.h"
#include "EditorTheme.h"
#include "interaction/EditorSearchModel.h"
#include <core/log/InxLog.h>

#include <imgui.h>

#include <array>
#include <atomic>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux
{

/// C++ native Console panel — replaces the Python ConsolePanel.
/// Subscribes to INXLOG sinks to receive all engine log messages directly.
/// Also exposes LogFromPython() so Python Debug.log() messages appear here.
class ConsolePanel : public EditorPanel
{
  public:
    ConsolePanel();
    ~ConsolePanel() override;

    // ── Public API (called from pybind11 or other C++ systems) ──

    /// Log a message originating from Python's Debug.log() system.
    void LogFromPython(LogLevel level, const std::string &message, const std::string &stackTrace = "",
                       const std::string &sourceFile = "", int sourceLine = 0);

    /// Clear all log entries.
    void Clear();

    /// Remove diagnostics owned by one source file without disturbing other
    /// Console history. Returns the number of removed entries.
    size_t RemoveEntriesFromSource(const std::string &sourceFile);

    /// Query counts for status bar integration.
    int GetInfoCount() const;
    int GetWarningCount() const;
    int GetErrorCount() const;

    std::unordered_map<std::string, double> ConsumeSubTimings() override;

    /// Select the latest entry and request window focus.
    /// Called from status bar click.
    void SelectLatestEntry();
    void SelectEntry(uint64_t uid);
    /// Project the authoritative editor selection without publishing a second
    /// selection event back to Python.
    void SetSelectionSnapshot(uint64_t uid);

    /// Authoritative status-bar snapshot. The latest entry and counts do not
    /// depend on the Console window's current filters.
    void GetStatusBarSnapshot(std::string &outMsg, std::string &outLevel, int &outInfoCount, int &outWarnCount,
                              int &outErrorCount, uint64_t &outUid);
    [[nodiscard]] uint64_t GetRevision() const noexcept;
    [[nodiscard]] uint64_t GetSelectedUid() const noexcept;
    [[nodiscard]] bool HasSelectedEntry() const noexcept;

    /// Snapshot the entries currently visible in the native Console view.
    /// This is intentionally a bounded, filtered view so external tools do
    /// not copy the full log buffer every frame.
    struct VisibleLogSnapshot
    {
        std::string message;
        std::string timestamp;
        std::string stackTrace;
        std::string sourceFile;
        int sourceLine = 0;
        LogLevel level = LOG_INFO;
        uint64_t uid = 0;
        uint64_t latestUid = 0;
        int count = 1;
    };

    std::vector<VisibleLogSnapshot> GetVisibleLogSnapshot(size_t limit);
    bool CopySelectedEntry();
    [[nodiscard]] bool HasViewOption(const std::string &option) const noexcept;
    [[nodiscard]] bool GetViewOption(const std::string &option) const noexcept;
    void SetViewOption(const std::string &option, bool enabled);
    [[nodiscard]] std::string GetSearchQuery() const;
    void SetSearchQuery(const std::string &query);
    void RequestSearchFocus()
    {
        m_focusSearchNextFrame = true;
    }
    [[nodiscard]] float GetDetailHeight() const noexcept;
    void SetDetailHeight(float height) noexcept;

    std::function<void()> onErrorPause;
    std::function<void()> onRequestFocus;
    std::function<void(uint64_t, bool)> onSelectionChanged;

    /// Filter state — exposed for pybind11 property access.
    bool showInfo = true;
    bool showWarnings = true;
    bool showErrors = true;
    bool collapse = false;
    bool clearOnPlay = true;
    bool errorPause = false;
    bool autoScroll = true;

  protected:
    void PreRender(InxGUIContext *ctx) override;
    void OnRenderContent(InxGUIContext *ctx) override;

  private:
    // ── Internal log entry ──
    struct LogEntry
    {
        std::string message;
        std::string firstLine; // cached first line for display
        std::string timestamp;
        std::string stackTrace;
        std::string sourceFile;
        int sourceLine = 0;
        LogLevel level = LOG_INFO;
        uint64_t uid = 0;
    };

    // ── Visible entry after filtering/collapsing ──
    struct VisibleEntry
    {
        size_t logIndex; // index into m_logs
        int count;       // collapse count
        uint64_t uid;    // stable UID of the first entry in this group
        uint64_t latestUid;
    };

    // ── INXLOG sink ──
    size_t m_sinkId = 0;
    void OnLogMessage(LogLevel level, const char *file, int line, const std::string &message, bool internalOnly);

    // ── Log storage ──
    static constexpr size_t MAX_LOGS = 100000u;
    std::deque<LogEntry> m_logs;
    uint64_t m_nextUid = 1;
    mutable std::mutex m_logMutex;       // protects m_logs + m_pendingLogs
    std::vector<LogEntry> m_pendingLogs; // accumulated off-main-thread, flushed in OnRender
    std::atomic<int> m_infoCount{0};
    std::atomic<int> m_warnCount{0};
    std::atomic<int> m_errorCount{0};
    std::atomic<uint64_t> m_revision{1};

    // ── Filter cache ──
    bool m_cacheDirty = true;
    bool m_filterDirty = true;
    bool m_prevShowInfo = true;
    bool m_prevShowWarnings = true;
    bool m_prevShowErrors = true;
    bool m_prevCollapse = false;
    EditorSearchModel m_searchModel;
    std::vector<VisibleEntry> m_visible;
    std::unordered_map<std::string, size_t> m_collapseLookup;
    int m_cachedInfoCount = 0;
    int m_cachedWarnCount = 0;
    int m_cachedErrorCount = 0;

    // ── Selection projection & scroll ──
    // m_selectedUid mirrors SelectionService for drawing only. User actions
    // publish intent and never mutate this field directly.
    uint64_t m_selectedUid = 0;
    uint64_t m_requestedUid = 0;
    bool m_followTail = true;
    bool m_scrollToBottom = false;
    std::array<char, 256> m_search{};
    std::string m_searchEditStart;
    bool m_focusSearchNextFrame = false;
    float m_rowHeight = 22.0f;
    bool m_rowHeightMeasured = false;
    float m_lastDpiScale = 0.0f;
    float m_detailHeight = 90.0f;
    float m_detailResizeStart = 90.0f;

    double m_subFlush = 0.0;
    double m_subCache = 0.0;
    double m_subToolbar = 0.0;
    double m_subBody = 0.0;

    // ── Helpers ──
    void FlushPendingLogs();
    void GetCountSnapshot(int &infoCount, int &warnCount, int &errorCount) const;
    void EnsureCache();
    void DetectFilterChange();
    bool MatchesCurrentFilters(const LogEntry &entry) const;
    std::string CollapseKey(const LogEntry &entry) const;
    int FindVisibleIndexByUid(uint64_t uid) const;
    void SelectUid(uint64_t uid, bool focusWindow, bool publishSelection = true, bool recordHistory = true);
    void PublishSelection(uint64_t uid, bool recordHistory);
    void RenderToolbar(InxGUIContext *ctx);
    void RenderBody(InxGUIContext *ctx);
    void RenderRow(InxGUIContext *ctx, int visIdx, const VisibleEntry &ve, bool selected);
    const ImVec4 &LevelColor(LogLevel lv) const;
    static std::string CurrentTimestamp();
    static bool IsInternalNoise(const std::string &msg);
};

} // namespace infernux
