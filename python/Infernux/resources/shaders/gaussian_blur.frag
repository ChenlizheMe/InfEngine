#version 450
@shader_id: gaussian_blur
@hidden

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(push_constant) uniform GaussianBlurParams {
    float directionX;
    float directionY;
    float radius;
    float sigma;
} pc;

layout(location = 0) in vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec2 texel = 1.0 / vec2(textureSize(_SourceTex, 0));
    vec2 direction = vec2(pc.directionX, pc.directionY) * texel;
    int radius = clamp(int(pc.radius + 0.5), 1, 16);
    float sigma = max(pc.sigma, 0.25);
    float inverseTwoSigmaSquared = 0.5 / (sigma * sigma);

    vec4 sum = texture(_SourceTex, inUV);
    float weightSum = 1.0;
    for (int offset = 1; offset <= 16; ++offset) {
        if (offset > radius) {
            break;
        }
        float weight = exp(-float(offset * offset) * inverseTwoSigmaSquared);
        vec2 sampleOffset = direction * float(offset);
        sum += texture(_SourceTex, inUV - sampleOffset) * weight;
        sum += texture(_SourceTex, inUV + sampleOffset) * weight;
        weightSum += 2.0 * weight;
    }
    outColor = sum / weightSum;
}
