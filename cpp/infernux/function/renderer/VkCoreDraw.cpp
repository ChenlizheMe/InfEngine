/**
 * @file VkCoreDraw.cpp
 * @brief InxVkCoreModular — Drawing and per-object buffer management
 *
 * Split from InxVkCoreModular.cpp for maintainability.
 * Contains: DrawFrame, DrawSceneFiltered,
 *           SetDrawCalls, EnsureObjectBuffers, CleanupUnusedBuffers.
 */

#include "InxError.h"
#include "InxVkCoreModular.h"
#include "ProfileConfig.h"
#include "SceneRenderGraph.h"
#include "vk/DescriptorBindTrace.h"
#include "vk/RhiVulkanTypes.h"
#include "vk/VkRenderUtils.h"
#include "vk/VkTypes.h"

#include <function/renderer/Frustum.h>
#include <function/renderer/shader/ShaderProgram.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/scene/LightingData.h>

#include <SDL3/SDL.h>
#include <glm/glm.hpp>
#include <glm/gtc/matrix_transform.hpp>

#include <algorithm>
#include <array>
#include <chrono>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace infernux
{

namespace
{

struct alignas(16) ShadowPassUniformData
{
    glm::mat4 model{1.0f};
    glm::mat4 view{1.0f};
    glm::mat4 projection{1.0f};
    glm::vec4 lightVector{}; ///< xyz = direction toward light or local-light position; w = position flag
    glm::vec4 bias{};        ///< xy = depth/normal bias in texels, z = world texel size, w = far plane
};

static_assert(sizeof(ShadowPassUniformData) == 224);

} // namespace

// ============================================================================
// Rendering
// ============================================================================

void InxVkCoreModular::DrawFrame(const float *viewPos, const float *viewLookAt, const float *viewUp)
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    auto _t0 = Clock::now();
    auto _tPrev = _t0;
    auto _tNow = _t0;
#endif

    // Skip rendering when the window is minimized (zero extent).
    // Without this guard, vkAcquireNextImageKHR blocks indefinitely
    // because the swapchain has no presentable images at 0×0.
    {
        VkExtent2D ext = m_backend.Presentation().GetExtent();
        if (ext.width == 0 || ext.height == 0) {
            // Yield a bit so we don't spin-lock the CPU while minimized
            SDL_Delay(16);
            return;
        }
    }

    const uint32_t frameSlot = GetCurrentFrameSlot();

    // Acquire next swapchain image using the renderer-owned frame slot.
    uint32_t imageIndex;
    auto result = m_backend.Presentation().AcquireNextImage(frameSlot, imageIndex);

    if (result == vk::SwapchainResult::SurfaceLost) {
        m_presentationSurfaceLost = true;
        INXLOG_WARN("Platform presentation surface was lost while acquiring an image; scheduling a surface rebind");
        return;
    }

    if (result == vk::SwapchainResult::NeedRecreate) {
        RecreateSwapchain();
        return;
    }

    if (result == vk::SwapchainResult::SkipFrame) {
        return;
    }

    if (result == vk::SwapchainResult::Error) {
        INXLOG_ERROR("Failed to acquire swapchain image");
        return;
    }
#if INFERNUX_FRAME_PROFILE
    _tNow = Clock::now();
    m_drawSubMs[0] += std::chrono::duration<double, std::milli>(_tNow - _tPrev).count();
    _tPrev = _tNow;
#endif

    rhi::SubmissionPlan submissionPlan;
    std::string submissionPlanError;
    const auto graphicsQueue = m_backend.Queues().GetSnapshot(rhi::QueueRole::Graphics);
    const auto computeQueue = m_backend.Queues().GetSnapshot(rhi::QueueRole::Compute);
    const bool independentCompute = graphicsQueue.queue != VK_NULL_HANDLE && computeQueue.queue != VK_NULL_HANDLE &&
                                    graphicsQueue.nativeLane != UINT32_MAX && computeQueue.nativeLane != UINT32_MAX &&
                                    graphicsQueue.nativeLane != computeQueue.nativeLane;
    const bool partitionedCompute = independentCompute && m_frameAsyncSimulationExecutor &&
                                    m_frameAsyncExportExecutor && m_framePartitionedComputeReady &&
                                    m_framePartitionedComputeReady();
    const bool asyncCompute =
        partitionedCompute && m_frameAsyncComputeReady && m_frameAsyncComputeGeneration && m_frameAsyncComputeReady();
    if (!asyncCompute) {
        // Losing async readiness is a lifecycle boundary (Play/Stop, reset,
        // graph removal, or a temporarily empty scheduler). The next async
        // frame must prime exported state again. Keep the last timeline: it is
        // cheap once signaled and is the only exact dependency on old resident
        // particle resources when readiness returns.
        m_frameAsyncComputePrimed = false;
        m_frameAsyncComputePrimedGeneration = 0;
    }
    const uint64_t asyncComputeGeneration = asyncCompute ? m_frameAsyncComputeGeneration() : 0;
    const bool primeAsyncCompute =
        asyncCompute && (!m_frameAsyncComputePrimed || asyncComputeGeneration != m_frameAsyncComputePrimedGeneration);
    const bool frameComputeHasWork =
        m_frameComputeExecutor && (!m_frameComputeWorkPredicate || m_frameComputeWorkPredicate());
    const bool separateComputeBatch = !asyncCompute && frameComputeHasWork && partitionedCompute;

    const bool composedFrame = static_cast<bool>(m_frameSubmissionBuilder);
    if (composedFrame) {
        if (!EnsureGuiRenderGraph(imageIndex))
            return;

        m_frameSubmission.Reset();
        const rhi::DeviceId device = m_backend.Device().GetDeviceId();
        std::vector<uint32_t> setupDependencies;
        if (m_framePreSetupBuilder && !m_framePreSetupBuilder(m_frameSubmission, setupDependencies)) {
            INXLOG_ERROR("Failed to publish pre-setup queue ownership releases");
            return;
        }

        uint32_t particleComputeWork = 0;
        const bool splitParticleCompute = primeAsyncCompute || (separateComputeBatch && partitionedCompute);
        if (splitParticleCompute) {
            // OwnershipRelease must be submitted before PrimeSimulation waits
            // on it. The previous order queued compute first; when Scene/Game
            // views were hidden that frame, the release never existed and the
            // compute acquire sat on exclusive Graphics-owned buffers.
            const uint32_t primeSimulation = m_frameSubmission.AddWork(
                device, rhi::QueueRole::Compute, rhi::SubmissionDomain::Frame, rhi::InvalidRenderViewId,
                rhi::PipelineStage::ComputeShader, setupDependencies,
                [this](VkCommandBuffer commandBuffer) { return m_frameAsyncSimulationExecutor(commandBuffer); },
                "GpuParticle/PrimeSimulation");
            particleComputeWork = m_frameSubmission.AddWork(
                device, rhi::QueueRole::Compute, rhi::SubmissionDomain::Frame, rhi::InvalidRenderViewId,
                rhi::PipelineStage::ComputeShader, {primeSimulation},
                [this](VkCommandBuffer commandBuffer) { return m_frameAsyncExportExecutor(commandBuffer); },
                "GpuParticle/PrimeExport");
            // Play -> Stop -> Play often has preroll, so CanExecuteAsync is
            // false and the old path recorded the entire particle graph into
            // one GpuParticle/Simulation buffer. That erased the compiled
            // sim/export boundary and tripped the 1000 ms frame watchdog.
            // Keep the same two-submission split as async prime.
        } else if (separateComputeBatch) {
            particleComputeWork = m_frameSubmission.AddWork(
                device, rhi::QueueRole::Compute, rhi::SubmissionDomain::Frame, rhi::InvalidRenderViewId,
                rhi::PipelineStage::ComputeShader, setupDependencies,
                [this](VkCommandBuffer commandBuffer) {
                    m_frameComputeExecutor(commandBuffer);
                    return true;
                },
                "GpuParticle/Simulation");
        } else if (!asyncCompute && frameComputeHasWork) {
            // Small particle workloads stay on Graphics. This is not a CPU
            // fallback: the same GPU compute graph is recorded before the
            // camera graphs, but queue order replaces the expensive
            // previous-frame fence/timeline dependency. It also keeps all
            // particle buffers on one queue family, preserving the validation
            // guarantees that motivated the cross-frame guard.
            particleComputeWork = m_frameSubmission.AddWork(
                device, rhi::QueueRole::Graphics, rhi::SubmissionDomain::Frame, rhi::InvalidRenderViewId,
                rhi::PipelineStage::ComputeShader, setupDependencies,
                [this](VkCommandBuffer commandBuffer) {
                    m_frameComputeExecutor(commandBuffer);
                    return true;
                },
                "GpuParticle/InlineSimulation");
        }

        // Frame/Setup only updates globals. Waiting here for Simulation plus
        // queuing OwnershipRelease after Setup deadlocks the independent
        // compute family when a newly selected emitter first touches exclusive
        // Graphics buffers (Game-only six-way preview).
        const uint32_t setupWork = m_frameSubmission.AddWork(
            device, rhi::QueueRole::Graphics, rhi::SubmissionDomain::Frame, m_presentationView.id,
            rhi::PipelineStage::AllGraphics, std::move(setupDependencies),
            [this](VkCommandBuffer commandBuffer) {
#if INFERNUX_FRAME_PROFILE
                m_gpuTimestampQueries.BeginFrame(commandBuffer, m_currentFrame);
                m_composedFrameTimestampRegion =
                    m_gpuTimestampQueries.BeginRegion(commandBuffer, "Frame", VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT);
#endif
                CmdUpdateGlobals(commandBuffer);
                return true;
            },
            "Frame/Setup");

        if (!m_frameSubmissionBuilder(m_frameSubmission, setupWork, particleComputeWork)) {
            INXLOG_ERROR("Failed to compose frame RenderGraph submissions");
            return;
        }

        uint32_t simulationWork = 0;
        if (asyncCompute && !primeAsyncCompute) {
            // RenderGraphs consume the previous exported generation. Queueing
            // next-frame simulation after their view-local compute batches
            // lets simulation overlap the current Graphics lane.
            simulationWork = m_frameSubmission.AddWork(
                device, rhi::QueueRole::Compute, rhi::SubmissionDomain::Frame, rhi::InvalidRenderViewId,
                rhi::PipelineStage::ComputeShader, {},
                [this](VkCommandBuffer commandBuffer) { return m_frameAsyncSimulationExecutor(commandBuffer); },
                "GpuParticle/AsyncSimulation");
        }

        vk::RenderGraph &guiGraph = GetGuiRenderGraph(imageIndex);
        const auto guiRange =
            m_frameSubmission.AppendRenderGraph(guiGraph, {setupWork}, {}, [this](VkCommandBuffer commandBuffer) {
#if INFERNUX_FRAME_PROFILE
                m_gpuTimestampQueries.EndRegion(commandBuffer, m_composedFrameTimestampRegion,
                                                VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT);
                m_composedFrameTimestampRegion = {};
                m_gpuTimestampQueries.FinishFrame(m_currentFrame);
#endif
                return true;
            });
        if (guiRange.Empty()) {
            INXLOG_ERROR("Swapchain GUI RenderGraph produced no submission work");
            return;
        }

        if (asyncCompute && !primeAsyncCompute) {
            std::vector<uint32_t> exportDependencies{simulationWork};
            const uint32_t finalGraphics = m_frameSubmission.LastWork(rhi::QueueRole::Graphics);
            if (finalGraphics != 0)
                exportDependencies.push_back(finalGraphics);
            (void)m_frameSubmission.AddWork(
                device, rhi::QueueRole::Compute, rhi::SubmissionDomain::Frame, rhi::InvalidRenderViewId,
                rhi::PipelineStage::ComputeShader, std::move(exportDependencies),
                [this](VkCommandBuffer commandBuffer) { return m_frameAsyncExportExecutor(commandBuffer); },
                "GpuParticle/AsyncExport");
        }

        if (!m_frameSubmission.Build(submissionPlan, submissionPlanError)) {
            INXLOG_ERROR("Failed to build composed frame submission plan: ", submissionPlanError);
            return;
        }
    } else {
        std::vector<rhi::SubmissionWorkItem> frameWork;
        if (asyncCompute && !primeAsyncCompute) {
            frameWork.push_back({0,
                                 m_backend.Device().GetDeviceId(),
                                 rhi::QueueRole::Compute,
                                 rhi::SubmissionDomain::Frame,
                                 rhi::InvalidRenderViewId,
                                 rhi::PipelineStage::ComputeShader,
                                 {}});
            frameWork.push_back({1,
                                 m_backend.Device().GetDeviceId(),
                                 rhi::QueueRole::Graphics,
                                 rhi::SubmissionDomain::Frame,
                                 rhi::InvalidRenderViewId,
                                 rhi::PipelineStage::AllGraphics,
                                 {}});
            frameWork.push_back({2,
                                 m_backend.Device().GetDeviceId(),
                                 rhi::QueueRole::Compute,
                                 rhi::SubmissionDomain::Frame,
                                 rhi::InvalidRenderViewId,
                                 rhi::PipelineStage::ComputeShader,
                                 {0, 1}});
        } else if (primeAsyncCompute) {
            frameWork.push_back({0,
                                 m_backend.Device().GetDeviceId(),
                                 rhi::QueueRole::Compute,
                                 rhi::SubmissionDomain::Frame,
                                 rhi::InvalidRenderViewId,
                                 rhi::PipelineStage::ComputeShader,
                                 {}});
            frameWork.push_back({1,
                                 m_backend.Device().GetDeviceId(),
                                 rhi::QueueRole::Compute,
                                 rhi::SubmissionDomain::Frame,
                                 rhi::InvalidRenderViewId,
                                 rhi::PipelineStage::ComputeShader,
                                 {0}});
            frameWork.push_back({2,
                                 m_backend.Device().GetDeviceId(),
                                 rhi::QueueRole::Graphics,
                                 rhi::SubmissionDomain::Frame,
                                 rhi::InvalidRenderViewId,
                                 rhi::PipelineStage::AllGraphics,
                                 {1}});
        } else if (asyncCompute || separateComputeBatch) {
            frameWork.push_back({0,
                                 m_backend.Device().GetDeviceId(),
                                 rhi::QueueRole::Compute,
                                 rhi::SubmissionDomain::Frame,
                                 rhi::InvalidRenderViewId,
                                 rhi::PipelineStage::ComputeShader,
                                 {}});
            frameWork.push_back({1,
                                 m_backend.Device().GetDeviceId(),
                                 rhi::QueueRole::Graphics,
                                 rhi::SubmissionDomain::Frame,
                                 rhi::InvalidRenderViewId,
                                 rhi::PipelineStage::AllGraphics,
                                 {0}});
        } else {
            frameWork.push_back({0,
                                 m_backend.Device().GetDeviceId(),
                                 rhi::QueueRole::Graphics,
                                 rhi::SubmissionDomain::Frame,
                                 rhi::InvalidRenderViewId,
                                 rhi::PipelineStage::AllGraphics,
                                 {}});
        }
        if (!rhi::BuildSubmissionPlan(frameWork, submissionPlan, submissionPlanError)) {
            INXLOG_ERROR("Failed to build frame submission plan: ", submissionPlanError);
            return;
        }
    }
    if (submissionPlan.batches.empty()) {
        INXLOG_ERROR("Failed to build frame submission plan: ", submissionPlanError);
        return;
    }
    {
        const rhi::SubmissionPlanStatistics statistics = rhi::AnalyzeSubmissionPlan(submissionPlan);
        auto &telemetry = m_frameSubmissionTelemetry;
        ++telemetry.generation;
        telemetry.composed = composedFrame;
        telemetry.computeQueueIndependent = independentCompute;
        const auto transferQueue = m_backend.Queues().GetSnapshot(rhi::QueueRole::Transfer);
        telemetry.transferQueueIndependent =
            graphicsQueue.queue != VK_NULL_HANDLE && transferQueue.queue != VK_NULL_HANDLE &&
            graphicsQueue.nativeLane != UINT32_MAX && transferQueue.nativeLane != UINT32_MAX &&
            graphicsQueue.nativeLane != transferQueue.nativeLane;
        telemetry.asyncComputeActive = asyncCompute;
        telemetry.batchCount = statistics.batchCount;
        telemetry.graphicsBatchCount = statistics.graphicsBatchCount;
        telemetry.computeBatchCount = statistics.computeBatchCount;
        telemetry.transferBatchCount = statistics.transferBatchCount;
        telemetry.crossQueueDependencyCount = statistics.crossQueueDependencyCount;
        telemetry.unorderedComputeGraphicsPairCount = statistics.unorderedComputeGraphicsPairCount;
        telemetry.parallelComputeGraphics = independentCompute && statistics.unorderedComputeGraphicsPairCount != 0;
    }
    if (!m_backend.Queues().ResetGraphicsFrameFence(frameSlot)) {
        INXLOG_ERROR("Failed to reset graphics frame fence for slot ", frameSlot);
        return;
    }

    vk::VulkanSubmissionExecutor::ExternalSync externalSync{};
    externalSync.imageAvailable = m_backend.Presentation().GetImageAvailableSemaphore(frameSlot);
    externalSync.imageAvailableStages = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    externalSync.uploadTimeline = m_resourceManager.GetUploadTimelineSemaphore();
    externalSync.uploadTimelineValue = m_resourceManager.GetRequiredUploadTimelineValue();
    externalSync.renderFinished = m_backend.Presentation().GetRenderFinishedSemaphore(imageIndex);
    externalSync.completionFence = m_backend.Queues().GetGraphicsFrameFence(frameSlot);
    externalSync.completionEpoch = m_backend.Queues().GetFrameCompletionEpoch(frameSlot);
    if (asyncCompute || separateComputeBatch) {
        // Game-only Play -> Stop -> Play submits GpuParticle/Simulation on
        // Compute while the previous Game view still owns exported particle
        // buffers on Graphics. Async prime already waits the previous
        // timeline; the sync fallback must do the same or the compute lane
        // hangs inside exclusive queue-family ownership.
        externalSync.previousFrameTimeline = m_previousFrameCompletionTimeline;
        externalSync.previousFrameTimelineValue = m_previousFrameCompletionTimelineValue;
        externalSync.previousFrameStages = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
        externalSync.previousFrameWaitAtFirstBatch = primeAsyncCompute || separateComputeBatch;
    }

    vk::VulkanSubmissionExecutor::ExecuteResult executeResult{};
    try {
        executeResult = m_submissionExecutor.Execute(
            frameSlot, submissionPlan,
            [this, imageIndex, asyncCompute, primeAsyncCompute, separateComputeBatch, frameComputeHasWork,
             composedFrame, &submissionPlan](uint32_t batchIndex, VkCommandBuffer commandBuffer) {
                if (batchIndex >= submissionPlan.batches.size())
                    return false;
                if (composedFrame)
                    return m_frameSubmission.RecordBatch(submissionPlan, batchIndex, commandBuffer);
                const auto queue = submissionPlan.batches[batchIndex].queue;
                if (asyncCompute) {
                    if (primeAsyncCompute) {
                        if (batchIndex == 0) {
                            return m_frameAsyncSimulationExecutor(commandBuffer);
                        }
                        if (batchIndex == 1)
                            return m_frameAsyncExportExecutor(commandBuffer);
                        return batchIndex == 2 && RecordFrameCommands(commandBuffer, imageIndex);
                    }
                    if (batchIndex == 0)
                        return m_frameAsyncSimulationExecutor(commandBuffer);
                    if (batchIndex == 1)
                        return RecordFrameCommands(commandBuffer, imageIndex);
                    if (batchIndex == 2)
                        return m_frameAsyncExportExecutor(commandBuffer);
                    return false;
                }
                if (queue == rhi::QueueRole::Compute && separateComputeBatch) {
                    m_frameComputeExecutor(commandBuffer);
                    return true;
                }
                if (queue != rhi::QueueRole::Graphics)
                    return false;
                if (!separateComputeBatch && frameComputeHasWork)
                    m_frameComputeExecutor(commandBuffer);
                return RecordFrameCommands(commandBuffer, imageIndex);
            },
            externalSync);
    } catch (...) {
        if (!m_backend.Queues().AbandonGraphicsFrameSlot(frameSlot))
            INXLOG_ERROR("Failed to restore graphics frame slot after command recording exception");
        throw;
    }
#if INFERNUX_FRAME_PROFILE
    _tNow = Clock::now();
    m_drawSubMs[1] += std::chrono::duration<double, std::milli>(_tNow - _tPrev).count();
    _tPrev = _tNow;
#endif

    const VkResult submitResult = executeResult.result;
    if (submitResult != VK_SUCCESS) {
        if (!m_backend.Queues().AbandonGraphicsFrameSlot(frameSlot))
            INXLOG_ERROR("Failed to restore graphics frame fence after submission failure");
        // DEVICE_LOST cascades produce one failure per frame; throttle so the
        // Console does not flood and hide the first useful diagnostic.
        static int s_submitFailLogs = 0;
        if (s_submitFailLogs < 3) {
            INXLOG_ERROR("Failed to submit draw command buffer: ", vk::VkResultToString(submitResult));
        } else if (s_submitFailLogs == 3) {
            INXLOG_ERROR("Further draw-command submit failures suppressed (device likely lost)");
        }
        ++s_submitFailLogs;
        return;
    } else {
        (void)m_backend.Queues().AssociateFrameSlot(frameSlot, executeResult.completionTicket);
        if (asyncCompute)
            m_frameAsyncComputePrimed = true;
        if (asyncCompute)
            m_frameAsyncComputePrimedGeneration = asyncComputeGeneration;
        // Always retain the previous frame's terminal completion point. The
        // next frame may enter async particle execution after an edit/play or
        // graph-readiness transition, while this frame's Graphics lane still
        // consumes the same exported indirect buffers.
        m_previousFrameCompletionTimeline = executeResult.completionTimeline;
        m_previousFrameCompletionTimelineValue = executeResult.completionTimelineValue;
    }
#if INFERNUX_FRAME_PROFILE
    if (submitResult == VK_SUCCESS) {
        m_gpuTimestampQueries.MarkSubmitted(m_currentFrame);
    }
#endif
#if INFERNUX_FRAME_PROFILE
    _tNow = Clock::now();
    m_drawSubMs[2] += std::chrono::duration<double, std::milli>(_tNow - _tPrev).count();
    _tPrev = _tNow;
#endif

    // Present
    result = m_backend.Presentation().Present(m_backend.Queues(), imageIndex);
    if (result == vk::SwapchainResult::SurfaceLost) {
        m_presentationSurfaceLost = true;
        INXLOG_WARN("Platform presentation surface was lost while presenting; scheduling a surface rebind");
    } else if (result == vk::SwapchainResult::NeedRecreate || m_framebufferResized) {
        m_framebufferResized = false;
        RecreateSwapchain();
    }
#if INFERNUX_FRAME_PROFILE
    _tNow = Clock::now();
    m_drawSubMs[3] += std::chrono::duration<double, std::milli>(_tNow - _tPrev).count();

    ++m_drawSubCount;
#endif

    // Advance frame
    m_currentFrame = (m_currentFrame + 1) % m_maxFramesInFlight;
}

void InxVkCoreModular::SetDrawCalls(const std::vector<DrawCall> *drawCalls, bool forceRefresh)
{
    if (!forceRefresh && m_drawCallsPtr == drawCalls && m_drawListMetadataSource == drawCalls &&
        m_drawListBufferRevision == m_objectBufferRevision &&
        (!drawCalls || m_drawListMetadata.size() == drawCalls->size()))
        return;

    ++m_drawListActivation;
    m_staticInstanceRanges.clear();
    m_staticFilteredListCaches.clear();
    m_shadowScratchValid = false;
    m_drawCallsPtr = drawCalls;
    m_drawListMetadataSource = drawCalls;
    m_drawListBufferRevision = 0;
    m_drawListMetadata.clear();
    m_skyboxDrawListSource = drawCalls;
    m_skyboxDrawCallIndices.clear();

    // Refresh cached builtin materials (avoids string-hash lookup per DrawSceneFiltered call)
    if (!m_cachedDefaultLit) {
        m_cachedDefaultLit = AssetRegistry::Instance().GetBuiltinMaterial("DefaultLit");
        m_cachedErrorMat = AssetRegistry::Instance().GetBuiltinMaterial("ErrorMaterial");
    }

    // Track only the small unique queue set. Sorting one queue value per
    // DrawCall costs more than the empty render-pass scans this replaces.
    m_drawQueueValues.clear();
    m_drawQueueValuesOverflow = false;
    if (!drawCalls)
        return;
    constexpr size_t kTrackedQueueLimit = 16;
    m_drawListMetadata.reserve(drawCalls->size());
    m_skyboxDrawCallIndices.reserve(1);
    m_drawQueueValues.reserve(kTrackedQueueLimit);
    for (size_t drawCallIndex = 0; drawCallIndex < drawCalls->size(); ++drawCallIndex) {
        const DrawCall &drawCall = (*drawCalls)[drawCallIndex];
        InxMaterial *material = drawCall.material ? drawCall.material.get() : m_cachedDefaultLit.get();
        const int queue = material ? material->GetRenderQueue() : 2000;
        if (drawCall.identity.domain == RenderDomain::Skybox)
            m_skyboxDrawCallIndices.push_back(drawCallIndex);
        const auto bufferIt = m_perObjectBuffers.find(drawCall.objectId);
        if (bufferIt != m_perObjectBuffers.end()) {
            m_drawListMetadata.push_back({drawCall.objectId, material, queue, bufferIt->second.vertexBuffer,
                                          bufferIt->second.indexBuffer, bufferIt->second.indexCount});
        } else {
            m_drawListMetadata.push_back({drawCall.objectId, material, queue, {}, {}, 0});
        }
        if (!material)
            continue;
        if (m_drawQueueValuesOverflow)
            continue;
        if (std::find(m_drawQueueValues.begin(), m_drawQueueValues.end(), queue) != m_drawQueueValues.end())
            continue;
        if (m_drawQueueValues.size() == kTrackedQueueLimit) {
            m_drawQueueValuesOverflow = true;
            m_drawQueueValues.clear();
            // Keep building per-draw metadata after the small queue-summary
            // optimization overflows; DrawSceneFiltered still benefits from
            // the cached buffer entries on large/mixed queue lists.
            continue;
        }
        m_drawQueueValues.push_back(queue);
    }
    m_drawListBufferRevision = m_objectBufferRevision;
}

void InxVkCoreModular::SetShadowDrawCalls(const std::vector<DrawCall> *drawCalls, bool forceRefresh)
{
    if (!forceRefresh && m_shadowDrawCallsPtr == drawCalls && m_shadowListMetadataSource == drawCalls &&
        m_shadowListBufferRevision == m_objectBufferRevision &&
        (!drawCalls || m_shadowListMetadata.size() == drawCalls->size()))
        return;

    m_shadowDrawCallsPtr = drawCalls;
    m_shadowScratchValid = false;
    m_shadowListMetadataSource = drawCalls;
    m_shadowListBufferRevision = 0;
    m_shadowListMetadata.clear();
    if (!drawCalls)
        return;

    m_shadowListMetadata.reserve(drawCalls->size());
    for (const DrawCall &drawCall : *drawCalls) {
        InxMaterial *material = drawCall.material ? drawCall.material.get() : m_cachedDefaultLit.get();
        const auto bufferIt = m_perObjectBuffers.find(drawCall.objectId);
        if (bufferIt != m_perObjectBuffers.end()) {
            m_shadowListMetadata.push_back({drawCall.objectId, material, material ? material->GetRenderQueue() : 2000,
                                            bufferIt->second.vertexBuffer, bufferIt->second.indexBuffer,
                                            bufferIt->second.indexCount});
        } else {
            m_shadowListMetadata.push_back(
                {drawCall.objectId, material, material ? material->GetRenderQueue() : 2000, {}, {}, 0});
        }
    }
    m_shadowListBufferRevision = m_objectBufferRevision;
}

void InxVkCoreModular::ReleaseActiveDrawLists() noexcept
{
    m_drawCallsPtr = nullptr;
    m_shadowDrawCallsPtr = nullptr;
    m_drawListMetadataSource = nullptr;
    m_shadowListMetadataSource = nullptr;
    m_skyboxDrawListSource = nullptr;
    m_drawListBufferRevision = 0;
    m_shadowListBufferRevision = 0;

    m_drawListMetadata.clear();
    m_shadowListMetadata.clear();
    m_skyboxDrawCallIndices.clear();
    m_drawQueueValues.clear();
    m_drawQueueValuesOverflow = false;
    m_staticInstanceRanges.clear();
    m_staticFilteredListCaches.clear();
    m_staticInstanceRangeFrame = UINT64_MAX;
    m_drawListActivation = 0;

    m_eligibleScratch.clear();
    m_shadowDrawScratch.clear();
    m_shadowCullGroups.clear();
    m_shadowAllVisible.clear();
    m_shadowScratchValid = false;
    m_shadowScratchStatic = false;
    m_shadowScratchUniformBatch = false;
    m_shadowViewVisible.clear();
    m_resolvedShadowMaterialsScratch.clear();
    m_cachedDefaultLit.reset();
    m_cachedErrorMat.reset();
}

// ============================================================================
// Filtered draw — renders only draw calls within a queue range
// ============================================================================

void InxVkCoreModular::DrawSceneFiltered(VkCommandBuffer cmdBuf, uint32_t width, uint32_t height,
                                         rhi::BindGroupHandle perViewGroup, const glm::mat4 &viewMatrix, int queueMin,
                                         int queueMax, const std::string &sortMode, const std::string &overrideMaterial,
                                         const std::string &passTag,
                                         const MaterialPassPipelineDescriptor *pipelineDescriptor,
                                         GraphMaterialFilter materialFilter)
{
    const MaterialPassPipelineDescriptor activePass =
        pipelineDescriptor ? *pipelineDescriptor : m_materialPipelineManager.GetDefaultPassPipelineDescriptor();
    if (!activePass.IsValid()) {
        INXLOG_ERROR("DrawSceneFiltered received an invalid ", ShaderCompileTargetName(activePass.target),
                     " pass pipeline descriptor");
        return;
    }
    const VkDescriptorSet perViewDescriptorSet = m_backend.Device().GetRhiDevice().Resolve(perViewGroup);
    if (perViewDescriptorSet == VK_NULL_HANDLE) {
        INXLOG_ERROR("DrawSceneFiltered received an invalid per-view bind group");
        return;
    }
    // One-shot diagnostic: log queue-range filtering for first N frames
    static int s_filterDiagFrames = 0;

#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    const auto totalStart = Clock::now();
    auto stageStart = totalStart;
#endif

    // Fast early-out when no draw calls are staged
    if (drawCalls().empty())
        return;

    if (!m_drawQueueValuesOverflow) {
        const bool queuePresent =
            std::any_of(m_drawQueueValues.begin(), m_drawQueueValues.end(),
                        [queueMin, queueMax](int queue) { return queue >= queueMin && queue <= queueMax; });
        if (!queuePresent)
            return;
    }
#if INFERNUX_FRAME_PROFILE
    ++m_drawSceneFilteredCalls;
#endif

    VkViewport viewport{};
    viewport.x = 0.0f;
    viewport.y = 0.0f;
    viewport.width = static_cast<float>(width);
    viewport.height = static_cast<float>(height);
    viewport.minDepth = 0.0f;
    viewport.maxDepth = 1.0f;
    vkCmdSetViewport(cmdBuf, 0, 1, &viewport);

    VkRect2D scissor{};
    scissor.offset = {0, 0};
    scissor.extent = {width, height};
    vkCmdSetScissor(cmdBuf, 0, 1, &scissor);

    const auto &defaultMaterial = m_cachedDefaultLit;
    const auto &errorMaterial = m_cachedErrorMat;
    if (!defaultMaterial) {
        return;
    }

    // Resolve override material (if specified)
    InxMaterial *overrideMatRaw = nullptr;
    std::shared_ptr<InxMaterial> overrideMatOwner; // keeps alive during this scope
    if (!overrideMaterial.empty()) {
        auto &registry = AssetRegistry::Instance();
        overrideMatOwner = registry.GetBuiltinMaterial(overrideMaterial);
        if (!overrideMatOwner)
            overrideMatOwner = registry.LoadAsset<InxMaterial>(overrideMaterial, ResourceType::Material);
        overrideMatRaw = overrideMatOwner.get();
    }

    InxMaterial *defaultMatRaw = defaultMaterial.get();

    // ---- Collect eligible draw calls (queue filter + frustum cull) ----
    m_eligibleScratch.clear();

    const auto &activeDrawCalls = drawCalls();
    // SceneRenderGraph marks its skybox callback explicitly. Do not infer this
    // from a queue number: user-authored pipelines are allowed to reuse any
    // queue value.
    const bool skyboxPass = passTag == "__infernux_internal_skybox";
    const uint64_t materialPublicationGeneration = m_materialPipelineManager.GetPublicationGeneration();
    auto staticFilterCache = m_staticFilteredListCaches.end();
    if (overrideMaterial.empty() && sortMode != "back_to_front") {
        staticFilterCache =
            std::find_if(m_staticFilteredListCaches.begin(), m_staticFilteredListCaches.end(),
                         [&](const StaticFilteredListCache &cache) {
                             return cache.drawListActivation == m_drawListActivation &&
                                    cache.materialPublicationGeneration == materialPublicationGeneration &&
                                    cache.queueMin == queueMin && cache.queueMax == queueMax &&
                                    cache.target == activePass.target && cache.materialFilter == materialFilter &&
                                    cache.sortMode == sortMode && cache.passTag == passTag;
                         });
    }
    const bool reusedStaticFilter = staticFilterCache != m_staticFilteredListCaches.end();
    uint64_t staticFilteredSequenceKey = reusedStaticFilter ? staticFilterCache->sequenceKey : 0;
    const bool hasListMetadata = m_drawListMetadataSource == m_drawCallsPtr &&
                                 m_drawListMetadata.size() == activeDrawCalls.size() &&
                                 m_drawListBufferRevision == m_objectBufferRevision;
    // This is only populated when an unusual caller stages draw calls before
    // their object buffers become available. It preserves ownership for the
    // remainder of this recording without adding shared_ptr traffic to the
    // normal per-pass loop.
    std::vector<DrawListMetadata> fallbackBufferLeases;
    if (!hasListMetadata)
        fallbackBufferLeases.reserve(activeDrawCalls.size());
    const bool hasCachedSkyboxIndices =
        skyboxPass && m_skyboxDrawListSource == m_drawCallsPtr && !m_skyboxDrawCallIndices.empty();
    const size_t candidateCount = hasCachedSkyboxIndices ? m_skyboxDrawCallIndices.size() : activeDrawCalls.size();
    size_t filterCandidateCount = 0;
    for (size_t candidateIndex = 0; !reusedStaticFilter && candidateIndex < candidateCount; ++candidateIndex) {
        const size_t drawCallIndex = hasCachedSkyboxIndices ? m_skyboxDrawCallIndices[candidateIndex] : candidateIndex;
        ++filterCandidateCount;
        const DrawCall &dc = activeDrawCalls[drawCallIndex];
        const DrawListMetadata *metadata = hasListMetadata ? &m_drawListMetadata[drawCallIndex] : nullptr;
        const InxMaterial *expectedMaterial = dc.material ? dc.material.get() : m_cachedDefaultLit.get();
        const uint64_t requiredIndexEnd = static_cast<uint64_t>(dc.indexStart) + dc.indexCount;
        if (metadata && (metadata->objectId != dc.objectId || metadata->material != expectedMaterial))
            metadata = nullptr;
        // The lease can still hold the previous published geometry of an
        // every-frame dynamic mesh whose fresh upload has not been published
        // yet (async/fence transfer). A whole-buffer draw (inline meshes use
        // indexStart==0, vertexStart==0) can safely present the complete
        // previous geometry for that frame; dropping the object instead makes
        // moving LineRenderer trails flicker on every growth frame.
        uint32_t indexCountClamp = 0;
        if (metadata && requiredIndexEnd > metadata->indexCapacity) {
            if (dc.indexStart == 0 && dc.vertexStart == 0 && metadata->indexCapacity > 0 && metadata->vertexBuffer &&
                metadata->indexBuffer) {
                indexCountClamp = static_cast<uint32_t>(metadata->indexCapacity);
            } else {
                metadata = nullptr;
            }
        }
        if (!dc.frustumVisible)
            continue;
        if (skyboxPass && dc.identity.domain != RenderDomain::Skybox)
            continue;

        const std::shared_ptr<InxMaterial> *materialOwner =
            overrideMatOwner ? &overrideMatOwner : (dc.material ? &dc.material : &defaultMaterial);
        InxMaterial *material =
            overrideMatRaw ? overrideMatRaw : (metadata ? metadata->material : materialOwner->get());
        if (!material)
            continue;

        // Filtering describes which source renderers participate in the pass.
        // An override material only changes how an accepted renderer is drawn;
        // it must not replace the source queue or pass tag used for selection.
        const InxMaterial *sourceMaterial = metadata ? metadata->material : expectedMaterial;
        const int queue = metadata ? metadata->renderQueue
                                   : (sourceMaterial ? sourceMaterial->GetRenderQueue() : material->GetRenderQueue());
        if (queue < queueMin || queue > queueMax)
            continue;

        if (materialFilter != GraphMaterialFilter::All) {
            ShaderStagePair stages{material->GetVertShaderName(), material->GetFragShaderName()};
            if (const MaterialRenderData *committed =
                    m_materialPipelineManager.GetRenderData(material->GetMaterialKey()))
                stages = committed->programKey.stages;
            const ShaderProgramArtifact *artifact = m_shaderCache.FindProgramArtifact(stages);
            if (!artifact && m_shaderProgramArtifactResolver) {
                m_shaderProgramArtifactResolver(*materialOwner);
                artifact = m_shaderCache.FindProgramArtifact(stages);
            }
            const bool deferredCompatible = artifact && artifact->FindVariant(ShaderCompileTarget::GBuffer);
            if ((materialFilter == GraphMaterialFilter::DeferredCompatible && !deferredCompatible) ||
                (materialFilter == GraphMaterialFilter::DeferredUnsupported && deferredCompatible))
                continue;
        }

        // Pass tag filter. Override passes are selectors, so an untagged source
        // must not leak into a tagged outline/depth/etc. pass. Legacy regular
        // passes retain the empty-source wildcard behavior.
        if (!passTag.empty() && !skyboxPass) {
            const std::string &matTag = sourceMaterial ? sourceMaterial->GetPassTag() : material->GetPassTag();
            const bool strictSourceTag = overrideMatRaw != nullptr;
            if ((strictSourceTag && matTag != passTag) || (!strictSourceTag && !matTag.empty() && matTag != passTag))
                continue;
        }

        // Compute view-space depth for transparent sort only.
        // Opaque front_to_back groups by material hash + vertex buffer
        // (stable order), so depth sort is unnecessary and its O(N log N)
        // cost every frame is avoided via the is_sorted() early-out.
        float sortKey = 0.0f;
        if (sortMode == "back_to_front") {
            glm::vec4 viewPos = viewMatrix * glm::vec4(glm::vec3(dc.worldMatrix[3]), 1.0f);
            sortKey = viewPos.z;
        }

        // Material + mesh hash for grouping optimization
        size_t matHash = std::hash<void *>{}(static_cast<void *>(material));
        const DrawListMetadata *bufferLease =
            metadata && metadata->vertexBuffer && metadata->indexBuffer ? metadata : nullptr;
        if (!bufferLease) {
            const auto bufferIt = m_perObjectBuffers.find(dc.objectId);
            if (bufferIt != m_perObjectBuffers.end() && bufferIt->second.vertexBuffer && bufferIt->second.indexBuffer) {
                if (requiredIndexEnd <= bufferIt->second.indexCount) {
                    fallbackBufferLeases.push_back({dc.objectId, material, queue, bufferIt->second.vertexBuffer,
                                                    bufferIt->second.indexBuffer, bufferIt->second.indexCount});
                    bufferLease = &fallbackBufferLeases.back();
                } else if (dc.indexStart == 0 && dc.vertexStart == 0 && bufferIt->second.indexCount > 0) {
                    // Same stale-lease fallback as the metadata path above.
                    fallbackBufferLeases.push_back({dc.objectId, material, queue, bufferIt->second.vertexBuffer,
                                                    bufferIt->second.indexBuffer, bufferIt->second.indexCount});
                    bufferLease = &fallbackBufferLeases.back();
                    indexCountClamp = static_cast<uint32_t>(bufferIt->second.indexCount);
                }
            }
        }
        const VkBuffer vb = bufferLease ? bufferLease->vertexBuffer->GetBuffer() : VK_NULL_HANDLE;
        const VkBuffer ib = bufferLease ? bufferLease->indexBuffer->GetBuffer() : VK_NULL_HANDLE;

        m_eligibleScratch.push_back({&dc, sortKey, matHash, vb, ib, materialOwner, material, indexCountClamp});
    }

    // A stable static publication already owns a sorted, fully resolved list.
    // Read it in place instead of copying tens of thousands of entries back
    // into the frame scratch on every pass. The scratch remains the mutable
    // source while a cache is built for the first time.
    const std::vector<SortableDrawCall> &eligibleDraws =
        reusedStaticFilter ? staticFilterCache->draws : m_eligibleScratch;

    // Diagnostic: log per-call eligible count with queue range
    if (s_filterDiagFrames < 3) {
        INXLOG_DEBUG("[DrawSceneFiltered] queue=[", queueMin, ",", queueMax, "] totalDC=", drawCalls().size(),
                     " eligible=", eligibleDraws.size());
        if (!eligibleDraws.empty()) {
            for (const auto &entry : eligibleDraws) {
                INXLOG_DEBUG("  -> objId=", entry.dc->objectId, " mat='", entry.material->GetName(),
                             "' queue=", entry.material->GetRenderQueue());
            }
        }
        ++s_filterDiagFrames;
    }

#if INFERNUX_FRAME_PROFILE
    auto stageNow = Clock::now();
    m_drawSubMs[9] += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
    stageStart = stageNow;
    m_drawSceneFilteredEligible += static_cast<uint64_t>(eligibleDraws.size());
#endif

    if (eligibleDraws.empty()) {
        if (skyboxPass) {
            static int s_emptySkyboxDiagCount = 0;
            if (s_emptySkyboxDiagCount < 8) {
                INXLOG_DEBUG("[DrawSceneFiltered] SkyboxPass candidates=", filterCandidateCount,
                             " eligible=0 materialResolve=0 materialCacheHits=0 materialUpdates=0 issued=0");
                ++s_emptySkyboxDiagCount;
            }
        }
#if INFERNUX_FRAME_PROFILE
        m_drawSubMs[8] += std::chrono::duration<double, std::milli>(Clock::now() - totalStart).count();
#endif
        return;
    }

    // ---- Sort if requested (skip for 0-1 elements) ----
    // Uniform-batch fast path: when every eligible entry shares the same
    // material hash and vertex/index buffer pair, all entries will be emitted as a
    // single instanced draw regardless of ordering.  Sorting would only
    // permute elements within that single batch, so we skip it entirely.
    bool uniformBatch = reusedStaticFilter && staticFilterCache->uniformBatch;
    if (!reusedStaticFilter && m_eligibleScratch.size() > 1) {
        const size_t firstMatHash = m_eligibleScratch[0].materialHash;
        const InxMaterial *firstMaterial = m_eligibleScratch[0].material;
        const VkBuffer firstVB = m_eligibleScratch[0].vertexBuf;
        const VkBuffer firstIB = m_eligibleScratch[0].indexBuf;
        const DrawCall &firstDraw = *m_eligibleScratch[0].dc;
        uniformBatch = true;
        const uint32_t firstIndexCount =
            m_eligibleScratch[0].indexCountClamp ? m_eligibleScratch[0].indexCountClamp : firstDraw.indexCount;
        for (size_t i = 1; i < m_eligibleScratch.size(); ++i) {
            const DrawCall &draw = *m_eligibleScratch[i].dc;
            const uint32_t drawIndexCount =
                m_eligibleScratch[i].indexCountClamp ? m_eligibleScratch[i].indexCountClamp : draw.indexCount;
            if (m_eligibleScratch[i].materialHash != firstMatHash || m_eligibleScratch[i].material != firstMaterial ||
                m_eligibleScratch[i].vertexBuf != firstVB || m_eligibleScratch[i].indexBuf != firstIB ||
                draw.indexStart != firstDraw.indexStart || drawIndexCount != firstIndexCount ||
                draw.vertexStart != firstDraw.vertexStart) {
                uniformBatch = false;
                break;
            }
        }
    }

    // is_sorted() early-out: O(N) comparison-only scan avoids the O(N log N)
    // std::sort when the eligible scratch is already in correct order from
    // a previous frame (common in stable scenes with static camera).
    const bool preserveOrder = sortMode.empty() || sortMode == "none" || sortMode == "preserve";
    // Transparent entries still need depth sorting even when they form one
    // instance batch. The sorted matrix stream determines blend order.
    const bool skipUniformBatchSort = uniformBatch && sortMode != "back_to_front";
    if (m_eligibleScratch.size() > 1 && !skipUniformBatchSort && !preserveOrder) {
        // In left-handed view space: near objects have small positive Z, far
        // objects have larger positive Z.
        if (sortMode == "front_to_back") {
            // Group by material + vertex/index buffer pair only (no depth).
            // This order is stable across frames for static material assignments,
            // so is_sorted() returns true and std::sort is skipped entirely.
            auto cmp = [](const SortableDrawCall &a, const SortableDrawCall &b) {
                if (a.materialHash != b.materialHash)
                    return a.materialHash < b.materialHash;
                if (a.vertexBuf != b.vertexBuf)
                    return a.vertexBuf < b.vertexBuf;
                return a.indexBuf < b.indexBuf;
            };
            if (!std::is_sorted(m_eligibleScratch.begin(), m_eligibleScratch.end(), cmp)) {
                std::sort(m_eligibleScratch.begin(), m_eligibleScratch.end(), cmp);
            }
        } else if (sortMode == "back_to_front") {
            auto cmp = [](const SortableDrawCall &a, const SortableDrawCall &b) { return a.sortKey > b.sortKey; };
            if (!std::is_sorted(m_eligibleScratch.begin(), m_eligibleScratch.end(), cmp)) {
                std::sort(m_eligibleScratch.begin(), m_eligibleScratch.end(), cmp);
            }
        } else {
            auto cmp = [](const SortableDrawCall &a, const SortableDrawCall &b) {
                if (a.materialHash != b.materialHash)
                    return a.materialHash < b.materialHash;
                if (a.vertexBuf != b.vertexBuf)
                    return a.vertexBuf < b.vertexBuf;
                return a.indexBuf < b.indexBuf;
            };
            if (!std::is_sorted(m_eligibleScratch.begin(), m_eligibleScratch.end(), cmp)) {
                std::sort(m_eligibleScratch.begin(), m_eligibleScratch.end(), cmp);
            }
        }
    } // size() > 1

    if (!reusedStaticFilter && overrideMaterial.empty() && sortMode != "back_to_front" &&
        m_eligibleScratch.size() >= 256 &&
        std::all_of(m_eligibleScratch.begin(), m_eligibleScratch.end(), [](const SortableDrawCall &entry) {
            return entry.dc && entry.dc->isStatic && entry.dc->skinBoneMatrices == nullptr &&
                   entry.dc->previousSkinBoneMatrices == nullptr;
        })) {
        StaticFilteredListCache cache;
        cache.drawListActivation = m_drawListActivation;
        cache.materialPublicationGeneration = materialPublicationGeneration;
        cache.queueMin = queueMin;
        cache.queueMax = queueMax;
        cache.target = activePass.target;
        cache.materialFilter = materialFilter;
        cache.sortMode = sortMode;
        cache.passTag = passTag;
        cache.draws = m_eligibleScratch;
        cache.sequenceKey = m_nextStaticFilteredSequenceKey++;
        if (m_nextStaticFilteredSequenceKey == 0)
            m_nextStaticFilteredSequenceKey = 1;
        staticFilteredSequenceKey = cache.sequenceKey;
        cache.uniformBatch = uniformBatch;
        constexpr size_t kMaximumStaticFilterCaches = 8;
        if (m_staticFilteredListCaches.size() == kMaximumStaticFilterCaches)
            m_staticFilteredListCaches.erase(m_staticFilteredListCaches.begin());
        m_staticFilteredListCaches.push_back(std::move(cache));
    }

#if INFERNUX_FRAME_PROFILE
    stageNow = Clock::now();
    m_drawSubMs[10] += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
    stageStart = stageNow;
#endif

    // ---- Upload instance model matrices to SSBO (set 2, binding 1) ----
    ResetPerFrameGpuStreamOffsets();

    const uint32_t frameIndex = m_currentFrame % m_maxFramesInFlight;
    const size_t totalEligible = eligibleDraws.size();
    uint32_t writeBase = m_instanceWriteOffset;
    const bool needsInstanceAuxiliary = ShaderCompileTargetUsesInstanceAuxiliary(activePass.target);
    // InstanceAuxBuffer contents are just as immutable as model matrices for a
    // static, unskinned publication. Keep one uploaded copy in every
    // frame-in-flight buffer instead of excluding Forward/Forward+/GBuffer
    // from the static range cache merely because they consume object identity,
    // layer mask, or transform history.
    const bool staticUnskinnedSequence = staticFilteredSequenceKey != 0;
    bool reusedStaticRange = false;
    if (staticUnskinnedSequence) {
        for (const StaticInstanceRange &range : m_staticInstanceRanges) {
            if (range.drawListActivation != m_drawListActivation || range.sequenceKey != staticFilteredSequenceKey ||
                range.frameIndex != frameIndex || range.instanceCount != totalEligible)
                continue;
            const VkBuffer activeInstanceBuffer =
                frameIndex < m_instanceBuffers.size() && m_instanceBuffers[frameIndex].buffer
                    ? m_instanceBuffers[frameIndex].buffer->GetBuffer()
                    : VK_NULL_HANDLE;
            const VkBuffer activeSkinInstanceBuffer =
                frameIndex < m_skinInstanceBuffers.size() && m_skinInstanceBuffers[frameIndex].buffer
                    ? m_skinInstanceBuffers[frameIndex].buffer->GetBuffer()
                    : VK_NULL_HANDLE;
            const VkBuffer activeInstanceAuxBuffer =
                frameIndex < m_instanceAuxBuffers.size() && m_instanceAuxBuffers[frameIndex].buffer
                    ? m_instanceAuxBuffers[frameIndex].buffer->GetBuffer()
                    : VK_NULL_HANDLE;
            if (range.instanceBuffer != activeInstanceBuffer || range.skinInstanceBuffer != activeSkinInstanceBuffer)
                continue;
            if (needsInstanceAuxiliary && range.instanceAuxBuffer != activeInstanceAuxBuffer)
                continue;
            writeBase = range.firstInstance;
            reusedStaticRange = true;
            break;
        }
    }

    if (needsInstanceAuxiliary)
        PrepareInstanceAuxiliary(m_ensureFrameCounter, writeBase + totalEligible);

    if (!reusedStaticRange && totalEligible > 0 && frameIndex < m_instanceBuffers.size()) {
        const bool needsPreviousSkinPalette = activePass.target == ShaderCompileTarget::Motion;
        size_t requiredBoneMatrices = m_skinPaletteWriteOffset;
        for (const auto &entry : eligibleDraws) {
            if (entry.dc->skinBoneMatrices)
                requiredBoneMatrices += entry.dc->skinBoneMatrices->size();
            if (needsPreviousSkinPalette && entry.dc->previousSkinBoneMatrices)
                requiredBoneMatrices += entry.dc->previousSkinBoneMatrices->size();
        }
        const VkBuffer previousInstanceBuffer =
            m_instanceBuffers[frameIndex].buffer ? m_instanceBuffers[frameIndex].buffer->GetBuffer() : VK_NULL_HANDLE;
        const VkBuffer previousSkinInstanceBuffer =
            frameIndex < m_skinInstanceBuffers.size() && m_skinInstanceBuffers[frameIndex].buffer
                ? m_skinInstanceBuffers[frameIndex].buffer->GetBuffer()
                : VK_NULL_HANDLE;
        const VkBuffer previousSkinPaletteBuffer =
            frameIndex < m_skinPaletteBuffers.size() && m_skinPaletteBuffers[frameIndex].buffer
                ? m_skinPaletteBuffers[frameIndex].buffer->GetBuffer()
                : VK_NULL_HANDLE;

        EnsureInstanceBufferCapacity(frameIndex, writeBase + totalEligible);
        EnsureSkinBuffersCapacity(frameIndex, writeBase + totalEligible, requiredBoneMatrices);
        const bool instanceBufferChanged = m_instanceBuffers[frameIndex].buffer &&
                                           previousInstanceBuffer != m_instanceBuffers[frameIndex].buffer->GetBuffer();
        const bool skinBufferChanged =
            frameIndex < m_skinInstanceBuffers.size() && frameIndex < m_skinPaletteBuffers.size() &&
            ((m_skinInstanceBuffers[frameIndex].buffer &&
              previousSkinInstanceBuffer != m_skinInstanceBuffers[frameIndex].buffer->GetBuffer()) ||
             (m_skinPaletteBuffers[frameIndex].buffer &&
              previousSkinPaletteBuffer != m_skinPaletteBuffers[frameIndex].buffer->GetBuffer()));
        if (instanceBufferChanged || skinBufferChanged)
            (void)PublishGlobalsDescriptorRevision(frameIndex);

        auto &instFrame = m_instanceBuffers[frameIndex];
        auto &skinInstFrame = m_skinInstanceBuffers[frameIndex];
        auto &skinPaletteFrame = m_skinPaletteBuffers[frameIndex];
        bool instanceMatricesWritten = false;
        if (instFrame.buffer) {
            void *mapped = instFrame.mapped;
            if (!mapped) {
                mapped = instFrame.buffer->Map();
                instFrame.mapped = mapped;
            }
            if (mapped) {
                glm::mat4 *matrices = static_cast<glm::mat4 *>(mapped);
                for (size_t i = 0; i < totalEligible; ++i) {
                    matrices[writeBase + i] = eligibleDraws[i].dc->worldMatrix;
                }
                instanceMatricesWritten = true;
            }
        }

        bool instanceAuxiliaryWritten = !needsInstanceAuxiliary;
        if (needsInstanceAuxiliary) {
            instanceAuxiliaryWritten = true;
            for (size_t i = 0; i < totalEligible; ++i) {
                const DrawCall &draw = *eligibleDraws[i].dc;
                const uint64_t pickingId = draw.pickingObjectId != 0 ? draw.pickingObjectId : draw.objectId;
                instanceAuxiliaryWritten &=
                    WriteInstanceAuxiliary(frameIndex, writeBase + static_cast<uint32_t>(i), draw.identity,
                                           draw.worldMatrix, pickingId, draw.layerMask);
            }
        }

        bool skinInstancesWritten = false;
        if (skinInstFrame.buffer && skinPaletteFrame.buffer) {
            auto *skinInstances = static_cast<GPUSkinInstanceData *>(skinInstFrame.mapped);
            if (!skinInstances) {
                skinInstances = static_cast<GPUSkinInstanceData *>(skinInstFrame.buffer->Map());
                skinInstFrame.mapped = skinInstances;
            }
            auto *skinBones = static_cast<glm::mat4 *>(skinPaletteFrame.mapped);
            if (!skinBones) {
                skinBones = static_cast<glm::mat4 *>(skinPaletteFrame.buffer->Map());
                skinPaletteFrame.mapped = skinBones;
            }
            if (skinInstances && skinBones) {
                auto appendPalette = [&](const std::vector<glm::mat4> *palette) {
                    const uint32_t offset = m_skinPaletteWriteOffset;
                    if (!palette || palette->empty())
                        return offset;
                    std::memcpy(&skinBones[offset], palette->data(), palette->size() * sizeof(glm::mat4));
                    m_skinPaletteWriteOffset += static_cast<uint32_t>(palette->size());
                    return offset;
                };
                auto resolveSkinData = [&](const DrawCall &draw) {
                    GPUSkinInstanceData skinData{};
                    const std::vector<glm::mat4> *palette = draw.skinBoneMatrices;
                    if (!palette || palette->empty())
                        return skinData;

                    skinData.boneCount = static_cast<uint32_t>(palette->size());
                    skinData.flags = kGPUSkinFlagEnabled;
                    if (!needsPreviousSkinPalette) {
                        const void *key = static_cast<const void *>(palette);
                        auto cached = m_skinPaletteFrameCache.find(key);
                        if (cached != m_skinPaletteFrameCache.end())
                            return cached->second;
                        skinData.boneOffset = appendPalette(palette);
                        skinData.previousBoneOffset = skinData.boneOffset;
                        m_skinPaletteFrameCache[key] = skinData;
                        return skinData;
                    }

                    skinData.boneOffset = appendPalette(palette);
                    const auto *previous = draw.previousSkinBoneMatrices;
                    if (!previous || previous->size() != palette->size())
                        previous = palette;
                    skinData.previousBoneOffset = previous == palette ? skinData.boneOffset : appendPalette(previous);
                    return skinData;
                };

                for (size_t i = 0; i < totalEligible; ++i) {
                    skinInstances[writeBase + i] = resolveSkinData(*eligibleDraws[i].dc);
                }
                skinInstancesWritten = true;
            }
        }
        m_instanceWriteOffset += static_cast<uint32_t>(totalEligible);
        if (staticUnskinnedSequence && instanceMatricesWritten && skinInstancesWritten && instanceAuxiliaryWritten) {
            const auto staleRange = std::remove_if(
                m_staticInstanceRanges.begin(), m_staticInstanceRanges.end(), [&](const StaticInstanceRange &range) {
                    return range.drawListActivation == m_drawListActivation &&
                           range.sequenceKey == staticFilteredSequenceKey && range.frameIndex == frameIndex;
                });
            m_staticInstanceRanges.erase(staleRange, m_staticInstanceRanges.end());
            StaticInstanceRange range;
            range.drawListActivation = m_drawListActivation;
            range.sequenceKey = staticFilteredSequenceKey;
            range.frameIndex = frameIndex;
            range.firstInstance = writeBase;
            range.instanceCount = static_cast<uint32_t>(totalEligible);
            range.instanceBuffer = m_instanceBuffers[frameIndex].buffer
                                       ? m_instanceBuffers[frameIndex].buffer->GetBuffer()
                                       : VK_NULL_HANDLE;
            range.skinInstanceBuffer = m_skinInstanceBuffers[frameIndex].buffer
                                           ? m_skinInstanceBuffers[frameIndex].buffer->GetBuffer()
                                           : VK_NULL_HANDLE;
            range.instanceAuxBuffer = m_instanceAuxBuffers[frameIndex].buffer
                                          ? m_instanceAuxBuffers[frameIndex].buffer->GetBuffer()
                                          : VK_NULL_HANDLE;
            range.lastUsedFrame = m_ensureFrameCounter;
            m_staticInstanceRanges.push_back(std::move(range));
        }
    }
    if (reusedStaticRange)
        m_instanceWriteOffset =
            std::max<uint32_t>(m_instanceWriteOffset, writeBase + static_cast<uint32_t>(totalEligible));

    // ---- Draw loop with instanced batching ----
    VkPipeline currentPipeline = VK_NULL_HANDLE;
    VkPipelineLayout currentLayout = VK_NULL_HANDLE;
    VkDescriptorSet currentDescriptorSet = VK_NULL_HANDLE;
    InxMaterial *currentMaterialRaw = nullptr;
    VkBuffer currentVertexBuffer = VK_NULL_HANDLE;
    VkBuffer currentIndexBuffer = VK_NULL_HANDLE;
    uint64_t issuedDraws = 0;

    struct ResolvedMaterialPass
    {
        VkPipeline pipeline = VK_NULL_HANDLE;
        VkPipelineLayout layout = VK_NULL_HANDLE;
        VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
        ShaderProgramPublication program;

        [[nodiscard]] bool IsValid() const noexcept
        {
            return pipeline != VK_NULL_HANDLE && layout != VK_NULL_HANDLE && descriptorSet != VK_NULL_HANDLE &&
                   program != nullptr;
        }
    };

    std::unordered_map<const InxMaterial *, ResolvedMaterialPass> resolvedMaterialCache;
    std::unordered_set<const InxMaterial *> updatedMaterials;
    // Draw count is not a useful estimate of unique materials. Reserving one
    // hash bucket per instance made each 65k-object pass allocate two large
    // tables even when every renderer shared DefaultLit.
    const size_t expectedUniqueMaterials = std::min<size_t>(totalEligible, 256);
    resolvedMaterialCache.reserve(expectedUniqueMaterials);
    updatedMaterials.reserve(expectedUniqueMaterials);
    size_t materialResolveCalls = 0;
    size_t materialCacheHits = 0;
    size_t materialUpdateCalls = 0;

    const auto syncPersistentMaterialPassCache = [&]() {
        const uint64_t publicationGeneration = m_materialPipelineManager.GetPublicationGeneration();
        if (m_materialPassResolutionCacheGeneration == publicationGeneration)
            return;
        ReleaseMaterialPassResolutionCache();
        m_materialPassResolutionCacheGeneration = publicationGeneration;
    };

    auto resolveMaterialPass = [&](const std::shared_ptr<InxMaterial> &owner) -> ResolvedMaterialPass {
        if (!owner)
            return {};

        const std::string materialKey = owner->GetMaterialKey();
        MaterialRenderData *forward = m_materialPipelineManager.GetRenderData(materialKey);
        if (forward && forward->descriptorSet != VK_NULL_HANDLE &&
            !m_materialPipelineManager.IsDescriptorSetLive(forward->descriptorSet)) {
            m_materialPipelineManager.RemoveRenderData(materialKey);
            forward = nullptr;
        }

        const ShaderStagePair requestedStages{owner->GetVertShaderName(), owner->GetFragShaderName()};
        const ShaderProgramArtifact *requestedArtifact = m_shaderCache.FindProgramArtifact(requestedStages);
        if (!requestedArtifact && m_shaderProgramArtifactResolver) {
            m_shaderProgramArtifactResolver(owner);
            requestedArtifact = m_shaderCache.FindProgramArtifact(requestedStages);
        }
        if (requestedArtifact && requestedArtifact->domain != ShaderProgramDomain::Mesh) {
            const std::string rejectionKey = materialKey + "|" + requestedStages.ToString() + "|Mesh";
            if (m_rejectedGeometryMaterialPrograms.insert(rejectionKey).second) {
                // The refresh performs the single user-facing domain report.
                // It deliberately leaves a complete previous generation live.
                RefreshMaterialPipeline(owner, requestedStages.vertexShaderId, requestedStages.fragmentShaderId);
            }
            if (!forward || !forward->isValid || forward->descriptorSet == VK_NULL_HANDLE ||
                !m_materialPipelineManager.IsDescriptorSetLive(forward->descriptorSet))
                return {};
            owner->ClearPipelineDirty();
        }

        if (!forward || owner->IsPipelineDirty()) {
            const std::string &vertName = owner->GetVertShaderName();
            const std::string &fragName = owner->GetFragShaderName();
            if (fragName.empty() || !RefreshMaterialPipeline(owner, vertName, fragName))
                return {};
            forward = m_materialPipelineManager.GetRenderData(materialKey);
        }
        if (!forward || !forward->isValid || forward->descriptorSet == VK_NULL_HANDLE ||
            !m_materialPipelineManager.IsDescriptorSetLive(forward->descriptorSet))
            return {};

        const MaterialPassPipelineDescriptor defaultForward =
            m_materialPipelineManager.GetDefaultPassPipelineDescriptor(ShaderCompileTarget::Forward);
        if (activePass == defaultForward) {
            return {forward->pipeline, forward->pipelineLayout, forward->descriptorSet, forward->shaderProgram};
        }

        ShaderProgramPublication program = forward->shaderProgram;
        if (activePass.target != ShaderCompileTarget::Forward) {
            // Every semantic pass belongs to the same committed generation as
            // Forward. The material may currently hold a rejected shader pair,
            // so resolving from the mutable asset fields would mix two ABIs.
            const ShaderStagePair &stages = forward->programKey.stages;
            const ShaderProgramArtifact *artifact = m_shaderCache.FindProgramArtifact(stages);
            if (!artifact && m_shaderProgramArtifactResolver) {
                m_shaderProgramArtifactResolver(owner);
                artifact = m_shaderCache.FindProgramArtifact(stages);
            }
            if (!artifact || !artifact->FindVariant(activePass.target)) {
                if (activePass.target == ShaderCompileTarget::Normal ||
                    activePass.target == ShaderCompileTarget::BaseColor) {
                    static std::unordered_set<std::string> reportedMissingGeometryVariants;
                    const std::string diagnosticKey =
                        stages.ToString() + "|" + ShaderCompileTargetName(activePass.target);
                    if (reportedMissingGeometryVariants.insert(diagnosticKey).second) {
                        INXLOG_ERROR(ShaderCompileTargetName(activePass.target),
                                     " buffer draw skipped: material shader program '", stages.ToString(),
                                     "' has no compiled ", ShaderCompileTargetName(activePass.target), " variant");
                    }
                }
                return {};
            }
            program = m_shaderCache.MaterializeProgramVariant(stages, activePass.target);
            if (!program)
                return {};
        }
        MaterialPassRenderData *pass = m_materialPipelineManager.GetOrCreatePassRenderData(owner, program, activePass);
        if (!pass || !pass->isValid) {
            if (activePass.target == ShaderCompileTarget::Normal ||
                activePass.target == ShaderCompileTarget::BaseColor) {
                static std::unordered_set<std::string> reportedInvalidGeometryPipelines;
                const std::string diagnosticKey =
                    forward->programKey.stages.ToString() + "|" + ShaderCompileTargetName(activePass.target);
                if (reportedInvalidGeometryPipelines.insert(diagnosticKey).second) {
                    INXLOG_ERROR(ShaderCompileTargetName(activePass.target),
                                 " buffer draw skipped: failed to create the ",
                                 ShaderCompileTargetName(activePass.target), " material pipeline for '",
                                 forward->programKey.stages.ToString(), "'");
                }
            }
            return {};
        }
        return {pass->pipeline, pass->pipelineLayout, pass->descriptorSet, pass->shaderProgram};
    };

    const auto resolveCachedMaterialPass = [&](const std::shared_ptr<InxMaterial> &owner) {
        if (!owner)
            return ResolvedMaterialPass{};

        syncPersistentMaterialPassCache();
        const auto cacheIt = resolvedMaterialCache.find(owner.get());
        if (cacheIt != resolvedMaterialCache.end()) {
            ++materialCacheHits;
            return cacheIt->second;
        }

        const MaterialPassResolutionCacheKey persistentKey{owner.get(),
                                                           MaterialPassPipelineDescriptorHash{}(activePass)};
        const auto persistentIt = m_materialPassResolutionCache.find(persistentKey);
        if (persistentIt != m_materialPassResolutionCache.end()) {
            auto &cached = persistentIt->second;
            const auto cachedOwner = cached.owner.lock();
            const bool cacheUsable = cachedOwner && cachedOwner.get() == owner.get() && cached.pipeline == activePass &&
                                     !owner->IsPipelineDirty() && cached.IsValid() &&
                                     m_materialPipelineManager.IsDescriptorSetLive(cached.descriptorSet);
            if (cacheUsable) {
                ++materialCacheHits;
                const ResolvedMaterialPass resolved{cached.pipelineHandle, cached.pipelineLayout, cached.descriptorSet,
                                                    cached.shaderProgram};
                resolvedMaterialCache.emplace(owner.get(), resolved);
                return resolved;
            }
            m_materialPassResolutionCache.erase(persistentIt);
        }

        ++materialResolveCalls;
        ResolvedMaterialPass resolved = resolveMaterialPass(owner);
        if (resolved.IsValid()) {
            resolvedMaterialCache.emplace(owner.get(), resolved);
            MaterialPassResolutionCacheEntry cached;
            cached.owner = owner;
            cached.pipeline = activePass;
            cached.pipelineHandle = resolved.pipeline;
            cached.pipelineLayout = resolved.layout;
            cached.descriptorSet = resolved.descriptorSet;
            cached.shaderProgram = resolved.program;
            m_materialPassResolutionCache[persistentKey] = std::move(cached);
        }
        return resolved;
    };

    // Batch accumulation: consecutive entries sharing (pipeline, descriptorSet, VB, IB, submesh) are
    // emitted as a single vkCmdDrawIndexed with instanceCount > 1.
    const bool allowBatching = (sortMode != "back_to_front" && sortMode != "preserve");

    size_t batchFirstInstance = 0;
    uint32_t batchInstanceCount = 0;
    uint32_t batchIndexStart = 0;
    uint32_t batchIndexCount = 0;
    int32_t batchVertexStart = 0;
    VkPipelineLayout batchPipelineLayout = VK_NULL_HANDLE;

    auto emitBatch = [&]() {
        if (batchInstanceCount == 0)
            return;
        // Push constants: model matrix for vertex shader (normalMat computed in shader from SSBO model)
        struct PushConstants
        {
            glm::mat4 model;
            glm::mat4 normalMat;
        };
        PushConstants pushData;
        pushData.model = eligibleDraws[batchFirstInstance].dc->worldMatrix;
        pushData.normalMat = glm::mat4(1.0f); // normalMat computed in shader from SSBO model
        vkCmdPushConstants(cmdBuf, batchPipelineLayout, VK_SHADER_STAGE_VERTEX_BIT, 0, sizeof(PushConstants),
                           &pushData);
        vkCmdDrawIndexed(cmdBuf, batchIndexCount, batchInstanceCount, batchIndexStart, batchVertexStart,
                         writeBase + static_cast<uint32_t>(batchFirstInstance));
        issuedDraws += batchInstanceCount;
#if INFERNUX_FRAME_PROFILE
        ++m_drawFilteredActualDraws;
#endif
        batchInstanceCount = 0;
    };

    for (size_t idx = 0; idx < totalEligible; ++idx) {
        const auto &entry = eligibleDraws[idx];
        const DrawCall &dc = *entry.dc;
        // A stale-lease clamp draws the leased buffer's complete previous
        // geometry instead of this frame's not-yet-published range.
        const uint32_t effectiveIndexCount = entry.indexCountClamp ? entry.indexCountClamp : dc.indexCount;

        // Once a batch has established valid Vulkan state, subsequent
        // consecutive instances with the same material/mesh can extend it
        // without repeating material-pipeline and descriptor validation.
        if (batchInstanceCount > 0) {
            const DrawCall &batchFirst = *eligibleDraws[batchFirstInstance].dc;
            const bool batchingAllowed =
                allowBatching || (batchFirst.allowTransparentInstancing && dc.allowTransparentInstancing);
            if (batchingAllowed && entry.material == currentMaterialRaw && entry.vertexBuf == currentVertexBuffer &&
                entry.indexBuf == currentIndexBuffer && dc.indexStart == batchIndexStart &&
                effectiveIndexCount == batchIndexCount && dc.vertexStart == batchVertexStart) {
                ++batchInstanceCount;
                continue;
            }
        }

        // Material already resolved in filter loop — use directly without
        // incrementing a shared_ptr reference count for every instance.
        const std::shared_ptr<InxMaterial> *matOwner = entry.materialOwner;
        InxMaterial *matRaw = matOwner->get();
        ResolvedMaterialPass resolved = resolveCachedMaterialPass(*matOwner);
        // SkyboxPass owns synthetic cube geometry whose vertices are meaningful
        // only to a skybox shader (translation-free view and far-plane depth).
        // Drawing that cube with Error/DefaultLit exposes its interior as a
        // giant box around the camera. Semantic sky draws therefore fail
        // closed until their own pipeline is ready; ordinary scene geometry
        // keeps the visible error/default fallback behavior.
        if (!resolved.IsValid() && !skyboxPass && errorMaterial) {
            resolved = resolveCachedMaterialPass(errorMaterial);
            if (resolved.IsValid()) {
                matOwner = &errorMaterial;
                matRaw = errorMaterial.get();
            }
        }
        if (!resolved.IsValid() && !skyboxPass && defaultMaterial) {
            resolved = resolveCachedMaterialPass(defaultMaterial);
            if (resolved.IsValid()) {
                matOwner = &defaultMaterial;
                matRaw = defaultMaterial.get();
            }
        }
        if (!resolved.IsValid()) {
            emitBatch();
            continue;
        }

        // Commit CPU-side material changes before selecting the immutable GPU
        // generation used by this draw. Texture resolution may publish a new
        // descriptor set, so refresh only when the update could have changed
        // the committed render data. The old code unconditionally resolved the
        // same material twice, which was especially visible in SkyboxPass.
        if (matRaw != currentMaterialRaw) {
            const std::string materialKey = matRaw->GetMaterialKey();
            const bool hasPendingTextures = m_materialPipelineManager.HasPendingTextureProperties(materialKey);
            const bool needsMaterialUpdate = matRaw->IsPropertiesDirty() || hasPendingTextures;
            const bool mayPublishDescriptor = needsMaterialUpdate;
            // Pending texture resolution may become ready between draw-list
            // passes, so preserve the old retry behavior for that state.
            if (needsMaterialUpdate && (updatedMaterials.insert(matRaw).second || hasPendingTextures)) {
                ++materialUpdateCalls;
                UpdateMaterialUBO(*matRaw);
            }
            if (mayPublishDescriptor) {
                MaterialRenderData *updated = m_materialPipelineManager.GetRenderData(materialKey);
                if (!updated || !updated->isValid || updated->pipeline != resolved.pipeline ||
                    updated->pipelineLayout != resolved.layout || updated->descriptorSet != resolved.descriptorSet ||
                    (activePass.target == ShaderCompileTarget::Forward &&
                     updated->shaderProgram.get() != resolved.program.get())) {
                    ++materialResolveCalls;
                    resolved = resolveMaterialPass(*matOwner);
                    if (!resolved.IsValid()) {
                        emitBatch();
                        continue;
                    }
                    resolvedMaterialCache[matRaw] = resolved;
                } else {
                    resolved.descriptorSet = updated->descriptorSet;
                    resolvedMaterialCache[matRaw] = resolved;
                }
            }
        }

        VkPipeline pipeline = resolved.pipeline;
        VkPipelineLayout pipelineLayout = resolved.layout;
        VkDescriptorSet descriptorSet = resolved.descriptorSet;

        if (descriptorSet == VK_NULL_HANDLE) {
            static int warnCount = 0;
            if (warnCount++ < 10) {
                INXLOG_WARN("[DrawSceneFiltered] descriptorSet=NULL for material '", matRaw->GetName(),
                            "' queue=", matRaw->GetRenderQueue(),
                            " pipeline=", (pipeline != VK_NULL_HANDLE ? "OK" : "NULL"), " vert='",
                            matRaw->GetVertShaderName(), "' frag='", matRaw->GetFragShaderName(), "'");
            }
            emitBatch();
            continue;
        }

        // The raw handles are backed by the active draw-list lease (or the
        // function-local fallback lease) for this entire command recording.
        if (entry.vertexBuf == VK_NULL_HANDLE || entry.indexBuf == VK_NULL_HANDLE) {
            static int bufWarnCount = 0;
            if (bufWarnCount++ < 10) {
                // INXLOG_WARN("[DrawSceneFiltered] no GPU buffers for objectId=", dc.objectId, " material='",
                //             matRaw->GetName(), "' queue=", matRaw->GetRenderQueue());
            }
            emitBatch();
            continue;
        }

        const VkBuffer vb = entry.vertexBuf;

        // Check if this entry can extend the current batch. Transparent
        // instancing is opt-in so normal alpha surfaces retain per-object draws.
        bool canExtendBatch = false;
        if (batchInstanceCount > 0) {
            const DrawCall &batchFirst = *eligibleDraws[batchFirstInstance].dc;
            const bool batchingAllowed =
                allowBatching || (batchFirst.allowTransparentInstancing && dc.allowTransparentInstancing);
            canExtendBatch = batchingAllowed && pipeline == currentPipeline && descriptorSet == currentDescriptorSet &&
                             matRaw == currentMaterialRaw && vb == currentVertexBuffer &&
                             entry.indexBuf == currentIndexBuffer && dc.indexStart == batchIndexStart &&
                             effectiveIndexCount == batchIndexCount && dc.vertexStart == batchVertexStart;
        }

        if (canExtendBatch) {
            ++batchInstanceCount;
            continue;
        }

        // Emit previous batch before changing state
        emitBatch();

        // ---- Bind new state ----
        if (pipeline != currentPipeline) {
            vkCmdBindPipeline(cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline);
            currentPipeline = pipeline;
        }

        if (matRaw != currentMaterialRaw) {
            currentMaterialRaw = matRaw;
            currentLayout = VK_NULL_HANDLE;
            currentDescriptorSet = VK_NULL_HANDLE;
        }

        if (descriptorSet != currentDescriptorSet || pipelineLayout != currentLayout) {
            if (descriptorSet != VK_NULL_HANDLE && !m_materialPipelineManager.IsDescriptorSetLive(descriptorSet)) {
                static int staleSet0WarnCount = 0;
                if (staleSet0WarnCount++ < 32) {
                    const uint64_t rawHandle = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(descriptorSet));
                    INXLOG_WARN("[DrawSceneFiltered] stale set0 descriptor before bind: 0x", rawHandle, " mat='",
                                matRaw->GetMaterialKey(), "' name='", matRaw->GetName(),
                                "' -- forcing pipeline refresh");
                }

                resolved = resolveMaterialPass(*matOwner);
                if (resolved.IsValid()) {
                    pipeline = resolved.pipeline;
                    pipelineLayout = resolved.layout;
                    descriptorSet = resolved.descriptorSet;
                    if (pipeline != currentPipeline) {
                        vkCmdBindPipeline(cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline);
                        currentPipeline = pipeline;
                    }
                }

                if (descriptorSet == VK_NULL_HANDLE || !m_materialPipelineManager.IsDescriptorSetLive(descriptorSet)) {
                    emitBatch();
                    continue;
                }
            }

            vkdebug::CmdBindDescriptorSetsTracked("VkCoreDraw.DrawSceneFiltered.Set0", cmdBuf,
                                                  VK_PIPELINE_BIND_POINT_GRAPHICS, pipelineLayout, 0, 1, &descriptorSet,
                                                  0, nullptr);
            currentDescriptorSet = descriptorSet;
            currentLayout = pipelineLayout;

            const ShaderProgram *program = resolved.program.get();

            if (program && program->HasDeclaredDescriptorSet(1)) {
                vkdebug::CmdBindDescriptorSetsTracked("VkCoreDraw.DrawSceneFiltered.Set1", cmdBuf,
                                                      VK_PIPELINE_BIND_POINT_GRAPHICS, pipelineLayout, 1, 1,
                                                      &perViewDescriptorSet, 0, nullptr);
            }

            if (program && program->HasDeclaredDescriptorSet(2)) {
                if (frameIndex < m_globalsDescSets.size()) {
                    VkDescriptorSet globalsDescSet = m_globalsDescSets[frameIndex];
                    if (globalsDescSet != VK_NULL_HANDLE) {
                        vkdebug::CmdBindDescriptorSetsTracked("VkCoreDraw.DrawSceneFiltered.Set2", cmdBuf,
                                                              VK_PIPELINE_BIND_POINT_GRAPHICS, pipelineLayout, 2, 1,
                                                              &globalsDescSet, 0, nullptr);
                    }
                }
            }

            // Bindless material shaders opt into the canonical device-global
            // texture table at set 3. Existing bounded and preview/special
            // shaders do not declare this set and therefore keep their old
            // descriptor path unchanged.
            if (program && program->UsesBindlessTextureABI()) {
                auto &rhiDevice = m_backend.Device().GetRhiDevice();
                const auto bindlessBinding = rhiDevice.GetBindlessTextureTableBinding();
                const VkDescriptorSet bindlessSet = rhiDevice.Resolve(bindlessBinding.group);
                if (bindlessSet == VK_NULL_HANDLE) {
                    static int missingBindlessSetErrorCount = 0;
                    if (missingBindlessSetErrorCount++ < 8) {
                        INXLOG_ERROR("Bindless material draw skipped because the device-global texture table is "
                                     "unavailable (material='",
                                     matRaw->GetMaterialKey(), "')");
                    }
                    currentDescriptorSet = VK_NULL_HANDLE;
                    currentLayout = VK_NULL_HANDLE;
                    continue;
                } else {
                    vkdebug::CmdBindDescriptorSetsTracked(
                        "VkCoreDraw.DrawSceneFiltered.BindlessTextures", cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS,
                        pipelineLayout, ShaderProgram::BindlessTextureSet, 1, &bindlessSet, 0, nullptr);

                    const auto *textureIndices =
                        m_materialPipelineManager.GetDescriptorManager().GetBindlessTextureIndices(
                            matRaw->GetMaterialKey());
                    rhiDevice.MarkBindlessTexturesUsed(
                        textureIndices && !textureIndices->empty() ? textureIndices->data() : nullptr,
                        textureIndices ? textureIndices->size() : 0);
                }
            }
        }

        if (vb != currentVertexBuffer) {
            VkBuffer vertBuffers[] = {vb};
            VkDeviceSize vbOffsets[] = {0};
            vkCmdBindVertexBuffers(cmdBuf, 0, 1, vertBuffers, vbOffsets);
            currentVertexBuffer = vb;
        }
        if (entry.indexBuf != currentIndexBuffer) {
            vkCmdBindIndexBuffer(cmdBuf, entry.indexBuf, 0, VK_INDEX_TYPE_UINT32);
            currentIndexBuffer = entry.indexBuf;
        }

        // Start new batch
        batchFirstInstance = idx;
        batchInstanceCount = 1;
        batchIndexStart = dc.indexStart;
        batchIndexCount = effectiveIndexCount;
        batchVertexStart = dc.vertexStart;
        batchPipelineLayout = pipelineLayout;
        if (uniformBatch && allowBatching) {
            batchInstanceCount = static_cast<uint32_t>(totalEligible);
            break;
        }
    }

    // Flush final batch
    emitBatch();

    if (skyboxPass) {
        static int s_skyboxDiagCount = 0;
        if (s_skyboxDiagCount < 8) {
            INXLOG_DEBUG("[DrawSceneFiltered] SkyboxPass candidates=", filterCandidateCount,
                         " eligible=", totalEligible, " materialResolve=", materialResolveCalls,
                         " materialCacheHits=", materialCacheHits, " materialUpdates=", materialUpdateCalls,
                         " issued=", issuedDraws);
            ++s_skyboxDiagCount;
        }
    }

#if INFERNUX_FRAME_PROFILE
    stageNow = Clock::now();
    m_drawSubMs[11] += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
    m_drawSubMs[8] += std::chrono::duration<double, std::milli>(stageNow - totalStart).count();
    m_drawSceneFilteredIssued += issuedDraws;
#endif
}

// ============================================================================
// Shadow Caster Draw — renders shadow-casting objects with shadow pipeline
// ============================================================================

void InxVkCoreModular::DrawShadowCasters(VkCommandBuffer cmdBuf, uint32_t width, uint32_t height, int queueMin,
                                         int queueMax, ShadowCameraResourceId resourceId,
                                         const lighting::ShadowFrame &shadowFrame, int lightIndex, VkFormat depthFormat,
                                         const ShadowViewDrawCallback &additionalDraws)
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    const auto totalStart = Clock::now();
    auto stageStart = totalStart;
#endif

    // NOTE: hard/soft shadow selection is NOT a property of this pass.
    // The shadow map only stores depth; stable PCF filtering happens in the
    // lit pass via shadowParams.w, driven
    // by the Light component. A former "shadowType" string parameter here was
    // a dead end and has been removed.

    (void)lightIndex;

    // Skip if shadow pipeline infrastructure not ready (lazy init)
    if (!EnsureShadowPipeline(depthFormat) || !EnsureShadowCameraResources(resourceId))
        return;
    auto resourcesIt = m_shadowCameraResources.find(resourceId);
    if (resourcesIt == m_shadowCameraResources.end())
        return;
    ShadowCameraResources &cameraResources = resourcesIt->second;
    const uint32_t viewCount =
        std::min<uint32_t>(static_cast<uint32_t>(shadowFrame.views.size()), lighting::MaxShadowViews);
    if (viewCount == 0)
        return;

#if INFERNUX_FRAME_PROFILE
    ++m_drawShadowCalls;
#endif

    const uint32_t frameIndex = m_currentFrame % m_maxFramesInFlight;

    for (uint32_t viewIndex = 0; viewIndex < viewCount; ++viewIndex) {
        const uint32_t bufferIndex = frameIndex * lighting::MaxShadowViews + viewIndex;
        if (bufferIndex >= cameraResources.mappedPointers.size() || !cameraResources.mappedPointers[bufferIndex])
            return;
        const lighting::ShadowView &shadowView = shadowFrame.views[viewIndex];
        ShadowPassUniformData shadowUbo{};
        shadowUbo.projection = shadowView.viewProjection;
        shadowUbo.lightVector = glm::vec4(shadowView.lightVector, shadowView.lightVectorIsPosition ? 1.0f : 0.0f);
        shadowUbo.bias = glm::vec4(shadowView.depthBiasTexels, shadowView.normalBiasTexels,
                                   shadowView.worldUnitsPerTexel, shadowView.farPlane);
        std::memcpy(cameraResources.mappedPointers[bufferIndex], &shadowUbo, sizeof(shadowUbo));
    }

    // Bind shadow pipeline once
    // NOTE: Per-material shadow pipelines override this in the inner loop
    VkPipeline lastBoundPipeline = VK_NULL_HANDLE;

    // Pre-build draw list (filter once, reuse for all cascades and, for an
    // immutable static publication, across frames).
    const uint64_t shadowMaterialPublicationGeneration = m_materialPipelineManager.GetPublicationGeneration();
    const bool reusedShadowScratch =
        m_shadowScratchValid && m_shadowScratchDrawListActivation == m_drawListActivation &&
        m_shadowScratchMaterialPublicationGeneration == shadowMaterialPublicationGeneration &&
        m_shadowScratchDepthFormat == depthFormat && m_shadowScratchQueueMin == queueMin &&
        m_shadowScratchQueueMax == queueMax;
#if INFERNUX_FRAME_PROFILE
    auto stageNow = Clock::now();
#endif
    const auto &activeShadowDrawCalls = shadowDrawCalls();
    const bool hasShadowListMetadata = m_shadowListMetadataSource == m_shadowDrawCallsPtr &&
                                       m_shadowListMetadata.size() == activeShadowDrawCalls.size() &&
                                       m_shadowListBufferRevision == m_objectBufferRevision;
    const bool usesMainDrawListMetadata = !m_shadowDrawCallsPtr && m_drawListMetadataSource == m_drawCallsPtr &&
                                          m_drawListMetadata.size() == activeShadowDrawCalls.size() &&
                                          m_drawListBufferRevision == m_objectBufferRevision;
    std::vector<DrawListMetadata> fallbackBufferLeases;
    if (!reusedShadowScratch) {
        m_shadowDrawScratch.clear();
        m_shadowDrawScratch.reserve(activeShadowDrawCalls.size());
        m_resolvedShadowMaterialsScratch.clear();
        m_resolvedShadowMaterialsScratch.reserve(std::min<size_t>(activeShadowDrawCalls.size(), 256));
        if (!hasShadowListMetadata && !usesMainDrawListMetadata)
            fallbackBufferLeases.reserve(activeShadowDrawCalls.size());
        const InxMaterial *lastResolvedShadowMaterial = nullptr;
        ResolvedShadowMaterial lastResolvedShadowResources{};
        for (size_t drawCallIndex = 0; drawCallIndex < activeShadowDrawCalls.size(); ++drawCallIndex) {
            const DrawCall &dc = activeShadowDrawCalls[drawCallIndex];
            const DrawListMetadata *metadata =
                hasShadowListMetadata ? &m_shadowListMetadata[drawCallIndex]
                                      : (usesMainDrawListMetadata ? &m_drawListMetadata[drawCallIndex] : nullptr);
            const uint64_t requiredIndexEnd = static_cast<uint64_t>(dc.indexStart) + dc.indexCount;
            if (metadata && (metadata->objectId != dc.objectId || metadata->material != dc.material.get() ||
                             requiredIndexEnd > metadata->indexCapacity))
                metadata = nullptr;
            if (!dc.castsShadows || !dc.material)
                continue;
            const int renderQueue = metadata ? metadata->renderQueue : dc.material->GetRenderQueue();
            if (renderQueue < queueMin || renderQueue > queueMax)
                continue;
            const DrawListMetadata *bufferLease =
                metadata && metadata->vertexBuffer && metadata->indexBuffer ? metadata : nullptr;
            if (!bufferLease) {
                const auto bufferIt = m_perObjectBuffers.find(dc.objectId);
                if (bufferIt != m_perObjectBuffers.end() && bufferIt->second.vertexBuffer &&
                    bufferIt->second.indexBuffer && requiredIndexEnd <= bufferIt->second.indexCount) {
                    fallbackBufferLeases.push_back({dc.objectId, dc.material.get(), renderQueue,
                                                    bufferIt->second.vertexBuffer, bufferIt->second.indexBuffer,
                                                    bufferIt->second.indexCount});
                    bufferLease = &fallbackBufferLeases.back();
                }
            }
            if (!bufferLease)
                continue;

            ResolvedShadowMaterial resources{};
            if (dc.material.get() == lastResolvedShadowMaterial) {
                resources = lastResolvedShadowResources;
            } else {
                auto resolved = m_resolvedShadowMaterialsScratch.find(dc.material.get());
                if (resolved == m_resolvedShadowMaterialsScratch.end()) {
                    const VkDescriptorSet descriptorSet = EnsureMaterialShadowPipeline(
                        dc.material, dc.material->GetVertShaderName(), dc.material->GetFragShaderName(), depthFormat);
                    resources.pipeline = dc.material->GetPassPipeline(ShaderCompileTarget::Shadow);
                    resources.descriptorSet = descriptorSet;
                    resolved = m_resolvedShadowMaterialsScratch.emplace(dc.material.get(), resources).first;
                } else {
                    resources = resolved->second;
                }
                lastResolvedShadowMaterial = dc.material.get();
                lastResolvedShadowResources = resources;
            }
            const VkPipeline pip = resources.pipeline;
            const VkDescriptorSet shadowMatDesc = resources.descriptorSet;
            if (pip == VK_NULL_HANDLE || shadowMatDesc == VK_NULL_HANDLE)
                continue;
            m_shadowDrawScratch.push_back({&dc, bufferLease->vertexBuffer->GetBuffer(),
                                           bufferLease->indexBuffer->GetBuffer(), pip, shadowMatDesc, dc.worldBounds});
        }

#if INFERNUX_FRAME_PROFILE
        stageNow = Clock::now();
        m_drawSubMs[13] += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
        stageStart = stageNow;
        m_drawShadowEligible += static_cast<uint64_t>(m_shadowDrawScratch.size());
#endif

        if (m_shadowDrawScratch.empty() && !additionalDraws) {
#if INFERNUX_FRAME_PROFILE
            m_drawSubMs[12] += std::chrono::duration<double, std::milli>(Clock::now() - totalStart).count();
#endif
            return;
        }

        // Sort shadow draw scratch by (pipeline, VB, submesh) for instanced batching.
        // Stable scene extraction commonly already produces this order (and one
        // uniform batch compares equivalent throughout), so avoid an unconditional
        // O(N log N) sort for large standard-GameObject scenes.
        const auto shadowDrawLess = [](const ShadowDraw &a, const ShadowDraw &b) {
            if (a.shadowPipeline != b.shadowPipeline)
                return a.shadowPipeline < b.shadowPipeline;
            if (a.shadowMaterialDescSet != b.shadowMaterialDescSet)
                return a.shadowMaterialDescSet < b.shadowMaterialDescSet;
            const VkBuffer va = a.vertexBuf;
            const VkBuffer vb_b = b.vertexBuf;
            if (va != vb_b)
                return va < vb_b;
            if (a.indexBuf != b.indexBuf)
                return a.indexBuf < b.indexBuf;
            if (a.dc->indexStart != b.dc->indexStart)
                return a.dc->indexStart < b.dc->indexStart;
            return a.dc->indexCount < b.dc->indexCount;
        };
        if (!std::is_sorted(m_shadowDrawScratch.begin(), m_shadowDrawScratch.end(), shadowDrawLess))
            std::sort(m_shadowDrawScratch.begin(), m_shadowDrawScratch.end(), shadowDrawLess);

        // Build a lightweight hierarchy over the stable, batch-sorted caster
        // list. It is deliberately independent of scene type: any large static
        // renderer publication benefits, while intersecting boundary groups
        // still fall back to exact per-object tests.
        constexpr uint32_t kShadowCullGroupSize = 64;
        m_shadowCullGroups.clear();
        m_shadowCullGroups.reserve((m_shadowDrawScratch.size() + kShadowCullGroupSize - 1) / kShadowCullGroupSize);
        for (uint32_t first = 0; first < m_shadowDrawScratch.size(); first += kShadowCullGroupSize) {
            ShadowCullGroup group;
            group.first = first;
            group.count =
                std::min<uint32_t>(kShadowCullGroupSize, static_cast<uint32_t>(m_shadowDrawScratch.size()) - first);
            group.layerMaskIntersection = UINT32_MAX;
            group.allBoundsValid = true;
            bool haveBounds = false;
            for (uint32_t offset = 0; offset < group.count; ++offset) {
                const ShadowDraw &draw = m_shadowDrawScratch[first + offset];
                group.layerMaskUnion |= draw.dc->layerMask;
                group.layerMaskIntersection &= draw.dc->layerMask;
                if (!draw.worldBounds.IsValid()) {
                    group.allBoundsValid = false;
                    continue;
                }
                if (!haveBounds) {
                    group.worldBounds = draw.worldBounds;
                    haveBounds = true;
                } else {
                    group.worldBounds.min = glm::min(group.worldBounds.min, draw.worldBounds.min);
                    group.worldBounds.max = glm::max(group.worldBounds.max, draw.worldBounds.max);
                }
            }
            group.allBoundsValid &= haveBounds;
            m_shadowCullGroups.push_back(group);
        }
        m_shadowAllVisible.resize(m_shadowDrawScratch.size());
        for (uint32_t index = 0; index < m_shadowAllVisible.size(); ++index)
            m_shadowAllVisible[index] = index;

        m_shadowScratchStatic =
            !m_shadowDrawScratch.empty() &&
            std::all_of(m_shadowDrawScratch.begin(), m_shadowDrawScratch.end(), [](const ShadowDraw &draw) {
                return draw.dc && draw.dc->isStatic && draw.dc->skinBoneMatrices == nullptr &&
                       draw.dc->previousSkinBoneMatrices == nullptr;
            });
        m_shadowScratchUniformBatch = m_shadowDrawScratch.size() > 1;
        if (m_shadowScratchUniformBatch) {
            const ShadowDraw &first = m_shadowDrawScratch.front();
            for (size_t index = 1; index < m_shadowDrawScratch.size(); ++index) {
                const ShadowDraw &draw = m_shadowDrawScratch[index];
                if (draw.shadowPipeline != first.shadowPipeline ||
                    draw.shadowMaterialDescSet != first.shadowMaterialDescSet || draw.vertexBuf != first.vertexBuf ||
                    draw.indexBuf != first.indexBuf || draw.dc->indexStart != first.dc->indexStart ||
                    draw.dc->indexCount != first.dc->indexCount || draw.dc->vertexStart != first.dc->vertexStart) {
                    m_shadowScratchUniformBatch = false;
                    break;
                }
            }
        }
        m_shadowScratchDrawListActivation = m_drawListActivation;
        m_shadowScratchMaterialPublicationGeneration = shadowMaterialPublicationGeneration;
        m_shadowScratchDepthFormat = depthFormat;
        m_shadowScratchQueueMin = queueMin;
        m_shadowScratchQueueMax = queueMax;
        m_shadowScratchValid = m_shadowScratchStatic && (hasShadowListMetadata || usesMainDrawListMetadata);
    }

#if INFERNUX_FRAME_PROFILE
    stageNow = Clock::now();
    m_drawSubMs[15] += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
    stageStart = stageNow;
#endif

    uint64_t issuedDraws = 0;

    // Static shadow cache eligibility is intentionally strict. A single
    // dynamic or skinned entry keeps the original per-view cull/upload path,
    // while a fully static submission is validated field-for-field before any
    // cached visibility is reused. This also catches edits made to objects
    // that are still marked static.
    const bool staticShadowSubmission = m_shadowScratchStatic;
    auto &staticShadowCache = cameraResources.staticSubmissionCache;
    auto stateMatches = [](const ShadowCameraResources::StaticShadowDrawState &cached, const ShadowDraw &current) {
        const DrawCall &draw = *current.dc;
        return cached.objectId == draw.objectId && cached.layerMask == draw.layerMask &&
               std::memcmp(&cached.worldMatrix, &draw.worldMatrix, sizeof(glm::mat4)) == 0 &&
               cached.worldBounds.min.x == draw.worldBounds.min.x &&
               cached.worldBounds.min.y == draw.worldBounds.min.y &&
               cached.worldBounds.min.z == draw.worldBounds.min.z &&
               cached.worldBounds.max.x == draw.worldBounds.max.x &&
               cached.worldBounds.max.y == draw.worldBounds.max.y &&
               cached.worldBounds.max.z == draw.worldBounds.max.z && cached.vertexBuffer == current.vertexBuf &&
               cached.indexBuffer == current.indexBuf && cached.pipeline == current.shadowPipeline &&
               cached.materialDescriptor == current.shadowMaterialDescSet && cached.indexStart == draw.indexStart &&
               cached.indexCount == draw.indexCount && cached.vertexStart == draw.vertexStart;
    };
    auto captureState = [](const ShadowDraw &current) {
        const DrawCall &draw = *current.dc;
        ShadowCameraResources::StaticShadowDrawState state;
        state.objectId = draw.objectId;
        state.layerMask = draw.layerMask;
        state.worldMatrix = draw.worldMatrix;
        state.worldBounds = draw.worldBounds;
        state.vertexBuffer = current.vertexBuf;
        state.indexBuffer = current.indexBuf;
        state.pipeline = current.shadowPipeline;
        state.materialDescriptor = current.shadowMaterialDescSet;
        state.indexStart = draw.indexStart;
        state.indexCount = draw.indexCount;
        state.vertexStart = draw.vertexStart;
        return state;
    };

    bool staticShadowStateUnchanged = reusedShadowScratch && staticShadowSubmission && staticShadowCache.valid;
    if (!reusedShadowScratch)
        staticShadowStateUnchanged = staticShadowSubmission && staticShadowCache.valid &&
                                     staticShadowCache.draws.size() == m_shadowDrawScratch.size();
    if (staticShadowStateUnchanged && !reusedShadowScratch) {
        for (size_t index = 0; index < m_shadowDrawScratch.size(); ++index) {
            if (!stateMatches(staticShadowCache.draws[index], m_shadowDrawScratch[index])) {
                staticShadowStateUnchanged = false;
                break;
            }
        }
    }
    if (staticShadowSubmission && !staticShadowStateUnchanged) {
        staticShadowCache.draws.clear();
        staticShadowCache.draws.reserve(m_shadowDrawScratch.size());
        for (const ShadowDraw &draw : m_shadowDrawScratch)
            staticShadowCache.draws.push_back(captureState(draw));
        for (auto &view : staticShadowCache.views) {
            view.valid = false;
            view.fullSequence = false;
            view.visibleIndices.clear();
        }
        staticShadowCache.valid = true;
    } else if (!staticShadowSubmission) {
        staticShadowCache.valid = false;
    }

    std::array<Frustum, lighting::MaxShadowViews> shadowFrustums{};
    for (uint32_t viewIndex = 0; viewIndex < viewCount; ++viewIndex)
        shadowFrustums[viewIndex].ExtractFromMatrix(shadowFrame.views[viewIndex].viewProjection);

    if (frameIndex >= cameraResources.streamFrames.size())
        return;
    auto &shadowStream = cameraResources.streamFrames[frameIndex];
    if (shadowStream.frameSerial != m_ensureFrameCounter) {
        shadowStream.frameSerial = m_ensureFrameCounter;
        shadowStream.instanceWriteOffset = 0;
        shadowStream.skinPaletteWriteOffset = 0;
        shadowStream.skinPaletteCache.clear();
    }

    size_t maxShadowBoneMatrices = shadowStream.skinPaletteWriteOffset;
    for (const auto &sd : m_shadowDrawScratch) {
        if (sd.dc->skinBoneMatrices)
            maxShadowBoneMatrices += sd.dc->skinBoneMatrices->size() * viewCount;
    }
    const size_t maxShadowInstances =
        static_cast<size_t>(shadowStream.instanceWriteOffset) + m_shadowDrawScratch.size() * viewCount;
    if (!EnsureShadowCameraStreamCapacity(cameraResources, frameIndex, maxShadowInstances, maxShadowBoneMatrices))
        return;

    const VkDescriptorSet shadowStreamDescSet = shadowStream.descriptorSet;
    if (shadowStreamDescSet == VK_NULL_HANDLE)
        return;

    for (uint32_t viewIndex = 0; viewIndex < viewCount; ++viewIndex) {
        const uint32_t descIdx = frameIndex * lighting::MaxShadowViews + viewIndex;
        if (descIdx >= cameraResources.descriptorSets.size())
            break;

        const lighting::ShadowView &shadowView = shadowFrame.views[viewIndex];
        if (!shadowView.atlas.IsValid())
            continue;
        const uint32_t tileX = shadowView.atlas.x + shadowView.atlas.guard;
        const uint32_t tileY = shadowView.atlas.y + shadowView.atlas.guard;
        const uint32_t tileW = std::min(shadowView.atlas.InnerSize(), width - std::min(tileX, width));
        const uint32_t tileH = std::min(shadowView.atlas.InnerSize(), height - std::min(tileY, height));
        if (tileW == 0 || tileH == 0)
            continue;

        VkViewport viewport{};
        viewport.x = static_cast<float>(tileX);
        viewport.y = static_cast<float>(tileY);
        viewport.width = static_cast<float>(tileW);
        viewport.height = static_cast<float>(tileH);
        viewport.minDepth = 0.0f;
        viewport.maxDepth = 1.0f;
        vkCmdSetViewport(cmdBuf, 0, 1, &viewport);

        VkRect2D scissor{};
        scissor.offset = {static_cast<int32_t>(tileX), static_cast<int32_t>(tileY)};
        scissor.extent = {tileW, tileH};
        vkCmdSetScissor(cmdBuf, 0, 1, &scissor);

        // Unified caster bias for every view type: the slope-scaled raster
        // bias tracks each polygon's own depth gradient (which no
        // receiver-side term can express), while the receiver-side
        // normal/light offsets in lighting.glsl absorb quantization. Casters
        // themselves are never moved in world space.
        vkCmdSetDepthBias(cmdBuf, 1.0f, 0.0f, 2.0f);

        // The complete shadow descriptor state is bound per batch below. Set 0
        // belongs to this camera and this shadow view.
        VkDescriptorSet cascadeDescSet = cameraResources.descriptorSets[descIdx];
        if (cascadeDescSet == VK_NULL_HANDLE)
            continue;

        const Frustum &shadowFrustum = shadowFrustums[viewIndex];
        VkBuffer currentVertexBuffer = VK_NULL_HANDLE;
        VkBuffer currentIndexBuffer = VK_NULL_HANDLE;
        // Per-cascade frustum cull into a compact index list, then upload
        // model matrices for visible objects and batch by (pipeline, VB, IB, submesh).
        // Directional cascades must not cull against the light-space near
        // plane: casters between the light and the cascade volume are pancaked
        // onto the near plane by the shadow vertex shader, so rejecting them
        // here would punch holes into shadows cast by tall or distant objects.
        const bool ignoreNearPlane = shadowView.type == lighting::ShadowViewType::DirectionalCascade;
        const std::vector<uint32_t> *visibleIndices = nullptr;
        auto &cachedView = staticShadowCache.views[viewIndex];
        const bool cachedVisibility =
            staticShadowSubmission && cachedView.valid && cachedView.cullingMask == shadowView.cullingMask &&
            cachedView.type == shadowView.type &&
            (cachedView.fullSequence ||
             std::memcmp(&cachedView.viewProjection, &shadowView.viewProjection, sizeof(glm::mat4)) == 0);
        if (cachedVisibility) {
            visibleIndices = cachedView.fullSequence ? &m_shadowAllVisible : &cachedView.visibleIndices;
        } else {
            m_shadowViewVisible.clear();
            m_shadowViewVisible.reserve(m_shadowDrawScratch.size());
            for (const ShadowCullGroup &group : m_shadowCullGroups) {
                if ((group.layerMaskUnion & shadowView.cullingMask) == 0u)
                    continue;

                const Frustum::AABBRelation relation =
                    group.allBoundsValid ? shadowFrustum.ClassifyAABB(group.worldBounds, ignoreNearPlane)
                                         : Frustum::AABBRelation::Intersecting;
                if (relation == Frustum::AABBRelation::Outside)
                    continue;

                const bool everyLayerAccepted = (group.layerMaskIntersection & shadowView.cullingMask) != 0u;
                if (relation == Frustum::AABBRelation::Inside && everyLayerAccepted) {
                    for (uint32_t offset = 0; offset < group.count; ++offset)
                        m_shadowViewVisible.push_back(group.first + offset);
                    continue;
                }

                for (uint32_t offset = 0; offset < group.count; ++offset) {
                    const uint32_t drawIndex = group.first + offset;
                    const ShadowDraw &draw = m_shadowDrawScratch[drawIndex];
                    if ((draw.dc->layerMask & shadowView.cullingMask) == 0u)
                        continue;
                    if (relation == Frustum::AABBRelation::Intersecting && draw.worldBounds.IsValid() &&
                        !shadowFrustum.IntersectsAABB(draw.worldBounds, ignoreNearPlane))
                        continue;
                    m_shadowViewVisible.push_back(drawIndex);
                }
            }
            const bool everyCasterLayerAccepted =
                std::all_of(m_shadowCullGroups.begin(), m_shadowCullGroups.end(), [&](const ShadowCullGroup &group) {
                    return (group.layerMaskIntersection & shadowView.cullingMask) != 0u;
                });
            // Compacting and uploading a list that already contains nearly all
            // static casters costs more CPU than letting fixed-function clipping
            // reject the small outside tail. Canonicalizing dense views to the
            // full sequence also makes their per-frame-in-flight uploads stable.
            // A large static submission that resolves to one cheap instanced
            // batch is usually faster when its immutable matrix stream is
            // reused verbatim. Re-compacting even one cascade copies several
            // megabytes every frame and prevents every frame-in-flight stream
            // from becoming resident. Keep the conservative dense-view rule
            // for arbitrary geometry, and extend it only to simple uniform
            // batches whose full index workload remains bounded.
            constexpr uint64_t kSimpleStaticShadowIndexBudget = 4ull * 1024ull * 1024ull;
            const uint64_t fullSequenceIndexWork =
                m_shadowScratchUniformBatch && !m_shadowDrawScratch.empty()
                    ? static_cast<uint64_t>(m_shadowAllVisible.size()) *
                          static_cast<uint64_t>(m_shadowDrawScratch.front().dc->indexCount)
                    : std::numeric_limits<uint64_t>::max();
            const bool cheapUniformFullSequence = m_shadowScratchUniformBatch && m_shadowAllVisible.size() >= 4096 &&
                                                  fullSequenceIndexWork <= kSimpleStaticShadowIndexBudget &&
                                                  m_shadowViewVisible.size() * 4 >= m_shadowAllVisible.size();
            const bool useFullStaticSequence =
                staticShadowSubmission && everyCasterLayerAccepted && !m_shadowAllVisible.empty() &&
                (m_shadowViewVisible.size() * 5 >= m_shadowAllVisible.size() * 4 || cheapUniformFullSequence);
            visibleIndices = useFullStaticSequence ? &m_shadowAllVisible : &m_shadowViewVisible;
            if (staticShadowSubmission) {
                // Adaptive CSM may move the light-space matrix by tiny amounts
                // while selecting the exact same static casters. Preserve the
                // visibility generation in that case so every frame-in-flight
                // stream can reuse its already uploaded matrix range.
                const bool sameVisibility = cachedView.valid && cachedView.cullingMask == shadowView.cullingMask &&
                                            cachedView.type == shadowView.type &&
                                            cachedView.fullSequence == useFullStaticSequence &&
                                            (useFullStaticSequence || cachedView.visibleIndices == m_shadowViewVisible);
                cachedView.viewProjection = shadowView.viewProjection;
                cachedView.cullingMask = shadowView.cullingMask;
                cachedView.type = shadowView.type;
                if (!sameVisibility) {
                    cachedView.fullSequence = useFullStaticSequence;
                    if (useFullStaticSequence)
                        cachedView.visibleIndices.clear();
                    else
                        cachedView.visibleIndices = m_shadowViewVisible;
                    cachedView.generation = staticShadowCache.nextGeneration++;
                    if (staticShadowCache.nextGeneration == 0)
                        staticShadowCache.nextGeneration = 1;
                }
                cachedView.valid = true;
                visibleIndices = cachedView.fullSequence ? &m_shadowAllVisible : &cachedView.visibleIndices;
            }
        }

#if INFERNUX_FRAME_PROFILE
        stageNow = Clock::now();
        m_drawSubMs[16] += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
        stageStart = stageNow;
#endif

        const uint32_t visibleCount = static_cast<uint32_t>(visibleIndices->size());
        if (visibleCount == 0) {
            if (additionalDraws) {
                additionalDraws(viewIndex, shadowView);
                lastBoundPipeline = VK_NULL_HANDLE;
            }
            continue;
        }

        // Each camera appends into its own frame-local shadow instance stream.
        const uint32_t writeBase = shadowStream.instanceWriteOffset;
        if (!shadowStream.instanceMapped || !shadowStream.skinInstanceMapped || !shadowStream.skinPaletteMapped)
            continue;
        const VkBuffer instanceBuffer =
            shadowStream.instanceBuffer ? shadowStream.instanceBuffer->GetBuffer() : VK_NULL_HANDLE;
        const VkBuffer skinInstanceBuffer =
            shadowStream.skinInstanceBuffer ? shadowStream.skinInstanceBuffer->GetBuffer() : VK_NULL_HANDLE;
        auto &staticUpload = shadowStream.staticViewUploads[viewIndex];
        const bool reuseStaticUpload =
            staticShadowSubmission && cachedView.valid && staticUpload.valid &&
            staticUpload.visibilityGeneration == cachedView.generation && staticUpload.firstInstance == writeBase &&
            staticUpload.instanceCount == visibleCount && staticUpload.instanceBuffer == instanceBuffer &&
            staticUpload.skinInstanceBuffer == skinInstanceBuffer;
        if (!reuseStaticUpload) {
            auto *matrices = static_cast<glm::mat4 *>(shadowStream.instanceMapped);
            for (uint32_t vi = 0; vi < visibleCount; ++vi) {
                matrices[writeBase + vi] = m_shadowDrawScratch[(*visibleIndices)[vi]].dc->worldMatrix;
            }

            auto *skinInstances = static_cast<GPUSkinInstanceData *>(shadowStream.skinInstanceMapped);
            auto *skinBones = static_cast<glm::mat4 *>(shadowStream.skinPaletteMapped);
            auto resolveSkinData = [&](const std::vector<glm::mat4> *palette) {
                GPUSkinInstanceData skinData{};
                if (!palette || palette->empty())
                    return skinData;
                const void *key = static_cast<const void *>(palette);
                auto cached = shadowStream.skinPaletteCache.find(key);
                if (cached != shadowStream.skinPaletteCache.end())
                    return cached->second;

                skinData.boneOffset = shadowStream.skinPaletteWriteOffset;
                skinData.boneCount = static_cast<uint32_t>(palette->size());
                skinData.flags = kGPUSkinFlagEnabled;
                std::memcpy(&skinBones[shadowStream.skinPaletteWriteOffset], palette->data(),
                            palette->size() * sizeof(glm::mat4));
                shadowStream.skinPaletteWriteOffset += static_cast<uint32_t>(palette->size());
                shadowStream.skinPaletteCache[key] = skinData;
                return skinData;
            };

            for (uint32_t vi = 0; vi < visibleCount; ++vi) {
                const DrawCall *dc = m_shadowDrawScratch[(*visibleIndices)[vi]].dc;
                skinInstances[writeBase + vi] = resolveSkinData(dc ? dc->skinBoneMatrices : nullptr);
            }
            if (staticShadowSubmission) {
                staticUpload.visibilityGeneration = cachedView.generation;
                staticUpload.firstInstance = writeBase;
                staticUpload.instanceCount = visibleCount;
                staticUpload.instanceBuffer = instanceBuffer;
                staticUpload.skinInstanceBuffer = skinInstanceBuffer;
                staticUpload.valid = true;
            } else {
                staticUpload.valid = false;
            }
        }
        shadowStream.instanceWriteOffset += visibleCount;

#if INFERNUX_FRAME_PROFILE
        stageNow = Clock::now();
        m_drawSubMs[17] += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
        stageStart = stageNow;
#endif

        // Batch accumulation: consecutive visible entries sharing (pipeline, VB, IB, submesh)
        // are emitted as a single instanced draw call.
        size_t batchStart = 0;
        uint32_t batchCount = 0;
        VkPipeline batchPipeline = VK_NULL_HANDLE;
        VkDescriptorSet batchShadowMaterialDescSet = VK_NULL_HANDLE;
        uint32_t batchIdxStart = 0;
        uint32_t batchIdxCount = 0;
        int32_t batchVtxStart = 0;

        auto emitShadowBatch = [&]() {
            if (batchCount == 0)
                return;
            const VkDescriptorSet materialDescSet = batchShadowMaterialDescSet != VK_NULL_HANDLE
                                                        ? batchShadowMaterialDescSet
                                                        : m_shadowMaterialDummyDescSet;
            if (materialDescSet == VK_NULL_HANDLE) {
                batchCount = 0;
                return;
            }
            auto &rhiDevice = m_backend.Device().GetRhiDevice();
            const VkDescriptorSet bindlessSet =
                m_shadowUsesBindlessTextureTable ? rhiDevice.Resolve(rhiDevice.GetBindlessTextureTableBinding().group)
                                                 : VK_NULL_HANDLE;
            if (m_shadowUsesBindlessTextureTable && bindlessSet == VK_NULL_HANDLE) {
                static int missingShadowBindlessSetErrorCount = 0;
                if (missingShadowBindlessSetErrorCount++ < 8) {
                    INXLOG_ERROR("Shadow draw batch skipped because the RHI bindless texture table is unavailable");
                }
                batchCount = 0;
                return;
            }
            const std::array<VkDescriptorSet, 4> descriptorSets = {cascadeDescSet, shadowStreamDescSet, materialDescSet,
                                                                   bindlessSet};
            vkdebug::CmdBindDescriptorSetsTracked(
                "VkCoreDraw.DrawShadowCasters.AllSets", cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS, m_shadowPipelineLayout,
                0, m_shadowUsesBindlessTextureTable ? 4u : 3u, descriptorSets.data(), 0, nullptr);
            struct PushData
            {
                glm::mat4 model;
                glm::mat4 normalMat;
            } pushData;
            pushData.model = m_shadowDrawScratch[(*visibleIndices)[batchStart]].dc->worldMatrix;
            pushData.normalMat = glm::mat4(1.0f);
            vkCmdPushConstants(cmdBuf, m_shadowPipelineLayout, VK_SHADER_STAGE_VERTEX_BIT, 0, sizeof(PushData),
                               &pushData);
            vkCmdDrawIndexed(cmdBuf, batchIdxCount, batchCount, batchIdxStart, batchVtxStart,
                             writeBase + static_cast<uint32_t>(batchStart));
            issuedDraws += batchCount;
#if INFERNUX_FRAME_PROFILE
            ++m_drawShadowActualDraws;
#endif
            batchCount = 0;
        };

        for (uint32_t vi = 0; vi < visibleCount; ++vi) {
            const auto &sd = m_shadowDrawScratch[(*visibleIndices)[vi]];

            const VkBuffer vb = sd.vertexBuf;

            bool canExtend = batchCount > 0 && sd.shadowPipeline == batchPipeline && vb == currentVertexBuffer &&
                             sd.indexBuf == currentIndexBuffer &&
                             sd.shadowMaterialDescSet == batchShadowMaterialDescSet &&
                             sd.dc->indexStart == batchIdxStart && sd.dc->indexCount == batchIdxCount &&
                             sd.dc->vertexStart == batchVtxStart;

            if (canExtend) {
                ++batchCount;
                continue;
            }

            emitShadowBatch();

            // Bind per-material shadow pipeline if changed
            if (sd.shadowPipeline != lastBoundPipeline) {
                vkCmdBindPipeline(cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS, sd.shadowPipeline);
                lastBoundPipeline = sd.shadowPipeline;
            }

            if (vb != currentVertexBuffer) {
                VkDeviceSize offsets[] = {0};
                vkCmdBindVertexBuffers(cmdBuf, 0, 1, &vb, offsets);
                currentVertexBuffer = vb;
            }
            if (sd.indexBuf != currentIndexBuffer) {
                vkCmdBindIndexBuffer(cmdBuf, sd.indexBuf, 0, VK_INDEX_TYPE_UINT32);
                currentIndexBuffer = sd.indexBuf;
            }

            batchStart = vi;
            batchCount = 1;
            batchPipeline = sd.shadowPipeline;
            batchShadowMaterialDescSet = sd.shadowMaterialDescSet;
            batchIdxStart = sd.dc->indexStart;
            batchIdxCount = sd.dc->indexCount;
            batchVtxStart = sd.dc->vertexStart;
            if (m_shadowScratchUniformBatch) {
                batchCount = visibleCount;
                break;
            }
        }

        emitShadowBatch();

        if (additionalDraws) {
            additionalDraws(viewIndex, shadowView);
            // The callback owns its RHI pipeline state. Geometry must bind its
            // pipeline again before recording the next cascade.
            lastBoundPipeline = VK_NULL_HANDLE;
        }

#if INFERNUX_FRAME_PROFILE
        stageNow = Clock::now();
        m_drawSubMs[18] += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
        stageStart = stageNow;
#endif
    }

#if INFERNUX_FRAME_PROFILE
    stageNow = Clock::now();
    m_drawSubMs[14] += std::chrono::duration<double, std::milli>(stageNow - totalStart).count();
    m_drawSubMs[12] += std::chrono::duration<double, std::milli>(stageNow - totalStart).count();
    m_drawShadowIssued += issuedDraws;
#endif
}

// ============================================================================
// Shadow Pipeline Management
// ============================================================================

InxVkCoreModular::ShadowCameraResourceId InxVkCoreModular::CreateShadowCameraResources()
{
    const ShadowCameraResourceId resourceId = m_nextShadowCameraResourceId++;
    m_shadowCameraResources.try_emplace(resourceId);
    return resourceId;
}

void InxVkCoreModular::DestroyShadowCameraResources(ShadowCameraResources &resources) noexcept
{
    const VkDevice device = GetDevice();
    const VmaAllocator allocator = m_backend.Device().GetVmaAllocator();
    auto uniformBuffers = std::make_shared<std::vector<VkBuffer>>(std::move(resources.uniformBuffers));
    auto allocations = std::make_shared<std::vector<VmaAllocation>>(std::move(resources.allocations));
    auto streamFrames =
        std::make_shared<std::vector<ShadowCameraResources::StreamFrame>>(std::move(resources.streamFrames));
    resources.mappedPointers.clear();
    resources.descriptorSets.clear();
    if (device != VK_NULL_HANDLE) {
        auto &descriptorManager = m_backend.Device().GetRhiDevice().GetDescriptorManager();
        for (const auto &lease : resources.descriptorLeases)
            descriptorManager.Retire(lease);
        for (const auto &lease : resources.streamDescriptorLeases)
            descriptorManager.Retire(lease);
    }
    resources.descriptorLeases.clear();
    resources.streamDescriptorLeases.clear();
    resources.staticSubmissionCache = {};
    if (allocator != VK_NULL_HANDLE) {
        m_deletionQueue.Retire([allocator, uniformBuffers, allocations, streamFrames]() mutable {
            for (size_t index = 0; index < uniformBuffers->size(); ++index) {
                if ((*uniformBuffers)[index] != VK_NULL_HANDLE)
                    vmaDestroyBuffer(allocator, (*uniformBuffers)[index], (*allocations)[index]);
            }
            streamFrames->clear();
        });
    }
}

void InxVkCoreModular::DestroyShadowCameraResources(ShadowCameraResourceId resourceId) noexcept
{
    auto found = m_shadowCameraResources.find(resourceId);
    if (found == m_shadowCameraResources.end())
        return;
    DestroyShadowCameraResources(found->second);
    m_shadowCameraResources.erase(found);
}

bool InxVkCoreModular::EnsureShadowCameraResources(ShadowCameraResourceId resourceId)
{
    if (resourceId == 0 || m_shadowDescSetLayout == VK_NULL_HANDLE || m_shadowGlobalsDescSetLayout == VK_NULL_HANDLE)
        return false;
    auto found = m_shadowCameraResources.find(resourceId);
    if (found == m_shadowCameraResources.end())
        return false;
    ShadowCameraResources &resources = found->second;
    if (!resources.descriptorSets.empty() && resources.streamFrames.size() == m_maxFramesInFlight)
        return true;

    const uint32_t totalSets = m_maxFramesInFlight * lighting::MaxShadowViews;
    auto &descriptorManager = m_backend.Device().GetRhiDevice().GetDescriptorManager();
    resources.descriptorSets.resize(totalSets, VK_NULL_HANDLE);
    resources.descriptorLeases.reserve(totalSets);
    for (uint32_t index = 0; index < totalSets; ++index) {
        auto lease = descriptorManager.Allocate(m_shadowDescSetLayout, vk::DescriptorArena::ViewPersistent);
        if (!lease.IsValid()) {
            INXLOG_ERROR("Failed to allocate camera-local shadow descriptor set ", index);
            DestroyShadowCameraResources(resources);
            return false;
        }
        resources.descriptorSets[index] = lease.set;
        resources.descriptorLeases.push_back(lease);
    }

    const VkDeviceSize uboSize = sizeof(ShadowPassUniformData);
    resources.uniformBuffers.resize(totalSets, VK_NULL_HANDLE);
    resources.allocations.resize(totalSets, VK_NULL_HANDLE);
    resources.mappedPointers.resize(totalSets, nullptr);
    const VmaAllocator allocator = m_backend.Device().GetVmaAllocator();
    for (uint32_t index = 0; index < totalSets; ++index) {
        VkBufferCreateInfo bufferInfo{};
        bufferInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
        bufferInfo.size = uboSize;
        bufferInfo.usage = VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT;
        bufferInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        VmaAllocationCreateInfo memoryInfo{};
        memoryInfo.usage = VMA_MEMORY_USAGE_AUTO;
        memoryInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT;
        memoryInfo.requiredFlags = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
        VmaAllocationInfo allocation{};
        if (vmaCreateBuffer(allocator, &bufferInfo, &memoryInfo, &resources.uniformBuffers[index],
                            &resources.allocations[index], &allocation) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create camera-local shadow UBO");
            DestroyShadowCameraResources(resources);
            return false;
        }
        resources.mappedPointers[index] = allocation.pMappedData;
        VkDescriptorBufferInfo descriptorInfo{resources.uniformBuffers[index], 0, uboSize};
        VkWriteDescriptorSet write{};
        write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        write.dstSet = resources.descriptorSets[index];
        write.dstBinding = 0;
        write.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
        write.descriptorCount = 1;
        write.pBufferInfo = &descriptorInfo;
        vkUpdateDescriptorSets(GetDevice(), 1, &write, 0, nullptr);
    }

    std::vector<VkDescriptorSet> streamDescriptorSets(m_maxFramesInFlight, VK_NULL_HANDLE);
    resources.streamDescriptorLeases.reserve(m_maxFramesInFlight);
    for (uint32_t index = 0; index < m_maxFramesInFlight; ++index) {
        auto lease = descriptorManager.Allocate(m_shadowGlobalsDescSetLayout, vk::DescriptorArena::ViewPersistent);
        if (!lease.IsValid()) {
            INXLOG_ERROR("Failed to allocate camera-local shadow stream descriptor set ", index);
            DestroyShadowCameraResources(resources);
            return false;
        }
        streamDescriptorSets[index] = lease.set;
        resources.streamDescriptorLeases.push_back(lease);
    }

    resources.streamFrames.resize(m_maxFramesInFlight);
    for (uint32_t frameIndex = 0; frameIndex < m_maxFramesInFlight; ++frameIndex) {
        resources.streamFrames[frameIndex].descriptorSet = streamDescriptorSets[frameIndex];
        if (!EnsureShadowCameraStreamCapacity(resources, frameIndex, INSTANCE_BUFFER_INITIAL_CAPACITY,
                                              SKIN_PALETTE_BUFFER_INITIAL_CAPACITY)) {
            INXLOG_ERROR("Failed to create camera-local shadow instance streams for frame ", frameIndex);
            DestroyShadowCameraResources(resources);
            return false;
        }
    }
    return true;
}

void InxVkCoreModular::UpdateShadowCameraStreamDescriptor(ShadowCameraResources::StreamFrame &frame,
                                                          uint32_t frameIndex)
{
    if (frame.descriptorSet == VK_NULL_HANDLE || frameIndex >= m_globalsBuffers.size() ||
        !m_globalsBuffers[frameIndex] || !frame.instanceBuffer || !frame.skinInstanceBuffer || !frame.skinPaletteBuffer)
        return;

    std::array<VkDescriptorBufferInfo, 4> infos{};
    infos[0] = {m_globalsBuffers[frameIndex]->GetBuffer(), 0, sizeof(EngineGlobalsUBO)};
    infos[1] = {frame.instanceBuffer->GetBuffer(), 0, VK_WHOLE_SIZE};
    infos[2] = {frame.skinInstanceBuffer->GetBuffer(), 0, VK_WHOLE_SIZE};
    infos[3] = {frame.skinPaletteBuffer->GetBuffer(), 0, VK_WHOLE_SIZE};

    std::array<VkWriteDescriptorSet, 4> writes{};
    for (uint32_t binding = 0; binding < writes.size(); ++binding) {
        writes[binding].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        writes[binding].dstSet = frame.descriptorSet;
        writes[binding].dstBinding = binding;
        writes[binding].descriptorType =
            binding == 0 ? VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER : VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        writes[binding].descriptorCount = 1;
        writes[binding].pBufferInfo = &infos[binding];
    }
    vkUpdateDescriptorSets(GetDevice(), static_cast<uint32_t>(writes.size()), writes.data(), 0, nullptr);
}

bool InxVkCoreModular::EnsureShadowCameraStreamCapacity(ShadowCameraResources &resources, uint32_t frameIndex,
                                                        size_t instanceCount, size_t skinPaletteCount)
{
    if (frameIndex >= resources.streamFrames.size())
        return false;
    auto &frame = resources.streamFrames[frameIndex];
    const bool growInstances = !frame.instanceBuffer || !frame.skinInstanceBuffer ||
                               frame.instanceCapacity < std::max<size_t>(instanceCount, 1);
    const bool growPalette =
        !frame.skinPaletteBuffer || frame.skinPaletteCapacity < std::max<size_t>(skinPaletteCount, 1);
    if (!growInstances && !growPalette)
        return true;

    size_t newInstanceCapacity = std::max<size_t>(frame.instanceCapacity, INSTANCE_BUFFER_INITIAL_CAPACITY);
    while (newInstanceCapacity < std::max<size_t>(instanceCount, 1))
        newInstanceCapacity *= 2;
    size_t newPaletteCapacity = std::max<size_t>(frame.skinPaletteCapacity, SKIN_PALETTE_BUFFER_INITIAL_CAPACITY);
    while (newPaletteCapacity < std::max<size_t>(skinPaletteCount, 1))
        newPaletteCapacity *= 2;

    auto newInstances = m_resourceManager.CreateStorageBuffer(newInstanceCapacity * sizeof(glm::mat4), false);
    auto newSkinInstances =
        m_resourceManager.CreateStorageBuffer(newInstanceCapacity * sizeof(GPUSkinInstanceData), false);
    auto newSkinPalette = m_resourceManager.CreateStorageBuffer(newPaletteCapacity * sizeof(glm::mat4), false);
    if (!newInstances || !newSkinInstances || !newSkinPalette)
        return false;

    void *newInstanceMapped = newInstances->Map();
    void *newSkinInstanceMapped = newSkinInstances->Map();
    void *newSkinPaletteMapped = newSkinPalette->Map();
    if (!newInstanceMapped || !newSkinInstanceMapped || !newSkinPaletteMapped)
        return false;

    if (frame.instanceBuffer && frame.instanceMapped && frame.instanceWriteOffset > 0) {
        std::memcpy(newInstanceMapped, frame.instanceMapped,
                    static_cast<size_t>(frame.instanceWriteOffset) * sizeof(glm::mat4));
    }
    if (frame.skinInstanceBuffer && frame.skinInstanceMapped && frame.instanceWriteOffset > 0) {
        std::memcpy(newSkinInstanceMapped, frame.skinInstanceMapped,
                    static_cast<size_t>(frame.instanceWriteOffset) * sizeof(GPUSkinInstanceData));
    }
    if (frame.skinPaletteBuffer && frame.skinPaletteMapped && frame.skinPaletteWriteOffset > 0) {
        std::memcpy(newSkinPaletteMapped, frame.skinPaletteMapped,
                    static_cast<size_t>(frame.skinPaletteWriteOffset) * sizeof(glm::mat4));
    }

    frame.instanceBuffer = std::move(newInstances);
    frame.skinInstanceBuffer = std::move(newSkinInstances);
    frame.skinPaletteBuffer = std::move(newSkinPalette);
    frame.instanceMapped = newInstanceMapped;
    frame.skinInstanceMapped = newSkinInstanceMapped;
    frame.skinPaletteMapped = newSkinPaletteMapped;
    frame.instanceCapacity = newInstanceCapacity;
    frame.skinPaletteCapacity = newPaletteCapacity;
    UpdateShadowCameraStreamDescriptor(frame, frameIndex);
    return true;
}

bool InxVkCoreModular::EnsureShadowPipeline(VkFormat depthFormat)
{
    if (m_shadowPipelineReady)
        return true;

    VkDevice device = GetDevice();
    if (device == VK_NULL_HANDLE || depthFormat == VK_FORMAT_UNDEFINED)
        return false;

    // --- Create descriptor set layout (binding 0 = UBO) ---
    if (m_shadowDescSetLayout == VK_NULL_HANDLE) {
        VkDescriptorSetLayoutBinding uboBinding{};
        uboBinding.binding = 0;
        uboBinding.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
        uboBinding.descriptorCount = 1;
        uboBinding.stageFlags = VK_SHADER_STAGE_VERTEX_BIT;

        VkDescriptorSetLayoutCreateInfo layoutInfo{};
        layoutInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
        layoutInfo.bindingCount = 1;
        layoutInfo.pBindings = &uboBinding;

        if (vkCreateDescriptorSetLayout(device, &layoutInfo, nullptr, &m_shadowDescSetLayout) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create shadow descriptor set layout");
            return false;
        }
    }

    // Camera-local shadow stream layout. Shadow draws deliberately do not use
    // the ordinary set-2 instance descriptor: each camera owns the matrices
    // and skinning data recorded for its shadow views.
    if (m_shadowGlobalsDescSetLayout == VK_NULL_HANDLE) {
        std::array<VkDescriptorSetLayoutBinding, 4> bindings{};
        bindings[0] = {0, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 1,
                       VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT, nullptr};
        bindings[1] = {1, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_VERTEX_BIT, nullptr};
        bindings[2] = {2, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_VERTEX_BIT, nullptr};
        bindings[3] = {3, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1, VK_SHADER_STAGE_VERTEX_BIT, nullptr};

        VkDescriptorSetLayoutCreateInfo layoutInfo{};
        layoutInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
        layoutInfo.bindingCount = static_cast<uint32_t>(bindings.size());
        layoutInfo.pBindings = bindings.data();
        if (vkCreateDescriptorSetLayout(device, &layoutInfo, nullptr, &m_shadowGlobalsDescSetLayout) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create camera-local shadow stream descriptor layout");
            return false;
        }
    }

    // --- Create shadow material descriptor set layout (set 2) ---
    //
    // This set serves two purposes:
    //   (a) Vertex-stage MaterialProperties UBO at binding 14 (all shadow materials)
    //   (b) Fragment-stage texture samplers (bindings 0..N-1) and fragment
    //       MaterialProperties UBO (binding N) for alpha-clip shadow materials.
    //
    // We declare a fixed set of sampler slots so the layout is
    // compatible with any alpha-clip shader.  Non-alpha-clip materials simply
    // leave those bindings unused.
    if (m_shadowMaterialDescSetLayout == VK_NULL_HANDLE) {
        std::vector<VkDescriptorSetLayoutBinding> bindings;

        // Texture samplers for alpha-clip and vertex deformation.
        for (uint32_t i = 0; i < kMaxShadowMaterialTextures; ++i) {
            VkDescriptorSetLayoutBinding texBinding{};
            texBinding.binding = i;
            texBinding.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
            texBinding.descriptorCount = 1;
            texBinding.stageFlags = VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT;
            bindings.push_back(texBinding);
        }

        // Fragment MaterialProperties UBO follows the sampler range.
        {
            VkDescriptorSetLayoutBinding fragMatBinding{};
            fragMatBinding.binding = kMaxShadowMaterialTextures;
            fragMatBinding.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
            fragMatBinding.descriptorCount = 1;
            fragMatBinding.stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
            bindings.push_back(fragMatBinding);
        }

        // Vertex MaterialProperties UBO at binding 14
        {
            VkDescriptorSetLayoutBinding vtxMatBinding{};
            vtxMatBinding.binding = 14;
            vtxMatBinding.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
            vtxMatBinding.descriptorCount = 1;
            vtxMatBinding.stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
            bindings.push_back(vtxMatBinding);
        }

        // Bindless texture indices used by alpha-clipped shadow variants.
        if (m_backend.Device().GetRhiDevice().GetBindlessTextureTableBinding().IsValid()) {
            VkDescriptorSetLayoutBinding textureIndexBinding{};
            textureIndexBinding.binding = ShaderProgram::MaterialTextureIndexBinding;
            textureIndexBinding.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
            textureIndexBinding.descriptorCount = 1;
            textureIndexBinding.stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
            bindings.push_back(textureIndexBinding);
        }

        VkDescriptorSetLayoutCreateInfo layoutInfo{};
        layoutInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
        layoutInfo.bindingCount = static_cast<uint32_t>(bindings.size());
        layoutInfo.pBindings = bindings.data();

        if (vkCreateDescriptorSetLayout(device, &layoutInfo, nullptr, &m_shadowMaterialDescSetLayout) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create shadow material descriptor set layout");
            return false;
        }
    }

    // --- Create shadow depth sampler ---
    if (m_shadowDepthSampler == VK_NULL_HANDLE) {
        if (!CreateShadowDepthSampler()) {
            return false;
        }
    }

    // --- Create shadow pipeline layout (shared by all per-material shadow pipelines) ---
    // Set 0 = shadow UBO (per-cascade), set 1 = camera-local globals and shadow streams,
    // set 2 = material resources, set 3 = global bindless textures when available.
    if (m_shadowGlobalsDescSetLayout == VK_NULL_HANDLE) {
        INXLOG_ERROR("EnsureShadowPipeline: camera-local shadow stream layout is null");
        return false;
    }
    if (m_shadowPipelineLayout == VK_NULL_HANDLE) {
        VkPushConstantRange pushRange{};
        pushRange.stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
        pushRange.offset = 0;
        pushRange.size = sizeof(glm::mat4) * 2; // model + normalMat

        auto &rhiDevice = m_backend.Device().GetRhiDevice();
        const auto bindlessBinding = rhiDevice.GetBindlessTextureTableBinding();
        const VkDescriptorSetLayout bindlessLayout = rhiDevice.Resolve(bindlessBinding.layout);
        const bool useBindlessTextureTable = bindlessBinding.IsValid() && bindlessLayout != VK_NULL_HANDLE;
        const std::array<VkDescriptorSetLayout, 4> setLayouts = {
            m_shadowDescSetLayout, m_shadowGlobalsDescSetLayout, m_shadowMaterialDescSetLayout,
            useBindlessTextureTable ? bindlessLayout : VK_NULL_HANDLE};

        VkPipelineLayoutCreateInfo pipelineLayoutInfo{};
        pipelineLayoutInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
        pipelineLayoutInfo.setLayoutCount = useBindlessTextureTable ? 4u : 3u;
        pipelineLayoutInfo.pSetLayouts = setLayouts.data();
        pipelineLayoutInfo.pushConstantRangeCount = 1;
        pipelineLayoutInfo.pPushConstantRanges = &pushRange;

        if (vkCreatePipelineLayout(device, &pipelineLayoutInfo, nullptr, &m_shadowPipelineLayout) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create shadow pipeline layout");
            return false;
        }
        m_shadowUsesBindlessTextureTable = useBindlessTextureTable;
    }
    (void)EnsureShadowMaterialDummyDescriptorSet();
    m_shadowPipelineReady = true;
    // INXLOG_INFO("Shadow pipeline infrastructure created successfully");
    return true;
}

bool InxVkCoreModular::CreateShadowDepthSampler()
{
    VkSamplerCreateInfo samplerInfo{};
    samplerInfo.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
    // Shaders gather four raw depth texels and compare before interpolation.
    // Keeping this a regular depth sampler also permits the engine's fully-lit
    // fallback descriptor when a graph has no shadow pass.
    samplerInfo.magFilter = VK_FILTER_NEAREST;
    samplerInfo.minFilter = VK_FILTER_NEAREST;
    samplerInfo.mipmapMode = VK_SAMPLER_MIPMAP_MODE_NEAREST;
    samplerInfo.addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER;
    samplerInfo.addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER;
    samplerInfo.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER;
    samplerInfo.borderColor = VK_BORDER_COLOR_FLOAT_OPAQUE_WHITE;
    samplerInfo.compareEnable = VK_FALSE;
    samplerInfo.compareOp = VK_COMPARE_OP_NEVER;
    samplerInfo.maxLod = 1.0f;

    if (vkCreateSampler(GetDevice(), &samplerInfo, nullptr, &m_shadowDepthSampler) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create shadow depth sampler");
        return false;
    }
    return true;
}

void InxVkCoreModular::CleanupShadowPipeline()
{
    VkDevice device = GetDevice();
    if (device == VK_NULL_HANDLE)
        return;

    const VkPipelineLayout retiredPipelineLayout = std::exchange(m_shadowPipelineLayout, VK_NULL_HANDLE);
    m_shadowUsesBindlessTextureTable = false;
    auto &descriptorManager = m_backend.Device().GetRhiDevice().GetDescriptorManager();
    for (auto &[owner, entry] : m_shadowMaterialBindingCache) {
        (void)owner;
        descriptorManager.Retire(entry.descriptorLease);
    }
    m_shadowMaterialBindingCache.clear();
    descriptorManager.Retire(m_shadowMaterialDummyLease);
    m_shadowMaterialDummyLease = {};
    m_shadowMaterialDummyDescSet = VK_NULL_HANDLE;
    for (auto &[resourceId, resources] : m_shadowCameraResources) {
        (void)resourceId;
        DestroyShadowCameraResources(resources);
    }
    m_shadowCameraResources.clear();
    const VkDescriptorSetLayout retiredViewLayout = std::exchange(m_shadowDescSetLayout, VK_NULL_HANDLE);
    const VkDescriptorSetLayout retiredGlobalsLayout = std::exchange(m_shadowGlobalsDescSetLayout, VK_NULL_HANDLE);
    const VkDescriptorSetLayout retiredMaterialLayout = std::exchange(m_shadowMaterialDescSetLayout, VK_NULL_HANDLE);
    const VkSampler retiredDepthSampler = std::exchange(m_shadowDepthSampler, VK_NULL_HANDLE);

    std::vector<VkPipeline> retiredPipelines;
    retiredPipelines.reserve(m_shadowPipelineCache.size());
    for (auto &[key, pipeline] : m_shadowPipelineCache) {
        (void)key;
        if (pipeline != VK_NULL_HANDLE)
            retiredPipelines.push_back(pipeline);
    }
    m_shadowPipelineCache.clear();

    // Shadow infrastructure can be rebuilt while older frames are still in
    // flight. Descriptor leases are retired above; keep every object they and
    // their command buffers reference alive through the same GPU epoch.
    m_deletionQueue.Retire([device, retiredPipelineLayout, retiredViewLayout, retiredGlobalsLayout,
                            retiredMaterialLayout, retiredDepthSampler,
                            retiredPipelines = std::move(retiredPipelines)]() mutable {
        for (const VkPipeline pipeline : retiredPipelines) {
            if (pipeline != VK_NULL_HANDLE)
                vkDestroyPipeline(device, pipeline, nullptr);
        }
        if (retiredPipelineLayout != VK_NULL_HANDLE)
            vkDestroyPipelineLayout(device, retiredPipelineLayout, nullptr);
        if (retiredViewLayout != VK_NULL_HANDLE)
            vkDestroyDescriptorSetLayout(device, retiredViewLayout, nullptr);
        if (retiredGlobalsLayout != VK_NULL_HANDLE)
            vkDestroyDescriptorSetLayout(device, retiredGlobalsLayout, nullptr);
        if (retiredMaterialLayout != VK_NULL_HANDLE)
            vkDestroyDescriptorSetLayout(device, retiredMaterialLayout, nullptr);
        if (retiredDepthSampler != VK_NULL_HANDLE)
            vkDestroySampler(device, retiredDepthSampler, nullptr);
    });
    m_shadowPipelineReady = false;
}

// ============================================================================
// Per-object buffer management
// ============================================================================

void InxVkCoreModular::PumpPendingMeshUploads()
{
    for (auto pending = m_pendingSharedMeshBuffers.begin(); pending != m_pendingSharedMeshBuffers.end();) {
        const bool vertexReady = m_resourceManager.TryPublishBufferUpload(pending->second.vertexUpload);
        const bool indexReady = m_resourceManager.TryPublishBufferUpload(pending->second.indexUpload);
        if (!vertexReady || !indexReady) {
            ++pending;
            continue;
        }

        if (!pending->first.assetGuid.empty()) {
            auto &registry = AssetRegistry::Instance();
            if (!registry.IsLoaded(pending->first.assetGuid) ||
                registry.GetAssetVersion(pending->first.assetGuid) != pending->first.runtimeVersion) {
                pending = m_pendingSharedMeshBuffers.erase(pending);
                continue;
            }
        }

        SharedMeshBuffers buffers;
        buffers.vertexBuffer = pending->second.vertexUpload->GetBuffer();
        buffers.indexBuffer = pending->second.indexUpload->GetBuffer();
        buffers.vertexCount = pending->second.vertexCount;
        buffers.indexCount = pending->second.indexCount;
        PublishSharedMeshBuffers(pending->first, std::move(buffers));
        pending = m_pendingSharedMeshBuffers.erase(pending);
        ++m_completedMeshUploadCount;
    }
}

void InxVkCoreModular::EnsureObjectBuffers(uint64_t objectId, const std::vector<Vertex> &vertices,
                                           const std::vector<uint32_t> &indices, bool forceUpdate,
                                           const std::string &assetGuid, uint64_t runtimeVersion)
{
    if (vertices.empty() || indices.empty())
        return;
    if (assetGuid.empty() != (runtimeVersion == 0))
        throw std::invalid_argument("Mesh GPU identity requires GUID and runtime version together");

    auto objectIt = m_perObjectBuffers.find(objectId);
    if (objectIt != m_perObjectBuffers.end()) {
        // A second camera can carry the same one-shot force flag in its copied
        // draw calls. The object was already updated from the same scene cache
        // on this frame, so skip it regardless of that stale copy.
        if (objectIt->second.ensuredOnFrame == m_ensureFrameCounter)
            return;
    }
    if (objectIt != m_perObjectBuffers.end() && !forceUpdate) {
        // Fast path: if data pointers AND sizes match, content hasn't changed
        if (objectIt->second.lastVertexPtr == vertices.data() && objectIt->second.lastIndexPtr == indices.data() &&
            objectIt->second.vertexCount == vertices.size() && objectIt->second.indexCount == indices.size()) {
            objectIt->second.ensuredOnFrame = m_ensureFrameCounter;
            return;
        }
    }

    // Asset-backed and built-in shared meshes already carry a generation-safe
    // identity. Hashing every vertex again for every GameObject defeats that
    // cache for large ordinary scenes (for example, 20k Cube renderers).
    // Dynamic inline meshes have no such identity and still use content hash
    // deduplication so pointer recycling cannot publish stale geometry.
    const size_t vtxBytes = vertices.size() * sizeof(Vertex);
    const size_t idxBytes = indices.size() * sizeof(uint32_t);
    const size_t contentHash =
        assetGuid.empty() ? HashMeshContent(vertices.data(), vtxBytes, indices.data(), idxBytes) : 0;
    const SharedMeshKey sharedKey{assetGuid, runtimeVersion, contentHash, vertices.size(), indices.size()};

    // Check if object already maps to this exact content (pointer changed but content same)
    if (objectIt != m_perObjectBuffers.end() && !forceUpdate) {
        if (objectIt->second.sharedKey == sharedKey) {
            objectIt->second.lastVertexPtr = vertices.data();
            objectIt->second.lastIndexPtr = indices.data();
            // Pointer identity is only a fast-path hint. RenderWorld frame
            // publications may recycle equivalent CPU mesh storage, so a
            // pointer change with identical content is still a live use of
            // this object on the current ensure frame. Without this stamp the
            // frame-end sweep removes the object, and it is recreated on the
            // next frame, producing an appear/disappear cycle.
            objectIt->second.ensuredOnFrame = m_ensureFrameCounter;
            return;
        }
    }

    auto sharedIt = m_sharedMeshBuffers.find(sharedKey);
    // NOTE: forceUpdate is intentionally NOT included in needsCreate.
    // The content hash already guarantees correctness — if the hash matches
    // an existing shared buffer, the GPU data is identical regardless of
    // forceUpdate.  Including forceUpdate here caused a catastrophic bug:
    // when the Scene view opened and ConsumeMeshBufferDirty() returned true
    // for all objects, each object would create its own VkBuffer (replacing
    // the shared entry), permanently destroying instancing (4 → 6000+ draws).
    const bool needsCreate =
        (sharedIt == m_sharedMeshBuffers.end() || sharedIt->second.vertexCount != vertices.size() ||
         sharedIt->second.indexCount != indices.size() || !sharedIt->second.vertexBuffer ||
         !sharedIt->second.indexBuffer);

    if (needsCreate) {
        SharedMeshBuffers sharedBuffers;
        auto pending = m_pendingSharedMeshBuffers.find(sharedKey);
        if (pending == m_pendingSharedMeshBuffers.end()) {
            PendingSharedMeshBuffers uploads;
            uploads.vertexUpload = m_resourceManager.BeginBufferUpload(
                {vertices.data(), vertices.size() * sizeof(Vertex), rhi::BufferUsage::Vertex});
            uploads.indexUpload = m_resourceManager.BeginBufferUpload(
                {indices.data(), indices.size() * sizeof(uint32_t), rhi::BufferUsage::Index});
            uploads.vertexCount = vertices.size();
            uploads.indexCount = indices.size();
            ++m_submittedMeshUploadCount;
            if (uploads.vertexUpload->IsAsync() || uploads.indexUpload->IsAsync())
                ++m_asyncMeshUploadCount;
            pending = m_pendingSharedMeshBuffers.emplace(sharedKey, std::move(uploads)).first;
        }

        const bool vertexReady = m_resourceManager.TryPublishBufferUpload(pending->second.vertexUpload);
        const bool indexReady = m_resourceManager.TryPublishBufferUpload(pending->second.indexUpload);
        if (!vertexReady || !indexReady) {
            // Keep the previous published buffers while the new content is in
            // flight. The draw path already validates dc.indexStart+indexCount
            // against the leased buffer's indexCount, so a stale lease can
            // never read out of bounds — it either draws last frame's geometry
            // or skips the object when the new draw range no longer fits.
            // Erasing the entry here made every-frame dynamic meshes
            // (LineRenderer trails) invisible for the whole movement on
            // devices where transfer publication falls back to fences.
            if (objectIt != m_perObjectBuffers.end())
                objectIt->second.ensuredOnFrame = m_ensureFrameCounter;
            return;
        }

        sharedBuffers.vertexBuffer = pending->second.vertexUpload->GetBuffer();
        sharedBuffers.indexBuffer = pending->second.indexUpload->GetBuffer();
        sharedBuffers.vertexCount = pending->second.vertexCount;
        sharedBuffers.indexCount = pending->second.indexCount;
        m_pendingSharedMeshBuffers.erase(pending);
        ++m_completedMeshUploadCount;

        PublishSharedMeshBuffers(sharedKey, std::move(sharedBuffers));
        sharedIt = m_sharedMeshBuffers.find(sharedKey);
    }

    PerObjectBuffers objectBuffers;
    objectBuffers.vertexBuffer = sharedIt->second.vertexBuffer;
    objectBuffers.indexBuffer = sharedIt->second.indexBuffer;
    objectBuffers.vertexCount = sharedIt->second.vertexCount;
    objectBuffers.indexCount = sharedIt->second.indexCount;
    objectBuffers.sharedKey = sharedKey;
    objectBuffers.lastVertexPtr = vertices.data();
    objectBuffers.lastIndexPtr = indices.data();
    objectBuffers.ensuredOnFrame = m_ensureFrameCounter;

    m_perObjectBuffers[objectId] = std::move(objectBuffers);
    ++m_objectBufferRevision;
    sharedIt->second.lastUsedFrame = m_ensureFrameCounter;
    (void)TrimMeshGpuBudget();
}

void InxVkCoreModular::CleanupUnusedBuffers(const std::vector<DrawCall> &activeDrawCalls)
{
    // Build set of active objectIds
    std::unordered_set<uint64_t> activeIds;
    for (const auto &dc : activeDrawCalls) {
        activeIds.insert(dc.objectId);
    }
    CleanupUnusedBuffersByIds(activeIds);
}

void InxVkCoreModular::CleanupUnusedBuffersByIds(const std::unordered_set<uint64_t> &activeIds)
{
    bool anyRemoved = false;
    for (auto it = m_perObjectBuffers.begin(); it != m_perObjectBuffers.end();) {
        if (activeIds.find(it->first) == activeIds.end()) {
            auto shared = m_sharedMeshBuffers.find(it->second.sharedKey);
            if (shared != m_sharedMeshBuffers.end())
                shared->second.lastUsedFrame = m_ensureFrameCounter;
            it = m_perObjectBuffers.erase(it);
            anyRemoved = true;
        } else {
            ++it;
        }
    }
    if (anyRemoved)
        ++m_objectBufferRevision;
    (void)TrimMeshGpuBudget();
}

size_t InxVkCoreModular::CleanupUnusedBuffersByFrameStamp()
{
    if (m_skipObjectBufferCleanupThisFrame) {
        m_skipObjectBufferCleanupThisFrame = false;
        return m_perObjectBuffers.size();
    }

    // Remove objects that were not referenced by EnsureObjectBuffers on this frame.
    bool anyRemoved = false;
    for (auto it = m_perObjectBuffers.begin(); it != m_perObjectBuffers.end();) {
        if (it->second.ensuredOnFrame != m_ensureFrameCounter) {
            auto shared = m_sharedMeshBuffers.find(it->second.sharedKey);
            if (shared != m_sharedMeshBuffers.end())
                shared->second.lastUsedFrame = m_ensureFrameCounter;
            it = m_perObjectBuffers.erase(it);
            anyRemoved = true;
        } else {
            ++it;
        }
    }

    if (anyRemoved)
        ++m_objectBufferRevision;

    if (anyRemoved)
        (void)TrimMeshGpuBudget();

    if (anyRemoved) {
        for (auto &entry : m_objectBufferBindingCache)
            entry.valid = false;
    }

    return m_perObjectBuffers.size();
}

void InxVkCoreModular::PublishSharedMeshBuffers(const SharedMeshKey &key, SharedMeshBuffers buffers)
{
    if (!buffers.vertexBuffer || !buffers.indexBuffer || !buffers.vertexBuffer->IsValid() ||
        !buffers.indexBuffer->IsValid())
        throw std::invalid_argument("Cannot publish invalid shared mesh buffers");
    const uint64_t vertexBytes = buffers.vertexBuffer->GetSize();
    const uint64_t indexBytes = buffers.indexBuffer->GetSize();
    if (vertexBytes == 0 || indexBytes == 0 || indexBytes > std::numeric_limits<uint64_t>::max() - vertexBytes)
        throw std::overflow_error("Invalid shared mesh GPU byte size");
    buffers.residentBytes = vertexBytes + indexBytes;
    buffers.lastUsedFrame = m_ensureFrameCounter;

    auto existing = m_sharedMeshBuffers.find(key);
    if (existing != m_sharedMeshBuffers.end()) {
        SharedMeshBuffers retired = std::move(existing->second);
        m_sharedMeshBuffers.erase(existing);
        RetireSharedMeshBuffers(std::move(retired), false);
    }
    if (buffers.residentBytes > std::numeric_limits<uint64_t>::max() - m_meshGpuResidentBytes)
        throw std::overflow_error("Mesh GPU residency byte counter overflow");
    m_meshGpuResidentBytes += buffers.residentBytes;
    const auto [inserted, didInsert] = m_sharedMeshBuffers.emplace(key, std::move(buffers));
    (void)inserted;
    if (!didInsert)
        throw std::logic_error("Shared mesh cache rejected a unique key");
}

void InxVkCoreModular::RetireSharedMeshBuffers(SharedMeshBuffers buffers, bool eviction)
{
    if (!buffers.vertexBuffer || !buffers.indexBuffer || buffers.residentBytes == 0)
        throw std::logic_error("Cannot retire invalid shared mesh residency");
    m_retiredMeshLeases.push_back({buffers.vertexBuffer, buffers.indexBuffer, buffers.residentBytes});
    auto retired = std::make_shared<SharedMeshBuffers>(std::move(buffers));
    m_deletionQueue.Retire([retired = std::move(retired)]() mutable {
        retired->vertexBuffer.reset();
        retired->indexBuffer.reset();
    });
    if (eviction)
        ++m_meshGpuEvictionCount;
}

void InxVkCoreModular::SweepRetiredMeshLeases() const
{
    size_t writeIndex = 0;
    for (size_t index = 0; index < m_retiredMeshLeases.size(); ++index) {
        const auto &lease = m_retiredMeshLeases[index];
        if (lease.vertexBuffer.expired() && lease.indexBuffer.expired()) {
            if (lease.residentBytes > m_meshGpuResidentBytes)
                throw std::logic_error("Mesh GPU residency byte counter underflow");
            m_meshGpuResidentBytes -= lease.residentBytes;
            continue;
        }
        if (writeIndex != index)
            m_retiredMeshLeases[writeIndex] = std::move(m_retiredMeshLeases[index]);
        ++writeIndex;
    }
    m_retiredMeshLeases.resize(writeIndex);
}

uint64_t InxVkCoreModular::GetMeshGpuResidentBytes() const
{
    SweepRetiredMeshLeases();
    return m_meshGpuResidentBytes;
}

size_t InxVkCoreModular::GetRetiredMeshGpuLeaseCount() const
{
    SweepRetiredMeshLeases();
    return m_retiredMeshLeases.size();
}

std::vector<GpuAssetResidencyRecord> InxVkCoreModular::GetAssetMeshGpuResidency() const
{
    std::vector<GpuAssetResidencyRecord> records;
    records.reserve(m_sharedMeshBuffers.size() + m_pendingSharedMeshBuffers.size());
    for (const auto &[key, buffers] : m_sharedMeshBuffers) {
        if (key.assetGuid.empty())
            continue;
        const bool pinned = (buffers.vertexBuffer && buffers.vertexBuffer.use_count() != 1) ||
                            (buffers.indexBuffer && buffers.indexBuffer.use_count() != 1);
        records.push_back({key.assetGuid, key.runtimeVersion, GpuAssetDomain::Mesh, buffers.residentBytes,
                           buffers.lastUsedFrame, false, pinned});
    }
    for (const auto &[key, uploads] : m_pendingSharedMeshBuffers) {
        if (key.assetGuid.empty())
            continue;
        const uint64_t bytes = uploads.vertexUpload->GetSize() + uploads.indexUpload->GetSize();
        records.push_back(
            {key.assetGuid, key.runtimeVersion, GpuAssetDomain::Mesh, bytes, m_ensureFrameCounter, true, true});
    }
    return records;
}

std::vector<GpuAssetResidencyRecord> InxVkCoreModular::GetAssetGpuResidency() const
{
    auto records = GetAssetMeshGpuResidency();
    auto textures = GetAssetTextureGpuResidency();
    records.insert(records.end(), textures.begin(), textures.end());
    return records;
}

size_t InxVkCoreModular::GetRuntimeMeshGpuEntryCount() const
{
    return static_cast<size_t>(std::count_if(m_sharedMeshBuffers.begin(), m_sharedMeshBuffers.end(),
                                             [](const auto &entry) { return entry.first.assetGuid.empty(); }));
}

uint64_t InxVkCoreModular::GetRuntimeMeshGpuResidentBytes() const
{
    uint64_t bytes = 0;
    for (const auto &[key, buffers] : m_sharedMeshBuffers) {
        if (key.assetGuid.empty())
            bytes += buffers.residentBytes;
    }
    return bytes;
}

uint64_t InxVkCoreModular::GetRetiredMeshGpuLeaseBytes() const
{
    SweepRetiredMeshLeases();
    uint64_t bytes = 0;
    for (const auto &lease : m_retiredMeshLeases)
        bytes += lease.residentBytes;
    return bytes;
}

GpuEvictionCandidate InxVkCoreModular::PeekOldestMeshGpuEvictable() const
{
    auto candidate = m_sharedMeshBuffers.end();
    for (auto entry = m_sharedMeshBuffers.begin(); entry != m_sharedMeshBuffers.end(); ++entry) {
        if (!entry->second.vertexBuffer || !entry->second.indexBuffer || entry->second.vertexBuffer.use_count() != 1 ||
            entry->second.indexBuffer.use_count() != 1 || entry->second.lastUsedFrame >= m_ensureFrameCounter)
            continue;
        if (candidate == m_sharedMeshBuffers.end() || entry->second.lastUsedFrame < candidate->second.lastUsedFrame)
            candidate = entry;
    }
    if (candidate == m_sharedMeshBuffers.end())
        return {};
    return {candidate->second.lastUsedFrame, candidate->second.residentBytes, true};
}

uint64_t InxVkCoreModular::EvictOldestMeshGpu()
{
    auto candidate = m_sharedMeshBuffers.end();
    for (auto entry = m_sharedMeshBuffers.begin(); entry != m_sharedMeshBuffers.end(); ++entry) {
        if (!entry->second.vertexBuffer || !entry->second.indexBuffer || entry->second.vertexBuffer.use_count() != 1 ||
            entry->second.indexBuffer.use_count() != 1 || entry->second.lastUsedFrame >= m_ensureFrameCounter)
            continue;
        if (candidate == m_sharedMeshBuffers.end() || entry->second.lastUsedFrame < candidate->second.lastUsedFrame)
            candidate = entry;
    }
    if (candidate == m_sharedMeshBuffers.end())
        return 0;
    SharedMeshBuffers retired = std::move(candidate->second);
    const uint64_t bytes = retired.residentBytes;
    m_sharedMeshBuffers.erase(candidate);
    RetireSharedMeshBuffers(std::move(retired), true);
    return bytes;
}

void InxVkCoreModular::SetMeshGpuBudgetBytes(uint64_t bytes)
{
    if (bytes == 0)
        throw std::invalid_argument("GPU mesh budget must be greater than zero");
    m_meshGpuBudgetBytes = bytes;
    (void)TrimMeshGpuBudget();
}

size_t InxVkCoreModular::TrimMeshGpuBudget()
{
    SweepRetiredMeshLeases();
    size_t evicted = 0;
    while (m_meshGpuResidentBytes > m_meshGpuBudgetBytes) {
        auto candidate = m_sharedMeshBuffers.end();
        for (auto entry = m_sharedMeshBuffers.begin(); entry != m_sharedMeshBuffers.end(); ++entry) {
            if (!entry->second.vertexBuffer || !entry->second.indexBuffer ||
                entry->second.vertexBuffer.use_count() != 1 || entry->second.indexBuffer.use_count() != 1 ||
                entry->second.lastUsedFrame >= m_ensureFrameCounter)
                continue;
            if (candidate == m_sharedMeshBuffers.end() || entry->second.lastUsedFrame < candidate->second.lastUsedFrame)
                candidate = entry;
        }
        if (candidate == m_sharedMeshBuffers.end())
            break;
        SharedMeshBuffers retired = std::move(candidate->second);
        m_sharedMeshBuffers.erase(candidate);
        RetireSharedMeshBuffers(std::move(retired), true);
        ++evicted;
    }
    return evicted;
}

// ============================================================================
// Per-View Descriptor Set (set 1) — multi-camera shadow isolation
// ============================================================================

bool InxVkCoreModular::CreatePerViewDescriptorResources()
{
    VkDevice device = GetDevice();
    if (device == VK_NULL_HANDLE)
        return false;

    // Canonical per-view ABI. Geometry uses binding 0 plus the tiled buffers;
    // particles also consume binding 4 because their set 0 remains dedicated
    // to simulation instances and material resources.
    std::array<VkDescriptorSetLayoutBinding, 6> bindings{};
    bindings[0].binding = 0;
    bindings[0].descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    bindings[0].descriptorCount = 1;
    bindings[0].stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
    for (uint32_t binding = 1; binding <= 3; ++binding) {
        bindings[binding].binding = binding;
        bindings[binding].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        bindings[binding].descriptorCount = 1;
        bindings[binding].stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
    }
    bindings[4].binding = 4;
    bindings[4].descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    bindings[4].descriptorCount = 1;
    bindings[4].stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
    bindings[5].binding = 5;
    bindings[5].descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    bindings[5].descriptorCount = 1;
    bindings[5].stageFlags = VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT;

    VkDescriptorSetLayoutCreateInfo layoutInfo{};
    layoutInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    layoutInfo.bindingCount = static_cast<uint32_t>(bindings.size());
    layoutInfo.pBindings = bindings.data();

    if (vkCreateDescriptorSetLayout(device, &layoutInfo, nullptr, &m_perViewDescSetLayout) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create per-view descriptor set layout");
        return false;
    }
    ShaderProgram::SetPerViewDescSetLayout(m_perViewDescSetLayout);

    INXLOG_INFO("Created per-view descriptor set layout (multi-camera shadow)");
    return true;
}

void InxVkCoreModular::DestroyPerViewDescriptorResources()
{
    VkDevice device = GetDevice();
    if (device == VK_NULL_HANDLE)
        return;

    if (m_perViewDescSetLayout != VK_NULL_HANDLE) {
        ShaderProgram::SetPerViewDescSetLayout(VK_NULL_HANDLE);
        vkDestroyDescriptorSetLayout(device, m_perViewDescSetLayout, nullptr);
        m_perViewDescSetLayout = VK_NULL_HANDLE;
    }
}

vk::DescriptorLease InxVkCoreModular::AllocatePerViewDescriptorLease()
{
    if (m_perViewDescSetLayout == VK_NULL_HANDLE) {
        INXLOG_ERROR("Per-view descriptor resources not initialized");
        return {};
    }

    auto lease = m_backend.Device().GetRhiDevice().GetDescriptorManager().Allocate(m_perViewDescSetLayout,
                                                                                   vk::DescriptorArena::ViewPersistent);
    if (!lease.IsValid()) {
        INXLOG_ERROR("Failed to allocate per-view descriptor set");
        return {};
    }

    // Initialize with default (white) texture so shaders don't sample garbage
    ClearPerViewShadowMap(lease.set);

    return lease;
}

void InxVkCoreModular::UpdatePerViewShadowMap(VkDescriptorSet perViewDescSet, VkImageView shadowView,
                                              VkSampler shadowSampler, VkImageLayout imageLayout)
{
    if (perViewDescSet == VK_NULL_HANDLE || shadowView == VK_NULL_HANDLE || shadowSampler == VK_NULL_HANDLE)
        return;

    VkDescriptorImageInfo imageInfo{};
    imageInfo.imageLayout = imageLayout;
    imageInfo.imageView = shadowView;
    imageInfo.sampler = shadowSampler;

    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = perViewDescSet;
    write.dstBinding = 0;
    write.dstArrayElement = 0;
    write.descriptorCount = 1;
    write.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    write.pImageInfo = &imageInfo;

    vkUpdateDescriptorSets(GetDevice(), 1, &write, 0, nullptr);
}

void InxVkCoreModular::ClearPerViewShadowMap(VkDescriptorSet perViewDescSet)
{
    if (perViewDescSet == VK_NULL_HANDLE)
        return;

    // Use default white texture so depth comparison = 1.0 → fully lit (no shadow)
    auto &descMgr = m_materialPipelineManager.GetDescriptorManager();
    VkImageView defaultView = descMgr.GetDefaultImageView();
    VkSampler defaultSampler = descMgr.GetDefaultSampler();

    if (defaultView == VK_NULL_HANDLE || defaultSampler == VK_NULL_HANDLE) {
        INXLOG_WARN("ClearPerViewShadowMap: default texture not available");
        return;
    }

    UpdatePerViewShadowMap(perViewDescSet, defaultView, defaultSampler, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);
}

void InxVkCoreModular::UpdatePerViewForwardPlusBuffers(VkDescriptorSet perViewDescSet,
                                                       rhi::BufferHandle canonicalLights, uint64_t canonicalBytes,
                                                       rhi::BufferHandle tileHeaders, uint64_t tileHeaderBytes,
                                                       rhi::BufferHandle tileLightMasks, uint64_t tileLightMaskBytes,
                                                       rhi::BufferHandle lightingUbo, uint64_t lightingUboBytes)
{
    if (perViewDescSet == VK_NULL_HANDLE || canonicalBytes == 0 || tileHeaderBytes == 0 || tileLightMaskBytes == 0)
        return;

    auto &rhiDevice = m_backend.Device().GetRhiDevice();
    const std::array<VkDescriptorBufferInfo, 3> infos = {{
        {rhiDevice.Resolve(canonicalLights), 0, canonicalBytes},
        {rhiDevice.Resolve(tileHeaders), 0, tileHeaderBytes},
        {rhiDevice.Resolve(tileLightMasks), 0, tileLightMaskBytes},
    }};
    if (std::any_of(infos.begin(), infos.end(), [](const auto &info) { return info.buffer == VK_NULL_HANDLE; }))
        return;

    std::array<VkWriteDescriptorSet, 4> writes{};
    uint32_t writeCount = 0;
    for (uint32_t index = 0; index < infos.size(); ++index) {
        auto &write = writes[writeCount++];
        write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        write.dstSet = perViewDescSet;
        write.dstBinding = index + 1u;
        write.descriptorCount = 1;
        write.descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        write.pBufferInfo = &infos[index];
    }
    VkDescriptorBufferInfo lightingInfo{};
    if (lightingUbo.IsValid() && lightingUboBytes > 0) {
        lightingInfo = {rhiDevice.Resolve(lightingUbo), 0, lightingUboBytes};
        if (lightingInfo.buffer != VK_NULL_HANDLE) {
            auto &write = writes[writeCount++];
            write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            write.dstSet = perViewDescSet;
            write.dstBinding = 4;
            write.descriptorCount = 1;
            write.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
            write.pBufferInfo = &lightingInfo;
        }
    }
    vkUpdateDescriptorSets(GetDevice(), writeCount, writes.data(), 0, nullptr);
}

void InxVkCoreModular::UpdatePerViewLightingBuffer(VkDescriptorSet perViewDescSet, VkBuffer lightingUbo,
                                                   uint64_t lightingUboBytes)
{
    if (perViewDescSet == VK_NULL_HANDLE || lightingUbo == VK_NULL_HANDLE || lightingUboBytes == 0)
        return;
    VkDescriptorBufferInfo info{lightingUbo, 0, lightingUboBytes};
    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = perViewDescSet;
    write.dstBinding = 4;
    write.descriptorCount = 1;
    write.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    write.pBufferInfo = &info;
    vkUpdateDescriptorSets(GetDevice(), 1, &write, 0, nullptr);
}

void InxVkCoreModular::UpdatePerViewCameraBuffer(VkDescriptorSet perViewDescSet, VkBuffer cameraUbo,
                                                 uint64_t cameraUboBytes)
{
    if (perViewDescSet == VK_NULL_HANDLE || cameraUbo == VK_NULL_HANDLE || cameraUboBytes == 0)
        return;
    VkDescriptorBufferInfo info{cameraUbo, 0, cameraUboBytes};
    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = perViewDescSet;
    write.dstBinding = 5;
    write.descriptorCount = 1;
    write.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    write.pBufferInfo = &info;
    vkUpdateDescriptorSets(GetDevice(), 1, &write, 0, nullptr);
}

} // namespace infernux
