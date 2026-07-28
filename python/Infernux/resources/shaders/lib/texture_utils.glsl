ShaderInfo {
    Name "Lib Texture Utils"
}

// ============================================================================
// lib/texture_utils.glsl — Texture sampling utilities
//
// Full-coverage texture toolkit matching Unity ShaderGraph Input/Texture category.
// Provides: normal blending, detail texture, height blend, unpack, LOD sampling,
// gradient sampling, cube reflection, texture bombing, channel packing, and more.
// Usage: ShaderInfo Imports: Lib Texture Utils
// ============================================================================

// ============================================================================
// Normal Blending  (Unity: Normal Blend)
// ============================================================================

// Reoriented Normal Mapping (RNM) — high quality  (Unity: Normal Blend — Reoriented)
vec3 blendNormalsRNM(vec3 base, vec3 detail) {
    vec3 t = base + vec3(0.0, 0.0, 1.0);
    vec3 u = detail * vec3(-1.0, -1.0, 1.0);
    return normalize(t * dot(t, u) - u * t.z);
}

// Linear blend of tangent-space normals  (Unity: Normal Blend — Default)
vec3 blendNormalsLinear(vec3 base, vec3 detail) {
    return normalize(vec3(base.xy + detail.xy, base.z));
}

// Whiteout blending (similar to RNM, common in terrain)
vec3 blendNormalsWhiteout(vec3 base, vec3 detail) {
    return normalize(vec3(base.xy + detail.xy, base.z * detail.z));
}

// ============================================================================
// Unpack / Decode  (Unity: Normal Unpack)
// ============================================================================

// Unpack two-channel normal (RG only, reconstruct Z)  (Unity: Normal Reconstruct Z)
vec3 unpackNormalRG(vec2 rg) {
    vec3 n;
    n.xy = rg * 2.0 - 1.0;
    n.z = sqrt(max(1.0 - dot(n.xy, n.xy), 0.0));
    return n;
}

// Decode normal from normal map texture sample [0,1] -> [-1,1]  (Unity: Normal Unpack)
vec3 unpackNormal(vec4 normalSample) {
    return unpackNormalRG(normalSample.rg);
}

// Decode normal with adjustable scale  (Unity: Normal Strength)
vec3 unpackNormalScale(vec4 normalSample, float scale) {
    vec3 n = unpackNormalRG(normalSample.rg);
    n.xy *= scale;
    return normalize(n);
}

// ============================================================================
// Detail / Blending  (Unity: Blend modes for textures)
// ============================================================================

// Detail texture blending (overlay-style)
vec3 detailBlend(vec3 baseColor, vec3 detailColor, float strength) {
    vec3 result = mix(
        2.0 * baseColor * detailColor,
        1.0 - 2.0 * (1.0 - baseColor) * (1.0 - detailColor),
        step(0.5, baseColor));
    return mix(baseColor, result, strength);
}

// Heightmap-based texture blending (for terrain-like layering)
float heightBlend(float h1, float h2, float blend, float contrast) {
    float height1 = h1 + (1.0 - blend);
    float height2 = h2 + blend;
    float maxH = max(height1, height2) - contrast;
    float b1 = max(height1 - maxH, 0.0);
    float b2 = max(height2 - maxH, 0.0);
    return b2 / (b1 + b2 + 1e-6);
}

// ============================================================================
// LOD / Gradient Sampling  (Unity: Sample Texture 2D LOD / Gradient)
// ============================================================================

// Sample texture at specific mip level  (Unity: Sample Texture 2D LOD)
vec4 sampleLOD(sampler2D tex, vec2 uv, float lod) {
    return textureLod(tex, uv, lod);
}

// Sample texture with explicit gradients  (Unity: Sample Texture 2D Gradient)
vec4 sampleGrad(sampler2D tex, vec2 uv, vec2 ddxUV, vec2 ddyUV) {
    return textureGrad(tex, uv, ddxUV, ddyUV);
}

// ============================================================================
// Cube Map  (Unity: Sample Cubemap, Reflection Probe)
// ============================================================================

// Sample cube map with reflection vector  (Unity: Sample Cubemap)
vec4 sampleCubeReflection(samplerCube cubeMap, vec3 normal, vec3 viewDir) {
    vec3 reflDir = reflect(-viewDir, normal);
    return texture(cubeMap, reflDir);
}

// Sample cube map at specific LOD (for roughness-based reflections)
vec4 sampleCubeReflectionLOD(samplerCube cubeMap, vec3 normal, vec3 viewDir, float lod) {
    vec3 reflDir = reflect(-viewDir, normal);
    return textureLod(cubeMap, reflDir, lod);
}

// ============================================================================
// Channel Operations  (Unity: Split / Combine / Swizzle)
// ============================================================================

// Split RGBA channels  (Unity: Split)
void splitRGBA(vec4 color, out float r, out float g, out float b, out float a) {
    r = color.r;
    g = color.g;
    b = color.b;
    a = color.a;
}

// Combine channels into vec4  (Unity: Combine)
vec4 combineRGBA(float r, float g, float b, float a) {
    return vec4(r, g, b, a);
}

// Swizzle — generic channel remap via index (0=R, 1=G, 2=B, 3=A)
float channelSelect(vec4 color, int channel) {
    if (channel == 0) return color.r;
    if (channel == 1) return color.g;
    if (channel == 2) return color.b;
    return color.a;
}

// ============================================================================
// Texture Bombing  (random tile offset to break repetition)
// ============================================================================

// Texture bombing — offsets UV per tile to break repetition
vec4 textureBombing(sampler2D tex, vec2 uv, float randomness) {
    vec2 cell = floor(uv);
    vec2 local = fract(uv);
    // Hash-based random offset per cell
    vec2 h = fract(sin(vec2(
        dot(cell, vec2(127.1, 311.7)),
        dot(cell, vec2(269.5, 183.3))
    )) * 43758.5453);
    vec2 offset = (h - 0.5) * randomness;
    return texture(tex, local + offset);
}

// ============================================================================
// Misc  (Unity: Texel Size, Gather)
// ============================================================================

// Compute texel size from texture dimensions  (Unity: Texel Size)
vec2 texelSize(vec2 textureSize) {
    return 1.0 / textureSize;
}

// Bilinear sharpening (for pixel-art upscaling)
vec4 sampleSharp(sampler2D tex, vec2 uv, vec2 textureSize) {
    vec2 pixel = uv * textureSize + 0.5;
    vec2 frac_ = fract(pixel);
    vec2 texel = (floor(pixel) - 0.5) / textureSize;
    vec2 sharp = clamp(frac_ / fwidth(pixel), 0.0, 0.5)
               + clamp((frac_ - 1.0) / fwidth(pixel) + 0.5, 0.0, 0.5);
    return texture(tex, texel + sharp / textureSize);
}
