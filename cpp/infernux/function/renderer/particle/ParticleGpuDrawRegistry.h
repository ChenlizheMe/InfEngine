#pragma once

#include "ParticleGpuCuller.h"
#include "ParticleGpuOutputRenderer.h"
#include "ParticleOutputSemantics.h"

#include <cstdint>
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
    [[nodiscard]] bool Set(GpuParticleDrawEntry entry);
    [[nodiscard]] bool Replace(std::vector<GpuParticleDrawEntry> entries);
    [[nodiscard]] bool Remove(uint64_t id);
    void Clear();

    [[nodiscard]] std::vector<GpuParticleDrawEntry> Snapshot(int32_t queueMin, int32_t queueMax) const;
    [[nodiscard]] uint64_t Revision() const;
    [[nodiscard]] size_t Size() const;

  private:
    mutable std::mutex m_mutex;
    std::vector<GpuParticleDrawEntry> m_entries;
    uint64_t m_revision = 1;
};

} // namespace infernux::particle
