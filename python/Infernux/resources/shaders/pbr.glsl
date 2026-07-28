ShaderInfo {
    Name "PBR"
    Imports ["Math"]
}

// ============================================================================
// pbr.glsl — Cook-Torrance GGX BRDF (HDRP-aligned)
//
// Matches Unity HDRP Lit shader BRDF model:
//   - GGX/Trowbridge-Reitz NDF (D_GGX)
//   - Height-correlated Smith-GGX visibility (V_SmithJointGGX, Heitz 2014)
//   - Schlick Fresnel with energy-based f90
//   - Disney/Burley renormalized diffuse
//   - Multiscatter GGX energy compensation (Fdez-Agüera 2019)
//
// Convention: "roughness" = LINEAR roughness (perceptualRoughness²).
//             "perceptualRoughness" = 1 - smoothness (artist-facing).
//
// Ref: Unity Graphics — BSDF.hlsl, ImageBasedLighting.hlsl, Lit.hlsl
// Requires: math.glsl (PI, INV_PI, EPSILON, saturate)
// ============================================================================


// ============================================================================
// Fresnel (HDRP F_Schlick)
// ============================================================================

// Scalar Fresnel-Schlick — used by Disney Diffuse internally.
float F_Schlick(float f0, float f90, float u) {
    float x  = 1.0 - u;
    float x2 = x * x;
    float x5 = x * x2 * x2;
    return (f90 - f0) * x5 + f0;
}

// Vector Fresnel-Schlick with explicit f90 (HDRP convention).
vec3 F_Schlick(vec3 f0, float f90, float u) {
    float x  = 1.0 - u;
    float x2 = x * x;
    float x5 = x * x2 * x2;
    return f0 * (1.0 - x5) + (f90 * x5);
}

// Fresnel with roughness attenuation for indirect/environment lighting.
// At grazing angles on rough surfaces the Fresnel rim is softened.
vec3 F_SchlickRoughness(vec3 f0, float f90, float u, float perceptualRoughness) {
    float x  = 1.0 - u;
    float x2 = x * x;
    float x5 = x * x2 * x2;
    return f0 + (max(vec3(1.0 - perceptualRoughness), f0) - f0) * x5;
}

// Compute f90 from F0 following HDRP convention.
// For typical dielectrics (F0 ≈ 0.04): f90 = saturate(50·0.04) ≈ 1.0.
// For very dark/absorptive specular: f90 is reduced → no unrealistic bright
// Fresnel rim on materials like carbon, soot, etc.
float ComputeF90(vec3 F0) {
    return saturate(50.0 * dot(F0, vec3(0.33333)));
}

// ============================================================================
// GGX Normal Distribution Function (D)
// ============================================================================

// Matches HDRP D_GGX. Input: roughness = LINEAR roughness.
float D_GGX(float NdotH, float roughness) {
    float a2 = roughness * roughness;
    float s  = (NdotH * a2 - NdotH) * NdotH + 1.0;
    return INV_PI * a2 / max(s * s, FLT_MIN);
}

// ============================================================================
// Height-Correlated Smith-GGX Visibility (V = G / (4·NdotL·NdotV))
// ============================================================================

// Matches HDRP V_SmithJointGGX exactly (Heitz 2014).
// Input: roughness = LINEAR roughness.
float V_SmithJointGGX(float NdotL, float NdotV, float roughness) {
    float a2 = roughness * roughness;
    float lambdaV = NdotL * sqrt((-NdotV * a2 + NdotV) * NdotV + a2);
    float lambdaL = NdotV * sqrt((-NdotL * a2 + NdotL) * NdotL + a2);
    return 0.5 / max(lambdaV + lambdaL, FLT_MIN);
}

// Fused D_GGX · V_SmithJointGGX for better codegen (HDRP DV_SmithJointGGX).
// Returns the full specular BRDF factor (excluding F), already / PI.
// Input: roughness = LINEAR roughness.
float DV_SmithJointGGX(float NdotH, float NdotL, float NdotV, float roughness) {
    float a2 = roughness * roughness;
    float s  = (NdotH * a2 - NdotH) * NdotH + 1.0;

    float lambdaV = NdotL * sqrt((-NdotV * a2 + NdotV) * NdotV + a2);
    float lambdaL = NdotV * sqrt((-NdotL * a2 + NdotL) * NdotL + a2);

    return INV_PI * 0.5 * a2 / max(s * s * (lambdaV + lambdaL), FLT_MIN);
}

// ============================================================================
// Disney/Burley Renormalized Diffuse (HDRP DisneyDiffuse)
// ============================================================================

// Energy-conserving diffuse BRDF that accounts for roughness-dependent
// retroreflection and grazing-angle darkening.  Replaces Lambert / PI.
// Input: perceptualRoughness = 1 - smoothness (NOT squared).
// Returns diffuse BRDF value already divided by PI.
float DisneyDiffuse(float NdotV, float NdotL, float LdotV, float perceptualRoughness) {
    // fd90 = 0.5 + 2·LdotH²·perceptualRoughness
    // Identity: 2·LdotH² = 1 + LdotV  →  fd90 = 0.5 + pR·(1 + LdotV)
    float fd90 = 0.5 + (perceptualRoughness + perceptualRoughness * LdotV);
    float lightScatter = F_Schlick(1.0, fd90, NdotL);
    float viewScatter  = F_Schlick(1.0, fd90, NdotV);
    // HDRP normalization factor: rcp(1.03571) keeps total energy ≤ 1.
    return INV_PI * (1.0 / 1.03571) * lightScatter * viewScatter;
}

// ============================================================================
// Pre-Integrated FGD Approximation (Karis '13 analytical fit)
// ============================================================================

// Approximates the GGX BRDF LUT (pre-integrated FGD texture) analytically.
// Returns vec2(scale, bias): specularFGD = F0 · scale + bias.
// Input: perceptualRoughness (artist-facing, NOT squared).
vec2 EnvBRDFApprox(float perceptualRoughness, float NdotV) {
    vec4 c0 = vec4(-1.0, -0.0275, -0.572,  0.022);
    vec4 c1 = vec4( 1.0,  0.0425,  1.04,  -0.04);
    vec4 r  = perceptualRoughness * c0 + c1;
    float a004 = min(r.x * r.x, exp2(-9.28 * NdotV)) * r.x + r.y;
    return vec2(-1.04, 1.04) * a004 + r.zw;
}

// Specular occlusion: reduces indirect specular leaking where AO is low.
float ComputeSpecularOcclusion(float NdotV, float ao, float perceptualRoughness) {
    return saturate(pow(NdotV + ao, exp2(-16.0 * perceptualRoughness - 1.0)) - 1.0 + ao);
}

// Horizon occlusion for indirect specular (HDRP GetHorizonOcclusion).
// An analytic sky probe has no geometry occlusion, so when a normal-mapped
// reflection vector dips below the *geometric* surface plane it would still
// fetch full sky radiance and glow. Fade it out instead.
// geomNormal must be the interpolated vertex normal, not the shading normal
// (dot(R, N) with the shading normal is always >= 0 by construction).
float HorizonOcclusion(vec3 R, vec3 geomNormal) {
    float horizon = saturate(1.0 + dot(R, geomNormal));
    return horizon * horizon;
}

// ============================================================================
// Utility Functions
// ============================================================================

// URP / HDRP-style smooth distance attenuation for punctual lights.
// Guarantees smooth falloff to zero at range boundary.
// attenParams.x = range (yz unused)
float calculateAttenuation(vec3 attenParams, float distance) {
    float range = max(attenParams.x, 1e-4);
    float d2 = distance * distance;
    float r2 = range * range;
    float ratio2 = d2 / r2;
    float factor = saturate(1.0 - ratio2 * ratio2);
    return (factor * factor) / (d2 + 1.0);
}

// Geometric specular anti-aliasing (Tokuyoshi 2017 / Kaplanyan 2016 / HDRP).
// Widens the GGX lobe where screen-space normal derivatives are large,
// preventing sub-pixel specular artifacts on curved geometry and normal maps.
// Input/output: LINEAR roughness.
float GeometricSpecularAA(vec3 worldNormal, float roughness) {
    vec3 du = dFdx(worldNormal);
    vec3 dv = dFdy(worldNormal);
    float variance = 0.25 * (dot(du, du) + dot(dv, dv));
    float kernelRoughness = min(variance, 0.18);
    return max(roughness, sqrt(roughness * roughness + kernelRoughness));
}

// Spotlight cone falloff
float calculateSpotFalloff(vec3 lightDir, vec3 spotDir, float innerAngleCos, float outerAngleCos) {
    float spotLen2 = max(dot(spotDir, spotDir), 1e-8);
    float theta   = dot(lightDir, -spotDir * inversesqrt(spotLen2));
    float epsilon = max(innerAngleCos - outerAngleCos, 1e-4);
    return clamp((theta - outerAngleCos) / epsilon, 0.0, 1.0);
}

// ============================================================================
// Cook-Torrance BRDF evaluation for a single light (HDRP-aligned)
//
// Direct lighting path matches Unity HDRP Lit:
//   Specular = F_Schlick(F0, f90, LdotH) · DV_SmithJointGGX() · energyCompensation
//   Diffuse  = diffuseColor · DisneyDiffuse()
//   Result   = (diffuse + specular) · radiance · NdotL · π   (π = Unity light unit)
//
// HDRP does NOT multiply diffuse by (1−F) per-light; the metallic factor
// removes diffuse via diffuseColor = albedo·(1−metallic), and Disney diffuse
// already contains its own Fresnel-like energy redistribution.
// ============================================================================
vec3 evaluatePBRLight(vec3 N, vec3 V, vec3 L, vec3 lightRadiance,
                      vec3 albedo, float metallic,
                      float roughness, float perceptualRoughness,
                      vec3 F0, float f90, vec3 energyCompensation) {
    vec3  halfVec = V + L;
    float halfLen2 = dot(halfVec, halfVec);
    vec3  H     = (halfLen2 > 1e-8) ? halfVec * inversesqrt(halfLen2) : N;
    float NdotL = max(dot(N, L), 0.0);
    float NdotV = max(dot(N, V), 0.0);
    float NdotH = max(dot(N, H), 0.0);
    float LdotH = max(dot(L, H), 0.0);
    float LdotV = max(dot(L, V), 0.0);

    if (NdotL <= 0.0) return vec3(0.0);

    // ---- Specular: D · V · F (single GGX lobe, no secondary) ----
    vec3  F  = F_Schlick(F0, f90, LdotH);
    float DV = DV_SmithJointGGX(NdotH, NdotL, NdotV, roughness);
    vec3  specular = F * DV;

    // Multiscatter GGX energy compensation (Fdez-Agüera 2019 / HDRP).
    // Pre-computed in caller from EnvBRDFApprox:
    //   specularFGD = F0·envBrdf.x + envBrdf.y
    //   energyCompensation = 1 + F0 · (1/specularFGD − 1)
    specular *= energyCompensation;

    // ---- Diffuse: Disney/Burley (energy-conserving, roughness-aware) ----
    float diffuseTerm = DisneyDiffuse(NdotV, NdotL, LdotV, perceptualRoughness);
    vec3  diffuse = albedo * (1.0 - metallic) * diffuseTerm;

    // Photometric convention (Unity parity): Unity folds the BRDF's 1/π into
    // the light unit — an intensity-1 directional light on a white Lambert
    // surface yields albedo·NdotL, and URP's specular term is π-scaled to
    // match. Our BRDF above is radiometric (D_GGX and DisneyDiffuse both
    // carry 1/π), so scale the whole punctual-light response by π. Without
    // this, authored Unity-style intensities render π× too dim relative to
    // the ambient probe (which is irradiance-based and has no 1/π), making
    // environment lighting visually dominate the sun.
    return (diffuse + specular) * lightRadiance * (NdotL * PI);
}
