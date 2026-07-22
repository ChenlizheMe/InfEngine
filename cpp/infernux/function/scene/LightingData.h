#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
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
};

/**
 * @brief GPU-side point light data (std140 layout)
 */
struct alignas(16) PointLightData
{
    glm::vec4 position;    ///< xyz = world position, w = range
    glm::vec4 color;       ///< rgb = color * intensity, a = intensity
    glm::vec4 attenuation; ///< x = constant, y = linear, z = quadratic, w = unused
};

/**
 * @brief GPU-side spot light data (std140 layout)
 */
struct alignas(16) SpotLightData
{
    glm::vec4 position;    ///< xyz = world position, w = range
    glm::vec4 direction;   ///< xyz = direction (world space), w = unused
    glm::vec4 color;       ///< rgb = color * intensity, a = intensity
    glm::vec4 spotParams;  ///< x = cos(innerAngle), y = cos(outerAngle), z = unused, w = unused
    glm::vec4 attenuation; ///< x = constant, y = linear, z = quadratic, w = unused
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
 * @brief Number of shadow cascades (single shadow map for now, CSM later).
 */
constexpr uint32_t NUM_SHADOW_CASCADES = 4;

/**
 * @brief Shader-compatible Lighting UBO structure.
 *
 * This structure EXACTLY matches the layout in lit.frag shader.
 * Use this for GPU upload to ensure byte-perfect alignment.
 *
 * Layout (std140):
 *   offset 0:    ivec4 lightCounts
 *   offset 16:   vec4 ambientColor
 *   offset 32:   vec4 ambientSkyColor
 *   offset 48:   vec4 ambientEquatorColor
 *   offset 64:   vec4 ambientGroundColor
 *   offset 80:   vec4 cameraPos
 *   offset 96:   DirectionalLightData[4]  (48 bytes each = 192 bytes)
 *   offset 288:  PointLightData[64]       (48 bytes each = 3072 bytes)
 *   offset 3360: SpotLightData[32]        (80 bytes each = 2560 bytes)
 *   offset 5920: mat4 lightVP[4]          (64 bytes each = 256 bytes)
 *   offset 6176: vec4 shadowCascadeSplits (split distances for CSM)
 *   offset 6192: vec4 shadowMapParams     (resolution, enabled, etc.)
 *   Total: 6208 bytes
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

    // Shadow mapping data (appended after lights array)
    glm::mat4 lightVP[NUM_SHADOW_CASCADES]; ///< Light view-projection matrices per cascade
    glm::vec4 shadowCascadeSplits;          ///< x,y,z,w = cascade split distances (view-space Z)
    glm::vec4 shadowMapParams;              ///< x = resolution, y = enabled(1/0), z = numCascades, w = unused
};

// Compile-time size verification
static_assert(sizeof(DirectionalLightData) == 48, "DirectionalLightData must be 48 bytes");
static_assert(sizeof(PointLightData) == 48, "PointLightData must be 48 bytes");
static_assert(sizeof(SpotLightData) == 80, "SpotLightData must be 80 bytes");

// ============================================================================
// Canonical light snapshot (std430-compatible, uncapped)
// ============================================================================

enum class CanonicalLightType : uint32_t
{
    Directional = 0,
    Point = 1,
    Spot = 2,
};

enum CanonicalLightFlags : uint32_t
{
    CanonicalLightAffectsScene = 1u << 0u,
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
    glm::vec4 positionRange;     ///< xyz = world position, w = range (0 for directional)
    glm::vec4 directionOuterCos; ///< xyz = ray direction, w = cos(outer spot half-angle)
    glm::vec4 colorIntensity;    ///< xyz = linear color, w = intensity
    glm::vec4 shadowAndInnerCos; ///< x = strength, y = bias, z = normal bias, w = cos(inner half-angle)
    glm::uvec4 metadata;         ///< x = type, y = culling mask, z = shadow mode, w = flags
};

static_assert(alignof(CanonicalLightData) == 16, "CanonicalLightData alignment must match std430");
static_assert(sizeof(CanonicalLightData) == 80, "CanonicalLightData must be 80 bytes");
static_assert(offsetof(CanonicalLightData, metadata) == 64, "CanonicalLightData metadata offset must match GLSL");

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
     * @brief Set shadow mapping data for GPU upload.
     *
     * Must be called BEFORE BuildShaderLightingUBO() so the data is
     * included in the UBO memcpy.
     *
     * @param lightVP Light view-projection matrix (cascade 0)
     * @param resolution Shadow map resolution in pixels
     */
    void SetShadowData(const glm::mat4 &lightVP, float resolution);

    /**
     * @brief Compute cascaded shadow VP matrices from the first shadow-casting directional light.
     *
     * Finds the first enabled directional light with shadows, splits the camera
     * frustum into NUM_SHADOW_CASCADES slices, and fits a tight ortho projection
     * per cascade with texel snapping to prevent shadow swimming.
     *
     * Must be called AFTER CollectLights() and BEFORE BuildShaderLightingUBO().
     *
     * @param scene         Active scene to search for lights
     * @param cameraPos     Camera world position
     * @param shadowMapResolution  Shadow atlas resolution (e.g. 4096)
     * @param camera        Camera whose frustum drives cascade fitting (nullptr = active camera)
     */
    void ComputeShadowVP(Scene *scene, const glm::vec3 &cameraPos, float shadowMapResolution,
                         const Camera *camera = nullptr);

    /**
     * @brief Get the computed shadow light view-projection matrix.
     *
     * Valid after ComputeShadowVP() has been called for the current frame.
     */
    [[nodiscard]] const glm::mat4 &GetShadowLightVP(uint32_t cascade = 0) const
    {
        return m_shadowLightVPs[cascade < NUM_SHADOW_CASCADES ? cascade : 0];
    }

    [[nodiscard]] uint32_t GetShadowCascadeCount() const
    {
        return m_shadowCascadeCount;
    }
    [[nodiscard]] const std::array<float, NUM_SHADOW_CASCADES> &GetShadowCascadeSplits() const
    {
        return m_shadowCascadeSplits;
    }
    [[nodiscard]] float GetShadowMapResolution() const
    {
        return m_shadowMapResolution;
    }

    /**
     * @brief Check whether shadow mapping is enabled this frame.
     */
    [[nodiscard]] bool IsShadowEnabled() const
    {
        return m_shadowEnabled;
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
        return m_directionalLightCount + m_pointLightCount + m_spotLightCount;
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
     */
    void SetAmbientGradient(const glm::vec3 &skyColor, const glm::vec3 &equatorColor, const glm::vec3 &groundColor);

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

    void AddCanonicalLight(const Light *light, const glm::vec3 &worldPosition, const glm::vec3 &worldDirection);

    /**
     * @brief Sort point lights by importance (distance to camera, intensity).
     */
    void SortPointLightsByImportance(const glm::vec3 &cameraPosition);

    /**
     * @brief Calculate attenuation factors for a given range.
     */
    static glm::vec3 CalculateAttenuation(float range);

    /**
     * @brief Prepare the simplified UBO from the full UBO.
     */
    void PrepareSimpleLightingUBO();

    // Collected data
    LightingUBO m_lightingUBO{};
    SimpleLightingUBO m_simpleLightingUBO{};
    ShaderLightingUBO m_shaderLightingUBO{}; ///< Shader-compatible UBO for GPU upload
    CanonicalLightSnapshot m_canonicalLightSnapshot{};

    // Light counts
    uint32_t m_directionalLightCount = 0;
    uint32_t m_pointLightCount = 0;
    uint32_t m_spotLightCount = 0;

    // Shadow data (set before BuildShaderLightingUBO)
    std::array<glm::mat4, NUM_SHADOW_CASCADES> m_shadowLightVPs{glm::mat4(1.0f), glm::mat4(1.0f), glm::mat4(1.0f),
                                                                glm::mat4(1.0f)};
    std::array<float, NUM_SHADOW_CASCADES> m_shadowCascadeSplits{0.f, 0.f, 0.f, 0.f};
    uint32_t m_shadowCascadeCount = 0;
    float m_shadowMapResolution = 0.0f;
    bool m_shadowEnabled = false;

    // Temporary storage for sorting
    struct PointLightSortData
    {
        PointLightData data;
        float importance;
    };
    std::vector<PointLightSortData> m_pointLightSortBuffer;
};

} // namespace infernux
