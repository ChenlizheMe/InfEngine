#version 450
@shader_id: Chromatic Aberration
@hidden

// Chromatic Aberration post-process — RGB channel offset from center.
// Matches Unity URP Chromatic Aberration.
//
// Push constants:
//   [0] intensity — channel separation strength (0 = off, 1 = max)

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(push_constant) uniform PushConstants {
    float intensity;
    float _pad0;
    float _pad1;
    float _pad2;
} pc;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec2 center = inUV - 0.5;
    float dist = length(center);

    // Offset increases with distance from center (radial CA)
    vec2 offset = center * dist * pc.intensity * 0.02;

    float r = texture(_SourceTex, inUV - offset).r;
    vec4 centerSample = texture(_SourceTex, inUV);
    float g = centerSample.g;
    float b = texture(_SourceTex, inUV + offset).b;

    outColor = vec4(r, g, b, centerSample.a);
}
