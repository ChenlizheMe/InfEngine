#pragma once

#include "ParticleGpuBounds.h"
#include "ParticleGpuMigrator.h"
#include "ParticleGpuRibbonTopology.h"
#include "ParticleGpuRuntime.h"

#include <function/renderer/vk/RenderGraph.h>

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace infernux::particle
{

struct GpuParticleFrameRequest
{
    uint64_t frameIndex = 0;
    uint32_t substepIndex = 0;
    uint32_t spawnCount = 0;
    uint32_t spawnBaseId = 0;
    uint32_t spawnGeneration = 0;
    uint32_t systemSeed = 0;
    uint32_t simulationStep = 0;
    /// Monotonic engine-owned continuation clock ticks. The runtime treats the
    /// value as opaque; generated Wait-for-seconds code defines the tick rate.
    uint64_t continuationTimeTicks = 0;
    float deltaTime = 0.0f;
    bool simulate = true;
    bool render = true;
    /// Internal, one-frame diagnostics gate. Normal simulation leaves this
    /// false so collision telemetry adds no atomics to gameplay frames.
    bool collectCollisionDiagnostics = false;
    /// Internal, one-frame diagnostics baseline reset. The RenderReset kernel
    /// clears collision telemetry before a new sampling window begins.
    bool resetCollisionDiagnostics = false;
    GpuParticleOffscreenPolicy offscreenPolicy = GpuParticleOffscreenPolicy::AlwaysSimulate;
    bool forceSimulation = false;
    GpuParticleBoundsMode boundsMode = GpuParticleBoundsMode::Automatic;
    std::array<float, 3> manualBoundsLower{};
    std::array<float, 3> manualBoundsUpper{};
};

/// Fixed set=3 ABI shared by every generated particle compute kernel.
/// binding 0: graph-wide uint burstRequestCounts[] (read/write)
/// binding 1: this emitter's 32-byte GpuParticleSpawnMetadata record
struct alignas(16) GpuParticleSpawnMetadata
{
    uint32_t count = 0;
    uint32_t baseId = 0;
    uint32_t generation = 0;
    uint32_t overflowCount = 0;
    uint32_t dispatchGroupCountX = 0;
    uint32_t dispatchGroupCountY = 1;
    uint32_t dispatchGroupCountZ = 1;
    uint32_t acceptedSpawnTotal = 0;
};
static_assert(sizeof(GpuParticleSpawnMetadata) == 32);
static_assert(offsetof(GpuParticleSpawnMetadata, dispatchGroupCountX) == 16);

struct alignas(16) GpuParticleSpawnDomainConstants
{
    uint32_t slotCount = 0;
    uint32_t targetSlot = 0;
    uint32_t capacity = 0;
    uint32_t cpuSpawnCount = 0;
    uint32_t spawnBaseId = 0;
    uint32_t spawnGeneration = 0;
    uint32_t reset = 0;
    uint32_t reserved = 0;
};

struct GpuParticleSpawnProgram
{
    ShaderBytecode advance;
    ShaderBytecode prepare;

    [[nodiscard]] bool IsValid() const noexcept;
};

struct GpuParticleSpawnProgramStorage
{
    std::array<std::vector<uint32_t>, 2> shaders;

    [[nodiscard]] bool Assign(const GpuParticleSpawnProgram &program);
    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] GpuParticleSpawnProgram View() const noexcept;
};

struct GpuParticleSpawnShaderSources
{
    [[nodiscard]] static std::string_view Advance() noexcept;
    [[nodiscard]] static std::string_view Prepare() noexcept;
};

/// One GPU spawn request domain per live ParticleGraph instance. A Burst node
/// only appends a count to the addressed emitter's request queue; it never
/// transfers source particles or source state. The next graph execution moves
/// each enabled and running emitter's queued count into consumingCounts, then that emitter
/// consumes its own dense graphEmitterIndex slot and runs its own Init. An
/// disabled or playback-stopped emitter silently discards requests. There is
/// no separate user-facing Active state: Enabled gates the entire emitter.
class ParticleGpuGraphSpawnDomain
{
  public:
    static constexpr uint32_t WorkgroupSize = 256;
    static constexpr uint64_t MetadataStride = sizeof(GpuParticleSpawnMetadata);
    static constexpr uint64_t IndirectOffset = offsetof(GpuParticleSpawnMetadata, dispatchGroupCountX);

    ParticleGpuGraphSpawnDomain() = default;
    ~ParticleGpuGraphSpawnDomain();

    ParticleGpuGraphSpawnDomain(const ParticleGpuGraphSpawnDomain &) = delete;
    ParticleGpuGraphSpawnDomain &operator=(const ParticleGpuGraphSpawnDomain &) = delete;
    ParticleGpuGraphSpawnDomain(ParticleGpuGraphSpawnDomain &&) = delete;
    ParticleGpuGraphSpawnDomain &operator=(ParticleGpuGraphSpawnDomain &&) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, uint64_t graphInstanceId, uint32_t slotCount,
                              const GpuParticleSpawnProgram &program, const std::vector<uint32_t> &parameterWords);
    void Destroy() noexcept;
    [[nodiscard]] bool RegisterEmitter(uint32_t targetSlot, const ParticleGpuRuntime &runtime);
    [[nodiscard]] bool SetEmitterAcceptingBurstRequests(uint32_t targetSlot, bool accepting);
    [[nodiscard]] bool SetEmitterPlaying(uint32_t targetSlot, bool playing);
    [[nodiscard]] bool UpdateParameters(const std::vector<uint32_t> &parameterWords);
    [[nodiscard]] bool Attach(vk::RenderGraph &graph, const std::string &namePrefix);
    /// Arm the graph-level spawn prepass for the next graph execution. The
    /// manager calls this only when an emitter in this graph has work.
    void MarkFramePending() noexcept
    {
        m_framePending = true;
    }
    /// Consume the graph-level spawn prepass token. Only the attached
    /// RenderGraph callback should call this; a second consume is a no-op.
    [[nodiscard]] bool ConsumeFramePending() noexcept
    {
        if (!m_framePending)
            return false;
        m_framePending = false;
        return true;
    }
    [[nodiscard]] bool HasFramePending() const noexcept
    {
        return m_framePending;
    }
    void DeclarePrepare(vk::PassBuilder &builder);
    void DeclareKernelWrite(vk::PassBuilder &builder);
    void DeclareInitRead(vk::PassBuilder &builder);
    void RecordPrepare(const rhi::ComputeCommandEncoder &encoder, uint32_t targetSlot, uint32_t capacity,
                       const GpuParticleFrameRequest &request, bool discardCpuSpawn,
                       bool resetPreviousState) const;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] uint64_t GraphInstanceId() const noexcept
    {
        return m_graphInstanceId;
    }
    [[nodiscard]] uint32_t SlotCount() const noexcept
    {
        return m_slotCount;
    }
    [[nodiscard]] rhi::BindGroupHandle RuntimeGroup(uint32_t targetSlot) const noexcept;
    [[nodiscard]] rhi::BufferHandle BurstRequestBuffer() const noexcept
    {
        return m_burstRequestCounts;
    }
    [[nodiscard]] rhi::BufferHandle ConsumingBuffer() const noexcept
    {
        return m_consumingCounts;
    }
    [[nodiscard]] rhi::BufferHandle BurstRequestAcceptanceBuffer() const noexcept
    {
        return m_acceptingRequestSlots;
    }
    [[nodiscard]] rhi::BufferHandle EmitterPlayingRequestBuffer() const noexcept
    {
        return m_emitterPlayingRequests;
    }
    [[nodiscard]] rhi::BufferHandle EmitterPlayingStateBuffer() const noexcept
    {
        return m_emitterPlayingStates;
    }
    [[nodiscard]] rhi::BufferHandle MetadataBuffer() const noexcept
    {
        return m_spawnMetadata;
    }
    [[nodiscard]] uint64_t MetadataOffset(uint32_t targetSlot) const noexcept
    {
        return uint64_t(targetSlot) * MetadataStride;
    }
    [[nodiscard]] uint64_t InitIndirectOffset(uint32_t targetSlot) const noexcept
    {
        return MetadataOffset(targetSlot) + IndirectOffset;
    }

  private:
    rhi::Device *m_device = nullptr;
    uint64_t m_graphInstanceId = 0;
    uint32_t m_slotCount = 0;
    rhi::BufferHandle m_burstRequestCounts;
    rhi::BufferHandle m_consumingCounts;
    rhi::BufferHandle m_acceptingRequestSlots;
    rhi::BufferHandle m_emitterPlayingRequests;
    rhi::BufferHandle m_emitterPlayingStates;
    rhi::BufferHandle m_spawnMetadata;
    rhi::BufferHandle m_parameterBuffer;
    uint32_t m_parameterWordCount = 0;
    rhi::BindingLayoutHandle m_domainLayout;
    rhi::BindGroupHandle m_advanceGroup;
    rhi::BindGroupHandle m_prepareGroup;
    rhi::ComputePipelineHandle m_advancePipeline;
    rhi::ComputePipelineHandle m_preparePipeline;
    bool m_resetPending = true;
    bool m_framePending = false;
    std::vector<rhi::BindGroupHandle> m_runtimeGroups;
    vk::ResourceHandle m_burstRequestResource;
    vk::ResourceHandle m_consumingResource;
    vk::ResourceHandle m_metadataResource;
    vk::ResourceHandle m_parameterResource;
    vk::ResourceHandle m_emitterPlayingRequestResource;
    vk::ResourceHandle m_emitterPlayingStateResource;
};

struct GpuParticleGraphOutputs
{
    vk::ResourceHandle instances;
    vk::ResourceHandle visibility;
    vk::ResourceHandle renderIndices;
    vk::ResourceHandle indirectArguments;
    vk::ResourceHandle bounds;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return instances.IsValid() && visibility.IsValid() && renderIndices.IsValid() && indirectArguments.IsValid() &&
               bounds.IsValid();
    }
};

/// Declares one GPU emitter's simulation and render-export stages in a
/// RenderGraph. BeginFrame arms a one-shot request: a second graph execution
/// in the same engine frame records no particle work, so multiple cameras
/// cannot accidentally advance the simulation more than once. This scheduler
/// and its ParticleGpuRuntime must outlive callbacks stored in the graph.
class ParticleRenderGraph
{
  public:
    ParticleRenderGraph() = default;
    ~ParticleRenderGraph() = default;

    ParticleRenderGraph(const ParticleRenderGraph &) = delete;
    ParticleRenderGraph &operator=(const ParticleRenderGraph &) = delete;
    ParticleRenderGraph(ParticleRenderGraph &&) = delete;
    ParticleRenderGraph &operator=(ParticleRenderGraph &&) = delete;

    [[nodiscard]] bool Attach(vk::RenderGraph &graph, ParticleGpuRuntime &runtime, ParticleGpuBounds &bounds,
                              ParticleGpuGraphSpawnDomain &spawnDomain, uint32_t graphEmitterIndex,
                              const std::string &namePrefix, ParticleGpuMigrator *migration = nullptr,
                              ParticleGpuRibbonTopology *ribbonTopology = nullptr);
    [[nodiscard]] static bool IsFrameRequestValid(const GpuParticleFrameRequest &request) noexcept;
    [[nodiscard]] static bool ShouldUseFusedUpdateRendering(const GpuParticleFrameRequest &request,
                                                            const ParticleGpuRuntime &runtime) noexcept;
    [[nodiscard]] bool CanBeginFrame(const GpuParticleFrameRequest &request) const noexcept;
    [[nodiscard]] bool BeginFrame(const GpuParticleFrameRequest &request) noexcept;
    void Reset() noexcept;
    [[nodiscard]] bool HasCompletedMigration() const noexcept
    {
        return m_migrationCompleted;
    }
    [[nodiscard]] bool ConsumeMigrationCompletion() noexcept;
    [[nodiscard]] GpuParticleContinuationTelemetry ContinuationTelemetry() const noexcept
    {
        return m_runtime ? m_runtime->ContinuationTelemetry() : GpuParticleContinuationTelemetry{};
    }

    [[nodiscard]] bool IsAttached() const noexcept
    {
        return m_runtime != nullptr && m_outputs.IsValid();
    }
    [[nodiscard]] bool HasPendingFrame() const noexcept
    {
        return m_framePending;
    }
    [[nodiscard]] bool HasResetPending() const noexcept
    {
        return m_resetPending;
    }
    [[nodiscard]] bool HasRenderResetPending() const noexcept
    {
        return m_renderResetPending;
    }
    /// Consume the one-shot render reset token. The RenderReset pass uses the
    /// same operation, so an idle frame cannot emit the reset twice.
    [[nodiscard]] bool ConsumeRenderResetPending() noexcept
    {
        if (!m_renderResetPending)
            return false;
        m_renderResetPending = false;
        return true;
    }
    [[nodiscard]] uint64_t LastConsumedFrame() const noexcept
    {
        return m_lastConsumedFrame;
    }
    [[nodiscard]] const GpuParticleGraphOutputs &Outputs() const noexcept
    {
        return m_outputs;
    }
    [[nodiscard]] uint32_t RenderExportPassId() const noexcept
    {
        return m_renderExportPassId;
    }

  private:
    ParticleGpuRuntime *m_runtime = nullptr;
    ParticleGpuBounds *m_bounds = nullptr;
    ParticleGpuMigrator *m_migrator = nullptr;
    ParticleGpuRibbonTopology *m_ribbonTopology = nullptr;
    ParticleGpuGraphSpawnDomain *m_spawnDomain = nullptr;
    uint32_t m_graphEmitterIndex = 0;
    GpuParticleFrameRequest m_request{};
    GpuParticleGraphOutputs m_outputs{};
    bool m_bootstrapPending = true;
    bool m_contactResetPending = true;
    bool m_migrationPending = false;
    bool m_migrationCompleted = false;
    bool m_framePending = false;
    bool m_resetPending = false;
    bool m_hasConsumedFrame = false;
    uint64_t m_lastConsumedFrame = 0;
    uint32_t m_lastConsumedSubstep = 0;
    uint32_t m_renderExportPassId = UINT32_MAX;
    // RenderReset is needed every rendering frame and once when rendering is
    // disabled, but not for an already-idle non-rendering frame.
    bool m_lastRenderStateActive = false;
    bool m_renderResetPending = true;
};

} // namespace infernux::particle
