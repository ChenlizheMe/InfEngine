#pragma once

/**
 * @file PhysicsECSStore.h
 * @brief Contiguous memory pools for Collider and Rigidbody hot data.
 *
 * Mirrors the TransformECSStore pattern: Component classes hold a Handle into
 * the pool; every-frame sync loops iterate contiguous data for cache-friendly
 * access.
 */

#include "core/types/InxContiguousPool.h"
#include <glm/glm.hpp>
#include <glm/gtc/quaternion.hpp>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace infernux
{

class Collider;
class GameObject;
class Rigidbody;

struct PhysicsActorData
{
    GameObject *owner = nullptr;
    Collider *primaryCollider = nullptr;
    Rigidbody *rigidbody = nullptr;
    uint32_t bodyId = 0xFFFFFFFF;
    uint32_t colliderCount = 0;
    bool bodyInBroadphase = false;
    glm::vec3 lastSyncedPos{0.0f};
    glm::quat lastSyncedRot{1.0f, 0.0f, 0.0f, 0.0f};
    glm::vec3 lastScale{1.0f};
    uint64_t shapeRevision = 0;
    uint64_t transformRevision = 0;
    bool transformDirtyQueued = false;
};

using PhysicsActorPool = InxContiguousPool<PhysicsActorData>;
using PhysicsActorHandle = PhysicsActorPool::Handle;

// ============================================================================
// Pooled Collider data (hot path — touched every SyncCollidersToPhysics)
// ============================================================================

struct ColliderECSData
{
    // ---- Identity ----
    Collider *owner = nullptr;

    PhysicsActorHandle actorHandle;

    // ---- Properties ----
    bool isTrigger = false;
    glm::vec3 center{0.0f};

    // ---- Misc ----
    bool deserialized = false;
};

// ============================================================================
// Pooled Rigidbody data (hot path — touched every SyncPhysicsToTransform)
// ============================================================================

struct RigidbodyECSData
{
    // ---- Identity ----
    Rigidbody *owner = nullptr;

    // ---- Serialized properties ----
    float mass = 1.0f;
    float drag = 0.0f;
    float angularDrag = 0.05f;
    bool useGravity = true;
    bool isKinematic = false;
    int constraints = 0;
    int collisionDetectionMode = 0;
    int interpolation = 1;
    float maxAngularVelocity = 7.0f;
    float maxLinearVelocity = 500.0f;

    // ---- Runtime velocity state (also survives deferred body creation) ----
    glm::vec3 linearVelocity{0.0f};
    glm::vec3 angularVelocity{0.0f};
    bool hasLinearVelocity = false;
    bool hasAngularVelocity = false;

    // ---- Sync cache (external-move detection) ----
    glm::vec3 lastSyncedPosition{0.0f};
    glm::quat lastSyncedRotation{1.0f, 0.0f, 0.0f, 0.0f};
    bool hasSyncedOnce = false;

    // ---- Physics interpolation cache ----
    glm::vec3 previousPhysicsPosition{0.0f};
    glm::quat previousPhysicsRotation{1.0f, 0.0f, 0.0f, 0.0f};
    glm::vec3 currentPhysicsPosition{0.0f};
    glm::quat currentPhysicsRotation{1.0f, 0.0f, 0.0f, 0.0f};
    bool hasPhysicsPose = false;
    bool wasSleeping = false;
};

// ============================================================================
// PhysicsECSStore — singleton managing both pools
// ============================================================================

class PhysicsECSStore
{
  public:
    using ColliderHandle = InxContiguousPool<ColliderECSData>::Handle;
    using RigidbodyHandle = InxContiguousPool<RigidbodyECSData>::Handle;
    using ActorHandle = PhysicsActorHandle;

    static PhysicsECSStore &Instance();

    // ---- Physics actor pool (one slot per GameObject with colliders) ----
    ActorHandle AcquireActor(GameObject *owner, Collider *collider);
    void ReleaseActor(ActorHandle handle, Collider *collider);
    [[nodiscard]] bool IsValid(ActorHandle handle) const;
    PhysicsActorData &GetActor(ActorHandle handle);
    const PhysicsActorData &GetActor(ActorHandle handle) const;
    [[nodiscard]] size_t GetAliveActorCount() const
    {
        return m_actorPool.AliveCount();
    }

    // ---- Collider pool ----
    ColliderHandle AllocateCollider(Collider *owner);
    void ReleaseCollider(ColliderHandle handle);
    [[nodiscard]] bool IsValid(ColliderHandle handle) const;
    ColliderECSData &GetCollider(ColliderHandle handle);
    const ColliderECSData &GetCollider(ColliderHandle handle) const;
    [[nodiscard]] std::vector<ColliderHandle> GetAliveColliderHandles() const
    {
        return m_colliderPool.GetAliveHandles();
    }
    [[nodiscard]] size_t GetAliveColliderCount() const
    {
        return m_colliderPool.AliveCount();
    }

    /// Zero-allocation iteration over all alive colliders.
    /// @p func receives (ColliderECSData &data).
    template <typename Func> void ForEachAliveCollider(Func &&func)
    {
        m_colliderPool.ForEachAlive(std::forward<Func>(func));
    }

    template <typename Func> void ForEachAliveCollider(Func &&func) const
    {
        m_colliderPool.ForEachAlive(std::forward<Func>(func));
    }

    // ---- Dirty collider tracking (Unity-style deferred sync) ----

    /// Mark a collider as needing transform→physics sync before the next physics step.
    void MarkColliderDirty(ColliderHandle handle);

    /// Mark the single shared physics actor owned by a GameObject dirty.
    /// Avoids allocating/scanning a temporary component list from Transform observers.
    void MarkGameObjectDirty(GameObject *owner);

    /// Consume the dirty set.  Returns handles that were dirty (moved since last flush).
    /// Clears the internal set atomically.
    /// Consume dirty actor primaries into retained scratch storage.
    /// The returned reference remains valid until the next call.
    const std::vector<ColliderHandle> &ConsumeDirtyColliders();

    /// True if any collider has been marked dirty since last consume.
    [[nodiscard]] bool HasDirtyColliders() const
    {
        return !m_dirtyActorList.empty();
    }

    /// Mark all alive colliders dirty (used for force-sync scenarios).
    void MarkAllCollidersDirty();

    /// Monotonic source revision for renderer-owned collision mirrors. This is
    /// independent of ConsumeDirtyColliders(), which remains owned by PhysX/Jolt
    /// synchronization and may only be consumed once.
    void NotifyCollisionSceneChanged() noexcept;
    [[nodiscard]] uint64_t GetCollisionSceneRevision() const noexcept
    {
        return m_collisionSceneRevision;
    }

    // ---- Pending body creation queue (deferred from Collider::Awake) ----

    /// Queue a collider for deferred Jolt body creation at the next pre-physics flush.
    /// This is the main batch-creation optimization: add_component("BoxCollider")
    /// becomes near-zero cost during the Python loop; the actual Jolt body is
    /// created in SceneManager::FlushPendingBroadphase().
    void QueueBodyCreation(ColliderHandle handle);

    /// Consume pending body creation queue.  Returns handles (dead entries filtered).
    std::vector<ColliderHandle> ConsumePendingBodyCreations();

    /// True if any colliders are waiting for body creation.
    [[nodiscard]] bool HasPendingBodyCreations() const
    {
        return !m_pendingBodyCreationSet.empty();
    }

    // ---- Pending broadphase queue (deferred body activation) ----

    /// Queue a body ID for batch broadphase addition at next flush.
    void QueueBroadphaseAdd(uint32_t bodyId, bool isStatic);

    /// Consume pending broadphase additions.  Returns pairs of (bodyId, isStatic).
    std::vector<std::pair<uint32_t, bool>> ConsumePendingBroadphaseAdds();

    /// True if any bodies are waiting to be added to the broadphase.
    [[nodiscard]] bool HasPendingBroadphaseAdds() const
    {
        return !m_pendingBroadphaseAdds.empty();
    }

    /// Pre-allocate internal pools and queues for @p count new colliders.
    void ReserveForBulkCreation(size_t count)
    {
        m_colliderPool.Reserve(m_colliderPool.Capacity() + count);
        m_actorPool.Reserve(m_actorPool.Capacity() + count);
        m_pendingBodyCreationList.reserve(m_pendingBodyCreationList.size() + count);
        m_pendingBodyCreationSet.reserve(m_pendingBodyCreationSet.size() + count);
        m_pendingBroadphaseAdds.reserve(m_pendingBroadphaseAdds.size() + count);
        m_pendingBroadphaseSet.reserve(m_pendingBroadphaseSet.size() + count);
    }

    /// Clear all pending queues (body creation + broadphase adds + dirty tracking).
    /// Must be called before scene rebuild so stale handle.index entries in the
    /// dedup sets don't block newly allocated colliders that reuse pool slots.
    void ClearPendingQueues();

    // ---- Rigidbody pool ----
    RigidbodyHandle AllocateRigidbody(Rigidbody *owner);
    void ReleaseRigidbody(RigidbodyHandle handle);
    [[nodiscard]] bool IsValid(RigidbodyHandle handle) const;
    RigidbodyECSData &GetRigidbody(RigidbodyHandle handle);
    const RigidbodyECSData &GetRigidbody(RigidbodyHandle handle) const;
    [[nodiscard]] std::vector<RigidbodyHandle> GetAliveRigidbodyHandles() const
    {
        return m_rigidbodyPool.GetAliveHandles();
    }
    [[nodiscard]] size_t GetAliveRigidbodyCount() const
    {
        return m_rigidbodyPool.AliveCount();
    }

    /// Null out the Rigidbody pointer on every actor that references *dying*.
    /// Idempotent and safe when no actor references the component.
    void ScrubCachedRigidbody(Rigidbody *dying);

    /// Zero-allocation iteration over all alive rigidbodies.
    template <typename Func> void ForEachAliveRigidbody(Func &&func)
    {
        m_rigidbodyPool.ForEachAlive(std::forward<Func>(func));
    }

    template <typename Func> void ForEachAliveRigidbody(Func &&func) const
    {
        m_rigidbodyPool.ForEachAlive(std::forward<Func>(func));
    }

  private:
    PhysicsECSStore() = default;

    InxContiguousPool<ColliderECSData> m_colliderPool;
    PhysicsActorPool m_actorPool;
    InxContiguousPool<RigidbodyECSData> m_rigidbodyPool;
    std::unordered_map<GameObject *, ActorHandle> m_actorByOwner;

    // Dirty collider tracking — colliders whose Transform changed and need physics sync.
    std::vector<ActorHandle> m_dirtyActorList;
    std::vector<ColliderHandle> m_dirtyColliderScratch;
    uint64_t m_collisionSceneRevision = 1;

    // Pending body creation queue — colliders that deferred RegisterBody.
    std::vector<ColliderHandle> m_pendingBodyCreationList;
    std::unordered_set<uint64_t> m_pendingBodyCreationSet; // generation-aware handle dedup

    // Pending broadphase add queue — (bodyId, isStatic) pairs.
    std::vector<std::pair<uint32_t, bool>> m_pendingBroadphaseAdds;
    std::unordered_set<uint32_t> m_pendingBroadphaseSet; // deduplicate
};

} // namespace infernux
