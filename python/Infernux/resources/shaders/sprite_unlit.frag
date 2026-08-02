#version 450

ShaderInfo {
    Name "Sprite Unlit"
    ShadingModel Unlit
    Queue 2000
    Cull None
    AlphaClip on
    Properties {
        Color baseColor = [1.0, 1.0, 1.0, 1.0]
        Texture2D texSampler = white
        Float4 uvRect = [0.0, 0.0, 1.0, 1.0]
        Float4 displayScale = [1.0, 1.0, 0.0, 0.0]
    }
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    // displayScale.xy: fraction of the quad the sprite occupies (aspect fit).
    // Remap quad UVs so the sprite is centered within the mesh.
    vec2 dScale = material.displayScale.xy;
    vec2 tc = (v_TexCoord - 0.5) / max(dScale, vec2(1e-6)) + 0.5;
    if (tc.x < 0.0 || tc.x > 1.0 || tc.y < 0.0 || tc.y > 1.0) {
        s.alpha = 0.0;
        return;
    }
    // uvRect: xy = offset, zw = scale (sub-rect in UV space)
    vec2 uv = material.uvRect.xy + tc * material.uvRect.zw;
    vec4 texColor = texture(texSampler, uv);
    s.albedo = texColor.rgb * material.baseColor.rgb;
    s.alpha  = texColor.a * material.baseColor.a;
}
