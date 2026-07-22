#include "LightingData.h"
#include "Camera.h"
#include "GameObject.h"
#include "Light.h"
#include "Scene.h"
#include "SceneManager.h"
#include "SceneRenderer.h"
#include "Transform.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <core/log/InxLog.h>
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

        if (light->GetLightType() != LightType::Area)
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
            // Area lights are typically for baked lighting only
            break;
        }
    }

    // Sort point lights by importance
    SortPointLightsByImportance(cameraPosition);

    // Update light counts in UBO
    m_lightingUBO.lightCounts = glm::ivec4(static_cast<int>(m_directionalLightCount),
                                           static_cast<int>(m_pointLightCount), static_cast<int>(m_spotLightCount), 0);

    // Prepare simplified UBO
    PrepareSimpleLightingUBO();
}

void SceneLightCollector::Clear()
{
    m_canonicalLightSnapshot.Clear(m_canonicalLightSnapshot.generation + 1u);
    m_lightingUBO = LightingUBO{};
    m_simpleLightingUBO = SimpleLightingUBO{};
    m_directionalLightCount = 0;
    m_pointLightCount = 0;
    m_spotLightCount = 0;
    m_pointLightSortBuffer.clear();
    m_shadowEnabled = false;
    m_shadowCascadeCount = 0;
    m_shadowMapResolution = 0.0f;
    m_shadowCascadeSplits.fill(0.0f);
    m_shadowLightVPs.fill(glm::mat4(1.0f));

    // Set default ambient
    m_lightingUBO.ambientSkyColor = glm::vec4(0.2f, 0.2f, 0.3f, 0.5f);
    m_lightingUBO.ambientGroundColor = glm::vec4(0.1f, 0.1f, 0.1f, 0.3f);
    m_lightingUBO.ambientEquatorColor = glm::vec4(0.15f, 0.15f, 0.2f, 0.0f); // mode = 0 (flat)
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
    data.colorIntensity = glm::vec4(light->GetColor(), std::max(light->GetIntensity(), 0.0f));
    data.shadowAndInnerCos = glm::vec4(light->GetShadowStrength(), light->GetShadowBias(), light->GetShadowNormalBias(),
                                       spot ? std::cos(glm::radians(light->GetSpotAngle() * 0.5f)) : -1.0f);
    data.metadata = glm::uvec4(static_cast<uint32_t>(lightType), light->GetCullingMask(),
                               static_cast<uint32_t>(light->GetShadows()),
                               CanonicalLightAffectsScene | CanonicalLightAffectsParticles);
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
    // Store raw color in rgb, intensity in w (shader does color.rgb * color.w)
    data.color = glm::vec4(light->GetColor(), light->GetIntensity());

    // Shadow parameters: x=strength, y=bias, z=normalBias, w=shadowType (0=off, 1=hard, 2=soft)
    float shadowType = 0.0f;
    if (light->GetShadows() == LightShadows::Hard)
        shadowType = 1.0f;
    else if (light->GetShadows() == LightShadows::Soft)
        shadowType = 2.0f;
    data.shadowParams =
        glm::vec4(light->GetShadowStrength(), light->GetShadowBias(), light->GetShadowNormalBias(), shadowType);

    m_directionalLightCount++;
}

void SceneLightCollector::AddPointLight(const Light *light, const glm::vec3 &worldPosition)
{
    // Don't add to main buffer yet, add to sort buffer
    PointLightSortData sortData;
    sortData.data.position = glm::vec4(worldPosition, light->GetRange());
    // Store raw color in rgb, intensity in w (shader does color.rgb * color.w)
    sortData.data.color = glm::vec4(light->GetColor(), light->GetIntensity());
    // Store range in x for URP-style smooth attenuation (yz unused, kept for compatibility)
    sortData.data.attenuation = glm::vec4(light->GetRange(), 0.0f, 0.0f, 0.0f);
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
    // Store raw color in rgb, intensity in w (shader does color.rgb * color.w)
    data.color = glm::vec4(light->GetColor(), light->GetIntensity());

    // Calculate cos of angles for spot falloff
    float innerAngleRad = glm::radians(light->GetSpotAngle() * 0.5f);
    float outerAngleRad = glm::radians(light->GetOuterSpotAngle() * 0.5f);
    data.spotParams = glm::vec4(std::cos(innerAngleRad), std::cos(outerAngleRad), 0.0f, 0.0f);
    // Store range in x for URP-style smooth attenuation
    data.attenuation = glm::vec4(light->GetRange(), 0.0f, 0.0f, 0.0f);

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
    }
}

glm::vec3 SceneLightCollector::CalculateAttenuation(float range)
{
    // Unity-style attenuation:
    // attenuation = 1.0 / (constant + linear * d + quadratic * d^2)
    // For a light with range R, we want attenuation ≈ 0 at d = R

    // Simple quadratic falloff that reaches ~0 at range
    float constant = 1.0f;
    float linear = 2.0f / range;
    float quadratic = 1.0f / (range * range);

    return glm::vec3(constant, linear, quadratic);
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
    m_lightingUBO.ambientSkyColor = glm::vec4(color, intensity);
    m_lightingUBO.ambientEquatorColor.a = 0.0f; // Flat mode
}

void SceneLightCollector::SetAmbientGradient(const glm::vec3 &skyColor, const glm::vec3 &equatorColor,
                                             const glm::vec3 &groundColor)
{
    m_lightingUBO.ambientSkyColor = glm::vec4(skyColor, 1.0f);
    m_lightingUBO.ambientEquatorColor = glm::vec4(equatorColor, 1.0f); // Gradient mode
    m_lightingUBO.ambientGroundColor = glm::vec4(groundColor, 1.0f);
}

void SceneLightCollector::SetFog(bool enabled, const glm::vec3 &color, float density, float start, float end, int mode)
{
    m_lightingUBO.fogColor = glm::vec4(color, enabled ? 1.0f : 0.0f);
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

void SceneLightCollector::SetShadowData(const glm::mat4 &lightVP, float resolution)
{
    m_shadowLightVPs.fill(glm::mat4(1.0f));
    m_shadowCascadeSplits.fill(0.0f);
    m_shadowLightVPs[0] = lightVP;
    m_shadowCascadeCount = 1;
    m_shadowMapResolution = resolution;
    m_shadowEnabled = true;
}

void SceneLightCollector::ComputeShadowVP(Scene *scene, const glm::vec3 &cameraPos, float shadowMapResolution,
                                          const Camera *camera)
{
    if (!scene) {
        INXLOG_WARN("CSM: ComputeShadowVP skipped because scene is null");
        return;
    }

    if (!camera)
        camera = SceneRenderBridge::Instance().GetEditorCamera();

    if (!camera) {
        INXLOG_WARN("CSM: ComputeShadowVP found no camera; using fallback frustum parameters");
    }

    m_shadowEnabled = false;
    m_shadowCascadeCount = 0;
    m_shadowMapResolution = shadowMapResolution;
    m_shadowCascadeSplits.fill(0.0f);
    m_shadowLightVPs.fill(glm::mat4(1.0f));

    constexpr float kMaxShadowDistance = 160.0f;
    constexpr float kCascadeLambda = 0.72f; // log/uniform blend

    const auto &activeLights = SceneManager::Instance().GetActiveLights();
    for (Light *light : activeLights) {
        if (!light || !light->IsEnabled())
            continue;
        GameObject *obj = light->GetGameObject();
        if (!obj || !obj->IsActiveInHierarchy())
            continue;
        if (light->GetLightType() != LightType::Directional)
            continue;
        if (light->GetShadows() == LightShadows::None)
            continue;

        float nearClip = camera ? std::max(camera->GetNearClip(), 0.05f) : 0.1f;
        float farClip = camera ? camera->GetFarClip() : 100.0f;
        float shadowDist = std::min(farClip, kMaxShadowDistance);
        float aspect = camera ? std::max(camera->GetAspectRatio(), 0.1f) : 16.0f / 9.0f;
        float fovRad = glm::radians(camera ? camera->GetFieldOfView() : 60.0f);

        // Derive camera orientation AND position from the camera's transform.
        // The passed-in cameraPos is only used as fallback; the camera's own
        // world position must be authoritative so the frustum matches what the
        // camera actually sees (critical for multi-camera shadow isolation).
        glm::vec3 camOrigin = cameraPos;
        glm::vec3 camForward(0, 0, -1), camRight(1, 0, 0), camUp(0, 1, 0);
        if (camera && camera->GetGameObject() && camera->GetGameObject()->GetTransform()) {
            Transform *t = camera->GetGameObject()->GetTransform();
            camOrigin = t->GetWorldPosition();
            camForward = glm::normalize(t->GetWorldForward());
            camRight = glm::normalize(t->GetWorldRight());
            camUp = glm::normalize(t->GetWorldUp());
        }

        // Compute cascade split planes (practical split scheme)
        std::array<float, NUM_SHADOW_CASCADES + 1> planes{};
        planes[0] = nearClip;
        for (uint32_t ci = 1; ci <= NUM_SHADOW_CASCADES; ++ci) {
            float p = float(ci) / float(NUM_SHADOW_CASCADES);
            float logSplit = nearClip * std::pow(shadowDist / nearClip, p);
            float uniSplit = nearClip + (shadowDist - nearClip) * p;
            planes[ci] = glm::mix(uniSplit, logSplit, kCascadeLambda);
        }
        planes[NUM_SHADOW_CASCADES] = shadowDist;

        float cascadeRes = std::max(shadowMapResolution * 0.5f, 1.0f);

        for (uint32_t ci = 0; ci < NUM_SHADOW_CASCADES; ++ci) {
            float sliceNear = planes[ci];
            float sliceFar = planes[ci + 1];

            // Build 8 frustum corners for this slice
            float tanHalf = std::tan(fovRad * 0.5f);
            float nh = tanHalf * sliceNear, nw = nh * aspect;
            float fh = tanHalf * sliceFar, fw = fh * aspect;
            glm::vec3 nc = camOrigin + camForward * sliceNear;
            glm::vec3 fc = camOrigin + camForward * sliceFar;

            std::array<glm::vec3, 8> corners = {nc - camRight * nw - camUp * nh, nc + camRight * nw - camUp * nh,
                                                nc + camRight * nw + camUp * nh, nc - camRight * nw + camUp * nh,
                                                fc - camRight * fw - camUp * fh, fc + camRight * fw - camUp * fh,
                                                fc + camRight * fw + camUp * fh, fc - camRight * fw + camUp * fh};

            glm::vec3 center(0);
            for (auto &c : corners)
                center += c;
            center /= 8.0f;

            glm::mat4 lightView = light->GetLightViewMatrix(center);

            glm::vec3 mins(std::numeric_limits<float>::max());
            glm::vec3 maxs(std::numeric_limits<float>::lowest());
            for (auto &c : corners) {
                glm::vec3 lc = glm::vec3(lightView * glm::vec4(c, 1.0f));
                mins = glm::min(mins, lc);
                maxs = glm::max(maxs, lc);
            }

            // Snap to texel grid to prevent swimming
            glm::vec2 halfExt = glm::max((glm::vec2(maxs) - glm::vec2(mins)) * 0.5f, glm::vec2(0.5f));
            glm::vec2 texelSz = (halfExt * 2.0f) / cascadeRes;
            glm::vec2 cXY = glm::round(((glm::vec2(mins) + glm::vec2(maxs)) * 0.5f) / texelSz) * texelSz;
            // Expand by 1 texel to compensate for center-snap shifting the box boundary
            halfExt += texelSz;

            // Push the near plane generously behind the camera frustum slice so
            // that shadow casters upstream along the light direction (behind) are
            // still included.  The far side only needs a small margin.
            float nearPad = std::max(200.0f, (maxs.z - mins.z) * 2.0f);
            float farPad = std::max(20.0f, (maxs.z - mins.z) * 0.35f);
            // GLM orthoLH_ZO: visible range is eye_z ∈ [zNear, zFar].
            // View-space z is positive for objects in front of the light.
            float orthoNear = mins.z - nearPad;
            float orthoFar = maxs.z + farPad;
            // Guard against degenerate range (near ≈ far) → depth precision disaster
            if (orthoFar - orthoNear < 1.0f)
                orthoFar = orthoNear + 1.0f;
            glm::mat4 lightProj = glm::ortho(cXY.x - halfExt.x, cXY.x + halfExt.x, cXY.y - halfExt.y, cXY.y + halfExt.y,
                                             orthoNear, orthoFar);

            m_shadowLightVPs[ci] = lightProj * lightView;
            m_shadowCascadeSplits[ci] = sliceFar;
        }

        m_shadowCascadeCount = NUM_SHADOW_CASCADES;
        m_shadowEnabled = true;

        static bool loggedShadowLight = false;
        if (!loggedShadowLight) {
            glm::vec3 fwd = obj->GetTransform()->GetWorldForward();
            // INXLOG_INFO("CSM: '", obj->GetName(), "' forward=(", fwd.x, ",", fwd.y, ",", fwd.z,
            //             ") cascades=", m_shadowCascadeCount, " splits=[", m_shadowCascadeSplits[0], ", ",
            //             m_shadowCascadeSplits[1], ", ", m_shadowCascadeSplits[2], ", ", m_shadowCascadeSplits[3],
            //             "]", " nearClip=", nearClip, " shadowDist=", shadowDist);
            loggedShadowLight = true;
        }
        return; // Only first shadow-casting directional light
    }

    static int s_noShadowLightWarnCount = 0;
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

    // Shadow mapping data
    if (m_shadowEnabled && m_shadowCascadeCount > 0) {
        for (uint32_t i = 0; i < NUM_SHADOW_CASCADES; ++i)
            m_shaderLightingUBO.lightVP[i] = m_shadowLightVPs[i];
        m_shaderLightingUBO.shadowCascadeSplits = glm::vec4(m_shadowCascadeSplits[0], m_shadowCascadeSplits[1],
                                                            m_shadowCascadeSplits[2], m_shadowCascadeSplits[3]);
        float cascadeRes = m_shadowMapResolution * 0.5f;
        m_shaderLightingUBO.shadowMapParams =
            glm::vec4(m_shadowMapResolution, 1.0f, static_cast<float>(m_shadowCascadeCount), cascadeRes);
    } else {
        for (uint32_t i = 0; i < NUM_SHADOW_CASCADES; ++i) {
            m_shaderLightingUBO.lightVP[i] = glm::mat4(1.0f);
        }
        m_shaderLightingUBO.shadowMapParams = glm::vec4(0.0f);
        m_shaderLightingUBO.shadowCascadeSplits = glm::vec4(0.0f);
    }

    // Reset shadow state for next frame
    m_shadowEnabled = false;
}

} // namespace infernux
