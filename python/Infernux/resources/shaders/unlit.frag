#version 450

ShaderInfo {
    Name "Unlit"
    ShadingModel Unlit
    Queue 2000
    Properties {
        Color baseColor = [1.0, 1.0, 1.0, 1.0]
        Texture2D texSampler = white
    }
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texColor = texture(texSampler, v_TexCoord);
    s.albedo = texColor.rgb * v_Color * material.baseColor.rgb;
    s.alpha  = texColor.a * material.baseColor.a;
}
