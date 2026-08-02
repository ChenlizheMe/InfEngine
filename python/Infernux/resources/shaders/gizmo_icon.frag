#version 450

ShaderInfo {
    Name "Gizmo Icon"
    Hidden On
    ShadingModel Unlit
    Surface Transparent
    DepthWrite Off
    Blend Alpha
    AlphaClip 0.01
    CastShadows Off
    Properties {
        Color baseColor = [1.0, 1.0, 1.0, 1.0]
        Texture2D texSampler = white
    }
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();

    vec4 texColor = texture(texSampler, v_TexCoord);
    s.albedo = texColor.rgb * material.baseColor.rgb;
    s.alpha = texColor.a * material.baseColor.a;
}
