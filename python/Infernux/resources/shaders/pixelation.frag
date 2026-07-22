#version 450
@shader_id: pixelation
@hidden

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(push_constant) uniform PixelationParams {
    float pixelSize;
    float pixelAspect;
    float gridOffsetX;
    float gridOffsetY;
    float intensity;
    float samplingMode;
    float preserveAlphaCoverage;
} pc;

layout(location = 0) in vec2 inUV;
layout(location = 0) out vec4 outColor;

vec2 clampToTexel(vec2 pixelPosition, vec2 textureSizePx) {
    return clamp(pixelPosition, vec2(0.5), textureSizePx - vec2(0.5));
}

vec4 sampleAtPixel(vec2 pixelPosition, vec2 textureSizePx) {
    return texture(_SourceTex, clampToTexel(pixelPosition, textureSizePx) / textureSizePx);
}

void accumulateSample(
    vec4 sampleColor,
    inout vec3 premultipliedColor,
    inout float alphaSum,
    inout float maxAlpha,
    inout float sampleCount
) {
    premultipliedColor += sampleColor.rgb * sampleColor.a;
    alphaSum += sampleColor.a;
    maxAlpha = max(maxAlpha, sampleColor.a);
    sampleCount += 1.0;
}

void main() {
    vec2 textureSizePx = vec2(textureSize(_SourceTex, 0));
    float cellHeight = clamp(round(pc.pixelSize), 1.0, 256.0);
    vec2 cellSize = vec2(
        max(1.0, round(cellHeight * clamp(pc.pixelAspect, 0.25, 4.0))),
        cellHeight
    );
    vec2 gridOffset = vec2(pc.gridOffsetX, pc.gridOffsetY) * cellSize;
    vec2 sourcePixel = inUV * textureSizePx;
    vec2 cellMin = floor((sourcePixel - gridOffset) / cellSize) * cellSize + gridOffset;
    vec2 cellCenter = cellMin + cellSize * 0.5;

    vec4 original = texture(_SourceTex, inUV);
    int samplingMode = int(clamp(round(pc.samplingMode), 0.0, 2.0));
    vec4 pixelated;

    if (samplingMode == 0) {
        pixelated = sampleAtPixel(cellCenter, textureSizePx);
    } else {
        vec3 premultipliedColor = vec3(0.0);
        float alphaSum = 0.0;
        float maxAlpha = 0.0;
        float sampleCount = 0.0;

        if (samplingMode == 1) {
            const vec2 OFFSETS[4] = vec2[](
                vec2(-0.25, -0.25), vec2(0.25, -0.25),
                vec2(-0.25, 0.25), vec2(0.25, 0.25)
            );
            for (int index = 0; index < 4; ++index) {
                accumulateSample(
                    sampleAtPixel(cellCenter + OFFSETS[index] * cellSize, textureSizePx),
                    premultipliedColor,
                    alphaSum,
                    maxAlpha,
                    sampleCount
                );
            }
        } else {
            for (int y = -1; y <= 1; ++y) {
                for (int x = -1; x <= 1; ++x) {
                    vec2 offset = vec2(float(x), float(y)) / 3.0;
                    accumulateSample(
                        sampleAtPixel(cellCenter + offset * cellSize, textureSizePx),
                        premultipliedColor,
                        alphaSum,
                        maxAlpha,
                        sampleCount
                    );
                }
            }
        }

        float reconstructedAlpha = alphaSum / max(sampleCount, 1.0);
        if (pc.preserveAlphaCoverage > 0.5) {
            reconstructedAlpha = maxAlpha;
        }
        vec3 reconstructedColor = alphaSum > 1e-6
            ? premultipliedColor / alphaSum
            : vec3(0.0);
        pixelated = vec4(reconstructedColor, reconstructedAlpha);
    }

    outColor = mix(original, pixelated, clamp(pc.intensity, 0.0, 1.0));
}
