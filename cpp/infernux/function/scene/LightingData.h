#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <function/renderer/lighting/ShadowFrame.h>
#include <glm/glm.hpp>
#include <vector>

namespace infernux
{

// Forward declarations
class Scene;
class Light;
class Camera;

// ============================================================================
// Light Data Structures for GPU (std140 layout compatible)
// ============================================================================

/**
 * @brief Maximum number of lights supported per frame.
 *
 * Matches typical Unity forward rendering limits.
 */
constexpr uint32_t MAX_DIRECTIONAL_LIGHTS = 4;
constexpr uint32_t MAX_POINT_LIGHTS = 64;
constexpr uint32_t MAX_SPOT_LIGHTS = 32;
constexpr uint32_t MAX_AREA_LIGHTS = 16;

/**
 * @brief GPU-side directional light data (std140 layout)
 *
 * Matches Unity's directional light representation.
 */
struct alignas(16) DirectionalLightData
{
    glm::vec4 direction;    ///< xyz = direction (world space), w = unused
    glm::vec4 color;        ///< rgb = color * intensity, a = intensity
    glm::vec4 shadowParams; ///< x = strength, y = bias, z = normalBias, w = enabled
    glm::uvec4 metadata;    ///< x = culling mask, y = influence domains, z = first shadow view, w = view count
};

/**
 * @brief GPU-side point light data (std140 layout)
 */
struct alignas(16) PointLightData
{
    glm::vec4 position;     ///< xyz = world position, w = range
    glm::vec4 color;        ///< rgb = color * intensity, a = intensity
    glm::vec4 attenuation;  ///< x = finite range, yzw = reserved
    glm::vec4 shadowParams; ///< x = strength, y = bias, z = normalBias, w = shadow mode
    glm::uvec4 metadata;    ///< x = culling mask, y = influence domains, z = first shadow view, w = view count
};

/**
 * @brief GPU-side spot light data (std140 layout)
 */
struct alignas(16) SpotLightData
{
    glm::vec4 position;     ///< xyz = world position, w = range
    glm::vec4 direction;    ///< xyz = direction (world space), w = unused
    glm::vec4 color;        ///< rgb = color * intensity, a = intensity
    glm::vec4 spotParams;   ///< x = cos(innerAngle), y = cos(outerAngle), z = unused, w = unused
    glm::vec4 attenuation;  ///< x = finite range, yzw = reserved
    glm::vec4 shadowParams; ///< x = strength, y = bias, z = normalBias, w = shadow mode
    glm::uvec4 metadata;    ///< x = culling mask, y = influence domains, z = first shadow view, w = view count
};

struct alignas(16) AreaLightData
{
    glm::vec4 positionRange;
    glm::vec4 direction;
    glm::vec4 rightWidth;
    glm::vec4 upHeight;
    glm::vec4 color;
    glm::vec4 shadowParams;
    glm::uvec4 metadata;
};

struct alignas(16) ShadowViewGpuData
{
    glm::mat4 viewProjection{1.0f};
    glm::vec4 atlasScaleOffset{};
    glm::vec4 depthTexel{};
    glm::vec4 splitData{};
    glm::uvec4 metadata{};
};

/**
 * @brief Lighting Uniform Buffer Object for GPU.
 *
 * This structure is uploaded to the GPU each frame and contains
 * all lighting information needed for forward rendering.
 *
 * Layout follows std140 for Vulkan/GLSL compatibility.
 * Designed to match Unity's lighting data structure.
 */
struct alignas(16) LightingUBO
{
    // Ambient lighting (approximation of indirect light)
    glm::vec4 ambientSkyColor;     ///< rgb = sky color, a = intensity
    glm::vec4 ambientGroundColor;  ///< rgb = ground color, a = intensity (for gradient)
    glm::vec4 ambientEquatorColor; ///< rgb = equator color, a = ambient mode (0=flat, 1=gradient, 2=skybox)

    // Light counts
    alignas(16) glm::ivec4 lightCounts; ///< x = directional, y = point, z = spot, w = unused

    // Camera data (needed for specular)
    glm::vec4 worldSpaceCameraPos; ///< xyz = camera position, w = unused

    // Directional lights (main light + additional)
    DirectionalLightData directionalLights[MAX_DIRECTIONAL_LIGHTS];

    // Point lights
    PointLightData pointLights[MAX_POINT_LIGHTS];

    // Spot lights
    SpotLightData spotLights[MAX_SPOT_LIGHTS];

    // Rectangle area lights
    AreaLightData areaLights[MAX_AREA_LIGHTS];

    // Fog settings (Unity-style)
    glm::vec4 fogColor;  ///< rgb = fog color, a = fog enabled
    glm::vec4 fogParams; ///< x = density, y = start, z = end, w = mode (0=linear, 1=exp, 2=exp2)

    // Global illumination settings
    glm::vec4 giParams; ///< x = bounceIntensity, y = indirectMultiplier, z,w = unused

    // Time (for animated effects)
    glm::vec4 time; ///< x = time, y = sin(time), z = cos(time), w = deltaTime
};

/**
 * @brief Simplified lighting data for basic forward rendering.
 *
 * Use this for simpler scenes or mobile targets.
 */
struct alignas(16) SimpleLightingUBO
{
    // Ambient
    glm::vec4 ambientColor; ///< rgb = ambient color, a = intensity

    // Main directional light (typically the sun)
    glm::vec4 mainLightDirection; ///< xyz = direction, w = unused
    glm::vec4 mainLightColor;     ///< rgb = color, a = intensity

    // Camera position
    glm::vec4 cameraPosition; ///< xyz = world position, w = unused

    // Point light count and data
    alignas(4) int pointLightCount;
    alignas(4) int _padding1;
    alignas(4) int _padding2;
    alignas(4) int _padding3;

    // Simple point light array (reduced from full version)
    PointLightData pointLights[16];
};

/**
 * @brief Shader-compatible Lighting UBO structure.
 *
 * This structure EXACTLY matches the layout in lit.frag shader.
 * Use this for GPU upload to ensure byte-perfect alignment.
 *
 * The matching GLSL declaration is generated from
 * resources/shaders/_templates/lighting_ubo.glsl. Compile-time
 * member and
 * record assertions below guard the shared std140 contract.
 */
struct alignas(16) ShaderLightingUBO
{
    // Light counts (must be first to match shader)
    alignas(16) glm::ivec4 lightCounts; ///< x = directional, y = point, z = spot, w = unused

    // Ambient and environment
    glm::vec4 ambientColor;        ///< xyz = flat ambient color, w = ambient intensity
    glm::vec4 ambientSkyColor;     ///< xyz = sky ambient color, w = intensity
    glm::vec4 ambientEquatorColor; ///< xyz = equator ambient color, w = mode (0=flat, 1=gradient, 2=skybox)
    glm::vec4 ambientGroundColor;  ///< xyz = ground ambient color, w = intensity
    glm::vec4 cameraPos;           ///< xyz = camera world position, w = unused

    // Lights arrays
    DirectionalLightData directionalLights[MAX_DIRECTIONAL_LIGHTS];
    PointLightData pointLights[MAX_POINT_LIGHTS];
    SpotLightData spotLights[MAX_SPOT_LIGHTS];
    AreaLightData areaLights[MAX_AREA_LIGHTS];

    glm::uvec4 shadowViewHeader; ///< x = view count, y = atlas size, zw = generation
    ShadowViewGpuData shadowViews[lighting::MaxShadowViews];
};

// Compile-time size verification
static_assert(sizeof(DirectionalLightData) == 64, "DirectionalLightData must be 64 bytes");
static_assert(sizeof(PointLightData) == 80, "PointLightData must be 80 bytes");
static_assert(sizeof(SpotLightData) == 112, "SpotLightData must be 112 bytes");
static_assert(sizeof(AreaLightData) == 112, "AreaLightData must be 112 bytes");
static_assert(sizeof(ShadowViewGpuData) == 128, "ShadowViewGpuData must be 128 bytes");

// ============================================================================
// Canonical light snapshot (std430-compatible, uncapped)
// ============================================================================

enum class CanonicalLightType : uint32_t
{
    Directional = 0,
    Point = 1,
    Spot = 2,
    Area = 3,
};

enum CanonicalLightFlags : uint32_t
{
    CanonicalLightAffectsGeometry = 1u << 0u,
    CanonicalLightAffectsParticles = 1u << 1u,
};

/**
 * Shared GPU light record for Forward+, Deferred, and lit particles.
 *
 * The old fixed arrays remain only as the
 * legacy Forward compatibility ABI.
 * This record is deliberately a dense std430-compatible structure so all new
 *
 * lighting paths consume the same immutable per-frame snapshot.
 */
struct alignas(16) CanonicalLightData
{
    glm::vec4 positionRange;      ///< xyz = world position, w = range (0 for directional)
    glm::vec4 directionOuterCos;  ///< xyz = ray direction, w = cos(outer spot half-angle)
    glm::vec4 colorIntensity;     ///< xyz = linear color, w = intensity
    glm::vec4 shadowAndInnerCos;  ///< x = strength, y = bias, z = normal bias, w = cos(inner half-angle)
    glm::vec4 areaRightWidth;     ///< xyz = rectangle right, w = width
    glm::vec4 areaUpHeight;       ///< xyz = rectangle up, w = height
    glm::uvec4 metadata;          ///< x = type, y = culling mask, z = shadow mode, w = flags
    glm::uvec4 identityAndShadow; ///< xy = stable light id, z = first shadow view, w = view count
};

static_assert(alignof(CanonicalLightData) == 16, "CanonicalLightData alignment must match std430");
static_assert(sizeof(CanonicalLightData) == 128, "CanonicalLightData must be 128 bytes");
static_assert(offsetof(CanonicalLightData, metadata) == 96, "CanonicalLightData metadata offset must match GLSL");

struct CanonicalLightSnapshot
{
    uint64_t generation = 0;
    std::vector<CanonicalLightData> directionalLights;
    std::vector<CanonicalLightData> localLights;

    void Clear(uint64_t nextGeneration)
    {
        generation = nextGeneration;
        directionalLights.clear();
        localLights.clear();
    }

    void Add(const CanonicalLightData &light)
    {
        if (light.metadata.x == static_cast<uint32_t>(CanonicalLightType::Directional))
            directionalLights.push_back(light);
        else
            localLights.push_back(light);
    }

    [[nodiscard]] size_t Size() const
    {
        return directionalLights.size() + localLights.size();
    }
};

// ============================================================================
// Scene Light Collector
// ============================================================================

/**
 * @brief Collects and prepares lighting data from a scene for GPU upload.
 *
 * This class traverses the scene hierarchy, finds all enabled Light components,
 * and packages their data into GPU-friendly structures.
 *
 * Usage:
 *   SceneLightCollector collector;
 *   collector.CollectLights(scene);
 *
 *   // Get data for GPU upload
 *   const LightingUBO& lightingData = collector.GetLightingUBO();
 *   memcpy(gpuBuffer, &lightingData, sizeof(LightingUBO));
 *
 * Features:
 * - Automatic light sorting by importance (distance, intensity)
 * - Light culling against camera frustum (optional)
 * - Support for light layers/culling masks
 * - Thread-safe collection (single writer, multiple readers)
 */
class SceneLightCollector
{
  public:
    SceneLightCollector() = default;
    ~SceneLightCollector() = default;

    // Non-copyable
    SceneLightCollector(const SceneLightCollector &) = delete;
    SceneLightCollector &operator=(const SceneLightCollector &) = delete;

    // ========================================================================
    // Collection
    // ========================================================================

    /**
     * @brief Collect all lights from a scene.
     * @param scene The scene to collect lights from
     * @param cameraPosition Camera world position for light sorting
     */
    void CollectLights(Scene *scene, const glm::vec3 &cameraPosition = glm::vec3(0.0f));

    /**
     * @brief Clear all collected light data.
     */
    void Clear();

    // ========================================================================
    // Accessors
    // ========================================================================

    /**
     * @brief Get the full lighting UBO for GPU upload.
     */
    [[nodiscard]] const LightingUBO &GetLightingUBO() const
    {
        return m_lightingUBO;
    }

    /**
     * @brief Get simplified lighting UBO (for mobile/simple rendering).
     */
    [[nodiscard]] const SimpleLightingUBO &GetSimpleLightingUBO() const
    {
        return m_simpleLightingUBO;
    }

    /**
     * @brief Get shader-compatible lighting UBO for GPU upload.
     *
     * This returns the UBO that exactly matches the shader layout.
     * Call BuildShaderLightingUBO() first to ensure data is current.
     */
    [[nodiscard]] const ShaderLightingUBO &GetShaderLightingUBO() const
    {
        return m_shaderLightingUBO;
    }

    [[nodiscard]] const CanonicalLightSnapshot &GetCanonicalLightSnapshot() const
    {
        return m_canonicalLightSnapshot;
    }

    /**
     * @brief Build shader-compatible UBO from collected light data.
     *
     * Call this after CollectLights() and before uploading to GPU.
     */
    void BuildShaderLightingUBO();

    /**
     * @brief Build all shadow views for the camera and active lights.
     *
     * Directional lights use
     * stable practical cascades. Spot, point and area
     * lights contribute one or more local views to the same
     * atlas.
     * Must be called AFTER CollectLights() and BEFORE BuildShaderLightingUBO().
     *
     * @param scene         Active scene to search for lights
     * @param cameraPos     Camera world position
     * @param shadowMapResolution  Shadow atlas resolution (e.g. 4096)
     * @param camera        Camera whose frustum drives cascade fitting (nullptr = active camera)
     * @param
     * visibleDepthRange Camera-space depth occupied by this camera's visible renderers
     */
    void ComputeShadowVP(Scene *scene, const glm::vec3 &cameraPos, float shadowMapResolution,
                         const Camera *camera = nullptr, const lighting::ShadowDepthRange &visibleDepthRange = {});

    [[nodiscard]] const lighting::ShadowFrame &GetShadowFrame() const noexcept
    {
        return m_shadowFrame;
    }

    /**
     * @brief Check whether shadow mapping is enabled this frame.
     */
    [[nodiscard]] bool IsShadowEnabled() const
    {
        return !m_shadowFrame.views.empty();
    }

    /**
     * @brief Get number of directional lights collected.
     */
    [[nodiscard]] uint32_t GetDirectionalLightCount() const
    {
        return m_directionalLightCount;
    }

    /**
     * @brief Get number of point lights collected.
     */
    [[nodiscard]] uint32_t GetPointLightCount() const
    {
        return m_pointLightCount;
    }

    /**
     * @brief Get number of spot lights collected.
     */
    [[nodiscard]] uint32_t GetSpotLightCount() const
    {
        return m_spotLightCount;
    }

    /**
     * @brief Get total number of lights collected.
     */
    [[nodiscard]] uint32_t GetTotalLightCount() const
    {
        return m_directionalLightCount + m_pointLightCount + m_spotLightCount + m_areaLightCount;
    }

    // ========================================================================
    // Settings
    // ========================================================================

    /**
     * @brief Set ambient color (flat ambient mode).
     */
    void SetAmbientColor(const glm::vec3 &color, float intensity = 1.0f);

    /**
     * @brief Set gradient ambient (sky/equator/ground).
     *
     * Colors are authored sRGB; @p intensity is a linear-space multiplier
     * applied after the sRGB -> linear conversion.
     */
    void SetAmbientGradient(const glm::vec3 &skyColor, const glm::vec3 &equatorColor, const glm::vec3 &groundColor,
                            float intensity = 1.0f);

    /**
     * @brief Copy already-linear ambient values verbatim (no sRGB conversion).
     *
     * Used to forward ambient state from another collector's built UBO
     * (alpha channels carry intensity/mode flags and are preserved).
     */
    void SetAmbientLinear(const glm::vec4 &skyColor, const glm::vec4 &equatorColor, const glm::vec4 &groundColor);

    /**
     * @brief Set fog parameters.
     */
    void SetFog(bool enabled, const glm::vec3 &color, float density, float start, float end, int mode = 0);

    /**
     * @brief Update time values for animated effects.
     */
    void UpdateTime(float time, float deltaTime);

    /**
     * @brief Set camera position for per-frame updates.
     */
    void SetCameraPosition(const glm::vec3 &position);

  private:
    void PublishCanonicalGeneration();

    /**
     * @brief Add a directional light to the collection.
     */
    void AddDirectionalLight(const Light *light);

    /**
     * @brief Add a point light to the collection.
     */
    void AddPointLight(const Light *light, const glm::vec3 &worldPosition);

    /**
     * @brief Add a spot light to the collection.
     */
    void AddSpotLight(const Light *light, const glm::vec3 &worldPosition, const glm::vec3 &worldDirection);

    void AddAreaLight(const Light *light, const glm::vec3 &worldPosition, const glm::vec3 &worldDirection);

    void AddCanonicalLight(const Light *light, const glm::vec3 &worldPosition, const glm::vec3 &worldDirection);

    /**
     * @brief Sort point lights by importance (distance to camera, intensity).
     */
    void SortPointLightsByImportance(const glm::vec3 &cameraPosition);

    /**
     * @brief Prepare the simplified UBO from the full UBO.
     */
    void PrepareSimpleLightingUBO();

    // Collected data
    LightingUBO m_lightingUBO{};
    SimpleLightingUBO m_simpleLightingUBO{};
    ShaderLightingUBO m_shaderLightingUBO{}; ///< Shader-compatible UBO for GPU upload
    CanonicalLightSnapshot m_canonicalLightSnapshot{};
    std::vector<CanonicalLightData> m_lastCanonicalDirectionalLights;
    std::vector<CanonicalLightData> m_lastCanonicalLocalLights;
    uint64_t m_canonicalGeneration = 0;
    bool m_hasCanonicalPublication = false;

    // Light counts
    uint32_t m_directionalLightCount = 0;
    uint32_t m_pointLightCount = 0;
    uint32_t m_spotLightCount = 0;
    uint32_t m_areaLightCount = 0;
    std::array<uint64_t, MAX_DIRECTIONAL_LIGHTS> m_directionalLightIds{};
    std::array<uint64_t, MAX_POINT_LIGHTS> m_pointLightIds{};
    std::array<uint64_t, MAX_SPOT_LIGHTS> m_spotLightIds{};
    std::array<uint64_t, MAX_AREA_LIGHTS> m_areaLightIds{};

    lighting::ShadowFrame m_shadowFrame;

    // Temporary storage for sorting
    struct PointLightSortData
    {
        PointLightData data;
        float importance;
        uint64_t lightId = 0;
    };
    std::vector<PointLightSortData> m_pointLightSortBuffer;
};

} // namespace infernux
