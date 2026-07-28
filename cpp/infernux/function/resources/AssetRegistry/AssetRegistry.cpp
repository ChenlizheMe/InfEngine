#include "AssetRegistry.h"

#include <core/log/InxLog.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/resources/InxTexture/InxTexture.h>
#include <function/resources/PhysicMaterial/PhysicMaterial.h>

#include <platform/filesystem/InxPath.h>

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <limits>
#include <unordered_set>

namespace infernux
{

// =============================================================================
// Singleton
// =============================================================================

AssetRegistry &AssetRegistry::Instance()
{
    // Intentionally leaked — Shutdown() runs explicitly in Infernux::Cleanup().
    static AssetRegistry *instance = new AssetRegistry();
    return *instance;
}

// =============================================================================
// Lifecycle
// =============================================================================

void AssetRegistry::Initialize(std::unique_ptr<AssetDatabase> adb)
{
    if (m_initialized)
        throw std::logic_error("AssetRegistry::Initialize may only be called once per engine lifetime");
    if (!adb)
        throw std::invalid_argument("AssetRegistry::Initialize requires an AssetDatabase");

    m_assetDb = std::move(adb);
    m_ownerThread = std::this_thread::get_id();
    m_accessSerial = 0;
    m_totalCpuBytes = 0;
    m_cpuBudgetBytes = 512ULL * 1024ULL * 1024ULL;
    m_cpuEvictionCount = 0;
    m_initialized = true;
    INXLOG_INFO("AssetRegistry initialized.");
}

void AssetRegistry::Shutdown()
{
    INXLOG_INFO("Shutting down AssetRegistry...");
    DrainPendingLoads();
    m_loadedAssets.clear();
    m_totalCpuBytes = 0;
    m_accessSerial = 0;
    m_cpuEvictionCount = 0;
    m_assetMutationGenerations.clear();
    m_assetRuntimeVersions.clear();
    m_assetRuntimeTypes.clear();
    m_pendingLoads.clear();
    m_loaders.clear();
    m_builtinMaterials.clear();
    m_assetDb.reset();
    m_ownerThread = {};
    m_initialized = false;
}

// =============================================================================
// Loader registration
// =============================================================================

void AssetRegistry::RegisterLoader(ResourceType type, std::unique_ptr<IAssetLoader> loader)
{
    if (!loader)
        throw std::invalid_argument("AssetRegistry loader cannot be null");
    if (!m_loaders.emplace(type, std::move(loader)).second)
        throw std::logic_error("AssetRegistry loader already registered for ResourceType");
}

IAssetLoader *AssetRegistry::GetLoader(ResourceType type) const
{
    auto it = m_loaders.find(type);
    return it != m_loaders.end() ? it->second.get() : nullptr;
}

void AssetRegistry::PopulateAssetDatabaseLoaders()
{
    if (!m_assetDb)
        throw std::logic_error("AssetRegistry has no AssetDatabase");
    for (auto &[type, loader] : m_loaders) {
        m_assetDb->SetMetaLoader(type, loader.get());
    }
    INXLOG_INFO("AssetRegistry: populated AssetDatabase with ", m_loaders.size(), " loaders");
}

// =============================================================================
// Internal load helper
// =============================================================================

RuntimeAssetPayload AssetRegistry::LoadAssetInternal(const std::string &filePath, const std::string &guid,
                                                     ResourceType type)
{
    auto loaderIt = m_loaders.find(type);
    if (loaderIt == m_loaders.end()) {
        INXLOG_WARN("AssetRegistry: no loader registered for ResourceType ", static_cast<int>(type));
        return nullptr;
    }

    auto payload = loaderIt->second->Load(filePath, guid, m_assetDb.get());
    if (!payload) {
        INXLOG_WARN("AssetRegistry: loader returned nullptr for '", filePath, "' (GUID: ", guid, ")");
        return nullptr;
    }

    const size_t cpuBytes = EstimatePayloadBytes(type, payload);
    if (cpuBytes > std::numeric_limits<size_t>::max() - m_totalCpuBytes)
        throw std::overflow_error("AssetRegistry CPU residency byte total overflow");
    const uint64_t runtimeVersion = NextRuntimeVersion(guid);
    m_assetRuntimeTypes[guid] = type;
    const bool inserted =
        m_loadedAssets.emplace(guid, AssetEntry{payload, type, runtimeVersion, cpuBytes, ++m_accessSerial, 0}).second;
    if (!inserted)
        throw std::logic_error("AssetRegistry attempted to replace a loaded asset without invalidation");
    m_totalCpuBytes += cpuBytes;
    ++m_assetMutationGenerations[guid];
    (void)TrimCpuBudget();
    return payload;
}

uint64_t AssetRegistry::NextRuntimeVersion(const std::string &guid)
{
    uint64_t &version = m_assetRuntimeVersions[guid];
    if (version == std::numeric_limits<uint64_t>::max())
        throw std::overflow_error("Asset runtime version overflow for GUID: " + guid);
    return ++version;
}

size_t AssetRegistry::EstimatePayloadBytes(ResourceType type, const RuntimeAssetPayload &payload) const
{
    if (!payload)
        throw std::invalid_argument("AssetRegistry cannot estimate an empty runtime payload");
    const auto loader = m_loaders.find(type);
    if (loader == m_loaders.end())
        throw std::logic_error("AssetRegistry cannot estimate payload without its loader");
    const size_t bytes = loader->second->EstimateRuntimeBytes(payload);
    if (bytes == 0)
        throw std::logic_error("Asset loader reported zero bytes for a non-empty runtime payload");
    return bytes;
}

void AssetRegistry::RemoveEntry(AssetEntryMap::iterator entry)
{
    if (entry == m_loadedAssets.end())
        return;
    if (entry->second.cpuBytes > m_totalCpuBytes)
        throw std::logic_error("AssetRegistry CPU residency total is corrupted");
    m_totalCpuBytes -= entry->second.cpuBytes;
    m_loadedAssets.erase(entry);
}

// =============================================================================
// Hot-reload / invalidation
// =============================================================================

bool AssetRegistry::ReloadAsset(const std::string &guid)
{
    auto it = m_loadedAssets.find(guid);
    if (it == m_loadedAssets.end())
        return false;

    if (!m_assetDb)
        return false;

    std::string path = m_assetDb->GetPathFromGuid(guid);
    if (path.empty()) {
        INXLOG_WARN("AssetRegistry::ReloadAsset: GUID has no path mapping — ", guid);
        return false;
    }

    auto loaderIt = m_loaders.find(it->second.type);
    if (loaderIt == m_loaders.end())
        return false;

    if (!loaderIt->second->Reload(it->second.payload, path, guid, m_assetDb.get()))
        return false;
    const size_t previousBytes = it->second.cpuBytes;
    const size_t updatedBytes = loaderIt->second->EstimateRuntimeBytes(it->second.payload);
    if (updatedBytes == 0)
        throw std::logic_error("Asset loader reported zero bytes after successful reload");
    if (previousBytes > m_totalCpuBytes)
        throw std::logic_error("AssetRegistry CPU residency total is corrupted before reload");
    if (updatedBytes > std::numeric_limits<size_t>::max() - (m_totalCpuBytes - previousBytes))
        throw std::overflow_error("AssetRegistry CPU residency byte total overflow after reload");
    m_totalCpuBytes = m_totalCpuBytes - previousBytes + updatedBytes;
    it->second.cpuBytes = updatedBytes;
    it->second.lastAccessSerial = ++m_accessSerial;
    it->second.version = NextRuntimeVersion(guid);
    ++m_assetMutationGenerations[guid];
    (void)TrimCpuBudget();
    return true;
}

void AssetRegistry::InvalidateAsset(const std::string &guid)
{
    ++m_assetMutationGenerations[guid];
    auto it = m_loadedAssets.find(guid);
    if (it != m_loadedAssets.end()) {
        RemoveEntry(it);
        INXLOG_DEBUG("AssetRegistry: invalidated cache for GUID ", guid);
    }
}

void AssetRegistry::RemoveAsset(const std::string &guid)
{
    ++m_assetMutationGenerations[guid];
    const auto entry = m_loadedAssets.find(guid);
    if (entry != m_loadedAssets.end())
        RemoveEntry(entry);
}

std::shared_ptr<AssetLoadTicket> AssetRegistry::BeginLoadAsset(const std::string &guid, ResourceType type)
{
    if (!m_initialized || std::this_thread::get_id() != m_ownerThread)
        throw std::logic_error("AssetRegistry::BeginLoadAsset must run on the initialized owner thread");
    if (guid.empty())
        throw std::invalid_argument("AssetRegistry worker load requires a GUID");

    auto ticket = std::make_shared<AssetLoadTicket>();
    ticket->m_registry = this;
    ticket->m_guid = guid;
    ticket->m_resourceType = type;
    ticket->m_ownerThread = m_ownerThread;
    ticket->m_expectedMutationGeneration = m_assetMutationGenerations[guid];

    const auto cached = m_loadedAssets.find(guid);
    if (cached != m_loadedAssets.end()) {
        if (cached->second.type != type)
            throw std::invalid_argument("AssetRegistry resource type mismatch for GUID: " + guid);
        ticket->m_payload = cached->second.payload;
        cached->second.lastAccessSerial = ++m_accessSerial;
        ticket->m_producerThread = m_ownerThread;
        ticket->m_committed = true;
        return ticket;
    }

    ticket->m_sourcePath = m_assetDb->GetPathFromGuid(guid);
    if (ticket->m_sourcePath.empty())
        throw std::invalid_argument("AssetRegistry worker load GUID has no path: " + guid);
    const auto loader = m_loaders.find(type);
    if (loader == m_loaders.end())
        throw std::invalid_argument("AssetRegistry has no loader for worker request");
    if (!loader->second->SupportsWorkerLoad())
        throw std::invalid_argument("AssetRegistry loader does not support worker loading");
    if (!JobSystem::IsAvailable())
        throw std::logic_error("AssetRegistry worker load requires JobSystem");

    IAssetLoader *loaderPtr = loader->second.get();
    AssetDatabase *database = m_assetDb.get();
    ticket->m_job = JobSystem::Get().Schedule([ticket, loaderPtr, database] {
        ticket->m_producerThread = std::this_thread::get_id();
        try {
            ticket->m_payload = loaderPtr->Load(ticket->m_sourcePath, ticket->m_guid, database);
            if (!ticket->m_payload)
                throw std::runtime_error("AssetRegistry worker loader returned an empty payload");
        } catch (...) {
            ticket->m_failure = std::current_exception();
        }
    });
    m_pendingLoads.erase(std::remove_if(m_pendingLoads.begin(), m_pendingLoads.end(),
                                        [](const auto &pending) { return pending.expired(); }),
                         m_pendingLoads.end());
    m_pendingLoads.emplace_back(ticket);
    return ticket;
}

bool AssetRegistry::TryCommitAssetLoad(const std::shared_ptr<AssetLoadTicket> &ticket, bool allowStaleIfUnloaded)
{
    if (!ticket || ticket->m_registry != this)
        throw std::invalid_argument("Asset load ticket belongs to another registry");
    if (std::this_thread::get_id() != m_ownerThread)
        throw std::logic_error("AssetRegistry::TryCommitAssetLoad must run on the owner thread");
    if (ticket->m_rejected)
        throw std::logic_error("Asset load ticket was already rejected");
    if (ticket->m_committed)
        return true;
    if (!ticket->IsComplete())
        return false;
    if (ticket->m_failure) {
        ticket->m_rejected = true;
        std::rethrow_exception(ticket->m_failure);
    }
    const bool staleAfterMutation = m_assetMutationGenerations[ticket->m_guid] != ticket->m_expectedMutationGeneration;
    const bool canUseStaleUnloadedPayload = allowStaleIfUnloaded &&
                                            m_loadedAssets.find(ticket->m_guid) == m_loadedAssets.end();
    if (staleAfterMutation && !canUseStaleUnloadedPayload) {
        ticket->m_rejected = true;
        throw std::logic_error("Asset load ticket is stale after a newer registry mutation");
    }
    if (staleAfterMutation)
        INXLOG_DEBUG("AssetRegistry: accepting stale worker payload for unloaded preview asset ", ticket->m_guid);
    if (!ticket->m_payload) {
        ticket->m_rejected = true;
        throw std::logic_error("Asset load ticket completed without a payload");
    }

    const auto existing = m_loadedAssets.find(ticket->m_guid);
    if (existing != m_loadedAssets.end()) {
        if (existing->second.type != ticket->m_resourceType)
            throw std::logic_error("Asset load ticket conflicts with the cached resource type");
        ticket->m_payload = existing->second.payload;
        existing->second.lastAccessSerial = ++m_accessSerial;
    } else {
        const size_t cpuBytes = EstimatePayloadBytes(ticket->m_resourceType, ticket->m_payload);
        if (cpuBytes > std::numeric_limits<size_t>::max() - m_totalCpuBytes)
            throw std::overflow_error("AssetRegistry CPU residency byte total overflow");
        const uint64_t runtimeVersion = NextRuntimeVersion(ticket->m_guid);
        m_assetRuntimeTypes[ticket->m_guid] = ticket->m_resourceType;
        m_loadedAssets.emplace(ticket->m_guid, AssetEntry{ticket->m_payload, ticket->m_resourceType, runtimeVersion,
                                                          cpuBytes, ++m_accessSerial, 0});
        m_totalCpuBytes += cpuBytes;
        ++m_assetMutationGenerations[ticket->m_guid];
    }
    ticket->m_committed = true;
    (void)TrimCpuBudget();
    return true;
}

void AssetRegistry::DrainPendingLoads() noexcept
{
    for (const auto &pending : m_pendingLoads) {
        const auto ticket = pending.lock();
        if (!ticket || !ticket->m_job.IsValid() || ticket->m_job.IsComplete())
            continue;
        if (!JobSystem::IsAvailable()) {
            INXLOG_ERROR("AssetRegistry pending load outlived JobSystem: ", ticket->m_guid);
            continue;
        }
        try {
            JobSystem::Get().WaitPassive(ticket->m_job);
        } catch (...) {
        }
    }
    m_pendingLoads.clear();
}

void AssetRegistry::UpdateLoadedAssetPath(const std::string &oldPath, const std::string &newPath)
{
    if (!m_assetDb)
        return;

    // AssetDatabase should have updated the GUID↔path mapping before we get here.
    // Because our cache is keyed by GUID, no cache surgery is needed.
    // However, some asset types store an internal path (InxMaterial::m_filePath)
    // that must be patched so SaveToFile() writes to the correct location.
    std::string guid = m_assetDb->GetGuidFromPath(newPath);
    if (guid.empty()) {
        // Fallback: try oldPath in case AssetDatabase hasn't updated yet
        guid = m_assetDb->GetGuidFromPath(oldPath);
    }
    if (guid.empty())
        return;
    ++m_assetMutationGenerations[guid];

    auto it = m_loadedAssets.find(guid);
    if (it == m_loadedAssets.end())
        return;

    auto newName = FromFsPath(ToFsPath(newPath).stem());

    if (it->second.type == ResourceType::Material) {
        auto mat = it->second.payload.Get<InxMaterial>();
        if (mat) {
            mat->SetFilePath(newPath);
            mat->SetName(newName);
        }
    }
    if (it->second.type == ResourceType::Texture) {
        auto tex = it->second.payload.Get<InxTexture>();
        if (tex) {
            tex->SetFilePath(newPath);
            tex->SetName(newName);
        }
    }
    if (it->second.type == ResourceType::PhysicMaterial) {
        auto material = it->second.payload.Get<PhysicMaterial>();
        if (material) {
            material->SetFilePath(newPath);
            material->SetName(newName);
        }
    }
    // Future: add per-type path patching for Audio, Scene, etc.
}

// =============================================================================
// Built-in materials (named, no GUID)
// =============================================================================

void AssetRegistry::RegisterBuiltinMaterial(const std::string &key, std::shared_ptr<InxMaterial> mat)
{
    if (!mat) {
        INXLOG_WARN("AssetRegistry::RegisterBuiltinMaterial: null material for key '", key, "'");
        return;
    }
    m_builtinMaterials[key] = std::move(mat);
}

std::shared_ptr<InxMaterial> AssetRegistry::GetBuiltinMaterial(const std::string &key) const
{
    auto it = m_builtinMaterials.find(key);
    return it != m_builtinMaterials.end() ? it->second : nullptr;
}

bool AssetRegistry::LoadBuiltinMaterialFromFile(const std::string &key, const std::string &matFilePath)
{
    if (matFilePath.empty())
        return false;

    std::ifstream file(ToFsPath(matFilePath));
    if (!file.is_open()) {
        INXLOG_WARN("AssetRegistry::LoadBuiltinMaterialFromFile: cannot open '", matFilePath, "'");
        return false;
    }
    std::string jsonStr((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    file.close();

    auto material = std::make_shared<InxMaterial>();
    if (!material->Deserialize(jsonStr)) {
        INXLOG_ERROR("AssetRegistry::LoadBuiltinMaterialFromFile: deserialization failed for '", matFilePath, "'");
        return false;
    }

    material->SetFilePath(matFilePath);
    material->SetBuiltin(true);
    RegisterBuiltinMaterial(key, material);

    INXLOG_INFO("AssetRegistry: loaded builtin material '", key, "' from: ", matFilePath);
    return true;
}

// =============================================================================
// Built-in material initialization
// =============================================================================

void AssetRegistry::InitializeBuiltinMaterials()
{
    INXLOG_INFO("AssetRegistry: initializing built-in materials...");

    auto registerBuiltin = [this](const std::string &key, std::shared_ptr<InxMaterial> mat) {
        if (mat) {
            mat->SetBuiltin(true);
            RegisterBuiltinMaterial(key, mat);
        }
    };

    registerBuiltin("DefaultLit", InxMaterial::CreateDefaultLit());
    registerBuiltin("DefaultUnlit", InxMaterial::CreateDefaultUnlit());
    registerBuiltin("ParticleSpriteMaterial", InxMaterial::CreateParticleSpriteMaterial());
    registerBuiltin("ParticleSixWaySmokeMaterial", InxMaterial::CreateParticleSixWaySmokeMaterial());
    registerBuiltin("GizmoMaterial", InxMaterial::CreateGizmoMaterial());
    registerBuiltin("GridMaterial", InxMaterial::CreateGridMaterial());
    registerBuiltin("ComponentGizmosMaterial", InxMaterial::CreateComponentGizmosMaterial());
    registerBuiltin("ComponentGizmoIconMaterial", InxMaterial::CreateComponentGizmoIconMaterial());
    registerBuiltin("ComponentGizmoCameraIconMaterial", InxMaterial::CreateComponentGizmoCameraIconMaterial());
    registerBuiltin("ComponentGizmoLightIconMaterial", InxMaterial::CreateComponentGizmoLightIconMaterial());
    registerBuiltin("ComponentGizmoParticleIconMaterial", InxMaterial::CreateComponentGizmoParticleIconMaterial());
    registerBuiltin("EditorToolsMaterial", InxMaterial::CreateEditorToolsMaterial());
    registerBuiltin("SkyboxProcedural", InxMaterial::CreateSkyboxProceduralMaterial());
    registerBuiltin("ErrorMaterial", InxMaterial::CreateErrorMaterial());

    INXLOG_INFO("AssetRegistry: built-in materials initialized.");
}

// =============================================================================
// GetAllMaterials — builtin + loaded from disk
// =============================================================================

std::vector<std::shared_ptr<InxMaterial>> AssetRegistry::GetAllMaterials() const
{
    std::vector<std::shared_ptr<InxMaterial>> result;
    std::unordered_set<InxMaterial *> seen;

    // 1. Built-in materials
    for (const auto &[key, mat] : m_builtinMaterials) {
        if (mat && seen.find(mat.get()) == seen.end()) {
            result.push_back(mat);
            seen.insert(mat.get());
        }
    }

    // 2. User-loaded materials from disk (via AssetRegistry cache)
    for (const auto &[guid, entry] : m_loadedAssets) {
        if (entry.type == ResourceType::Material) {
            auto mat = entry.payload.Get<InxMaterial>();
            if (mat && seen.find(mat.get()) == seen.end()) {
                result.push_back(mat);
                seen.insert(mat.get());
            }
        }
    }

    return result;
}

// =============================================================================
// Queries
// =============================================================================

bool AssetRegistry::IsLoaded(const std::string &guid) const
{
    return m_loadedAssets.count(guid) > 0;
}

ResourceType AssetRegistry::GetAssetType(const std::string &guid) const
{
    auto it = m_loadedAssets.find(guid);
    if (it != m_loadedAssets.end())
        return it->second.type;
    return ResourceType::DefaultBinary;
}

uint64_t AssetRegistry::GetAssetVersion(const std::string &guid) const
{
    const auto found = m_assetRuntimeVersions.find(guid);
    return found != m_assetRuntimeVersions.end() ? found->second : 0;
}

std::string AssetRegistry::GetAssetRuntimeTypeName(const std::string &guid) const
{
    const auto found = m_loadedAssets.find(guid);
    return found != m_loadedAssets.end() ? found->second.payload.GetTypeName() : std::string{};
}

std::vector<std::string> AssetRegistry::GetAllLoadedGuids() const
{
    std::vector<std::string> guids;
    guids.reserve(m_loadedAssets.size());
    for (const auto &[guid, entry] : m_loadedAssets)
        guids.push_back(guid);
    return guids;
}

AssetResidencyRecord AssetRegistry::GetAssetResidency(const std::string &guid) const
{
    const auto found = m_loadedAssets.find(guid);
    if (found == m_loadedAssets.end())
        throw std::invalid_argument("AssetRegistry has no loaded residency record for GUID: " + guid);
    const long useCount = found->second.payload.GetUseCount();
    const size_t externalReferences = useCount > 1 ? static_cast<size_t>(useCount - 1) : 0;
    return {guid,
            found->second.type,
            found->second.payload.GetTypeName(),
            found->second.version,
            found->second.cpuBytes,
            found->second.lastAccessSerial,
            found->second.explicitPinCount,
            externalReferences,
            found->second.explicitPinCount == 0 && externalReferences == 0};
}

std::vector<AssetResidencyRecord> AssetRegistry::GetAllAssetResidency() const
{
    std::vector<AssetResidencyRecord> records;
    records.reserve(m_loadedAssets.size());
    for (const auto &loaded : m_loadedAssets)
        records.push_back(GetAssetResidency(loaded.first));
    std::sort(records.begin(), records.end(),
              [](const auto &left, const auto &right) { return left.guid < right.guid; });
    return records;
}

std::vector<PublishedAssetVersion> AssetRegistry::GetAllPublishedAssetVersions() const
{
    std::vector<PublishedAssetVersion> versions;
    versions.reserve(m_assetRuntimeVersions.size());
    for (const auto &[guid, runtimeVersion] : m_assetRuntimeVersions) {
        const auto type = m_assetRuntimeTypes.find(guid);
        if (type == m_assetRuntimeTypes.end())
            throw std::logic_error("Asset runtime version has no resource type: " + guid);
        versions.push_back({guid, type->second, runtimeVersion});
    }
    std::sort(versions.begin(), versions.end(),
              [](const auto &left, const auto &right) { return left.guid < right.guid; });
    return versions;
}

void AssetRegistry::SetCpuBudgetBytes(size_t bytes)
{
    if (bytes == 0)
        throw std::invalid_argument("AssetRegistry CPU budget must be greater than zero");
    m_cpuBudgetBytes = bytes;
    (void)TrimCpuBudget();
}

size_t AssetRegistry::TrimCpuBudget()
{
    size_t evicted = 0;
    while (m_totalCpuBytes > m_cpuBudgetBytes) {
        auto candidate = m_loadedAssets.end();
        for (auto entry = m_loadedAssets.begin(); entry != m_loadedAssets.end(); ++entry) {
            if (entry->second.explicitPinCount != 0 || entry->second.payload.GetUseCount() != 1)
                continue;
            if (candidate == m_loadedAssets.end() ||
                entry->second.lastAccessSerial < candidate->second.lastAccessSerial)
                candidate = entry;
        }
        if (candidate == m_loadedAssets.end())
            break;
        RemoveEntry(candidate);
        ++evicted;
        ++m_cpuEvictionCount;
    }
    return evicted;
}

void AssetRegistry::PinAsset(const std::string &guid)
{
    const auto found = m_loadedAssets.find(guid);
    if (found == m_loadedAssets.end())
        throw std::invalid_argument("cannot pin an unloaded asset: " + guid);
    if (found->second.explicitPinCount == std::numeric_limits<uint32_t>::max())
        throw std::overflow_error("asset pin count overflow: " + guid);
    ++found->second.explicitPinCount;
    found->second.lastAccessSerial = ++m_accessSerial;
}

void AssetRegistry::UnpinAsset(const std::string &guid)
{
    const auto found = m_loadedAssets.find(guid);
    if (found == m_loadedAssets.end())
        throw std::invalid_argument("cannot unpin an unloaded asset: " + guid);
    if (found->second.explicitPinCount == 0)
        throw std::logic_error("asset pin count is already zero: " + guid);
    --found->second.explicitPinCount;
}

} // namespace infernux
