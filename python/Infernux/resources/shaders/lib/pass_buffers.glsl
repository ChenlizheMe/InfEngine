ShaderInfo {
    Name "Lib Pass Buffers"
}

// Source-scoped buffers supplied by the PassResult selected for this effect.
// Shaders importing this library declare Capabilities [Fullscreen, PassBuffers].

vec4 samplePassColor(vec2 uv) {
    return texture(_InxPassColor, uv);
}

float samplePassDeviceDepth(vec2 uv) {
    return texture(_InxPassDepth, uv).r;
}

float linearizePassEyeDepth(float rawDepth) {
    return 1.0 / (ubo.zBufferParams.z * rawDepth + ubo.zBufferParams.w);
}

float samplePassLinearEyeDepth(vec2 uv) {
    return linearizePassEyeDepth(samplePassDeviceDepth(uv));
}

float samplePassLinear01Depth(vec2 uv) {
    return samplePassLinearEyeDepth(uv) / ubo.projectionParams.y;
}

vec4 samplePassNormalEncoded(vec2 uv) {
    return texture(_InxPassNormal, uv);
}

float samplePassNormalCoverage(vec2 uv) {
    return samplePassNormalEncoded(uv).a;
}

vec3 decodePassWorldNormal(vec4 encodedNormal) {
    if (encodedNormal.a <= 0.0)
        return vec3(0.0);
    return normalize(encodedNormal.xyz * 2.0 - 1.0);
}

vec3 samplePassWorldNormal(vec2 uv) {
    return decodePassWorldNormal(samplePassNormalEncoded(uv));
}

vec3 samplePassViewNormal(vec2 uv) {
    vec3 normalWS = samplePassWorldNormal(uv);
    return dot(normalWS, normalWS) > 0.0
        ? normalize(mat3(ubo.view) * normalWS)
        : vec3(0.0);
}

vec2 samplePassMotionUV(vec2 uv) {
    return texture(_InxPassMotion, uv).xy;
}

vec2 samplePassMotionPixels(vec2 uv) {
    return samplePassMotionUV(uv) * vec2(textureSize(_InxPassMotion, 0));
}
