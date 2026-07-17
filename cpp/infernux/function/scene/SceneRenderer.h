#pragma once

#include "../scene/SceneSystem.h"
#include <function/renderer/Frustum.h>
#include <function/renderer/InxRenderStruct.h>
#include <function/renderer/ProfileConfig.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <functional>
#include <memory>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux
{

class SkinnedMeshRenderer;
class Transform;

/**
 * @brief RenderableObject - data needed to render one object.
 *
 * This is a lightweight struct extracted from GameObject+MeshRenderer
 * for efficient rendering without traversing the scene graph during draw calls.
 */
struct RenderableObject
{
    uint64_t objectId;
    RenderProxyHandle renderProxy;
    glm::mat4 worldMatrix;
    MeshRef mesh;
    std::shared_ptr<InxMaterial> renderMaterial;    // Actual material for rendering (kept alive by MeshRenderer)
    InxMaterial *renderMaterialRaw = nullptr;       // Raw pointer for fast sort/access
    MeshRenderer *meshRenderer = nullptr;           // Direct pointer to avoid re-lookup
    Transform *transform = nullptr;                 // Stable while the renderer registry version is unchanged
    SkinnedMeshRenderer *skinnedRenderer = nullptr; // Resolved once instead of RTTI checks every frame
    AABB worldBounds;                               // World-space bounding box for culling
    size_t drawCallStart = 0;                       // Cached span in m_cachedDrawCalls
    size_t drawCallCount = 0;                       // Number of draw calls emitted for this renderable
    bool visible;
};

struct CameraDrawCallResult
{
    std::vector<DrawCall> visibleDrawCalls;
    std::vector<DrawCall> shadowDrawCalls;
    const std::vector<DrawCall> *shadowDrawCallsRef = nullptr; ///< Zero-copy ref (valid when cullingMask == all)
};

/**
 * @brief SceneRenderer - bridges Scene system with Vulkan rendering.
 *
 * Responsibilities:
 * - Collect renderable objects from the active scene
 * - Provide camera matrices to the renderer
 * - Sort objects for rendering (by material, depth, etc.)
 * - Culling (frustum, occlusion)
 */
class SceneRenderer
{
  public:
    SceneRenderer() = default;
    ~SceneRenderer() = default;

    // ========================================================================
    // Frame preparation
    // ========================================================================

    /// @brief Prepare render data for the current frame using editor camera.
    /// When useActiveCameraCulling is false, only updates transforms/bounds/cache state.
    void PrepareFrame(bool useActiveCameraCulling = true);

    /// @brief Prepare render data for a specific camera (independent culling).
    /// Used by Game View to cull against the game camera's frustum.
    void PrepareFrame(Camera *camera);

    /// @brief Get the view matrix for rendering
    [[nodiscard]] glm::mat4 GetViewMatrix() const;

    /// @brief Get the projection matrix for rendering
    [[nodiscard]] glm::mat4 GetProjectionMatrix() const;

    /// @brief Get camera position for shaders
    [[nodiscard]] glm::vec3 GetCameraPosition() const;

    /// @brief Get camera forward direction
    [[nodiscard]] glm::vec3 GetCameraForward() const;

    /// @brief Get camera up vector
    [[nodiscard]] glm::vec3 GetCameraUp() const;

    // ========================================================================
    // Renderable access
    // ========================================================================

    /// @brief Get all renderable objects for this frame
    [[nodiscard]] const std::vector<RenderableObject> &GetRenderables() const
    {
        return m_renderables;
    }

    /// @brief Get number of visible objects after culling
    [[nodiscard]] size_t GetVisibleCount() const
    {
        return m_visibleCount;
    }

    /// @brief Build draw calls from visible renderables.
    /// Converts the culled/sorted RenderableObject list into DrawCall + combined vertex/index data.
    /// @note Vertices remain in model/local space; per-object world transform is applied on GPU via push constants.
    [[nodiscard]] const DrawCallResult &BuildDrawCalls();

    /// Clear one-shot mesh upload requests after a render context has
    /// consumed the cached draw calls. Cached draw calls survive across
    /// frames, while forceBufferUpdate must not.
    void AcknowledgeMeshBufferUpdates();

    /// @brief Build draw calls by re-culling existing renderables against a different camera.
    /// Reuses cached draw-call spans and world bounds from PrepareFrame().
    /// Returns a forward-visible set, plus an optional layer-filtered shadow candidate set.
    [[nodiscard]] CameraDrawCallResult BuildDrawCallsForCamera(Camera *camera, bool includeShadowDrawCalls);

    // ========================================================================
    // Settings
    // ========================================================================

    /// @brief Enable/disable frustum culling
    void SetFrustumCullingEnabled(bool enabled)
    {
        m_frustumCulling = enabled;
    }
    [[nodiscard]] bool IsFrustumCullingEnabled() const
    {
        return m_frustumCulling;
    }

    /// @brief Set aspect ratio (from window)
    void SetAspectRatio(float aspect);

#if INFERNUX_FRAME_PROFILE
    struct ProfileSnapshot
    {
        double prepareMs = 0.0;
        double collectMs = 0.0;
        double updateMs = 0.0;
        double cullMs = 0.0;
        double sortMs = 0.0;
        double buildMs = 0.0;
        double buildCameraMs = 0.0;
        double prepareCalls = 0.0;
        double prepareFastCalls = 0.0;
        double prepareSlowCalls = 0.0;
        double buildCalls = 0.0;
        double buildCameraCalls = 0.0;
        double renderables = 0.0;
        double visible = 0.0;
        double drawCalls = 0.0;
    };

    [[nodiscard]] const ProfileSnapshot &GetProfileSnapshot() const
    {
        return m_profileSnapshot;
    }

    void ResetProfileSnapshot()
    {
        m_profileSnapshot = {};
    }
#endif

  private:
    void CollectRenderables(uint32_t cullingMask = 0xFFFFFFFF);
    void PerformCulling();
    void SortRenderables();

    /// @brief Fast-path: update world matrices, bounds, optional culling, and cached draw calls in one pass.
    void UpdateCachedRenderableTransforms(bool useActiveCameraCulling);

    /// @brief Shared draw-call emission logic used by both BuildDrawCalls() and BuildDrawCallsForCamera().
    void EmitDrawCallsForRenderable(DrawCallResult &result, const RenderableObject &renderable, bool visible,
                                    bool bufferDirty) const;

    std::vector<RenderableObject> m_renderables;
    size_t m_visibleCount = 0;

    bool m_frustumCulling = true;

    // Cached camera state for the frame
    Camera *m_activeCamera = nullptr;

    // ── Renderable cache ─────────────────────────────────────────────
    // When the renderer set hasn't changed (same MeshRenderers, same
    // enable/disable state), we skip full CollectRenderables/Sort and
    // only update world matrices + bounds in-place.
    uint64_t m_cachedMeshRendererVersion = 0;

    // Draw call cache: reused when renderables are cached.
    DrawCallResult m_cachedDrawCalls;
    bool m_drawCallsCacheValid = false;

    // True after a frustum-culled frame marks some draw calls as invisible.
    // Cleared by a one-time sweep when switching back to non-frustum mode.
    bool m_frustumVisibilityDirty = false;

#if INFERNUX_FRAME_PROFILE
    ProfileSnapshot m_profileSnapshot;
#endif
};

/**
 * @brief Helper to integrate SceneRenderer with existing InxVkCore.
 *
 * This provides the DrawScene callback that reads from SceneManager.
 */
class SceneRenderBridge
{
  public:
    static SceneRenderBridge &Instance();

    SceneRenderBridge(const SceneRenderBridge &) = delete;
    SceneRenderBridge &operator=(const SceneRenderBridge &) = delete;

    /// @brief Get the scene renderer
    [[nodiscard]] SceneRenderer &GetSceneRenderer()
    {
        return m_sceneRenderer;
    }

    /// @brief Update camera data (call before DrawFrame)
    /// Returns camera data in the format expected by InxVkCore::DrawFrame
    void UpdateCameraData(float *outPos, float *outLookAt, float *outUp);

    /// @brief Set aspect ratio from window dimensions
    void OnWindowResize(uint32_t width, uint32_t height);

    /// @brief Prepare editor camera rendering (call once per frame before draw calls)
    void PrepareFrame(bool useActiveCameraCulling = true);

    /// @brief Prepare rendering for a specific camera (independent culling).
    /// @param camera The camera to cull and collect renderables for.
    /// @return DrawCallResult with draw calls visible to this camera.
    [[nodiscard]] DrawCallResult PrepareAndBuildForCamera(Camera *camera);

    /// @brief Build draw calls for a camera reusing the editor camera's renderables.
    /// Avoids re-collecting world matrices, bounds, and materials (from PrepareFrame).
    /// Only re-applies frustum culling and layer filtering for the given camera.
    [[nodiscard]] CameraDrawCallResult CullAndBuildForCamera(Camera *camera, bool includeShadowDrawCalls);

    /// @brief Build draw calls from the current frame's visible renderables.
    /// Delegates to SceneRenderer::BuildDrawCalls().
    [[nodiscard]] const DrawCallResult &BuildDrawCalls();

    /// @brief Get the editor camera.
    [[nodiscard]] Camera *GetEditorCamera() const;

  private:
    SceneRenderBridge() = default;
    ~SceneRenderBridge() = default;

    SceneRenderer m_sceneRenderer;
};

} // namespace infernux
