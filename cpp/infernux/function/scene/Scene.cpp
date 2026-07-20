#include "Scene.h"
#include "Collider.h"
#include "ComponentFactory.h"
#include "ComponentRecord.h"
#include "Light.h"
#include "MeshRenderer.h"
#include "SceneManager.h"
#include "TransformECSStore.h"
#include "core/threading/JobSystem.h"
#include "function/resources/AssetDependencyGraph.h"
#include "platform/filesystem/DocumentStore.h"
#include <SDL3/SDL.h>
#include <algorithm>
#include <atomic>
#include <core/log/InxLog.h>
#include <fstream>
#include <limits>
#include <nlohmann/json.hpp>
#include <numeric>
#include <type_traits>
#include <unordered_set>

using json = nlohmann::json;

namespace infernux
{

static std::atomic<uint64_t> s_nextSceneWorldId{1};

uint64_t Scene::GenerateWorldId()
{
    return s_nextSceneWorldId.fetch_add(1, std::memory_order_relaxed);
}

namespace
{
constexpr size_t kDenseSceneObjectThreshold = 512;
constexpr size_t kParallelSceneRootThreshold = 256;

std::string DumpSceneDocument(const nlohmann::json &document, size_t objectCount)
{
    if (objectCount < kDenseSceneObjectThreshold)
        return document.dump(2);

    // Keep large authored scenes diffable by root object without paying the
    // substantial whitespace cost of recursively pretty-printing every field.
    std::string output;
    output.reserve(objectCount * 1024 + 1024);
    output += "{\n";

    size_t fieldIndex = 0;
    for (const auto &[key, value] : document.items()) {
        output += "  ";
        output += nlohmann::json(key).dump();
        output += ": ";

        if (key == "objects" && value.is_array()) {
            output += "[\n";
            for (size_t objectIndex = 0; objectIndex < value.size(); ++objectIndex) {
                output += "    ";
                output += value[objectIndex].dump();
                if (objectIndex + 1 < value.size())
                    output += ',';
                output += '\n';
            }
            output += "  ]";
        } else {
            output += value.dump();
        }

        if (++fieldIndex < document.size())
            output += ',';
        output += '\n';
    }
    output += '}';
    return output;
}

/// Cheap header validation used before SceneCommitToken moves the live world.
/// Full graph validation still happens inside DeserializeDocument.
bool ValidateSceneDocumentHeader(const nlohmann::json &document)
{
    if (!document.is_object()) {
        INXLOG_ERROR("Scene::Deserialize: expected an object document");
        return false;
    }
    if (!document.contains("name") || !document["name"].is_string() || !document.contains("isPlaying") ||
        !document["isPlaying"].is_boolean() || !document.contains("objects") || !document["objects"].is_array()) {
        INXLOG_ERROR("Scene::Deserialize: scene document is missing required fields");
        return false;
    }
    static const std::unordered_set<std::string> allowedSceneFields = {
        "name",
        "isPlaying",
        "objects",
        "mainCameraComponentId",
    };
    for (const auto &[key, value] : document.items()) {
        (void)value;
        if (allowedSceneFields.find(key) == allowedSceneFields.end()) {
            INXLOG_ERROR("Scene::Deserialize: scene document contains unknown field: ", key);
            return false;
        }
    }
    return true;
}
} // namespace

struct SceneCommitToken::Impl
{
    using ComponentRegistryNode = std::unordered_map<uint64_t, Component *>::node_type;

    Scene *scene = nullptr;
    bool active = true;
    std::string name;
    std::vector<std::unique_ptr<GameObject>> rootObjects;
    std::unordered_map<uint64_t, GameObject *> objectsById;
    std::vector<uint64_t> pendingDestroy;
    std::unordered_set<uint64_t> pendingDestroySet;
    std::vector<uint64_t> pendingStartComponentIds;
    std::unordered_set<uint64_t> pendingStartComponentIdSet;
    std::vector<Scene::PendingPyComponent> pendingPyComponents;
    std::vector<ComponentRegistryNode> componentRegistryNodes;
    std::vector<Collider *> residentColliders;
    Camera *mainCamera = nullptr;
    bool isLoaded = false;
    bool isPlaying = false;
    bool hasStarted = false;
    uint64_t structureVersion = 0;
};

namespace
{
void RestoreSceneComponentRegistries(Scene &scene)
{
    auto &manager = SceneManager::Instance();
    manager.ClearComponentRegistries();
    for (GameObject *object : scene.GetAllObjects()) {
        if (!object || !object->IsActiveInHierarchy())
            continue;
        for (MeshRenderer *renderer : object->GetComponents<MeshRenderer>()) {
            if (renderer && renderer->IsEnabled())
                manager.RegisterMeshRenderer(renderer);
        }
        for (Light *light : object->GetComponents<Light>()) {
            if (light && light->IsEnabled())
                manager.RegisterLight(light);
        }
    }
}
} // namespace

SceneCommitToken::SceneCommitToken(Scene &scene) : m_impl(std::make_unique<Impl>())
{
    Impl &state = *m_impl;
    state.scene = &scene;
    state.name = scene.m_name;
    state.mainCamera = scene.m_mainCamera;
    state.isLoaded = scene.m_isLoaded;
    state.isPlaying = scene.m_isPlaying;
    state.hasStarted = scene.m_hasStarted;
    state.structureVersion = scene.m_structureVersion;

    const std::vector<GameObject *> objects = scene.GetAllObjects();
    std::vector<Component *> components;
    components.reserve(objects.size() * 3);
    state.residentColliders.reserve(objects.size());
    for (GameObject *object : objects) {
        components.push_back(object->GetTransform());
        for (const auto &ownedComponent : object->GetAllComponents()) {
            Component *component = ownedComponent.get();
            if (!component)
                continue;
            components.push_back(component);
            if (auto *collider = dynamic_cast<Collider *>(component))
                state.residentColliders.push_back(collider);
        }
    }
    state.componentRegistryNodes.reserve(components.size());
    auto &registry = Component::GetInstanceRegistry();
    for (Component *component : components) {
        auto node = registry.extract(component->GetComponentID());
        if (node.empty() || node.mapped() != component)
            throw std::logic_error("retained Scene component is missing from the instance registry");
        state.componentRegistryNodes.push_back(std::move(node));
    }

    for (Collider *collider : state.residentColliders)
        collider->SuspendSceneResidency();

    state.rootObjects = std::move(scene.m_rootObjects);
    state.objectsById = std::move(scene.m_objectsById);
    state.pendingDestroy = std::move(scene.m_pendingDestroy);
    state.pendingDestroySet = std::move(scene.m_pendingDestroySet);
    state.pendingStartComponentIds = std::move(scene.m_pendingStartComponentIds);
    state.pendingStartComponentIdSet = std::move(scene.m_pendingStartComponentIdSet);
    state.pendingPyComponents = std::move(scene.m_pendingPyComponents);

    scene.m_mainCamera = nullptr;
    scene.m_hasStarted = false;
}

SceneCommitToken::~SceneCommitToken()
{
    if (IsActive() && !Rollback())
        Finalize();
}

bool SceneCommitToken::IsActive() const noexcept
{
    return m_impl && m_impl->active;
}

bool SceneCommitToken::Rollback()
{
    if (!IsActive())
        return false;

    Impl &state = *m_impl;
    Scene &scene = *state.scene;
    try {
        SceneManager::Instance().ClearComponentRegistries();
        scene.m_mainCamera = nullptr;
        scene.m_rootObjects.clear();
        scene.m_objectsById.clear();
        scene.m_pendingDestroy.clear();
        scene.m_pendingDestroySet.clear();
        scene.m_pendingStartComponentIds.clear();
        scene.m_pendingStartComponentIdSet.clear();
        scene.m_pendingPyComponents.clear();

        auto &registry = Component::GetInstanceRegistry();
        registry.reserve(registry.size() + state.componentRegistryNodes.size());
        for (auto &node : state.componentRegistryNodes) {
            const auto result = registry.insert(std::move(node));
            if (!result.inserted)
                throw std::logic_error("retained Scene component ID collided during rollback");
        }

        scene.m_name = std::move(state.name);
        scene.m_rootObjects = std::move(state.rootObjects);
        scene.m_objectsById = std::move(state.objectsById);
        scene.m_pendingDestroy = std::move(state.pendingDestroy);
        scene.m_pendingDestroySet = std::move(state.pendingDestroySet);
        scene.m_pendingStartComponentIds = std::move(state.pendingStartComponentIds);
        scene.m_pendingStartComponentIdSet = std::move(state.pendingStartComponentIdSet);
        scene.m_pendingPyComponents = std::move(state.pendingPyComponents);
        scene.m_mainCamera = state.mainCamera;
        scene.m_isLoaded = state.isLoaded;
        scene.m_isPlaying = state.isPlaying;
        scene.m_hasStarted = state.hasStarted;
        scene.m_structureVersion = state.structureVersion;
        for (auto &root : scene.m_rootObjects)
            root->SetScene(&scene);
        RestoreSceneComponentRegistries(scene);
        for (Collider *collider : state.residentColliders)
            collider->RestoreSceneResidency();
        SceneManager::Instance().FlushPendingBroadphase();
        state.active = false;
        return true;
    } catch (const std::exception &error) {
        INXLOG_ERROR("Scene retained-world rollback failed: ", error.what());
        return false;
    }
}

void SceneCommitToken::Finalize()
{
    if (!IsActive())
        return;
    Impl &state = *m_impl;
    for (auto &root : state.rootObjects)
        root->SetScene(nullptr);
    state.rootObjects.clear();
    state.objectsById.clear();
    state.componentRegistryNodes.clear();
    state.active = false;
}

std::shared_ptr<SceneCommitToken> Scene::CommitDocumentRetainingCurrentWorld(const nlohmann::json &document)
{
    // Reject bad headers before SceneCommitToken extracts the live world.
    // Otherwise a schema mismatch empties the scene, and a failed Rollback
    // Finalize() destroys the retained graph — save/load then crash.
    if (!ValidateSceneDocumentHeader(document))
        return nullptr;

    auto token = std::shared_ptr<SceneCommitToken>(new SceneCommitToken(*this));
    if (DeserializeDocument(document))
        return token;
    if (!token->Rollback())
        INXLOG_ERROR("Scene candidate commit failed and retained world could not be restored");
    return nullptr;
}

Scene::~Scene()
{
    // Explicitly clear root objects to ensure destructors run while Scene members are valid
    m_rootObjects.clear();
}

GameObject *Scene::CreateGameObject(const std::string &name)
{
    auto gameObject = std::make_unique<GameObject>(name);
    gameObject->m_scene = this;

    GameObject *ptr = gameObject.get();
    m_objectsById[ptr->GetID()] = ptr;
    m_rootObjects.push_back(std::move(gameObject));
    ++m_structureVersion;

    return ptr;
}

void Scene::ReserveCapacity(size_t count)
{
    m_rootObjects.reserve(m_rootObjects.size() + count);
    m_objectsById.reserve(m_objectsById.size() + count);
    // Each GO gets ~2-3 components that queue for Start()
    m_pendingStartComponentIds.reserve(m_pendingStartComponentIds.size() + count * 3);
}

void Scene::AddGameObject(std::unique_ptr<GameObject> gameObject)
{
    if (!gameObject)
        return;

    gameObject->m_scene = this;

    GameObject *ptr = gameObject.get();
    m_objectsById[ptr->GetID()] = ptr;

    // If it has no parent, add to root objects
    if (gameObject->GetParent() == nullptr) {
        m_rootObjects.push_back(std::move(gameObject));
    }
}

void Scene::RemoveGameObject(GameObject *gameObject)
{
    if (!gameObject)
        return;

    // 1. Locate and Detach ownership
    std::unique_ptr<GameObject> ownedPtr;

    if (gameObject->GetParent()) {
        ownedPtr = gameObject->GetParent()->DetachChild(gameObject);
    } else {
        ownedPtr = DetachRootObject(gameObject);
    }
    ++m_structureVersion;

    // 2. ownedPtr goes out of scope -> deleted.
}

void Scene::DestroyGameObject(GameObject *gameObject)
{
    if (!gameObject)
        return;

    // Queue for removal at frame-end, not immediate
    const uint64_t id = gameObject->GetID();
    if (m_pendingDestroySet.insert(id).second) {
        m_pendingDestroy.push_back(id);

        // Unity-like behavior: once Destroy() is requested, object is treated as
        // inactive for this frame's remaining callbacks.  This triggers OnDisable
        // immediately for active components; OnDestroy still runs at frame-end.
        if (gameObject->IsActiveInHierarchy()) {
            gameObject->SetActive(false);
        }
    }
    ++m_structureVersion;
}

std::unique_ptr<GameObject> Scene::DetachRootObject(GameObject *gameObject)
{
    auto it = std::find_if(m_rootObjects.begin(), m_rootObjects.end(),
                           [gameObject](const std::unique_ptr<GameObject> &obj) { return obj.get() == gameObject; });

    if (it != m_rootObjects.end()) {
        std::unique_ptr<GameObject> ret = std::move(*it);
        m_rootObjects.erase(it);
        ++m_structureVersion;
        return ret;
    }
    return nullptr;
}

void Scene::AttachRootObject(std::unique_ptr<GameObject> gameObject)
{
    if (!gameObject)
        return;
    gameObject->SetScene(this); // Ensure scene is set
    m_rootObjects.push_back(std::move(gameObject));
    ++m_structureVersion;
}

void Scene::SetRootObjectSiblingIndex(GameObject *gameObject, int newIndex)
{
    int currentIndex = -1;
    for (size_t i = 0; i < m_rootObjects.size(); ++i) {
        if (m_rootObjects[i].get() == gameObject) {
            currentIndex = static_cast<int>(i);
            break;
        }
    }
    if (currentIndex < 0)
        return;
    newIndex = std::max(0, std::min(newIndex, static_cast<int>(m_rootObjects.size()) - 1));
    if (currentIndex == newIndex)
        return;
    auto ptr = std::move(m_rootObjects[currentIndex]);
    m_rootObjects.erase(m_rootObjects.begin() + currentIndex);
    m_rootObjects.insert(m_rootObjects.begin() + newIndex, std::move(ptr));
    ++m_structureVersion;
}

void Scene::UnregisterGameObject(uint64_t id)
{
    m_objectsById.erase(id);
}

void Scene::RegisterGameObject(GameObject *gameObject)
{
    if (!gameObject)
        return;
    m_objectsById[gameObject->GetID()] = gameObject;
}

std::vector<GameObject *> Scene::GetAllObjects() const
{
    std::vector<GameObject *> result;
    result.reserve(m_objectsById.size());

    for (const auto &root : m_rootObjects) {
        CollectAllObjects(root.get(), result);
    }

    return result;
}

void Scene::CollectAllObjects(GameObject *obj, std::vector<GameObject *> &result) const
{
    if (!obj)
        return;

    result.push_back(obj);

    for (const auto &child : obj->GetChildren()) {
        CollectAllObjects(child.get(), result);
    }
}

GameObject *Scene::Find(const std::string &name) const
{
    for (const auto &root : m_rootObjects) {
        if (root->GetName() == name) {
            return root.get();
        }

        // Search in children recursively
        GameObject *found = root->FindDescendant(name);
        if (found)
            return found;
    }
    return nullptr;
}

std::vector<GameObject *> Scene::FindAll(const std::string &name) const
{
    std::vector<GameObject *> result;
    std::vector<GameObject *> allObjects = GetAllObjects();

    for (GameObject *obj : allObjects) {
        if (obj->GetName() == name) {
            result.push_back(obj);
        }
    }

    return result;
}

GameObject *Scene::FindByID(uint64_t id) const
{
    auto it = m_objectsById.find(id);
    if (it != m_objectsById.end()) {
        return it->second;
    }
    return nullptr;
}

GameObject *Scene::ResolveGameObject(const ObjectHandle &handle) const
{
    if (!handle.IsValid() || handle.worldId != m_worldId)
        return nullptr;

    GameObject *object = FindByID(handle.id);
    if (!object || object->GetLifetimeGeneration() != handle.generation)
        return nullptr;
    return object;
}

Component *Scene::ResolveComponent(const ObjectHandle &handle) const
{
    if (!handle.IsValid() || handle.worldId != m_worldId)
        return nullptr;

    Component *component = Component::FindByComponentId(handle.id);
    if (!component || component->GetLifetimeGeneration() != handle.generation)
        return nullptr;
    GameObject *owner = component->GetGameObject();
    if (!owner || owner->GetScene() != this)
        return nullptr;
    return component;
}

GameObject *Scene::FindWithTag(const std::string &tag) const
{
    for (const auto &[id, obj] : m_objectsById) {
        if (obj && obj->GetTag() == tag) {
            return obj;
        }
    }
    return nullptr;
}

std::vector<GameObject *> Scene::FindGameObjectsWithTag(const std::string &tag) const
{
    std::vector<GameObject *> result;
    for (const auto &[id, obj] : m_objectsById) {
        if (obj && obj->GetTag() == tag) {
            result.push_back(obj);
        }
    }
    return result;
}

std::vector<GameObject *> Scene::FindGameObjectsInLayer(int layer) const
{
    std::vector<GameObject *> result;
    for (const auto &[id, obj] : m_objectsById) {
        if (obj && obj->GetLayer() == layer) {
            result.push_back(obj);
        }
    }
    return result;
}

void Scene::Start()
{
    if (m_hasStarted)
        return;

    m_isLoaded = true;
    m_hasStarted = true;

    // ---- Unity-correct 2-pass lifecycle ----
    // Pass 1: Awake + OnEnable on every object/component
    for (size_t i = 0; i < m_rootObjects.size(); ++i) {
        AwakeObject(m_rootObjects[i].get());
    }
    // Pass 2: Start on every enabled component (all Awake calls finished)
    for (size_t i = 0; i < m_rootObjects.size(); ++i) {
        StartObject(m_rootObjects[i].get());
    }
}

void Scene::AwakeObject(GameObject *obj)
{
    if (!obj)
        return;

    // Unity: Awake is only called on GameObjects that are active in the hierarchy.
    // Inactive objects will have Awake deferred until they are first activated
    // (handled by GameObject::HandleActiveStateChanged).
    if (!obj->IsActiveInHierarchy())
        return;

    const auto &components = obj->GetComponentsInExecutionOrderCached();
    for (Component *component : components) {
        if (component) {
            component->CallAwake();
        }
    }

    const auto &children = obj->GetChildren();
    for (size_t i = 0; i < children.size(); ++i) {
        AwakeObject(children[i].get());
    }
}

void Scene::StartObject(GameObject *obj)
{
    if (!obj)
        return;

    const bool activeInHierarchy = obj->IsActiveInHierarchy();

    const auto &components = obj->GetComponentsInExecutionOrderCached();
    for (Component *component : components) {
        if (component && activeInHierarchy && component->IsEnabled()) {
            component->CallStart();
        }
    }

    const auto &children = obj->GetChildren();
    for (size_t i = 0; i < children.size(); ++i) {
        StartObject(children[i].get());
    }
}

void Scene::Update(float deltaTime)
{
    if (!m_isPlaying)
        return;

    TransformECSStore::Instance().SyncSceneWorldMatrices(this);

    // Flush deferred Start() calls for components that were added/enabled
    // during previous callbacks.
    ProcessPendingStarts();

    // Snapshot root count so objects instantiated mid-frame are not updated
    // until the next frame (Unity-style frame consistency).
    const size_t rootCount = m_rootObjects.size();
    for (size_t i = 0; i < rootCount && i < m_rootObjects.size(); ++i) {
        UpdateObject(m_rootObjects[i].get(), deltaTime);
    }
}

void Scene::FixedUpdate(float fixedDeltaTime)
{
    if (!m_isPlaying)
        return;

    TransformECSStore::Instance().SyncSceneWorldMatrices(this);

    const size_t rootCount = m_rootObjects.size();
    for (size_t i = 0; i < rootCount && i < m_rootObjects.size(); ++i) {
        FixedUpdateObject(m_rootObjects[i].get(), fixedDeltaTime);
    }
}

void Scene::TraverseActiveObjects(GameObject *obj, float dt, void (GameObject::*updateMethod)(float))
{
    if (!obj || !obj->IsActiveInHierarchy() || IsPendingDestroy(obj))
        return;

    (obj->*updateMethod)(dt);

    const auto &children = obj->GetChildren();
    const size_t childCount = children.size();
    for (size_t i = 0; i < childCount && i < children.size(); ++i) {
        TraverseActiveObjects(children[i].get(), dt, updateMethod);
    }
}

void Scene::FixedUpdateObject(GameObject *obj, float fixedDeltaTime)
{
    TraverseActiveObjects(obj, fixedDeltaTime, &GameObject::FixedUpdate);
}

void Scene::UpdateObject(GameObject *obj, float deltaTime)
{
    TraverseActiveObjects(obj, deltaTime, &GameObject::Update);
}

void Scene::LateUpdate(float deltaTime)
{
    if (!m_isPlaying)
        return;

    TransformECSStore::Instance().SyncSceneWorldMatrices(this);

    const size_t rootCount = m_rootObjects.size();
    for (size_t i = 0; i < rootCount && i < m_rootObjects.size(); ++i) {
        LateUpdateObject(m_rootObjects[i].get(), deltaTime);
    }
}

void Scene::EditorUpdate(float deltaTime)
{
    if (m_isPlaying)
        return;

    TransformECSStore::Instance().SyncSceneWorldMatrices(this);

    const size_t rootCount = m_rootObjects.size();
    for (size_t i = 0; i < rootCount && i < m_rootObjects.size(); ++i) {
        EditorUpdateObject(m_rootObjects[i].get(), deltaTime);
    }
}

void Scene::LateUpdateObject(GameObject *obj, float deltaTime)
{
    TraverseActiveObjects(obj, deltaTime, &GameObject::LateUpdate);
}

void Scene::EditorUpdateObject(GameObject *obj, float deltaTime)
{
    TraverseActiveObjects(obj, deltaTime, &GameObject::EditorUpdate);
}

// ============================================================================
// Shared JSON → GameObject builder (used by both Deserialize and InstantiateFromJson)
// ============================================================================

// Internal overload operating directly on a parsed json value.
std::unique_ptr<GameObject> Scene::BuildGameObjectFromJsonImpl(const json &objJson, bool preserveIds,
                                                               ComponentPrototypeCache *prototypeCache)
{
    const size_t pendingPyStart = m_pendingPyComponents.size();
    const auto fail = [&]() -> std::unique_ptr<GameObject> {
        m_pendingPyComponents.resize(pendingPyStart);
        return nullptr;
    };

    if (!objJson.is_object()) {
        INXLOG_ERROR("Scene object must be an object document");
        return fail();
    }

    static const std::unordered_set<std::string> allowedObjectFields = {
        "name",        "id",          "active",    "is_static",  "tag",      "layer",
        "prefab_guid", "prefab_root", "transform", "components", "children",
    };
    for (const auto &[key, value] : objJson.items()) {
        (void)value;
        if (allowedObjectFields.find(key) == allowedObjectFields.end()) {
            INXLOG_ERROR("Scene object contains unknown field '", key, "'");
            return fail();
        }
    }
    if (!objJson.contains("name") || !objJson["name"].is_string() || !objJson.contains("active") ||
        !objJson["active"].is_boolean() || !objJson.contains("is_static") || !objJson["is_static"].is_boolean() ||
        !objJson.contains("tag") || !objJson["tag"].is_string() || !objJson.contains("layer") ||
        !objJson["layer"].is_number_integer() || !objJson.contains("components") || !objJson["components"].is_array()) {
        INXLOG_ERROR("Scene object is missing required typed fields");
        return fail();
    }
    if (preserveIds &&
        (!objJson.contains("id") || !objJson["id"].is_number_unsigned() || objJson["id"].get<uint64_t>() == 0)) {
        INXLOG_ERROR("Scene object must contain a non-zero unsigned id");
        return fail();
    }
    if (objJson.contains("id") && (!objJson["id"].is_number_unsigned() || objJson["id"].get<uint64_t>() == 0)) {
        INXLOG_ERROR("Scene object id must be a non-zero unsigned integer");
        return fail();
    }
    const int layer = objJson["layer"].get<int>();
    if (layer < 0 || layer >= 32) {
        INXLOG_ERROR("Scene object layer must be in [0, 31]");
        return fail();
    }
    if (objJson.contains("prefab_guid") && !objJson["prefab_guid"].is_string()) {
        INXLOG_ERROR("Scene object prefab_guid must be a string");
        return fail();
    }
    if (objJson.contains("prefab_root") && !objJson["prefab_root"].is_boolean()) {
        INXLOG_ERROR("Scene object prefab_root must be a boolean");
        return fail();
    }

    std::string name = objJson["name"].get<std::string>();
    auto obj = std::make_unique<GameObject>(name);
    obj->m_scene = this;

    // Restore original ID only when deserializing (not cloning)
    if (preserveIds && objJson.contains("id")) {
        obj->m_id = objJson["id"].get<uint64_t>();
        GameObject::EnsureNextID(obj->m_id);
    }

    obj->m_active = objJson["active"].get<bool>();
    obj->m_isStatic = objJson["is_static"].get<bool>();
    obj->m_tag = objJson["tag"].get<std::string>();
    obj->m_layer = layer;
    if (objJson.contains("prefab_guid"))
        obj->m_prefabGuid = objJson["prefab_guid"].get<std::string>();
    obj->m_prefabRoot = objJson.value("prefab_root", false);

    // Transform
    if (!objJson.contains("transform") || !objJson["transform"].is_object()) {
        INXLOG_ERROR("Scene object '", name, "' is missing a valid transform document");
        return fail();
    }
    json tJson = objJson["transform"];
    if (!preserveIds)
        tJson.erase("component_id");
    if (!obj->m_transform.DeserializeDocument(tJson)) {
        INXLOG_ERROR("Failed to deserialize transform on scene object '", name, "'");
        return fail();
    }

    const uint64_t objId = obj->m_id ? obj->m_id : obj->GetID();
    for (size_t componentIndex = 0; componentIndex < objJson["components"].size(); ++componentIndex) {
        const auto &componentRecordDocument = objJson["components"][componentIndex];
        DecodedComponentRecord record;
        try {
            record = DecodeComponentRecord(componentRecordDocument);
        } catch (const std::exception &error) {
            INXLOG_ERROR("Invalid component record on scene object '", name, "': ", error.what());
            return fail();
        }

        if (record.kind == ComponentRecordKind::Python) {
            PendingPyComponent pending;
            pending.gameObjectId = objId;
            pending.typeName = record.pythonTypeName;
            pending.scriptGuid = record.scriptGuid;
            pending.typeGuid = record.typeGuid;
            pending.enabled = record.enabled;
            pending.executionOrder = record.executionOrder;
            pending.componentIndex = componentIndex;
            pending.fieldsDocument = BuildPythonFieldsDocument(record);
            m_pendingPyComponents.push_back(std::move(pending));
            continue;
        }

        const std::string &typeName = record.nativeTypeName;
        if (typeName == "Transform") {
            INXLOG_ERROR("Scene object '", name, "' contains Transform in its components array");
            return fail();
        }
        bool supportsPrototype = typeName == "BoxCollider";
        if (typeName == "MeshRenderer") {
            supportsPrototype = true;
            const auto materialsIt = record.data.find("materials");
            if (materialsIt == record.data.end() || !materialsIt->is_array())
                supportsPrototype = false;
            else {
                for (const json &slot : *materialsIt) {
                    if (!slot.is_null() && !slot.is_string()) {
                        supportsPrototype = false;
                        break;
                    }
                }
            }
        }
        Component *prototype = nullptr;
        size_t prototypeHash = 0;
        if (prototypeCache && supportsPrototype) {
            prototypeHash = std::hash<std::string>{}(record.typeId);
            const auto mixHash = [&](size_t value) {
                prototypeHash ^= value + 0x9e3779b97f4a7c15ULL + (prototypeHash << 6u) + (prototypeHash >> 2u);
            };
            mixHash(std::hash<bool>{}(record.enabled));
            mixHash(std::hash<int>{}(record.executionOrder));
            mixHash(std::hash<json>{}(record.data));
            const auto cacheIt = prototypeCache->find(prototypeHash);
            if (cacheIt != prototypeCache->end()) {
                for (const ComponentPrototype &candidate : cacheIt->second) {
                    const json &candidateRecord = *candidate.record;
                    if (candidateRecord.at("type_id") == componentRecordDocument.at("type_id") &&
                        candidateRecord.at("enabled") == componentRecordDocument.at("enabled") &&
                        candidateRecord.at("execution_order") == componentRecordDocument.at("execution_order") &&
                        candidateRecord.at("data") == componentRecordDocument.at("data")) {
                        prototype = candidate.component;
                        break;
                    }
                }
            }
        }

        std::unique_ptr<Component> comp;
        if (prototype)
            comp = prototype->Clone();
        else {
            comp = ComponentFactory::Create(typeName);
            if (!comp) {
                INXLOG_ERROR("Scene object '", name, "' references unknown component type '", typeName, "'");
                return fail();
            }
            json componentDocument = BuildNativeComponentDocument(record);
            if (!preserveIds)
                componentDocument.erase("component_id");
            comp->SetGameObject(obj.get());
            if (!comp->DeserializeDocument(componentDocument)) {
                INXLOG_ERROR("Failed to deserialize component '", typeName, "' on scene object '", name, "'");
                return fail();
            }
        }
        comp->SetGameObject(obj.get());
        if (prototypeCache && supportsPrototype && !prototype)
            (*prototypeCache)[prototypeHash].push_back({&componentRecordDocument, comp.get()});
        obj->m_components.push_back(std::move(comp));
    }

    // Recurse children
    if (!objJson.contains("children") || !objJson["children"].is_array()) {
        INXLOG_ERROR("Scene object '", name, "' is missing its children array");
        return fail();
    }
    {
        for (const auto &childJson : objJson["children"]) {
            auto child = BuildGameObjectFromJsonImpl(childJson, preserveIds, prototypeCache);
            if (!child)
                return fail();
            obj->AttachChild(std::move(child));
        }
    }

    return obj;
}

std::unique_ptr<GameObject> Scene::BuildGameObjectFromJson(const std::string &jsonStr, bool preserveIds)
{
    json objJson = json::parse(jsonStr);
    return BuildGameObjectFromJsonImpl(objJson, preserveIds);
}

void Scene::RegisterObjectSubtree(GameObject *root)
{
    if (!root)
        return;
    RegisterGameObject(root);
    for (const auto &child : root->GetChildren())
        RegisterObjectSubtree(child.get());
}

void Scene::ProcessPendingDestroys()
{
    std::vector<uint64_t> currentPending;
    currentPending.swap(m_pendingDestroy); // To ensure we don't loop forever if destroy triggers destroy

    for (uint64_t id : currentPending) {
        m_pendingDestroySet.erase(id);
        GameObject *obj = FindByID(id);
        if (obj) {
            RemoveGameObject(obj);
        }
    }
}

bool Scene::IsPendingDestroy(const GameObject *obj) const
{
    if (!obj)
        return false;

    const GameObject *current = obj;
    while (current) {
        if (m_pendingDestroySet.find(current->GetID()) != m_pendingDestroySet.end()) {
            return true;
        }
        current = current->GetParent();
    }
    return false;
}

void Scene::QueueComponentStart(Component *component)
{
    if (!component)
        return;

    const uint64_t id = component->GetComponentID();
    if (id == 0)
        return;

    if (m_pendingStartComponentIdSet.insert(id).second) {
        m_pendingStartComponentIds.push_back(id);
    }
}

void Scene::ProcessPendingStarts()
{
    if (m_pendingStartComponentIds.empty())
        return;

    std::vector<uint64_t> pending;
    pending.swap(m_pendingStartComponentIds);
    m_pendingStartComponentIdSet.clear();

    // Build a component-pointer cache so the sort and dispatch each do O(1) lookups.
    std::vector<Component *> comps;
    comps.reserve(pending.size());
    for (uint64_t id : pending) {
        comps.push_back(Component::FindByComponentId(id));
    }

    // Stable-sort by execution order, then by component ID.
    std::vector<size_t> indices(pending.size());
    std::iota(indices.begin(), indices.end(), size_t(0));
    std::stable_sort(indices.begin(), indices.end(), [&](size_t i, size_t j) {
        Component *a = comps[i];
        Component *b = comps[j];
        if (!a || !b)
            return pending[i] < pending[j];
        if (a->GetExecutionOrder() != b->GetExecutionOrder())
            return a->GetExecutionOrder() < b->GetExecutionOrder();
        return a->GetComponentID() < b->GetComponentID();
    });

    for (size_t idx : indices) {
        Component *component = comps[idx];
        if (!component)
            continue;

        GameObject *go = component->GetGameObject();
        if (!go)
            continue;

        if (!m_isPlaying || !m_hasStarted)
            continue;

        if (component->IsEnabled() && go->IsActiveInHierarchy() && component->HasAwake()) {
            component->CallStart();
        }
    }
}

void Scene::QueueStartObject(GameObject *obj)
{
    if (!obj || !obj->IsActiveInHierarchy())
        return;

    const auto &components = obj->GetComponentsInExecutionOrderCached();
    for (Component *component : components) {
        if (component && component->IsEnabled() && component->HasAwake()) {
            QueueComponentStart(component);
        }
    }

    const auto &children = obj->GetChildren();
    for (size_t i = 0; i < children.size(); ++i) {
        QueueStartObject(children[i].get());
    }
}

Component *Scene::FindComponentByID(uint64_t componentId) const
{
    if (componentId == 0)
        return nullptr;

    return Component::FindByComponentId(componentId);
}

// ============================================================================
// Instantiate (deep clone) — Unity: Object.Instantiate()
// ============================================================================

GameObject *Scene::InstantiateGameObject(GameObject *source, GameObject *parent, bool instantiateInWorldSpace)
{
    if (!source)
        return nullptr;

    const glm::vec3 sourceWorldPosition = source->GetTransform()->GetWorldPosition();
    const glm::quat sourceWorldRotation = source->GetTransform()->GetWorldRotation();
    const glm::vec3 sourceWorldScale = source->GetTransform()->GetWorldScale();

    // Native deep clone — no JSON serialization round-trip.
    auto clone = source->Clone(this);
    if (!clone)
        return nullptr;

    // Unity: cloned root object gets " (Clone)" suffix
    clone->SetName(source->GetName() + " (Clone)");

    // Register all cloned objects in scene lookup
    GameObject *ptr = clone.get();
    auto registerAll = [&](auto &&self, GameObject *go) -> void {
        if (!go)
            return;
        RegisterGameObject(go);
        for (const auto &child : go->GetChildren()) {
            self(self, child.get());
        }
    };
    registerAll(registerAll, ptr);

    // Attach to scene hierarchy
    if (parent) {
        parent->AttachChild(std::move(clone));
    } else {
        m_rootObjects.push_back(std::move(clone));
    }

    if (instantiateInWorldSpace) {
        ptr->GetTransform()->SetWorldPosition(sourceWorldPosition);
        ptr->GetTransform()->SetWorldRotation(sourceWorldRotation);
        ptr->GetTransform()->SetWorldScale(sourceWorldScale);
    }

    // Awake C++ components so they register with subsystems
    AwakeObject(ptr);
    if (m_isPlaying && m_hasStarted) {
        QueueStartObject(ptr);
    }

    ++m_structureVersion;

    return ptr;
}

// ============================================================================
// Instantiate from JSON (prefab) — clone from raw JSON string (prefab file)
// ============================================================================

GameObject *Scene::InstantiateFromJson(const std::string &jsonStr, GameObject *parent)
{
    try {
        return InstantiateFromDocument(json::parse(jsonStr), parent);
    } catch (const std::exception &e) {
        INXLOG_ERROR("Scene::InstantiateFromJson: JSON parse error: ", e.what());
        return nullptr;
    }
}

GameObject *Scene::InstantiateFromDocument(const nlohmann::json &document, GameObject *parent)
{
    auto clone = BuildGameObjectFromJsonImpl(document, /*preserveIds=*/false);
    if (!clone)
        return nullptr;

    GameObject *ptr = clone.get();
    RegisterObjectSubtree(ptr);

    if (parent) {
        parent->AttachChild(std::move(clone));
    } else {
        m_rootObjects.push_back(std::move(clone));
    }

    AwakeObject(ptr);
    if (m_isPlaying && m_hasStarted) {
        QueueStartObject(ptr);
    }

    ++m_structureVersion;

    return ptr;
}

nlohmann::json Scene::SerializeDocument() const
{
    json j;
    j["name"] = m_name;
    j["isPlaying"] = m_isPlaying;

    // Serialize main camera reference via component_id (survives deserialization)
    if (m_mainCamera) {
        j["mainCameraComponentId"] = m_mainCamera->GetComponentID();
    }

    // Root documents are independent. Keep Python-backed roots on the caller
    // thread, while large native-only scenes use the engine worker pool.
    std::vector<json> rootDocuments(m_rootObjects.size());
    std::vector<size_t> nativeRootIndices;
    nativeRootIndices.reserve(m_rootObjects.size());
    const auto containsPythonComponent = [&](const auto &self, const GameObject *object) -> bool {
        if (object->m_hasPyProxy)
            return true;
        for (const auto &child : object->m_children) {
            if (self(self, child.get()))
                return true;
        }
        return false;
    };
    for (size_t index = 0; index < m_rootObjects.size(); ++index) {
        const GameObject *root = m_rootObjects[index].get();
        if (containsPythonComponent(containsPythonComponent, root))
            rootDocuments[index] = root->SerializeDocument();
        else
            nativeRootIndices.push_back(index);
    }

    if (nativeRootIndices.size() >= kParallelSceneRootThreshold && JobSystem::IsAvailable()) {
        const uint32_t workerCount = std::max(1u, JobSystem::Get().GetWorkerCount());
        const uint32_t jobCount =
            static_cast<uint32_t>(std::min(nativeRootIndices.size(), static_cast<size_t>(workerCount * 4u)));
        JobSystem::Get().ParallelFor(jobCount, [&](uint32_t jobIndex) {
            const size_t begin = nativeRootIndices.size() * jobIndex / jobCount;
            const size_t end = nativeRootIndices.size() * (jobIndex + 1u) / jobCount;
            for (size_t position = begin; position < end; ++position) {
                const size_t rootIndex = nativeRootIndices[position];
                rootDocuments[rootIndex] = m_rootObjects[rootIndex]->SerializeDocument();
            }
        });
    } else {
        for (const size_t rootIndex : nativeRootIndices)
            rootDocuments[rootIndex] = m_rootObjects[rootIndex]->SerializeDocument();
    }
    json objectsArray = json::array();
    auto &objects = objectsArray.get_ref<json::array_t &>();
    objects.reserve(rootDocuments.size());
    for (json &document : rootDocuments)
        objects.push_back(std::move(document));
    j["objects"] = objectsArray;

    return j;
}

std::string Scene::Serialize() const
{
    return DumpSceneDocument(SerializeDocument(), m_objectsById.size());
}

bool Scene::DeserializeDocument(const nlohmann::json &j)
{
    try {
        const uint64_t profileStart = SDL_GetPerformanceCounter();
        const double profileFrequency = static_cast<double>(SDL_GetPerformanceFrequency());
        const auto elapsedMs = [&](uint64_t begin, uint64_t end) {
            return static_cast<double>(end - begin) * 1000.0 / profileFrequency;
        };
        if (!ValidateSceneDocumentHeader(j))
            return false;

        // Build the complete graph with temporary IDs in an isolated Scene.
        // Transform/physics stores can hold both graphs, while temporary component
        // IDs avoid publishing over live registry entries before validation succeeds.
        Scene staging(j["name"].get<std::string>());
        staging.m_isPlaying = j["isPlaying"].get<bool>();
        staging.m_rootObjects.reserve(j["objects"].size());
        ComponentPrototypeCache prototypeCache;
        prototypeCache.reserve(16);
        for (const auto &objJson : j["objects"]) {
            auto obj = staging.BuildGameObjectFromJsonImpl(objJson, /*preserveIds=*/false, &prototypeCache);
            if (!obj)
                throw std::invalid_argument("scene object graph validation failed");
            staging.m_rootObjects.push_back(std::move(obj));
        }
        const uint64_t profileStaged = SDL_GetPerformanceCounter();

        std::unordered_set<uint64_t> objectIds;
        std::unordered_set<uint64_t> componentIds;
        std::unordered_map<uint64_t, Component *> componentsByDocumentId;
        std::unordered_map<uint64_t, uint64_t> stagedToDocumentObjectId;
        std::vector<std::pair<Component *, uint64_t>> componentIdAssignments;
        std::vector<uint64_t> pythonComponentIds;

        struct ObjectIdAssignment
        {
            GameObject *object = nullptr;
            uint64_t stagedId = 0;
            uint64_t documentId = 0;
        };
        struct RootIdCollection
        {
            std::vector<ObjectIdAssignment> objects;
            std::vector<std::pair<Component *, uint64_t>> components;
            std::vector<uint64_t> pythonComponents;
        };

        const auto collectRootIds = [&](size_t rootIndex, RootIdCollection &collection) {
            const auto collectIds = [&](const auto &self, GameObject *obj, const json &objJson) -> void {
                if (!objJson.contains("id") || !objJson["id"].is_number_unsigned())
                    throw std::invalid_argument("scene GameObject is missing an unsigned id");
                const uint64_t objectId = objJson["id"].get<uint64_t>();
                if (objectId == 0)
                    throw std::invalid_argument("scene contains a zero GameObject id");
                collection.objects.push_back({obj, obj->m_id, objectId});

                const auto collectComponentId = [&](Component *component, const json &componentJson) {
                    if (!componentJson.contains("component_id") || !componentJson["component_id"].is_number_unsigned())
                        throw std::invalid_argument("scene component is missing an unsigned component_id");
                    const uint64_t componentId = componentJson["component_id"].get<uint64_t>();
                    if (componentId == 0)
                        throw std::invalid_argument("scene contains a zero component_id");
                    collection.components.emplace_back(component, componentId);
                };

                collectComponentId(&obj->m_transform, objJson.at("transform"));
                size_t nativeComponentIndex = 0;
                for (const auto &componentDocument : objJson.at("components")) {
                    const DecodedComponentRecord record = DecodeComponentRecord(componentDocument);
                    if (record.kind == ComponentRecordKind::Python) {
                        collection.pythonComponents.push_back(record.componentId);
                        continue;
                    }
                    if (nativeComponentIndex >= obj->m_components.size())
                        throw std::invalid_argument("scene native component count changed during staging");
                    collectComponentId(obj->m_components[nativeComponentIndex++].get(), componentDocument);
                }
                if (nativeComponentIndex != obj->m_components.size())
                    throw std::invalid_argument("scene native component count changed during staging");

                const auto &childDocuments = objJson.at("children");
                if (childDocuments.size() != obj->m_children.size())
                    throw std::invalid_argument("scene child count changed during staging");
                for (size_t i = 0; i < obj->m_children.size(); ++i)
                    self(self, obj->m_children[i].get(), childDocuments[i]);
            };
            collectIds(collectIds, staging.m_rootObjects[rootIndex].get(), j["objects"][rootIndex]);
        };

        const size_t rootCount = staging.m_rootObjects.size();
        const uint32_t indexJobCount =
            rootCount >= kParallelSceneRootThreshold && JobSystem::IsAvailable()
                ? static_cast<uint32_t>(
                      std::min(rootCount, static_cast<size_t>(std::max(1u, JobSystem::Get().GetWorkerCount()) * 4u)))
                : 1u;
        std::vector<RootIdCollection> rootCollections(indexJobCount);
        const auto collectRange = [&](uint32_t jobIndex) {
            RootIdCollection &collection = rootCollections[jobIndex];
            const size_t begin = rootCount * jobIndex / indexJobCount;
            const size_t end = rootCount * (jobIndex + 1u) / indexJobCount;
            collection.objects.reserve(end - begin);
            collection.components.reserve((end - begin) * 3u);
            for (size_t rootIndex = begin; rootIndex < end; ++rootIndex)
                collectRootIds(rootIndex, collection);
        };
        if (indexJobCount > 1u)
            JobSystem::Get().ParallelFor(indexJobCount, collectRange);
        else
            collectRange(0u);

        size_t totalObjectCount = 0;
        size_t totalNativeComponentCount = 0;
        size_t totalPythonComponentCount = 0;
        for (const RootIdCollection &collection : rootCollections) {
            totalObjectCount += collection.objects.size();
            totalNativeComponentCount += collection.components.size();
            totalPythonComponentCount += collection.pythonComponents.size();
        }
        objectIds.reserve(totalObjectCount);
        componentIds.reserve(totalNativeComponentCount + totalPythonComponentCount);
        stagedToDocumentObjectId.reserve(totalObjectCount);
        componentIdAssignments.reserve(totalNativeComponentCount);
        componentsByDocumentId.reserve(totalNativeComponentCount);
        pythonComponentIds.reserve(totalPythonComponentCount);
        staging.m_objectsById.reserve(totalObjectCount);
        for (RootIdCollection &collection : rootCollections) {
            for (const ObjectIdAssignment &assignment : collection.objects) {
                if (!objectIds.insert(assignment.documentId).second)
                    throw std::invalid_argument("scene contains a duplicate GameObject id");
                stagedToDocumentObjectId.emplace(assignment.stagedId, assignment.documentId);
                assignment.object->m_id = assignment.documentId;
                staging.m_objectsById.emplace(assignment.documentId, assignment.object);
            }
            for (const auto &[component, componentId] : collection.components) {
                if (!componentIds.insert(componentId).second)
                    throw std::invalid_argument("scene contains a duplicate component_id");
                componentIdAssignments.emplace_back(component, componentId);
                componentsByDocumentId.emplace(componentId, component);
            }
            for (const uint64_t componentId : collection.pythonComponents) {
                if (!componentIds.insert(componentId).second)
                    throw std::invalid_argument("scene contains a duplicate component_id");
                pythonComponentIds.push_back(componentId);
            }
        }
        const uint64_t profileIndexed = SDL_GetPerformanceCounter();

        bool requiresFreshComponentIds = false;
        for (const auto &[component, componentId] : componentIdAssignments) {
            Component *occupant = Component::FindByComponentId(componentId);
            if (!occupant || occupant == component)
                continue;
            GameObject *owner = occupant->GetGameObject();
            Scene *ownerScene = owner ? owner->GetScene() : nullptr;
            if (ownerScene != this && ownerScene != &staging) {
                requiresFreshComponentIds = true;
                break;
            }
        }
        for (const uint64_t componentId : pythonComponentIds) {
            Component *occupant = Component::FindByComponentId(componentId);
            if (!occupant)
                continue;
            GameObject *owner = occupant->GetGameObject();
            Scene *ownerScene = owner ? owner->GetScene() : nullptr;
            if (ownerScene != this && ownerScene != &staging) {
                throw std::invalid_argument(
                    "scene Python component_id collides with a component owned by another live Scene");
            }
        }

        for (auto &pending : staging.m_pendingPyComponents) {
            const auto it = stagedToDocumentObjectId.find(pending.gameObjectId);
            if (it == stagedToDocumentObjectId.end())
                throw std::invalid_argument("pending Python component references an unknown staged object");
            pending.gameObjectId = it->second;
        }

        Component *stagedMainCamera = nullptr;
        if (j.contains("mainCameraComponentId")) {
            if (!j["mainCameraComponentId"].is_number_unsigned())
                throw std::invalid_argument("mainCameraComponentId must be unsigned");
            const uint64_t mainCameraComponentId = j["mainCameraComponentId"].get<uint64_t>();
            const auto cameraIt = componentsByDocumentId.find(mainCameraComponentId);
            if (cameraIt == componentsByDocumentId.end() || cameraIt->second->GetTypeName() != "Camera")
                throw std::invalid_argument("mainCameraComponentId does not reference a Camera");
            stagedMainCamera = cameraIt->second;
        }

        // Loading a copy while its source Scene is still alive cannot preserve
        // globally unique component IDs. Keep every staging ID in that case so
        // the copied graph is internally consistent and no live registry entry
        // is overwritten.
        if (requiresFreshComponentIds) {
            for (auto &[component, componentId] : componentIdAssignments)
                componentId = component->GetComponentID();
        }

        auto &componentRegistry = Component::GetInstanceRegistry();
        componentRegistry.reserve(componentRegistry.size() + componentIdAssignments.size());
        using ComponentRegistry = std::remove_reference_t<decltype(componentRegistry)>;
        std::vector<ComponentRegistry::node_type> stagedRegistryNodes;
        stagedRegistryNodes.reserve(componentIdAssignments.size());
        for (const auto &[component, componentId] : componentIdAssignments) {
            (void)componentId;
            auto node = componentRegistry.extract(component->m_componentId);
            if (node.empty())
                throw std::logic_error("staging component was not present in the instance registry");
            stagedRegistryNodes.push_back(std::move(node));
        }
        const uint64_t profileValidated = SDL_GetPerformanceCounter();

        // Commit starts here. All schema/factory/component validation has completed.
        m_mainCamera = nullptr;
        SceneManager::Instance().ClearComponentRegistries();
        m_rootObjects.clear();
        m_objectsById.clear();
        m_pendingDestroy.clear();
        m_pendingDestroySet.clear();
        m_pendingStartComponentIds.clear();
        m_pendingPyComponents.clear();
        m_hasStarted = false;

        m_name = std::move(staging.m_name);
        m_isPlaying = staging.m_isPlaying;
        m_pendingPyComponents = std::move(staging.m_pendingPyComponents);
        for (size_t i = 0; i < componentIdAssignments.size(); ++i) {
            auto &[component, componentId] = componentIdAssignments[i];
            if (auto *renderer = dynamic_cast<MeshRenderer *>(component)) {
                auto &graph = AssetDependencyGraph::Instance();
                graph.ClearRuntimeDependenciesOf(std::to_string(component->m_componentId));
                const std::string publishedOwner = std::to_string(componentId);
                if (renderer->GetMeshAssetRef().HasGuid())
                    graph.AddRuntimeDependency(publishedOwner, renderer->GetMeshAssetRef().GetGuid());
                for (const auto &reference : renderer->GetMaterialRefs()) {
                    if (reference.HasGuid())
                        graph.AddRuntimeDependency(publishedOwner, reference.GetGuid());
                }
            }
            component->m_componentId = componentId;
            Component::EnsureNextComponentID(componentId);
            auto &node = stagedRegistryNodes[i];
            node.key() = componentId;
            node.mapped() = component;
            componentRegistry.insert(std::move(node));
        }
        m_objectsById = std::move(staging.m_objectsById);
        for (auto &root : staging.m_rootObjects) {
            root->SetScene(this);
            m_rootObjects.push_back(std::move(root));
        }

        // Documents preserve GameObject IDs. Advance the process-wide allocator
        // before Awake can create more objects, otherwise a fresh object after
        // loading can overwrite an existing ID in m_objectsById.
        for (const uint64_t objectId : objectIds)
            GameObject::EnsureNextID(objectId);
        const uint64_t profileCommitted = SDL_GetPerformanceCounter();

        // ── Step 5: native Awake pass. ──
        // PyComponentProxy instances are NOT in m_rootObjects yet — they live
        // in m_pendingPyComponents and the Python side restores them after we
        // return.  This loop touches C++ components only and re-populates the
        // MeshRenderer/Rigidbody/Collider registries that the renderer and
        // physics step rely on.
        for (const auto &root : m_rootObjects) {
            AwakeObject(root.get());
        }
        const uint64_t profileAwake = SDL_GetPerformanceCounter();

        // Restore main camera reference from component ID
        if (stagedMainCamera)
            m_mainCamera = static_cast<Camera *>(stagedMainCamera);

        ++m_structureVersion; // Scene was fully rebuilt

        const uint64_t profileEnd = SDL_GetPerformanceCounter();
        if (elapsedMs(profileStart, profileEnd) >= 20.0) {
            INXLOG_INFO("[Perf] Scene deserialize: total=", elapsedMs(profileStart, profileEnd),
                        "ms stage=", elapsedMs(profileStart, profileStaged),
                        "ms index=", elapsedMs(profileStaged, profileIndexed),
                        "ms validate=", elapsedMs(profileIndexed, profileValidated),
                        "ms commit=", elapsedMs(profileValidated, profileCommitted),
                        "ms awake=", elapsedMs(profileCommitted, profileAwake),
                        "ms finish=", elapsedMs(profileAwake, profileEnd), "ms roots=", m_rootObjects.size(),
                        " objects=", m_objectsById.size());
        }

        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("Scene::Deserialize failed for scene '", m_name, "': ", e.what());
        return false;
    }
}

bool Scene::SaveToFile(const std::string &path) const
{
    try {
        const std::string jsonStr = DumpSceneDocument(SerializeDocument(), m_objectsById.size());
        DocumentStore::Instance().WriteAndWait(path, jsonStr);
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("Scene::SaveToFile failed for '", path, "': ", e.what());
        return false;
    }
}

Camera *Scene::FindGameCamera(Camera *editorCam)
{
    // Fast path: cached main camera is still valid and active
    if (m_mainCamera && m_mainCamera != editorCam) {
        // Verify the camera's GameObject is still active and the component is enabled
        GameObject *go = m_mainCamera->GetGameObject();
        if (go && go->IsActiveInHierarchy() && m_mainCamera->IsEnabled()) {
            return m_mainCamera;
        }
        // Cached main camera is no longer valid — clear and re-discover
        m_mainCamera = nullptr;
    }

    // Auto-discover: find highest-priority (lowest depth) active Camera component
    auto objects = FindObjectsWithComponent<Camera>();
    Camera *bestCam = nullptr;
    float bestDepth = std::numeric_limits<float>::max();

    for (auto *obj : objects) {
        if (!obj->IsActiveInHierarchy())
            continue;

        Camera *c = obj->GetComponent<Camera>();
        if (!c || !c->IsEnabled() || c == editorCam)
            continue;

        if (c->GetDepth() < bestDepth) {
            bestDepth = c->GetDepth();
            bestCam = c;
        }
    }

    if (bestCam) {
        m_mainCamera = bestCam;
        INXLOG_DEBUG("Game camera auto-assigned from GameObject '", bestCam->GetGameObject()->GetName(),
                     "' (depth=", bestDepth, ")");
    }

    return bestCam;
}

} // namespace infernux
