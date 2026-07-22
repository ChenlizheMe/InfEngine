#version 450
@shader_id: digital_glitch
@hidden

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(push_constant) uniform DigitalGlitchParams {
    float intensity;
    float blockSize;
    float colorShift;
    float scanlineStrength;
} pc;

layout(location = 0) in vec2 inUV;
layout(location = 0) out vec4 outColor;

float hash11(float value) {
    return fract(sin(value * 127.1 + 311.7) * 43758.5453123);
}

void main() {
    vec2 textureSizePx = vec2(textureSize(_SourceTex, 0));
    vec2 texel = 1.0 / textureSizePx;
    float intensity = clamp(pc.intensity, 0.0, 1.0);
    float blockSize = clamp(pc.blockSize, 4.0, 64.0);
    float band = floor(inUV.y * textureSizePx.y / blockSize);
    float noise = hash11(band * 1.713 + floor(inUV.x * 7.0) * 0.193);
    float activeBand = step(1.0 - intensity * 0.52, noise);
    float displacementPx = (hash11(band * 3.17 + 19.0) - 0.5) * 42.0 * intensity * activeBand;
    vec2 displacedUV = clamp(inUV + vec2(displacementPx * texel.x, 0.0), vec2(0.0), vec2(1.0));

    float colorOffsetPx = (2.0 + 10.0 * intensity) * clamp(pc.colorShift, 0.0, 1.0);
    vec2 colorOffset = vec2(colorOffsetPx * texel.x, 0.0);
    vec4 centerSample = texture(_SourceTex, displacedUV);
    vec4 redSample = texture(_SourceTex, clamp(displacedUV + colorOffset, vec2(0.0), vec2(1.0)));
    vec4 blueSample = texture(_SourceTex, clamp(displacedUV - colorOffset, vec2(0.0), vec2(1.0)));

    vec3 color = vec3(redSample.r, centerSample.g, blueSample.b);
    vec3 glitchTint = mix(vec3(0.05, 0.95, 1.0), vec3(1.0, 0.08, 0.58), step(0.5, noise));
    color = mix(color, color * glitchTint * 1.65, activeBand * intensity * 0.42);

    float scanline = 0.5 + 0.5 * sin(inUV.y * textureSizePx.y * 3.14159265);
    color *= 1.0 - scanline * clamp(pc.scanlineStrength, 0.0, 1.0) * 0.42;
    float alpha = max(centerSample.a, max(redSample.a, blueSample.a));
    outColor = vec4(color, alpha);
}
