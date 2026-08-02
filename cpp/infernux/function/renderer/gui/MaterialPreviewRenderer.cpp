#include "MaterialPreviewRenderer.h"
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <algorithm>
#include <cmath>
#include <fstream>
#include <mutex>
#include <optional>
#include <regex>

namespace infernux
{

// ============================================================================
// Helpers
// ============================================================================

static constexpr float PI = 3.14159265358979323846f;

static inline float clamp01(float x)
{
    return std::max(0.0f, std::min(1.0f, x));
}

static inline glm::vec3 clampVec(const glm::vec3 &v)
{
    return {clamp01(v.x), clamp01(v.y), clamp01(v.z)};
}

/// GGX / Trowbridge-Reitz normal distribution.
static float DistributionGGX(float NdotH, float roughness)
{
    float a = roughness * roughness;
    float a2 = a * a;
    float denom = NdotH * NdotH * (a2 - 1.0f) + 1.0f;
    return a2 / (PI * denom * denom + 1e-7f);
}

/// Schlick-GGX geometry for a single direction.
static float GeometrySchlickGGX(float NdotV, float roughness)
{
    float r = roughness + 1.0f;
    float k = (r * r) / 8.0f;
    return NdotV / (NdotV * (1.0f - k) + k + 1e-7f);
}

/// Smith's method combining view and light geometry.
static float GeometrySmith(float NdotV, float NdotL, float roughness)
{
    return GeometrySchlickGGX(NdotV, roughness) * GeometrySchlickGGX(NdotL, roughness);
}

/// Schlick approximation for Fresnel.
static glm::vec3 FresnelSchlick(float cosTheta, const glm::vec3 &F0)
{
    float t = 1.0f - cosTheta;
    float t2 = t * t;
    float t5 = t2 * t2 * t;
    return F0 + (glm::vec3(1.0f) - F0) * t5;
}

/// Linear → sRGB gamma curve for a single channel.
static float LinearToSRGB(float x)
{
    if (x <= 0.0031308f)
        return x * 12.92f;
    return 1.055f * std::pow(x, 1.0f / 2.4f) - 0.055f;
}

/// sRGB → linear for a single channel (decode texture values).
static float SRGBToLinear(float x)
{
    if (x <= 0.04045f)
        return x / 12.92f;
    return std::pow((x + 0.055f) / 1.055f, 2.4f);
}

/// Compute equirectangular UV from a unit sphere normal.
static inline void SphereUV(const glm::vec3 &n, float &u, float &v)
{
    u = 0.5f + std::atan2(n.z, n.x) / (2.0f * PI);
    v = 0.5f - std::asin(clamp01(std::max(-1.0f, std::min(1.0f, n.y)))) / PI;
}

// ============================================================================
// PreviewTexture
// ============================================================================

glm::vec4 PreviewTexture::Sample(float u, float v) const
{
    if (!IsValid())
        return glm::vec4(1.0f);

    // Wrap UVs
    u = u - std::floor(u);
    v = v - std::floor(v);

    float fx = u * (width - 1);
    float fy = v * (height - 1);

    int x0 = std::clamp(static_cast<int>(fx), 0, width - 1);
    int y0 = std::clamp(static_cast<int>(fy), 0, height - 1);
    int x1 = std::min(x0 + 1, width - 1);
    int y1 = std::min(y0 + 1, height - 1);

    float sx = fx - static_cast<float>(x0);
    float sy = fy - static_cast<float>(y0);

    auto fetch = [&](int px, int py) -> glm::vec4 {
        size_t idx = (static_cast<size_t>(py) * width + px) * 4;
        return glm::vec4(pixels[idx] / 255.0f, pixels[idx + 1] / 255.0f, pixels[idx + 2] / 255.0f,
                         pixels[idx + 3] / 255.0f);
    };

    glm::vec4 c00 = fetch(x0, y0);
    glm::vec4 c10 = fetch(x1, y0);
    glm::vec4 c01 = fetch(x0, y1);
    glm::vec4 c11 = fetch(x1, y1);

    return glm::mix(glm::mix(c00, c10, sx), glm::mix(c01, c11, sx), sy);
}

// ============================================================================
// ShaderPreviewMapping — parsing surface() to extract PBR slot mappings
// ============================================================================

/// Extract the body of `void surface(out SurfaceData s) { ... }` from shader source.
static std::string ExtractSurfaceBody(const std::string &source)
{
    auto pos = source.find("void surface(");
    if (pos == std::string::npos)
        return {};

    auto braceStart = source.find('{', pos);
    if (braceStart == std::string::npos)
        return {};

    int depth = 1;
    size_t i = braceStart + 1;
    while (i < source.size() && depth > 0) {
        if (source[i] == '{')
            ++depth;
        else if (source[i] == '}')
            --depth;
        ++i;
    }
    return source.substr(braceStart + 1, i - braceStart - 2);
}

ShaderPreviewMapping ParseShaderPreviewMapping(const std::string &shaderSource)
{
    ShaderPreviewMapping mapping;

    std::string body = ExtractSurfaceBody(shaderSource);
    if (body.empty())
        return mapping;

    // --- Parse s.FIELD = ... lines ---
    // We match: s.fieldName = expression;
    // Then extract `material.propName` and `sampleXxx(texName` from the expression.

    static const std::regex reLine(R"(s\.(\w+)\s*=\s*([^;]+);)");
    static const std::regex reMaterialProp(R"(material\.(\w+))");
    // sampleAlbedoAlpha(x), sampleGrayscale(x), sampleNormal(x, ...), texture(x, ...)
    static const std::regex reTextureSample(
        R"((?:sampleAlbedoAlpha|sampleGrayscale|sampleNormal|texture)\s*\(\s*(\w+))");

    auto bodyBegin = std::sregex_iterator(body.begin(), body.end(), reLine);
    auto bodyEnd = std::sregex_iterator();

    // Map surface field names to our canonical slot names
    auto canonicalSlot = [](const std::string &field) -> std::string {
        if (field == "albedo")
            return "albedo";
        if (field == "metallic")
            return "metallic";
        if (field == "smoothness")
            return "smoothness";
        if (field == "occlusion")
            return "occlusion";
        if (field == "normalWS")
            return "normal";
        if (field == "emission")
            return "emission";
        if (field == "alpha")
            return "alpha";
        if (field == "specularHighlights")
            return "specularHighlights";
        return field; // pass through unknown fields
    };

    for (auto it = bodyBegin; it != bodyEnd; ++it) {
        std::string field = (*it)[1].str();
        std::string expr = (*it)[2].str();
        std::string slot = canonicalSlot(field);

        SurfaceSlotMapping slotMapping;

        // Find all material.XXX references
        std::vector<std::string> materialProps;
        {
            auto mBegin = std::sregex_iterator(expr.begin(), expr.end(), reMaterialProp);
            auto mEnd = std::sregex_iterator();
            for (auto m = mBegin; m != mEnd; ++m)
                materialProps.push_back((*m)[1].str());
        }

        // Find texture sampler reference
        {
            std::smatch texMatch;
            if (std::regex_search(expr, texMatch, reTextureSample))
                slotMapping.textureProp = texMatch[1].str();
        }

        // For normal slot: sampleNormal(texName, material.scaleParam)
        // The scale param is the second material.XXX on the same line
        if (slot == "normal") {
            if (!materialProps.empty())
                slotMapping.scaleProp = materialProps[0]; // normalScale
            // Texture already captured above
        } else if (slot == "emission") {
            // emission = material.emissionColor.rgb * material.emissionColor.a
            // Only one unique property name
            if (!materialProps.empty())
                slotMapping.scalarProp = materialProps[0];
        } else {
            // For most slots: scalar is the material.XXX reference
            if (!materialProps.empty())
                slotMapping.scalarProp = materialProps[0];
        }

        mapping.slots[slot] = std::move(slotMapping);
    }

    return mapping;
}

// Cache: shader ID → parsed mapping
static std::mutex s_mappingCacheMutex;
static std::unordered_map<std::string, ShaderPreviewMapping> s_mappingCache;

ShaderPreviewMapping GetShaderPreviewMapping(const std::string &fragShaderId, AssetDatabase *adb)
{
    if (!adb || fragShaderId.empty())
        return {};

    {
        std::lock_guard lock(s_mappingCacheMutex);
        auto it = s_mappingCache.find(fragShaderId);
        if (it != s_mappingCache.end())
            return it->second;
    }

    // Resolve shader ID → file path
    std::string shaderPath = adb->FindShaderPathById(fragShaderId, "fragment");
    if (shaderPath.empty())
        return {};

    // Read shader source
    std::ifstream file(shaderPath);
    if (!file.is_open())
        return {};
    std::string source((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    file.close();

    ShaderPreviewMapping mapping = ParseShaderPreviewMapping(source);

    {
        std::lock_guard lock(s_mappingCacheMutex);
        s_mappingCache[fragShaderId] = mapping;
    }

    return mapping;
}

// ============================================================================
// RenderPreview (from material, with optional texture resolver and mapping)
// ============================================================================

/// Helper: try to read a float property from material by name.
static std::optional<float> TryGetFloat(const InxMaterial &material, const std::string &name)
{
    const auto *prop = material.GetProperty(name);
    if (prop && prop->type == MaterialPropertyType::Float)
        return std::get<float>(prop->value);
    return std::nullopt;
}

/// Helper: try to read a color property as vec3 from material by name.
static bool TryGetColor(const InxMaterial &material, const std::string &name, glm::vec3 &out)
{
    const auto *prop = material.GetProperty(name);
    if (!prop)
        return false;
    if (prop->type == MaterialPropertyType::Color || prop->type == MaterialPropertyType::Float4) {
        const auto &v = std::get<glm::vec4>(prop->value);
        out = glm::vec3(v.x, v.y, v.z);
        return true;
    }
    if (prop->type == MaterialPropertyType::Float3) {
        out = std::get<glm::vec3>(prop->value);
        return true;
    }
    return false;
}

/// Helper: try to resolve a texture property by name.
static PreviewTexture TryResolveTexture(const InxMaterial &material, const std::string &name,
                                        const PreviewTextureResolver &resolver)
{
    if (!resolver || name.empty())
        return {};
    const auto *prop = material.GetProperty(name);
    if (!prop || prop->type != MaterialPropertyType::Texture2D)
        return {};
    const auto *guid = std::get_if<std::string>(&prop->value);
    if (!guid || guid->empty())
        return {};
    return resolver(*guid);
}

void MaterialPreviewRenderer::RenderPreview(const InxMaterial &material, int size,
                                            std::vector<unsigned char> &outPixels,
                                            const PreviewTextureResolver &resolver, const ShaderPreviewMapping *mapping)
{
    PreviewMaterialParams params;

    PreviewTexture albedoTex, metallicTex, smoothnessTex, aoTex, normalTex;

    if (mapping && !mapping->IsEmpty()) {
        // ---- Dynamic mode: use parsed shader mapping ----

        // Albedo: color tint + texture
        if (auto it = mapping->slots.find("albedo"); it != mapping->slots.end()) {
            if (!it->second.scalarProp.empty())
                TryGetColor(material, it->second.scalarProp, params.baseColor);
            if (!it->second.textureProp.empty())
                albedoTex = TryResolveTexture(material, it->second.textureProp, resolver);
        }

        // Metallic
        if (auto it = mapping->slots.find("metallic"); it != mapping->slots.end()) {
            if (auto v = TryGetFloat(material, it->second.scalarProp))
                params.metallic = *v;
            if (!it->second.textureProp.empty())
                metallicTex = TryResolveTexture(material, it->second.textureProp, resolver);
        }

        // Smoothness → roughness
        if (auto it = mapping->slots.find("smoothness"); it != mapping->slots.end()) {
            if (auto v = TryGetFloat(material, it->second.scalarProp))
                params.roughness = 1.0f - *v;
            if (!it->second.textureProp.empty())
                smoothnessTex = TryResolveTexture(material, it->second.textureProp, resolver);
        }

        // Occlusion
        if (auto it = mapping->slots.find("occlusion"); it != mapping->slots.end()) {
            if (auto v = TryGetFloat(material, it->second.scalarProp))
                params.ambientOcclusion = *v;
            if (!it->second.textureProp.empty())
                aoTex = TryResolveTexture(material, it->second.textureProp, resolver);
        }

        // Normal
        if (auto it = mapping->slots.find("normal"); it != mapping->slots.end()) {
            if (auto v = TryGetFloat(material, it->second.scaleProp))
                params.normalScale = *v;
            if (!it->second.textureProp.empty())
                normalTex = TryResolveTexture(material, it->second.textureProp, resolver);
        }

        // Emission (HDR: rgb * alpha)
        if (auto it = mapping->slots.find("emission"); it != mapping->slots.end()) {
            if (!it->second.scalarProp.empty()) {
                const auto *prop = material.GetProperty(it->second.scalarProp);
                if (prop && (prop->type == MaterialPropertyType::Color || prop->type == MaterialPropertyType::Float4)) {
                    const auto &v = std::get<glm::vec4>(prop->value);
                    params.emissionColor = glm::vec3(v.x, v.y, v.z) * v.w;
                }
            }
        }

        // Specular highlights
        if (auto it = mapping->slots.find("specularHighlights"); it != mapping->slots.end()) {
            if (auto v = TryGetFloat(material, it->second.scalarProp))
                params.specularHighlights = *v;
        }

    } else {
        // No shader mapping available — use standard PBR property names.
        TryGetColor(material, "baseColor", params.baseColor);

        if (auto v = TryGetFloat(material, "metallic"))
            params.metallic = *v;

        if (auto v = TryGetFloat(material, "roughness"))
            params.roughness = *v;
        else if (auto v2 = TryGetFloat(material, "smoothness"))
            params.roughness = 1.0f - *v2;

        if (auto v = TryGetFloat(material, "ambientOcclusion"))
            params.ambientOcclusion = *v;

        if (auto v = TryGetFloat(material, "normalScale"))
            params.normalScale = *v;

        if (auto v = TryGetFloat(material, "specularHighlights"))
            params.specularHighlights = *v;

        {
            const auto *prop = material.GetProperty("emissionColor");
            if (prop && (prop->type == MaterialPropertyType::Color || prop->type == MaterialPropertyType::Float4)) {
                const auto &v = std::get<glm::vec4>(prop->value);
                params.emissionColor = glm::vec3(v.x, v.y, v.z) * v.w;
            }
        }

        albedoTex = TryResolveTexture(material, "baseColorTexture", resolver);
        metallicTex = TryResolveTexture(material, "metallicMap", resolver);
        smoothnessTex = TryResolveTexture(material, "smoothnessMap", resolver);
        aoTex = TryResolveTexture(material, "aoMap", resolver);
        normalTex = TryResolveTexture(material, "normalMap", resolver);
    }

    params.albedoTex = albedoTex.IsValid() ? &albedoTex : nullptr;
    params.metallicTex = metallicTex.IsValid() ? &metallicTex : nullptr;
    params.smoothnessTex = smoothnessTex.IsValid() ? &smoothnessTex : nullptr;
    params.aoTex = aoTex.IsValid() ? &aoTex : nullptr;
    params.normalTex = normalTex.IsValid() ? &normalTex : nullptr;

    RenderPreview(params, size, outPixels);
}

// ============================================================================
// RenderPreview (full, from PreviewMaterialParams)
// ============================================================================

void MaterialPreviewRenderer::RenderPreview(const PreviewMaterialParams &params, int size,
                                            std::vector<unsigned char> &outPixels)
{
    outPixels.resize(static_cast<size_t>(size) * size * 4, 0);

    float roughness = std::max(params.roughness, 0.04f);

    // Scene setup
    const glm::vec3 viewPos(0.0f, 0.0f, 2.5f);
    const glm::vec3 sphereCenter(0.0f, 0.0f, 0.0f);
    const float sphereRadius = 1.0f;

    // Single key light from above (matches the GPU preview rig — no lower
    // fill light, ambient keeps the dark side readable).
    const glm::vec3 lightDir0 = glm::normalize(glm::vec3(0.8f, 1.0f, 0.6f));
    const glm::vec3 lightColor0(1.0f, 0.95f, 0.9f);
    const float lightIntensity0 = 2.0f;

    const glm::vec3 ambientColor(0.03f, 0.03f, 0.04f);

    const float aaWidth = 1.5f / static_cast<float>(size);
    const float invSize = 1.0f / static_cast<float>(size);

    for (int y = 0; y < size; ++y) {
        float sv = 1.0f - (static_cast<float>(y) + 0.5f) * invSize;
        for (int x = 0; x < size; ++x) {
            float su = (static_cast<float>(x) + 0.5f) * invSize;

            float nx = su * 2.0f - 1.0f;
            float ny = sv * 2.0f - 1.0f;

            float dist2 = nx * nx + ny * ny;
            float dist = std::sqrt(dist2);
            if (dist > 1.0f + aaWidth)
                continue;

            float sphereDist = std::min(dist, 1.0f);
            float nz = std::sqrt(1.0f - sphereDist * sphereDist);
            glm::vec3 geometryNormal = glm::normalize(glm::vec3(nx, ny, nz));

            // Normal mapping: perturb geometric normal using tangent-space normal map
            glm::vec3 normal = geometryNormal;
            float texU, texV;
            SphereUV(geometryNormal, texU, texV);

            if (params.normalTex) {
                // Build TBN from sphere geometry
                // Tangent: derivative of sphere position w.r.t. azimuth angle
                glm::vec3 up(0.0f, 1.0f, 0.0f);
                glm::vec3 tangent = glm::normalize(glm::cross(up, geometryNormal));
                if (glm::length(tangent) < 0.001f)
                    tangent = glm::normalize(glm::cross(glm::vec3(0.0f, 0.0f, 1.0f), geometryNormal));
                glm::vec3 bitangent = glm::cross(geometryNormal, tangent);

                // Sample normal map: stored as [0,1], decode to [-1,1]
                glm::vec4 nSample = params.normalTex->Sample(texU, texV);
                glm::vec3 tangentNormal;
                tangentNormal.x = (nSample.x * 2.0f - 1.0f) * params.normalScale;
                tangentNormal.y = (nSample.y * 2.0f - 1.0f) * params.normalScale;
                // Reconstruct Z to keep unit length
                float zSq = 1.0f - tangentNormal.x * tangentNormal.x - tangentNormal.y * tangentNormal.y;
                tangentNormal.z = std::sqrt(std::max(zSq, 0.0f));

                normal = glm::normalize(tangent * tangentNormal.x + bitangent * tangentNormal.y +
                                        geometryNormal * tangentNormal.z);
            }

            glm::vec3 worldPos = sphereCenter + geometryNormal * sphereRadius;
            glm::vec3 V = glm::normalize(viewPos - worldPos);

            // Per-pixel material parameters
            glm::vec3 pixelBaseColor = params.baseColor;
            float pixelMetallic = params.metallic;
            float pixelRoughness = roughness;
            float pixelAO = params.ambientOcclusion;

            if (params.albedoTex) {
                glm::vec4 texSample = params.albedoTex->Sample(texU, texV);
                glm::vec3 linearTex(SRGBToLinear(texSample.x), SRGBToLinear(texSample.y), SRGBToLinear(texSample.z));
                pixelBaseColor *= linearTex;
            }
            if (params.metallicTex) {
                float texMetal = params.metallicTex->Sample(texU, texV).x;
                pixelMetallic *= texMetal;
            }
            if (params.smoothnessTex) {
                float texSmooth = params.smoothnessTex->Sample(texU, texV).x;
                pixelRoughness = 1.0f - ((1.0f - pixelRoughness) * texSmooth);
            }
            if (params.aoTex) {
                pixelAO *= params.aoTex->Sample(texU, texV).x;
            }

            pixelRoughness = std::max(pixelRoughness, 0.04f);

            // PBR
            glm::vec3 F0 = glm::mix(glm::vec3(0.04f), pixelBaseColor, pixelMetallic);
            glm::vec3 albedo = pixelBaseColor * (1.0f - pixelMetallic);

            glm::vec3 Lo(0.0f);

            auto addLight = [&](const glm::vec3 &L, const glm::vec3 &lightCol, float intensity) {
                float NdotL = std::max(glm::dot(normal, L), 0.0f);
                if (NdotL <= 0.0f)
                    return;
                glm::vec3 H = glm::normalize(V + L);
                float NdotV = std::max(glm::dot(normal, V), 0.001f);
                float NdotH = std::max(glm::dot(normal, H), 0.0f);
                float HdotV = std::max(glm::dot(H, V), 0.0f);

                float D = DistributionGGX(NdotH, pixelRoughness);
                float G = GeometrySmith(NdotV, NdotL, pixelRoughness);
                glm::vec3 F = FresnelSchlick(HdotV, F0);

                glm::vec3 specular = (D * G * F) / (4.0f * NdotV * NdotL + 0.001f);
                specular *= params.specularHighlights;
                glm::vec3 kD = (glm::vec3(1.0f) - F) * (1.0f - pixelMetallic);
                glm::vec3 diffuse = kD * albedo / PI;

                Lo += (diffuse + specular) * lightCol * intensity * NdotL;
            };

            addLight(lightDir0, lightColor0, lightIntensity0);

            float NdotV = std::max(glm::dot(normal, V), 0.001f);
            glm::vec3 Fa = FresnelSchlick(NdotV, F0);
            glm::vec3 ambient = ambientColor * ((glm::vec3(1.0f) - Fa) * albedo + Fa * 0.3f) * pixelAO;

            float rim = 1.0f - NdotV;
            rim = rim * rim * rim * 0.15f;

            glm::vec3 color = Lo + ambient + glm::vec3(rim) + params.emissionColor;

            // Tonemap + gamma
            color = color / (color + glm::vec3(1.0f));
            color = clampVec(color);

            float alpha = 1.0f;
            if (dist > 1.0f - aaWidth) {
                alpha = clamp01((1.0f + aaWidth - dist) / (2.0f * aaWidth));
            }

            size_t idx = (static_cast<size_t>(y) * size + x) * 4;
            outPixels[idx + 0] = static_cast<unsigned char>(LinearToSRGB(color.x) * 255.0f + 0.5f);
            outPixels[idx + 1] = static_cast<unsigned char>(LinearToSRGB(color.y) * 255.0f + 0.5f);
            outPixels[idx + 2] = static_cast<unsigned char>(LinearToSRGB(color.z) * 255.0f + 0.5f);
            outPixels[idx + 3] = static_cast<unsigned char>(alpha * 255.0f + 0.5f);
        }
    }
}

} // namespace infernux
