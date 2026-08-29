#include "Camera.h"
#include "ComponentDocumentValidation.h"
#include "ComponentFactory.h"
#include "GameObject.h"
#include "Scene.h"
#include <cmath>
#include <core/log/InxLog.h>
#include <limits>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace infernux
{

INFERNUX_REGISTER_VALIDATED_COMPONENT("Camera", Camera)

void Camera::SetDepth(float depth)
{
    if (m_depth == depth)
        return;

    m_depth = depth;
    if (GameObject *owner = GetGameObject()) {
        if (Scene *scene = owner->GetScene())
            scene->BumpStructureVersion();
    }
}

// ============================================================================
// Serialization
// ============================================================================

nlohmann::json Camera::SerializeDocument() const
{
    json j = Component::SerializeDocument();

    j["projectionMode"] = static_cast<int>(m_projectionMode);
    j["fov"] = m_fov;
    j["aspectRatio"] = m_aspectRatio;
    j["orthoSize"] = m_orthoSize;
    j["nearClip"] = m_nearClip;
    j["farClip"] = m_farClip;
    j["depth"] = m_depth;
    j["cullingMask"] = m_cullingMask;
    j["clearFlags"] = static_cast<int>(m_clearFlags);
    j["backgroundColor"] = {m_backgroundColor.r, m_backgroundColor.g, m_backgroundColor.b, m_backgroundColor.a};
    j["dithering"] = m_dithering;
    j["stopNaNs"] = m_stopNaNs;

    return j;
}

void Camera::ValidateSerializedDocument(const nlohmann::json &j)
{
    using namespace component_document_validation;
    ValidateComponentDocument(j, "Camera",
                              {"projectionMode", "fov", "aspectRatio", "orthoSize", "nearClip", "farClip", "depth",
                               "cullingMask", "clearFlags", "backgroundColor"},
                              {"dithering", "stopNaNs"});
    const int projectionMode = RequireInteger(j, "projectionMode", "Camera");
    const float fov = RequireFiniteFloat(j, "fov", "Camera");
    const float aspectRatio = RequireFiniteFloat(j, "aspectRatio", "Camera");
    const float orthoSize = RequireFiniteFloat(j, "orthoSize", "Camera");
    const float nearClip = RequireFiniteFloat(j, "nearClip", "Camera");
    const float farClip = RequireFiniteFloat(j, "farClip", "Camera");
    RequireFiniteFloat(j, "depth", "Camera");
    const uint64_t cullingMask = RequireUnsignedInteger(j, "cullingMask", "Camera");
    const int clearFlags = RequireInteger(j, "clearFlags", "Camera");
    RequireFiniteVector(j, "backgroundColor", 4, "Camera");
    if (j.contains("dithering"))
        RequireBoolean(j, "dithering", "Camera");
    if (j.contains("stopNaNs"))
        RequireBoolean(j, "stopNaNs", "Camera");

    if (projectionMode < static_cast<int>(CameraProjection::Perspective) ||
        projectionMode > static_cast<int>(CameraProjection::Orthographic))
        throw std::invalid_argument("Camera.projectionMode is unsupported");
    if (fov <= 0.0f || fov >= 180.0f)
        throw std::invalid_argument("Camera.fov must be in (0, 180)");
    if (aspectRatio < 0.01f || orthoSize <= 0.0f)
        throw std::invalid_argument("Camera aspectRatio and orthoSize must be positive");
    if (nearClip <= 0.0f || farClip <= nearClip)
        throw std::invalid_argument("Camera clip planes are invalid");
    if (cullingMask > std::numeric_limits<uint32_t>::max())
        throw std::invalid_argument("Camera.cullingMask exceeds 32 bits");
    if (clearFlags < static_cast<int>(CameraClearFlags::Skybox) ||
        clearFlags > static_cast<int>(CameraClearFlags::DontClear))
        throw std::invalid_argument("Camera.clearFlags is unsupported");
}

bool Camera::DeserializeDocument(const nlohmann::json &j)
{
    try {
        ValidateSerializedDocument(j);
        if (!Component::DeserializeDocument(j))
            return false;

        m_projectionMode = static_cast<CameraProjection>(j["projectionMode"].get<int>());
        m_fov = j["fov"].get<float>();
        m_aspectRatio = j["aspectRatio"].get<float>();
        m_orthoSize = j["orthoSize"].get<float>();
        m_nearClip = j["nearClip"].get<float>();
        m_farClip = j["farClip"].get<float>();
        m_depth = j["depth"].get<float>();
        m_cullingMask = j["cullingMask"].get<uint32_t>();
        m_clearFlags = static_cast<CameraClearFlags>(j["clearFlags"].get<int>());
        const auto &background = j["backgroundColor"];
        m_backgroundColor = glm::vec4(background[0].get<float>(), background[1].get<float>(),
                                      background[2].get<float>(), background[3].get<float>());
        m_dithering = j.value("dithering", false);
        m_stopNaNs = j.value("stopNaNs", false);
        m_projectionDirty = true;

        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("Camera::Deserialize failed: ", e.what());
        return false;
    }
}

// ============================================================================
// Matrices
// ============================================================================

glm::mat4 Camera::GetViewMatrix() const
{
    if (!m_gameObject) {
        return glm::mat4{1.0f};
    }

    const Transform *transform = m_gameObject->GetTransform();

    // Use world-space position and orientation so that the camera
    // correctly follows parent transform hierarchy (e.g. camera
    // attached as a child of a moving character).
    glm::vec3 position = transform->GetWorldPosition();
    glm::vec3 forward = transform->GetWorldForward();
    glm::vec3 up = transform->GetWorldUp();

    return glm::lookAt(position, position + forward, up);
}

glm::mat4 Camera::GetProjectionMatrix() const
{
    if (m_projectionDirty) {
        UpdateProjectionMatrix();
    }
    return m_cachedProjection;
}

void Camera::UpdateProjectionMatrix() const
{
    if (m_projectionMode == CameraProjection::Perspective) {
        float fovRad = glm::radians(m_fov);
        m_cachedProjection = glm::perspective(fovRad, m_aspectRatio, m_nearClip, m_farClip);
    } else {
        float halfWidth = m_orthoSize * m_aspectRatio;
        float halfHeight = m_orthoSize;
        m_cachedProjection = glm::ortho(-halfWidth, halfWidth, -halfHeight, halfHeight, m_nearClip, m_farClip);
    }

    // Vulkan uses [0,1] depth range and inverted Y
    m_cachedProjection[1][1] *= -1.0f;

    m_projectionDirty = false;
}

glm::vec3 Camera::ScreenToWorldPoint(const glm::vec2 &screenPos, float depth) const
{
    if (m_screenWidth == 0 || m_screenHeight == 0) {
        return glm::vec3(0.0f);
    }

    // Normalise screen coords to NDC [-1, 1]
    // Vulkan uses top-left origin → invert Y in projection (already done),
    // so we keep standard NDC conversion here.
    float ndcX = (2.0f * screenPos.x / static_cast<float>(m_screenWidth)) - 1.0f;
    float ndcY = 1.0f - (2.0f * screenPos.y / static_cast<float>(m_screenHeight));

    // Vulkan depth range [0, 1] — depth parameter is already in this range
    float ndcZ = depth;

    glm::vec4 clipPos(ndcX, ndcY, ndcZ, 1.0f);

    // Inverse VP (note: our projection already has Vulkan Y-flip baked in)
    glm::mat4 invVP = glm::inverse(GetProjectionMatrix() * GetViewMatrix());
    glm::vec4 worldPos = invVP * clipPos;

    if (std::abs(worldPos.w) < 1e-8f) {
        return glm::vec3(0.0f);
    }

    return glm::vec3(worldPos) / worldPos.w;
}

glm::vec2 Camera::WorldToScreenPoint(const glm::vec3 &worldPos) const
{
    if (m_screenWidth == 0 || m_screenHeight == 0) {
        return glm::vec2(0.0f);
    }

    glm::vec4 clipPos = GetProjectionMatrix() * GetViewMatrix() * glm::vec4(worldPos, 1.0f);

    if (std::abs(clipPos.w) < 1e-8f) {
        return glm::vec2(0.0f);
    }

    // Perspective divide → NDC
    glm::vec3 ndc = glm::vec3(clipPos) / clipPos.w;

    // NDC → screen (Y inverted for Vulkan projection Y-flip)
    float screenX = (ndc.x + 1.0f) * 0.5f * static_cast<float>(m_screenWidth);
    float screenY = (1.0f - ndc.y) * 0.5f * static_cast<float>(m_screenHeight);

    return glm::vec2(screenX, screenY);
}

std::pair<glm::vec3, glm::vec3> Camera::ScreenPointToRay(const glm::vec2 &screenPos) const
{
    return ScreenPointToRay(screenPos, static_cast<float>(m_screenWidth), static_cast<float>(m_screenHeight));
}

std::pair<glm::vec3, glm::vec3> Camera::ScreenPointToRay(const glm::vec2 &screenPos, float viewportWidth,
                                                         float viewportHeight) const
{
    // Use a direct FOV + camera-transform approach (same as ScenePicker)
    // instead of inverse-VP unprojection.  The latter silently fails when
    // farClip is very large (e.g. 1e8) because the far-plane column of
    // inv(VP) degenerates to w ≈ 0 in float32.

    if (viewportWidth <= 0.0f || viewportHeight <= 0.0f || !m_gameObject) {
        return {glm::vec3(0.0f), glm::vec3(0.0f, 0.0f, 1.0f)};
    }

    const Transform *transform = m_gameObject->GetTransform();
    glm::vec3 rayOrigin = transform->GetWorldPosition();
    glm::vec3 forward = transform->GetWorldForward();
    glm::vec3 right = transform->GetWorldRight();
    glm::vec3 up = transform->GetWorldUp();

    // Screen → NDC  (screen: top-left origin; NDC: center origin)
    const float ndcX = (2.0f * screenPos.x / viewportWidth) - 1.0f;
    const float ndcY = (2.0f * screenPos.y / viewportHeight) - 1.0f;

    if (m_projectionMode == CameraProjection::Perspective) {
        const float fovRad = glm::radians(m_fov);
        const float tanHalfFov = std::tan(fovRad * 0.5f);
        const float aspectRatio = viewportWidth / viewportHeight;

        // View-space offsets on the image plane at unit distance
        const float viewDirX = ndcX * tanHalfFov * aspectRatio;
        const float viewDirY = -ndcY * tanHalfFov; // screen-Y down → world-Y up

        glm::vec3 dir = glm::normalize(forward + right * viewDirX + up * viewDirY);
        return {rayOrigin, dir};
    } else {
        // Orthographic: ray origin shifts on the image plane; direction = forward
        const float halfHeight = m_orthoSize;
        const float halfWidth = m_orthoSize * (viewportWidth / viewportHeight);

        glm::vec3 origin = rayOrigin + right * (ndcX * halfWidth) + up * (-ndcY * halfHeight);
        return {origin, forward};
    }
}

std::unique_ptr<Component> Camera::Clone() const
{
    auto clone = std::make_unique<Camera>();
    clone->m_enabled = m_enabled;
    clone->m_executionOrder = m_executionOrder;
    clone->m_projectionMode = m_projectionMode;
    clone->m_fov = m_fov;
    clone->m_aspectRatio = m_aspectRatio;
    clone->m_orthoSize = m_orthoSize;
    clone->m_nearClip = m_nearClip;
    clone->m_farClip = m_farClip;
    clone->m_depth = m_depth;
    clone->m_cullingMask = m_cullingMask;
    clone->m_clearFlags = m_clearFlags;
    clone->m_backgroundColor = m_backgroundColor;
    clone->m_dithering = m_dithering;
    clone->m_stopNaNs = m_stopNaNs;
    clone->m_screenWidth = m_screenWidth;
    clone->m_screenHeight = m_screenHeight;
    return clone;
}

} // namespace infernux
