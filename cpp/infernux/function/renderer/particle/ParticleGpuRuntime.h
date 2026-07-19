#pragma once

#include <function/renderer/rhi/RhiCommand.h>
#include <function/renderer/rhi/RhiDevice.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>

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
    /// Build a new kernel/pipeline revision over the same GPU-resident state.
    /// Capacity and state ABI must match exactly.
    [[nodiscard]] bool CreateCompatible(rhi::Device &device, const GpuEmitterDesc &desc,
                                        const ParticleGpuRuntime &previous);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] bool SharesStateWith(const ParticleGpuRuntime &other) const noexcept;
    [[nodiscard]] bool NeedsBootstrap() const noexcept;
    void RequestBootstrap() noexcept;
    void MarkStateInitialized() noexcept;
    [[nodiscard]] bool UpdateTransforms(const GpuParticleTransforms &transforms);

    void RecordBootstrap(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed);
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
    [[nodiscard]] rhi::BufferHandle StateBuffer() const noexcept;
    [[nodiscard]] rhi::BufferHandle FreeListBuffer() const noexcept;
    [[nodiscard]] rhi::BufferHandle CounterBuffer() const noexcept;
    [[nodiscard]] rhi::BufferHandle InstanceBuffer() const noexcept;
    [[nodiscard]] rhi::BufferHandle IndirectBuffer() const noexcept;
    [[nodiscard]] rhi::BufferHandle RenderIndexBuffer() const noexcept;
    [[nodiscard]] rhi::BufferHandle TransformBuffer() const noexcept;

  private:
    struct ResidentState;

    [[nodiscard]] bool CreateInternal(rhi::Device &device, const GpuEmitterDesc &desc,
                                      std::shared_ptr<ResidentState> residentState);
    void Record(const rhi::ComputeCommandEncoder &encoder, GpuKernelStage stage,
                const GpuParticlePushConstants &constants, uint32_t invocationCount) const;
    [[nodiscard]] static uint32_t GroupCount(uint32_t invocationCount) noexcept;

    rhi::Device *m_device = nullptr;
    uint32_t m_capacity = 0;
    uint32_t m_stateStride = 0;
    std::shared_ptr<ResidentState> m_residentState;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    std::array<rhi::ComputePipelineHandle, static_cast<size_t>(GpuKernelStage::Count)> m_pipelines{};
};

static_assert(sizeof(GpuParticleTransforms) == 256);
static_assert(sizeof(GpuParticlePushConstants) == 32);

} // namespace infernux::particle
