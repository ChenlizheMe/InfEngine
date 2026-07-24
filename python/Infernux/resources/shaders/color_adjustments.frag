#version 450
@shader_id: Color Adjustments
@hidden

@import: Lib Utils

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

// ---- Alexa LogC (El 1000) — matches Unity URP's contrast space ----
// URP applies contrast in LogC space around ACEScc_MIDGRAY (0.4135884).
// Doing it in linear space instead crushes shadows and grays the image.
const float LOGC_A = 5.555556;
const float LOGC_B = 0.047996;
const float LOGC_C = 0.244161;
const float LOGC_D = 0.386036;
const float ACEScc_MIDGRAY = 0.4135884;

vec3 linearToLogC(vec3 x) {
    return LOGC_C * (log2(max(LOGC_A * x + LOGC_B, vec3(1e-6))) / log2(10.0)) + LOGC_D;
}

vec3 logCToLinear(vec3 x) {
    return (exp2((x - LOGC_D) / LOGC_C * log2(10.0)) - LOGC_B) / LOGC_A;
}

void main() {
    vec4 source = texture(_SourceTex, inUV);
    vec3 color = source.rgb;

    // Post-exposure (EV units, applied in linear space)
    color *= exp2(pc.postExposure);

    // Contrast in LogC space around ACEScc mid-gray (URP behaviour)
    float contrast = pc.contrast * 0.01 + 1.0;
    vec3 logc = linearToLogC(color);
    color = logCToLinear((logc - ACEScc_MIDGRAY) * contrast + ACEScc_MIDGRAY);
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
