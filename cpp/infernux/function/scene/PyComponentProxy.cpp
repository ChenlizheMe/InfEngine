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

    pyComponent.attr("_bind_native_component")(py::cast(nativeComponent, py::return_value_policy::reference),
                                               gameObject ? py::cast(gameObject, py::return_value_policy::reference)
                                                          : py::none());
}

void SyncPythonMirrorState(const py::object &pyComponent, const Component *nativeComponent)
{
    if (pyComponent.is_none() || !nativeComponent)
        return;

    pyComponent.attr("_sync_native_state")(nativeComponent->IsEnabled(), nativeComponent->HasAwake(),
                                           nativeComponent->HasStarted(), nativeComponent->IsDestroyed(),
                                           nativeComponent->GetExecutionOrder());
}

void SyncEnabledFromPython(const py::object &pyComponent, bool &enabled)
{
    enabled = pyComponent.attr("enabled").cast<bool>();
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

            RefreshPythonLifecycleOverrideMask();

            m_executeInEditMode = pyType.attr("_execute_in_edit_mode_").cast<bool>();

            // Get stable type GUID from Python class (module.classname hash)
            m_typeGuid = pyType.attr("_get_type_guid")().cast<std::string>();

            SyncEnabledFromPython(m_pyComponent, m_enabled);

            m_scriptGuid = m_pyComponent.attr("_script_guid").cast<std::string>();

            RefreshConstraintTypeId();
            const bool isMissingScript = m_pyComponent.attr("_is_broken").cast<bool>();
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

PyComponentProxy::PyComponentProxy(PyComponentProxy &&other) noexcept
    : Component(std::move(other)), m_pyComponent(std::move(other.m_pyComponent)),
      m_callAwake(std::move(other.m_callAwake)), m_callStart(std::move(other.m_callStart)),
      m_callOnEnable(std::move(other.m_callOnEnable)), m_callOnDisable(std::move(other.m_callOnDisable)),
      m_callOnDestroy(std::move(other.m_callOnDestroy)), m_callOnValidate(std::move(other.m_callOnValidate)),
      m_callReset(std::move(other.m_callReset)), m_typeName(std::move(other.m_typeName)),
      m_typeGuid(std::move(other.m_typeGuid)), m_scriptGuid(std::move(other.m_scriptGuid)),
      m_moduleName(std::move(other.m_moduleName)), m_qualifiedName(std::move(other.m_qualifiedName)),
      m_constraintTypeId(std::move(other.m_constraintTypeId)), m_typeConstraints(std::move(other.m_typeConstraints)),
      m_executeInEditMode(other.m_executeInEditMode), m_overridesCollisionEnter(other.m_overridesCollisionEnter),
      m_overridesCollisionStay(other.m_overridesCollisionStay),
      m_overridesCollisionExit(other.m_overridesCollisionExit), m_overridesTriggerEnter(other.m_overridesTriggerEnter),
      m_overridesTriggerStay(other.m_overridesTriggerStay), m_overridesTriggerExit(other.m_overridesTriggerExit)
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
        m_overridesCollisionEnter = other.m_overridesCollisionEnter;
        m_overridesCollisionStay = other.m_overridesCollisionStay;
        m_overridesCollisionExit = other.m_overridesCollisionExit;
        m_overridesTriggerEnter = other.m_overridesTriggerEnter;
        m_overridesTriggerStay = other.m_overridesTriggerStay;
        m_overridesTriggerExit = other.m_overridesTriggerExit;
        other.m_pyComponent = py::none();
    }
    return *this;
}

void PyComponentProxy::RefreshPythonLifecycleDispatchPlan()
{
    if (m_pyComponent.is_none()) {
        m_callAwake = py::none();
        m_callStart = py::none();
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
    m_pyComponent.attr("_execute_in_edit_mode") = py::bool_(m_executeInEditMode);
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

void PyComponentProxy::ResetLifecycleForPlay()
{
    py::gil_scoped_acquire acquire;
    m_wasEnabled = false;
    m_hasAwake = false;
    m_hasStarted = false;
    m_hasDestroyed = false;
    m_isBeingDestroyed = false;
    SyncPythonMirror();
}

void PyComponentProxy::RefreshPythonLifecycleDispatch()
{
    py::gil_scoped_acquire acquire;
    RefreshPythonLifecycleDispatchPlan();
    RefreshPythonLifecycleOverrideMask();
}

void PyComponentProxy::RefreshPythonLifecycleOverrideMask()
{
    if (m_pyComponent.is_none()) {
        m_overridesCollisionEnter = false;
        m_overridesCollisionStay = false;
        m_overridesCollisionExit = false;
        m_overridesTriggerEnter = false;
        m_overridesTriggerStay = false;
        m_overridesTriggerExit = false;
        return;
    }

    const py::object pyType = m_pyComponent.attr("__class__");
    const py::object inxComponentType = py::module_::import("Infernux.components").attr("InxComponent");
    m_overridesCollisionEnter = !pyType.attr("on_collision_enter").is(inxComponentType.attr("on_collision_enter"));
    m_overridesCollisionStay = !pyType.attr("on_collision_stay").is(inxComponentType.attr("on_collision_stay"));
    m_overridesCollisionExit = !pyType.attr("on_collision_exit").is(inxComponentType.attr("on_collision_exit"));
    m_overridesTriggerEnter = !pyType.attr("on_trigger_enter").is(inxComponentType.attr("on_trigger_enter"));
    m_overridesTriggerStay = !pyType.attr("on_trigger_stay").is(inxComponentType.attr("on_trigger_stay"));
    m_overridesTriggerExit = !pyType.attr("on_trigger_exit").is(inxComponentType.attr("on_trigger_exit"));
}

void PyComponentProxy::RefreshPythonMirrorIdentity() noexcept
{
    try {
        py::gil_scoped_acquire acquire;
        if (!m_pyComponent.is_none())
            m_pyComponent.attr("_component_id") = py::int_(GetComponentID());
        SyncPythonMirror();
        if (!m_pyComponent.is_none())
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
        m_pyComponent.attr("_invalidate_native_binding")();
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
        SyncEnabledFromPython(m_pyComponent, m_enabled);

        // Call Python awake
        CallCachedLifecycleNoArg(m_callAwake, m_typeName, "awake");
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
}

void PyComponentProxy::Start()
{
    PythonLifecyclePhaseScope acquire;
    if (m_pyComponent.is_none())
        return;

    CallCachedLifecycleNoArg(m_callStart, m_typeName, "start");
    SyncPythonMirror();
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
        SyncEnabledFromPython(m_pyComponent, enabled);
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
