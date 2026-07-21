#version 450

@shader_id: particle_unlit
@shading_model: unlit
@queue: 3000
@property: baseColor, Color, [1.0, 1.0, 1.0, 1.0]
@property: texSampler, Texture2D, white
@property: softness, Float, 0.18

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texColor = texture(texSampler, v_TexCoord);
    vec2 centeredUv = v_TexCoord * 2.0 - 1.0;
    float edgeWidth = max(material.softness, 0.0001);
    float radialAlpha = 1.0 - smoothstep(1.0 - edgeWidth, 1.0, length(centeredUv));
    s.albedo = texColor.rgb * v_Color * material.baseColor.rgb;
    s.alpha = texColor.a * material.baseColor.a * radialAlpha;
}
