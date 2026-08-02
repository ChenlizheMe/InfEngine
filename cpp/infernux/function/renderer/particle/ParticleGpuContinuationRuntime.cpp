#include "ParticleGpuContinuationRuntime.h"

#include <algorithm>
#include <array>
#include <limits>
#include <utility>

namespace infernux::particle
{

namespace
{

bool IsSpirv(const GpuParticleContinuationShader &shader) noexcept
{
    return shader.words && shader.wordCount >= 5 && shader.words[0] == 0x07230203u;
}

bool CheckedMultiply(uint32_t count, uint64_t stride, uint64_t &result) noexcept
{
    if (count == 0 || stride == 0 || uint64_t(count) > std::numeric_limits<uint64_t>::max() / stride)
        return false;
    result = uint64_t(count) * stride;
    return true;
}

std::array<uint32_t, 2> DispatchGrid(uint64_t invocationCount) noexcept
{
    constexpr uint64_t MaxDimension = 65535;
    const uint64_t groupCount =
        std::max<uint64_t>(1, (invocationCount + ParticleGpuContinuationRuntime::WorkgroupSize - 1) /
                                  ParticleGpuContinuationRuntime::WorkgroupSize);
    const uint64_t x = std::min(groupCount, MaxDimension);
    const uint64_t y = (groupCount + x - 1) / x;
    if (y > MaxDimension)
        return {};
    return {static_cast<uint32_t>(x), static_cast<uint32_t>(y)};
}

rhi::BufferHandle CreateGpuBuffer(rhi::Device &device, uint64_t byteSize, rhi::BufferUsageFlags usage)
{
    rhi::BufferDesc desc;
    desc.byteSize = byteSize;
    desc.usage = usage;
    desc.queueAccess = rhi::QueueAccessFlags::Compute;
    return device.CreateBuffer(desc);
}

} // namespace

bool GpuParticleContinuationShader::IsValid() const noexcept
{
    return IsSpirv(*this);
}

bool GpuParticleContinuationProgram::IsValid() const noexcept
{
    return prepare.IsValid() && classify.IsValid() && dispatch.IsValid();
}

bool GpuParticleContinuationResources::IsValid() const noexcept
{
    return records.IsValid() && freeList.IsValid() && readyQueue.IsValid() && activeQueueA.IsValid() &&
           activeQueueB.IsValid() && counters.IsValid() && classifyIndirectArguments.IsValid() &&
           dispatchIndirectArguments.IsValid() && laneSlots.IsValid() && joinStates.IsValid();
}

struct ParticleGpuContinuationRuntime::ResidentStorage
{
    ~ResidentStorage()
    {
        if (!device)
            return;
        device->Release(resources.dispatchIndirectArguments);
        device->Release(resources.classifyIndirectArguments);
        device->Release(resources.joinStates);
        device->Release(resources.laneSlots);
        device->Release(resources.counters);
        device->Release(resources.activeQueueB);
        device->Release(resources.activeQueueA);
        device->Release(resources.readyQueue);
        device->Release(resources.freeList);
        device->Release(resources.records);
    }

    rhi::Device *device = nullptr;
    uint32_t capacity = 0;
    uint32_t particleCapacity = 0;
    uint32_t recordStride = 0;
    uint32_t laneCount = 0;
    uint32_t joinCount = 0;
    uint64_t recordBytes = 0;
    uint64_t queueBytes = 0;
    uint64_t laneSlotBytes = 0;
    uint64_t joinStateBytes = 0;
    GpuParticleContinuationResources resources;
};

struct ParticleGpuContinuationRuntime::ProgramRevision
{
    ~ProgramRevision()
    {
        if (!device)
            return;
        device->Release(dispatchPipeline);
        device->Release(classifyPipeline);
        device->Release(preparePipeline);
        device->Release(group);
        device->Release(layout);
    }

    rhi::Device *device = nullptr;
    rhi::BindingLayoutHandle layout;
    rhi::BindGroupHandle group;
    rhi::BindGroupHandle ownerGroup;
    rhi::BindGroupHandle dataInterfaceGroup;
    rhi::BindGroupHandle vectorFieldGroup;
    rhi::BindGroupHandle emptyGroup;
    rhi::BindGroupHandle collisionSceneGroup;
    rhi::BindGroupHandle contactGroup;
    rhi::ComputePipelineHandle preparePipeline;
    rhi::ComputePipelineHandle classifyPipeline;
    rhi::ComputePipelineHandle dispatchPipeline;
};

ParticleGpuContinuationRuntime::ParticleGpuContinuationRuntime() = default;

ParticleGpuContinuationRuntime::~ParticleGpuContinuationRuntime()
{
    Destroy();
}

bool ParticleGpuContinuationRuntime::Create(rhi::Device &device, const GpuParticleContinuationDesc &desc)
{
    return CreateInternal(device, desc, {}, desc.initialProgramGeneration);
}

bool ParticleGpuContinuationRuntime::CreateCompatible(rhi::Device &device, const GpuParticleContinuationDesc &desc,
                                                      const ParticleGpuContinuationRuntime &previous)
{
    if (!previous.IsValid() || previous.m_device != &device || desc.capacity != previous.Capacity() ||
        desc.particleCapacity != previous.ParticleCapacity())
        return false;
    if (desc.recordStride != previous.RecordStride())
        return false;
    return CreateInternal(device, desc, previous.m_storage, NextGeneration(previous.ProgramGeneration()));
}

bool ParticleGpuContinuationRuntime::CreateInternal(rhi::Device &device, const GpuParticleContinuationDesc &desc,
                                                    std::shared_ptr<ResidentStorage> storage,
                                                    uint32_t programGeneration)
{
    Destroy();
    if (desc.capacity == 0 || desc.capacity > MaximumCapacity || desc.particleCapacity == 0 ||
        desc.particleCapacity > MaximumCapacity || desc.recordStride < sizeof(GpuParticleContinuationRecord) ||
        desc.recordStride > MaximumRecordStride || desc.recordStride % 16 != 0 || desc.laneCount == 0 ||
        desc.laneCount > MaximumLaneCount || desc.joinCount > MaximumJoinCount || programGeneration == 0 ||
        !desc.program.IsValid() || !desc.ownerLayout.IsValid() || !desc.ownerGroup.IsValid() ||
        !desc.dataInterfaceLayout.IsValid() || !desc.dataInterfaceGroup.IsValid() ||
        !desc.vectorFieldLayout.IsValid() || !desc.vectorFieldGroup.IsValid() || !desc.graphSpawnLayout.IsValid() ||
        !desc.emptyLayout.IsValid() || !desc.emptyGroup.IsValid() || !desc.collisionSceneLayout.IsValid() ||
        !desc.collisionSceneGroup.IsValid() || !desc.contactLayout.IsValid() || !desc.contactGroup.IsValid())
        return false;

    m_device = &device;
    m_programGeneration = programGeneration;
    m_resetSerial = 1;
    m_resetPending = true;
    if (storage) {
        if (storage->device != &device || storage->capacity != desc.capacity ||
            storage->particleCapacity != desc.particleCapacity || storage->recordStride != desc.recordStride ||
            storage->laneCount != desc.laneCount || storage->joinCount != desc.joinCount) {
            Destroy();
            return false;
        }
        m_storage = std::move(storage);
    } else {
        uint64_t recordBytes = 0;
        uint64_t queueBytes = 0;
        uint64_t laneSlotBytes = 0;
        uint64_t joinStateBytes = sizeof(GpuParticleContinuationJoinState);
        uint64_t laneSlotCount = 0;
        uint64_t joinStateCount = 0;
        if (!CheckedMultiply(desc.capacity, desc.recordStride, recordBytes) ||
            !CheckedMultiply(desc.capacity, sizeof(uint32_t), queueBytes) ||
            !CheckedMultiply(desc.particleCapacity, uint64_t(desc.laneCount) * sizeof(uint32_t), laneSlotBytes) ||
            (desc.joinCount > 0 &&
             !CheckedMultiply(desc.particleCapacity,
                              uint64_t(desc.joinCount) * sizeof(GpuParticleContinuationJoinState), joinStateBytes))) {
            Destroy();
            return false;
        }
        laneSlotCount = uint64_t(desc.particleCapacity) * desc.laneCount;
        joinStateCount = uint64_t(desc.particleCapacity) * desc.joinCount;
        if (laneSlotCount > std::numeric_limits<uint32_t>::max() ||
            joinStateCount > std::numeric_limits<uint32_t>::max()) {
            Destroy();
            return false;
        }
        auto created = std::make_shared<ResidentStorage>();
        created->device = &device;
        created->capacity = desc.capacity;
        created->particleCapacity = desc.particleCapacity;
        created->recordStride = desc.recordStride;
        created->laneCount = desc.laneCount;
        created->joinCount = desc.joinCount;
        created->recordBytes = recordBytes;
        created->queueBytes = queueBytes;
        created->laneSlotBytes = laneSlotBytes;
        created->joinStateBytes = joinStateBytes;
        const auto storageUsage = rhi::BufferUsageFlags::Storage;
        created->resources.records = CreateGpuBuffer(device, recordBytes, storageUsage);
        created->resources.freeList = CreateGpuBuffer(device, queueBytes, storageUsage);
        created->resources.readyQueue = CreateGpuBuffer(device, queueBytes, storageUsage);
        created->resources.activeQueueA = CreateGpuBuffer(device, queueBytes, storageUsage);
        created->resources.activeQueueB = CreateGpuBuffer(device, queueBytes, storageUsage);
        created->resources.counters = CreateGpuBuffer(device, sizeof(GpuParticleContinuationCounters),
                                                      storageUsage | rhi::BufferUsageFlags::TransferSource);
        created->resources.classifyIndirectArguments =
            CreateGpuBuffer(device, IndirectBufferBytes, storageUsage | rhi::BufferUsageFlags::Indirect);
        created->resources.dispatchIndirectArguments =
            CreateGpuBuffer(device, IndirectBufferBytes, storageUsage | rhi::BufferUsageFlags::Indirect);
        created->resources.laneSlots = CreateGpuBuffer(device, laneSlotBytes, storageUsage);
        created->resources.joinStates = CreateGpuBuffer(device, joinStateBytes, storageUsage);
        if (!created->resources.IsValid()) {
            Destroy();
            return false;
        }
        m_storage = std::move(created);
    }

    auto revision = std::make_unique<ProgramRevision>();
    revision->device = &device;
    revision->ownerGroup = desc.ownerGroup;
    revision->dataInterfaceGroup = desc.dataInterfaceGroup;
    revision->vectorFieldGroup = desc.vectorFieldGroup;
    revision->emptyGroup = desc.emptyGroup;
    revision->collisionSceneGroup = desc.collisionSceneGroup;
    revision->contactGroup = desc.contactGroup;
    rhi::BindingLayoutDesc layoutDesc;
    for (uint32_t binding = 0; binding < 10; ++binding)
        layoutDesc.entries[binding] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entryCount = 10;
    revision->layout = device.CreateBindingLayout(layoutDesc);
    if (!revision->layout.IsValid()) {
        Destroy();
        return false;
    }

    const std::array<rhi::BufferHandle, 10> buffers = {
        m_storage->resources.records,
        m_storage->resources.freeList,
        m_storage->resources.readyQueue,
        m_storage->resources.activeQueueA,
        m_storage->resources.activeQueueB,
        m_storage->resources.counters,
        m_storage->resources.classifyIndirectArguments,
        m_storage->resources.dispatchIndirectArguments,
        m_storage->resources.laneSlots,
        m_storage->resources.joinStates,
    };
    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = revision->layout;
    groupDesc.bufferCount = static_cast<uint32_t>(buffers.size());
    for (uint32_t binding = 0; binding < buffers.size(); ++binding)
        groupDesc.buffers[binding] = {binding, rhi::BindingType::StorageBuffer, buffers[binding]};
    revision->group = device.CreateBindGroup(groupDesc);
    if (!revision->group.IsValid()) {
        Destroy();
        return false;
    }

    const auto createPipeline = [&](const GpuParticleContinuationShader &source, bool executionProgram) {
        const auto shader = device.CreateShaderModule({source.words, source.wordCount});
        if (!shader.IsValid())
            return rhi::ComputePipelineHandle{};
        rhi::ComputePipelineDesc pipelineDesc;
        pipelineDesc.computeShader = shader;
        if (executionProgram) {
            pipelineDesc.bindingLayouts[0] = desc.ownerLayout;
            pipelineDesc.bindingLayouts[1] = desc.dataInterfaceLayout;
            pipelineDesc.bindingLayouts[2] = desc.vectorFieldLayout;
            pipelineDesc.bindingLayouts[3] = desc.graphSpawnLayout;
            pipelineDesc.bindingLayouts[4] = desc.emptyLayout;
            pipelineDesc.bindingLayouts[5] = revision->layout;
            pipelineDesc.bindingLayouts[6] = desc.collisionSceneLayout;
            pipelineDesc.bindingLayouts[7] = desc.contactLayout;
            pipelineDesc.bindingLayoutCount = 8;
        } else {
            pipelineDesc.bindingLayouts[0] = revision->layout;
            pipelineDesc.bindingLayoutCount = 1;
        }
        pipelineDesc.pushConstantBytes = sizeof(GpuParticleContinuationConstants);
        const auto pipeline = device.CreateComputePipeline(pipelineDesc);
        device.Release(shader);
        return pipeline;
    };
    revision->preparePipeline = createPipeline(desc.program.prepare, false);
    revision->classifyPipeline = createPipeline(desc.program.classify, false);
    revision->dispatchPipeline = createPipeline(desc.program.dispatch, true);
    if (!revision->preparePipeline.IsValid() || !revision->classifyPipeline.IsValid() ||
        !revision->dispatchPipeline.IsValid()) {
        Destroy();
        return false;
    }
    m_revision = std::move(revision);
    return true;
}

void ParticleGpuContinuationRuntime::Destroy() noexcept
{
    m_revision.reset();
    m_storage.reset();
    m_device = nullptr;
    m_programGeneration = 0;
    m_resetSerial = 0;
    m_resetPending = true;
    m_prepareRecordCalls = 0;
    m_classifyRecordCalls = 0;
    m_dispatchRecordCalls = 0;
    m_recordEpoch = 0;
    m_classifiedEpoch = 0;
    m_dispatchedEpoch = 0;
}

bool ParticleGpuContinuationRuntime::IsValid() const noexcept
{
    return m_device && m_storage && m_revision && m_storage->capacity > 0 && m_storage->resources.IsValid() &&
           m_revision->layout.IsValid() && m_revision->group.IsValid() && m_revision->ownerGroup.IsValid() &&
           m_revision->collisionSceneGroup.IsValid() && m_revision->contactGroup.IsValid() &&
           m_revision->preparePipeline.IsValid() && m_revision->classifyPipeline.IsValid() &&
           m_revision->dispatchPipeline.IsValid() && m_programGeneration != 0 && m_resetSerial != 0;
}

bool ParticleGpuContinuationRuntime::SharesStorageWith(const ParticleGpuContinuationRuntime &other) const noexcept
{
    return m_storage && m_storage == other.m_storage;
}

uint32_t ParticleGpuContinuationRuntime::Capacity() const noexcept
{
    return m_storage ? m_storage->capacity : 0;
}

uint32_t ParticleGpuContinuationRuntime::RecordStride() const noexcept
{
    return m_storage ? m_storage->recordStride : 0;
}

uint32_t ParticleGpuContinuationRuntime::ParticleCapacity() const noexcept
{
    return m_storage ? m_storage->particleCapacity : 0;
}

uint32_t ParticleGpuContinuationRuntime::LaneCount() const noexcept
{
    return m_storage ? m_storage->laneCount : 0;
}

uint32_t ParticleGpuContinuationRuntime::JoinCount() const noexcept
{
    return m_storage ? m_storage->joinCount : 0;
}

uint32_t ParticleGpuContinuationRuntime::ProgramGeneration() const noexcept
{
    return m_programGeneration;
}

uint32_t ParticleGpuContinuationRuntime::ResetSerial() const noexcept
{
    return m_resetSerial;
}

const GpuParticleContinuationResources &ParticleGpuContinuationRuntime::Resources() const noexcept
{
    static const GpuParticleContinuationResources Empty;
    return m_storage ? m_storage->resources : Empty;
}

GpuParticleContinuationTelemetry ParticleGpuContinuationRuntime::Telemetry() const noexcept
{
    GpuParticleContinuationTelemetry result;
    result.capacity = Capacity();
    result.particleCapacity = ParticleCapacity();
    result.recordStride = RecordStride();
    result.laneCount = LaneCount();
    result.joinCount = JoinCount();
    result.programGeneration = m_programGeneration;
    result.resetSerial = m_resetSerial;
    result.recordBytes = m_storage ? m_storage->recordBytes : 0;
    result.queueBytes = m_storage ? m_storage->queueBytes : 0;
    result.laneSlotBytes = m_storage ? m_storage->laneSlotBytes : 0;
    result.joinStateBytes = m_storage ? m_storage->joinStateBytes : 0;
    result.prepareRecordCalls = m_prepareRecordCalls;
    result.classifyRecordCalls = m_classifyRecordCalls;
    result.dispatchRecordCalls = m_dispatchRecordCalls;
    result.resetPending = m_resetPending;
    result.gpuCounters = m_storage ? m_storage->resources.counters : rhi::BufferHandle{};
    result.gpuClassifyIndirectArguments =
        m_storage ? m_storage->resources.classifyIndirectArguments : rhi::BufferHandle{};
    result.gpuDispatchIndirectArguments =
        m_storage ? m_storage->resources.dispatchIndirectArguments : rhi::BufferHandle{};
    return result;
}

rhi::BindingLayoutHandle ParticleGpuContinuationRuntime::Layout() const noexcept
{
    return m_revision ? m_revision->layout : rhi::BindingLayoutHandle{};
}

rhi::BindGroupHandle ParticleGpuContinuationRuntime::Group() const noexcept
{
    return m_revision ? m_revision->group : rhi::BindGroupHandle{};
}

void ParticleGpuContinuationRuntime::RequestReset() noexcept
{
    if (!IsValid())
        return;
    m_programGeneration = NextGeneration(m_programGeneration);
    m_resetSerial = NextGeneration(m_resetSerial);
    m_resetPending = true;
}

bool ParticleGpuContinuationRuntime::RecordPrepare(const rhi::ComputeCommandEncoder &encoder, uint32_t simulationStep,
                                                   uint64_t elapsedTimeTicks)
{
    if (!IsValid() || !encoder.IsValid())
        return false;
    const auto constants = Constants(simulationStep, elapsedTimeTicks);
    encoder.BindPipeline(m_revision->preparePipeline);
    encoder.BindGroup(m_revision->preparePipeline, 0, m_revision->group);
    encoder.PushConstants(m_revision->preparePipeline, sizeof(constants), &constants);
    uint64_t resetInvocationCount = 1;
    if (m_resetPending) {
        resetInvocationCount = std::max<uint64_t>(Capacity(), uint64_t(ParticleCapacity()) * LaneCount());
        resetInvocationCount = std::max<uint64_t>(resetInvocationCount, uint64_t(ParticleCapacity()) * JoinCount());
    }
    const auto dispatch = DispatchGrid(resetInvocationCount);
    if (dispatch[0] == 0 || dispatch[1] == 0)
        return false;
    encoder.Dispatch(dispatch[0], dispatch[1], 1);
    ++m_recordEpoch;
    if (m_recordEpoch == 0)
        m_recordEpoch = 1;
    m_resetPending = false;
    ++m_prepareRecordCalls;
    return true;
}

bool ParticleGpuContinuationRuntime::RecordClassify(const rhi::ComputeCommandEncoder &encoder, uint32_t simulationStep,
                                                    uint64_t elapsedTimeTicks) const
{
    if (!IsValid() || !encoder.IsValid() || m_recordEpoch == 0 || m_classifiedEpoch == m_recordEpoch)
        return false;
    const auto constants = Constants(simulationStep, elapsedTimeTicks);
    encoder.BindPipeline(m_revision->classifyPipeline);
    encoder.BindGroup(m_revision->classifyPipeline, 0, m_revision->group);
    encoder.PushConstants(m_revision->classifyPipeline, sizeof(constants), &constants);
    encoder.DispatchIndirect(m_storage->resources.classifyIndirectArguments, 0);
    m_classifiedEpoch = m_recordEpoch;
    ++m_classifyRecordCalls;
    return true;
}

bool ParticleGpuContinuationRuntime::RecordDispatch(const rhi::ComputeCommandEncoder &encoder, uint32_t simulationStep,
                                                    uint64_t elapsedTimeTicks, uint32_t systemSeed, float deltaTime,
                                                    rhi::BindGroupHandle graphSpawnGroup) const
{
    if (!IsValid() || !encoder.IsValid() || !graphSpawnGroup.IsValid() || m_classifiedEpoch != m_recordEpoch ||
        m_dispatchedEpoch == m_recordEpoch)
        return false;
    const auto constants = Constants(simulationStep, elapsedTimeTicks, systemSeed, deltaTime);
    encoder.BindPipeline(m_revision->dispatchPipeline);
    encoder.BindGroup(m_revision->dispatchPipeline, 0, m_revision->ownerGroup);
    encoder.BindGroup(m_revision->dispatchPipeline, 1, m_revision->dataInterfaceGroup);
    encoder.BindGroup(m_revision->dispatchPipeline, 2, m_revision->vectorFieldGroup);
    encoder.BindGroup(m_revision->dispatchPipeline, 3, graphSpawnGroup);
    encoder.BindGroup(m_revision->dispatchPipeline, 4, m_revision->emptyGroup);
    encoder.BindGroup(m_revision->dispatchPipeline, 5, m_revision->group);
    encoder.BindGroup(m_revision->dispatchPipeline, 6, m_revision->collisionSceneGroup);
    encoder.BindGroup(m_revision->dispatchPipeline, 7, m_revision->contactGroup);
    encoder.PushConstants(m_revision->dispatchPipeline, sizeof(constants), &constants);
    encoder.DispatchIndirect(m_storage->resources.dispatchIndirectArguments, 0);
    m_dispatchedEpoch = m_recordEpoch;
    ++m_dispatchRecordCalls;
    return true;
}

GpuParticleContinuationConstants ParticleGpuContinuationRuntime::Constants(uint32_t simulationStep,
                                                                           uint64_t elapsedTimeTicks,
                                                                           uint32_t systemSeed,
                                                                           float deltaTime) const noexcept
{
    GpuParticleContinuationConstants result;
    result.capacity = Capacity();
    result.particleCapacity = ParticleCapacity();
    result.laneCount = LaneCount();
    result.joinCount = JoinCount();
    result.programGeneration = m_programGeneration;
    result.simulationStep = simulationStep;
    result.resetSerial = m_resetSerial;
    result.resetRequested = m_resetPending ? 1u : 0u;
    result.elapsedTimeLow = static_cast<uint32_t>(elapsedTimeTicks);
    result.elapsedTimeHigh = static_cast<uint32_t>(elapsedTimeTicks >> 32u);
    result.recordStrideWords = RecordStride() / sizeof(uint32_t);
    result.systemSeed = systemSeed;
    result.deltaTime = deltaTime;
    return result;
}

uint32_t ParticleGpuContinuationRuntime::NextGeneration(uint32_t value) noexcept
{
    ++value;
    return value == 0 ? 1 : value;
}

} // namespace infernux::particle
