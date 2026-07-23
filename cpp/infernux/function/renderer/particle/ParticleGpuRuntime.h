#pragma once

#include <function/renderer/rhi/RhiCommand.h>
#include <function/renderer/rhi/RhiDevice.h>
#include <function/resources/InxPointCache/PointCacheArtifact.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

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

struct GpuPointCacheSampleDesc
{
    uint32_t sampleIndex = 0;
    std::string channel;
    PointCacheChannelType expectedType = PointCacheChannelType::Float;
    bool requiresNormalTransform = false;
};

struct GpuPointCacheDesc
{
    uint32_t interfaceIndex = 0;
    uint32_t dataBinding = 0;
    uint32_t lookupBinding = 0;
    bool worldSpace = true;
    std::array<float, 16> cacheToSpace = {
        1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f,
    };
    std::shared_ptr<const PointCacheCpuData> data;
    rhi::BufferHandle dataBuffer;
    rhi::BufferHandle lookupBuffer;
    std::shared_ptr<void> keepAlive;
    std::vector<GpuPointCacheSampleDesc> samples;
};

struct GpuPointCacheLayoutDesc
{
    uint32_t metadataBinding = 0;
    uint32_t interfaceStrideWords = 32;
    uint32_t sampleStrideWords = 4;
    uint32_t sampleCount = 0;
    std::vector<GpuPointCacheDesc> pointCaches;
};

struct GpuVectorFieldDesc
{
    uint32_t interfaceIndex = 0;
    uint32_t textureBinding = 0;
    bool worldSpace = true;
    std::array<float, 16> fieldToSpace = {
        1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f,
    };
    float vectorScale = 1.0f;
    rhi::TextureViewHandle texture;
    rhi::SamplerHandle sampler;
    std::shared_ptr<void> keepAlive;
};

struct GpuVectorFieldLayoutDesc
{
    uint32_t metadataBinding = 0;
    uint32_t interfaceStrideWords = 32;
    std::vector<GpuVectorFieldDesc> vectorFields;
};

struct GpuEmitterDesc
{
    uint32_t capacity = 0;
    uint32_t stateStride = 0;
    uint32_t eventOutputStageMask = 0;
    std::array<ShaderBytecode, static_cast<size_t>(GpuKernelStage::Count)> kernels{};
    ShaderBytecode eventInitKernel;
    GpuPointCacheLayoutDesc pointCaches;
    GpuVectorFieldLayoutDesc vectorFields;
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
    static constexpr uint32_t RenderInstanceStride = 80;

    ParticleGpuRuntime();
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
    /// Atomically install a compatible pipeline/data-interface revision while
    /// leaving this runtime object and its GPU-resident particle state stable.
    /// The replacement receives the retired resources and must remain alive
    /// until all in-flight frames that referenced them have completed.
    [[nodiscard]] bool AdoptCompatibleRevision(ParticleGpuRuntime &replacement) noexcept;
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] bool SharesStateWith(const ParticleGpuRuntime &other) const noexcept;
    [[nodiscard]] bool NeedsBootstrap() const noexcept;
    void RequestBootstrap() noexcept;
    void MarkStateInitialized() noexcept;
    [[nodiscard]] bool UpdateTransforms(const GpuParticleTransforms &transforms);

    void RecordBootstrap(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed);
    void RecordInit(const rhi::ComputeCommandEncoder &encoder, uint32_t spawnCount, uint32_t spawnBaseId,
                    uint32_t spawnGeneration, uint32_t systemSeed, uint32_t simulationStep, float deltaTime,
                    rhi::BindGroupHandle eventOutput = {}) const;
    void RecordEventInit(const rhi::ComputeCommandEncoder &encoder, rhi::BindGroupHandle eventInput,
                         rhi::BufferHandle indirectArguments, uint64_t indirectOffset, uint32_t channelIndex,
                         uint32_t systemSeed, uint32_t simulationStep, float deltaTime,
                         rhi::BindGroupHandle eventOutput = {}) const;
    void RecordUpdate(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed, uint32_t simulationStep,
                      float deltaTime, rhi::BindGroupHandle eventOutput = {}) const;
    void RecordRenderReset(const rhi::ComputeCommandEncoder &encoder) const;
    void RecordRendering(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed, uint32_t simulationStep,
                         rhi::BindGroupHandle eventOutput = {}, bool emitEvents = true) const;
    [[nodiscard]] bool HasEventOutput(GpuKernelStage stage) const noexcept;

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
    [[nodiscard]] rhi::BindingLayoutHandle EventInputLayout() const noexcept
    {
        return m_eventInputLayout;
    }

  private:
    struct ResidentState;
    struct DataInterfaceState;
    struct VectorFieldState;

    [[nodiscard]] bool CreateInternal(rhi::Device &device, const GpuEmitterDesc &desc,
                                      std::shared_ptr<ResidentState> residentState);
    [[nodiscard]] bool UpdatePointCacheMetadata(const GpuParticleTransforms &transforms);
    [[nodiscard]] bool UpdateVectorFieldMetadata(const GpuParticleTransforms &transforms);
    void Record(const rhi::ComputeCommandEncoder &encoder, GpuKernelStage stage,
                const GpuParticlePushConstants &constants, uint32_t invocationCount,
                rhi::BindGroupHandle eventOutput = {}) const;
    [[nodiscard]] static uint32_t GroupCount(uint32_t invocationCount) noexcept;

    rhi::Device *m_device = nullptr;
    uint32_t m_capacity = 0;
    uint32_t m_stateStride = 0;
    uint32_t m_eventOutputStageMask = 0;
    std::shared_ptr<ResidentState> m_residentState;
    std::unique_ptr<DataInterfaceState> m_dataInterfaces;
    std::unique_ptr<VectorFieldState> m_vectorFields;
    rhi::BindingLayoutHandle m_emptyDataInterfaceLayout;
    rhi::BindGroupHandle m_emptyDataInterfaceGroup;
    rhi::BindingLayoutHandle m_eventInputLayout;
    rhi::BindingLayoutHandle m_eventOutputLayout;
    rhi::BindingLayoutHandle m_layout;
    rhi::BindGroupHandle m_group;
    std::array<rhi::ComputePipelineHandle, static_cast<size_t>(GpuKernelStage::Count)> m_pipelines{};
    rhi::ComputePipelineHandle m_eventInitPipeline;
};

static_assert(sizeof(GpuParticleTransforms) == 256);
static_assert(sizeof(GpuParticlePushConstants) == 32);

} // namespace infernux::particle
