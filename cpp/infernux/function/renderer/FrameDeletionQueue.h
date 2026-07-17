/**
 * @file FrameDeletionQueue.h
 * @brief Per-frame deferred deletion queue for Vulkan resources
 *
 * Vulkan resources (buffers, images, etc.) may still be referenced by
 * in-flight command buffers when the CPU side decides to destroy them.
 * This queue defers actual destruction until `maxFramesInFlight` frames
 * have elapsed, guaranteeing that every in-flight command buffer has
 * finished executing by the time the resources are freed.
 *
 * Usage:
 *   // At init:
 *   queue.Initialize(maxFramesInFlight);
 *
 *   // When a resource should be deleted:
 *   queue.Push([buffer = std::move(myBuffer)]() mutable { buffer.reset(); });
 *
 *   // Once per frame, AFTER fence wait:
 *   queue.Tick();
 *
 *   // At shutdown:
 *   queue.FlushAll();
 */

#pragma once

#include <cstdint>
#include <functional>
#include <utility>
#include <vector>

namespace infernux
{

class FrameDeletionQueue
{
  public:
    struct Stats
    {
        uint64_t completedFenceTicks = 0;
        uint64_t pushed = 0;
        uint64_t retired = 0;
        size_t pending = 0;
        size_t highWatermark = 0;
    };

    FrameDeletionQueue() = default;
    ~FrameDeletionQueue()
    {
        FlushAll();
    }

    // Non-copyable, movable
    FrameDeletionQueue(const FrameDeletionQueue &) = delete;
    FrameDeletionQueue &operator=(const FrameDeletionQueue &) = delete;
    FrameDeletionQueue(FrameDeletionQueue &&other) noexcept
        : m_entries(std::move(other.m_entries)), m_ready(std::move(other.m_ready)),
          m_frameCounter(std::exchange(other.m_frameCounter, 0)),
          m_maxFramesInFlight(std::exchange(other.m_maxFramesInFlight, 2)), m_pushed(std::exchange(other.m_pushed, 0)),
          m_retired(std::exchange(other.m_retired, 0)), m_highWatermark(std::exchange(other.m_highWatermark, 0))
    {
    }

    FrameDeletionQueue &operator=(FrameDeletionQueue &&other) noexcept
    {
        if (this != &other) {
            FlushAll();
            m_entries = std::move(other.m_entries);
            m_ready = std::move(other.m_ready);
            m_frameCounter = std::exchange(other.m_frameCounter, 0);
            m_maxFramesInFlight = std::exchange(other.m_maxFramesInFlight, 2);
            m_pushed = std::exchange(other.m_pushed, 0);
            m_retired = std::exchange(other.m_retired, 0);
            m_highWatermark = std::exchange(other.m_highWatermark, 0);
        }
        return *this;
    }

    /// @brief Initialize with the number of frames that may be in-flight
    void Initialize(uint32_t maxFramesInFlight)
    {
        m_maxFramesInFlight = maxFramesInFlight > 0 ? maxFramesInFlight : 1;
    }

    /// @brief Queue a cleanup lambda for deferred execution.
    ///
    /// The lambda will be invoked after at least @c maxFramesInFlight
    /// calls to @c Tick(), ensuring all in-flight command buffers
    /// referencing the resource have completed.
    void Push(std::function<void()> deleter)
    {
        if (!deleter) {
            return;
        }
        m_entries.push_back({m_frameCounter, std::move(deleter)});
        ++m_pushed;
        if (m_entries.size() > m_highWatermark) {
            m_highWatermark = m_entries.size();
        }
    }

    /// @brief Call exactly once per frame, AFTER the per-frame fence wait.
    ///
    /// Flushes entries whose frame age >= maxFramesInFlight, then
    /// increments the internal frame counter.
    void Tick()
    {
        m_ready.clear();
        size_t writeIdx = 0;
        for (size_t i = 0; i < m_entries.size(); ++i) {
            if (m_frameCounter - m_entries[i].frameNumber >= m_maxFramesInFlight) {
                m_ready.push_back(std::move(m_entries[i].deleter));
            } else {
                if (writeIdx != i) {
                    m_entries[writeIdx] = std::move(m_entries[i]);
                }
                ++writeIdx;
            }
        }
        m_entries.resize(writeIdx);

        // Invoke after compacting m_entries so a deleter may safely enqueue
        // another retirement without invalidating the scan above.
        for (auto &deleter : m_ready) {
            deleter();
            ++m_retired;
        }
        m_ready.clear();
        ++m_frameCounter;
    }

    /// @brief Immediately flush ALL remaining entries (use at shutdown).
    void FlushAll()
    {
        while (!m_entries.empty()) {
            m_ready.clear();
            m_ready.reserve(m_entries.size());
            for (auto &entry : m_entries) {
                m_ready.push_back(std::move(entry.deleter));
            }
            m_entries.clear();
            for (auto &deleter : m_ready) {
                deleter();
                ++m_retired;
            }
        }
        m_ready.clear();
    }

    /// @brief Get the number of pending entries
    [[nodiscard]] size_t PendingCount() const
    {
        return m_entries.size();
    }

    [[nodiscard]] Stats GetStats() const noexcept
    {
        return {m_frameCounter, m_pushed, m_retired, m_entries.size(), m_highWatermark};
    }

  private:
    struct Entry
    {
        uint64_t frameNumber = 0;
        std::function<void()> deleter;
    };

    std::vector<Entry> m_entries;
    std::vector<std::function<void()>> m_ready;
    uint64_t m_frameCounter = 0;
    uint32_t m_maxFramesInFlight = 2;
    uint64_t m_pushed = 0;
    uint64_t m_retired = 0;
    size_t m_highWatermark = 0;
};

} // namespace infernux
