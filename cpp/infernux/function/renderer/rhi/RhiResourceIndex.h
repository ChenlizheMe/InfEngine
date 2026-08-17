#pragma once

#include "RhiSubmission.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <mutex>
#include <vector>

namespace infernux::rhi
{

/// CPU-visible identity for one shader resource table slot. Shaders consume
/// only `index`; `generation` prevents stale CPU-side material state from
/// resolving to a different resource after the slot is recycled.
struct ResourceIndex final
{
    static constexpr uint32_t InvalidIndex = std::numeric_limits<uint32_t>::max();
    static constexpr uint32_t FallbackIndex = 0;

    uint32_t index = InvalidIndex;
    uint32_t generation = 0;

    [[nodiscard]] constexpr bool IsValid() const noexcept
    {
        return index != InvalidIndex && generation != 0;
    }

    [[nodiscard]] constexpr bool IsFallback() const noexcept
    {
        return index == FallbackIndex && generation != 0;
    }

    friend constexpr bool operator==(ResourceIndex lhs, ResourceIndex rhs) noexcept
    {
        return lhs.index == rhs.index && lhs.generation == rhs.generation;
    }

    friend constexpr bool operator!=(ResourceIndex lhs, ResourceIndex rhs) noexcept
    {
        return !(lhs == rhs);
    }
};

/// Backend-neutral slot lifecycle for bindless resource tables.
///
/// Slot zero is permanently reserved for a backend-provided fallback resource.
/// Retiring a slot invalidates its CPU handle immediately, while reuse is held
/// until the exact GPU submission serial that can still reference it completes.
class ResourceIndexAllocator final
{
  public:
    struct Stats final
    {
        uint32_t capacity = 0;
        uint32_t live = 0;
        uint32_t pendingRetirement = 0;
        uint32_t available = 0;
        uint32_t highWatermark = 0;
        uint64_t allocations = 0;
        uint64_t allocationFailures = 0;
        uint64_t retirements = 0;
        uint64_t collections = 0;
    };

    explicit ResourceIndexAllocator(uint32_t capacity) : m_slots((std::max)(capacity, 1u))
    {
        m_slots[ResourceIndex::FallbackIndex].generation = 1;
        m_slots[ResourceIndex::FallbackIndex].live = true;
        m_free.reserve(m_slots.size() - 1);
        // Pop the lowest available index first so descriptor-table contents and
        // diagnostics stay deterministic across runs.
        for (uint32_t index = static_cast<uint32_t>(m_slots.size()); index-- > 1;)
            m_free.push_back(index);
    }

    ResourceIndexAllocator(const ResourceIndexAllocator &) = delete;
    ResourceIndexAllocator &operator=(const ResourceIndexAllocator &) = delete;
    ResourceIndexAllocator(ResourceIndexAllocator &&) = delete;
    ResourceIndexAllocator &operator=(ResourceIndexAllocator &&) = delete;

    [[nodiscard]] ResourceIndex Fallback() const noexcept
    {
        return {ResourceIndex::FallbackIndex, 1};
    }

    [[nodiscard]] ResourceIndex Allocate() noexcept
    {
        std::lock_guard lock(m_mutex);
        if (m_free.empty()) {
            ++m_allocationFailures;
            return {};
        }

        const uint32_t index = m_free.back();
        m_free.pop_back();
        Slot &slot = m_slots[index];
        slot.live = true;
        slot.lastUse = InvalidSubmissionSerial;
        slot.retireAfter = InvalidSubmissionSerial;
        ++m_live;
        ++m_allocations;
        m_highWatermark = (std::max)(m_highWatermark, m_live);
        return {index, slot.generation};
    }

    /// Releases an allocation that has not been published to a command
    /// buffer. This is intentionally separate from RetireAfter: a failed
    /// publication never became GPU-visible and must not require a fake
    /// submission serial just to return the slot to the allocator.
    [[nodiscard]] bool Cancel(ResourceIndex resource) noexcept
    {
        if (!resource.IsValid() || resource.index == ResourceIndex::FallbackIndex)
            return false;

        std::lock_guard lock(m_mutex);
        if (!MatchesLiveSlot(resource))
            return false;

        Slot &slot = m_slots[resource.index];
        slot.live = false;
        slot.lastUse = InvalidSubmissionSerial;
        slot.retireAfter = InvalidSubmissionSerial;
        slot.generation = NextGeneration(slot.generation);
        m_free.push_back(resource.index);
        --m_live;
        return true;
    }

    /// Records a submission that may read this shader slot. Calls are cheap
    /// enough to issue when a table is bound; the maximum serial wins across
    /// Scene, Game, preview, and background submissions.
    [[nodiscard]] bool MarkUsed(ResourceIndex resource, SubmissionSerial serial) noexcept
    {
        if (!resource.IsValid() || resource.index == ResourceIndex::FallbackIndex || serial == InvalidSubmissionSerial)
            return false;

        std::lock_guard lock(m_mutex);
        if (!MatchesLiveSlot(resource))
            return false;
        Slot &slot = m_slots[resource.index];
        slot.lastUse = (std::max)(slot.lastUse, serial);
        return true;
    }

    /// Immediately invalidates `resource`, but holds its shader slot until the
    /// exact submission has completed. Serial zero is rejected fail-closed.
    [[nodiscard]] bool RetireAfter(ResourceIndex resource, SubmissionSerial serial) noexcept
    {
        if (!resource.IsValid() || resource.index == ResourceIndex::FallbackIndex || serial == InvalidSubmissionSerial)
            return false;

        std::lock_guard lock(m_mutex);
        if (!MatchesLiveSlot(resource))
            return false;

        Slot &slot = m_slots[resource.index];
        slot.live = false;
        slot.retireAfter = (std::max)(slot.lastUse, serial);
        --m_live;
        ++m_pendingRetirement;
        ++m_retirements;
        return true;
    }

    /// Reclaims every retired slot whose last possible GPU use has completed.
    [[nodiscard]] size_t Collect(SubmissionSerial completedSerial) noexcept
    {
        return CollectIndices(completedSerial, nullptr);
    }

    /// Same collection operation with optional reclaimed-slot reporting for
    /// descriptor tables that must release per-slot ownership after GPU use.
    [[nodiscard]] size_t CollectIndices(SubmissionSerial completedSerial, std::vector<uint32_t> *reclaimed) noexcept
    {
        if (completedSerial == InvalidSubmissionSerial)
            return 0;

        std::lock_guard lock(m_mutex);
        size_t collected = 0;
        for (uint32_t index = 1; index < m_slots.size(); ++index) {
            Slot &slot = m_slots[index];
            if (slot.live || slot.retireAfter == InvalidSubmissionSerial || slot.retireAfter > completedSerial)
                continue;

            slot.retireAfter = InvalidSubmissionSerial;
            slot.lastUse = InvalidSubmissionSerial;
            slot.generation = NextGeneration(slot.generation);
            m_free.push_back(index);
            if (reclaimed)
                reclaimed->push_back(index);
            --m_pendingRetirement;
            ++collected;
        }
        m_collections += collected;
        return collected;
    }

    [[nodiscard]] bool IsLive(ResourceIndex resource) const noexcept
    {
        std::lock_guard lock(m_mutex);
        return MatchesLiveSlot(resource);
    }

    /// Returns the shader-visible slot, or the permanent fallback slot when
    /// the CPU handle is stale, retired, or otherwise invalid.
    [[nodiscard]] uint32_t ResolveShaderIndex(ResourceIndex resource) const noexcept
    {
        std::lock_guard lock(m_mutex);
        return MatchesLiveSlot(resource) ? resource.index : ResourceIndex::FallbackIndex;
    }

    [[nodiscard]] Stats GetStats() const noexcept
    {
        std::lock_guard lock(m_mutex);
        return {static_cast<uint32_t>(m_slots.size()),
                m_live,
                m_pendingRetirement,
                static_cast<uint32_t>(m_free.size()),
                m_highWatermark,
                m_allocations,
                m_allocationFailures,
                m_retirements,
                m_collections};
    }

  private:
    struct Slot final
    {
        uint32_t generation = 1;
        SubmissionSerial lastUse = InvalidSubmissionSerial;
        SubmissionSerial retireAfter = InvalidSubmissionSerial;
        bool live = false;
    };

    [[nodiscard]] bool MatchesLiveSlot(ResourceIndex resource) const noexcept
    {
        return resource.IsValid() && resource.index < m_slots.size() && m_slots[resource.index].live &&
               m_slots[resource.index].generation == resource.generation;
    }

    [[nodiscard]] static constexpr uint32_t NextGeneration(uint32_t generation) noexcept
    {
        ++generation;
        return generation == 0 ? 1 : generation;
    }

    mutable std::mutex m_mutex;
    std::vector<Slot> m_slots;
    std::vector<uint32_t> m_free;
    uint32_t m_live = 0;
    uint32_t m_pendingRetirement = 0;
    uint32_t m_highWatermark = 0;
    uint64_t m_allocations = 0;
    uint64_t m_allocationFailures = 0;
    uint64_t m_retirements = 0;
    uint64_t m_collections = 0;
};

static_assert(sizeof(ResourceIndex) == 8);

} // namespace infernux::rhi
