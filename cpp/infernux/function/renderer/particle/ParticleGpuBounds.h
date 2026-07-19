#pragma once

#include "ParticleGpuRuntime.h"

#include <array>
#include <cstdint>
#include <string_view>
#include <vector>

namespace infernux::particle
{

struct GpuParticleBoundsProgram
{
    ShaderBytecode reset;
    ShaderBytecode reduce;

    [[nodiscard]] bool IsValid() const noexcept;
};

struct GpuParticleBoundsProgramStorage
{
    std::array<std::vector<uint32_t>, 2> shaders;

    [[nodiscard]] bool Assign(const GpuParticleBoundsProgram &program);
    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] GpuParticleBoundsProgram View() const noexcept;
};

struct GpuParticleBoundsShaderSources
{
    [[nodiscard]] static std::string_view Reset() noexcept;
    [[nodiscard]] static std::string_view Reduce() noexcept;
};

struct GpuParticleBoundsDesc
{
    uint32_t capacity = 0;
    rhi::BufferHandle instances;
    rhi::BufferHandle sourceIndirectArguments;
    GpuParticleBoundsProgram program;
};

struct alignas(16) GpuParticleBoundsConstants
{
    uint32_t capacity = 0;
    uint32_t reserved0 = 0;
    uint32_t reserved1 = 0;
    uint32_t reserved2 = 0;
};

/// Once-per-simulation-frame conservative bounds reduction over exported
/// render instances. View graphs consume the result without mutating it.
class ParticleGpuBounds
{
  public:
    static constexpr uint32_t WorkgroupSize = 256;
    static constexpr uint32_t BoundsBufferBytes = 32;
    static constexpr uint32_t DispatchBufferBytes = 12;

    ParticleGpuBounds() = default;
    ~ParticleGpuBounds();

    ParticleGpuBounds(const ParticleGpuBounds &) = delete;
    ParticleGpuBounds &operator=(const ParticleGpuBounds &) = delete;
    ParticleGpuBounds(ParticleGpuBounds &&) = delete;
    ParticleGpuBounds &operator=(ParticleGpuBounds &&) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, const GpuParticleBoundsDesc &desc);
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
    [[nodiscard]] rhi::BufferHandle DispatchBuffer() const noexcept
    {
        return m_dispatchArguments;
    }

    void RecordReset(const rhi::ComputeCommandEncoder &encoder) const;
    void RecordReduce(const rhi::ComputeCommandEncoder &encoder) const;

  private:
    void Bind(const rhi::ComputeCommandEncoder &encoder, rhi::ComputePipelineHandle pipeline) const;

    rhi::Device *m_device = nullptr;
    uint32_t m_capacity = 0;
    rhi::BufferHandle m_instances;
    rhi::BufferHandle m_sourceIndirectArguments;
    rhi::BufferHandle m_bounds;
    rhi::BufferHandle m_dispatchArguments;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    rhi::ComputePipelineHandle m_resetPipeline;
    rhi::ComputePipelineHandle m_reducePipeline;
};

static_assert(sizeof(GpuParticleBoundsConstants) == 16);

} // namespace infernux::particle
