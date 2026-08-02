#pragma once

#include <chrono>

namespace infernux
{

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
        if (!m_started) {
            m_started = true;
            m_requested = false;
            m_nextBuild = now + m_interval;
            return true;
        }

        const bool due = m_interval == Clock::duration::zero() || now >= m_nextBuild;
        if (!force && !m_requested && !due)
            return false;

        m_requested = false;
        if (due && m_interval != Clock::duration::zero()) {
            do {
                m_nextBuild += m_interval;
            } while (m_nextBuild <= now);
        }
        return true;
    }

    void Request() noexcept
    {
        m_requested = true;
    }

  private:
    Clock::duration m_interval;
    TimePoint m_nextBuild{};
    bool m_started = false;
    bool m_requested = false;
};

} // namespace infernux
