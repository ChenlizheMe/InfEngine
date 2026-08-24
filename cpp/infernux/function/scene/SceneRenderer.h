#pragma once

#include <atomic>
#include <function/renderer/Frustum.h>
#include <function/renderer/InxRenderStruct.h>
#include <function/renderer/ProfileConfig.h>
#include <function/renderer/RenderWorld.h>
#include <memory>
#include <unordered_map>
#include <vector>

namespace infernux
{

class SceneRenderBridge;

struct SceneRendererProfileSnapshot
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
    double cameraCacheHits = 0.0;
    double cameraCacheMisses = 0.0;
    double cameraMissWorld = 0.0;
    double cameraMissStructural = 0.0;
    double cameraMissTransform = 0.0;
    double cameraMissMask = 0.0;
    double cameraMissFrustum = 0.0;
    double cameraMissViewProjection = 0.0;
    double conservativeFullListUses = 0.0;
};

struct CameraDrawCallResult
{
    std::vector<DrawCall> visibleDrawCalls;
    std::vector<DrawCall> shadowDrawCalls;
    const std::vector<DrawCall> *visibleDrawCallsRef = nullptr;
    const std::vector<DrawCall> *shadowDrawCallsRef = nullptr; ///< Zero-copy ref (valid when cullingMask == all)
    std::shared_ptr<const RenderWorldFrame> worldOwner;        ///< Keeps zero-copy data alive across frame publication.
    uint64_t visibleListRevision = 0;
    uint64_t shadowListRevision = 0;
};

/**
 * @brief Renderer frontend consuming immutable scene publications.
 *
 * Scene traversal and mutable Component access
 * live in the scene adapter. This
 * class only acquires immutable RenderWorld publications and derives camera
 *
 * renderer lists from value-type RenderViewData.
 */
class SceneRenderer
{
  public:
    using ProfileSnapshot = SceneRendererProfileSnapshot;

    SceneRenderer() = default;
    ~SceneRenderer() = default;

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

    [[nodiscard]] const RenderWorldSnapshot &GetRenderWorld() const
    {
        return m_renderWorld;
    }

    /// @brief Get number of visible objects after culling
    [[nodiscard]] size_t GetVisibleCount() const
    {
        return m_visibleCount.load(std::memory_order_relaxed);
    }

    /// @brief Build draw calls from visible renderables.
    /// Converts the culled/sorted RenderableObject list into DrawCall + combined vertex/index data.
    /// @note Vertices remain in model/local space; per-object world transform is applied on GPU via push constants.
    [[nodiscard]] const DrawCallResult &BuildDrawCalls();

    /// @brief Build draw calls by re-culling existing renderables against a different camera.
    /// Reuses cached draw-call spans and world bounds from PrepareFrame().
    /// Returns a forward-visible set, plus an optional layer-filtered shadow candidate set.
    [[nodiscard]] CameraDrawCallResult BuildDrawCallsForCamera(const RenderViewData &camera,
                                                               bool includeShadowDrawCalls);

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

#if INFERNUX_FRAME_PROFILE
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
    friend class SceneRenderBridge;

    [[nodiscard]] RenderWorldSnapshot &WritableRenderWorld() noexcept
    {
        return m_renderWorld;
    }

    RenderWorldSnapshot m_renderWorld;
    std::shared_ptr<const RenderWorldFrame> m_buildOwner;
    std::atomic<size_t> m_visibleCount{0};
    struct CameraCullCache
    {
        uint64_t worldId = 0;
        uint64_t structuralRevision = 0;
        uint64_t transformRevision = 0;
        uint64_t contentRevision = 0;
        glm::mat4 viewProjection{1.0f};
        uint32_t cullingMask = 0xFFFFFFFFu;
        bool frustumCulling = true;
        bool usesWorldDrawCalls = false;
        std::vector<DrawCall> visibleDrawCalls;
        std::vector<size_t> visibleDrawCallSourceIndices;
        size_t visibleCount = 0;
        uint64_t visibleListRevision = 0;
        std::shared_ptr<const RenderWorldFrame> worldOwner;
    };
    struct CoarseCullGroup
    {
        size_t proxyStart = 0;
        size_t proxyCount = 0;
        AABB worldBounds;
        uint32_t layerMask = 0;
    };

    void RebuildCoarseCullGroups(const RenderWorldFrame &world);

    std::unordered_map<uint64_t, CameraCullCache> m_cameraCullCaches;
    std::vector<CoarseCullGroup> m_coarseCullGroups;
    uint64_t m_coarseCullWorldId = 0;
    uint64_t m_coarseCullStructuralRevision = 0;
    uint64_t m_coarseCullTransformRevision = 0;
    uint64_t m_nextCameraCullRevision = 1;
#if INFERNUX_FRAME_PROFILE
    ProfileSnapshot m_profileSnapshot;
#endif
    bool m_frustumCulling = true;
};

} // namespace infernux
