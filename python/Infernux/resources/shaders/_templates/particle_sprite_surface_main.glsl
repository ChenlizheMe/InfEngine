void main() {
    SurfaceData s = InitSurfaceData();
    s.normalWS = normalize(v_Normal);
${SURFACE_CALL}
    s.alpha *= v_ParticleAlpha;
    if (!gl_FrontFacing)
        s.normalWS = -s.normalWS;
    if (material._AlphaClipThreshold > 0.0 && s.alpha < material._AlphaClipThreshold) discard;
    vec4 _forwardResult;
    evaluate(s, _forwardResult);
    outColor = _forwardResult;
}
