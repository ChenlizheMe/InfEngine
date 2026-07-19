/**
 * @file Rigidbody.cpp
 * @brief Rigidbody component — drives dynamic/kinematic physics simulation.
 *
 * Lifecycle:
 *   Awake  → (Collider has already registered its body as static)
 *   OnEnable → Tell sibling Colliders to switch body to Dynamic/Kinematic,
 *              apply mass/drag/gravity settings.
 *   OnDisable → Tell sibling Colliders to switch body back to Static.
 *   OnDestroy → Same as OnDisable.
 *
 * The Jolt body is owned by the Collider, not the Rigidbody. Rigidbody
 * merely configures the body's motion type and dynamics properties.
 */

#include "Rigidbody.h"
#include "ComponentDocumentValidation.h"

#include "Collider.h"
#include "ComponentFactory.h"
#include "GameObject.h"
#include "MeshCollider.h"
#include "SceneManager.h"
#include "Transform.h"
#include "physics/PhysicsECSStore.h"
#include "physics/PhysicsWorld.h"

#include <core/log/InxLog.h>

#include <algorithm>
#include <cmath>
#include <nlohmann/json.hpp>
#include <stdexcept>

namespace infernux
{

namespace
{

static Collider *GetPrimaryBodyCollider(GameObject *go)
{
    if (!go)
        return nullptr;

    const uint32_t bodyId = Collider::GetSharedBodyId(go);
    if (bodyId == 0xFFFFFFFF)
        return nullptr;
    for (const auto &component : go->GetAllComponents()) {
        auto *col = dynamic_cast<Collider *>(component.get());
        if (col && col->GetBodyId() == bodyId)
            return col;
    }
    throw std::logic_error("Physics actor body exists without an owning collider");
}

/// Returns the first valid body ID for this game object, or 0xFFFFFFFF.
static uint32_t GetPrimaryBodyId(GameObject *go)
{
    auto *col = GetPrimaryBodyCollider(go);
    return (col && col->GetBodyId() != 0xFFFFFFFF) ? col->GetBodyId() : 0xFFFFFFFF;
}

static int MapCollisionDetectionModeToMotionQuality(int mode, bool isKinematic)
{
    switch (mode) {
    case static_cast<int>(CollisionDetectionMode::Continuous):
        // Jolt's LinearCast path only runs for dynamic bodies. Kinematic CCD
        // would require a separate speculative-contact implementation.
        return isKinematic ? 0 : 1;
    case static_cast<int>(CollisionDetectionMode::Discrete):
        return 0;
    default:
        throw std::invalid_argument("Unsupported collision detection mode");
    }
}

static bool IsFinite(const glm::vec3 &value)
{
    return std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z);
}

static bool IsFiniteRotation(const glm::quat &value)
{
    return std::isfinite(value.w) && std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z) &&
           glm::dot(value, value) > 1e-12f;
}

static void ValidateForceMode(ForceMode mode)
{
    switch (mode) {
    case ForceMode::Force:
    case ForceMode::Acceleration:
    case ForceMode::Impulse:
    case ForceMode::VelocityChange:
        return;
    }
    throw std::invalid_argument("unsupported force mode");
}

} // namespace

INFERNUX_REGISTER_VALIDATED_COMPONENT("Rigidbody", Rigidbody)

// ============================================================================
// Shared helpers
// ============================================================================

template <typename Fn> void Rigidbody::ForEachBody(Fn &&fn)
{
    GameObject *go = nullptr;
    auto *pw = GetActivePhysicsWorld(go);
    if (!pw)
        return;
    const uint32_t bodyId = GetPrimaryBodyId(go);
    if (bodyId != 0xFFFFFFFF)
        fn(*pw, bodyId);
}

void Rigidbody::TeleportBodies(PhysicsWorld &pw, GameObject *go, const glm::vec3 &pos, const glm::quat &rot)
{
    if (auto *transform = go->GetTransform()) {
        transform->SetPosition(pos);
        transform->SetWorldRotation(rot);
    }
    auto colliders = go->GetComponents<Collider>();
    for (auto *col : colliders) {
        if (col && col->GetBodyId() != 0xFFFFFFFF)
            col->SetLastSyncedTransform(pos, rot);
    }
    const uint32_t bodyId = GetPrimaryBodyId(go);
    if (bodyId != 0xFFFFFFFF) {
        pw.SetBodyPosition(bodyId, pos, rot);
        pw.ActivateBody(bodyId);
    }
    auto &d = DataMut();
    d.previousPhysicsPosition = pos;
    d.previousPhysicsRotation = rot;
    d.currentPhysicsPosition = pos;
    d.currentPhysicsRotation = rot;
    d.hasPhysicsPose = true;
    d.wasSleeping = false;
    d.lastSyncedPosition = pos;
    d.lastSyncedRotation = rot;
    d.hasSyncedOnce = true;
}

// ============================================================================
// Constructor / Destructor
// ============================================================================

Rigidbody::Rigidbody()
{
    m_ecsHandle = PhysicsECSStore::Instance().AllocateRigidbody(this);
}

Rigidbody::~Rigidbody()
{
    // Safety-net unregister (idempotent — no-op if already done by OnDisable).
    // NOTE: Do NOT call GetComponents<Collider>() here — during
    // ~GameObject → m_components.clear(), sibling unique_ptrs may already be
    // destroyed, so iterating the vector and calling dynamic_cast on dangling
    // pointers is undefined behaviour.  OnDisable() already clears
    // cachedRigidbody on every sibling Collider before we reach this point.

    // Release pool slot
    PhysicsECSStore::Instance().ReleaseRigidbody(m_ecsHandle);
}

// ============================================================================
// Lifecycle
// ============================================================================

void Rigidbody::Awake()
{
    // Nothing here — Collider::Awake() creates the body as static.
    // OnEnable (called right after Awake) will switch it to dynamic.
}

void Rigidbody::OnEnable()
{

    // Cache this Rigidbody pointer on all sibling Colliders
    if (auto *go = GetGameObject()) {
        auto colliders = go->GetComponents<Collider>();
        for (auto *col : colliders)
            if (col)
                col->SetCachedRigidbody(this);
    }

    NotifyCollidersBodyTypeChanged();
}

void Rigidbody::OnDisable()
{

    // Clear cached Rigidbody pointer on sibling Colliders
    auto *go = GetGameObject();
    if (!go)
        return;

    auto colliders = go->GetComponents<Collider>();
    for (auto *col : colliders)
        if (col)
            col->SetCachedRigidbody(nullptr);

    auto *pw = &PhysicsWorld::Instance();
    if (!pw->IsInitialized())
        return;

    // Rebuild shapes — MeshCollider may switch back to MeshShape now
    // that there is no dynamic Rigidbody.  Must happen before setting
    // motionType to Static so the shape matches the new body type.
    for (auto *col : colliders) {
        if (col && col->IsEnabled() && col->GetBodyId() != 0xFFFFFFFF) {
            pw->UpdateBodyShape(col);
            break; // shared body — one rebuild is enough
        }
    }

    const uint32_t bodyId = GetPrimaryBodyId(go);
    if (bodyId != 0xFFFFFFFF) {
        pw->SetBodyMotionType(bodyId, 0); // 0 = Static
    }
}

void Rigidbody::OnDestroy()
{
    // Ensure bodies revert to static before Rigidbody goes away.
    // Use CallOnDisable() (not OnDisable()) so the m_wasEnabled guard
    // prevents double-execution when ~GameObject calls OnDisable + OnDestroy.
    CallOnDisable();
}

// ============================================================================
// Property setters
// ============================================================================

void Rigidbody::SetMass(float mass)
{
    if (!std::isfinite(mass) || mass < 0.001f)
        throw std::invalid_argument("mass must be finite and at least 0.001");
    auto &d = DataMut();
    d.mass = mass;
    ForEachBody([&](PhysicsWorld &pw, uint32_t id) { pw.SetBodyMassProperties(id, d.mass); });
}

void Rigidbody::SetDrag(float drag)
{
    if (!std::isfinite(drag) || drag < 0.0f)
        throw std::invalid_argument("drag must be finite and non-negative");
    DataMut().drag = drag;
    ApplyDragSettings();
}

void Rigidbody::SetAngularDrag(float drag)
{
    if (!std::isfinite(drag) || drag < 0.0f)
        throw std::invalid_argument("angular drag must be finite and non-negative");
    DataMut().angularDrag = drag;
    ApplyDragSettings();
}

void Rigidbody::SetUseGravity(bool use)
{
    auto &d = DataMut();
    if (d.useGravity == use)
        return;
    d.useGravity = use;
    float factor = d.useGravity ? 1.0f : 0.0f;
    ForEachBody([&](PhysicsWorld &pw, uint32_t id) { pw.SetBodyGravityFactor(id, factor); });
}

void Rigidbody::SetIsKinematic(bool kinematic)
{
    auto &d = DataMut();
    if (d.isKinematic == kinematic)
        return;
    bool wasKinematic = d.isKinematic;
    d.isKinematic = kinematic;
    NotifyCollidersBodyTypeChanged();

    // When switching kinematic → dynamic, synchronise interpolation caches
    // from the current Jolt body position.  During kinematic mode neither
    // SyncPhysicsToTransform nor ApplyInterpolatedTransform update
    // previousPhysicsPosition / currentPhysicsPosition, so they remain at
    // the pre-drag value.  Without this reset, the first
    // ApplyInterpolatedRigidbodies call after the switch overwrites the
    // Transform with the stale position, causing a one-frame flash-back.
    if (wasKinematic && !kinematic) {
        auto *go = GetGameObject();
        if (go) {
            uint32_t bid = GetPrimaryBodyId(go);
            if (bid != 0xFFFFFFFF) {
                auto &pw = PhysicsWorld::Instance();
                glm::vec3 bodyPos = pw.GetBodyPosition(bid);
                glm::quat bodyRot = glm::normalize(pw.GetBodyRotation(bid));
                d.previousPhysicsPosition = bodyPos;
                d.previousPhysicsRotation = bodyRot;
                d.currentPhysicsPosition = bodyPos;
                d.currentPhysicsRotation = bodyRot;
                d.hasPhysicsPose = true;
            }
            Transform *tf = go->GetTransform();
            if (tf) {
                d.lastSyncedPosition = tf->GetPosition();
                d.lastSyncedRotation = tf->GetWorldRotation();
                d.hasSyncedOnce = true;
            }
        }
    }
}

void Rigidbody::SetConstraints(int constraints)
{
    constexpr int kValidConstraintBits = static_cast<int>(RigidbodyConstraints::FreezeAll);
    if (constraints < 0 || (constraints & ~kValidConstraintBits) != 0)
        throw std::invalid_argument("constraints contains unsupported bits");
    auto &d = DataMut();
    if (d.constraints == constraints)
        return;
    d.constraints = constraints;
    ApplyConstraints();
}

void Rigidbody::SetFreezeRotation(bool freeze)
{
    int rotBits = static_cast<int>(RigidbodyConstraints::FreezeRotation);
    int newConstraints = freeze ? (Data().constraints | rotBits) : (Data().constraints & ~rotBits);
    SetConstraints(newConstraints);
}

void Rigidbody::SetCollisionDetectionMode(int mode)
{
    if (mode < static_cast<int>(CollisionDetectionMode::Discrete) ||
        mode > static_cast<int>(CollisionDetectionMode::Continuous)) {
        throw std::invalid_argument("collision detection mode must be Discrete or Continuous");
    }
    auto &d = DataMut();
    if (d.collisionDetectionMode == mode)
        return;
    d.collisionDetectionMode = mode;
    ApplyMotionQuality();
}

void Rigidbody::SetInterpolation(int mode)
{
    if (mode < static_cast<int>(RigidbodyInterpolation::None) ||
        mode > static_cast<int>(RigidbodyInterpolation::Interpolate))
        throw std::invalid_argument("interpolation must be None or Interpolate");
    auto &d = DataMut();
    d.interpolation = mode;
}

void Rigidbody::SetMaxAngularVelocity(float vel)
{
    if (!std::isfinite(vel) || vel < 0.0f)
        throw std::invalid_argument("max angular velocity must be finite and non-negative");
    DataMut().maxAngularVelocity = vel;
    ApplyVelocityLimits();
}

void Rigidbody::SetMaxLinearVelocity(float vel)
{
    if (!std::isfinite(vel) || vel < 0.0f)
        throw std::invalid_argument("max linear velocity must be finite and non-negative");
    DataMut().maxLinearVelocity = vel;
    ApplyVelocityLimits();
}

// ============================================================================
// Velocity
// ============================================================================

glm::vec3 Rigidbody::GetVelocity() const
{
    uint32_t bid = GetPrimaryBodyId(GetGameObject());
    return (bid != 0xFFFFFFFF) ? PhysicsWorld::Instance().GetBodyLinearVelocity(bid) : Data().linearVelocity;
}

void Rigidbody::SetVelocity(const glm::vec3 &vel)
{
    if (!IsFinite(vel))
        throw std::invalid_argument("velocity must contain finite values");
    auto &data = DataMut();
    data.linearVelocity = vel;
    data.hasLinearVelocity = true;
    auto *go = GetGameObject();
    if (go) {
        auto &pw = PhysicsWorld::Instance();
        const uint32_t bodyId = GetPrimaryBodyId(go);
        if (bodyId != 0xFFFFFFFF) {
            pw.SetBodyLinearVelocity(bodyId, vel);
            data.hasLinearVelocity = false;
        }
    }
}

glm::vec3 Rigidbody::GetAngularVelocity() const
{
    uint32_t bid = GetPrimaryBodyId(GetGameObject());
    return (bid != 0xFFFFFFFF) ? PhysicsWorld::Instance().GetBodyAngularVelocity(bid) : Data().angularVelocity;
}

void Rigidbody::SetAngularVelocity(const glm::vec3 &vel)
{
    if (!IsFinite(vel))
        throw std::invalid_argument("angular velocity must contain finite values");
    auto &data = DataMut();
    data.angularVelocity = vel;
    data.hasAngularVelocity = true;
    auto *go = GetGameObject();
    if (go) {
        auto &pw = PhysicsWorld::Instance();
        const uint32_t bodyId = GetPrimaryBodyId(go);
        if (bodyId != 0xFFFFFFFF) {
            pw.SetBodyAngularVelocity(bodyId, vel);
            data.hasAngularVelocity = false;
        }
    }
}

// ============================================================================
// Forces
// ============================================================================

void Rigidbody::AddForce(const glm::vec3 &force, ForceMode mode)
{
    if (!IsFinite(force))
        throw std::invalid_argument("force must contain finite values");
    ValidateForceMode(mode);
    SubmitForceCommand(ForceCommand{ForceCommandKind::Force, force, glm::vec3(0.0f), mode});
}

void Rigidbody::AddTorque(const glm::vec3 &torque, ForceMode mode)
{
    if (!IsFinite(torque))
        throw std::invalid_argument("torque must contain finite values");
    ValidateForceMode(mode);
    SubmitForceCommand(ForceCommand{ForceCommandKind::Torque, torque, glm::vec3(0.0f), mode});
}

// ============================================================================
// AddForceAtPosition
// ============================================================================

void Rigidbody::AddForceAtPosition(const glm::vec3 &force, const glm::vec3 &position, ForceMode mode)
{
    if (!IsFinite(force) || !IsFinite(position))
        throw std::invalid_argument("force and position must contain finite values");
    ValidateForceMode(mode);
    SubmitForceCommand(ForceCommand{ForceCommandKind::ForceAtPosition, force, position, mode});
}

void Rigidbody::SubmitForceCommand(ForceCommand command)
{
    const uint32_t bodyId = GetPrimaryBodyId(GetGameObject());
    if (bodyId == 0xFFFFFFFF) {
        m_pendingForceCommands.push_back(std::move(command));
        return;
    }
    ApplyForceCommand(PhysicsWorld::Instance(), bodyId, command);
}

void Rigidbody::ApplyForceCommand(PhysicsWorld &world, uint32_t bodyId, const ForceCommand &command)
{
    const float mass = Data().mass;
    if (command.kind == ForceCommandKind::Force) {
        switch (command.mode) {
        case ForceMode::Force:
            world.AddBodyForce(bodyId, command.value);
            return;
        case ForceMode::Acceleration:
            world.AddBodyForce(bodyId, command.value * mass);
            return;
        case ForceMode::Impulse:
            world.AddBodyImpulse(bodyId, command.value);
            return;
        case ForceMode::VelocityChange:
            world.AddBodyImpulse(bodyId, command.value * mass);
            return;
        }
    }

    if (command.kind == ForceCommandKind::Torque) {
        switch (command.mode) {
        case ForceMode::Force:
            world.AddBodyTorque(bodyId, command.value);
            return;
        case ForceMode::Acceleration:
            world.AddBodyTorque(bodyId, world.GetBodyWorldSpaceInertiaTensor(bodyId) * command.value);
            return;
        case ForceMode::Impulse:
            world.AddBodyAngularImpulse(bodyId, command.value);
            return;
        case ForceMode::VelocityChange:
            world.AddBodyAngularImpulse(bodyId, world.GetBodyWorldSpaceInertiaTensor(bodyId) * command.value);
            return;
        }
    }

    switch (command.mode) {
    case ForceMode::Force:
        world.AddBodyForceAtPosition(bodyId, command.value, command.position);
        return;
    case ForceMode::Acceleration:
        world.AddBodyForceAtPosition(bodyId, command.value * mass, command.position);
        return;
    case ForceMode::Impulse:
        world.AddBodyImpulseAtPosition(bodyId, command.value, command.position);
        return;
    case ForceMode::VelocityChange:
        world.AddBodyImpulseAtPosition(bodyId, command.value * mass, command.position);
        return;
    }
    throw std::logic_error("validated force command has an invalid mode");
}

void Rigidbody::FlushPendingForceCommands()
{
    if (m_pendingForceCommands.empty())
        return;
    const uint32_t bodyId = GetPrimaryBodyId(GetGameObject());
    if (bodyId == 0xFFFFFFFF)
        throw std::logic_error("cannot flush force commands without a physics body");

    auto commands = std::move(m_pendingForceCommands);
    m_pendingForceCommands.clear();
    auto &world = PhysicsWorld::Instance();
    for (const auto &command : commands)
        ApplyForceCommand(world, bodyId, command);
}

// ============================================================================
// Kinematic movement
// ============================================================================

void Rigidbody::MovePosition(const glm::vec3 &position)
{
    if (!IsFinite(position))
        throw std::invalid_argument("position must contain finite values");
    if (!Data().isKinematic)
        throw std::logic_error("MovePosition requires a kinematic Rigidbody");

    GameObject *go = nullptr;
    auto *pw = GetActivePhysicsWorld(go);
    if (!pw)
        throw std::logic_error("MovePosition requires an active physics world");

    // Use current fixed timestep from SceneManager.
    float dt = SceneManager::Instance().GetFixedTimeStep();

    const uint32_t bodyId = GetPrimaryBodyId(go);
    if (bodyId == 0xFFFFFFFF)
        throw std::logic_error("MovePosition requires an enabled Collider body");

    const glm::quat rotation = pw->GetBodyRotation(bodyId);
    pw->MoveBodyKinematic(bodyId, position, rotation, dt);
}

void Rigidbody::MoveRotation(const glm::quat &rotation)
{
    if (!IsFiniteRotation(rotation))
        throw std::invalid_argument("rotation must be a finite non-zero quaternion");
    if (!Data().isKinematic)
        throw std::logic_error("MoveRotation requires a kinematic Rigidbody");

    GameObject *go = nullptr;
    auto *pw = GetActivePhysicsWorld(go);
    if (!pw)
        throw std::logic_error("MoveRotation requires an active physics world");

    float dt = SceneManager::Instance().GetFixedTimeStep();
    const uint32_t bodyId = GetPrimaryBodyId(go);
    if (bodyId == 0xFFFFFFFF)
        throw std::logic_error("MoveRotation requires an enabled Collider body");

    const glm::vec3 position = pw->GetBodyPosition(bodyId);
    pw->MoveBodyKinematic(bodyId, position, glm::normalize(rotation), dt);
}

// ============================================================================
// Read-only world info
// ============================================================================

glm::vec3 Rigidbody::GetWorldCenterOfMass() const
{
    uint32_t bid = GetPrimaryBodyId(GetGameObject());
    return (bid != 0xFFFFFFFF) ? PhysicsWorld::Instance().GetBodyCenterOfMassPosition(bid) : glm::vec3(0.0f);
}

glm::vec3 Rigidbody::GetPosition() const
{
    auto *go = GetGameObject();
    if (!go)
        return glm::vec3(0.0f);

    uint32_t bid = GetPrimaryBodyId(go);
    if (bid == 0xFFFFFFFF) {
        if (auto *tf = go->GetTransform())
            return tf->GetPosition();
        return glm::vec3(0.0f);
    }

    return PhysicsWorld::Instance().GetBodyPosition(bid);
}

void Rigidbody::SetPosition(const glm::vec3 &position)
{
    if (!IsFinite(position))
        throw std::invalid_argument("position must contain finite values");
    auto *go = GetGameObject();
    if (!go)
        throw std::runtime_error("cannot set position on a detached Rigidbody");
    auto *transform = go->GetTransform();
    if (!transform)
        throw std::logic_error("Rigidbody owner has no Transform");

    auto &world = PhysicsWorld::Instance();
    const uint32_t bodyId = GetPrimaryBodyId(go);
    const bool hasBody = world.IsInitialized() && bodyId != 0xFFFFFFFF;
    const glm::quat rotation =
        hasBody ? glm::normalize(world.GetBodyRotation(bodyId)) : glm::normalize(transform->GetWorldRotation());
    if (hasBody) {
        TeleportBodies(world, go, position, rotation);
    } else {
        transform->SetPosition(position);
        auto &d = DataMut();
        d.previousPhysicsPosition = d.currentPhysicsPosition = position;
        d.previousPhysicsRotation = d.currentPhysicsRotation = rotation;
        d.hasPhysicsPose = true;
        d.wasSleeping = false;
        d.lastSyncedPosition = position;
        d.lastSyncedRotation = rotation;
        d.hasSyncedOnce = true;
    }
}

glm::quat Rigidbody::GetRotation() const
{
    auto *go = GetGameObject();
    if (!go)
        return glm::quat(1.0f, 0.0f, 0.0f, 0.0f);

    uint32_t bid = GetPrimaryBodyId(go);
    if (bid == 0xFFFFFFFF) {
        if (auto *tf = go->GetTransform())
            return tf->GetWorldRotation();
        return glm::quat(1.0f, 0.0f, 0.0f, 0.0f);
    }

    return PhysicsWorld::Instance().GetBodyRotation(bid);
}

void Rigidbody::SetRotation(const glm::quat &rotation)
{
    if (!IsFiniteRotation(rotation))
        throw std::invalid_argument("rotation must be a finite non-zero quaternion");
    auto *go = GetGameObject();
    if (!go)
        throw std::runtime_error("cannot set rotation on a detached Rigidbody");
    auto *transform = go->GetTransform();
    if (!transform)
        throw std::logic_error("Rigidbody owner has no Transform");

    const glm::quat normalized = glm::normalize(rotation);
    auto &world = PhysicsWorld::Instance();
    const uint32_t bodyId = GetPrimaryBodyId(go);
    const bool hasBody = world.IsInitialized() && bodyId != 0xFFFFFFFF;
    const glm::vec3 position = hasBody ? world.GetBodyPosition(bodyId) : transform->GetPosition();
    if (hasBody) {
        TeleportBodies(world, go, position, normalized);
    } else {
        transform->SetWorldRotation(normalized);
        auto &d = DataMut();
        d.previousPhysicsPosition = d.currentPhysicsPosition = position;
        d.previousPhysicsRotation = d.currentPhysicsRotation = normalized;
        d.hasPhysicsPose = true;
        d.wasSleeping = false;
        d.lastSyncedPosition = position;
        d.lastSyncedRotation = normalized;
        d.hasSyncedOnce = true;
    }
}

// ============================================================================
// Sleep
// ============================================================================

bool Rigidbody::IsSleeping() const
{
    uint32_t bid = GetPrimaryBodyId(GetGameObject());
    return (bid != 0xFFFFFFFF) ? PhysicsWorld::Instance().IsBodySleeping(bid) : true;
}

void Rigidbody::WakeUp()
{
    auto *go = GetGameObject();
    if (!go)
        return;

    auto &pw = PhysicsWorld::Instance();
    const uint32_t bodyId = GetPrimaryBodyId(go);
    if (bodyId != 0xFFFFFFFF)
        pw.ActivateBody(bodyId);
}

void Rigidbody::Sleep()
{
    auto *go = GetGameObject();
    if (!go)
        return;

    auto &pw = PhysicsWorld::Instance();
    const uint32_t bodyId = GetPrimaryBodyId(go);
    if (bodyId != 0xFFFFFFFF)
        pw.DeactivateBody(bodyId);
}

// ============================================================================
// Physics → Transform writeback (called after Jolt step)
// ============================================================================

void Rigidbody::SyncPhysicsToTransform()
{
    auto &d = DataMut();
    auto *go = GetGameObject();
    if (!go)
        return;

    uint32_t bid = GetPrimaryBodyId(go);
    if (bid == 0xFFFFFFFF)
        return;

    auto &pw = PhysicsWorld::Instance();

    // Read once on the active -> sleeping edge so the final solver pose is not
    // lost. Only bodies that were already sleeping can skip the Jolt reads.
    const bool sleeping = pw.IsBodySleeping(bid);
    if (sleeping && d.wasSleeping && d.hasPhysicsPose)
        return;
    d.wasSleeping = sleeping;

    glm::vec3 bodyPos = pw.GetBodyPosition(bid);
    glm::quat bodyRot = glm::normalize(pw.GetBodyRotation(bid));

    Transform *tf = go->GetTransform();
    if (!tf)
        return;

    const bool firstPose = !d.hasPhysicsPose;
    if (firstPose) {
        d.previousPhysicsPosition = bodyPos;
        d.previousPhysicsRotation = bodyRot;
    } else {
        d.previousPhysicsPosition = d.currentPhysicsPosition;
        d.previousPhysicsRotation = d.currentPhysicsRotation;
    }
    d.currentPhysicsPosition = bodyPos;
    d.currentPhysicsRotation = bodyRot;
    d.hasPhysicsPose = true;

    const bool rotFrozen = (d.constraints & static_cast<int>(RigidbodyConstraints::FreezeRotation)) ==
                           static_cast<int>(RigidbodyConstraints::FreezeRotation);

    if (d.interpolation == static_cast<int>(RigidbodyInterpolation::None) || firstPose) {
        tf->SetPosition(bodyPos);
        if (!rotFrozen) {
            tf->SetWorldRotation(bodyRot);
        }
        d.lastSyncedPosition = bodyPos;
        // Always read back the reconstructed rotation from the Transform so
        // that the cached value matches what SyncExternalMovesToPhysics() will
        // later read via GetWorldRotation() (avoids float round-trip mismatch).
        d.lastSyncedRotation = tf->GetWorldRotation();
        d.hasSyncedOnce = true;
    }

    // Also update the cached transform on ALL sibling colliders so that
    // SyncTransformToPhysics() won't see a spurious delta next step.
    auto colliders = go->GetComponents<Collider>();
    for (auto *c : colliders) {
        if (c && c->GetBodyId() != 0xFFFFFFFF) {
            const glm::vec3 cachePos = (d.interpolation == static_cast<int>(RigidbodyInterpolation::None) || firstPose)
                                           ? bodyPos
                                           : d.lastSyncedPosition;
            // Use the same reconstructed rotation we cached above.
            const glm::quat cacheRot = d.lastSyncedRotation;
            c->SetLastSyncedTransform(cachePos, cacheRot);
        }
    }
}

void Rigidbody::ApplyInterpolatedTransform(float alpha)
{
    auto &d = DataMut();
    if (!d.hasPhysicsPose)
        return;

    auto *go = GetGameObject();
    if (!go)
        return;

    Transform *tf = go->GetTransform();
    if (!tf)
        return;

    glm::vec3 presentedPos = d.currentPhysicsPosition;
    glm::quat presentedRot = glm::normalize(d.currentPhysicsRotation);

    if (d.interpolation == static_cast<int>(RigidbodyInterpolation::Interpolate)) {
        float t = std::clamp(alpha, 0.0f, 1.0f);
        presentedPos = glm::mix(d.previousPhysicsPosition, d.currentPhysicsPosition, t);
        presentedRot = glm::normalize(
            glm::slerp(glm::normalize(d.previousPhysicsRotation), glm::normalize(d.currentPhysicsRotation), t));
    }

    const bool rotFrozen = (d.constraints & static_cast<int>(RigidbodyConstraints::FreezeRotation)) ==
                           static_cast<int>(RigidbodyConstraints::FreezeRotation);

    tf->SetPosition(presentedPos);
    if (!rotFrozen) {
        tf->SetWorldRotation(presentedRot);
    }

    d.lastSyncedPosition = presentedPos;
    // Read back reconstructed rotation so the cache matches what
    // SyncExternalMovesToPhysics() will see via GetWorldRotation().
    d.lastSyncedRotation = tf->GetWorldRotation();
    d.hasSyncedOnce = true;

    auto colliders = go->GetComponents<Collider>();
    for (auto *c : colliders) {
        if (c && c->GetBodyId() != 0xFFFFFFFF) {
            c->SetLastSyncedTransform(presentedPos, d.lastSyncedRotation);
        }
    }
}

void Rigidbody::SyncExternalMovesToPhysics()
{
    auto &d = DataMut();
    if (d.isKinematic)
        return; // Kinematic is user-driven via SyncCollidersToPhysics already

    auto *go = GetGameObject();
    if (!go)
        return;

    Transform *tf = go->GetTransform();
    if (!tf)
        return;

    glm::vec3 currentPos = tf->GetPosition();
    glm::quat currentRot = tf->GetWorldRotation();

    const float posEps = 1e-4f;
    const float rotEps = 1e-4f;

    // First frame: initialise cache from current Transform.
    // Also check whether the script already moved the Transform away from
    // the body position (e.g. instantiate then set position).  If so,
    // teleport the body immediately instead of waiting one more tick.
    if (!d.hasSyncedOnce) {
        d.lastSyncedPosition = currentPos;
        d.lastSyncedRotation = currentRot;
        d.hasSyncedOnce = true;

        auto &pw = PhysicsWorld::Instance();
        if (!pw.IsInitialized())
            return;

        const uint32_t bodyId = GetPrimaryBodyId(go);
        if (bodyId == 0xFFFFFFFF)
            return;

        glm::vec3 bodyPos = pw.GetBodyPosition(bodyId);
        bool firstFrameDiff = glm::length(currentPos - bodyPos) > posEps;
        if (!firstFrameDiff)
            return;

        // Transform was modified after body creation — teleport now.
        TeleportBodies(pw, go, currentPos, currentRot);
        return;
    }

    bool posDiff = glm::length(currentPos - d.lastSyncedPosition) > posEps;
    bool rotDiff = (1.0f - std::abs(glm::dot(currentRot, d.lastSyncedRotation))) > rotEps;

    if (!posDiff && !rotDiff)
        return; // Transform unchanged since last physics write — nothing to do

    // INXLOG_WARN("Rigidbody::SyncExternalMovesToPhysics TELEPORT — posDiff=", posDiff, " rotDiff=", rotDiff,
    //             " posDelta=", glm::length(currentPos - d.lastSyncedPosition),
    //             " rotDelta=", (1.0f - std::abs(glm::dot(currentRot, d.lastSyncedRotation))));

    // The user (gizmo / inspector) moved the object externally.
    // Teleport ALL sibling collider bodies to the new Transform position.
    auto &pw = PhysicsWorld::Instance();
    if (!pw.IsInitialized())
        return;

    TeleportBodies(pw, go, currentPos, currentRot);

    // Update cache
    d.lastSyncedPosition = currentPos;
    d.lastSyncedRotation = currentRot;
}

bool Rigidbody::HasLinkedColliders() const
{
    auto *go = GetGameObject();
    if (!go)
        return false;

    return GetPrimaryBodyId(go) != 0xFFFFFFFF;
}

// ============================================================================
// Internal helpers
// ============================================================================

PhysicsWorld *Rigidbody::GetActivePhysicsWorld(GameObject *&outGo) const
{
    outGo = GetGameObject();
    if (!outGo)
        return nullptr;
    auto &pw = PhysicsWorld::Instance();
    return pw.IsInitialized() ? &pw : nullptr;
}

void Rigidbody::NotifyCollidersBodyTypeChanged()
{
    GameObject *go = GetGameObject();
    if (!go)
        return;

    const auto &d = Data();

    if (!d.isKinematic) {
        for (auto *meshCollider : go->GetComponents<MeshCollider>()) {
            if (meshCollider && meshCollider->IsEnabled() && !meshCollider->IsConvex())
                meshCollider->SetConvex(true);
        }
    }

    auto &physicsWorld = PhysicsWorld::Instance();
    if (!physicsWorld.IsInitialized())
        return;
    auto *pw = &physicsWorld;

    // Rebuild shapes first — some colliders (e.g. MeshCollider) produce
    // different shape types depending on whether a dynamic Rigidbody exists.
    // This must happen BEFORE SetBodyMotionType so Jolt never sees a
    // MeshShape on a dynamic body.
    auto colliders = go->GetComponents<Collider>();
    for (auto *col : colliders) {
        if (col && col->IsEnabled() && col->GetBodyId() != 0xFFFFFFFF) {
            pw->UpdateBodyShape(col);
            break; // shared body — one rebuild is enough
        }
    }

    // motionType: 0=Static, 1=Kinematic, 2=Dynamic
    int motionType = d.isKinematic ? 1 : 2;

    const uint32_t bodyId = GetPrimaryBodyId(go);
    if (bodyId != 0xFFFFFFFF) {
        pw->SetBodyMotionType(bodyId, motionType);
        ApplyConfigurationToBody(bodyId);
        FlushPendingForceCommands();
    }
}

void Rigidbody::ApplyConfigurationToBody(uint32_t bodyId)
{
    if (bodyId == 0xFFFFFFFF)
        throw std::invalid_argument("cannot configure an invalid physics body");
    auto &world = PhysicsWorld::Instance();
    if (!world.IsInitialized())
        throw std::runtime_error("cannot configure a body before PhysicsWorld initialization");

    auto &data = DataMut();
    world.SetBodyMassProperties(bodyId, data.mass);
    world.SetBodyDamping(bodyId, data.drag, data.angularDrag);
    world.SetBodyGravityFactor(bodyId, data.useGravity ? 1.0f : 0.0f);
    const int allowedDofs = 0x3F & ~(data.constraints >> 1);
    world.SetBodyAllowedDOFs(bodyId, allowedDofs, data.mass);
    world.SetBodyMotionQuality(bodyId,
                               MapCollisionDetectionModeToMotionQuality(data.collisionDetectionMode, data.isKinematic));
    world.SetBodyMaxAngularVelocity(bodyId, data.maxAngularVelocity);
    world.SetBodyMaxLinearVelocity(bodyId, data.maxLinearVelocity);
    if (data.hasLinearVelocity) {
        world.SetBodyLinearVelocity(bodyId, data.linearVelocity);
        data.hasLinearVelocity = false;
    }
    if (data.hasAngularVelocity) {
        world.SetBodyAngularVelocity(bodyId, data.angularVelocity);
        data.hasAngularVelocity = false;
    }
}

void Rigidbody::ApplyDragSettings()
{
    const auto &d = Data();
    ForEachBody([&](PhysicsWorld &pw, uint32_t id) { pw.SetBodyDamping(id, d.drag, d.angularDrag); });
}

void Rigidbody::ApplyConstraints()
{
    const auto &d = Data();
    // Convert Unity constraints bitmask to Jolt EAllowedDOFs.
    // Unity constraints bits start at bit 1 (FreezePositionX=2), Jolt at bit 0.
    int joltAllowed = 0x3F & ~(d.constraints >> 1);
    ForEachBody([&](PhysicsWorld &pw, uint32_t id) { pw.SetBodyAllowedDOFs(id, joltAllowed, d.mass); });
}

void Rigidbody::ApplyMotionQuality()
{
    const auto &d = Data();
    int joltQuality = MapCollisionDetectionModeToMotionQuality(d.collisionDetectionMode, d.isKinematic);
    ForEachBody([&](PhysicsWorld &pw, uint32_t id) { pw.SetBodyMotionQuality(id, joltQuality); });
}

void Rigidbody::ApplyVelocityLimits()
{
    const auto &d = Data();
    ForEachBody([&](PhysicsWorld &pw, uint32_t id) {
        pw.SetBodyMaxAngularVelocity(id, d.maxAngularVelocity);
        pw.SetBodyMaxLinearVelocity(id, d.maxLinearVelocity);
    });
}

// ============================================================================
// Serialization
// ============================================================================

nlohmann::json Rigidbody::SerializeDocument() const
{
    const auto &d = Data();
    auto j = Component::SerializeDocument();
    j["mass"] = d.mass;
    j["drag"] = d.drag;
    j["angular_drag"] = d.angularDrag;
    j["use_gravity"] = d.useGravity;
    j["is_kinematic"] = d.isKinematic;
    j["constraints"] = d.constraints;
    j["collision_detection_mode"] = d.collisionDetectionMode;
    j["interpolation"] = d.interpolation;
    j["max_angular_velocity"] = d.maxAngularVelocity;
    j["max_linear_velocity"] = d.maxLinearVelocity;
    return j;
}

void Rigidbody::ValidateSerializedDocument(const nlohmann::json &j)
{
    using namespace component_document_validation;
    ValidateComponentDocument(j, "Rigidbody",
                              {"mass", "drag", "angular_drag", "use_gravity", "is_kinematic", "constraints",
                               "collision_detection_mode", "interpolation", "max_angular_velocity",
                               "max_linear_velocity"});

    const float mass = RequireFiniteFloat(j, "mass", "Rigidbody");
    const float drag = RequireFiniteFloat(j, "drag", "Rigidbody");
    const float angularDrag = RequireFiniteFloat(j, "angular_drag", "Rigidbody");
    RequireBoolean(j, "use_gravity", "Rigidbody");
    RequireBoolean(j, "is_kinematic", "Rigidbody");
    const int constraints = RequireInteger(j, "constraints", "Rigidbody");
    const int collisionDetectionMode = RequireInteger(j, "collision_detection_mode", "Rigidbody");
    const int interpolation = RequireInteger(j, "interpolation", "Rigidbody");
    const float maxAngularVelocity = RequireFiniteFloat(j, "max_angular_velocity", "Rigidbody");
    const float maxLinearVelocity = RequireFiniteFloat(j, "max_linear_velocity", "Rigidbody");

    if (mass < 0.001f)
        throw std::invalid_argument("Rigidbody.mass must be at least 0.001");
    if (drag < 0.0f || angularDrag < 0.0f)
        throw std::invalid_argument("Rigidbody drag values must be non-negative");
    constexpr int kValidConstraintBits = static_cast<int>(RigidbodyConstraints::FreezeAll);
    if (constraints < 0 || (constraints & ~kValidConstraintBits) != 0)
        throw std::invalid_argument("Rigidbody.constraints contains unsupported bits");
    if (collisionDetectionMode < static_cast<int>(CollisionDetectionMode::Discrete) ||
        collisionDetectionMode > static_cast<int>(CollisionDetectionMode::Continuous))
        throw std::invalid_argument("Rigidbody.collision_detection_mode is unsupported");
    if (interpolation < static_cast<int>(RigidbodyInterpolation::None) ||
        interpolation > static_cast<int>(RigidbodyInterpolation::Interpolate))
        throw std::invalid_argument("Rigidbody.interpolation is unsupported");
    if (maxAngularVelocity < 0.0f || maxLinearVelocity < 0.0f)
        throw std::invalid_argument("Rigidbody velocity limits must be non-negative");
}

bool Rigidbody::DeserializeDocument(const nlohmann::json &j)
{
    try {
        ValidateSerializedDocument(j);

        RigidbodyECSData staged = Data();
        staged.mass = j.at("mass").get<float>();
        staged.drag = j.at("drag").get<float>();
        staged.angularDrag = j.at("angular_drag").get<float>();
        staged.useGravity = j.at("use_gravity").get<bool>();
        staged.isKinematic = j.at("is_kinematic").get<bool>();
        staged.constraints = j.at("constraints").get<int>();
        staged.collisionDetectionMode = j.at("collision_detection_mode").get<int>();
        staged.interpolation = j.at("interpolation").get<int>();
        staged.maxAngularVelocity = j.at("max_angular_velocity").get<float>();
        staged.maxLinearVelocity = j.at("max_linear_velocity").get<float>();

        if (!Component::DeserializeDocument(j))
            return false;
        DataMut() = staged;

        // Propagate all settings to Jolt bodies (e.g. when edited in Inspector during play)
        NotifyCollidersBodyTypeChanged();

        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("Rigidbody::Deserialize failed: ", e.what());
        return false;
    }
}

std::unique_ptr<Component> Rigidbody::Clone() const
{
    auto clone = std::make_unique<Rigidbody>();
    clone->m_enabled = m_enabled;
    clone->m_executionOrder = m_executionOrder;
    const auto &src = Data();
    auto &dst = clone->DataMut();
    dst.mass = src.mass;
    dst.drag = src.drag;
    dst.angularDrag = src.angularDrag;
    dst.useGravity = src.useGravity;
    dst.isKinematic = src.isKinematic;
    dst.constraints = src.constraints;
    dst.collisionDetectionMode = src.collisionDetectionMode;
    dst.interpolation = src.interpolation;
    dst.maxAngularVelocity = src.maxAngularVelocity;
    dst.maxLinearVelocity = src.maxLinearVelocity;
    return clone;
}

} // namespace infernux
