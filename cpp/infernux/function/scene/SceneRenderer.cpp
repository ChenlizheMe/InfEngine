#include "SceneRenderer.h"
#include "SceneManager.h"
#include "SkinnedMeshRenderer.h"
#include <algorithm>
#include <chrono>
#include <cstring>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <glm/gtc/matrix_transform.hpp>
#include <memory>

namespace infernux
{

// ============================================================================
// SceneRenderer Implementation
// ============================================================================

void SceneRenderer::PrepareFrame(bool useActiveCameraCulling)
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    const auto prepareStart = Clock::now();
#endif
    SceneManager &sm = SceneManager::Instance();
    m_activeCamera = sm.GetEditorCameraController().GetCamera();

    const uint64_t currentVersion = sm.GetMeshRendererVersion();
    const bool fastPath = (currentVersion == m_cachedMeshRendererVersion && !m_renderables.empty());
    if (fastPath) {
        // Fast path: renderer set unchanged — fuse transform/bounds/culling/draw-call patch.
#if INFERNUX_FRAME_PROFILE
        const auto t0 = Clock::now();
#endif
        UpdateCachedRenderableTransforms(useActiveCameraCulling);
#if INFERNUX_FRAME_PROFILE
        m_profileSnapshot.updateMs += std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
        m_profileSnapshot.prepareFastCalls += 1.0;
#endif
    } else {
        // Slow path: full rebuild.
#if INFERNUX_FRAME_PROFILE
        const auto t0 = Clock::now();
#endif
        CollectRenderables(0xFFFFFFFF);
        m_cachedMeshRendererVersion = currentVersion;
        m_drawCallsCacheValid = m_renderables.empty();
        if (m_drawCallsCacheValid)
            m_cachedDrawCalls.drawCalls.clear();
#if INFERNUX_FRAME_PROFILE
        m_profileSnapshot.collectMs += std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
        m_profileSnapshot.prepareSlowCalls += 1.0;
#endif
    }

    if (!fastPath && useActiveCameraCulling && m_frustumCulling) {
#if INFERNUX_FRAME_PROFILE
        const auto t0 = Clock::now();
#endif
        PerformCulling();
#if INFERNUX_FRAME_PROFILE
        m_profileSnapshot.cullMs += std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
    }

    // Skip sort when cache is valid (material sort keys are stable).
    if (!m_drawCallsCacheValid) {
#if INFERNUX_FRAME_PROFILE
        const auto t0 = Clock::now();
#endif
        SortRenderables();
#if INFERNUX_FRAME_PROFILE
        m_profileSnapshot.sortMs += std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
    }

#if INFERNUX_FRAME_PROFILE
    m_profileSnapshot.prepareMs += std::chrono::duration<double, std::milli>(Clock::now() - prepareStart).count();
    m_profileSnapshot.prepareCalls += 1.0;
    m_profileSnapshot.renderables += static_cast<double>(m_renderables.size());
    m_profileSnapshot.visible += static_cast<double>(m_visibleCount);
#endif
}

void SceneRenderer::PrepareFrame(Camera *camera)
{
    if (!camera) {
        m_renderables.clear();
        m_cachedDrawCalls.drawCalls.clear();
        m_drawCallsCacheValid = true;
        m_visibleCount = 0;
        return;
    }

#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    const auto prepareStart = Clock::now();
#endif
    m_activeCamera = camera;

    SceneManager &sm = SceneManager::Instance();
    const uint64_t currentVersion = sm.GetMeshRendererVersion();
    const bool fastPath = (currentVersion == m_cachedMeshRendererVersion && !m_renderables.empty());
    if (fastPath) {
#if INFERNUX_FRAME_PROFILE
        const auto t0 = Clock::now();
#endif
        UpdateCachedRenderableTransforms(true);
#if INFERNUX_FRAME_PROFILE
        m_profileSnapshot.updateMs += std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
        m_profileSnapshot.prepareFastCalls += 1.0;
#endif
    } else {
#if INFERNUX_FRAME_PROFILE
        const auto t0 = Clock::now();
#endif
        CollectRenderables(camera->GetCullingMask());
        m_cachedMeshRendererVersion = currentVersion;
        m_drawCallsCacheValid = m_renderables.empty();
        if (m_drawCallsCacheValid)
            m_cachedDrawCalls.drawCalls.clear();
#if INFERNUX_FRAME_PROFILE
        m_profileSnapshot.collectMs += std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
        m_profileSnapshot.prepareSlowCalls += 1.0;
#endif
    }

    if (!fastPath && m_frustumCulling) {
#if INFERNUX_FRAME_PROFILE
        const auto t0 = Clock::now();
#endif
        PerformCulling();
#if INFERNUX_FRAME_PROFILE
        m_profileSnapshot.cullMs += std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
    }

    if (!m_drawCallsCacheValid) {
#if INFERNUX_FRAME_PROFILE
        const auto t0 = Clock::now();
#endif
        SortRenderables();
#if INFERNUX_FRAME_PROFILE
        m_profileSnapshot.sortMs += std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
#endif
    }

#if INFERNUX_FRAME_PROFILE
    m_profileSnapshot.prepareMs += std::chrono::duration<double, std::milli>(Clock::now() - prepareStart).count();
    m_profileSnapshot.prepareCalls += 1.0;
    m_profileSnapshot.renderables += static_cast<double>(m_renderables.size());
    m_profileSnapshot.visible += static_cast<double>(m_visibleCount);
#endif
}

glm::mat4 SceneRenderer::GetViewMatrix() const
{
    if (m_activeCamera) {
        return m_activeCamera->GetViewMatrix();
    }
    return glm::mat4{1.0f};
}

glm::mat4 SceneRenderer::GetProjectionMatrix() const
{
    if (m_activeCamera) {
        return m_activeCamera->GetProjectionMatrix();
    }
    return glm::perspective(glm::radians(60.0f), 16.0f / 9.0f, 0.1f, 1000.0f);
}

glm::vec3 SceneRenderer::GetCameraPosition() const
{
    if (m_activeCamera && m_activeCamera->GetGameObject()) {
        return m_activeCamera->GetGameObject()->GetTransform()->GetWorldPosition();
    }
    return glm::vec3{0.0f, 0.0f, 5.0f};
}

glm::vec3 SceneRenderer::GetCameraForward() const
{
    if (m_activeCamera && m_activeCamera->GetGameObject()) {
        return m_activeCamera->GetGameObject()->GetTransform()->GetWorldForward();
    }
    return glm::vec3{0.0f, 0.0f, 1.0f};
}

glm::vec3 SceneRenderer::GetCameraUp() const
{
    if (m_activeCamera && m_activeCamera->GetGameObject()) {
        return m_activeCamera->GetGameObject()->GetTransform()->GetWorldUp();
    }
    return glm::vec3{0.0f, 1.0f, 0.0f};
}

void SceneRenderer::SetAspectRatio(float aspect)
{
    if (m_activeCamera) {
        m_activeCamera->SetAspectRatio(aspect);
    }
}

void SceneRenderer::CollectRenderables(uint32_t cullingMask)
{
    m_renderables.clear();

    Scene *activeScene = SceneManager::Instance().GetActiveScene();
    if (!activeScene)
        return;

    // Use the MeshRenderer registry — O(N) over active renderers only,
    // no GetAllObjects() tree walk, no dynamic_cast, no vector allocation.
    const auto &meshRenderers = SceneManager::Instance().GetActiveMeshRenderers();

    m_renderables.reserve(meshRenderers.size());

    for (MeshRenderer *renderer : meshRenderers) {
        if (!renderer || !renderer->IsEnabled())
            continue;

        GameObject *obj = renderer->GetGameObject();
        if (!obj || !obj->IsActiveInHierarchy())
            continue;

        // Layer-based culling: skip objects not in the camera's culling mask
        uint32_t objectLayerBit = 1u << static_cast<uint32_t>(obj->GetLayer());
        if ((cullingMask & objectLayerBit) == 0)
            continue;

        // Check if renderer has inline mesh, asset mesh, or mesh reference
        if (!renderer->HasMeshAsset() && !renderer->HasInlineMesh() && !renderer->GetMesh().IsValid())
            continue;

        RenderableObject renderable;
        renderable.objectId = obj->GetID();
        renderable.renderProxy = RenderProxyHandle::FromScene(obj->GetHandle(), renderer->GetHandle());
        renderable.worldMatrix = obj->GetTransform()->GetWorldMatrix();
        renderable.mesh = renderer->GetMesh();
        renderable.renderMaterial = renderer->GetEffectiveMaterial(); // Get actual InxMaterial
        renderable.renderMaterialRaw = renderable.renderMaterial.get();
        renderable.meshRenderer = renderer; // Store direct pointer
        renderable.transform = obj->GetTransform();
        renderable.skinnedRenderer = dynamic_cast<SkinnedMeshRenderer *>(renderer);
        renderable.drawCallStart = 0;
        renderable.drawCallCount = 0;

        // Get world-space bounding box for frustum culling — reuse the world matrix
        glm::vec3 boundsMin, boundsMax;
        renderer->ComputeWorldBounds(renderable.worldMatrix, boundsMin, boundsMax);
        renderable.worldBounds = AABB(boundsMin, boundsMax);
        renderable.visible = true; // Will be set by culling

        m_renderables.push_back(std::move(renderable));
    }

    m_visibleCount = m_renderables.size();
}

void SceneRenderer::UpdateCachedRenderableTransforms(bool useActiveCameraCulling)
{
    // Fast path: renderer set unchanged. Refresh transforms, bounds, culling,
    // and cached draw calls in one O(N) pass.
    // Optimization: skip bounds recomputation and heavy draw-call patching
    // for objects whose world transform has not changed since last frame.
    Frustum frustum;
    const bool useFrustum = useActiveCameraCulling && m_frustumCulling && m_activeCamera;
    if (useFrustum) {
        frustum.ExtractFromMatrix(m_activeCamera->GetViewProjectionMatrix());
        m_frustumVisibilityDirty = true;
    } else if (m_frustumVisibilityDirty && m_drawCallsCacheValid) {
        // Transition from frustum-culled → non-frustum: sweep all draw calls
        // to mark visible, since some may have been marked invisible last frame.
        for (auto &dc : m_cachedDrawCalls.drawCalls) {
            dc.frustumVisible = true;
        }
        m_frustumVisibilityDirty = false;
    }

    m_visibleCount = 0;

    for (auto &renderable : m_renderables) {
        MeshRenderer *mr = renderable.meshRenderer;
        if (!mr)
            continue;
        Transform *transform = renderable.transform;
        if (!transform)
            continue;

        const glm::mat4 &worldMatrix = transform->GetWorldMatrix();

        // Detect transform change: skip bounds + draw-call patch for static objects.
        const bool transformChanged = std::memcmp(&worldMatrix, &renderable.worldMatrix, sizeof(glm::mat4)) != 0;
        const bool bufferDirty = mr->ConsumeMeshBufferDirty();

        if (transformChanged) {
            const bool translationOnly =
                std::memcmp(&worldMatrix[0], &renderable.worldMatrix[0], sizeof(glm::vec4) * 3) == 0;
            const glm::vec3 translationDelta = glm::vec3(worldMatrix[3] - renderable.worldMatrix[3]);
            renderable.worldMatrix = worldMatrix;

            if (translationOnly) {
                renderable.worldBounds.min += translationDelta;
                renderable.worldBounds.max += translationDelta;
            } else {
                glm::vec3 bmin, bmax;
                mr->ComputeWorldBounds(worldMatrix, bmin, bmax);
                renderable.worldBounds = AABB(bmin, bmax);
            }
        }

        if (useFrustum) {
            renderable.visible = frustum.IntersectsAABB(renderable.worldBounds);
        } else {
            renderable.visible = true;
        }

        if (m_drawCallsCacheValid && renderable.drawCallCount > 0) {
            const size_t drawCallEnd =
                std::min(renderable.drawCallStart + renderable.drawCallCount, m_cachedDrawCalls.drawCalls.size());
            std::shared_ptr<const std::vector<glm::mat4>> skinBoneMatricesOwner;
            const std::vector<glm::mat4> *skinBoneMatricesPtr = nullptr;
            const bool isSkinnedRenderer = renderable.skinnedRenderer != nullptr;
            if (auto *skinned = renderable.skinnedRenderer; skinned && skinned->HasRuntimeSkinnedMesh()) {
                skinBoneMatricesOwner = skinned->GetRuntimeSkinBonePalette();
                skinBoneMatricesPtr = skinBoneMatricesOwner.get();
            }

            auto patchSkinPalette = [&](DrawCall &dc) {
                if (!isSkinnedRenderer)
                    return;
                dc.skinBoneMatricesOwner = skinBoneMatricesOwner;
                dc.skinBoneMatrices = skinBoneMatricesPtr;
            };

            if (transformChanged || bufferDirty) {
                // Full patch: transform changed or dynamic mesh data needs a GPU re-upload.
                const glm::vec3 &pivot = mr->GetMeshPivotOffset();
                glm::mat4 drawWorldMatrix = worldMatrix;
                if (mr->GetSubmeshIndex() >= 0 && pivot != glm::vec3(0.0f)) {
                    drawWorldMatrix = worldMatrix * glm::translate(glm::mat4(1.0f), pivot);
                }

                bool firstDirty = true;
                for (size_t drawCallIndex = renderable.drawCallStart; drawCallIndex < drawCallEnd; ++drawCallIndex) {
                    DrawCall &dc = m_cachedDrawCalls.drawCalls[drawCallIndex];
                    dc.worldMatrix = drawWorldMatrix;
                    dc.worldBounds = renderable.worldBounds;
                    dc.frustumVisible = renderable.visible;
                    dc.forceBufferUpdate = firstDirty ? bufferDirty : false;
                    patchSkinPalette(dc);
                    firstDirty = false;
                }
            } else if (useFrustum) {
                // Light patch: only update frustumVisible (camera may have moved).
                for (size_t drawCallIndex = renderable.drawCallStart; drawCallIndex < drawCallEnd; ++drawCallIndex) {
                    DrawCall &dc = m_cachedDrawCalls.drawCalls[drawCallIndex];
                    dc.frustumVisible = renderable.visible;
                    patchSkinPalette(dc);
                }
            } else if (isSkinnedRenderer) {
                for (size_t drawCallIndex = renderable.drawCallStart; drawCallIndex < drawCallEnd; ++drawCallIndex) {
                    patchSkinPalette(m_cachedDrawCalls.drawCalls[drawCallIndex]);
                }
            }
            // else: !transformChanged && !useFrustum → draw calls already correct, skip.
        }

        if (renderable.visible) {
            ++m_visibleCount;
        }
    }
}

void SceneRenderer::PerformCulling()
{
    if (!m_activeCamera) {
        // No camera, mark all as visible
        m_visibleCount = m_renderables.size();
        return;
    }

    // Extract frustum from view-projection matrix
    glm::mat4 viewProj = m_activeCamera->GetViewProjectionMatrix();
    Frustum frustum;
    frustum.ExtractFromMatrix(viewProj);

    // Test each object against frustum
    m_visibleCount = 0;
    for (auto &renderable : m_renderables) {
        // Use AABB-frustum intersection test
        renderable.visible = frustum.IntersectsAABB(renderable.worldBounds);
        if (renderable.visible) {
            ++m_visibleCount;
        }
    }
}

void SceneRenderer::SortRenderables()
{
    // Sort by material render queue (for proper render order)
    // - Lower queue = render first (opaque before transparent)
    // - Within same queue, sort by material pointer (minimize state changes)

    std::sort(m_renderables.begin(), m_renderables.end(), [](const RenderableObject &a, const RenderableObject &b) {
        // Get render queues (default 2000 for opaque)
        int32_t queueA = a.renderMaterialRaw ? a.renderMaterialRaw->GetRenderQueue() : 2000;
        int32_t queueB = b.renderMaterialRaw ? b.renderMaterialRaw->GetRenderQueue() : 2000;

        if (queueA != queueB) {
            return queueA < queueB; // Lower queue first
        }

        // Same queue: sort by material pointer to minimize state changes
        return a.renderMaterialRaw < b.renderMaterialRaw;
    });
}

// ============================================================================
// Shared draw-call emission (eliminates duplication between the two Build paths)
// ============================================================================
void SceneRenderer::EmitDrawCallsForRenderable(DrawCallResult &result, const RenderableObject &renderable, bool visible,
                                               bool bufferDirty) const
{
    MeshRenderer *renderer = renderable.meshRenderer;
    if (!renderer)
        return;

    if (renderer->HasMeshAsset()) {
        const auto &assetRef = renderer->GetMeshAssetRef();
        auto &registry = AssetRegistry::Instance();
        if (!registry.IsLoaded(assetRef.GetGuid()) ||
            assetRef.GetCachedVersion() != registry.GetAssetVersion(assetRef.GetGuid()))
            return;
        auto meshPtr = assetRef.Get();
        if (!meshPtr)
            return;
        const auto stampAssetIdentity = [&assetRef](DrawCall &drawCall) {
            drawCall.meshAssetGuid = assetRef.GetGuid();
            drawCall.meshRuntimeVersion = assetRef.GetCachedVersion();
        };
        const std::vector<Vertex> *objVerticesPtr = &meshPtr->GetVertices();
        const std::vector<uint32_t> *objIndicesPtr = &meshPtr->GetIndices();
        const std::vector<SubMesh> *subMeshesPtr = &meshPtr->GetSubMeshes();
        std::shared_ptr<const std::vector<glm::mat4>> skinBoneMatricesOwner;
        const std::vector<glm::mat4> *skinBoneMatricesPtr = nullptr;
        if (auto *skinned = dynamic_cast<SkinnedMeshRenderer *>(renderer);
            skinned && skinned->HasRuntimeSkinnedMesh()) {
            objVerticesPtr = &skinned->GetRuntimeSkinnedVertices();
            objIndicesPtr = &skinned->GetRuntimeSkinnedIndices();
            subMeshesPtr = &skinned->GetRuntimeSkinnedSubMeshes();
            skinBoneMatricesOwner = skinned->GetRuntimeSkinBonePalette();
            skinBoneMatricesPtr = skinBoneMatricesOwner.get();
        }
        const auto &objVertices = *objVerticesPtr;
        const auto &objIndices = *objIndicesPtr;
        if (objVertices.empty() || objIndices.empty())
            return;

        const glm::mat4 &worldMatrix = renderable.worldMatrix;

        uint32_t subMeshCount = static_cast<uint32_t>(subMeshesPtr ? subMeshesPtr->size() : 0);
        int32_t submeshFilter = renderer->GetSubmeshIndex();
        int32_t nodeGroup = renderer->GetNodeGroup();
        if (subMeshCount == 0) {
            // Fallback: single draw call for entire mesh
            DrawCall dc;
            dc.indexStart = 0;
            dc.indexCount = static_cast<uint32_t>(objIndices.size());
            dc.vertexStart = 0;
            dc.worldMatrix = worldMatrix;
            dc.material = renderer->GetEffectiveMaterial(0);
            dc.objectId = renderable.objectId;
            dc.identity = renderable.renderProxy.MakeDrawIdentity();
            dc.frustumVisible = visible;
            dc.castsShadows = renderer->CastsShadows();
            dc.worldBounds = renderable.worldBounds;
            dc.meshVertices = &objVertices;
            dc.meshIndices = &objIndices;
            stampAssetIdentity(dc);
            dc.skinBoneMatricesOwner = skinBoneMatricesOwner;
            dc.skinBoneMatrices = skinBoneMatricesPtr;
            dc.forceBufferUpdate = bufferDirty;
            result.drawCalls.push_back(dc);
        } else if (submeshFilter >= 0 && static_cast<uint32_t>(submeshFilter) < subMeshCount) {
            // Single submesh mode — render only the specified submesh
            const auto &sub = (*subMeshesPtr)[static_cast<uint32_t>(submeshFilter)];
            glm::mat4 effectiveMatrix = worldMatrix;
            const glm::vec3 &pivot = renderer->GetMeshPivotOffset();
            if (pivot != glm::vec3(0.0f)) {
                effectiveMatrix = worldMatrix * glm::translate(glm::mat4(1.0f), pivot);
            }
            DrawCall dc;
            dc.indexStart = sub.indexStart;
            dc.indexCount = sub.indexCount;
            dc.vertexStart = 0;
            dc.worldMatrix = effectiveMatrix;
            dc.material = renderer->GetEffectiveMaterial(0);
            dc.objectId = renderable.objectId;
            dc.identity = renderable.renderProxy.MakeDrawIdentity(static_cast<uint32_t>(submeshFilter));
            dc.frustumVisible = visible;
            dc.castsShadows = renderer->CastsShadows();
            dc.worldBounds = renderable.worldBounds;
            dc.meshVertices = &objVertices;
            dc.meshIndices = &objIndices;
            stampAssetIdentity(dc);
            dc.skinBoneMatricesOwner = skinBoneMatricesOwner;
            dc.skinBoneMatrices = skinBoneMatricesPtr;
            dc.forceBufferUpdate = bufferDirty;
            result.drawCalls.push_back(dc);
        } else {
            // One DrawCall per submesh with its own material slot
            // Build local slot remap for nodeGroup so material indices are contiguous
            constexpr uint32_t SLOT_REMAP_CAP = 32;
            uint32_t slotRemap[SLOT_REMAP_CAP];
            std::memset(slotRemap, 0xFF, sizeof(slotRemap));
            uint32_t nextSlotIdx = 0;
            if (nodeGroup >= 0) {
                for (uint32_t si = 0; si < subMeshCount; ++si) {
                    const auto &s = (*subMeshesPtr)[si];
                    if (static_cast<int32_t>(s.nodeGroup) != nodeGroup)
                        continue;
                    if (s.materialSlot < SLOT_REMAP_CAP && slotRemap[s.materialSlot] == 0xFFFFFFFF)
                        slotRemap[s.materialSlot] = nextSlotIdx++;
                }
            }
            bool firstDirty = true;
            for (uint32_t si = 0; si < subMeshCount; ++si) {
                const auto &sub = (*subMeshesPtr)[si];
                if (nodeGroup >= 0 && static_cast<int32_t>(sub.nodeGroup) != nodeGroup)
                    continue;
                DrawCall dc;
                dc.indexStart = sub.indexStart;
                dc.indexCount = sub.indexCount;
                dc.vertexStart = 0;
                dc.worldMatrix = worldMatrix;
                uint32_t matSlot = sub.materialSlot;
                if (nodeGroup >= 0 && matSlot < SLOT_REMAP_CAP && slotRemap[matSlot] != 0xFFFFFFFF)
                    matSlot = slotRemap[matSlot];
                dc.material = renderer->GetEffectiveMaterial(matSlot);
                dc.objectId = renderable.objectId;
                dc.identity = renderable.renderProxy.MakeDrawIdentity(si);
                dc.frustumVisible = visible;
                dc.castsShadows = renderer->CastsShadows();
                dc.worldBounds = renderable.worldBounds;
                dc.meshVertices = &objVertices;
                dc.meshIndices = &objIndices;
                stampAssetIdentity(dc);
                dc.skinBoneMatricesOwner = skinBoneMatricesOwner;
                dc.skinBoneMatrices = skinBoneMatricesPtr;
                dc.forceBufferUpdate = firstDirty ? bufferDirty : false;
                firstDirty = false;
                result.drawCalls.push_back(dc);
            }
        }
    } else if (renderer->HasInlineMesh()) {
        const auto &objVertices = renderer->GetInlineVertices();
        const auto &objIndices = renderer->GetInlineIndices();
        if (objVertices.empty() || objIndices.empty())
            return;

        const glm::mat4 &worldMatrix = renderable.worldMatrix;
        DrawCall dc;
        dc.indexStart = 0;
        dc.indexCount = static_cast<uint32_t>(objIndices.size());
        dc.vertexStart = 0;
        dc.worldMatrix = worldMatrix;
        dc.material = renderer->GetEffectiveMaterial(0);
        dc.objectId = renderable.objectId;
        dc.identity = renderable.renderProxy.MakeDrawIdentity();
        dc.frustumVisible = visible;
        dc.castsShadows = renderer->CastsShadows();
        dc.worldBounds = renderable.worldBounds;
        dc.meshVertices = &objVertices;
        dc.meshIndices = &objIndices;
        dc.forceBufferUpdate = bufferDirty;
        result.drawCalls.push_back(dc);
    }
}

const DrawCallResult &SceneRenderer::BuildDrawCalls()
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    const auto buildStart = Clock::now();
#endif
    if (m_drawCallsCacheValid) {
#if INFERNUX_FRAME_PROFILE
        m_profileSnapshot.buildMs += std::chrono::duration<double, std::milli>(Clock::now() - buildStart).count();
        m_profileSnapshot.buildCalls += 1.0;
        m_profileSnapshot.drawCalls += static_cast<double>(m_cachedDrawCalls.drawCalls.size());
#endif
        return m_cachedDrawCalls;
    }

    // Slow path: full rebuild.
    DrawCallResult result;
    result.drawCalls.reserve(m_cachedDrawCalls.drawCalls.empty() ? m_renderables.size()
                                                                 : m_cachedDrawCalls.drawCalls.size());

    for (auto &renderable : m_renderables) {
        MeshRenderer *renderer = renderable.meshRenderer;
        if (!renderer)
            continue;

        bool bufferDirty = renderer->ConsumeMeshBufferDirty();
        renderable.drawCallStart = result.drawCalls.size();
        EmitDrawCallsForRenderable(result, renderable, renderable.visible, bufferDirty);
        renderable.drawCallCount = result.drawCalls.size() - renderable.drawCallStart;
    }

    m_cachedDrawCalls = std::move(result);
    m_drawCallsCacheValid = true;
#if INFERNUX_FRAME_PROFILE
    m_profileSnapshot.buildMs += std::chrono::duration<double, std::milli>(Clock::now() - buildStart).count();
    m_profileSnapshot.buildCalls += 1.0;
    m_profileSnapshot.drawCalls += static_cast<double>(m_cachedDrawCalls.drawCalls.size());
#endif
    return m_cachedDrawCalls;
}

void SceneRenderer::AcknowledgeMeshBufferUpdates()
{
    if (!m_drawCallsCacheValid)
        return;

    for (DrawCall &drawCall : m_cachedDrawCalls.drawCalls)
        drawCall.forceBufferUpdate = false;
}

CameraDrawCallResult SceneRenderer::BuildDrawCallsForCamera(Camera *camera, bool includeShadowDrawCalls)
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    const auto buildStart = Clock::now();
#endif
    CameraDrawCallResult result;
    if (!camera || m_renderables.empty())
        return result;

    const DrawCallResult &cachedResult = BuildDrawCalls();
    if (cachedResult.drawCalls.empty())
        return result;

    const uint32_t cullingMask = camera->GetCullingMask();
    Frustum frustum;
    if (m_frustumCulling) {
        frustum.ExtractFromMatrix(camera->GetViewProjectionMatrix());
    }

    // When culling mask allows all layers, shadow draw calls can reference
    // the scene's cached draw calls directly (zero-copy).  DrawShadowCasters
    // does its own per-cascade frustum culling and never reads frustumVisible.
    const bool allLayersVisible = (cullingMask == 0xFFFFFFFF);
    if (allLayersVisible && includeShadowDrawCalls) {
        result.shadowDrawCallsRef = &cachedResult.drawCalls;
    }

    constexpr size_t kRenderContextAppendSlack = 32;
    result.visibleDrawCalls.reserve((m_visibleCount > 0 ? m_visibleCount : cachedResult.drawCalls.size()) +
                                    kRenderContextAppendSlack);

    m_visibleCount = 0;
    for (auto &renderable : m_renderables) {
        MeshRenderer *renderer = renderable.meshRenderer;
        if (!renderer)
            continue;

        if (!allLayersVisible) {
            // Layer mask filter (only when not all-layers)
            GameObject *obj = renderer->GetGameObject();
            if (!obj)
                continue;
            uint32_t layerBit = 1u << static_cast<uint32_t>(obj->GetLayer());
            if ((cullingMask & layerBit) == 0)
                continue;
        }

        const bool visible = m_frustumCulling ? frustum.IntersectsAABB(renderable.worldBounds) : true;
        renderable.visible = visible;

        if (!visible) {
            // Shadow uses reference (or full copy below for non-all-layers).
            // Forward list only needs visible objects — skip early.
            if (allLayersVisible)
                continue;

            // Non-all-layers: still need to push shadow draw calls for invisible-but-layer-included objects.
            if (includeShadowDrawCalls) {
                const size_t drawCallStart = renderable.drawCallStart;
                const size_t drawCallEnd =
                    std::min(drawCallStart + renderable.drawCallCount, cachedResult.drawCalls.size());
                for (size_t drawCallIndex = drawCallStart; drawCallIndex < drawCallEnd; ++drawCallIndex) {
                    DrawCall dc = cachedResult.drawCalls[drawCallIndex];
                    dc.frustumVisible = false;
                    result.shadowDrawCalls.push_back(dc);
                }
            }
            continue;
        }

        ++m_visibleCount;
        const size_t drawCallStart = renderable.drawCallStart;
        const size_t drawCallEnd = std::min(drawCallStart + renderable.drawCallCount, cachedResult.drawCalls.size());
        if (drawCallStart >= drawCallEnd)
            continue;

        for (size_t drawCallIndex = drawCallStart; drawCallIndex < drawCallEnd; ++drawCallIndex) {
            DrawCall dc = cachedResult.drawCalls[drawCallIndex];
            dc.frustumVisible = true;
            result.visibleDrawCalls.push_back(dc);
            if (includeShadowDrawCalls && !allLayersVisible) {
                result.shadowDrawCalls.push_back(dc);
            }
        }
    }

#if INFERNUX_FRAME_PROFILE
    m_profileSnapshot.buildCameraMs += std::chrono::duration<double, std::milli>(Clock::now() - buildStart).count();
    m_profileSnapshot.buildCameraCalls += 1.0;
    m_profileSnapshot.drawCalls += static_cast<double>(result.visibleDrawCalls.size());
#endif
    return result;
}

// ============================================================================
// SceneRenderBridge Implementation
// ============================================================================

SceneRenderBridge &SceneRenderBridge::Instance()
{
    // Intentionally leaked (see SceneManager::Instance).
    static SceneRenderBridge *instance = new SceneRenderBridge();
    return *instance;
}

void SceneRenderBridge::UpdateCameraData(float *outPos, float *outLookAt, float *outUp)
{
    glm::vec3 pos = m_sceneRenderer.GetCameraPosition();
    glm::vec3 forward = m_sceneRenderer.GetCameraForward();
    glm::vec3 up = m_sceneRenderer.GetCameraUp();

    // LookAt is position + forward direction
    glm::vec3 lookAt = pos + forward;

    if (outPos) {
        outPos[0] = pos.x;
        outPos[1] = pos.y;
        outPos[2] = pos.z;
    }

    if (outLookAt) {
        outLookAt[0] = lookAt.x;
        outLookAt[1] = lookAt.y;
        outLookAt[2] = lookAt.z;
    }

    if (outUp) {
        outUp[0] = up.x;
        outUp[1] = up.y;
        outUp[2] = up.z;
    }
}

void SceneRenderBridge::OnWindowResize(uint32_t width, uint32_t height)
{
    if (height > 0) {
        float aspect = static_cast<float>(width) / static_cast<float>(height);
        m_sceneRenderer.SetAspectRatio(aspect);

        // Sync screen dimensions for ScreenToWorld/WorldToScreen
        Camera *editorCam = GetEditorCamera();
        if (editorCam) {
            editorCam->SetScreenDimensions(width, height);
        }
    }
}

void SceneRenderBridge::PrepareFrame(bool useActiveCameraCulling)
{
    m_sceneRenderer.PrepareFrame(useActiveCameraCulling);
}

DrawCallResult SceneRenderBridge::PrepareAndBuildForCamera(Camera *camera)
{
    // Use a temporary SceneRenderer for independent camera culling
    // so we don't disturb the editor camera's cached state.
    SceneRenderer tempRenderer;
    tempRenderer.SetFrustumCullingEnabled(m_sceneRenderer.IsFrustumCullingEnabled());
    tempRenderer.PrepareFrame(camera);
    return tempRenderer.BuildDrawCalls();
}

CameraDrawCallResult SceneRenderBridge::CullAndBuildForCamera(Camera *camera, bool includeShadowDrawCalls)
{
    // Reuse editor camera's already-collected renderables.
    // Only re-cull with the given camera's frustum + layer mask.
    return m_sceneRenderer.BuildDrawCallsForCamera(camera, includeShadowDrawCalls);
}

const DrawCallResult &SceneRenderBridge::BuildDrawCalls()
{
    return m_sceneRenderer.BuildDrawCalls();
}

Camera *SceneRenderBridge::GetEditorCamera() const
{
    return SceneManager::Instance().GetEditorCameraController().GetCamera();
}

} // namespace infernux
