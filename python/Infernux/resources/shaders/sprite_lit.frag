#version 450

@shader_id: Sprite Lit
@shading_model: PBR
@queue: 2000
@cull: none
@property: baseColor, Color, [1.0, 1.0, 1.0, 1.0]
@property: metallic, Float, 0.0
@property: smoothness, Float, 0.5
@property: ambientOcclusion, Float, 1.0
@property: emissionColor, Color, [0.0, 0.0, 0.0, 0.0], HDR
@property: normalScale, Float, 1.0
@property: specularHighlights, Float, 1.0
@property: texSampler, Texture2D, white
@property: normalMap, Texture2D, normal
@alpha_clip: on
@property: uvRect, Float4, [0.0, 0.0, 1.0, 1.0]
@property: displayScale, Float4, [1.0, 1.0, 0.0, 0.0]

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    // displayScale.xy: fraction of the quad the sprite occupies (aspect fit).
    vec2 dScale = material.displayScale.xy;
    vec2 tc = (v_TexCoord - 0.5) / max(dScale, vec2(1e-6)) + 0.5;
    if (tc.x < 0.0 || tc.x > 1.0 || tc.y < 0.0 || tc.y > 1.0) {
        s.alpha = 0.0;
        return;
    }
    // uvRect: xy = offset, zw = scale (sub-rect in UV space)
    vec2 uv = material.uvRect.xy + tc * material.uvRect.zw;
    vec4 texColor = texture(texSampler, uv);
    s.albedo     = texColor.rgb * material.baseColor.rgb;
    s.metallic   = material.metallic;
    s.smoothness = material.smoothness;
    s.occlusion  = material.ambientOcclusion;
    s.normalWS   = sampleNormal(normalMap, material.normalScale);
    s.emission   = material.emissionColor.rgb * material.emissionColor.a;
    s.alpha      = texColor.a * material.baseColor.a;
    s.specularHighlights = material.specularHighlights;
}
