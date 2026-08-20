/**
 * @file Collider.cpp
 * @brief Base Collider implementation — body registration, transform sync.
 */

#include "Collider.h"

#include "GameObject.h"
#include "MeshRenderer.h"
#include "Rigidbody.h"
#include "SceneManager.h"
#include "Transform.h"
#include "physics/PhysicsECSStore.h"
#include "physics/PhysicsWorld.h"
#include <InxLog.h>
#include <core/config/EngineConfig.h>
#include <function/resources/AssetDependencyGraph.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>

#include <algorithm>
#include <cmath>
#include <nlohmann/json.hpp>
#include <stdexcept>

namespace infernux
{

// ============================================================================
// Constructor / Destructor
// ============================================================================

Collider::Collider()
{
    m_ecsHandle = PhysicsECSStore::Instance().AllocateCollider(this);
}

Collider::~Collider()
{
    if (m_physicMaterial.HasGuid())
        AssetDependencyGraph::Instance().RemoveRuntimeDependency(GetInstanceGuid(), m_physicMaterial.GetGuid());

    // Safe: UnregisterBody checks bodyId == 0xFFFFFFFF and returns early
    // if already cleaned up via OnDestroy().
    UnregisterBody();

    auto &store = PhysicsECSStore::Instance();
    if (store.IsValid(Data().actorHandle))
        store.ReleaseActor(Data().actorHandle, this);

    // Release pool slot
    store.ReleaseCollider(m_ecsHandle);
}

void Collider::EnsureActor()
{
    auto &store = PhysicsECSStore::Instance();
    auto &data = DataMut();
    if (store.IsValid(data.actorHandle))
        return;
    auto *gameObject = GetGameObject();
    if (!gameObject)
        throw std::logic_error("Collider must be attached to a GameObject before accessing its physics actor");
    data.actorHandle = store.AcquireActor(gameObject, this);
}

PhysicsActorData &Collider::ActorMut()
{
    EnsureActor();
    return PhysicsECSStore::Instance().GetActor(Data().actorHandle);
}

const PhysicsActorData &Collider::Actor() const
{
    const_cast<Collider *>(this)->EnsureActor();
    return PhysicsECSStore::Instance().GetActor(Data().actorHandle);
}

uint32_t Collider::GetBodyId() const
{
    auto &store = PhysicsECSStore::Instance();
    if (!store.IsValid(Data().actorHandle)) {
        if (!GetGameObject())
            return 0xFFFFFFFF;
        const_cast<Collider *>(this)->EnsureActor();
    }
    return store.GetActor(Data().actorHandle).bodyId;
}

void Collider::SetCachedRigidbody(Rigidbody *rigidbody)
{
    auto &actor = ActorMut();
    if (actor.rigidbody == rigidbody)
        return;
    actor.rigidbody = rigidbody;
    PhysicsECSStore::Instance().NotifyCollisionSceneChanged();
}

Rigidbody *Collider::GetCachedRigidbody() const
{
    return Actor().rigidbody;
}

void Collider::SetLastSyncedTransform(const glm::vec3 &position, const glm::quat &rotation)
{
    auto &actor = ActorMut();
    actor.lastSyncedPos = position;
    actor.lastSyncedRot = rotation;
}

uint32_t Collider::GetSharedBodyId(const GameObject *gameObject)
{
    if (!gameObject)
        return 0xFFFFFFFF;

    uint32_t sharedBodyId = 0xFFFFFFFF;
    for (const auto &component : gameObject->GetAllComponents()) {
        auto *collider = dynamic_cast<Collider *>(component.get());
        if (!collider || collider->GetBodyId() == 0xFFFFFFFF)
            continue;
        if (sharedBodyId == 0xFFFFFFFF) {
            sharedBodyId = collider->GetBodyId();
        } else if (collider->GetBodyId() != sharedBodyId) {
            throw std::logic_error("A GameObject's colliders must share exactly one physics body");
        }
    }
    return sharedBodyId;
}

// ============================================================================
// Lifecycle
// ============================================================================

void Collider::Awake()
{
    EnsureActor();
    // If a sibling Rigidbody already exists and is enabled, cache it before
    // body creation so the body is created as dynamic/kinematic instead of
    // static.  This handles the case where the Collider is added *after* the
    // Rigidbody (whose OnEnable already ran and won't re-fire).
    if (auto *go = GetGameObject()) {
        auto *rb = go->GetComponent<Rigidbody>();
        if (rb && rb->IsEnabled())
            ActorMut().rigidbody = rb;
    }

    if (!Data().deserialized) {
        AutoFitToMesh();
    }

    // Defer body creation to the next pre-physics flush (Unity-style).
    // This is the key batch-creation optimization: add_component("BoxCollider")
    // in a Python loop becomes near-zero physics cost; the actual Jolt bodies
    // are created in one batch inside SceneManager::FlushPendingBroadphase().
    PhysicsECSStore::Instance().QueueBodyCreation(m_ecsHandle);
}

void Collider::OnEnable()
{
    PhysicsECSStore::Instance().NotifyCollisionSceneChanged();
    if (GetBodyId() == 0xFFFFFFFF) {
        // Body not yet created (deferred from Awake) — ensure it's queued.
        PhysicsECSStore::Instance().QueueBodyCreation(m_ecsHandle);
        return;
    }
    auto &actor = ActorMut();
    if (!actor.primaryCollider || !actor.primaryCollider->IsEnabled())
        actor.primaryCollider = this;
    // Re-enable after disable — body already exists, normal path.
    PhysicsWorld::Instance().UpdateBodyShape(this);
    AddToBroadphase();
}

void Collider::OnDisable()
{
    PhysicsECSStore::Instance().NotifyCollisionSceneChanged();
    if (IsBeingDestroyed()) {
        return;
    }

    auto *go = GetGameObject();
    const uint32_t bodyId = GetBodyId();
    if (go && bodyId != 0xFFFFFFFF) {
        bool hasOtherEnabledSibling = false;
        Collider *replacement = nullptr;
        auto colliders = go->GetComponents<Collider>();
        for (auto *col : colliders) {
            if (!col || col == this || !col->IsEnabled())
                continue;
            if (col->GetBodyId() == bodyId) {
                hasOtherEnabledSibling = true;
                replacement = col;
                break;
            }
        }

        if (hasOtherEnabledSibling) {
            ActorMut().primaryCollider = replacement;
            PhysicsWorld::Instance().RebindBodyCollider(bodyId, replacement);
            PhysicsWorld::Instance().UpdateBodyShape(this, this);
        } else if (Actor().primaryCollider == this) {
            ActorMut().primaryCollider = nullptr;
        }
    }

    RemoveFromBroadphase();
}

void Collider::OnDestroy()
{
    UnregisterBody();
}

// ============================================================================
// Property setters (rebuild body when changed)
// ============================================================================

void Collider::SetIsTrigger(bool trigger)
{
    auto &d = DataMut();
    if (d.isTrigger == trigger)
        return;
    d.isTrigger = trigger;
    PhysicsECSStore::Instance().NotifyCollisionSceneChanged();

    const uint32_t bodyId = GetBodyId();
    if (bodyId != 0xFFFFFFFF) {
        bool hasEnabledCollider = false;
        bool groupIsTrigger = true;
        if (auto *go = GetGameObject()) {
            auto colliders = go->GetComponents<Collider>();
            for (auto *col : colliders) {
                if (!col || !col->IsEnabled())
                    continue;
                if (col->GetBodyId() != bodyId)
                    continue;
                hasEnabledCollider = true;
                groupIsTrigger = groupIsTrigger && col->IsTrigger();
            }
        } else {
            hasEnabledCollider = true;
            groupIsTrigger = trigger;
        }
        groupIsTrigger = hasEnabledCollider && groupIsTrigger;

        auto &physics = PhysicsWorld::Instance();
        physics.SetBodyIsSensor(bodyId, groupIsTrigger);

        // Wake dynamic bodies that may be resting on (or overlapping) this
        // collider so they re-evaluate the changed sensor state.
        physics.WakeBodiesTouchingStatic(bodyId);

        // Clear stale contact-pair tracking so the listener produces fresh
        // Enter events (Trigger or Collision) on the next physics step
        // instead of suppressing them as wake-from-sleep duplicates.
        physics.InvalidateContactPairsForBody(bodyId);
    }
}

void Collider::SetCenter(const glm::vec3 &center)
{
    if (!std::isfinite(center.x) || !std::isfinite(center.y) || !std::isfinite(center.z))
        throw std::invalid_argument("collider center must contain finite values");
    auto &d = DataMut();
    if (d.center == center)
        return;
    d.center = center;
    RebuildShape();
}

// ---- Friction / Bounciness helpers ----

/// @brief Update Jolt's body-level fallback material from the primary enabled collider.
/// Actual contacts are combined per subshape by InxContactListener.
static void ApplyMaterialToBody(Collider *self)
{
    const uint32_t bodyId = self->GetBodyId();
    if (bodyId == 0xFFFFFFFF)
        return;

    auto *go = self->GetGameObject();
    if (!go)
        return;

    auto colliders = go->GetComponents<Collider>();
    for (auto *col : colliders) {
        if (!col || !col->IsEnabled())
            continue;
        auto &pw = PhysicsWorld::Instance();
        pw.SetBodyFriction(bodyId, col->GetFriction());
        pw.SetBodyRestitution(bodyId, col->GetBounciness());
        return;
    }
}

std::shared_ptr<PhysicMaterial> Collider::GetPhysicMaterial() const
{
    return m_physicMaterial.Get();
}

const std::string &Collider::GetPhysicMaterialGuid() const
{
    return m_physicMaterial.GetGuid();
}

void Collider::SetPhysicMaterial(std::shared_ptr<PhysicMaterial> material)
{
    const std::string guid = material ? material->GetGuid() : std::string();
    if (m_physicMaterial.Get() == material && m_physicMaterial.GetGuid() == guid)
        return;
    auto &graph = AssetDependencyGraph::Instance();
    if (m_physicMaterial.HasGuid())
        graph.RemoveRuntimeDependency(GetInstanceGuid(), m_physicMaterial.GetGuid());
    const uint64_t runtimeVersion = guid.empty() ? 0 : AssetRegistry::Instance().GetAssetVersion(guid);
    m_physicMaterial = AssetRef<PhysicMaterial>(guid, std::move(material), runtimeVersion);
    if (!guid.empty())
        graph.AddRuntimeDependency(GetInstanceGuid(), guid);
    ApplyMaterialToBody(this);
    PhysicsECSStore::Instance().NotifyCollisionSceneChanged();
}

void Collider::SetPhysicMaterialGuid(const std::string &guid)
{
    if (guid.empty()) {
        ClearPhysicMaterial();
        return;
    }
    auto &graph = AssetDependencyGraph::Instance();
    const std::string oldGuid = m_physicMaterial.GetGuid();
    if (!oldGuid.empty() && oldGuid != guid)
        graph.RemoveRuntimeDependency(GetInstanceGuid(), oldGuid);
    m_physicMaterial.SetGuid(guid);
    AssetRegistry::Instance().Resolve(m_physicMaterial, ResourceType::PhysicMaterial);
    graph.AddRuntimeDependency(GetInstanceGuid(), guid);
    ApplyMaterialToBody(this);
    PhysicsECSStore::Instance().NotifyCollisionSceneChanged();
}

void Collider::ClearPhysicMaterial()
{
    SetPhysicMaterial(nullptr);
}

void Collider::OnPhysicMaterialAssetEvent(AssetEvent event)
{
    if (event == AssetEvent::Deleted) {
        m_physicMaterial.Invalidate();
        ApplyMaterialToBody(this);
        PhysicsECSStore::Instance().NotifyCollisionSceneChanged();
        return;
    }
    if (event == AssetEvent::Modified) {
        m_physicMaterial.MarkStale();
        AssetRegistry::Instance().Resolve(m_physicMaterial, ResourceType::PhysicMaterial);
        ApplyMaterialToBody(this);
        PhysicsECSStore::Instance().NotifyCollisionSceneChanged();
    }
}

float Collider::GetFriction() const
{
    const auto material = m_physicMaterial.Get();
    return material ? material->GetFriction() : EngineConfig::Get().defaultColliderFriction;
}

float Collider::GetBounciness() const
{
    const auto material = m_physicMaterial.Get();
    return material ? material->GetBounciness() : EngineConfig::Get().defaultColliderBounciness;
}

PhysicsMaterialCombine Collider::GetFrictionCombine() const
{
    const auto material = m_physicMaterial.Get();
    return material ? material->GetFrictionCombine() : PhysicsMaterialCombine::Average;
}

PhysicsMaterialCombine Collider::GetBounceCombine() const
{
    const auto material = m_physicMaterial.Get();
    return material ? material->GetBounceCombine() : PhysicsMaterialCombine::Average;
}

// ============================================================================
// Helpers
// ============================================================================

glm::vec3 Collider::GetWorldScale() const
{
    if (auto *go = GetGameObject()) {
        if (auto *tf = go->GetTransform()) {
            return glm::abs(tf->GetWorldScale());
        }
    }
    return glm::vec3(1.0f);
}

void Collider::AutoFitToMesh()
{
    // Default no-op. Derived classes override to set size/radius/height
    // from sibling MeshRenderer bounds.
}

// ============================================================================
// Jolt body management
// ============================================================================

void Collider::RegisterBody()
{
    auto &pw = PhysicsWorld::Instance();
    if (!pw.IsInitialized())
        return;

    auto &actor = ActorMut();

    auto *go = GetGameObject();
    if (!go)
        return;

    // Skip physics body creation for non-runtime scenes (for example prefab
    // template caches). DontDestroyOnLoad owns a dedicated runtime Scene and
    // must remain physically resident across active-scene replacement.
    if (!SceneManager::Instance().IsRuntimeScene(go->GetScene()))
        return;

    if (actor.bodyId != 0xFFFFFFFF) {
        if (!actor.primaryCollider || !actor.primaryCollider->IsEnabled())
            actor.primaryCollider = this;
        pw.UpdateBodyShape(this);
        return;
    }

    auto colliders = go->GetComponents<Collider>();
    bool isStatic = (actor.rigidbody == nullptr || !actor.rigidbody->IsEnabled());

    bool hasEnabledCollider = false;
    bool groupIsTrigger = true;
    for (auto *col : colliders) {
        if (!col || !col->IsEnabled())
            continue;
        hasEnabledCollider = true;
        groupIsTrigger = groupIsTrigger && col->IsTrigger();
    }
    groupIsTrigger = hasEnabledCollider && groupIsTrigger;

    actor.primaryCollider = this;
    actor.bodyId = pw.CreateBody(this, isStatic, groupIsTrigger);
    actor.bodyInBroadphase = false;

    // Initialize cached transform so first SyncTransformToPhysics doesn't
    // see a spurious move from the (0,0,0) default.
    if (auto *tf = go->GetTransform()) {
        glm::quat rot = tf->GetWorldRotation();
        actor.lastSyncedRot = rot;
        actor.lastSyncedPos = tf->GetPosition();
        actor.lastScale = tf->GetWorldScale();
    }

    // ========================================================================
    // Fix component-ordering dependency: If a sibling Rigidbody has already
    // been OnEnable'd (setting cachedRigidbody) but this Collider's body
    // didn't exist yet at that time, NotifyCollidersBodyTypeChanged() would
    // have skipped this body.  Apply the Rigidbody's full configuration now
    // that the body exists.
    // ========================================================================
    if (actor.rigidbody && actor.rigidbody->IsEnabled() && actor.bodyId != 0xFFFFFFFF) {
        int motionType = actor.rigidbody->IsKinematic() ? 1 : 2;
        pw.SetBodyMotionType(actor.bodyId, motionType);
        actor.rigidbody->ApplyConfigurationToBody(actor.bodyId);
    }
}

void Collider::UnregisterBody()
{
    auto &store = PhysicsECSStore::Instance();
    if (!store.IsValid(Data().actorHandle))
        return;
    auto &actor = store.GetActor(Data().actorHandle);
    if (actor.bodyId == 0xFFFFFFFF)
        return;

    auto *go = GetGameObject();
    Collider *replacement = nullptr;
    if (go) {
        auto colliders = go->GetComponents<Collider>();
        for (auto *col : colliders) {
            if (!col || col == this)
                continue;
            if (col->GetBodyId() == actor.bodyId) {
                replacement = col;
                break;
            }
        }
    }

    if (replacement) {
        actor.primaryCollider = replacement;
        PhysicsWorld::Instance().RebindBodyCollider(actor.bodyId, replacement);

        // Teardown fast path: when this collider dies as part of GameObject /
        // scene destruction, every sibling is about to die too. Rebuilding the
        // compound shape per destroyed collider is O(n²) churn with no
        // observable effect — skip it and let the last sibling destroy the body.
        if (!go || !go->IsDestroying()) {
            bool hasOtherEnabledSibling = false;
            if (go) {
                auto colliders = go->GetComponents<Collider>();
                for (auto *col : colliders) {
                    if (!col || col == this || !col->IsEnabled())
                        continue;
                    if (col->GetBodyId() == actor.bodyId) {
                        hasOtherEnabledSibling = true;
                        break;
                    }
                }
            }

            if (hasOtherEnabledSibling) {
                PhysicsWorld::Instance().UpdateBodyShape(replacement, this);
            }
        }
    } else {
        auto &pendingStore = PhysicsECSStore::Instance();
        const bool hadPendingAdd = pendingStore.CancelBroadphaseAdd(actor.bodyId);
        const bool hadPendingRemove = pendingStore.CancelBroadphaseRemove(actor.bodyId);
        // Destruction is already committed at a main-thread safe point. A
        // queued removal means the body is still physically resident even
        // though the logical actor state was changed when it was disabled.
        if (!hadPendingAdd && (actor.bodyInBroadphase || hadPendingRemove)) {
            PhysicsWorld::Instance().RemoveBodyFromBroadphase(actor.bodyId);
        }
        PhysicsWorld::Instance().DestroyBody(this);
        actor.bodyId = 0xFFFFFFFF;
        actor.bodyInBroadphase = false;
        actor.primaryCollider = nullptr;
    }
}

void Collider::AddToBroadphase()
{
    auto &actor = ActorMut();
    if (actor.bodyId == 0xFFFFFFFF)
        return;
    auto &store = PhysicsECSStore::Instance();
    if (store.CancelBroadphaseRemove(actor.bodyId)) {
        actor.bodyInBroadphase = true;
        return;
    }
    if (actor.bodyInBroadphase)
        return;

    bool isStatic = (actor.rigidbody == nullptr || !actor.rigidbody->IsEnabled());

    // Defer broadphase addition to the next pre-physics flush (Unity-style).
    // The body exists in Jolt but won't participate in queries/simulation
    // until SceneManager flushes the pending queue.
    store.QueueBroadphaseAdd(actor.bodyId, isStatic);
    actor.bodyInBroadphase = true;
}

void Collider::RemoveFromBroadphase()
{
    auto &actor = ActorMut();
    if (actor.bodyId == 0xFFFFFFFF || !actor.bodyInBroadphase)
        return;

    auto *go = GetGameObject();
    if (go) {
        auto colliders = go->GetComponents<Collider>();
        for (auto *col : colliders) {
            if (!col || col == this || !col->IsEnabled())
                continue;
            if (col->GetBodyId() == actor.bodyId) {
                return;
            }
        }
    }

    auto &store = PhysicsECSStore::Instance();
    if (!store.CancelBroadphaseAdd(actor.bodyId)) {
        store.QueueBroadphaseRemove(actor.bodyId);
    }
    actor.bodyInBroadphase = false;
}

void Collider::SuspendSceneResidency()
{
    auto &actor = ActorMut();
    if (actor.bodyId == 0xFFFFFFFF || !actor.bodyInBroadphase)
        return;
    PhysicsWorld::Instance().RemoveBodyFromBroadphase(actor.bodyId);
    actor.bodyInBroadphase = false;
}

void Collider::RestoreSceneResidency()
{
    if (!IsEnabled() || !GetGameObject() || !GetGameObject()->IsActiveInHierarchy())
        return;
    AddToBroadphase();
}

void Collider::RebuildShape()
{
    PhysicsECSStore::Instance().NotifyCollisionSceneChanged();
    auto &actor = ActorMut();
    if (actor.bodyId == 0xFFFFFFFF)
        return;

    PhysicsWorld::Instance().UpdateBodyShape(this);
    ++actor.shapeRevision;

    // If this is a static body (no Rigidbody), wake nearby dynamic
    // bodies so they react to the shape change immediately.
    bool isStatic = (actor.rigidbody == nullptr || !actor.rigidbody->IsEnabled());
    if (isStatic) {
        PhysicsWorld::Instance().WakeBodiesTouchingStatic(actor.bodyId);
    }
}

void Collider::SyncTransformToPhysics(float fixedDeltaTime, std::vector<PhysicsBodyPoseUpdate> *staticPoseBatch)
{
    auto &actor = ActorMut();
    if (actor.bodyId == 0xFFFFFFFF)
        return;
    if (actor.primaryCollider != this)
        return;

    // Use cached Rigidbody pointer — no dynamic_cast needed.
    Rigidbody *rb = actor.rigidbody;

    auto *go = GetGameObject();
    if (!go)
        return;

    Transform *tf = go->GetTransform();
    if (!tf)
        return;

    // Rebuild the Jolt shape when effective world scale changes.
    // Collider geometry is baked using world-space scale, so parent scaling
    // must also invalidate the body shape.
    glm::vec3 currentScale = tf->GetWorldScale();
    if (currentScale != actor.lastScale) {
        actor.lastScale = currentScale;
        RebuildShape();
    }

    // Rigidbody owns the pose contract for both dynamic and kinematic bodies.
    // In particular, a direct Transform write must also update interpolation
    // history; otherwise an old presentation sample can overwrite it before
    // the next fixed step.
    if (rb && rb->IsEnabled()) {
        rb->SyncExternalMovesToPhysics(fixedDeltaTime);
        return;
    }

    glm::quat rot = tf->GetWorldRotation();
    glm::vec3 pos = tf->GetPosition();

    // Compare against cached last-synced values (pure C++ — no Jolt lock)
    bool moved = (pos != actor.lastSyncedPos || rot != actor.lastSyncedRot);

    if (moved) {
        actor.lastSyncedPos = pos;
        actor.lastSyncedRot = rot;
        ++actor.transformRevision;

        PhysicsWorld &physicsWorld = PhysicsWorld::Instance();
        const bool hasRigidbodies = PhysicsECSStore::Instance().GetAliveRigidbodyCount() > 0;
        bool movedWithVelocity = false;
        if (fixedDeltaTime > 0.0f && hasRigidbodies) {
            // Collider-only body moved while the simulation is stepping
            // (gizmo drag / scripted move). A static teleport only produces
            // positional depenetration — overlapped dynamic bodies would be
            // squeezed out with zero exit velocity and stop dead. Drive it
            // kinematically instead so contacts carry real momentum.
            physicsWorld.MoveStaticBodyWithVelocity(actor.bodyId, pos, rot, fixedDeltaTime);
            movedWithVelocity = true;
        } else if (staticPoseBatch) {
            staticPoseBatch->push_back({actor.bodyId, pos, rot});
        } else {
            physicsWorld.SetBodyPosition(actor.bodyId, pos, rot);
        }

        // After teleporting a static body, wake nearby dynamic bodies. The
        // velocity path is exempt: the body is temporarily kinematic and Jolt
        // wakes everything it touches during the step.
        if (!movedWithVelocity && hasRigidbodies) {
            physicsWorld.WakeBodiesTouchingStatic(actor.bodyId);
        }
    }
}

// ============================================================================
// Serialization
// ============================================================================

nlohmann::json Collider::SerializeDocument() const
{
    const auto &d = Data();
    auto j = Component::SerializeDocument();
    j["is_trigger"] = d.isTrigger;
    j["center"] = {d.center.x, d.center.y, d.center.z};
    j["physic_material_guid"] = m_physicMaterial.GetGuid();
    return j;
}

bool Collider::DeserializeDocument(const nlohmann::json &j)
{
    try {
        ColliderECSData staged = Data();
        staged.isTrigger = j.at("is_trigger").get<bool>();
        const auto &center = j.at("center");
        staged.center = glm::vec3(center[0].get<float>(), center[1].get<float>(), center[2].get<float>());
        const std::string materialGuid = j.at("physic_material_guid").get<std::string>();
        if (!Component::DeserializeDocument(j))
            return false;
        staged.deserialized = true;
        DataMut() = staged;
        if (materialGuid.empty())
            ClearPhysicMaterial();
        else
            SetPhysicMaterialGuid(materialGuid);
        // NOTE: RebuildShape() is called by derived classes after their own
        // fields are deserialized (so both base + derived changes are applied
        // in a single shape rebuild).
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("Collider::Deserialize failed: ", e.what());
        return false;
    }
}

void Collider::CloneBaseColliderData(Collider &target) const
{
    target.m_enabled = m_enabled;
    target.m_executionOrder = m_executionOrder;
    const auto &src = Data();
    auto &dst = target.DataMut();
    dst.isTrigger = src.isTrigger;
    dst.center = src.center;
    if (m_physicMaterial.HasGuid())
        target.SetPhysicMaterialGuid(m_physicMaterial.GetGuid());
    else
        target.SetPhysicMaterial(m_physicMaterial.Get());
}

} // namespace infernux
