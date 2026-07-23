#pragma once

#define GLM_FORCE_RADIANS
#ifndef GLM_FORCE_DEPTH_ZERO_TO_ONE
#define GLM_FORCE_DEPTH_ZERO_TO_ONE
#endif

#include "Component.h"
#include <algorithm>
#include <glm/glm.hpp>
#include <glm/gtc/matrix_transform.hpp>

namespace infernux
{

/**
 * @brief Light type enumeration (matches Unity's LightType)
 */
enum class LightType
{
    Directional = 0, ///< Infinite distance light (sun-like)
    Point = 1,       ///< Omni-directional point light
    Spot = 2,        ///< Cone-shaped spotlight
    Area = 3         ///< Rectangle emitter evaluated at runtime
};

/**
 * @brief Shadow type for lights
 */
enum class LightShadows
{
    None = 0, ///< No shadows
    Hard = 1, ///< Hard edge shadows
    Soft = 2  ///< Filtered soft shadows
};

/**
 * @brief Light rendering mode
 */
enum class LightRenderMode
{
    Auto = 0,       ///< Automatic based on importance
    ForcePixel = 1, ///< Always per-pixel lighting
    ForceVertex = 2 ///< Always per-vertex lighting
};

enum class LightInfluenceDomain : uint32_t
{
    Geometry = 1u << 0u,
    Particles = 1u << 1u,
};

constexpr uint32_t AllLightInfluenceDomains =
    static_cast<uint32_t>(LightInfluenceDomain::Geometry) | static_cast<uint32_t>(LightInfluenceDomain::Particles);

/**
 * @brief Light component - Base class for all light sources.
 *
 * Follows Unity's Light component API for familiarity.
 * Attach to a GameObject to illuminate the scene.
 *
 * Usage:
 *   auto lightObj = scene->CreateGameObject("Directional Light");
 *   auto light = lightObj->AddComponent<Light>();
 *   light->SetLightType(LightType::Directional);
 *   light->SetColor(glm::vec3(1.0f, 0.95f, 0.9f));
 *   light->SetIntensity(1.0f);
 */
class Light : public Component
{
  public:
    Light() = default;
    ~Light() override;

    void OnEnable() override;
    void OnDisable() override;

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "Light";
    }

    // ========================================================================
    // Light Type
    // ========================================================================

    [[nodiscard]] LightType GetLightType() const
    {
        return m_lightType;
    }
    void SetLightType(LightType type)
    {
        m_lightType = type;
    }

    // ========================================================================
    // Color & Intensity (Unity-style)
    // ========================================================================

    /// @brief Get light color (linear RGB, not gamma)
    [[nodiscard]] glm::vec3 GetColor() const
    {
        return m_color;
    }
    void SetColor(const glm::vec3 &color)
    {
        m_color = color;
    }
    void SetColor(float r, float g, float b)
    {
        m_color = glm::vec3(r, g, b);
    }

    /// @brief Get light intensity (multiplier for color)
    [[nodiscard]] float GetIntensity() const
    {
        return m_intensity;
    }
    void SetIntensity(float intensity)
    {
        m_intensity = std::max(intensity, 0.0f);
    }

    /// @brief Get final light color (color * intensity)
    [[nodiscard]] glm::vec3 GetFinalColor() const
    {
        return m_color * m_intensity;
    }

    // ========================================================================
    // Range & Attenuation (Point/Spot lights)
    // ========================================================================

    /// @brief Get light range (for Point/Spot lights)
    [[nodiscard]] float GetRange() const
    {
        return m_range;
    }
    void SetRange(float range)
    {
        m_range = std::max(range, 0.001f);
    }

    // ========================================================================
    // Spot Light Settings
    // ========================================================================

    /// @brief Get spot angle in degrees (inner cone)
    [[nodiscard]] float GetSpotAngle() const
    {
        return m_spotAngle;
    }
    void SetSpotAngle(float angle)
    {
        m_spotAngle = glm::clamp(angle, 0.1f, std::max(m_outerSpotAngle, 0.1f));
    }

    /// @brief Get outer spot angle in degrees
    [[nodiscard]] float GetOuterSpotAngle() const
    {
        return m_outerSpotAngle;
    }
    void SetOuterSpotAngle(float angle)
    {
        m_outerSpotAngle = glm::clamp(angle, std::max(m_spotAngle, 0.1f), 179.0f);
    }

    [[nodiscard]] glm::vec2 GetAreaSize() const
    {
        return m_areaSize;
    }
    void SetAreaSize(const glm::vec2 &size)
    {
        m_areaSize = glm::max(size, glm::vec2(0.001f));
    }
    [[nodiscard]] bool GetAreaTwoSided() const
    {
        return m_areaTwoSided;
    }
    void SetAreaTwoSided(bool twoSided)
    {
        m_areaTwoSided = twoSided;
    }

    // ========================================================================
    // Shadows
    // ========================================================================

    [[nodiscard]] LightShadows GetShadows() const
    {
        return m_shadows;
    }
    void SetShadows(LightShadows shadows)
    {
        m_shadows = shadows;
    }

    [[nodiscard]] float GetShadowStrength() const
    {
        return m_shadowStrength;
    }
    void SetShadowStrength(float strength)
    {
        m_shadowStrength = glm::clamp(strength, 0.0f, 1.0f);
    }

    [[nodiscard]] float GetShadowBias() const
    {
        return m_shadowBias;
    }
    void SetShadowBias(float bias)
    {
        m_shadowBias = std::max(bias, 0.0f);
    }

    [[nodiscard]] float GetShadowNormalBias() const
    {
        return m_shadowNormalBias;
    }
    void SetShadowNormalBias(float bias)
    {
        m_shadowNormalBias = std::max(bias, 0.0f);
    }

    [[nodiscard]] float GetShadowSoftness() const
    {
        return m_shadowSoftness;
    }
    void SetShadowSoftness(float softness)
    {
        m_shadowSoftness = glm::clamp(softness, 0.25f, 8.0f);
    }

    // ========================================================================
    // Rendering
    // ========================================================================

    [[nodiscard]] LightRenderMode GetRenderMode() const
    {
        return m_renderMode;
    }
    void SetRenderMode(LightRenderMode mode)
    {
        m_renderMode = mode;
    }

    /// @brief Get culling mask (which layers this light affects)
    [[nodiscard]] uint32_t GetCullingMask() const
    {
        return m_cullingMask;
    }
    void SetCullingMask(uint32_t mask)
    {
        m_cullingMask = mask;
    }

    [[nodiscard]] uint32_t GetInfluenceDomains() const
    {
        return m_influenceDomains;
    }
    [[nodiscard]] bool GetAffectGeometry() const
    {
        return (m_influenceDomains & static_cast<uint32_t>(LightInfluenceDomain::Geometry)) != 0u;
    }
    void SetAffectGeometry(bool enabled)
    {
        SetInfluenceDomain(LightInfluenceDomain::Geometry, enabled);
    }
    [[nodiscard]] bool GetAffectParticles() const
    {
        return (m_influenceDomains & static_cast<uint32_t>(LightInfluenceDomain::Particles)) != 0u;
    }
    void SetAffectParticles(bool enabled)
    {
        SetInfluenceDomain(LightInfluenceDomain::Particles, enabled);
    }

    // ========================================================================
    // Baking
    // ========================================================================

    /// @brief Check if this light contributes to baked lightmaps
    [[nodiscard]] bool IsBaked() const
    {
        return m_baked;
    }
    void SetBaked(bool baked)
    {
        m_baked = baked;
    }

    // ========================================================================
    // Serialization
    // ========================================================================

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    static void ValidateSerializedDocument(const nlohmann::json &document);
    bool DeserializeDocument(const nlohmann::json &document) override;
    [[nodiscard]] std::unique_ptr<Component> Clone() const override;

  protected:
    // Light properties
    LightType m_lightType = LightType::Directional;
    glm::vec3 m_color = glm::vec3(1.0f, 1.0f, 1.0f);
    float m_intensity = 1.0f;

    // Range (Point/Spot)
    float m_range = 10.0f;

    // Spot light
    float m_spotAngle = 30.0f;      // Inner cone angle
    float m_outerSpotAngle = 45.0f; // Outer cone angle
    glm::vec2 m_areaSize{1.6f, 1.0f};
    bool m_areaTwoSided = false;

    // Shadows
    LightShadows m_shadows = LightShadows::Hard;
    float m_shadowStrength = 1.0f;
    // Bias values are expressed in shadow-map texels. Keeping both values in
    // the same resolution-independent unit makes a light behave consistently
    // when the pipeline changes its shadow atlas size.
    float m_shadowBias = 0.5f;
    float m_shadowNormalBias = 0.5f;
    float m_shadowSoftness = 1.5f;

    // Rendering
    LightRenderMode m_renderMode = LightRenderMode::Auto;
    uint32_t m_cullingMask = 0xFFFFFFFF; // All layers by default
    uint32_t m_influenceDomains = AllLightInfluenceDomains;

    // Baking
    bool m_baked = false;

  private:
    void SetInfluenceDomain(LightInfluenceDomain domain, bool enabled)
    {
        const uint32_t bit = static_cast<uint32_t>(domain);
        m_influenceDomains = enabled ? (m_influenceDomains | bit) : (m_influenceDomains & ~bit);
    }
};

} // namespace infernux
