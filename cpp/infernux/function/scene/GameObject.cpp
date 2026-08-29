#include "GameObject.h"
#include "Camera.h"
#include "Collider.h"
#include "ComponentFactory.h"
#include "ComponentRecord.h"
#include "MeshRenderer.h"
#include "PyComponentProxy.h"
#include "Rigidbody.h"
#include "Scene.h"
#include "function/audio/AudioSource.h"
#include "physics/PhysicsECSStore.h"
#include "physics/PhysicsWorld.h"
#include <algorithm>
#include <atomic>
#include <core/log/InxLog.h>
#include <functional>
#include <nlohmann/json.hpp>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

namespace py = pybind11;

using json = nlohmann::json;

namespace infernux
{

void InvalidateGameObjectLifecycleCaches(GameObject *gameObject)
{
    if (!gameObject) {
        return;
    }

    gameObject->InvalidateComponentExecutionCache();
    gameObject->RefreshLifecycleDispatchFlags();
}

// Static ID generator
static std::atomic<uint64_t> s_nextID{1};
static std::atomic<uint64_t> s_nextGameObjectLifetimeGeneration{1};

uint64_t GameObject::GenerateID()
{
    return s_nextID.fetch_add(1, std::memory_order_relaxed);
}

void GameObject::EnsureNextID(uint64_t id)
{
    uint64_t next = id + 1;
    uint64_t current = s_nextID.load(std::memory_order_relaxed);
    while (current < next && !s_nextID.compare_exchange_weak(current, next, std::memory_order_relaxed)) {
        // retry with updated current
    }
}

GameObject::GameObject(const std::string &name)
    : m_name(name), m_id(GenerateID()),
      m_lifetimeGeneration(s_nextGameObjectLifetimeGeneration.fetch_add(1, std::memory_order_relaxed))
{
    // Transform is automatically part of GameObject
    m_transform.SetGameObject(this);
}

ObjectHandle GameObject::GetHandle() const
{
    return ObjectHandle{m_id, m_lifetimeGeneration, m_scene ? m_scene->GetWorldId() : 0};
}

void GameObject::SetLayer(int layer)
{
    if (layer < 0 || layer >= 32) {
        throw std::out_of_range("GameObject layer must be in range [0, 31]");
    }
    if (m_layer == layer) {
        return;
    }

    m_layer = layer;
    PhysicsECSStore::Instance().NotifyCollisionSceneChanged();

    auto &physics = PhysicsWorld::Instance();
    if (!physics.IsInitialized()) {
        return;
    }

    const uint32_t bodyId = Collider::GetSharedBodyId(this);
    if (bodyId != 0xFFFFFFFF)
        physics.SetBodyGameLayer(bodyId, m_layer);
}

void GameObject::SetScene(Scene *scene)
{
    m_scene = scene;
    m_transform.SetGameObject(this);
    for (const auto &child : m_children) {
        if (child) {
            child->SetScene(scene);
        }
    }
}

GameObject::~GameObject()
{
    m_isDestroying = true;

    // Unregister self from Scene lookup
    if (m_scene) {
        m_scene->UnregisterGameObject(m_id);
    }

    // Run lifecycle callbacks while all components are still alive.
    // This lets OnDisable/OnDestroy safely call GetComponents<>() on siblings.
    for (auto &comp : m_components) {
        comp->CallOnDestroy();
    }

    // Move components out of the vector before destructors run.
    // During vector::clear(), C++ destructors fire while the vector is
    // partially destroyed — calling GetComponents<>() from a destructor
    // would dynamic_cast on dangling pointers (undefined behaviour).
    // Moving into a local vector ensures m_components is empty so any
    // accidental GetComponents<>() call from a destructor returns [].
    auto dying = std::move(m_components);
    // m_components is now empty — safe for any destructor that reads it.
    dying.clear(); // Destroy unique_ptrs; destructors see empty m_components.
}

bool GameObject::IsActiveInHierarchy() const
{
    if (!m_active)
        return false;
    if (m_parent)
        return m_parent->IsActiveInHierarchy();
    return true;
}

void GameObject::HandleActiveStateChanged(bool wasActiveInHierarchy, bool isActiveInHierarchy)
{
    if (wasActiveInHierarchy == isActiveInHierarchy) {
        return;
    }

    // Awake/OnEnable/OnDisable propagate on effective-active transitions in
    // both play mode and edit mode. Per-frame edit-mode updates remain gated
    // separately via WantsEditModeUpdate().
    bool playing = m_scene && m_scene->IsPlaying();
    if (!m_scene) {
        return;
    }

    if (isActiveInHierarchy) {
        const auto &components = GetComponentsInExecutionOrderCached();
        for (Component *comp : components) {
            if (!comp)
                continue;
            if (!comp->HasAwake()) {
                comp->CallAwake();
            }
            if (comp->IsEnabled()) {
                comp->CallOnEnable();
                if (playing && m_scene->HasStarted()) {
                    m_scene->QueueComponentStart(comp);
                }
            }
        }
    } else {
        const auto &components = GetComponentsInExecutionOrderCached();
        for (Component *comp : components) {
            if (!comp)
                continue;
            comp->OnGameObjectDeactivated();
            if (comp->IsEnabled() && comp->HasAwake()) {
                comp->CallOnDisable();
            }
        }
    }

    for (size_t i = 0; i < m_children.size(); ++i) {
        auto &child = m_children[i];
        if (!child)
            continue;
        bool childWasActive = wasActiveInHierarchy && child->m_active;
        bool childIsActive = isActiveInHierarchy && child->m_active;
        child->HandleActiveStateChanged(childWasActive, childIsActive);
    }
}

std::vector<Component *> GameObject::GetComponentsInExecutionOrder() const
{
    return GetComponentsInExecutionOrderCached();
}

const std::vector<Component *> &GameObject::GetComponentsInExecutionOrderCached() const
{
    if (!m_executionOrderCacheDirty) {
        return m_executionOrderCache;
    }

    m_executionOrderCache.clear();
    m_executionOrderCache.reserve(m_components.size());

    for (const auto &comp : m_components) {
        if (comp) {
            m_executionOrderCache.push_back(comp.get());
        }
    }

    std::stable_sort(m_executionOrderCache.begin(), m_executionOrderCache.end(),
                     [](const Component *a, const Component *b) {
                         if (a->GetExecutionOrder() != b->GetExecutionOrder()) {
                             return a->GetExecutionOrder() < b->GetExecutionOrder();
                         }
                         return a->GetComponentID() < b->GetComponentID();
                     });

    m_executionOrderCacheDirty = false;
    return m_executionOrderCache;
}

void GameObject::InvalidateComponentExecutionCache()
{
    m_executionOrderCacheDirty = true;
}

void GameObject::RefreshLifecycleDispatchFlags()
{
    m_hasPyProxy = false;
    m_hasUpdateReceivers = false;
    m_hasFixedUpdateReceivers = false;
    m_hasLateUpdateReceivers = false;

    for (const auto &component : m_components) {
        if (!component) {
            continue;
        }

        if (dynamic_cast<PyComponentProxy *>(component.get())) {
            m_hasPyProxy = true;
            m_hasUpdateReceivers = true;
            m_hasFixedUpdateReceivers = true;
            m_hasLateUpdateReceivers = true;
            continue;
        }

        if (component->WantsRuntimeUpdate()) {
            m_hasUpdateReceivers = true;
        }
        if (component->WantsRuntimeFixedUpdate())
            m_hasFixedUpdateReceivers = true;
        if (component->WantsRuntimeLateUpdate())
            m_hasLateUpdateReceivers = true;
    }
}

void GameObject::SetActive(bool active)
{
    bool wasActiveInHierarchy = IsActiveInHierarchy();

    if (m_active == active) {
        return;
    }

    m_active = active;

    // Active state participates in hierarchy-derived render and editor caches.
    // Invalidate those caches without requiring per-frame active-state scans.
    if (m_scene) {
        m_scene->BumpStructureVersion();
    }

    bool isActiveInHierarchy = IsActiveInHierarchy();
    HandleActiveStateChanged(wasActiveInHierarchy, isActiveInHierarchy);
}

GameObject *GameObject::GetChild(size_t index) const
{
    if (index < m_children.size()) {
        return m_children[index].get();
    }
    return nullptr;
}

void GameObject::SetParent(GameObject *newParent, bool worldPositionStays)
{
    if (newParent == m_parent)
        return;

    bool wasActiveInHierarchy = IsActiveInHierarchy();

    // Prevent circular reference
    if (newParent) {
        GameObject *ancestor = newParent;
        while (ancestor) {
            if (ancestor == this)
                return; // Cannot be child of own descendant
            ancestor = ancestor->m_parent;
        }
    }

    // Cache world transform before reparenting
    glm::vec3 savedWorldPos;
    glm::quat savedWorldRot;
    glm::vec3 savedWorldScale;
    if (worldPositionStays) {
        savedWorldPos = m_transform.GetWorldPosition();
        savedWorldRot = m_transform.GetWorldRotation();
        savedWorldScale = m_transform.GetWorldScale();
    }

    std::unique_ptr<GameObject> selfPtr;

    // 1. Detach from current owner
    if (m_parent) {
        selfPtr = m_parent->DetachChild(this);
    } else if (m_scene) {
        selfPtr = m_scene->DetachRootObject(this);
    }

    if (!selfPtr) {
        // Should not happen unless object is in limbo state
        return;
    }

    // 2. Attach to new owner
    if (newParent) {
        m_parent = newParent;
        // Ensure scene matches new parent
        if (newParent->m_scene != m_scene) {
            m_scene = newParent->m_scene;
        }
        newParent->AttachChild(std::move(selfPtr));
    } else {
        m_parent = nullptr;
        // Attached to root
        if (m_scene) {
            m_scene->AttachRootObject(std::move(selfPtr));
        }
    }

    // 3. Restore world transform after hierarchy change
    if (worldPositionStays) {
        m_transform.SetWorldPosition(savedWorldPos);
        m_transform.SetWorldRotation(savedWorldRot);
        m_transform.SetWorldScale(savedWorldScale);
    } else {
        // Parent changed without adjusting local values — world matrix is now stale
        m_transform.InvalidateWorldMatrix(true);
    }

    bool isActiveInHierarchy = IsActiveInHierarchy();
    HandleActiveStateChanged(wasActiveInHierarchy, isActiveInHierarchy);
}

std::vector<std::string> GameObject::GetAttachmentBlockers(const std::string &constraintTypeId,
                                                           const std::string &typeName,
                                                           const ComponentTypeConstraints &constraints,
                                                           const Component *replacing, bool enforceUserAddable,
                                                           bool enforceRequirements) const
{
    std::vector<std::string> blockers;
    if (typeName.empty() || constraintTypeId.empty()) {
        blockers.emplace_back("component has no stable registry identity");
        return blockers;
    }
    if (constraintTypeId.rfind("native:", 0) == 0 && !ComponentFactory::IsRegistered(typeName)) {
        blockers.emplace_back("native component type is not registered");
        return blockers;
    }
    if (constraints.intrinsic) {
        blockers.emplace_back("intrinsic components cannot be attached");
        return blockers;
    }
    if (enforceUserAddable && !constraints.userAddable)
        blockers.emplace_back("component is not user-addable");

    if (enforceRequirements) {
        for (const std::string &requiredType : constraints.requiredTypes) {
            bool found = requiredType == "Transform";
            for (const auto &entry : m_components) {
                if (entry && entry.get() != replacing && entry->IsComponentType(requiredType)) {
                    found = true;
                    break;
                }
            }
            if (!found)
                blockers.push_back("requires missing component '" + requiredType + "'");
        }
    }

    const auto candidateSatisfies = [&](const std::string &requestedType) {
        return constraintTypeId == requestedType || typeName == requestedType ||
               std::find(constraints.satisfiedTypes.begin(), constraints.satisfiedTypes.end(), requestedType) !=
                   constraints.satisfiedTypes.end();
    };
    const auto sharesExclusiveGroup = [](const ComponentTypeConstraints &left, const ComponentTypeConstraints &right) {
        return std::any_of(left.exclusiveGroups.begin(), left.exclusiveGroups.end(), [&](const std::string &group) {
            return std::find(right.exclusiveGroups.begin(), right.exclusiveGroups.end(), group) !=
                   right.exclusiveGroups.end();
        });
    };

    for (const auto &entry : m_components) {
        const Component *existing = entry.get();
        if (!existing || existing == replacing)
            continue;
        const auto &existingConstraints = existing->GetComponentTypeConstraints();
        if (!constraints.allowMultiple && existing->GetConstraintTypeId() == constraintTypeId)
            blockers.emplace_back("only one instance is allowed per GameObject");

        if (sharesExclusiveGroup(constraints, existingConstraints)) {
            blockers.push_back("exclusive component group already owned by '" + std::string(existing->GetTypeName()) +
                               "'");
            continue;
        }

        const bool candidateRejectsExisting = std::any_of(
            constraints.incompatibleTypes.begin(), constraints.incompatibleTypes.end(),
            [&](const std::string &incompatibleType) { return existing->IsComponentType(incompatibleType); });
        const bool existingRejectsCandidate =
            std::any_of(existingConstraints.incompatibleTypes.begin(), existingConstraints.incompatibleTypes.end(),
                        candidateSatisfies);
        if (candidateRejectsExisting || existingRejectsCandidate)
            blockers.push_back("incompatible with existing component '" + std::string(existing->GetTypeName()) + "'");
    }

    std::sort(blockers.begin(), blockers.end());
    blockers.erase(std::unique(blockers.begin(), blockers.end()), blockers.end());
    return blockers;
}

std::vector<std::string> GameObject::GetComponentSetBlockers() const
{
    std::vector<const Component *> components;
    components.reserve(m_components.size() + 1);
    components.push_back(&m_transform);
    for (const auto &component : m_components) {
        if (component)
            components.push_back(component.get());
    }

    std::vector<std::string> blockers;
    const auto describe = [](const Component *component) {
        return std::string(component ? component->GetTypeName() : "<null>");
    };
    const auto sharesExclusiveGroup = [](const ComponentTypeConstraints &left, const ComponentTypeConstraints &right) {
        return std::any_of(left.exclusiveGroups.begin(), left.exclusiveGroups.end(), [&](const std::string &group) {
            return std::find(right.exclusiveGroups.begin(), right.exclusiveGroups.end(), group) !=
                   right.exclusiveGroups.end();
        });
    };

    for (size_t index = 0; index < components.size(); ++index) {
        const Component *component = components[index];
        const auto &constraints = component->GetComponentTypeConstraints();
        const std::string identity = component->GetConstraintTypeId();
        if (identity.empty()) {
            blockers.push_back(describe(component) + " has no stable registry identity");
            continue;
        }
        if (component != &m_transform && constraints.intrinsic)
            blockers.push_back(describe(component) + " is intrinsic and cannot occupy a component slot");
        if (!constraints.allowMultiple) {
            const size_t count = static_cast<size_t>(
                std::count_if(components.begin(), components.end(), [&](const Component *candidate) {
                    return candidate && candidate->GetConstraintTypeId() == identity;
                }));
            if (count > 1)
                blockers.push_back(describe(component) + " allows only one instance per GameObject");
        }
        for (const std::string &requiredType : constraints.requiredTypes) {
            const bool found = std::any_of(components.begin(), components.end(), [&](const Component *candidate) {
                return candidate && candidate != component && candidate->IsComponentType(requiredType);
            });
            if (!found)
                blockers.push_back(describe(component) + " requires missing component '" + requiredType + "'");
        }

        for (size_t otherIndex = index + 1; otherIndex < components.size(); ++otherIndex) {
            const Component *other = components[otherIndex];
            const auto &otherConstraints = other->GetComponentTypeConstraints();
            if (sharesExclusiveGroup(constraints, otherConstraints)) {
                blockers.push_back(describe(component) + " and " + describe(other) +
                                   " occupy the same exclusive component group");
                continue;
            }
            const bool componentRejectsOther =
                std::any_of(constraints.incompatibleTypes.begin(), constraints.incompatibleTypes.end(),
                            [&](const std::string &type) { return other->IsComponentType(type); });
            const bool otherRejectsComponent =
                std::any_of(otherConstraints.incompatibleTypes.begin(), otherConstraints.incompatibleTypes.end(),
                            [&](const std::string &type) { return component->IsComponentType(type); });
            if (componentRejectsOther || otherRejectsComponent)
                blockers.push_back(describe(component) + " is incompatible with " + describe(other));
        }
    }

    std::sort(blockers.begin(), blockers.end());
    blockers.erase(std::unique(blockers.begin(), blockers.end()), blockers.end());
    return blockers;
}

Component *GameObject::AttachComponent(std::unique_ptr<Component> component, bool enforceUserAddable)
{
    if (!component)
        return nullptr;
    const auto blockers =
        GetAttachmentBlockers(component->GetConstraintTypeId(), component->GetTypeName(),
                              component->GetComponentTypeConstraints(), nullptr, enforceUserAddable, true);
    if (!blockers.empty()) {
        INXLOG_WARN("Cannot add component '", component->GetTypeName(), "' to GameObject '", m_name,
                    "': ", blockers.front());
        return nullptr;
    }

    Component *ptr = component.get();
    ptr->SetGameObject(this);
    // Publish ownership before binding a Python mirror. The mirror validates
    // its native handle through Scene::ResolveComponent, which must already be
    // able to find the proxy during _bind_native_component callbacks.
    m_components.push_back(std::move(component));
    if (auto *proxy = dynamic_cast<PyComponentProxy *>(ptr)) {
        try {
            proxy->RebindPythonMirror();
        } catch (const std::exception &error) {
            proxy->InvalidatePythonMirrorBinding();
            ptr->SetGameObject(nullptr);
            INXLOG_ERROR("Cannot bind Python component '", ptr->GetTypeName(), "' to GameObject '", m_name,
                         "': ", error.what());
            m_components.pop_back();
            return nullptr;
        }
    }
    PostAddComponent(ptr);
    return ptr;
}

Component *GameObject::AddExistingComponent(std::unique_ptr<Component> component)
{
    return AttachComponent(std::move(component), true);
}

std::vector<uint64_t> GameObject::GetComponentOrder() const
{
    std::vector<uint64_t> result;
    result.reserve(m_components.size());
    for (const auto &component : m_components) {
        if (!component)
            throw std::logic_error("GameObject component storage contains a null entry");
        result.push_back(component->GetComponentID());
    }
    return result;
}

bool GameObject::SetComponentOrder(const std::vector<uint64_t> &componentIds)
{
    if (componentIds.size() != m_components.size())
        return false;

    std::unordered_map<uint64_t, size_t> currentIndices;
    currentIndices.reserve(m_components.size());
    bool changed = false;
    for (size_t index = 0; index < m_components.size(); ++index) {
        const auto &component = m_components[index];
        if (!component || !currentIndices.emplace(component->GetComponentID(), index).second)
            return false;
        changed = changed || componentIds[index] != component->GetComponentID();
    }

    std::unordered_set<uint64_t> requestedIds;
    requestedIds.reserve(componentIds.size());
    for (const uint64_t componentId : componentIds) {
        if (componentId == 0 || !requestedIds.insert(componentId).second ||
            currentIndices.find(componentId) == currentIndices.end()) {
            return false;
        }
    }
    if (!changed)
        return true;

    std::vector<std::unique_ptr<Component>> reordered;
    reordered.reserve(m_components.size());
    for (const uint64_t componentId : componentIds)
        reordered.push_back(std::move(m_components[currentIndices.at(componentId)]));
    m_components = std::move(reordered);

    if (m_scene)
        m_scene->BumpStructureVersion();
    return true;
}

nlohmann::json GameObject::GetDefaultComponentDocument(Component *component) const
{
    if (!component || dynamic_cast<PyComponentProxy *>(component) != nullptr)
        throw std::invalid_argument("default component document requires a native component");
    const bool isTransform = component == &m_transform;
    if (!isTransform && std::none_of(m_components.begin(), m_components.end(),
                                     [component](const auto &candidate) { return candidate.get() == component; })) {
        throw std::invalid_argument("component is not attached to this GameObject");
    }

    std::unique_ptr<Component> defaults =
        isTransform ? std::make_unique<Transform>() : ComponentFactory::Create(component->GetTypeName());
    if (!defaults)
        throw std::runtime_error("component factory cannot construct default document for " +
                                 std::string(component->GetTypeName()));
    nlohmann::json document = defaults->SerializeDocument();
    document["component_id"] = component->GetComponentID();
    document["execution_order"] = component->GetExecutionOrder();
    return document;
}

Component *GameObject::AddPreparedPythonComponent(std::unique_ptr<Component> component, size_t componentIndex)
{
    if (!component || dynamic_cast<PyComponentProxy *>(component.get()) == nullptr)
        throw std::invalid_argument("prepared component must be a PyComponentProxy");

    const auto blockers = GetAttachmentBlockers(component->GetConstraintTypeId(), component->GetTypeName(),
                                                component->GetComponentTypeConstraints(), nullptr, false, false);
    if (!blockers.empty())
        throw std::invalid_argument("prepared Python component rejected: " + blockers.front());
    if (componentIndex > m_components.size())
        throw std::out_of_range("prepared Python component index exceeds component count");

    Component *ptr = component.get();
    ptr->SetGameObject(this);
    auto *proxy = static_cast<PyComponentProxy *>(ptr);
    try {
        proxy->RebindPythonMirror();
    } catch (...) {
        proxy->InvalidatePythonMirrorBinding();
        ptr->SetGameObject(nullptr);
        throw;
    }
    m_components.insert(m_components.begin() + static_cast<std::ptrdiff_t>(componentIndex), std::move(component));
    m_preparedPythonComponents.insert(ptr);
    if (m_scene)
        m_scene->BumpStructureVersion();
    InvalidateComponentExecutionCache();
    RefreshLifecycleDispatchFlags();
    return ptr;
}

void GameObject::ActivatePreparedPythonComponent(Component *component)
{
    if (!component || component->GetGameObject() != this || dynamic_cast<PyComponentProxy *>(component) == nullptr)
        throw std::invalid_argument("component is not a prepared Python proxy owned by this GameObject");
    if (m_preparedPythonComponents.find(component) == m_preparedPythonComponents.end())
        throw std::invalid_argument("Python proxy is not awaiting prepared publication");
    const auto blockers = GetComponentSetBlockers();
    if (!blockers.empty())
        throw std::invalid_argument("prepared component set rejected: " + blockers.front());
    m_preparedPythonComponents.erase(component);
    if (!m_scene || !IsActiveInHierarchy())
        return;

    component->CallAwake();
    if (m_scene->IsPlaying() && m_scene->HasStarted() && component->IsEnabled())
        m_scene->QueueComponentStart(component);
}

bool GameObject::RemovePreparedPythonComponent(Component *component)
{
    if (!component || dynamic_cast<PyComponentProxy *>(component) == nullptr)
        return false;
    if (m_preparedPythonComponents.erase(component) == 0)
        return false;
    for (auto it = m_components.begin(); it != m_components.end(); ++it) {
        if (it->get() != component)
            continue;
        (*it)->CallOnDestroy();
        m_components.erase(it);
        if (m_scene)
            m_scene->BumpStructureVersion();
        InvalidateComponentExecutionCache();
        RefreshLifecycleDispatchFlags();
        return true;
    }
    return false;
}

Component *GameObject::AddComponentByTypeName(const std::string &typeName)
{
    std::unique_ptr<Component> component = ComponentFactory::Create(typeName);
    if (!component)
        return nullptr;
    return AttachComponent(std::move(component), true);
}

std::vector<std::string> GameObject::GetAddComponentBlockers(const std::string &typeName) const
{
    if (!ComponentFactory::IsRegistered(typeName))
        return {"component type is not registered"};
    return GetAttachmentBlockers("native:" + typeName, typeName, ComponentFactory::GetTypeConstraints(typeName),
                                 nullptr, true, true);
}

void GameObject::PostAddComponent(Component *component)
{
    if (!component || !m_scene) {
        return;
    }

    m_scene->BumpStructureVersion();
    InvalidateComponentExecutionCache();
    RefreshLifecycleDispatchFlags();

    // Unity: Reset is editor-only and fires when a component is first added.
    if (!m_scene->IsPlaying())
        component->CallReset();

    // Unity: components added to inactive objects do not Awake until the
    // object first becomes active in the hierarchy.
    if (!IsActiveInHierarchy()) {
        return;
    }

    component->CallAwake();

    // Start is always play-mode only and is deferred until the component's
    // first simulation frame.
    if (m_scene->IsPlaying() && m_scene->HasStarted() && component->IsEnabled() && IsActiveInHierarchy()) {
        m_scene->QueueComponentStart(component);
    }
}

bool GameObject::RemoveComponent(Component *component)
{
    if (!component) {
        return false;
    }

    if (!component->GetComponentTypeConstraints().removable) {
        INXLOG_WARN("Cannot remove non-removable component '", component->GetTypeName(), "' from GameObject '", m_name,
                    "'");
        return false;
    }

    const auto blockers = GetRemovalBlockingComponentTypes(component);
    if (!blockers.empty()) {
        std::string blockerList;
        for (size_t i = 0; i < blockers.size(); ++i) {
            if (i > 0) {
                blockerList += ", ";
            }
            blockerList += blockers[i];
        }
        INXLOG_WARN("Cannot remove component '", component->GetTypeName(), "' from GameObject '", m_name,
                    "' because required by: ", blockerList);
        return false;
    }

    for (auto it = m_components.begin(); it != m_components.end(); ++it) {
        if (it->get() == component) {
            (*it)->CallOnDestroy();
            m_components.erase(it);
            if (m_scene) {
                m_scene->BumpStructureVersion();
            }
            InvalidateComponentExecutionCache();
            RefreshLifecycleDispatchFlags();
            return true;
        }
    }

    return false;
}

Component *GameObject::ReplacePythonComponent(Component *current, std::unique_ptr<Component> replacement)
{
    if (!current || !replacement || dynamic_cast<PyComponentProxy *>(current) == nullptr ||
        dynamic_cast<PyComponentProxy *>(replacement.get()) == nullptr) {
        return nullptr;
    }

    const auto blockers = GetAttachmentBlockers(replacement->GetConstraintTypeId(), replacement->GetTypeName(),
                                                replacement->GetComponentTypeConstraints(), current, false, true);
    if (!blockers.empty()) {
        INXLOG_ERROR("Cannot hot-reload Python component '", replacement->GetTypeName(), "' on GameObject '", m_name,
                     "': ", blockers.front());
        return nullptr;
    }

    for (auto &slot : m_components) {
        if (slot.get() != current) {
            continue;
        }

        // Preserve every externally observable native identity and lifecycle
        // bit. SetComponentID publishes the replacement in the registry before
        // the old proxy is destroyed, so handle resolution never has a gap.
        replacement->SetGameObject(this);
        replacement->m_enabled = current->m_enabled;
        replacement->m_wasEnabled = current->m_wasEnabled;
        replacement->m_hasAwake = current->m_hasAwake;
        replacement->m_hasStarted = current->m_hasStarted;
        replacement->m_hasDestroyed = false;
        replacement->m_isBeingDestroyed = false;
        replacement->m_executionOrder = current->m_executionOrder;
        replacement->m_lifetimeGeneration = current->m_lifetimeGeneration;
        auto *replacementProxy = static_cast<PyComponentProxy *>(replacement.get());
        try {
            replacementProxy->RebindPythonMirror();
        } catch (const std::exception &error) {
            replacementProxy->InvalidatePythonMirrorBinding();
            replacement->SetGameObject(nullptr);
            INXLOG_ERROR("Cannot bind replacement Python component '", replacement->GetTypeName(), "' on GameObject '",
                         m_name, "': ", error.what());
            return nullptr;
        }
        replacement->SetComponentID(current->GetComponentID());

        Component *published = replacement.get();
        slot = std::move(replacement);
        static_cast<PyComponentProxy *>(published)->RefreshPythonMirrorIdentity();

        if (m_scene) {
            m_scene->BumpStructureVersion();
        }
        InvalidateComponentExecutionCache();
        RefreshLifecycleDispatchFlags();
        return published;
    }

    return nullptr;
}

bool GameObject::CanRemoveComponent(Component *component) const
{
    return component && component->GetComponentTypeConstraints().removable &&
           GetRemovalBlockingComponentTypes(component).empty();
}

std::vector<std::string> GameObject::GetRemovalBlockingComponentTypes(Component *component) const
{
    std::vector<std::string> blockers;
    if (!component)
        return blockers;

    // For every OTHER component on this GameObject, check if it declares a
    // requirement that only `component` satisfies.
    for (const auto &other : m_components) {
        if (other.get() == component)
            continue;

        const auto reqs = other->GetRequiredComponentTypes();
        for (const auto &req : reqs) {
            // Does the component being removed satisfy this requirement?
            if (!component->IsComponentType(req))
                continue;

            // It does — check whether any OTHER component also satisfies it.
            bool hasAlternative = false;
            for (const auto &c : m_components) {
                if (c.get() == component)
                    continue; // skip the one being removed
                if (c->IsComponentType(req)) {
                    hasAlternative = true;
                    break;
                }
            }
            if (!hasAlternative) {
                const std::string blockerType = other->GetTypeName();
                if (std::find(blockers.begin(), blockers.end(), blockerType) == blockers.end()) {
                    blockers.push_back(blockerType);
                }
                break;
            }
        }
    }
    return blockers;
}

void GameObject::AttachChild(std::unique_ptr<GameObject> child)
{
    if (!child)
        return;
    child->m_parent = this;
    // Propagate scene
    if (m_scene && child->m_scene != m_scene) {
        child->SetScene(m_scene);
    }
    m_children.push_back(std::move(child));
}

void GameObject::SetChildSiblingIndex(GameObject *child, int newIndex)
{
    int currentIndex = -1;
    for (size_t i = 0; i < m_children.size(); ++i) {
        if (m_children[i].get() == child) {
            currentIndex = static_cast<int>(i);
            break;
        }
    }
    if (currentIndex < 0)
        return;
    newIndex = std::max(0, std::min(newIndex, static_cast<int>(m_children.size()) - 1));
    if (currentIndex == newIndex)
        return;
    auto ptr = std::move(m_children[currentIndex]);
    m_children.erase(m_children.begin() + currentIndex);
    m_children.insert(m_children.begin() + newIndex, std::move(ptr));

    if (m_scene)
        m_scene->BumpStructureVersion();
}

std::unique_ptr<GameObject> GameObject::DetachChild(GameObject *child)
{
    auto it = std::find_if(m_children.begin(), m_children.end(), [&](const auto &ptr) { return ptr.get() == child; });

    if (it != m_children.end()) {
        std::unique_ptr<GameObject> ret = std::move(*it);
        m_children.erase(it);
        if (ret)
            ret->m_parent = nullptr;
        return ret;
    }
    return nullptr;
}

GameObject *GameObject::FindChild(const std::string &name) const
{
    for (const auto &child : m_children) {
        if (child->GetName() == name) {
            return child.get();
        }
    }
    return nullptr;
}

GameObject *GameObject::FindDescendant(const std::string &name) const
{
    // First check direct children
    for (const auto &child : m_children) {
        if (child->GetName() == name) {
            return child.get();
        }
    }

    // Then search recursively
    for (const auto &child : m_children) {
        if (GameObject *found = child->FindDescendant(name)) {
            return found;
        }
    }

    return nullptr;
}

void GameObject::Update(float deltaTime)
{
    if (!m_active || !m_hasUpdateReceivers)
        return;

    const auto &components = GetComponentsInExecutionOrderCached();
    for (Component *comp : components) {
        if (!comp)
            continue;
        // Python proxies are dispatched by SceneManager's shared runtime
        // scheduler when the bridge is active. Native components remain in
        // this traversal, preserving the native scene update path without
        // invoking the same Python proxy a second time.
        if (m_scene && m_scene->UsesRuntimeLifecycleScheduler() && comp->UsesRuntimeLifecycleScheduler())
            continue;
        if (comp->IsEnabled()) {
            comp->Update(deltaTime);
        } else {
            comp->TickWhileDisabledUpdate(deltaTime);
        }
    }
}

void GameObject::FixedUpdate(float fixedDeltaTime)
{
    if (!m_active || !m_hasFixedUpdateReceivers)
        return;

    const auto &components = GetComponentsInExecutionOrderCached();
    for (Component *comp : components) {
        if (!comp)
            continue;
        if (m_scene && m_scene->UsesRuntimeLifecycleScheduler() && comp->UsesRuntimeLifecycleScheduler())
            continue;
        if (comp->IsEnabled()) {
            comp->FixedUpdate(fixedDeltaTime);
        } else {
            comp->TickWhileDisabledFixedUpdate(fixedDeltaTime);
        }
    }
}

void GameObject::LateUpdate(float deltaTime)
{
    if (!m_active || !m_hasLateUpdateReceivers)
        return;

    const auto &components = GetComponentsInExecutionOrderCached();
    for (Component *comp : components) {
        if (!comp)
            continue;
        if (m_scene && m_scene->UsesRuntimeLifecycleScheduler() && comp->UsesRuntimeLifecycleScheduler())
            continue;
        if (comp->IsEnabled()) {
            comp->LateUpdate(deltaTime);
        } else {
            comp->TickWhileDisabledLateUpdate(deltaTime);
        }
    }
}

void GameObject::EditorUpdate(float deltaTime)
{
    if (!m_active)
        return;

    const auto &components = GetComponentsInExecutionOrderCached();
    for (Component *comp : components) {
        if (!comp || !comp->IsEnabled())
            continue;
        if (m_scene && m_scene->UsesRuntimeLifecycleScheduler() && comp->UsesRuntimeLifecycleScheduler())
            continue;
        if (!comp->WantsEditModeUpdate())
            continue;
        if (!comp->HasAwake() && comp->WantsEditModeLifecycle()) {
            comp->CallAwake();
        }
        if (comp->HasAwake()) {
            comp->Update(deltaTime);
        }
    }
}

nlohmann::json GameObject::SerializeDocument() const
{
    json j;
    j["name"] = m_name;
    j["id"] = m_id;
    j["active"] = m_active;
    j["is_static"] = m_isStatic;
    j["tag"] = m_tag;
    j["layer"] = m_layer;

    // Prefab instance tracking (only serialize when set)
    if (!m_prefabGuid.empty()) {
        j["prefab_guid"] = m_prefabGuid;
    }
    if (m_prefabRoot) {
        j["prefab_root"] = true;
    }

    j["transform"] = m_transform.SerializeDocument();

    // Preserve the actual component order across native and Python types.
    json componentsArray = json::array();
    for (const auto &comp : m_components) {
        componentsArray.push_back(SerializeComponentRecord(*comp));
    }
    j["components"] = componentsArray;

    // Serialize children
    json childrenArray = json::array();
    for (const auto &child : m_children) {
        childrenArray.push_back(child->SerializeDocument());
    }
    j["children"] = childrenArray;

    return j;
}

std::string GameObject::Serialize() const
{
    return SerializeDocument().dump(2);
}

bool GameObject::DeserializeDocument(const nlohmann::json &j, bool preserveDocumentIds)
{
    try {
        Scene stagingScene("GameObject document staging");
        auto stagedRoot = stagingScene.BuildGameObjectFromJsonImpl(j, /*preserveIds=*/false);
        if (!stagedRoot)
            return false;
        Scene *targetScene = m_scene;
        GameObject *targetParent = m_parent;

        std::vector<GameObject *> currentObjects{this};
        for (const auto &child : m_children) {
            currentObjects.push_back(child.get());
            child->CollectAllDescendants(currentObjects);
        }
        std::unordered_set<GameObject *> currentObjectSet(currentObjects.begin(), currentObjects.end());
        std::unordered_set<Component *> currentComponentSet;
        for (GameObject *object : currentObjects) {
            currentComponentSet.insert(&object->m_transform);
            for (const auto &component : object->m_components)
                currentComponentSet.insert(component.get());
        }

        struct ComponentIdAssignment
        {
            Component *component = nullptr;
            uint64_t id = 0;
        };
        std::vector<ComponentIdAssignment> componentAssignments;
        std::vector<uint64_t> pythonComponentIds;
        std::unordered_set<uint64_t> objectIds;
        std::unordered_set<uint64_t> componentIds;
        std::unordered_map<uint64_t, uint64_t> stagedToCommittedObjectId;
        uint64_t rootObjectId = m_id;
        uint64_t rootTransformId = m_transform.GetComponentID();

        const auto documentId = [](const json &document, const char *field, uint64_t fallback, const char *context) {
            if (!document.contains(field))
                return fallback;
            if (!document[field].is_number_unsigned() || document[field].get<uint64_t>() == 0)
                throw std::invalid_argument(std::string(context) + " must be a non-zero unsigned integer");
            return document[field].get<uint64_t>();
        };

        std::function<void(GameObject *, const json &, bool)> collectAssignments;
        collectAssignments = [&](GameObject *object, const json &document, bool isRoot) {
            const uint64_t committedObjectId =
                preserveDocumentIds ? documentId(document, "id", isRoot ? m_id : object->m_id, "GameObject id")
                                    : (isRoot ? m_id : object->m_id);
            if (!objectIds.insert(committedObjectId).second)
                throw std::invalid_argument("ObjectGraph contains a duplicate GameObject id");
            stagedToCommittedObjectId.emplace(object->m_id, committedObjectId);
            object->m_id = committedObjectId;
            if (isRoot)
                rootObjectId = committedObjectId;

            const json &transformDocument = document.at("transform");
            const uint64_t committedTransformId =
                preserveDocumentIds
                    ? documentId(transformDocument, "component_id",
                                 isRoot ? m_transform.GetComponentID() : object->m_transform.GetComponentID(),
                                 "Transform component_id")
                    : (isRoot ? m_transform.GetComponentID() : object->m_transform.GetComponentID());
            if (!componentIds.insert(committedTransformId).second)
                throw std::invalid_argument("ObjectGraph contains a duplicate component_id");
            if (isRoot)
                rootTransformId = committedTransformId;
            else
                componentAssignments.push_back({&object->m_transform, committedTransformId});

            size_t nativeComponentIndex = 0;
            for (const auto &componentDocument : document.at("components")) {
                const DecodedComponentRecord record = DecodeComponentRecord(componentDocument);
                if (record.kind == ComponentRecordKind::Python) {
                    if (preserveDocumentIds) {
                        if (!componentIds.insert(record.componentId).second)
                            throw std::invalid_argument("ObjectGraph contains a duplicate component_id");
                        pythonComponentIds.push_back(record.componentId);
                    }
                    continue;
                }
                if (nativeComponentIndex >= object->m_components.size())
                    throw std::logic_error("staged ObjectGraph native component count changed");
                Component *stagedComponent = object->m_components[nativeComponentIndex++].get();
                const uint64_t componentId =
                    preserveDocumentIds ? record.componentId : stagedComponent->GetComponentID();
                if (!componentIds.insert(componentId).second)
                    throw std::invalid_argument("ObjectGraph contains a duplicate component_id");
                componentAssignments.push_back({stagedComponent, componentId});
            }
            if (nativeComponentIndex != object->m_components.size())
                throw std::logic_error("staged ObjectGraph native component count changed");

            const auto &childDocuments = document.at("children");
            if (childDocuments.size() != object->m_children.size())
                throw std::logic_error("staged ObjectGraph child count changed");
            for (size_t index = 0; index < object->m_children.size(); ++index)
                collectAssignments(object->m_children[index].get(), childDocuments[index], false);
        };
        collectAssignments(stagedRoot.get(), j, true);

        if (targetScene) {
            for (const uint64_t objectId : objectIds) {
                GameObject *occupant = targetScene->FindByID(objectId);
                if (occupant && currentObjectSet.find(occupant) == currentObjectSet.end())
                    throw std::invalid_argument(
                        "ObjectGraph GameObject id collides with an object outside the subtree");
            }
        }
        for (const auto &[component, componentId] : componentAssignments) {
            Component *occupant = Component::FindByComponentId(componentId);
            if (occupant && occupant != component && currentComponentSet.find(occupant) == currentComponentSet.end())
                throw std::invalid_argument("ObjectGraph component_id collides with a component outside the subtree");
        }
        if (Component *occupant = Component::FindByComponentId(rootTransformId);
            occupant && occupant != &m_transform && currentComponentSet.find(occupant) == currentComponentSet.end()) {
            throw std::invalid_argument("ObjectGraph Transform component_id collides outside the subtree");
        }
        for (const uint64_t componentId : pythonComponentIds) {
            Component *occupant = Component::FindByComponentId(componentId);
            if (occupant && currentComponentSet.find(occupant) == currentComponentSet.end())
                throw std::invalid_argument(
                    "ObjectGraph Python component_id collides with a component outside the subtree");
        }

        for (auto &pending : stagingScene.m_pendingPyComponents) {
            const auto id = stagedToCommittedObjectId.find(pending.gameObjectId);
            if (id == stagedToCommittedObjectId.end())
                throw std::logic_error("pending Python component targets an unknown staged GameObject");
            pending.gameObjectId = id->second;
        }

        Component::ReserveRegistry(Component::GetInstanceCount() + componentAssignments.size());
        if (targetScene) {
            targetScene->m_objectsById.reserve(targetScene->m_objectsById.size() + objectIds.size());
            targetScene->m_pendingPyComponents.reserve(targetScene->m_pendingPyComponents.size() +
                                                       stagingScene.m_pendingPyComponents.size());
        }

        // Commit starts here. Every document field, factory, component decoder,
        // recursive ID and Python descriptor has already succeeded in staging.
        if (targetScene) {
            for (GameObject *object : currentObjects)
                targetScene->UnregisterGameObject(object->m_id);

            for (GameObject *object : currentObjects) {
                targetScene->m_pendingDestroySet.erase(object->m_id);
                for (const auto &component : object->m_components) {
                    targetScene->m_pendingStartComponentIdSet.erase(component->GetComponentID());
                }
                targetScene->m_pendingStartComponentIdSet.erase(object->m_transform.GetComponentID());
            }
            targetScene->m_pendingDestroy.erase(
                std::remove_if(targetScene->m_pendingDestroy.begin(), targetScene->m_pendingDestroy.end(),
                               [&](uint64_t id) {
                                   return std::any_of(currentObjects.begin(), currentObjects.end(),
                                                      [&](GameObject *object) { return object->m_id == id; });
                               }),
                targetScene->m_pendingDestroy.end());
            targetScene->m_pendingStartComponentIds.erase(
                std::remove_if(
                    targetScene->m_pendingStartComponentIds.begin(), targetScene->m_pendingStartComponentIds.end(),
                    [&](uint64_t id) {
                        return currentComponentSet.find(Component::FindByComponentId(id)) != currentComponentSet.end();
                    }),
                targetScene->m_pendingStartComponentIds.end());
            if (targetScene->m_mainCamera &&
                currentObjectSet.find(targetScene->m_mainCamera->GetGameObject()) != currentObjectSet.end()) {
                targetScene->m_mainCamera = nullptr;
            }
        }

        for (auto &component : m_components)
            component->CallOnDestroy();
        auto oldComponents = std::move(m_components);
        m_children.clear();
        oldComponents.clear();

        m_name = std::move(stagedRoot->m_name);
        m_id = rootObjectId;
        for (const uint64_t objectId : objectIds)
            EnsureNextID(objectId);
        m_active = stagedRoot->m_active;
        m_isStatic = stagedRoot->m_isStatic;
        m_tag = std::move(stagedRoot->m_tag);
        m_layer = stagedRoot->m_layer;
        m_prefabGuid = std::move(stagedRoot->m_prefabGuid);
        m_prefabRoot = stagedRoot->m_prefabRoot;
        m_parent = targetParent;
        m_scene = targetScene;

        stagedRoot->m_transform.CloneDataTo(m_transform);
        m_transform.m_enabled = stagedRoot->m_transform.m_enabled;
        m_transform.m_executionOrder = stagedRoot->m_transform.m_executionOrder;
        m_transform.SetComponentID(rootTransformId);
        m_transform.SetGameObject(this);

        m_components = std::move(stagedRoot->m_components);
        for (auto &component : m_components)
            component->SetGameObject(this);
        m_children = std::move(stagedRoot->m_children);
        for (auto &child : m_children) {
            child->m_parent = this;
            child->SetScene(targetScene);
        }
        for (const auto &[component, componentId] : componentAssignments)
            component->SetComponentID(componentId);

        InvalidateComponentExecutionCache();
        RefreshLifecycleDispatchFlags();
        if (targetScene) {
            targetScene->RegisterObjectSubtree(this);
            for (auto &pending : stagingScene.m_pendingPyComponents)
                targetScene->m_pendingPyComponents.push_back(std::move(pending));
            ++targetScene->m_structureVersion;
        }
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("GameObject::Deserialize staging failed for '", m_name, "' (id=", m_id, "): ", e.what());
        return false;
    }
}

void GameObject::CollectAllDescendants(std::vector<GameObject *> &out) const
{
    for (const auto &child : m_children) {
        out.push_back(child.get());
        child->CollectAllDescendants(out);
    }
}

std::unique_ptr<GameObject> GameObject::Clone(Scene *scene) const
{
    auto obj = std::make_unique<GameObject>(m_name); // fresh ID
    obj->m_scene = scene;
    obj->m_active = m_active;
    obj->m_isStatic = m_isStatic;
    obj->m_tag = m_tag;
    obj->m_layer = m_layer;
    obj->m_prefabGuid = m_prefabGuid;
    obj->m_prefabRoot = m_prefabRoot;

    // Clone transform data (ECS store copy, no JSON)
    m_transform.CloneDataTo(obj->m_transform);

    // Clone components
    for (size_t componentIndex = 0; componentIndex < m_components.size(); ++componentIndex) {
        const auto &comp = m_components[componentIndex];
        const PyComponentProxy *proxy = dynamic_cast<const PyComponentProxy *>(comp.get());
        if (proxy) {
            // Python components → push to Scene pending list (C++ can't clone py::object)
            if (scene) {
                Scene::PendingPyComponent pending;
                pending.gameObjectId = obj->GetID();
                pending.typeName = proxy->GetPyTypeName();
                pending.scriptGuid = proxy->GetScriptGuid();
                pending.typeGuid = proxy->GetTypeGuid();
                pending.enabled = proxy->IsEnabled();
                pending.executionOrder = proxy->GetExecutionOrder();
                pending.componentIndex = componentIndex;
                pending.fieldsDocument = proxy->SerializePyFieldsDocument();
                scene->AddPendingPyComponent(std::move(pending));
            }
        } else {
            auto clonedComp = comp->Clone();
            if (clonedComp) {
                clonedComp->SetGameObject(obj.get());
                obj->m_components.push_back(std::move(clonedComp));
            }
        }
    }

    // Recursively clone children
    for (const auto &child : m_children) {
        auto clonedChild = child->Clone(scene);
        if (clonedChild) {
            obj->AttachChild(std::move(clonedChild));
        }
    }

    return obj;
}

} // namespace infernux
