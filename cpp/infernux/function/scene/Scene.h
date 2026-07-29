#pragma once

#include "Camera.h"
#include "GameObject.h"
#include "ObjectHandle.h"
#include "SceneEnvironment.h"
#include <memory>
#include <nlohmann/json.hpp>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace infernux
{

class SceneCommitToken;
class InxMaterial;

/**
 * @brief Scene container that holds all GameObjects.
 *
 * The Scene is the root container for all GameObjects in the game world.
 * It manages object lifecycle, updates, and provides lookup functionality.
 */
class Scene
{
  public:
    Scene() = default;
    explicit Scene(const std::string &name) : m_name(name)
    {
    }
    ~Scene();

    // Non-copyable, movable
    Scene(const Scene &) = delete;
    Scene &operator=(const Scene &) = delete;
    Scene(Scene &&) = default;
    Scene &operator=(Scene &&) = default;

    // ========================================================================
    // Properties
    // ========================================================================

    [[nodiscard]] const std::string &GetName() const
    {
        return m_name;
    }
    void SetName(const std::string &name)
    {
        m_name = name;
    }

    /// Per-scene environment (skybox material + ambient) settings.
    [[nodiscard]] const SceneEnvironmentSettings &GetEnvironment() const
    {
        return m_environment;
    }
    [[nodiscard]] SceneEnvironmentSettings &GetEnvironment()
    {
        return m_environment;
    }
    void SetEnvironment(const SceneEnvironmentSettings &environment)
    {
        m_environment = environment;
    }

    /// Mark an explicit jump in the rendered world's time domain. Continuous
    /// Update/FixedUpdate playback must not touch this revision.
    void MarkTemporalDiscontinuity() noexcept
    {
        ++m_temporalDiscontinuityRevision;
    }
    [[nodiscard]] uint64_t GetTemporalDiscontinuityRevision() const noexcept
    {
        return m_temporalDiscontinuityRevision;
    }

    /// Resolve the active skybox material: the environment's material asset
    /// when set and loadable, otherwise the builtin procedural sky.
    [[nodiscard]] std::shared_ptr<InxMaterial> ResolveSkyboxMaterial() const;

    // ========================================================================
    // GameObject management
    // ========================================================================

    /// @brief Create a new empty GameObject in this scene
    GameObject *CreateGameObject(const std::string &name = "GameObject");

    /// @brief Pre-allocate capacity for root objects, id map, and pending-start queue
    void ReserveCapacity(size_t count);

    /// @brief Add an existing GameObject to this scene (takes ownership)
    void AddGameObject(std::unique_ptr<GameObject> gameObject);

    /// @brief Remove a GameObject from this scene
    void RemoveGameObject(GameObject *gameObject);

    /// @brief Destroy a GameObject (will be removed at end of frame)
    void DestroyGameObject(GameObject *gameObject);

    /// @brief Clone a GameObject (deep copy). Unity: Object.Instantiate()
    /// Creates a full copy including all children and components.
    /// Python components are stored as pending for Python-side reconstruction.
    /// @param source The GameObject to clone
    /// @param parent Optional parent for the clone (nullptr = root level)
    /// @param instantiateInWorldSpace Preserve the source root's world transform after parenting
    /// @return The cloned GameObject, or nullptr on failure
    GameObject *InstantiateGameObject(GameObject *source, GameObject *parent = nullptr,
                                      bool instantiateInWorldSpace = false);

    /// @brief Instantiate a GameObject hierarchy from a JSON string (e.g. prefab file).
    /// Creates fresh IDs for all objects. Python components are stored as pending.
    /// @param jsonStr The serialized GameObject JSON (from GameObject::Serialize())
    /// @param parent Optional parent for the new object (nullptr = root level)
    /// @return The root GameObject, or nullptr on failure
    GameObject *InstantiateFromJson(const std::string &jsonStr, GameObject *parent = nullptr);

    /// @brief Instantiate directly from a parsed document. Fresh IDs are assigned.
    GameObject *InstantiateFromDocument(const nlohmann::json &document, GameObject *parent = nullptr);

    /// @brief Internal: Unregister object ID from lookup (called from GameObject dtor)
    void UnregisterGameObject(uint64_t id);

    /// @brief Internal: Register object ID into lookup
    void RegisterGameObject(GameObject *gameObject);

    /// @brief Detach an object from root list (returns ownership)
    std::unique_ptr<GameObject> DetachRootObject(GameObject *gameObject);

    /// @brief Attach an object to root list (takes ownership)
    void AttachRootObject(std::unique_ptr<GameObject> gameObject);

    /// @brief Reorder a root object to a new sibling index
    void SetRootObjectSiblingIndex(GameObject *gameObject, int newIndex);

    /// @brief Get all root GameObjects (objects without parents)
    [[nodiscard]] const std::vector<std::unique_ptr<GameObject>> &GetRootObjects() const
    {
        return m_rootObjects;
    }

    /// @brief Get all GameObjects in the scene (including children)
    [[nodiscard]] std::vector<GameObject *> GetAllObjects() const;

    // ========================================================================
    // Finding objects
    // ========================================================================

    /// @brief Find a GameObject by name (first match)
    [[nodiscard]] GameObject *Find(const std::string &name) const;

    /// @brief Find all GameObjects with a given name
    [[nodiscard]] std::vector<GameObject *> FindAll(const std::string &name) const;

    /// @brief Find a GameObject by ID
    [[nodiscard]] GameObject *FindByID(uint64_t id) const;

    /// @brief Resolve a GameObject handle only when world, ID, and lifetime match.
    [[nodiscard]] GameObject *ResolveGameObject(const ObjectHandle &handle) const;

    /// @brief Resolve a Component handle only when world, ID, and lifetime match.
    [[nodiscard]] Component *ResolveComponent(const ObjectHandle &handle) const;

    [[nodiscard]] uint64_t GetWorldId() const
    {
        return m_worldId;
    }

    /// @brief Find all GameObjects with a specific component type
    template <typename T> [[nodiscard]] std::vector<GameObject *> FindObjectsWithComponent() const;

    /// @brief Find the first GameObject with a given tag
    [[nodiscard]] GameObject *FindWithTag(const std::string &tag) const;

    /// @brief Find all GameObjects with a given tag
    [[nodiscard]] std::vector<GameObject *> FindGameObjectsWithTag(const std::string &tag) const;

    /// @brief Find all GameObjects in a given layer
    [[nodiscard]] std::vector<GameObject *> FindGameObjectsInLayer(int layer) const;

    // ========================================================================
    // Camera
    // ========================================================================

    /// @brief Get the main camera for this scene
    [[nodiscard]] Camera *GetMainCamera() const
    {
        return m_mainCamera;
    }

    /// @brief Set the explicitly preferred game camera for this scene.
    ///
    /// A disabled preferred camera remains assigned and becomes effective
    /// again when re-enabled. While it is unavailable, FindGameCamera()
    /// selects a deterministic active fallback without overwriting this
    /// authored choice.
    void SetMainCamera(Camera *camera);

    /// @brief Find the effective game camera based on authored preference,
    /// active state, depth, and stable component identity.
    /// Skips the editor camera. If m_mainCamera is valid and active, returns it.
    /// Otherwise returns the lowest-depth active Camera without mutating the
    /// explicitly authored main-camera reference.
    /// @param editorCam Editor camera to exclude from search
    /// @return The best game camera, or nullptr if none found
    Camera *FindGameCamera(Camera *editorCam);

    /// @brief Collect all active game cameras in deterministic render order.
    /// Lower depth sorts first; component identity breaks equal-depth ties.
    [[nodiscard]] std::vector<Camera *> GetActiveGameCameras(Camera *editorCam) const;

    // ========================================================================
    // Update loop
    // ========================================================================

    /// @brief Called once at scene start
    void Start();

    /// @brief Called every frame
    void Update(float deltaTime);

    /// @brief Called at a fixed time step (physics / deterministic logic)
    void FixedUpdate(float fixedDeltaTime);

    /// @brief Called every frame after Update
    void LateUpdate(float deltaTime);

    /// @brief Called every frame while not playing; runs edit-mode component updates.
    void EditorUpdate(float deltaTime);

    /// @brief Process pending destroy operations
    void ProcessPendingDestroys();

    /// @brief Queue a component for deferred Start() (runtime add/enable path).
    /// Start will execute before the next simulation/update pass.
    void QueueComponentStart(class Component *component);

    /// @brief Flush queued Start() calls for components that became active.
    void ProcessPendingStarts();

    // ========================================================================
    // Scene state
    // ========================================================================

    [[nodiscard]] bool IsLoaded() const
    {
        return m_isLoaded;
    }
    [[nodiscard]] bool IsPlaying() const
    {
        return m_isPlaying;
    }

    [[nodiscard]] bool HasStarted() const
    {
        return m_hasStarted;
    }

    void SetPlaying(bool playing)
    {
        m_isPlaying = playing;
    }

    /// @brief Monotonically increasing counter bumped whenever the scene
    ///        structure changes (object add/remove/reparent or component
    ///        add/remove).
    ///        Python caches can compare this to their last-seen value to decide
    ///        whether to re-query cached object/component lists.
    [[nodiscard]] uint64_t GetStructureVersion() const
    {
        return m_structureVersion;
    }

    /// @brief Bump structure version (public so that external mutators can signal changes)
    void BumpStructureVersion()
    {
        ++m_structureVersion;
    }

    // ========================================================================
    // Serialization
    // ========================================================================

    /// @brief Serialize scene to JSON string
    /// @return JSON string representation of the scene
    [[nodiscard]] std::string Serialize() const;

    /// @brief Build the structured current-schema document without text conversion.
    [[nodiscard]] nlohmann::json SerializeDocument() const;

    /// @brief Commit an already parsed and cross-language-preflighted scene document.
    ///
    /// **Transactional Scene Rebuild Contract**:
    ///
    ///   1. The complete native graph is built in an isolated staging Scene with
    ///      temporary object/component IDs. Schema, factory, hierarchy, ID and
    ///      main-camera validation complete while the active graph remains live.
    ///   2. A validation failure destroys only staging allocations and leaves the
    ///      active scene, registries, pending queues and lifecycle state unchanged.
    ///   3. Commit clears renderer/physics registries, destroys the old graph,
    ///      adopts staging roots, restores document IDs and registers the new graph.
    ///   4. `AwakeObject()` is a native-only pass. Python component descriptors
    ///      remain pending for Python-side reconstruction after this call returns.
    ///   5. `m_structureVersion` is bumped only after a successful commit.
    ///
    /// @brief Rebuild the scene from an already parsed current-schema document.
    bool DeserializeDocument(const nlohmann::json &document);

    /// Commit a validated candidate while retaining the current native world.
    /// The returned token must be finalized after cross-language publish or
    /// rolled back to restore the exact previous GameObject/Component instances.
    [[nodiscard]] std::shared_ptr<SceneCommitToken> CommitDocumentRetainingCurrentWorld(const nlohmann::json &document);

    /// @brief Atomically save the scene to *path* through a same-directory temporary file.
    /// @return true on success; logs INXLOG_ERROR on failure.
    bool SaveToFile(const std::string &path) const;

    // ========================================================================
    // Pending Python Components (for deserialization)
    // ========================================================================

    /**
     * @brief Info about a Python component that needs to be recreated
     * after scene deserialization. The actual component creation is done
     * by Python code, as C++ cannot directly instantiate Python classes.
     */
    struct PendingPyComponent
    {
        uint64_t gameObjectId = 0; // Which GameObject this belongs to
        std::string typeName;      // Python class name
        std::string scriptGuid;    // GUID for the script asset
        std::string typeGuid;      // GUID for the concrete class within the script
        nlohmann::json fieldsDocument = nlohmann::json::object();
        bool enabled = true;
        int executionOrder = 0;
        size_t componentIndex = 0;
    };

    /// @brief Get pending Python components to be restored (and clear the list)
    [[nodiscard]] std::vector<PendingPyComponent> TakePendingPyComponents()
    {
        std::vector<PendingPyComponent> result;
        result.swap(m_pendingPyComponents);
        return result;
    }

    /// @brief Read pending Python component descriptors without consuming them.
    [[nodiscard]] const std::vector<PendingPyComponent> &GetPendingPyComponents() const
    {
        return m_pendingPyComponents;
    }

    /// @brief Check if there are pending Python components
    [[nodiscard]] bool HasPendingPyComponents() const
    {
        return !m_pendingPyComponents.empty();
    }

    /// @brief Push a pending Python component (used by native clone to avoid JSON round-trip).
    void AddPendingPyComponent(PendingPyComponent pc)
    {
        m_pendingPyComponents.push_back(std::move(pc));
    }

    /// @brief Re-run Awake+OnEnable on a GameObject and its descendants.
    /// Used after undo-driven deserialization to initialise newly-created
    /// C++ components (e.g. MeshRenderer registration).
    void AwakeObject(GameObject *obj);

  private:
    friend class GameObject;
    friend class SceneCommitToken;

    void CollectAllObjects(GameObject *obj, std::vector<GameObject *> &result) const;
    void QueueStartObject(GameObject *obj);
    void StartObject(GameObject *obj);

    /// @brief Shared recursive traversal for all update variants.
    /// @param updateMethod Pointer-to-member on GameObject (e.g. &GameObject::Update).
    void TraverseActiveObjects(GameObject *obj, float dt, void (GameObject::*updateMethod)(float));

    void UpdateObject(GameObject *obj, float deltaTime);
    void FixedUpdateObject(GameObject *obj, float fixedDeltaTime);
    void LateUpdateObject(GameObject *obj, float deltaTime);
    void EditorUpdateObject(GameObject *obj, float deltaTime);
    class Component *FindComponentByID(uint64_t componentId) const;
    bool IsPendingDestroy(const GameObject *obj) const;

    /// @brief Shared recursive GameObject builder from JSON string.
    /// @param preserveIds If true, restores original IDs (Deserialize); otherwise generates new ones (Instantiate).
    std::unique_ptr<GameObject> BuildGameObjectFromJson(const std::string &jsonStr, bool preserveIds);

    /// @brief Internal overload operating on an already-parsed JSON value.
    struct ComponentPrototype
    {
        const nlohmann::json *record = nullptr;
        Component *component = nullptr;
    };
    using ComponentPrototypeCache = std::unordered_map<size_t, std::vector<ComponentPrototype>>;
    std::unique_ptr<GameObject> BuildGameObjectFromJsonImpl(const nlohmann::json &objJson, bool preserveIds,
                                                            ComponentPrototypeCache *prototypeCache = nullptr);

    /// @brief Recursively register all objects in a subtree with Scene's lookup map.
    void RegisterObjectSubtree(GameObject *root);

    static uint64_t GenerateWorldId();

    std::string m_name = "Untitled Scene";
    uint64_t m_worldId = GenerateWorldId();

    // Root-level game objects (objects without parents)
    std::vector<std::unique_ptr<GameObject>> m_rootObjects;

    // Quick lookup by ID
    std::unordered_map<uint64_t, GameObject *> m_objectsById;

    // GameObjects pending destruction (IDs)
    std::vector<uint64_t> m_pendingDestroy;
    std::unordered_set<uint64_t> m_pendingDestroySet;

    // Components pending first Start() (stored by stable component ID)
    std::vector<uint64_t> m_pendingStartComponentIds;
    std::unordered_set<uint64_t> m_pendingStartComponentIdSet; // O(1) dedup

    // Python components pending recreation after deserialize
    std::vector<PendingPyComponent> m_pendingPyComponents;

    // Main camera reference
    Camera *m_mainCamera = nullptr;

    // State flags
    bool m_isLoaded = false;
    bool m_isPlaying = false;
    bool m_hasStarted = false;

    // Per-scene environment (skybox material + ambient) settings
    SceneEnvironmentSettings m_environment;

    // Structure version counter (bumped on add/remove/reparent)
    uint64_t m_structureVersion = 0;

    // Explicit seeks/cuts only. The renderer consumes this monotonic revision
    // once before view extraction and invalidates temporal histories together.
    uint64_t m_temporalDiscontinuityRevision = 0;
};

class SceneCommitToken final
{
  public:
    ~SceneCommitToken();

    SceneCommitToken(const SceneCommitToken &) = delete;
    SceneCommitToken &operator=(const SceneCommitToken &) = delete;

    [[nodiscard]] bool IsActive() const noexcept;
    bool Rollback();
    void Finalize();

  private:
    friend class Scene;
    struct Impl;

    explicit SceneCommitToken(Scene &scene);
    std::unique_ptr<Impl> m_impl;
};

// ============================================================================
// Template implementations
// ============================================================================

template <typename T> std::vector<GameObject *> Scene::FindObjectsWithComponent() const
{
    std::vector<GameObject *> result;
    std::vector<GameObject *> allObjects = GetAllObjects();

    for (GameObject *obj : allObjects) {
        if (obj->GetComponent<T>() != nullptr) {
            result.push_back(obj);
        }
    }

    return result;
}

} // namespace infernux
