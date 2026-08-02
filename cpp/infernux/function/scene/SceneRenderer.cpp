#include "SceneRenderer.h"

#include <algorithm>
#include <chrono>
#include <glm/gtc/matrix_transform.hpp>

namespace infernux
{

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

    const uint32_t cullingMask = camera.cullingMask;
    Frustum frustum;
    if (m_frustumCulling)
        frustum.ExtractFromMatrix(camera.viewProjection);

    const bool allLayersVisible = cullingMask == 0xFFFFFFFFu;
    if (allLayersVisible && includeShadowDrawCalls)
        result.shadowDrawCallsRef = &cachedResult.drawCalls;

    constexpr size_t kRenderContextAppendSlack = 32;
    const size_t previousVisibleCount = m_visibleCount.load(std::memory_order_relaxed);
    result.visibleDrawCalls.reserve((previousVisibleCount > 0 ? previousVisibleCount : cachedResult.drawCalls.size()) +
                                    kRenderContextAppendSlack);

    size_t visibleCount = 0;
    for (const auto &renderable : renderables) {
        const auto &cache = renderable.cache;
        if (!allLayersVisible && (cullingMask & renderable.structural.layerMask) == 0)
            continue;

        const bool visible = m_frustumCulling ? frustum.IntersectsAABB(renderable.frame.worldBounds) : true;
        if (!visible) {
            if (allLayersVisible)
                continue;
            if (includeShadowDrawCalls) {
                const size_t drawCallEnd =
                    std::min(cache.drawCallStart + cache.drawCallCount, cachedResult.drawCalls.size());
                for (size_t drawCallIndex = cache.drawCallStart; drawCallIndex < drawCallEnd; ++drawCallIndex) {
                    DrawCall drawCall = cachedResult.drawCalls[drawCallIndex];
                    drawCall.frustumVisible = false;
                    result.shadowDrawCalls.push_back(std::move(drawCall));
                }
            }
            continue;
        }

        ++visibleCount;
        const size_t drawCallEnd = std::min(cache.drawCallStart + cache.drawCallCount, cachedResult.drawCalls.size());
        if (cache.drawCallStart >= drawCallEnd)
            continue;
        for (size_t drawCallIndex = cache.drawCallStart; drawCallIndex < drawCallEnd; ++drawCallIndex) {
            DrawCall drawCall = cachedResult.drawCalls[drawCallIndex];
            drawCall.frustumVisible = true;
            result.visibleDrawCalls.push_back(drawCall);
            if (includeShadowDrawCalls && !allLayersVisible)
                result.shadowDrawCalls.push_back(std::move(drawCall));
        }
    }
    m_visibleCount.store(visibleCount, std::memory_order_relaxed);

#if INFERNUX_FRAME_PROFILE
    m_profileSnapshot.buildCameraMs += std::chrono::duration<double, std::milli>(Clock::now() - buildStart).count();
    m_profileSnapshot.buildCameraCalls += 1.0;
    m_profileSnapshot.drawCalls += static_cast<double>(result.visibleDrawCalls.size());
#endif
    return result;
}

} // namespace infernux
