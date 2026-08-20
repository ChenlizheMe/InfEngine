#include "LightingData.h"
#include "Camera.h"
#include "GameObject.h"
#include "Light.h"
#include "Scene.h"
#include "SceneManager.h"
#include "SceneRenderBridge.h"
#include "Transform.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <core/log/InxLog.h>
#include <core/types/ColorSpace.h>
#include <cstring>
#include <limits>

namespace infernux
{

void SceneLightCollector::CollectLights(Scene *scene, const glm::vec3 &cameraPosition)
{
    Clear();

    if (!scene) {
        return;
    }

    // Set camera position
    m_lightingUBO.worldSpaceCameraPos = glm::vec4(cameraPosition, 1.0f);

    // Iterate the Light component registry (O(L) where L = light count)
    // instead of walking the full scene tree (O(N) where N = all objects).
    const auto &activeLights = SceneManager::Instance().GetActiveLights();

    for (Light *light : activeLights) {
        if (!light || !light->IsEnabled())
            continue;

        GameObject *obj = light->GetGameObject();
        if (!obj || !obj->IsActiveInHierarchy())
            continue;

        Transform *transform = obj->GetTransform();
        if (!transform)
            continue;

        glm::vec3 worldPosition = transform->GetWorldPosition();
        glm::vec3 worldForward = transform->GetWorldForward();

        AddCanonicalLight(light, worldPosition, worldForward);

        switch (light->GetLightType()) {
        case LightType::Directional:
            AddDirectionalLight(light);
            break;
        case LightType::Point:
            AddPointLight(light, worldPosition);
            break;
        case LightType::Spot:
            AddSpotLight(light, worldPosition, worldForward);
            break;
        case LightType::Area:
            AddAreaLight(light, worldPosition, worldForward);
            break;
        }
    }

    // Sort point lights by importance
    SortPointLightsByImportance(cameraPosition);

    // Update light counts in UBO
    m_lightingUBO.lightCounts =
        glm::ivec4(static_cast<int>(m_directionalLightCount), static_cast<int>(m_pointLightCount),
                   static_cast<int>(m_spotLightCount), static_cast<int>(m_areaLightCount));

    // Prepare simplified UBO
    PrepareSimpleLightingUBO();
}

void SceneLightCollector::Clear()
{
    m_canonicalLightSnapshot.Clear(m_canonicalGeneration);
    m_lightingUBO = LightingUBO{};
    m_simpleLightingUBO = SimpleLightingUBO{};
    m_directionalLightCount = 0;
    m_pointLightCount = 0;
    m_spotLightCount = 0;
    m_areaLightCount = 0;
    m_directionalLightIds.fill(0);
    m_pointLightIds.fill(0);
    m_spotLightIds.fill(0);
    m_areaLightIds.fill(0);
    m_pointLightSortBuffer.clear();
    m_shadowFrame = {};

    // Set default ambient (authored sRGB -> linear; alpha channels carry intensity/mode)
    m_lightingUBO.ambientSkyColor = glm::vec4(inx::color::SrgbToLinear(glm::vec3(0.2f, 0.2f, 0.3f)), 0.5f);
    m_lightingUBO.ambientGroundColor = glm::vec4(inx::color::SrgbToLinear(glm::vec3(0.1f, 0.1f, 0.1f)), 0.3f);
    m_lightingUBO.ambientEquatorColor =
        glm::vec4(inx::color::SrgbToLinear(glm::vec3(0.15f, 0.15f, 0.2f)), 0.0f); // mode = 0 (flat)
}

void SceneLightCollector::PublishCanonicalGeneration()
{
    const auto bytesEqual = [](const std::vector<CanonicalLightData> &left,
                               const std::vector<CanonicalLightData> &right) {
        return left.size() == right.size() &&
               (left.empty() || std::memcmp(left.data(), right.data(), left.size() * sizeof(CanonicalLightData)) == 0);
    };
    const bool changed = !m_hasCanonicalPublication ||
                         !bytesEqual(m_canonicalLightSnapshot.directionalLights, m_lastCanonicalDirectionalLights) ||
                         !bytesEqual(m_canonicalLightSnapshot.localLights, m_lastCanonicalLocalLights);
    if (changed) {
        ++m_canonicalGeneration;
        m_lastCanonicalDirectionalLights = m_canonicalLightSnapshot.directionalLights;
        m_lastCanonicalLocalLights = m_canonicalLightSnapshot.localLights;
        m_hasCanonicalPublication = true;
    }
    m_canonicalLightSnapshot.generation = m_canonicalGeneration;
}

void SceneLightCollector::AddCanonicalLight(const Light *light, const glm::vec3 &worldPosition,
                                            const glm::vec3 &worldDirection)
{
    if (!light)
        return;

    CanonicalLightData data{};
    const LightType lightType = light->GetLightType();
    const bool directional = lightType == LightType::Directional;
    const bool spot = lightType == LightType::Spot;
    const glm::vec3 fallbackDirection(0.0f, -1.0f, 0.0f);
    const glm::vec3 direction =
        glm::dot(worldDirection, worldDirection) > 0.0f ? glm::normalize(worldDirection) : fallbackDirection;

    data.positionRange = glm::vec4(worldPosition, directional ? 0.0f : std::max(light->GetRange(), 0.0f));
    data.directionOuterCos =
        glm::vec4(direction, spot ? std::cos(glm::radians(light->GetOuterSpotAngle() * 0.5f)) : -1.0f);
    data.colorIntensity = glm::vec4(inx::color::SrgbToLinear(light->GetColor()), std::max(light->GetIntensity(), 0.0f));
    data.shadowAndInnerCos = glm::vec4(light->GetShadowStrength(), light->GetShadowBias(), light->GetShadowNormalBias(),
                                       spot ? std::cos(glm::radians(light->GetSpotAngle() * 0.5f)) : -1.0f);
    if (lightType == LightType::Area && light->GetTransform()) {
        const glm::vec2 size = light->GetAreaSize();
        data.areaRightWidth = glm::vec4(glm::normalize(light->GetTransform()->GetWorldRight()), size.x);
        data.areaUpHeight = glm::vec4(glm::normalize(light->GetTransform()->GetWorldUp()), size.y);
        data.directionOuterCos.w = light->GetAreaTwoSided() ? 1.0f : 0.0f;
    }
    data.metadata = glm::uvec4(static_cast<uint32_t>(lightType), light->GetCullingMask(),
                               static_cast<uint32_t>(light->GetShadows()), light->GetInfluenceDomains());
    const uint64_t lightId = light->GetGameObject() ? light->GetGameObject()->GetID() : 0;
    data.identityAndShadow = glm::uvec4(static_cast<uint32_t>(lightId), static_cast<uint32_t>(lightId >> 32u), 0u, 0u);
    m_canonicalLightSnapshot.Add(data);
}

void SceneLightCollector::AddDirectionalLight(const Light *light)
{
    if (m_directionalLightCount >= MAX_DIRECTIONAL_LIGHTS) {
        INXLOG_WARN("Maximum directional lights (", MAX_DIRECTIONAL_LIGHTS, ") exceeded, ignoring light");
        return;
    }

    // Get direction from the light's transform (forward vector)
    // Convention: direction = light ray direction (the way light travels).
    // The shader computes L = normalize(-direction) to get toward-light vector.
    // GetForward() already returns the light ray direction, so NO negation here.
    Transform *transform = light->GetTransform();
    glm::vec3 direction = transform ? transform->GetWorldForward() : glm::vec3(0.0f, -1.0f, 0.0f);

    DirectionalLightData &data = m_lightingUBO.directionalLights[m_directionalLightCount];
    data.direction = glm::vec4(glm::normalize(direction), 0.0f);
    // Linear-space color in rgb, intensity in w (shader does color.rgb * color.w)
    data.color = glm::vec4(inx::color::SrgbToLinear(light->GetColor()), light->GetIntensity());

    // Shadow parameters: x=strength, y=bias, z=normalBias, w=shadowType (0=off, 1=hard, 2=soft)
    float shadowType = 0.0f;
    if (light->GetShadows() == LightShadows::Hard)
        shadowType = 1.0f;
    else if (light->GetShadows() == LightShadows::Soft)
        shadowType = 2.0f;
    data.shadowParams =
        glm::vec4(light->GetShadowStrength(), light->GetShadowBias(), light->GetShadowNormalBias(), shadowType);
    data.metadata = glm::uvec4(light->GetCullingMask(), light->GetInfluenceDomains(), 0u, 0u);
    m_directionalLightIds[m_directionalLightCount] = light->GetGameObject() ? light->GetGameObject()->GetID() : 0;

    m_directionalLightCount++;
}

void SceneLightCollector::AddPointLight(const Light *light, const glm::vec3 &worldPosition)
{
    // Don't add to main buffer yet, add to sort buffer
    PointLightSortData sortData;
    sortData.data.position = glm::vec4(worldPosition, light->GetRange());
    // Linear-space color in rgb, intensity in w (shader does color.rgb * color.w)
    sortData.data.color = glm::vec4(inx::color::SrgbToLinear(light->GetColor()), light->GetIntensity());
    // Store range in x for URP-style smooth attenuation (yz unused, kept for compatibility)
    sortData.data.attenuation = glm::vec4(light->GetRange(), 0.0f, 0.0f, 0.0f);
    sortData.data.shadowParams = glm::vec4(light->GetShadowStrength(), light->GetShadowBias(),
                                           light->GetShadowNormalBias(), static_cast<float>(light->GetShadows()));
    sortData.data.metadata = glm::uvec4(light->GetCullingMask(), light->GetInfluenceDomains(), 0u, 0u);
    sortData.lightId = light->GetGameObject() ? light->GetGameObject()->GetID() : 0;
    sortData.importance = 0.0f; // Will be calculated during sorting

    m_pointLightSortBuffer.push_back(sortData);
}

void SceneLightCollector::AddSpotLight(const Light *light, const glm::vec3 &worldPosition,
                                       const glm::vec3 &worldDirection)
{
    if (m_spotLightCount >= MAX_SPOT_LIGHTS) {
        INXLOG_WARN("Maximum spot lights (", MAX_SPOT_LIGHTS, ") exceeded, ignoring light");
        return;
    }

    SpotLightData &data = m_lightingUBO.spotLights[m_spotLightCount];
    data.position = glm::vec4(worldPosition, light->GetRange());
    data.direction = glm::vec4(glm::normalize(worldDirection), 0.0f);
    // Linear-space color in rgb, intensity in w (shader does color.rgb * color.w)
    data.color = glm::vec4(inx::color::SrgbToLinear(light->GetColor()), light->GetIntensity());

    // Calculate cos of angles for spot falloff
    float innerAngleRad = glm::radians(light->GetSpotAngle() * 0.5f);
    float outerAngleRad = glm::radians(light->GetOuterSpotAngle() * 0.5f);
    data.spotParams = glm::vec4(std::cos(innerAngleRad), std::cos(outerAngleRad), 0.0f, 0.0f);
    // Store range in x for URP-style smooth attenuation
    data.attenuation = glm::vec4(light->GetRange(), 0.0f, 0.0f, 0.0f);
    data.shadowParams = glm::vec4(light->GetShadowStrength(), light->GetShadowBias(), light->GetShadowNormalBias(),
                                  static_cast<float>(light->GetShadows()));
    data.metadata = glm::uvec4(light->GetCullingMask(), light->GetInfluenceDomains(), 0u, 0u);
    m_spotLightIds[m_spotLightCount] = light->GetGameObject() ? light->GetGameObject()->GetID() : 0;

    m_spotLightCount++;
}

void SceneLightCollector::SortPointLightsByImportance(const glm::vec3 &cameraPosition)
{
    // Calculate importance for each point light
    for (auto &sortData : m_pointLightSortBuffer) {
        glm::vec3 lightPos = glm::vec3(sortData.data.position);
        float range = sortData.data.position.w;
        float intensity = sortData.data.color.a;
        float distance = glm::length(lightPos - cameraPosition);

        // Importance = intensity / (distance + 1)^2, capped at range
        if (distance > range * 2.0f) {
            sortData.importance = 0.0f;
        } else {
            sortData.importance = intensity / ((distance + 1.0f) * (distance + 1.0f));
        }
    }

    // Sort by importance (highest first)
    std::sort(m_pointLightSortBuffer.begin(), m_pointLightSortBuffer.end(),
              [](const PointLightSortData &a, const PointLightSortData &b) { return a.importance > b.importance; });

    // Copy to UBO (up to MAX_POINT_LIGHTS)
    m_pointLightCount = std::min(static_cast<uint32_t>(m_pointLightSortBuffer.size()), MAX_POINT_LIGHTS);
    for (uint32_t i = 0; i < m_pointLightCount; ++i) {
        m_lightingUBO.pointLights[i] = m_pointLightSortBuffer[i].data;
        m_pointLightIds[i] = m_pointLightSortBuffer[i].lightId;
    }
}

void SceneLightCollector::AddAreaLight(const Light *light, const glm::vec3 &worldPosition,
                                       const glm::vec3 &worldDirection)
{
    if (m_areaLightCount >= MAX_AREA_LIGHTS)
        return;
    AreaLightData &data = m_lightingUBO.areaLights[m_areaLightCount];
    const Transform *transform = light->GetTransform();
    const glm::vec3 right = transform ? glm::normalize(transform->GetWorldRight()) : glm::vec3(1, 0, 0);
    const glm::vec3 up = transform ? glm::normalize(transform->GetWorldUp()) : glm::vec3(0, 1, 0);
    const glm::vec2 size = light->GetAreaSize();
    data.positionRange = glm::vec4(worldPosition, light->GetRange());
    data.direction = glm::vec4(glm::normalize(worldDirection), light->GetAreaTwoSided() ? 1.0f : 0.0f);
    data.rightWidth = glm::vec4(right, size.x);
    data.upHeight = glm::vec4(up, size.y);
    data.color = glm::vec4(inx::color::SrgbToLinear(light->GetColor()), light->GetIntensity());
    data.shadowParams = glm::vec4(light->GetShadowStrength(), light->GetShadowBias(), light->GetShadowNormalBias(),
                                  static_cast<float>(light->GetShadows()));
    data.metadata = glm::uvec4(light->GetCullingMask(), light->GetInfluenceDomains(), 0u, 0u);
    m_areaLightIds[m_areaLightCount] = light->GetGameObject() ? light->GetGameObject()->GetID() : 0;
    ++m_areaLightCount;
}

void SceneLightCollector::PrepareSimpleLightingUBO()
{
    // Copy ambient
    m_simpleLightingUBO.ambientColor = m_lightingUBO.ambientSkyColor;

    // Main directional light (first directional light)
    if (m_directionalLightCount > 0) {
        m_simpleLightingUBO.mainLightDirection = m_lightingUBO.directionalLights[0].direction;
        m_simpleLightingUBO.mainLightColor = m_lightingUBO.directionalLights[0].color;
    } else {
        m_simpleLightingUBO.mainLightDirection = glm::vec4(0.0f, -1.0f, 0.0f, 0.0f);
        m_simpleLightingUBO.mainLightColor = glm::vec4(0.0f);
    }

    // Camera position
    m_simpleLightingUBO.cameraPosition = m_lightingUBO.worldSpaceCameraPos;

    // Point lights (up to 16 for simple mode)
    m_simpleLightingUBO.pointLightCount = static_cast<int>(std::min(m_pointLightCount, 16u));
    for (int i = 0; i < m_simpleLightingUBO.pointLightCount; ++i) {
        m_simpleLightingUBO.pointLights[i] = m_lightingUBO.pointLights[i];
    }
}

void SceneLightCollector::SetAmbientColor(const glm::vec3 &color, float intensity)
{
    m_lightingUBO.ambientSkyColor = glm::vec4(inx::color::SrgbToLinear(color), intensity);
    m_lightingUBO.ambientEquatorColor.a = 0.0f; // Flat mode
}

void SceneLightCollector::SetAmbientGradient(const glm::vec3 &skyColor, const glm::vec3 &equatorColor,
                                             const glm::vec3 &groundColor, float intensity)
{
    const float scale = std::max(intensity, 0.0f);
    m_lightingUBO.ambientSkyColor = glm::vec4(inx::color::SrgbToLinear(skyColor) * scale, 1.0f);
    m_lightingUBO.ambientEquatorColor =
        glm::vec4(inx::color::SrgbToLinear(equatorColor) * scale, 1.0f); // Gradient mode
    m_lightingUBO.ambientGroundColor = glm::vec4(inx::color::SrgbToLinear(groundColor) * scale, 1.0f);
}

void SceneLightCollector::SetAmbientLinear(const glm::vec4 &skyColor, const glm::vec4 &equatorColor,
                                           const glm::vec4 &groundColor)
{
    m_lightingUBO.ambientSkyColor = skyColor;
    m_lightingUBO.ambientEquatorColor = equatorColor;
    m_lightingUBO.ambientGroundColor = groundColor;
}

void SceneLightCollector::SetFog(bool enabled, const glm::vec3 &color, float density, float start, float end, int mode)
{
    m_lightingUBO.fogColor = glm::vec4(inx::color::SrgbToLinear(color), enabled ? 1.0f : 0.0f);
    m_lightingUBO.fogParams = glm::vec4(density, start, end, static_cast<float>(mode));
}

void SceneLightCollector::UpdateTime(float time, float deltaTime)
{
    m_lightingUBO.time = glm::vec4(time, std::sin(time), std::cos(time), deltaTime);
}

void SceneLightCollector::SetCameraPosition(const glm::vec3 &position)
{
    m_lightingUBO.worldSpaceCameraPos = glm::vec4(position, 1.0f);
    m_simpleLightingUBO.cameraPosition = glm::vec4(position, 1.0f);
}

void SceneLightCollector::ComputeShadowVP(Scene *scene, const glm::vec3 &cameraPos, float shadowMapResolution,
                                          const Camera *camera, const lighting::ShadowDepthRange &visibleDepthRange)
{
    m_shadowFrame = {};
    if (!scene || !std::isfinite(shadowMapResolution) || shadowMapResolution < 1.0f)
        return;
    m_shadowFrame.atlasSize = static_cast<uint32_t>(shadowMapResolution);

    if (!camera)
        camera = SceneRenderBridge::Instance().GetEditorCamera();

    lighting::ShadowCamera shadowCamera;
    shadowCamera.position = cameraPos;
    if (camera) {
        shadowCamera.nearClip = std::max(camera->GetNearClip(), 0.01f);
        shadowCamera.farClip = std::max(camera->GetFarClip(), shadowCamera.nearClip + 0.01f);
        shadowCamera.verticalFovRadians = glm::radians(camera->GetFieldOfView());
        shadowCamera.aspect = std::max(camera->GetAspectRatio(), 0.01f);
        shadowCamera.orthographic = camera->GetProjectionMode() == CameraProjection::Orthographic;
        shadowCamera.orthographicHalfHeight = std::max(camera->GetOrthographicSize(), 0.01f);
        if (camera->GetGameObject() && camera->GetGameObject()->GetTransform()) {
            const Transform *transform = camera->GetGameObject()->GetTransform();
            shadowCamera.position = transform->GetWorldPosition();
            shadowCamera.forward = glm::normalize(transform->GetWorldForward());
            shadowCamera.right = glm::normalize(transform->GetWorldRight());
            shadowCamera.up = glm::normalize(transform->GetWorldUp());
        }
    }

    // Cascades must cover exactly what the camera renders. Unproject the real
    // view-projection matrix instead of rebuilding the frustum from
    // fov/aspect: any drift between the two (render-target aspect updates,
    // orthographic or oblique projections) shows up as shadows clipped along
    // a straight light-space edge, worst when the camera is close to objects.
    bool haveFrustumCorners = false;
    std::array<glm::vec3, 4> frustumNearCorners{};
    std::array<glm::vec3, 4> frustumFarCorners{};
    if (camera) {
        const glm::mat4 inverseViewProjection = glm::inverse(camera->GetViewProjectionMatrix());
        const std::array<glm::vec2, 4> ndcCorners = {glm::vec2(-1.0f, -1.0f), glm::vec2(1.0f, -1.0f),
                                                     glm::vec2(1.0f, 1.0f), glm::vec2(-1.0f, 1.0f)};
        haveFrustumCorners = true;
        for (size_t index = 0; index < ndcCorners.size(); ++index) {
            const glm::vec4 nearPoint = inverseViewProjection * glm::vec4(ndcCorners[index], 0.0f, 1.0f);
            const glm::vec4 farPoint = inverseViewProjection * glm::vec4(ndcCorners[index], 1.0f, 1.0f);
            if (std::abs(nearPoint.w) < 1e-9f || std::abs(farPoint.w) < 1e-9f) {
                haveFrustumCorners = false;
                break;
            }
            frustumNearCorners[index] = glm::vec3(nearPoint) / nearPoint.w;
            frustumFarCorners[index] = glm::vec3(farPoint) / farPoint.w;
            for (int component = 0; component < 3; ++component) {
                if (!std::isfinite(frustumNearCorners[index][component]) ||
                    !std::isfinite(frustumFarCorners[index][component])) {
                    haveFrustumCorners = false;
                }
            }
            if (!haveFrustumCorners)
                break;
        }
    }
    // Points along a frustum edge vary linearly in view depth, so a slice at
    // view depth d sits at fraction (d - near) / (far - near) along each edge.
    const float frustumDepthRange = std::max(shadowCamera.farClip - shadowCamera.nearClip, 0.001f);
    const auto frustumSliceAt = [&](float viewDepth) {
        const float t = glm::clamp((viewDepth - shadowCamera.nearClip) / frustumDepthRange, 0.0f, 1.0f);
        std::array<glm::vec3, 4> corners{};
        for (size_t index = 0; index < corners.size(); ++index)
            corners[index] = glm::mix(frustumNearCorners[index], frustumFarCorners[index], t);
        return corners;
    };

    std::vector<Light *> shadowLights;
    for (Light *light : SceneManager::Instance().GetActiveLights()) {
        if (!light || !light->IsEnabled() || light->GetShadows() == LightShadows::None)
            continue;
        GameObject *object = light->GetGameObject();
        if (!object || object->GetScene() != scene || !object->IsActiveInHierarchy() || !object->GetTransform())
            continue;
        shadowLights.push_back(light);
    }
    std::stable_sort(shadowLights.begin(), shadowLights.end(), [&](const Light *left, const Light *right) {
        const auto typePriority = [](LightType type) {
            return type == LightType::Directional ? 0u
                   : type == LightType::Spot      ? 1u
                   : type == LightType::Point     ? 2u
                                                  : 3u;
        };
        const uint32_t leftType = typePriority(left->GetLightType());
        const uint32_t rightType = typePriority(right->GetLightType());
        if (leftType != rightType)
            return leftType < rightType;
        const float leftImportance = left->GetIntensity() * std::max(left->GetRange(), 1.0f);
        const float rightImportance = right->GetIntensity() * std::max(right->GetRange(), 1.0f);
        if (leftImportance != rightImportance)
            return leftImportance > rightImportance;
        return left->GetGameObject()->GetID() < right->GetGameObject()->GetID();
    });

    lighting::ShadowAtlasAllocator atlas(m_shadowFrame.atlasSize);
    bool mainDirectional = true;
    for (Light *light : shadowLights) {
        if (m_shadowFrame.views.size() >= lighting::MaxShadowViews)
            break;
        const uint64_t lightId = light->GetGameObject()->GetID();
        const glm::vec3 position = light->GetTransform()->GetWorldPosition();
        const glm::vec3 direction = light->GetTransform()->GetWorldForward();
        const uint32_t firstView = static_cast<uint32_t>(m_shadowFrame.views.size());

        if (light->GetLightType() == LightType::Directional) {
            if (firstView + lighting::DirectionalCascadeCount > lighting::MaxShadowViews)
                continue;
            const uint32_t atlasSize = m_shadowFrame.atlasSize;
            // Unity-style equal cascade tiles: every cascade of a directional
            // light uses the same resolution, so shadow quality does not fall
            // off a cliff at a split boundary the way a 2:2:1:1 layout did.
            // The sole shadow light fills the whole atlas with a 2x2 grid;
            // with more shadow lights the main light keeps 3/8-atlas tiles
            // (56% of the area), leaving room for spot/point/secondary views.
            const bool soleShadowLight = shadowLights.size() == 1;
            const uint32_t mainTile = soleShadowLight ? atlasSize / 2u : atlasSize * 3u / 8u;
            const std::array<uint32_t, lighting::DirectionalCascadeCount> mainSizes{mainTile, mainTile, mainTile,
                                                                                    mainTile};
            const std::array<uint32_t, lighting::DirectionalCascadeCount> secondarySizes{
                atlasSize / 4u, atlasSize / 4u, atlasSize / 4u, atlasSize / 4u};
            const auto &sizes = mainDirectional ? mainSizes : secondarySizes;
            // Fit each camera to its visible depth range. Distant objects move
            // the logarithmic distribution outward without wasting close-up
            // resolution in ordinary scenes.
            const auto splits =
                lighting::AdaptiveCascadeSplits(shadowCamera.nearClip, visibleDepthRange, shadowCamera.farClip);
            const auto tiles = atlas.AllocateBatchWithFallback(sizes, std::max(atlasSize / 16u, 16u), 4);
            if (!tiles)
                continue;
            for (uint32_t cascade = 0; cascade < lighting::DirectionalCascadeCount; ++cascade) {
                if (haveFrustumCorners) {
                    const auto nearSlice = frustumSliceAt(splits[cascade]);
                    const auto farSlice = frustumSliceAt(splits[cascade + 1]);
                    const std::array<glm::vec3, 8> sliceCorners = {nearSlice[0], nearSlice[1], nearSlice[2],
                                                                   nearSlice[3], farSlice[0],  farSlice[1],
                                                                   farSlice[2],  farSlice[3]};
                    m_shadowFrame.views.push_back(lighting::BuildStableDirectionalCascade(
                        lightId, cascade, sliceCorners, direction, splits[cascade], splits[cascade + 1],
                        (*tiles)[cascade]));
                } else {
                    m_shadowFrame.views.push_back(lighting::BuildStableDirectionalCascade(
                        lightId, cascade, shadowCamera, direction, splits[cascade], splits[cascade + 1],
                        (*tiles)[cascade]));
                }
            }
            mainDirectional = false;
        } else if (light->GetLightType() == LightType::Spot) {
            if (firstView + 1u > lighting::MaxShadowViews)
                continue;
            // Spend the available atlas budget on the active light first. A
            // lone spot should not be capped at quarter resolution; the
            // allocator still halves this request when higher-priority views
            // already occupy the atlas.
            const std::array<uint32_t, 1> preferredSize{m_shadowFrame.atlasSize / 2u};
            const auto tiles =
                atlas.AllocateBatchWithFallback(preferredSize, std::max(m_shadowFrame.atlasSize / 32u, 16u), 4);
            if (!tiles)
                continue;
            m_shadowFrame.views.push_back(lighting::BuildSpotShadowView(
                lightId, position, direction, light->GetOuterSpotAngle(), light->GetRange(), (*tiles)[0]));
        } else {
            constexpr uint32_t faceCount = 6u;
            if (firstView + faceCount > lighting::MaxShadowViews)
                continue;
            // Six quarter-atlas faces fit in an otherwise empty atlas. Under
            // pressure AllocateBatchWithFallback reduces the whole cubemap
            // uniformly, preserving equal resolution across every seam.
            const uint32_t faceSize = m_shadowFrame.atlasSize / 4u;
            const std::array<uint32_t, faceCount> faceSizes{faceSize, faceSize, faceSize, faceSize, faceSize, faceSize};
            const auto tiles =
                atlas.AllocateBatchWithFallback(faceSizes, std::max(m_shadowFrame.atlasSize / 32u, 16u), 4);
            if (!tiles)
                continue;
            const auto type = light->GetLightType() == LightType::Area ? lighting::ShadowViewType::AreaFace
                                                                       : lighting::ShadowViewType::PointFace;
            const auto views = lighting::BuildPointShadowViews(lightId, position, light->GetRange(), *tiles, type);
            m_shadowFrame.views.insert(m_shadowFrame.views.end(), views.begin(), views.end());
        }

        const uint32_t viewCount = static_cast<uint32_t>(m_shadowFrame.views.size()) - firstView;
        if (viewCount > 0) {
            for (uint32_t index = firstView; index < firstView + viewCount; ++index) {
                m_shadowFrame.views[index].cullingMask = light->GetCullingMask();
                m_shadowFrame.views[index].filterRadiusTexels = light->GetShadowSoftness();
                m_shadowFrame.views[index].depthBiasTexels = light->GetShadowBias();
                m_shadowFrame.views[index].normalBiasTexels = light->GetShadowNormalBias();
            }
            m_shadowFrame.assignments.push_back({lightId, firstView, viewCount});
        }
    }
}

void SceneLightCollector::BuildShaderLightingUBO()
{
    // Build shader-compatible UBO from full UBO data
    // This structure exactly matches lit.frag layout

    // Light counts
    m_shaderLightingUBO.lightCounts = m_lightingUBO.lightCounts;

    // Ambient data (flat + hemisphere/gradient probe)
    m_shaderLightingUBO.ambientColor = m_lightingUBO.ambientSkyColor;
    m_shaderLightingUBO.ambientSkyColor = m_lightingUBO.ambientSkyColor;
    m_shaderLightingUBO.ambientEquatorColor = m_lightingUBO.ambientEquatorColor;
    m_shaderLightingUBO.ambientGroundColor = m_lightingUBO.ambientGroundColor;

    // Camera position
    m_shaderLightingUBO.cameraPos = m_lightingUBO.worldSpaceCameraPos;

    // Copy directional lights
    for (uint32_t i = 0; i < MAX_DIRECTIONAL_LIGHTS; ++i) {
        m_shaderLightingUBO.directionalLights[i] = m_lightingUBO.directionalLights[i];
    }

    // Copy point lights
    for (uint32_t i = 0; i < MAX_POINT_LIGHTS; ++i) {
        m_shaderLightingUBO.pointLights[i] = m_lightingUBO.pointLights[i];
    }

    // Copy spot lights
    for (uint32_t i = 0; i < MAX_SPOT_LIGHTS; ++i) {
        m_shaderLightingUBO.spotLights[i] = m_lightingUBO.spotLights[i];
    }
    for (uint32_t i = 0; i < MAX_AREA_LIGHTS; ++i) {
        m_shaderLightingUBO.areaLights[i] = m_lightingUBO.areaLights[i];
    }

    const auto applyAssignment = [&](uint64_t lightId, glm::uvec4 &metadata) {
        if (const auto *assignment = m_shadowFrame.Find(lightId)) {
            metadata.z = assignment->firstView;
            metadata.w = assignment->viewCount;
        } else {
            metadata.z = 0;
            metadata.w = 0;
        }
    };
    for (uint32_t i = 0; i < m_directionalLightCount; ++i)
        applyAssignment(m_directionalLightIds[i], m_shaderLightingUBO.directionalLights[i].metadata);
    for (uint32_t i = 0; i < m_pointLightCount; ++i)
        applyAssignment(m_pointLightIds[i], m_shaderLightingUBO.pointLights[i].metadata);
    for (uint32_t i = 0; i < m_spotLightCount; ++i)
        applyAssignment(m_spotLightIds[i], m_shaderLightingUBO.spotLights[i].metadata);
    for (uint32_t i = 0; i < m_areaLightCount; ++i)
        applyAssignment(m_areaLightIds[i], m_shaderLightingUBO.areaLights[i].metadata);

    auto applyCanonicalAssignments = [&](std::vector<CanonicalLightData> &lights) {
        for (auto &light : lights) {
            const uint64_t lightId = static_cast<uint64_t>(light.identityAndShadow.x) |
                                     (static_cast<uint64_t>(light.identityAndShadow.y) << 32u);
            if (const auto *assignment = m_shadowFrame.Find(lightId)) {
                light.identityAndShadow.z = assignment->firstView;
                light.identityAndShadow.w = assignment->viewCount;
            } else {
                light.identityAndShadow.z = 0;
                light.identityAndShadow.w = 0;
            }
        }
    };
    applyCanonicalAssignments(m_canonicalLightSnapshot.directionalLights);
    applyCanonicalAssignments(m_canonicalLightSnapshot.localLights);
    PublishCanonicalGeneration();

    const uint32_t shadowViewCount =
        std::min<uint32_t>(static_cast<uint32_t>(m_shadowFrame.views.size()), lighting::MaxShadowViews);
    m_shaderLightingUBO.shadowViewHeader =
        glm::uvec4(shadowViewCount, m_shadowFrame.atlasSize, static_cast<uint32_t>(m_canonicalLightSnapshot.generation),
                   static_cast<uint32_t>(m_canonicalLightSnapshot.generation >> 32u));
    for (uint32_t index = 0; index < lighting::MaxShadowViews; ++index) {
        auto &gpuView = m_shaderLightingUBO.shadowViews[index];
        if (index >= shadowViewCount) {
            gpuView = {};
            continue;
        }
        const auto &view = m_shadowFrame.views[index];
        gpuView.viewProjection = view.viewProjection;
        gpuView.atlasScaleOffset = view.atlas.ScaleOffset(m_shadowFrame.atlasSize);
        gpuView.depthTexel = glm::vec4(view.nearPlane, view.farPlane, view.worldUnitsPerTexel, view.filterRadiusTexels);
        gpuView.splitData = glm::vec4(view.splitNear, view.splitFar, 0.0f, 0.0f);
        gpuView.metadata = glm::uvec4(static_cast<uint32_t>(view.type), view.subView, 0u, 0u);
    }
}

} // namespace infernux
