@shader_id: Lib Depth

// ============================================================================
// lib/depth.glsl — Depth buffer utilities
//
// Full-coverage depth toolkit matching Unity ShaderGraph Scene Depth,
// Depth Fade, and fog nodes.
// Requires: InfGlobals UBO (auto-injected by engine at set 2, binding 0)
// Usage: @import: Lib Depth
// ============================================================================

// ============================================================================
// Depth Fade & Soft Particles  (Unity: Scene Depth, Depth Fade)
// ============================================================================

// Soft blending based on depth difference  (Unity: depth fade for particles)
float depthFade(float sceneDepthLinear, float fragmentDepth, float fadeDistance) {
    return clamp((sceneDepthLinear - fragmentDepth) / fadeDistance, 0.0, 1.0);
}

// Depth difference (signed) between scene and fragment
float depthDifference(float sceneDepthLinear, float fragmentDepth) {
    return sceneDepthLinear - fragmentDepth;
}

// ============================================================================
// World Position Reconstruction  (Unity: Scene Depth based reconstruction)
// ============================================================================

// Reconstruct world-space position from depth buffer
vec3 reconstructWorldPos(float rawDepth, vec2 screenUV, mat4 invViewProj) {
    vec4 clipPos = vec4(screenUV * 2.0 - 1.0, rawDepth, 1.0);
    vec4 worldPos = invViewProj * clipPos;
    return worldPos.xyz / worldPos.w;
}

// Reconstruct view-space position from depth and screen UV
vec3 reconstructViewPos(float linearDepth, vec2 screenUV, float tanHalfFov, float aspect) {
    vec2 ndc = screenUV * 2.0 - 1.0;
    return vec3(ndc.x * aspect * tanHalfFov * linearDepth,
                ndc.y * tanHalfFov * linearDepth,
                -linearDepth);
}

// ============================================================================
// Fog  (Unity: Fog, Exponential Fog, Linear Fog)
// ============================================================================

// Linear fog factor  (Unity: Fog — Linear mode)
float linearFog(float viewDepth, float fogStart, float fogEnd) {
    return clamp((viewDepth - fogStart) / (fogEnd - fogStart), 0.0, 1.0);
}

// Exponential fog  (Unity: Fog — Exponential mode)
float exponentialFog(float viewDepth, float density) {
    return 1.0 - exp(-density * viewDepth);
}

// Exponential squared fog  (Unity: Fog — Exponential Squared mode)
float exponentialSquaredFog(float viewDepth, float density) {
    float f = density * viewDepth;
    return 1.0 - exp(-f * f);
}

// Height fog (exponential fog with height falloff)
float heightFog(float viewDepth, float worldY, float density, float heightFalloff, float fogBaseY) {
    float heightFactor = exp(-max(worldY - fogBaseY, 0.0) * heightFalloff);
    float distFog = 1.0 - exp(-density * viewDepth);
    return distFog * heightFactor;
}

// Apply fog color to scene color  (Unity: Fog — apply)
vec3 applyFog(vec3 sceneColor, vec3 fogColor, float fogFactor) {
    return mix(sceneColor, fogColor, fogFactor);
}

// ============================================================================
// Depth Edge Detection  (useful for outlines, post-processing)
// ============================================================================

// Depth-based edge detection (Roberts cross)
float depthEdge(sampler2D depthTex, vec2 uv, vec2 texelSize, float threshold) {
    float d00 = texture(depthTex, uv).r;
    float d10 = texture(depthTex, uv + vec2(texelSize.x, 0.0)).r;
    float d01 = texture(depthTex, uv + vec2(0.0, texelSize.y)).r;
    float d11 = texture(depthTex, uv + texelSize).r;
    float edge = abs(d00 - d11) + abs(d10 - d01);
    return step(threshold, edge);
}
