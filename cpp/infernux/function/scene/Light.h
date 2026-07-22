#pragma once

#define GLM_FORCE_RADIANS
#ifndef GLM_FORCE_DEPTH_ZERO_TO_ONE
#define GLM_FORCE_DEPTH_ZERO_TO_ONE
#endif

#include "Component.h"
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
    Area = 3         ///< Area/Rectangle light (for baked lighting)
};

/**
 * @brief Shadow type for lights
 */
enum class LightShadows
{
    None = 0, ///< No shadows
    Hard = 1, ///< Hard edge shadows
    Soft = 2  ///< Soft shadows with PCF/VSM
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

constexpr uint32_t AllLightInfluenceDomains = static_cast<uint32_t>(LightInfluenceDomain::Geometry) |
                                              static_cast<uint32_t>(LightInfluenceDomain::Particles);

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
        m_intensity = intensity;
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
        m_range = range;
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
        m_spotAngle = angle;
    }

    /// @brief Get outer spot angle in degrees
    [[nodiscard]] float GetOuterSpotAngle() const
    {
        return m_outerSpotAngle;
    }
    void SetOuterSpotAngle(float angle)
    {
        m_outerSpotAngle = angle;
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
        m_shadowBias = bias * 0.1f;
    }

    [[nodiscard]] float GetShadowNormalBias() const
    {
        return m_shadowNormalBias;
    }
    void SetShadowNormalBias(float bias)
    {
        m_shadowNormalBias = bias;
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
    // Shadow mapping — light view/projection helpers
    // ========================================================================

    /// @brief Get the light's view matrix for shadow mapping.
    /// For directional lights, uses an orthographic view looking along the light direction.
    /// @param shadowCenter Center point for the shadow volume (typically camera position).
    ///        For directional lights, the ortho frustum is centered on this point.
    [[nodiscard]] glm::mat4 GetLightViewMatrix(const glm::vec3 &shadowCenter = glm::vec3(0.0f)) const;

    /// @brief Get the light's projection matrix for shadow mapping.
    /// Directional: orthographic, Spot: perspective, Point: not yet supported.
    /// @param shadowExtent Half-size of the orthographic shadow volume (default 20m)
    /// @param nearPlane Near clip plane for shadow frustum
    /// @param farPlane Far clip plane for shadow frustum
    [[nodiscard]] glm::mat4 GetLightProjectionMatrix(float shadowExtent = 20.0f, float nearPlane = 0.1f,
                                                     float farPlane = 100.0f) const;

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

    // Shadows
    LightShadows m_shadows = LightShadows::Hard;
    float m_shadowStrength = 1.0f;
    float m_shadowBias = 0.0f;
    float m_shadowNormalBias = 0.01f;

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
