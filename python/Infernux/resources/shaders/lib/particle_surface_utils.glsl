@shader_id: Lib Particle Surface Utils

@import: Lib Normal Utils

// Surface helpers shared by billboard and mesh particle materials. Particle
// programs own their view state through ParticleViewConstants, so this library
// deliberately has no dependency on the geometry-domain InfGlobals UBO.

vec3 getWorldPosition() {
    return v_WorldPos;
}

vec3 getWorldNormal() {
    return normalize(v_Normal);
}

vec4 getWorldTangent() {
    return v_Tangent;
}

vec3 getWorldBitangent() {
    vec3 normal = normalize(v_Normal);
    vec3 tangent = normalize(v_Tangent.xyz);
    return cross(normal, tangent) * v_Tangent.w;
}

vec3 getVertexColor() {
    return v_Color;
}

vec2 getUV() {
    return v_TexCoord;
}

vec2 getParticleLocalUV() {
    return v_ParticleLocalTexCoord;
}

vec2 getParticleFlipbookNextUV() {
    return v_ParticleFlipbookNextTexCoord;
}

float getParticleFlipbookBlend() {
    return v_ParticleFlipbookBlend;
}

vec4 sampleParticleFlipbook(sampler2D textureSampler) {
    vec4 currentFrame = texture(textureSampler, v_TexCoord);
    vec4 nextFrame = texture(textureSampler, v_ParticleFlipbookNextTexCoord);
    return mix(currentFrame, nextFrame, v_ParticleFlipbookBlend);
}

float getViewDepth() {
    return v_ViewDepth;
}

mat3 getTBN() {
    return constructTBN(v_Normal, v_Tangent);
}

vec3 tangentToWorld(vec3 tangentDirection) {
    return getTBN() * tangentDirection;
}

vec3 worldToTangent(vec3 worldDirection) {
    return transpose(getTBN()) * worldDirection;
}

vec3 sampleNormal(sampler2D normalMap, vec2 uv, float scale) {
    return getNormalFromMap(normalMap, uv, scale, v_Normal, v_Tangent);
}

vec3 sampleNormal(sampler2D normalMap, float scale) {
    vec3 currentNormal = texture(normalMap, v_TexCoord).rgb * 2.0 - 1.0;
    vec3 nextNormal = texture(normalMap, v_ParticleFlipbookNextTexCoord).rgb * 2.0 - 1.0;
    vec3 tangentNormal = normalize(mix(currentNormal, nextNormal, v_ParticleFlipbookBlend));
    tangentNormal.xy *= scale;
    return normalize(getTBN() * normalize(tangentNormal));
}

vec3 sampleNormalFromHeight(sampler2D heightMap, vec2 uv, float strength, vec2 texelSize) {
    return normalFromHeightWS(heightMap, uv, strength, texelSize, v_Normal, v_Tangent);
}

vec3 blendNormalMaps(sampler2D mapA, sampler2D mapB, vec2 uv, float scaleA, float scaleB) {
    vec3 normalA = texture(mapA, uv).rgb * 2.0 - 1.0;
    normalA.xy *= scaleA;
    vec3 normalB = texture(mapB, uv).rgb * 2.0 - 1.0;
    normalB.xy *= scaleB;
    vec3 tangent = normalA + vec3(0.0, 0.0, 1.0);
    vec3 detail = normalB * vec3(-1.0, -1.0, 1.0);
    vec3 blended = normalize(tangent * dot(tangent, detail) - detail * tangent.z);
    return normalize(getTBN() * blended);
}

vec3 sampleNormalWithDetail(sampler2D baseMap, sampler2D detailMap,
                            vec2 baseUV, vec2 detailUV,
                            float baseScale, float detailScale) {
    vec3 baseNormal = texture(baseMap, baseUV).rgb * 2.0 - 1.0;
    baseNormal.xy *= baseScale;
    vec3 detailNormal = texture(detailMap, detailUV).rgb * 2.0 - 1.0;
    detailNormal.xy *= detailScale;
    vec3 blended = normalize(vec3(baseNormal.xy + detailNormal.xy,
                                  baseNormal.z * detailNormal.z));
    return normalize(getTBN() * blended);
}

vec3 sampleAlbedo(sampler2D textureSampler) {
    return sampleParticleFlipbook(textureSampler).rgb;
}

vec4 sampleAlbedoAlpha(sampler2D textureSampler) {
    return sampleParticleFlipbook(textureSampler);
}

vec3 sampleAlbedo(sampler2D textureSampler, vec2 uv) {
    return texture(textureSampler, uv).rgb;
}

vec4 sampleAlbedoAlpha(sampler2D textureSampler, vec2 uv) {
    return texture(textureSampler, uv);
}

float sampleGrayscale(sampler2D textureSampler) {
    return sampleParticleFlipbook(textureSampler).r;
}

float sampleGrayscale(sampler2D textureSampler, vec2 uv) {
    return texture(textureSampler, uv).r;
}

vec3 sampleEmission(sampler2D textureSampler) {
    return sampleParticleFlipbook(textureSampler).rgb;
}

vec3 sampleEmission(sampler2D textureSampler, vec2 uv) {
    return texture(textureSampler, uv).rgb;
}

vec3 sampleORM(sampler2D textureSampler) {
    return sampleParticleFlipbook(textureSampler).rgb;
}

vec3 sampleORM(sampler2D textureSampler, vec2 uv) {
    return texture(textureSampler, uv).rgb;
}

void unpackORM(sampler2D textureSampler, vec2 uv,
               out float ao, out float roughness, out float metallic) {
    vec3 packed = texture(textureSampler, uv).rgb;
    ao = packed.r;
    roughness = packed.g;
    metallic = packed.b;
}

void sampleMetallicSmoothness(sampler2D textureSampler, vec2 uv,
                              out float metallic, out float smoothness) {
    vec4 packed = texture(textureSampler, uv);
    metallic = packed.r;
    smoothness = packed.a;
}

float alphaClip(float alpha, float threshold) {
    if (alpha < threshold)
        discard;
    return alpha;
}

float ditherAlpha(float alpha, vec2 screenPosition) {
    int x = int(mod(screenPosition.x, 4.0));
    int y = int(mod(screenPosition.y, 4.0));
    float bayer[16] = float[16](
         0.0 / 16.0,  8.0 / 16.0,  2.0 / 16.0, 10.0 / 16.0,
        12.0 / 16.0,  4.0 / 16.0, 14.0 / 16.0,  6.0 / 16.0,
         3.0 / 16.0, 11.0 / 16.0,  1.0 / 16.0,  9.0 / 16.0,
        15.0 / 16.0,  7.0 / 16.0, 13.0 / 16.0,  5.0 / 16.0);
    if (alpha < bayer[y * 4 + x])
        discard;
    return 1.0;
}

vec2 getScreenPosition() {
    return gl_FragCoord.xy;
}

ivec2 getPixelPosition() {
    return ivec2(gl_FragCoord.xy);
}

bool isFrontFace() {
    return gl_FrontFacing;
}

vec3 getDoubleSidedNormal() {
    return gl_FrontFacing ? normalize(v_Normal) : -normalize(v_Normal);
}

vec4 sampleTriplanar(sampler2D textureSampler, float tiling, float sharpness) {
    vec3 weights = pow(abs(normalize(v_Normal)), vec3(sharpness));
    weights /= max(weights.x + weights.y + weights.z, 1e-6);
    vec4 x = texture(textureSampler, v_WorldPos.yz * tiling);
    vec4 y = texture(textureSampler, v_WorldPos.xz * tiling);
    vec4 z = texture(textureSampler, v_WorldPos.xy * tiling);
    return x * weights.x + y * weights.y + z * weights.z;
}
