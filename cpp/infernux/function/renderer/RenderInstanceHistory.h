#pragma once

#include <function/renderer/RenderIdentity.h>

#include <glm/glm.hpp>

#include <cstddef>
#include <cstdint>
#include <unordered_map>

namespace infernux
{

static constexpr uint32_t kGPUInstanceAuxFlagValidHistory = 1u;

/// Optional per-instance data consumed by picking and motion passes. Ordinary
/// forward/shadow draws keep using the compact model-matrix-only stream.
struct alignas(16) GPUInstanceAuxData
{
    glm::mat4 previousModel{1.0f};
    glm::uvec2 objectId{0u};
    uint32_t flags = 0;
    uint32_t layerMask = 1u;
};

static_assert(alignof(GPUInstanceAuxData) == 16, "GPU instance auxiliary data must remain std430-compatible");
static_assert(sizeof(GPUInstanceAuxData) == 80, "GPU instance auxiliary data layout changed");
static_assert(offsetof(GPUInstanceAuxData, objectId) == 64, "GPU object identity must follow the previous model");

[[nodiscard]] constexpr glm::uvec2 PackGPUObjectId(uint64_t objectId) noexcept
{
    return {static_cast<uint32_t>(objectId), static_cast<uint32_t>(objectId >> 32u)};
}

[[nodiscard]] constexpr uint64_t UnpackGPUObjectId(const glm::uvec2 &objectId) noexcept
{
    return static_cast<uint64_t>(objectId.x) | (static_cast<uint64_t>(objectId.y) << 32u);
}

/// Tracks one previous transform per stable render primitive. A logical frame
/// may query the same primitive from several cameras and passes without
/// advancing its history more than once.
class RenderInstanceHistory
{
  public:
    void BeginFrame(uint64_t frameSerial)
    {
        if (frameSerial == m_frameSerial)
            return;

        if (m_frameSerial != 0 && frameSerial < m_frameSerial)
            Clear();

        m_frameSerial = frameSerial;
        if (frameSerial != 0 && (m_lastPruneFrame == 0 || frameSerial - m_lastPruneFrame >= kPruneInterval)) {
            Prune(frameSerial);
            m_lastPruneFrame = frameSerial;
        }
    }

    [[nodiscard]] GPUInstanceAuxData Resolve(const RenderDrawIdentity &identity, const glm::mat4 &currentModel,
                                             uint64_t objectId, uint32_t layerMask)
    {
        GPUInstanceAuxData result;
        result.previousModel = currentModel;
        result.objectId = PackGPUObjectId(objectId);
        result.layerMask = layerMask;

        if (m_frameSerial == 0 || !identity.IsValid())
            return result;

        auto [it, inserted] = m_entries.try_emplace(identity);
        Entry &entry = it->second;
        if (inserted) {
            entry.currentModel = currentModel;
            entry.previousForFrame = currentModel;
            entry.lastObservedFrame = m_frameSerial;
            return result;
        }

        if (entry.lastObservedFrame == m_frameSerial) {
            result.previousModel = entry.previousForFrame;
            result.flags = entry.hasContinuousHistory ? kGPUInstanceAuxFlagValidHistory : 0u;
            return result;
        }

        entry.hasContinuousHistory = entry.lastObservedFrame + 1u == m_frameSerial;
        entry.previousForFrame = entry.hasContinuousHistory ? entry.currentModel : currentModel;
        entry.currentModel = currentModel;
        entry.lastObservedFrame = m_frameSerial;

        result.previousModel = entry.previousForFrame;
        result.flags = entry.hasContinuousHistory ? kGPUInstanceAuxFlagValidHistory : 0u;
        return result;
    }

    void Clear() noexcept
    {
        m_entries.clear();
        m_frameSerial = 0;
        m_lastPruneFrame = 0;
    }

    [[nodiscard]] size_t Size() const noexcept
    {
        return m_entries.size();
    }

  private:
    struct Entry
    {
        glm::mat4 currentModel{1.0f};
        glm::mat4 previousForFrame{1.0f};
        uint64_t lastObservedFrame = 0;
        bool hasContinuousHistory = false;
    };

    void Prune(uint64_t frameSerial)
    {
        for (auto it = m_entries.begin(); it != m_entries.end();) {
            if (it->second.lastObservedFrame + kRetentionFrames < frameSerial)
                it = m_entries.erase(it);
            else
                ++it;
        }
    }

    static constexpr uint64_t kRetentionFrames = 120;
    static constexpr uint64_t kPruneInterval = 60;

    std::unordered_map<RenderDrawIdentity, Entry, RenderDrawIdentityHash> m_entries;
    uint64_t m_frameSerial = 0;
    uint64_t m_lastPruneFrame = 0;
};

} // namespace infernux
