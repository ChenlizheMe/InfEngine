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

float sampleShadowView(uint viewIndex, vec3 worldPos, vec3 normal, vec3 toLight, vec4 shadowParams) {
    if (viewIndex >= lighting.shadowViewHeader.x || shadowParams.w < 0.5 || shadowParams.x <= 0.0) return 1.0;
    ShadowViewData shadowView = lighting.shadowViews[viewIndex];
    float worldTexel = max(shadowView.depthTexel.z, 0.000001);
    float slope = 1.0 - max(dot(normalize(normal), normalize(toLight)), 0.0);
    vec3 biasedPosition = worldPos + normalize(normal) * shadowParams.z;
    biasedPosition += normalize(toLight) * (shadowParams.y + slope * worldTexel);

    vec4 clip = shadowView.viewProjection * vec4(biasedPosition, 1.0);
    if (abs(clip.w) < 0.000001) return 1.0;
    vec3 ndc = clip.xyz / clip.w;
    vec2 localUv = ndc.xy * 0.5 + 0.5;
    if (any(lessThan(localUv, vec2(0.0))) || any(greaterThan(localUv, vec2(1.0))) ||
        ndc.z <= 0.0 || ndc.z >= 1.0) return 1.0;

    vec2 atlasScale = shadowView.atlasScaleOffset.xy;
    vec2 atlasOffset = shadowView.atlasScaleOffset.zw;
    vec2 atlasUv = atlasOffset + localUv * atlasScale;
    float atlasSize = max(float(lighting.shadowViewHeader.y), 1.0);
    vec2 texel = vec2(1.0 / atlasSize);
    vec2 tileMin = atlasOffset + texel * 0.5;
    vec2 tileMax = atlasOffset + atlasScale - texel * 0.5;

    int sampleCount = shadowParams.w > 1.5 ? 16 : 1;
    float visibility = 0.0;
    float rotation = interleavedGradientNoise(gl_FragCoord.xy) * 6.283185;
    float radius = max(shadowView.depthTexel.w, 0.0);
    for (int sampleIndex = 0; sampleIndex < 16; ++sampleIndex) {
        if (sampleIndex >= sampleCount) break;
        vec2 offset = sampleCount == 1 ? vec2(0.0)
                                       : vogelDiskSample(sampleIndex, sampleCount, rotation) * radius * texel;
        float storedDepth = texture(shadowMap, clamp(atlasUv + offset, tileMin, tileMax)).r;
        visibility += ndc.z <= storedDepth ? 1.0 : 0.0;
    }
    return mix(1.0, visibility / float(sampleCount), shadowParams.x);
}

float sampleDirectionalViews(uint firstView, uint viewCount, vec4 shadowParams,
                             vec3 worldPos, vec3 normal, vec3 toLight, float viewDepth) {
    uint availableViews = lighting.shadowViewHeader.x;
    if (viewCount == 0u || firstView >= availableViews) return 1.0;
    viewCount = min(viewCount, availableViews - firstView);
    uint selected = viewCount - 1u;
    for (uint index = 0u; index < viewCount; ++index) {
        if (viewDepth < lighting.shadowViews[firstView + index].splitData.y) {
            selected = index;
            break;
        }
    }
    float shadow = sampleShadowView(firstView + selected, worldPos, normal, toLight, shadowParams);
    if (selected + 1u < viewCount) {
        ShadowViewData current = lighting.shadowViews[firstView + selected];
        float overlap = max((current.splitData.y - current.splitData.x) * 0.1, 0.0001);
        float blend = smoothstep(0.0, overlap, current.splitData.y - viewDepth);
        float nextShadow = sampleShadowView(firstView + selected + 1u, worldPos, normal, toLight, shadowParams);
        shadow = mix(nextShadow, shadow, blend);
    }
    return shadow;
}

uint pointShadowFace(vec3 directionFromLight) {
    vec3 a = abs(directionFromLight);
    if (a.x >= a.y && a.x >= a.z) return directionFromLight.x >= 0.0 ? 0u : 1u;
    if (a.y >= a.z) return directionFromLight.y >= 0.0 ? 2u : 3u;
    return directionFromLight.z >= 0.0 ? 4u : 5u;
}

float sampleLocalShadow(uint firstView, uint viewCount, uint lightType, vec4 shadowParams,
                        vec3 lightPosition, vec3 worldPos, vec3 normal, vec3 toLight) {
    uint availableViews = lighting.shadowViewHeader.x;
    if (viewCount == 0u || firstView >= availableViews) return 1.0;
    viewCount = min(viewCount, availableViews - firstView);
    uint offset = lightType == 2u ? 0u : min(pointShadowFace(worldPos - lightPosition), viewCount - 1u);
    return sampleShadowView(firstView + offset, worldPos, normal, toLight, shadowParams);
}

/**
 * Calculate shadow factor with cascaded shadow maps.
 * fragViewDepthVal should be the fragment's view-space depth.
 * Returns: 1.0 = fully lit, 0.0 = fully shadowed
 */
float calculateShadow(vec3 worldPos, vec3 normal, float fragViewDepthVal) {
#ifdef INX_PARTICLE_FORWARD_PLUS
    if (!inxParticleReceivesShadows()) return 1.0;
#endif
    if (lighting.shadowViewHeader.x == 0u) return 1.0;
#ifdef INX_FORWARD_PLUS_PASS
    if (canonicalLightCountsAndGeneration.x == 0u) return 1.0;
    CanonicalLightData light = canonicalLights[0];
    return sampleDirectionalViews(light.identityAndShadow.z, light.identityAndShadow.w,
                                  vec4(light.shadowAndInnerCos.xyz, float(light.metadata.z)), worldPos, normal,
                                  normalize(-light.directionOuterCos.xyz), fragViewDepthVal);
#else
    if (lighting.lightCounts.x <= 0) return 1.0;
    DirectionalLightData light = lighting.directionalLights[0];
    return sampleDirectionalViews(light.metadata.z, light.metadata.w, light.shadowParams, worldPos, normal,
                                  normalize(-light.direction.xyz), fragViewDepthVal);
#endif
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

vec3 evaluateRectangleAreaLight(vec3 lightPosition, vec3 rayDirection, vec4 rightWidth, vec4 upHeight,
                                vec3 lightColor, float intensity, float range, bool twoSided,
                                uint firstShadowView, uint shadowViewCount, vec4 shadowParams,
                                vec3 worldPos, vec3 N, vec3 V, vec3 albedo, float metallic,
                                float roughness, float perceptualRoughness, vec3 F0, float f90,
                                vec3 energyCompensation) {
    vec3 fromLight = worldPos - lightPosition;
    float emitterFacing = dot(normalize(rayDirection), normalize(fromLight));
    emitterFacing = twoSided ? abs(emitterFacing) : max(emitterFacing, 0.0);
    if (emitterFacing <= 0.0001) return vec3(0.0);

    vec3 right = normalize(rightWidth.xyz) * rightWidth.w * 0.5;
    vec3 up = normalize(upHeight.xyz) * upHeight.w * 0.5;
    vec3 samples[4] = vec3[4](lightPosition - right - up, lightPosition + right - up,
                              lightPosition + right + up, lightPosition - right + up);
    vec3 result = vec3(0.0);
    vec3 centerToLight = normalize(lightPosition - worldPos);
    float shadow = sampleLocalShadow(firstShadowView, shadowViewCount, 3u, shadowParams,
                                     lightPosition, worldPos, N, centerToLight);
    for (int sampleIndex = 0; sampleIndex < 4; ++sampleIndex) {
        vec3 lightVector = samples[sampleIndex] - worldPos;
        float distanceToLight = length(lightVector);
        if (distanceToLight <= 0.00001 || distanceToLight >= range) continue;
        vec3 L = lightVector / distanceToLight;
        float attenuation = calculateAttenuation(vec3(range, 0.0, 0.0), distanceToLight);
        vec3 radiance = lightColor * intensity * attenuation * emitterFacing * shadow * 0.25;
        result += evaluatePBRLight(N, V, L, radiance, albedo, metallic, roughness,
                                   perceptualRoughness, F0, f90, energyCompensation);
    }
    return result;
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
                          float viewDepth, float shadow) {
    vec3 Lo = vec3(0.0);
#ifdef INX_PARTICLE_FORWARD_PLUS
    const uint currentLightDomain = 2u;
#else
    const uint currentLightDomain = 1u;
#endif

    // Directional lights
#ifdef INX_FORWARD_PLUS_PASS
    uint directionalCount = canonicalLightCountsAndGeneration.x;
    for (uint i = 0u; i < directionalCount; ++i) {
        CanonicalLightData light = canonicalLights[i];
        if ((light.metadata.w & currentLightDomain) == 0u) continue;
        if ((light.metadata.y & _inx_ObjectLayerMask) == 0u) continue;
        vec3 L = normalize(-light.directionOuterCos.xyz);
        vec3 radiance = light.colorIntensity.rgb * light.colorIntensity.w;
        float lightShadow =
            i == 0u ? shadow
                    : sampleDirectionalViews(
                          light.identityAndShadow.z, light.identityAndShadow.w,
                          vec4(light.shadowAndInnerCos.xyz, float(light.metadata.z)), worldPos, N, L, viewDepth);
        Lo += evaluatePBRLight(N, V, L, radiance, albedo, metallic,
                               roughness, perceptualRoughness,
                               F0, f90, energyCompensation) * lightShadow;
    }

    uvec4 tileHeader = inxForwardPlusTileHeader();
    for (uint wordEntry = 0u; wordEntry < tileHeader.y; ++wordEntry) {
        uint lightMask = forwardPlusTileMasks[tileHeader.x + wordEntry];
        while (lightMask != 0u) {
            uint bit = uint(findLSB(lightMask));
            uint localIndex = wordEntry * 32u + bit;
            lightMask &= lightMask - 1u;
            if (localIndex >= tileHeader.z) continue;
            CanonicalLightData light = canonicalLights[directionalCount + localIndex];
            if ((light.metadata.w & currentLightDomain) == 0u) continue;
            if ((light.metadata.y & _inx_ObjectLayerMask) == 0u) continue;
            if (light.metadata.x == 3u) {
                Lo += evaluateRectangleAreaLight(
                    light.positionRange.xyz, light.directionOuterCos.xyz, light.areaRightWidth,
                    light.areaUpHeight, light.colorIntensity.rgb, light.colorIntensity.w,
                    light.positionRange.w, light.directionOuterCos.w > 0.5, light.identityAndShadow.z,
                    light.identityAndShadow.w, vec4(light.shadowAndInnerCos.xyz, float(light.metadata.z)),
                    worldPos, N, V, albedo, metallic, roughness, perceptualRoughness, F0, f90,
                    energyCompensation);
                continue;
            }
            vec3 lightVec = light.positionRange.xyz - worldPos;
            float distanceToLight = length(lightVec);
            float range = max(light.positionRange.w, 0.0001);
            if (distanceToLight <= 0.00001 || distanceToLight >= range) continue;

            vec3 L = lightVec / distanceToLight;
            float attenuation = calculateAttenuation(vec3(range, 0.0, 0.0), distanceToLight);
            if (light.metadata.x == 2u) {
                float cone = dot(L, -normalize(light.directionOuterCos.xyz));
                attenuation *= smoothstep(light.directionOuterCos.w, light.shadowAndInnerCos.w, cone);
            }
            if (attenuation <= 0.0001) continue;

            float localShadow = sampleLocalShadow(
                light.identityAndShadow.z, light.identityAndShadow.w, light.metadata.x,
                vec4(light.shadowAndInnerCos.xyz, float(light.metadata.z)), light.positionRange.xyz,
                worldPos, N, L);
            vec3 radiance = light.colorIntensity.rgb * light.colorIntensity.w * attenuation * localShadow;
            Lo += evaluatePBRLight(N, V, L, radiance, albedo, metallic,
                                   roughness, perceptualRoughness,
                                   F0, f90, energyCompensation);
        }
    }
#else
    for (int i = 0; i < lighting.lightCounts.x && i < MAX_DIRECTIONAL_LIGHTS; ++i) {
        DirectionalLightData light = lighting.directionalLights[i];
        if ((light.metadata.y & currentLightDomain) == 0u) continue;
        if ((light.metadata.x & _inx_ObjectLayerMask) == 0u) continue;
        vec3 L        = normalize(-light.direction.xyz);
        vec3 radiance = light.color.rgb * light.color.w;
        float lightShadow =
            i == 0 ? shadow
                   : sampleDirectionalViews(light.metadata.z, light.metadata.w, light.shadowParams, worldPos, N, L,
                                            viewDepth);
        Lo += evaluatePBRLight(N, V, L, radiance, albedo, metallic,
                               roughness, perceptualRoughness,
                               F0, f90, energyCompensation) * lightShadow;
    }

    // Point lights
    for (int i = 0; i < lighting.lightCounts.y && i < MAX_POINT_LIGHTS; ++i) {
        PointLightData light = lighting.pointLights[i];
        if ((light.metadata.y & currentLightDomain) == 0u) continue;
        if ((light.metadata.x & _inx_ObjectLayerMask) == 0u) continue;
        vec3  lightVec = light.position.xyz - worldPos;
        float distance = length(lightVec);
        if (distance > 1e-5) {
            vec3  L = lightVec / distance;
            float attenuation = calculateAttenuation(light.attenuation.xyz, distance);
            if (attenuation > 0.001) {
                float localShadow = sampleLocalShadow(light.metadata.z, light.metadata.w, 1u, light.shadowParams,
                                                      light.position.xyz, worldPos, N, L);
                vec3 radiance = light.color.rgb * light.color.w * attenuation * localShadow;
                Lo += evaluatePBRLight(N, V, L, radiance, albedo, metallic,
                                       roughness, perceptualRoughness,
                                       F0, f90, energyCompensation);
            }
        }
    }

    // Spot lights
    for (int i = 0; i < lighting.lightCounts.z && i < MAX_SPOT_LIGHTS; ++i) {
        SpotLightData light = lighting.spotLights[i];
        if ((light.metadata.y & currentLightDomain) == 0u) continue;
        if ((light.metadata.x & _inx_ObjectLayerMask) == 0u) continue;
        vec3  lightVec = light.position.xyz - worldPos;
        float distance = length(lightVec);
        if (distance > 1e-5) {
            vec3 L = lightVec / distance;
            float spotFalloff = calculateSpotFalloff(L, light.direction.xyz,
                                                      light.spotParams.x, light.spotParams.y);
            if (spotFalloff > 0.0) {
                float attenuation = calculateAttenuation(light.attenuation.xyz, distance);
                if (attenuation > 0.001) {
                    float localShadow = sampleLocalShadow(light.metadata.z, light.metadata.w, 2u, light.shadowParams,
                                                          light.position.xyz, worldPos, N, L);
                    vec3 radiance = light.color.rgb * light.color.w * attenuation * spotFalloff * localShadow;
                    Lo += evaluatePBRLight(N, V, L, radiance, albedo, metallic,
                                           roughness, perceptualRoughness,
                                           F0, f90, energyCompensation);
                }
            }
        }
    }

    for (int i = 0; i < lighting.lightCounts.w && i < MAX_AREA_LIGHTS; ++i) {
        AreaLightData light = lighting.areaLights[i];
        if ((light.metadata.y & currentLightDomain) == 0u) continue;
        if ((light.metadata.x & _inx_ObjectLayerMask) == 0u) continue;
        Lo += evaluateRectangleAreaLight(
            light.positionRange.xyz, light.direction.xyz, light.rightWidth, light.upHeight,
            light.color.rgb, light.color.w, light.positionRange.w, light.direction.w > 0.5,
            light.metadata.z, light.metadata.w, light.shadowParams, worldPos, N, V, albedo,
            metallic, roughness, perceptualRoughness, F0, f90, energyCompensation);
    }
#endif

    return Lo;
}
