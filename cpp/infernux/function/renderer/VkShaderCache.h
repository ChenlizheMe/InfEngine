/**
 * @file VkShaderCache.h
 * @brief Extracted shader-module and SPIR-V code cache from InxVkCoreModular.
 *
 * Owns VkShaderModule handles, raw SPIR-V code bytes, shader render-state
 * annotations, and the ShaderProgramCache.  InxVkCoreModular delegates
 * all shader-related data access to this class.
 */

#pragma once

#include <function/renderer/shader/ShaderProgram.h>

#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux
{

namespace vk
{
class VkPipelineManager;
}

/// Shader render-state annotations parsed from @cull, @depth_write, etc.
/// Keyed by shader_id (e.g. "lit", "unlit"). Applied to materials before
/// pipeline creation so shader authors can control GPU state via annotations.
struct ShaderRenderMeta
{
    std::string cullMode;   // "back", "front", "none" (empty = default)
    std::string depthWrite; // "on", "off" (empty = default)
    std::string depthTest;  // "on", "off", "less", "less_equal", "always", "never" (empty = default)
    std::string blend;      // "off", "alpha", "additive" (empty = default)
    int queue = -1;         // -1 = use material default
    std::string passTag;    // "opaque", "transparent", etc. (empty = matches all)
    std::string stencil;    // "compare,ref,pass,fail,zfail" (empty = no stencil)
    std::string alphaClip;  // "off" or threshold string e.g. "0.5" (empty = default)
};

/**
 * @brief Manages all shader modules, SPIR-V code, render-state annotations,
 *        and the ShaderProgramCache.
 *
 * This is a data-owning helper class — it does NOT call vkDeviceWaitIdle or
 * interact with MaterialPipelineManager.  Those cross-system orchestrations
 * remain on InxVkCoreModular.
 */
class VkShaderCache
{
  public:
    VkShaderCache() = default;
    ~VkShaderCache() = default;

    VkShaderCache(const VkShaderCache &) = delete;
    VkShaderCache &operator=(const VkShaderCache &) = delete;

    // ── Module Management ──────────────────────────────────────────────────

    /// Load a shader (vert/frag) from SPIR-V code, creating a VkShaderModule.
    void LoadShader(const char *name, const std::vector<char> &spirvCode, const char *type, vk::VkPipelineManager &pm);

    /// Unload (destroy) a shader module and erase its cached code/meta.
    void UnloadShader(const char *name, VkDevice device);

    /// Check if a module exists for the given name and type ("vert"/"frag").
    [[nodiscard]] bool HasShader(const std::string &name, const std::string &type) const;

    /// Retrieve a VkShaderModule handle (VK_NULL_HANDLE if not found).
    [[nodiscard]] VkShaderModule GetModule(const std::string &name, const std::string &type) const;

    // ── Render-State Annotations ───────────────────────────────────────────

    void StoreRenderMeta(const std::string &shaderId, const std::string &cullMode, const std::string &depthWrite,
                         const std::string &depthTest, const std::string &blend, int queue,
                         const std::string &passTag = "", const std::string &stencil = "",
                         const std::string &alphaClip = "");

    /// Get render-state annotations (nullptr if none stored).
    [[nodiscard]] const ShaderRenderMeta *GetRenderMeta(const std::string &shaderId) const;

    // ── SPIR-V Code Lookup ─────────────────────────────────────────────────

    /// Find vertex SPIR-V code by exact name, filename, or stem.
    [[nodiscard]] const std::vector<char> *FindVertCode(const std::string &id) const;

    /// Find fragment SPIR-V code by exact name, filename, or stem.
    [[nodiscard]] const std::vector<char> *FindFragCode(const std::string &id) const;

    // ── ShaderProgramCache Access ──────────────────────────────────────────

    [[nodiscard]] ShaderProgramCache &GetProgramCache()
    {
        return m_programCache;
    }
    [[nodiscard]] const ShaderProgramCache &GetProgramCache() const
    {
        return m_programCache;
    }

    // ── Lifecycle ──────────────────────────────────────────────────────────

    /// Destroy all shader modules via VkPipelineManager (removes from tracking).
    void DestroyModules(vk::VkPipelineManager &pm);

    /// Clear all data containers (modules should already be destroyed).
    void Clear();

    // ── Debug Helpers ──────────────────────────────────────────────────────

    /// Dump available shader code keys (for diagnostics).
    void DumpAvailableKeys(std::string &outVert, std::string &outFrag) const;

  private:
    /// Path-aware lookup: tries exact match, filename, then stem.
    static const std::vector<char> *FindCodeInMap(const std::unordered_map<std::string, std::vector<char>> &map,
                                                  const std::string &path);

    std::unordered_map<std::string, VkShaderModule> m_vertModules;
    std::unordered_map<std::string, VkShaderModule> m_fragModules;
    std::unordered_map<std::string, std::vector<char>> m_vertCodes;
    std::unordered_map<std::string, std::vector<char>> m_fragCodes;
    std::unordered_map<std::string, ShaderRenderMeta> m_renderMetas;
    ShaderProgramCache m_programCache;
    mutable std::mutex m_mutex;
};

} // namespace infernux
