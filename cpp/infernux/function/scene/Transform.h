#pragma once

#define GLM_FORCE_RADIANS
#ifndef GLM_FORCE_DEPTH_ZERO_TO_ONE
#define GLM_FORCE_DEPTH_ZERO_TO_ONE
#endif

#include "Component.h"
#include "TransformECSStore.h"
#include <cmath>
#include <glm/glm.hpp>
#include <glm/gtc/matrix_transform.hpp>
#include <glm/gtc/quaternion.hpp>

namespace infernux
{

/**
 * @brief Transform component for position, rotation, and scale.
 *
 * Every GameObject has a Transform. It defines the object's position,
 * rotation (as Euler angles or quaternion), and scale in 3D space.
 *
 * Supports hierarchical transforms through parent-child relationships.
 */
class Transform : public Component
{
  public:
    [[nodiscard]] static ComponentTypeConstraints GetTypeConstraints()
    {
        ComponentTypeConstraints constraints;
        constraints.allowMultiple = false;
        constraints.userAddable = false;
        constraints.removable = false;
        constraints.intrinsic = true;
        return constraints;
    }

    [[nodiscard]] const ComponentTypeConstraints &GetComponentTypeConstraints() const override
    {
        static const ComponentTypeConstraints constraints = GetTypeConstraints();
        return constraints;
    }

    Transform();
    ~Transform() override;

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "Transform";
    }

    // ========================================================================
    // Local-space Position (direct access to stored local coordinates)
    // ========================================================================

    /// @brief Get position in local (parent) space
    [[nodiscard]] glm::vec3 GetLocalPosition() const
    {
        return TransformECSStore::Instance().GetLocalPosition(m_ecsHandle);
    }

    /// @brief Set position in local (parent) space
    void SetLocalPosition(const glm::vec3 &position)
    {
        auto &store = TransformECSStore::Instance();
        store.SetLocalPosition(m_ecsHandle, position);
        store.SetDirty(m_ecsHandle, true);
        InvalidateWorldMatrix(false);
    }
    void SetLocalPosition(float x, float y, float z)
    {
        SetLocalPosition(glm::vec3(x, y, z));
    }

    // ========================================================================
    // World-space Position (considering parent hierarchy)
    // ========================================================================

    /// @brief Get position in world space (considering parent hierarchy)
    [[nodiscard]] glm::vec3 GetWorldPosition() const;

    /// @brief Set position in world space (computes required local position)
    void SetWorldPosition(const glm::vec3 &worldPos);
    void SetWorldPosition(float x, float y, float z)
    {
        SetWorldPosition(glm::vec3(x, y, z));
    }

    /// @brief Alias — matches Unity convention: GetPosition() == world space
    [[nodiscard]] glm::vec3 GetPosition() const
    {
        return GetWorldPosition();
    }
    void SetPosition(const glm::vec3 &pos)
    {
        SetWorldPosition(pos);
    }
    void SetPosition(float x, float y, float z)
    {
        SetWorldPosition(x, y, z);
    }

    // ========================================================================
    // Local-space Rotation
    // ========================================================================

    /// @brief Get rotation as Euler angles (degrees) in local space
    [[nodiscard]] glm::vec3 GetLocalEulerAngles() const
    {
        return ToPublicEulerAngles(TransformECSStore::Instance().GetLocalEulerAngles(m_ecsHandle));
    }

    /// @brief Set rotation from Euler angles (degrees) in local space
    void SetLocalEulerAngles(const glm::vec3 &euler)
    {
        auto &store = TransformECSStore::Instance();
        store.SetLocalEulerAngles(m_ecsHandle, euler);
        store.SetLocalRotation(m_ecsHandle, EulerYXZToQuat(euler));
        store.SetHasCachedWorldEulerAngles(m_ecsHandle, false);
        store.SetDirty(m_ecsHandle, true);
        InvalidateWorldMatrix(true);
    }
    void SetLocalEulerAngles(float x, float y, float z)
    {
        SetLocalEulerAngles(glm::vec3(x, y, z));
    }

    /// @brief Get rotation as quaternion in local space
    [[nodiscard]] glm::quat GetLocalRotation() const
    {
        return TransformECSStore::Instance().GetLocalRotation(m_ecsHandle);
    }

    /// @brief Set rotation from quaternion in local space
    void SetLocalRotation(const glm::quat &rotation)
    {
        auto &store = TransformECSStore::Instance();
        store.SetLocalRotation(m_ecsHandle, rotation);
        store.SetLocalEulerAngles(m_ecsHandle,
                                  ExtractEulerAnglesNear(rotation, store.GetLocalEulerAngles(m_ecsHandle)));
        store.SetHasCachedWorldEulerAngles(m_ecsHandle, false);
        store.SetDirty(m_ecsHandle, true);
        InvalidateWorldMatrix(true);
    }

    // ========================================================================
    // World-space Rotation (considering parent hierarchy)
    // ========================================================================

    /// @brief Get rotation as Euler angles (degrees) in world space
    [[nodiscard]] glm::vec3 GetWorldEulerAngles() const;

    /// @brief Set rotation from Euler angles (degrees) in world space
    void SetWorldEulerAngles(const glm::vec3 &euler);

    /// @brief Get world-space rotation quaternion (considering parent hierarchy)
    [[nodiscard]] glm::quat GetWorldRotation() const;

    /// @brief Set rotation from quaternion in world space
    void SetWorldRotation(const glm::quat &worldRot);

    /// @brief Aliases — Unity convention: Get/SetEulerAngles() == world space
    [[nodiscard]] glm::vec3 GetEulerAngles() const
    {
        return GetWorldEulerAngles();
    }
    void SetEulerAngles(const glm::vec3 &euler)
    {
        SetWorldEulerAngles(euler);
    }
    void SetEulerAngles(float x, float y, float z)
    {
        SetWorldEulerAngles(glm::vec3(x, y, z));
    }
    [[nodiscard]] glm::quat GetRotation() const
    {
        return GetWorldRotation();
    }
    void SetRotation(const glm::quat &q)
    {
        SetWorldRotation(q);
    }

    // ========================================================================
    // Local-space Scale
    // ========================================================================

    [[nodiscard]] glm::vec3 GetLocalScale() const
    {
        return TransformECSStore::Instance().GetLocalScale(m_ecsHandle);
    }
    void SetLocalScale(const glm::vec3 &scale)
    {
        auto &store = TransformECSStore::Instance();
        store.SetLocalScale(m_ecsHandle, scale);
        store.SetDirty(m_ecsHandle, true);
        InvalidateWorldMatrix(false);
    }
    void SetLocalScale(float x, float y, float z)
    {
        SetLocalScale(glm::vec3(x, y, z));
    }
    void SetLocalScale(float uniform)
    {
        SetLocalScale(glm::vec3(uniform));
    }

    /// @brief Set local position + euler rotation (degrees) + scale in one call.
    ///
    /// Fast path for per-frame animation drivers (TimelineAction / timeline FSM
    /// states): folds three property writes into a single store update with ONE
    /// dirty flag + ONE subtree invalidation, instead of three separate setters
    /// each invalidating the world-matrix subtree.  This is the C++-side analogue
    /// of the skeletal animator's single `submit_animation_pose` call.
    void SetLocalTRS(const glm::vec3 &position, const glm::vec3 &euler, const glm::vec3 &scale)
    {
        auto &store = TransformECSStore::Instance();
        store.SetLocalPosition(m_ecsHandle, position);
        store.SetLocalEulerAngles(m_ecsHandle, euler);
        store.SetLocalRotation(m_ecsHandle, EulerYXZToQuat(euler));
        store.SetLocalScale(m_ecsHandle, scale);
        store.SetHasCachedWorldEulerAngles(m_ecsHandle, false);
        store.SetDirty(m_ecsHandle, true);
        InvalidateWorldMatrix(true);
    }

    /// @brief Aliases — GetScale/SetScale map to local scale (Unity convention)
    [[nodiscard]] glm::vec3 GetScale() const
    {
        return TransformECSStore::Instance().GetLocalScale(m_ecsHandle);
    }
    void SetScale(const glm::vec3 &s)
    {
        SetLocalScale(s);
    }
    void SetScale(float x, float y, float z)
    {
        SetLocalScale(x, y, z);
    }
    void SetScale(float u)
    {
        SetLocalScale(u);
    }

    // ========================================================================
    // World-space Scale (approximate, read-only lossyScale + writable setter)
    // ========================================================================

    /// @brief Get approximate world scale (product of parent scales, like Unity lossyScale)
    [[nodiscard]] glm::vec3 GetWorldScale() const;

    /// @brief Set world scale (computes required local scale given parent hierarchy)
    void SetWorldScale(const glm::vec3 &worldScale);

    // ========================================================================
    // Hierarchy (delegates to owning GameObject, Unity-style access)
    // ========================================================================

    /// @brief Get parent Transform (nullptr if root). Unity: transform.parent
    [[nodiscard]] Transform *GetParent() const;

    /// @brief Set parent Transform. Unity: transform.SetParent(parent, worldPositionStays)
    void SetParent(Transform *parent, bool worldPositionStays = true);

    /// @brief Get the topmost Transform in the hierarchy. Unity: transform.root
    [[nodiscard]] Transform *GetRoot();

    /// @brief Number of children. Unity: transform.childCount
    [[nodiscard]] size_t GetChildCount() const;

    /// @brief Get child Transform by index. Unity: transform.GetChild(index)
    [[nodiscard]] Transform *GetChild(size_t index) const;

    /// @brief Find a child by name (non-recursive). Unity: transform.Find(name)
    [[nodiscard]] Transform *Find(const std::string &name) const;

    /// @brief Unparent all children. Unity: transform.DetachChildren()
    void DetachChildren();

    /// @brief Is this transform a child of parent? Unity: transform.IsChildOf(parent)
    [[nodiscard]] bool IsChildOf(const Transform *parent) const;

    /// @brief Get sibling index in parent's children list. Unity: transform.GetSiblingIndex()
    [[nodiscard]] int GetSiblingIndex() const;

    /// @brief Set sibling index. Unity: transform.SetSiblingIndex(index)
    void SetSiblingIndex(int index);

    /// @brief Move to first sibling position. Unity: transform.SetAsFirstSibling()
    void SetAsFirstSibling();

    /// @brief Move to last sibling position. Unity: transform.SetAsLastSibling()
    void SetAsLastSibling();

    // ========================================================================
    // Direction vectors
    // ========================================================================

    /// @brief Get forward direction in world space (Unity convention: transform.forward)
    [[nodiscard]] glm::vec3 GetForward() const
    {
        return GetWorldForward();
    }

    /// @brief Get right direction in world space (Unity convention: transform.right)
    [[nodiscard]] glm::vec3 GetRight() const
    {
        return GetWorldRight();
    }

    /// @brief Get up direction in world space (Unity convention: transform.up)
    [[nodiscard]] glm::vec3 GetUp() const
    {
        return GetWorldUp();
    }

    // ========================================================================
    // Local-space direction vectors
    // ========================================================================

    /// @brief Get forward direction in local space (positive Z, local rotation only)
    [[nodiscard]] glm::vec3 GetLocalForward() const
    {
        return glm::normalize(GetLocalRotation() * glm::vec3(0.0f, 0.0f, 1.0f));
    }

    /// @brief Get right direction in local space (positive X, local rotation only)
    [[nodiscard]] glm::vec3 GetLocalRight() const
    {
        return glm::normalize(GetLocalRotation() * glm::vec3(1.0f, 0.0f, 0.0f));
    }

    /// @brief Get up direction in local space (positive Y, local rotation only)
    [[nodiscard]] glm::vec3 GetLocalUp() const
    {
        return glm::normalize(GetLocalRotation() * glm::vec3(0.0f, 1.0f, 0.0f));
    }

    // ========================================================================
    // World-space direction vectors (considering parent hierarchy)
    // ========================================================================

    /// @brief Get forward direction in world space (positive Z, considering parents)
    [[nodiscard]] glm::vec3 GetWorldForward() const;

    /// @brief Get right direction in world space (positive X, considering parents)
    [[nodiscard]] glm::vec3 GetWorldRight() const;

    /// @brief Get up direction in world space (positive Y, considering parents)
    [[nodiscard]] glm::vec3 GetWorldUp() const;

    // ========================================================================
    // Transformations
    // ========================================================================

    /// @brief Translate by delta in world space (Unity convention)
    void Translate(const glm::vec3 &delta);

    /// @brief Translate by delta in local space (along object's own axes)
    void TranslateLocal(const glm::vec3 &delta);

    /// @brief Rotate by Euler angles (degrees) in local space. Unity: transform.Rotate(euler)
    void Rotate(const glm::vec3 &euler)
    {
        auto &store = TransformECSStore::Instance();
        glm::quat deltaRotation = EulerYXZToQuat(euler);
        glm::quat newRot = store.GetLocalRotation(m_ecsHandle) * deltaRotation;
        store.SetLocalRotation(m_ecsHandle, newRot);
        store.SetLocalEulerAngles(m_ecsHandle, ExtractEulerAnglesNear(newRot, store.GetLocalEulerAngles(m_ecsHandle)));
        store.SetHasCachedWorldEulerAngles(m_ecsHandle, false);
        store.SetDirty(m_ecsHandle, true);
        InvalidateWorldMatrix(true);
    }

    /// @brief Rotate around a local axis by angle (degrees). Unity: transform.Rotate(axis, angle)
    void Rotate(const glm::vec3 &axis, float angle)
    {
        auto &store = TransformECSStore::Instance();
        glm::quat deltaRotation = glm::angleAxis(glm::radians(angle), glm::normalize(axis));
        glm::quat newRot = store.GetLocalRotation(m_ecsHandle) * deltaRotation;
        store.SetLocalRotation(m_ecsHandle, newRot);
        store.SetLocalEulerAngles(m_ecsHandle, ExtractEulerAnglesNear(newRot, store.GetLocalEulerAngles(m_ecsHandle)));
        store.SetHasCachedWorldEulerAngles(m_ecsHandle, false);
        store.SetDirty(m_ecsHandle, true);
        InvalidateWorldMatrix(true);
    }

    /// @brief Rotate around a world-space point. Unity: transform.RotateAround(point, axis, angle)
    void RotateAround(const glm::vec3 &point, const glm::vec3 &axis, float angle);

    /// @brief Look at a world-space target position (Unity convention)
    void LookAt(const glm::vec3 &target, const glm::vec3 &up = glm::vec3(0.0f, 1.0f, 0.0f));

    // ========================================================================
    // Space conversion methods (Unity: TransformPoint, InverseTransformPoint, etc.)
    // ========================================================================

    /// @brief Transform position from local space to world space. Unity: transform.TransformPoint(point)
    [[nodiscard]] glm::vec3 TransformPoint(const glm::vec3 &point) const;

    /// @brief Transform position from world space to local space. Unity: transform.InverseTransformPoint(point)
    [[nodiscard]] glm::vec3 InverseTransformPoint(const glm::vec3 &point) const;

    /// @brief Transform direction from local to world (rotation only, no scale). Unity:
    /// transform.TransformDirection(dir)
    [[nodiscard]] glm::vec3 TransformDirection(const glm::vec3 &direction) const;

    /// @brief Transform direction from world to local (rotation only, no scale). Unity:
    /// transform.InverseTransformDirection(dir)
    [[nodiscard]] glm::vec3 InverseTransformDirection(const glm::vec3 &direction) const;

    /// @brief Transform vector from local to world (with scale, no position). Unity: transform.TransformVector(vec)
    [[nodiscard]] glm::vec3 TransformVector(const glm::vec3 &vector) const;

    /// @brief Transform vector from world to local (with scale, no position). Unity:
    /// transform.InverseTransformVector(vec)
    [[nodiscard]] glm::vec3 InverseTransformVector(const glm::vec3 &vector) const;

    // ========================================================================
    // Matrix
    // ========================================================================

    /// @brief Get local transformation matrix (from local position/rotation/scale)
    [[nodiscard]] glm::mat4 GetLocalMatrix() const
    {
        const auto &store = TransformECSStore::Instance();
        glm::mat4 translation = glm::translate(glm::mat4(1.0f), store.GetLocalPosition(m_ecsHandle));
        glm::mat4 rotation = glm::mat4_cast(store.GetLocalRotation(m_ecsHandle));
        glm::mat4 scale = glm::scale(glm::mat4(1.0f), store.GetLocalScale(m_ecsHandle));
        return translation * rotation * scale;
    }

    /// @brief Get world transformation matrix (considering parent hierarchy). Unity: transform.localToWorldMatrix
    [[nodiscard]] const glm::mat4 &GetWorldMatrix() const;

    /// @brief Alias for GetWorldMatrix(). Unity naming: localToWorldMatrix
    [[nodiscard]] const glm::mat4 &GetLocalToWorldMatrix() const
    {
        return GetWorldMatrix();
    }

    /// @brief Get inverse world matrix (world to local). Unity: transform.worldToLocalMatrix
    [[nodiscard]] glm::mat4 GetWorldToLocalMatrix() const;

    /// @brief Check if transform has been modified. Unity: transform.hasChanged
    [[nodiscard]] bool HasChanged() const
    {
        return TransformECSStore::Instance().GetDirty(m_ecsHandle);
    }

    /// @brief Clear changed flag. Unity: transform.hasChanged = false
    void SetHasChanged(bool value)
    {
        TransformECSStore::Instance().SetDirty(m_ecsHandle, value);
    }

    [[nodiscard]] TransformECSStore::Handle GetECSHandle() const
    {
        return m_ecsHandle;
    }

    /// @brief Recursively invalidate cached world matrix for this transform and all children.
    void InvalidateWorldMatrix(bool clearWorldEulerExact = false) const;

    // ========================================================================
    // Serialization
    // ========================================================================

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    static void ValidateSerializedDocument(const nlohmann::json &document);
    bool DeserializeDocument(const nlohmann::json &document) override;

    /// @brief Native clone: copy all ECS transform data to target (no JSON).
    /// The target keeps its own component ID and instance GUID.
    void CloneDataTo(Transform &target) const;

    /// @brief Native clone override — not used for Transform (embedded in GO).
    [[nodiscard]] std::unique_ptr<Component> Clone() const override;

  private:
    friend class Rigidbody;

    /// Physics owns the authoritative pose for a dynamic Rigidbody. Apply its
    /// position and rotation as one cache invalidation while suppressing only
    /// the owning Transform's authored-change observer. Descendant observers
    /// remain active so hierarchy-driven collider motion is still published.
    void ApplyWorldPoseFromPhysics(const glm::vec3 &worldPos, const glm::quat &worldRot, bool applyRotation);

    [[nodiscard]] Transform *GetParentTransformSafe() const;
    [[nodiscard]] glm::vec3 GetWorldDirection(const glm::vec3 &localAxis) const;

    // ========================================================================
    // Unity-aligned YXZ intrinsic Euler convention
    //   R = Ry(yaw) * Rx(pitch) * Rz(roll)
    //   Pitch (X) uses asin => range [-90, 90]
    //   Yaw   (Y) uses atan2 => range (-180, 180]
    //   Roll  (Z) uses atan2 => range (-180, 180]
    // ========================================================================

    /// @brief Construct quaternion from Euler angles (degrees) using YXZ intrinsic order.
    ///        Matches Unity's Quaternion.Euler(x, y, z).
    static glm::quat EulerYXZToQuat(const glm::vec3 &eulerDeg)
    {
        glm::vec3 r = glm::radians(eulerDeg);
        float cx = std::cos(r.x * 0.5f), sx = std::sin(r.x * 0.5f);
        float cy = std::cos(r.y * 0.5f), sy = std::sin(r.y * 0.5f);
        float cz = std::cos(r.z * 0.5f), sz = std::sin(r.z * 0.5f);
        // q = qY * qX * qZ
        glm::quat q;
        q.w = cy * cx * cz + sy * sx * sz;
        q.x = cy * sx * cz + sy * cx * sz;
        q.y = sy * cx * cz - cy * sx * sz;
        q.z = cy * cx * sz - sy * sx * cz;
        return q;
    }

    /// @brief Extract Euler angles (degrees) from quaternion using YXZ intrinsic order.
    ///        Matches Unity's Quaternion.eulerAngles.
    static glm::vec3 QuatToEulerYXZ(const glm::quat &q)
    {
        // sinX = 2(w*x - y*z)  derived from rotation-matrix element M[1][2] = -sinX
        float sinX = 2.0f * (q.w * q.x - q.y * q.z);

        float x, y, z;
        if (std::abs(sinX) < 0.9999f) {
            x = std::asin(sinX);
            y = std::atan2(2.0f * (q.x * q.z + q.w * q.y), 1.0f - 2.0f * (q.x * q.x + q.y * q.y));
            z = std::atan2(2.0f * (q.x * q.y + q.w * q.z), 1.0f - 2.0f * (q.x * q.x + q.z * q.z));
        } else {
            // Gimbal lock at pitch = ±90°
            x = std::copysign(glm::half_pi<float>(), sinX);
            y = std::atan2(-(2.0f * (q.x * q.z - q.w * q.y)), 1.0f - 2.0f * (q.y * q.y + q.z * q.z));
            z = 0.0f;
        }
        return glm::degrees(glm::vec3(x, y, z));
    }

    // ========================================================================
    // Angle continuity helpers
    // ========================================================================

    static float NormalizeAngleDegrees(float angle)
    {
        float normalized = std::fmod(angle, 360.0f);
        if (normalized > 180.0f)
            normalized -= 360.0f;
        if (normalized <= -180.0f)
            normalized += 360.0f;
        return normalized;
    }

    static float WrapAngleNear(float angle, float reference)
    {
        float wrapped = NormalizeAngleDegrees(angle);
        wrapped += std::round((reference - wrapped) / 360.0f) * 360.0f;
        return wrapped;
    }

    static glm::vec3 WrapEulerNear(const glm::vec3 &euler, const glm::vec3 &reference)
    {
        return glm::vec3(WrapAngleNear(euler.x, reference.x), WrapAngleNear(euler.y, reference.y),
                         WrapAngleNear(euler.z, reference.z));
    }

    static float ToPublicAngleDegrees(float angle)
    {
        float positive = std::fmod(angle, 360.0f);
        if (positive < 0.0f) {
            positive += 360.0f;
        }

        constexpr float kUnityEulerSnapEpsilon = 0.005729578f;
        if (positive < kUnityEulerSnapEpsilon || positive > 360.0f - kUnityEulerSnapEpsilon) {
            return 0.0f;
        }

        return positive;
    }

    static glm::vec3 ToPublicEulerAngles(const glm::vec3 &euler)
    {
        return glm::vec3(ToPublicAngleDegrees(euler.x), ToPublicAngleDegrees(euler.y), ToPublicAngleDegrees(euler.z));
    }

    /// @brief Alternate Euler representation for YXZ: (180-x, y+180, z+180) gives same rotation.
    static glm::vec3 AlternateEulerYXZ(const glm::vec3 &euler)
    {
        return glm::vec3(180.0f - euler.x, euler.y + 180.0f, euler.z + 180.0f);
    }

    static glm::vec3 ChooseClosestEulerRepresentation(const glm::vec3 &euler, const glm::vec3 &reference)
    {
        glm::vec3 primary = WrapEulerNear(euler, reference);
        glm::vec3 alternate = WrapEulerNear(AlternateEulerYXZ(euler), reference);
        glm::vec3 pd = primary - reference;
        glm::vec3 ad = alternate - reference;
        return (glm::dot(ad, ad) < glm::dot(pd, pd)) ? alternate : primary;
    }

    /// @brief Extract Euler (YXZ) from quaternion, choosing the representation closest to reference.
    static glm::vec3 ExtractEulerAnglesNear(const glm::quat &q, const glm::vec3 &reference)
    {
        return ChooseClosestEulerRepresentation(QuatToEulerYXZ(q), reference);
    }

    /// @brief Extract Euler (YXZ) from quaternion in canonical form.
    static glm::vec3 ExtractEulerAngles(const glm::quat &q)
    {
        return QuatToEulerYXZ(q);
    }

    TransformECSStore::Handle m_ecsHandle;
};

} // namespace infernux
