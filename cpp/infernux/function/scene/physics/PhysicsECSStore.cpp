/**
 * @file PhysicsECSStore.cpp
 * @brief Contiguous memory pools for Collider and Rigidbody data.
 *
 * Lifecycle invariants enforced by this file:
 *
 *   * Allocate*() always zero-initialises the entry before stamping the owner,
 *     so reused pool slots never inherit stale state from a previous owner.
 *   * Release*() always nulls the owner pointer first, then frees the slot;
 *     callers may safely observe a half-released entry (owner == nullptr,
 *     IsAlive == false).
 *   * Releasing a Rigidbody invalidates every PhysicsActorData::rigidbody
 *     pointer that references it.
 * Rigidbody::OnDisable() is the primary cleaner;
 *     ScrubCachedRigidbody() is the safety net for paths that bypass
 * it (editor undo/redo on inactive objects, scene rebuild order anomalies).
 *   * The pending-queue dedup sets MUST be cleared whenever pool slots may be
 *     recycled; ClearPendingQueues() is the single chokepoint for that — see
 *     SceneManager::ClearComponentRegistries.
 */

#include "PhysicsECSStore.h"
#include "../Collider.h"
#include <core/config/EngineConfig.h>

#include <algorithm>
#include <stdexcept>

namespace infernux
{

namespace
{
uint64_t ColliderHandleKey(PhysicsECSStore::ColliderHandle handle)
{
    return (static_cast<uint64_t>(handle.generation) << 32U) | handle.index;
}

uint64_t ActorHandleKey(PhysicsECSStore::ActorHandle handle)
{
    return (static_cast<uint64_t>(handle.generation) << 32U) | handle.index;
}

template <typename HandleList>
std::vector<PhysicsECSStore::ColliderHandle> CollapseToActorPrimary(HandleList &handles, const PhysicsECSStore &store)
{
    std::vector<PhysicsECSStore::ColliderHandle> result;
    result.reserve(handles.size());
    std::unordered_set<uint64_t> seenActors;
    for (const auto handle : handles) {
        if (!store.IsValid(handle))
            continue;
        const auto &colliderData = store.GetCollider(handle);
        const auto actorHandle = colliderData.actorHandle;
        if (!store.IsValid(actorHandle)) {
            result.push_back(handle);
            continue;
        }
        const auto &actor = store.GetActor(actorHandle);
        Collider *primary = actor.primaryCollider;
        if (!primary || !primary->IsEnabled())
            primary = colliderData.owner;
        if (!primary || !primary->IsEnabled())
            continue;
        if (!seenActors.insert(ActorHandleKey(actorHandle)).second)
            continue;
        if (primary && store.IsValid(primary->GetECSHandle()))
            result.push_back(primary->GetECSHandle());
    }
    return result;
}
} // namespace

PhysicsECSStore &PhysicsECSStore::Instance()
{
    // Intentionally leaked (see SceneManager::Instance) — Collider
    // destructors must always find a live store.
    static PhysicsECSStore *instance = new PhysicsECSStore();
    return *instance;
}

PhysicsECSStore::ActorHandle PhysicsECSStore::AcquireActor(GameObject *owner, Collider *collider)
{
    if (!owner || !collider)
        throw std::invalid_argument("Physics actor requires an owner and collider");
    auto existing = m_actorByOwner.find(owner);
    if (existing != m_actorByOwner.end()) {
        if (!m_actorPool.IsAlive(existing->second))
            throw std::logic_error("Physics actor owner map contains a stale handle");
        auto &actor = m_actorPool.Get(existing->second);
        ++actor.colliderCount;
        if (!actor.primaryCollider || (!actor.primaryCollider->IsEnabled() && collider->IsEnabled()))
            actor.primaryCollider = collider;
        return existing->second;
    }

    ActorHandle handle = m_actorPool.Allocate();
    auto &actor = m_actorPool.Get(handle);
    actor = PhysicsActorData{};
    actor.owner = owner;
    actor.primaryCollider = collider->IsEnabled() ? collider : nullptr;
    actor.colliderCount = 1;
    m_actorByOwner.emplace(owner, handle);
    return handle;
}

void PhysicsECSStore::ReleaseActor(ActorHandle handle, Collider *collider)
{
    if (!m_actorPool.IsAlive(handle))
        return;
    auto &actor = m_actorPool.Get(handle);
    if (actor.colliderCount == 0)
        throw std::logic_error("Physics actor collider reference count underflow");
    if (actor.primaryCollider == collider)
        actor.primaryCollider = nullptr;
    --actor.colliderCount;
    if (actor.colliderCount != 0)
        return;
    if (actor.bodyId != 0xFFFFFFFF)
        throw std::logic_error("Physics actor released while its Jolt body is still alive");
    m_actorByOwner.erase(actor.owner);
    actor = PhysicsActorData{};
    m_actorPool.Free(handle);
}

bool PhysicsECSStore::IsValid(ActorHandle handle) const
{
    return m_actorPool.IsAlive(handle);
}

PhysicsActorData &PhysicsECSStore::GetActor(ActorHandle handle)
{
    return m_actorPool.Get(handle);
}

const PhysicsActorData &PhysicsECSStore::GetActor(ActorHandle handle) const
{
    return m_actorPool.Get(handle);
}

// ============================================================================
// Collider pool
// ============================================================================

PhysicsECSStore::ColliderHandle PhysicsECSStore::AllocateCollider(Collider *owner)
{
    ColliderHandle handle = m_colliderPool.Allocate();
    ColliderECSData &data = m_colliderPool.Get(handle);
    data = ColliderECSData{};
    data.owner = owner;
    NotifyCollisionSceneChanged();
    return handle;
}

void PhysicsECSStore::ReleaseCollider(ColliderHandle handle)
{
    if (!m_colliderPool.IsAlive(handle))
        return;
    m_colliderPool.Get(handle).owner = nullptr;
    m_colliderPool.Free(handle);
    NotifyCollisionSceneChanged();
}

bool PhysicsECSStore::IsValid(ColliderHandle handle) const
{
    return m_colliderPool.IsAlive(handle);
}

ColliderECSData &PhysicsECSStore::GetCollider(ColliderHandle handle)
{
    return m_colliderPool.Get(handle);
}

const ColliderECSData &PhysicsECSStore::GetCollider(ColliderHandle handle) const
{
    return m_colliderPool.Get(handle);
}

// ============================================================================
// Rigidbody pool
// ============================================================================

PhysicsECSStore::RigidbodyHandle PhysicsECSStore::AllocateRigidbody(Rigidbody *owner)
{
    RigidbodyHandle handle = m_rigidbodyPool.Allocate();
    RigidbodyECSData &data = m_rigidbodyPool.Get(handle);
    data = RigidbodyECSData{};
    const auto &config = EngineConfig::Get();
    data.mass = config.defaultRigidbodyMass;
    data.drag = config.defaultRigidbodyDrag;
    data.angularDrag = config.defaultRigidbodyAngularDrag;
    data.maxAngularVelocity = config.defaultMaxAngularVelocity;
    data.maxLinearVelocity = config.defaultMaxLinearVelocity;
    data.owner = owner;
    return handle;
}

void PhysicsECSStore::ScrubCachedRigidbody(Rigidbody *dying)
{
    if (!dying)
        return;
    m_actorPool.ForEachAlive([dying](PhysicsActorData &actor) {
        if (actor.rigidbody == dying)
            actor.rigidbody = nullptr;
    });
}

void PhysicsECSStore::ReleaseRigidbody(RigidbodyHandle handle)
{
    if (!m_rigidbodyPool.IsAlive(handle))
        return;

    // Safety net: see ScrubCachedRigidbody. Rigidbody::OnDisable() is the
    // primary cleaner, but editor undo/redo on inactive components and other
    // bypass paths reach Release without going through OnDisable; without this
    // scrub, sibling colliders would dereference a freed Rigidbody.
    ScrubCachedRigidbody(m_rigidbodyPool.Get(handle).owner);

    m_rigidbodyPool.Get(handle).owner = nullptr;
    m_rigidbodyPool.Free(handle);
}

bool PhysicsECSStore::IsValid(RigidbodyHandle handle) const
{
    return m_rigidbodyPool.IsAlive(handle);
}

RigidbodyECSData &PhysicsECSStore::GetRigidbody(RigidbodyHandle handle)
{
    return m_rigidbodyPool.Get(handle);
}

const RigidbodyECSData &PhysicsECSStore::GetRigidbody(RigidbodyHandle handle) const
{
    return m_rigidbodyPool.Get(handle);
}

// ============================================================================
// Dirty collider tracking
// ============================================================================

void PhysicsECSStore::MarkColliderDirty(ColliderHandle handle)
{
    if (!m_colliderPool.IsAlive(handle))
        return;
    NotifyCollisionSceneChanged();
    const ActorHandle actorHandle = m_colliderPool.Get(handle).actorHandle;
    if (!m_actorPool.IsAlive(actorHandle))
        return;
    auto &actor = m_actorPool.Get(actorHandle);
    if (actor.transformDirtyQueued)
        return;
    actor.transformDirtyQueued = true;
    m_dirtyActorList.push_back(actorHandle);
}

void PhysicsECSStore::MarkGameObjectDirty(GameObject *owner)
{
    if (!owner)
        return;
    const auto found = m_actorByOwner.find(owner);
    if (found == m_actorByOwner.end() || !m_actorPool.IsAlive(found->second))
        return;
    NotifyCollisionSceneChanged();
    auto &actor = m_actorPool.Get(found->second);
    if (actor.transformDirtyQueued)
        return;
    actor.transformDirtyQueued = true;
    m_dirtyActorList.push_back(found->second);
}

const std::vector<PhysicsECSStore::ColliderHandle> &PhysicsECSStore::ConsumeDirtyColliders()
{
    m_dirtyColliderScratch.clear();
    m_dirtyColliderScratch.reserve(m_dirtyActorList.size());
    for (const ActorHandle actorHandle : m_dirtyActorList) {
        if (!m_actorPool.IsAlive(actorHandle))
            continue;
        auto &actor = m_actorPool.Get(actorHandle);
        actor.transformDirtyQueued = false;
        Collider *primary = actor.primaryCollider;
        if (primary && primary->IsEnabled() && m_colliderPool.IsAlive(primary->GetECSHandle()))
            m_dirtyColliderScratch.push_back(primary->GetECSHandle());
    }
    m_dirtyActorList.clear();
    return m_dirtyColliderScratch;
}

void PhysicsECSStore::NotifyCollisionSceneChanged() noexcept
{
    ++m_collisionSceneRevision;
    if (m_collisionSceneRevision == 0)
        ++m_collisionSceneRevision;
}

void PhysicsECSStore::MarkAllCollidersDirty()
{
    NotifyCollisionSceneChanged();
    for (const ActorHandle actorHandle : m_dirtyActorList) {
        if (m_actorPool.IsAlive(actorHandle))
            m_actorPool.Get(actorHandle).transformDirtyQueued = false;
    }
    m_dirtyActorList.clear();

    const auto actors = m_actorPool.GetAliveHandles();
    m_dirtyActorList.reserve(actors.size());
    for (const ActorHandle actorHandle : actors) {
        auto &actor = m_actorPool.Get(actorHandle);
        if (!actor.primaryCollider || !actor.primaryCollider->IsEnabled())
            continue;
        actor.transformDirtyQueued = true;
        m_dirtyActorList.push_back(actorHandle);
    }
}

// ============================================================================
// Pending body creation queue
// ============================================================================

void PhysicsECSStore::QueueBodyCreation(ColliderHandle handle)
{
    if (m_colliderPool.IsAlive(handle)) {
        if (m_pendingBodyCreationSet.insert(ColliderHandleKey(handle)).second)
            m_pendingBodyCreationList.push_back(handle);
    }
}

std::vector<PhysicsECSStore::ColliderHandle> PhysicsECSStore::ConsumePendingBodyCreations()
{
    std::vector<ColliderHandle> pending;
    pending.swap(m_pendingBodyCreationList);
    m_pendingBodyCreationSet.clear();
    return CollapseToActorPrimary(pending, *this);
}

// ============================================================================
// Pending broadphase queue
// ============================================================================

void PhysicsECSStore::QueueBroadphaseAdd(uint32_t bodyId, bool isStatic)
{
    if (bodyId == 0xFFFFFFFF)
        return;
    if (m_pendingBroadphaseSet.insert(bodyId).second)
        m_pendingBroadphaseAdds.push_back({bodyId, isStatic});
}

std::vector<std::pair<uint32_t, bool>> PhysicsECSStore::ConsumePendingBroadphaseAdds()
{
    std::vector<std::pair<uint32_t, bool>> result;
    result.swap(m_pendingBroadphaseAdds);
    m_pendingBroadphaseSet.clear();
    return result;
}

void PhysicsECSStore::ClearPendingQueues()
{
    m_pendingBodyCreationList.clear();
    m_pendingBodyCreationSet.clear();
    m_pendingBroadphaseAdds.clear();
    m_pendingBroadphaseSet.clear();
    for (const ActorHandle actorHandle : m_dirtyActorList) {
        if (m_actorPool.IsAlive(actorHandle))
            m_actorPool.Get(actorHandle).transformDirtyQueued = false;
    }
    m_dirtyActorList.clear();
}

} // namespace infernux
