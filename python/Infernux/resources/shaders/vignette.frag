#version 450
@shader_id: vignette
@hidden

// Vignette post-process — darkens screen edges.
// Matches Unity URP Vignette parameters.
//
// Push constants:
//   [0] intensity  — vignette strength (0 = off, 1 = full)
//   [1] smoothness — falloff softness
//   [2] roundness  — shape (0 = square-ish, 1 = circular)
//   [3] rounded    — 1.0 = force circular, 0.0 = follow aspect ratio

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(push_constant) uniform PushConstants {
    float intensity;
    float smoothness;
    float roundness;
    float rounded;
} pc;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec4 color = texture(_SourceTex, inUV);

    vec2 center = inUV - 0.5;

    // Aspect correction via texture size
    vec2 texSize = vec2(textureSize(_SourceTex, 0));
    float aspect = texSize.x / texSize.y;

    if (pc.rounded > 0.5) {
        // Circular vignette
        center.x *= aspect;
    }

    // Distance from center with roundness control
    vec2 d = abs(center) * 2.0;
    float roundness = max(pc.roundness, 0.001);
    d = pow(d, vec2(roundness));
    float dist = pow(d.x + d.y, 1.0 / roundness);

    // Smooth falloff
    float vfactor = 1.0 - smoothstep(1.0 - pc.smoothness, 1.0, dist * pc.intensity);

    color.rgb *= vfactor;
    outColor = color;
}
