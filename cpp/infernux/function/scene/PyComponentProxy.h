#pragma once

#include "Component.h"
#include <cstdint>
#include <pybind11/pybind11.h>
#include <string>

namespace py = pybind11;

namespace infernux
{

class Collider;
struct CollisionInfo;

/**
 * @brief C++ proxy component that holds a reference to a Python InxComponent.
 *
 * This class bridges Python-defined components with the C++ update loop.
 * When the C++ Scene calls Update/LateUpdate, this proxy forwards those
 * calls to the corresponding Python methods.
 *
 * Ownership: The PyComponentProxy owns a reference to the Python object.
 * GameObject invokes on_destroy for actual
 * component destruction; replacing a
 * proxy during script reload deliberately skips that user lifecycle callback.
 */
class PyComponentProxy : public Component
{
  public:
    /**
     * @brief Construct a proxy for a Python component.
     * @param pyComponent The Python InxComponent instance
     */
    explicit PyComponentProxy(py::object pyComponent);
    ~PyComponentProxy() override;

    // Non-copyable
    PyComponentProxy(const PyComponentProxy &) = delete;
    PyComponentProxy &operator=(const PyComponentProxy &) = delete;

    // Movable
    PyComponentProxy(PyComponentProxy &&other) noexcept;
    PyComponentProxy &operator=(PyComponentProxy &&other) noexcept;

    // ========================================================================
    // Lifecycle (forwarded to Python)
    // ========================================================================

    void Awake() override;
    void OnEnable() override;
    void Start() override;
    void Update(float deltaTime) override;
    void FixedUpdate(float fixedDeltaTime) override;
    void LateUpdate(float deltaTime) override;
    void TickWhileDisabledUpdate(float deltaTime) override;
    void TickWhileDisabledFixedUpdate(float fixedDeltaTime) override;
    void TickWhileDisabledLateUpdate(float deltaTime) override;
    void OnDisable() override;
    void OnGameObjectDeactivated() override;
    void OnDestroy() override;
    void OnValidate() override;
    void Reset() override;

    // Physics callbacks (forwarded to Python)
    void OnCollisionEnter(const CollisionInfo &collision) override;
    void OnCollisionStay(const CollisionInfo &collision) override;
    void OnCollisionExit(const CollisionInfo &collision) override;
    void OnTriggerEnter(Collider *other) override;
    void OnTriggerStay(Collider *other) override;
    void OnTriggerExit(Collider *other) override;

    // ========================================================================
    // Accessors
    // ========================================================================

    [[nodiscard]] const char *GetTypeName() const override;
    [[nodiscard]] std::string GetConstraintTypeId() const override
    {
        return m_constraintTypeId;
    }
    [[nodiscard]] const ComponentTypeConstraints &GetComponentTypeConstraints() const override
    {
        return m_typeConstraints;
    }

    /// Python components only receive Awake/OnEnable/OnDisable in edit mode
    /// when explicitly decorated with @execute_in_edit_mode, matching the
    /// gating used for per-frame Update/FixedUpdate/LateUpdate.
    [[nodiscard]] bool WantsEditModeLifecycle() const override
    {
        return m_executeInEditMode;
    }

    [[nodiscard]] bool WantsEditModeUpdate() const override
    {
        return m_executeInEditMode;
    }

    [[nodiscard]] bool WantsPhysicsCallbacks() const override
    {
        return true;
    }

    /// Bridge Python @require_component decorator to C++ RequireComponent system.
    [[nodiscard]] std::vector<std::string> GetRequiredComponentTypes() const override;

    /// @brief Get the underlying Python component object
    [[nodiscard]] py::object GetPyComponent() const
    {
        py::gil_scoped_acquire acquire;
        return m_pyComponent;
    }

    /// @brief Check if this proxy holds a valid Python component
    [[nodiscard]] bool IsValid() const
    {
        py::gil_scoped_acquire acquire;
        return !m_pyComponent.is_none();
    }

    /// @brief Get the Python component's type name
    [[nodiscard]] const std::string &GetPyTypeName() const
    {
        return m_typeName;
    }

    /// @brief Get the type GUID for stable serialization (based on module.classname hash)
    [[nodiscard]] const std::string &GetTypeGuid() const
    {
        return m_typeGuid;
    }

    [[nodiscard]] const std::string &GetModuleName() const
    {
        return m_moduleName;
    }

    [[nodiscard]] const std::string &GetQualifiedName() const
    {
        return m_qualifiedName;
    }

    [[nodiscard]] bool OverridesUpdate() const
    {
        return m_overridesUpdate;
    }

    [[nodiscard]] bool HasCoroutineScheduler() const
    {
        return m_hasCoroutineScheduler;
    }

    [[nodiscard]] uint64_t GetUpdateDispatchCount() const
    {
        return m_updateDispatchCount;
    }

    [[nodiscard]] uint64_t GetUpdateForwardCount() const
    {
        return m_updateForwardCount;
    }

    /// Bind a newly published Python mirror and copy the preserved native
    /// lifecycle state into it without invoking user lifecycle methods.
    void RebindPythonMirror();
    void RefreshPythonMirrorIdentity() noexcept;
    void InvalidatePythonMirrorBinding() noexcept;

    // ========================================================================
    // Serialization
    // ========================================================================

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    bool DeserializeDocument(const nlohmann::json &document) override;

    /// @brief PyComponentProxy does not support native Clone (Python objects need
    /// Python-side reconstruction). Returns nullptr.
    [[nodiscard]] std::unique_ptr<Component> Clone() const override;

    /// @brief Serialize only the py_fields portion for native clone pending info.
    [[nodiscard]] nlohmann::json SerializePyFieldsDocument() const;

    /// @brief Get the script GUID associated with this component
    [[nodiscard]] const std::string &GetScriptGuid() const
    {
        return m_scriptGuid;
    }

    /// @brief Set script GUID (used during deserialization)
    void SetScriptGuid(const std::string &guid);

  private:
    void BindPythonMirror();
    void SyncPythonMirror() const;
    void RefreshCoroutineSchedulerFlag();
    void RefreshConstraintTypeId();

    py::object m_pyComponent;
    std::string m_typeName;
    std::string m_typeGuid;   // Stable type GUID (hash of module.classname)
    std::string m_scriptGuid; // Stable GUID for the script asset
    std::string m_moduleName;
    std::string m_qualifiedName;
    std::string m_constraintTypeId;
    ComponentTypeConstraints m_typeConstraints;
    bool m_executeInEditMode = false;
    bool m_overridesUpdate = true;
    bool m_overridesFixedUpdate = true;
    bool m_overridesLateUpdate = true;
    bool m_hasCoroutineScheduler = false;
    uint64_t m_updateDispatchCount = 0;
    uint64_t m_updateForwardCount = 0;
};

} // namespace infernux
