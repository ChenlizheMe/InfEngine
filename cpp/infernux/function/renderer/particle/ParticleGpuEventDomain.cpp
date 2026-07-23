#include "ParticleGpuEventDomain.h"

#include <algorithm>
#include <array>
#include <limits>
#include <string>
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

bool IsShaderBytecodeValid(const GpuParticleEventProgram &program) noexcept
{
    return program.words && program.wordCount >= 5 && program.words[0] == 0x07230203u;
}

} // namespace

bool GpuParticleEventProgram::IsValid() const noexcept
{
    return IsShaderBytecodeValid(*this);
}

bool GpuParticleEventProgramStorage::Assign(const GpuParticleEventProgram &program)
{
    if (!program.IsValid())
        return false;
    shader.assign(program.words, program.words + program.wordCount);
    return true;
}

bool GpuParticleEventProgramStorage::IsValid() const noexcept
{
    return View().IsValid();
}

GpuParticleEventProgram GpuParticleEventProgramStorage::View() const noexcept
{
    return {shader.data(), shader.size()};
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
    uint reserved;
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
        input_indirect[channel] = uvec4(
            (count + pc.consumer_workgroup_size - 1u) / pc.consumer_workgroup_size, 1u, 1u, 0u);
    }

    output_counters[channel] = uvec4(0u);
    output_indirect[channel] = uvec4(0u, 1u, 1u, 0u);
}
)glsl";
    return Source;
}

ParticleGpuEventDomain::~ParticleGpuEventDomain()
{
    Destroy();
}

bool ParticleGpuEventDomain::Create(rhi::Device &device, const GpuParticleEventDomainDesc &desc,
                                    const GpuParticleEventProgram &program)
{
    Destroy();
    if (desc.graphInstanceId == 0 || desc.eventAbiHash == 0 || desc.framesInFlight == 0 ||
        desc.framesInFlight > MaximumPageCount || desc.channels.empty() ||
        desc.channels.size() > std::numeric_limits<uint32_t>::max() || !program.IsValid()) {
        return false;
    }

    std::vector<GpuParticleEventChannelRecord> records;
    records.reserve(desc.channels.size());
    uint64_t recordWords = 0;
    for (const auto &channel : desc.channels) {
        if (channel.stableEventTypeHash == 0 || channel.capacity == 0 ||
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

        const uint32_t strideWords = EventHeaderWords + channel.payloadStrideWords;
        const uint64_t channelWords = static_cast<uint64_t>(strideWords) * channel.capacity;
        const uint64_t nextRecordWords = recordWords + channelWords;
        if (nextRecordWords < recordWords || recordWords > std::numeric_limits<uint32_t>::max())
            return false;
        records.push_back({static_cast<uint32_t>(recordWords), strideWords, channel.capacity,
                           channel.sourceEmitterIndex, channel.targetEmitterIndex, channel.eventTypeIndex,
                           channel.payloadStrideWords, 0});
        recordWords = nextRecordWords;
    }
    if (recordWords == 0 || recordWords > std::numeric_limits<uint64_t>::max() / sizeof(uint32_t) ||
        recordWords > std::numeric_limits<uint32_t>::max()) {
        return false;
    }

    uint64_t recordBytes = 0;
    if (!CheckedAdd(0, recordWords * sizeof(uint32_t), recordBytes))
        return false;
    const uint64_t counterBytes = records.size() * sizeof(GpuParticleEventCounter);
    const uint64_t dispatchBytes = records.size() * sizeof(GpuParticleEventDispatchArguments);

    m_device = &device;
    m_graphInstanceId = desc.graphInstanceId;
    m_eventAbiHash = desc.eventAbiHash;
    m_recordBufferBytes = recordBytes;
    m_channelCount = static_cast<uint32_t>(records.size());

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

    const auto shader = device.CreateShaderModule({program.words, program.wordCount});
    if (!shader.IsValid()) {
        Destroy();
        return false;
    }
    rhi::ComputePipelineDesc pipelineDesc;
    pipelineDesc.computeShader = shader;
    pipelineDesc.bindingLayouts[0] = m_prepareLayout;
    pipelineDesc.bindingLayoutCount = 1;
    pipelineDesc.pushConstantBytes = sizeof(GpuParticleEventPrepareConstants);
    m_preparePipeline = device.CreateComputePipeline(pipelineDesc);
    device.Release(shader);
    if (!m_preparePipeline.IsValid()) {
        Destroy();
        return false;
    }
    return true;
}

void ParticleGpuEventDomain::Destroy() noexcept
{
    if (m_device) {
        m_device->Release(m_preparePipeline);
        for (auto &group : m_prepareGroups)
            m_device->Release(group);
        m_device->Release(m_prepareLayout);
        for (auto &page : m_pages) {
            m_device->Release(page.indirectArguments);
            m_device->Release(page.counters);
            m_device->Release(page.records);
        }
        m_device->Release(m_channelTable);
    }
    m_pages.clear();
    m_prepareGroups.clear();
    m_preparePipeline = {};
    m_prepareLayout = {};
    m_hasPreparedPage = false;
    m_nextPrepareEpoch = 0;
    m_channelTable = {};
    m_channelCount = 0;
    m_recordBufferBytes = 0;
    m_eventAbiHash = 0;
    m_graphInstanceId = 0;
    m_device = nullptr;
}

bool ParticleGpuEventDomain::IsValid() const noexcept
{
    return m_device && m_graphInstanceId != 0 && m_eventAbiHash != 0 && m_channelCount > 0 && m_recordBufferBytes > 0 &&
           m_channelTable.IsValid() && m_pages.size() >= MinimumPageCount && m_prepareLayout.IsValid() &&
           m_preparePipeline.IsValid() && m_prepareGroups.size() == m_pages.size() &&
           std::all_of(m_prepareGroups.begin(), m_prepareGroups.end(),
                       [](const auto &group) { return group.IsValid(); }) &&
           std::all_of(m_pages.begin(), m_pages.end(), [](const auto &page) { return page.IsValid(); });
}

const GpuParticleEventPage *ParticleGpuEventDomain::WritePage(uint64_t simulationStep) const noexcept
{
    if (!IsValid())
        return nullptr;
    return &m_pages[simulationStep % m_pages.size()];
}

const GpuParticleEventPage *ParticleGpuEventDomain::ReadPage(uint64_t simulationStep) const noexcept
{
    if (!IsValid() || simulationStep == 0)
        return nullptr;
    return &m_pages[(simulationStep - 1) % m_pages.size()];
}

void ParticleGpuEventDomain::RecordPrepare(const rhi::ComputeCommandEncoder &encoder)
{
    if (!IsValid() || !encoder.IsValid())
        return;
    const uint32_t pageIndex = static_cast<uint32_t>(m_nextPrepareEpoch % m_pages.size());
    GpuParticleEventPrepareConstants constants;
    constants.channelCount = m_channelCount;
    constants.hasInput = m_hasPreparedPage ? 1u : 0u;
    encoder.BindPipeline(m_preparePipeline);
    encoder.BindGroup(m_preparePipeline, 0, m_prepareGroups[pageIndex]);
    encoder.PushConstants(m_preparePipeline, sizeof(constants), &constants);
    encoder.Dispatch((m_channelCount + 63u) / 64u, 1, 1);
    m_hasPreparedPage = true;
    ++m_nextPrepareEpoch;
}

} // namespace infernux::particle
