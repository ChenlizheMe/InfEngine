#pragma once

#include "ParticleGpuCuller.h"
#include "ParticleGpuOutputRenderer.h"
#include "ParticleOutputSemantics.h"

#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace infernux::particle
{

struct GpuParticleSortProgramStorage;
struct GpuParticleCullProgramStorage;

struct GpuParticleDrawEntry
{
    uint64_t id = 0;
    uint64_t emitterId = 0;
    uint64_t graphInstanceId = 0;
    uint32_t emitterIndex = 0;
    std::string outputStableId;
    uint64_t ownerObjectId = 0;
    uint32_t ownerLayerMask = 1u;
    uint32_t capacity = 0;
    rhi::BufferHandle instances;
    rhi::BufferHandle visibility;
    rhi::BufferHandle renderIndices;
    rhi::BufferHandle indirectArguments;
    rhi::BufferHandle bounds;
    rhi::BufferHandle simulationControl;
    std::shared_ptr<ParticleGpuOutputRenderer> renderer;
    std::shared_ptr<const GpuParticleCullProgramStorage> cullProgram;
    std::shared_ptr<const GpuParticleSortProgramStorage> sortProgram;
    GpuParticleCullMode cullMode = GpuParticleCullMode::Instances;
    ParticleOutputSemantics semantics;
};

class ParticleGpuDrawRegistry
{
  public:
    ParticleGpuDrawRegistry();

    [[nodiscard]] bool Set(GpuParticleDrawEntry entry);
    [[nodiscard]] bool Replace(std::vector<GpuParticleDrawEntry> entries);
    [[nodiscard]] bool Remove(uint64_t id);
    void Clear();

    // The shared handle keeps the immutable result alive without copying the
    // entry array.
    using SnapshotEntries = std::vector<GpuParticleDrawEntry>;
    using SnapshotHandle = std::shared_ptr<const SnapshotEntries>;

    [[nodiscard]] SnapshotHandle SnapshotShared(int32_t queueMin, int32_t queueMax) const;
    [[nodiscard]] uint64_t Revision() const;
    [[nodiscard]] size_t Size() const;

  private:
    struct RegistryState
    {
        uint64_t revision = 1;
        std::vector<GpuParticleDrawEntry> entries;
    };

    struct SnapshotKey
    {
        int32_t queueMin = 0;
        int32_t queueMax = 0;

        [[nodiscard]] bool operator<(const SnapshotKey &other) const noexcept
        {
            return queueMin != other.queueMin ? queueMin < other.queueMin : queueMax < other.queueMax;
        }
    };

    struct SnapshotCacheEntry
    {
        uint64_t revision = 0;
        std::vector<int32_t> queueValues;
        SnapshotHandle entries;
    };

    struct SnapshotCache
    {
        std::map<SnapshotKey, SnapshotCacheEntry> entries;
    };

    mutable std::mutex m_mutex;
    // Registry mutations publish a complete state. Readers can retain the
    // old state while a writer prepares and publishes a replacement.
    mutable std::shared_ptr<const RegistryState> m_state;
    mutable std::shared_ptr<const SnapshotCache> m_snapshotCache;
};

} // namespace infernux::particle
