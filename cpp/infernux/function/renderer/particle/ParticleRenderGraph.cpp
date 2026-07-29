#include "ParticleRenderGraph.h"

#include <algorithm>
#include <cmath>
#include <limits>

namespace infernux::particle
{

namespace
{

constexpr uint64_t CounterBufferBytes = 16;
constexpr uint64_t IndirectBufferBytes = 16;

std::string StageName(const std::string &prefix, const char *stage)
{
    return prefix + "/" + stage;
}

bool IsShaderBytecodeValid(const ShaderBytecode &bytecode) noexcept
{
    return bytecode.words && bytecode.wordCount >= 5 && bytecode.words[0] == 0x07230203u;
}

} // namespace

bool GpuParticleSpawnProgram::IsValid() const noexcept
{
    return IsShaderBytecodeValid(advance) && IsShaderBytecodeValid(prepare);
}

bool GpuParticleSpawnProgramStorage::Assign(const GpuParticleSpawnProgram &program)
{
    if (!program.IsValid())
        return false;
    shaders[0].assign(program.advance.words, program.advance.words + program.advance.wordCount);
    shaders[1].assign(program.prepare.words, program.prepare.words + program.prepare.wordCount);
    return true;
}

bool GpuParticleSpawnProgramStorage::IsValid() const noexcept
{
    return View().IsValid();
}

GpuParticleSpawnProgram GpuParticleSpawnProgramStorage::View() const noexcept
{
    return {{shaders[0].data(), shaders[0].size()}, {shaders[1].data(), shaders[1].size()}};
}

std::string_view GpuParticleSpawnShaderSources::Advance() noexcept
{
    return R"glsl(#version 450
layout(local_size_x = 256) in;
layout(std430, set = 0, binding = 0) buffer BurstRequestQueues { uint burstRequestCounts[]; };
layout(std430, set = 0, binding = 1) buffer ConsumingCounts { uint consumingCounts[]; };
layout(std430, set = 0, binding = 2) readonly buffer BurstRequestAcceptance { uint acceptingRequests[]; };
layout(push_constant) uniform SpawnDomainConstants {
    uint slotCount;
    uint targetSlot;
    uint capacity;
    uint cpuSpawnCount;
    uint spawnBaseId;
    uint spawnGeneration;
    uint reset;
    uint reserved;
} pc;

void saturatingAtomicAdd(uint slot, uint amount) {
    uint observed = consumingCounts[slot];
    for (;;) {
        uint desired = observed > 0xffffffffu - amount ? 0xffffffffu : observed + amount;
        uint prior = atomicCompSwap(consumingCounts[slot], observed, desired);
        if (prior == observed) return;
        observed = prior;
    }
}

void main() {
    uint slot = gl_GlobalInvocationID.x;
    if (slot >= pc.slotCount) return;
    if (pc.reset != 0u) {
        burstRequestCounts[slot] = 0u;
        consumingCounts[slot] = 0u;
        return;
    }
    bool accepting = acceptingRequests[slot] != 0u;
    // The sentinel keeps writes made later in this frame discarded. On the
    // first accepting frame it is exchanged for zero, so requests never catch up.
    uint requested = atomicExchange(burstRequestCounts[slot], accepting ? 0u : 0xffffffffu);
    if (!accepting) {
        atomicExchange(consumingCounts[slot], 0u);
    } else if (requested != 0u && requested != 0xffffffffu) {
        saturatingAtomicAdd(slot, requested);
    }
}
)glsl";
}

std::string_view GpuParticleSpawnShaderSources::Prepare() noexcept
{
    return R"glsl(#version 450
layout(local_size_x = 1) in;
layout(std430, set = 0, binding = 0) buffer ConsumingCounts { uint consumingCounts[]; };
layout(std430, set = 0, binding = 1) buffer SpawnMetadata { uint metadata[]; };
layout(push_constant) uniform SpawnDomainConstants {
    uint slotCount;
    uint targetSlot;
    uint capacity;
    uint cpuSpawnCount;
    uint spawnBaseId;
    uint spawnGeneration;
    uint reset;
    uint reserved;
} pc;

void main() {
    uint base = pc.targetSlot * 8u;
    if (pc.reset != 0u) {
        atomicExchange(consumingCounts[pc.targetSlot], 0u);
        metadata[base + 0u] = 0u;
        metadata[base + 1u] = pc.spawnBaseId;
        metadata[base + 2u] = pc.spawnGeneration;
        metadata[base + 3u] = 0u;
        metadata[base + 4u] = 0u;
        metadata[base + 5u] = 1u;
        metadata[base + 6u] = 1u;
        metadata[base + 7u] = 0u;
        return;
    }
    uint gpuCount = atomicExchange(consumingCounts[pc.targetSlot], 0u);
    uint requested = pc.cpuSpawnCount > 0xffffffffu - gpuCount
                         ? 0xffffffffu
                         : pc.cpuSpawnCount + gpuCount;
    uint accepted = min(requested, pc.capacity);
    metadata[base + 0u] = accepted;
    metadata[base + 1u] = pc.spawnBaseId;
    metadata[base + 2u] = pc.spawnGeneration;
    metadata[base + 3u] = requested - accepted;
    metadata[base + 4u] = (accepted + 255u) / 256u;
    metadata[base + 5u] = 1u;
    metadata[base + 6u] = 1u;
    metadata[base + 7u] = 0u;
}
)glsl";
}

ParticleGpuGraphSpawnDomain::~ParticleGpuGraphSpawnDomain()
{
    Destroy();
}

bool ParticleGpuGraphSpawnDomain::Create(rhi::Device &device, uint64_t graphInstanceId, uint32_t slotCount,
                                         const GpuParticleSpawnProgram &program)
{
    Destroy();
    if (graphInstanceId == 0 || slotCount == 0 || !program.IsValid() ||
        uint64_t(slotCount) > std::numeric_limits<uint64_t>::max() / MetadataStride)
        return false;

    m_device = &device;
    m_graphInstanceId = graphInstanceId;
    m_slotCount = slotCount;
    const std::vector<uint32_t> zeroCounts(slotCount, 0u);
    const auto storage = rhi::BufferUsageFlags::Storage;
    const auto createDeviceLocal = [&](uint64_t bytes, rhi::BufferUsageFlags usage) {
        rhi::BufferDesc desc;
        desc.byteSize = bytes;
        desc.usage = usage;
        desc.queueAccess = rhi::QueueAccessFlags::Compute;
        return device.CreateBuffer(desc);
    };
    m_burstRequestCounts = createDeviceLocal(uint64_t(slotCount) * sizeof(uint32_t), storage);
    m_consumingCounts = createDeviceLocal(uint64_t(slotCount) * sizeof(uint32_t), storage);
    rhi::BufferDesc activeDesc;
    activeDesc.byteSize = uint64_t(slotCount) * sizeof(uint32_t);
    activeDesc.usage = storage;
    activeDesc.memory = rhi::BufferMemory::Upload;
    activeDesc.queueAccess = rhi::QueueAccessFlags::Compute;
    activeDesc.initialData = zeroCounts.data();
    activeDesc.initialDataBytes = activeDesc.byteSize;
    m_acceptingRequestSlots = device.CreateBuffer(activeDesc);
    m_spawnMetadata =
        createDeviceLocal(uint64_t(slotCount) * MetadataStride,
                          storage | rhi::BufferUsageFlags::Indirect | rhi::BufferUsageFlags::TransferSource);
    if (!m_burstRequestCounts.IsValid() || !m_consumingCounts.IsValid() || !m_acceptingRequestSlots.IsValid() ||
        !m_spawnMetadata.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc layoutDesc;
    layoutDesc.entries[0] = {0, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entries[1] = {1, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entries[2] = {2, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entryCount = 3;
    m_domainLayout = device.CreateBindingLayout(layoutDesc);
    if (!m_domainLayout.IsValid()) {
        Destroy();
        return false;
    }
    const auto createGroup = [&](rhi::BufferHandle first, rhi::BufferHandle second) {
        rhi::BindGroupDesc desc;
        desc.layout = m_domainLayout;
        desc.buffers[0] = {0, rhi::BindingType::StorageBuffer, first};
        desc.buffers[1] = {1, rhi::BindingType::StorageBuffer, second};
        desc.buffers[2] = {2, rhi::BindingType::StorageBuffer, m_acceptingRequestSlots};
        desc.bufferCount = 3;
        return device.CreateBindGroup(desc);
    };
    m_advanceGroup = createGroup(m_burstRequestCounts, m_consumingCounts);
    m_prepareGroup = createGroup(m_consumingCounts, m_spawnMetadata);
    if (!m_advanceGroup.IsValid() || !m_prepareGroup.IsValid()) {
        Destroy();
        return false;
    }

    const std::array<ShaderBytecode, 2> shaders = {program.advance, program.prepare};
    std::array<rhi::ComputePipelineHandle *, 2> pipelines = {&m_advancePipeline, &m_preparePipeline};
    for (size_t index = 0; index < shaders.size(); ++index) {
        const auto module = device.CreateShaderModule({shaders[index].words, shaders[index].wordCount});
        if (!module.IsValid()) {
            Destroy();
            return false;
        }
        rhi::ComputePipelineDesc pipelineDesc;
        pipelineDesc.computeShader = module;
        pipelineDesc.bindingLayouts[0] = m_domainLayout;
        pipelineDesc.bindingLayoutCount = 1;
        pipelineDesc.pushConstantBytes = sizeof(GpuParticleSpawnDomainConstants);
        *pipelines[index] = device.CreateComputePipeline(pipelineDesc);
        device.Release(module);
        if (!pipelines[index]->IsValid()) {
            Destroy();
            return false;
        }
    }
    m_runtimeGroups.resize(slotCount);
    m_resetPending = true;
    return true;
}

void ParticleGpuGraphSpawnDomain::Destroy() noexcept
{
    if (m_device) {
        for (const auto group : m_runtimeGroups)
            m_device->Release(group);
        m_device->Release(m_preparePipeline);
        m_device->Release(m_advancePipeline);
        m_device->Release(m_prepareGroup);
        m_device->Release(m_advanceGroup);
        m_device->Release(m_domainLayout);
        m_device->Release(m_spawnMetadata);
        m_device->Release(m_acceptingRequestSlots);
        m_device->Release(m_consumingCounts);
        m_device->Release(m_burstRequestCounts);
    }
    m_device = nullptr;
    m_graphInstanceId = 0;
    m_slotCount = 0;
    m_burstRequestCounts = {};
    m_consumingCounts = {};
    m_spawnMetadata = {};
    m_acceptingRequestSlots = {};
    m_domainLayout = {};
    m_advanceGroup = {};
    m_prepareGroup = {};
    m_advancePipeline = {};
    m_preparePipeline = {};
    m_resetPending = true;
    m_runtimeGroups.clear();
    m_burstRequestResource = {};
    m_consumingResource = {};
    m_metadataResource = {};
}

bool ParticleGpuGraphSpawnDomain::RegisterEmitter(uint32_t targetSlot, const ParticleGpuRuntime &runtime)
{
    if (!IsValid() || targetSlot >= m_slotCount || !runtime.IsValid() || !runtime.GraphSpawnLayout().IsValid() ||
        m_runtimeGroups[targetSlot].IsValid())
        return false;
    rhi::BindGroupDesc desc;
    desc.layout = runtime.GraphSpawnLayout();
    desc.buffers[0] = {0, rhi::BindingType::StorageBuffer, m_burstRequestCounts, 0,
                       uint64_t(m_slotCount) * sizeof(uint32_t)};
    desc.buffers[1] = {1, rhi::BindingType::StorageBuffer, m_spawnMetadata, MetadataOffset(targetSlot), MetadataStride};
    desc.bufferCount = 2;
    m_runtimeGroups[targetSlot] = m_device->CreateBindGroup(desc);
    return m_runtimeGroups[targetSlot].IsValid();
}

bool ParticleGpuGraphSpawnDomain::SetEmitterAcceptingBurstRequests(uint32_t targetSlot, bool accepting)
{
    if (!IsValid() || targetSlot >= m_slotCount)
        return false;
    const uint32_t value = accepting ? 1u : 0u;
    return m_device->WriteBuffer(m_acceptingRequestSlots, uint64_t(targetSlot) * sizeof(uint32_t), &value,
                                 sizeof(value));
}

bool ParticleGpuGraphSpawnDomain::Attach(vk::RenderGraph &graph, const std::string &namePrefix)
{
    if (!IsValid() || namePrefix.empty() || m_burstRequestResource.IsValid())
        return false;
    graph.AddComputePass(StageName(namePrefix, "SpawnDomainAdvance"), [&](vk::PassBuilder &builder) {
        const uint64_t countBytes = uint64_t(m_slotCount) * sizeof(uint32_t);
        m_burstRequestResource =
            builder.ImportBuffer(StageName(namePrefix, "BurstRequestQueue"), m_burstRequestCounts, countBytes);
        m_consumingResource =
            builder.ImportBuffer(StageName(namePrefix, "SpawnConsuming"), m_consumingCounts, countBytes);
        const auto acceptanceResource =
            builder.ImportBuffer(StageName(namePrefix, "BurstRequestAcceptance"), m_acceptingRequestSlots, countBytes);
        m_metadataResource = builder.ImportBuffer(StageName(namePrefix, "SpawnMetadata"), m_spawnMetadata,
                                                  uint64_t(m_slotCount) * MetadataStride);
        if (!m_burstRequestResource.IsValid() || !m_consumingResource.IsValid() || !acceptanceResource.IsValid() ||
            !m_metadataResource.IsValid())
            return vk::PassExecuteCallback{};
        builder.ReadStorageBuffer(acceptanceResource);
        m_burstRequestResource = builder.ReadWrite(m_burstRequestResource, rhi::PipelineStage::ComputeShader);
        m_consumingResource = builder.ReadWrite(m_consumingResource, rhi::PipelineStage::ComputeShader);
        return vk::PassExecuteCallback{[this](vk::RenderContext &context) {
            if (!IsValid())
                return;
            GpuParticleSpawnDomainConstants constants;
            constants.slotCount = m_slotCount;
            constants.reset = m_resetPending ? 1u : 0u;
            const auto encoder = context.GetComputeCommandEncoder();
            encoder.BindPipeline(m_advancePipeline);
            encoder.BindGroup(m_advancePipeline, 0, m_advanceGroup);
            encoder.PushConstants(m_advancePipeline, sizeof(constants), &constants);
            encoder.Dispatch(1u + (m_slotCount - 1u) / WorkgroupSize, 1, 1);
            m_resetPending = false;
        }};
    });
    return m_burstRequestResource.IsValid() && m_consumingResource.IsValid() && m_metadataResource.IsValid();
}

void ParticleGpuGraphSpawnDomain::DeclarePrepare(vk::PassBuilder &builder)
{
    m_consumingResource = builder.ReadWrite(m_consumingResource, rhi::PipelineStage::ComputeShader);
    m_metadataResource = builder.ReadWrite(m_metadataResource, rhi::PipelineStage::ComputeShader);
}

void ParticleGpuGraphSpawnDomain::DeclareKernelWrite(vk::PassBuilder &builder)
{
    m_burstRequestResource = builder.ReadWrite(m_burstRequestResource, rhi::PipelineStage::ComputeShader);
}

void ParticleGpuGraphSpawnDomain::DeclareInitRead(vk::PassBuilder &builder)
{
    builder.ReadStorageBuffer(m_metadataResource);
    builder.ReadIndirectBuffer(m_metadataResource);
}

void ParticleGpuGraphSpawnDomain::RecordPrepare(const rhi::ComputeCommandEncoder &encoder, uint32_t targetSlot,
                                                uint32_t capacity, const GpuParticleFrameRequest &request,
                                                bool discard) const
{
    if (!IsValid() || !encoder.IsValid() || targetSlot >= m_slotCount)
        return;
    GpuParticleSpawnDomainConstants constants;
    constants.slotCount = m_slotCount;
    constants.targetSlot = targetSlot;
    constants.capacity = capacity;
    constants.cpuSpawnCount = discard ? 0u : request.spawnCount;
    constants.spawnBaseId = request.spawnBaseId;
    constants.spawnGeneration = request.spawnGeneration;
    constants.reset = discard ? 1u : 0u;
    encoder.BindPipeline(m_preparePipeline);
    encoder.BindGroup(m_preparePipeline, 0, m_prepareGroup);
    encoder.PushConstants(m_preparePipeline, sizeof(constants), &constants);
    encoder.Dispatch(1, 1, 1);
}

bool ParticleGpuGraphSpawnDomain::IsValid() const noexcept
{
    return m_device && m_graphInstanceId != 0 && m_slotCount != 0 && m_burstRequestCounts.IsValid() &&
           m_consumingCounts.IsValid() && m_acceptingRequestSlots.IsValid() && m_spawnMetadata.IsValid() &&
           m_domainLayout.IsValid() && m_advanceGroup.IsValid() && m_prepareGroup.IsValid() &&
           m_advancePipeline.IsValid() && m_preparePipeline.IsValid();
}

rhi::BindGroupHandle ParticleGpuGraphSpawnDomain::RuntimeGroup(uint32_t targetSlot) const noexcept
{
    return targetSlot < m_runtimeGroups.size() ? m_runtimeGroups[targetSlot] : rhi::BindGroupHandle{};
}

bool ParticleRenderGraph::Attach(vk::RenderGraph &graph, ParticleGpuRuntime &runtime, ParticleGpuBounds &bounds,
                                 ParticleGpuGraphSpawnDomain &spawnDomain, uint32_t graphEmitterIndex,
                                 const std::string &namePrefix, ParticleGpuMigrator *migration,
                                 ParticleGpuRibbonTopology *ribbonTopology)
{
    if (IsAttached() || !runtime.IsValid() || !bounds.IsValid() || !spawnDomain.IsValid() ||
        graphEmitterIndex >= spawnDomain.SlotCount() || !spawnDomain.RuntimeGroup(graphEmitterIndex).IsValid() ||
        bounds.InstanceBuffer() != runtime.InstanceBuffer() ||
        bounds.SourceIndirectBuffer() != runtime.IndirectBuffer() || namePrefix.empty() || runtime.StateStride() == 0 ||
        (migration && (!migration->IsValid() || migration->DestinationStateBuffer() != runtime.StateBuffer() ||
                       migration->DestinationFreeListBuffer() != runtime.FreeListBuffer() ||
                       migration->DestinationCounterBuffer() != runtime.CounterBuffer())) ||
        (ribbonTopology &&
         (!ribbonTopology->IsValid() || ribbonTopology->InstanceBuffer() != runtime.InstanceBuffer() ||
          ribbonTopology->SourceIndexBuffer() != runtime.RenderIndexBuffer() ||
          ribbonTopology->SourceIndirectBuffer() != runtime.IndirectBuffer())))
        return false;

    m_runtime = &runtime;
    m_bounds = &bounds;
    m_migrator = migration;
    m_ribbonTopology = ribbonTopology;
    m_spawnDomain = &spawnDomain;
    m_graphEmitterIndex = graphEmitterIndex;
    m_migrationPending = migration != nullptr;
    m_migrationCompleted = false;
    m_bootstrapPending = !migration && runtime.NeedsBootstrap();
    vk::ResourceHandle states;
    vk::ResourceHandle freeList;
    vk::ResourceHandle counters;
    vk::ResourceHandle instances;
    vk::ResourceHandle renderIndices;
    vk::ResourceHandle indirect;
    vk::ResourceHandle transforms;
    vk::ResourceHandle simulationControl;
    vk::ResourceHandle continuationRecords;
    vk::ResourceHandle continuationFreeList;
    vk::ResourceHandle continuationReadyQueue;
    vk::ResourceHandle continuationActiveQueueA;
    vk::ResourceHandle continuationActiveQueueB;
    vk::ResourceHandle continuationCounters;
    vk::ResourceHandle continuationClassifyIndirect;
    vk::ResourceHandle continuationDispatchIndirect;
    vk::ResourceHandle continuationLaneSlots;
    vk::ResourceHandle continuationJoinStates;
    vk::ResourceHandle boundsBuffer;
    vk::ResourceHandle boundsDispatch;
    vk::ResourceHandle migrationSourceStates;
    vk::ResourceHandle migrationSourceCounters;
    vk::ResourceHandle migrationRanges;
    vk::ResourceHandle migrationDefaults;
    std::array<vk::ResourceHandle, 2> ribbonIndices{};
    vk::ResourceHandle ribbonIndirect;
    vk::ResourceHandle ribbonDispatch;
    vk::ResourceHandle ribbonHistograms;
    vk::ResourceHandle ribbonBlockOffsets;
    vk::ResourceHandle ribbonGlobalOffsets;

    graph.AddComputePass(StageName(namePrefix, "Bootstrap"), [&](vk::PassBuilder &builder) {
        const uint64_t capacity = runtime.Capacity();
        states = builder.ImportBuffer(StageName(namePrefix, "States"), runtime.StateBuffer(),
                                      capacity * runtime.StateStride());
        freeList = builder.ImportBuffer(StageName(namePrefix, "FreeList"), runtime.FreeListBuffer(),
                                        capacity * sizeof(uint32_t));
        counters = builder.ImportBuffer(StageName(namePrefix, "Counters"), runtime.CounterBuffer(), CounterBufferBytes);
        instances = builder.ImportBuffer(StageName(namePrefix, "Instances"), runtime.InstanceBuffer(),
                                         capacity * ParticleGpuRuntime::RenderInstanceStride);
        renderIndices = builder.ImportBuffer(StageName(namePrefix, "RenderIndices"), runtime.RenderIndexBuffer(),
                                             capacity * sizeof(uint32_t));
        indirect =
            builder.ImportBuffer(StageName(namePrefix, "Indirect"), runtime.IndirectBuffer(), IndirectBufferBytes);
        transforms = builder.ImportBuffer(StageName(namePrefix, "Transforms"), runtime.TransformBuffer(),
                                          sizeof(GpuParticleTransforms));
        simulationControl =
            builder.ImportBuffer(StageName(namePrefix, "SimulationControl"), runtime.SimulationControlBuffer(),
                                 sizeof(GpuParticleSimulationControl));
        if (runtime.HasContinuations()) {
            const auto &continuation = runtime.ContinuationResources();
            const auto telemetry = runtime.ContinuationTelemetry();
            continuationRecords = builder.ImportBuffer(StageName(namePrefix, "ContinuationRecords"),
                                                       continuation.records, telemetry.recordBytes);
            continuationFreeList = builder.ImportBuffer(StageName(namePrefix, "ContinuationFreeList"),
                                                        continuation.freeList, telemetry.queueBytes);
            continuationReadyQueue = builder.ImportBuffer(StageName(namePrefix, "ContinuationReadyQueue"),
                                                          continuation.readyQueue, telemetry.queueBytes);
            continuationActiveQueueA = builder.ImportBuffer(StageName(namePrefix, "ContinuationActiveQueueA"),
                                                            continuation.activeQueueA, telemetry.queueBytes);
            continuationActiveQueueB = builder.ImportBuffer(StageName(namePrefix, "ContinuationActiveQueueB"),
                                                            continuation.activeQueueB, telemetry.queueBytes);
            continuationCounters = builder.ImportBuffer(StageName(namePrefix, "ContinuationCounters"),
                                                        continuation.counters, sizeof(GpuParticleContinuationCounters));
            continuationClassifyIndirect = builder.ImportBuffer(StageName(namePrefix, "ContinuationClassifyIndirect"),
                                                                continuation.classifyIndirectArguments,
                                                                ParticleGpuContinuationRuntime::IndirectBufferBytes);
            continuationDispatchIndirect = builder.ImportBuffer(StageName(namePrefix, "ContinuationDispatchIndirect"),
                                                                continuation.dispatchIndirectArguments,
                                                                ParticleGpuContinuationRuntime::IndirectBufferBytes);
            continuationLaneSlots = builder.ImportBuffer(StageName(namePrefix, "ContinuationLaneSlots"),
                                                         continuation.laneSlots, telemetry.laneSlotBytes);
            continuationJoinStates = builder.ImportBuffer(StageName(namePrefix, "ContinuationJoinStates"),
                                                          continuation.joinStates, telemetry.joinStateBytes);
        }
        boundsBuffer = builder.ImportBuffer(StageName(namePrefix, "Bounds"), bounds.BoundsBuffer(),
                                            ParticleGpuBounds::BoundsBufferBytes);
        boundsDispatch = builder.ImportBuffer(StageName(namePrefix, "BoundsDispatch"), bounds.DispatchBuffer(),
                                              ParticleGpuBounds::DispatchBufferBytes);
        if (!states.IsValid() || !freeList.IsValid() || !counters.IsValid() || !instances.IsValid() ||
            !renderIndices.IsValid() || !indirect.IsValid() || !transforms.IsValid() || !simulationControl.IsValid() ||
            !boundsBuffer.IsValid() || !boundsDispatch.IsValid())
            return vk::PassExecuteCallback{};
        if (runtime.HasContinuations() &&
            (!continuationRecords.IsValid() || !continuationFreeList.IsValid() || !continuationReadyQueue.IsValid() ||
             !continuationActiveQueueA.IsValid() || !continuationActiveQueueB.IsValid() ||
             !continuationCounters.IsValid() || !continuationClassifyIndirect.IsValid() ||
             !continuationDispatchIndirect.IsValid() || !continuationLaneSlots.IsValid() ||
             !continuationJoinStates.IsValid()))
            return vk::PassExecuteCallback{};

        states = builder.ReadWrite(states, rhi::PipelineStage::ComputeShader);
        freeList = builder.ReadWrite(freeList, rhi::PipelineStage::ComputeShader);
        counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
        indirect = builder.ReadWrite(indirect, rhi::PipelineStage::ComputeShader);
        return vk::PassExecuteCallback{[this](vk::RenderContext &context) {
            if (!m_framePending || !m_bootstrapPending || !m_runtime)
                return;
            m_runtime->RecordBootstrap(context.GetComputeCommandEncoder(), m_request.systemSeed,
                                       m_spawnDomain->RuntimeGroup(m_graphEmitterIndex));
            m_bootstrapPending = false;
        }};
    });

    if (!states.IsValid() || !freeList.IsValid() || !counters.IsValid() || !instances.IsValid() ||
        !renderIndices.IsValid() || !indirect.IsValid() || !transforms.IsValid() || !simulationControl.IsValid() ||
        !boundsBuffer.IsValid() || !boundsDispatch.IsValid() ||
        (runtime.HasContinuations() &&
         (!continuationRecords.IsValid() || !continuationFreeList.IsValid() || !continuationReadyQueue.IsValid() ||
          !continuationActiveQueueA.IsValid() || !continuationActiveQueueB.IsValid() ||
          !continuationCounters.IsValid() || !continuationClassifyIndirect.IsValid() ||
          !continuationDispatchIndirect.IsValid() || !continuationLaneSlots.IsValid() ||
          !continuationJoinStates.IsValid()))) {
        m_runtime = nullptr;
        m_bounds = nullptr;
        m_spawnDomain = nullptr;
        return false;
    }

    if (migration) {
        graph.AddComputePass(StageName(namePrefix, "MigrationReset"), [&](vk::PassBuilder &builder) {
            const auto &constants = migration->Constants();
            migrationSourceStates = builder.ImportBuffer(
                StageName(namePrefix, "MigrationSourceStates"), migration->SourceStateBuffer(),
                static_cast<uint64_t>(constants.sourceCapacity) * constants.sourceStrideWords * sizeof(uint32_t));
            migrationSourceCounters = builder.ImportBuffer(StageName(namePrefix, "MigrationSourceCounters"),
                                                           migration->SourceCounterBuffer(), CounterBufferBytes);
            migrationRanges = builder.ImportBuffer(
                StageName(namePrefix, "MigrationRanges"), migration->CopyRangeBuffer(),
                std::max<uint64_t>(static_cast<uint64_t>(constants.copyRangeCount) * sizeof(GpuParticleMigrationRange),
                                   sizeof(GpuParticleMigrationRange)));
            migrationDefaults =
                builder.ImportBuffer(StageName(namePrefix, "MigrationDefaults"), migration->DefaultStateBuffer(),
                                     static_cast<uint64_t>(constants.destinationStrideWords) * sizeof(uint32_t));
            if (!migrationSourceStates.IsValid() || !migrationSourceCounters.IsValid() || !migrationRanges.IsValid() ||
                !migrationDefaults.IsValid())
                return vk::PassExecuteCallback{};
            builder.ReadStorageBuffer(migrationSourceCounters);
            counters = builder.WriteStorageBuffer(counters);
            return vk::PassExecuteCallback{[this](vk::RenderContext &context) {
                if (m_framePending && m_migrationPending && m_migrator)
                    m_migrator->RecordReset(context.GetComputeCommandEncoder());
            }};
        });

        if (!migrationSourceStates.IsValid() || !migrationSourceCounters.IsValid() || !migrationRanges.IsValid() ||
            !migrationDefaults.IsValid()) {
            m_runtime = nullptr;
            m_bounds = nullptr;
            m_migrator = nullptr;
            return false;
        }

        graph.AddComputePass(StageName(namePrefix, "Migration"), [&](vk::PassBuilder &builder) {
            builder.ReadStorageBuffer(migrationSourceStates);
            builder.ReadStorageBuffer(migrationRanges);
            builder.ReadStorageBuffer(migrationDefaults);
            states = builder.WriteStorageBuffer(states);
            freeList = builder.WriteStorageBuffer(freeList);
            counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
            return [this](vk::RenderContext &context) {
                if (!m_framePending || !m_migrationPending || !m_migrator || !m_runtime)
                    return;
                m_migrator->RecordMigrate(context.GetComputeCommandEncoder());
                if (!m_migrator->WasRecorded())
                    return;
                m_runtime->MarkStateInitialized();
                m_migrationPending = false;
                m_migrationCompleted = true;
            };
        });
    }

    graph.AddComputePass(StageName(namePrefix, "VisibilityPrepare"), [&](vk::PassBuilder &builder) {
        simulationControl = builder.ReadWrite(simulationControl, rhi::PipelineStage::ComputeShader);
        return [this](vk::RenderContext &context) {
            if (!m_framePending || !m_bounds)
                return;
            m_bounds->RecordPrepare(context.GetComputeCommandEncoder(), m_request.offscreenPolicy,
                                    m_request.forceSimulation);
        };
    });

    graph.AddComputePass(StageName(namePrefix, "SpawnPrepare"), [&](vk::PassBuilder &builder) {
        m_spawnDomain->DeclarePrepare(builder);
        return [this](vk::RenderContext &context) {
            if (!m_framePending || !m_runtime || !m_spawnDomain)
                return;
            const bool discard = m_resetPending || !m_request.simulate;
            m_spawnDomain->RecordPrepare(context.GetComputeCommandEncoder(), m_graphEmitterIndex, m_runtime->Capacity(),
                                         m_request, discard);
        };
    });

    if (runtime.HasContinuations()) {
        graph.AddComputePass(StageName(namePrefix, "ContinuationPrepare"), [&](vk::PassBuilder &builder) {
            continuationRecords = builder.ReadWrite(continuationRecords, rhi::PipelineStage::ComputeShader);
            continuationFreeList = builder.ReadWrite(continuationFreeList, rhi::PipelineStage::ComputeShader);
            continuationReadyQueue = builder.ReadWrite(continuationReadyQueue, rhi::PipelineStage::ComputeShader);
            continuationActiveQueueA = builder.ReadWrite(continuationActiveQueueA, rhi::PipelineStage::ComputeShader);
            continuationActiveQueueB = builder.ReadWrite(continuationActiveQueueB, rhi::PipelineStage::ComputeShader);
            continuationCounters = builder.ReadWrite(continuationCounters, rhi::PipelineStage::ComputeShader);
            continuationClassifyIndirect = builder.WriteStorageBuffer(continuationClassifyIndirect);
            continuationDispatchIndirect = builder.WriteStorageBuffer(continuationDispatchIndirect);
            continuationLaneSlots = builder.ReadWrite(continuationLaneSlots, rhi::PipelineStage::ComputeShader);
            continuationJoinStates = builder.ReadWrite(continuationJoinStates, rhi::PipelineStage::ComputeShader);
            return [this](vk::RenderContext &context) {
                if (!m_framePending || !m_runtime)
                    return;
                (void)m_runtime->RecordContinuationPrepare(context.GetComputeCommandEncoder(), m_request.simulationStep,
                                                           m_request.continuationTimeTicks);
            };
        });
    }

    graph.AddComputePass(StageName(namePrefix, "Init"), [&](vk::PassBuilder &builder) {
        m_spawnDomain->DeclareInitRead(builder);
        m_spawnDomain->DeclareKernelWrite(builder);
        states = builder.ReadWrite(states, rhi::PipelineStage::ComputeShader);
        freeList = builder.ReadWrite(freeList, rhi::PipelineStage::ComputeShader);
        counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
        builder.ReadUniformBuffer(transforms);
        builder.ReadStorageBuffer(simulationControl);
        if (runtime.HasContinuations()) {
            continuationRecords = builder.ReadWrite(continuationRecords, rhi::PipelineStage::ComputeShader);
            continuationFreeList = builder.ReadWrite(continuationFreeList, rhi::PipelineStage::ComputeShader);
            continuationActiveQueueA = builder.ReadWrite(continuationActiveQueueA, rhi::PipelineStage::ComputeShader);
            continuationActiveQueueB = builder.ReadWrite(continuationActiveQueueB, rhi::PipelineStage::ComputeShader);
            continuationCounters = builder.ReadWrite(continuationCounters, rhi::PipelineStage::ComputeShader);
            continuationLaneSlots = builder.ReadWrite(continuationLaneSlots, rhi::PipelineStage::ComputeShader);
            continuationJoinStates = builder.ReadWrite(continuationJoinStates, rhi::PipelineStage::ComputeShader);
        }
        return [this](vk::RenderContext &context) {
            if (!m_framePending || !m_request.simulate || !m_runtime || !m_spawnDomain)
                return;
            m_runtime->RecordInitIndirect(
                context.GetComputeCommandEncoder(), m_request.spawnCount, m_request.spawnBaseId,
                m_request.spawnGeneration, m_request.systemSeed, m_request.simulationStep, m_request.deltaTime,
                m_spawnDomain->RuntimeGroup(m_graphEmitterIndex), m_spawnDomain->MetadataBuffer(),
                m_spawnDomain->InitIndirectOffset(m_graphEmitterIndex));
        };
    });

    if (runtime.HasContinuations()) {
        graph.AddComputePass(StageName(namePrefix, "ContinuationClassify"), [&](vk::PassBuilder &builder) {
            builder.ReadStorageBuffer(continuationRecords);
            continuationFreeList = builder.ReadWrite(continuationFreeList, rhi::PipelineStage::ComputeShader);
            continuationReadyQueue = builder.ReadWrite(continuationReadyQueue, rhi::PipelineStage::ComputeShader);
            continuationActiveQueueA = builder.ReadWrite(continuationActiveQueueA, rhi::PipelineStage::ComputeShader);
            continuationActiveQueueB = builder.ReadWrite(continuationActiveQueueB, rhi::PipelineStage::ComputeShader);
            continuationCounters = builder.ReadWrite(continuationCounters, rhi::PipelineStage::ComputeShader);
            builder.ReadIndirectBuffer(continuationClassifyIndirect);
            continuationDispatchIndirect = builder.WriteStorageBuffer(continuationDispatchIndirect);
            continuationLaneSlots = builder.ReadWrite(continuationLaneSlots, rhi::PipelineStage::ComputeShader);
            continuationJoinStates = builder.ReadWrite(continuationJoinStates, rhi::PipelineStage::ComputeShader);
            return [this](vk::RenderContext &context) {
                if (!m_framePending || !m_request.simulate || !m_runtime)
                    return;
                (void)m_runtime->RecordContinuationClassify(context.GetComputeCommandEncoder(),
                                                            m_request.simulationStep, m_request.continuationTimeTicks);
            };
        });

        graph.AddComputePass(StageName(namePrefix, "ContinuationDispatch"), [&](vk::PassBuilder &builder) {
            m_spawnDomain->DeclareKernelWrite(builder);
            states = builder.ReadWrite(states, rhi::PipelineStage::ComputeShader);
            freeList = builder.ReadWrite(freeList, rhi::PipelineStage::ComputeShader);
            counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
            builder.ReadUniformBuffer(transforms);
            builder.ReadStorageBuffer(simulationControl);
            continuationRecords = builder.ReadWrite(continuationRecords, rhi::PipelineStage::ComputeShader);
            continuationFreeList = builder.ReadWrite(continuationFreeList, rhi::PipelineStage::ComputeShader);
            continuationReadyQueue = builder.ReadWrite(continuationReadyQueue, rhi::PipelineStage::ComputeShader);
            continuationActiveQueueA = builder.ReadWrite(continuationActiveQueueA, rhi::PipelineStage::ComputeShader);
            continuationActiveQueueB = builder.ReadWrite(continuationActiveQueueB, rhi::PipelineStage::ComputeShader);
            continuationCounters = builder.ReadWrite(continuationCounters, rhi::PipelineStage::ComputeShader);
            builder.ReadIndirectBuffer(continuationDispatchIndirect);
            continuationLaneSlots = builder.ReadWrite(continuationLaneSlots, rhi::PipelineStage::ComputeShader);
            continuationJoinStates = builder.ReadWrite(continuationJoinStates, rhi::PipelineStage::ComputeShader);
            return [this](vk::RenderContext &context) {
                if (!m_framePending || !m_request.simulate || !m_runtime)
                    return;
                (void)m_runtime->RecordContinuationDispatch(
                    context.GetComputeCommandEncoder(), m_request.simulationStep, m_request.continuationTimeTicks,
                    m_request.systemSeed, m_request.deltaTime, m_spawnDomain->RuntimeGroup(m_graphEmitterIndex));
            };
        });
    }

    graph.AddComputePass(StageName(namePrefix, "Update"), [&](vk::PassBuilder &builder) {
        m_spawnDomain->DeclareKernelWrite(builder);
        states = builder.ReadWrite(states, rhi::PipelineStage::ComputeShader);
        freeList = builder.ReadWrite(freeList, rhi::PipelineStage::ComputeShader);
        counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
        builder.ReadUniformBuffer(transforms);
        builder.ReadStorageBuffer(simulationControl);
        if (runtime.HasContinuations()) {
            continuationRecords = builder.ReadWrite(continuationRecords, rhi::PipelineStage::ComputeShader);
            continuationFreeList = builder.ReadWrite(continuationFreeList, rhi::PipelineStage::ComputeShader);
            continuationActiveQueueA = builder.ReadWrite(continuationActiveQueueA, rhi::PipelineStage::ComputeShader);
            continuationActiveQueueB = builder.ReadWrite(continuationActiveQueueB, rhi::PipelineStage::ComputeShader);
            continuationCounters = builder.ReadWrite(continuationCounters, rhi::PipelineStage::ComputeShader);
            continuationLaneSlots = builder.ReadWrite(continuationLaneSlots, rhi::PipelineStage::ComputeShader);
            continuationJoinStates = builder.ReadWrite(continuationJoinStates, rhi::PipelineStage::ComputeShader);
        }
        return [this](vk::RenderContext &context) {
            if (!m_framePending || !m_request.simulate || !m_runtime)
                return;
            m_runtime->RecordUpdate(context.GetComputeCommandEncoder(), m_request.systemSeed, m_request.simulationStep,
                                    m_request.deltaTime, m_spawnDomain->RuntimeGroup(m_graphEmitterIndex));
        };
    });

    const auto renderExportBoundary =
        graph.AddComputePass(StageName(namePrefix, "RenderReset"), [&](vk::PassBuilder &builder) {
            counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
            indirect = builder.ReadWrite(indirect, rhi::PipelineStage::ComputeShader);
            builder.ReadStorageBuffer(simulationControl);
            return [this](vk::RenderContext &context) {
                if (!m_framePending || !m_runtime)
                    return;
                m_runtime->RecordRenderReset(context.GetComputeCommandEncoder(),
                                             m_spawnDomain->RuntimeGroup(m_graphEmitterIndex));
            };
        });
    m_renderExportPassId = renderExportBoundary.id;
    graph.SetSubmissionBoundaryBefore(renderExportBoundary);

    graph.AddComputePass(StageName(namePrefix, "Rendering"), [&](vk::PassBuilder &builder) {
        m_spawnDomain->DeclareKernelWrite(builder);
        states = builder.ReadWrite(states, rhi::PipelineStage::ComputeShader);
        freeList = builder.ReadWrite(freeList, rhi::PipelineStage::ComputeShader);
        counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
        instances = builder.ReadWrite(instances, rhi::PipelineStage::ComputeShader);
        renderIndices = builder.ReadWrite(renderIndices, rhi::PipelineStage::ComputeShader);
        indirect = builder.ReadWrite(indirect, rhi::PipelineStage::ComputeShader);
        builder.ReadUniformBuffer(transforms);
        builder.ReadStorageBuffer(simulationControl);
        if (runtime.HasContinuations()) {
            continuationRecords = builder.ReadWrite(continuationRecords, rhi::PipelineStage::ComputeShader);
            continuationFreeList = builder.ReadWrite(continuationFreeList, rhi::PipelineStage::ComputeShader);
            continuationActiveQueueA = builder.ReadWrite(continuationActiveQueueA, rhi::PipelineStage::ComputeShader);
            continuationActiveQueueB = builder.ReadWrite(continuationActiveQueueB, rhi::PipelineStage::ComputeShader);
            continuationCounters = builder.ReadWrite(continuationCounters, rhi::PipelineStage::ComputeShader);
            continuationLaneSlots = builder.ReadWrite(continuationLaneSlots, rhi::PipelineStage::ComputeShader);
            continuationJoinStates = builder.ReadWrite(continuationJoinStates, rhi::PipelineStage::ComputeShader);
        }
        return [this](vk::RenderContext &context) {
            if (!m_framePending)
                return;
            if (m_request.render && m_runtime) {
                m_runtime->RecordRendering(context.GetComputeCommandEncoder(), m_request.systemSeed,
                                           m_request.simulationStep, m_spawnDomain->RuntimeGroup(m_graphEmitterIndex));
            }
        };
    });

    if (ribbonTopology) {
        const uint64_t indexBytes = static_cast<uint64_t>(runtime.Capacity()) * sizeof(uint32_t);
        const uint64_t histogramBytes =
            static_cast<uint64_t>(ribbonTopology->BlockCount()) * ParticleGpuRibbonTopology::Radix * sizeof(uint32_t);
        graph.AddComputePass(StageName(namePrefix, "RibbonReset"), [&](vk::PassBuilder &builder) {
            ribbonIndices = {
                builder.ImportBuffer(StageName(namePrefix, "RibbonIndices0"), ribbonTopology->IndexBuffer(0),
                                     indexBytes),
                builder.ImportBuffer(StageName(namePrefix, "RibbonIndices1"), ribbonTopology->IndexBuffer(1),
                                     indexBytes),
            };
            ribbonIndirect = builder.ImportBuffer(StageName(namePrefix, "RibbonIndirect"),
                                                  ribbonTopology->DrawIndirectBuffer(), IndirectBufferBytes);
            ribbonDispatch = builder.ImportBuffer(StageName(namePrefix, "RibbonDispatch"),
                                                  ribbonTopology->DispatchBuffer(), 3u * sizeof(uint32_t));
            ribbonHistograms = builder.ImportBuffer(StageName(namePrefix, "RibbonHistograms"),
                                                    ribbonTopology->HistogramBuffer(), histogramBytes);
            ribbonBlockOffsets = builder.ImportBuffer(StageName(namePrefix, "RibbonBlockOffsets"),
                                                      ribbonTopology->BlockOffsetBuffer(), histogramBytes);
            ribbonGlobalOffsets =
                builder.ImportBuffer(StageName(namePrefix, "RibbonGlobalOffsets"), ribbonTopology->GlobalOffsetBuffer(),
                                     ParticleGpuRibbonTopology::Radix * sizeof(uint32_t));
            if (!ribbonIndices[0].IsValid() || !ribbonIndices[1].IsValid() || !ribbonIndirect.IsValid() ||
                !ribbonDispatch.IsValid() || !ribbonHistograms.IsValid() || !ribbonBlockOffsets.IsValid() ||
                !ribbonGlobalOffsets.IsValid()) {
                return vk::PassExecuteCallback{};
            }
            builder.ReadStorageBuffer(indirect);
            builder.ReadStorageBuffer(simulationControl);
            ribbonIndirect = builder.WriteStorageBuffer(ribbonIndirect);
            ribbonDispatch = builder.WriteStorageBuffer(ribbonDispatch);
            return vk::PassExecuteCallback{[this](vk::RenderContext &context) {
                if (m_framePending && m_ribbonTopology)
                    m_ribbonTopology->RecordReset(context.GetComputeCommandEncoder());
            }};
        });

        graph.AddComputePass(StageName(namePrefix, "RibbonInitialize"), [&](vk::PassBuilder &builder) {
            builder.ReadStorageBuffer(renderIndices);
            builder.ReadStorageBuffer(indirect);
            builder.ReadStorageBuffer(simulationControl);
            builder.ReadIndirectBuffer(ribbonDispatch);
            ribbonIndices[0] = builder.WriteStorageBuffer(ribbonIndices[0]);
            return vk::PassExecuteCallback{[this](vk::RenderContext &context) {
                if (m_framePending && m_ribbonTopology)
                    m_ribbonTopology->RecordInitialize(context.GetComputeCommandEncoder());
            }};
        });

        for (uint32_t passIndex = 0; passIndex < ParticleGpuRibbonTopology::PassCount; ++passIndex) {
            const uint32_t input = passIndex % 2u;
            const uint32_t output = 1u - input;
            const std::string passPrefix = StageName(namePrefix, "RibbonRadix") + "/" + std::to_string(passIndex);
            graph.AddComputePass(passPrefix + "/Histogram", [&, passIndex, input](vk::PassBuilder &builder) {
                builder.ReadStorageBuffer(instances);
                builder.ReadStorageBuffer(indirect);
                builder.ReadStorageBuffer(simulationControl);
                builder.ReadStorageBuffer(ribbonIndices[input]);
                builder.ReadIndirectBuffer(ribbonDispatch);
                ribbonHistograms = builder.WriteStorageBuffer(ribbonHistograms);
                return vk::PassExecuteCallback{[this, passIndex](vk::RenderContext &context) {
                    if (m_framePending && m_ribbonTopology)
                        m_ribbonTopology->RecordHistogram(context.GetComputeCommandEncoder(), passIndex);
                }};
            });
            graph.AddComputePass(passPrefix + "/Scan", [&, passIndex](vk::PassBuilder &builder) {
                builder.ReadStorageBuffer(ribbonHistograms);
                builder.ReadStorageBuffer(simulationControl);
                ribbonBlockOffsets = builder.WriteStorageBuffer(ribbonBlockOffsets);
                ribbonGlobalOffsets = builder.WriteStorageBuffer(ribbonGlobalOffsets);
                return vk::PassExecuteCallback{[this, passIndex](vk::RenderContext &context) {
                    if (m_framePending && m_ribbonTopology)
                        m_ribbonTopology->RecordScan(context.GetComputeCommandEncoder(), passIndex);
                }};
            });
            graph.AddComputePass(passPrefix + "/Scatter", [&, passIndex, input, output](vk::PassBuilder &builder) {
                builder.ReadStorageBuffer(instances);
                builder.ReadStorageBuffer(indirect);
                builder.ReadStorageBuffer(simulationControl);
                builder.ReadStorageBuffer(ribbonIndices[input]);
                builder.ReadStorageBuffer(ribbonHistograms);
                builder.ReadStorageBuffer(ribbonBlockOffsets);
                builder.ReadStorageBuffer(ribbonGlobalOffsets);
                builder.ReadIndirectBuffer(ribbonDispatch);
                ribbonIndices[output] = builder.WriteStorageBuffer(ribbonIndices[output]);
                return vk::PassExecuteCallback{[this, passIndex](vk::RenderContext &context) {
                    if (m_framePending && m_ribbonTopology)
                        m_ribbonTopology->RecordScatter(context.GetComputeCommandEncoder(), passIndex);
                }};
            });
        }
    }

    graph.AddComputePass(StageName(namePrefix, "BoundsReset"), [&](vk::PassBuilder &builder) {
        builder.ReadStorageBuffer(instances);
        builder.ReadStorageBuffer(renderIndices);
        builder.ReadStorageBuffer(indirect);
        builder.ReadStorageBuffer(simulationControl);
        boundsBuffer = builder.WriteStorageBuffer(boundsBuffer);
        boundsDispatch = builder.WriteStorageBuffer(boundsDispatch);
        return [this](vk::RenderContext &context) {
            if (!m_framePending || !m_bounds)
                return;
            m_bounds->RecordReset(context.GetComputeCommandEncoder(), m_request.boundsMode, m_request.manualBoundsLower,
                                  m_request.manualBoundsUpper);
        };
    });

    graph.AddComputePass(StageName(namePrefix, "BoundsReduce"), [&](vk::PassBuilder &builder) {
        builder.ReadStorageBuffer(instances);
        builder.ReadStorageBuffer(renderIndices);
        builder.ReadStorageBuffer(indirect);
        builder.ReadStorageBuffer(simulationControl);
        boundsBuffer = builder.ReadWrite(boundsBuffer, rhi::PipelineStage::ComputeShader);
        builder.ReadIndirectBuffer(boundsDispatch);
        return [this](vk::RenderContext &context) {
            if (!m_framePending)
                return;
            if (m_bounds)
                m_bounds->RecordReduce(context.GetComputeCommandEncoder());
            m_lastConsumedFrame = m_request.frameIndex;
            m_lastConsumedSubstep = m_request.substepIndex;
            m_hasConsumedFrame = true;
            m_framePending = false;
            m_resetPending = false;
        };
    });

    m_outputs = {instances, renderIndices, indirect, boundsBuffer};
    return m_outputs.IsValid();
}

bool ParticleRenderGraph::IsFrameRequestValid(const GpuParticleFrameRequest &request) noexcept
{
    if (!std::isfinite(request.deltaTime) || request.deltaTime < 0.0f)
        return false;
    if (request.boundsMode != GpuParticleBoundsMode::Automatic && request.boundsMode != GpuParticleBoundsMode::Manual)
        return false;
    if (request.offscreenPolicy != GpuParticleOffscreenPolicy::AlwaysSimulate &&
        request.offscreenPolicy != GpuParticleOffscreenPolicy::PauseWhenOffscreen)
        return false;
    if (request.boundsMode == GpuParticleBoundsMode::Manual) {
        for (size_t axis = 0; axis < request.manualBoundsLower.size(); ++axis) {
            if (!std::isfinite(request.manualBoundsLower[axis]) || !std::isfinite(request.manualBoundsUpper[axis]) ||
                request.manualBoundsLower[axis] > request.manualBoundsUpper[axis])
                return false;
        }
    }
    return true;
}

bool ParticleRenderGraph::CanBeginFrame(const GpuParticleFrameRequest &request) const noexcept
{
    if (!IsAttached() || !m_runtime->IsValid() || !IsFrameRequestValid(request) ||
        (m_framePending && !m_resetPending && request.frameIndex == m_request.frameIndex &&
         request.substepIndex <= m_request.substepIndex) ||
        (m_hasConsumedFrame && request.frameIndex == m_lastConsumedFrame &&
         request.substepIndex <= m_lastConsumedSubstep))
        return false;
    return true;
}

bool ParticleRenderGraph::BeginFrame(const GpuParticleFrameRequest &request) noexcept
{
    if (!CanBeginFrame(request))
        return false;
    m_request = request;
    m_framePending = true;
    // Reset owns the next graph execution even if the control plane resumes
    // simulation before that execution is recorded.  SpawnPrepare must see
    // the reset flag once so bootstrap clears resident state and stale queued
    // requests cannot share a frame with the restarted emitter.
    return true;
}

void ParticleRenderGraph::Reset() noexcept
{
    if (m_migrationPending) {
        m_migrationPending = false;
        m_migrationCompleted = true;
    }
    if (m_runtime)
        m_runtime->RequestBootstrap();
    if (m_runtime)
        m_runtime->RequestContinuationReset();
    m_bootstrapPending = true;
    // Reset is also the Stop contract. Arm a render-graph execution so the
    // bootstrap pass clears resident counters and indirect draws even when
    // the caller will no longer submit simulation frames after stopping.
    m_request.spawnCount = 0;
    m_request.deltaTime = 0.0f;
    m_request.simulate = false;
    m_request.render = false;
    m_framePending = m_runtime != nullptr;
    m_resetPending = m_framePending;
    m_hasConsumedFrame = false;
    m_lastConsumedFrame = 0;
    m_lastConsumedSubstep = 0;
}

bool ParticleRenderGraph::ConsumeMigrationCompletion() noexcept
{
    if (!m_migrationCompleted)
        return false;
    m_migrationCompleted = false;
    m_migrator = nullptr;
    return true;
}

} // namespace infernux::particle
