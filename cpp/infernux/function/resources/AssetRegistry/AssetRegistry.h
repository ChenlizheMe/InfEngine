#pragma once

#include <core/threading/JobSystem.h>
#include <core/types/InxFwdType.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetRef.h>
#include <function/resources/AssetRegistry/IAssetLoader.h>

#include <cstdint>
#include <exception>
#include <functional>
#include <memory>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

namespace infernux
{

// Forward declarations — avoid pulling in heavy headers
class InxMaterial;
class InxTexture;
class AssetRegistry;
struct TextureCpuData;

struct AssetResidencyRecord
{
    std::string guid;
    ResourceType type = ResourceType::DefaultBinary;
    std::string runtimeTypeName;
    uint64_t runtimeVersion = 0;
    size_t cpuBytes = 0;
    uint64_t lastAccessSerial = 0;
    uint32_t explicitPinCount = 0;
    size_t externalReferenceCount = 0;
    bool evictable = false;
};

struct PublishedAssetVersion
{
    std::string guid;
    ResourceType type = ResourceType::DefaultBinary;
    uint64_t runtimeVersion = 0;
};

class AssetLoadTicket final
{
  public:
    [[nodiscard]] const std::string &GetGuid() const noexcept
    {
        return m_guid;
    }
    [[nodiscard]] ResourceType GetResourceType() const noexcept
    {
        return m_resourceType;
    }
    [[nodiscard]] bool IsComplete() const noexcept
    {
        return !m_job.IsValid() || m_job.IsComplete();
    }
    [[nodiscard]] bool IsCommitted() const noexcept
    {
        return m_committed;
    }
    [[nodiscard]] bool WasProducedOnWorker() const noexcept
    {
        return IsComplete() && m_producerThread != std::thread::id{} && m_producerThread != m_ownerThread;
    }

  private:
    friend class AssetRegistry;
    AssetRegistry *m_registry = nullptr;
    std::string m_guid;
    std::string m_sourcePath;
    ResourceType m_resourceType = ResourceType::DefaultBinary;
    RuntimeAssetPayload m_payload;
    JobHandle m_job;
    std::exception_ptr m_failure;
    std::thread::id m_ownerThread;
    std::thread::id m_producerThread;
    uint64_t m_expectedMutationGeneration = 0;
    bool m_committed = false;
    bool m_rejected = false;
};

/// One-shot loader-to-RHI bridge for imported texture bytes. The payload is
/// never published as an asset and is discarded as soon as the renderer has
/// copied it into a native upload buffer.
class TextureUploadStagingTicket final
{
  public:
    [[nodiscard]] const std::string &GetGuid() const noexcept
    {
        return m_guid;
    }
    [[nodiscard]] uint64_t GetRuntimeVersion() const noexcept
    {
        return m_runtimeVersion;
    }
    [[nodiscard]] bool IsComplete() const noexcept
    {
        return !m_job.IsValid() || m_job.IsComplete();
    }
    [[nodiscard]] bool WasProducedOnWorker() const noexcept
    {
        return IsComplete() && m_producerThread != std::thread::id{} && m_producerThread != m_ownerThread;
    }

  private:
    friend class AssetRegistry;
    AssetRegistry *m_registry = nullptr;
    std::string m_guid;
    std::string m_sourcePath;
    RuntimeAssetPayload m_stagingPayload;
    JobHandle m_job;
    std::exception_ptr m_failure;
    std::thread::id m_ownerThread;
    std::thread::id m_producerThread;
    uint64_t m_expectedMutationGeneration = 0;
    uint64_t m_runtimeVersion = 0;
    bool m_consumed = false;
    bool m_rejected = false;
};

// =============================================================================
// AssetRegistry — the single source of truth for loaded asset instances
// =============================================================================

/**
 * @brief Unified asset registry — owns all loaded C++ asset instances.
 *
 * Design principles (Unity / UE5 alignment):
 *   1. **GUID-first** — all lookups and cache keys use GUIDs.
 *   2. **Single cache** — one map for all resource types (no per-type Manager singletons).
 *   3. **Loader plug-ins** — type-specific logic (deserialization, dependency scanning)
 *      is isolated in IAssetLoader implementations registered at startup.
 *   4. **Pointer stability** — Reload() updates the *existing* instance in-place
 *      so all shared_ptr holders (MeshRenderers, inspectors, Python wrappers)
 *      see changes without re-resolving.
 *   5. **Built-in assets** — engine-internal materials (grid, gizmo, error …)
 *      live in a separate map keyed by a stable name (no GUID required).
 *
 * Ownership: Infernux creates AssetDatabase and hands it to AssetRegistry
 * via Initialize(). AssetRegistry becomes the sole owner of the AssetDatabase.
 */
class AssetRegistry
{
  public:
    static AssetRegistry &Instance();

    // Non-copyable, non-moveable
    AssetRegistry(const AssetRegistry &) = delete;
    AssetRegistry &operator=(const AssetRegistry &) = delete;

    // ── Lifecycle ────────────────────────────────────────────────────────────

    /// Take ownership of the AssetDatabase and prepare the registry.
    void Initialize(std::unique_ptr<AssetDatabase> adb);

    /// Release all loaded assets and loaders.  Called during engine shutdown.
    void Shutdown();

    [[nodiscard]] bool IsInitialized() const
    {
        return m_initialized;
    }

    // ── AssetDatabase access ─────────────────────────────────────────────────

    [[nodiscard]] AssetDatabase *GetAssetDatabase() const
    {
        return m_assetDb.get();
    }

    // ── Loader registration ──────────────────────────────────────────────────

    void RegisterLoader(ResourceType type, std::unique_ptr<IAssetLoader> loader);

    [[nodiscard]] IAssetLoader *GetLoader(ResourceType type) const;

    /// @brief Populate AssetDatabase's meta-loader table from the registered loaders.
    /// Must be called after all RegisterLoader() calls and before AssetDatabase::Refresh().
    void PopulateAssetDatabaseLoaders();

    // ── Load / Get API (GUID-first) ──────────────────────────────────────────

    /// Return a cached instance if already loaded, or nullptr.
    template <typename T> std::shared_ptr<T> GetAsset(const std::string &guid) const;

    /// Load by GUID: cache hit → return, miss → GUID→path → Loader.
    template <typename T> std::shared_ptr<T> LoadAsset(const std::string &guid, ResourceType type);

    /// Load by path: path→GUID→LoadAsset.  Convenience for callers that only have a path.
    template <typename T> std::shared_ptr<T> LoadAssetByPath(const std::string &path, ResourceType type);

    // ── Hot-reload / invalidation ────────────────────────────────────────────

    /// Reload an already-loaded asset in-place from disk.
    bool ReloadAsset(const std::string &guid);

    /// Evict the instance from cache (next Load will re-read from disk).
    void InvalidateAsset(const std::string &guid);

    /// Fully remove the record (e.g. when the file is deleted).
    void RemoveAsset(const std::string &guid);

    [[nodiscard]] std::shared_ptr<AssetLoadTicket> BeginLoadAsset(const std::string &guid, ResourceType type);
    /// Commit a completed worker load on the owner thread.  The optional
    /// stale-if-unloaded mode is reserved for non-authoritative previews: if
    /// no live cache entry exists, metadata/index churn during a first import
    /// must not discard an otherwise valid decoded payload.
    bool TryCommitAssetLoad(const std::shared_ptr<AssetLoadTicket> &ticket, bool allowStaleIfUnloaded = false);
    [[nodiscard]] std::shared_ptr<TextureUploadStagingTicket> BeginTextureUploadStaging(const std::string &guid);
    [[nodiscard]] std::shared_ptr<const TextureCpuData>
    TryConsumeTextureUploadStaging(const std::shared_ptr<TextureUploadStagingTicket> &ticket);
    void DrainPendingLoads() noexcept;

    // ── Built-in material helpers (named, no GUID) ───────────────────────────

    /// Create and register all engine built-in materials (DefaultLit, Error, Gizmo, etc.).
    /// Populates builtin material pointers from registered loaders.
    void InitializeBuiltinMaterials();

    void RegisterBuiltinMaterial(const std::string &key, std::shared_ptr<InxMaterial> mat);
    [[nodiscard]] std::shared_ptr<InxMaterial> GetBuiltinMaterial(const std::string &key) const;

    /// @brief Load a builtin material from a .mat file, replacing the existing
    /// entry for the given key.  Used at startup to override DefaultLit etc.
    bool LoadBuiltinMaterialFromFile(const std::string &key, const std::string &matFilePath);

    // ── Queries ──────────────────────────────────────────────────────────────

    [[nodiscard]] bool IsLoaded(const std::string &guid) const;
    [[nodiscard]] ResourceType GetAssetType(const std::string &guid) const;
    [[nodiscard]] uint64_t GetAssetVersion(const std::string &guid) const;
    [[nodiscard]] std::string GetAssetRuntimeTypeName(const std::string &guid) const;
    [[nodiscard]] std::vector<std::string> GetAllLoadedGuids() const;
    [[nodiscard]] AssetResidencyRecord GetAssetResidency(const std::string &guid) const;
    [[nodiscard]] std::vector<AssetResidencyRecord> GetAllAssetResidency() const;
    [[nodiscard]] std::vector<PublishedAssetVersion> GetAllPublishedAssetVersions() const;
    [[nodiscard]] size_t GetTotalCpuBytes() const noexcept
    {
        return m_totalCpuBytes;
    }
    [[nodiscard]] size_t GetCpuBudgetBytes() const noexcept
    {
        return m_cpuBudgetBytes;
    }
    [[nodiscard]] uint64_t GetCpuEvictionCount() const noexcept
    {
        return m_cpuEvictionCount;
    }
    void SetCpuBudgetBytes(size_t bytes);
    [[nodiscard]] size_t TrimCpuBudget();
    void PinAsset(const std::string &guid);
    void UnpinAsset(const std::string &guid);

    /// Return all known materials — builtin + loaded from disk.
    [[nodiscard]] std::vector<std::shared_ptr<InxMaterial>> GetAllMaterials() const;

    // ── AssetRef resolution ──────────────────────────────────────────────────

    /// Resolve an AssetRef<T> in-place: if the GUID is set but the cached pointer
    /// is null, load the asset via the registered loader and cache it in the ref.
    /// @return true if the ref now holds a valid pointer.
    template <typename T> bool Resolve(AssetRef<T> &ref, ResourceType type);

  private:
    AssetRegistry() = default;
    ~AssetRegistry() = default;

    /// Internal load helper — assumes GUID / path are valid.
    RuntimeAssetPayload LoadAssetInternal(const std::string &filePath, const std::string &guid, ResourceType type);
    [[nodiscard]] size_t EstimatePayloadBytes(ResourceType type, const RuntimeAssetPayload &payload) const;

    struct AssetEntry
    {
        RuntimeAssetPayload payload;
        ResourceType type = ResourceType::DefaultBinary;
        uint64_t version = 1;
        size_t cpuBytes = 0;
        mutable uint64_t lastAccessSerial = 0;
        uint32_t explicitPinCount = 0;
    };
    using AssetEntryMap = std::unordered_map<std::string, AssetEntry>;
    void RemoveEntry(AssetEntryMap::iterator entry);
    [[nodiscard]] uint64_t NextRuntimeVersion(const std::string &guid);

    bool m_initialized = false;
    std::thread::id m_ownerThread;
    std::unique_ptr<AssetDatabase> m_assetDb;
    AssetEntryMap m_loadedAssets; // GUID → live instance
    std::unordered_map<std::string, uint64_t> m_assetMutationGenerations;
    std::unordered_map<std::string, uint64_t> m_assetRuntimeVersions;
    std::unordered_map<std::string, ResourceType> m_assetRuntimeTypes;
    std::vector<std::weak_ptr<AssetLoadTicket>> m_pendingLoads;
    std::vector<std::weak_ptr<TextureUploadStagingTicket>> m_pendingTextureStagingLoads;
    std::unordered_map<ResourceType, std::unique_ptr<IAssetLoader>> m_loaders;        // type → loader
    std::unordered_map<std::string, std::shared_ptr<InxMaterial>> m_builtinMaterials; // name → builtin mat
    mutable uint64_t m_accessSerial = 0;
    size_t m_totalCpuBytes = 0;
    size_t m_cpuBudgetBytes = 512ULL * 1024ULL * 1024ULL;
    uint64_t m_cpuEvictionCount = 0;
};

// =============================================================================
// Template implementations (must be in header)
// =============================================================================

template <typename T> std::shared_ptr<T> AssetRegistry::GetAsset(const std::string &guid) const
{
    auto it = m_loadedAssets.find(guid);
    if (it != m_loadedAssets.end()) {
        it->second.lastAccessSerial = ++m_accessSerial;
        return it->second.payload.Get<T>();
    }
    return nullptr;
}

template <typename T> std::shared_ptr<T> AssetRegistry::LoadAsset(const std::string &guid, ResourceType type)
{
    // 1. Cache hit with strict resource and C++ type validation.
    const auto cached = m_loadedAssets.find(guid);
    if (cached != m_loadedAssets.end()) {
        if (cached->second.type != type)
            throw std::invalid_argument("AssetRegistry resource type mismatch for GUID: " + guid);
        cached->second.lastAccessSerial = ++m_accessSerial;
        return cached->second.payload.Get<T>();
    }

    // 2. GUID → path via AssetDatabase
    if (!m_assetDb)
        return nullptr;
    std::string path = m_assetDb->GetPathFromGuid(guid);
    if (path.empty())
        return nullptr;

    // 3. Delegate to loader
    auto payload = LoadAssetInternal(path, guid, type);
    return payload ? payload.Get<T>() : nullptr;
}

template <typename T> std::shared_ptr<T> AssetRegistry::LoadAssetByPath(const std::string &path, ResourceType type)
{
    if (!m_assetDb)
        return nullptr;
    std::string guid = m_assetDb->GetGuidFromPath(path);
    if (guid.empty())
        return nullptr;

    const auto cached = m_loadedAssets.find(guid);
    if (cached != m_loadedAssets.end()) {
        if (cached->second.type != type)
            throw std::invalid_argument("AssetRegistry resource type mismatch for GUID: " + guid);
        cached->second.lastAccessSerial = ++m_accessSerial;
        return cached->second.payload.Get<T>();
    }

    auto payload = LoadAssetInternal(path, guid, type);
    return payload ? payload.Get<T>() : nullptr;
}

template <typename T> bool AssetRegistry::Resolve(AssetRef<T> &ref, ResourceType type)
{
    if (!ref.HasGuid())
        return false;
    const uint64_t loadedVersion = GetAssetVersion(ref.GetGuid());
    if (ref.Get() && IsLoaded(ref.GetGuid()) && ref.GetCachedVersion() == loadedVersion)
        return true;
    auto asset = LoadAsset<T>(ref.GetGuid(), type);
    if (asset) {
        ref.SetCached(std::move(asset), GetAssetVersion(ref.GetGuid()));
        return true;
    }
    ref.Invalidate();
    return false;
}

} // namespace infernux
