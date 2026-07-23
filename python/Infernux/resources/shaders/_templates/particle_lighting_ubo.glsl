#define MAX_DIRECTIONAL_LIGHTS 4
#define MAX_POINT_LIGHTS 64
#define MAX_SPOT_LIGHTS 32
#define INX_PARTICLE_FORWARD_PLUS 1

struct DirectionalLightData {
    vec4 direction;
    vec4 color;
    vec4 shadowParams;
};

struct PointLightData {
    vec4 position;
    vec4 color;
    vec4 attenuation;
};

struct SpotLightData {
    vec4 position;
    vec4 direction;
    vec4 color;
    vec4 spotParams;
    vec4 attenuation;
};

// Read-only view lighting shared with the regular material renderer. Particle
// set 0 remains dedicated to instances and material resources.
layout(std140, set = 1, binding = 4) uniform ParticleLightingUBO {
    ivec4 lightCounts;
    vec4 ambientColor;
    vec4 ambientSkyColor;
    vec4 ambientEquatorColor;
    vec4 ambientGroundColor;
    vec4 cameraPos;
    DirectionalLightData directionalLights[MAX_DIRECTIONAL_LIGHTS];
    PointLightData pointLights[MAX_POINT_LIGHTS];
    SpotLightData spotLights[MAX_SPOT_LIGHTS];
    mat4 lightVP[4];
    vec4 shadowCascadeSplits;
    vec4 shadowMapParams;
} lighting;

layout(set = 1, binding = 0) uniform sampler2D shadowMap;

bool inxParticleReceivesShadows();
