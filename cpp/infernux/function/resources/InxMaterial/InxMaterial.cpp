#include "InxMaterial.h"
#include "MaterialDocumentValidation.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <core/config/EngineConfig.h>
#include <core/log/InxLog.h>
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
                const std::string resolved = FromFsPath(fs::weakly_canonical(candidate, error));
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

std::shared_ptr<InxMaterial> CreateTexturedComponentGizmoIconMaterial(const std::string &name,
                                                                      const std::string &textureRef)
{
    auto material = std::make_shared<InxMaterial>(name);
    material->SetShader("gizmo_icon");

    RenderState state;
    state.cullMode = VK_CULL_MODE_NONE;
    state.frontFace = VK_FRONT_FACE_CLOCKWISE;
    state.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
    state.depthTestEnable = true;
    state.depthWriteEnable = false;
    state.depthCompareOp = VK_COMPARE_OP_LESS_OR_EQUAL;
    state.blendEnable = true;
    state.srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;
    state.dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    state.colorBlendOp = VK_BLEND_OP_ADD;
    state.srcAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
    state.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    state.alphaBlendOp = VK_BLEND_OP_ADD;
    state.alphaClipEnabled = false;
    state.alphaClipThreshold = 0.0f;
    state.renderQueue = 24950;
    material->SetRenderState(state);
    material->SyncAlphaClipProperty();

    material->SetColor("baseColor", glm::vec4(1.0f));
    material->SetTextureGuid("texSampler", ResolveEngineTextureGuid(textureRef));
    material->SetBuiltin(true);

    return material;
}

VkCompareOp ParseDepthCompareOpString(const std::string &value, VkCompareOp fallback)
{
    if (value == "on" || value == "true" || value == "less")
        return VK_COMPARE_OP_LESS;
    if (value == "less_equal")
        return VK_COMPARE_OP_LESS_OR_EQUAL;
    if (value == "always")
        return VK_COMPARE_OP_ALWAYS;
    if (value == "never")
        return VK_COMPARE_OP_NEVER;
    if (value == "greater")
        return VK_COMPARE_OP_GREATER;
    if (value == "greater_equal")
        return VK_COMPARE_OP_GREATER_OR_EQUAL;
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

    VkCompareOp newOp = ParseDepthCompareOpString(depthTest, renderState.depthCompareOp);
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
        renderState.srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;
        renderState.dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
        renderState.colorBlendOp = VK_BLEND_OP_ADD;
        renderState.srcAlphaBlendFactor = VK_BLEND_FACTOR_ZERO;
        renderState.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
        renderState.alphaBlendOp = VK_BLEND_OP_ADD;
        return true;
    }
    if (blend == "additive") {
        renderState.blendEnable = true;
        renderState.srcColorBlendFactor = VK_BLEND_FACTOR_ONE;
        renderState.dstColorBlendFactor = VK_BLEND_FACTOR_ONE;
        renderState.colorBlendOp = VK_BLEND_OP_ADD;
        renderState.srcAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
        renderState.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
        renderState.alphaBlendOp = VK_BLEND_OP_ADD;
        return true;
    }

    return false;
}

VkStencilOp ParseStencilOpString(const std::string &value)
{
    if (value == "keep")
        return VK_STENCIL_OP_KEEP;
    if (value == "zero")
        return VK_STENCIL_OP_ZERO;
    if (value == "replace")
        return VK_STENCIL_OP_REPLACE;
    if (value == "incr" || value == "increment_clamp")
        return VK_STENCIL_OP_INCREMENT_AND_CLAMP;
    if (value == "decr" || value == "decrement_clamp")
        return VK_STENCIL_OP_DECREMENT_AND_CLAMP;
    if (value == "invert")
        return VK_STENCIL_OP_INVERT;
    if (value == "incr_wrap" || value == "increment_wrap")
        return VK_STENCIL_OP_INCREMENT_AND_WRAP;
    if (value == "decr_wrap" || value == "decrement_wrap")
        return VK_STENCIL_OP_DECREMENT_AND_WRAP;
    return VK_STENCIL_OP_KEEP;
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

    VkStencilOpState opState{};
    opState.compareOp = ParseDepthCompareOpString(parts[0], VK_COMPARE_OP_ALWAYS);
    try {
        opState.reference = static_cast<uint32_t>(std::stoi(parts[1]));
    } catch (...) {
        opState.reference = 0;
    }
    opState.passOp = (parts.size() > 2) ? ParseStencilOpString(parts[2]) : VK_STENCIL_OP_KEEP;
    opState.failOp = (parts.size() > 3) ? ParseStencilOpString(parts[3]) : VK_STENCIL_OP_KEEP;
    opState.depthFailOp = (parts.size() > 4) ? ParseStencilOpString(parts[4]) : VK_STENCIL_OP_KEEP;
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
           stencilTestEnable == other.stencilTestEnable &&
           std::memcmp(&stencilFront, &other.stencilFront, sizeof(VkStencilOpState)) == 0 &&
           std::memcmp(&stencilBack, &other.stencilBack, sizeof(VkStencilOpState)) == 0 &&
           blendEnable == other.blendEnable && srcColorBlendFactor == other.srcColorBlendFactor &&
           dstColorBlendFactor == other.dstColorBlendFactor && srcAlphaBlendFactor == other.srcAlphaBlendFactor &&
           dstAlphaBlendFactor == other.dstAlphaBlendFactor && colorBlendOp == other.colorBlendOp &&
           alphaBlendOp == other.alphaBlendOp && alphaClipEnabled == other.alphaClipEnabled &&
           alphaClipThreshold == other.alphaClipThreshold && renderQueue == other.renderQueue;
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
                   m_vertShaderName.capacity() + m_fragShaderName.capacity() + m_passTag.capacity();
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
    : m_name(name), m_vertShaderName(shaderName), m_fragShaderName(shaderName)
{
}

InxMaterial::InxMaterial(const InxMaterial &other)
    : m_name(other.m_name), m_guid(other.m_guid), m_filePath(other.m_filePath), m_builtin(other.m_builtin),
      m_vertShaderName(other.m_vertShaderName), m_fragShaderName(other.m_fragShaderName), m_passTag(other.m_passTag),
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
    m_vertShaderName = other.m_vertShaderName;
    m_fragShaderName = other.m_fragShaderName;
    m_passTag = other.m_passTag;
    m_renderState = other.m_renderState;
    m_renderStateOverrides = other.m_renderStateOverrides;
    m_properties = other.m_properties;
    m_shaderPropertyOrder = other.m_shaderPropertyOrder;

    // Reset runtime-only GPU state so this instance cannot retain stale handles.
    ClearAllPassPipelines();
    m_uboBuffer = VK_NULL_HANDLE;
    m_uboAllocator = VK_NULL_HANDLE;
    m_uboAllocation = VK_NULL_HANDLE;
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

size_t InxMaterial::GetPipelineHash() const
{
    size_t hash = 0;
    auto hashCombine = [&hash](size_t value) { hash ^= value + 0x9e3779b9 + (hash << 6) + (hash >> 2); };

    // Hash shader names
    hashCombine(std::hash<std::string>{}(m_vertShaderName));
    hashCombine(std::hash<std::string>{}(m_fragShaderName));

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

    // @cull: none / front / back — skip if user has overridden CullMode
    if (!cullMode.empty() && !HasOverride(RenderStateOverride::CullMode)) {
        VkCullModeFlags newCull = m_renderState.cullMode;
        if (cullMode == "none" || cullMode == "off")
            newCull = VK_CULL_MODE_NONE;
        else if (cullMode == "front")
            newCull = VK_CULL_MODE_FRONT_BIT;
        else if (cullMode == "back")
            newCull = VK_CULL_MODE_BACK_BIT;
        if (newCull != m_renderState.cullMode) {
            m_renderState.cullMode = newCull;
            changed = true;
        }
    }

    // @depth_write: on / off — skip if user has overridden DepthWrite
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

    // @depth_test: on / off / less / less_equal / always / never
    // Skip if user has overridden DepthTest or DepthCompareOp
    changed |= ApplyDepthTestMeta(m_renderState, depthTest, !HasOverride(RenderStateOverride::DepthTest),
                                  !HasOverride(RenderStateOverride::DepthCompareOp));

    // @blend: off / alpha / additive — skip if user has overridden BlendEnable or BlendMode
    changed |= ApplyBlendMeta(m_renderState, blend, !HasOverride(RenderStateOverride::BlendEnable),
                              !HasOverride(RenderStateOverride::BlendMode));

    // @queue: integer render queue — skip if overridden or builtin
    if (!m_builtin && !HasOverride(RenderStateOverride::RenderQueue) && queue >= 0 &&
        queue != m_renderState.renderQueue) {
        m_renderState.renderQueue = queue;
        changed = true;
    }

    // @pass_tag: set material pass tag for draw call filtering
    // Skip for builtin materials — same reason as renderQueue above.
    if (!m_builtin && !passTag.empty() && passTag != m_passTag) {
        m_passTag = passTag;
    }

    // @stencil: compare_op, ref, pass_op, fail_op, depth_fail_op
    changed |= ApplyStencilMeta(m_renderState, stencil);

    if (changed) {
        m_pipelineDirty = true;
    }

    // @alpha_clip: <threshold> — skip if user has overridden AlphaClip
    if (!alphaClip.empty() && alphaClip != "off" && !HasOverride(RenderStateOverride::AlphaClip)) {
        m_renderState.alphaClipEnabled = true;
        try {
            m_renderState.alphaClipThreshold = std::stof(alphaClip);
        } catch (...) {
            m_renderState.alphaClipThreshold = 0.5f;
        }
        SyncAlphaClipProperty();
    } else if ((alphaClip.empty() || alphaClip == "off") && !HasOverride(RenderStateOverride::AlphaClip)) {
        if (m_renderState.alphaClipEnabled) {
            m_renderState.alphaClipEnabled = false;
            SyncAlphaClipProperty();
        }
    }
}

void InxMaterial::SyncAlphaClipProperty()
{
    float value = m_renderState.alphaClipEnabled ? m_renderState.alphaClipThreshold : 0.0f;
    SetFloat("_AlphaClipThreshold", value);
}

nlohmann::json InxMaterial::SerializeDocument() const
{
    json j;
    j["material_version"] = 3;
    j["name"] = m_name;
    j["builtin"] = m_builtin;

    // Shader identity — separate vertex/fragment keys.
    j["shaders"]["vertex"] = m_vertShaderName;
    j["shaders"]["fragment"] = m_fragShaderName;

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
        auto stencilOpToJson = [](const VkStencilOpState &op) {
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
    m_vertShaderName = std::move(staged.m_vertShaderName);
    m_fragShaderName = std::move(staged.m_fragShaderName);
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

bool InxMaterial::ApplyDocument(const nlohmann::json &j)
{
    try {
        material_document_validation::ValidateMaterialDocument(j);
        m_name = j["name"].get<std::string>();
        m_builtin = j["builtin"].get<bool>();

        const auto &shaders = j["shaders"];
        m_vertShaderName = shaders["vertex"].get<std::string>();
        m_fragShaderName = shaders["fragment"].get<std::string>();

        const auto &rs = j["renderState"];
        m_renderState.cullMode = static_cast<VkCullModeFlags>(rs["cullMode"].get<int>());
        m_renderState.frontFace = static_cast<VkFrontFace>(rs["frontFace"].get<int>());
        m_renderState.polygonMode = static_cast<VkPolygonMode>(rs["polygonMode"].get<int>());
        m_renderState.lineWidth = rs["lineWidth"].get<float>();
        m_renderState.depthBiasEnable = rs["depthBiasEnable"].get<bool>();
        m_renderState.depthBiasConstantFactor = rs["depthBiasConstantFactor"].get<float>();
        m_renderState.depthBiasSlopeFactor = rs["depthBiasSlopeFactor"].get<float>();
        m_renderState.depthBiasClamp = rs["depthBiasClamp"].get<float>();
        m_renderState.topology = static_cast<VkPrimitiveTopology>(rs["topology"].get<int>());
        m_renderState.depthTestEnable = rs["depthTestEnable"].get<bool>();
        m_renderState.depthWriteEnable = rs["depthWriteEnable"].get<bool>();
        m_renderState.depthCompareOp = static_cast<VkCompareOp>(rs["depthCompareOp"].get<int>());
        m_renderState.blendEnable = rs["blendEnable"].get<bool>();
        m_renderState.srcColorBlendFactor = static_cast<VkBlendFactor>(rs["srcColorBlendFactor"].get<int>());
        m_renderState.dstColorBlendFactor = static_cast<VkBlendFactor>(rs["dstColorBlendFactor"].get<int>());
        m_renderState.colorBlendOp = static_cast<VkBlendOp>(rs["colorBlendOp"].get<int>());
        m_renderState.srcAlphaBlendFactor = static_cast<VkBlendFactor>(rs["srcAlphaBlendFactor"].get<int>());
        m_renderState.dstAlphaBlendFactor = static_cast<VkBlendFactor>(rs["dstAlphaBlendFactor"].get<int>());
        m_renderState.alphaBlendOp = static_cast<VkBlendOp>(rs["alphaBlendOp"].get<int>());
        m_renderState.alphaClipEnabled = rs["alphaClipEnabled"].get<bool>();
        m_renderState.alphaClipThreshold = rs["alphaClipThreshold"].get<float>();
        m_renderState.renderQueue = rs["renderQueue"].get<int32_t>();
        m_renderState.stencilTestEnable = rs["stencilTestEnable"].get<bool>();
        m_renderState.stencilFront = {};
        m_renderState.stencilBack = {};
        if (m_renderState.stencilTestEnable) {
            const auto jsonToStencilOp = [](const json &state) {
                VkStencilOpState result{};
                result.failOp = static_cast<VkStencilOp>(state["failOp"].get<int>());
                result.passOp = static_cast<VkStencilOp>(state["passOp"].get<int>());
                result.depthFailOp = static_cast<VkStencilOp>(state["depthFailOp"].get<int>());
                result.compareOp = static_cast<VkCompareOp>(state["compareOp"].get<int>());
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
                prop.value = RequireTextureGuid(propJson["guid"].get<std::string>());
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
    material->SetVertShader("standard");
    material->SetFragShader("lit");

    // Default lit opaque render state
    RenderState state;
    state.cullMode = VK_CULL_MODE_BACK_BIT;
    state.frontFace = VK_FRONT_FACE_CLOCKWISE;
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
    material->SetVertShader("standard");
    material->SetFragShader("unlit");

    // Default unlit opaque render state
    RenderState state;
    state.cullMode = VK_CULL_MODE_BACK_BIT;
    state.frontFace = VK_FRONT_FACE_CLOCKWISE;
    state.depthTestEnable = true;
    state.depthWriteEnable = true;
    state.blendEnable = false;
    state.renderQueue = 2000; // Opaque queue
    material->SetRenderState(state);

    // Default property from shader annotation: baseColor
    material->SetColor("baseColor", glm::vec4(1.0f, 1.0f, 1.0f, 1.0f));

    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateParticleBillboardMaterial()
{
    auto material = std::make_shared<InxMaterial>("ParticleBillboardMaterial");
    material->SetShader("particle_billboard");

    RenderState state;
    state.cullMode = VK_CULL_MODE_NONE;
    state.frontFace = VK_FRONT_FACE_CLOCKWISE;
    state.depthTestEnable = true;
    state.depthWriteEnable = false;
    state.blendEnable = true;
    state.srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;
    state.dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    state.colorBlendOp = VK_BLEND_OP_ADD;
    state.srcAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
    state.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    state.alphaBlendOp = VK_BLEND_OP_ADD;
    state.renderQueue = 3000;
    material->SetRenderState(state);
    material->SetBuiltin(true);
    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateGizmoMaterial()
{
    auto material = std::make_shared<InxMaterial>("GizmoMaterial");

    // Use gizmo shader (simple unlit with vertex color)
    material->SetShader("gizmo");

    // Gizmo render state: no culling (double-sided), depth test, depth write
    RenderState state;
    state.cullMode = VK_CULL_MODE_NONE; // Double-sided for grid visibility
    state.frontFace = VK_FRONT_FACE_CLOCKWISE;
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

    material->SetShader("Infernux/Grid");

    // Grid render state: double-sided, alpha-blended, depth test but no depth write
    RenderState state;
    state.cullMode = VK_CULL_MODE_NONE;
    state.frontFace = VK_FRONT_FACE_CLOCKWISE;
    state.depthTestEnable = true;
    state.depthWriteEnable = false; // Transparent — don't write depth
    state.depthCompareOp = VK_COMPARE_OP_LESS_OR_EQUAL;
    // Keep only a tiny constant bias. Slope-scaled bias explodes at grazing
    // angles when the editor camera is close to the XZ plane, which produces
    // driver-dependent stair-step artifacts on older AMD GPUs.
    state.depthBiasEnable = false;
    state.depthBiasConstantFactor = 0.0f;
    state.depthBiasSlopeFactor = 0.0f;
    state.depthBiasClamp = 0.0f;
    state.blendEnable = true;
    state.srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;
    state.dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    state.colorBlendOp = VK_BLEND_OP_ADD;
    // Alpha channel: preserve destination alpha (1.0 from opaques/skybox) so
    // the scene texture stays fully opaque when displayed in ImGui viewport.
    state.srcAlphaBlendFactor = VK_BLEND_FACTOR_ZERO;
    state.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
    state.alphaBlendOp = VK_BLEND_OP_ADD;
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
    material->SetShader("gizmo");

    // Editor tools render state: always on top (no depth test), double-sided
    RenderState state;
    state.cullMode = VK_CULL_MODE_NONE;
    state.frontFace = VK_FRONT_FACE_CLOCKWISE;
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
    material->SetShader("gizmo");

    // Component gizmos: depth-tested (occluded by scene geometry), double-sided, LINE topology
    RenderState state;
    state.cullMode = VK_CULL_MODE_NONE;
    state.frontFace = VK_FRONT_FACE_CLOCKWISE;
    state.topology = VK_PRIMITIVE_TOPOLOGY_LINE_LIST;
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
    return CreateTexturedComponentGizmoIconMaterial("ComponentGizmoLightIconMaterial", "icons/gizmo_light.png");
}

std::shared_ptr<InxMaterial> InxMaterial::CreateSkyboxProceduralMaterial()
{
    auto material = std::make_shared<InxMaterial>("SkyboxProcedural");

    // Use procedural skybox shader (registered by @shader_id in .vert/.frag)
    material->SetShader("Infernux/Skybox-Procedural");

    // Skybox render state:
    // - Cull back faces (the outside of the cube). In the LH coordinate system,
    //   CW winding is front-facing. From inside the cube the camera sees indices
    //   wound CW → front faces. Back-face culling removes the outside faces.
    // - No depth write (skybox should always be behind everything)
    // - Depth test <= (skybox writes z=1.0, passes only where nothing closer exists)
    // - renderQueue = EngineConfig::skyboxQueue (last; after opaque+transparent
    //   and outside the shadow-caster range so it never casts shadows)
    RenderState state;
    state.cullMode = VK_CULL_MODE_BACK_BIT;
    state.frontFace = VK_FRONT_FACE_CLOCKWISE;
    state.depthTestEnable = true;
    state.depthWriteEnable = false;
    state.depthCompareOp = VK_COMPARE_OP_LESS_OR_EQUAL;
    state.blendEnable = false;
    state.renderQueue = EngineConfig::Get().skyboxQueue; // After all opaque/transparent, outside shadow caster range
    material->SetRenderState(state);

    // Default sky properties (matching shader @property annotations)
    material->SetColor("skyTopColor", glm::vec4(0.20f, 0.28f, 0.46f, 1.0f));
    material->SetColor("skyHorizonColor", glm::vec4(0.50f, 0.58f, 0.70f, 1.0f));
    material->SetColor("groundColor", glm::vec4(0.24f, 0.22f, 0.22f, 1.0f));
    material->SetFloat("exposure", 1.35f);

    material->SetBuiltin(true);

    return material;
}

std::shared_ptr<InxMaterial> InxMaterial::CreateErrorMaterial()
{
    auto material = std::make_shared<InxMaterial>("ErrorMaterial");

    // Use dedicated error shaders: unlit magenta-black checkerboard pattern.
    // These shaders are self-contained (no material UBO, no textures) and
    // output a procedural checkerboard using world-position + UV.
    material->SetShader("error");

    // Double-sided so the error pattern is visible from all angles
    RenderState state;
    state.cullMode = VK_CULL_MODE_NONE;
    state.frontFace = VK_FRONT_FACE_CLOCKWISE;
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
    clone->m_vertShaderName = m_vertShaderName;
    clone->m_fragShaderName = m_fragShaderName;
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
