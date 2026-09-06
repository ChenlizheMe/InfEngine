/**
 * @file CylinderCollider.cpp
 * @brief CylinderCollider implementation and serialization.
 */

// Jolt/Jolt.h MUST be the first include in this translation unit.
#include <Jolt/Jolt.h>
#include <Jolt/Physics/Collision/Shape/CylinderShape.h>
#include <Jolt/Physics/Collision/Shape/RotatedTranslatedShape.h>

#include "ComponentDocumentValidation.h"
#include "ComponentFactory.h"
#include "CylinderCollider.h"
#include "GameObject.h"
#include "MeshRenderer.h"
#include "Transform.h"
#include <core/log/InxLog.h>

#include <algorithm>
#include <cmath>
#include <nlohmann/json.hpp>

namespace infernux
{

INFERNUX_REGISTER_VALIDATED_COMPONENT("CylinderCollider", CylinderCollider)

void CylinderCollider::SetRadius(float radius)
{
    if (!std::isfinite(radius) || radius < 0.001f)
        throw std::invalid_argument("cylinder radius must be at least 0.001");
    m_radius = radius;
    RebuildShape();
}

void CylinderCollider::SetHeight(float height)
{
    if (!std::isfinite(height) || height < 0.001f)
        throw std::invalid_argument("cylinder height must be at least 0.001");
    m_height = height;
    RebuildShape();
}

void CylinderCollider::SetDirection(int direction)
{
    if (direction < 0 || direction > 2)
        throw std::invalid_argument("cylinder direction must be X, Y, or Z");
    m_direction = direction;
    RebuildShape();
}

void CylinderCollider::AutoFitToMesh()
{
    auto *gameObject = GetGameObject();
    if (!gameObject)
        return;
    auto *renderer = gameObject->GetComponent<MeshRenderer>();
    if (!renderer)
        return;

    const glm::vec3 boundsMin = renderer->GetLocalBoundsMin();
    const glm::vec3 boundsMax = renderer->GetLocalBoundsMax();
    const glm::vec3 extent = boundsMax - boundsMin;
    DataMut().center = (boundsMin + boundsMax) * 0.5f;

    // Direction is an authored collider property (Y by default), not a mesh
    // inference.  Choosing the shortest extent changed an equal-bounds
    // primitive from Y to X and silently rotated its physics shape.
    if (m_direction == 0) {
        m_height = std::max(extent.x, 0.001f);
        m_radius = std::max(std::max(extent.y, extent.z) * 0.5f, 0.001f);
    } else if (m_direction == 1) {
        m_height = std::max(extent.y, 0.001f);
        m_radius = std::max(std::max(extent.x, extent.z) * 0.5f, 0.001f);
    } else {
        m_height = std::max(extent.z, 0.001f);
        m_radius = std::max(std::max(extent.x, extent.y) * 0.5f, 0.001f);
    }
}

void *CylinderCollider::CreateJoltShapeRaw() const
{
    float radius = m_radius;
    float height = m_height;
    glm::vec3 signedScale(1.0f);
    if (auto *gameObject = GetGameObject()) {
        if (auto *transform = gameObject->GetTransform())
            signedScale = transform->GetWorldScale();
    }

    const glm::vec3 scale = glm::abs(signedScale);
    const float axisScale = m_direction == 0 ? scale.x : (m_direction == 1 ? scale.y : scale.z);
    float radialScale = 1.0f;
    if (m_direction == 0)
        radialScale = std::max(scale.y, scale.z);
    else if (m_direction == 1)
        radialScale = std::max(scale.x, scale.z);
    else
        radialScale = std::max(scale.x, scale.y);
    height *= axisScale;
    radius *= radialScale;

    const float halfHeight = std::max(height * 0.5f, 0.0005f);
    const float convexRadius = std::min({JPH::cDefaultConvexRadius, halfHeight * 0.25f, radius * 0.1f});
    JPH::Shape *shape = new JPH::CylinderShape(halfHeight, radius, convexRadius);
    JPH::Quat rotation = JPH::Quat::sIdentity();
    if (m_direction == 0)
        rotation = JPH::Quat::sRotation(JPH::Vec3::sAxisZ(), JPH::JPH_PI * 0.5f);
    else if (m_direction == 2)
        rotation = JPH::Quat::sRotation(JPH::Vec3::sAxisX(), JPH::JPH_PI * 0.5f);

    const glm::vec3 center = GetCenter() * signedScale;
    return new JPH::RotatedTranslatedShape(JPH::Vec3(center.x, center.y, center.z), rotation, shape);
}

nlohmann::json CylinderCollider::SerializeDocument() const
{
    auto document = Collider::SerializeDocument();
    document["radius"] = m_radius;
    document["height"] = m_height;
    document["direction"] = m_direction;
    return document;
}

void CylinderCollider::ValidateSerializedDocument(const nlohmann::json &document)
{
    using namespace component_document_validation;
    ValidateComponentDocument(document, "CylinderCollider",
                              {"is_trigger", "center", "physic_material_guid", "radius", "height", "direction"});
    RequireBoolean(document, "is_trigger", "CylinderCollider");
    RequireFiniteVector(document, "center", 3, "CylinderCollider");
    RequireString(document, "physic_material_guid", "CylinderCollider");
    const float radius = RequireFiniteFloat(document, "radius", "CylinderCollider");
    const float height = RequireFiniteFloat(document, "height", "CylinderCollider");
    const int direction = RequireInteger(document, "direction", "CylinderCollider");
    if (radius < 0.001f || height < 0.001f)
        throw std::invalid_argument("CylinderCollider geometry is invalid");
    if (direction < 0 || direction > 2)
        throw std::invalid_argument("CylinderCollider.direction must be 0, 1, or 2");
}

bool CylinderCollider::DeserializeDocument(const nlohmann::json &document)
{
    try {
        ValidateSerializedDocument(document);
        const float stagedRadius = document["radius"].get<float>();
        const float stagedHeight = document["height"].get<float>();
        const int stagedDirection = document["direction"].get<int>();
        if (!Collider::DeserializeDocument(document))
            return false;
        m_radius = stagedRadius;
        m_height = stagedHeight;
        m_direction = stagedDirection;
        RebuildShape();
        return true;
    } catch (const std::exception &error) {
        INXLOG_ERROR("CylinderCollider::Deserialize failed: ", error.what());
        return false;
    }
}

std::unique_ptr<Component> CylinderCollider::Clone() const
{
    auto clone = std::make_unique<CylinderCollider>();
    CloneBaseColliderData(*clone);
    clone->m_radius = m_radius;
    clone->m_height = m_height;
    clone->m_direction = m_direction;
    return clone;
}

} // namespace infernux
