#include "InxMaterial.h"
#include "MaterialDocumentValidation.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <core/config/EngineConfig.h>
#include <core/log/InxLog.h>
#include <core/types/ShaderProgramArtifact.h>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetDependencyGraph.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <function/scene/MeshRenderer.h>
#include <functional>
#include <nlohmann/json.hpp>
#include <platform/filesystem/DocumentStore.h>
#include <platform/filesystem/InxPath.h>
#include <sstream>
#include <stdexcept>
#include <unordered_set>

using json = nlohmann::json;

namespace infernux
{

namespace
{

bool IsBuiltinTextureToken(const std::string &value)
{
    return value == "white" || value == "black" || value == "normal";
}

std::optional<MaterialPropertyType> ShaderMaterialPropertyType(const ShaderProgramPropertyBinding &binding)
{
    if (binding.IsTexture() || binding.type == "Texture2D")
        return MaterialPropertyType::Texture2D;
    if (binding.type == "Float")
        return MaterialPropertyType::Float;
    if (binding.type == "Float2")
        return MaterialPropertyType::Float2;
    if (binding.type == "Float3")
        return MaterialPropertyType::Float3;
    if (binding.type == "Float4")
        return MaterialPropertyType::Float4;
    if (binding.type == "Color")
        return MaterialPropertyType::Color;
    if (binding.type == "Int")
        return MaterialPropertyType::Int;
    if (binding.type == "Mat4")
        return MaterialPropertyType::Mat4;
    return std::nullopt;
}

bool MaterialValueMatchesType(const MaterialProperty &property, MaterialPropertyType type)
{
    if (property.type != type)
        return false;
    switch (type) {
    case MaterialPropertyType::Float:
        return std::holds_alternative<float>(property.value);
    case MaterialPropertyType::Float2:
        return std::holds_alternative<glm::vec2>(property.value);
    case MaterialPropertyType::Float3:
        return std::holds_alternative<glm::vec3>(property.value);
    case MaterialPropertyType::Float4:
    case MaterialPropertyType::Color:
        return std::holds_alternative<glm::vec4>(property.value);
    case MaterialPropertyType::Int:
        return std::holds_alternative<int>(property.value);
    case MaterialPropertyType::Mat4:
        return std::holds_alternative<glm::mat4>(property.value);
    case MaterialPropertyType::Texture2D:
        return std::holds_alternative<std::string>(property.value);
    }
    return false;
}

MaterialPropertyValue ShaderPropertyFallback(MaterialPropertyType type)
{
    switch (type) {
    case MaterialPropertyType::Float:
        return 0.0f;
    case MaterialPropertyType::Float2:
        return glm::vec2(0.0f);
    case MaterialPropertyType::Float3:
        return glm::vec3(0.0f);
    case MaterialPropertyType::Float4:
        return glm::vec4(0.0f);
    case MaterialPropertyType::Color:
        return glm::vec4(1.0f);
    case MaterialPropertyType::Int:
        return 0;
    case MaterialPropertyType::Mat4:
        return glm::mat4(1.0f);
    case MaterialPropertyType::Texture2D:
        return std::string{};
    }
    return 0.0f;
}

MaterialPropertyValue ParseShaderPropertyDefault(const ShaderProgramPropertyBinding &binding, MaterialPropertyType type)
{
    if (type == MaterialPropertyType::Texture2D || binding.defaultValue.empty())
        return ShaderPropertyFallback(type);

    const auto value = json::parse(binding.defaultValue, nullptr, false);
    if (value.is_discarded())
        return ShaderPropertyFallback(type);

    try {
        if (type == MaterialPropertyType::Float && value.is_number())
            return value.get<float>();
        if (type == MaterialPropertyType::Int && value.is_number_integer())
            return value.get<int>();

        const auto readVector = [&value](float *destination, size_t count) {
            if (!value.is_array() || value.size() != count)
                return false;
            for (size_t index = 0; index < count; ++index) {
                if (!value[index].is_number())
                    return false;
                destination[index] = value[index].get<float>();
            }
            return true;
        };
        if (type == MaterialPropertyType::Float2) {
            glm::vec2 result{};
            if (readVector(&result[0], 2))
                return result;
        } else if (type == MaterialPropertyType::Float3) {
            glm::vec3 result{};
            if (readVector(&result[0], 3))
                return result;
        } else if (type == MaterialPropertyType::Float4 || type == MaterialPropertyType::Color) {
            glm::vec4 result{};
            if (readVector(&result[0], 4))
                return result;
        } else if (type == MaterialPropertyType::Mat4) {
            glm::mat4 result{0.0f};
            if (readVector(&result[0][0], 16))
                return result;
        }
    } catch (const json::exception &) {
    }
    return ShaderPropertyFallback(type);
}

std::string RestoreTextureGuidReference(const std::string &textureGuid)
{
    if (textureGuid.empty() || IsBuiltinTextureToken(textureGuid))
        return textureGuid;

    auto *database = AssetRegistry::Instance().GetAssetDatabase();
    if (database == nullptr)
        return textureGuid;

    const auto metadata = database->GetMetaByGuid(textureGuid);
    if (!metadata)
        return textureGuid;
    if (metadata->GetResourceType() != ResourceType::Texture)
        throw std::invalid_argument("asset GUID is not a Texture: " + textureGuid);
    return textureGuid;
}

json SerializeShaderReference(const ShaderAssetReference &reference)
{
    return {
        {"guid", reference.guid},
        {"shader_id", reference.shaderId},
        {"path_hint", reference.pathHint},
    };
}

ShaderAssetReference DeserializeShaderReference(const json &document)
{
    return ShaderAssetReference{
        document["guid"].get<std::string>(),
        document["shader_id"].get<std::string>(),
        document["path_hint"].get<std::string>(),
    };
}

std::string ResolveEngineTextureGuid(const std::string &textureRef)
{
    if (IsBuiltinTextureToken(textureRef))
        return textureRef;
    auto *database = AssetRegistry::Instance().GetAssetDatabase();
    if (database == nullptr)
        throw std::logic_error("engine texture bootstrap requires an initialized AssetDatabase");

    namespace fs = std::filesystem;
    std::error_code error;
    const fs::path relative = ToFsPath(textureRef);
    for (const auto &searchPath : InxShaderLoader::GetShaderSearchPaths()) {
        fs::path current = ToFsPath(searchPath);
        for (int depth = 0; depth < 8 && !current.empty(); ++depth) {
            const fs::path candidate = current / relative;
            if (fs::exists(candidate, error) && !error) {
                const std::string resolved = ResolveFilesystemPath(FromFsPath(candidate));
                std::string guid = database->GetGuidFromPath(resolved);
                if (guid.empty())
                    guid = database->ImportAsset(resolved).guid;
                if (!guid.empty())
                    return guid;
            }
            current = current.parent_path();
        }
    }
    throw std::invalid_argument("engine texture cannot be resolved: " + textureRef);
}

std::shared_ptr<InxMaterial>
CreateTexturedComponentGizmoIconMaterial(const std::string &name, const std::string &textureRef, bool hardAlpha = false)
{
    auto material = std::make_shared<InxMaterial>(name);
    material->SetShader("Gizmo Icon");

    RenderState state;
    state.cullMode = MaterialCullMode::None;
    state.frontFace = MaterialFrontFace::Clockwise;
    state.topology = MaterialPrimitiveTopology::TriangleList;
    state.depthTestEnable = true;
    state.depthWriteEnable = false;
    state.depthCompareOp = MaterialCompareOp::LessOrEqual;
    state.blendEnable = true;
    state.srcColorBlendFactor = MaterialBlendFactor::SourceAlpha;
    state.dstColorBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
    state.colorBlendOp = MaterialBlendOp::Add;
    state.srcAlphaBlendFactor = MaterialBlendFactor::One;
    state.dstAlphaBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
    state.alphaBlendOp = MaterialBlendOp::Add;
    // Camera and particle icons keep their soft source edges.  The light
    // icon is authored as a binary white mask, so use a cutout there to
    // prevent bilinear sampling from creating a gray translucent fringe.
    state.alphaClipEnabled = hardAlpha;
    state.alphaClipThreshold = hardAlpha ? 0.5f : 0.0f;
    state.renderQueue = 24950;
    material->SetRenderState(state);
    material->SyncAlphaClipProperty();

    material->SetColor("baseColor", glm::vec4(1.0f));
    material->SetTextureGuid("texSampler", ResolveEngineTextureGuid(textureRef));
    material->SetBuiltin(true);

    return material;
}

MaterialCompareOp ParseDepthCompareOpString(const std::string &value, MaterialCompareOp fallback)
{
    if (value == "on" || value == "true" || value == "less")
        return MaterialCompareOp::Less;
    if (value == "less_equal")
        return MaterialCompareOp::LessOrEqual;
    if (value == "always")
        return MaterialCompareOp::Always;
    if (value == "never")
        return MaterialCompareOp::Never;
    if (value == "greater")
        return MaterialCompareOp::Greater;
    if (value == "greater_equal")
        return MaterialCompareOp::GreaterOrEqual;
    return fallback;
}

bool ApplyDepthTestMeta(RenderState &renderState, const std::string &depthTest, bool canEditDepthTest,
                        bool canEditDepthCompare)
{
    if (depthTest.empty() || !canEditDepthTest) {
        return false;
    }

    bool changed = false;
    if (depthTest == "off" || depthTest == "false") {
        if (renderState.depthTestEnable) {
            renderState.depthTestEnable = false;
            changed = true;
        }
        return changed;
    }

    if (!renderState.depthTestEnable) {
        renderState.depthTestEnable = true;
        changed = true;
    }
    if (!canEditDepthCompare) {
        return changed;
    }

    MaterialCompareOp newOp = ParseDepthCompareOpString(depthTest, renderState.depthCompareOp);
    if (newOp != renderState.depthCompareOp) {
        renderState.depthCompareOp = newOp;
        changed = true;
    }
    return changed;
}

bool ApplyBlendMeta(RenderState &renderState, const std::string &blend, bool canEditBlendEnable, bool canEditBlendMode)
{
    if (blend.empty() || !canEditBlendEnable) {
        return false;
    }

    if (blend == "off" || blend == "false") {
        if (!renderState.blendEnable) {
            return false;
        }
        renderState.blendEnable = false;
        return true;
    }

    if (!canEditBlendMode) {
        return false;
    }

    if (blend == "alpha") {
        renderState.blendEnable = true;
        renderState.srcColorBlendFactor = MaterialBlendFactor::SourceAlpha;
        renderState.dstColorBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
        renderState.colorBlendOp = MaterialBlendOp::Add;
        renderState.srcAlphaBlendFactor = MaterialBlendFactor::Zero;
        renderState.dstAlphaBlendFactor = MaterialBlendFactor::One;
        renderState.alphaBlendOp = MaterialBlendOp::Add;
        return true;
    }
    if (blend == "premultiply" || blend == "premultiplied" || blend == "premultiplied_alpha") {
        renderState.blendEnable = true;
        renderState.srcColorBlendFactor = MaterialBlendFactor::One;
        renderState.dstColorBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
        renderState.colorBlendOp = MaterialBlendOp::Add;
        renderState.srcAlphaBlendFactor = MaterialBlendFactor::One;
        renderState.dstAlphaBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
        renderState.alphaBlendOp = MaterialBlendOp::Add;
        return true;
    }
    if (blend == "additive") {
        renderState.blendEnable = true;
        renderState.srcColorBlendFactor = MaterialBlendFactor::One;
        renderState.dstColorBlendFactor = MaterialBlendFactor::One;
        renderState.colorBlendOp = MaterialBlendOp::Add;
        renderState.srcAlphaBlendFactor = MaterialBlendFactor::One;
        renderState.dstAlphaBlendFactor = MaterialBlendFactor::One;
        renderState.alphaBlendOp = MaterialBlendOp::Add;
        return true;
    }

    return false;
}

MaterialStencilOp ParseStencilOpString(const std::string &value)
{
    if (value == "keep")
        return MaterialStencilOp::Keep;
    if (value == "zero")
        return MaterialStencilOp::Zero;
    if (value == "replace")
        return MaterialStencilOp::Replace;
    if (value == "incr" || value == "increment_clamp")
        return MaterialStencilOp::IncrementAndClamp;
    if (value == "decr" || value == "decrement_clamp")
        return MaterialStencilOp::DecrementAndClamp;
    if (value == "invert")
        return MaterialStencilOp::Invert;
    if (value == "incr_wrap" || value == "increment_wrap")
        return MaterialStencilOp::IncrementAndWrap;
    if (value == "decr_wrap" || value == "decrement_wrap")
        return MaterialStencilOp::DecrementAndWrap;
    return MaterialStencilOp::Keep;
}

std::vector<std::string> SplitTrimmed(const std::string &text, char separator)
{
    std::vector<std::string> parts;
    std::istringstream stream(text);
    std::string token;
    while (std::getline(stream, token, separator)) {
        size_t start = token.find_first_not_of(" \t");
        size_t end = token.find_last_not_of(" \t");
        if (start != std::string::npos && end != std::string::npos) {
            parts.push_back(token.substr(start, end - start + 1));
        }
    }
    return parts;
}

bool ApplyStencilMeta(RenderState &renderState, const std::string &stencil)
{
    if (stencil.empty()) {
        return false;
    }

    std::vector<std::string> parts = SplitTrimmed(stencil, ',');
    if (parts.size() < 2) {
        return false;
    }

    MaterialStencilOpState opState{};
    opState.compareOp = ParseDepthCompareOpString(parts[0], MaterialCompareOp::Always);
    try {
        opState.reference = static_cast<uint32_t>(std::stoi(parts[1]));
    } catch (...) {
        opState.reference = 0;
    }
    opState.passOp = (parts.size() > 2) ? ParseStencilOpString(parts[2]) : MaterialStencilOp::Keep;
    opState.failOp = (parts.size() > 3) ? ParseStencilOpString(parts[3]) : MaterialStencilOp::Keep;
    opState.depthFailOp = (parts.size() > 4) ? ParseStencilOpString(parts[4]) : MaterialStencilOp::Keep;
    opState.compareMask = 0xFF;
    opState.writeMask = 0xFF;

    renderState.stencilTestEnable = true;
    renderState.stencilFront = opState;
    renderState.stencilBack = opState;
    return true;
}

} // namespace

// ============================================================================
// RenderState Implementation
// ============================================================================

bool RenderState::operator==(const RenderState &other) const
{
    return cullMode == other.cullMode && frontFace == other.frontFace && polygonMode == other.polygonMode &&
           depthBiasEnable == other.depthBiasEnable && depthBiasConstantFactor == other.depthBiasConstantFactor &&
           depthBiasSlopeFactor == other.depthBiasSlopeFactor && depthBiasClamp == other.depthBiasClamp &&
           topology == other.topology && depthTestEnable == other.depthTestEnable &&
           depthWriteEnable == other.depthWriteEnable && depthCompareOp == other.depthCompareOp &&
           stencilTestEnable == other.stencilTestEnable && stencilFront == other.stencilFront &&
           stencilBack == other.stencilBack && blendEnable == other.blendEnable &&
           srcColorBlendFactor == other.srcColorBlendFactor && dstColorBlendFactor == other.dstColorBlendFactor &&
           srcAlphaBlendFactor == other.srcAlphaBlendFactor && dstAlphaBlendFactor == other.dstAlphaBlendFactor &&
           colorBlendOp == other.colorBlendOp && alphaBlendOp == other.alphaBlendOp &&
           alphaClipEnabled == other.alphaClipEnabled && alphaClipThreshold == other.alphaClipThreshold &&
           renderQueue == other.renderQueue;
}

size_t RenderState::Hash() const
{
    size_t hash = 0;
    auto hashCombine = [&hash](size_t value) { hash ^= value + 0x9e3779b9 + (hash << 6) + (hash >> 2); };

    hashCombine(static_cast<size_t>(cullMode));
    hashCombine(static_cast<size_t>(frontFace));
    hashCombine(static_cast<size_t>(polygonMode));
    hashCombine(static_cast<size_t>(depthBiasEnable));
    if (depthBiasEnable) {
        hashCombine(std::hash<float>{}(depthBiasConstantFactor));
        hashCombine(std::hash<float>{}(depthBiasSlopeFactor));
        hashCombine(std::hash<float>{}(depthBiasClamp));
    }
    hashCombine(static_cast<size_t>(topology));
    hashCombine(static_cast<size_t>(depthTestEnable));
    hashCombine(static_cast<size_t>(depthWriteEnable));
    hashCombine(static_cast<size_t>(depthCompareOp));
    hashCombine(static_cast<size_t>(stencilTestEnable));
    if (stencilTestEnable) {
        hashCombine(static_cast<size_t>(stencilFront.failOp));
        hashCombine(static_cast<size_t>(stencilFront.passOp));
        hashCombine(static_cast<size_t>(stencilFront.depthFailOp));
        hashCombine(static_cast<size_t>(stencilFront.compareOp));
        hashCombine(static_cast<size_t>(stencilFront.compareMask));
        hashCombine(static_cast<size_t>(stencilFront.writeMask));
        hashCombine(static_cast<size_t>(stencilFront.reference));
        hashCombine(static_cast<size_t>(stencilBack.failOp));
        hashCombine(static_cast<size_t>(stencilBack.passOp));
        hashCombine(static_cast<size_t>(stencilBack.depthFailOp));
        hashCombine(static_cast<size_t>(stencilBack.compareOp));
        hashCombine(static_cast<size_t>(stencilBack.compareMask));
        hashCombine(static_cast<size_t>(stencilBack.writeMask));
        hashCombine(static_cast<size_t>(stencilBack.reference));
    }
    hashCombine(static_cast<size_t>(blendEnable));
    hashCombine(static_cast<size_t>(srcColorBlendFactor));
    hashCombine(static_cast<size_t>(dstColorBlendFactor));
    hashCombine(static_cast<size_t>(srcAlphaBlendFactor));
    hashCombine(static_cast<size_t>(dstAlphaBlendFactor));
    hashCombine(static_cast<size_t>(colorBlendOp));
    hashCombine(static_cast<size_t>(alphaBlendOp));
    hashCombine(static_cast<size_t>(alphaClipEnabled));
    if (alphaClipEnabled)
        hashCombine(std::hash<float>{}(alphaClipThreshold));
    hashCombine(static_cast<size_t>(renderQueue));

    return hash;
}

// ============================================================================
// InxMaterial Implementation
// ============================================================================

uint64_t InxMaterial::AllocateRuntimeId() noexcept
{
    static std::atomic<uint64_t> nextId{1};
    return nextId.fetch_add(1, std::memory_order_relaxed);
}

size_t InxMaterial::GetRuntimeMemoryBytes() const noexcept
{
    size_t bytes = sizeof(*this) + m_name.capacity() + m_guid.capacity() + m_filePath.capacity() +
                   m_vertexShader.guid.capacity() + m_vertexShader.shaderId.capacity() +
                   m_vertexShader.pathHint.capacity() + m_fragmentShader.guid.capacity() +
                   m_fragmentShader.shaderId.capacity() + m_fragmentShader.pathHint.capacity() + m_passTag.capacity();
    bytes += m_properties.bucket_count() * sizeof(void *);
    bytes += m_properties.size() * sizeof(std::pair<const std::string, MaterialProperty>);
    for (const auto &[key, property] : m_properties) {
        bytes += key.capacity() + property.name.capacity();
        if (const auto *text = std::get_if<std::string>(&property.value))
            bytes += text->capacity();
    }
    bytes += m_shaderPropertyOrder.capacity() * sizeof(std::string);
    for (const auto &name : m_shaderPropertyOrder)
        bytes += name.capacity();
    return bytes;
}

InxMaterial::InxMaterial(const std::string &name) : m_name(name)
{
}

InxMaterial::InxMaterial(const std::string &name, const std::string &shaderName)
    : m_name(name), m_vertexShader{"", shaderName, ""}, m_fragmentShader{"", shaderName, ""}
{
}

InxMaterial::InxMaterial(const InxMaterial &other)
    : m_name(other.m_name), m_guid(other.m_guid), m_filePath(other.m_filePath), m_builtin(other.m_builtin),
      m_vertexShader(other.m_vertexShader), m_fragmentShader(other.m_fragmentShader), m_passTag(other.m_passTag),
      m_renderState(other.m_renderState), m_renderStateOverrides(other.m_renderStateOverrides),
      m_properties(other.m_properties), m_shaderPropertyOrder(other.m_shaderPropertyOrder), m_pipelineDirty(true),
      m_propertiesDirty(true), m_version(0), m_isDeleted(other.m_isDeleted)
{
    // GPU-transient state must never be copied across logical material instances.
}

InxMaterial &InxMaterial::operator=(const InxMaterial &other)
{
    if (this == &other) {
        return *this;
    }

    m_name = other.m_name;
    m_guid = other.m_guid;
    m_filePath = other.m_filePath;
    m_builtin = other.m_builtin;
    m_vertexShader = other.m_vertexShader;
    m_fragmentShader = other.m_fragmentShader;
    m_passTag = other.m_passTag;
    m_renderState = other.m_renderState;
    m_renderStateOverrides = other.m_renderStateOverrides;
    m_properties = other.m_properties;
    m_shaderPropertyOrder = other.m_shaderPropertyOrder;

    // Reset runtime-only GPU state so this instance cannot retain stale handles.
    ClearAllPassPipelines();
#if !defined(INFERNUX_DISABLE_VULKAN_MATERIAL_RUNTIME)
    m_uboBuffer = VK_NULL_HANDLE;
    m_uboAllocator = VK_NULL_HANDLE;
    m_uboAllocation = VK_NULL_HANDLE;
#endif
    m_uboMappedData = nullptr;
    m_pipelineDirty = true;
    m_propertiesDirty = true;
    m_version = 0;
    m_isDeleted = other.m_isDeleted;

    return *this;
}

void InxMaterial::SetPropertyValue(const std::string &name, MaterialPropertyType type, MaterialPropertyValue value)
{
    const auto existing = m_properties.find(name);
    const bool hdr = existing != m_properties.end() && existing->second.hdr;
    const auto range = existing != m_properties.end() ? existing->second.range : std::nullopt;
    m_properties[name] = MaterialProperty{name, type, std::move(value), hdr, range};
    m_propertiesDirty = true;
    ++m_version;
}

void InxMaterial::SetFloat(const std::string &name, float value)
{
    SetPropertyValue(name, MaterialPropertyType::Float, value);
}

void InxMaterial::SetVector2(const std::string &name, const glm::vec2 &value)
{
    SetPropertyValue(name, MaterialPropertyType::Float2, value);
}

void InxMaterial::SetVector3(const std::string &name, const glm::vec3 &value)
{
    SetPropertyValue(name, MaterialPropertyType::Float3, value);
}

void InxMaterial::SetVector4(const std::string &name, const glm::vec4 &value)
{
    SetPropertyValue(name, MaterialPropertyType::Float4, value);
}

void InxMaterial::SetColor(const std::string &name, const glm::vec4 &color)
{
    SetPropertyValue(name, MaterialPropertyType::Color, color);
}

void InxMaterial::SetInt(const std::string &name, int value)
{
    SetPropertyValue(name, MaterialPropertyType::Int, value);
}

void InxMaterial::SetMatrix(const std::string &name, const glm::mat4 &matrix)
{
    SetPropertyValue(name, MaterialPropertyType::Mat4, matrix);
}

std::string InxMaterial::RequireTextureGuid(const std::string &textureGuid)
{
    if (textureGuid.empty())
        return {};
    if (IsBuiltinTextureToken(textureGuid))
        return textureGuid;
    auto *database = AssetRegistry::Instance().GetAssetDatabase();
    if (database == nullptr)
        throw std::logic_error("texture GUID validation requires an initialized AssetDatabase");
    const auto metadata = database->GetMetaByGuid(textureGuid);
    if (!metadata)
        throw std::invalid_argument("texture GUID does not exist: " + textureGuid);
    if (metadata->GetResourceType() != ResourceType::Texture)
        throw std::invalid_argument("asset GUID is not a Texture: " + textureGuid);
    return textureGuid;
}

void InxMaterial::SetTextureGuid(const std::string &name, const std::string &textureGuid)
{
    const std::string validatedGuid = RequireTextureGuid(textureGuid);

    auto it = m_properties.find(name);
    std::string previousGuid;
    bool hdr = false;
    std::optional<std::array<double, 2>> range;
    if (it != m_properties.end() && it->second.type == MaterialPropertyType::Texture2D) {
        hdr = it->second.hdr;
        range = it->second.range;
        const auto *existing = std::get_if<std::string>(&it->second.value);
        if (existing) {
            if (*existing == validatedGuid)
                return;
            previousGuid = *existing;
        }
    }

    if (!m_guid.empty() && !previousGuid.empty() && !IsBuiltinTextureToken(previousGuid))
        AssetDependencyGraph::Instance().RemoveAssetDependency(m_guid, previousGuid);

    m_properties[name] = MaterialProperty{name, MaterialPropertyType::Texture2D, validatedGuid, hdr, range};
    m_propertiesDirty = true;
    ++m_version;

    if (!m_guid.empty() && !validatedGuid.empty() && !IsBuiltinTextureToken(validatedGuid))
        AssetDependencyGraph::Instance().AddAssetDependency(m_guid, validatedGuid);
}

void InxMaterial::ClearTexture(const std::string &name)
{
    auto it = m_properties.find(name);
    if (it != m_properties.end() && it->second.type == MaterialPropertyType::Texture2D) {
        const auto *oldGuid = std::get_if<std::string>(&it->second.value);
        // Early-out: already cleared.
        if (oldGuid && oldGuid->empty())
            return;
        // Remove dependency for the old texture GUID
        if (!m_guid.empty() && oldGuid && !oldGuid->empty()) {
            AssetDependencyGraph::Instance().RemoveAssetDependency(m_guid, *oldGuid);
        }
        it->second.value = std::string{};
        m_propertiesDirty = true;
        ++m_version;
    }
}

bool InxMaterial::RemoveProperty(const std::string &name)
{
    auto it = m_properties.find(name);
    if (it == m_properties.end())
        return false;

    if (it->second.type == MaterialPropertyType::Texture2D) {
        const auto *oldGuid = std::get_if<std::string>(&it->second.value);
        if (!m_guid.empty() && oldGuid && !oldGuid->empty() && !IsBuiltinTextureToken(*oldGuid))
            AssetDependencyGraph::Instance().RemoveAssetDependency(m_guid, *oldGuid);
    }

    m_properties.erase(it);
    m_propertiesDirty = true;
    ++m_version;
    return true;
}

bool InxMaterial::HasProperty(const std::string &name) const
{
    return m_properties.find(name) != m_properties.end();
}

const MaterialProperty *InxMaterial::GetProperty(const std::string &name) const
{
    auto it = m_properties.find(name);
    if (it != m_properties.end()) {
        return &it->second;
    }
    return nullptr;
}

bool InxMaterial::SynchronizeShaderPropertyDefaults(const ShaderProgramArtifact &artifact)
{
    bool changed = false;
    std::vector<std::string> propertyOrder;
    propertyOrder.reserve(artifact.properties.size());

    for (const auto &binding : artifact.properties) {
        const auto expectedType = ShaderMaterialPropertyType(binding);
        if (!expectedType || binding.name.empty())
            continue;

        propertyOrder.push_back(binding.name);
        auto existing = m_properties.find(binding.name);
        if (existing == m_properties.end() || !MaterialValueMatchesType(existing->second, *expectedType)) {
            m_properties[binding.name] =
                MaterialProperty{binding.name, *expectedType, ParseShaderPropertyDefault(binding, *expectedType),
                                 binding.hdr, binding.range};
            changed = true;
            continue;
        }

        if (existing->second.hdr != binding.hdr) {
            existing->second.hdr = binding.hdr;
            changed = true;
        }
        if (existing->second.range != binding.range) {
            existing->second.range = binding.range;
            changed = true;
        }
    }

    if (m_shaderPropertyOrder != propertyOrder) {
        m_shaderPropertyOrder = std::move(propertyOrder);
        changed = true;
    }
    if (changed) {
        m_propertiesDirty = true;
        ++m_version;
    }
    return changed;
}

size_t InxMaterial::GetPipelineHash() const
{
    size_t hash = 0;
    auto hashCombine = [&hash](size_t value) { hash ^= value + 0x9e3779b9 + (hash << 6) + (hash >> 2); };

    // Path hints are recovery metadata and do not create distinct pipelines.
    hashCombine(std::hash<std::string>{}(m_vertexShader.StableKey()));
    hashCombine(std::hash<std::string>{}(m_fragmentShader.StableKey()));

    // Hash render state
    hashCombine(m_renderState.Hash());

    return hash;
}

void InxMaterial::ApplyShaderRenderMeta(const std::string &cullMode, const std::string &depthWrite,
                                        const std::string &depthTest, const std::string &blend, int queue,
                                        const std::string &passTag, const std::string &stencil,
                                        const std::string &alphaClip)
{
    bool changed = false;

    // Shader metadata describes the complete default state, not a patch over
    // the previously selected shader. Restore omitted fields before applying
    // annotations so switching away from a transparent/particle shader cannot
    // leak its culling, depth, blend, queue, stencil, or pass-tag state into a
    // regular mesh shader. Explicit per-material overrides remain authoritative.
    if (!m_builtin) {
        const RenderState defaults;
        const auto assign = [&changed](auto &target, const auto &value) {
            if (target != value) {
                target = value;
                changed = true;
            }
        };

        if (!HasOverride(RenderStateOverride::CullMode))
            assign(m_renderState.cullMode, defaults.cullMode);
        if (!HasOverride(RenderStateOverride::DepthWrite))
            assign(m_renderState.depthWriteEnable, defaults.depthWriteEnable);
        if (!HasOverride(RenderStateOverride::DepthTest))
            assign(m_renderState.depthTestEnable, defaults.depthTestEnable);
        if (!HasOverride(RenderStateOverride::DepthCompareOp))
            assign(m_renderState.depthCompareOp, defaults.depthCompareOp);
        if (!HasOverride(RenderStateOverride::BlendEnable))
            assign(m_renderState.blendEnable, defaults.blendEnable);
        if (!HasOverride(RenderStateOverride::BlendMode)) {
            assign(m_renderState.srcColorBlendFactor, defaults.srcColorBlendFactor);
            assign(m_renderState.dstColorBlendFactor, defaults.dstColorBlendFactor);
            assign(m_renderState.colorBlendOp, defaults.colorBlendOp);
            assign(m_renderState.srcAlphaBlendFactor, defaults.srcAlphaBlendFactor);
            assign(m_renderState.dstAlphaBlendFactor, defaults.dstAlphaBlendFactor);
            assign(m_renderState.alphaBlendOp, defaults.alphaBlendOp);
        }
        if (!HasOverride(RenderStateOverride::RenderQueue))
            assign(m_renderState.renderQueue, defaults.renderQueue);

        assign(m_renderState.stencilTestEnable, defaults.stencilTestEnable);
        if (m_renderState.stencilFront != defaults.stencilFront) {
            m_renderState.stencilFront = defaults.stencilFront;
            changed = true;
        }
        if (m_renderState.stencilBack != defaults.stencilBack) {
            m_renderState.stencilBack = defaults.stencilBack;
            changed = true;
        }
        if (m_passTag != passTag) {
            m_passTag = passTag;
            changed = true;
        }
    }

    // Cull: none / front / back. Skip when the material overrides CullMode.
    if (!cullMode.empty() && !HasOverride(RenderStateOverride::CullMode)) {
        MaterialCullMode newCull = m_renderState.cullMode;
        if (cullMode == "none" || cullMode == "off")
            newCull = MaterialCullMode::None;
        else if (cullMode == "front")
            newCull = MaterialCullMode::Front;
        else if (cullMode == "back")
            newCull = MaterialCullMode::Back;
        if (newCull != m_renderState.cullMode) {
            m_renderState.cullMode = newCull;
            changed = true;
        }
    }

    // DepthWrite: on / off. Skip when the material overrides DepthWrite.
    if (!depthWrite.empty() && !HasOverride(RenderStateOverride::DepthWrite)) {
        bool newDW = m_renderState.depthWriteEnable;
        if (depthWrite == "on" || depthWrite == "true")
            newDW = true;
        else if (depthWrite == "off" || depthWrite == "false")
            newDW = false;
        if (newDW != m_renderState.depthWriteEnable) {
            m_renderState.depthWriteEnable = newDW;
            changed = true;
        }
    }

    // DepthTest: on / off / less / less_equal / always / never.
    // Skip if user has overridden DepthTest or DepthCompareOp
    changed |= ApplyDepthTestMeta(m_renderState, depthTest, !HasOverride(RenderStateOverride::DepthTest),
                                  !HasOverride(RenderStateOverride::DepthCompareOp));

    // Blend: off / alpha / additive. Skip when the material overrides blending.
    changed |= ApplyBlendMeta(m_renderState, blend, !HasOverride(RenderStateOverride::BlendEnable),
                              !HasOverride(RenderStateOverride::BlendMode));

    // Queue: integer render queue, skipped when overridden or built in.
    if (!m_builtin && !HasOverride(RenderStateOverride::RenderQueue) && queue >= 0 &&
        queue != m_renderState.renderQueue) {
        m_renderState.renderQueue = queue;
        changed = true;
    }

    // Stencil: compare_op, ref, pass_op, fail_op, depth_fail_op.
    changed |= ApplyStencilMeta(m_renderState, stencil);

    // AlphaClip threshold. Skip when the material overrides alpha clipping.
    if (!alphaClip.empty() && alphaClip != "off" && !HasOverride(RenderStateOverride::AlphaClip)) {
        if (!m_renderState.alphaClipEnabled) {
            m_renderState.alphaClipEnabled = true;
            changed = true;
        }
        float threshold = 0.5f;
        try {
            threshold = std::stof(alphaClip);
        } catch (...) {
        }
        if (m_renderState.alphaClipThreshold != threshold) {
            m_renderState.alphaClipThreshold = threshold;
            changed = true;
        }
        SyncAlphaClipProperty();
    } else if ((alphaClip.empty() || alphaClip == "off") && !HasOverride(RenderStateOverride::AlphaClip)) {
        if (m_renderState.alphaClipEnabled) {
            m_renderState.alphaClipEnabled = false;
            changed = true;
            SyncAlphaClipProperty();
        }
    }

    if (changed)
        m_pipelineDirty = true;
}

void InxMaterial::SyncAlphaClipProperty()
{
    float value = m_renderState.alphaClipEnabled ? m_renderState.alphaClipThreshold : 0.0f;
    SetFloat("_AlphaClipThreshold", value);
}

nlohmann::json InxMaterial::SerializeDocument() const
{
    json j;
    j["name"] = m_name;
    j["builtin"] = m_builtin;

    // GUID is authoritative, shader_id is the stable compiler identity, and
    // path_hint recovers assets whose database has not been rebuilt yet.
    j["shaders"]["vertex"] = SerializeShaderReference(m_vertexShader);
    j["shaders"]["fragment"] = SerializeShaderReference(m_fragmentShader);

    // Render state
    json rs;
    rs["cullMode"] = static_cast<int>(m_renderState.cullMode);
    rs["frontFace"] = static_cast<int>(m_renderState.frontFace);
    rs["polygonMode"] = static_cast<int>(m_renderState.polygonMode);
    rs["lineWidth"] = m_renderState.lineWidth;
    rs["depthBiasEnable"] = m_renderState.depthBiasEnable;
    rs["depthBiasConstantFactor"] = m_renderState.depthBiasConstantFactor;
    rs["depthBiasSlopeFactor"] = m_renderState.depthBiasSlopeFactor;
    rs["depthBiasClamp"] = m_renderState.depthBiasClamp;
    rs["topology"] = static_cast<int>(m_renderState.topology);
    rs["depthTestEnable"] = m_renderState.depthTestEnable;
    rs["depthWriteEnable"] = m_renderState.depthWriteEnable;
    rs["depthCompareOp"] = static_cast<int>(m_renderState.depthCompareOp);
    rs["blendEnable"] = m_renderState.blendEnable;
    rs["srcColorBlendFactor"] = static_cast<int>(m_renderState.srcColorBlendFactor);
    rs["dstColorBlendFactor"] = static_cast<int>(m_renderState.dstColorBlendFactor);
    rs["colorBlendOp"] = static_cast<int>(m_renderState.colorBlendOp);
    rs["srcAlphaBlendFactor"] = static_cast<int>(m_renderState.srcAlphaBlendFactor);
    rs["dstAlphaBlendFactor"] = static_cast<int>(m_renderState.dstAlphaBlendFactor);
    rs["alphaBlendOp"] = static_cast<int>(m_renderState.alphaBlendOp);
    rs["alphaClipEnabled"] = m_renderState.alphaClipEnabled;
    rs["alphaClipThreshold"] = m_renderState.alphaClipThreshold;
    rs["renderQueue"] = m_renderState.renderQueue;
    rs["stencilTestEnable"] = m_renderState.stencilTestEnable;
    if (m_renderState.stencilTestEnable) {
        auto stencilOpToJson = [](const MaterialStencilOpState &op) {
            json s;
            s["failOp"] = static_cast<int>(op.failOp);
            s["passOp"] = static_cast<int>(op.passOp);
            s["depthFailOp"] = static_cast<int>(op.depthFailOp);
            s["compareOp"] = static_cast<int>(op.compareOp);
            s["compareMask"] = op.compareMask;
            s["writeMask"] = op.writeMask;
            s["reference"] = op.reference;
            return s;
        };
        rs["stencilFront"] = stencilOpToJson(m_renderState.stencilFront);
        rs["stencilBack"] = stencilOpToJson(m_renderState.stencilBack);
    }
    j["renderState"] = rs;

    // Pass tag for draw call filtering
    if (!m_passTag.empty()) {
        j["passTag"] = m_passTag;
    }

    // Render state override bitmask (which fields user has manually set)
    if (m_renderStateOverrides != 0) {
        j["renderStateOverrides"] = m_renderStateOverrides;
    }

    // Properties
    json props = json::object();
    for (const auto &[propName, prop] : m_properties) {
        // Skip engine-internal properties derived from renderState
        if (propName == "_AlphaClipThreshold")
            continue;
        json propJson;
        propJson["type"] = static_cast<int>(prop.type);
        if (prop.hdr)
            propJson["hdr"] = true;
        if (prop.range)
            propJson["range"] = {(*prop.range)[0], (*prop.range)[1]};

        switch (prop.type) {
        case MaterialPropertyType::Float:
            propJson["value"] = std::get<float>(prop.value);
            break;
        case MaterialPropertyType::Float2: {
            auto v = std::get<glm::vec2>(prop.value);
            propJson["value"] = {v.x, v.y};
            break;
        }
        case MaterialPropertyType::Float3: {
            auto v = std::get<glm::vec3>(prop.value);
            propJson["value"] = {v.x, v.y, v.z};
            break;
        }
        case MaterialPropertyType::Float4:
        case MaterialPropertyType::Color: {
            auto v = std::get<glm::vec4>(prop.value);
            propJson["value"] = {v.x, v.y, v.z, v.w};
            break;
        }
        case MaterialPropertyType::Int:
            propJson["value"] = std::get<int>(prop.value);
            break;
        case MaterialPropertyType::Mat4: {
            auto m = std::get<glm::mat4>(prop.value);
            json matArr = json::array();
            for (int i = 0; i < 4; i++) {
                for (int k = 0; k < 4; k++) {
                    matArr.push_back(m[i][k]);
                }
            }
            propJson["value"] = matArr;
            break;
        }
        case MaterialPropertyType::Texture2D:
            propJson["guid"] = std::get<std::string>(prop.value);
            break;
        }
        props[propName] = propJson;
    }
    j["properties"] = props;

    if (!m_shaderPropertyOrder.empty())
        j["_shader_property_order"] = m_shaderPropertyOrder;

    return j;
}

std::string InxMaterial::Serialize() const
{
    return SerializeDocument().dump(2);
}

bool InxMaterial::SaveToFile() const
{
    if (m_isDeleted) {
        INXLOG_WARN("InxMaterial::SaveToFile: material '", m_name, "' is deleted, refusing to write");
        return false;
    }
    if (m_filePath.empty()) {
        INXLOG_WARN("InxMaterial::SaveToFile: No file path set for material '", m_name, "'");
        return false;
    }
    try {
        const std::string jsonStr = Serialize();
        DocumentStore::Instance().WriteAndWait(m_filePath, jsonStr);
        INXLOG_DEBUG("InxMaterial::SaveToFile: Saved material '", m_name, "' to '", m_filePath, "'");
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("InxMaterial::SaveToFile: Exception - ", e.what());
        return false;
    }
}

bool InxMaterial::SaveToFile(const std::string &path)
{
    if (m_isDeleted) {
        INXLOG_WARN("InxMaterial::SaveToFile: material '", m_name, "' is deleted, refusing to write");
        return false;
    }
    try {
        const std::string jsonStr = Serialize();
        DocumentStore::Instance().WriteAndWait(path, jsonStr);

        // Update stored file path
        const_cast<InxMaterial *>(this)->m_filePath = path;

        INXLOG_DEBUG("InxMaterial::SaveToFile: Saved material '", m_name, "' to '", path, "'");
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("InxMaterial::SaveToFile: Exception - ", e.what());
        return false;
    }
}

bool InxMaterial::Deserialize(const std::string &jsonStr)
{
    try {
        return DeserializeDocument(json::parse(jsonStr));
    } catch (const std::exception &e) {
        INXLOG_ERROR("Failed to parse material document: ", e.what());
        return false;
    }
}

bool InxMaterial::DeserializeDocument(const nlohmann::json &document)
{
    InxMaterial staged(*this);
    if (!staged.ApplyDocument(document)) {
        return false;
    }

    m_name = std::move(staged.m_name);
    m_builtin = staged.m_builtin;
    m_vertexShader = std::move(staged.m_vertexShader);
    m_fragmentShader = std::move(staged.m_fragmentShader);
    m_passTag = std::move(staged.m_passTag);
    m_renderState = staged.m_renderState;
    m_renderStateOverrides = staged.m_renderStateOverrides;
    m_properties = std::move(staged.m_properties);
    m_shaderPropertyOrder = std::move(staged.m_shaderPropertyOrder);
    m_pipelineDirty = true;
    m_propertiesDirty = true;
    ++m_version;
    return true;
}

bool InxMaterial::ApplyDocument(const nlohmann::json &document)
{
    try {
        material_document_validation::ValidateMaterialDocument(document);
        const json &j = document;
        m_name = j["name"].get<std::string>();
        m_builtin = j["builtin"].get<bool>();

        const auto &shaders = j["shaders"];
        m_vertexShader = DeserializeShaderReference(shaders["vertex"]);
        m_fragmentShader = DeserializeShaderReference(shaders["fragment"]);

        const auto &rs = j["renderState"];
        m_renderState.cullMode = static_cast<MaterialCullMode>(rs["cullMode"].get<int>());
        m_renderState.frontFace = static_cast<MaterialFrontFace>(rs["frontFace"].get<int>());
        m_renderState.polygonMode = static_cast<MaterialPolygonMode>(rs["polygonMode"].get<int>());
        m_renderState.lineWidth = rs["lineWidth"].get<float>();
        m_renderState.depthBiasEnable = rs["depthBiasEnable"].get<bool>();
        m_renderState.depthBiasConstantFactor = rs["depthBiasConstantFactor"].get<float>();
        m_renderState.depthBiasSlopeFactor = rs["depthBiasSlopeFactor"].get<float>();
        m_renderState.depthBiasClamp = rs["depthBiasClamp"].get<float>();
        m_renderState.topology = static_cast<MaterialPrimitiveTopology>(rs["topology"].get<int>());
        m_renderState.depthTestEnable = rs["depthTestEnable"].get<bool>();
        m_renderState.depthWriteEnable = rs["depthWriteEnable"].get<bool>();
        m_renderState.depthCompareOp = static_cast<MaterialCompareOp>(rs["depthCompareOp"].get<int>());
        m_renderState.blendEnable = rs["blendEnable"].get<bool>();
        m_renderState.srcColorBlendFactor = static_cast<MaterialBlendFactor>(rs["srcColorBlendFactor"].get<int>());
        m_renderState.dstColorBlendFactor = static_cast<MaterialBlendFactor>(rs["dstColorBlendFactor"].get<int>());
        m_renderState.colorBlendOp = static_cast<MaterialBlendOp>(rs["colorBlendOp"].get<int>());
        m_renderState.srcAlphaBlendFactor = static_cast<MaterialBlendFactor>(rs["srcAlphaBlendFactor"].get<int>());
        m_renderState.dstAlphaBlendFactor = static_cast<MaterialBlendFactor>(rs["dstAlphaBlendFactor"].get<int>());
        m_renderState.alphaBlendOp = static_cast<MaterialBlendOp>(rs["alphaBlendOp"].get<int>());
        m_renderState.alphaClipEnabled = rs["alphaClipEnabled"].get<bool>();
        m_renderState.alphaClipThreshold = rs["alphaClipThreshold"].get<float>();
        m_renderState.renderQueue = rs["renderQueue"].get<int32_t>();
        m_renderState.stencilTestEnable = rs["stencilTestEnable"].get<bool>();
        m_renderState.stencilFront = {};
        m_renderState.stencilBack = {};
        if (m_renderState.stencilTestEnable) {
            const auto jsonToStencilOp = [](const json &state) {
                MaterialStencilOpState result{};
                result.failOp = static_cast<MaterialStencilOp>(state["failOp"].get<int>());
                result.passOp = static_cast<MaterialStencilOp>(state["passOp"].get<int>());
                result.depthFailOp = static_cast<MaterialStencilOp>(state["depthFailOp"].get<int>());
                result.compareOp = static_cast<MaterialCompareOp>(state["compareOp"].get<int>());
                result.compareMask = state["compareMask"].get<uint32_t>();
                result.writeMask = state["writeMask"].get<uint32_t>();
                result.reference = state["reference"].get<uint32_t>();
                return result;
            };
            m_renderState.stencilFront = jsonToStencilOp(rs["stencilFront"]);
            m_renderState.stencilBack = jsonToStencilOp(rs["stencilBack"]);
        }

        m_passTag = j.value("passTag", std::string());
        m_renderStateOverrides = j.value("renderStateOverrides", uint32_t{0});
        m_shaderPropertyOrder = j.value("_shader_property_order", std::vector<std::string>{});

        m_properties.clear();
        for (const auto &[propName, propJson] : j["properties"].items()) {
            const int typeValue = propJson["type"].get<int>();
            MaterialProperty prop;
            prop.name = propName;
            prop.type = static_cast<MaterialPropertyType>(typeValue);
            prop.hdr = propJson.value("hdr", false);
            if (propJson.contains("range")) {
                const auto &range = propJson["range"];
                prop.range = std::array<double, 2>{range[0].get<double>(), range[1].get<double>()};
            }

            switch (prop.type) {
            case MaterialPropertyType::Float:
                prop.value = propJson["value"].get<float>();
                break;
            case MaterialPropertyType::Float2: {
                const auto &value = propJson["value"];
                prop.value = glm::vec2(value[0].get<float>(), value[1].get<float>());
                break;
            }
            case MaterialPropertyType::Float3: {
                const auto &value = propJson["value"];
                prop.value = glm::vec3(value[0].get<float>(), value[1].get<float>(), value[2].get<float>());
                break;
            }
            case MaterialPropertyType::Float4:
            case MaterialPropertyType::Color: {
                const auto &value = propJson["value"];
                prop.value = glm::vec4(value[0].get<float>(), value[1].get<float>(), value[2].get<float>(),
                                       value[3].get<float>());
                break;
            }
            case MaterialPropertyType::Int:
                prop.value = propJson["value"].get<int>();
                break;
            case MaterialPropertyType::Mat4: {
                glm::mat4 m;
                const auto &value = propJson["value"];
                for (int i = 0; i < 4; i++) {
                    for (int k = 0; k < 4; k++) {
                        m[i][k] = value[i * 4 + k].get<float>();
                    }
                }
                prop.value = m;
                break;
            }
            case MaterialPropertyType::Texture2D:
                // A durable material reference survives deletion of its target.
                // Keep the GUID so Undo/reimport can reconnect it and the
                // Inspector can present a recoverable Missing Texture field.
                // Interactive assignment remains strict in SetTextureGuid().
                prop.value = RestoreTextureGuidReference(propJson["guid"].get<std::string>());
                break;
            }
            m_properties[propName] = std::move(prop);
        }

        m_pipelineDirty = true;
        m_propertiesDirty = true;
        SyncAlphaClipProperty();

        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("Failed to deserialize material: ", e.what());
        return false;
    }
}

std::shared_ptr<InxMaterial> InxMaterial::CreateDefaultLit()
{
    auto material = std::make_shared<InxMaterial>("DefaultLit");

    // Use the shared standard vertex shader with the lit fragment shader.
    material->SetVertShader("Standard");
    material->SetFragShader("Lit");

    // Default lit opaque render state
    RenderState state;
    state.cullMode = MaterialCullMode::Back;
    state.frontFace = MaterialFrontFace::Clockwise;
    state.depthTestEnable = true;
    state.depthWriteEnable = true;
    state.blendEnable = false;
    state.renderQueue = 2000; // Opaque queue
    material->SetRenderState(state);

    // Default properties from lit shader annotations
    material->SetColor("baseColor", glm::vec4(1.0f, 1.0f, 1.0f, 1.0f));
    material->SetFloat("metallic", 0.0f);
    material->SetFloat("smoothness", 0.5f);
    material->SetFloat("ambientOcclusion", 1.0f);
    material->SetColor("emissionColor", glm::vec4(0.0f, 0.0f, 0.0f, 0.0f));
    material->SetFloat("normalScale", 1.0f);
    material->SetFloat("specularHighlights", 1.0f);

    // Mark as built-in (shader cannot be changed by user)
    material->SetBuiltin(true);

    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateDefaultUnlit()
{
    auto material = std::make_shared<InxMaterial>("DefaultUnlit");

    // Use the shared standard vertex shader with the unlit fragment shader.
    material->SetVertShader("Standard");
    material->SetFragShader("Unlit");

    // Default unlit opaque render state
    RenderState state;
    state.cullMode = MaterialCullMode::Back;
    state.frontFace = MaterialFrontFace::Clockwise;
    state.depthTestEnable = true;
    state.depthWriteEnable = true;
    state.blendEnable = false;
    state.renderQueue = 2000; // Opaque queue
    material->SetRenderState(state);

    // Default property from shader annotation: baseColor
    material->SetColor("baseColor", glm::vec4(1.0f, 1.0f, 1.0f, 1.0f));

    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateDefaultLineMaterial()
{
    auto material = std::make_shared<InxMaterial>("DefaultLineMaterial");
    material->SetVertShader("Standard");
    material->SetFragShader("Unlit");

    // A camera-facing ribbon is not a closed surface: both windings must be
    // visible. Line colour alpha is carried per vertex, so the default also
    // needs the conventional transparent blend state instead of DefaultLit's
    // opaque depth-writing state.
    RenderState state;
    state.cullMode = MaterialCullMode::None;
    state.frontFace = MaterialFrontFace::Clockwise;
    state.depthTestEnable = true;
    state.depthWriteEnable = false;
    state.blendEnable = true;
    state.srcColorBlendFactor = MaterialBlendFactor::SourceAlpha;
    state.dstColorBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
    state.colorBlendOp = MaterialBlendOp::Add;
    state.srcAlphaBlendFactor = MaterialBlendFactor::One;
    state.dstAlphaBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
    state.alphaBlendOp = MaterialBlendOp::Add;
    state.renderQueue = 3000;
    material->SetRenderState(state);
    material->SetColor("baseColor", glm::vec4(1.0f));
    material->SetBuiltin(true);
    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateParticleSpriteMaterial()
{
    auto material = std::make_shared<InxMaterial>("ParticleSpriteMaterial");
    material->SetVertShader("Particle Sprite");
    material->SetFragShader("Particle Unlit");

    RenderState state;
    state.cullMode = MaterialCullMode::None;
    state.frontFace = MaterialFrontFace::Clockwise;
    state.depthTestEnable = true;
    state.depthWriteEnable = false;
    state.blendEnable = true;
    state.srcColorBlendFactor = MaterialBlendFactor::SourceAlpha;
    state.dstColorBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
    state.colorBlendOp = MaterialBlendOp::Add;
    state.srcAlphaBlendFactor = MaterialBlendFactor::One;
    state.dstAlphaBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
    state.alphaBlendOp = MaterialBlendOp::Add;
    state.renderQueue = 3000;
    material->SetRenderState(state);
    material->SetColor("baseColor", glm::vec4(1.0f));
    material->SetTextureGuid("texSampler", "white");
    material->SetFloat("softness", 0.18f);
    material->SetBuiltin(true);
    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateParticleSixWaySmokeMaterial()
{
    auto material = std::make_shared<InxMaterial>("ParticleSixWaySmokeMaterial");
    material->SetVertShader("Particle Sprite");
    material->SetFragShader("Particle Six-Way Smoke");

    RenderState state;
    state.cullMode = MaterialCullMode::None;
    state.frontFace = MaterialFrontFace::Clockwise;
    state.depthTestEnable = true;
    state.depthWriteEnable = false;
    state.blendEnable = true;
    state.srcColorBlendFactor = MaterialBlendFactor::One;
    state.dstColorBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
    state.colorBlendOp = MaterialBlendOp::Add;
    state.srcAlphaBlendFactor = MaterialBlendFactor::One;
    state.dstAlphaBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
    state.alphaBlendOp = MaterialBlendOp::Add;
    state.renderQueue = 3000;
    material->SetRenderState(state);

    material->SetColor("baseColor", glm::vec4(0.66f, 0.66f, 0.66f, 1.0f));
    material->SetColor("emissionColor", glm::vec4(1.0f, 0.28f, 0.04f, 1.0f));
    material->SetFloat("lightingIntensity", 1.0f);
    material->SetFloat("ambientIntensity", 0.0f);
    material->SetFloat("ambientSaturation", 0.0f);
    material->SetFloat("emissionIntensity", 0.0f);
    material->SetFloat("absorption", 0.5f);
    material->SetFloat("alphaScale", 1.0f);
    material->SetFloat("flipbookColumns", 1.0f);
    material->SetFloat("flipbookRows", 1.0f);
    material->SetFloat("flipbookFrameJitter", 0.0f);
    material->SetFloat("flipbookFrameOffset", 0.0f);
    material->SetFloat("fadeInFraction", 0.08f);
    material->SetFloat("fadeOutStart", 0.68f);
    material->SetFloat("densityClipThreshold", 0.025f);
    material->SetTextureGuid("positiveAxesMap", "white");
    material->SetTextureGuid("negativeAxesMap", "black");
    material->SetBuiltin(true);
    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateGizmoMaterial()
{
    auto material = std::make_shared<InxMaterial>("GizmoMaterial");

    // Use gizmo shader (simple unlit with vertex color)
    material->SetShader("Gizmo");

    // Gizmo render state: no culling (double-sided), depth test, depth write
    RenderState state;
    state.cullMode = MaterialCullMode::None; // Double-sided for grid visibility
    state.frontFace = MaterialFrontFace::Clockwise;
    state.depthTestEnable = true;
    state.depthWriteEnable = true;
    state.blendEnable = false;
    state.renderQueue = 20100; // Editor gizmo layer (20001-25000)
    material->SetRenderState(state);

    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateGridMaterial()
{
    auto material = std::make_shared<InxMaterial>("GridMaterial");

    material->SetShader("Grid");

    // Grid render state: double-sided, alpha-blended, depth test but no depth write
    RenderState state;
    state.cullMode = MaterialCullMode::None;
    state.frontFace = MaterialFrontFace::Clockwise;
    state.depthTestEnable = true;
    state.depthWriteEnable = false; // Transparent — don't write depth
    state.depthCompareOp = MaterialCompareOp::LessOrEqual;
    // Keep only a tiny constant bias. Slope-scaled bias explodes at grazing
    // angles when the editor camera is close to the XZ plane, which produces
    // driver-dependent stair-step artifacts on older AMD GPUs.
    state.depthBiasEnable = false;
    state.depthBiasConstantFactor = 0.0f;
    state.depthBiasSlopeFactor = 0.0f;
    state.depthBiasClamp = 0.0f;
    state.blendEnable = true;
    state.srcColorBlendFactor = MaterialBlendFactor::SourceAlpha;
    state.dstColorBlendFactor = MaterialBlendFactor::OneMinusSourceAlpha;
    state.colorBlendOp = MaterialBlendOp::Add;
    // Alpha channel: preserve destination alpha (1.0 from opaques/skybox) so
    // the scene texture stays fully opaque when displayed in ImGui viewport.
    state.srcAlphaBlendFactor = MaterialBlendFactor::Zero;
    state.dstAlphaBlendFactor = MaterialBlendFactor::One;
    state.alphaBlendOp = MaterialBlendOp::Add;
    state.renderQueue = 20001; // Editor gizmo layer (20001-25000), renders after all user passes
    material->SetRenderState(state);

    // Default fade distances
    material->SetFloat("fadeStart", 15.0f);
    material->SetFloat("fadeEnd", 80.0f);

    material->SetBuiltin(true);

    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateEditorToolsMaterial()
{
    auto material = std::make_shared<InxMaterial>("EditorToolsMaterial");

    // Same gizmo shader: simple unlit with vertex color
    material->SetShader("Gizmo");

    // Editor tools render state: always on top (no depth test), double-sided
    RenderState state;
    state.cullMode = MaterialCullMode::None;
    state.frontFace = MaterialFrontFace::Clockwise;
    state.depthTestEnable = false;  // Render on top of everything
    state.depthWriteEnable = false; // Don't affect depth buffer
    state.blendEnable = false;
    state.renderQueue = 25001; // Editor tools layer (25001-30000)
    material->SetRenderState(state);

    material->SetBuiltin(true);

    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateComponentGizmosMaterial()
{
    auto material = std::make_shared<InxMaterial>("ComponentGizmosMaterial");

    // Same gizmo shader: simple unlit with vertex color
    material->SetShader("Gizmo");

    // Component gizmos: depth-tested (occluded by scene geometry), double-sided, LINE topology
    RenderState state;
    state.cullMode = MaterialCullMode::None;
    state.frontFace = MaterialFrontFace::Clockwise;
    state.topology = MaterialPrimitiveTopology::LineList;
    state.depthTestEnable = true;
    state.depthWriteEnable = false; // Don't affect depth buffer
    state.blendEnable = false;
    state.renderQueue = 10000; // Component gizmos layer (10000-20000)
    material->SetRenderState(state);

    material->SetBuiltin(true);

    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateComponentGizmoIconMaterial()
{
    return CreateTexturedComponentGizmoIconMaterial("ComponentGizmoIconMaterial", "white");
}

std::shared_ptr<InxMaterial> InxMaterial::CreateComponentGizmoCameraIconMaterial()
{
    return CreateTexturedComponentGizmoIconMaterial("ComponentGizmoCameraIconMaterial", "icons/gizmo_camera.png");
}

std::shared_ptr<InxMaterial> InxMaterial::CreateComponentGizmoLightIconMaterial()
{
    return CreateTexturedComponentGizmoIconMaterial("ComponentGizmoLightIconMaterial", "icons/gizmo_light.png", true);
}

std::shared_ptr<InxMaterial> InxMaterial::CreateComponentGizmoParticleIconMaterial()
{
    return CreateTexturedComponentGizmoIconMaterial("ComponentGizmoParticleIconMaterial", "icons/gizmo_particle.png");
}

std::shared_ptr<InxMaterial> InxMaterial::CreateSkyboxProceduralMaterial()
{
    auto material = std::make_shared<InxMaterial>("SkyboxProcedural");

    // Use the procedural skybox shader registered by ShaderInfo Name.
    material->SetShader("Skybox Procedural");

    // Skybox render state:
    // - Cull back faces (the outside of the cube). In the LH coordinate system,
    //   CW winding is front-facing. From inside the cube the camera sees indices
    //   wound CW → front faces. Back-face culling removes the outside faces.
    // - No depth write (skybox should always be behind everything)
    // - Depth test <= (skybox writes z=1.0, passes only where nothing closer exists)
    // - renderQueue = EngineConfig::skyboxQueue (last; after opaque+transparent
    //   and outside the shadow-caster range so it never casts shadows)
    RenderState state;
    state.cullMode = MaterialCullMode::Back;
    state.frontFace = MaterialFrontFace::Clockwise;
    state.depthTestEnable = true;
    state.depthWriteEnable = false;
    state.depthCompareOp = MaterialCompareOp::LessOrEqual;
    state.blendEnable = false;
    state.renderQueue = EngineConfig::Get().skyboxQueue; // After all opaque/transparent, outside shadow caster range
    material->SetRenderState(state);

    // Default sky properties matching the ShaderInfo property declaration.
    // sRGB: zenith #6E7E9C, horizon #A6B9D0, ground #585858
    material->SetColor("skyTopColor", glm::vec4(0.431f, 0.494f, 0.612f, 1.0f));
    material->SetColor("skyHorizonColor", glm::vec4(0.651f, 0.725f, 0.816f, 1.0f));
    material->SetColor("groundColor", glm::vec4(0.345f, 0.345f, 0.345f, 1.0f));
    material->SetFloat("exposure", 1.0f);

    material->SetBuiltin(true);

    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateErrorMaterial()
{
    auto material = std::make_shared<InxMaterial>("ErrorMaterial");

    // Use dedicated error shaders: unlit magenta-black checkerboard pattern.
    // These shaders are self-contained (no material UBO, no textures) and
    // output a procedural checkerboard using world-position + UV.
    material->SetShader("Error");

    // Double-sided so the error pattern is visible from all angles
    RenderState state;
    state.cullMode = MaterialCullMode::None;
    state.frontFace = MaterialFrontFace::Clockwise;
    state.depthTestEnable = true;
    state.depthWriteEnable = true;
    state.blendEnable = false;
    state.renderQueue = 2000; // Opaque queue
    material->SetRenderState(state);

    material->SetBuiltin(true);

    return material;
}

// ============================================================================
// Clone — Unity-style Object.Instantiate for materials
// ============================================================================

std::shared_ptr<InxMaterial> InxMaterial::Clone() const
{
    static std::atomic<uint64_t> s_cloneCounter{0};
    auto clone = std::make_shared<InxMaterial>();

    // Deep copy identity (clear GUID & file path — runtime-only instance)
    // Each clone gets a unique name so GetMaterialKey() returns a unique key,
    // ensuring separate descriptor sets / UBOs in the renderer.
    clone->m_name = m_name + " (Instance_" + std::to_string(s_cloneCounter.fetch_add(1)) + ")";
    // clone->m_guid intentionally left empty — no asset identity
    // clone->m_filePath intentionally left empty — not saved to disk
    clone->m_builtin = false; // Clones are never builtin

    // Deep copy shader identity
    clone->m_vertexShader = m_vertexShader;
    clone->m_fragmentShader = m_fragmentShader;
    clone->m_passTag = m_passTag;

    // Deep copy render state & overrides
    clone->m_renderState = m_renderState;
    clone->m_renderStateOverrides = m_renderStateOverrides;

    // Deep copy all properties (floats, vecs, colors, texture GUIDs, etc.)
    // Texture references are GUIDs (strings) — shared by value, same as Unity.
    clone->m_properties = m_properties;

    // GPU-transient state is NOT copied — lazily recreated by the renderer.
    // m_passPipelines[] are already default-initialized (VK_NULL_HANDLE).
    // m_uboBuffer etc. are already default-initialized (VK_NULL_HANDLE).
    clone->m_pipelineDirty = true;
    clone->m_propertiesDirty = true;
    clone->m_version = 0;
    clone->m_isDeleted = false;

    return clone;
}

} // namespace infernux
