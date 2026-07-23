#pragma once

#include "ParticleGpuRuntime.h"

#include <array>
#include <cstdint>
#include <string_view>
#include <vector>

namespace infernux::particle
{

struct GpuParticleRibbonShaderSources
{
    [[nodiscard]] static std::string_view Reset() noexcept;
    [[nodiscard]] static std::string_view Initialize() noexcept;
    [[nodiscard]] static std::string_view Histogram() noexcept;
    [[nodiscard]] static std::string_view Scan() noexcept;
    [[nodiscard]] static std::string_view Scatter() noexcept;
};

struct GpuParticleRibbonProgram
{
    ShaderBytecode reset;
    ShaderBytecode initialize;
    ShaderBytecode histogram;
    ShaderBytecode scan;
    ShaderBytecode scatter;

    [[nodiscard]] bool IsValid() const noexcept;
};

struct GpuParticleRibbonProgramStorage
{
    std::array<std::vector<uint32_t>, 5> shaders;

    [[nodiscard]] bool Assign(const GpuParticleRibbonProgram &program);
    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] GpuParticleRibbonProgram View() const noexcept;
};

struct GpuParticleRibbonDesc
{
    uint32_t capacity = 0;
    rhi::BufferHandle instances;
    rhi::BufferHandle sourceIndices;
    rhi::BufferHandle sourceIndirectArguments;
    GpuParticleRibbonProgram program;
};

struct GpuParticleRibbonConstants
{
    uint32_t capacity = 0;
    uint32_t blockCount = 0;
    uint32_t keyField = 0;
    uint32_t digitShift = 0;
};

/// Builds a camera-independent, GPU-resident ribbon topology. The source
/// particle stream is stably ordered by the complete 96-bit tuple
/// (strip_id, order, source_id) using three full-width 32-bit radix keys.
class ParticleGpuRibbonTopology
{
  public:
    static constexpr uint32_t WorkgroupSize = 256;
    static constexpr uint32_t Radix = 16;
    static constexpr uint32_t PassesPerKey = 8;
    static constexpr uint32_t KeyCount = 3;
    static constexpr uint32_t PassCount = PassesPerKey * KeyCount;

    ParticleGpuRibbonTopology() = default;
    ~ParticleGpuRibbonTopology();

    ParticleGpuRibbonTopology(const ParticleGpuRibbonTopology &) = delete;
    ParticleGpuRibbonTopology &operator=(const ParticleGpuRibbonTopology &) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, const GpuParticleRibbonDesc &desc);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] uint32_t Capacity() const noexcept
    {
        return m_capacity;
    }
    [[nodiscard]] uint32_t BlockCount() const noexcept
    {
        return m_blockCount;
    }
    [[nodiscard]] rhi::BufferHandle InstanceBuffer() const noexcept
    {
        return m_instances;
    }
    [[nodiscard]] rhi::BufferHandle SourceIndexBuffer() const noexcept
    {
        return m_sourceIndices;
    }
    [[nodiscard]] rhi::BufferHandle SourceIndirectBuffer() const noexcept
    {
        return m_sourceIndirectArguments;
    }
    [[nodiscard]] rhi::BufferHandle SortedIndexBuffer() const noexcept
    {
        return m_indices[0];
    }
    [[nodiscard]] rhi::BufferHandle IndexBuffer(uint32_t pingPong) const noexcept
    {
        return pingPong < m_indices.size() ? m_indices[pingPong] : rhi::BufferHandle{};
    }
    [[nodiscard]] rhi::BufferHandle DrawIndirectBuffer() const noexcept
    {
        return m_drawIndirectArguments;
    }
    [[nodiscard]] rhi::BufferHandle DispatchBuffer() const noexcept
    {
        return m_dispatchArguments;
    }
    [[nodiscard]] rhi::BufferHandle HistogramBuffer() const noexcept
    {
        return m_histograms;
    }
    [[nodiscard]] rhi::BufferHandle BlockOffsetBuffer() const noexcept
    {
        return m_blockOffsets;
    }
    [[nodiscard]] rhi::BufferHandle GlobalOffsetBuffer() const noexcept
    {
        return m_globalOffsets;
    }

    void RecordReset(const rhi::ComputeCommandEncoder &encoder) const;
    void RecordInitialize(const rhi::ComputeCommandEncoder &encoder) const;
    void RecordHistogram(const rhi::ComputeCommandEncoder &encoder, uint32_t passIndex) const;
    void RecordScan(const rhi::ComputeCommandEncoder &encoder, uint32_t passIndex) const;
    void RecordScatter(const rhi::ComputeCommandEncoder &encoder, uint32_t passIndex) const;

  private:
    [[nodiscard]] GpuParticleRibbonConstants Constants(uint32_t passIndex = 0) const noexcept;
    void RecordDirect(const rhi::ComputeCommandEncoder &encoder, rhi::ComputePipelineHandle pipeline,
                      rhi::BindGroupHandle group, const GpuParticleRibbonConstants &constants, uint32_t groups) const;
    void RecordIndirect(const rhi::ComputeCommandEncoder &encoder, rhi::ComputePipelineHandle pipeline,
                        rhi::BindGroupHandle group, const GpuParticleRibbonConstants &constants) const;

    rhi::Device *m_device = nullptr;
    uint32_t m_capacity = 0;
    uint32_t m_blockCount = 0;
    rhi::BufferHandle m_instances;
    rhi::BufferHandle m_sourceIndices;
    rhi::BufferHandle m_sourceIndirectArguments;
    std::array<rhi::BufferHandle, 2> m_indices{};
    rhi::BufferHandle m_drawIndirectArguments;
    rhi::BufferHandle m_dispatchArguments;
    rhi::BufferHandle m_histograms;
    rhi::BufferHandle m_blockOffsets;
    rhi::BufferHandle m_globalOffsets;
    rhi::BindingLayoutHandle m_layout;
    std::array<rhi::BindGroupHandle, 2> m_groups{};
    rhi::ComputePipelineHandle m_resetPipeline;
    rhi::ComputePipelineHandle m_initializePipeline;
    rhi::ComputePipelineHandle m_histogramPipeline;
    rhi::ComputePipelineHandle m_scanPipeline;
    rhi::ComputePipelineHandle m_scatterPipeline;
};

static_assert(sizeof(GpuParticleRibbonConstants) == 16);

} // namespace infernux::particle
