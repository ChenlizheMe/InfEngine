@shader_id: lib/normal_utils

// ============================================================================
// lib/normal_utils.glsl — Normal mapping and tangent-space utilities
//
// Provides: height-to-normal, tangent-space transform, normal map sampling.
// Usage: @import: lib/normal_utils
//
// Available varyings (from fragment_varyings.glsl):
//   v_Normal    — interpolated world-space normal
//   v_Tangent   — world-space tangent (xyz) + bitangent sign (w)
// ============================================================================

// ---- TBN matrix construction ----

// Construct TBN matrix from world-space normal and tangent
// Returns mat3 that transforms tangent-space vectors to world-space
mat3 constructTBN(vec3 worldNormal, vec4 worldTangent) {
    vec3 N = normalize(worldNormal);
    vec3 T = normalize(worldTangent.xyz);
    vec3 B = cross(N, T) * worldTangent.w;
    return mat3(T, B, N);
}

// ---- Normal from tangent-space ----

// Transform a tangent-space normal to world-space using TBN
vec3 normalFromTangentSpace(vec3 tangentNormal, vec3 worldNormal, vec4 worldTangent) {
    mat3 TBN = constructTBN(worldNormal, worldTangent);
    return normalize(TBN * tangentNormal);
}

// Sample normal map and transform to world-space
// normalMap: standard tangent-space normal map ([0,1] encoded)
// scale: normal strength (1.0 = normal, <1 = flatter, >1 = sharper)
vec3 getNormalFromMap(sampler2D normalMap, vec2 uv, float scale,
                      vec3 worldNormal, vec4 worldTangent) {
    vec2 encodedXY = texture(normalMap, uv).rg * 2.0 - 1.0;
    vec3 tsNormal = vec3(encodedXY, sqrt(max(1.0 - dot(encodedXY, encodedXY), 0.0)));
    tsNormal.xy *= scale;
    tsNormal = normalize(tsNormal);
    return normalFromTangentSpace(tsNormal, worldNormal, worldTangent);
}

// ---- Normal from height map ----

// Generate normal from grayscale height map using central differences
// heightMap: single-channel height texture
// uv: current UV coordinates
// strength: bump strength multiplier (1.0 = standard)
// texelSize: 1.0 / textureSize (vec2)
vec3 normalFromHeight(sampler2D heightMap, vec2 uv, float strength, vec2 texelSize) {
    float hL = texture(heightMap, uv - vec2(texelSize.x, 0.0)).r;
    float hR = texture(heightMap, uv + vec2(texelSize.x, 0.0)).r;
    float hD = texture(heightMap, uv - vec2(0.0, texelSize.y)).r;
    float hU = texture(heightMap, uv + vec2(0.0, texelSize.y)).r;
    vec3 n = vec3(hL - hR, hD - hU, 2.0 / strength);
    return normalize(n);
}

// Generate world-space normal from height map, applying TBN
vec3 normalFromHeightWS(sampler2D heightMap, vec2 uv, float strength,
                        vec2 texelSize, vec3 worldNormal, vec4 worldTangent) {
    vec3 tsNormal = normalFromHeight(heightMap, uv, strength, texelSize);
    return normalFromTangentSpace(tsNormal, worldNormal, worldTangent);
}
