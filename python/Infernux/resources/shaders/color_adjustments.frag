#version 450
@shader_id: color_adjustments
@hidden

@import: lib/utils

// Color Adjustments post-process — Brightness, Contrast, Saturation, Hue Shift.
// Matches Unity URP Color Adjustments parameters.
//
// Push constants:
//   [0] postExposure   — exposure adjustment in EV (applied as 2^value)
//   [1] contrast       — contrast (-100 to 100, 0 = no change)
//   [2] saturation     — saturation (-100 to 100, 0 = no change)
//   [3] hueShift       — hue rotation in degrees (-180 to 180)

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(push_constant) uniform PushConstants {
    float postExposure;
    float contrast;
    float saturation;
    float hueShift;
} pc;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec4 source = texture(_SourceTex, inUV);
    vec3 color = source.rgb;

    // Post-exposure (EV units, applied in linear space)
    color *= exp2(pc.postExposure);

    // Contrast (centered around midpoint 0.4135884 in linear ≈ 0.18 in gamma)
    float contrast = pc.contrast * 0.01 + 1.0;
    color = (color - 0.4135884) * contrast + 0.4135884;
    color = max(color, vec3(0.0));

    // Saturation
    float luma = luminance(color);
    float sat = pc.saturation * 0.01 + 1.0;
    color = mix(vec3(luma), color, sat);
    color = max(color, vec3(0.0));

    // Hue shift
    if (abs(pc.hueShift) > 0.5) {
        vec3 hsv = rgbToHSV(color);
        hsv.x = fract(hsv.x + pc.hueShift / 360.0);
        color = hsvToRGB(hsv);
    }

    outColor = vec4(color, source.a);
}
