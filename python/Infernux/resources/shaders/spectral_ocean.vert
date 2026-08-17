#version 450

ShaderInfo {
    Name "Surfing/SpectralOceanVertex"
    Properties {
        Texture2D displacementAtlas = black
        Float waveHeight = 3.2 Range(0.0, 12.0)
        Float horizontalDisplacement = 1.8 Range(0.0, 6.0)
        Float playbackSpeed = 1.0 Range(0.0, 4.0)
        Float patchRepeat = 1.0 Range(0.25, 8.0)
        Float patchWorldSize = 256.0 Range(16.0, 2048.0)
    }
    Outputs {
        Smooth Float2 oceanUV Semantic(TexCoord7)
        Smooth Float crestFactor
        Smooth Float normalizedHeight
    }
}

const float OCEAN_GRID = 8.0;
const float OCEAN_INNER = 128.0;
const float OCEAN_BORDER = 4.0;
const float OCEAN_TILE = 136.0;
const float OCEAN_FRAMES = 64.0;
const float OCEAN_LOOP_SECONDS = 16.0;

vec2 oceanAtlasUV(vec2 uv, float frameIndex) {
    float frame = mod(frameIndex, OCEAN_FRAMES);
    vec2 tile = vec2(mod(frame, OCEAN_GRID), floor(frame / OCEAN_GRID));
    vec2 local = (vec2(OCEAN_BORDER) + fract(uv) * (OCEAN_INNER - 1.0) + 0.5) / OCEAN_TILE;
    return (tile + local) / OCEAN_GRID;
}

vec4 oceanSampleFrame(vec2 uv, float frameIndex) {
    return texture(displacementAtlas, oceanAtlasUV(uv, frameIndex));
}

vec4 oceanSample(vec2 uv, float timeSeconds) {
    float phase = mod(timeSeconds, OCEAN_LOOP_SECONDS) / OCEAN_LOOP_SECONDS * OCEAN_FRAMES;
    float firstFrame = floor(phase);
    float blend = smoothstep(0.0, 1.0, fract(phase));
    return mix(oceanSampleFrame(uv, firstFrame), oceanSampleFrame(uv, firstFrame + 1.0), blend);
}

VertexOutput vertex(inout VertexInput v) {
    VertexOutput result;
    vec2 uv = v.texCoord * material.patchRepeat;
    float timeSeconds = _Globals._Time.x * material.playbackSpeed;
    vec4 packed = oceanSample(uv, timeSeconds);
    vec3 displacement = packed.rgb * 2.0 - 1.0;

    v.position.x += displacement.x * material.horizontalDisplacement;
    v.position.y += displacement.y * material.waveHeight;
    v.position.z += displacement.z * material.horizontalDisplacement;

    float texel = 1.0 / OCEAN_INNER;
    float leftHeight = (oceanSample(uv - vec2(texel, 0.0), timeSeconds).g * 2.0 - 1.0) * material.waveHeight;
    float rightHeight = (oceanSample(uv + vec2(texel, 0.0), timeSeconds).g * 2.0 - 1.0) * material.waveHeight;
    float downHeight = (oceanSample(uv - vec2(0.0, texel), timeSeconds).g * 2.0 - 1.0) * material.waveHeight;
    float upHeight = (oceanSample(uv + vec2(0.0, texel), timeSeconds).g * 2.0 - 1.0) * material.waveHeight;
    float sampleSpacing = material.patchWorldSize /
        max(OCEAN_INNER * material.patchRepeat, 1.0);
    v.normal = normalize(vec3(
        leftHeight - rightHeight,
        2.0 * sampleSpacing,
        downHeight - upHeight));

    float slope = length(vec2(rightHeight - leftHeight, upHeight - downHeight)) /
        max(2.0 * sampleSpacing, 0.0001);
    float heightCrest = smoothstep(0.18, 0.82, displacement.y);
    float slopeCrest = smoothstep(0.06, 0.32, slope);
    // The atlas alpha channel is an intentionally conservative binary crest hint.
    // Keep it subordinate to continuous FFT height/slope data so it cannot create
    // hard foam islands after interpolation or texture-format changes.
    result.oceanUV = uv;
    result.crestFactor = clamp(
        heightCrest * (0.62 + slopeCrest * 0.38) + packed.a * 0.08,
        0.0,
        1.0);
    result.normalizedHeight = displacement.y;
    return result;
}
