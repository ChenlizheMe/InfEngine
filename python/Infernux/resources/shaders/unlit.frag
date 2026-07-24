#version 450

@shader_id: Unlit
@shading_model: Unlit
@queue: 2000
@property: baseColor, Color, [1.0, 1.0, 1.0, 1.0]
@property: texSampler, Texture2D, white

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texColor = texture(texSampler, v_TexCoord);
    s.albedo = texColor.rgb * v_Color * material.baseColor.rgb;
    s.alpha  = texColor.a * material.baseColor.a;
}
