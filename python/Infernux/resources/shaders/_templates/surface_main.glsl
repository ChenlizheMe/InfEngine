// ============================================================================
// surface_main.glsl — Auto-generated main() for forward surface shaders
//
// Calls surface() to fill SurfaceData, then evaluates lighting via evaluate()
// from the referenced .shadingmodel file.
//
// _AlphaClipThreshold is always available in MaterialProperties UBO.
// When > 0.0, fragments with alpha below the threshold are discarded.
// ============================================================================

void main() {
    SurfaceData s = InitSurfaceData();
    s.normalWS = normalize(v_Normal);
${SURFACE_CALL}
    // Unity-style double-sided normal fix: when the fragment is a back-face,
    // negate the world-space normal so lighting evaluates correctly regardless
    // of which face the camera sees.  This applies to both flat normals and
    // normal-mapped normals because sampleNormal() builds its TBN from
    // v_Normal/v_Tangent (the geometric basis), and the resulting world-space
    // normal inherits the same facing.
    if (!gl_FrontFacing)
        s.normalWS = -s.normalWS;
    if (material._AlphaClipThreshold > 0.0 && s.alpha < material._AlphaClipThreshold) discard;
    vec4 _forwardResult;
    evaluate(s, _forwardResult);
    outColor = _forwardResult;
}
