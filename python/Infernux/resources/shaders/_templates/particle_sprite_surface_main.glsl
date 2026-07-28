layout(push_constant) uniform ParticleViewConstants {
    mat4 view_projection;
    mat4 previous_view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
    vec4 alignment_reference;
} particleView;

layout(set = 0, binding = 15) uniform sampler2D _InxParticleSceneDepth;

float _inxParticleEyeDepth(float deviceDepth) {
    float numerator = particleView.depth_reconstruct.y - deviceDepth * particleView.depth_reconstruct.w;
    float denominator = deviceDepth * particleView.depth_reconstruct.z - particleView.depth_reconstruct.x;
    // GLM can generate either LH (+Z forward) or RH (-Z forward) projections.
    // Soft-particle fading needs a positive eye-space distance in both cases.
    return abs(numerator / (abs(denominator) > 1e-7 ? denominator : 1e-7));
}

bool inxParticleReceivesShadows() {
    return particleView.rendering_control.x > 0.5;
}

void main() {
    SurfaceData s = InitSurfaceData();
    s.normalWS = normalize(v_Normal);
${SURFACE_CALL}
    float postSurfaceCoverage = v_ParticleAlpha;
    if (particleView.camera_up.w > 0.5) {
        ivec2 depthSize = textureSize(_InxParticleSceneDepth, 0);
        ivec2 depthCoord = clamp(ivec2(gl_FragCoord.xy), ivec2(0), depthSize - ivec2(1));
        float sceneDepth = _inxParticleEyeDepth(texelFetch(_InxParticleSceneDepth, depthCoord, 0).r);
        float particleDepth = _inxParticleEyeDepth(gl_FragCoord.z);
        float softCoverage = smoothstep(
            0.0,
            1.0,
            clamp((sceneDepth - particleDepth) / max(particleView.camera_right.w, 1e-4), 0.0, 1.0));
        postSurfaceCoverage *= softCoverage;
    }
    s.alpha *= postSurfaceCoverage;
    if (!gl_FrontFacing)
        s.normalWS = -s.normalWS;
    if (material._AlphaClipThreshold > 0.0 && s.alpha < material._AlphaClipThreshold) discard;
    vec4 _forwardResult;
    evaluate(s, _forwardResult);
    // Premultiplied outputs must fade color and alpha by identical coverage.
    // Otherwise soft intersections leave bright horizontal bands where alpha
    // approaches zero but the custom shading model still emits lit RGB.
    if (particleView.rendering_control.y > 0.5)
        _forwardResult.rgb *= postSurfaceCoverage;
    outColor = _forwardResult;
}
