/**
 * @file BoxCollider.cpp
 * @brief BoxCollider implementation — Jolt BoxShape creation & serialization.
 */

// Jolt/Jolt.h MUST be the very first include in this TU
#include <Jolt/Jolt.h>
#include <Jolt/Physics/Collision/Shape/BoxShape.h>
#include <Jolt/Physics/Collision/Shape/RotatedTranslatedShape.h>

#include "BoxCollider.h"
#include "ComponentDocumentValidation.h"
#include "ComponentFactory.h"
#include "GameObject.h"
#include "MeshRenderer.h"
#include "Transform.h"
#include <InxLog.h>

#include <cmath>
#include <nlohmann/json.hpp>

namespace infernux
{

INFERNUX_REGISTER_VALIDATED_COMPONENT("BoxCollider", BoxCollider)

void BoxCollider::SetSize(const glm::vec3 &size)
{
    if (!std::isfinite(size.x) || !std::isfinite(size.y) || !std::isfinite(size.z) || size.x < 0.001f ||
        size.y < 0.001f || size.z < 0.001f)
        throw std::invalid_argument("box size must be finite and at least 0.001 on every axis");
    m_size = size;
    RebuildShape();
}

void BoxCollider::AutoFitToMesh()
{
    auto *go = GetGameObject();
    if (!go)
        return;
    auto *mr = go->GetComponent<MeshRenderer>();
    if (!mr)
        return;

    glm::vec3 boundsMin = mr->GetLocalBoundsMin();
    glm::vec3 boundsMax = mr->GetLocalBoundsMax();
    glm::vec3 extent = boundsMax - boundsMin;

    // Set m_size faithfully to mesh extents with only a tiny floor for
    // numerical stability.  Like Unity, the BoxCollider stores the *visual*
    // size; the physics layer (CreateJoltShapeRaw) applies its own minimum
    // half-extent so thin meshes still get a working collision shape,
    // similar to how PhysX's contactOffset (default 0.01) provides a
    // detection shell even for zero-thickness colliders.
    m_size = glm::max(extent, glm::vec3(0.001f));
    DataMut().center = (boundsMin + boundsMax) * 0.5f;
}

void *BoxCollider::CreateJoltShapeRaw() const
{
    glm::vec3 signedScale(1.0f);
    if (auto *go = GetGameObject()) {
        if (auto *tf = go->GetTransform()) {
            signedScale = tf->GetWorldScale();
        }
    }

    // Jolt expects half-extents; Unity exposes full size.
    // Minimum half-extent must comfortably exceed Jolt mPenetrationSlop
    // (tuned to 0.002 m).  0.01 m → total thickness 0.02 m = 10× slop,
    // thin enough for visually flat Quads/Planes while keeping a stable
    // collision shell.  Convex radius ≤ 40 % of thinnest axis so the
    // inner box always retains ≥ 60 % real volume.
    static constexpr float kMinHalfExtent = 0.01f;
    glm::vec3 halfExt = glm::max(m_size * 0.5f * glm::abs(signedScale), glm::vec3(kMinHalfExtent));
    float minHalf = glm::min(halfExt.x, glm::min(halfExt.y, halfExt.z));
    float convexRadius = std::min(JPH::cDefaultConvexRadius, minHalf * 0.4f);
    JPH::Shape *shape = new JPH::BoxShape(JPH::Vec3(halfExt.x, halfExt.y, halfExt.z), convexRadius);

    glm::vec3 center = GetCenter() * signedScale;
    if (center != glm::vec3(0.0f)) {
        shape = new JPH::RotatedTranslatedShape(JPH::Vec3(center.x, center.y, center.z), JPH::Quat::sIdentity(), shape);
    }

    return shape;
}

// ============================================================================
// Serialization
// ============================================================================

nlohmann::json BoxCollider::SerializeDocument() const
{
    auto baseJson = Collider::SerializeDocument();
    baseJson["size"] = {m_size.x, m_size.y, m_size.z};
    return baseJson;
}

void BoxCollider::ValidateSerializedDocument(const nlohmann::json &j)
{
    using namespace component_document_validation;
    ValidateComponentDocument(j, "BoxCollider", {"is_trigger", "center", "physic_material_guid", "size"});
    RequireBoolean(j, "is_trigger", "BoxCollider");
    RequireFiniteVector(j, "center", 3, "BoxCollider");
    RequireString(j, "physic_material_guid", "BoxCollider");
    RequireFiniteVector(j, "size", 3, "BoxCollider");
    const auto &size = j["size"];
    if (size[0].get<float>() < 0.001f || size[1].get<float>() < 0.001f || size[2].get<float>() < 0.001f)
        throw std::invalid_argument("BoxCollider.size values must be at least 0.001");
}

bool BoxCollider::DeserializeDocument(const nlohmann::json &j)
{
    try {
        ValidateSerializedDocument(j);
        const auto &size = j["size"];
        const glm::vec3 stagedSize(size[0].get<float>(), size[1].get<float>(), size[2].get<float>());
        if (!Collider::DeserializeDocument(j))
            return false;
        m_size = stagedSize;
        // Rebuild Jolt shape so Inspector edits take effect immediately.
        // Safe during scene load: RebuildShape() is a no-op when bodyId == 0xFFFFFFFF.
        RebuildShape();
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("BoxCollider::Deserialize failed: ", e.what());
        return false;
    }
}

std::unique_ptr<Component> BoxCollider::Clone() const
{
    auto clone = std::make_unique<BoxCollider>();
    CloneBaseColliderData(*clone);
    clone->m_size = m_size;
    return clone;
}

} // namespace infernux
