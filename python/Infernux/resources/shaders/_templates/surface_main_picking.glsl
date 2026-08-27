// Picking shares the exact material alpha decision used by visible rendering.
void main() {
    SurfaceData s = InitSurfaceData();
    s.normalWS = normalize(v_Normal);
${SURFACE_CALL}
    s.alpha *= v_LineColor.a;
    if (material._AlphaClipThreshold > 0.0 && s.alpha < material._AlphaClipThreshold) discard;
    outObjectId = _inx_ObjectId;
}
