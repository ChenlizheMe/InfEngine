#include "ScriptableRenderContext.h"
#include "CommandBuffer.h"
#include "EditorGizmos.h"
#include "EditorTools.h"
#include "GizmosDrawCallBuffer.h"
#include "InxVkCoreModular.h"
#include "SceneRenderGraph.h"
#include "TransientResourcePool.h"
#include "vk/RhiVulkanTypes.h"
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/scene/Scene.h>
#include <function/scene/SceneRenderBridge.h>

#include <core/log/InxLog.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <stdexcept>

namespace infernux
{

#if INFERNUX_FRAME_PROFILE
namespace
{
ScriptableRenderContext::ProfileSnapshot g_srcProfileSnapshot;
}

ScriptableRenderContext::ProfileSnapshot ScriptableRenderContext::GetProfileSnapshot()
{
    return g_srcProfileSnapshot;
}

void ScriptableRenderContext::ResetProfileSnapshot()
{
    g_srcProfileSnapshot = {};
}
#endif

// ============================================================================
// Construction
// ============================================================================

ScriptableRenderContext::ScriptableRenderContext(InxVkCoreModular *vkCore, SceneRenderGraph *graph,
                                                 const EditorGizmosContext &gizmoCtx)
    : m_vkCore(vkCore), m_graph(graph), m_gizmoCtx(gizmoCtx)
{
    // Resolve the scene pointer: prefer the gizmo context's activeScene,
    // fall back to SceneManager's active scene.
    m_scene = gizmoCtx.activeScene;
    if (!m_scene) {
        m_scene = SceneManager::Instance().GetActiveScene();
    }
}

// ============================================================================
// SetupCameraProperties
// ============================================================================

void ScriptableRenderContext::SetupCameraProperties(Camera *camera)
{
    m_activeCamera = camera;

    // Snapshot the camera's VP matrices NOW so that the executor lambda
    // uses the exact same values even if the camera transform is modified
    // later in the frame.  The cached matrices are propagated to the
    // associated SceneRenderGraph in SubmitCulling().
    if (camera) {
        m_cachedView = camera->GetViewMatrix();
        m_cachedProj = camera->GetProjectionMatrix();

        // Propagate Camera clear flags / background color to the render graph
        // so the MainColor pass uses the correct clear behaviour this frame.
        if (m_graph) {
            m_graph->UpdateMainPassClearSettings(camera->GetClearFlags(), camera->GetBackgroundColor(),
                                                 camera->GetDithering(), camera->GetStopNaNs());
        }
    }
}

// ============================================================================
// Cull
// ============================================================================

CullingResults &ScriptableRenderContext::Cull(Camera *camera)
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    const auto cullStart = Clock::now();
#endif
    if (m_hasCullData) {
        // Multiple Cull() calls in one frame: return cached results with a
        // warning rather than crashing.  Multi-camera rendering within a
        // single context is not yet supported; create a new SRC per camera.
        INXLOG_WARN("ScriptableRenderContext::Cull() called more than once — "
                    "returning cached results. Create a new context per camera "
                    "for multi-camera rendering.");
        return m_cachedCullingResults;
    }

    SceneRenderBridge &bridge = SceneRenderBridge::Instance();
    Camera *editorCam = bridge.GetEditorCamera();

    const bool needsShadowDrawCalls = m_graph && m_graph->HasCameraShadows();
    CameraDrawCallResult ownedResult;
    CullingResults results;

    // RenderWorld extraction is camera-neutral. Scene, Game, preview, and
    // future stacked cameras all derive an independent visible list here.
    ownedResult = bridge.CullAndBuildForCamera(camera, needsShadowDrawCalls);
    const std::vector<DrawCall> *drawCallsPtr =
        ownedResult.visibleDrawCallsRef ? ownedResult.visibleDrawCallsRef : &ownedResult.visibleDrawCalls;
    results.visibleListRevision = ownedResult.visibleListRevision;
    results.shadowListRevision = ownedResult.shadowListRevision;
    results.renderWorldOwner = ownedResult.worldOwner;

    m_hasCullData = true;

    if (ownedResult.visibleDrawCallsRef) {
        results.visibleRenderers =
            RendererList::Borrow(*ownedResult.visibleDrawCallsRef, RendererListPurpose::CameraVisible,
                                 RenderDomainBit(RenderDomain::SceneGeometry));
    } else {
        results.visibleRenderers =
            RendererList::Own(std::move(ownedResult.visibleDrawCalls), RendererListPurpose::CameraVisible,
                              RenderDomainBit(RenderDomain::SceneGeometry));
    }
    if (needsShadowDrawCalls) {
        if (ownedResult.shadowDrawCallsRef) {
            results.shadowCasters =
                RendererList::Borrow(*ownedResult.shadowDrawCallsRef, RendererListPurpose::ShadowCasters,
                                     RenderDomainBit(RenderDomain::SceneGeometry));
        } else {
            results.shadowCasters =
                RendererList::Own(std::move(ownedResult.shadowDrawCalls), RendererListPurpose::ShadowCasters,
                                  RenderDomainBit(RenderDomain::SceneGeometry));
        }
    }
    // Populate visible light count from the scene light collector.
    // CollectLights() runs earlier in the frame (InxRenderer::UpdateSceneLighting),
    // so the count is already available.
    results.lightCount = m_graph ? m_graph->GetCameraLightCount() : 0;
    m_cachedCullingResults = std::move(results);
#if INFERNUX_FRAME_PROFILE
    const double elapsedMs = std::chrono::duration<double, std::milli>(Clock::now() - cullStart).count();
    g_srcProfileSnapshot.cullMs += elapsedMs;
    if (camera && camera != editorCam) {
        g_srcProfileSnapshot.cullGameMs += elapsedMs;
        g_srcProfileSnapshot.cullGameCalls += 1.0;
    } else {
        g_srcProfileSnapshot.cullEditorMs += elapsedMs;
        g_srcProfileSnapshot.cullEditorCalls += 1.0;
    }
    g_srcProfileSnapshot.cullCalls += 1.0;
    g_srcProfileSnapshot.baseDrawCalls += static_cast<double>(results.visibleObjectCount());
#endif
    return m_cachedCullingResults;
}

// ============================================================================
// RenderGraph-driven API
// ============================================================================

void ScriptableRenderContext::ApplyGraph(const RenderGraphDescription &desc)
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    const auto t0 = Clock::now();
#endif
    if (m_graph) {
        m_graph->ApplyPythonGraph(desc);
    } else {
        INXLOG_WARN("ScriptableRenderContext::ApplyGraph: No SceneRenderGraph available");
    }
#if INFERNUX_FRAME_PROFILE
    g_srcProfileSnapshot.applyGraphMs += std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
}

void ScriptableRenderContext::UpdateParameterBlocks(const std::vector<GraphParameterBlockUpdate> &updates)
{
    if (m_graph && !updates.empty())
        m_graph->UpdateParameterBlocks(updates);
}

void ScriptableRenderContext::SubmitCulling(CullingResults &culling)
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    const auto submitStart = Clock::now();
    const size_t baseDrawCount = culling.visibleRenderers.Size();
    const bool baseRendererListBorrowed = culling.visibleRenderers.IsBorrowed();
#endif
    if (m_submitted) {
        INXLOG_WARN("ScriptableRenderContext::SubmitCulling() called after already submitted");
        return;
    }

    // A game camera with an unchanged borrowed visible set can reuse the
    // RenderGraph's complete submitted list (including its skybox) without
    // copying and then destroying tens of thousands of DrawCalls every frame.
    // Editor-only appenders deliberately stay on the normal path.
    const bool hasEditorAppenders = m_gizmoCtx.gizmos || m_gizmoCtx.editorTools ||
                                    (m_gizmoCtx.componentGizmos && (m_gizmoCtx.componentGizmos->HasData() ||
                                                                    m_gizmoCtx.componentGizmos->HasIconData()));
    uint64_t submissionSignature = 1469598103934665603ULL;
    auto mixSubmission = [&](uint64_t value) {
        submissionSignature ^= value;
        submissionSignature *= 1099511628211ULL;
    };
    // Camera culling publishes a monotonic content revision. RenderWorld
    // publications rotate between recyclable frame objects, so pointer values
    // cannot represent draw-list identity.
    mixSubmission(culling.visibleListRevision);
    // RenderWorld publications rotate between recyclable frame objects. The
    // shadow vector address therefore changes even when its contents do not.
    // Use the camera cache revision as the durable identity instead of that
    // transient address, otherwise a static scene can never reuse submission.
    mixSubmission(culling.shadowListRevision);
    const bool drawSkyboxForSignature = !m_activeCamera || m_activeCamera->GetClearFlags() == CameraClearFlags::Skybox;
    mixSubmission(drawSkyboxForSignature ? 1ULL : 0ULL);
    std::shared_ptr<InxMaterial> signatureSkyboxMaterial;
    if (drawSkyboxForSignature) {
        if (m_gizmoCtx.activeScene)
            signatureSkyboxMaterial = m_gizmoCtx.activeScene->ResolveSkyboxMaterial();
        if (!signatureSkyboxMaterial)
            signatureSkyboxMaterial = AssetRegistry::Instance().GetBuiltinMaterial("SkyboxProcedural");
        mixSubmission(static_cast<uint64_t>(reinterpret_cast<uintptr_t>(signatureSkyboxMaterial.get())));
    }
    const bool shadowListReusable = culling.shadowCasters.Empty() || culling.shadowCasters.IsBorrowed();
    const bool visibleListReusable = culling.visibleRenderers.IsBorrowed();
    const bool graphAvailable = m_graph != nullptr;
    const bool uploadsSettled = m_vkCore->GetPendingMeshUploadCount() == 0;
    const uint64_t objectBufferRevision = m_vkCore->GetObjectBufferRevision();
    const bool signatureReusable =
        graphAvailable && m_graph->CanReuseCachedSubmission(submissionSignature, objectBufferRevision);
    if (!hasEditorAppenders && visibleListReusable && shadowListReusable && graphAvailable && uploadsSettled &&
        signatureReusable) {
        m_graph->SetCachedSubmissionSignature(submissionSignature, culling.renderWorldOwner, objectBufferRevision);
        if (m_activeCamera)
            m_graph->SetCachedCameraVP(m_activeCamera, m_cachedView, m_cachedProj);
        m_vkCore->SetDrawCalls(&m_graph->GetCachedDrawCalls());
        m_vkCore->SetShadowDrawCalls(m_graph->HasCachedShadowDrawCalls() ? &m_graph->GetCachedShadowDrawCalls()
                                                                         : nullptr);
        m_vkCore->ReuseObjectBufferBindingsThisFrame();
        if (m_transientPool)
            m_transientPool->EndFrame();
        m_submitted = true;
#if INFERNUX_FRAME_PROFILE
        g_srcProfileSnapshot.cachedSubmissionReuses += 1.0;
        g_srcProfileSnapshot.submitMs += std::chrono::duration<double, std::milli>(Clock::now() - submitStart).count();
        g_srcProfileSnapshot.submitCalls += 1.0;
        g_srcProfileSnapshot.finalDrawCalls += static_cast<double>(m_graph->GetCachedDrawCalls().size());
#endif
        return;
    }
#if INFERNUX_FRAME_PROFILE
    g_srcProfileSnapshot.submissionRejectEditorAppenders += hasEditorAppenders ? 1.0 : 0.0;
    g_srcProfileSnapshot.submissionRejectOwnedVisibleList += visibleListReusable ? 0.0 : 1.0;
    g_srcProfileSnapshot.submissionRejectOwnedShadowList += shadowListReusable ? 0.0 : 1.0;
    g_srcProfileSnapshot.submissionRejectMissingGraph += graphAvailable ? 0.0 : 1.0;
    g_srcProfileSnapshot.submissionRejectPendingUploads += uploadsSettled ? 0.0 : 1.0;
    g_srcProfileSnapshot.submissionRejectSignature += signatureReusable ? 0.0 : 1.0;
    if (baseRendererListBorrowed) {
        g_srcProfileSnapshot.borrowedRendererListSubmits += 1.0;
        g_srcProfileSnapshot.materializedDrawCalls += static_cast<double>(baseDrawCount);
    } else {
        g_srcProfileSnapshot.ownedRendererListSubmits += 1.0;
    }
#endif

    // Move or reference draw calls — avoids 1000+ shared_ptr atomic refcount ops.
#if INFERNUX_FRAME_PROFILE
    auto t0 = Clock::now();
#endif
    RenderDomainMask submittedDomains = culling.visibleRenderers.Domains();
    m_orderedDrawCalls = culling.visibleRenderers.Consume();
    const size_t baseOrderedDrawCallCount = m_orderedDrawCalls.size();
    m_orderedDrawCalls.reserve(m_orderedDrawCalls.size() + 16);
#if INFERNUX_FRAME_PROFILE
    g_srcProfileSnapshot.submitBaseMs += std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif

    // Append skybox draw call only when ClearFlags == Skybox (or no camera set)
    bool drawSkybox = true;
    if (m_activeCamera) {
        CameraClearFlags flags = m_activeCamera->GetClearFlags();
        drawSkybox = (flags == CameraClearFlags::Skybox);
    }

    if (drawSkybox) {
#if INFERNUX_FRAME_PROFILE
        t0 = Clock::now();
#endif
        // Scene environment picks the skybox material (falls back to the
        // builtin procedural sky when unset or unloadable).
        std::shared_ptr<InxMaterial> skyboxMat;
        if (m_gizmoCtx.activeScene)
            skyboxMat = m_gizmoCtx.activeScene->ResolveSkyboxMaterial();
        if (!skyboxMat)
            skyboxMat = AssetRegistry::Instance().GetBuiltinMaterial("SkyboxProcedural");
        if (skyboxMat) {
            static constexpr uint64_t SKYBOX_OBJECT_ID = 0xFFFFFFFFFFFFFF00ULL;
            DrawCall dc;
            dc.indexStart = 0;
            dc.indexCount = static_cast<uint32_t>(PrimitiveMeshes::GetSkyboxCubeIndices().size());
            dc.worldMatrix = glm::mat4(1.0f);
            dc.material = skyboxMat;
            dc.objectId = SKYBOX_OBJECT_ID;
            dc.identity = RenderProxyHandle::Synthetic(RenderDomain::Skybox, dc.objectId).MakeDrawIdentity();
            dc.isStatic = true;
            dc.meshVertices = &PrimitiveMeshes::GetSkyboxCubeVertices();
            dc.meshIndices = &PrimitiveMeshes::GetSkyboxCubeIndices();
            m_orderedDrawCalls.push_back(dc);
            submittedDomains |= RenderDomainBit(RenderDomain::Skybox);
        }
#if INFERNUX_FRAME_PROFILE
        g_srcProfileSnapshot.submitEditorAppendMs +=
            std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
    }

    // Auto-append editor gizmos
    if (m_gizmoCtx.gizmos) {
#if INFERNUX_FRAME_PROFILE
        t0 = Clock::now();
#endif
        DrawCallResult gizmoResult =
            m_gizmoCtx.gizmos->GetDrawCalls(m_gizmoCtx.gizmoMaterial, m_gizmoCtx.gridMaterial,
                                            m_gizmoCtx.selectedObjectId, m_gizmoCtx.activeScene, m_gizmoCtx.cameraPos);
        if (!gizmoResult.drawCalls.empty())
            submittedDomains |= RenderDomainBit(RenderDomain::EditorGizmo);
        for (auto &dc : gizmoResult.drawCalls) {
            m_orderedDrawCalls.push_back(dc);
        }
#if INFERNUX_FRAME_PROFILE
        g_srcProfileSnapshot.submitEditorAppendMs +=
            std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
    }

    // Auto-append component gizmos (script-side, depth-tested)
    if (m_gizmoCtx.componentGizmos && m_gizmoCtx.componentGizmos->HasData()) {
#if INFERNUX_FRAME_PROFILE
        t0 = Clock::now();
#endif
        DrawCallResult compGizmoResult = m_gizmoCtx.componentGizmos->GetDrawCalls(m_gizmoCtx.componentGizmosMaterial);
        if (!compGizmoResult.drawCalls.empty())
            submittedDomains |= RenderDomainBit(RenderDomain::ComponentGizmo);
        for (auto &dc : compGizmoResult.drawCalls) {
            m_orderedDrawCalls.push_back(dc);
        }
#if INFERNUX_FRAME_PROFILE
        g_srcProfileSnapshot.submitEditorAppendMs +=
            std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
    }

    // Auto-append component gizmo icons (script-side, billboard diamonds)
    if (m_gizmoCtx.componentGizmos && m_gizmoCtx.componentGizmos->HasIconData()) {
#if INFERNUX_FRAME_PROFILE
        t0 = Clock::now();
#endif
        glm::vec3 cameraRight(1.0f, 0.0f, 0.0f);
        glm::vec3 cameraUp(0.0f, 1.0f, 0.0f);
        if (m_activeCamera && m_activeCamera->GetGameObject() && m_activeCamera->GetGameObject()->GetTransform()) {
            Transform *cameraTransform = m_activeCamera->GetGameObject()->GetTransform();
            cameraRight = cameraTransform->GetWorldRight();
            cameraUp = cameraTransform->GetWorldUp();
        }
        const GizmosDrawCallBuffer::IconMaterials iconMaterials{
            m_gizmoCtx.componentGizmoIconMaterial, m_gizmoCtx.cameraGizmoIconMaterial,
            m_gizmoCtx.lightGizmoIconMaterial, m_gizmoCtx.particleGizmoIconMaterial};
        DrawCallResult iconResult =
            m_gizmoCtx.componentGizmos->GetIconDrawCalls(iconMaterials, m_gizmoCtx.cameraPos, cameraRight, cameraUp);
        if (!iconResult.drawCalls.empty())
            submittedDomains |= RenderDomainBit(RenderDomain::ComponentGizmo);
        static size_t s_lastSubmittedIconDrawCalls = static_cast<size_t>(-1);
        if (s_lastSubmittedIconDrawCalls != iconResult.drawCalls.size()) {
            s_lastSubmittedIconDrawCalls = iconResult.drawCalls.size();
        }
        for (auto &dc : iconResult.drawCalls) {
            m_orderedDrawCalls.push_back(dc);
        }
#if INFERNUX_FRAME_PROFILE
        g_srcProfileSnapshot.submitEditorAppendMs +=
            std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
    }

    // Auto-append editor tools (translate/rotate/scale handles)
    if (m_gizmoCtx.editorTools) {
#if INFERNUX_FRAME_PROFILE
        t0 = Clock::now();
#endif
        DrawCallResult toolsResult = m_gizmoCtx.editorTools->GetDrawCalls(
            m_gizmoCtx.editorToolsMaterial, m_gizmoCtx.selectedObjectId, m_gizmoCtx.activeScene, m_gizmoCtx.cameraPos);
        if (!toolsResult.drawCalls.empty())
            submittedDomains |= RenderDomainBit(RenderDomain::EditorTool);
        for (auto &dc : toolsResult.drawCalls) {
            m_orderedDrawCalls.push_back(dc);
        }
#if INFERNUX_FRAME_PROFILE
        g_srcProfileSnapshot.submitEditorAppendMs +=
            std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
    }

    const std::vector<DrawCall> *shadowSource =
        culling.shadowCasters.Empty() ? nullptr : &culling.shadowCasters.DrawCalls();

    // Compute a lightweight resource-identity fingerprint. World matrices and
    // visibility intentionally do not participate: neither changes mesh GPU
    // buffers. The fingerprint still detects a same-sized but different
    // visible set after camera movement.
    uint64_t bufferIdentity = 1469598103934665603ULL;
    bool hasForcedBufferUpdate = false;
    auto accumulateBufferIdentity = [&](const DrawCall &dc) {
        auto mix = [&](uint64_t value) {
            bufferIdentity ^= value;
            bufferIdentity *= 1099511628211ULL;
        };
        mix(dc.objectId);
        mix(static_cast<uint64_t>(reinterpret_cast<uintptr_t>(dc.meshVertices)));
        mix(static_cast<uint64_t>(reinterpret_cast<uintptr_t>(dc.meshIndices)));
        mix(dc.meshRuntimeVersion);
        hasForcedBufferUpdate = hasForcedBufferUpdate || dc.forceBufferUpdate;
    };
    if (shadowSource) {
        for (const DrawCall &dc : *shadowSource)
            accumulateBufferIdentity(dc);
    }
    for (const DrawCall &dc : m_orderedDrawCalls)
        accumulateBufferIdentity(dc);

    const size_t bufferIdentityDrawCallCount = m_orderedDrawCalls.size() + (shadowSource ? shadowSource->size() : 0);
    const bool reuseObjectBuffers =
        !hasForcedBufferUpdate && m_vkCore->CanReuseObjectBufferBindings(bufferIdentity, bufferIdentityDrawCallCount);

    // Ensure per-object GPU buffers
#if INFERNUX_FRAME_PROFILE
    t0 = Clock::now();
#endif
    if (reuseObjectBuffers) {
        m_vkCore->ReuseObjectBufferBindingsThisFrame();
    } else if (shadowSource) {
        // Consecutive-objectId dedup: draw calls for multi-submesh objects
        // share the same objectId and are adjacent in the array.  Skip
        // redundant hash-map lookups inside EnsureObjectBuffers.
        uint64_t lastEnsuredId = 0;
        for (const DrawCall &dc : *shadowSource) {
            if (dc.objectId == lastEnsuredId)
                continue;
            lastEnsuredId = dc.objectId;
            if (dc.meshVertices && dc.meshIndices) {
                m_vkCore->EnsureObjectBuffers(dc.objectId, *dc.meshVertices, *dc.meshIndices, dc.forceBufferUpdate,
                                              dc.meshAssetGuid, dc.meshRuntimeVersion);
            }
        }

        for (size_t drawCallIndex = baseOrderedDrawCallCount; drawCallIndex < m_orderedDrawCalls.size();
             ++drawCallIndex) {
            const DrawCall &dc = m_orderedDrawCalls[drawCallIndex];
            if (dc.objectId == lastEnsuredId)
                continue;
            lastEnsuredId = dc.objectId;
            if (dc.meshVertices && dc.meshIndices) {
                m_vkCore->EnsureObjectBuffers(dc.objectId, *dc.meshVertices, *dc.meshIndices, dc.forceBufferUpdate,
                                              dc.meshAssetGuid, dc.meshRuntimeVersion);
            }
        }
    }

    // Build the forward-render list.
    // Game camera path already has a compact visible-only list (move it).
    // Editor camera path: skip visibility pre-filter — DrawSceneFiltered
    // already checks frustumVisible per draw call, so pre-filtering is
    // redundant memcpy of ~2.7k DrawCalls.
    std::vector<DrawCall> forwardDrawCalls = std::move(m_orderedDrawCalls);

    if (!shadowSource) {
        if (!reuseObjectBuffers) {
            for (const DrawCall &dc : forwardDrawCalls) {
                if (dc.meshVertices && dc.meshIndices) {
                    m_vkCore->EnsureObjectBuffers(dc.objectId, *dc.meshVertices, *dc.meshIndices, dc.forceBufferUpdate,
                                                  dc.meshAssetGuid, dc.meshRuntimeVersion);
                }
            }
        }
    }

    if (!reuseObjectBuffers)
        m_vkCore->PrimeObjectBufferBindingCache(bufferIdentity, bufferIdentityDrawCallCount);

    DrawCallResult result;
    result.drawCalls = std::move(forwardDrawCalls);
#if INFERNUX_FRAME_PROFILE
    g_srcProfileSnapshot.ensureBuffersMs += std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif

    // Cache draw calls AND camera VP on the associated render graph.
    // Each SceneRenderGraph stores its own draw-call set and VP matrices
    // so the executor lambda can swap them before each graph execution,
    // ensuring full isolation between Scene View and Game View rendering.
    if (m_graph) {
#if INFERNUX_FRAME_PROFILE
        t0 = Clock::now();
#endif
        m_graph->SetCachedRendererList(
            RendererList::Own(std::move(result.drawCalls), RendererListPurpose::CameraVisible, submittedDomains));
        if (shadowSource) {
            m_graph->SetCachedShadowRendererList(std::move(culling.shadowCasters));
        } else {
            m_graph->ClearCachedShadowDrawCalls();
        }
        m_graph->SetCachedSubmissionSignature(submissionSignature, culling.renderWorldOwner,
                                              m_vkCore->GetObjectBufferRevision());
        if (m_activeCamera) {
            m_graph->SetCachedCameraVP(m_activeCamera, m_cachedView, m_cachedProj);
        }
        // Point VkCore at the graph's cached copy (survives this scope).
        m_vkCore->SetDrawCalls(&m_graph->GetCachedDrawCalls(), true);
        m_vkCore->SetShadowDrawCalls(
            m_graph->HasCachedShadowDrawCalls() ? &m_graph->GetCachedShadowDrawCalls() : nullptr, true);
#if INFERNUX_FRAME_PROFILE
        g_srcProfileSnapshot.cacheGraphMs += std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
    }

    // NOTE: CleanupUnusedBuffers is called by InxRenderer::DrawFrame() after
    // all pipeline renders, using the union of all graphs' draw calls.
    // This prevents one graph's cleanup from removing buffers another graph needs.

    // Release transient resources
    if (m_transientPool) {
        m_transientPool->EndFrame();
    }

    m_submitted = true;
#if INFERNUX_FRAME_PROFILE
    g_srcProfileSnapshot.submitMs += std::chrono::duration<double, std::milli>(Clock::now() - submitStart).count();
    g_srcProfileSnapshot.submitCalls += 1.0;
    g_srcProfileSnapshot.finalDrawCalls +=
        static_cast<double>(m_graph ? m_graph->GetCachedDrawCalls().size() : baseDrawCount);
#endif
}

void ScriptableRenderContext::RenderWithGraph(Camera *camera, const RenderGraphDescription &desc)
{
    SetupCameraProperties(camera);
    CullingResults &culling = Cull(camera);
    ApplyGraph(desc);
    SubmitCulling(culling);
}

bool ScriptableRenderContext::IsGraphRevisionCurrent(uint64_t sourceRevision) const
{
    return m_graph && m_graph->IsPythonGraphCurrent(sourceRevision);
}

bool ScriptableRenderContext::RenderCompiled(Camera *camera, uint64_t sourceRevision)
{
    if (!IsGraphRevisionCurrent(sourceRevision))
        return false;
    SetupCameraProperties(camera);
    CullingResults &culling = Cull(camera);
    SubmitCulling(culling);
    return true;
}

// ============================================================================
// CommandBuffer integration
// ============================================================================

void ScriptableRenderContext::ExecuteCommandBuffer(CommandBuffer &cmd)
{
    // Accumulate pending CommandBuffers; they are processed during Submit()
    m_pendingCommandBuffers.push_back(&cmd);
}

// Names for the unsupported command types so the once-per-process warning is
// readable. Keep in lockstep with RenderCommandType (CommandBuffer.h).
namespace
{
const char *RenderCommandTypeName(RenderCommandType type)
{
    switch (type) {
    case RenderCommandType::GetTemporaryRT:
        return "GetTemporaryRT";
    case RenderCommandType::ReleaseTemporaryRT:
        return "ReleaseTemporaryRT";
    case RenderCommandType::SetRenderTarget:
        return "SetRenderTarget";
    case RenderCommandType::ClearRenderTarget:
        return "ClearRenderTarget";
    case RenderCommandType::DrawMesh:
        return "DrawMesh";
    case RenderCommandType::SetGlobalTexture:
        return "SetGlobalTexture";
    case RenderCommandType::SetGlobalFloat:
        return "SetGlobalFloat";
    case RenderCommandType::SetGlobalVector:
        return "SetGlobalVector";
    case RenderCommandType::SetGlobalMatrix:
        return "SetGlobalMatrix";
    }
    return "Unknown";
}

void WarnUnimplementedCommand(RenderCommandType type)
{
    // Per-type latch so each unsupported command logs exactly once per process,
    // instead of either spamming or silently swallowing after a global cap.
    constexpr size_t kCommandCount = 16; // bounded by RenderCommandType (uint8_t enum); plenty of slack
    static std::array<std::atomic<bool>, kCommandCount> warned{};
    const auto idx = static_cast<size_t>(type);
    if (idx >= warned.size())
        return;
    bool expected = false;
    if (warned[idx].compare_exchange_strong(expected, true)) {
        INXLOG_WARN("[SRP] CommandBuffer command '", RenderCommandTypeName(type),
                    "' is not yet implemented in the Vulkan backend — ignoring all "
                    "subsequent invocations of this command type for the rest of the process. "
                    "Subsequent rendering may behave unexpectedly until the backend lands.");
    }
}
} // namespace

void ScriptableRenderContext::ProcessPendingCommandBuffers()
{
    for (CommandBuffer *cmd : m_pendingCommandBuffers) {
        if (!cmd)
            continue;

        for (const auto &command : cmd->GetCommands()) {
            switch (command.type) {
            case RenderCommandType::GetTemporaryRT: {
                if (m_transientPool) {
                    const auto &params = std::get<GetTemporaryRTParams>(command.data);
                    uint32_t slotId =
                        m_transientPool->Acquire(params.width, params.height, rhi::ToVkFormat(params.format),
                                                 rhi::ToVkSampleCount(params.samples));
                    m_handleToSlotMap[params.handleId] = slotId;
                }
                break;
            }

            case RenderCommandType::ReleaseTemporaryRT: {
                if (m_transientPool) {
                    const auto &params = std::get<ReleaseTemporaryRTParams>(command.data);
                    auto it = m_handleToSlotMap.find(params.handleId);
                    if (it != m_handleToSlotMap.end()) {
                        m_transientPool->Release(it->second);
                        m_handleToSlotMap.erase(it);
                    }
                }
                break;
            }

            case RenderCommandType::SetGlobalFloat: {
                const auto &params = std::get<SetGlobalFloatParams>(command.data);
                m_globalFloats[params.name] = params.value;
                break;
            }

            case RenderCommandType::SetGlobalVector: {
                const auto &params = std::get<SetGlobalVectorParams>(command.data);
                m_globalVectors[params.name] = {params.x, params.y, params.z, params.w};
                break;
            }

            case RenderCommandType::SetGlobalTexture: {
                const auto &params = std::get<SetGlobalTextureParams>(command.data);
                m_globalTextures[params.name] = params.handleId;
                break;
            }

            // Commands that still need the Vulkan command-buffer integration.
            // See ScriptableRenderContext::IsCommandImplemented for the
            // canonical "is this safe to call" predicate exposed to bindings.
            case RenderCommandType::ClearRenderTarget:
            case RenderCommandType::SetRenderTarget:
            case RenderCommandType::DrawMesh:
            case RenderCommandType::SetGlobalMatrix:
                WarnUnimplementedCommand(command.type);
                break;
            }
        }
    }
    m_pendingCommandBuffers.clear();
}

bool ScriptableRenderContext::IsCommandImplemented(RenderCommandType type) noexcept
{
    switch (type) {
    case RenderCommandType::GetTemporaryRT:
    case RenderCommandType::ReleaseTemporaryRT:
    case RenderCommandType::SetGlobalFloat:
    case RenderCommandType::SetGlobalVector:
    case RenderCommandType::SetGlobalTexture:
        return true;
    case RenderCommandType::ClearRenderTarget:
    case RenderCommandType::SetRenderTarget:
    case RenderCommandType::DrawMesh:
    case RenderCommandType::SetGlobalMatrix:
        return false;
    }
    return false;
}

// ============================================================================
// Camera target
// ============================================================================

RenderTargetHandle ScriptableRenderContext::GetCameraTarget(Camera * /*camera*/) const
{
    // Returns the sentinel CAMERA_TARGET_HANDLE.
    // At execution time, this resolves to the scene render target's resolved color image.
    return CAMERA_TARGET_HANDLE;
}

// ============================================================================
// Global shader parameters (immediate mode)
// ============================================================================

void ScriptableRenderContext::SetGlobalTexture(const std::string &name, RenderTargetHandle handle)
{
    m_globalTextures[name] = handle.id;
}

void ScriptableRenderContext::SetGlobalFloat(const std::string &name, float value)
{
    m_globalFloats[name] = value;
}

void ScriptableRenderContext::SetGlobalVector(const std::string &name, float x, float y, float z, float w)
{
    m_globalVectors[name] = {x, y, z, w};
}

} // namespace infernux
