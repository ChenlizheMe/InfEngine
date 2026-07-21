#version 450
@shader_id: sharpen_cas
@hidden

// Contrast Adaptive Sharpening (CAS) — AMD FidelityFX inspired.
// Enhances local contrast without visible halos.
//
// Push constants:
//   [0] intensity — sharpening strength (0 = off, 1 = maximum)

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(push_constant) uniform PushConstants {
    float intensity;
} pc;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec2 texelSize = 1.0 / vec2(textureSize(_SourceTex, 0));

    // Sample 3x3 neighborhood (cross pattern for efficiency)
    vec4 centerSample = texture(_SourceTex, inUV);
    vec3 center = centerSample.rgb;
    vec3 top    = texture(_SourceTex, inUV + vec2( 0.0, -texelSize.y)).rgb;
    vec3 bottom = texture(_SourceTex, inUV + vec2( 0.0,  texelSize.y)).rgb;
    vec3 left   = texture(_SourceTex, inUV + vec2(-texelSize.x,  0.0)).rgb;
    vec3 right  = texture(_SourceTex, inUV + vec2( texelSize.x,  0.0)).rgb;

    // Find min/max of the cross neighborhood
    vec3 minColor = min(center, min(min(top, bottom), min(left, right)));
    vec3 maxColor = max(center, max(max(top, bottom), max(left, right)));

    // CAS: compute adaptive sharpening weight
    // Higher weight where contrast is low; lower where it's already high
    vec3 reciprocalRange = 1.0 / (maxColor - minColor + 0.001);
    vec3 w = clamp(min(minColor, 1.0 - maxColor) * reciprocalRange, 0.0, 1.0);
    w = w * w; // square for softer falloff

    float sharpness = mix(0.0, -0.5, pc.intensity);

    // Apply sharpening: center + (center - average) * weight * intensity
    vec3 average = (top + bottom + left + right) * 0.25;
    vec3 sharpened = center + (center - average) * w * sharpness * -4.0;

    outColor = vec4(clamp(sharpened, 0.0, 1.0), centerSample.a);
}
