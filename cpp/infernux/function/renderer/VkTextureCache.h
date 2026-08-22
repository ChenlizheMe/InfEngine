/**
 * @file VkTextureCache.h
 * @brief Extracted GPU texture cache from InxVkCoreModular.
 *
 * Owns the `name -> RHI TextureResource` map and its mutex. Simple CRUD
 * operations live here; complex resolution
 * logic (GUID lookup, import-setting parsing) remains on InxVkCoreModular.
 */

#pragma once

#include "GpuResidency.h"
#include "rhi/RhiTexture.h"
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux
{

namespace vk
{
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

    /// Create a 1×1 white texture and store under @p name.
    void CreateDefaultWhiteTexture(const std::string &name, vk::VkResourceManager &rm);

    /// Create a 1×1 solid-color texture (arbitrary RGBA + format).
    void CreateSolidColorTexture(const std::string &name, uint8_t r, uint8_t g, uint8_t b, uint8_t a,
                                 vk::VkResourceManager &rm);

    // ── Cache Operations ───────────────────────────────────────────────────

    /// Insert a pre-loaded texture into the cache (thread-safe, shares ownership).
    [[nodiscard]] std::shared_ptr<rhi::TextureGpuViewSlot> Insert(const std::string &key,
                                                                  std::shared_ptr<rhi::TextureResource> texture,
                                                                  uint64_t lastUsedFrame, bool permanentlyPinned,
                                                                  std::string assetGuid, uint64_t runtimeVersion);

    /// Look up and lease a cached texture; returns nullptr if not found.
    [[nodiscard]] std::shared_ptr<rhi::TextureGpuViewSlot> Find(const std::string &key, uint64_t frame = 0);
    [[nodiscard]] std::shared_ptr<rhi::TextureGpuViewSlot>
    FindAsset(const std::string &key, const std::string &assetGuid, uint64_t runtimeVersion, uint64_t frame);

    /// Advance the renderer-owned residency clock even when no texture lookup
    /// occurs, so current-frame protection never turns into permanent pinning.
    void AdvanceFrame(uint64_t frame);

    /// Mark every resident variant of an asset as awaiting a newer runtime
    /// revision while preserving its last-known-good publication.
    [[nodiscard]] size_t RequestAssetRevision(const std::string &assetGuid, uint64_t runtimeVersion);

    /// Remove all cache entries whose key starts with @p prefix (thread-safe).
    /// Returns the number of entries removed.
    size_t EvictByPrefix(const std::string &prefix);
    void SetBudgetBytes(uint64_t bytes);
    [[nodiscard]] size_t TrimToBudget();
    [[nodiscard]] uint64_t GetBudgetBytes() const;
    [[nodiscard]] uint64_t GetResidentBytes() const;
    [[nodiscard]] size_t GetEntryCount() const;
    [[nodiscard]] size_t GetRetiredLeaseCount() const;
    [[nodiscard]] uint64_t GetEvictionCount() const;
    [[nodiscard]] uint64_t GetRetiredLeaseBytes() const;
    [[nodiscard]] GpuEvictionCandidate PeekOldestEvictable() const;
    [[nodiscard]] uint64_t EvictOldest();
    [[nodiscard]] std::vector<GpuAssetResidencyRecord> GetAssetResidency() const;

    /// Clear all entries (not thread-safe — call only when renderer is idle).
    void Clear();

    /// Acquire the internal mutex for multi-step atomic operations.
    [[nodiscard]] std::unique_lock<std::mutex> Lock() const
    {
        return std::unique_lock<std::mutex>(m_mutex);
    }

  private:
    struct Entry
    {
        std::shared_ptr<rhi::TextureGpuViewSlot> slot;
        uint64_t residentBytes = 0;
        uint64_t lastUsedFrame = 0;
        bool permanentlyPinned = false;
        std::string assetGuid;
        uint64_t runtimeVersion = 0;
    };

    struct RetiredLease
    {
        std::weak_ptr<const rhi::TextureGpuView> publication;
        uint64_t residentBytes = 0;
    };

    void RetireEntryLocked(Entry entry);
    void RetirePublicationLocked(std::shared_ptr<const rhi::TextureGpuView> publication, uint64_t residentBytes);
    void SweepRetiredLeasesLocked() const;

    std::unordered_map<std::string, Entry> m_textures;
    mutable std::vector<RetiredLease> m_retiredLeases;
    uint64_t m_budgetBytes = 512ULL * 1024ULL * 1024ULL;
    uint64_t m_latestFrame = 0;
    mutable uint64_t m_residentBytes = 0;
    uint64_t m_evictionCount = 0;
    mutable std::mutex m_mutex;
};

} // namespace infernux
