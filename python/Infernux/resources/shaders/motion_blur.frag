#version 450

ShaderInfo {
    Name "Motion Blur"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D _SourceTex
        Texture2D _MotionTex
        Texture2D _DepthTex
    }
    PushConstants pc {
        Float intensity
        Float maxBlurPixels
        Float depthRejection
        Float _pad0
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

// Motion is currentUV - previousUV. Twelve fixed taps keep topology stable
// while parameters update through the RenderEffect parameter block.
void main() {
    vec4 centerColor = texture(_SourceTex, inUV);
    vec2 textureSizePixels = vec2(textureSize(_SourceTex, 0));
    vec2 motion = texture(_MotionTex, inUV).xy * pc.intensity;
    float motionPixels = length(motion * textureSizePixels);

    if (motionPixels < 0.25 || pc.maxBlurPixels <= 0.0 || pc.intensity <= 0.0) {
        outColor = centerColor;
        return;
    }

    float limitedPixels = min(motionPixels, pc.maxBlurPixels);
    vec2 blurVector = motion * (limitedPixels / max(motionPixels, 1e-5));
    float centerDepth = texture(_DepthTex, inUV).r;
    vec4 accumulated = centerColor;
    float totalWeight = 1.0;

    const int tapCount = 12;
    for (int tap = 0; tap < tapCount; ++tap) {
        float unitOffset = (float(tap) + 0.5) / float(tapCount) - 0.5;
        vec2 sampleUV = clamp(inUV - blurVector * unitOffset, vec2(0.0), vec2(1.0));
        float sampleDepth = texture(_DepthTex, sampleUV).r;

        // Reject across silhouettes. The relative term keeps the threshold
        // useful with both near and far depth values without reconstructing Z.
        float depthDelta = abs(sampleDepth - centerDepth);
        float depthScale = max(max(abs(centerDepth), abs(sampleDepth)), 1e-3);
        float depthWeight = exp2(-depthDelta * pc.depthRejection * 256.0 / depthScale);
        float spatialWeight = 1.0 - abs(unitOffset) * 1.4;
        float weight = max(spatialWeight, 0.05) * depthWeight;

        accumulated += texture(_SourceTex, sampleUV) * weight;
        totalWeight += weight;
    }

    outColor = accumulated / max(totalWeight, 1e-5);
}

