#version 450

ShaderInfo {
    Name "Particle Six-Way Smoke"
    ShadingModel "SixWaySmoke"
    Surface Transparent
    Queue 3000
    Blend Premultiplied
    DepthWrite Off
    Cull None
    CastShadows Off
    Properties {
        Color baseColor = [0.66, 0.66, 0.66, 1.0]
        Color emissionColor = [1.0, 0.28, 0.04, 1.0] HDR
        Float lightingIntensity = 1.0 Range(0.0, 8.0)
        Float ambientIntensity = 0.0 Range(0.0, 0.002)
        Float ambientSaturation = 0.0 Range(0.0, 1.0)
        Float emissionIntensity = 0.0 Range(0.0, 32.0)
        Float absorption = 0.5 Range(0.0, 1.0)
        Float alphaScale = 1.0 Range(0.0, 4.0)
        Float flipbookColumns = 8.0 Range(1.0, 32.0)
        Float flipbookRows = 8.0 Range(1.0, 32.0)
        Float flipbookFrameJitter = 3.0 Range(0.0, 16.0)
        Float flipbookFrameOffset = 0.0 Range(0.0, 1024.0)
        Float fadeInFraction = 0.08 Range(0.0, 0.5)
        Float fadeOutStart = 0.68 Range(0.5, 1.0)
        Float densityClipThreshold = 0.025 Range(0.0, 0.5)
        Texture2D positiveAxesMap = white
        Texture2D negativeAxesMap = black
    }
}

vec3 _inxSixWayPositive = vec3(0.0);
vec3 _inxSixWayNegative = vec3(0.0);

float inxParticleHash(uint value) {
    value ^= value >> 16u;
    value *= 0x7feb352du;
    value ^= value >> 15u;
    value *= 0x846ca68bu;
    value ^= value >> 16u;
    return float(value) * (1.0 / 4294967295.0);
}

vec2 inxFlipbookUv(vec2 uv, float frame) {
    vec2 grid = max(vec2(material.flipbookColumns, material.flipbookRows), vec2(1.0));
    float frameCount = grid.x * grid.y;
    frame = clamp(frame, 0.0, frameCount - 1.0);
    vec2 cell = vec2(mod(frame, grid.x), floor(frame / grid.x));
    vec2 textureExtent = max(vec2(textureSize(positiveAxesMap, 0)), vec2(1.0));
    vec2 localInset = 0.5 * grid / textureExtent;
    vec2 safeUv = clamp(uv, localInset, vec2(1.0) - localInset);
    return (safeUv + cell) / grid;
}

void surface(out SurfaceData s) {
    s = InitSurfaceData();

    vec2 grid = max(vec2(material.flipbookColumns, material.flipbookRows), vec2(1.0));
    float frameCount = grid.x * grid.y;
    float frameJitter = (inxParticleHash(v_ParticleId) - 0.5) * material.flipbookFrameJitter;
    float framePosition = clamp(
        v_ParticleNormalizedAge * (frameCount - 1.0) + material.flipbookFrameOffset + frameJitter,
        0.0,
        frameCount - 1.0);
    float firstFrame = floor(framePosition);
    float secondFrame = min(firstFrame + 1.0, frameCount - 1.0);
    float frameBlend = fract(framePosition);
    vec2 atlasGradientX = dFdx(v_TexCoord) / grid;
    vec2 atlasGradientY = dFdy(v_TexCoord) / grid;
    vec4 positiveAxes = mix(
        textureGrad(positiveAxesMap, inxFlipbookUv(v_TexCoord, firstFrame), atlasGradientX, atlasGradientY),
        textureGrad(positiveAxesMap, inxFlipbookUv(v_TexCoord, secondFrame), atlasGradientX, atlasGradientY),
        frameBlend);
    // The positive six-way map stores opacity directly in A. Absorption only
    // changes how colored light traverses the smoke; applying it here would
    // darken opacity a second time when cards overlap.
    float opacity = max(positiveAxes.a, 0.0) * material.alphaScale;
    float fadeIn = smoothstep(0.0, max(material.fadeInFraction, 0.0001), v_ParticleNormalizedAge);
    float fadeOut = 1.0 - smoothstep(material.fadeOutStart, 1.0, v_ParticleNormalizedAge);
    float lifetimeFade = fadeIn * fadeOut;
    float densityAlpha = opacity * lifetimeFade;
    if (densityAlpha < material.densityClipThreshold) discard;
    vec4 negativeAxes = mix(
        textureGrad(negativeAxesMap, inxFlipbookUv(v_TexCoord, firstFrame), atlasGradientX, atlasGradientY),
        textureGrad(negativeAxesMap, inxFlipbookUv(v_TexCoord, secondFrame), atlasGradientX, atlasGradientY),
        frameBlend);
    const float inversePi = 0.31830988618;
    // The six-way maps are authored premultiplied by their own density. The
    // material and lifetime fades are additional coverage and therefore must
    // attenuate RGB as well as output alpha.
    float dynamicCoverage = clamp(
        material.alphaScale * lifetimeFade * material.baseColor.a,
        0.0,
        1.0);
    _inxSixWayPositive = max(positiveAxes.rgb, vec3(0.0)) * inversePi * dynamicCoverage;
    _inxSixWayNegative = max(negativeAxes.rgb, vec3(0.0)) * inversePi * dynamicCoverage;

    s.albedo = material.baseColor.rgb * v_Color;
    s.emission = material.emissionColor.rgb * negativeAxes.a * material.emissionIntensity;
    s.alpha = clamp(densityAlpha * material.baseColor.a, 0.0, 1.0);
    s.normalWS = normalize(v_Normal);
}
