#version 450
@shader_id: film_grain
@hidden

@import: lib/utils

// Film Grain post-process — adds cinematic noise overlay.
// Matches Unity URP Film Grain (simplified white noise type).
//
// Push constants:
//   [0] intensity  — grain strength (0 = off, 1 = full)
//   [1] response   — luminance-based response (0 = uniform, 1 = highlights only)
//   [2] time       — animated grain seed (frame time or counter)

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(push_constant) uniform PushConstants {
    float intensity;
    float response;
    float time;
    float _pad0;
} pc;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec4 color = texture(_SourceTex, inUV);

    // Generate noise based on UV + time
    vec2 texSize = vec2(textureSize(_SourceTex, 0));
    vec2 pixelCoord = inUV * texSize;
    float noise = hash21(pixelCoord + vec2(pc.time * 17.13, pc.time * 31.71));

    // Center noise around 0 (-0.5 to 0.5)
    noise = noise - 0.5;

    // Luminance-based response: reduce grain in bright areas
    float luma = luminance(color.rgb);
    float response = mix(1.0, 1.0 - sqrt(luma), pc.response);

    // Apply grain
    color.rgb += noise * pc.intensity * response;
    color.rgb = max(color.rgb, vec3(0.0));

    outColor = color;
}
