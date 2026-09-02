#include "Light.h"
#include "ComponentDocumentValidation.h"
#include "ComponentFactory.h"
#include "GameObject.h"
#include "SceneManager.h"
#include "Transform.h"
#include <core/log/InxLog.h>
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
    // Only runtime-resident scenes contribute to the global light list.
    // Prefab/template utility scenes must not leak here, while objects moved
    // to the DontDestroyOnLoad scene must be able to re-enable normally.
    if (auto *go = GetGameObject())
        if (!SceneManager::Instance().IsRuntimeScene(go->GetScene()))
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
    j["areaSize"] = {m_areaSize.x, m_areaSize.y};
    j["areaTwoSided"] = m_areaTwoSided;

    // Shadows
    j["shadows"] = static_cast<int>(m_shadows);
    j["shadowStrength"] = m_shadowStrength;
    j["shadowSoftness"] = m_shadowSoftness;

    // Rendering
    j["renderMode"] = static_cast<int>(m_renderMode);
    j["cullingMask"] = m_cullingMask;
    j["influenceDomains"] = m_influenceDomains;

    // Baking
    j["baked"] = m_baked;

    return j;
}

void Light::ValidateSerializedDocument(const nlohmann::json &j)
{
    using namespace component_document_validation;
    ValidateComponentDocument(j, "Light",
                              {"lightType", "color", "intensity", "range", "spotAngle", "outerSpotAngle", "areaSize",
                               "areaTwoSided", "shadows", "shadowStrength", "shadowSoftness", "renderMode",
                               "cullingMask", "influenceDomains", "baked"});
    const int lightType = RequireInteger(j, "lightType", "Light");
    RequireFiniteVector(j, "color", 3, "Light");
    const float intensity = RequireFiniteFloat(j, "intensity", "Light");
    const float range = RequireFiniteFloat(j, "range", "Light");
    const float spotAngle = RequireFiniteFloat(j, "spotAngle", "Light");
    const float outerSpotAngle = RequireFiniteFloat(j, "outerSpotAngle", "Light");
    RequireFiniteVector(j, "areaSize", 2, "Light");
    RequireBoolean(j, "areaTwoSided", "Light");
    const int shadows = RequireInteger(j, "shadows", "Light");
    const float shadowStrength = RequireFiniteFloat(j, "shadowStrength", "Light");
    const float shadowSoftness = RequireFiniteFloat(j, "shadowSoftness", "Light");
    const int renderMode = RequireInteger(j, "renderMode", "Light");
    const uint64_t cullingMask = RequireUnsignedInteger(j, "cullingMask", "Light");
    const uint64_t influenceDomains = RequireUnsignedInteger(j, "influenceDomains", "Light");
    RequireBoolean(j, "baked", "Light");

    if (lightType < static_cast<int>(LightType::Directional) || lightType > static_cast<int>(LightType::Area))
        throw std::invalid_argument("Light.lightType is unsupported");
    if (intensity < 0.0f || range <= 0.0f)
        throw std::invalid_argument("Light intensity and range are invalid");
    if (spotAngle <= 0.0f || outerSpotAngle < spotAngle || outerSpotAngle >= 180.0f)
        throw std::invalid_argument("Light spot cone angles are invalid");
    if (j["areaSize"][0].get<float>() <= 0.0f || j["areaSize"][1].get<float>() <= 0.0f)
        throw std::invalid_argument("Light area size is invalid");
    if (shadows < static_cast<int>(LightShadows::None) || shadows > static_cast<int>(LightShadows::Soft))
        throw std::invalid_argument("Light.shadows is unsupported");
    if (shadowStrength < 0.0f || shadowStrength > 1.0f || shadowSoftness < 0.25f || shadowSoftness > 8.0f)
        throw std::invalid_argument("Light shadow parameters are invalid");
    if (renderMode < static_cast<int>(LightRenderMode::Auto) ||
        renderMode > static_cast<int>(LightRenderMode::ForceVertex))
        throw std::invalid_argument("Light.renderMode is unsupported");
    if (cullingMask > std::numeric_limits<uint32_t>::max())
        throw std::invalid_argument("Light.cullingMask exceeds 32 bits");
    if ((influenceDomains & ~static_cast<uint64_t>(AllLightInfluenceDomains)) != 0u)
        throw std::invalid_argument("Light.influenceDomains contains unsupported flags");
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
        m_areaSize = glm::vec2(j["areaSize"][0].get<float>(), j["areaSize"][1].get<float>());
        m_areaTwoSided = j["areaTwoSided"].get<bool>();
        m_shadows = static_cast<LightShadows>(j["shadows"].get<int>());
        m_shadowStrength = j["shadowStrength"].get<float>();
        m_shadowSoftness = j["shadowSoftness"].get<float>();
        m_renderMode = static_cast<LightRenderMode>(j["renderMode"].get<int>());
        m_cullingMask = j["cullingMask"].get<uint32_t>();
        m_influenceDomains = j["influenceDomains"].get<uint32_t>();
        m_baked = j["baked"].get<bool>();

        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("Light::Deserialize failed: ", e.what());
        return false;
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
    clone->m_areaSize = m_areaSize;
    clone->m_areaTwoSided = m_areaTwoSided;
    clone->m_shadows = m_shadows;
    clone->m_shadowStrength = m_shadowStrength;
    clone->m_shadowSoftness = m_shadowSoftness;
    clone->m_renderMode = m_renderMode;
    clone->m_cullingMask = m_cullingMask;
    clone->m_influenceDomains = m_influenceDomains;
    clone->m_baked = m_baked;
    return clone;
}

} // namespace infernux
