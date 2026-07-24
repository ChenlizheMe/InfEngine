#version 450
@shader_id: Bloom Prefilter
@hidden

// Bloom prefilter pass — aligned with Unity's Bloom
// Applies a soft knee threshold curve to extract bright pixels.
//
// Push constants layout (indexed by slot):
//   [0] threshold   — luminance threshold
//   [1] knee        — softness of the threshold curve (0 = hard, 1 = full soft)
//   [2] clamp_max   — maximum brightness clamp value
//   [3] (unused)

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(push_constant) uniform PushConstants {
    float threshold;
    float knee;
    float clampMax;
    float _pad0;
} pc;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

// Unity's soft knee threshold curve:
//   knee = threshold * pc.knee
//   x = brightness - threshold + knee
//   result = clamp(x, 0, 2*knee)^2 / (4*knee + 1e-5)
vec3 QuadraticThreshold(vec3 color, float threshold, float knee) {
    float br = max(color.r, max(color.g, color.b));
    float rq = clamp(br - threshold + knee, 0.0, 2.0 * knee);
    rq = rq * rq / (4.0 * knee + 1e-5);
    float contrib = max(rq, br - threshold) / max(br, 1e-5);
    return color * max(contrib, 0.0);
}

void main() {
    vec4 src = texture(_SourceTex, inUV);

    // Clamp maximum brightness to prevent fireflies
    src.rgb = min(src.rgb, vec3(pc.clampMax));

    // Apply soft threshold
    float softKnee = pc.threshold * pc.knee;
    src.rgb = QuadraticThreshold(src.rgb, pc.threshold, softKnee);

    outColor = vec4(src.rgb, 1.0);
}
