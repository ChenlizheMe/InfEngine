#version 450

@shader_id: Error
@shading_model: Unlit
@queue: 2000
@hidden

void surface(out SurfaceData s) {
    s = InitSurfaceData();

    vec2 uvGrid = v_TexCoord * 4.0;
    vec2 worldGrid = v_WorldPos.xz * 2.0;
    float uvPattern = mod(step(0.5, fract(uvGrid.x)) + step(0.5, fract(uvGrid.y)), 2.0);
    float worldPattern = mod(step(0.5, fract(worldGrid.x)) + step(0.5, fract(worldGrid.y)), 2.0);
    float checker = step(0.5, mix(uvPattern, worldPattern, 0.3));

    s.albedo = mix(vec3(0.0), vec3(1.0, 0.0, 1.0), checker);
    s.alpha = 1.0;
}
