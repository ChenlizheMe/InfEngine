/**
 * @file VkTextureCache.h
 * @brief Extracted GPU texture cache from InxVkCoreModular.
 *
 * Owns the `name → VkTexture` map and its mutex.  Simple CRUD
 * operations live here; complex resolution logic (GUID lookup,
 * import-setting parsing) remains on InxVkCoreModular.
 */

#pragma once

#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vulkan/vulkan.h>

namespace infernux
{

namespace vk
{
class VkTexture;
class VkResourceManager;
} // namespace vk

/**
 * @brief Thread-safe cache of GPU textures keyed by name/GUID.
 */
class VkTextureCache
{
  public:
    VkTextureCache() = default;
    ~VkTextureCache() = default;

    VkTextureCache(const VkTextureCache &) = delete;
    VkTextureCache &operator=(const VkTextureCache &) = delete;

    // ── Simple Loaders ─────────────────────────────────────────────────────

    /// Load a texture from disk and store under @p name.
    void CreateTextureImage(const std::string &name, const std::string &path, vk::VkResourceManager &rm);

    /// Create a 1×1 white texture and store under @p name.
    void CreateDefaultWhiteTexture(const std::string &name, vk::VkResourceManager &rm);

    /// Create a 1×1 solid-color texture (arbitrary RGBA + format).
    void CreateSolidColorTexture(const std::string &name, uint8_t r, uint8_t g, uint8_t b, uint8_t a, VkFormat format,
                                 vk::VkResourceManager &rm);

    // ── Cache Operations ───────────────────────────────────────────────────

    /// Insert a pre-loaded texture into the cache (thread-safe, moves ownership).
    void Insert(const std::string &key, std::unique_ptr<vk::VkTexture> texture);

    /// Look up a cached texture; returns nullptr if not found (thread-safe).
    [[nodiscard]] vk::VkTexture *Find(const std::string &key) const;

    /// Remove all cache entries whose key starts with @p prefix (thread-safe).
    /// Returns the number of entries removed.
    size_t EvictByPrefix(const std::string &prefix);

    /// Clear all entries.
    void Clear();

    /// Acquire the internal mutex for multi-step atomic operations.
    [[nodiscard]] std::unique_lock<std::mutex> Lock() const
    {
        return std::unique_lock<std::mutex>(m_mutex);
    }

  private:
    std::unordered_map<std::string, std::unique_ptr<vk::VkTexture>> m_textures;
    mutable std::mutex m_mutex;
};

} // namespace infernux
