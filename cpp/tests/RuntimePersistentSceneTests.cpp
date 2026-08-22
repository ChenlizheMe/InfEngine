#include <function/scene/Camera.h>
#include <function/scene/Component.h>
#include <function/scene/GameObject.h>
#include <function/scene/Light.h>
#include <function/scene/Scene.h>
#include <function/scene/SceneManager.h>

#include <cassert>

using namespace infernux;

namespace
{
class LifecycleProbe final : public Component
{
  public:
    const char *GetTypeName() const override
    {
        return "RuntimePersistentLifecycleProbe";
    }

    std::string GetConstraintTypeId() const override
    {
        return "test:RuntimePersistentLifecycleProbe";
    }

    const ComponentTypeConstraints &GetComponentTypeConstraints() const override
    {
        static const ComponentTypeConstraints constraints = [] {
            ComponentTypeConstraints value;
            value.userAddable = true;
            return value;
        }();
        return constraints;
    }

    bool WantsRuntimeUpdate() const override
    {
        return true;
    }

    static inline int awakeCount = 0;
    static inline int enableCount = 0;
    static inline int startCount = 0;
    static inline int updateCount = 0;
    static inline int destroyCount = 0;

    void Awake() override
    {
        ++awakeCount;
    }
    void OnEnable() override
    {
        ++enableCount;
    }
    void Start() override
    {
        ++startCount;
    }
    void Update(float) override
    {
        ++updateCount;
    }
    void OnDestroy() override
    {
        ++destroyCount;
    }
};
} // namespace

int main()
{
    SceneManager &manager = SceneManager::Instance();
    manager.Stop();
    manager.UnloadAllScenes();

    Scene *sceneA = manager.CreateScene("PersistentA");
    GameObject *root = sceneA->CreateGameObject("PersistentRoot");
    GameObject *child = sceneA->CreateGameObject("ChildRequest");
    child->SetParent(root, true);
    LifecycleProbe *probe = root->AddComponent<LifecycleProbe>();
    Light *light = root->AddComponent<Light>();
    assert(probe && light);

    const uint64_t rootId = root->GetID();
    const uint64_t childId = child->GetID();
    const uint64_t probeId = probe->GetComponentID();
    manager.Play();
    assert(LifecycleProbe::awakeCount == 1);
    assert(LifecycleProbe::enableCount == 1);
    assert(LifecycleProbe::startCount == 1);
    const size_t lightCount = manager.GetActiveLights().size();

    // A child request promotes the complete root hierarchy at a safe point.
    manager.DontDestroyOnLoad(child);
    manager.PrepareActiveSceneReplacement();
    Scene *persistent = manager.GetRuntimePersistentScene();
    assert(persistent);
    assert(persistent->GetRootObjects().size() == 1);
    assert(persistent->FindByID(rootId) == root);
    assert(persistent->FindByID(childId) == child);
    assert(sceneA->FindByID(rootId) == nullptr);
    assert(root->GetComponent<LifecycleProbe>() == probe);
    assert(probe->GetComponentID() == probeId);
    assert(manager.GetActiveLights().size() == lightCount);

    // Repeated active-scene switches and unloads preserve identity and do not
    // replay lifecycle callbacks or duplicate global component registries.
    Scene *sceneB = manager.CreateScene("PersistentB");
    manager.SetActiveScene(sceneB);
    manager.UnloadScene(sceneA);
    Scene *sceneC = manager.CreateScene("PersistentC");
    manager.SetActiveScene(sceneC);
    manager.UnloadScene(sceneB);
    assert(manager.FindRuntimeObjectByID(rootId) == root);
    assert(root->GetScene() == persistent);
    assert(LifecycleProbe::awakeCount == 1);
    assert(LifecycleProbe::enableCount == 1);
    assert(LifecycleProbe::startCount == 1);
    assert(manager.GetActiveLights().size() == lightCount);

    manager.Update(1.0f / 60.0f);
    manager.LateUpdate(1.0f / 60.0f);
    manager.EndFrame();
    assert(LifecycleProbe::updateCount == 1);

    manager.UnloadAllScenes();
    assert(manager.GetActiveScene() == nullptr);
    assert(manager.FindRuntimeObjectByID(rootId) == root);
    manager.Update(1.0f / 60.0f);
    manager.LateUpdate(1.0f / 60.0f);
    manager.EndFrame();
    assert(LifecycleProbe::updateCount == 2);

    manager.Stop();
    assert(manager.GetRuntimePersistentScene() == nullptr);
    assert(manager.FindRuntimeObjectByID(rootId) == nullptr);
    assert(LifecycleProbe::destroyCount == 1);

    // Edit Mode requests are intentionally non-persistent authored no-ops.
    Scene *editScene = manager.CreateScene("EditScene");
    GameObject *editObject = editScene->CreateGameObject("EditOnly");
    manager.DontDestroyOnLoad(editObject);
    assert(!editObject->IsPersistent());
    assert(manager.GetRuntimePersistentScene() == nullptr);

    // Camera discovery caches use the scene structure revision. Mutations
    // that change camera membership or ordering must invalidate exactly once.
    GameObject *cameraObject = editScene->CreateGameObject("RevisionCamera");
    Camera *camera = cameraObject->AddComponent<Camera>();
    assert(camera);

    uint64_t revision = editScene->GetStructureVersion();
    camera->SetEnabled(false);
    assert(editScene->GetStructureVersion() == revision + 1);
    revision = editScene->GetStructureVersion();
    camera->SetEnabled(false);
    assert(editScene->GetStructureVersion() == revision);

    camera->SetDepth(3.0f);
    assert(editScene->GetStructureVersion() == revision + 1);
    revision = editScene->GetStructureVersion();
    camera->SetDepth(3.0f);
    assert(editScene->GetStructureVersion() == revision);

    editScene->SetMainCamera(camera);
    assert(editScene->GetStructureVersion() == revision + 1);
    revision = editScene->GetStructureVersion();
    editScene->SetMainCamera(camera);
    assert(editScene->GetStructureVersion() == revision);

    manager.UnloadAllScenes();
    return 0;
}
