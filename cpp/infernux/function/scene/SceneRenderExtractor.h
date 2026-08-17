#pragma once

#include "SceneRenderer.h"

#include <memory>
#include <vector>

namespace infernux
{

class Camera;
class MeshRenderer;
class SkinnedMeshRenderer;
class Transform;

/// Scene-side producer for immutable RenderWorld publications. Scene pointers
/// are confined to this type and never cross into SceneRenderer consumers.
class SceneRenderExtractor final
{
  public:
    [[nodiscard]] size_t ExtractEditorFrame(RenderWorldSnapshot &world, bool useActiveCameraCulling);
    [[nodiscard]] size_t ExtractCameraFrame(RenderWorldSnapshot &world, Camera *camera);

    void SetFrustumCullingEnabled(bool enabled)
    {
        m_frustumCulling = enabled;
    }
    [[nodiscard]] bool IsFrustumCullingEnabled() const
    {
        return m_frustumCulling;
    }
    void SetAspectRatio(float aspect);

#if INFERNUX_FRAME_PROFILE
    [[nodiscard]] const SceneRendererProfileSnapshot &GetProfileSnapshot() const
    {
        return m_profileSnapshot;
    }
    void ResetProfileSnapshot()
    {
        m_profileSnapshot = {};
    }
#endif

  private:
    struct SceneRenderSource
    {
        MeshRenderer *meshRenderer = nullptr;
        Transform *transform = nullptr;
        SkinnedMeshRenderer *skinnedRenderer = nullptr;
        std::shared_ptr<const void> inlineMeshOwner;
        const std::vector<Vertex> *inlineVertices = nullptr;
        const std::vector<uint32_t> *inlineIndices = nullptr;
    };

    void CollectRenderables(RenderWorldFrame &frame);
    void CapturePrimaryView(RenderWorldFrame &frame, Camera *camera);
    void PerformCulling(RenderWorldFrame &frame);
    void SortRenderables(RenderWorldFrame &frame);
    void UpdateCachedRenderableTransforms(RenderWorldFrame &frame, bool useActiveCameraCulling);
    void EmitDrawCallsForRenderable(DrawCallResult &result, const RenderProxy &renderable, SceneRenderSource &source,
                                    bool visible, bool bufferDirty) const;
    void RebuildDrawCalls(RenderWorldFrame &frame);

    Camera *m_activeCamera = nullptr;
    std::vector<SceneRenderSource> m_sceneSources;
    size_t m_visibleCount = 0;
    uint64_t m_lastTransformRevision = 0;
    glm::mat4 m_lastViewProjection{1.0f};
    uint32_t m_lastCullingMask = 0xFFFFFFFFu;
    bool m_hasLastFrameState = false;
    bool m_allRenderersStatic = false;
    bool m_lastUsedFrustum = false;
    bool m_frustumCulling = true;
    bool m_frustumVisibilityDirty = false;

#if INFERNUX_FRAME_PROFILE
    SceneRendererProfileSnapshot m_profileSnapshot;
#endif
};

} // namespace infernux
