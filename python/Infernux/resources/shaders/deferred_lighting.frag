#version 450

ShaderInfo {
    Name "Deferred Lighting"
    Hidden On
    Capabilities [Fullscreen, DeferredLighting, CameraMatrices]
    Imports ["Lighting", "PBR"]
    Resources {
        Texture2D _GAlbedo
        Texture2D _GNormal
        Texture2D _GMaterial
        Texture2D _GEmission
        Texture2DUInt _GObject
        Texture2D _SceneDepth
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

// Canonical deferred lighting pass. Geometry records only surface state;
// lighting is evaluated here from the camera-local Forward+ light grid.
//
// Resource order (matches GBuffer MRT + depth + shadow map):
//   binding 0 — gAlbedo     (RGBA16_SFLOAT: base color + alpha)
//   binding 1 — gNormal     (RGBA16_SFLOAT: encoded world normal.xyz)
//   binding 2 — gMaterial   (RGBA8_UNORM: metallic, occlusion, specularHighlights, 1.0)
//   binding 3 — gEmission   (RGBA16_SFLOAT: emission.rgb)
//   binding 4 — gObject     (RG32_UINT: layer mask, shading model)
//   binding 5 — sceneDepth  (D32_SFLOAT)

const uint INX_SHADING_MODEL_UNLIT = 0u;
const uint INX_SHADING_MODEL_PBR = 1u;

vec3 reconstructWorldPosition(vec2 uv, float depth) {
    vec4 clip = vec4(uv * 2.0 - 1.0, depth, 1.0);
    vec4 world = ubo.inverseViewProj * clip;
    return world.xyz / max(abs(world.w), 1e-7);
}

vec3 evaluateDeferredPbr(vec3 worldPos, vec3 normalWS, vec3 albedo,
                         float smoothness, float metallic, float occlusion,
                         float specularHighlights, vec3 emission) {
    vec3 N = normalize(normalWS);
    vec3 V = normalize(lighting.cameraPos.xyz - worldPos);
    float perceptualRoughness = clamp(1.0 - smoothness, 0.045, 1.0);
    float roughness = GeometricSpecularAA(N, perceptualRoughness * perceptualRoughness);
    perceptualRoughness = max(perceptualRoughness, sqrt(roughness));
    vec3 F0 = mix(vec3(0.04), albedo, metallic);
    float f90 = ComputeF90(F0);
    float NdotV = max(dot(N, V), 0.0);
    vec2 envBrdf = EnvBRDFApprox(perceptualRoughness, NdotV);
    float reflectivity = envBrdf.x + envBrdf.y;
    vec3 energyCompensation = 1.0 + F0 * (1.0 / max(reflectivity, 0.001) - 1.0);
    float viewDepth = abs((ubo.view * vec4(worldPos, 1.0)).z);
    Light mainLight = getMainLight(worldPos, N, viewDepth);
    vec3 direct = calculateAllLighting(
        worldPos, N, V, albedo, metallic, roughness, perceptualRoughness,
        F0, f90, energyCompensation, viewDepth, mainLight.shadow);

    vec3 diffuseIrradiance = sampleAmbientIrradiance(N);
    vec3 reflection = getSpecularAmbientDirection(N, V, perceptualRoughness);
    float prefilter = saturate(perceptualRoughness * (1.7 - 0.7 * perceptualRoughness));
    vec3 specularIrradiance = mix(sampleAmbientProbe(reflection),
                                  sampleAmbientIrradiance(reflection), prefilter);
    vec3 kS = F_SchlickRoughness(F0, f90, NdotV, perceptualRoughness);
    vec3 diffuse = (1.0 - kS) * (1.0 - metallic) * albedo * diffuseIrradiance * occlusion;
    float specOcclusion = ComputeSpecularOcclusion(NdotV, occlusion, perceptualRoughness);
    float horizonFade = HorizonOcclusion(reflection, N);
    vec3 specular = specularIrradiance
                  * (F0 * envBrdf.x + envBrdf.y)
                  * energyCompensation
                  * (specOcclusion * horizonFade)
                  * specularHighlights;
    return direct + diffuse + specular + emission;
}

void main() {
    float depth = texture(_SceneDepth, inUV).r;
    if (depth >= 0.999999) {
        outColor = vec4(0.0);
        return;
    }

    vec4 base = texture(_GAlbedo, inUV);
    vec4 normalData = texture(_GNormal, inUV);
    vec4 materialData = texture(_GMaterial, inUV);
    vec3 emission = texture(_GEmission, inUV).rgb;
    uvec2 objectData = texture(_GObject, inUV).rg;
    _inx_ObjectLayerMask = objectData.x;

    if (objectData.y == INX_SHADING_MODEL_UNLIT) {
        outColor = vec4(base.rgb + emission, base.a);
        return;
    }

    vec3 worldPos = reconstructWorldPosition(inUV, depth);
    vec3 normalWS = normalData.xyz * 2.0 - 1.0;
    outColor = vec4(
        evaluateDeferredPbr(worldPos, normalWS, clamp(base.rgb, 0.0, 1.0),
                            normalData.a, materialData.r, materialData.g,
                            materialData.b, emission),
        base.a);
}
