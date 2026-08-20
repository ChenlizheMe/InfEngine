#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>

namespace infernux
{

/// Limits the extra editor builds needed to drain one synthetic ImGui input
/// batch. Physical input arriving on later frames never extends this budget.
class EditorGuiInputRearmBudget final
{
  public:
    static constexpr uint32_t kMaxFrames = 4;

    void BeginBatch() noexcept
    {
        m_remaining = kMaxFrames;
    }

    [[nodiscard]] bool AfterBuild(bool hasPendingTransitions) noexcept
    {
        if (m_remaining == 0 || !hasPendingTransitions) {
            m_remaining = 0;
            return false;
        }
        --m_remaining;
        return m_remaining != 0;
    }

    [[nodiscard]] uint32_t Remaining() const noexcept
    {
        return m_remaining;
    }

  private:
    uint32_t m_remaining = 0;
};

/// Limits expensive Editor ImGui construction without limiting scene or game rendering.
class EditorGuiFrameScheduler
{
  public:
    using Clock = std::chrono::steady_clock;
    using TimePoint = Clock::time_point;

    explicit EditorGuiFrameScheduler(double targetHz = 60.0)
        : m_interval(targetHz > 0.0
                         ? std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(1.0 / targetHz))
                         : Clock::duration::zero())
    {
    }

    [[nodiscard]] bool Consume(TimePoint now, bool force = false)
    {
        return ConsumeInternal(now, force, false);
    }

    // Standalone Player builds do not have authoring panels to throttle. Keep
    // this path explicit so Editor Play cannot accidentally inherit Player's
    // unthrottled cadence through the generic force flag.
    [[nodiscard]] bool ConsumeUnthrottled(TimePoint now, bool force = false)
    {
        return ConsumeInternal(now, force, true);
    }

    void Request() noexcept
    {
        m_requested.store(true, std::memory_order_release);
        m_requestCount.fetch_add(1, std::memory_order_relaxed);
    }

    struct Snapshot
    {
        bool started = false;
        bool requested = false;
        double intervalMs = 0.0;
        double untilDueMs = 0.0;
        uint64_t consumeCount = 0;
        uint64_t approvedCount = 0;
        uint64_t forcedCount = 0;
        uint64_t requestCount = 0;
        bool forceActive = false;
    };

    [[nodiscard]] Snapshot Inspect(TimePoint now = Clock::now()) const noexcept
    {
        Snapshot snapshot;
        snapshot.started = m_started;
        snapshot.requested = m_requested.load(std::memory_order_acquire);
        snapshot.intervalMs = std::chrono::duration<double, std::milli>(m_interval).count();
        snapshot.untilDueMs = m_started ? std::chrono::duration<double, std::milli>(m_nextBuild - now).count() : 0.0;
        snapshot.consumeCount = m_consumeCount;
        snapshot.approvedCount = m_approvedCount;
        snapshot.forcedCount = m_forcedCount;
        snapshot.requestCount = m_requestCount.load(std::memory_order_relaxed);
        snapshot.forceActive = m_forceActive;
        return snapshot;
    }

  private:
    [[nodiscard]] bool ConsumeInternal(TimePoint now, bool force, bool unthrottled)
    {
        ++m_consumeCount;
        const bool requested = m_requested.exchange(false, std::memory_order_acq_rel);
        // Input events can keep the renderer's immediate-refresh flag high
        // for a run of consecutive frames (for example while a held key is
        // being repeated).  A force request is an edge-triggered refresh;
        // once that edge has been serviced, the normal cadence still applies.
        // This keeps editor Play responsive without turning every gameplay
        // frame into a full ImGui/Python rebuild.  Explicit Request() calls
        // remain a deliberate invalidation path and may still bypass the
        // cadence.
        const bool forceEdge = force && !m_forceActive;
        m_forceActive = force;
        if (!m_started) {
            m_started = true;
            m_nextBuild = now + m_interval;
            ++m_approvedCount;
            if (force)
                ++m_forcedCount;
            return true;
        }

        const bool due = m_interval == Clock::duration::zero() || now >= m_nextBuild;
        if (!unthrottled && !requested && !due && !(force && forceEdge))
            return false;

        if (unthrottled && m_interval != Clock::duration::zero()) {
            // Leave a sensible cadence marker behind if the process switches
            // back to an editor-owned GUI scheduler after Player mode.
            m_nextBuild = now + m_interval;
        } else if (due && m_interval != Clock::duration::zero()) {
            do {
                m_nextBuild += m_interval;
            } while (m_nextBuild <= now);
        }
        ++m_approvedCount;
        if (force)
            ++m_forcedCount;
        return true;
    }
    Clock::duration m_interval;
    TimePoint m_nextBuild{};
    bool m_started = false;
    std::atomic_bool m_requested{false};
    uint64_t m_consumeCount = 0;
    uint64_t m_approvedCount = 0;
    uint64_t m_forcedCount = 0;
    bool m_forceActive = false;
    std::atomic_uint64_t m_requestCount{0};
};

} // namespace infernux
