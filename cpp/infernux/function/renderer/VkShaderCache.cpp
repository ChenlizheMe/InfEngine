/**
 * @file VkShaderCache.cpp
 * @brief Implementation of VkShaderCache — shader module management and SPIR-V code cache.
 */

#include "VkShaderCache.h"
#include "InxError.h"
#include "vk/VkCore.h"
#include <platform/filesystem/InxPath.h>

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
        const auto existing = m_vertModules.find(name);
        const VkShaderModule replaced = existing == m_vertModules.end() ? VK_NULL_HANDLE : existing->second;
        m_vertModules[name] = module;
        m_vertCodes[name] = spirvCode;
        if (replaced != VK_NULL_HANDLE)
            pm.DestroyShaderModule(replaced);
    } else if (typeStr == "frag" || typeStr == "fragment") {
        const auto existing = m_fragModules.find(name);
        const VkShaderModule replaced = existing == m_fragModules.end() ? VK_NULL_HANDLE : existing->second;
        m_fragModules[name] = module;
        m_fragCodes[name] = spirvCode;
        if (replaced != VK_NULL_HANDLE)
            pm.DestroyShaderModule(replaced);
    } else {
        INXLOG_WARN("VkShaderCache: unknown shader type: ", type);
        pm.DestroyShaderModule(module);
    }
}

void VkShaderCache::UnloadShader(const char *name, vk::VkPipelineManager &pm, const std::string &shaderType)
{
    std::string nameStr(name);
    // A stage reload must not erase a same-name partner stage. Render metadata
    // belongs to the fragment stage; whole-shader removal still clears both.
    if (shaderType.empty() || shaderType == "vertex") {
        auto vertIt = m_vertModules.find(nameStr);
        if (vertIt != m_vertModules.end()) {
            pm.DestroyShaderModule(vertIt->second);
            m_vertModules.erase(vertIt);
        }
        m_vertCodes.erase(nameStr);
    }
    if (shaderType.empty() || shaderType == "fragment") {
        m_renderMetas.erase(nameStr);
        auto fragIt = m_fragModules.find(nameStr);
        if (fragIt != m_fragModules.end()) {
            pm.DestroyShaderModule(fragIt->second);
            m_fragModules.erase(fragIt);
        }
        m_fragCodes.erase(nameStr);
    }
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
    std::string filename = PortablePathFilename(path);

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

uint64_t VkShaderCache::GetCodeFingerprint(const std::string &name, const std::string &type) const
{
    const std::vector<char> *code = nullptr;
    if (type == "vert" || type == "vertex")
        code = FindVertCode(name);
    else if (type == "frag" || type == "fragment")
        code = FindFragCode(name);
    if (!code || code->empty())
        return 0;

    uint64_t hash = 1469598103934665603ull;
    for (const char value : *code) {
        hash ^= static_cast<uint8_t>(value);
        hash *= 1099511628211ull;
    }
    return hash == 0 ? 1 : hash;
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
    if (sameRevision && m_programCache.HasProgram({artifact.key, ShaderCompileTarget::Forward})) {
        result.accepted = true;
        return result;
    }

    // Validate the mandatory Forward program before replacing last-known-good.
    // Optional semantic passes stay as SPIR-V until their first real consumer.
    const auto *forward = artifact.FindVariant(ShaderCompileTarget::Forward);
    if (!forward) {
        INXLOG_ERROR("VkShaderCache: shader program artifact has no Forward variant");
        return result;
    }

    const ShaderProgramVariantKey forwardKey{artifact.key, ShaderCompileTarget::Forward};
    ShaderProgramPublication forwardProgram =
        m_programCache.GetOrCreateProgram(forwardKey, forward->vertexSpirv, forward->fragmentSpirv);
    if (!forwardProgram || !forwardProgram->IsValid()) {
        INXLOG_ERROR("VkShaderCache: failed to materialize shader program variant '", forwardKey.ToString(), "'");
        (void)m_programCache.TakePrograms(artifact.key);
        return result;
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

ShaderProgramPublication VkShaderCache::MaterializeProgramVariant(const ShaderStagePair &stages,
                                                                  ShaderCompileTarget target)
{
    const auto artifact = m_programArtifacts.find(stages);
    if (artifact == m_programArtifacts.end())
        return nullptr;

    const auto *variant = artifact->second.FindVariant(target);
    if (!variant)
        return nullptr;

    const ShaderProgramVariantKey key{artifact->second.key, target};
    ShaderProgramPublication program =
        m_programCache.GetOrCreateProgram(key, variant->vertexSpirv, variant->fragmentSpirv);
    if (!program || !program->IsValid()) {
        INXLOG_ERROR("VkShaderCache: failed to lazily materialize shader program variant '", key.ToString(), "'");
        return nullptr;
    }
    return program;
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
