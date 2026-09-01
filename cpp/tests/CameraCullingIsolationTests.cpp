#include <function/scene/Camera.h>
#include <function/scene/GameObject.h>
#include <function/scene/LineRenderer.h>
#include <function/scene/MeshRenderer.h>
#include <function/scene/PrimitiveMeshes.h>
#include <function/scene/Scene.h>
#include <function/scene/SceneManager.h>
#include <function/scene/SceneRenderBridge.h>

#include <algorithm>
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

    GameObject *dynamicLineObject = scene->CreateGameObject("WorldSpaceDynamicLine");
    LineRenderer *dynamicLine = dynamicLineObject->AddComponent<LineRenderer>();
    dynamicLine->SetUseWorldSpace(true);
    dynamicLine->SetPositions({glm::vec3(-0.5f, 0.0f, 0.0f), glm::vec3(0.5f, 0.0f, 0.0f)});

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
    assert(leftResult.visibleListRevision != 0);
    assert(rightResult.visibleListRevision != 0);
    assert(leftResult.visibleDrawCallsRef && leftResult.visibleDrawCallsRef->size() == 2);
    assert(rightResult.visibleDrawCallsRef && rightResult.visibleDrawCallsRef->size() == 1);
    assert(std::any_of(leftResult.visibleDrawCallsRef->begin(), leftResult.visibleDrawCallsRef->end(),
                       [leftCube](const DrawCall &draw) { return draw.objectId == leftCube->GetID(); }));
    assert(rightResult.visibleDrawCallsRef->front().objectId == rightCube->GetID());

    // The second camera must not mutate the first camera's cached list.
    assert(std::any_of(leftResult.visibleDrawCallsRef->begin(), leftResult.visibleDrawCallsRef->end(),
                       [leftCube](const DrawCall &draw) { return draw.objectId == leftCube->GetID(); }));
    CameraDrawCallResult leftCachedResult = bridge.CullAndBuildForCamera(leftCamera, false);
    assert(leftCachedResult.visibleDrawCallsRef && leftCachedResult.visibleDrawCallsRef->size() == 2);
    assert(std::any_of(leftCachedResult.visibleDrawCallsRef->begin(), leftCachedResult.visibleDrawCallsRef->end(),
                       [leftCube](const DrawCall &draw) { return draw.objectId == leftCube->GetID(); }));
    assert(leftCachedResult.visibleListRevision == leftResult.visibleListRevision);

    // Skinning changes dynamic draw-call payload without changing camera
    // visibility. A new content publication must still invalidate a graph's
    // cached submission, or Game View will keep the first (bind) pose.
    manager.NotifyMeshRendererContentChanged(leftCube->GetComponent<MeshRenderer>());
    bridge.PrepareFrame(false);
    CameraDrawCallResult leftContentChanged = bridge.CullAndBuildForCamera(leftCamera, false);
    assert(leftContentChanged.visibleDrawCallsRef && leftContentChanged.visibleDrawCallsRef->size() == 2);
    assert(std::any_of(leftContentChanged.visibleDrawCallsRef->begin(), leftContentChanged.visibleDrawCallsRef->end(),
                       [leftCube](const DrawCall &draw) { return draw.objectId == leftCube->GetID(); }));
    assert(leftContentChanged.visibleListRevision != leftCachedResult.visibleListRevision);

    // A procedural renderer can move only its vertices. The unchanged object
    // Transform must not leave the old world bounds in the camera cache.
    dynamicLine->SetPositions({glm::vec3(99.5f, 0.0f, 0.0f), glm::vec3(100.5f, 0.0f, 0.0f)});
    bridge.PrepareFrame(false);
    CameraDrawCallResult rightAfterLineMove = bridge.CullAndBuildForCamera(rightCamera, false);
    assert(rightAfterLineMove.visibleDrawCallsRef);
    assert(
        std::any_of(rightAfterLineMove.visibleDrawCallsRef->begin(), rightAfterLineMove.visibleDrawCallsRef->end(),
                    [dynamicLineObject](const DrawCall &draw) { return draw.objectId == dynamicLineObject->GetID(); }));

    manager.UnloadAllScenes();
    return 0;
}
