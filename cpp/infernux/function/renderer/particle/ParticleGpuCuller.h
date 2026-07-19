#pragma once

#include "ParticleGpuRuntime.h"

#include <array>
#include <cstdint>
#include <string_view>
#include <vector>

namespace infernux::particle
{

struct GpuParticleCullProgram
{
    ShaderBytecode reset;
    ShaderBytecode cull;
    ShaderBytecode finalize;

    [[nodiscard]] bool IsValid() const noexcept;
};

struct GpuParticleCullProgramStorage
{
    std::array<std::vector<uint32_t>, 3> shaders;

    [[nodiscard]] bool Assign(const GpuParticleCullProgram &program);
    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] GpuParticleCullProgram View() const noexcept;
};

struct GpuParticleCullShaderSources
{
    [[nodiscard]] static std::string_view Reset() noexcept;
    [[nodiscard]] static std::string_view Cull() noexcept;
    [[nodiscard]] static std::string_view Finalize() noexcept;
};

struct GpuParticleCullerDesc
{
    uint32_t capacity = 0;
    rhi::BufferHandle instances;
    rhi::BufferHandle sourceIndirectArguments;
    rhi::BufferHandle bounds;
    GpuParticleCullProgram program;
};

struct alignas(16) GpuParticleCullConstants
{
    std::array<float, 24> frustumPlanes{};
    uint32_t capacity = 0;
    uint32_t reserved0 = 0;
    uint32_t reserved1 = 0;
    uint32_t reserved2 = 0;
};

/// Per-view visibility workspace. It never mutates simulation-owned instance
/// data and produces compact draw indices plus draw/sort indirect arguments.
class ParticleGpuCuller
{
  public:
    static constexpr uint32_t WorkgroupSize = 256;
    static constexpr uint32_t PlaneCount = 6;

    ParticleGpuCuller() = default;
    ~ParticleGpuCuller();

    ParticleGpuCuller(const ParticleGpuCuller &) = delete;
    ParticleGpuCuller &operator=(const ParticleGpuCuller &) = delete;
    ParticleGpuCuller(ParticleGpuCuller &&) = delete;
    ParticleGpuCuller &operator=(ParticleGpuCuller &&) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, const GpuParticleCullerDesc &desc);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] uint32_t Capacity() const noexcept
    {
        return m_capacity;
    }
    [[nodiscard]] rhi::BufferHandle InstanceBuffer() const noexcept
    {
        return m_instances;
    }
    [[nodiscard]] rhi::BufferHandle SourceIndirectBuffer() const noexcept
    {
        return m_sourceIndirectArguments;
    }
    [[nodiscard]] rhi::BufferHandle BoundsBuffer() const noexcept
    {
        return m_bounds;
    }
    [[nodiscard]] rhi::BufferHandle VisibleIndexBuffer() const noexcept
    {
        return m_visibleIndices;
    }
    [[nodiscard]] rhi::BufferHandle DrawIndirectBuffer() const noexcept
    {
        return m_drawIndirectArguments;
    }
    [[nodiscard]] rhi::BufferHandle SortDispatchBuffer() const noexcept
    {
        return m_sortDispatchArguments;
    }

    void RecordReset(const rhi::ComputeCommandEncoder &encoder,
                     const std::array<float, PlaneCount * 4> &frustumPlanes) const;
    void RecordCull(const rhi::ComputeCommandEncoder &encoder,
                    const std::array<float, PlaneCount * 4> &frustumPlanes) const;
    void RecordFinalize(const rhi::ComputeCommandEncoder &encoder) const;

  private:
    void Record(const rhi::ComputeCommandEncoder &encoder, rhi::ComputePipelineHandle pipeline,
                const GpuParticleCullConstants &constants, uint32_t groups) const;

    rhi::Device *m_device = nullptr;
    uint32_t m_capacity = 0;
    rhi::BufferHandle m_instances;
    rhi::BufferHandle m_sourceIndirectArguments;
    rhi::BufferHandle m_bounds;
    rhi::BufferHandle m_visibleIndices;
    rhi::BufferHandle m_drawIndirectArguments;
    rhi::BufferHandle m_sortDispatchArguments;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    rhi::ComputePipelineHandle m_resetPipeline;
    rhi::ComputePipelineHandle m_cullPipeline;
    rhi::ComputePipelineHandle m_finalizePipeline;
};

static_assert(sizeof(GpuParticleCullConstants) == 112);

} // namespace infernux::particle
