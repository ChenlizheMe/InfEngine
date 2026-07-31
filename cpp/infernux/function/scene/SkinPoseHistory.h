#pragma once

#include <glm/glm.hpp>

#include <atomic>
#include <cstdint>
#include <memory>
#include <utility>
#include <vector>

namespace infernux
{

class SkinPoseHistory
{
  public:
    using Palette = std::vector<glm::mat4>;
    using PalettePtr = std::shared_ptr<const Palette>;

    struct Snapshot
    {
        PalettePtr current;
        PalettePtr previous;
        uint64_t revision = 0;

        [[nodiscard]] bool IsValid() const noexcept
        {
            return current && previous && !current->empty() && current->size() == previous->size();
        }
    };

    using SnapshotPtr = std::shared_ptr<const Snapshot>;

    SkinPoseHistory() : m_snapshot(std::make_shared<const Snapshot>())
    {
    }

    void Publish(PalettePtr palette, bool resetHistory)
    {
        if (!palette || palette->empty()) {
            Reset();
            return;
        }

        const SnapshotPtr published = Acquire();
        auto next = std::make_shared<Snapshot>();
        next->current = std::move(palette);
        next->previous =
            resetHistory || !published || !published->current || published->current->size() != next->current->size()
                ? next->current
                : published->current;
        next->revision = m_nextRevision.fetch_add(1, std::memory_order_relaxed);
        std::atomic_store_explicit(&m_snapshot, std::const_pointer_cast<const Snapshot>(next),
                                   std::memory_order_release);
    }

    void Reset() noexcept
    {
        auto next = std::make_shared<Snapshot>();
        next->revision = m_nextRevision.fetch_add(1, std::memory_order_relaxed);
        std::atomic_store_explicit(&m_snapshot, std::const_pointer_cast<const Snapshot>(next),
                                   std::memory_order_release);
    }

    [[nodiscard]] SnapshotPtr Acquire() const noexcept
    {
        return std::atomic_load_explicit(&m_snapshot, std::memory_order_acquire);
    }

    [[nodiscard]] PalettePtr Current() const noexcept
    {
        const SnapshotPtr snapshot = Acquire();
        return snapshot ? snapshot->current : nullptr;
    }

    [[nodiscard]] PalettePtr Previous() const noexcept
    {
        const SnapshotPtr snapshot = Acquire();
        return snapshot ? snapshot->previous : nullptr;
    }

  private:
    mutable SnapshotPtr m_snapshot;
    std::atomic<uint64_t> m_nextRevision{1};
};

} // namespace infernux
