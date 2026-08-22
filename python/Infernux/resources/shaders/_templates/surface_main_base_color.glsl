// Geometry Stage base-color pass. It executes the authored surface function
// without lighting and preserves the material's alpha-clip contract.
void main() {
    SurfaceData s = InitSurfaceData();
    s.normalWS = normalize(v_Normal);
${SURFACE_CALL}
    if (material._AlphaClipThreshold > 0.0 && s.alpha < material._AlphaClipThreshold) discard;
    outBaseColor = vec4(s.albedo, s.alpha);
}
