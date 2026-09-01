#include <function/scene/GameObject.h>
#include <function/scene/PyComponentProxy.h>
#include <function/scene/Scene.h>
#include <function/scene/SceneManager.h>

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <pybind11/embed.h>

namespace py = pybind11;

namespace
{
class NativeUpdateProbe final : public infernux::Component
{
  public:
    explicit NativeUpdateProbe(int &updates) : m_updates(updates)
    {
    }

    void Update(float) override
    {
        ++m_updates;
    }

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "NativeUpdateProbe";
    }

    [[nodiscard]] std::string GetConstraintTypeId() const override
    {
        return "test:native-update-probe";
    }

    [[nodiscard]] bool WantsRuntimeUpdate() const override
    {
        return true;
    }

  private:
    int &m_updates;
};
} // namespace

int main()
{
    py::scoped_interpreter interpreter{};
    py::exec(R"PY(
from Infernux.components import InxComponent
class CollisionEnterOnlyProbe(InxComponent):
    _uses_component_data_store = False

    def on_collision_enter(self, collision):
        pass
)PY");

    const py::object collisionProbe = py::globals()["CollisionEnterOnlyProbe"]();
    infernux::PyComponentProxy collisionProxy(collisionProbe);
    assert(collisionProxy.WantsPhysicsCallbacks());
    assert(collisionProxy.WantsCollisionEnterCallbacks());
    assert(!collisionProxy.WantsCollisionStayCallbacks());
    assert(!collisionProxy.WantsCollisionExitCallbacks());
    assert(!collisionProxy.WantsTriggerEnterCallbacks());
    assert(!collisionProxy.WantsTriggerStayCallbacks());
    assert(!collisionProxy.WantsTriggerExitCallbacks());

    // Python work availability controls only Python callbacks. Native
    // components remain on the native Scene traversal.
    auto &sceneManager = infernux::SceneManager::Instance();
    infernux::Scene *scene = sceneManager.CreateScene("RuntimeSchedulerOwner");
    infernux::GameObject *owner = scene->CreateGameObject("PythonOwner");
    bool missingSchedulerRejected = false;
    try {
        owner->AddExistingComponent(std::make_unique<infernux::PyComponentProxy>(collisionProbe));
    } catch (const std::runtime_error &) {
        missingSchedulerRejected = true;
    }
    assert(missingSchedulerRejected);

    int nativeUpdates = 0;
    assert(owner->AddExistingComponent(std::make_unique<NativeUpdateProbe>(nativeUpdates)) != nullptr);

    int runtimeUpdates = 0;
    sceneManager.SetRuntimeLifecycleCallbacks([] {}, [](float) {}, [&runtimeUpdates](float) { ++runtimeUpdates; },
                                              [](float) {}, [](float) {}, [] {});
    sceneManager.SetRuntimeLifecycleWorkAvailable(false);
    sceneManager.Play();
    sceneManager.Update(1.0f / 60.0f);
    sceneManager.LateUpdate(1.0f / 60.0f);
    sceneManager.EndFrame();
    assert(runtimeUpdates == 0);
    assert(nativeUpdates == 1);
    sceneManager.Stop();

    // The native frame driver consumes the published phase plan. A scheduler
    // may remain installed while a structural rebuild produces an empty phase;
    // that phase must not cross into Python until a newer plan enables it.
    sceneManager.SetRuntimeLifecycleWorkAvailable(true);
    sceneManager.SetRuntimeLifecyclePlan(1, 0, 0, 0);
    sceneManager.Play();
    sceneManager.Update(1.0f / 60.0f);
    sceneManager.LateUpdate(1.0f / 60.0f);
    sceneManager.EndFrame();
    assert(runtimeUpdates == 0);
    sceneManager.SetRuntimeLifecyclePlan(2, 0, 1, 0);
    sceneManager.Update(1.0f / 60.0f);
    sceneManager.LateUpdate(1.0f / 60.0f);
    sceneManager.EndFrame();
    assert(runtimeUpdates == 1);
    sceneManager.Stop();
    sceneManager.ClearRuntimeLifecycleCallbacks();
    sceneManager.UnloadAllScenes();
    return 0;
}
