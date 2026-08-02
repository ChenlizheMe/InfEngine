#pragma once

#include "ParticleGpuRuntime.h"

#include <cstdint>
#include <string_view>
#include <vector>

namespace infernux::particle
{

struct GpuParticleMigrationProgram
{
    ShaderBytecode reset;
    ShaderBytecode migrate;

    [[nodiscard]] bool IsValid() const noexcept;
};

struct GpuParticleMigrationProgramStorage
{
    std::vector<uint32_t> reset;
    std::vector<uint32_t> migrate;

    [[nodiscard]] bool Assign(const GpuParticleMigrationProgram &program);
    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] GpuParticleMigrationProgram View() const noexcept;
};

struct GpuParticleMigrationShaderSources
{
    [[nodiscard]] static std::string_view Reset() noexcept;
    [[nodiscard]] static std::string_view Migrate() noexcept;
};

struct alignas(16) GpuParticleMigrationRange
{
    uint32_t sourceOffsetWords = 0;
    uint32_t destinationOffsetWords = 0;
    uint32_t wordCount = 0;
    uint32_t reserved = 0;
};

struct GpuParticleMigrationDesc
{
    uint32_t sourceCapacity = 0;
    uint32_t destinationCapacity = 0;
    uint32_t sourceStride = 0;
    uint32_t destinationStride = 0;
    rhi::BufferHandle sourceStates;
    rhi::BufferHandle sourceCounters;
    uint64_t sourceCounterByteSize = 0;
    rhi::BufferHandle destinationStates;
    rhi::BufferHandle destinationFreeList;
    rhi::BufferHandle destinationCounters;
    std::vector<GpuParticleMigrationRange> copyRanges;
    std::vector<uint32_t> defaultStateWords;
    GpuParticleMigrationProgram program;
};

struct alignas(16) GpuParticleMigrationConstants
{
    uint32_t sourceCapacity = 0;
    uint32_t destinationCapacity = 0;
    uint32_t sourceStrideWords = 0;
    uint32_t destinationStrideWords = 0;
    uint32_t copyRangeCount = 0;
    uint32_t invocationCount = 0;
    uint32_t reserved0 = 0;
    uint32_t reserved1 = 0;
};

/// One-shot GPU state migration used when a saved emitter changes capacity or
/// std430 attribute layout. All particle payload remains GPU resident.
class ParticleGpuMigrator
{
  public:
    static constexpr uint32_t WorkgroupSize = 256;

    ParticleGpuMigrator() = default;
    ~ParticleGpuMigrator();

    ParticleGpuMigrator(const ParticleGpuMigrator &) = delete;
    ParticleGpuMigrator &operator=(const ParticleGpuMigrator &) = delete;
    ParticleGpuMigrator(ParticleGpuMigrator &&) = delete;
    ParticleGpuMigrator &operator=(ParticleGpuMigrator &&) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, const GpuParticleMigrationDesc &desc);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] bool WasRecorded() const noexcept
    {
        return m_recorded;
    }

    void RecordReset(const rhi::ComputeCommandEncoder &encoder);
    void RecordMigrate(const rhi::ComputeCommandEncoder &encoder);

    [[nodiscard]] rhi::BufferHandle SourceStateBuffer() const noexcept
    {
        return m_sourceStates;
    }
    [[nodiscard]] rhi::BufferHandle SourceCounterBuffer() const noexcept
    {
        return m_sourceCounters;
    }
    [[nodiscard]] uint64_t SourceCounterBufferByteSize() const noexcept
    {
        return m_sourceCounterByteSize;
    }
    [[nodiscard]] rhi::BufferHandle DestinationStateBuffer() const noexcept
    {
        return m_destinationStates;
    }
    [[nodiscard]] rhi::BufferHandle DestinationFreeListBuffer() const noexcept
    {
        return m_destinationFreeList;
    }
    [[nodiscard]] rhi::BufferHandle DestinationCounterBuffer() const noexcept
    {
        return m_destinationCounters;
    }
    [[nodiscard]] rhi::BufferHandle CopyRangeBuffer() const noexcept
    {
        return m_copyRanges;
    }
    [[nodiscard]] rhi::BufferHandle DefaultStateBuffer() const noexcept
    {
        return m_defaultState;
    }
    [[nodiscard]] const GpuParticleMigrationConstants &Constants() const noexcept
    {
        return m_constants;
    }

  private:
    rhi::Device *m_device = nullptr;
    rhi::BufferHandle m_sourceStates;
    rhi::BufferHandle m_sourceCounters;
    uint64_t m_sourceCounterByteSize = 0;
    rhi::BufferHandle m_destinationStates;
    rhi::BufferHandle m_destinationFreeList;
    rhi::BufferHandle m_destinationCounters;
    rhi::BufferHandle m_copyRanges;
    rhi::BufferHandle m_defaultState;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    rhi::ComputePipelineHandle m_resetPipeline;
    rhi::ComputePipelineHandle m_migratePipeline;
    GpuParticleMigrationConstants m_constants{};
    bool m_resetRecorded = false;
    bool m_recorded = false;
};

static_assert(sizeof(GpuParticleMigrationRange) == 16);
static_assert(sizeof(GpuParticleMigrationConstants) == 32);

} // namespace infernux::particle
