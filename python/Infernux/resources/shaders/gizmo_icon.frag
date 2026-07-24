#version 450

@shader_id: Gizmo Icon
@shading_model: Unlit
@hidden
@surface_type: transparent
@depth_write: off
@blend: alpha
@alpha_clip: 0.01
@cast_shadows: off
@property: baseColor, Color, [1.0, 1.0, 1.0, 1.0]
@property: texSampler, Texture2D, white

void surface(out SurfaceData s) {
    s = InitSurfaceData();

    vec4 texColor = texture(texSampler, v_TexCoord);
    s.albedo = texColor.rgb * material.baseColor.rgb;
    s.alpha = texColor.a * material.baseColor.a;
}
