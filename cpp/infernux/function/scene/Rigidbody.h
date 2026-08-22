/**
 * @file Rigidbody.h
 * @brief Rigidbody component — dynamic/kinematic body physics.
 *
 * Mirrors Unity's Rigidbody API. When attached to a GameObject that also has a
 * Collider, the Collider's body becomes dynamic (or kinematic) instead of static.
 * Without a Rigidbody sibling, colliders remain static.
 */

#pragma once

#include "Component.h"
#include "physics/PhysicsECSStore.h"
#include <glm/glm.hpp>
#include <glm/gtc/quaternion.hpp>

namespace infernux
{

/**
 * @brief Unity-style ForceMode for AddForce / AddTorque.
 */
enum class ForceMode
{
    Force = 0,     ///< Continuous force (mass-dependent), applied over time (N)
    Acceleration,  ///< Continuous acceleration (mass-independent), applied over time (m/s²)
    Impulse,       ///< Instant force impulse (mass-dependent) (N·s)
    VelocityChange ///< Instant velocity change (mass-independent) (m/s)
};

/**
 * @brief Unity-style RigidbodyConstraints bitmask.
 *
 * Values match Unity's enum so scripts can use the same constants.
 */
enum class RigidbodyConstraints : int
{
    None = 0,
    FreezePositionX = 2,
    FreezePositionY = 4,
    FreezePositionZ = 8,
    FreezeRotationX = 16,
    FreezeRotationY = 32,
    FreezeRotationZ = 64,
    FreezePosition = 2 | 4 | 8,    // 14
    FreezeRotation = 16 | 32 | 64, // 112
    FreezeAll = 14 | 112           // 126
};

/// Allow bitwise OR/AND for RigidbodyConstraints.
inline RigidbodyConstraints operator|(RigidbodyConstraints a, RigidbodyConstraints b)
{
    return static_cast<RigidbodyConstraints>(static_cast<int>(a) | static_cast<int>(b));
}
inline RigidbodyConstraints operator&(RigidbodyConstraints a, RigidbodyConstraints b)
{
    return static_cast<RigidbodyConstraints>(static_cast<int>(a) & static_cast<int>(b));
}
inline RigidbodyConstraints operator~(RigidbodyConstraints a)
{
    return static_cast<RigidbodyConstraints>(~static_cast<int>(a));
}

/// Collision algorithms actually supported by the Jolt backend.
enum class CollisionDetectionMode : int
{
    Discrete = 0,
    Continuous = 1,
    ContinuousDynamic = 2
};

enum class RigidbodyInterpolation : int
{
    None = 0,
    Interpolate = 1,
};

/**
 * @brief Rigidbody component — drives physics simulation on a GameObject.
 *
 * NOTE: The Jolt body is owned by the *Collider* component. Rigidbody tells
 * colliders to switch their body from Static to Dynamic/Kinematic and provides
 * the physics API (forces, velocity, mass, etc.).  If there is no Collider
 * sibling, Rigidbody has no effect.
 */
class Rigidbody : public Component
{
  public:
    using ECSHandle = PhysicsECSStore::RigidbodyHandle;

    Rigidbody();
    ~Rigidbody() override;

    // ====================================================================
    // Lifecycle
    // ====================================================================

    void Awake() override;
    void OnEnable() override;
    void OnDisable() override;
    void OnDestroy() override;

    // ====================================================================
    // Properties (Unity-style)
    // ====================================================================

    /// @brief Mass in kilograms (Unity: Rigidbody.mass). Default 1.
    [[nodiscard]] float GetMass() const
    {
        return Data().mass;
    }
    void SetMass(float mass);

    /// @brief Linear drag (Unity: Rigidbody.drag). Default 0.
    [[nodiscard]] float GetDrag() const
    {
        return Data().drag;
    }
    void SetDrag(float drag);

    /// @brief Angular drag (Unity: Rigidbody.angularDrag). Default 0.05.
    [[nodiscard]] float GetAngularDrag() const
    {
        return Data().angularDrag;
    }
    void SetAngularDrag(float drag);

    /// @brief Use gravity? (Unity: Rigidbody.useGravity). Default true.
    [[nodiscard]] bool GetUseGravity() const
    {
        return Data().useGravity;
    }
    void SetUseGravity(bool use);

    /// @brief Is kinematic? (Unity: Rigidbody.isKinematic). Default false.
    [[nodiscard]] bool IsKinematic() const
    {
        return Data().isKinematic;
    }
    void SetIsKinematic(bool kinematic);

    // ---- Constraints ----

    /// @brief Freeze position/rotation axes (Unity: Rigidbody.constraints).
    [[nodiscard]] int GetConstraints() const
    {
        return Data().constraints;
    }
    void SetConstraints(int constraints);

    /// @brief Shortcut: freeze all rotation axes (Unity: Rigidbody.freezeRotation).
    [[nodiscard]] bool GetFreezeRotation() const
    {
        return (Data().constraints & static_cast<int>(RigidbodyConstraints::FreezeRotation)) ==
               static_cast<int>(RigidbodyConstraints::FreezeRotation);
    }
    void SetFreezeRotation(bool freeze);

    // ---- Collision detection ----

    /// @brief Collision detection mode (Unity: Rigidbody.collisionDetectionMode).
    [[nodiscard]] int GetCollisionDetectionMode() const
    {
        return Data().collisionDetectionMode;
    }
    void SetCollisionDetectionMode(int mode);

    [[nodiscard]] int GetInterpolation() const
    {
        return Data().interpolation;
    }
    void SetInterpolation(int mode);

    // ---- Velocity limits ----

    /// @brief Maximum angular velocity in rad/s (Unity: Rigidbody.maxAngularVelocity). Default 7.
    [[nodiscard]] float GetMaxAngularVelocity() const
    {
        return Data().maxAngularVelocity;
    }
    void SetMaxAngularVelocity(float vel);

    /// @brief Maximum linear velocity in m/s (Unity: Rigidbody.maxLinearVelocity).
    [[nodiscard]] float GetMaxLinearVelocity() const
    {
        return Data().maxLinearVelocity;
    }
    void SetMaxLinearVelocity(float vel);

    // ---- Velocity (read/write) ----

    /// @brief Linear velocity in world space (Unity: Rigidbody.velocity).
    [[nodiscard]] glm::vec3 GetVelocity() const;
    void SetVelocity(const glm::vec3 &vel);

    /// @brief Angular velocity in world space (Unity: Rigidbody.angularVelocity).
    [[nodiscard]] glm::vec3 GetAngularVelocity() const;
    void SetAngularVelocity(const glm::vec3 &vel);

    // ---- Forces ----

    /// @brief Add a force to the rigidbody (Unity: Rigidbody.AddForce).
    void AddForce(const glm::vec3 &force, ForceMode mode = ForceMode::Force);

    /// @brief Add a torque to the rigidbody (Unity: Rigidbody.AddTorque).
    void AddTorque(const glm::vec3 &torque, ForceMode mode = ForceMode::Force);

    /// @brief Add a force at a world-space position (Unity: Rigidbody.AddForceAtPosition).
    void AddForceAtPosition(const glm::vec3 &force, const glm::vec3 &position, ForceMode mode = ForceMode::Force);

    // ---- Kinematic movement ----

    /// @brief Schedule a kinematic move; presentation follows the configured interpolation mode.
    /// @throws std::logic_error if this is not an active kinematic body with an enabled Collider.
    void MovePosition(const glm::vec3 &position);

    /// @brief Schedule a kinematic rotation without teleporting the presentation Transform.
    /// @throws std::logic_error if this is not an active kinematic body with an enabled Collider.
    void MoveRotation(const glm::quat &rotation);

    // ---- Read-only world info ----

    /// @brief World-space center of mass (Unity: Rigidbody.worldCenterOfMass).
    [[nodiscard]] glm::vec3 GetWorldCenterOfMass() const;

    /// @brief World-space position of the rigidbody (Unity: Rigidbody.position).
    [[nodiscard]] glm::vec3 GetPosition() const;
    void SetPosition(const glm::vec3 &position);

    /// @brief World-space rotation of the rigidbody (Unity: Rigidbody.rotation).
    [[nodiscard]] glm::quat GetRotation() const;
    void SetRotation(const glm::quat &rotation);

    // ---- Sleep ----

    /// @brief Is the rigidbody sleeping? (Unity: Rigidbody.IsSleeping())
    [[nodiscard]] bool IsSleeping() const;

    /// @brief Wake the rigidbody up. (Unity: Rigidbody.WakeUp())
    void WakeUp();

    /// @brief Put the rigidbody to sleep. (Unity: Rigidbody.Sleep())
    void Sleep();

    // ====================================================================
    // Internal — used by Collider and SceneManager
    // ====================================================================

    /// @brief Sync physics → Transform after Jolt step.
    ///        Called by SceneManager's post-step pass.
    void SyncPhysicsToTransform();

    /// @brief Apply render interpolation between previous and current physics poses.
    void ApplyInterpolatedTransform(float alpha);

    /// @brief Publish an authored Transform pose to the physics body.
    ///
    /// Direct Transform writes are a distinct path from MovePosition: outside
    /// a fixed step they are committed as an immediate teleport and reset the
    /// interpolation history, so an older presentation pose cannot overwrite
    /// the authored value on the next frame. During a fixed step, kinematic
    /// bodies retain their velocity-producing drive semantics.
    void SyncExternalMovesToPhysics(float fixedDeltaTime = 0.0f);

    /// @brief Is this Rigidbody responsible for any collider's body?
    ///        Returns true when enabled and has collider siblings.
    [[nodiscard]] bool HasLinkedColliders() const;

    /// @brief Apply the complete Rigidbody configuration to a newly created or
    ///        motion-type-changed actor body.
    void ApplyConfigurationToBody(uint32_t bodyId);

    /// @brief Submit forces queued before deferred body creation.
    ///        Called after the body has entered the Jolt broadphase.
    void FlushPendingForceCommands();

    // ====================================================================
    // Type info
    // ====================================================================

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "Rigidbody";
    }

    // ====================================================================
    // Serialization
    // ====================================================================

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    static void ValidateSerializedDocument(const nlohmann::json &document);
    bool DeserializeDocument(const nlohmann::json &document) override;
    [[nodiscard]] std::unique_ptr<Component> Clone() const override;

  private:
    enum class ForceCommandKind : uint8_t
    {
        Force,
        Torque,
        ForceAtPosition,
    };

    struct ForceCommand
    {
        ForceCommandKind kind = ForceCommandKind::Force;
        glm::vec3 value{0.0f};
        glm::vec3 position{0.0f};
        ForceMode mode = ForceMode::Force;
    };

    void SubmitForceCommand(ForceCommand command);
    void ApplyForceCommand(class PhysicsWorld &world, uint32_t bodyId, const ForceCommand &command);

    /// Notify all sibling Colliders to rebuild their Jolt body type.
    void NotifyCollidersBodyTypeChanged();

    /// Apply drag damping (called via Jolt motion properties).
    void ApplyDragSettings();

    /// Apply constraints (allowed DOFs) to all sibling Collider bodies.
    void ApplyConstraints();

    /// Apply motion quality to all sibling Collider bodies.
    void ApplyMotionQuality();

    /// Apply velocity limits to all sibling Collider bodies.
    void ApplyVelocityLimits();

    /// Returns the active PhysicsWorld if this Rigidbody's GameObject is valid
    /// and physics is initialised; otherwise nullptr.  @p outGo receives the
    /// GameObject pointer (may be nullptr on failure).
    class PhysicsWorld *GetActivePhysicsWorld(GameObject *&outGo) const;

    /// Invoke @p fn(PhysicsWorld&, bodyId) for every unique Jolt body on this GO.
    template <typename Fn> void ForEachBody(Fn &&fn);

    /// Teleport the actor body to @p pos / @p rot while preserving velocity,
    /// and update the transform/interpolation caches.
    void TeleportBodies(PhysicsWorld &pw, GameObject *go, const glm::vec3 &pos, const glm::quat &rot);

    /// Pool-backed data — read access
    [[nodiscard]] const RigidbodyECSData &Data() const
    {
        return PhysicsECSStore::Instance().GetRigidbody(m_ecsHandle);
    }
    /// Pool-backed data — write access
    [[nodiscard]] RigidbodyECSData &DataMut() const
    {
        return PhysicsECSStore::Instance().GetRigidbody(m_ecsHandle);
    }

    ECSHandle m_ecsHandle;
    std::vector<ForceCommand> m_pendingForceCommands;
};

} // namespace infernux
