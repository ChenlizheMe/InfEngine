#include <function/scene/Camera.h>
#include <function/scene/GameObject.h>
#include <function/scene/MeshRenderer.h>
#include <function/scene/PrimitiveMeshes.h>
#include <function/scene/Scene.h>
#include <function/scene/SceneManager.h>
#include <function/scene/SceneRenderBridge.h>

#include <cassert>

using namespace infernux;

int main()
{
    SceneManager &manager = SceneManager::Instance();
    manager.Stop();
    manager.UnloadAllScenes();

    Scene *scene = manager.CreateScene("IndependentCameraCulling");
    auto createCube = [scene](const char *name, const glm::vec3 &position) {
        GameObject *object = scene->CreateGameObject(name);
        object->GetTransform()->SetPosition(position);
        MeshRenderer *renderer = object->AddComponent<MeshRenderer>();
        renderer->SetSharedPrimitiveMesh(PrimitiveMeshes::GetCubeVertices(), PrimitiveMeshes::GetCubeIndices(), "Cube");
        return object;
    };
    GameObject *leftCube = createCube("LeftCameraCube", glm::vec3(0.0f, 0.0f, 0.0f));
    GameObject *rightCube = createCube("RightCameraCube", glm::vec3(100.0f, 0.0f, 0.0f));

    GameObject *leftCameraObject = scene->CreateGameObject("LeftCamera");
    leftCameraObject->GetTransform()->SetPosition(glm::vec3(0.0f, 0.0f, -5.0f));
    Camera *leftCamera = leftCameraObject->AddComponent<Camera>();
    leftCamera->SetAspectRatio(1.0f);

    GameObject *rightCameraObject = scene->CreateGameObject("RightCamera");
    rightCameraObject->GetTransform()->SetPosition(glm::vec3(100.0f, 0.0f, -5.0f));
    Camera *rightCamera = rightCameraObject->AddComponent<Camera>();
    rightCamera->SetAspectRatio(1.0f);

    SceneRenderBridge &bridge = SceneRenderBridge::Instance();
    bridge.PrepareFrame(false);
    CameraDrawCallResult leftResult = bridge.CullAndBuildForCamera(leftCamera, false);
    CameraDrawCallResult rightResult = bridge.CullAndBuildForCamera(rightCamera, false);
    assert(leftResult.visibleDrawCallsRef && leftResult.visibleDrawCallsRef->size() == 1);
    assert(rightResult.visibleDrawCallsRef && rightResult.visibleDrawCallsRef->size() == 1);
    assert(leftResult.visibleDrawCallsRef->front().objectId == leftCube->GetID());
    assert(rightResult.visibleDrawCallsRef->front().objectId == rightCube->GetID());

    // The second camera must not mutate the first camera's cached list.
    assert(leftResult.visibleDrawCallsRef->front().objectId == leftCube->GetID());
    CameraDrawCallResult leftCachedResult = bridge.CullAndBuildForCamera(leftCamera, false);
    assert(leftCachedResult.visibleDrawCallsRef && leftCachedResult.visibleDrawCallsRef->size() == 1);
    assert(leftCachedResult.visibleDrawCallsRef->front().objectId == leftCube->GetID());

    manager.UnloadAllScenes();
    return 0;
}
