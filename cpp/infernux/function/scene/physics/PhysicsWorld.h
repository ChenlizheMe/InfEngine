#pragma once

/**
 * @file PhysicsWorld.h
 * @brief Singleton physics world backed by Jolt Physics.
 *
 * Manages Jolt initialisation/shutdown, stepping, body management,
 * and raycast queries. Integrated with SceneManager::FixedUpdate.
 */

#include <cstdint>
#include <glm/glm.hpp>
#include <glm/gtc/quaternion.hpp>
#include <memory>
#include <unordered_map>
#include <unordered_set>
#include <vector>

// Forward declaration
namespace infernux
{
class InxContactListener;
}

// Forward-declare Jolt types to avoid Jolt.h leak into every TU
namespace JPH
{
class PhysicsSystem;
class TempAllocatorImpl;
class Body;
class BodyID;
class Shape;
} // namespace JPH

namespace infernux
{

class GameObject;
class Collider;
class Rigidbody;
class InfernuxJoltJobSystemAdapter;

struct PhysicsBodyPoseUpdate
{
    uint32_t bodyId = 0xFFFFFFFF;
    glm::vec3 position{0.0f};
    glm::quat rotation{1.0f, 0.0f, 0.0f, 0.0f};
};

/**
 * @brief Result of a physics raycast (Unity: RaycastHit).
 */
struct RaycastHit
{
    glm::vec3 point{0.0f};            ///< World-space hit point
    glm::vec3 normal{0.0f};           ///< Surface normal at hit
    float distance = 0.0f;            ///< Distance from ray origin
    uint32_t bodyId = 0xFFFFFFFF;     ///< Hit Jolt body id (index+sequence)
    GameObject *gameObject = nullptr; ///< Hit GameObject
    Collider *collider = nullptr;     ///< Hit Collider component
};

/**
 * @brief Singleton physics world wrapping Jolt Physics.
 */
class PhysicsWorld
{
  public:
    static PhysicsWorld &Instance();

    /// Initialise Jolt (call once after engine starts)
    void Initialize();

    /// Shut down Jolt (call on engine cleanup).
    ///
    /// Tear-down order is fixed:
    ///   1. ContactListener::ClearAll() — drop pair tracking before bodies die.
    ///   2. m_bodyToCollider cleared — no caller may resolve a Collider* now.
    ///   3. ContactListener / PhysicsSystem / JobSystem / TempAllocator / Layers
    ///      destroyed in dependency order.
    ///   4. Jolt globals (Factory, registered types) torn down.
    /// Idempotent: subsequent calls are no-ops.
    void Shutdown();
    [[nodiscard]] size_t GetBodyCount() const noexcept
    {
        return m_bodyToCollider.size();
    }

    /// @return true if initialised
    [[nodiscard]] bool IsInitialized() const
    {
        return m_initialized;
    }

    /// Advance the simulation by one fixed step.
    void Step(float deltaTime);

    /// Bodies that were active immediately before or after the latest step.
    /// The union includes bodies that entered sleep during the step so their
    /// final solver pose is still read back exactly once.
    [[nodiscard]] const std::vector<uint32_t> &GetPoseReadbackBodyIds() const
    {
        return m_poseReadbackBodyIds;
    }

    [[nodiscard]] size_t GetLastDynamicCCDSplitCount() const
    {
        return m_lastDynamicCCDSplitCount;
    }

    // ========================================================================
    // Body management (called by Collider components)
    // ========================================================================

    /// Register a collider's Jolt body. Returns Jolt body ID as uint32.
    uint32_t CreateBody(Collider *collider, bool isStatic, bool isTrigger);

    /// Remove a body by collider pointer.
    ///
    /// Caller MUST first call RemoveBodyFromBroadphase(bodyId) — DestroyBody
    /// only releases the body slot and drops the body→collider mapping.
    /// Calling this on a body that is still in the broadphase will leave a
    /// stale handle dangling inside Jolt. Collider::UnregisterBody is the
    /// canonical caller and already enforces this ordering.
    void DestroyBody(Collider *collider);

    /// Inform the physics world that a body has moved (kinematic / editor move).
    void SetBodyPosition(uint32_t bodyId, const glm::vec3 &pos, const glm::quat &rot);

    /// Update many static body poses with one broadphase notification.
    void SetBodyPositionsBatch(const std::vector<PhysicsBodyPoseUpdate> &updates);

    /// Notify that a body's shape or properties changed.
    void UpdateBodyShape(Collider *collider, const Collider *exclude = nullptr);

    /// Update a body's sensor (trigger) flag at runtime without recreating the body.
    void SetBodyIsSensor(uint32_t bodyId, bool isSensor);

    /// Clear cached contact-pair tracking for a body so the next physics step
    /// produces fresh Enter events.  Call after changing sensor flag at runtime.
    void InvalidateContactPairsForBody(uint32_t bodyId);

    /// Add an existing body to the broadphase (visible to raycasts/queries).
    void AddBodyToBroadphase(uint32_t bodyId, bool isStatic);

    /// Batch-add bodies to the broadphase using Jolt's AddBodiesPrepare/Finalize.
    /// Much faster than individual AddBodyToBroadphase for large batches (10k+).
    void AddBodiesBatch(const std::vector<std::pair<uint32_t, bool>> &bodies);

    /// Remove a body from the broadphase (body stays alive for re-adding later).
    void RemoveBodyFromBroadphase(uint32_t bodyId);

    // ========================================================================
    // Body dynamics — used by Rigidbody component
    // ========================================================================

    /// Switch a body between Static, Dynamic, Kinematic.
    /// motionType: 0 = Static, 1 = Kinematic, 2 = Dynamic.
    void SetBodyMotionType(uint32_t bodyId, int motionType);

    /// Update a body's user layer while preserving whether it is moving/static.
    void SetBodyGameLayer(uint32_t bodyId, int gameLayer);

    /// Set mass override via the body's MassProperties.
    void SetBodyMassProperties(uint32_t bodyId, float mass);

    /// Set linear / angular damping (drag).
    void SetBodyDamping(uint32_t bodyId, float linearDamping, float angularDamping);

    /// Override per-body gravity factor (0 = no gravity, 1 = normal).
    void SetBodyGravityFactor(uint32_t bodyId, float factor);

    /// Set per-body friction coefficient [0..1].
    void SetBodyFriction(uint32_t bodyId, float friction);

    /// Set per-body restitution (bounciness) [0..1].
    void SetBodyRestitution(uint32_t bodyId, float restitution);

    // ---- Velocity ----

    [[nodiscard]] glm::vec3 GetBodyLinearVelocity(uint32_t bodyId) const;
    void SetBodyLinearVelocity(uint32_t bodyId, const glm::vec3 &vel);

    [[nodiscard]] glm::vec3 GetBodyAngularVelocity(uint32_t bodyId) const;
    void SetBodyAngularVelocity(uint32_t bodyId, const glm::vec3 &vel);

    // ---- Forces ----

    void AddBodyForce(uint32_t bodyId, const glm::vec3 &force);
    void AddBodyImpulse(uint32_t bodyId, const glm::vec3 &impulse);
    void AddBodyTorque(uint32_t bodyId, const glm::vec3 &torque);
    void AddBodyAngularImpulse(uint32_t bodyId, const glm::vec3 &impulse);

    // ---- Forces at position ----

    void AddBodyForceAtPosition(uint32_t bodyId, const glm::vec3 &force, const glm::vec3 &point);
    void AddBodyImpulseAtPosition(uint32_t bodyId, const glm::vec3 &impulse, const glm::vec3 &point);

    // ---- Constraints / Motion quality ----

    /// Set allowed degrees-of-freedom (Jolt EAllowedDOFs bitmask). Recalculates mass for the new DOFs.
    void SetBodyAllowedDOFs(uint32_t bodyId, int allowedDOFs, float mass);

    /// Set collision mode: 0 = Discrete, 1 = Continuous static sweep,
    /// 2 = ContinuousDynamic with dynamic-pair TOI subdivision.
    void SetBodyMotionQuality(uint32_t bodyId, int quality);

    /// Set max angular velocity (rad/s).
    void SetBodyMaxAngularVelocity(uint32_t bodyId, float maxVel);

    /// Set max linear velocity (m/s).
    void SetBodyMaxLinearVelocity(uint32_t bodyId, float maxVel);

    // ---- Kinematic move ----

    /// Speed cap for transform-driven body moves (gizmo drags, kinematic
    /// transform writes). Matches PhysX's default maxDepenetrationVelocity.
    static constexpr float kMaxTransformDriveSpeed = 10.0f;

    /// Move a kinematic body towards target position/rotation over deltaTime.
    ///
    /// Jolt implements this by giving the body the velocity needed to reach
    /// the target during the next step — and that velocity persists after
    /// arrival. Bodies moved through here are therefore tracked, and any body
    /// that does not receive a new target before the next Step() has its
    /// velocity zeroed so it stops exactly at the target instead of gliding
    /// away from its Transform forever.
    ///
    /// With maxSpeed > 0 the velocity the move can impart is capped: excess
    /// displacement is applied as a teleport, and only the final stretch is
    /// driven with velocity. Transform writes use this so long-range scripted
    /// teleports stay teleports while gizmo drags carry momentum. Script APIs
    /// (Rigidbody::MovePosition) pass 0 — uncapped, like Unity.
    void MoveBodyKinematic(uint32_t bodyId, const glm::vec3 &targetPos, const glm::quat &targetRot, float deltaTime,
                           float maxSpeed = 0.0f);

    /// Move a collider-only (static) body to a new pose with real velocity so
    /// overlapping dynamic bodies receive momentum (Unity-like drag push).
    ///
    /// Teleporting a static body only produces positional depenetration in
    /// Jolt: dynamic bodies are squeezed out with zero exit velocity and stop
    /// dead. This call temporarily switches the body to Kinematic and drives
    /// it with MoveKinematic; once move commands stop arriving the body is
    /// stopped and restored to Static (see SettleKinematicMoves).
    void MoveStaticBodyWithVelocity(uint32_t bodyId, const glm::vec3 &targetPos, const glm::quat &targetRot,
                                    float deltaTime);

    // ---- Sleep ----

    [[nodiscard]] bool IsBodySleeping(uint32_t bodyId) const;
    [[nodiscard]] bool IsBodySensor(uint32_t bodyId) const;
    void ActivateBody(uint32_t bodyId);
    void DeactivateBody(uint32_t bodyId);

    /// Wake all dynamic bodies whose AABBs overlap the given world-space box.
    /// Used after moving a static collider to wake sleeping bodies that were resting on it.
    void ActivateBodiesInAABB(const glm::vec3 &min, const glm::vec3 &max);

    /// Wake dynamic bodies overlapping a specific body's world-space AABB.
    /// Convenience wrapper for use after moving a static collider.
    void WakeBodiesTouchingStatic(uint32_t bodyId);

    // ---- Read-back (physics → transform) ----

    [[nodiscard]] glm::vec3 GetBodyPosition(uint32_t bodyId) const;
    [[nodiscard]] glm::quat GetBodyRotation(uint32_t bodyId) const;
    [[nodiscard]] glm::vec3 GetBodyCenterOfMassPosition(uint32_t bodyId) const;

    /// Get the world-space inertia tensor on the body's allowed angular subspace.
    /// Frozen axes and invalid/static bodies produce zero rows and columns.
    [[nodiscard]] glm::mat3 GetBodyWorldSpaceInertiaTensor(uint32_t bodyId) const;

    // ========================================================================
    // Raycast API (Unity: Physics.Raycast)
    // ========================================================================

    /// Cast a ray and return the closest hit.  Returns true if hit.
    bool Raycast(const glm::vec3 &origin, const glm::vec3 &direction, float maxDistance, RaycastHit &outHit,
                 uint32_t layerMask = (0xFFFFFFFFu & ~(1u << 2)), bool queryTriggers = true) const;

    /// Cast a ray and return all hits.
    std::vector<RaycastHit> RaycastAll(const glm::vec3 &origin, const glm::vec3 &direction, float maxDistance,
                                       uint32_t layerMask = (0xFFFFFFFFu & ~(1u << 2)),
                                       bool queryTriggers = true) const;

    // ========================================================================
    // Overlap queries (Unity: Physics.OverlapSphere / OverlapBox)
    // ========================================================================

    /// Find all Colliders within a sphere. Returns list of Collider*.
    std::vector<Collider *> OverlapSphere(const glm::vec3 &center, float radius,
                                          uint32_t layerMask = (0xFFFFFFFFu & ~(1u << 2)),
                                          bool queryTriggers = true) const;

    /// Find all Colliders within an oriented box.
    std::vector<Collider *> OverlapBox(const glm::vec3 &center, const glm::vec3 &halfExtents,
                                       const glm::quat &orientation = glm::quat(1.0f, 0.0f, 0.0f, 0.0f),
                                       uint32_t layerMask = (0xFFFFFFFFu & ~(1u << 2)),
                                       bool queryTriggers = true) const;

    /// Find all Colliders within a capsule defined by world-space segment endpoints.
    std::vector<Collider *> OverlapCapsule(const glm::vec3 &point0, const glm::vec3 &point1, float radius,
                                           uint32_t layerMask = (0xFFFFFFFFu & ~(1u << 2)),
                                           bool queryTriggers = true) const;

    // ========================================================================
    // Shape cast queries (Unity: Physics.SphereCast / BoxCast)
    // ========================================================================

    /// Cast a sphere along a direction. Returns closest RaycastHit or empty.
    bool SphereCast(const glm::vec3 &origin, float radius, const glm::vec3 &direction, float maxDistance,
                    RaycastHit &outHit, uint32_t layerMask = (0xFFFFFFFFu & ~(1u << 2)),
                    bool queryTriggers = true) const;

    /// Cast a box along a direction. Returns closest RaycastHit or empty.
    bool BoxCast(const glm::vec3 &center, const glm::vec3 &halfExtents, const glm::vec3 &direction,
                 const glm::quat &orientation, float maxDistance, RaycastHit &outHit,
                 uint32_t layerMask = (0xFFFFFFFFu & ~(1u << 2)), bool queryTriggers = true) const;

    /// Cast a capsule defined by world-space segment endpoints.
    bool CapsuleCast(const glm::vec3 &point0, const glm::vec3 &point1, float radius, const glm::vec3 &direction,
                     float maxDistance, RaycastHit &outHit, uint32_t layerMask = (0xFFFFFFFFu & ~(1u << 2)),
                     bool queryTriggers = true) const;

    // ========================================================================
    // Lookup
    // ========================================================================

    /// Find the Collider* that owns a given body ID.
    Collider *FindColliderByBodyId(uint32_t bodyId) const;

    /// Resolve a specific subshape hit/contact back to the owning Collider.
    Collider *ResolveColliderForSubShape(uint32_t bodyId, uint32_t subShapeIdValue) const;

    /// Rebind a body lookup entry to another collider on the same body.
    void RebindBodyCollider(uint32_t bodyId, Collider *collider);

    /// Ensure all Colliders in the given scene have registered bodies
    /// and their transforms are up to date. Call before editor-mode raycasts.
    void EnsureSceneBodiesRegistered(class Scene *scene);

    /// Rebuild the broad-phase tree so raycasts find newly added static bodies.
    void OptimizeBroadPhase();

    /// @brief Dispatch buffered contact events to Component callbacks.
    ///        Call once per fixed step, immediately after Step().
    [[nodiscard]] size_t DispatchContactEvents();

    /// Get the Jolt PhysicsSystem (for advanced usage). May be nullptr.
    JPH::PhysicsSystem *GetJoltSystem() const
    {
        return m_physicsSystem.get();
    }

  private:
    PhysicsWorld() = default;
    ~PhysicsWorld();
    PhysicsWorld(const PhysicsWorld &) = delete;
    PhysicsWorld &operator=(const PhysicsWorld &) = delete;

    /// @brief Shared overlap implementation for OverlapSphere/OverlapBox.
    std::vector<Collider *> OverlapShapeImpl(const JPH::Shape &shape, const glm::vec3 &center,
                                             const glm::quat &orientation, uint32_t layerMask,
                                             bool queryTriggers) const;

    /// @brief Shared shape cast implementation for SphereCast/BoxCast.
    bool ShapeCastImpl(const JPH::Shape &shape, const glm::vec3 &origin, const glm::quat &orientation,
                       const glm::vec3 &direction, float maxDistance, RaycastHit &outHit, uint32_t layerMask,
                       bool queryTriggers) const;

    [[nodiscard]] float FindEarliestDynamicCCDFraction(float deltaTime) const;
    [[nodiscard]] float FindEarliestStaticCCDFraction(float deltaTime) const;

    /// Stop tracked kinematic-move bodies that did not receive a new target
    /// since the previous Step(), and restore temporarily-kinematic statics.
    /// Called at the start of every Step().
    void SettleKinematicMoves();

    bool m_initialized = false;

    std::unique_ptr<JPH::TempAllocatorImpl> m_tempAllocator;
    std::unique_ptr<InfernuxJoltJobSystemAdapter> m_jobSystem;
    std::unique_ptr<JPH::PhysicsSystem> m_physicsSystem;

    // Layer interfaces (must outlive PhysicsSystem)
    struct LayerInterfaces;
    std::unique_ptr<LayerInterfaces> m_layers;

    // Mapping: Jolt body index → Collider*
    std::unordered_map<uint32_t, Collider *> m_bodyToCollider;

    // Dense active-body union produced by the latest completed Step().
    std::vector<uint32_t> m_poseReadbackBodyIds;

    // Full Jolt IDs explicitly configured for each CCD contract. Plain
    // Continuous uses an engine-owned static-only sweep; ContinuousDynamic
    // keeps Jolt LinearCast and adds relative dynamic-pair subdivision.
    std::unordered_set<uint32_t> m_staticContinuousBodyIds;
    std::unordered_set<uint32_t> m_continuousBodyIds;
    size_t m_lastDynamicCCDSplitCount = 0;

    /// Bodies whose velocity was authored by a MoveKinematic-style target
    /// move. See MoveBodyKinematic / MoveStaticBodyWithVelocity.
    struct KinematicMoveState
    {
        bool movedThisStep = true;  ///< Received a target since the last Step().
        bool restoreStatic = false; ///< Body is a collider-only static, temporarily kinematic.
        int idleSteps = 0;          ///< Steps elapsed without a new target.
    };
    std::unordered_map<uint32_t, KinematicMoveState> m_kinematicMoveStates;

    // Contact listener for collision/trigger callbacks
    std::unique_ptr<InxContactListener> m_contactListener;
};

} // namespace infernux
