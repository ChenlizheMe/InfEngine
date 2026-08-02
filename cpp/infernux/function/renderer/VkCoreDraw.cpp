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

    if (result == vk::SwapchainResult::NeedRecreate) {
        RecreateSwapchain();
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
    const bool asyncCompute = independentCompute && m_frameAsyncSimulationExecutor && m_frameAsyncExportExecutor &&
                              m_frameAsyncComputeReady && m_frameAsyncComputeGeneration && m_frameAsyncComputeReady();
    const uint64_t asyncComputeGeneration = asyncCompute ? m_frameAsyncComputeGeneration() : 0;
    const bool primeAsyncCompute =
        asyncCompute && (!m_frameAsyncComputePrimed || asyncComputeGeneration != m_frameAsyncComputePrimedGeneration);
    const bool separateComputeBatch = !asyncCompute && m_frameComputeExecutor &&
                                      graphicsQueue.queue != VK_NULL_HANDLE && computeQueue.queue != VK_NULL_HANDLE;

    const bool composedFrame = static_cast<bool>(m_frameSubmissionBuilder);
    if (composedFrame) {
        if (!EnsureGuiRenderGraph(imageIndex))
            return;

        m_frameSubmission.Reset();
        const rhi::DeviceId device = m_backend.Device().GetDeviceId();
        std::vector<uint32_t> setupDependencies;
        if (primeAsyncCompute) {
            const uint32_t prime = m_frameSubmission.AddWork(
                device, rhi::QueueRole::Compute, rhi::SubmissionDomain::Frame, rhi::InvalidRenderViewId,
                rhi::PipelineStage::ComputeShader, {}, [this](VkCommandBuffer commandBuffer) {
                    return m_frameAsyncSimulationExecutor(commandBuffer) && m_frameAsyncExportExecutor(commandBuffer);
                });
            setupDependencies.push_back(prime);
        } else if (separateComputeBatch) {
            const uint32_t compute = m_frameSubmission.AddWork(
                device, rhi::QueueRole::Compute, rhi::SubmissionDomain::Frame, rhi::InvalidRenderViewId,
                rhi::PipelineStage::ComputeShader, {}, [this](VkCommandBuffer commandBuffer) {
                    m_frameComputeExecutor(commandBuffer);
                    return true;
                });
            setupDependencies.push_back(compute);
        }

        const uint32_t setupWork = m_frameSubmission.AddWork(
            device, rhi::QueueRole::Graphics, rhi::SubmissionDomain::Frame, m_presentationView.id,
            rhi::PipelineStage::AllGraphics, std::move(setupDependencies), [this](VkCommandBuffer commandBuffer) {
#if INFERNUX_FRAME_PROFILE
                m_gpuTimestampQueries.BeginFrame(commandBuffer, m_currentFrame);
#endif
                CmdUpdateGlobals(commandBuffer);
                return true;
            });

        if (!m_frameSubmissionBuilder(m_frameSubmission, setupWork)) {
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
                [this](VkCommandBuffer commandBuffer) { return m_frameAsyncSimulationExecutor(commandBuffer); });
        }

        vk::RenderGraph &guiGraph = GetGuiRenderGraph(imageIndex);
        const auto guiRange = m_frameSubmission.AppendRenderGraph(guiGraph, {setupWork}, {}, [this](VkCommandBuffer) {
#if INFERNUX_FRAME_PROFILE
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
                [this](VkCommandBuffer commandBuffer) { return m_frameAsyncExportExecutor(commandBuffer); });
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
    if (asyncCompute && !primeAsyncCompute) {
        externalSync.previousFrameTimeline = m_frameAsyncPreviousExportTimeline;
        externalSync.previousFrameTimelineValue = m_frameAsyncPreviousExportTimelineValue;
        externalSync.previousFrameStages = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
    }

    vk::VulkanSubmissionExecutor::ExecuteResult executeResult{};
    try {
        executeResult = m_submissionExecutor.Execute(
            frameSlot, submissionPlan,
            [this, imageIndex, asyncCompute, primeAsyncCompute, separateComputeBatch, composedFrame,
             &submissionPlan](uint32_t batchIndex, VkCommandBuffer commandBuffer) {
                if (batchIndex >= submissionPlan.batches.size())
                    return false;
                if (composedFrame)
                    return m_frameSubmission.RecordBatch(submissionPlan, batchIndex, commandBuffer);
                const auto queue = submissionPlan.batches[batchIndex].queue;
                if (asyncCompute) {
                    if (primeAsyncCompute) {
                        if (batchIndex == 0)
                            return m_frameAsyncSimulationExecutor(commandBuffer) &&
                                   m_frameAsyncExportExecutor(commandBuffer);
                        return batchIndex == 1 && RecordFrameCommands(commandBuffer, imageIndex);
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
                if (!separateComputeBatch && m_frameComputeExecutor)
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
        if (asyncCompute && !primeAsyncCompute) {
            m_frameAsyncPreviousExportTimeline = executeResult.completionTimeline;
            m_frameAsyncPreviousExportTimelineValue = executeResult.completionTimelineValue;
        } else {
            m_frameAsyncPreviousExportTimeline = VK_NULL_HANDLE;
            m_frameAsyncPreviousExportTimelineValue = 0;
        }
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
    if (result == vk::SwapchainResult::NeedRecreate || m_framebufferResized) {
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

void InxVkCoreModular::SetDrawCalls(const std::vector<DrawCall> *drawCalls)
{
    m_drawCallsPtr = drawCalls;

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
    m_drawQueueValues.reserve(kTrackedQueueLimit);
    for (const DrawCall &drawCall : *drawCalls) {
        const InxMaterial *material = drawCall.material ? drawCall.material.get() : m_cachedDefaultLit.get();
        if (!material)
            continue;
        const int queue = material->GetRenderQueue();
        if (std::find(m_drawQueueValues.begin(), m_drawQueueValues.end(), queue) != m_drawQueueValues.end())
            continue;
        if (m_drawQueueValues.size() == kTrackedQueueLimit) {
            m_drawQueueValuesOverflow = true;
            m_drawQueueValues.clear();
            break;
        }
        m_drawQueueValues.push_back(queue);
    }
}

void InxVkCoreModular::SetShadowDrawCalls(const std::vector<DrawCall> *drawCalls)
{
    m_shadowDrawCallsPtr = drawCalls;
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

    if (overrideMaterial.empty() && !m_drawQueueValuesOverflow) {
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

    bool hasAnyBuffers = !m_perObjectBuffers.empty();
    if (!hasAnyBuffers) {
        return;
    }

    const auto &defaultMaterial = m_cachedDefaultLit;
    const auto &errorMaterial = m_cachedErrorMat;
    if (!defaultMaterial) {
        return;
    }

    // Resolve override material (if specified)
    InxMaterial *overrideMatRaw = nullptr;
    std::shared_ptr<InxMaterial> overrideMatOwner; // keeps alive during this scope
    if (!overrideMaterial.empty()) {
        overrideMatOwner = AssetRegistry::Instance().GetBuiltinMaterial(overrideMaterial);
        overrideMatRaw = overrideMatOwner.get();
    }

    InxMaterial *defaultMatRaw = defaultMaterial.get();

    // ---- Collect eligible draw calls (queue filter + frustum cull) ----
    m_eligibleScratch.clear();

    for (const DrawCall &dc : drawCalls()) {
        if (!dc.frustumVisible)
            continue;

        const std::shared_ptr<InxMaterial> *materialOwner =
            overrideMatOwner ? &overrideMatOwner : (dc.material ? &dc.material : &defaultMaterial);
        InxMaterial *material = materialOwner->get();
        if (!material)
            continue;

        int queue = material->GetRenderQueue();
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

        // Pass tag filter: if a pass tag is specified, only draw materials whose
        // passTag matches. Empty passTag on either side means "match all".
        if (!passTag.empty()) {
            const std::string &matTag = material->GetPassTag();
            if (!matTag.empty() && matTag != passTag)
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
        auto bufIt = m_perObjectBuffers.find(dc.objectId);
        VkBuffer vb = VK_NULL_HANDLE;
        if (bufIt != m_perObjectBuffers.end() && bufIt->second.vertexBuffer)
            vb = bufIt->second.vertexBuffer->GetBuffer();

        m_eligibleScratch.push_back({&dc, sortKey, matHash, vb, materialOwner, material, bufIt});
    }

    // Diagnostic: log per-call eligible count with queue range
    if (s_filterDiagFrames < 3) {
        INXLOG_DEBUG("[DrawSceneFiltered] queue=[", queueMin, ",", queueMax, "] totalDC=", drawCalls().size(),
                     " eligible=", m_eligibleScratch.size());
        if (!m_eligibleScratch.empty()) {
            for (const auto &entry : m_eligibleScratch) {
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
    m_drawSceneFilteredEligible += static_cast<uint64_t>(m_eligibleScratch.size());
#endif

    // One-shot diagnostic: log icon draw calls that pass filtering
    {
        static int s_iconDiagCount = 0;
        if (s_iconDiagCount < 5) {
            for (const auto &entry : m_eligibleScratch) {
                const std::string &matName = entry.material->GetName();
                if (matName.find("GizmoIcon") != std::string::npos || matName.find("Gizmo") != std::string::npos) {
                    const DrawCall &dc = *entry.dc;
                    auto bufIt = m_perObjectBuffers.find(dc.objectId);
                    bool hasBuf =
                        (bufIt != m_perObjectBuffers.end() && bufIt->second.vertexBuffer && bufIt->second.indexBuffer);
                    VkPipeline pip = entry.material->GetPassPipeline(ShaderCompileTarget::Forward);
                    VkDescriptorSet ds = entry.material->GetPassDescriptorSet(ShaderCompileTarget::Forward);
                    ++s_iconDiagCount;
                }
            }
        }
    }

    if (m_eligibleScratch.empty()) {
#if INFERNUX_FRAME_PROFILE
        m_drawSubMs[8] += std::chrono::duration<double, std::milli>(Clock::now() - totalStart).count();
#endif
        return;
    }

    // ---- Sort if requested (skip for 0-1 elements) ----
    // Uniform-batch fast path: when every eligible entry shares the same
    // material hash and vertex buffer, all entries will be emitted as a
    // single instanced draw regardless of ordering.  Sorting would only
    // permute elements within that single batch, so we skip it entirely.
    bool uniformBatch = false;
    if (m_eligibleScratch.size() > 1) {
        const size_t firstMatHash = m_eligibleScratch[0].materialHash;
        const VkBuffer firstVB = m_eligibleScratch[0].vertexBuf;
        uniformBatch = true;
        for (size_t i = 1; i < m_eligibleScratch.size(); ++i) {
            if (m_eligibleScratch[i].materialHash != firstMatHash || m_eligibleScratch[i].vertexBuf != firstVB) {
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
            // Group by material + vertex buffer only (no depth).
            // This order is stable across frames for static material assignments,
            // so is_sorted() returns true and std::sort is skipped entirely.
            auto cmp = [](const SortableDrawCall &a, const SortableDrawCall &b) {
                if (a.materialHash != b.materialHash)
                    return a.materialHash < b.materialHash;
                return a.vertexBuf < b.vertexBuf;
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
                return a.vertexBuf < b.vertexBuf;
            };
            if (!std::is_sorted(m_eligibleScratch.begin(), m_eligibleScratch.end(), cmp)) {
                std::sort(m_eligibleScratch.begin(), m_eligibleScratch.end(), cmp);
            }
        }
    } // size() > 1

#if INFERNUX_FRAME_PROFILE
    stageNow = Clock::now();
    m_drawSubMs[10] += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
    stageStart = stageNow;
#endif

    // ---- Upload instance model matrices to SSBO (set 2, binding 1) ----
    ResetPerFrameGpuStreamOffsets();

    const uint32_t frameIndex = m_currentFrame % m_maxFramesInFlight;
    const size_t totalEligible = m_eligibleScratch.size();
    const uint32_t writeBase = m_instanceWriteOffset;
    const bool needsInstanceAuxiliary = ShaderCompileTargetUsesInstanceAuxiliary(activePass.target);
    if (needsInstanceAuxiliary)
        PrepareInstanceAuxiliary(m_ensureFrameCounter, writeBase + totalEligible);

    if (totalEligible > 0 && frameIndex < m_instanceBuffers.size()) {
        const bool needsPreviousSkinPalette = activePass.target == ShaderCompileTarget::Motion;
        size_t requiredBoneMatrices = m_skinPaletteWriteOffset;
        for (const auto &entry : m_eligibleScratch) {
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
        if (instanceBufferChanged)
            UpdateInstanceBufferDescriptor(frameIndex);
        if (skinBufferChanged)
            UpdateSkinBufferDescriptors(frameIndex);

        auto &instFrame = m_instanceBuffers[frameIndex];
        auto &skinInstFrame = m_skinInstanceBuffers[frameIndex];
        auto &skinPaletteFrame = m_skinPaletteBuffers[frameIndex];
        if (instFrame.buffer) {
            void *mapped = instFrame.mapped;
            if (!mapped) {
                mapped = instFrame.buffer->Map();
                instFrame.mapped = mapped;
            }
            if (mapped) {
                glm::mat4 *matrices = static_cast<glm::mat4 *>(mapped);
                for (size_t i = 0; i < totalEligible; ++i) {
                    matrices[writeBase + i] = m_eligibleScratch[i].dc->worldMatrix;
                }
            }
        }

        if (needsInstanceAuxiliary) {
            for (size_t i = 0; i < totalEligible; ++i) {
                const DrawCall &draw = *m_eligibleScratch[i].dc;
                const uint64_t pickingId = draw.pickingObjectId != 0 ? draw.pickingObjectId : draw.objectId;
                (void)WriteInstanceAuxiliary(frameIndex, writeBase + static_cast<uint32_t>(i), draw.identity,
                                             draw.worldMatrix, pickingId, draw.layerMask);
            }
        }

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
                    skinInstances[writeBase + i] = resolveSkinData(*m_eligibleScratch[i].dc);
                }
            }
        }
        m_instanceWriteOffset += static_cast<uint32_t>(totalEligible);
    }

    // ---- Draw loop with instanced batching ----
    VkPipeline currentPipeline = VK_NULL_HANDLE;
    VkPipelineLayout currentLayout = VK_NULL_HANDLE;
    VkDescriptorSet currentDescriptorSet = VK_NULL_HANDLE;
    InxMaterial *currentMaterialRaw = nullptr;
    VkBuffer currentVertexBuffer = VK_NULL_HANDLE;
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
            if (!artifact || !artifact->FindVariant(activePass.target))
                return {};
            program = m_shaderCache.MaterializeProgramVariant(stages, activePass.target);
            if (!program)
                return {};
        }
        MaterialPassRenderData *pass = m_materialPipelineManager.GetOrCreatePassRenderData(owner, program, activePass);
        if (!pass || !pass->isValid)
            return {};
        return {pass->pipeline, pass->pipelineLayout, pass->descriptorSet, pass->shaderProgram};
    };

    // Batch accumulation: consecutive entries sharing (pipeline, descriptorSet, VB, submesh) are
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
        pushData.model = m_eligibleScratch[batchFirstInstance].dc->worldMatrix;
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
        const auto &entry = m_eligibleScratch[idx];
        const DrawCall &dc = *entry.dc;

        // Once a batch has established valid Vulkan state, subsequent
        // consecutive instances with the same material/mesh can extend it
        // without repeating material-pipeline and descriptor validation.
        if (batchInstanceCount > 0) {
            const DrawCall &batchFirst = *m_eligibleScratch[batchFirstInstance].dc;
            const bool batchingAllowed =
                allowBatching || (batchFirst.allowTransparentInstancing && dc.allowTransparentInstancing);
            if (batchingAllowed && entry.material == currentMaterialRaw && entry.vertexBuf == currentVertexBuffer &&
                dc.indexStart == batchIndexStart && dc.indexCount == batchIndexCount &&
                dc.vertexStart == batchVertexStart) {
                ++batchInstanceCount;
                continue;
            }
        }

        // Material already resolved in filter loop — use directly without
        // incrementing a shared_ptr reference count for every instance.
        const std::shared_ptr<InxMaterial> *matOwner = entry.materialOwner;
        InxMaterial *matRaw = matOwner->get();
        ResolvedMaterialPass resolved = resolveMaterialPass(*matOwner);
        if (!resolved.IsValid() && errorMaterial) {
            resolved = resolveMaterialPass(errorMaterial);
            if (resolved.IsValid()) {
                matOwner = &errorMaterial;
                matRaw = errorMaterial.get();
            }
        }
        if (!resolved.IsValid() && defaultMaterial) {
            resolved = resolveMaterialPass(defaultMaterial);
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
        // descriptor set, so resolving first and updating afterwards leaves a
        // stale set in the local draw state during the same command recording.
        if (matRaw != currentMaterialRaw) {
            UpdateMaterialUBO(*matRaw);
            resolved = resolveMaterialPass(*matOwner);
            if (!resolved.IsValid()) {
                emitBatch();
                continue;
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

        // Check GPU buffers for this entry — fresh lookup to avoid stale iterators
        auto bufIt = m_perObjectBuffers.find(dc.objectId);
        if (bufIt == m_perObjectBuffers.end() || !bufIt->second.vertexBuffer || !bufIt->second.indexBuffer) {
            static int bufWarnCount = 0;
            if (bufWarnCount++ < 10) {
                // INXLOG_WARN("[DrawSceneFiltered] no GPU buffers for objectId=", dc.objectId, " material='",
                //             matRaw->GetName(), "' queue=", matRaw->GetRenderQueue());
            }
            emitBatch();
            continue;
        }

        VkBuffer vb = bufIt->second.vertexBuffer->GetBuffer();

        // Check if this entry can extend the current batch. Transparent
        // instancing is opt-in so normal alpha surfaces retain per-object draws.
        bool canExtendBatch = false;
        if (batchInstanceCount > 0) {
            const DrawCall &batchFirst = *m_eligibleScratch[batchFirstInstance].dc;
            const bool batchingAllowed =
                allowBatching || (batchFirst.allowTransparentInstancing && dc.allowTransparentInstancing);
            canExtendBatch = batchingAllowed && pipeline == currentPipeline && descriptorSet == currentDescriptorSet &&
                             matRaw == currentMaterialRaw && vb == currentVertexBuffer &&
                             dc.indexStart == batchIndexStart && dc.indexCount == batchIndexCount &&
                             dc.vertexStart == batchVertexStart;
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
        }

        if (vb != currentVertexBuffer) {
            VkBuffer vertBuffers[] = {vb};
            VkDeviceSize vbOffsets[] = {0};
            vkCmdBindVertexBuffers(cmdBuf, 0, 1, vertBuffers, vbOffsets);
            vkCmdBindIndexBuffer(cmdBuf, bufIt->second.indexBuffer->GetBuffer(), 0, VK_INDEX_TYPE_UINT32);
            currentVertexBuffer = vb;
        }

        // Start new batch
        batchFirstInstance = idx;
        batchInstanceCount = 1;
        batchIndexStart = dc.indexStart;
        batchIndexCount = dc.indexCount;
        batchVertexStart = dc.vertexStart;
        batchPipelineLayout = pipelineLayout;
    }

    // Flush final batch
    emitBatch();

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
                                         const lighting::ShadowFrame &shadowFrame, int lightIndex,
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
    if (!EnsureShadowPipeline(VK_NULL_HANDLE) || !EnsureShadowCameraResources(resourceId))
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

    // Pre-build draw list (filter once, reuse for all cascades)
    m_shadowDrawScratch.clear();
    m_shadowDrawScratch.reserve(shadowDrawCalls().size());
    m_resolvedShadowMaterialsScratch.clear();
    m_resolvedShadowMaterialsScratch.reserve(shadowDrawCalls().size());
    for (const DrawCall &dc : shadowDrawCalls()) {
        if (!dc.castsShadows || !dc.material)
            continue;
        int renderQueue = dc.material->GetRenderQueue();
        if (renderQueue < queueMin || renderQueue > queueMax)
            continue;
        auto bufIt = m_perObjectBuffers.find(dc.objectId);
        if (bufIt == m_perObjectBuffers.end() || !bufIt->second.vertexBuffer || !bufIt->second.indexBuffer)
            continue;

        auto resolved = m_resolvedShadowMaterialsScratch.find(dc.material.get());
        if (resolved == m_resolvedShadowMaterialsScratch.end()) {
            const VkDescriptorSet descriptorSet = EnsureMaterialShadowPipeline(
                dc.material, dc.material->GetVertShaderName(), dc.material->GetFragShaderName());
            ResolvedShadowMaterial resources{};
            resources.pipeline = dc.material->GetPassPipeline(ShaderCompileTarget::Shadow);
            resources.descriptorSet = descriptorSet;
            resolved = m_resolvedShadowMaterialsScratch.emplace(dc.material.get(), resources).first;
        }
        const VkPipeline pip = resolved->second.pipeline;
        const VkDescriptorSet shadowMatDesc = resolved->second.descriptorSet;
        if (pip == VK_NULL_HANDLE || shadowMatDesc == VK_NULL_HANDLE)
            continue;
        m_shadowDrawScratch.push_back({&dc, bufIt, pip, shadowMatDesc, dc.worldBounds});
    }

#if INFERNUX_FRAME_PROFILE
    auto stageNow = Clock::now();
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

    // Sort shadow draw scratch by (pipeline, VB, submesh) for instanced batching
    std::sort(m_shadowDrawScratch.begin(), m_shadowDrawScratch.end(), [](const ShadowDraw &a, const ShadowDraw &b) {
        if (a.shadowPipeline != b.shadowPipeline)
            return a.shadowPipeline < b.shadowPipeline;
        if (a.shadowMaterialDescSet != b.shadowMaterialDescSet)
            return a.shadowMaterialDescSet < b.shadowMaterialDescSet;
        VkBuffer va = a.bufIt->second.vertexBuffer->GetBuffer();
        VkBuffer vb_b = b.bufIt->second.vertexBuffer->GetBuffer();
        if (va != vb_b)
            return va < vb_b;
        if (a.dc->indexStart != b.dc->indexStart)
            return a.dc->indexStart < b.dc->indexStart;
        return a.dc->indexCount < b.dc->indexCount;
    });

#if INFERNUX_FRAME_PROFILE
    stageNow = Clock::now();
    m_drawSubMs[15] += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
    stageStart = stageNow;
#endif

    uint64_t issuedDraws = 0;

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
        // Per-cascade frustum cull into a compact index list, then upload
        // model matrices for visible objects and batch by (pipeline, VB, submesh).
        // Directional cascades must not cull against the light-space near
        // plane: casters between the light and the cascade volume are pancaked
        // onto the near plane by the shadow vertex shader, so rejecting them
        // here would punch holes into shadows cast by tall or distant objects.
        const bool ignoreNearPlane = shadowView.type == lighting::ShadowViewType::DirectionalCascade;
        m_shadowViewVisible.clear();
        m_shadowViewVisible.reserve(m_shadowDrawScratch.size());
        for (size_t si = 0; si < m_shadowDrawScratch.size(); ++si) {
            const auto &sd = m_shadowDrawScratch[si];
            if ((sd.dc->layerMask & shadowView.cullingMask) == 0u)
                continue;
            if (sd.worldBounds.IsValid() && !shadowFrustum.IntersectsAABB(sd.worldBounds, ignoreNearPlane))
                continue;
            m_shadowViewVisible.push_back(static_cast<uint32_t>(si));
        }

#if INFERNUX_FRAME_PROFILE
        stageNow = Clock::now();
        m_drawSubMs[16] += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
        stageStart = stageNow;
#endif

        const uint32_t visibleCount = static_cast<uint32_t>(m_shadowViewVisible.size());
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
        auto *matrices = static_cast<glm::mat4 *>(shadowStream.instanceMapped);
        for (uint32_t vi = 0; vi < visibleCount; ++vi) {
            matrices[writeBase + vi] = m_shadowDrawScratch[m_shadowViewVisible[vi]].dc->worldMatrix;
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
            const DrawCall *dc = m_shadowDrawScratch[m_shadowViewVisible[vi]].dc;
            skinInstances[writeBase + vi] = resolveSkinData(dc ? dc->skinBoneMatrices : nullptr);
        }
        shadowStream.instanceWriteOffset += visibleCount;

#if INFERNUX_FRAME_PROFILE
        stageNow = Clock::now();
        m_drawSubMs[17] += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
        stageStart = stageNow;
#endif

        // Batch accumulation: consecutive visible entries sharing (pipeline, VB, submesh)
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
            const std::array<VkDescriptorSet, 3> descriptorSets = {cascadeDescSet, shadowStreamDescSet,
                                                                   materialDescSet};
            vkdebug::CmdBindDescriptorSetsTracked(
                "VkCoreDraw.DrawShadowCasters.AllSets", cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS, m_shadowPipelineLayout,
                0, static_cast<uint32_t>(descriptorSets.size()), descriptorSets.data(), 0, nullptr);
            struct PushData
            {
                glm::mat4 model;
                glm::mat4 normalMat;
            } pushData;
            pushData.model = m_shadowDrawScratch[m_shadowViewVisible[batchStart]].dc->worldMatrix;
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
            const auto &sd = m_shadowDrawScratch[m_shadowViewVisible[vi]];

            VkBuffer vb = sd.bufIt->second.vertexBuffer->GetBuffer();

            bool canExtend = batchCount > 0 && sd.shadowPipeline == batchPipeline && vb == currentVertexBuffer &&
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
                vkCmdBindIndexBuffer(cmdBuf, sd.bufIt->second.indexBuffer->GetBuffer(), 0, VK_INDEX_TYPE_UINT32);
                currentVertexBuffer = vb;
            }

            batchStart = vi;
            batchCount = 1;
            batchPipeline = sd.shadowPipeline;
            batchShadowMaterialDescSet = sd.shadowMaterialDescSet;
            batchIdxStart = sd.dc->indexStart;
            batchIdxCount = sd.dc->indexCount;
            batchVtxStart = sd.dc->vertexStart;
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

bool InxVkCoreModular::EnsureShadowPipeline(VkRenderPass /*compatibleRenderPass*/)
{
    if (m_shadowPipelineReady)
        return true;

    VkDevice device = GetDevice();

    // --- Create a compatible depth-only render pass ---
    if (m_shadowCompatRenderPass == VK_NULL_HANDLE) {
        VkAttachmentDescription depthAttachment{};
        depthAttachment.format = VK_FORMAT_D32_SFLOAT;
        depthAttachment.samples = VK_SAMPLE_COUNT_1_BIT;
        depthAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        depthAttachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        depthAttachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        depthAttachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        depthAttachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        depthAttachment.finalLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL;

        VkAttachmentReference depthRef{};
        depthRef.attachment = 0;
        depthRef.layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;

        VkSubpassDescription subpass{};
        subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
        subpass.colorAttachmentCount = 0;
        subpass.pDepthStencilAttachment = &depthRef;

        const VkSubpassDependency dependency = vkrender::MakePipelineCompatibleSubpassDependency();

        VkRenderPassCreateInfo rpInfo{};
        rpInfo.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
        rpInfo.attachmentCount = 1;
        rpInfo.pAttachments = &depthAttachment;
        rpInfo.subpassCount = 1;
        rpInfo.pSubpasses = &subpass;
        rpInfo.dependencyCount = 1;
        rpInfo.pDependencies = &dependency;

        if (vkCreateRenderPass(device, &rpInfo, nullptr, &m_shadowCompatRenderPass) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create shadow-compatible render pass");
            return false;
        }
    }

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
    // set 2 = vertex material UBO (binding 14, when needed).
    if (m_shadowGlobalsDescSetLayout == VK_NULL_HANDLE) {
        INXLOG_ERROR("EnsureShadowPipeline: camera-local shadow stream layout is null");
        return false;
    }
    if (m_shadowPipelineLayout == VK_NULL_HANDLE) {
        VkPushConstantRange pushRange{};
        pushRange.stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
        pushRange.offset = 0;
        pushRange.size = sizeof(glm::mat4) * 2; // model + normalMat

        VkDescriptorSetLayout setLayouts[3] = {m_shadowDescSetLayout, m_shadowGlobalsDescSetLayout,
                                               m_shadowMaterialDescSetLayout};

        VkPipelineLayoutCreateInfo pipelineLayoutInfo{};
        pipelineLayoutInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
        pipelineLayoutInfo.setLayoutCount = 3;
        pipelineLayoutInfo.pSetLayouts = setLayouts;
        pipelineLayoutInfo.pushConstantRangeCount = 1;
        pipelineLayoutInfo.pPushConstantRanges = &pushRange;

        if (vkCreatePipelineLayout(device, &pipelineLayoutInfo, nullptr, &m_shadowPipelineLayout) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create shadow pipeline layout");
            return false;
        }
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

    if (m_shadowPipelineLayout != VK_NULL_HANDLE) {
        vkDestroyPipelineLayout(device, m_shadowPipelineLayout, nullptr);
        m_shadowPipelineLayout = VK_NULL_HANDLE;
    }
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
    if (m_shadowDescSetLayout != VK_NULL_HANDLE) {
        vkDestroyDescriptorSetLayout(device, m_shadowDescSetLayout, nullptr);
        m_shadowDescSetLayout = VK_NULL_HANDLE;
    }
    if (m_shadowGlobalsDescSetLayout != VK_NULL_HANDLE) {
        vkDestroyDescriptorSetLayout(device, m_shadowGlobalsDescSetLayout, nullptr);
        m_shadowGlobalsDescSetLayout = VK_NULL_HANDLE;
    }
    (void)descriptorManager.Collect((std::numeric_limits<rhi::SubmissionSerial>::max)());
    if (m_shadowMaterialDescSetLayout != VK_NULL_HANDLE) {
        vkDestroyDescriptorSetLayout(device, m_shadowMaterialDescSetLayout, nullptr);
        m_shadowMaterialDescSetLayout = VK_NULL_HANDLE;
    }
    if (m_shadowDepthSampler != VK_NULL_HANDLE) {
        vkDestroySampler(device, m_shadowDepthSampler, nullptr);
        m_shadowDepthSampler = VK_NULL_HANDLE;
    }
    if (m_shadowCompatRenderPass != VK_NULL_HANDLE) {
        vkDestroyRenderPass(device, m_shadowCompatRenderPass, nullptr);
        m_shadowCompatRenderPass = VK_NULL_HANDLE;
    }
    // Destroy cached shadow pipelines
    for (auto &[key, pipeline] : m_shadowPipelineCache) {
        if (pipeline != VK_NULL_HANDLE)
            vkDestroyPipeline(device, pipeline, nullptr);
    }
    m_shadowPipelineCache.clear();
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
        if (objectIt->second.ensuredOnFrame == m_ensureFrameCounter) {
            return;
        }
    }
    if (objectIt != m_perObjectBuffers.end() && !forceUpdate) {
        // Fast path: if data pointers AND sizes match, content hasn't changed
        if (objectIt->second.lastVertexPtr == vertices.data() && objectIt->second.lastIndexPtr == indices.data() &&
            objectIt->second.vertexCount == vertices.size() && objectIt->second.indexCount == indices.size()) {
            objectIt->second.ensuredOnFrame = m_ensureFrameCounter;
            return;
        }
    }

    // Slow path: compute content hash for deduplication
    const size_t vtxBytes = vertices.size() * sizeof(Vertex);
    const size_t idxBytes = indices.size() * sizeof(uint32_t);
    const size_t contentHash = HashMeshContent(vertices.data(), vtxBytes, indices.data(), idxBytes);
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
            // The draw call's CPU arrays may already describe different geometry
            // than the object's old buffers. Hide the object until both uploads
            // can be published together instead of issuing unsafe mixed-version draws.
            m_perObjectBuffers.erase(objectId);
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
    for (auto it = m_perObjectBuffers.begin(); it != m_perObjectBuffers.end();) {
        if (activeIds.find(it->first) == activeIds.end()) {
            auto shared = m_sharedMeshBuffers.find(it->second.sharedKey);
            if (shared != m_sharedMeshBuffers.end())
                shared->second.lastUsedFrame = m_ensureFrameCounter;
            it = m_perObjectBuffers.erase(it);
        } else {
            ++it;
        }
    }
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
            entry->second.indexBuffer.use_count() != 1)
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
            entry->second.indexBuffer.use_count() != 1)
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
                entry->second.vertexBuffer.use_count() != 1 || entry->second.indexBuffer.use_count() != 1)
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
