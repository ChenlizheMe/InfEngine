#pragma once

#include <function/renderer/rhi/RhiCommand.h>
#include <function/renderer/rhi/RhiDevice.h>

#include <cstddef>
#include <cstdint>
#include <memory>

namespace infernux::particle
{

enum class GpuParticleContinuationKernelStage : uint8_t
{
    Prepare,
    Classify,
    Dispatch,
    Count,
};

struct GpuParticleContinuationShader
{
    const uint32_t *words = nullptr;
    size_t wordCount = 0;

    [[nodiscard]] bool IsValid() const noexcept;
};

/// GPU programs used by the continuation scheduler. These kernels establish
/// the storage and dispatch contract only; execution of a Wait opcode belongs
/// to the generated continuation program supplied by the particle compiler.
struct GpuParticleContinuationProgram
{
    GpuParticleContinuationShader prepare;
    GpuParticleContinuationShader classify;
    GpuParticleContinuationShader dispatch;

    [[nodiscard]] bool IsValid() const noexcept;
};

/// Fixed GPU ABI for one suspended branch. The payload remains deliberately
/// opaque to the C++ scheduler so future compiler revisions can assign it
/// without introducing a CPU interpretation path.
struct alignas(16) GpuParticleContinuationRecord
{
    uint32_t particleIndex = 0;
    uint32_t particleGeneration = 0;
    uint32_t programGeneration = 0;
    uint32_t resumeProgramCounter = 0;
    uint32_t wakeFrame = 0;
    uint32_t wakeTimeLow = 0;
    uint32_t wakeTimeHigh = 0;
    uint32_t laneIndex = 0;
    uint32_t branchToken = 0;
    uint32_t joinIndex = 0xFFFFFFFFu;
    uint32_t reservedContext = 0;
    uint32_t flags = 0;
    uint32_t payloadOffsetWords = 0;
    uint32_t payloadWordCount = 0;
    uint32_t reserved0 = 0;
    uint32_t reserved1 = 0;
};

/// Shader-visible counters. They remain GPU-resident during normal execution;
/// the TransferSource usage exists only for explicit diagnostics captures.
struct alignas(16) GpuParticleContinuationCounters
{
    uint32_t freeCount = 0;
    uint32_t activeCountA = 0;
    uint32_t activeCountB = 0;
    uint32_t readyCount = 0;
    uint32_t droppedCapacity = 0;
    uint32_t staleGeneration = 0;
    uint32_t resumedCount = 0;
    uint32_t completedCount = 0;
    uint32_t programGeneration = 0;
    uint32_t resetSerial = 0;
    uint32_t currentSimulationStep = 0;
    uint32_t elapsedTimeLow = 0;
    uint32_t elapsedTimeHigh = 0;
    uint32_t recordStrideWords = 0;
    uint32_t laneCount = 0;
    uint32_t joinCount = 0;
    uint32_t continuationCapacity = 0;
    uint32_t particleCapacity = 0;
    uint32_t branchTokenCounter = 0;
    uint32_t reserved = 0;
};

struct alignas(16) GpuParticleContinuationDispatchArguments
{
    uint32_t groupCountX = 0;
    uint32_t groupCountY = 1;
    uint32_t groupCountZ = 1;
    uint32_t reserved = 0;
};

struct alignas(16) GpuParticleContinuationJoinState
{
    uint32_t branchToken = 0;
    uint32_t expectedMask = 0;
    uint32_t arrivedMask = 0;
    uint32_t generation = 0;
};

struct alignas(16) GpuParticleContinuationConstants
{
    uint32_t capacity = 0;
    uint32_t particleCapacity = 0;
    uint32_t laneCount = 0;
    uint32_t joinCount = 0;
    uint32_t programGeneration = 0;
    uint32_t simulationStep = 0;
    uint32_t resetSerial = 0;
    uint32_t resetRequested = 0;
    uint32_t elapsedTimeLow = 0;
    uint32_t elapsedTimeHigh = 0;
    uint32_t recordStrideWords = 0;
    uint32_t systemSeed = 0;
    float deltaTime = 0.0f;
    uint32_t eventOutputEnabled = 0;
    uint32_t reserved0 = 0;
    uint32_t reserved1 = 0;
};

struct GpuParticleContinuationDesc
{
    uint32_t capacity = 0;
    uint32_t particleCapacity = 0;
    uint32_t recordStride = sizeof(GpuParticleContinuationRecord);
    uint32_t laneCount = 0;
    uint32_t joinCount = 0;
    uint32_t initialProgramGeneration = 1;
    GpuParticleContinuationProgram program;
    rhi::BindingLayoutHandle ownerLayout;
    rhi::BindGroupHandle ownerGroup;
    rhi::BindingLayoutHandle dataInterfaceLayout;
    rhi::BindGroupHandle dataInterfaceGroup;
    rhi::BindingLayoutHandle vectorFieldLayout;
    rhi::BindGroupHandle vectorFieldGroup;
    rhi::BindingLayoutHandle emptyLayout;
    rhi::BindGroupHandle emptyGroup;
    rhi::BindingLayoutHandle eventOutputLayout;
    bool emitsEvents = false;
};

struct GpuParticleContinuationResources
{
    rhi::BufferHandle records;
    rhi::BufferHandle freeList;
    rhi::BufferHandle readyQueue;
    rhi::BufferHandle activeQueueA;
    rhi::BufferHandle activeQueueB;
    rhi::BufferHandle counters;
    rhi::BufferHandle classifyIndirectArguments;
    rhi::BufferHandle dispatchIndirectArguments;
    rhi::BufferHandle laneSlots;
    rhi::BufferHandle joinStates;

    [[nodiscard]] bool IsValid() const noexcept;
};

struct GpuParticleContinuationTelemetry
{
    uint32_t capacity = 0;
    uint32_t particleCapacity = 0;
    uint32_t recordStride = 0;
    uint32_t laneCount = 0;
    uint32_t joinCount = 0;
    uint32_t programGeneration = 0;
    uint32_t resetSerial = 0;
    uint64_t recordBytes = 0;
    uint64_t queueBytes = 0;
    uint64_t laneSlotBytes = 0;
    uint64_t joinStateBytes = 0;
    uint64_t prepareRecordCalls = 0;
    uint64_t classifyRecordCalls = 0;
    uint64_t dispatchRecordCalls = 0;
    bool resetPending = false;
    bool gpuCountersOnly = true;
    rhi::BufferHandle gpuCounters;
    rhi::BufferHandle gpuClassifyIndirectArguments;
    rhi::BufferHandle gpuDispatchIndirectArguments;
};

/// Per-emitter GPU continuation storage and scheduling pipelines. The class
/// never reads counters back on the normal path. Compatible revisions share
/// the bounded resident pool while receiving a new program generation and a
/// separately retireable pipeline/bind-group revision.
class ParticleGpuContinuationRuntime
{
  public:
    static constexpr uint32_t WorkgroupSize = 256;
    static constexpr uint32_t MaximumCapacity = 1u << 24u;
    static constexpr uint32_t MaximumRecordStride = 4096;
    static constexpr uint32_t MaximumLaneCount = 4096;
    static constexpr uint32_t MaximumJoinCount = 1024;
    static constexpr uint64_t IndirectBufferBytes = sizeof(GpuParticleContinuationDispatchArguments);

    ParticleGpuContinuationRuntime();
    ~ParticleGpuContinuationRuntime();

    ParticleGpuContinuationRuntime(const ParticleGpuContinuationRuntime &) = delete;
    ParticleGpuContinuationRuntime &operator=(const ParticleGpuContinuationRuntime &) = delete;
    ParticleGpuContinuationRuntime(ParticleGpuContinuationRuntime &&) = delete;
    ParticleGpuContinuationRuntime &operator=(ParticleGpuContinuationRuntime &&) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, const GpuParticleContinuationDesc &desc);
    [[nodiscard]] bool CreateCompatible(rhi::Device &device, const GpuParticleContinuationDesc &desc,
                                        const ParticleGpuContinuationRuntime &previous);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] bool SharesStorageWith(const ParticleGpuContinuationRuntime &other) const noexcept;
    [[nodiscard]] uint32_t Capacity() const noexcept;
    [[nodiscard]] uint32_t ParticleCapacity() const noexcept;
    [[nodiscard]] uint32_t RecordStride() const noexcept;
    [[nodiscard]] uint32_t LaneCount() const noexcept;
    [[nodiscard]] uint32_t JoinCount() const noexcept;
    [[nodiscard]] uint32_t ProgramGeneration() const noexcept;
    [[nodiscard]] uint32_t ResetSerial() const noexcept;
    [[nodiscard]] const GpuParticleContinuationResources &Resources() const noexcept;
    [[nodiscard]] GpuParticleContinuationTelemetry Telemetry() const noexcept;
    [[nodiscard]] rhi::BindingLayoutHandle Layout() const noexcept;
    [[nodiscard]] rhi::BindGroupHandle Group() const noexcept;

    /// Invalidates every outstanding continuation without a CPU scan. The next
    /// Prepare pass publishes the new generation/reset serial to the GPU.
    void RequestReset() noexcept;

    [[nodiscard]] bool RecordPrepare(const rhi::ComputeCommandEncoder &encoder, uint32_t simulationStep,
                                     uint64_t elapsedTimeTicks);
    [[nodiscard]] bool RecordClassify(const rhi::ComputeCommandEncoder &encoder, uint32_t simulationStep,
                                      uint64_t elapsedTimeTicks) const;
    [[nodiscard]] bool RecordDispatch(const rhi::ComputeCommandEncoder &encoder, uint32_t simulationStep,
                                      uint64_t elapsedTimeTicks, uint32_t systemSeed, float deltaTime,
                                      rhi::BindGroupHandle eventOutput = {}) const;

  private:
    struct ResidentStorage;
    struct ProgramRevision;

    [[nodiscard]] bool CreateInternal(rhi::Device &device, const GpuParticleContinuationDesc &desc,
                                      std::shared_ptr<ResidentStorage> storage, uint32_t programGeneration);
    [[nodiscard]] GpuParticleContinuationConstants Constants(uint32_t simulationStep, uint64_t elapsedTimeTicks,
                                                             uint32_t systemSeed = 0, float deltaTime = 0.0f,
                                                             bool eventOutputEnabled = false) const noexcept;
    [[nodiscard]] static uint32_t NextGeneration(uint32_t value) noexcept;

    rhi::Device *m_device = nullptr;
    std::shared_ptr<ResidentStorage> m_storage;
    std::unique_ptr<ProgramRevision> m_revision;
    uint32_t m_programGeneration = 0;
    uint32_t m_resetSerial = 0;
    bool m_resetPending = true;
    mutable uint64_t m_prepareRecordCalls = 0;
    mutable uint64_t m_classifyRecordCalls = 0;
    mutable uint64_t m_dispatchRecordCalls = 0;
    uint64_t m_recordEpoch = 0;
    mutable uint64_t m_classifiedEpoch = 0;
    mutable uint64_t m_dispatchedEpoch = 0;
};

static_assert(sizeof(GpuParticleContinuationRecord) == 64);
static_assert(sizeof(GpuParticleContinuationCounters) == 80);
static_assert(sizeof(GpuParticleContinuationDispatchArguments) == 16);
static_assert(sizeof(GpuParticleContinuationJoinState) == 16);
static_assert(sizeof(GpuParticleContinuationConstants) == 64);

} // namespace infernux::particle
