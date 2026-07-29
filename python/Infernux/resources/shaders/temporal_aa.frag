#version 450

ShaderInfo {
    Name "Temporal Anti-Aliasing"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D _SourceTex
        Texture2D _HistoryTex
        Texture2D _MotionTex
        Texture2D _DepthTex
    }
    PushConstants pc {
        Float feedback
        Float motionRejection
        Float depthRejection
        Float _InfernuxHistoryValid
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

void main() {
    vec4 current = texture(_SourceTex, inUV);
    if (pc._InfernuxHistoryValid < 0.5 || pc.feedback <= 0.0) {
        outColor = current;
        return;
    }

    vec2 extent = vec2(textureSize(_SourceTex, 0));
    vec2 texel = 1.0 / max(extent, vec2(1.0));
    vec2 motion = texture(_MotionTex, inUV).xy;
    vec2 historyUV = inUV - motion;
    if (any(lessThan(historyUV, vec2(0.0))) || any(greaterThan(historyUV, vec2(1.0)))) {
        outColor = current;
        return;
    }

    vec3 neighborhoodMin = current.rgb;
    vec3 neighborhoodMax = current.rgb;
    float centerDepth = texture(_DepthTex, inUV).r;
    float maxDepthDelta = 0.0;
    for (int y = -1; y <= 1; ++y) {
        for (int x = -1; x <= 1; ++x) {
            vec2 sampleUV = clamp(inUV + vec2(x, y) * texel, vec2(0.0), vec2(1.0));
            vec3 sampleColor = texture(_SourceTex, sampleUV).rgb;
            neighborhoodMin = min(neighborhoodMin, sampleColor);
            neighborhoodMax = max(neighborhoodMax, sampleColor);
            maxDepthDelta = max(maxDepthDelta, abs(texture(_DepthTex, sampleUV).r - centerDepth));
        }
    }

    vec4 history = texture(_HistoryTex, historyUV);
    history.rgb = clamp(history.rgb, neighborhoodMin, neighborhoodMax);
    float motionPixels = length(motion * extent);
    float motionWeight = exp2(-motionPixels * pc.motionRejection);
    float depthWeight = exp2(-maxDepthDelta * pc.depthRejection);
    float historyWeight = clamp(pc.feedback * motionWeight * depthWeight, 0.0, pc.feedback);
    outColor = mix(current, history, historyWeight);
}
