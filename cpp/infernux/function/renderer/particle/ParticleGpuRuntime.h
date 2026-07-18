#pragma once

#include <function/renderer/rhi/RhiCommand.h>
#include <function/renderer/rhi/RhiDevice.h>

#include <array>
#include <cstddef>
#include <cstdint>

namespace infernux::particle
{

enum class GpuKernelStage : uint8_t
{
    Bootstrap,
    Init,
    Update,
    RenderReset,
    Rendering,
    Count,
};

struct ShaderBytecode
{
    const uint32_t *words = nullptr;
    size_t wordCount = 0;
};

struct GpuEmitterDesc
{
    uint32_t capacity = 0;
    uint32_t stateStride = 0;
    std::array<ShaderBytecode, static_cast<size_t>(GpuKernelStage::Count)> kernels{};
};

struct alignas(16) GpuParticleTransforms
{
    std::array<float, 16> emitterToWorld{};
    std::array<float, 16> worldToEmitter{};
    std::array<float, 16> simulationToWorld{};
    std::array<float, 16> worldToSimulation{};
};

struct GpuParticlePushConstants
{
    uint32_t capacity = 0;
    uint32_t invocationCount = 0;
    uint32_t spawnBaseId = 0;
    uint32_t spawnGeneration = 0;
    uint32_t systemSeed = 0;
    uint32_t simulationStep = 0;
    float deltaTime = 0.0f;
    uint32_t reserved = 0;
};

class ParticleGpuRuntime
{
  public:
    static constexpr uint32_t WorkgroupSize = 256;
    static constexpr uint32_t RenderInstanceStride = 48;

    ParticleGpuRuntime() = default;
    ~ParticleGpuRuntime();

    ParticleGpuRuntime(const ParticleGpuRuntime &) = delete;
    ParticleGpuRuntime &operator=(const ParticleGpuRuntime &) = delete;
    ParticleGpuRuntime(ParticleGpuRuntime &&) = delete;
    ParticleGpuRuntime &operator=(ParticleGpuRuntime &&) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, const GpuEmitterDesc &desc);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] bool UpdateTransforms(const GpuParticleTransforms &transforms);

    void RecordBootstrap(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed) const;
    void RecordInit(const rhi::ComputeCommandEncoder &encoder, uint32_t spawnCount, uint32_t spawnBaseId,
                    uint32_t spawnGeneration, uint32_t systemSeed, uint32_t simulationStep, float deltaTime) const;
    void RecordUpdate(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed, uint32_t simulationStep,
                      float deltaTime) const;
    void RecordRenderReset(const rhi::ComputeCommandEncoder &encoder) const;
    void RecordRendering(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed, uint32_t simulationStep) const;

    [[nodiscard]] uint32_t Capacity() const noexcept
    {
        return m_capacity;
    }
    [[nodiscard]] uint32_t StateStride() const noexcept
    {
        return m_stateStride;
    }
    [[nodiscard]] rhi::BufferHandle StateBuffer() const noexcept
    {
        return m_states;
    }
    [[nodiscard]] rhi::BufferHandle FreeListBuffer() const noexcept
    {
        return m_freeList;
    }
    [[nodiscard]] rhi::BufferHandle CounterBuffer() const noexcept
    {
        return m_counters;
    }
    [[nodiscard]] rhi::BufferHandle InstanceBuffer() const noexcept
    {
        return m_instances;
    }
    [[nodiscard]] rhi::BufferHandle IndirectBuffer() const noexcept
    {
        return m_indirect;
    }
    [[nodiscard]] rhi::BufferHandle TransformBuffer() const noexcept
    {
        return m_transforms;
    }

  private:
    void Record(const rhi::ComputeCommandEncoder &encoder, GpuKernelStage stage,
                const GpuParticlePushConstants &constants, uint32_t invocationCount) const;
    [[nodiscard]] static uint32_t GroupCount(uint32_t invocationCount) noexcept;

    rhi::Device *m_device = nullptr;
    uint32_t m_capacity = 0;
    uint32_t m_stateStride = 0;
    rhi::BufferHandle m_states;
    rhi::BufferHandle m_freeList;
    rhi::BufferHandle m_counters;
    rhi::BufferHandle m_instances;
    rhi::BufferHandle m_indirect;
    rhi::BufferHandle m_transforms;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    std::array<rhi::ComputePipelineHandle, static_cast<size_t>(GpuKernelStage::Count)> m_pipelines{};
};

static_assert(sizeof(GpuParticleTransforms) == 256);
static_assert(sizeof(GpuParticlePushConstants) == 32);

} // namespace infernux::particle
