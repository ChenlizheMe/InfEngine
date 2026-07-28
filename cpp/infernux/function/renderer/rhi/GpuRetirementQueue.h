#pragma once

#include "RhiSubmission.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <stdexcept>
#include <utility>
#include <vector>

namespace infernux
{

/// Defers GPU-owned destruction until the submission that can reference the
/// retired object has completed. There is intentionally no frame-age fallback:
/// callers must bind an exact retirement serial source or provide a serial.
class GpuRetirementQueue
{
  public:
    using SerialSource = std::function<rhi::SubmissionSerial()>;

    struct Stats
    {
        uint64_t collectCalls = 0;
        uint64_t pushed = 0;
        uint64_t retired = 0;
        size_t pending = 0;
        size_t highWatermark = 0;
    };

    GpuRetirementQueue() = default;
    ~GpuRetirementQueue()
    {
        FlushAll();
    }

    GpuRetirementQueue(const GpuRetirementQueue &) = delete;
    GpuRetirementQueue &operator=(const GpuRetirementQueue &) = delete;
    GpuRetirementQueue(GpuRetirementQueue &&other) noexcept
        : m_entries(std::move(other.m_entries)), m_ready(std::move(other.m_ready)),
          m_serialSource(std::move(other.m_serialSource)), m_collectCalls(std::exchange(other.m_collectCalls, 0)),
          m_pushed(std::exchange(other.m_pushed, 0)), m_retired(std::exchange(other.m_retired, 0)),
          m_highWatermark(std::exchange(other.m_highWatermark, 0))
    {
    }

    GpuRetirementQueue &operator=(GpuRetirementQueue &&other) noexcept
    {
        if (this != &other) {
            FlushAll();
            m_entries = std::move(other.m_entries);
            m_ready = std::move(other.m_ready);
            m_serialSource = std::move(other.m_serialSource);
            m_collectCalls = std::exchange(other.m_collectCalls, 0);
            m_pushed = std::exchange(other.m_pushed, 0);
            m_retired = std::exchange(other.m_retired, 0);
            m_highWatermark = std::exchange(other.m_highWatermark, 0);
        }
        return *this;
    }

    void BindSerialSource(SerialSource serialSource)
    {
        if (!serialSource)
            throw std::invalid_argument("GPU retirement requires a submission serial source");
        m_serialSource = std::move(serialSource);
    }

    [[nodiscard]] bool HasSerialSource() const noexcept
    {
        return static_cast<bool>(m_serialSource);
    }

    void Retire(std::function<void()> deleter)
    {
        if (!m_serialSource)
            throw std::logic_error("GPU retirement queue is not bound to a submission serial source");
        RetireAfter(m_serialSource(), std::move(deleter));
    }

    void RetireAfter(rhi::SubmissionSerial retirementSerial, std::function<void()> deleter)
    {
        if (!deleter)
            return;
        m_entries.push_back({retirementSerial, std::move(deleter)});
        ++m_pushed;
        if (m_entries.size() > m_highWatermark)
            m_highWatermark = m_entries.size();
    }

    size_t Collect(rhi::SubmissionSerial completedSerial)
    {
        ++m_collectCalls;
        m_ready.clear();
        size_t writeIndex = 0;
        for (size_t index = 0; index < m_entries.size(); ++index) {
            if (m_entries[index].retirementSerial <= completedSerial) {
                m_ready.push_back(std::move(m_entries[index].deleter));
            } else {
                if (writeIndex != index)
                    m_entries[writeIndex] = std::move(m_entries[index]);
                ++writeIndex;
            }
        }
        m_entries.resize(writeIndex);

        const size_t retiredNow = m_ready.size();
        for (auto &deleter : m_ready) {
            deleter();
            ++m_retired;
        }
        m_ready.clear();
        return retiredNow;
    }

    /// The owner must drain the relevant devices/queues before using this.
    void FlushAll()
    {
        while (!m_entries.empty()) {
            m_ready.clear();
            m_ready.reserve(m_entries.size());
            for (auto &entry : m_entries)
                m_ready.push_back(std::move(entry.deleter));
            m_entries.clear();
            for (auto &deleter : m_ready) {
                deleter();
                ++m_retired;
            }
        }
        m_ready.clear();
    }

    [[nodiscard]] size_t PendingCount() const noexcept
    {
        return m_entries.size();
    }

    [[nodiscard]] Stats GetStats() const noexcept
    {
        return {m_collectCalls, m_pushed, m_retired, m_entries.size(), m_highWatermark};
    }

  private:
    struct Entry
    {
        rhi::SubmissionSerial retirementSerial = rhi::InvalidSubmissionSerial;
        std::function<void()> deleter;
    };

    std::vector<Entry> m_entries;
    std::vector<std::function<void()>> m_ready;
    SerialSource m_serialSource;
    uint64_t m_collectCalls = 0;
    uint64_t m_pushed = 0;
    uint64_t m_retired = 0;
    size_t m_highWatermark = 0;
};

} // namespace infernux
