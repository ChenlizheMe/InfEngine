#pragma once

#include "Component.h"
#include "ObjectHandle.h"
#include "Transform.h"
#include <memory>
#include <nlohmann/json.hpp>
#include <string>
#include <typeindex>
#include <unordered_map>
#include <vector>

namespace infernux
{

// Forward declaration
class Scene;

/**
 * @brief Base entity in the scene hierarchy.
 *
 * GameObjects are containers for Components. Every GameObject has a Transform
 * component by default. GameObjects can have parent-child relationships.
 *
 * Usage:
 *   auto player = std::make_unique<GameObject>("Player");
 *   player->AddComponent<MeshRenderer>();
 *   player->GetTransform()->SetPosition(0, 1, 0);
 */
class GameObject
{
  public:
    explicit GameObject(const std::string &name = "GameObject");
    ~GameObject();

    // Non-copyable, movable
    GameObject(const GameObject &) = delete;
    GameObject &operator=(const GameObject &) = delete;
    GameObject(GameObject &&) = default;
    GameObject &operator=(GameObject &&) = default;

    // ========================================================================
    // Identity
    // ========================================================================

    [[nodiscard]] const std::string &GetName() const
    {
        return m_name;
    }
    void SetName(const std::string &name)
    {
        m_name = name;
    }

    [[nodiscard]] uint64_t GetID() const
    {
        return m_id;
    }

    /// @brief Get the stable handle for this object's current native lifetime.
    [[nodiscard]] ObjectHandle GetHandle() const;

    [[nodiscard]] uint64_t GetLifetimeGeneration() const
    {
        return m_lifetimeGeneration;
    }

    // ========================================================================
    // Tag & Layer
    // ========================================================================

    [[nodiscard]] const std::string &GetTag() const
    {
        return m_tag;
    }
    void SetTag(const std::string &tag)
    {
        m_tag = tag;
    }
    [[nodiscard]] bool CompareTag(const std::string &tag) const
    {
        return m_tag == tag;
    }

    [[nodiscard]] int GetLayer() const
    {
        return m_layer;
    }
    void SetLayer(int layer);

    // ========================================================================
    // Activation (Unity: activeSelf / activeInHierarchy)
    // ========================================================================

    /// @brief Get local active state. Unity: gameObject.activeSelf
    [[nodiscard]] bool IsActive() const
    {
        return m_active;
    }

    /// @brief Alias for IsActive(). Unity naming: activeSelf
    [[nodiscard]] bool GetActiveSelf() const
    {
        return m_active;
    }

    void SetActive(bool active);

    /// @brief Check if this object and all parents are active. Unity: gameObject.activeInHierarchy
    [[nodiscard]] bool IsActiveInHierarchy() const;

    [[nodiscard]] bool IsDestroying() const
    {
        return m_isDestroying;
    }

    // ========================================================================
    // Static flag (Unity: gameObject.isStatic)
    // ========================================================================

    [[nodiscard]] bool IsStatic() const
    {
        return m_isStatic;
    }
    void SetStatic(bool isStatic)
    {
        m_isStatic = isStatic;
    }

    // ========================================================================
    // DontDestroyOnLoad (Unity: Object.DontDestroyOnLoad)
    // ========================================================================

    [[nodiscard]] bool IsPersistent() const
    {
        return m_persistent;
    }
    void SetPersistent(bool persistent)
    {
        m_persistent = persistent;
    }

    // ========================================================================
    // Prefab instance tracking
    // ========================================================================

    /// @brief GUID of the source .prefab asset (empty = not a prefab instance)
    [[nodiscard]] const std::string &GetPrefabGuid() const
    {
        return m_prefabGuid;
    }
    void SetPrefabGuid(const std::string &guid)
    {
        m_prefabGuid = guid;
    }

    /// @brief True if this object is the root of a prefab instance hierarchy
    [[nodiscard]] bool IsPrefabRoot() const
    {
        return m_prefabRoot;
    }
    void SetPrefabRoot(bool isRoot)
    {
        m_prefabRoot = isRoot;
    }

    /// @brief True if this object belongs to a prefab instance (has a non-empty prefab GUID)
    [[nodiscard]] bool IsPrefabInstance() const
    {
        return !m_prefabGuid.empty();
    }

    // ========================================================================
    // Transform (always available)
    // ========================================================================

    [[nodiscard]] Transform *GetTransform()
    {
        return &m_transform;
    }
    [[nodiscard]] const Transform *GetTransform() const
    {
        return &m_transform;
    }

    // ========================================================================
    // Component management
    // ========================================================================

    /// @brief Add a component of type T
    template <typename T, typename... Args> T *AddComponent(Args &&...args)
    {
        static_assert(std::is_base_of_v<Component, T>, "T must derive from Component");
        static_assert(!std::is_same_v<Transform, T>, "Cannot add Transform component manually");

        auto component = std::make_unique<T>(std::forward<Args>(args)...);
        T *ptr = component.get();
        ptr->SetGameObject(this);

        m_components.push_back(std::move(component));
        PostAddComponent(ptr);
        return ptr;
    }

    /// @brief Get the first component of type T
    template <typename T> [[nodiscard]] T *GetComponent() const
    {
        static_assert(std::is_base_of_v<Component, T>, "T must derive from Component");

        // Special case for Transform
        if constexpr (std::is_same_v<Transform, T>) {
            return const_cast<Transform *>(&m_transform);
        }

        for (const auto &comp : m_components) {
            if (T *casted = dynamic_cast<T *>(comp.get())) {
                return casted;
            }
        }
        return nullptr;
    }

    /// @brief Get all components of type T
    template <typename T> [[nodiscard]] std::vector<T *> GetComponents() const
    {
        static_assert(std::is_base_of_v<Component, T>, "T must derive from Component");

        std::vector<T *> result;
        for (const auto &comp : m_components) {
            if (T *casted = dynamic_cast<T *>(comp.get())) {
                result.push_back(casted);
            }
        }
        return result;
    }

    /// @brief Check if the GameObject has a component of type T
    template <typename T> [[nodiscard]] bool HasComponent() const
    {
        return GetComponent<T>() != nullptr;
    }

    /// @brief Get a component of type T on this or any child GameObject. Unity: GetComponentInChildren<T>()
    template <typename T> [[nodiscard]] T *GetComponentInChildren() const
    {
        static_assert(std::is_base_of_v<Component, T>, "T must derive from Component");
        // Check self first
        T *comp = GetComponent<T>();
        if (comp)
            return comp;
        // Search children recursively
        for (const auto &child : m_children) {
            comp = child->GetComponentInChildren<T>();
            if (comp)
                return comp;
        }
        return nullptr;
    }

    /// @brief Get a component of type T on this or any parent GameObject. Unity: GetComponentInParent<T>()
    template <typename T> [[nodiscard]] T *GetComponentInParent() const
    {
        static_assert(std::is_base_of_v<Component, T>, "T must derive from Component");
        // Check self first
        T *comp = GetComponent<T>();
        if (comp)
            return comp;
        // Walk up the hierarchy
        if (m_parent)
            return m_parent->GetComponentInParent<T>();
        return nullptr;
    }

    /// @brief Get all components of type T on this and all child GameObjects. Unity: GetComponentsInChildren<T>()
    template <typename T> [[nodiscard]] std::vector<T *> GetComponentsInChildren() const
    {
        static_assert(std::is_base_of_v<Component, T>, "T must derive from Component");
        std::vector<T *> result;
        // Check self
        for (const auto &comp : m_components) {
            if (T *casted = dynamic_cast<T *>(comp.get())) {
                result.push_back(casted);
            }
        }
        // Search children recursively
        for (const auto &child : m_children) {
            auto childComps = child->GetComponentsInChildren<T>();
            result.insert(result.end(), childComps.begin(), childComps.end());
        }
        return result;
    }

    /// @brief Get all components of type T on this and all parent GameObjects. Unity: GetComponentsInParent<T>()
    template <typename T> [[nodiscard]] std::vector<T *> GetComponentsInParent() const
    {
        static_assert(std::is_base_of_v<Component, T>, "T must derive from Component");
        std::vector<T *> result;
        // Check self
        for (const auto &comp : m_components) {
            if (T *casted = dynamic_cast<T *>(comp.get())) {
                result.push_back(casted);
            }
        }
        // Walk up the hierarchy
        if (m_parent) {
            auto parentComps = m_parent->GetComponentsInParent<T>();
            result.insert(result.end(), parentComps.begin(), parentComps.end());
        }
        return result;
    }

    /// @brief Remove the first component of type T
    template <typename T> bool RemoveComponent()
    {
        static_assert(std::is_base_of_v<Component, T>, "T must derive from Component");
        static_assert(!std::is_same_v<Transform, T>, "Cannot remove Transform component");

        for (auto it = m_components.begin(); it != m_components.end(); ++it) {
            if (dynamic_cast<T *>(it->get())) {
                return RemoveComponent(it->get());
            }
        }
        return false;
    }

    /// @brief Get all components
    [[nodiscard]] const std::vector<std::unique_ptr<Component>> &GetAllComponents() const
    {
        return m_components;
    }

    /// @brief Get components sorted by execution order (ascending), then by stable component ID.
    [[nodiscard]] std::vector<Component *> GetComponentsInExecutionOrder() const;

    /// @brief Return the serialized/Inspector component order.
    /// Transform is structurally fixed and is intentionally excluded.
    [[nodiscard]] std::vector<uint64_t> GetComponentOrder() const;

    /// @brief Atomically replace the serialized/Inspector component order.
    /// The IDs must be an exact permutation of the attached components.
    bool SetComponentOrder(const std::vector<uint64_t> &componentIds);

    /// @brief Build the default current-schema document for an attached native component.
    /// Stable identity and execution order are preserved in the returned document.
    [[nodiscard]] nlohmann::json GetDefaultComponentDocument(Component *component) const;

    /// @brief Add a pre-created component (used for PyComponentProxy)
    Component *AddExistingComponent(std::unique_ptr<Component> component);

    /// Attach a deserialized Python proxy without Reset/Awake/OnEnable.
    /// Scene publication activates the complete batch only after every proxy
    /// is attached, re-keyed, and its post-deserialize hook succeeds.
    Component *AddPreparedPythonComponent(std::unique_ptr<Component> component, size_t componentIndex);

    /// Activate one proxy previously attached by AddPreparedPythonComponent.
    void ActivatePreparedPythonComponent(Component *component);

    /// Remove an unactivated prepared proxy without RequireComponent blocking.
    bool RemovePreparedPythonComponent(Component *component);

    /// @brief Add a component by registered type name
    Component *AddComponentByTypeName(const std::string &typeName);

    [[nodiscard]] std::vector<std::string> GetAddComponentBlockers(const std::string &typeName) const;
    [[nodiscard]] bool CanAddComponentByTypeName(const std::string &typeName) const
    {
        return GetAddComponentBlockers(typeName).empty();
    }

    /// @brief Remove a component instance by pointer
    bool RemoveComponent(Component *component);

    /// Replace one Python proxy in place without running the removed script's
    /// lifecycle. Script asset deletion and hot reload are identity-preserving
    /// editor operations, not user-requested component destruction.
    Component *ReplacePythonComponent(Component *current, std::unique_ptr<Component> replacement);

    /// @brief Check whether a component can be removed (not blocked by RequireComponent).
    ///
    /// Returns false when another sibling component declares a requirement (via
    /// GetRequiredComponentTypes) that only `component` satisfies.
    [[nodiscard]] bool CanRemoveComponent(Component *component) const;

    /// @brief Get the sibling component type names that block removing `component`.
    ///
    /// If empty, removal is allowed. Each returned entry is a sibling component
    /// whose RequireComponent contract would be violated by removing `component`.
    [[nodiscard]] std::vector<std::string> GetRemovalBlockingComponentTypes(Component *component) const;

    // ========================================================================
    // Hierarchy
    // ========================================================================

    [[nodiscard]] GameObject *GetParent() const
    {
        return m_parent;
    }

    // Return reference to unique_ptrs?
    // No, for compatibility and ease of use in C++, providing raw pointers is often better if ownership is internal.
    // However, for strict C++17 RAII, we expose the structure.
    // To minimize breakage, we can return a constructed vector of pointers, or change the API to return the unique_ptrs
    // const ref. Let's go with const ref to storage for performance.
    [[nodiscard]] const std::vector<std::unique_ptr<GameObject>> &GetChildren() const
    {
        return m_children;
    }

    [[nodiscard]] size_t GetChildCount() const
    {
        return m_children.size();
    }
    [[nodiscard]] GameObject *GetChild(size_t index) const;

    /// @brief Set parent (nullptr for root). worldPositionStays preserves world transform.
    void SetParent(GameObject *parent, bool worldPositionStays = true);

    /// @brief Internal: Attach a child (takes ownership)
    void AttachChild(std::unique_ptr<GameObject> child);

    /// @brief Reorder a child to a new sibling index
    void SetChildSiblingIndex(GameObject *child, int newIndex);

    /// @brief Internal: Detach a child (returns ownership)
    std::unique_ptr<GameObject> DetachChild(GameObject *child);

    /// @brief Find a child by name (non-recursive)
    [[nodiscard]] GameObject *FindChild(const std::string &name) const;

    /// @brief Find a descendant by name (recursive)
    [[nodiscard]] GameObject *FindDescendant(const std::string &name) const;

    // ========================================================================
    // Scene
    // ========================================================================

    [[nodiscard]] Scene *GetScene() const
    {
        return m_scene;
    }

    // ========================================================================
    // Lifecycle (called by Scene)
    // ========================================================================

    void Update(float deltaTime);
    void FixedUpdate(float fixedDeltaTime);
    void LateUpdate(float deltaTime);
    void EditorUpdate(float deltaTime);

    // ========================================================================
    // Serialization
    // ========================================================================

    /// @brief Serialize GameObject and all components to JSON string
    [[nodiscard]] std::string Serialize() const;

    /// @brief Build the structured current-schema document without text conversion.
    [[nodiscard]] nlohmann::json SerializeDocument() const;

    /// @brief Commit an already parsed and cross-language-preflighted document.
    bool DeserializeDocument(const nlohmann::json &document, bool preserveDocumentIds = true);

    /// @brief Deep clone this GameObject and all children (native, no JSON).
    /// Creates fresh IDs for all objects and components. Python components
    /// are pushed to the Scene's pending list for Python-side reconstruction.
    /// @param scene The scene to associate the clone with (for pending py components)
    /// @return A new detached GameObject hierarchy (caller must attach to parent/scene)
    [[nodiscard]] std::unique_ptr<GameObject> Clone(Scene *scene) const;

    /// @brief Ensure ID generator is ahead of a given ID (for deserialization)
    static void EnsureNextID(uint64_t id);

  private:
    friend class Scene;
    friend class SceneCommitToken;
    friend class SceneManager;
    friend void InvalidateGameObjectLifecycleCaches(GameObject *gameObject);

    void SetScene(Scene *scene);

    void PostAddComponent(Component *component);
    void HandleActiveStateChanged(bool wasActiveInHierarchy, bool isActiveInHierarchy);
    void InvalidateComponentExecutionCache();
    void RefreshLifecycleDispatchFlags();
    [[nodiscard]] const std::vector<Component *> &GetComponentsInExecutionOrderCached() const;

    void CollectAllDescendants(std::vector<GameObject *> &out) const;

    static uint64_t GenerateID();

    std::string m_name;
    uint64_t m_id;
    uint64_t m_lifetimeGeneration;
    bool m_active = true;
    bool m_isDestroying = false;
    bool m_isStatic = false;
    bool m_persistent = false;
    bool m_hasPyProxy = false; // true when a PyComponentProxy is attached
    bool m_hasUpdateReceivers = false;
    bool m_hasFixedUpdateReceivers = false;
    bool m_hasLateUpdateReceivers = false;
    std::string m_tag = "Untagged";
    int m_layer = 0; // Default layer

    Transform m_transform;
    std::vector<std::unique_ptr<Component>> m_components;
    mutable std::vector<Component *> m_executionOrderCache;
    mutable bool m_executionOrderCacheDirty = true;

    GameObject *m_parent = nullptr;
    std::vector<std::unique_ptr<GameObject>> m_children;

    Scene *m_scene = nullptr;

    std::string m_prefabGuid;  // GUID of source .prefab asset
    bool m_prefabRoot = false; // true only on the root of a prefab instance
};

} // namespace infernux
