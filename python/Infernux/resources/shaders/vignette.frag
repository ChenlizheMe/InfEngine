#version 450
@shader_id: Vignette
@hidden

@import: Lib Color

// Vignette post-process — darkens screen edges.
// Matches Unity URP's vignette math (ApplyVignette in Common.hlsl):
// the falloff is a smooth quadratic dome, and the edges fade toward a
// configurable vignette color instead of hard black multiplication.
//
// Push constants:
//   [0] intensity  — vignette strength (0 = off, 1 = full)
//   [1] smoothness — falloff softness
//   [2] roundness  — shape (1 = circular, lower = squared)
//   [3] rounded    — 1.0 = force circular, 0.0 = follow aspect ratio
//   [4..6] colorR/G/B — vignette color (usually black)

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(push_constant) uniform PushConstants {
    float intensity;
    float smoothness;
    float roundness;
    float rounded;
    float colorR;
    float colorG;
    float colorB;
} pc;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec4 color = texture(_SourceTex, inUV);

    vec2 texSize = vec2(textureSize(_SourceTex, 0));
    float aspect = texSize.x / texSize.y;

    // URP parameter mapping: intensity*3, smoothness*5, and roundness
    // remapped so 1 = circle and 0 = rounded rectangle.
    vec2 dist = abs(inUV - vec2(0.5)) * (pc.intensity * 3.0);
    if (pc.rounded > 0.5) {
        dist.x *= aspect;
    }
    float roundness = (1.0 - pc.roundness) * 6.0 + pc.roundness;
    dist = pow(clamp(dist, 0.0, 1.0), vec2(roundness));
    float vfactor = pow(clamp(1.0 - dot(dist, dist), 0.0, 1.0), pc.smoothness * 5.0);

    // Authored color is sRGB; this pass runs in linear HDR space.
    vec3 vignetteColor = sRGBToLinear(vec3(pc.colorR, pc.colorG, pc.colorB));
    color.rgb *= mix(vignetteColor, vec3(1.0), vfactor);
    outColor = color;
}
