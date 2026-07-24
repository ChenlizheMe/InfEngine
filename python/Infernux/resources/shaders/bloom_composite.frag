#version 450
@shader_id: Bloom Composite
@hidden

// Bloom composite pass — additive blend bloom texture onto scene color.
// Aligned with Unity URP's Bloom compositing.
//
// Push constants layout:
//   [0] intensity   — bloom intensity multiplier
//   [1] tintR       — bloom tint color R
//   [2] tintG       — bloom tint color G
//   [3] tintB       — bloom tint color B

layout(set = 0, binding = 0) uniform sampler2D _BloomTex;   // final bloom result
layout(set = 0, binding = 1) uniform sampler2D _SceneColor;  // original scene color

layout(push_constant) uniform PushConstants {
    float intensity;
    float tintR;
    float tintG;
    float tintB;
} pc;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec3 bloom = texture(_BloomTex, inUV).rgb;
    vec4 sceneSample = texture(_SceneColor, inUV);
    vec3 scene = sceneSample.rgb;

    // Apply tint and intensity
    vec3 tint = vec3(pc.tintR, pc.tintG, pc.tintB);
    bloom *= tint * pc.intensity;

    // Additive blend
    vec3 result = scene + bloom;

    outColor = vec4(result, sceneSample.a);
}
