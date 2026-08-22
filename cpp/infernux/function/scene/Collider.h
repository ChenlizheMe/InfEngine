/**
 * @file Collider.h
 * @brief Base class for all physics collider components.
 *
 * Mirrors Unity's Collider API. Derived classes (BoxCollider, SphereCollider,
 * CapsuleCollider) override CreateJoltShape() to provide their specific geometry.
 *
 * A Collider without a Rigidbody sibling acts as a static collider (for raycasts,
 * overlap tests, etc.). With a Rigidbody it becomes dynamic.
 */

#pragma once

#include "Component.h"
#include "physics/PhysicsECSStore.h"
#include <function/resources/AssetRef.h>
#include <function/resources/PhysicMaterial/PhysicMaterial.h>
#include <glm/glm.hpp>
#include <glm/gtc/quaternion.hpp>
#include <initializer_list>
#include <memory>
#include <string_view>
#include <vector>

namespace infernux
{

enum class AssetEvent;

// Forward declaration — Collider caches a Rigidbody* pointer but does not
// dereference it in the header (only in Collider.cpp which includes Rigidbody.h).
class Rigidbody;
struct PhysicsBodyPoseUpdate;

/**
 * @brief Abstract base class for Collider components.
 *
 * NOTE: Jolt types are NOT exposed in this header to avoid Jolt include-order
 * issues. The virtual CreateJoltShapeRaw() returns void* which is actually
 * a JPH::Shape* with one reference added. Callers in .cpp files cast it.
 */
class Collider : public Component
{
  public:
    [[nodiscard]] static ComponentTypeConstraints GetTypeConstraints()
    {
        ComponentTypeConstraints constraints;
        constraints.satisfiedTypes = {"Collider"};
        return constraints;
    }

    using ECSHandle = PhysicsECSStore::ColliderHandle;

    Collider();
    ~Collider() override;

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

    /// @brief Is this collider a trigger? (Unity: Collider.isTrigger)
    [[nodiscard]] bool IsTrigger() const
    {
        return Data().isTrigger;
    }
    void SetIsTrigger(bool trigger);

    /// @brief Center offset in local space (Unity: Collider.center for Box/Capsule)
    [[nodiscard]] glm::vec3 GetCenter() const
    {
        return Data().center;
    }
    void SetCenter(const glm::vec3 &center);

    [[nodiscard]] std::shared_ptr<PhysicMaterial> GetPhysicMaterial() const;
    [[nodiscard]] const std::string &GetPhysicMaterialGuid() const;
    void SetPhysicMaterial(std::shared_ptr<PhysicMaterial> material);
    void SetPhysicMaterialGuid(const std::string &guid);
    void ClearPhysicMaterial();
    void OnPhysicMaterialAssetEvent(AssetEvent event);

    [[nodiscard]] float GetFriction() const;
    [[nodiscard]] float GetBounciness() const;
    [[nodiscard]] PhysicsMaterialCombine GetFrictionCombine() const;
    [[nodiscard]] PhysicsMaterialCombine GetBounceCombine() const;

    // ====================================================================
    // Jolt integration (opaque — Jolt types hidden from header)
    // ====================================================================

    /// @brief Create the Jolt collision shape. Returns a new-ed JPH::Shape* (caller
    ///        must wrap it in RefConst). Override in derived colliders.
    [[nodiscard]] virtual void *CreateJoltShapeRaw() const = 0;

    /// @brief Get the absolute world scale of the owning GameObject.
    ///        Returns (1,1,1) if no GameObject/Transform is available.
    [[nodiscard]] glm::vec3 GetWorldScale() const;

    /// @brief Get the Jolt body ID (0xFFFFFFFF = not registered).
    [[nodiscard]] uint32_t GetBodyId() const;

    /// Return the one body shared by all colliders on a GameObject.
    /// Throws if collider state has split across multiple bodies.
    [[nodiscard]] static uint32_t GetSharedBodyId(const GameObject *gameObject);

    /// @brief Sync the body transform with the GameObject's Transform.
    void SyncTransformToPhysics(float fixedDeltaTime = 0.0f,
                                std::vector<PhysicsBodyPoseUpdate> *staticPoseBatch = nullptr);

    /// Register body in PhysicsWorld (creates the Jolt body, does NOT add to broadphase).
    void RegisterBody();

    /// Unregister body from PhysicsWorld (removes from broadphase + destroys).
    void UnregisterBody();

    /// Add body to the Jolt broadphase (makes it visible to raycasts/queries).
    void AddToBroadphase();

    /// Remove body from the Jolt broadphase (invisible to raycasts, body kept alive).
    void RemoveFromBroadphase();

    /// Temporarily remove the shared body while a Scene candidate is published.
    /// Unlike disable/destroy this preserves the body allocation for rollback.
    void SuspendSceneResidency();

    /// Re-publish a body suspended by SuspendSceneResidency().
    void RestoreSceneResidency();

    // ====================================================================
    // Type info
    // ====================================================================

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "Collider";
    }

    // ====================================================================
    // Serialization
    // ====================================================================

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    bool DeserializeDocument(const nlohmann::json &document) override;

    /// @brief Auto-fit collider shape to sibling MeshRenderer bounds.
    ///        Called in Awake() for freshly-added colliders (not deserialized).
    ///        Override in derived classes to set size/radius/height from mesh AABB.
    virtual void AutoFitToMesh();

    /// @brief Cache (or invalidate) the sibling Rigidbody pointer.
    ///        Called by Rigidbody::OnEnable / OnDisable.
    void SetCachedRigidbody(Rigidbody *rb);

    /// @brief Get the cached Rigidbody (may be nullptr).
    [[nodiscard]] Rigidbody *GetCachedRigidbody() const;

    /// @brief Update the cached last-synced position/rotation after physics step
    ///        writes back to Transform. Called by Rigidbody::SyncPhysicsToTransform.
    void SetLastSyncedTransform(const glm::vec3 &pos, const glm::quat &rot);

    /// @brief Get the ECS pool handle.
    [[nodiscard]] ECSHandle GetECSHandle() const
    {
        return m_ecsHandle;
    }

  protected:
    /// @brief Copy base Collider ECS properties to a clone.
    /// Called by derived Clone() implementations.
    void CloneBaseColliderData(Collider &target) const;

    /// Called after shape parameters change to update the Jolt body.
    void RebuildShape();

    /// Pool-backed data — read access
    [[nodiscard]] const ColliderECSData &Data() const
    {
        return PhysicsECSStore::Instance().GetCollider(m_ecsHandle);
    }
    /// Pool-backed data — write access
    [[nodiscard]] ColliderECSData &DataMut() const
    {
        return PhysicsECSStore::Instance().GetCollider(m_ecsHandle);
    }

    [[nodiscard]] PhysicsActorData &ActorMut();
    [[nodiscard]] const PhysicsActorData &Actor() const;
    void EnsureActor();

    ECSHandle m_ecsHandle;
    AssetRef<PhysicMaterial> m_physicMaterial;
};

} // namespace infernux
