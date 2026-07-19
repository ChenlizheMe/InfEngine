#pragma once

#include "ParticleGpuRuntime.h"
#include "ParticleOutputSemantics.h"

#include <array>
#include <cstdint>
#include <string_view>
#include <vector>

namespace infernux::particle
{

struct GpuParticleSortProgram
{
    ShaderBytecode generate;
    ShaderBytecode histogram;
    ShaderBytecode scan;
    ShaderBytecode scatter;

    [[nodiscard]] bool IsValid() const noexcept;
};

/// Shared, owning representation of the four sort kernels. View render graphs
/// keep only this immutable program while allocating independent workspaces.
struct GpuParticleSortProgramStorage
{
    std::array<std::vector<uint32_t>, 4> shaders;

    [[nodiscard]] bool Assign(const GpuParticleSortProgram &program);
    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] GpuParticleSortProgram View() const noexcept;
};

struct GpuParticleSortShaderSources
{
    [[nodiscard]] static std::string_view Generate() noexcept;
    [[nodiscard]] static std::string_view Histogram() noexcept;
    [[nodiscard]] static std::string_view Scan() noexcept;
    [[nodiscard]] static std::string_view Scatter() noexcept;
};

struct GpuParticleSorterDesc
{
    uint32_t capacity = 0;
    rhi::BufferHandle instances;
    rhi::BufferHandle indirectArguments;
    rhi::BufferHandle sourceIndices;
    rhi::BufferHandle dispatchArguments;
    GpuParticleSortProgram program;
};

struct alignas(16) GpuParticleSortConstants
{
    std::array<float, 16> view{};
    uint32_t capacity = 0;
    uint32_t blockCount = 0;
    uint32_t digitShift = 0;
    uint32_t descending = 0;
};

/// Per-view radix-sort workspace. Simulation-owned instance data remains
/// immutable; only a compact uint index stream is reordered for drawing.
class ParticleGpuSorter
{
  public:
    static constexpr uint32_t WorkgroupSize = 256;
    static constexpr uint32_t Radix = 16;
    static constexpr uint32_t PassCount = 8;

    ParticleGpuSorter() = default;
    ~ParticleGpuSorter();

    ParticleGpuSorter(const ParticleGpuSorter &) = delete;
    ParticleGpuSorter &operator=(const ParticleGpuSorter &) = delete;
    ParticleGpuSorter(ParticleGpuSorter &&) = delete;
    ParticleGpuSorter &operator=(ParticleGpuSorter &&) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, const GpuParticleSorterDesc &desc);
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
    [[nodiscard]] rhi::BufferHandle IndirectBuffer() const noexcept
    {
        return m_indirectArguments;
    }
    [[nodiscard]] rhi::BufferHandle SourceIndexBuffer() const noexcept
    {
        return m_sourceIndices;
    }
    [[nodiscard]] rhi::BufferHandle DispatchBuffer() const noexcept
    {
        return m_dispatchArguments;
    }
    [[nodiscard]] rhi::BufferHandle SortedIndices() const noexcept
    {
        return m_indices[0];
    }
    [[nodiscard]] rhi::BufferHandle KeyBuffer(uint32_t pingPong) const noexcept
    {
        return pingPong < m_keys.size() ? m_keys[pingPong] : rhi::BufferHandle{};
    }
    [[nodiscard]] rhi::BufferHandle IndexBuffer(uint32_t pingPong) const noexcept
    {
        return pingPong < m_indices.size() ? m_indices[pingPong] : rhi::BufferHandle{};
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

    void RecordGenerate(const rhi::ComputeCommandEncoder &encoder, const std::array<float, 16> &view,
                        ParticleSortMode mode) const;
    void RecordHistogram(const rhi::ComputeCommandEncoder &encoder, uint32_t passIndex) const;
    void RecordScan(const rhi::ComputeCommandEncoder &encoder, uint32_t passIndex) const;
    void RecordScatter(const rhi::ComputeCommandEncoder &encoder, uint32_t passIndex) const;

  private:
    [[nodiscard]] GpuParticleSortConstants Constants(uint32_t passIndex = 0) const noexcept;
    void RecordDirect(const rhi::ComputeCommandEncoder &encoder, rhi::ComputePipelineHandle pipeline,
                      rhi::BindGroupHandle group, const GpuParticleSortConstants &constants, uint32_t groups) const;
    void RecordIndirect(const rhi::ComputeCommandEncoder &encoder, rhi::ComputePipelineHandle pipeline,
                        rhi::BindGroupHandle group, const GpuParticleSortConstants &constants) const;

    rhi::Device *m_device = nullptr;
    uint32_t m_capacity = 0;
    uint32_t m_blockCount = 0;
    rhi::BufferHandle m_instances;
    rhi::BufferHandle m_indirectArguments;
    rhi::BufferHandle m_sourceIndices;
    rhi::BufferHandle m_dispatchArguments;
    std::array<rhi::BufferHandle, 2> m_keys{};
    std::array<rhi::BufferHandle, 2> m_indices{};
    rhi::BufferHandle m_histograms;
    rhi::BufferHandle m_blockOffsets;
    rhi::BufferHandle m_globalOffsets;
    rhi::BindingLayoutHandle m_layout;
    std::array<rhi::BindGroupHandle, 2> m_groups{};
    rhi::ComputePipelineHandle m_generatePipeline;
    rhi::ComputePipelineHandle m_histogramPipeline;
    rhi::ComputePipelineHandle m_scanPipeline;
    rhi::ComputePipelineHandle m_scatterPipeline;
};

static_assert(sizeof(GpuParticleSortConstants) == 80);

} // namespace infernux::particle
