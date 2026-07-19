#include "Light.h"
#include "ComponentDocumentValidation.h"
#include "ComponentFactory.h"
#include "GameObject.h"
#include "SceneManager.h"
#include "Transform.h"
#include <InxLog.h>
#include <limits>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace infernux
{

// Register Light component with factory
INFERNUX_REGISTER_VALIDATED_COMPONENT("Light", Light)

Light::~Light()
{
    SceneManager::Instance().UnregisterLight(this);
}

void Light::OnEnable()
{
    // Only register with the global light list if this object belongs to
    // the active scene.  Prefab template cache objects must not leak here.
    if (auto *go = GetGameObject())
        if (go->GetScene() != SceneManager::Instance().GetActiveScene())
            return;
    SceneManager::Instance().RegisterLight(this);
}

void Light::OnDisable()
{
    SceneManager::Instance().UnregisterLight(this);
}

nlohmann::json Light::SerializeDocument() const
{
    json j = Component::SerializeDocument();

    // Light type
    j["lightType"] = static_cast<int>(m_lightType);

    // Color & intensity
    j["color"] = {m_color.r, m_color.g, m_color.b};
    j["intensity"] = m_intensity;

    // Range
    j["range"] = m_range;

    // Spot settings
    j["spotAngle"] = m_spotAngle;
    j["outerSpotAngle"] = m_outerSpotAngle;

    // Shadows
    j["shadows"] = static_cast<int>(m_shadows);
    j["shadowStrength"] = m_shadowStrength;
    j["shadowBias"] = m_shadowBias;
    j["shadowNormalBias"] = m_shadowNormalBias;

    // Rendering
    j["renderMode"] = static_cast<int>(m_renderMode);
    j["cullingMask"] = m_cullingMask;

    // Baking
    j["baked"] = m_baked;

    return j;
}

void Light::ValidateSerializedDocument(const nlohmann::json &j)
{
    using namespace component_document_validation;
    ValidateComponentDocument(j, "Light",
                              {"lightType", "color", "intensity", "range", "spotAngle", "outerSpotAngle", "shadows",
                               "shadowStrength", "shadowBias", "shadowNormalBias", "renderMode", "cullingMask",
                               "baked"});
    const int lightType = RequireInteger(j, "lightType", "Light");
    RequireFiniteVector(j, "color", 3, "Light");
    const float intensity = RequireFiniteFloat(j, "intensity", "Light");
    const float range = RequireFiniteFloat(j, "range", "Light");
    const float spotAngle = RequireFiniteFloat(j, "spotAngle", "Light");
    const float outerSpotAngle = RequireFiniteFloat(j, "outerSpotAngle", "Light");
    const int shadows = RequireInteger(j, "shadows", "Light");
    const float shadowStrength = RequireFiniteFloat(j, "shadowStrength", "Light");
    const float shadowBias = RequireFiniteFloat(j, "shadowBias", "Light");
    const float shadowNormalBias = RequireFiniteFloat(j, "shadowNormalBias", "Light");
    const int renderMode = RequireInteger(j, "renderMode", "Light");
    const uint64_t cullingMask = RequireUnsignedInteger(j, "cullingMask", "Light");
    RequireBoolean(j, "baked", "Light");

    if (lightType < static_cast<int>(LightType::Directional) || lightType > static_cast<int>(LightType::Area))
        throw std::invalid_argument("Light.lightType is unsupported");
    if (intensity < 0.0f || range <= 0.0f)
        throw std::invalid_argument("Light intensity and range are invalid");
    if (spotAngle <= 0.0f || outerSpotAngle < spotAngle || outerSpotAngle >= 180.0f)
        throw std::invalid_argument("Light spot cone angles are invalid");
    if (shadows < static_cast<int>(LightShadows::None) || shadows > static_cast<int>(LightShadows::Soft))
        throw std::invalid_argument("Light.shadows is unsupported");
    if (shadowStrength < 0.0f || shadowStrength > 1.0f || shadowBias < 0.0f || shadowNormalBias < 0.0f)
        throw std::invalid_argument("Light shadow parameters are invalid");
    if (renderMode < static_cast<int>(LightRenderMode::Auto) ||
        renderMode > static_cast<int>(LightRenderMode::ForceVertex))
        throw std::invalid_argument("Light.renderMode is unsupported");
    if (cullingMask > std::numeric_limits<uint32_t>::max())
        throw std::invalid_argument("Light.cullingMask exceeds 32 bits");
}

bool Light::DeserializeDocument(const nlohmann::json &j)
{
    try {
        ValidateSerializedDocument(j);
        if (!Component::DeserializeDocument(j))
            return false;

        m_lightType = static_cast<LightType>(j["lightType"].get<int>());
        m_color = glm::vec3(j["color"][0].get<float>(), j["color"][1].get<float>(), j["color"][2].get<float>());
        m_intensity = j["intensity"].get<float>();
        m_range = j["range"].get<float>();
        m_spotAngle = j["spotAngle"].get<float>();
        m_outerSpotAngle = j["outerSpotAngle"].get<float>();
        m_shadows = static_cast<LightShadows>(j["shadows"].get<int>());
        m_shadowStrength = j["shadowStrength"].get<float>();
        m_shadowBias = j["shadowBias"].get<float>();
        m_shadowNormalBias = j["shadowNormalBias"].get<float>();
        m_renderMode = static_cast<LightRenderMode>(j["renderMode"].get<int>());
        m_cullingMask = j["cullingMask"].get<uint32_t>();
        m_baked = j["baked"].get<bool>();

        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("Light::Deserialize failed: ", e.what());
        return false;
    }
}

// ============================================================================
// Shadow mapping — light view/projection helpers
// ============================================================================

glm::mat4 Light::GetLightViewMatrix(const glm::vec3 &shadowCenter) const
{
    // Default: look along -Z
    glm::vec3 lightDir = glm::vec3(0.0f, -1.0f, 0.0f);
    glm::vec3 lightPos = glm::vec3(0.0f, 10.0f, 0.0f);

    // If attached to a GameObject, use its transform
    if (GetGameObject()) {
        Transform *transform = GetGameObject()->GetTransform();
        if (transform) {
            lightDir = transform->GetWorldForward();
            lightPos = transform->GetWorldPosition();
        }
    }

    // For directional lights, center the shadow frustum on shadowCenter
    // (typically the camera position) and place the light far along -lightDir
    if (m_lightType == LightType::Directional) {
        lightPos = shadowCenter - lightDir * 50.0f;
    }

    glm::vec3 target = lightPos + lightDir;
    glm::vec3 up = glm::vec3(0.0f, 1.0f, 0.0f);

    // Avoid degenerate case when light points straight up/down
    if (std::abs(glm::dot(lightDir, up)) > 0.99f) {
        up = glm::vec3(0.0f, 0.0f, 1.0f);
    }

    return glm::lookAt(lightPos, target, up);
}

glm::mat4 Light::GetLightProjectionMatrix(float shadowExtent, float nearPlane, float farPlane) const
{
    switch (m_lightType) {
    case LightType::Directional:
        // Orthographic projection for directional light shadows
        return glm::ortho(-shadowExtent, shadowExtent, -shadowExtent, shadowExtent, nearPlane, farPlane);

    case LightType::Spot: {
        // Perspective projection matching the spot cone angle
        float fov = glm::radians(m_outerSpotAngle * 2.0f);
        return glm::perspective(fov, 1.0f, nearPlane, m_range);
    }

    case LightType::Point:
    case LightType::Area:
    default:
        // Point light shadow map requires cubemap — not yet supported
        // Return identity as placeholder
        return glm::mat4(1.0f);
    }
}

std::unique_ptr<Component> Light::Clone() const
{
    auto clone = std::make_unique<Light>();
    clone->m_enabled = m_enabled;
    clone->m_executionOrder = m_executionOrder;
    clone->m_lightType = m_lightType;
    clone->m_color = m_color;
    clone->m_intensity = m_intensity;
    clone->m_range = m_range;
    clone->m_spotAngle = m_spotAngle;
    clone->m_outerSpotAngle = m_outerSpotAngle;
    clone->m_shadows = m_shadows;
    clone->m_shadowStrength = m_shadowStrength;
    clone->m_shadowBias = m_shadowBias;
    clone->m_shadowNormalBias = m_shadowNormalBias;
    clone->m_renderMode = m_renderMode;
    clone->m_cullingMask = m_cullingMask;
    clone->m_baked = m_baked;
    return clone;
}

} // namespace infernux
