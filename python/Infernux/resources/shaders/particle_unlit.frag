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
        Float softness = 0.18
    }
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();
    vec4 texColor = sampleAlbedoAlpha(texSampler);
    vec2 centeredUv = getParticleLocalUV() * 2.0 - 1.0;
    float edgeWidth = max(material.softness, 0.0001);
    float radialAlpha = 1.0 - smoothstep(1.0 - edgeWidth, 1.0, length(centeredUv));
    s.albedo = texColor.rgb * v_Color * material.baseColor.rgb;
    s.alpha = texColor.a * material.baseColor.a * radialAlpha;
}
