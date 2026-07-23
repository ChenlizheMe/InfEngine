#include "ParticleGpuEventDomain.h"

#include <algorithm>
#include <limits>

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

} // namespace

ParticleGpuEventDomain::~ParticleGpuEventDomain()
{
    Destroy();
}

bool ParticleGpuEventDomain::Create(rhi::Device &device, const GpuParticleEventDomainDesc &desc)
{
    Destroy();
    if (desc.graphInstanceId == 0 || desc.eventAbiHash == 0 || desc.framesInFlight == 0 ||
        desc.framesInFlight > MaximumPageCount || desc.channels.empty() ||
        desc.channels.size() > std::numeric_limits<uint32_t>::max()) {
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
    return true;
}

void ParticleGpuEventDomain::Destroy() noexcept
{
    if (m_device) {
        for (auto &page : m_pages) {
            m_device->Release(page.indirectArguments);
            m_device->Release(page.counters);
            m_device->Release(page.records);
        }
        m_device->Release(m_channelTable);
    }
    m_pages.clear();
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
           m_channelTable.IsValid() && m_pages.size() >= MinimumPageCount &&
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

} // namespace infernux::particle
