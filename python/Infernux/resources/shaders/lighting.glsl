@shader_id: lighting

// ============================================================================
// lighting.glsl — Importable lighting utilities for PBR shaders
//
// Provides Unity-style helper functions so custom lit shaders can use shadows
// and lighting without duplicating the full lighting loop. Requires the
// auto-injected LightingUBO and shadowMap sampler (@shading_model: pbr).
//
// Usage in a custom shader:
//   @shading_model: pbr
//   @import: lighting
//   void main() {
//       Light mainLight = getMainLight(worldPos, normal);
//       vec3 color = mainLight.color * mainLight.attenuation * mainLight.shadow;
//       // ... custom shading ...
//   }
// ============================================================================

@import: pbr

// ============================================================================
// Light struct — similar to Unity's Light struct in URP
// ============================================================================
struct Light {
    vec3  direction;    // Normalized direction TO the light (i.e. -lightDir)
    vec3  color;        // Light color × intensity
    float attenuation;  // Distance attenuation (1.0 for directional)
    float shadow;       // Shadow factor: 1.0 = fully lit, 0.0 = fully shadowed
};

// ============================================================================
// Ambient probe helpers — HDRP-style hemisphere approximation
// ============================================================================

vec3 sampleAmbientProbe(vec3 direction) {
    float mode = lighting.ambientEquatorColor.a;
    if (mode < 0.5) {
        return lighting.ambientColor.rgb * lighting.ambientColor.a;
    }

    vec3 sky     = lighting.ambientSkyColor.rgb * lighting.ambientSkyColor.a;
    vec3 equator = lighting.ambientEquatorColor.rgb;
    vec3 ground  = lighting.ambientGroundColor.rgb * lighting.ambientGroundColor.a;

    return max(skyGradient(direction.y, sky, equator, ground), vec3(0.0));
}

vec3 sampleAmbientProbeAverage() {
    float mode = lighting.ambientEquatorColor.a;
    if (mode < 0.5) {
        return lighting.ambientColor.rgb * lighting.ambientColor.a;
    }

    vec3 sky     = lighting.ambientSkyColor.rgb * lighting.ambientSkyColor.a;
    vec3 equator = lighting.ambientEquatorColor.rgb;
    vec3 ground  = lighting.ambientGroundColor.rgb * lighting.ambientGroundColor.a;

    // With only a tri-color ambient probe and no true diffuse SH/irradiance data,
    // use a uniform average for diffuse GI so rough dielectrics do not inherit
    // obvious sky/ground directionality.
    return max((sky + equator + ground) * 0.33333333, vec3(0.0));
}

vec3 getSpecularAmbientDirection(vec3 N, vec3 V, float perceptualRoughness) {
    vec3 R = reflect(-V, N);
    // Blend from mirror reflection R toward normal N as roughness increases.
    // Using linear smoothness (not squared) preserves more view-angle
    // responsiveness on rough surfaces, matching Unity's behavior.
    float lerpFactor = saturate(1.0 - perceptualRoughness);
    return normalize(mix(N, R, lerpFactor));
}

// ============================================================================
// Shadow Mapping — Cascaded Shadow Maps with Vogel Disk Soft Shadows
// ============================================================================

// Atlas tile offset for 2x2 cascade layout
vec2 getCascadeAtlasOffset(int ci) {
    float u = float(ci - 2 * (ci / 2)) * 0.5; // ci % 2
    float v = float(ci / 2) * 0.5;
    return vec2(u, v);
}

// Interleaved gradient noise — per-pixel pseudo-random rotation
float interleavedGradientNoise(vec2 screenPos) {
    vec3 magic = vec3(0.06711056, 0.00583715, 52.9829189);
    return fract(magic.z * fract(dot(screenPos, magic.xy)));
}

// Vogel disk sample point on a unit disk
vec2 vogelDiskSample(int sampleIdx, int totalSamples, float phi) {
    float GoldenAngle = 2.399963;
    float r = sqrt((float(sampleIdx) + 0.5) / float(totalSamples));
    float theta = float(sampleIdx) * GoldenAngle + phi;
    return vec2(cos(theta), sin(theta)) * r;
}

// Per-cascade bias scale proportional to texel-to-world size ratio
float getCascadeBiasScale(int ci) {
    float split0 = max(lighting.shadowCascadeSplits[0], 1.0);
    float splitI = lighting.shadowCascadeSplits[ci];
    return max(sqrt(splitI / split0), 1.0);
}

// Sample shadow for a specific cascade index
float sampleCascadeShadow(vec3 worldPos, vec3 normal, int ci) {
    DirectionalLightData mainLight = lighting.directionalLights[0];
    vec3 lightDir = normalize(-mainLight.direction.xyz);
    float cosTheta = max(dot(normal, lightDir), 0.0);
    float userBias = mainLight.shadowParams.y;
    float userNormalBias = mainLight.shadowParams.z;
    float cascadeScale = getCascadeBiasScale(ci);
    float slopeBias = (1.0 - cosTheta) * userBias * 2.0;
    float bias = (userBias + slopeBias) * cascadeScale;
    vec3 biasedPos = worldPos + normal * userNormalBias * cascadeScale;

    vec4 lightClipPos = lighting.lightVP[ci] * vec4(biasedPos, 1.0);
    vec3 projCoords = lightClipPos.xyz / lightClipPos.w;
    projCoords.xy = projCoords.xy * 0.5 + 0.5;

    if (projCoords.x < 0.0 || projCoords.x > 1.0 ||
        projCoords.y < 0.0 || projCoords.y > 1.0 ||
        projCoords.z > 1.0 || projCoords.z < 0.0) {
        return 1.0;
    }

    float currentDepth = projCoords.z;
    vec2 atlasOffset = getCascadeAtlasOffset(ci);
    vec2 atlasUV = atlasOffset + projCoords.xy * 0.5;
    float texelSize = 2.0 / lighting.shadowMapParams.x;
    float shadowType = mainLight.shadowParams.w;
    float shadow = 0.0;

    vec2 tileMin = atlasOffset + vec2(texelSize);
    vec2 tileMax = atlasOffset + vec2(0.5) - vec2(texelSize);

    if (shadowType < 1.5) {
        float closestDepth = texture(shadowMap, clamp(atlasUV, tileMin, tileMax)).r;
        shadow = (currentDepth - bias > closestDepth) ? 0.0 : 1.0;
    } else {
        float noise = interleavedGradientNoise(gl_FragCoord.xy);
        float rotation = noise * 6.283185;
        float radius = 1.5 * texelSize;
        for (int s = 0; s < 16; ++s) {
            vec2 diskOffset = vogelDiskSample(s, 16, rotation) * radius;
            vec2 sampleUV = clamp(atlasUV + diskOffset, tileMin, tileMax);
            float sampleDepth = texture(shadowMap, sampleUV).r;
            shadow += (currentDepth - bias > sampleDepth) ? 0.0 : 1.0;
        }
        shadow *= 0.0625;
    }
    return shadow;
}

/**
 * Calculate shadow factor with cascaded shadow maps.
 * fragViewDepthVal should be the fragment's view-space depth.
 * Returns: 1.0 = fully lit, 0.0 = fully shadowed
 */
float calculateShadow(vec3 worldPos, vec3 normal, float fragViewDepthVal) {
    DirectionalLightData light = lighting.directionalLights[0];
    float shadowType = light.shadowParams.w;
    if (shadowType < 0.5) return 1.0;
    if (lighting.shadowMapParams.y < 0.5) return 1.0;

    int cascadeCount = int(lighting.shadowMapParams.z);
    if (cascadeCount < 1) return 1.0;

    float viewDepth = fragViewDepthVal;
    int cascadeIdx = cascadeCount - 1;
    for (int i = 0; i < 4; ++i) {
        if (i >= cascadeCount) break;
        if (viewDepth < lighting.shadowCascadeSplits[i]) {
            cascadeIdx = i;
            break;
        }
    }

    float shadow = sampleCascadeShadow(worldPos, normal, cascadeIdx);

    if (cascadeIdx < cascadeCount - 1) {
        float splitDist = lighting.shadowCascadeSplits[cascadeIdx];
        float prevSplit = 0.0;
        if (cascadeIdx > 0) {
            prevSplit = lighting.shadowCascadeSplits[cascadeIdx - 1];
        }
        float cascadeRange = splitDist - prevSplit;
        float blendZone = cascadeRange * 0.1;
        float distToEdge = splitDist - viewDepth;
        if (distToEdge < blendZone && blendZone > 0.0) {
            float t = smoothstep(0.0, blendZone, distToEdge);
            float nextShadow = sampleCascadeShadow(worldPos, normal, cascadeIdx + 1);
            shadow = mix(nextShadow, shadow, t);
        }
    }

    float shadowStrength = light.shadowParams.x;
    return mix(1.0, shadow, shadowStrength);
}

// ============================================================================
// getMainLight — Unity-style main directional light accessor
// ============================================================================

/**
 * Get the main directional light with shadow factor already computed.
 * @param worldPos Fragment world position
 * @param normal Fragment world-space normal
 * @param fragViewDepthVal Fragment view-space depth (for cascade selection)
 */
Light getMainLight(vec3 worldPos, vec3 normal, float fragViewDepthVal) {
    Light l;
#ifdef INX_FORWARD_PLUS_PASS
    if (canonicalLightCountsAndGeneration.x > 0u) {
        CanonicalLightData dl = canonicalLights[0];
        l.direction = normalize(-dl.directionOuterCos.xyz);
        l.color = dl.colorIntensity.rgb * dl.colorIntensity.w;
        l.attenuation = 1.0;
        l.shadow = calculateShadow(worldPos, normal, fragViewDepthVal);
    } else {
        l.direction = vec3(0.0, 1.0, 0.0);
        l.color = vec3(0.0);
        l.attenuation = 0.0;
        l.shadow = 1.0;
    }
#else
    if (lighting.lightCounts.x > 0) {
        DirectionalLightData dl = lighting.directionalLights[0];
        l.direction = normalize(-dl.direction.xyz);
        l.color = dl.color.rgb * dl.color.w;
        l.attenuation = 1.0;
        l.shadow = calculateShadow(worldPos, normal, fragViewDepthVal);
    } else {
        l.direction = vec3(0.0, 1.0, 0.0);
        l.color = vec3(0.0);
        l.attenuation = 0.0;
        l.shadow = 1.0;
    }
#endif
    return l;
}

// ============================================================================
// calculateAllLighting — Full PBR lighting evaluation (directional + point + spot)
// ============================================================================

/**
 * Evaluate all lights (directional, point, spot) using HDRP-aligned Cook-Torrance PBR.
 * Shadow is applied only to the main directional light (index 0).
 *
 * @param roughness           LINEAR roughness (= perceptualRoughness²)
 * @param perceptualRoughness Artist-facing roughness (1 − smoothness)
 * @param f90                 Fresnel reflectance at 90° (from ComputeF90)
 * @param energyCompensation  Multiscatter energy boost (pre-computed)
 * @param shadow              Pre-computed shadow factor for main light
 */
vec3 calculateAllLighting(vec3 worldPos, vec3 N, vec3 V,
                          vec3 albedo, float metallic,
                          float roughness, float perceptualRoughness,
                          vec3 F0, float f90, vec3 energyCompensation,
                          float shadow) {
    vec3 Lo = vec3(0.0);

    // Directional lights
#ifdef INX_FORWARD_PLUS_PASS
    uint directionalCount = canonicalLightCountsAndGeneration.x;
    for (uint i = 0u; i < directionalCount; ++i) {
        CanonicalLightData light = canonicalLights[i];
        if ((light.metadata.w & 1u) == 0u) continue;
        if ((light.metadata.y & _inx_ObjectLayerMask) == 0u) continue;
        vec3 L = normalize(-light.directionOuterCos.xyz);
        vec3 radiance = light.colorIntensity.rgb * light.colorIntensity.w;
        float lightShadow = (i == 0u) ? shadow : 1.0;
        Lo += evaluatePBRLight(N, V, L, radiance, albedo, metallic,
                               roughness, perceptualRoughness,
                               F0, f90, energyCompensation) * lightShadow;
    }

    uvec4 tileHeader = inxForwardPlusTileHeader();
    for (uint entry = 0u; entry < tileHeader.y; ++entry) {
        uint localIndex = forwardPlusTileIndices[tileHeader.x + entry];
        CanonicalLightData light = canonicalLights[directionalCount + localIndex];
        if ((light.metadata.y & _inx_ObjectLayerMask) == 0u) continue;
        vec3 lightVec = light.positionRange.xyz - worldPos;
        float distanceToLight = length(lightVec);
        float range = max(light.positionRange.w, 0.0001);
        if (distanceToLight <= 0.00001 || distanceToLight >= range) continue;

        vec3 L = lightVec / distanceToLight;
        float normalizedDistance = distanceToLight / range;
        float rangeWindow = max(1.0 - normalizedDistance * normalizedDistance *
                                      normalizedDistance * normalizedDistance, 0.0);
        float attenuation = (rangeWindow * rangeWindow) / max(distanceToLight * distanceToLight, 0.01);
        if (light.metadata.x == 2u) {
            float cone = dot(L, -normalize(light.directionOuterCos.xyz));
            attenuation *= smoothstep(light.directionOuterCos.w, light.shadowAndInnerCos.w, cone);
        }
        if (attenuation <= 0.0001) continue;

        vec3 radiance = light.colorIntensity.rgb * light.colorIntensity.w * attenuation;
        Lo += evaluatePBRLight(N, V, L, radiance, albedo, metallic,
                               roughness, perceptualRoughness,
                               F0, f90, energyCompensation);
    }
#else
    for (int i = 0; i < lighting.lightCounts.x && i < MAX_DIRECTIONAL_LIGHTS; ++i) {
        DirectionalLightData light = lighting.directionalLights[i];
        vec3 L        = normalize(-light.direction.xyz);
        vec3 radiance = light.color.rgb * light.color.w;
        float lightShadow = (i == 0) ? shadow : 1.0;
        Lo += evaluatePBRLight(N, V, L, radiance, albedo, metallic,
                               roughness, perceptualRoughness,
                               F0, f90, energyCompensation) * lightShadow;
    }

    // Point lights
    for (int i = 0; i < lighting.lightCounts.y && i < MAX_POINT_LIGHTS; ++i) {
        PointLightData light = lighting.pointLights[i];
        vec3  lightVec = light.position.xyz - worldPos;
        float distance = length(lightVec);
        if (distance > 1e-5) {
            vec3  L = lightVec / distance;
            float attenuation = calculateAttenuation(light.attenuation.xyz, distance);
            if (attenuation > 0.001) {
                vec3 radiance = light.color.rgb * light.color.w * attenuation;
                Lo += evaluatePBRLight(N, V, L, radiance, albedo, metallic,
                                       roughness, perceptualRoughness,
                                       F0, f90, energyCompensation);
            }
        }
    }

    // Spot lights
    for (int i = 0; i < lighting.lightCounts.z && i < MAX_SPOT_LIGHTS; ++i) {
        SpotLightData light = lighting.spotLights[i];
        vec3  lightVec = light.position.xyz - worldPos;
        float distance = length(lightVec);
        if (distance > 1e-5) {
            vec3 L = lightVec / distance;
            float spotFalloff = calculateSpotFalloff(L, light.direction.xyz,
                                                      light.spotParams.x, light.spotParams.y);
            if (spotFalloff > 0.0) {
                float attenuation = calculateAttenuation(light.attenuation.xyz, distance);
                if (attenuation > 0.001) {
                    vec3 radiance = light.color.rgb * light.color.w * attenuation * spotFalloff;
                    Lo += evaluatePBRLight(N, V, L, radiance, albedo, metallic,
                                           roughness, perceptualRoughness,
                                           F0, f90, energyCompensation);
                }
            }
        }
    }
#endif

    return Lo;
}
