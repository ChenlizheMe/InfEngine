#pragma once

#include "SceneRenderExtractor.h"
#include "SceneRenderer.h"

namespace infernux
{

class Camera;

/// Scene-side adapter that publishes extracted frames to the renderer
/// frontend. It is the only owner that connects mutable Scene state to the
/// immutable RenderWorld boundary.
class SceneRenderBridge final
{
  public:
    static SceneRenderBridge &Instance();

    SceneRenderBridge(const SceneRenderBridge &) = delete;
    SceneRenderBridge &operator=(const SceneRenderBridge &) = delete;

    [[nodiscard]] SceneRenderer &GetSceneRenderer()
    {
        return m_sceneRenderer;
    }

    void UpdateCameraData(float *outPos, float *outLookAt, float *outUp);
    void OnWindowResize(uint32_t width, uint32_t height);
    void PrepareFrame(bool useActiveCameraCulling = true);
    [[nodiscard]] DrawCallResult PrepareAndBuildForCamera(Camera *camera);
    [[nodiscard]] CameraDrawCallResult CullAndBuildForCamera(Camera *camera, bool includeShadowDrawCalls);
    [[nodiscard]] const DrawCallResult &BuildDrawCalls();
    [[nodiscard]] Camera *GetEditorCamera() const;

#if INFERNUX_FRAME_PROFILE
    [[nodiscard]] SceneRendererProfileSnapshot GetProfileSnapshot() const;
    void ResetProfileSnapshot();
#endif

  private:
    SceneRenderBridge() = default;
    ~SceneRenderBridge() = default;

    SceneRenderExtractor m_extractor;
    SceneRenderer m_sceneRenderer;
};

} // namespace infernux
