#include "Component.h"
#include "ComponentFactory.h"
#include "GameObject.h"
#include "Scene.h"
#include "Transform.h"
#include <InxLog.h>
#include <algorithm>
#include <atomic>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace infernux
{

// Static component ID generator
static std::atomic<uint64_t> s_nextComponentID{1};
static std::atomic<uint64_t> s_nextComponentLifetimeGeneration{1};

uint64_t Component::GenerateComponentID()
{
    return s_nextComponentID.fetch_add(1, std::memory_order_relaxed);
}

void Component::EnsureNextComponentID(uint64_t id)
{
    uint64_t next = id + 1;
    uint64_t current = s_nextComponentID.load(std::memory_order_relaxed);
    while (current < next && !s_nextComponentID.compare_exchange_weak(current, next, std::memory_order_relaxed)) {
        // retry with updated current
    }
}

Component::Component()
    : m_wasEnabled(false), m_componentId(GenerateComponentID()),
      m_lifetimeGeneration(s_nextComponentLifetimeGeneration.fetch_add(1, std::memory_order_relaxed))
{
    GetInstanceRegistry()[m_componentId] = this;
}

Component::~Component()
{
    auto &registry = GetInstanceRegistry();
    const auto entry = registry.find(m_componentId);
    if (entry != registry.end() && entry->second == this)
        registry.erase(entry);
}

Component::Component(Component &&other) noexcept
    : m_gameObject(other.m_gameObject), m_enabled(other.m_enabled), m_wasEnabled(other.m_wasEnabled),
      m_hasAwake(other.m_hasAwake), m_hasStarted(other.m_hasStarted), m_hasDestroyed(other.m_hasDestroyed),
      m_isBeingDestroyed(other.m_isBeingDestroyed), m_executionOrder(other.m_executionOrder),
      m_componentId(other.m_componentId), m_lifetimeGeneration(other.m_lifetimeGeneration)
{
    // Update registry to point to this new address
    GetInstanceRegistry()[m_componentId] = this;
    other.m_componentId = 0;
    other.m_lifetimeGeneration = 0;
}

Component &Component::operator=(Component &&other) noexcept
{
    if (this != &other) {
        GetInstanceRegistry().erase(m_componentId);
        m_gameObject = other.m_gameObject;
        m_enabled = other.m_enabled;
        m_wasEnabled = other.m_wasEnabled;
        m_hasAwake = other.m_hasAwake;
        m_hasStarted = other.m_hasStarted;
        m_hasDestroyed = other.m_hasDestroyed;
        m_isBeingDestroyed = other.m_isBeingDestroyed;
        m_executionOrder = other.m_executionOrder;
        m_componentId = other.m_componentId;
        m_lifetimeGeneration = other.m_lifetimeGeneration;
        GetInstanceRegistry()[m_componentId] = this;
        other.m_componentId = 0;
        other.m_lifetimeGeneration = 0;
    }
    return *this;
}

ObjectHandle Component::GetHandle() const
{
    const Scene *scene = m_gameObject ? m_gameObject->GetScene() : nullptr;
    return ObjectHandle{m_componentId, m_lifetimeGeneration, scene ? scene->GetWorldId() : 0};
}

std::string Component::GetConstraintTypeId() const
{
    return std::string("native:") + GetTypeName();
}

const ComponentTypeConstraints &Component::GetComponentTypeConstraints() const
{
    const std::string typeName = GetTypeName();
    if (ComponentFactory::IsRegistered(typeName))
        return ComponentFactory::GetTypeConstraints(typeName);
    static const ComponentTypeConstraints defaults{};
    return defaults;
}

std::vector<std::string> Component::GetRequiredComponentTypes() const
{
    return GetComponentTypeConstraints().requiredTypes;
}

bool Component::IsComponentType(const std::string &typeName) const
{
    if (typeName.empty())
        return false;
    if (typeName == GetConstraintTypeId() || typeName == GetTypeName())
        return true;
    const auto &aliases = GetComponentTypeConstraints().satisfiedTypes;
    return std::find(aliases.begin(), aliases.end(), typeName) != aliases.end();
}

void Component::CallAwake()
{
    if (m_hasAwake || m_hasDestroyed) {
        return;
    }
    m_hasAwake = true;
    Awake();
    // After awake, if enabled AND active in hierarchy, call OnEnable.
    if (m_enabled && m_gameObject && m_gameObject->IsActiveInHierarchy()) {
        CallOnEnable();
    }
}

void Component::CallStart()
{
    if (m_hasStarted || m_hasDestroyed) {
        return;
    }
    m_hasStarted = true;
    Start();
}

void Component::CallOnEnable()
{
    if (m_wasEnabled || m_hasDestroyed) {
        return;
    }
    m_wasEnabled = true;
    OnEnable();
}

void Component::CallOnDisable()
{
    if (!m_wasEnabled || m_hasDestroyed) {
        return;
    }
    m_wasEnabled = false;
    OnDisable();
}

void Component::CallOnDestroy()
{
    if (m_hasDestroyed) {
        return;
    }

    m_isBeingDestroyed = true;

    if (m_wasEnabled) {
        CallOnDisable();
    }

    m_enabled = false;
    m_hasDestroyed = true;
    OnDestroy();
}

void Component::CallOnValidate()
{
    OnValidate();
}

void Component::CallReset()
{
    Reset();
}

void Component::SetEnabled(bool enabled)
{
    if (m_hasDestroyed) {
        return;
    }

    if (m_enabled == enabled) {
        return;
    }

    m_enabled = enabled;

    // Enabled components enter or leave scene queries (camera discovery in
    // particular), so cached query results must observe this state change.
    if (m_gameObject) {
        if (Scene *scene = m_gameObject->GetScene())
            scene->BumpStructureVersion();
    }

    // Unity semantics:
    // - OnEnable fires only when component is enabled and active in hierarchy.
    // - OnDisable fires when transitioning out of that effective-active state.
    // - Start fires once, first time component becomes effectively active in play mode.
    if (!m_hasAwake || !m_gameObject) {
        return;
    }

    Scene *scene = m_gameObject->GetScene();
    if (!scene) {
        return;
    }

    const bool effectiveActive = m_enabled && m_gameObject->IsActiveInHierarchy();
    if (effectiveActive) {
        CallOnEnable();
        if (scene->IsPlaying() && scene->HasStarted()) {
            scene->QueueComponentStart(this);
        }
    } else {
        CallOnDisable();
    }
}

void Component::SetComponentID(uint64_t id)
{
    // Re-key the registry
    GetInstanceRegistry().erase(m_componentId);
    m_componentId = id;
    EnsureNextComponentID(id);
    GetInstanceRegistry()[m_componentId] = this;
}

void Component::ReserveRegistry(size_t n)
{
    GetInstanceRegistry().reserve(n);
}

Component *Component::FindByComponentId(uint64_t id)
{
    auto &reg = GetInstanceRegistry();
    auto it = reg.find(id);
    return it != reg.end() ? it->second : nullptr;
}

std::unordered_map<uint64_t, Component *> &Component::GetInstanceRegistry()
{
    // Intentionally leaked — Component destructors erase from this map and
    // may run late in explicit Cleanup; static teardown order must not matter.
    static auto *s_registry = new std::unordered_map<uint64_t, Component *>();
    return *s_registry;
}

Transform *Component::GetTransform() const
{
    if (m_gameObject) {
        return m_gameObject->GetTransform();
    }
    return nullptr;
}

nlohmann::json Component::SerializeDocument() const
{
    json j;
    j["type"] = GetTypeName();
    j["enabled"] = m_enabled;
    j["execution_order"] = m_executionOrder;
    j["component_id"] = m_componentId;
    return j;
}

std::string Component::Serialize() const
{
    return SerializeDocument().dump(2);
}

bool Component::Deserialize(const std::string &jsonStr)
{
    try {
        return DeserializeDocument(json::parse(jsonStr));
    } catch (const std::exception &e) {
        INXLOG_ERROR("Component::Deserialize failed to parse JSON for '", GetTypeName(), "': ", e.what());
        return false;
    }
}

bool Component::DeserializeDocument(const nlohmann::json &j)
{
    try {
        if (!j.is_object()) {
            INXLOG_ERROR("Component::Deserialize for '", GetTypeName(), "': expected an object document");
            return false;
        }

        if (!j.contains("type") || !j["type"].is_string() || j["type"].get<std::string>() != GetTypeName()) {
            INXLOG_ERROR("Component::Deserialize for '", GetTypeName(), "': document type does not match");
            return false;
        }
        if (!j.contains("enabled") || !j["enabled"].is_boolean() || !j.contains("execution_order") ||
            !j["execution_order"].is_number_integer()) {
            INXLOG_ERROR("Component::Deserialize for '", GetTypeName(), "': missing or invalid base fields");
            return false;
        }
        if (j.contains("instance_guid")) {
            INXLOG_ERROR("Component::Deserialize for '", GetTypeName(),
                         "': instance_guid was removed; use component_id");
            return false;
        }

        m_enabled = j["enabled"].get<bool>();
        m_executionOrder = j["execution_order"].get<int>();
        if (j.contains("component_id")) {
            if (!j["component_id"].is_number_unsigned()) {
                INXLOG_ERROR("Component::Deserialize for '", GetTypeName(), "': component_id must be unsigned");
                return false;
            }
            SetComponentID(j["component_id"].get<uint64_t>());
        }
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("Component::Deserialize failed for '", GetTypeName(), "' (id=", m_componentId, "): ", e.what());
        return false;
    }
}

std::unique_ptr<Component> Component::Clone() const
{
    // Base implementation — should be overridden by every concrete component.
    // Falls back to factory + Serialize/Deserialize for unrecognized types.
    auto clone = ComponentFactory::Create(GetTypeName());
    if (!clone)
        return nullptr;
    // Copy base-class fields (clone already has fresh componentId + instanceGuid)
    clone->m_enabled = m_enabled;
    clone->m_executionOrder = m_executionOrder;
    return clone;
}

} // namespace infernux
