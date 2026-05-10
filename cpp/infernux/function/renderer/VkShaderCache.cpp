/**
 * @file VkShaderCache.cpp
 * @brief Implementation of VkShaderCache — shader module management and SPIR-V code cache.
 */

#include "VkShaderCache.h"
#include "InxError.h"
#include "vk/VkCore.h"

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
    std::lock_guard<std::mutex> lock(m_mutex);
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
    std::lock_guard<std::mutex> lock(m_mutex);
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
    std::lock_guard<std::mutex> lock(m_mutex);
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
    std::lock_guard<std::mutex> lock(m_mutex);
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
    std::lock_guard<std::mutex> lock(m_mutex);
    m_renderMetas[shaderId] = meta;
}

const ShaderRenderMeta *VkShaderCache::GetRenderMeta(const std::string &shaderId) const
{
    std::lock_guard<std::mutex> lock(m_mutex);
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
    std::lock_guard<std::mutex> lock(m_mutex);
    return FindCodeInMap(m_vertCodes, id);
}

const std::vector<char> *VkShaderCache::FindFragCode(const std::string &id) const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    return FindCodeInMap(m_fragCodes, id);
}

// ============================================================================
// Lifecycle
// ============================================================================

void VkShaderCache::DestroyModules(vk::VkPipelineManager &pm)
{
    std::lock_guard<std::mutex> lock(m_mutex);
    for (auto &[name, shader] : m_vertModules)
        pm.DestroyShaderModule(shader);
    for (auto &[name, shader] : m_fragModules)
        pm.DestroyShaderModule(shader);
}

void VkShaderCache::Clear()
{
    std::lock_guard<std::mutex> lock(m_mutex);
    m_programCache.Clear();
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
    std::lock_guard<std::mutex> lock(m_mutex);
    outVert.clear();
    outFrag.clear();
    for (const auto &kv : m_vertCodes)
        outVert += " [" + kv.first + "]";
    for (const auto &kv : m_fragCodes)
        outFrag += " [" + kv.first + "]";
}

} // namespace infernux
