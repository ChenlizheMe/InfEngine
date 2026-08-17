#version 450

ShaderInfo {
    Name "Surfing/SpectralOceanSurface"
    ShadingModel PBR
    Surface Opaque
    Queue 2000
    Properties {
        Color deepColor = [0.005, 0.045, 0.11, 1.0] HDR
        Color shallowColor = [0.01, 0.30, 0.43, 1.0] HDR
        Color crestColor = [0.68, 0.92, 0.96, 1.0] HDR
        Color horizonColor = [0.16, 0.42, 0.62, 1.0] HDR
        Float metallic = 0.0
        Float smoothness = 0.94 Range(0.0, 1.0)
        Float foamStrength = 0.85 Range(0.0, 2.0)
        Float foamThreshold = 0.48 Range(0.0, 1.0)
        Float foamSharpness = 0.16 Range(0.01, 0.5)
        Float microNormalStrength = 0.72 Range(0.0, 2.0)
        Float fresnelStrength = 0.82 Range(0.0, 1.0)
        Float crestGlow = 0.12 Range(0.0, 1.0)
    }
    Inputs {
        Smooth Float2 oceanUV Semantic(TexCoord7)
        Smooth Float crestFactor
        Smooth Float normalizedHeight
    }
}

float oceanDetailWave(vec2 position, vec2 direction, float frequency, float phase) {
    return sin(dot(position, direction) * frequency + phase);
}

vec2 oceanDirectionalSlope(
    vec2 position,
    vec2 direction,
    float frequency,
    float speed,
    float phaseOffset,
    float amplitude,
    float timeSeconds) {
    vec2 normalizedDirection = normalize(direction);
    float phase = dot(position, normalizedDirection) * frequency +
        timeSeconds * speed + phaseOffset;
    return normalizedDirection * cos(phase) * amplitude;
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();

    vec3 worldPosition = getWorldPosition();
    float timeSeconds = getTime() * material.playbackSpeed;
    vec2 position = worldPosition.xz;
    vec2 detailSlope =
        oceanDirectionalSlope(position, vec2(0.93, 0.37), 0.17, 0.62, 0.4, 0.45, timeSeconds) +
        oceanDirectionalSlope(position, vec2(-0.28, 0.96), 0.29, -0.81, 1.7, 0.34, timeSeconds) +
        oceanDirectionalSlope(position, vec2(0.42, 0.91), 0.47, 1.16, 2.9, 0.25, timeSeconds) +
        oceanDirectionalSlope(position, vec2(0.98, -0.21), 0.73, 1.53, 4.1, 0.19, timeSeconds) +
        oceanDirectionalSlope(position, vec2(-0.61, 0.79), 1.11, -1.92, 0.8, 0.14, timeSeconds) +
        oceanDirectionalSlope(position, vec2(0.76, 0.65), 1.67, 2.37, 2.2, 0.10, timeSeconds) +
        oceanDirectionalSlope(position, vec2(-0.89, 0.46), 2.31, -2.84, 3.5, 0.07, timeSeconds) +
        oceanDirectionalSlope(position, vec2(0.19, 0.98), 3.13, 3.29, 5.0, 0.05, timeSeconds);
    float detailEnvelope = 0.78 + 0.22 * oceanDetailWave(
        position,
        normalize(vec2(0.67, -0.74)),
        0.047,
        timeSeconds * 0.21);
    detailSlope *= detailEnvelope;
    vec3 normalWS = normalize(
        getWorldNormal() - vec3(detailSlope.x, 0.0, detailSlope.y) *
            (0.085 * material.microNormalStrength));

    float heightBlend = smoothstep(-0.45, 0.8, fragmentInput.normalizedHeight);
    float foamVariation = clamp(
        0.50 +
        0.23 * oceanDetailWave(position, normalize(vec2(0.81, -0.59)), 0.38, timeSeconds * 0.64) +
        0.17 * oceanDetailWave(position, normalize(vec2(0.35, 0.94)), 0.71, -timeSeconds * 0.47) +
        0.10 * oceanDetailWave(position, normalize(vec2(-0.92, 0.39)), 1.21, timeSeconds * 0.33),
        0.0,
        1.0);
    float foamSignal = fragmentInput.crestFactor +
        max(fragmentInput.normalizedHeight, 0.0) * 0.12 +
        foamVariation * 0.10;
    float foam = smoothstep(
        material.foamThreshold,
        material.foamThreshold + max(material.foamSharpness, 0.01),
        foamSignal) * material.foamStrength;

    float ndotv = max(dot(normalWS, getViewDir()), 0.0);
    float fresnel = pow(1.0 - ndotv, 5.0) * material.fresnelStrength;
    vec3 water = mix(material.deepColor.rgb, material.shallowColor.rgb, heightBlend);
    water = mix(water, material.horizonColor.rgb, clamp(fresnel, 0.0, 0.88));
    s.albedo = mix(water, material.crestColor.rgb, clamp(foam, 0.0, 1.0));
    s.normalWS = normalWS;
    s.metallic = material.metallic;
    s.smoothness = clamp(material.smoothness - foam * 0.42, 0.0, 1.0);
    s.occlusion = 1.0;
    s.emission = material.crestColor.rgb * foam * material.crestGlow +
        material.horizonColor.rgb * fresnel * 0.025;
    s.alpha = 1.0;
    s.specularHighlights = 1.0;
}
