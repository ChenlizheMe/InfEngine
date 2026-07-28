// ============================================================================
// default_gbuffer_evaluate.glsl — Engine default GBuffer packing (pre-lit)
//
// Used automatically when a .shadingmodel file does not provide a custom
// ShadingModel Entry: gbuffer block.  Performs full PBR lighting evaluation identical to
// the forward path and stores the HDR lit result in gbuf0 so that the
// deferred lighting pass can simply pass the color through.
//
// G-Buffer layout:
//   gbuf0.rgb  = lit HDR color (incl. emission), gbuf0.a = alpha
//   gbuf1.rgb  = normal (encoded),               gbuf1.a = smoothness
//   gbuf2.r    = metallic, gbuf2.g = occlusion,  gbuf2.b = specularHighlights, gbuf2.a = 1.0
//   gbuf3.rgb  = emission,                        gbuf3.a = 1.0
// ============================================================================

void evaluate(in SurfaceData s, out vec4 gbuf0, out vec4 gbuf1,
              out vec4 gbuf2, out vec4 gbuf3) {
    vec3 albedo = clamp(s.albedo, 0.0, 1.0);
    float metallic = clamp(s.metallic, 0.0, 1.0);
    float perceptualRoughness = clamp(1.0 - s.smoothness, 0.045, 1.0);
    float roughness = perceptualRoughness * perceptualRoughness;
    float ao = clamp(s.occlusion, 0.0, 1.0);
    vec3 N = normalize(s.normalWS);

    roughness = GeometricSpecularAA(N, roughness);
    perceptualRoughness = max(perceptualRoughness, sqrt(roughness));
    vec3 V = normalize(lighting.cameraPos.xyz - v_WorldPos);

    vec3  F0    = mix(vec3(0.04), albedo, metallic);
    float f90   = ComputeF90(F0);
    float NdotV = max(dot(N, V), 0.0);

    vec2  envBrdf      = EnvBRDFApprox(perceptualRoughness, NdotV);
    float reflectivity = envBrdf.x + envBrdf.y;
    vec3  energyCompensation = 1.0 + F0 * (1.0 / max(reflectivity, 0.001) - 1.0);

    Light mainLight = getMainLight(v_WorldPos, N, v_ViewDepth);
    vec3 Lo = calculateAllLighting(v_WorldPos, N, V,
                                   albedo, metallic,
                                   roughness, perceptualRoughness,
                                   F0, f90, energyCompensation,
                                   v_ViewDepth, mainLight.shadow);

    // Indirect lighting — keep in sync with pbr.shadingmodel ShadingModel Entry: forward.
    // Diffuse: cosine-convolved irradiance (roughness-free, SH-style).
    // Specular: radiance model blended toward irradiance as the GGX lobe
    // widens (analytic prefiltered-mip stand-in, metals included), gated by
    // AO-derived specular occlusion and geometric horizon fade.
    vec3 diffuseIrradiance = sampleAmbientIrradiance(N);
    vec3 reflDir = getSpecularAmbientDirection(N, V, perceptualRoughness);
    float prefilter = saturate(perceptualRoughness * (1.7 - 0.7 * perceptualRoughness));
    vec3 specularIrradiance = mix(sampleAmbientProbe(reflDir),
                                  sampleAmbientIrradiance(reflDir),
                                  prefilter);

    vec3 kS_env = F_SchlickRoughness(F0, f90, NdotV, perceptualRoughness);
    vec3 kD_env = (1.0 - kS_env) * (1.0 - metallic);
    vec3 diffuseEnv = kD_env * albedo * diffuseIrradiance * ao;

    float specOcclusion = ComputeSpecularOcclusion(NdotV, ao, perceptualRoughness);
    vec3 geomN = normalize(v_Normal);
    geomN = (dot(geomN, N) < 0.0) ? -geomN : geomN;
    float horizonFade = HorizonOcclusion(reflDir, geomN);
    vec3 specEnv = specularIrradiance
                 * (F0 * envBrdf.x + envBrdf.y)
                 * energyCompensation
                 * (specOcclusion * horizonFade)
                 * s.specularHighlights;

    vec3 ambient = diffuseEnv + specEnv;
    vec3 litColor = ambient + Lo + s.emission;

    gbuf0 = vec4(litColor, s.alpha);
    gbuf1 = vec4(normalize(s.normalWS) * 0.5 + 0.5, s.smoothness);
    gbuf2 = vec4(metallic, s.occlusion, s.specularHighlights, 1.0);
    gbuf3 = vec4(s.emission, 1.0);
}
