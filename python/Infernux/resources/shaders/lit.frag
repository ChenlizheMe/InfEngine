#version 450

@shader_id: Lit
@shading_model: PBR
@queue: 2000
@property: baseColor, Color, [1.0, 1.0, 1.0, 1.0]
@property: metallic, Float, 0.0
@property: smoothness, Float, 0.5
@property: ambientOcclusion, Float, 1.0
@property: emissionColor, Color, [0.0, 0.0, 0.0, 0.0], HDR
@property: normalScale, Float, 1.0
@property: specularHighlights, Float, 1.0
@property: texSampler, Texture2D, white
@property: metallicMap, Texture2D, white
@property: smoothnessMap, Texture2D, white
@property: aoMap, Texture2D, white
@property: normalMap, Texture2D, normal

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
