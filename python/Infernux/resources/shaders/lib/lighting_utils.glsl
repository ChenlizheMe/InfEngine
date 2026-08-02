ShaderInfo {
    Name "Lib Lighting Utils"
}

// ============================================================================
// lib/lighting_utils.glsl — Lighting helper functions
//
// Full-coverage lighting toolkit matching Unity ShaderGraph Lighting category.
// Standalone lighting utilities for surface shaders.
// For full PBR pipeline: use ShaderInfo Imports: PBR instead.
// Usage: ShaderInfo Imports: Lib Lighting Utils
// ============================================================================

// ============================================================================
// Fresnel  (Unity: Fresnel Effect)
// ============================================================================

// Fresnel effect (Schlick approximation, single-channel)  (Unity: Fresnel Effect)
float fresnel(vec3 normal, vec3 viewDir, float power) {
    return pow(1.0 - max(dot(normal, viewDir), 0.0), power);
}

// Fresnel with F0 reflectance at normal incidence (vec3, for PBR metals)
vec3 fresnelSchlick(float cosTheta, vec3 F0) {
    return F0 + (1.0 - F0) * pow(clamp(1.0 - cosTheta, 0.0, 1.0), 5.0);
}

// Fresnel with roughness (for environment map reflections)
vec3 fresnelSchlickRoughness(float cosTheta, vec3 F0, float roughness) {
    return F0 + (max(vec3(1.0 - roughness), F0) - F0) *
           pow(clamp(1.0 - cosTheta, 0.0, 1.0), 5.0);
}

// F0 from IOR  (Unity: Dielectric Specular)
vec3 f0FromIOR(float ior) {
    float r = (ior - 1.0) / (ior + 1.0);
    return vec3(r * r);
}

// F0 from metallic + albedo  (Unity: PBR Master internal)
vec3 f0FromMetallic(vec3 albedo, float metallic) {
    return mix(vec3(0.04), albedo, metallic);
}

// ============================================================================
// Diffuse models  (Unity: Half Lambert, custom lighting)
// ============================================================================

// Lambert diffuse
float lambert(vec3 normal, vec3 lightDir) {
    return max(dot(normal, lightDir), 0.0);
}

// Half Lambert  (Unity: Half Lambert — wraps diffuse to avoid harsh terminator)
float halfLambert(vec3 normal, vec3 lightDir) {
    return dot(normal, lightDir) * 0.5 + 0.5;
}

// Quantized diffuse for cel/toon shading
float toonDiffuse(vec3 normal, vec3 lightDir, float bands) {
    float NdotL = dot(normal, lightDir) * 0.5 + 0.5;
    return floor(NdotL * bands) / bands;
}

// Oren-Nayar diffuse (rough diffuse, more realistic than Lambert)
float orenNayar(vec3 normal, vec3 lightDir, vec3 viewDir, float roughness) {
    float NdotL = dot(normal, lightDir);
    float NdotV = dot(normal, viewDir);
    float angleVN = acos(max(NdotV, 0.0));
    float angleLN = acos(max(NdotL, 0.0));
    float alpha = max(angleVN, angleLN);
    float beta  = min(angleVN, angleLN);
    float sigma2 = roughness * roughness;
    float A = 1.0 - 0.5 * sigma2 / (sigma2 + 0.33);
    float B = 0.45 * sigma2 / (sigma2 + 0.09);
    vec3 tanL = normalize(lightDir - normal * NdotL);
    vec3 tanV = normalize(viewDir - normal * NdotV);
    float gamma = max(0.0, dot(tanV, tanL));
    return max(NdotL, 0.0) * (A + B * gamma * sin(alpha) * tan(beta));
}

// ============================================================================
// Specular — GGX / Cook-Torrance components  (Unity: PBR internals)
// ============================================================================

// GGX Normal Distribution Function (Trowbridge-Reitz)
// Named ggxNDF to avoid collision with pbr.glsl's D_GGX (used by the engine PBR pipeline).
float ggxNDF(float NdotH, float roughness) {
    float a  = roughness * roughness;
    float a2 = a * a;
    float d  = NdotH * NdotH * (a2 - 1.0) + 1.0;
    return a2 / (3.14159265359 * d * d + 1e-7);
}

// GGX Geometry / Visibility — Smith joint approximation (height-correlated)
float V_SmithGGXCorrelated(float NdotV, float NdotL, float roughness) {
    float a2 = roughness * roughness;
    float ggxV = NdotL * sqrt(NdotV * NdotV * (1.0 - a2) + a2);
    float ggxL = NdotV * sqrt(NdotL * NdotL * (1.0 - a2) + a2);
    return 0.5 / (ggxV + ggxL + 1e-7);
}

// Schlick-GGX single-direction geometry term
float G_SchlickGGX(float NdotX, float roughness) {
    float r = roughness + 1.0;
    float k = (r * r) / 8.0;
    return NdotX / (NdotX * (1.0 - k) + k);
}

// Smith G term — product of two Schlick-GGX
float G_Smith(float NdotV, float NdotL, float roughness) {
    return G_SchlickGGX(NdotV, roughness) * G_SchlickGGX(NdotL, roughness);
}

// Full Cook-Torrance specular BRDF (D * G * F / denominator)
vec3 cookTorranceSpecular(vec3 N, vec3 V, vec3 L, vec3 F0, float roughness) {
    vec3 H = normalize(V + L);
    float NdotH = max(dot(N, H), 0.0);
    float NdotV = max(dot(N, V), 0.0);
    float NdotL = max(dot(N, L), 0.0);
    float HdotV = max(dot(H, V), 0.0);

    float D = ggxNDF(NdotH, roughness);
    float G = G_Smith(NdotV, NdotL, roughness);
    vec3  F = fresnelSchlick(HdotV, F0);

    return (D * G * F) / (4.0 * NdotV * NdotL + 1e-7);
}

// Blinn-Phong specular (simple, non-PBR)
float blinnPhongSpecular(vec3 normal, vec3 lightDir, vec3 viewDir, float shininess) {
    vec3 halfDir = normalize(lightDir + viewDir);
    return pow(max(dot(normal, halfDir), 0.0), shininess);
}

// Phong specular
float phongSpecular(vec3 normal, vec3 lightDir, vec3 viewDir, float shininess) {
    vec3 reflectDir = reflect(-lightDir, normal);
    return pow(max(dot(viewDir, reflectDir), 0.0), shininess);
}

// ============================================================================
// Environment / IBL helpers
// ============================================================================

// Approximate environment BRDF using Split-Sum (Karis 2013)
// Uses polynomial fit — no LUT texture needed
vec2 envBRDFApprox(float NdotV, float roughness) {
    vec4 c0 = vec4(-1.0, -0.0275, -0.572, 0.022);
    vec4 c1 = vec4( 1.0,  0.0425,  1.04, -0.04);
    vec4 r = roughness * c0 + c1;
    float a004 = min(r.x * r.x, exp2(-9.28 * NdotV)) * r.x + r.y;
    return vec2(-1.04, 1.04) * a004 + r.zw;
}

// Reflection direction with roughness mip level
// Returns reflection vector; use mipLevel = roughness * maxMip for cubemap LOD
vec3 reflectionVector(vec3 normal, vec3 viewDir) {
    return reflect(-viewDir, normal);
}

// ============================================================================
// Rim / Edge lighting  (Unity: custom, Fresnel-based rim)
// ============================================================================

// Rim light  (Unity: Fresnel Effect → used as rim)
float rimLight(vec3 normal, vec3 viewDir, float power, float strength) {
    float rim = 1.0 - max(dot(normal, viewDir), 0.0);
    return pow(rim, power) * strength;
}

// Rim light with color
vec3 rimLightColor(vec3 normal, vec3 viewDir, float power, float strength, vec3 color) {
    return rimLight(normal, viewDir, power, strength) * color;
}

// ============================================================================
// Attenuation  (Unity: light falloff)
// ============================================================================

// Point light distance attenuation (inverse square law)
float distanceAttenuation(float distance, float range) {
    float d = distance / range;
    float d2 = d * d;
    float factor = clamp(1.0 - d2 * d2, 0.0, 1.0);
    return factor * factor / (distance * distance + 1.0);
}

// Spot light angular attenuation
float spotAttenuation(vec3 lightDir, vec3 spotDir, float innerConeAngle, float outerConeAngle) {
    float cosAngle = dot(normalize(-lightDir), spotDir);
    return clamp((cosAngle - cos(outerConeAngle)) / (cos(innerConeAngle) - cos(outerConeAngle)), 0.0, 1.0);
}

// ============================================================================
// Subsurface Scattering approximation
// ============================================================================

// Fast subsurface scattering (wrap diffuse + back-lighting)
float subsurfaceScattering(vec3 normal, vec3 lightDir, vec3 viewDir,
                            float distortion, float power, float scale) {
    vec3 sssLightDir = lightDir + normal * distortion;
    float sss = pow(clamp(dot(viewDir, -sssLightDir), 0.0, 1.0), power) * scale;
    return sss;
}

// ============================================================================
// Ambient Occlusion helpers
// ============================================================================

// Multi-bounce AO approximation (Jimenez et al.)
vec3 multiBounceAO(float ao, vec3 albedo) {
    vec3 a = 2.0404 * albedo - 0.3324;
    vec3 b = -4.7951 * albedo + 0.6417;
    vec3 c = 2.7552 * albedo + 0.6903;
    return max(vec3(ao), ((ao * a + b) * ao + c) * ao);
}

// Specular occlusion from AO (Lagarde 2014)
float specularOcclusion(float NdotV, float ao, float roughness) {
    return clamp(pow(NdotV + ao, exp2(-16.0 * roughness - 1.0)) - 1.0 + ao, 0.0, 1.0);
}
