#include "ParticleRenderGraph.h"

#include <algorithm>
#include <cmath>

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

} // namespace

bool ParticleRenderGraph::Attach(vk::RenderGraph &graph, ParticleGpuRuntime &runtime, ParticleGpuBounds &bounds,
                                 const std::string &namePrefix, ParticleGpuMigrator *migration,
                                 ParticleGpuRibbonTopology *ribbonTopology, ParticleGpuEventDomain *eventDomain)
{
    if (IsAttached() || !runtime.IsValid() || !bounds.IsValid() ||
        bounds.InstanceBuffer() != runtime.InstanceBuffer() ||
        bounds.SourceIndirectBuffer() != runtime.IndirectBuffer() || namePrefix.empty() || runtime.StateStride() == 0 ||
        (migration && (!migration->IsValid() || migration->DestinationStateBuffer() != runtime.StateBuffer() ||
                       migration->DestinationFreeListBuffer() != runtime.FreeListBuffer() ||
                       migration->DestinationCounterBuffer() != runtime.CounterBuffer())) ||
        (ribbonTopology &&
         (!ribbonTopology->IsValid() || ribbonTopology->InstanceBuffer() != runtime.InstanceBuffer() ||
          ribbonTopology->SourceIndexBuffer() != runtime.RenderIndexBuffer() ||
          ribbonTopology->SourceIndirectBuffer() != runtime.IndirectBuffer())) ||
        ((runtime.HasEventOutput(GpuKernelStage::Init) || runtime.HasEventOutput(GpuKernelStage::Update) ||
          runtime.HasEventOutput(GpuKernelStage::Rendering)) &&
         (!eventDomain || !eventDomain->IsValid())))
        return false;

    m_runtime = &runtime;
    m_bounds = &bounds;
    m_migrator = migration;
    m_ribbonTopology = ribbonTopology;
    m_eventDomain = eventDomain;
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
    std::vector<vk::ResourceHandle> eventRecords;
    std::vector<vk::ResourceHandle> eventCounters;

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
        if (eventDomain) {
            eventRecords.reserve(eventDomain->PageCount());
            eventCounters.reserve(eventDomain->PageCount());
            for (uint32_t pageIndex = 0; pageIndex < eventDomain->PageCount(); ++pageIndex) {
                const auto *page = eventDomain->Page(pageIndex);
                if (!page)
                    return vk::PassExecuteCallback{};
                eventRecords.push_back(
                    builder.ImportBuffer(StageName(namePrefix, "EventRecords") + std::to_string(pageIndex),
                                         page->records, eventDomain->RecordBufferBytes()));
                eventCounters.push_back(builder.ImportBuffer(
                    StageName(namePrefix, "EventCounters") + std::to_string(pageIndex), page->counters,
                    uint64_t(eventDomain->ChannelCount()) * sizeof(GpuParticleEventCounter)));
            }
        }
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
            m_runtime->RecordBootstrap(context.GetComputeCommandEncoder(), m_request.systemSeed);
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
          !continuationJoinStates.IsValid())) ||
        std::any_of(eventRecords.begin(), eventRecords.end(), [](const auto &handle) { return !handle.IsValid(); }) ||
        std::any_of(eventCounters.begin(), eventCounters.end(), [](const auto &handle) { return !handle.IsValid(); })) {
        m_runtime = nullptr;
        m_bounds = nullptr;
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
        if (runtime.HasEventOutput(GpuKernelStage::Init)) {
            for (auto &handle : eventRecords)
                handle = builder.ReadWrite(handle, rhi::PipelineStage::ComputeShader);
            for (auto &handle : eventCounters)
                handle = builder.ReadWrite(handle, rhi::PipelineStage::ComputeShader);
        }
        return [this](vk::RenderContext &context) {
            if (!m_framePending || !m_request.simulate || m_request.spawnCount == 0 || !m_runtime)
                return;
            m_runtime->RecordInit(context.GetComputeCommandEncoder(), m_request.spawnCount, m_request.spawnBaseId,
                                  m_request.spawnGeneration, m_request.systemSeed, m_request.simulationStep,
                                  m_request.deltaTime,
                                  m_eventDomain ? m_eventDomain->CurrentEventOutputGroup() : rhi::BindGroupHandle{});
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
                    m_request.systemSeed, m_request.deltaTime,
                    m_eventDomain ? m_eventDomain->CurrentEventOutputGroup() : rhi::BindGroupHandle{});
            };
        });
    }

    graph.AddComputePass(StageName(namePrefix, "Update"), [&](vk::PassBuilder &builder) {
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
        if (runtime.HasEventOutput(GpuKernelStage::Update)) {
            for (auto &handle : eventRecords)
                handle = builder.ReadWrite(handle, rhi::PipelineStage::ComputeShader);
            for (auto &handle : eventCounters)
                handle = builder.ReadWrite(handle, rhi::PipelineStage::ComputeShader);
        }
        return [this](vk::RenderContext &context) {
            if (!m_framePending || !m_request.simulate || !m_runtime)
                return;
            m_runtime->RecordUpdate(context.GetComputeCommandEncoder(), m_request.systemSeed, m_request.simulationStep,
                                    m_request.deltaTime,
                                    m_eventDomain ? m_eventDomain->CurrentEventOutputGroup() : rhi::BindGroupHandle{});
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
                m_runtime->RecordRenderReset(context.GetComputeCommandEncoder());
            };
        });
    m_renderExportPassId = renderExportBoundary.id;
    graph.SetSubmissionBoundaryBefore(renderExportBoundary);

    graph.AddComputePass(StageName(namePrefix, "Rendering"), [&](vk::PassBuilder &builder) {
        states = builder.ReadWrite(states, rhi::PipelineStage::ComputeShader);
        freeList = builder.ReadWrite(freeList, rhi::PipelineStage::ComputeShader);
        counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
        instances = builder.ReadWrite(instances, rhi::PipelineStage::ComputeShader);
        renderIndices = builder.ReadWrite(renderIndices, rhi::PipelineStage::ComputeShader);
        indirect = builder.ReadWrite(indirect, rhi::PipelineStage::ComputeShader);
        builder.ReadUniformBuffer(transforms);
        builder.ReadStorageBuffer(simulationControl);
        if (runtime.HasEventOutput(GpuKernelStage::Rendering)) {
            for (auto &handle : eventRecords)
                handle = builder.ReadWrite(handle, rhi::PipelineStage::ComputeShader);
            for (auto &handle : eventCounters)
                handle = builder.ReadWrite(handle, rhi::PipelineStage::ComputeShader);
        }
        return [this](vk::RenderContext &context) {
            if (!m_framePending)
                return;
            if (m_request.render && m_runtime) {
                m_runtime->RecordRendering(
                    context.GetComputeCommandEncoder(), m_request.systemSeed, m_request.simulationStep,
                    m_eventDomain ? m_eventDomain->CurrentEventOutputGroup() : rhi::BindGroupHandle{},
                    m_request.simulate);
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
            m_hasConsumedFrame = true;
            m_framePending = false;
            m_resetPending = false;
        };
    });

    m_outputs = {instances, renderIndices, indirect, boundsBuffer};
    return m_outputs.IsValid();
}

bool ParticleRenderGraph::CanBeginFrame(const GpuParticleFrameRequest &request) const noexcept
{
    if (!IsAttached() || !m_runtime->IsValid() || !std::isfinite(request.deltaTime) || request.deltaTime < 0.0f ||
        (m_framePending && !m_resetPending && request.frameIndex == m_request.frameIndex) ||
        (m_hasConsumedFrame && request.frameIndex == m_lastConsumedFrame))
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

bool ParticleRenderGraph::BeginFrame(const GpuParticleFrameRequest &request) noexcept
{
    if (!CanBeginFrame(request))
        return false;
    m_request = request;
    m_framePending = true;
    m_resetPending = false;
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
