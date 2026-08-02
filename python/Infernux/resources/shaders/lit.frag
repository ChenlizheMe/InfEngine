#version 450

ShaderInfo {
    Name "Lit"
    ShadingModel PBR
    Queue 2000
    Properties {
        Color baseColor = [1.0, 1.0, 1.0, 1.0]
        Float metallic = 0.0
        Float smoothness = 0.5
        Float ambientOcclusion = 1.0
        Color emissionColor = [0.0, 0.0, 0.0, 0.0] HDR
        Float normalScale = 1.0
        Float specularHighlights = 1.0
        Texture2D texSampler = white
        Texture2D metallicMap = white
        Texture2D smoothnessMap = white
        Texture2D aoMap = white
        Texture2D normalMap = normal
    }
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();

    vec4 texColor = sampleAlbedoAlpha(texSampler);
    s.albedo     = texColor.rgb * getVertexColor() * material.baseColor.rgb;
    s.metallic   = sampleGrayscale(metallicMap) * material.metallic;
    s.smoothness = sampleGrayscale(smoothnessMap) * material.smoothness;
    s.occlusion  = sampleGrayscale(aoMap) * material.ambientOcclusion;
    s.normalWS   = sampleNormal(normalMap, material.normalScale);
    s.emission   = material.emissionColor.rgb * material.emissionColor.a;
    s.alpha      = texColor.a * material.baseColor.a;
    s.specularHighlights = material.specularHighlights;
}
