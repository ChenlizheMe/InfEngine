/**
 * @file CapsuleCollider.cpp
 * @brief CapsuleCollider implementation — Jolt CapsuleShape creation & serialization.
 */

// Jolt/Jolt.h MUST be the very first include in this TU
#include <Jolt/Jolt.h>
#include <Jolt/Physics/Collision/Shape/CapsuleShape.h>
#include <Jolt/Physics/Collision/Shape/RotatedTranslatedShape.h>

#include "CapsuleCollider.h"
#include "ComponentDocumentValidation.h"
#include "ComponentFactory.h"
#include "GameObject.h"
#include "MeshRenderer.h"
#include "Transform.h"
#include <InxLog.h>

#include <algorithm>
#include <cmath>
#include <nlohmann/json.hpp>

namespace infernux
{

INFERNUX_REGISTER_VALIDATED_COMPONENT("CapsuleCollider", CapsuleCollider)

void CapsuleCollider::SetRadius(float radius)
{
    if (!std::isfinite(radius) || radius < 0.001f || m_height < radius * 2.0f + 0.001f)
        throw std::invalid_argument("capsule radius is invalid for the current height");
    m_radius = radius;
    RebuildShape();
}

void CapsuleCollider::SetHeight(float height)
{
    if (!std::isfinite(height) || height < m_radius * 2.0f + 0.001f)
        throw std::invalid_argument("capsule height must exceed its diameter");
    m_height = height;
    RebuildShape();
}

void CapsuleCollider::SetDirection(int dir)
{
    if (dir < 0 || dir > 2)
        throw std::invalid_argument("capsule direction must be X, Y, or Z");
    m_direction = dir;
    RebuildShape();
}

void CapsuleCollider::AutoFitToMesh()
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

    // Choose direction from longest axis (Unity behaviour)
    if (extent.x >= extent.y && extent.x >= extent.z) {
        m_direction = 0; // X
        m_height = std::max(extent.x, 0.001f);
        m_radius = std::max(std::max(extent.y, extent.z) * 0.5f, 0.001f);
    } else if (extent.y >= extent.x && extent.y >= extent.z) {
        m_direction = 1; // Y
        m_height = std::max(extent.y, 0.001f);
        m_radius = std::max(std::max(extent.x, extent.z) * 0.5f, 0.001f);
    } else {
        m_direction = 2; // Z
        m_height = std::max(extent.z, 0.001f);
        m_radius = std::max(std::max(extent.x, extent.y) * 0.5f, 0.001f);
    }

    // Capsule constraint: height >= 2 * radius
    if (m_height < m_radius * 2.0f) {
        m_height = m_radius * 2.0f + 0.001f;
    }
}

void *CapsuleCollider::CreateJoltShapeRaw() const
{
    float r = m_radius;
    float h = m_height;
    glm::vec3 signedScale(1.0f);
    if (auto *go = GetGameObject()) {
        if (auto *tf = go->GetTransform()) {
            signedScale = tf->GetWorldScale();
        }
    }

    // Account for scale
    {
        glm::vec3 s = glm::abs(signedScale);
        float axisScale = (m_direction == 0) ? s.x : (m_direction == 1) ? s.y : s.z;
        float radScale = 0.0f;
        switch (m_direction) {
        case 0:
            radScale = std::max(s.y, s.z);
            break;
        case 1:
            radScale = std::max(s.x, s.z);
            break;
        case 2:
            radScale = std::max(s.x, s.y);
            break;
        }
        h *= axisScale;
        r *= radScale;
    }

    // Jolt CapsuleShape(halfHeight, radius) — half height is the cylinder portion only
    float halfCylinder = std::max((h - 2.0f * r) * 0.5f, 0.0f);
    JPH::Shape *shape = new JPH::CapsuleShape(halfCylinder, r);
    JPH::Quat rotation = JPH::Quat::sIdentity();

    // Jolt capsule is always along Y. Rotate if direction != Y.
    if (m_direction == 0) {
        // Rotate capsule from Y to X
        rotation = JPH::Quat::sRotation(JPH::Vec3::sAxisZ(), JPH::JPH_PI * 0.5f);
    } else if (m_direction == 2) {
        // Rotate capsule from Y to Z
        rotation = JPH::Quat::sRotation(JPH::Vec3::sAxisX(), JPH::JPH_PI * 0.5f);
    }

    glm::vec3 center = GetCenter() * signedScale;
    return new JPH::RotatedTranslatedShape(JPH::Vec3(center.x, center.y, center.z), rotation, shape);
}

// ============================================================================
// Serialization
// ============================================================================

nlohmann::json CapsuleCollider::SerializeDocument() const
{
    auto baseJson = Collider::SerializeDocument();
    baseJson["radius"] = m_radius;
    baseJson["height"] = m_height;
    baseJson["direction"] = m_direction;
    return baseJson;
}

void CapsuleCollider::ValidateSerializedDocument(const nlohmann::json &j)
{
    using namespace component_document_validation;
    ValidateComponentDocument(j, "CapsuleCollider",
                              {"is_trigger", "center", "physic_material_guid", "radius", "height", "direction"});
    RequireBoolean(j, "is_trigger", "CapsuleCollider");
    RequireFiniteVector(j, "center", 3, "CapsuleCollider");
    RequireString(j, "physic_material_guid", "CapsuleCollider");
    const float radius = RequireFiniteFloat(j, "radius", "CapsuleCollider");
    const float height = RequireFiniteFloat(j, "height", "CapsuleCollider");
    const int direction = RequireInteger(j, "direction", "CapsuleCollider");
    if (radius < 0.001f || height < radius * 2.0f + 0.001f)
        throw std::invalid_argument("CapsuleCollider geometry is invalid");
    if (direction < 0 || direction > 2)
        throw std::invalid_argument("CapsuleCollider.direction must be 0, 1, or 2");
}

bool CapsuleCollider::DeserializeDocument(const nlohmann::json &j)
{
    try {
        ValidateSerializedDocument(j);
        const float stagedRadius = j["radius"].get<float>();
        const float stagedHeight = j["height"].get<float>();
        const int stagedDirection = j["direction"].get<int>();
        if (!Collider::DeserializeDocument(j))
            return false;
        m_radius = stagedRadius;
        m_height = stagedHeight;
        m_direction = stagedDirection;
        RebuildShape();
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("CapsuleCollider::Deserialize failed: ", e.what());
        return false;
    }
}

std::unique_ptr<Component> CapsuleCollider::Clone() const
{
    auto clone = std::make_unique<CapsuleCollider>();
    CloneBaseColliderData(*clone);
    clone->m_radius = m_radius;
    clone->m_height = m_height;
    clone->m_direction = m_direction;
    return clone;
}

} // namespace infernux
