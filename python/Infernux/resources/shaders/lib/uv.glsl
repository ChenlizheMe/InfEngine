@shader_id: Lib UV

// ============================================================================
// lib/uv.glsl — UV manipulation utilities
//
// Full-coverage UV toolkit matching Unity ShaderGraph UV category.
// Provides: tiling, rotation, flipbook, parallax, triplanar, polar,
// radial shear, spherize, twirl, and more.
// Usage: @import: Lib UV
// ============================================================================

// ============================================================================
// Basic UV Transforms  (Unity: Tiling And Offset, Rotate, Flipbook)
// ============================================================================

// Tiling And Offset  (Unity: Tiling And Offset)
vec2 tilingOffset(vec2 uv, vec2 tiling, vec2 offset) {
    return uv * tiling + offset;
}

// Rotate  (Unity: Rotate — Radians mode)
vec2 rotateUV(vec2 uv, float angle, vec2 center) {
    float s = sin(angle);
    float c = cos(angle);
    uv -= center;
    return vec2(uv.x * c - uv.y * s, uv.x * s + uv.y * c) + center;
}

// Rotate — Degrees mode  (Unity: Rotate — Degrees)
vec2 rotateUVDegrees(vec2 uv, float degrees, vec2 center) {
    return rotateUV(uv, radians(degrees), center);
}

// Flipbook  (Unity: Flipbook)
vec2 flipbookUV(vec2 uv, float cols, float rows, float frame) {
    float totalFrames = cols * rows;
    float idx = mod(floor(frame), totalFrames);
    float col = mod(idx, cols);
    float row = floor(idx / cols);
    vec2 size = vec2(1.0 / cols, 1.0 / rows);
    return vec2(col, row) * size + uv * size;
}

// Flipbook with invert Y  (Unity: Flipbook — Invert Y)
vec2 flipbookUVInvertY(vec2 uv, float cols, float rows, float frame) {
    float totalFrames = cols * rows;
    float idx = mod(floor(frame), totalFrames);
    float col = mod(idx, cols);
    float row = rows - 1.0 - floor(idx / cols);
    vec2 size = vec2(1.0 / cols, 1.0 / rows);
    return vec2(col, row) * size + uv * size;
}

// ============================================================================
// Distortion  (Unity: Twirl, Spherize, Radial Shear)
// ============================================================================

// Twirl  (Unity: Twirl)
vec2 twirlUV(vec2 uv, vec2 center, float strength, vec2 offset) {
    vec2 delta = uv - center;
    float dist = length(delta);
    float angle = strength * dist;
    float s = sin(angle);
    float c = cos(angle);
    return vec2(delta.x * c - delta.y * s, delta.x * s + delta.y * c) + center + offset;
}

// Spherize  (Unity: Spherize)
vec2 spherizeUV(vec2 uv, vec2 center, float strength, vec2 offset) {
    vec2 delta = uv - center;
    float dist = length(delta);
    float r = dist * 2.0;
    float phi = (1.0 - sqrt(max(1.0 - r * r, 0.0))) / max(r, 1e-6);
    return mix(delta, normalize(delta + 1e-6) * phi, strength) + center + offset;
}

// Radial Shear  (Unity: Radial Shear)
vec2 radialShearUV(vec2 uv, vec2 center, float strengthX, float strengthY, vec2 offset) {
    vec2 delta = uv - center;
    float dist = length(delta);
    return uv + delta * vec2(strengthX, strengthY) * dist + offset;
}

// ============================================================================
// Parallax  (Unity: Parallax Offset, Parallax Occlusion Mapping)
// ============================================================================

// Simple parallax offset  (Unity: Parallax Offset)
vec2 parallaxOffset(vec2 uv, vec3 viewDirTangent, float heightMap, float scale) {
    float h = heightMap * scale - scale * 0.5;
    float viewZ = (viewDirTangent.z < 0.0 ? -1.0 : 1.0) * max(abs(viewDirTangent.z), 1e-4);
    return uv + viewDirTangent.xy / viewZ * h;
}

// Parallax Occlusion Mapping  (Unity: Parallax Occlusion Mapping)
vec2 parallaxOcclusionMapping(sampler2D heightTex, vec2 uv, vec3 viewDirTangent,
                               float heightScale, int numLayers) {
    int layerCount = max(numLayers, 1);
    float layerDepth = 1.0 / float(layerCount);
    float currentLayerDepth = 0.0;
    float viewZ = (viewDirTangent.z < 0.0 ? -1.0 : 1.0) * max(abs(viewDirTangent.z), 1e-4);
    vec2 deltaUV = viewDirTangent.xy / viewZ * heightScale / float(layerCount);
    vec2 currentUV = uv;
    float currentHeight = texture(heightTex, currentUV).r;

    for (int i = 0; i < layerCount; ++i) {
        if (currentLayerDepth >= currentHeight) break;
        currentUV -= deltaUV;
        currentHeight = texture(heightTex, currentUV).r;
        currentLayerDepth += layerDepth;
    }

    vec2 prevUV = currentUV + deltaUV;
    float afterDepth = currentHeight - currentLayerDepth;
    float beforeDepth = texture(heightTex, prevUV).r - currentLayerDepth + layerDepth;
    float denom = afterDepth - beforeDepth;
    denom = (denom < 0.0 ? -1.0 : 1.0) * max(abs(denom), 1e-4);
    float weight = afterDepth / denom;
    return mix(currentUV, prevUV, weight);
}

// ============================================================================
// Projection  (Unity: Triplanar, Polar Coordinates)
// ============================================================================

// Triplanar blending weights  (Unity: Triplanar — weight computation)
vec3 triplanarWeights(vec3 worldNormal, float sharpness) {
    vec3 w = pow(abs(worldNormal), vec3(sharpness));
    return w / max(w.x + w.y + w.z, 1e-5);
}

// Triplanar sampling  (Unity: Triplanar)
vec4 triplanarSample(sampler2D tex, vec3 worldPos, vec3 worldNormal, float tiling, float sharpness) {
    vec3 w = triplanarWeights(worldNormal, sharpness);
    vec4 x = texture(tex, worldPos.yz * tiling);
    vec4 y = texture(tex, worldPos.xz * tiling);
    vec4 z = texture(tex, worldPos.xy * tiling);
    return x * w.x + y * w.y + z * w.z;
}

// Triplanar normal mapping  (project + blend normals per axis)
vec3 triplanarNormal(sampler2D normalMap, vec3 worldPos, vec3 worldNormal, float tiling, float sharpness) {
    vec3 w = triplanarWeights(worldNormal, sharpness);
    vec2 nxy = texture(normalMap, worldPos.yz * tiling).rg * 2.0 - 1.0;
    vec2 nyx = texture(normalMap, worldPos.xz * tiling).rg * 2.0 - 1.0;
    vec2 nzx = texture(normalMap, worldPos.xy * tiling).rg * 2.0 - 1.0;
    vec3 nx = vec3(nxy, sqrt(max(1.0 - dot(nxy, nxy), 0.0)));
    vec3 ny = vec3(nyx, sqrt(max(1.0 - dot(nyx, nyx), 0.0)));
    vec3 nz = vec3(nzx, sqrt(max(1.0 - dot(nzx, nzx), 0.0)));
    vec3 tnx = vec3(0.0, nx.y, nx.x);
    vec3 tny = vec3(ny.x, 0.0, ny.y);
    vec3 tnz = vec3(nz.x, nz.y, 0.0);
    return normalize(tnx * w.x + tny * w.y + tnz * w.z + worldNormal);
}

// Polar Coordinates  (Unity: Polar Coordinates)
vec2 polarUV(vec2 uv, vec2 center) {
    vec2 d = uv - center;
    float r = length(d);
    float angle = atan(d.y, d.x);
    return vec2(angle / 6.28318530718 + 0.5, r);
}

// Polar Coordinates with radial and length scale  (Unity: Polar Coordinates)
vec2 polarUVScaled(vec2 uv, vec2 center, float radialScale, float lengthScale) {
    vec2 d = uv - center;
    float r = length(d) * lengthScale;
    float angle = atan(d.y, d.x) * radialScale;
    return vec2(angle / 6.28318530718 + 0.5, r);
}

// ============================================================================
// Scrolling / Animated UV
// ============================================================================

// Scrolling UV (requires _Time via lib/camera or _Globals UBO)
vec2 scrollUV(vec2 uv, vec2 speed, float time) {
    return uv + speed * time;
}

// Panning UV (same as scroll but with tiling)
vec2 panUV(vec2 uv, vec2 tiling, vec2 speed, float time) {
    return uv * tiling + speed * time;
}
