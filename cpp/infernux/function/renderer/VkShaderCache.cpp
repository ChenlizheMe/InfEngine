/**
 * @file VkShaderCache.cpp
 * @brief Implementation of VkShaderCache — shader module management and SPIR-V code cache.
 */

#include "VkShaderCache.h"
#include "InxError.h"
#include "vk/VkCore.h"

#include <algorithm>
#include <cstring>

namespace infernux
{

// ============================================================================
// Module Management
// ============================================================================

void VkShaderCache::LoadShader(const char *name, const std::vector<char> &spirvCode, const char *type,
                               vk::VkPipelineManager &pm)
{
    std::vector<uint32_t> code(spirvCode.size() / sizeof(uint32_t));
    std::memcpy(code.data(), spirvCode.data(), spirvCode.size());

    VkShaderModule module = pm.CreateShaderModule(code);
    if (module == VK_NULL_HANDLE) {
        INXLOG_ERROR("VkShaderCache: failed to load shader: ", name);
        return;
    }

    std::string typeStr(type);
    if (typeStr == "vert" || typeStr == "vertex") {
        m_vertModules[name] = module;
        m_vertCodes[name] = spirvCode;
    } else if (typeStr == "frag" || typeStr == "fragment") {
        m_fragModules[name] = module;
        m_fragCodes[name] = spirvCode;
    } else {
        INXLOG_WARN("VkShaderCache: unknown shader type: ", type);
        pm.DestroyShaderModule(module);
    }
}

void VkShaderCache::UnloadShader(const char *name, VkDevice device)
{
    std::string nameStr(name);
    m_renderMetas.erase(nameStr);

    auto vertIt = m_vertModules.find(nameStr);
    if (vertIt != m_vertModules.end()) {
        vkDestroyShaderModule(device, vertIt->second, nullptr);
        m_vertModules.erase(vertIt);
    }
    m_vertCodes.erase(nameStr);

    auto fragIt = m_fragModules.find(nameStr);
    if (fragIt != m_fragModules.end()) {
        vkDestroyShaderModule(device, fragIt->second, nullptr);
        m_fragModules.erase(fragIt);
    }
    m_fragCodes.erase(nameStr);
}

bool VkShaderCache::HasShader(const std::string &name, const std::string &type) const
{
    if (type == "vert" || type == "vertex") {
        return m_vertModules.find(name) != m_vertModules.end();
    }
    if (type == "frag" || type == "fragment") {
        return m_fragModules.find(name) != m_fragModules.end();
    }
    return false;
}

VkShaderModule VkShaderCache::GetModule(const std::string &name, const std::string &type) const
{
    const auto &map = (type == "vertex") ? m_vertModules : m_fragModules;
    auto it = map.find(name);
    if (it != map.end())
        return it->second;
    return VK_NULL_HANDLE;
}

// ============================================================================
// Render-State Annotations
// ============================================================================

void VkShaderCache::StoreRenderMeta(const std::string &shaderId, const std::string &cullMode,
                                    const std::string &depthWrite, const std::string &depthTest,
                                    const std::string &blend, int queue, const std::string &passTag,
                                    const std::string &stencil, const std::string &alphaClip)
{
    ShaderRenderMeta meta;
    meta.cullMode = cullMode;
    meta.depthWrite = depthWrite;
    meta.depthTest = depthTest;
    meta.blend = blend;
    meta.queue = queue;
    meta.passTag = passTag;
    meta.stencil = stencil;
    meta.alphaClip = alphaClip;
    m_renderMetas[shaderId] = meta;
}

const ShaderRenderMeta *VkShaderCache::GetRenderMeta(const std::string &shaderId) const
{
    auto it = m_renderMetas.find(shaderId);
    return (it != m_renderMetas.end()) ? &it->second : nullptr;
}

// ============================================================================
// SPIR-V Code Lookup
// ============================================================================

const std::vector<char> *VkShaderCache::FindCodeInMap(const std::unordered_map<std::string, std::vector<char>> &map,
                                                      const std::string &path)
{
    // Try exact match first
    auto it = map.find(path);
    if (it != map.end())
        return &it->second;

    // Extract filename from path
    size_t lastSlash = path.find_last_of("/\\");
    std::string filename = (lastSlash != std::string::npos) ? path.substr(lastSlash + 1) : path;

    // Try with filename (with extension)
    it = map.find(filename);
    if (it != map.end())
        return &it->second;

    // Try without extension (shader_id style: "123" instead of "123.frag")
    size_t dotPos = filename.find_last_of('.');
    if (dotPos != std::string::npos) {
        std::string nameWithoutExt = filename.substr(0, dotPos);
        it = map.find(nameWithoutExt);
        if (it != map.end())
            return &it->second;
    }

    return nullptr;
}

const std::vector<char> *VkShaderCache::FindVertCode(const std::string &id) const
{
    return FindCodeInMap(m_vertCodes, id);
}

const std::vector<char> *VkShaderCache::FindFragCode(const std::string &id) const
{
    return FindCodeInMap(m_fragCodes, id);
}

ShaderProgramArtifactPublishResult VkShaderCache::PublishProgramArtifact(const ShaderProgramArtifact &artifact)
{
    ShaderProgramArtifactPublishResult result;
    if (!artifact.IsValid()) {
        INXLOG_ERROR("VkShaderCache: rejected invalid shader program artifact");
        return result;
    }

    const auto existing = m_programArtifacts.find(artifact.key.stages);
    const bool sameRevision = existing != m_programArtifacts.end() && existing->second.key == artifact.key;
    if (sameRevision) {
        const bool fullyMaterialized = std::all_of(artifact.variants.begin(), artifact.variants.end(),
                                                   [&](const ShaderProgramArtifact::PassVariant &variant) {
                                                       return m_programCache.HasProgram({artifact.key, variant.target});
                                                   });
        if (fullyMaterialized) {
            result.accepted = true;
            return result;
        }
    }

    // Validate and materialize all Vulkan program-level resources before the
    // current artifact is replaced. A failed publish removes the incomplete
    // revision and leaves last-known-good live.
    if (!artifact.FindVariant(ShaderCompileTarget::Forward)) {
        INXLOG_ERROR("VkShaderCache: shader program artifact has no Forward variant");
        return result;
    }

    for (const auto &variant : artifact.variants) {
        const ShaderProgramVariantKey variantKey{artifact.key, variant.target};
        ShaderProgram *program =
            m_programCache.GetOrCreateProgram(variantKey, variant.vertexSpirv, variant.fragmentSpirv);
        if (!program || !program->IsValid()) {
            INXLOG_ERROR("VkShaderCache: failed to materialize shader program variant '", variantKey.ToString(), "'");
            (void)m_programCache.TakePrograms(artifact.key);
            return result;
        }
    }

    if (sameRevision) {
        result.accepted = true;
        result.changed = true;
        return result;
    }

    if (existing != m_programArtifacts.end())
        result.replacedProgram = existing->second.key;
    m_programArtifacts[artifact.key.stages] = artifact;
    result.accepted = true;
    result.changed = true;
    return result;
}

const ShaderProgramArtifact *VkShaderCache::FindProgramArtifact(const ShaderStagePair &stages) const
{
    const auto found = m_programArtifacts.find(stages);
    return found != m_programArtifacts.end() ? &found->second : nullptr;
}

// ============================================================================
// Lifecycle
// ============================================================================

void VkShaderCache::DestroyModules(vk::VkPipelineManager &pm)
{
    for (auto &[name, shader] : m_vertModules)
        pm.DestroyShaderModule(shader);
    for (auto &[name, shader] : m_fragModules)
        pm.DestroyShaderModule(shader);
}

void VkShaderCache::Clear()
{
    m_programCache.Clear();
    m_programArtifacts.clear();
    m_vertCodes.clear();
    m_fragCodes.clear();
    m_vertModules.clear();
    m_fragModules.clear();
    m_renderMetas.clear();
}

// ============================================================================
// Debug Helpers
// ============================================================================

void VkShaderCache::DumpAvailableKeys(std::string &outVert, std::string &outFrag) const
{
    outVert.clear();
    outFrag.clear();
    for (const auto &kv : m_vertCodes)
        outVert += " [" + kv.first + "]";
    for (const auto &kv : m_fragCodes)
        outFrag += " [" + kv.first + "]";
}

} // namespace infernux
