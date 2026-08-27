// Depth must execute the material's real alpha path. A later IR optimization
// may slice this down to alpha dependencies without changing the contract.
void main() {
    SurfaceData s = InitSurfaceData();
    s.normalWS = normalize(v_Normal);
${SURFACE_CALL}
    s.alpha *= v_LineColor.a;
    if (material._AlphaClipThreshold > 0.0 && s.alpha < material._AlphaClipThreshold) discard;
}
