/**
 * @file VkTextureCache.cpp
 * @brief Implementation of VkTextureCache — simple GPU texture CRUD.
 */

#include "VkTextureCache.h"
#include "InxError.h"
#include "vk/VkHandle.h"
#include "vk/VkResourceManager.h"

namespace infernux
{

// ============================================================================
// Simple Loaders
// ============================================================================

void VkTextureCache::CreateTextureImage(const std::string &name, const std::string &path, vk::VkResourceManager &rm)
{
    auto texture = rm.LoadTexture(path);
    if (texture) {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_textures[name] = std::move(texture);
        INXLOG_INFO("VkTextureCache: loaded texture: ", name);
    }
}

void VkTextureCache::CreateDefaultWhiteTexture(const std::string &name, vk::VkResourceManager &rm)
{
    auto texture = rm.CreateSolidColorTexture(1, 1, 255, 255, 255, 255);
    if (texture) {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_textures[name] = std::move(texture);
        INXLOG_INFO("VkTextureCache: created default white texture: ", name);
    }
}

void VkTextureCache::CreateSolidColorTexture(const std::string &name, uint8_t r, uint8_t g, uint8_t b, uint8_t a,
                                             VkFormat format, vk::VkResourceManager &rm)
{
    auto texture = rm.CreateSolidColorTexture(1, 1, r, g, b, a, format);
    if (texture) {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_textures[name] = std::move(texture);
    }
}

// ============================================================================
// Cache Operations
// ============================================================================

void VkTextureCache::Insert(const std::string &key, std::unique_ptr<vk::VkTexture> texture)
{
    std::lock_guard<std::mutex> lock(m_mutex);
    m_textures[key] = std::move(texture);
}

vk::VkTexture *VkTextureCache::Find(const std::string &key) const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    auto it = m_textures.find(key);
    if (it != m_textures.end() && it->second)
        return it->second.get();
    return nullptr;
}

size_t VkTextureCache::EvictByPrefix(const std::string &prefix)
{
    std::lock_guard<std::mutex> lock(m_mutex);
    std::vector<std::string> keysToRemove;
    for (const auto &[key, tex] : m_textures) {
        if (key == prefix || key.rfind(prefix + "::", 0) == 0) {
            keysToRemove.push_back(key);
        }
    }
    for (const auto &key : keysToRemove) {
        INXLOG_DEBUG("VkTextureCache: evicting: ", key);
        m_textures.erase(key);
    }
    return keysToRemove.size();
}

void VkTextureCache::Clear()
{
    std::lock_guard<std::mutex> lock(m_mutex);
    m_textures.clear();
}

} // namespace infernux
