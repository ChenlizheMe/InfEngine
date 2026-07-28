// ============================================================================
// default_gbuffer_evaluate.glsl — Engine PBR GBuffer packing
//
// Used automatically by deferred-capable lit shading models that do not
// provide custom packing. Lighting is deliberately absent here: this pass
// records material state and the fullscreen deferred pass evaluates lights.
//
// G-Buffer layout:
//   gbuf0.rgb  = base color,                    gbuf0.a = alpha
//   gbuf1.rgb  = normal (encoded),               gbuf1.a = smoothness
//   gbuf2.r    = metallic, gbuf2.g = occlusion,  gbuf2.b = specularHighlights, gbuf2.a = 1.0
//   gbuf3.rgb  = emission,                        gbuf3.a = 1.0
//   gbuf4.x    = object light-layer mask,          gbuf4.y = shading model (1 = PBR)
// ============================================================================

void evaluate(in SurfaceData s, out vec4 gbuf0, out vec4 gbuf1,
              out vec4 gbuf2, out vec4 gbuf3, out uvec2 gbuf4) {
    gbuf0 = vec4(clamp(s.albedo, 0.0, 1.0), s.alpha);
    gbuf1 = vec4(normalize(s.normalWS) * 0.5 + 0.5, s.smoothness);
    gbuf2 = vec4(clamp(s.metallic, 0.0, 1.0), clamp(s.occlusion, 0.0, 1.0),
                 clamp(s.specularHighlights, 0.0, 1.0), 1.0);
    gbuf3 = vec4(s.emission, 1.0);
    gbuf4 = uvec2(_inx_ObjectLayerMask, 1u);
}
