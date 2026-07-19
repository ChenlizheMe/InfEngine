#pragma once

#include "ParticleGpuBillboardRenderer.h"
#include "ParticleOutputSemantics.h"

#include <cstdint>
#include <memory>
#include <mutex>
#include <vector>

namespace infernux::particle
{

struct GpuParticleDrawEntry
{
    uint64_t id = 0;
    uint32_t capacity = 0;
    rhi::BufferHandle instances;
    rhi::BufferHandle renderIndices;
    rhi::BufferHandle indirectArguments;
    std::shared_ptr<ParticleGpuBillboardRenderer> renderer;
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
