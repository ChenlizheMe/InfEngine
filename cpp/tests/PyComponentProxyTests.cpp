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

int main()
{
    // Production loads the native runtime from a Python-owned process and
    // never asks pybind11 to finalize CPython. Keep that ownership model here:
    // all local py::objects still destruct before main returns, while the OS
    // tears down the one-shot test interpreter with the process.
    py::initialize_interpreter();
    py::exec(R"PY(
class ContractScheduler:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    @property
    def count(self):
        self.calls += 1
        return self.value
)PY");

    const py::object schedulerType = py::globals()["ContractScheduler"];
    const py::object scheduler = schedulerType(3);
    assert(infernux::PyComponentProxy::ReadCoroutineSchedulerCount(scheduler) == 3);
    assert(scheduler.attr("calls").cast<int>() == 1);

    scheduler.attr("value") = 0;
    assert(infernux::PyComponentProxy::ReadCoroutineSchedulerCount(scheduler) == 0);
    assert(scheduler.attr("calls").cast<int>() == 2);
    assert(infernux::PyComponentProxy::ReadCoroutineSchedulerCount(py::none()) == 0);

    py::exec(R"PY(
from Infernux.components import InxComponent
class ProxyFrameworkEntryProbe(InxComponent):
    _uses_component_data_store = False

    def update(self, delta_time):
        self._native_test_sink("old")

def replacement_update(self, delta_time):
    self._native_test_sink("new")

class CollisionEnterOnlyProbe(InxComponent):
    _uses_component_data_store = False

    def on_collision_enter(self, collision):
        pass
)PY");

    const py::object probeType = py::globals()["ProxyFrameworkEntryProbe"];
    const py::object probe = probeType();
    std::string observed;
    probe.attr("_native_test_sink") = py::cpp_function([&observed](const std::string &value) { observed = value; });
    infernux::PyComponentProxy proxy(probe);

    // The native proxy must retain the framework wrapper, not the user's
    // bound update method.  The wrapper is the stable native entry point.
    const py::object frameworkEntry = probe.attr("_call_update");
    const py::object userEntry = probe.attr("update");
    assert(!frameworkEntry.attr("__func__").is(userEntry.attr("__func__")));

    // Replace and publish the Python body without refreshing the native proxy.
    probeType.attr("update") = py::globals()["replacement_update"];
    const py::object refreshDispatch =
        py::module_::import("Infernux.components._component_lifecycle").attr("refresh_runtime_dispatch_cache");
    refreshDispatch(probeType, py::make_tuple(probe));

    {
        // Mirror the engine's native lifecycle entry: the caller does not own
        // the GIL, and PyComponentProxy acquires it for the Python phase.
        py::gil_scoped_release release;
        proxy.Update(0.5f);
    }
    assert(observed == "new");

    const py::object collisionProbe = py::globals()["CollisionEnterOnlyProbe"]();
    infernux::PyComponentProxy collisionProxy(collisionProbe);
    assert(collisionProxy.WantsPhysicsCallbacks());
    assert(collisionProxy.WantsCollisionEnterCallbacks());
    assert(!collisionProxy.WantsCollisionStayCallbacks());
    assert(!collisionProxy.WantsCollisionExitCallbacks());
    assert(!collisionProxy.WantsTriggerEnterCallbacks());
    assert(!collisionProxy.WantsTriggerStayCallbacks());
    assert(!collisionProxy.WantsTriggerExitCallbacks());

    // A stale Python-side work hint must never freeze ordinary Play frames.
    // The native scene graph remains authoritative when a live Python proxy is
    // present, matching the paused single-step path.
    auto &sceneManager = infernux::SceneManager::Instance();
    infernux::Scene *scene = sceneManager.CreateScene("RuntimeSchedulerFallback");
    infernux::GameObject *owner = scene->CreateGameObject("PythonOwner");
    const py::object runtimeProbe = probeType();
    int nativeProxyUpdates = 0;
    runtimeProbe.attr("_native_test_sink") =
        py::cpp_function([&nativeProxyUpdates](const std::string &) { ++nativeProxyUpdates; });
    assert(owner->AddExistingComponent(std::make_unique<infernux::PyComponentProxy>(runtimeProbe)) != nullptr);

    int runtimeUpdates = 0;
    sceneManager.SetRuntimeLifecycleCallbacks([] {}, [](float) {}, [&runtimeUpdates](float) { ++runtimeUpdates; },
                                              [](float) {}, [](float) {}, [] {});
    sceneManager.SetRuntimeLifecycleWorkAvailable(false);
    sceneManager.Play();
    sceneManager.Update(1.0f / 60.0f);
    sceneManager.LateUpdate(1.0f / 60.0f);
    sceneManager.EndFrame();
    assert(runtimeUpdates == 0);
    assert(nativeProxyUpdates == 1);
    sceneManager.Stop();
    sceneManager.ClearRuntimeLifecycleCallbacks();
    sceneManager.UnloadAllScenes();

    return 0;
}
