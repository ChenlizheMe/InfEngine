// ============================================================================
// lighting_ubo.glsl — LightingUBO and shadow map sampler for lit shaders
//
// Matches ShaderLightingUBO layout in C++ (InfLight.h / InfRenderer).
// ============================================================================

#define MAX_DIRECTIONAL_LIGHTS 4
#define MAX_POINT_LIGHTS 64
#define MAX_SPOT_LIGHTS 32
#define MAX_AREA_LIGHTS 16
#define MAX_SHADOW_VIEWS 64

struct DirectionalLightData {
    vec4 direction;      // xyz = direction, w = unused
    vec4 color;          // xyz = color, w = intensity
    vec4 shadowParams;   // x = strength, y = bias, z = normal bias, w = type (0=off, 1=hard, 2=soft PCF)
    uvec4 metadata;      // x = layer mask, y = influence domains, z = first shadow view, w = view count
};

struct PointLightData {
    vec4 position;       // xyz = position, w = range
    vec4 color;          // xyz = color, w = intensity
    vec4 attenuation;    // x = constant, y = linear, z = quadratic, w = unused
    vec4 shadowParams;
    uvec4 metadata;
};

struct SpotLightData {
    vec4 position;       // xyz = position, w = range
    vec4 direction;      // xyz = direction, w = unused
    vec4 color;          // xyz = color, w = intensity
    vec4 spotParams;     // x = inner angle cos, y = outer angle cos, zw = unused
    vec4 attenuation;    // x = constant, y = linear, z = quadratic, w = unused
    vec4 shadowParams;
    uvec4 metadata;
};

struct AreaLightData {
    vec4 positionRange;
    vec4 direction;
    vec4 rightWidth;
    vec4 upHeight;
    vec4 color;
    vec4 shadowParams;
    uvec4 metadata;
};

struct ShadowViewData {
    mat4 viewProjection;
    vec4 atlasScaleOffset;
    vec4 depthTexel;
    vec4 splitData;
    uvec4 metadata;
};

// Lighting belongs to the camera/view, not to a material. Keeping it in set 1
// prevents Scene, Game and future camera graphs from overwriting one another.
layout(std140, set = 1, binding = 4) uniform LightingUBO {
    ivec4 lightCounts;   // x = directional, y = point, z = spot, w = unused
    vec4 ambientColor;   // xyz = flat/legacy ambient color, w = ambient intensity
    vec4 ambientSkyColor;     // xyz = sky ambient color, w = intensity
    vec4 ambientEquatorColor; // xyz = equator color, w = ambient mode
    vec4 ambientGroundColor;  // xyz = ground ambient color, w = intensity
    vec4 cameraPos;      // xyz = camera world position, w = unused
    DirectionalLightData directionalLights[MAX_DIRECTIONAL_LIGHTS];
    PointLightData pointLights[MAX_POINT_LIGHTS];
    SpotLightData spotLights[MAX_SPOT_LIGHTS];
    AreaLightData areaLights[MAX_AREA_LIGHTS];
    uvec4 shadowViewHeader;   // x = view count, y = atlas size, zw = generation
    ShadowViewData shadowViews[MAX_SHADOW_VIEWS];
} lighting;

// Shadow map sampler (per-view descriptor set 1)
layout(set = 1, binding = 0) uniform sampler2D shadowMap;
