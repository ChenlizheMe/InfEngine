#include "ParticleGpuContactRuntime.h"

#include <algorithm>
#include <array>
#include <limits>

namespace infernux::particle
{
namespace
{

bool CheckedMultiply(uint64_t lhs, uint64_t rhs, uint64_t &result) noexcept
{
    if (lhs == 0 || rhs == 0) {
        result = 0;
        return true;
    }
    if (lhs > std::numeric_limits<uint64_t>::max() / rhs)
        return false;
    result = lhs * rhs;
    return true;
}

rhi::BufferHandle CreateStorageBuffer(rhi::Device &device, uint64_t bytes, bool diagnostics = false,
                                      bool indirect = false)
{
    rhi::BufferDesc desc;
    desc.byteSize = bytes;
    desc.usage = rhi::BufferUsageFlags::Storage;
    if (diagnostics)
        desc.usage = desc.usage | rhi::BufferUsageFlags::TransferSource;
    if (indirect)
        desc.usage = desc.usage | rhi::BufferUsageFlags::Indirect;
    // Particle compute is allowed to execute on the graphics queue as a
    // fallback. Diagnostics can additionally copy selected buffers.
    desc.queueAccess = rhi::QueueAccessFlags::Graphics | rhi::QueueAccessFlags::Compute;
    if (diagnostics)
        desc.queueAccess = desc.queueAccess | rhi::QueueAccessFlags::Transfer;
    return device.CreateBuffer(desc);
}

uint32_t NextPowerOfTwo(uint64_t value) noexcept
{
    uint64_t result = 1;
    while (result < value && result < (uint64_t{1} << 31u))
        result <<= 1u;
    return result >= value ? static_cast<uint32_t>(result) : 0u;
}

} // namespace

struct ParticleGpuContactRuntime::ResidentStorage
{
    ~ResidentStorage()
    {
        if (!device)
            return;
        device->Release(resources.continuationJoinStates);
        device->Release(resources.continuationSnapshots);
        device->Release(resources.counters);
        device->Release(resources.dispatchIndirect);
        device->Release(resources.workItems);
        device->Release(resources.particleStates);
        device->Release(resources.particleRecordIndices);
        device->Release(resources.hashSlots);
        device->Release(resources.contactRecords);
    }

    rhi::Device *device = nullptr;
    GpuParticleContactResources resources;
    uint32_t particleCapacity = 0;
    uint32_t contactsPerParticle = 0;
    uint32_t contactRecordCapacity = 0;
    uint32_t contactHashCapacity = 0;
    uint32_t workItemCapacity = 0;
    uint32_t continuationSnapshotCapacity = 0;
    uint32_t continuationJoinCapacity = 0;
    uint64_t contactBytes = 0;
    uint64_t hashBytes = 0;
    uint64_t particleIndexBytes = 0;
    uint64_t particleStateBytes = 0;
    uint64_t workItemBytes = 0;
    uint64_t continuationSnapshotBytes = 0;
    uint64_t continuationJoinBytes = 0;
};

bool GpuParticleContactResources::IsValid() const noexcept
{
    return contactRecords.IsValid() && hashSlots.IsValid() && particleRecordIndices.IsValid() &&
           particleStates.IsValid() && workItems.IsValid() && dispatchIndirect.IsValid() && counters.IsValid() &&
           continuationSnapshots.IsValid() && continuationJoinStates.IsValid();
}

ParticleGpuContactRuntime::ParticleGpuContactRuntime() = default;

ParticleGpuContactRuntime::~ParticleGpuContactRuntime()
{
    Destroy();
}

bool ParticleGpuContactRuntime::Create(rhi::Device &device, uint32_t particleCapacity,
                                       uint32_t continuationSnapshotCapacity)
{
    return CreateInternal(device, particleCapacity, continuationSnapshotCapacity, {});
}

bool ParticleGpuContactRuntime::CreateCompatible(rhi::Device &device, uint32_t particleCapacity,
                                                 uint32_t continuationSnapshotCapacity,
                                                 const ParticleGpuContactRuntime &previous)
{
    if (!previous.IsValid() || previous.m_device != &device || previous.m_storage->particleCapacity != particleCapacity)
        return false;
    if (previous.m_storage->continuationSnapshotCapacity != std::max(continuationSnapshotCapacity, 1u))
        return false;
    if (previous.m_storage->contactsPerParticle != DefaultContactsPerParticle)
        return false;
    return CreateInternal(device, particleCapacity, continuationSnapshotCapacity, previous.m_storage);
}

bool ParticleGpuContactRuntime::CreateInternal(rhi::Device &device, uint32_t particleCapacity,
                                               uint32_t continuationSnapshotCapacity,
                                               std::shared_ptr<ResidentStorage> storage)
{
    Destroy();
    if (particleCapacity == 0 || particleCapacity > MaximumParticleCapacity)
        return false;
    constexpr uint32_t contactsPerParticle = DefaultContactsPerParticle;
    continuationSnapshotCapacity = std::max(continuationSnapshotCapacity, 1u);

    m_device = &device;
    if (storage) {
        if (storage->device != &device || storage->particleCapacity != particleCapacity ||
            storage->contactsPerParticle != contactsPerParticle ||
            storage->continuationSnapshotCapacity != continuationSnapshotCapacity) {
            Destroy();
            return false;
        }
        m_storage = std::move(storage);
    } else {
        uint64_t requestedContactCount = 0;
        uint64_t contactBytes = 0;
        uint64_t hashBytes = 0;
        uint64_t particleIndexBytes = 0;
        uint64_t particleStateBytes = 0;
        uint64_t workItemBytes = 0;
        uint64_t continuationSnapshotBytes = 0;
        uint64_t continuationJoinBytes = 0;
        if (!CheckedMultiply(particleCapacity, contactsPerParticle, requestedContactCount)) {
            Destroy();
            return false;
        }
        const uint32_t contactRecordCapacity =
            static_cast<uint32_t>(std::min<uint64_t>(requestedContactCount, DefaultContactRecordBudget));
        const uint32_t contactHashCapacity = NextPowerOfTwo(uint64_t(contactRecordCapacity) * 2u);
        const uint32_t workItemCapacity = contactRecordCapacity * 2u;
        const uint32_t continuationJoinCapacity = NextPowerOfTwo(uint64_t(continuationSnapshotCapacity) * 2u);
        if (contactRecordCapacity == 0 || contactHashCapacity == 0 || continuationJoinCapacity == 0 ||
            !CheckedMultiply(uint64_t(contactRecordCapacity) * 2u, sizeof(GpuParticleContactRecord), contactBytes) ||
            !CheckedMultiply(uint64_t(contactHashCapacity) * 2u, sizeof(GpuParticleContactHashSlot), hashBytes) ||
            !CheckedMultiply(uint64_t(particleCapacity) * contactsPerParticle * 2u, sizeof(uint32_t),
                             particleIndexBytes) ||
            !CheckedMultiply(particleCapacity, sizeof(std::array<uint32_t, 4>), particleStateBytes) ||
            !CheckedMultiply(workItemCapacity, sizeof(GpuParticleContactWorkItem), workItemBytes) ||
            !CheckedMultiply(continuationSnapshotCapacity, sizeof(GpuParticleContactRecord),
                             continuationSnapshotBytes) ||
            !CheckedMultiply(continuationJoinCapacity, sizeof(GpuParticleContactJoinState), continuationJoinBytes)) {
            Destroy();
            return false;
        }
        auto created = std::make_shared<ResidentStorage>();
        created->device = &device;
        created->particleCapacity = particleCapacity;
        created->contactsPerParticle = contactsPerParticle;
        created->contactRecordCapacity = contactRecordCapacity;
        created->contactHashCapacity = contactHashCapacity;
        created->workItemCapacity = workItemCapacity;
        created->continuationSnapshotCapacity = continuationSnapshotCapacity;
        created->continuationJoinCapacity = continuationJoinCapacity;
        created->contactBytes = contactBytes;
        created->hashBytes = hashBytes;
        created->particleIndexBytes = particleIndexBytes;
        created->particleStateBytes = particleStateBytes;
        created->workItemBytes = workItemBytes;
        created->continuationSnapshotBytes = continuationSnapshotBytes;
        created->continuationJoinBytes = continuationJoinBytes;
        created->resources.contactRecords = CreateStorageBuffer(device, contactBytes);
        created->resources.hashSlots = CreateStorageBuffer(device, hashBytes);
        created->resources.particleRecordIndices = CreateStorageBuffer(device, particleIndexBytes);
        created->resources.particleStates = CreateStorageBuffer(device, particleStateBytes);
        created->resources.workItems = CreateStorageBuffer(device, workItemBytes);
        created->resources.dispatchIndirect = CreateStorageBuffer(device, sizeof(std::array<uint32_t, 4>), false, true);
        created->resources.counters = CreateStorageBuffer(device, sizeof(GpuParticleContactCounters), true);
        created->resources.continuationSnapshots = CreateStorageBuffer(device, continuationSnapshotBytes);
        created->resources.continuationJoinStates = CreateStorageBuffer(device, continuationJoinBytes);
        if (!created->resources.IsValid()) {
            Destroy();
            return false;
        }
        m_storage = std::move(created);
    }

    rhi::BindingLayoutDesc layoutDesc;
    for (uint32_t binding = 0; binding < 9; ++binding)
        layoutDesc.entries[layoutDesc.entryCount++] = {binding, rhi::BindingType::StorageBuffer,
                                                       rhi::ShaderStage::Compute, 1};
    m_layout = device.CreateBindingLayout(layoutDesc);
    if (!m_layout.IsValid()) {
        Destroy();
        return false;
    }

    const std::array<rhi::BufferHandle, 9> buffers = {
        m_storage->resources.contactRecords,
        m_storage->resources.hashSlots,
        m_storage->resources.particleRecordIndices,
        m_storage->resources.particleStates,
        m_storage->resources.workItems,
        m_storage->resources.dispatchIndirect,
        m_storage->resources.counters,
        m_storage->resources.continuationSnapshots,
        m_storage->resources.continuationJoinStates,
    };
    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_layout;
    for (uint32_t binding = 0; binding < buffers.size(); ++binding)
        groupDesc.buffers[groupDesc.bufferCount++] = {binding, rhi::BindingType::StorageBuffer, buffers[binding]};
    m_group = device.CreateBindGroup(groupDesc);
    if (!m_group.IsValid()) {
        Destroy();
        return false;
    }
    m_resetSerial = 1;
    return true;
}

void ParticleGpuContactRuntime::Destroy() noexcept
{
    if (m_device) {
        m_device->Release(m_group);
        m_device->Release(m_layout);
    }
    m_storage.reset();
    m_device = nullptr;
    m_layout = {};
    m_group = {};
    m_resetSerial = 0;
}

bool ParticleGpuContactRuntime::IsValid() const noexcept
{
    return m_device && m_storage && m_storage->resources.IsValid() && m_layout.IsValid() && m_group.IsValid() &&
           m_resetSerial != 0;
}

bool ParticleGpuContactRuntime::SharesStorageWith(const ParticleGpuContactRuntime &other) const noexcept
{
    return m_storage && m_storage == other.m_storage;
}

const GpuParticleContactResources &ParticleGpuContactRuntime::Resources() const noexcept
{
    static const GpuParticleContactResources Empty;
    return m_storage ? m_storage->resources : Empty;
}

GpuParticleContactTelemetry ParticleGpuContactRuntime::Telemetry() const noexcept
{
    GpuParticleContactTelemetry result;
    if (!m_storage)
        return result;
    result.particleCapacity = m_storage->particleCapacity;
    result.contactsPerParticle = m_storage->contactsPerParticle;
    result.contactRecordCapacity = m_storage->contactRecordCapacity;
    result.contactHashCapacity = m_storage->contactHashCapacity;
    result.workItemCapacity = m_storage->workItemCapacity;
    result.continuationSnapshotCapacity = m_storage->continuationSnapshotCapacity;
    result.continuationJoinCapacity = m_storage->continuationJoinCapacity;
    result.resetSerial = m_resetSerial;
    result.contactBytes = m_storage->contactBytes;
    result.hashBytes = m_storage->hashBytes;
    result.particleIndexBytes = m_storage->particleIndexBytes;
    result.particleStateBytes = m_storage->particleStateBytes;
    result.workItemBytes = m_storage->workItemBytes;
    result.continuationSnapshotBytes = m_storage->continuationSnapshotBytes;
    result.continuationJoinBytes = m_storage->continuationJoinBytes;
    return result;
}

rhi::BindingLayoutHandle ParticleGpuContactRuntime::Layout() const noexcept
{
    return m_layout;
}

rhi::BindGroupHandle ParticleGpuContactRuntime::Group() const noexcept
{
    return m_group;
}

} // namespace infernux::particle
