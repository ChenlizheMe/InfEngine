/**
 * @file SphereCollider.cpp
 * @brief SphereCollider implementation — Jolt SphereShape creation & serialization.
 */

// Jolt/Jolt.h MUST be the very first include in this TU
#include <Jolt/Jolt.h>
#include <Jolt/Physics/Collision/Shape/RotatedTranslatedShape.h>
#include <Jolt/Physics/Collision/Shape/SphereShape.h>

#include "ComponentDocumentValidation.h"
#include "ComponentFactory.h"
#include "GameObject.h"
#include "MeshRenderer.h"
#include "SphereCollider.h"
#include "Transform.h"
#include <InxLog.h>

#include <algorithm>
#include <cmath>
#include <nlohmann/json.hpp>

namespace infernux
{

INFERNUX_REGISTER_VALIDATED_COMPONENT("SphereCollider", SphereCollider)

void SphereCollider::SetRadius(float radius)
{
    if (!std::isfinite(radius) || radius < 0.001f)
        throw std::invalid_argument("sphere radius must be finite and at least 0.001");
    m_radius = radius;
    RebuildShape();
}

void SphereCollider::AutoFitToMesh()
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

    DataMut().center = (boundsMin + boundsMax) * 0.5f;
    m_radius = std::max({extent.x, extent.y, extent.z}) * 0.5f;
    m_radius = std::max(m_radius, 0.001f);
}

void *SphereCollider::CreateJoltShapeRaw() const
{
    glm::vec3 signedScale(1.0f);
    if (auto *go = GetGameObject()) {
        if (auto *tf = go->GetTransform()) {
            signedScale = tf->GetWorldScale();
        }
    }

    glm::vec3 s = glm::abs(signedScale);
    float r = m_radius * std::max({s.x, s.y, s.z});
    JPH::Shape *shape = new JPH::SphereShape(r);

    glm::vec3 center = GetCenter() * signedScale;
    if (center != glm::vec3(0.0f)) {
        shape = new JPH::RotatedTranslatedShape(JPH::Vec3(center.x, center.y, center.z), JPH::Quat::sIdentity(), shape);
    }

    return shape;
}

// ============================================================================
// Serialization
// ============================================================================

nlohmann::json SphereCollider::SerializeDocument() const
{
    auto baseJson = Collider::SerializeDocument();
    baseJson["radius"] = m_radius;
    return baseJson;
}

void SphereCollider::ValidateSerializedDocument(const nlohmann::json &j)
{
    using namespace component_document_validation;
    ValidateComponentDocument(j, "SphereCollider", {"is_trigger", "center", "physic_material_guid", "radius"});
    RequireBoolean(j, "is_trigger", "SphereCollider");
    RequireFiniteVector(j, "center", 3, "SphereCollider");
    RequireString(j, "physic_material_guid", "SphereCollider");
    if (RequireFiniteFloat(j, "radius", "SphereCollider") < 0.001f)
        throw std::invalid_argument("SphereCollider.radius must be at least 0.001");
}

bool SphereCollider::DeserializeDocument(const nlohmann::json &j)
{
    try {
        ValidateSerializedDocument(j);
        const float stagedRadius = j["radius"].get<float>();
        if (!Collider::DeserializeDocument(j))
            return false;
        m_radius = stagedRadius;
        RebuildShape();
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("SphereCollider::Deserialize failed: ", e.what());
        return false;
    }
}

std::unique_ptr<Component> SphereCollider::Clone() const
{
    auto clone = std::make_unique<SphereCollider>();
    CloneBaseColliderData(*clone);
    clone->m_radius = m_radius;
    return clone;
}

} // namespace infernux
