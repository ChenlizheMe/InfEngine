#include "PyComponentProxy.h"
#include "Collider.h"
#include "GameObject.h"
#include "physics/PhysicsContactListener.h"
#include <core/log/InxLog.h>
#include <nlohmann/json.hpp>
#include <tools/pybinding/JsonPyBridge.h>

using json = nlohmann::json;

namespace infernux
{

namespace
{
void BindPythonMirrorHelpers(const py::object &pyComponent, Component *nativeComponent, GameObject *gameObject)
{
    if (pyComponent.is_none())
        return;

    try {
        if (py::hasattr(pyComponent, "_bind_native_component")) {
            pyComponent.attr("_bind_native_component")(
                py::cast(nativeComponent, py::return_value_policy::reference),
                gameObject ? py::cast(gameObject, py::return_value_policy::reference) : py::none());
            return;
        }

        if (gameObject && py::hasattr(pyComponent, "_set_game_object")) {
            pyComponent.attr("_set_game_object")(py::cast(gameObject, py::return_value_policy::reference));
        }
        pyComponent.attr("_cpp_component") = py::cast(nativeComponent, py::return_value_policy::reference);
    } catch (const py::error_already_set &e) {
        INXLOG_ERROR("[PyComponentProxy] Failed to bind Python mirror: ", e.what());
    }
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
} // namespace

PyComponentProxy::PyComponentProxy(py::object pyComponent)
    : m_pyComponent(std::move(pyComponent)), m_typeName("PyComponent")
{
    py::gil_scoped_acquire acquire;
    if (!m_pyComponent.is_none()) {
        try {
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

            // Inject component ID (generated in Component constructor)
            m_pyComponent.attr("_component_id") = py::int_(m_componentId);

            SyncEnabledFromPython(m_pyComponent, m_enabled, "constructor");

            if (py::hasattr(m_pyComponent, "_script_guid")) {
                py::object guidAttr = m_pyComponent.attr("_script_guid");
                if (!guidAttr.is_none()) {
                    m_scriptGuid = guidAttr.cast<std::string>();
                }
            }

            RefreshCoroutineSchedulerFlag();
        } catch (const py::error_already_set &e) {
            INXLOG_ERROR("[PyComponentProxy] Failed to get type name: ", e.what());
        }
    }
}

PyComponentProxy::~PyComponentProxy()
{
    py::gil_scoped_acquire acquire;
    // Note: OnDestroy is called explicitly before destruction by GameObject
    // Clear reference to allow Python GC
    m_pyComponent = py::none();
}

PyComponentProxy::PyComponentProxy(PyComponentProxy &&other) noexcept
    : Component(std::move(other)), m_pyComponent(std::move(other.m_pyComponent)),
      m_typeName(std::move(other.m_typeName)), m_typeGuid(std::move(other.m_typeGuid)),
      m_scriptGuid(std::move(other.m_scriptGuid)), m_moduleName(std::move(other.m_moduleName)),
      m_qualifiedName(std::move(other.m_qualifiedName)), m_executeInEditMode(other.m_executeInEditMode),
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
        m_typeName = std::move(other.m_typeName);
        m_typeGuid = std::move(other.m_typeGuid);
        m_scriptGuid = std::move(other.m_scriptGuid);
        m_moduleName = std::move(other.m_moduleName);
        m_qualifiedName = std::move(other.m_qualifiedName);
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

        m_hasCoroutineScheduler = !m_pyComponent.attr("_coroutine_scheduler").is_none();
    } catch (const py::error_already_set &e) {
        INXLOG_WARN("[PyComponentProxy] Failed to inspect coroutine scheduler for '", m_typeName, "': ", e.what());
        m_hasCoroutineScheduler = true;
    }
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
    SyncPythonMirror();
    RefreshCoroutineSchedulerFlag();
}

void PyComponentProxy::Awake()
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;

    try {
        BindPythonMirror();
        SyncEnabledFromPython(m_pyComponent, m_enabled, "Awake");

        // Call Python awake
        CallPythonLifecycleNoArg(m_pyComponent, m_typeName, "_call_awake", "awake");
        RefreshCoroutineSchedulerFlag();
        SyncPythonMirror();
    } catch (const py::error_already_set &e) {
        INXLOG_ERROR("[PyComponentProxy] Error in ", m_typeName, ".awake setup: ", e.what());
    }
}

void PyComponentProxy::OnEnable()
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;

    SyncPythonMirror();
    CallPythonLifecycleNoArg(m_pyComponent, m_typeName, "_call_on_enable", "on_enable");
    RefreshCoroutineSchedulerFlag();
}

void PyComponentProxy::Start()
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;

    CallPythonLifecycleNoArg(m_pyComponent, m_typeName, "_call_start", "start");
    RefreshCoroutineSchedulerFlag();
    SyncPythonMirror();
}

void PyComponentProxy::Update(float deltaTime)
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;

    ++m_updateDispatchCount;

    if (!m_overridesUpdate && !m_hasCoroutineScheduler)
        return;

    ++m_updateForwardCount;
    CallPythonLifecycleFloatArg(m_pyComponent, m_typeName, "_call_update", "update", deltaTime);
    RefreshCoroutineSchedulerFlag();
}

void PyComponentProxy::FixedUpdate(float fixedDeltaTime)
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;

    if (!m_overridesFixedUpdate && !m_hasCoroutineScheduler)
        return;

    CallPythonLifecycleFloatArg(m_pyComponent, m_typeName, "_call_fixed_update", "fixed_update", fixedDeltaTime);
    RefreshCoroutineSchedulerFlag();
}

void PyComponentProxy::LateUpdate(float deltaTime)
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;

    if (!m_overridesLateUpdate && !m_hasCoroutineScheduler)
        return;

    CallPythonLifecycleFloatArg(m_pyComponent, m_typeName, "_call_late_update", "late_update", deltaTime);
    RefreshCoroutineSchedulerFlag();
}

void PyComponentProxy::TickWhileDisabledUpdate(float deltaTime)
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none() || !m_hasCoroutineScheduler)
        return;

    CallPythonLifecycleFloatArg(m_pyComponent, m_typeName, "_tick_coroutines_update", "tick_coroutines_update",
                                deltaTime);
    RefreshCoroutineSchedulerFlag();
}

void PyComponentProxy::TickWhileDisabledFixedUpdate(float fixedDeltaTime)
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none() || !m_hasCoroutineScheduler)
        return;

    CallPythonLifecycleFloatArg(m_pyComponent, m_typeName, "_tick_coroutines_fixed_update",
                                "tick_coroutines_fixed_update", fixedDeltaTime);
    RefreshCoroutineSchedulerFlag();
}

void PyComponentProxy::TickWhileDisabledLateUpdate(float deltaTime)
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none() || !m_hasCoroutineScheduler)
        return;

    CallPythonLifecycleFloatArg(m_pyComponent, m_typeName, "_tick_coroutines_late_update",
                                "tick_coroutines_late_update", deltaTime);
    RefreshCoroutineSchedulerFlag();
}

void PyComponentProxy::OnDisable()
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;

    SyncPythonMirror();
    CallPythonLifecycleNoArg(m_pyComponent, m_typeName, "_call_on_disable", "on_disable");
}

void PyComponentProxy::OnGameObjectDeactivated()
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;

    CallPythonLifecycleNoArg(m_pyComponent, m_typeName, "_stop_coroutines_for_game_object_deactivate",
                             "stop_coroutines_for_game_object_deactivate");
}

void PyComponentProxy::OnDestroy()
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;

    CallPythonLifecycleNoArg(m_pyComponent, m_typeName, "_call_on_destroy", "on_destroy");
}

void PyComponentProxy::OnValidate()
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;

    CallPythonLifecycleNoArg(m_pyComponent, m_typeName, "_call_on_validate", "on_validate");
}

void PyComponentProxy::Reset()
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;

    CallPythonLifecycleNoArg(m_pyComponent, m_typeName, "_call_reset", "reset");
}

// ========================================================================
// Physics callbacks (Unity-style) — forwarded to Python
// ========================================================================

void PyComponentProxy::OnCollisionEnter(const CollisionInfo &collision)
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;
    CallPythonLifecycleOneArg(m_pyComponent, m_typeName, "_call_on_collision_enter", "on_collision_enter",
                              py::cast(collision));
}

void PyComponentProxy::OnCollisionStay(const CollisionInfo &collision)
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;
    CallPythonLifecycleOneArg(m_pyComponent, m_typeName, "_call_on_collision_stay", "on_collision_stay",
                              py::cast(collision));
}

void PyComponentProxy::OnCollisionExit(const CollisionInfo &collision)
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;
    CallPythonLifecycleOneArg(m_pyComponent, m_typeName, "_call_on_collision_exit", "on_collision_exit",
                              py::cast(collision));
}

void PyComponentProxy::OnTriggerEnter(Collider *other)
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;
    CallPythonLifecycleOneArg(m_pyComponent, m_typeName, "_call_on_trigger_enter", "on_trigger_enter",
                              py::cast(other, py::return_value_policy::reference));
}

void PyComponentProxy::OnTriggerStay(Collider *other)
{
    py::gil_scoped_acquire acquire;
    if (m_pyComponent.is_none())
        return;
    CallPythonLifecycleOneArg(m_pyComponent, m_typeName, "_call_on_trigger_stay", "on_trigger_stay",
                              py::cast(other, py::return_value_policy::reference));
}

void PyComponentProxy::OnTriggerExit(Collider *other)
{
    py::gil_scoped_acquire acquire;
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
    py::gil_scoped_acquire acquire;
    std::vector<std::string> result;
    if (m_pyComponent.is_none())
        return result;

    try {
        py::object pyType = m_pyComponent.attr("__class__");
        const py::object constraints =
            py::module_::import("Infernux.components.registry").attr("get_component_constraints")(pyType);
        const py::tuple requiredTypes = constraints.attr("required_types").cast<py::tuple>();
        if (requiredTypes.size() > 0) {
            for (auto item : requiredTypes) {
                // Each entry is either a string or a Python type with __name__
                if (py::isinstance<py::str>(item)) {
                    result.push_back(item.cast<std::string>());
                } else if (py::hasattr(item, "__name__")) {
                    result.push_back(item.attr("__name__").cast<std::string>());
                }
            }
        }
    } catch (const py::error_already_set &e) {
        INXLOG_WARN("[PyComponentProxy] Failed to get required components for '", m_typeName, "': ", e.what());
    }
    return result;
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
