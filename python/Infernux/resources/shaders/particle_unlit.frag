#version 450

ShaderInfo {
    Name "Particle Unlit"
    ShadingModel Unlit
    Queue 3000
    Cull None
    DepthWrite Off
    Blend Alpha
    Properties {
        Color baseColor = [1.0, 1.0, 1.0, 1.0]
        Texture2D texSampler = white
    }
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texColor = sampleAlbedoAlpha(texSampler);
    s.albedo = texColor.rgb * v_Color * material.baseColor.rgb;
    s.alpha = texColor.a * material.baseColor.a;
}
