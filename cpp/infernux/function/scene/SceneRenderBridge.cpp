#include "SceneRenderBridge.h"

#include "SceneManager.h"

namespace infernux
{

SceneRenderBridge &SceneRenderBridge::Instance()
{
    // The engine singletons intentionally outlive Python and renderer teardown.
    static SceneRenderBridge *instance = new SceneRenderBridge();
    return *instance;
}

void SceneRenderBridge::UpdateCameraData(float *outPos, float *outLookAt, float *outUp)
{
    const glm::vec3 position = m_sceneRenderer.GetCameraPosition();
    const glm::vec3 lookAt = position + m_sceneRenderer.GetCameraForward();
    const glm::vec3 up = m_sceneRenderer.GetCameraUp();

    if (outPos) {
        outPos[0] = position.x;
        outPos[1] = position.y;
        outPos[2] = position.z;
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
    if (height == 0)
        return;

    m_extractor.SetAspectRatio(static_cast<float>(width) / static_cast<float>(height));
    if (Camera *editorCamera = GetEditorCamera())
        editorCamera->SetScreenDimensions(width, height);
}

void SceneRenderBridge::PrepareFrame(bool useActiveCameraCulling)
{
    m_extractor.SetFrustumCullingEnabled(m_sceneRenderer.IsFrustumCullingEnabled());
    const size_t visible =
        m_extractor.ExtractEditorFrame(m_sceneRenderer.WritableRenderWorld(), useActiveCameraCulling);
    m_sceneRenderer.m_visibleCount.store(visible, std::memory_order_relaxed);
}

DrawCallResult SceneRenderBridge::PrepareAndBuildForCamera(Camera *camera)
{
    SceneRenderExtractor extractor;
    SceneRenderer renderer;
    renderer.SetFrustumCullingEnabled(m_sceneRenderer.IsFrustumCullingEnabled());
    extractor.SetFrustumCullingEnabled(renderer.IsFrustumCullingEnabled());
    const size_t visible = extractor.ExtractCameraFrame(renderer.WritableRenderWorld(), camera);
    renderer.m_visibleCount.store(visible, std::memory_order_relaxed);
    return renderer.BuildDrawCalls();
}

CameraDrawCallResult SceneRenderBridge::CullAndBuildForCamera(Camera *camera, bool includeShadowDrawCalls)
{
    RenderViewData view;
    if (camera) {
        view.view = camera->GetViewMatrix();
        view.projection = camera->GetProjectionMatrix();
        view.viewProjection = camera->GetViewProjectionMatrix();
        view.cullingMask = camera->GetCullingMask();
        view.cameraId = camera->GetComponentID();
        view.valid = true;
        if (GameObject *object = camera->GetGameObject()) {
            if (Transform *transform = object->GetTransform()) {
                view.position = transform->GetWorldPosition();
                view.forward = transform->GetWorldForward();
                view.up = transform->GetWorldUp();
            }
        }
    }
    return m_sceneRenderer.BuildDrawCallsForCamera(view, includeShadowDrawCalls);
}

const DrawCallResult &SceneRenderBridge::BuildDrawCalls()
{
    return m_sceneRenderer.BuildDrawCalls();
}

Camera *SceneRenderBridge::GetEditorCamera() const
{
    return SceneManager::Instance().GetEditorCameraController().GetCamera();
}

#if INFERNUX_FRAME_PROFILE
SceneRendererProfileSnapshot SceneRenderBridge::GetProfileSnapshot() const
{
    SceneRendererProfileSnapshot result = m_extractor.GetProfileSnapshot();
    const SceneRendererProfileSnapshot &renderer = m_sceneRenderer.GetProfileSnapshot();
    result.buildMs += renderer.buildMs;
    result.buildCameraMs += renderer.buildCameraMs;
    result.buildCalls += renderer.buildCalls;
    result.buildCameraCalls += renderer.buildCameraCalls;
    result.drawCalls += renderer.drawCalls;
    return result;
}

void SceneRenderBridge::ResetProfileSnapshot()
{
    m_extractor.ResetProfileSnapshot();
    m_sceneRenderer.ResetProfileSnapshot();
}
#endif

} // namespace infernux
