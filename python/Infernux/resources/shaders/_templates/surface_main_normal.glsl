// World-space normal pass. It executes the real surface function so authored
// normal mapping and alpha clipping remain identical to the visible material.
void main() {
    SurfaceData s = InitSurfaceData();
    s.normalWS = normalize(v_Normal);
${SURFACE_CALL}
    s.alpha *= v_LineColor.a;
    s.normalWS = ResolveSurfaceNormal(s.normalWS, v_Normal);
    if (!gl_FrontFacing)
        s.normalWS = -s.normalWS;
    if (material._AlphaClipThreshold > 0.0 && s.alpha < material._AlphaClipThreshold) discard;
    vec3 normalWS = normalize(s.normalWS);
    outNormal = vec4(normalWS * 0.5 + 0.5, 1.0);
}
