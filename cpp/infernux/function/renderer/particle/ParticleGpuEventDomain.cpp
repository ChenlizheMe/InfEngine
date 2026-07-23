#include "ParticleGpuEventDomain.h"

#include <algorithm>
#include <array>
#include <limits>
#include <string>
#include <unordered_map>
#include <utility>

namespace infernux::particle
{

namespace
{

bool CheckedAdd(uint64_t lhs, uint64_t rhs, uint64_t &result) noexcept
{
    if (lhs > std::numeric_limits<uint64_t>::max() - rhs)
        return false;
    result = lhs + rhs;
    return true;
}

bool IsShaderBytecodeValid(const uint32_t *words, size_t wordCount) noexcept
{
    return words && wordCount >= 5 && words[0] == 0x07230203u;
}

} // namespace

bool GpuParticleEventProgram::IsValid() const noexcept
{
    return IsShaderBytecodeValid(prepareWords, prepareWordCount) &&
           IsShaderBytecodeValid(allocateWords, allocateWordCount);
}

bool GpuParticleEventProgramStorage::Assign(const GpuParticleEventProgram &program)
{
    if (!program.IsValid())
        return false;
    prepareShader.assign(program.prepareWords, program.prepareWords + program.prepareWordCount);
    allocateShader.assign(program.allocateWords, program.allocateWords + program.allocateWordCount);
    return true;
}

bool GpuParticleEventProgramStorage::IsValid() const noexcept
{
    return View().IsValid();
}

GpuParticleEventProgram GpuParticleEventProgramStorage::View() const noexcept
{
    return {prepareShader.data(), prepareShader.size(), allocateShader.data(), allocateShader.size()};
}

std::string_view GpuParticleEventShaderSource::Prepare() noexcept
{
    static constexpr std::string_view Source = R"glsl(#version 450
layout(local_size_x = 64) in;

struct EventChannel {
    uint record_base_words;
    uint record_stride_words;
    uint capacity;
    uint source_emitter_index;
    uint target_emitter_index;
    uint event_type_index;
    uint payload_stride_words;
    uint spawn_count;
    uint spawn_base_indices;
    uint target_capacity;
    uint reserved0;
    uint reserved1;
};
layout(std430, set = 0, binding = 0) readonly buffer ChannelTable { EventChannel channels[]; };
layout(std430, set = 0, binding = 1) buffer InputCounters { uvec4 input_counters[]; };
layout(std430, set = 0, binding = 2) buffer InputIndirect { uvec4 input_indirect[]; };
layout(std430, set = 0, binding = 3) buffer OutputCounters { uvec4 output_counters[]; };
layout(std430, set = 0, binding = 4) buffer OutputIndirect { uvec4 output_indirect[]; };
layout(push_constant) uniform EventPrepareConstants {
    uint channel_count;
    uint has_input;
    uint consumer_workgroup_size;
    uint reserved;
} pc;

void main() {
    uint channel = gl_GlobalInvocationID.x;
    if (channel >= pc.channel_count) return;

    if (pc.has_input != 0u) {
        uint count = min(input_counters[channel].x, channels[channel].capacity);
        input_counters[channel].z = count;
        uint spawn_total = count * channels[channel].spawn_count;
        input_indirect[channel] = uvec4(
            (spawn_total + pc.consumer_workgroup_size - 1u) / pc.consumer_workgroup_size, 1u, 1u, 0u);
    }

    output_counters[channel] = uvec4(0u);
    output_indirect[channel] = uvec4(0u, 1u, 1u, 0u);
}
)glsl";
    return Source;
}

std::string_view GpuParticleEventShaderSource::Allocate() noexcept
{
    static constexpr std::string_view Source = R"glsl(#version 450
layout(local_size_x = 256) in;

struct EventChannel {
    uint record_base_words;
    uint record_stride_words;
    uint capacity;
    uint source_emitter_index;
    uint target_emitter_index;
    uint event_type_index;
    uint payload_stride_words;
    uint spawn_count;
    uint spawn_base_indices;
    uint target_capacity;
    uint reserved0;
    uint reserved1;
};
layout(std430, set = 0, binding = 0) readonly buffer ChannelTable { EventChannel channels[]; };
layout(std430, set = 0, binding = 1) buffer InputCounters { uvec4 input_counters[]; };
layout(std430, set = 0, binding = 2) buffer SpawnIndices { uint spawn_indices[]; };
layout(std430, set = 0, binding = 3) buffer TargetFreeList { uint target_free_slots[]; };
layout(std430, set = 0, binding = 4) buffer TargetCounters {
    uint target_free_count;
    uint target_visible_count;
    uint target_dropped_count;
    uint target_reserved_count;
};
layout(push_constant) uniform EventAllocateConstants {
    uint channel_index;
    uint reserved0;
    uint reserved1;
    uint reserved2;
} pc;

const uint INX_INVALID_INDEX = 0xffffffffu;

uint pop_target_free(uint target_capacity) {
    uint observed = atomicAdd(target_free_count, 0u);
    while (observed > 0u) {
        uint prior = atomicCompSwap(target_free_count, observed, observed - 1u);
        if (prior == observed) {
            uint index = target_free_slots[observed - 1u];
            if (index < target_capacity) return index;
            atomicAdd(target_free_count, 1u);
            return INX_INVALID_INDEX;
        }
        observed = prior;
    }
    return INX_INVALID_INDEX;
}

void main() {
    EventChannel channel = channels[pc.channel_index];
    uint invocation = gl_GlobalInvocationID.x;
    uint total = input_counters[pc.channel_index].z * channel.spawn_count;
    if (invocation >= total) return;
    uint destination = channel.spawn_base_indices + invocation;
    uint particle_index = pop_target_free(channel.target_capacity);
    spawn_indices[destination] = particle_index;
    if (particle_index == INX_INVALID_INDEX)
        atomicAdd(input_counters[pc.channel_index].w, 1u);
}
)glsl";
    return Source;
}

ParticleGpuEventDomain::~ParticleGpuEventDomain()
{
    Destroy();
}

bool ParticleGpuEventDomain::Create(rhi::Device &device, const GpuParticleEventDomainDesc &desc,
                                    const GpuParticleEventProgram &program,
                                    const std::vector<GpuParticleEventTargetDesc> &targets)
{
    Destroy();
    if (desc.graphInstanceId == 0 || desc.eventAbiHash == 0 || desc.framesInFlight == 0 ||
        desc.framesInFlight > MaximumPageCount || desc.channels.empty() ||
        desc.channels.size() > std::numeric_limits<uint32_t>::max() || !program.IsValid() || targets.empty()) {
        return false;
    }

    std::unordered_map<uint32_t, const GpuParticleEventTargetDesc *> targetsByIndex;
    targetsByIndex.reserve(targets.size());
    for (const auto &target : targets) {
        if (target.capacity == 0 || !target.freeList.IsValid() || !target.counters.IsValid() ||
            !target.eventInputLayout.IsValid() || !targetsByIndex.emplace(target.emitterIndex, &target).second) {
            return false;
        }
    }

    std::vector<GpuParticleEventChannelRecord> records;
    records.reserve(desc.channels.size());
    uint64_t recordWords = 0;
    uint64_t spawnIndices = 0;
    for (const auto &channel : desc.channels) {
        if (channel.stableEventTypeHash == 0 || channel.capacity == 0 || channel.spawnCount == 0 ||
            channel.payloadStrideWords > std::numeric_limits<uint32_t>::max() - EventHeaderWords) {
            return false;
        }
        const bool duplicate = std::any_of(records.begin(), records.end(), [&](const auto &existing) {
            return existing.sourceEmitterIndex == channel.sourceEmitterIndex &&
                   existing.targetEmitterIndex == channel.targetEmitterIndex &&
                   existing.eventTypeIndex == channel.eventTypeIndex;
        });
        if (duplicate)
            return false;
        const auto target = targetsByIndex.find(channel.targetEmitterIndex);
        if (target == targetsByIndex.end())
            return false;

        const uint32_t strideWords = EventHeaderWords + channel.payloadStrideWords;
        const uint64_t channelWords = static_cast<uint64_t>(strideWords) * channel.capacity;
        const uint64_t nextRecordWords = recordWords + channelWords;
        const uint64_t channelSpawnIndices = static_cast<uint64_t>(channel.capacity) * channel.spawnCount;
        const uint64_t nextSpawnIndices = spawnIndices + channelSpawnIndices;
        if (nextRecordWords < recordWords || nextSpawnIndices < spawnIndices ||
            recordWords > std::numeric_limits<uint32_t>::max() || spawnIndices > std::numeric_limits<uint32_t>::max() ||
            channelSpawnIndices > std::numeric_limits<uint32_t>::max())
            return false;
        records.push_back({static_cast<uint32_t>(recordWords), strideWords, channel.capacity,
                           channel.sourceEmitterIndex, channel.targetEmitterIndex, channel.eventTypeIndex,
                           channel.payloadStrideWords, channel.spawnCount, static_cast<uint32_t>(spawnIndices),
                           target->second->capacity, 0, 0});
        recordWords = nextRecordWords;
        spawnIndices = nextSpawnIndices;
    }
    if (recordWords == 0 || recordWords > std::numeric_limits<uint64_t>::max() / sizeof(uint32_t) ||
        recordWords > std::numeric_limits<uint32_t>::max()) {
        return false;
    }
    if (spawnIndices == 0 || spawnIndices > std::numeric_limits<uint32_t>::max())
        return false;

    uint64_t recordBytes = 0;
    if (!CheckedAdd(0, recordWords * sizeof(uint32_t), recordBytes))
        return false;
    const uint64_t counterBytes = records.size() * sizeof(GpuParticleEventCounter);
    const uint64_t dispatchBytes = records.size() * sizeof(GpuParticleEventDispatchArguments);
    const uint64_t spawnIndexBytes = spawnIndices * sizeof(uint32_t);

    m_device = &device;
    m_graphInstanceId = desc.graphInstanceId;
    m_eventAbiHash = desc.eventAbiHash;
    m_recordBufferBytes = recordBytes;
    m_spawnIndexBufferBytes = spawnIndexBytes;
    m_channelCount = static_cast<uint32_t>(records.size());
    m_targets = targets;
    std::sort(m_targets.begin(), m_targets.end(),
              [](const auto &lhs, const auto &rhs) { return lhs.emitterIndex < rhs.emitterIndex; });
    m_channels = records;

    rhi::BufferDesc tableDesc;
    tableDesc.byteSize = records.size() * sizeof(GpuParticleEventChannelRecord);
    tableDesc.usage = rhi::BufferUsageFlags::Storage;
    tableDesc.memory = rhi::BufferMemory::Upload;
    tableDesc.initialData = records.data();
    tableDesc.initialDataBytes = tableDesc.byteSize;
    m_channelTable = device.CreateBuffer(tableDesc);
    if (!m_channelTable.IsValid()) {
        Destroy();
        return false;
    }

    const uint32_t pageCount = std::max(desc.framesInFlight, MinimumPageCount);
    m_pages.resize(pageCount);
    for (auto &page : m_pages) {
        page.records = device.CreateBuffer({recordBytes, rhi::BufferUsageFlags::Storage});
        page.counters = device.CreateBuffer({counterBytes, rhi::BufferUsageFlags::Storage});
        page.indirectArguments =
            device.CreateBuffer({dispatchBytes, rhi::BufferUsageFlags::Storage | rhi::BufferUsageFlags::Indirect});
        page.spawnIndices = device.CreateBuffer({spawnIndexBytes, rhi::BufferUsageFlags::Storage});
        if (!page.IsValid()) {
            Destroy();
            return false;
        }
    }

    rhi::BindingLayoutDesc layoutDesc;
    for (uint32_t binding = 0; binding < 5; ++binding)
        layoutDesc.entries[binding] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entryCount = 5;
    m_prepareLayout = device.CreateBindingLayout(layoutDesc);
    if (!m_prepareLayout.IsValid()) {
        Destroy();
        return false;
    }

    m_prepareGroups.resize(pageCount);
    for (uint32_t pageIndex = 0; pageIndex < pageCount; ++pageIndex) {
        const uint32_t previousPageIndex = (pageIndex + pageCount - 1) % pageCount;
        const auto &input = m_pages[previousPageIndex];
        const auto &output = m_pages[pageIndex];
        const std::array<rhi::BufferHandle, 5> buffers = {m_channelTable, input.counters, input.indirectArguments,
                                                          output.counters, output.indirectArguments};
        rhi::BindGroupDesc groupDesc;
        groupDesc.layout = m_prepareLayout;
        for (uint32_t binding = 0; binding < buffers.size(); ++binding)
            groupDesc.buffers[binding] = {binding, rhi::BindingType::StorageBuffer, buffers[binding], 0, 0};
        groupDesc.bufferCount = static_cast<uint32_t>(buffers.size());
        m_prepareGroups[pageIndex] = device.CreateBindGroup(groupDesc);
        if (!m_prepareGroups[pageIndex].IsValid()) {
            Destroy();
            return false;
        }
    }

    rhi::BindingLayoutDesc allocateLayoutDesc;
    for (uint32_t binding = 0; binding < 5; ++binding)
        allocateLayoutDesc.entries[binding] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    allocateLayoutDesc.entryCount = 5;
    m_allocateLayout = device.CreateBindingLayout(allocateLayoutDesc);
    if (!m_allocateLayout.IsValid()) {
        Destroy();
        return false;
    }

    const size_t routePageCount = static_cast<size_t>(pageCount) * records.size();
    m_allocateGroups.resize(routePageCount);
    m_eventInputGroups.resize(routePageCount);
    for (uint32_t pageIndex = 0; pageIndex < pageCount; ++pageIndex) {
        const auto &page = m_pages[pageIndex];
        for (uint32_t channelIndex = 0; channelIndex < m_channelCount; ++channelIndex) {
            const size_t groupIndex = static_cast<size_t>(pageIndex) * m_channelCount + channelIndex;
            const auto target = targetsByIndex.at(records[channelIndex].targetEmitterIndex);

            rhi::BindGroupDesc allocateGroupDesc;
            allocateGroupDesc.layout = m_allocateLayout;
            const std::array<rhi::BufferHandle, 5> allocateBuffers = {
                m_channelTable, page.counters, page.spawnIndices, target->freeList, target->counters,
            };
            for (uint32_t binding = 0; binding < allocateBuffers.size(); ++binding) {
                allocateGroupDesc.buffers[binding] = {binding, rhi::BindingType::StorageBuffer,
                                                      allocateBuffers[binding], 0, 0};
            }
            allocateGroupDesc.bufferCount = static_cast<uint32_t>(allocateBuffers.size());
            m_allocateGroups[groupIndex] = device.CreateBindGroup(allocateGroupDesc);

            rhi::BindGroupDesc eventInputGroupDesc;
            eventInputGroupDesc.layout = target->eventInputLayout;
            const std::array<rhi::BufferHandle, 4> eventInputBuffers = {
                m_channelTable,
                page.records,
                page.counters,
                page.spawnIndices,
            };
            for (uint32_t binding = 0; binding < eventInputBuffers.size(); ++binding) {
                eventInputGroupDesc.buffers[binding] = {binding, rhi::BindingType::StorageBuffer,
                                                        eventInputBuffers[binding], 0, 0};
            }
            eventInputGroupDesc.bufferCount = static_cast<uint32_t>(eventInputBuffers.size());
            m_eventInputGroups[groupIndex] = device.CreateBindGroup(eventInputGroupDesc);
            if (!m_allocateGroups[groupIndex].IsValid() || !m_eventInputGroups[groupIndex].IsValid()) {
                Destroy();
                return false;
            }
        }
    }

    const auto prepareShader = device.CreateShaderModule({program.prepareWords, program.prepareWordCount});
    if (!prepareShader.IsValid()) {
        Destroy();
        return false;
    }
    rhi::ComputePipelineDesc pipelineDesc;
    pipelineDesc.computeShader = prepareShader;
    pipelineDesc.bindingLayouts[0] = m_prepareLayout;
    pipelineDesc.bindingLayoutCount = 1;
    pipelineDesc.pushConstantBytes = sizeof(GpuParticleEventPrepareConstants);
    m_preparePipeline = device.CreateComputePipeline(pipelineDesc);
    device.Release(prepareShader);
    if (!m_preparePipeline.IsValid()) {
        Destroy();
        return false;
    }

    const auto allocateShader = device.CreateShaderModule({program.allocateWords, program.allocateWordCount});
    if (!allocateShader.IsValid()) {
        Destroy();
        return false;
    }
    rhi::ComputePipelineDesc allocatePipelineDesc;
    allocatePipelineDesc.computeShader = allocateShader;
    allocatePipelineDesc.bindingLayouts[0] = m_allocateLayout;
    allocatePipelineDesc.bindingLayoutCount = 1;
    allocatePipelineDesc.pushConstantBytes = sizeof(GpuParticleEventAllocateConstants);
    m_allocatePipeline = device.CreateComputePipeline(allocatePipelineDesc);
    device.Release(allocateShader);
    if (!m_allocatePipeline.IsValid()) {
        Destroy();
        return false;
    }
    return true;
}

void ParticleGpuEventDomain::Destroy() noexcept
{
    if (m_device) {
        m_device->Release(m_allocatePipeline);
        for (auto &group : m_eventInputGroups)
            m_device->Release(group);
        for (auto &group : m_allocateGroups)
            m_device->Release(group);
        m_device->Release(m_allocateLayout);
        m_device->Release(m_preparePipeline);
        for (auto &group : m_prepareGroups)
            m_device->Release(group);
        m_device->Release(m_prepareLayout);
        for (auto &page : m_pages) {
            m_device->Release(page.spawnIndices);
            m_device->Release(page.indirectArguments);
            m_device->Release(page.counters);
            m_device->Release(page.records);
        }
        m_device->Release(m_channelTable);
    }
    m_pages.clear();
    m_prepareGroups.clear();
    m_allocateGroups.clear();
    m_eventInputGroups.clear();
    m_allocatePipeline = {};
    m_allocateLayout = {};
    m_preparePipeline = {};
    m_prepareLayout = {};
    m_hasPreparedPage = false;
    m_hasInputForCurrentStep = false;
    m_currentReadPageIndex = 0;
    m_nextPrepareEpoch = 0;
    m_channelTable = {};
    m_targets.clear();
    m_channels.clear();
    m_channelCount = 0;
    m_recordBufferBytes = 0;
    m_spawnIndexBufferBytes = 0;
    m_eventAbiHash = 0;
    m_graphInstanceId = 0;
    m_device = nullptr;
}

bool ParticleGpuEventDomain::IsValid() const noexcept
{
    const size_t routePageCount = m_pages.size() * m_channelCount;
    return m_device && m_graphInstanceId != 0 && m_eventAbiHash != 0 && m_channelCount > 0 && m_recordBufferBytes > 0 &&
           m_spawnIndexBufferBytes > 0 && m_channelTable.IsValid() && m_pages.size() >= MinimumPageCount &&
           m_prepareLayout.IsValid() && m_preparePipeline.IsValid() && m_allocateLayout.IsValid() &&
           m_allocatePipeline.IsValid() && m_prepareGroups.size() == m_pages.size() &&
           m_allocateGroups.size() == routePageCount && m_eventInputGroups.size() == routePageCount &&
           std::all_of(m_prepareGroups.begin(), m_prepareGroups.end(),
                       [](const auto &group) { return group.IsValid(); }) &&
           std::all_of(m_allocateGroups.begin(), m_allocateGroups.end(),
                       [](const auto &group) { return group.IsValid(); }) &&
           std::all_of(m_eventInputGroups.begin(), m_eventInputGroups.end(),
                       [](const auto &group) { return group.IsValid(); }) &&
           std::all_of(m_pages.begin(), m_pages.end(), [](const auto &page) { return page.IsValid(); });
}

const GpuParticleEventPage *ParticleGpuEventDomain::Page(uint32_t pageIndex) const noexcept
{
    if (!IsValid() || pageIndex >= m_pages.size())
        return nullptr;
    return &m_pages[pageIndex];
}

void ParticleGpuEventDomain::RecordPrepare(const rhi::ComputeCommandEncoder &encoder)
{
    if (!IsValid() || !encoder.IsValid())
        return;
    const uint32_t pageIndex = static_cast<uint32_t>(m_nextPrepareEpoch % m_pages.size());
    m_currentReadPageIndex = (pageIndex + static_cast<uint32_t>(m_pages.size()) - 1u) % m_pages.size();
    m_hasInputForCurrentStep = m_hasPreparedPage;
    GpuParticleEventPrepareConstants constants;
    constants.channelCount = m_channelCount;
    constants.hasInput = m_hasInputForCurrentStep ? 1u : 0u;
    encoder.BindPipeline(m_preparePipeline);
    encoder.BindGroup(m_preparePipeline, 0, m_prepareGroups[pageIndex]);
    encoder.PushConstants(m_preparePipeline, sizeof(constants), &constants);
    encoder.Dispatch((m_channelCount + 63u) / 64u, 1, 1);
    m_hasPreparedPage = true;
    ++m_nextPrepareEpoch;
}

void ParticleGpuEventDomain::RecordAllocate(const rhi::ComputeCommandEncoder &encoder, uint32_t channelIndex) const
{
    if (!IsValid() || !encoder.IsValid() || !m_hasInputForCurrentStep || channelIndex >= m_channelCount)
        return;
    const size_t groupIndex = static_cast<size_t>(m_currentReadPageIndex) * m_channelCount + channelIndex;
    GpuParticleEventAllocateConstants constants;
    constants.channelIndex = channelIndex;
    encoder.BindPipeline(m_allocatePipeline);
    encoder.BindGroup(m_allocatePipeline, 0, m_allocateGroups[groupIndex]);
    encoder.PushConstants(m_allocatePipeline, sizeof(constants), &constants);
    encoder.DispatchIndirect(m_pages[m_currentReadPageIndex].indirectArguments,
                             static_cast<uint64_t>(channelIndex) * sizeof(GpuParticleEventDispatchArguments));
}

rhi::BindGroupHandle ParticleGpuEventDomain::CurrentEventInputGroup(uint32_t channelIndex) const noexcept
{
    if (!IsValid() || !m_hasInputForCurrentStep || channelIndex >= m_channelCount)
        return {};
    return m_eventInputGroups[static_cast<size_t>(m_currentReadPageIndex) * m_channelCount + channelIndex];
}

rhi::BufferHandle ParticleGpuEventDomain::CurrentIndirectArguments() const noexcept
{
    if (!IsValid() || !m_hasInputForCurrentStep)
        return {};
    return m_pages[m_currentReadPageIndex].indirectArguments;
}

bool ParticleGpuEventDomain::MatchesTargets(const std::vector<GpuParticleEventTargetDesc> &targets) const noexcept
{
    if (!IsValid() || targets.size() != m_targets.size())
        return false;
    auto ordered = targets;
    std::sort(ordered.begin(), ordered.end(),
              [](const auto &lhs, const auto &rhs) { return lhs.emitterIndex < rhs.emitterIndex; });
    for (size_t index = 0; index < ordered.size(); ++index) {
        const auto &lhs = ordered[index];
        const auto &rhs = m_targets[index];
        if (lhs.emitterIndex != rhs.emitterIndex || lhs.capacity != rhs.capacity || lhs.freeList != rhs.freeList ||
            lhs.counters != rhs.counters || lhs.eventInputLayout != rhs.eventInputLayout) {
            return false;
        }
    }
    return true;
}

uint32_t ParticleGpuEventDomain::ChannelTargetEmitterIndex(uint32_t channelIndex) const noexcept
{
    if (channelIndex >= m_channels.size())
        return std::numeric_limits<uint32_t>::max();
    return m_channels[channelIndex].targetEmitterIndex;
}

} // namespace infernux::particle
