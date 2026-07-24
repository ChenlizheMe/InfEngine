@shader_id: Lib Surface Utils

@import: Lib Normal Utils
@import: Lib Camera
@import: Lib Common

// ============================================================================
// lib/surface_utils.glsl — Surface shader utility functions
//
// Auto-imported for all surface() shaders. Provides pre-built helpers
// for common surface() operations matching Unity ShaderGraph convenience.
//
// Available varyings (from fragment_varyings.glsl):
//   v_WorldPos   — world-space fragment position
//   v_Normal     — interpolated world-space normal
//   v_Tangent    — world-space tangent (w = bitangent sign)
//   v_Color      — vertex color
//   v_TexCoord   — primary UV coordinates
//   v_ViewDepth  — linear eye-space depth
//
// Available uniforms (auto-injected by engine):
//   material.<name>  — MaterialProperties UBO from @property declarations
//   _Globals.*       — Engine globals (time, screen, camera, etc.)
// ============================================================================

// ============================================================================
// Fragment Inputs — Quick access to interpolated vertex data
// (Unity: Position, Normal, Tangent, UV, Vertex Color nodes)
// ============================================================================

// World-space fragment position  (Unity: Position — World)
vec3 getWorldPosition() {
    return v_WorldPos;
}

// Interpolated world-space normal (normalized)  (Unity: Normal Vector — World)
vec3 getWorldNormal() {
    return normalize(v_Normal);
}

// Interpolated world-space tangent  (Unity: Tangent Vector — World)
vec4 getWorldTangent() {
    return v_Tangent;
}

// Bitangent vector  (Unity: Bitangent Vector — World)
vec3 getWorldBitangent() {
    vec3 N = normalize(v_Normal);
    vec3 T = normalize(v_Tangent.xyz);
    return cross(N, T) * v_Tangent.w;
}

// Vertex color (linear)  (Unity: Vertex Color)
vec3 getVertexColor() {
    return v_Color;
}

// Primary UV coordinates  (Unity: UV)
vec2 getUV() {
    return v_TexCoord;
}

// Linear eye-space depth of this fragment
float getViewDepth() {
    return v_ViewDepth;
}

// ============================================================================
// TBN / Tangent Space  (Unity: Transform node, Tangent space conversions)
// ============================================================================

// Construct TBN matrix (tangent → world)
mat3 getTBN() {
    vec3 N = normalize(v_Normal);
    vec3 T = normalize(v_Tangent.xyz);
    vec3 B = cross(N, T) * v_Tangent.w;
    return mat3(T, B, N);
}

// Transform direction from tangent space to world space
vec3 tangentToWorld(vec3 tangentDir) {
    return getTBN() * tangentDir;
}

// Transform direction from world space to tangent space
vec3 worldToTangent(vec3 worldDir) {
    return transpose(getTBN()) * worldDir;
}

// ============================================================================
// View & Camera — Direction, distance, Fresnel
// (Unity: View Direction, Fresnel Effect)
// ============================================================================

// Normalized view direction (from fragment toward camera)  (Unity: View Direction — World)
vec3 getViewDir() {
    return getViewDirection(v_WorldPos);
}

// View direction in tangent space (for parallax mapping etc.)
vec3 getViewDirTangent() {
    return worldToTangent(getViewDir());
}

// Distance from camera to fragment  (Unity: distance to camera)
float getCameraDistance() {
    return length(getCameraPosition() - v_WorldPos);
}

// NdotV (clamped, commonly needed for PBR)
float getNdotV() {
    return max(dot(getWorldNormal(), getViewDir()), 0.0);
}

// Basic Fresnel using surface normal (Schlick, F0 = 0.04)  (Unity: Fresnel Effect)
float getFresnel() {
    float NdotV = getNdotV();
    return 0.04 + 0.96 * pow(1.0 - NdotV, 5.0);
}

// Fresnel with custom F0  (Unity: Fresnel Effect — custom)
float getFresnelF0(float f0) {
    float NdotV = getNdotV();
    return f0 + (1.0 - f0) * pow(1.0 - NdotV, 5.0);
}

// Fresnel with custom power (stylized)  (Unity: Fresnel Effect — Power mode)
float getFresnelPower(float power) {
    return pow(1.0 - getNdotV(), power);
}

// ============================================================================
// Normal Mapping — Simplified one-call interface
// (Unity: Normal Map, Normal From Height, Normal Blend)
// ============================================================================

// Sample a normal map → world-space normal  (Unity: Normal Map / Sample Texture 2D Normal)
vec3 sampleNormal(sampler2D normalMap, vec2 uv, float scale) {
    return getNormalFromMap(normalMap, uv, scale, v_Normal, v_Tangent);
}

// Overload: uses primary UV
vec3 sampleNormal(sampler2D normalMap, float scale) {
    return getNormalFromMap(normalMap, v_TexCoord, scale, v_Normal, v_Tangent);
}

// Sample height map → world-space normal (bump mapping)  (Unity: Normal From Height)
vec3 sampleNormalFromHeight(sampler2D heightMap, vec2 uv, float strength, vec2 texelSize) {
    return normalFromHeightWS(heightMap, uv, strength, texelSize, v_Normal, v_Tangent);
}

// Blend two normal maps (both sampled at [0,1], RNM blend → world space)
vec3 blendNormalMaps(sampler2D mapA, sampler2D mapB, vec2 uv, float scaleA, float scaleB) {
    vec3 nA = texture(mapA, uv).rgb * 2.0 - 1.0;
    nA.xy *= scaleA;
    vec3 nB = texture(mapB, uv).rgb * 2.0 - 1.0;
    nB.xy *= scaleB;
    // RNM blend in tangent space
    vec3 t = nA + vec3(0.0, 0.0, 1.0);
    vec3 u = nB * vec3(-1.0, -1.0, 1.0);
    vec3 blended = normalize(t * dot(t, u) - u * t.z);
    mat3 TBN = constructTBN(v_Normal, v_Tangent);
    return normalize(TBN * blended);
}

// Detail normal map overlay (applies detail on top of base normal map)
vec3 sampleNormalWithDetail(sampler2D baseMap, sampler2D detailMap,
                             vec2 baseUV, vec2 detailUV,
                             float baseScale, float detailScale) {
    vec3 nBase = texture(baseMap, baseUV).rgb * 2.0 - 1.0;
    nBase.xy *= baseScale;
    vec3 nDetail = texture(detailMap, detailUV).rgb * 2.0 - 1.0;
    nDetail.xy *= detailScale;
    // Whiteout blend
    vec3 blended = normalize(vec3(nBase.xy + nDetail.xy, nBase.z * nDetail.z));
    mat3 TBN = constructTBN(v_Normal, v_Tangent);
    return normalize(TBN * blended);
}

// ============================================================================
// Texture Sampling — Convenience wrappers
// (Unity: Sample Texture 2D)
// ============================================================================

// Sample albedo/color at primary UV → linear RGB  (Unity: Sample Texture 2D)
vec3 sampleAlbedo(sampler2D tex) {
    return texture(tex, v_TexCoord).rgb;
}

// Sample albedo/color at primary UV → RGBA
vec4 sampleAlbedoAlpha(sampler2D tex) {
    return texture(tex, v_TexCoord);
}

// Sample albedo at custom UV
vec3 sampleAlbedo(sampler2D tex, vec2 uv) {
    return texture(tex, uv).rgb;
}

// Sample RGBA at custom UV
vec4 sampleAlbedoAlpha(sampler2D tex, vec2 uv) {
    return texture(tex, uv);
}

// Sample a single-channel map (metallic, smoothness, AO, height)
float sampleGrayscale(sampler2D tex) {
    return texture(tex, v_TexCoord).r;
}

// Sample single-channel at custom UV
float sampleGrayscale(sampler2D tex, vec2 uv) {
    return texture(tex, uv).r;
}

// Sample emission map at primary UV (linear HDR)
vec3 sampleEmission(sampler2D tex) {
    return texture(tex, v_TexCoord).rgb;
}

// Sample emission at custom UV
vec3 sampleEmission(sampler2D tex, vec2 uv) {
    return texture(tex, uv).rgb;
}

// ============================================================================
// ORM / Packed Texture Sampling  (Unity: common packed texture patterns)
// ============================================================================

// Sample ORM (Occlusion / Roughness / Metallic) packed texture
// Returns: x=AO(R), y=Roughness(G), z=Metallic(B)
vec3 sampleORM(sampler2D tex) {
    return texture(tex, v_TexCoord).rgb;
}

vec3 sampleORM(sampler2D tex, vec2 uv) {
    return texture(tex, uv).rgb;
}

// Unpack ORM into individual values
void unpackORM(sampler2D tex, vec2 uv, out float ao, out float roughness, out float metallic) {
    vec3 orm = texture(tex, uv).rgb;
    ao = orm.r;
    roughness = orm.g;
    metallic = orm.b;
}

// Sample metallic-smoothness packed texture (R=Metallic, A=Smoothness)
void sampleMetallicSmoothness(sampler2D tex, vec2 uv, out float metallic, out float smoothness) {
    vec4 s = texture(tex, uv);
    metallic = s.r;
    smoothness = s.a;
}

// ============================================================================
// Alpha Helpers  (Unity: Alpha Clip Threshold, Dithering)
// ============================================================================

// Alpha clip / cutout  (Unity: Alpha Clip Threshold)
float alphaClip(float alpha, float threshold) {
    if (alpha < threshold) discard;
    return alpha;
}

// Dithered transparency (screen-door effect, 4x4 Bayer matrix)
float ditherAlpha(float alpha, vec2 screenPos) {
    int x = int(mod(screenPos.x, 4.0));
    int y = int(mod(screenPos.y, 4.0));
    // 4x4 Bayer dither matrix (normalized)
    float bayer[16] = float[16](
         0.0/16.0,  8.0/16.0,  2.0/16.0, 10.0/16.0,
        12.0/16.0,  4.0/16.0, 14.0/16.0,  6.0/16.0,
         3.0/16.0, 11.0/16.0,  1.0/16.0,  9.0/16.0,
        15.0/16.0,  7.0/16.0, 13.0/16.0,  5.0/16.0
    );
    float threshold = bayer[y * 4 + x];
    if (alpha < threshold) discard;
    return 1.0;
}

// ============================================================================
// Depth Helpers  (fragment's own depth)
// ============================================================================

// Normalized depth [0, 1] (0 = near, 1 = far)
float getLinear01Depth() {
    return v_ViewDepth / getCameraFar();
}

// Eye-space depth range normalized to [0,1]
float getEyeDepthNormalized(float near, float far) {
    return clamp((v_ViewDepth - near) / (far - near), 0.0, 1.0);
}

// ============================================================================
// Screen Space  (Unity: Screen Position)
// ============================================================================

// Screen UV [0,1]  (Unity: Screen Position — Default)
vec2 getScreenPosition() {
    return gl_FragCoord.xy * _Globals._ScreenParams.zw;
}

// Pixel coordinates (integer)
ivec2 getPixelPosition() {
    return ivec2(gl_FragCoord.xy);
}

// ============================================================================
// Facing / Double-Sided  (Unity: Is Front Face)
// ============================================================================

// Check if fragment is front-facing
bool isFrontFace() {
    return gl_FrontFacing;
}

// Flip normal for back faces (for double-sided rendering)
vec3 getDoubleSidedNormal() {
    return gl_FrontFacing ? normalize(v_Normal) : -normalize(v_Normal);
}

// ============================================================================
// Reflection & Refraction  (Unity: Reflection, Refraction)
// ============================================================================

// Reflection vector  (Unity: Reflection)
vec3 getReflectionDir() {
    return reflect(-getViewDir(), getWorldNormal());
}

// Reflection vector from custom normal
vec3 getReflectionDir(vec3 normal) {
    return reflect(-getViewDir(), normal);
}

// Refraction direction (Snell's law)  (Unity: not directly exposed, but useful)
vec3 getRefractionDir(float ior) {
    return refract(-getViewDir(), getWorldNormal(), 1.0 / ior);
}

// Refraction with custom normal
vec3 getRefractionDir(vec3 normal, float ior) {
    return refract(-getViewDir(), normal, 1.0 / ior);
}

// ============================================================================
// Parallax — surface-level convenience  (Unity: Parallax Offset)
// ============================================================================

// Quick parallax offset UV from a height map sample
vec2 getParallaxUV(float heightSample, float scale) {
    vec3 V = getViewDirTangent();
    float h = heightSample * scale - scale * 0.5;
    return v_TexCoord + V.xy / V.z * h;
}

// ============================================================================
// Distance / LOD Helpers  (Unity: distance-based effects)
// ============================================================================

// Distance-based lerp (blend between near and far values)
float distanceLerp(float nearVal, float farVal, float nearDist, float farDist) {
    float d = getCameraDistance();
    return mix(nearVal, farVal, clamp((d - nearDist) / (farDist - nearDist), 0.0, 1.0));
}

// Distance-based fade (returns 0 at nearDist, 1 at farDist)
float distanceFade(float nearDist, float farDist) {
    return clamp((getCameraDistance() - nearDist) / (farDist - nearDist), 0.0, 1.0);
}

// ============================================================================
// Triplanar (surface-level convenience)
// ============================================================================

// Triplanar sample using fragment world pos + normal
vec4 sampleTriplanar(sampler2D tex, float tiling, float sharpness) {
    vec3 w = pow(abs(normalize(v_Normal)), vec3(sharpness));
    w /= (w.x + w.y + w.z);
    vec4 x = texture(tex, v_WorldPos.yz * tiling);
    vec4 y = texture(tex, v_WorldPos.xz * tiling);
    vec4 z = texture(tex, v_WorldPos.xy * tiling);
    return x * w.x + y * w.y + z * w.z;
}

// ============================================================================
// Rim / Edge Effects  (Unity: Fresnel as rim effect)
// ============================================================================

// Rim light mask (0 = facing camera, 1 = edge)
float getRimMask(float power) {
    return pow(1.0 - getNdotV(), power);
}

// Rim light with strength
float getRimLight(float power, float strength) {
    return getRimMask(power) * strength;
}

// ============================================================================
// Utility
// ============================================================================

// Object space — approximate object-space position.
// Note: For exact object-space coords, pass them via a custom varying in vertex().
// For procedural textures, consider using v_WorldPos directly.
