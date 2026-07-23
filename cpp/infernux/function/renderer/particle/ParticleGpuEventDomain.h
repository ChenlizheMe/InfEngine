#pragma once

#include <function/renderer/rhi/RhiCommand.h>
#include <function/renderer/rhi/RhiDevice.h>

#include <cstddef>
#include <cstdint>
#include <string_view>
#include <vector>

namespace infernux::particle
{

struct GpuParticleEventProgram
{
    const uint32_t *prepareWords = nullptr;
    size_t prepareWordCount = 0;
    const uint32_t *allocateWords = nullptr;
    size_t allocateWordCount = 0;

    [[nodiscard]] bool IsValid() const noexcept;
};

struct GpuParticleEventProgramStorage
{
    std::vector<uint32_t> prepareShader;
    std::vector<uint32_t> allocateShader;

    [[nodiscard]] bool Assign(const GpuParticleEventProgram &program);
    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] GpuParticleEventProgram View() const noexcept;
};

struct GpuParticleEventShaderSource
{
    [[nodiscard]] static std::string_view Prepare() noexcept;
    [[nodiscard]] static std::string_view Allocate() noexcept;
};

/// One statically compiled event route inside a ParticleGraph instance.
/// Emitter indexes and eventTypeIndex are dense indexes assigned by the
/// artifact compiler; stableEventTypeHash remains available for ABI checks.
struct GpuParticleEventChannelDesc
{
    uint64_t stableEventTypeHash = 0;
    uint32_t sourceEmitterIndex = 0;
    uint32_t targetEmitterIndex = 0;
    uint32_t eventTypeIndex = 0;
    uint32_t payloadStrideWords = 0;
    uint32_t capacity = 0;
    uint32_t spawnCount = 1;
};

struct GpuParticleEventDomainDesc
{
    uint64_t graphInstanceId = 0;
    uint64_t eventAbiHash = 0;
    uint32_t framesInFlight = 0;
    std::vector<GpuParticleEventChannelDesc> channels;
};

struct GpuParticleEventTargetDesc
{
    uint32_t emitterIndex = 0;
    uint32_t capacity = 0;
    rhi::BufferHandle freeList;
    rhi::BufferHandle counters;
    rhi::BindingLayoutHandle eventInputLayout;
};

/// Shader-visible immutable route metadata. Event records begin with four
/// words (type, source emitter, source particle id, source generation), then
/// the typed payload emitted by the compiler.
struct alignas(16) GpuParticleEventChannelRecord
{
    uint32_t recordBaseWords = 0;
    uint32_t recordStrideWords = 0;
    uint32_t capacity = 0;
    uint32_t sourceEmitterIndex = 0;
    uint32_t targetEmitterIndex = 0;
    uint32_t eventTypeIndex = 0;
    uint32_t payloadStrideWords = 0;
    uint32_t spawnCount = 1;
    uint32_t spawnBaseIndices = 0;
    uint32_t targetCapacity = 0;
    uint32_t reserved0 = 0;
    uint32_t reserved1 = 0;
};

struct alignas(16) GpuParticleEventCounter
{
    uint32_t writeCount = 0;
    uint32_t droppedCount = 0;
    uint32_t consumeCount = 0;
    uint32_t reserved = 0;
};

struct alignas(16) GpuParticleEventDispatchArguments
{
    uint32_t groupCountX = 0;
    uint32_t groupCountY = 1;
    uint32_t groupCountZ = 1;
    uint32_t reserved = 0;
};

struct alignas(16) GpuParticleEventPrepareConstants
{
    uint32_t channelCount = 0;
    uint32_t hasInput = 0;
    uint32_t consumerWorkgroupSize = 256;
    uint32_t reserved = 0;
};

struct alignas(16) GpuParticleEventAllocateConstants
{
    uint32_t channelIndex = 0;
    uint32_t reserved0 = 0;
    uint32_t reserved1 = 0;
    uint32_t reserved2 = 0;
};

struct GpuParticleEventPage
{
    rhi::BufferHandle records;
    rhi::BufferHandle counters;
    rhi::BufferHandle indirectArguments;
    rhi::BufferHandle spawnIndices;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return records.IsValid() && counters.IsValid() && indirectArguments.IsValid() && spawnIndices.IsValid();
    }
};

/// Graph-owned, GPU-local event transport. A simulation step writes only its
/// current ring page and may consume only the preceding step's page. The
/// renderer's frame fence must be complete before a ring page is reused.
class ParticleGpuEventDomain
{
  public:
    static constexpr uint32_t EventHeaderWords = 4;
    static constexpr uint32_t MinimumPageCount = 2;
    static constexpr uint32_t MaximumPageCount = 8;

    ParticleGpuEventDomain() = default;
    ~ParticleGpuEventDomain();

    ParticleGpuEventDomain(const ParticleGpuEventDomain &) = delete;
    ParticleGpuEventDomain &operator=(const ParticleGpuEventDomain &) = delete;
    ParticleGpuEventDomain(ParticleGpuEventDomain &&) = delete;
    ParticleGpuEventDomain &operator=(ParticleGpuEventDomain &&) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, const GpuParticleEventDomainDesc &desc,
                              const GpuParticleEventProgram &program,
                              const std::vector<GpuParticleEventTargetDesc> &targets);
    void Destroy() noexcept;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] uint64_t GraphInstanceId() const noexcept
    {
        return m_graphInstanceId;
    }
    [[nodiscard]] uint64_t EventAbiHash() const noexcept
    {
        return m_eventAbiHash;
    }
    [[nodiscard]] uint32_t PageCount() const noexcept
    {
        return static_cast<uint32_t>(m_pages.size());
    }
    [[nodiscard]] uint32_t ChannelCount() const noexcept
    {
        return m_channelCount;
    }
    [[nodiscard]] uint64_t RecordBufferBytes() const noexcept
    {
        return m_recordBufferBytes;
    }
    [[nodiscard]] uint64_t SpawnIndexBufferBytes() const noexcept
    {
        return m_spawnIndexBufferBytes;
    }
    [[nodiscard]] rhi::BufferHandle ChannelTable() const noexcept
    {
        return m_channelTable;
    }
    [[nodiscard]] const GpuParticleEventPage *Page(uint32_t pageIndex) const noexcept;
    void RecordPrepare(const rhi::ComputeCommandEncoder &encoder);
    void RecordAllocate(const rhi::ComputeCommandEncoder &encoder, uint32_t channelIndex) const;
    [[nodiscard]] bool HasPreparedInput() const noexcept
    {
        return m_hasInputForCurrentStep;
    }
    [[nodiscard]] rhi::BindGroupHandle CurrentEventInputGroup(uint32_t channelIndex) const noexcept;
    [[nodiscard]] rhi::BindGroupHandle CurrentEventOutputGroup() const noexcept;
    [[nodiscard]] rhi::BufferHandle CurrentIndirectArguments() const noexcept;
    [[nodiscard]] bool MatchesTargets(const std::vector<GpuParticleEventTargetDesc> &targets) const noexcept;
    [[nodiscard]] uint32_t ChannelTargetEmitterIndex(uint32_t channelIndex) const noexcept;

  private:
    rhi::Device *m_device = nullptr;
    uint64_t m_graphInstanceId = 0;
    uint64_t m_eventAbiHash = 0;
    uint64_t m_recordBufferBytes = 0;
    uint64_t m_spawnIndexBufferBytes = 0;
    uint32_t m_channelCount = 0;
    rhi::BufferHandle m_channelTable;
    std::vector<GpuParticleEventTargetDesc> m_targets;
    std::vector<GpuParticleEventChannelRecord> m_channels;
    std::vector<GpuParticleEventPage> m_pages;
    rhi::BindingLayoutHandle m_prepareLayout;
    std::vector<rhi::BindGroupHandle> m_prepareGroups;
    rhi::ComputePipelineHandle m_preparePipeline;
    rhi::BindingLayoutHandle m_allocateLayout;
    std::vector<rhi::BindGroupHandle> m_allocateGroups;
    std::vector<rhi::BindGroupHandle> m_eventInputGroups;
    rhi::ComputePipelineHandle m_allocatePipeline;
    rhi::BindingLayoutHandle m_outputLayout;
    std::vector<rhi::BindGroupHandle> m_outputGroups;
    bool m_hasPreparedPage = false;
    bool m_hasInputForCurrentStep = false;
    uint32_t m_currentReadPageIndex = 0;
    uint32_t m_currentWritePageIndex = 0;
    uint64_t m_nextPrepareEpoch = 0;
};

static_assert(sizeof(GpuParticleEventChannelRecord) == 48);
static_assert(sizeof(GpuParticleEventCounter) == 16);
static_assert(sizeof(GpuParticleEventDispatchArguments) == 16);
static_assert(sizeof(GpuParticleEventPrepareConstants) == 16);
static_assert(sizeof(GpuParticleEventAllocateConstants) == 16);

} // namespace infernux::particle
