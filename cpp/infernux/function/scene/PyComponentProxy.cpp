#include "PyComponentProxy.h"
#include "Collider.h"
#include "GameObject.h"
#include "physics/PhysicsContactListener.h"
#include <algorithm>
#include <core/log/InxLog.h>
#include <nlohmann/json.hpp>
#include <tools/pybinding/JsonPyBridge.h>

using json = nlohmann::json;

namespace infernux
{

namespace
{
thread_local bool g_pythonLifecyclePhaseActive = false;
}

PyComponentProxy::PythonLifecyclePhaseScope::PythonLifecyclePhaseScope()
{
    if (!g_pythonLifecyclePhaseActive && Py_IsInitialized()) {
        m_acquire.emplace();
        g_pythonLifecyclePhaseActive = true;
    }
}

PyComponentProxy::PythonLifecyclePhaseScope::~PythonLifecyclePhaseScope()
{
    if (m_acquire.has_value())
        g_pythonLifecyclePhaseActive = false;
}

namespace
{
void BindPythonMirrorHelpers(const py::object &pyComponent, Component *nativeComponent, GameObject *gameObject)
{
    if (pyComponent.is_none())
        return;

    if (py::hasattr(pyComponent, "_bind_native_component")) {
        pyComponent.attr("_bind_native_component")(py::cast(nativeComponent, py::return_value_policy::reference),
                                                   gameObject ? py::cast(gameObject, py::return_value_policy::reference)
                                                              : py::none());
        return;
    }

    if (gameObject && py::hasattr(pyComponent, "_set_game_object"))
        pyComponent.attr("_set_game_object")(py::cast(gameObject, py::return_value_policy::reference));
    pyComponent.attr("_cpp_component") = py::cast(nativeComponent, py::return_value_policy::reference);
}

void SyncPythonMirrorState(const py::object &pyComponent, const Component *nativeComponent)
{
    if (pyComponent.is_none() || !nativeComponent)
        return;

    try {
        if (py::hasattr(pyComponent, "_sync_native_state")) {
            pyComponent.attr("_sync_native_state")(nativeComponent->IsEnabled(), nativeComponent->HasAwake(),
                                                   nativeComponent->HasStarted(), nativeComponent->IsDestroyed(),
                                                   nativeComponent->GetExecutionOrder());
            return;
        }

        pyComponent.attr("_component_id") = py::int_(nativeComponent->GetComponentID());
        pyComponent.attr("_execution_order") = py::int_(nativeComponent->GetExecutionOrder());
        pyComponent.attr("_enabled") = py::bool_(nativeComponent->IsEnabled());
        pyComponent.attr("_awake_called") = py::bool_(nativeComponent->HasAwake());
        pyComponent.attr("_has_started") = py::bool_(nativeComponent->HasStarted());
        pyComponent.attr("_is_destroyed") = py::bool_(nativeComponent->IsDestroyed());
    } catch (const py::error_already_set &e) {
        INXLOG_ERROR("[PyComponentProxy] Failed to sync Python mirror state: ", e.what());
    }
}

void SyncEnabledFromPython(const py::object &pyComponent, bool &enabled, const char *phase)
{
    if (!py::hasattr(pyComponent, "enabled")) {
        return;
    }

    try {
        enabled = pyComponent.attr("enabled").cast<bool>();
    } catch (const py::error_already_set &e) {
        INXLOG_ERROR("[PyComponentProxy] Failed to get enabled state",
                     (phase && phase[0] != '\0') ? std::string(" in ") + phase : std::string(), ": ", e.what());
    }

    pyComponent.attr("enabled") = py::bool_(enabled);
}

void CallPythonLifecycleNoArg(const py::object &pyComponent, const std::string &typeName, const char *entryPoint,
                              const char *displayName)
{
    try {
        pyComponent.attr(entryPoint)();
    } catch (const py::error_already_set &e) {
        INXLOG_ERROR("[PyComponentProxy] Error in ", typeName, ".", displayName, "(): ", e.what());
    }
}

void CallPythonLifecycleFloatArg(const py::object &pyComponent, const std::string &typeName, const char *entryPoint,
                                 const char *displayName, float value)
{
    try {
        pyComponent.attr(entryPoint)(value);
    } catch (const py::error_already_set &e) {
        INXLOG_ERROR("[PyComponentProxy] Error in ", typeName, ".", displayName, "(): ", e.what());
    }
}

void CallPythonLifecycleOneArg(const py::object &pyComponent, const std::string &typeName, const char *entryPoint,
                               const char *displayName, py::object arg)
{
    try {
        pyComponent.attr(entryPoint)(std::move(arg));
    } catch (const py::error_already_set &e) {
        INXLOG_ERROR("[PyComponentProxy] Error in ", typeName, ".", displayName, "(): ", e.what());
    }
}

std::string ConstraintTypeName(const py::handle &value)
{
    if (py::isinstance<py::str>(value))
        return py::cast<std::string>(value);
    if (py::hasattr(value, "_cpp_type_name")) {
        const py::object cppTypeName = py::reinterpret_borrow<py::object>(value).attr("_cpp_type_name");
        if (!cppTypeName.is_none()) {
            const std::string name = cppTypeName.cast<std::string>();
            if (!name.empty())
                return "native:" + name;
        }
    }
    if (py::hasattr(value, "_get_type_guid")) {
        const py::object identity = py::reinterpret_borrow<py::object>(value).attr("_get_type_guid")();
        if (!identity.is_none()) {
            const std::string typeGuid = identity.cast<std::string>();
            if (!typeGuid.empty())
                return "python:" + typeGuid;
        }
    }
    if (py::hasattr(value, "__name__"))
        return py::reinterpret_borrow<py::object>(value).attr("__name__").cast<std::string>();
    return {};
}

std::vector<std::string> ConstraintTypeNames(const py::handle &values)
{
    std::vector<std::string> result;
    for (const py::handle value : py::reinterpret_borrow<py::iterable>(values)) {
        std::string name = ConstraintTypeName(value);
        if (!name.empty() && std::find(result.begin(), result.end(), name) == result.end())
            result.push_back(std::move(name));
    }
    return result;
}
} // namespace

PyComponentProxy::PyComponentProxy(py::object pyComponent)
    : m_pyComponent(std::move(pyComponent)), m_typeName("PyComponent")
{
    py::gil_scoped_acquire acquire;
    if (!m_pyComponent.is_none()) {
        try {
            RefreshPythonLifecycleDispatchPlan();

            // Get the Python class name for type identification
            py::object pyType = m_pyComponent.attr("__class__");
            m_typeName = pyType.attr("__name__").cast<std::string>();
            m_moduleName = pyType.attr("__module__").cast<std::string>();
            m_qualifiedName = pyType.attr("__qualname__").cast<std::string>();

            try {
                py::object inxComponentType = py::module_::import("Infernux.components").attr("InxComponent");
                m_overridesUpdate = !pyType.attr("update").is(inxComponentType.attr("update"));
                m_overridesFixedUpdate = !pyType.attr("fixed_update").is(inxComponentType.attr("fixed_update"));
                m_overridesLateUpdate = !pyType.attr("late_update").is(inxComponentType.attr("late_update"));
            } catch (const py::error_already_set &e) {
                INXLOG_WARN("[PyComponentProxy] Failed to inspect lifecycle overrides for '", m_typeName,
                            "': ", e.what());
                m_overridesUpdate = true;
                m_overridesFixedUpdate = true;
                m_overridesLateUpdate = true;
            }

            if (py::hasattr(pyType, "_execute_in_edit_mode_")) {
                try {
                    m_executeInEditMode = pyType.attr("_execute_in_edit_mode_").cast<bool>();
                } catch (const py::error_already_set &e) {
                    INXLOG_WARN("[PyComponentProxy] Failed to read _execute_in_edit_mode_ for '", m_typeName,
                                "': ", e.what());
                    m_executeInEditMode = false;
                }
            }

            // Get stable type GUID from Python class (module.classname hash)
            if (py::hasattr(m_pyComponent, "_get_type_guid")) {
                py::object typeGuid = pyType.attr("_get_type_guid")();
                if (!typeGuid.is_none()) {
                    m_typeGuid = typeGuid.cast<std::string>();
                }
            }

            SyncEnabledFromPython(m_pyComponent, m_enabled, "constructor");

            if (py::hasattr(m_pyComponent, "_script_guid")) {
                py::object guidAttr = m_pyComponent.attr("_script_guid");
                if (!guidAttr.is_none()) {
                    m_scriptGuid = guidAttr.cast<std::string>();
                }
            }

            RefreshConstraintTypeId();
            const bool isMissingScript =
                py::hasattr(m_pyComponent, "_is_broken") && m_pyComponent.attr("_is_broken").cast<bool>();
            if (isMissingScript) {
                // Missing-script placeholders preserve authored data but are
                // never offered as addable component types.
                m_typeConstraints.userAddable = false;
            } else {
                try {
                    const py::object constraints =
                        py::module_::import("Infernux.components.registry").attr("get_component_constraints")(pyType);
                    m_typeConstraints.allowMultiple = constraints.attr("allow_multiple").cast<bool>();
                    m_typeConstraints.userAddable = constraints.attr("user_addable").cast<bool>();
                    m_typeConstraints.removable = constraints.attr("removable").cast<bool>();
                    m_typeConstraints.intrinsic = constraints.attr("intrinsic").cast<bool>();
                    m_typeConstraints.requiredTypes = ConstraintTypeNames(constraints.attr("required_types"));
                    m_typeConstraints.incompatibleTypes = ConstraintTypeNames(constraints.attr("incompatible_types"));
                    m_typeConstraints.exclusiveGroups = ConstraintTypeNames(constraints.attr("exclusive_groups"));
                    m_typeConstraints.satisfiedTypes = ConstraintTypeNames(constraints.attr("satisfied_types"));
                } catch (const py::error_already_set &e) {
                    INXLOG_ERROR("[PyComponentProxy] Failed to resolve component constraints for '", m_typeName,
                                 "': ", e.what());
                    throw;
                }
            }

            RefreshCoroutineSchedulerFlag();
        } catch (const py::error_already_set &e) {
            INXLOG_ERROR("[PyComponentProxy] Failed to get type name: ", e.what());
            throw;
        }
    }
}

PyComponentProxy::~PyComponentProxy()
{
    py::gil_scoped_acquire acquire;
    // Note: OnDestroy is called explicitly before destruction by GameObject
    // Clear reference to allow Python GC
    m_callAwake = py::none();
    m_callStart = py::none();
    m_callUpdate = py::none();
    m_callFixedUpdate = py::none();
    m_callLateUpdate = py::none();
    m_callDisabledUpdate = py::none();
    m_callDisabledFixedUpdate = py::none();
    m_callDisabledLateUpdate = py::none();
    m_callOnEnable = py::none();
    m_callOnDisable = py::none();
    m_callOnDestroy = py::none();
    m_callOnValidate = py::none();
    m_callReset = py::none();
    m_pyComponent = py::none();
}

void CallCachedLifecycleNoArg(const py::object &callable, const std::string &typeName, const char *displayName)
{
    try {
        callable();
    } catch (const py::error_already_set &e) {
        INXLOG_ERROR("[PyComponentProxy] Error in ", typeName, ".", displayName, "(): ", e.what());
    }
}

void CallCachedLifecycleFloatArg(const py::object &callable, const std::string &typeName, const char *displayName,
                                 float value)
{
    try {
        callable(value);
    } catch (const py::error_already_set &e) {
        INXLOG_ERROR("[PyComponentProxy] Error in ", typeName, ".", displayName, "(): ", e.what());
    }
}

PyComponentProxy::PyComponentProxy(PyComponentProxy &&other) noexcept
    : Component(std::move(other)), m_pyComponent(std::move(other.m_pyComponent)),
      m_callAwake(std::move(other.m_callAwake)), m_callStart(std::move(other.m_callStart)),
      m_callUpdate(std::move(other.m_callUpdate)), m_callFixedUpdate(std::move(other.m_callFixedUpdate)),
      m_callLateUpdate(std::move(other.m_callLateUpdate)),
      m_callDisabledUpdate(std::move(other.m_callDisabledUpdate)),
      m_callDisabledFixedUpdate(std::move(other.m_callDisabledFixedUpdate)),
      m_callDisabledLateUpdate(std::move(other.m_callDisabledLateUpdate)),
      m_callOnEnable(std::move(other.m_callOnEnable)), m_callOnDisable(std::move(other.m_callOnDisable)),
      m_callOnDestroy(std::move(other.m_callOnDestroy)), m_callOnValidate(std::move(other.m_callOnValidate)),
      m_callReset(std::move(other.m_callReset)),
      m_typeName(std::move(other.m_typeName)), m_typeGuid(std::move(other.m_typeGuid)),
      m_scriptGuid(std::move(other.m_scriptGuid)), m_moduleName(std::move(other.m_moduleName)),
      m_qualifiedName(std::move(other.m_qualifiedName)), m_constraintTypeId(std::move(other.m_constraintTypeId)),
      m_typeConstraints(std::move(other.m_typeConstraints)), m_executeInEditMode(other.m_executeInEditMode),
      m_overridesUpdate(other.m_overridesUpdate), m_overridesFixedUpdate(other.m_overridesFixedUpdate),
      m_overridesLateUpdate(other.m_overridesLateUpdate), m_hasCoroutineScheduler(other.m_hasCoroutineScheduler),
      m_updateDispatchCount(other.m_updateDispatchCount), m_updateForwardCount(other.m_updateForwardCount)
{
    other.m_pyComponent = py::none();
}

PyComponentProxy &PyComponentProxy::operator=(PyComponentProxy &&other) noexcept
{
    if (this != &other) {
        Component::operator=(std::move(other));
        m_pyComponent = std::move(other.m_pyComponent);
        m_callAwake = std::move(other.m_callAwake);
        m_callStart = std::move(other.m_callStart);
        m_callUpdate = std::move(other.m_callUpdate);
        m_callFixedUpdate = std::move(other.m_callFixedUpdate);
        m_callLateUpdate = std::move(other.m_callLateUpdate);
        m_callDisabledUpdate = std::move(other.m_callDisabledUpdate);
        m_callDisabledFixedUpdate = std::move(other.m_callDisabledFixedUpdate);
        m_callDisabledLateUpdate = std::move(other.m_callDisabledLateUpdate);
        m_callOnEnable = std::move(other.m_callOnEnable);
        m_callOnDisable = std::move(other.m_callOnDisable);
        m_callOnDestroy = std::move(other.m_callOnDestroy);
        m_callOnValidate = std::move(other.m_callOnValidate);
        m_callReset = std::move(other.m_callReset);
        m_typeName = std::move(other.m_typeName);
        m_typeGuid = std::move(other.m_typeGuid);
        m_scriptGuid = std::move(other.m_scriptGuid);
        m_moduleName = std::move(other.m_moduleName);
        m_qualifiedName = std::move(other.m_qualifiedName);
        m_constraintTypeId = std::move(other.m_constraintTypeId);
        m_typeConstraints = std::move(other.m_typeConstraints);
        m_executeInEditMode = other.m_executeInEditMode;
        m_overridesUpdate = other.m_overridesUpdate;
        m_overridesFixedUpdate = other.m_overridesFixedUpdate;
        m_overridesLateUpdate = other.m_overridesLateUpdate;
        m_hasCoroutineScheduler = other.m_hasCoroutineScheduler;
        m_updateDispatchCount = other.m_updateDispatchCount;
        m_updateForwardCount = other.m_updateForwardCount;
        other.m_pyComponent = py::none();
    }
    return *this;
}

void PyComponentProxy::RefreshCoroutineSchedulerFlag()
{
    if (m_pyComponent.is_none()) {
        m_hasCoroutineScheduler = false;
        return;
    }

    try {
        if (!py::hasattr(m_pyComponent, "_coroutine_scheduler")) {
            m_hasCoroutineScheduler = false;
            return;
        }

        const py::object scheduler = m_pyComponent.attr("_coroutine_scheduler");
        // The scheduler object is retained after natural completion and
        // stop_all(). Dispatch is required only while it owns live work.
        m_hasCoroutineScheduler = ReadCoroutineSchedulerCount(scheduler) > 0;
    } catch (const py::error_already_set &e) {
        INXLOG_WARN("[PyComponentProxy] Failed to inspect coroutine scheduler for '", m_typeName, "': ", e.what());
        m_hasCoroutineScheduler = true;
    }
}

std::size_t PyComponentProxy::ReadCoroutineSchedulerCount(const py::handle &scheduler)
{
    if (scheduler.is_none())
        return 0;

    const py::object countMethod = py::reinterpret_borrow<py::object>(scheduler).attr("count");
    return countMethod().cast<std::size_t>();
}

void PyComponentProxy::RefreshPythonLifecycleDispatchPlan()
{
    if (m_pyComponent.is_none()) {
        m_callAwake = py::none();
        m_callStart = py::none();
        m_callUpdate = py::none();
        m_callFixedUpdate = py::none();
        m_callLateUpdate = py::none();
        m_callDisabledUpdate = py::none();
        m_callDisabledFixedUpdate = py::none();
        m_callDisabledLateUpdate = py::none();
        m_callOnEnable = py::none();
        m_callOnDisable = py::none();
        m_callOnDestroy = py::none();
        m_callOnValidate = py::none();
        m_callReset = py::none();
        return;
    }

    // These are framework lifecycle wrappers, not user callbacks. The
    // wrappers resolve the current user method, so replacing a script mirror
    // is the only event that needs to rebuild this plan.
    m_callAwake = m_pyComponent.attr("_call_awake");
    m_callStart = m_pyComponent.attr("_call_start");
    m_callUpdate = m_pyComponent.attr("_call_update");
    m_callFixedUpdate = m_pyComponent.attr("_call_fixed_update");
    m_callLateUpdate = m_pyComponent.attr("_call_late_update");
    m_callDisabledUpdate = m_pyComponent.attr("_tick_coroutines_update");
    m_callDisabledFixedUpdate = m_pyComponent.attr("_tick_coroutines_fixed_update");
    m_callDisabledLateUpdate = m_pyComponent.attr("_tick_coroutines_late_update");
    m_callOnEnable = m_pyComponent.attr("_call_on_enable");
    m_callOnDisable = m_pyComponent.attr("_call_on_disable");
    m_callOnDestroy = m_pyComponent.attr("_call_on_destroy");
    m_callOnValidate = m_pyComponent.attr("_call_on_validate");
    m_callReset = m_pyComponent.attr("_call_reset");
}

void PyComponentProxy::RefreshConstraintTypeId()
{
    m_constraintTypeId = "python:" + (m_typeGuid.empty() ? m_moduleName + "." + m_qualifiedName : m_typeGuid);
}

void PyComponentProxy::BindPythonMirror()
{
    if (m_pyComponent.is_none())
        return;

    BindPythonMirrorHelpers(m_pyComponent, static_cast<Component *>(this), m_gameObject);
    try {
        m_pyComponent.attr("_execute_in_edit_mode") = py::bool_(m_executeInEditMode);
    } catch (const py::error_already_set &e) {
        INXLOG_WARN("[PyComponentProxy] Failed to set _execute_in_edit_mode on '", m_typeName, "': ", e.what());
    }
}

void PyComponentProxy::SyncPythonMirror() const
{
    if (m_pyComponent.is_none())
        return;

    SyncPythonMirrorState(m_pyComponent, this);
}

void PyComponentProxy::RebindPythonMirror()
{
    py::gil_scoped_acquire acquire;
    BindPythonMirror();
    RefreshPythonLifecycleDispatch();
    SyncPythonMirror();
}

void PyComponentProxy::RefreshPythonLifecycleDispatch()
{
    py::gil_scoped_acquire acquire;
    RefreshPythonLifecycleDispatchPlan();
    if (m_pyComponent.is_none()) {
        m_overridesUpdate = false;
        m_overridesFixedUpdate = false;
        m_overridesLateUpdate = false;
        m_hasCoroutineScheduler = false;
        return;
    }

    try {
        const py::object pyType = m_pyComponent.attr("__class__");
        const py::object inxComponentType = py::module_::import("Infernux.components").attr("InxComponent");
        m_overridesUpdate = !pyType.attr("update").is(inxComponentType.attr("update"));
        m_overridesFixedUpdate = !pyType.attr("fixed_update").is(inxComponentType.attr("fixed_update"));
        m_overridesLateUpdate = !pyType.attr("late_update").is(inxComponentType.attr("late_update"));
    } catch (const py::error_already_set &error) {
        INXLOG_WARN("[PyComponentProxy] Failed to refresh lifecycle override mask for '", m_typeName,
                    "': ", error.what());
        m_overridesUpdate = true;
        m_overridesFixedUpdate = true;
        m_overridesLateUpdate = true;
    }
    RefreshCoroutineSchedulerFlag();
}

void PyComponentProxy::RefreshPythonMirrorIdentity() noexcept
{
    try {
        py::gil_scoped_acquire acquire;
        if (!m_pyComponent.is_none())
            m_pyComponent.attr("_component_id") = py::int_(GetComponentID());
        SyncPythonMirror();
        if (!m_pyComponent.is_none() && py::hasattr(m_pyComponent, "_refresh_native_handle"))
            m_pyComponent.attr("_refresh_native_handle")();
    } catch (const py::error_already_set &error) {
        INXLOG_ERROR("[PyComponentProxy] Failed to refresh published Python mirror identity: ", error.what());
    }
}

void PyComponentProxy::InvalidatePythonMirrorBinding() noexcept
{
    try {
        py::gil_scoped_acquire acquire;
        if (m_pyComponent.is_none())
            return;
        if (py::hasattr(m_pyComponent, "_invalidate_native_binding")) {
            m_pyComponent.attr("_invalidate_native_binding")();
            return;
        }
        m_pyComponent.attr("_cpp_component") = py::none();
    } catch (const py::error_already_set &error) {
        INXLOG_ERROR("[PyComponentProxy] Failed to invalidate Python mirror binding: ", error.what());
    }
}

void PyComponentProxy::Awake()
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;

    try {
        BindPythonMirror();
        SyncEnabledFromPython(m_pyComponent, m_enabled, "Awake");

        // Call Python awake
        CallCachedLifecycleNoArg(m_callAwake, m_typeName, "awake");
        RefreshCoroutineSchedulerFlag();
        SyncPythonMirror();
    } catch (const py::error_already_set &e) {
        INXLOG_ERROR("[PyComponentProxy] Error in ", m_typeName, ".awake setup: ", e.what());
    }
}

void PyComponentProxy::OnEnable()
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;

    SyncPythonMirror();
    CallCachedLifecycleNoArg(m_callOnEnable, m_typeName, "on_enable");
    RefreshCoroutineSchedulerFlag();
}

void PyComponentProxy::Start()
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;

    CallCachedLifecycleNoArg(m_callStart, m_typeName, "start");
    RefreshCoroutineSchedulerFlag();
    SyncPythonMirror();
}

void PyComponentProxy::Update(float deltaTime)
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;

    ++m_updateDispatchCount;

    if (!m_overridesUpdate && !m_hasCoroutineScheduler)
        return;

    ++m_updateForwardCount;
    CallCachedLifecycleFloatArg(m_callUpdate, m_typeName, "update", deltaTime);
}

void PyComponentProxy::FixedUpdate(float fixedDeltaTime)
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;

    if (!m_overridesFixedUpdate && !m_hasCoroutineScheduler)
        return;

    CallCachedLifecycleFloatArg(m_callFixedUpdate, m_typeName, "fixed_update", fixedDeltaTime);
}

void PyComponentProxy::LateUpdate(float deltaTime)
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;

    if (!m_overridesLateUpdate && !m_hasCoroutineScheduler)
        return;

    CallCachedLifecycleFloatArg(m_callLateUpdate, m_typeName, "late_update", deltaTime);
}

void PyComponentProxy::TickWhileDisabledUpdate(float deltaTime)
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none() || !m_hasCoroutineScheduler)
        return;

    CallCachedLifecycleFloatArg(m_callDisabledUpdate, m_typeName, "tick_coroutines_update", deltaTime);
}

void PyComponentProxy::TickWhileDisabledFixedUpdate(float fixedDeltaTime)
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none() || !m_hasCoroutineScheduler)
        return;

    CallCachedLifecycleFloatArg(m_callDisabledFixedUpdate, m_typeName, "tick_coroutines_fixed_update", fixedDeltaTime);
}

void PyComponentProxy::TickWhileDisabledLateUpdate(float deltaTime)
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none() || !m_hasCoroutineScheduler)
        return;

    CallCachedLifecycleFloatArg(m_callDisabledLateUpdate, m_typeName, "tick_coroutines_late_update", deltaTime);
}

void PyComponentProxy::OnDisable()
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;

    SyncPythonMirror();
    CallCachedLifecycleNoArg(m_callOnDisable, m_typeName, "on_disable");
}

void PyComponentProxy::OnGameObjectDeactivated()
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;

    CallPythonLifecycleNoArg(m_pyComponent, m_typeName, "_stop_coroutines_for_game_object_deactivate",
                             "stop_coroutines_for_game_object_deactivate");
}

void PyComponentProxy::OnDestroy()
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;

    CallCachedLifecycleNoArg(m_callOnDestroy, m_typeName, "on_destroy");
}

void PyComponentProxy::OnValidate()
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;

    CallCachedLifecycleNoArg(m_callOnValidate, m_typeName, "on_validate");
}

void PyComponentProxy::Reset()
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;

    CallCachedLifecycleNoArg(m_callReset, m_typeName, "reset");
}

// ========================================================================
// Physics callbacks (Unity-style) — forwarded to Python
// ========================================================================

void PyComponentProxy::OnCollisionEnter(const CollisionInfo &collision)
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;
    CallPythonLifecycleOneArg(m_pyComponent, m_typeName, "_call_on_collision_enter", "on_collision_enter",
                              py::cast(collision));
}

void PyComponentProxy::OnCollisionStay(const CollisionInfo &collision)
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;
    CallPythonLifecycleOneArg(m_pyComponent, m_typeName, "_call_on_collision_stay", "on_collision_stay",
                              py::cast(collision));
}

void PyComponentProxy::OnCollisionExit(const CollisionInfo &collision)
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;
    CallPythonLifecycleOneArg(m_pyComponent, m_typeName, "_call_on_collision_exit", "on_collision_exit",
                              py::cast(collision));
}

void PyComponentProxy::OnTriggerEnter(Collider *other)
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;
    CallPythonLifecycleOneArg(m_pyComponent, m_typeName, "_call_on_trigger_enter", "on_trigger_enter",
                              py::cast(other, py::return_value_policy::reference));
}

void PyComponentProxy::OnTriggerStay(Collider *other)
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;
    CallPythonLifecycleOneArg(m_pyComponent, m_typeName, "_call_on_trigger_stay", "on_trigger_stay",
                              py::cast(other, py::return_value_policy::reference));
}

void PyComponentProxy::OnTriggerExit(Collider *other)
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;
    CallPythonLifecycleOneArg(m_pyComponent, m_typeName, "_call_on_trigger_exit", "on_trigger_exit",
                              py::cast(other, py::return_value_policy::reference));
}

const char *PyComponentProxy::GetTypeName() const
{
    return m_typeName.c_str();
}

std::vector<std::string> PyComponentProxy::GetRequiredComponentTypes() const
{
    return m_typeConstraints.requiredTypes;
}

nlohmann::json PyComponentProxy::SerializeDocument() const
{
    py::gil_scoped_acquire acquire;
    if (m_scriptGuid.empty() || m_typeGuid.empty())
        throw std::logic_error("Python component '" + m_typeName + "' has no stable script/type GUID");

    json j = Component::SerializeDocument();
    j["py_type_name"] = m_typeName;
    j["type_guid"] = m_typeGuid; // Stable type GUID for deserialization
    j["execution_order"] = GetExecutionOrder();
    bool enabled = m_enabled;
    if (!m_pyComponent.is_none()) {
        SyncEnabledFromPython(m_pyComponent, enabled, "Serialize");
    }
    j["enabled"] = enabled;
    j["component_id"] = m_componentId;
    j["script_guid"] = m_scriptGuid;

    // Serialize Python component's serializable fields
    if (!m_pyComponent.is_none()) {
        j["py_fields"] = SerializePyFieldsDocument();
    }

    return j;
}

bool PyComponentProxy::DeserializeDocument(const nlohmann::json &j)
{
    if (!Component::DeserializeDocument(j)) {
        return false;
    }

    try {
        if (j.contains("py_type_name")) {
            m_typeName = j["py_type_name"].get<std::string>();
        }
        if (j.contains("script_guid")) {
            m_scriptGuid = j["script_guid"].get<std::string>();
        }
        if (j.contains("type_guid")) {
            m_typeGuid = j["type_guid"].get<std::string>();
        }
        RefreshConstraintTypeId();

        // Python component and fields will be restored by Python side
        // after the C++ scene structure is rebuilt

        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("[PyComponentProxy] Error deserializing: ", e.what());
        return false;
    }
}

void PyComponentProxy::SetScriptGuid(const std::string &guid)
{
    py::gil_scoped_acquire acquire;
    m_scriptGuid = guid;
    RefreshConstraintTypeId();
    if (!m_pyComponent.is_none()) {
        try {
            m_pyComponent.attr("_script_guid") = py::str(guid);
        } catch (const py::error_already_set &e) {
            INXLOG_ERROR("[PyComponentProxy] Failed to set script guid: ", e.what());
        }
    }
}

std::unique_ptr<Component> PyComponentProxy::Clone() const
{
    // Python components cannot be natively cloned in C++.
    // The caller (GameObject::Clone) handles PyComponentProxy by pushing
    // pending info directly into the Scene.
    return nullptr;
}

nlohmann::json PyComponentProxy::SerializePyFieldsDocument() const
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        throw std::logic_error("cannot serialize fields from an unbound Python component proxy");
    if (!py::hasattr(m_pyComponent, "_serialize_fields_document"))
        throw std::logic_error("Python component '" + m_typeName + "' has no _serialize_fields_document method");
    py::object document = m_pyComponent.attr("_serialize_fields_document")();
    if (!py::isinstance<py::dict>(document))
        throw std::invalid_argument("Python component '" + m_typeName +
                                    "' _serialize_fields_document must return dict");
    return PythonToJson(document);
}

} // namespace infernux
