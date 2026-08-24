#include "SceneRenderer.h"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <glm/gtc/matrix_transform.hpp>

namespace infernux
{

namespace
{
enum class FrustumAABBRelation : uint8_t
{
    Outside,
    Intersecting,
    Inside,
};

FrustumAABBRelation ClassifyAABB(const Frustum &frustum, const AABB &bounds)
{
    bool fullyInside = true;
    for (int planeIndex = 0; planeIndex < Frustum::PlaneIndex::Count; ++planeIndex) {
        const Plane &plane = frustum.GetPlane(static_cast<Frustum::PlaneIndex>(planeIndex));
        glm::vec3 positive;
        glm::vec3 negative;
        positive.x = plane.normal.x >= 0.0f ? bounds.max.x : bounds.min.x;
        positive.y = plane.normal.y >= 0.0f ? bounds.max.y : bounds.min.y;
        positive.z = plane.normal.z >= 0.0f ? bounds.max.z : bounds.min.z;
        negative.x = plane.normal.x >= 0.0f ? bounds.min.x : bounds.max.x;
        negative.y = plane.normal.y >= 0.0f ? bounds.min.y : bounds.max.y;
        negative.z = plane.normal.z >= 0.0f ? bounds.min.z : bounds.max.z;
        if (plane.DistanceToPoint(positive) < 0.0f)
            return FrustumAABBRelation::Outside;
        if (plane.DistanceToPoint(negative) < 0.0f)
            fullyInside = false;
    }
    return fullyInside ? FrustumAABBRelation::Inside : FrustumAABBRelation::Intersecting;
}
} // namespace

void SceneRenderer::RebuildCoarseCullGroups(const RenderWorldFrame &world)
{
    if (m_coarseCullWorldId == world.WorldId() && m_coarseCullStructuralRevision == world.StructuralRevision() &&
        m_coarseCullTransformRevision == world.TransformRevision())
        return;

    constexpr size_t kProxiesPerGroup = 64;
    const auto &proxies = world.Proxies();
    m_coarseCullGroups.clear();
    m_coarseCullGroups.reserve((proxies.size() + kProxiesPerGroup - 1) / kProxiesPerGroup);
    for (size_t start = 0; start < proxies.size(); start += kProxiesPerGroup) {
        const size_t end = std::min(start + kProxiesPerGroup, proxies.size());
        CoarseCullGroup group;
        group.proxyStart = start;
        group.proxyCount = end - start;
        group.worldBounds = proxies[start].frame.worldBounds;
        for (size_t index = start; index < end; ++index) {
            const RenderProxy &proxy = proxies[index];
            group.layerMask |= proxy.structural.layerMask;
            group.worldBounds.min = glm::min(group.worldBounds.min, proxy.frame.worldBounds.min);
            group.worldBounds.max = glm::max(group.worldBounds.max, proxy.frame.worldBounds.max);
        }
        m_coarseCullGroups.push_back(group);
    }
    m_coarseCullWorldId = world.WorldId();
    m_coarseCullStructuralRevision = world.StructuralRevision();
    m_coarseCullTransformRevision = world.TransformRevision();
}

glm::mat4 SceneRenderer::GetViewMatrix() const
{
    const auto frame = m_renderWorld.Acquire();
    return frame && frame->PrimaryView().valid ? frame->PrimaryView().view : glm::mat4{1.0f};
}

glm::mat4 SceneRenderer::GetProjectionMatrix() const
{
    const auto frame = m_renderWorld.Acquire();
    return frame && frame->PrimaryView().valid ? frame->PrimaryView().projection
                                               : glm::perspective(glm::radians(60.0f), 16.0f / 9.0f, 0.1f, 1000.0f);
}

glm::vec3 SceneRenderer::GetCameraPosition() const
{
    const auto frame = m_renderWorld.Acquire();
    return frame ? frame->PrimaryView().position : glm::vec3{0.0f, 0.0f, 5.0f};
}

glm::vec3 SceneRenderer::GetCameraForward() const
{
    const auto frame = m_renderWorld.Acquire();
    return frame ? frame->PrimaryView().forward : glm::vec3{0.0f, 0.0f, 1.0f};
}

glm::vec3 SceneRenderer::GetCameraUp() const
{
    const auto frame = m_renderWorld.Acquire();
    return frame ? frame->PrimaryView().up : glm::vec3{0.0f, 1.0f, 0.0f};
}

const DrawCallResult &SceneRenderer::BuildDrawCalls()
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    const auto buildStart = Clock::now();
#endif
    m_buildOwner = m_renderWorld.Acquire();
    static const DrawCallResult empty;
    const DrawCallResult &result = m_buildOwner ? m_buildOwner->DrawCalls() : empty;
#if INFERNUX_FRAME_PROFILE
    m_profileSnapshot.buildMs += std::chrono::duration<double, std::milli>(Clock::now() - buildStart).count();
    m_profileSnapshot.buildCalls += 1.0;
    m_profileSnapshot.drawCalls += static_cast<double>(result.drawCalls.size());
#endif
    return result;
}

CameraDrawCallResult SceneRenderer::BuildDrawCallsForCamera(const RenderViewData &camera, bool includeShadowDrawCalls)
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    const auto buildStart = Clock::now();
#endif
    CameraDrawCallResult result;
    result.worldOwner = m_renderWorld.Acquire();
    if (!camera.valid || !result.worldOwner || result.worldOwner->Proxies().empty())
        return result;

    const auto &renderables = result.worldOwner->Proxies();
    const DrawCallResult &cachedResult = result.worldOwner->DrawCalls();
    if (cachedResult.drawCalls.empty())
        return result;

    const uint64_t cacheKey = camera.cameraId != 0 ? camera.cameraId : 1;
    CameraCullCache &cameraCache = m_cameraCullCaches[cacheKey];
    const bool worldMatches = cameraCache.worldId == result.worldOwner->WorldId();
    const bool structuralMatches = cameraCache.structuralRevision == result.worldOwner->StructuralRevision();
    const bool transformMatches = cameraCache.transformRevision == result.worldOwner->TransformRevision();
    const bool contentMatches = cameraCache.contentRevision == result.worldOwner->ContentRevision();
    const bool maskMatches = cameraCache.cullingMask == camera.cullingMask;
    const bool frustumMatches = cameraCache.frustumCulling == m_frustumCulling;
    const bool viewProjectionMatches =
        std::memcmp(&cameraCache.viewProjection, &camera.viewProjection, sizeof(glm::mat4)) == 0;
    const bool cacheHit =
        worldMatches && structuralMatches && transformMatches && maskMatches && frustumMatches && viewProjectionMatches;
#if INFERNUX_FRAME_PROFILE
    if (cacheHit) {
        m_profileSnapshot.cameraCacheHits += 1.0;
    } else {
        m_profileSnapshot.cameraCacheMisses += 1.0;
        m_profileSnapshot.cameraMissWorld += worldMatches ? 0.0 : 1.0;
        m_profileSnapshot.cameraMissStructural += structuralMatches ? 0.0 : 1.0;
        m_profileSnapshot.cameraMissTransform += transformMatches ? 0.0 : 1.0;
        m_profileSnapshot.cameraMissMask += maskMatches ? 0.0 : 1.0;
        m_profileSnapshot.cameraMissFrustum += frustumMatches ? 0.0 : 1.0;
        m_profileSnapshot.cameraMissViewProjection += viewProjectionMatches ? 0.0 : 1.0;
    }
#endif
    if (cacheHit) {
        if (!contentMatches) {
            if (!cameraCache.usesWorldDrawCalls) {
                const size_t patchCount =
                    std::min(cameraCache.visibleDrawCalls.size(), cameraCache.visibleDrawCallSourceIndices.size());
                for (size_t index = 0; index < patchCount; ++index) {
                    const size_t sourceIndex = cameraCache.visibleDrawCallSourceIndices[index];
                    if (sourceIndex >= cachedResult.drawCalls.size())
                        continue;
                    DrawCall &destination = cameraCache.visibleDrawCalls[index];
                    const DrawCall &source = cachedResult.drawCalls[sourceIndex];
                    destination.skinBoneMatricesOwner = source.skinBoneMatricesOwner;
                    destination.skinBoneMatrices = source.skinBoneMatrices;
                    destination.previousSkinBoneMatricesOwner = source.previousSkinBoneMatricesOwner;
                    destination.previousSkinBoneMatrices = source.previousSkinBoneMatrices;
                }
            }
            cameraCache.contentRevision = result.worldOwner->ContentRevision();
            cameraCache.visibleListRevision = m_nextCameraCullRevision++;
            if (m_nextCameraCullRevision == 0)
                m_nextCameraCullRevision = 1;
        }
        cameraCache.worldOwner = result.worldOwner;
        result.visibleDrawCallsRef =
            cameraCache.usesWorldDrawCalls ? &cachedResult.drawCalls : &cameraCache.visibleDrawCalls;
        result.visibleListRevision = cameraCache.visibleListRevision;
        if (camera.cullingMask == 0xFFFFFFFFu && includeShadowDrawCalls) {
            result.shadowDrawCallsRef = &cachedResult.drawCalls;
            result.shadowListRevision = cameraCache.visibleListRevision;
        }
        m_visibleCount.store(cameraCache.visibleCount, std::memory_order_relaxed);
#if INFERNUX_FRAME_PROFILE
        m_profileSnapshot.buildCameraMs += std::chrono::duration<double, std::milli>(Clock::now() - buildStart).count();
        m_profileSnapshot.buildCameraCalls += 1.0;
        m_profileSnapshot.drawCalls += static_cast<double>(result.visibleDrawCallsRef->size());
#endif
        return result;
    }

    cameraCache.visibleDrawCalls.clear();
    cameraCache.visibleDrawCallSourceIndices.clear();
    const uint32_t cullingMask = camera.cullingMask;
    Frustum frustum;
    if (m_frustumCulling)
        frustum.ExtractFromMatrix(camera.viewProjection);

    const bool allLayersVisible = cullingMask == 0xFFFFFFFFu;
    if (allLayersVisible && includeShadowDrawCalls)
        result.shadowDrawCallsRef = &cachedResult.drawCalls;

    // A dense static field is cheaper to submit as one conservative instance
    // batch than to compact tens of thousands of DrawCalls merely to reject a
    // few edge instances. The decision remains camera-local: every camera
    // classifies the coarse groups independently, and sparse views still take
    // the exact per-renderable path below.
    RebuildCoarseCullGroups(*result.worldOwner);
    constexpr size_t kDenseFieldMinimumProxyCount = 4096;
    constexpr size_t kDenseFieldVisibilityPercent = 90;
    size_t coarseCandidateCount = 0;
    if (m_frustumCulling && allLayersVisible && renderables.size() >= kDenseFieldMinimumProxyCount) {
        for (const CoarseCullGroup &group : m_coarseCullGroups) {
            if (ClassifyAABB(frustum, group.worldBounds) != FrustumAABBRelation::Outside)
                coarseCandidateCount += group.proxyCount;
        }
    }
    const bool useConservativeFullList =
        !m_frustumCulling || (allLayersVisible && renderables.size() >= kDenseFieldMinimumProxyCount &&
                              coarseCandidateCount * 100 >= renderables.size() * kDenseFieldVisibilityPercent);
    if (useConservativeFullList) {
#if INFERNUX_FRAME_PROFILE
        m_profileSnapshot.conservativeFullListUses += 1.0;
#endif
        const bool sameWorldList = cameraCache.usesWorldDrawCalls &&
                                   cameraCache.worldId == result.worldOwner->WorldId() &&
                                   cameraCache.structuralRevision == result.worldOwner->StructuralRevision() &&
                                   cameraCache.transformRevision == result.worldOwner->TransformRevision() &&
                                   cameraCache.cullingMask == camera.cullingMask;
        cameraCache.usesWorldDrawCalls = true;
        cameraCache.worldId = result.worldOwner->WorldId();
        cameraCache.structuralRevision = result.worldOwner->StructuralRevision();
        cameraCache.transformRevision = result.worldOwner->TransformRevision();
        cameraCache.contentRevision = result.worldOwner->ContentRevision();
        cameraCache.viewProjection = camera.viewProjection;
        cameraCache.cullingMask = camera.cullingMask;
        cameraCache.frustumCulling = m_frustumCulling;
        cameraCache.visibleCount = renderables.size();
        if (!sameWorldList) {
            cameraCache.visibleListRevision = m_nextCameraCullRevision++;
            if (m_nextCameraCullRevision == 0)
                m_nextCameraCullRevision = 1;
        }
        cameraCache.worldOwner = result.worldOwner;
        m_visibleCount.store(renderables.size(), std::memory_order_relaxed);
        result.visibleDrawCallsRef = &cachedResult.drawCalls;
        result.visibleListRevision = cameraCache.visibleListRevision;
        if (result.shadowDrawCallsRef)
            result.shadowListRevision = cameraCache.visibleListRevision;
#if INFERNUX_FRAME_PROFILE
        m_profileSnapshot.buildCameraMs += std::chrono::duration<double, std::milli>(Clock::now() - buildStart).count();
        m_profileSnapshot.buildCameraCalls += 1.0;
        m_profileSnapshot.drawCalls += static_cast<double>(cachedResult.drawCalls.size());
#endif
        return result;
    }

    cameraCache.usesWorldDrawCalls = false;

    constexpr size_t kRenderContextAppendSlack = 32;
    const size_t previousVisibleCount = m_visibleCount.load(std::memory_order_relaxed);
    cameraCache.visibleDrawCalls.reserve(
        (previousVisibleCount > 0 ? previousVisibleCount : cachedResult.drawCalls.size()) + kRenderContextAppendSlack);

    size_t visibleCount = 0;
    auto appendRenderable = [&](const RenderProxy &renderable, bool testBounds) {
        const auto &cache = renderable.cache;
        if (!allLayersVisible && (cullingMask & renderable.structural.layerMask) == 0)
            return;

        const bool visible = !testBounds || frustum.IntersectsAABB(renderable.frame.worldBounds);
        if (!visible) {
            if (allLayersVisible)
                return;
            if (includeShadowDrawCalls) {
                const size_t drawCallEnd =
                    std::min(cache.drawCallStart + cache.drawCallCount, cachedResult.drawCalls.size());
                for (size_t drawCallIndex = cache.drawCallStart; drawCallIndex < drawCallEnd; ++drawCallIndex) {
                    DrawCall drawCall = cachedResult.drawCalls[drawCallIndex];
                    drawCall.frustumVisible = false;
                    result.shadowDrawCalls.push_back(std::move(drawCall));
                }
            }
            return;
        }

        ++visibleCount;
        const size_t drawCallEnd = std::min(cache.drawCallStart + cache.drawCallCount, cachedResult.drawCalls.size());
        if (cache.drawCallStart >= drawCallEnd)
            return;
        for (size_t drawCallIndex = cache.drawCallStart; drawCallIndex < drawCallEnd; ++drawCallIndex) {
            DrawCall drawCall = cachedResult.drawCalls[drawCallIndex];
            drawCall.frustumVisible = true;
            cameraCache.visibleDrawCalls.push_back(drawCall);
            cameraCache.visibleDrawCallSourceIndices.push_back(drawCallIndex);
            if (includeShadowDrawCalls && !allLayersVisible)
                result.shadowDrawCalls.push_back(std::move(drawCall));
        }
    };

    for (const CoarseCullGroup &group : m_coarseCullGroups) {
        if (!allLayersVisible && (group.layerMask & cullingMask) == 0)
            continue;
        const FrustumAABBRelation relation = ClassifyAABB(frustum, group.worldBounds);
        if (relation == FrustumAABBRelation::Outside)
            continue;
        const bool testBounds = relation == FrustumAABBRelation::Intersecting;
        const size_t end = std::min(group.proxyStart + group.proxyCount, renderables.size());
        for (size_t index = group.proxyStart; index < end; ++index)
            appendRenderable(renderables[index], testBounds);
    }
    m_visibleCount.store(visibleCount, std::memory_order_relaxed);
    cameraCache.worldId = result.worldOwner->WorldId();
    cameraCache.structuralRevision = result.worldOwner->StructuralRevision();
    cameraCache.transformRevision = result.worldOwner->TransformRevision();
    cameraCache.contentRevision = result.worldOwner->ContentRevision();
    cameraCache.viewProjection = camera.viewProjection;
    cameraCache.cullingMask = camera.cullingMask;
    cameraCache.frustumCulling = m_frustumCulling;
    cameraCache.visibleCount = visibleCount;
    cameraCache.visibleListRevision = m_nextCameraCullRevision++;
    if (m_nextCameraCullRevision == 0)
        m_nextCameraCullRevision = 1;
    cameraCache.worldOwner = result.worldOwner;
    result.visibleDrawCallsRef = &cameraCache.visibleDrawCalls;
    result.visibleListRevision = cameraCache.visibleListRevision;
    if (result.shadowDrawCallsRef || !result.shadowDrawCalls.empty())
        result.shadowListRevision = cameraCache.visibleListRevision;

#if INFERNUX_FRAME_PROFILE
    m_profileSnapshot.buildCameraMs += std::chrono::duration<double, std::milli>(Clock::now() - buildStart).count();
    m_profileSnapshot.buildCameraCalls += 1.0;
    m_profileSnapshot.drawCalls += static_cast<double>(cameraCache.visibleDrawCalls.size());
#endif
    return result;
}

} // namespace infernux
