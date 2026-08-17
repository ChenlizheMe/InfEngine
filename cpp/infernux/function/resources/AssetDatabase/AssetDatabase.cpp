#include "AssetDatabase.h"

#include <function/resources/AssetFormatRegistry.h>

#include <function/resources/AssetDependencyGraph.h>
#include <function/resources/AssetImporter/ConcreteImporters.h>
#include <function/resources/InxMesh/MeshArtifact.h>
#include <function/resources/InxSkinnedMesh/SkinnedMeshArtifact.h>
#include <function/resources/InxTexture/TextureArtifact.h>

#include <core/log/InxLog.h>
#include <platform/filesystem/DocumentStore.h>
#include <platform/filesystem/InxPath.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <fstream>
#include <iomanip>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <thread>
#include <utility>

namespace infernux
{

namespace
{
constexpr size_t kOwnerMergeEntryBudget = 256;
constexpr auto kOwnerMergeTimeBudget = std::chrono::milliseconds(2);

bool IsCanonicalAssetGuid(std::string_view value)
{
    return value.size() == 32 && std::all_of(value.begin(), value.end(), [](unsigned char character) {
               return std::isdigit(character) != 0 || (character >= 'a' && character <= 'f');
           });
}

void ValidateImportedDependencyIdentities(const ImportArtifact &artifact, const std::string &sourcePath)
{
    if (!artifact.dependenciesAuthoritative)
        return;

    // This is the publication boundary for the durable dependency graph.
    // Asset identity is GUID-only: paths and path_hint values are mutable
    // presentation metadata and must never be accepted as graph identities.
    for (const auto &dependency : artifact.dependencies) {
        if (!IsCanonicalAssetGuid(dependency)) {
            throw std::runtime_error("Importer for '" + sourcePath + "' produced a non-GUID dependency identity '" +
                                     dependency + "'");
        }
    }
}

bool LoadCurrentMetadataDocument(const std::string &path, InxResourceMeta &metadata, std::string &recoveredGuid,
                                 std::string &error) noexcept
{
    try {
        std::ifstream file(ToFsPath(path));
        if (!file)
            throw std::runtime_error("metadata sidecar cannot be opened");
        const nlohmann::json document = nlohmann::json::parse(file);
        const auto entries = document.find("metadata");
        if (entries != document.end() && entries->is_object()) {
            const auto guid = entries->find("guid");
            if (guid != entries->end() && guid->is_object()) {
                const auto value = guid->find("value");
                if (value != guid->end() && value->is_string())
                    recoveredGuid = value->get<std::string>();
            }
        }
        metadata.DeserializeDocument(document);
        return true;
    } catch (const std::exception &exception) {
        error = exception.what();
    } catch (...) {
        error = "metadata sidecar raised a non-standard parsing exception";
    }
    return false;
}

bool HasValidContentHash(const InxResourceMeta &metadata) noexcept
{
    if (!metadata.HasKey("content_hash"))
        return false;
    try {
        return !metadata.GetDataAs<std::string>("content_hash").empty();
    } catch (...) {
        return false;
    }
}

void RemoveInvalidMetadataDocument(const std::string &path, const std::string &reason)
{
    std::error_code error;
    const bool removed = std::filesystem::remove(ToFsPath(path), error);
    if (error)
        throw std::runtime_error("failed to remove incompatible metadata sidecar '" + path + "': " + error.message());
    INXLOG_WARN("AssetDatabase: ", removed ? "removed" : "discarded", " incompatible metadata sidecar '", path,
                "' and will regenerate it: ", reason);
}

std::string
RuntimeArtifactRelativePath(const std::string &guid, ResourceType type,
                            ImportArtifact::RuntimeArtifactKind kind = ImportArtifact::RuntimeArtifactKind::Primary)
{
    if (type != ResourceType::Mesh && type != ResourceType::Texture && type != ResourceType::ParticleGraph)
        return {};
    if (guid.empty() || !std::all_of(guid.begin(), guid.end(), [](unsigned char character) {
            return std::isalnum(character) != 0 || character == '-' || character == '_';
        }))
        throw std::invalid_argument("runtime artifact GUID contains invalid path characters");
    if (type == ResourceType::Mesh) {
        if (kind == ImportArtifact::RuntimeArtifactKind::Primary)
            return "Library/Artifacts/Mesh/" + guid + ".inxmesh";
        if (kind == ImportArtifact::RuntimeArtifactKind::SkinnedMesh)
            return "Library/Artifacts/SkinnedMesh/" + guid + ".inxskin";
        throw std::invalid_argument("unsupported Mesh runtime artifact kind");
    }
    if (kind != ImportArtifact::RuntimeArtifactKind::Primary)
        throw std::invalid_argument("non-Mesh assets only support a primary runtime artifact");
    if (type == ResourceType::Texture)
        return "Library/Artifacts/Texture/" + guid + ".inxtex";
    if (type == ResourceType::ParticleGraph)
        return "Library/Artifacts/Particle/" + guid + ".inxparticle";
    return {};
}

bool RequiresRuntimeCpuArtifact(ResourceType type)
{
    return type == ResourceType::Mesh || type == ResourceType::Texture;
}

bool HasCurrentRuntimeArtifactHeader(const std::filesystem::path &path, ResourceType type,
                                     ImportArtifact::RuntimeArtifactKind kind)
{
    std::ifstream stream(path, std::ios::binary);
    if (!stream)
        return false;
    char buffer[32]{};
    stream.read(buffer, sizeof(buffer));
    const std::string_view header(buffer, static_cast<size_t>(stream.gcount()));
    if (type == ResourceType::Texture)
        return kind == ImportArtifact::RuntimeArtifactKind::Primary && TextureArtifact::HasCurrentHeader(header);
    if (type == ResourceType::Mesh)
        return kind == ImportArtifact::RuntimeArtifactKind::Primary ? MeshArtifact::HasCurrentHeader(header)
                                                                    : SkinnedMeshArtifact::HasCurrentHeader(header);
    return false;
}

bool HasReusableRuntimeArtifact(const AssetIndexEntry &entry, ResourceType type,
                                const std::filesystem::path &projectRoot)
{
    if (entry.contentHash.empty())
        return false;
    const std::string expected = RuntimeArtifactRelativePath(entry.guid, type);
    if (expected.empty())
        return entry.artifactPath.empty();
    if (type == ResourceType::ParticleGraph) {
        // Python publishes the GUID-addressed AOT after import. Native refresh
        // stays reusable before that file exists, then requires the current
        // path once AssetIndex records it.
        if (entry.artifactPath.empty())
            return true;
        if (entry.artifactPath != expected)
            return false;
        std::error_code error;
        const auto primaryPath = projectRoot / std::filesystem::u8path(expected);
        return std::filesystem::is_regular_file(primaryPath, error) && !error;
    }
    if (entry.artifactPath != expected)
        return false;
    std::error_code error;
    const auto primaryPath = projectRoot / std::filesystem::u8path(expected);
    if (!std::filesystem::is_regular_file(primaryPath, error) || error ||
        !HasCurrentRuntimeArtifactHeader(primaryPath, type, ImportArtifact::RuntimeArtifactKind::Primary))
        return false;
    if (type != ResourceType::Mesh)
        return true;
    const std::string skinned =
        RuntimeArtifactRelativePath(entry.guid, ResourceType::Mesh, ImportArtifact::RuntimeArtifactKind::SkinnedMesh);
    error.clear();
    const auto skinnedPath = projectRoot / std::filesystem::u8path(skinned);
    return std::filesystem::is_regular_file(skinnedPath, error) && !error &&
           HasCurrentRuntimeArtifactHeader(skinnedPath, type, ImportArtifact::RuntimeArtifactKind::SkinnedMesh);
}

std::vector<DocumentTransactionEntry>
TakeRuntimeArtifactWrites(std::vector<ImportArtifact::RuntimeCpuArtifact> &artifacts, const std::string &guid,
                          ResourceType requestedType, const std::string &projectRoot)
{
    bool hasPrimary = false;
    bool hasSkinnedMesh = false;
    std::vector<DocumentTransactionEntry> writes;
    writes.reserve(artifacts.size());
    for (auto &artifact : artifacts) {
        if (artifact.resourceType != requestedType || artifact.bytes.empty())
            throw std::logic_error("Importer produced an invalid runtime CPU artifact");
        switch (artifact.kind) {
        case ImportArtifact::RuntimeArtifactKind::Primary:
            if (hasPrimary)
                throw std::logic_error("Importer produced duplicate primary runtime CPU artifacts");
            hasPrimary = true;
            break;
        case ImportArtifact::RuntimeArtifactKind::SkinnedMesh:
            if (requestedType != ResourceType::Mesh || hasSkinnedMesh)
                throw std::logic_error("Importer produced an invalid duplicate skinned Mesh artifact");
            hasSkinnedMesh = true;
            break;
        }
        const std::string relative = RuntimeArtifactRelativePath(guid, requestedType, artifact.kind);
        writes.push_back(
            {FromFsPath(ToFsPath(projectRoot) / std::filesystem::u8path(relative)), std::move(artifact.bytes)});
    }
    if (RequiresRuntimeCpuArtifact(requestedType) && !hasPrimary)
        throw std::logic_error("Importer completed without its required primary runtime CPU artifact");
    if (requestedType == ResourceType::Mesh && !hasSkinnedMesh)
        throw std::logic_error("Mesh importer completed without its required skinned companion artifact");
    return writes;
}

bool HasOwnerMergeBudget(const std::chrono::steady_clock::time_point started, size_t processed)
{
    return processed < kOwnerMergeEntryBudget &&
           (processed == 0 || std::chrono::steady_clock::now() - started < kOwnerMergeTimeBudget);
}

template <typename Callback> class ScopeExit final
{
  public:
    explicit ScopeExit(Callback callback) : m_callback(std::move(callback))
    {
    }
    ScopeExit(const ScopeExit &) = delete;
    ScopeExit &operator=(const ScopeExit &) = delete;
    ~ScopeExit()
    {
        if (m_active)
            m_callback();
    }
    void Release() noexcept
    {
        m_active = false;
    }

  private:
    Callback m_callback;
    bool m_active = true;
};

template <typename Callback> ScopeExit<Callback> MakeScopeExit(Callback callback)
{
    return ScopeExit<Callback>(std::move(callback));
}

uint64_t Fnv1a64(const std::string &value, uint64_t seed)
{
    uint64_t hash = seed;
    for (unsigned char byte : value) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }
    return hash;
}

std::string MakeReadOnlyGuid(const std::string &identityKey)
{
    const std::string namespaced = "infernux-builtin:" + identityKey;
    std::ostringstream stream;
    stream << std::hex << std::setfill('0') << std::setw(16) << Fnv1a64(namespaced, 14695981039346656037ULL)
           << std::setw(16) << Fnv1a64(namespaced, 1099511628211ULL);
    return stream.str();
}

std::vector<char> ReadExactSourceBytes(const std::string &path)
{
    std::ifstream file(ToFsPath(path), std::ios::in | std::ios::binary);
    if (!file.is_open())
        throw std::runtime_error("failed to open source file: " + path);
    file.seekg(0, std::ios::end);
    const std::streampos size = file.tellg();
    if (size == std::streampos(-1))
        throw std::runtime_error("failed to determine source file size: " + path);
    file.seekg(0, std::ios::beg);
    if (!file)
        throw std::runtime_error("failed to seek source file: " + path);

    std::vector<char> content;
    content.reserve(static_cast<size_t>(size));
    content.assign(std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>());
    if (file.bad())
        throw std::runtime_error("failed while reading source file: " + path);
    return content;
}

bool ReadFingerprint(const std::filesystem::path &path, AssetFileFingerprint &out)
{
    std::error_code error;
    if (!std::filesystem::is_regular_file(path, error) || error)
        return false;
    const uintmax_t size = std::filesystem::file_size(path, error);
    if (error || size > std::numeric_limits<uint64_t>::max())
        return false;
    const auto modified = std::filesystem::last_write_time(path, error);
    if (error)
        return false;
    out.size = static_cast<uint64_t>(size);
    out.modifiedNs = static_cast<int64_t>(modified.time_since_epoch().count());
    return true;
}

bool ReadFingerprint(const std::filesystem::directory_entry &entry, AssetFileFingerprint &out)
{
    std::error_code error;
    const uintmax_t size = entry.file_size(error);
    if (error || size > std::numeric_limits<uint64_t>::max())
        return false;
    const auto modified = entry.last_write_time(error);
    if (error)
        return false;
    out.size = static_cast<uint64_t>(size);
    out.modifiedNs = static_cast<int64_t>(modified.time_since_epoch().count());
    return true;
}

void RequireUnchangedFingerprint(const std::string &path, const AssetFileFingerprint &expected)
{
    AssetFileFingerprint current;
    if (!ReadFingerprint(ToFsPath(path), current) || current != expected)
        throw std::runtime_error("asset source changed during asynchronous refresh: " + path);
}

} // namespace

const std::vector<AssetCatalogEntry> &AssetCatalogSnapshot::GetDirectory(const std::string &normalizedDirectory) const
{
    static const std::vector<AssetCatalogEntry> empty;
    const auto found = m_directories.find(normalizedDirectory);
    return found != m_directories.end() ? found->second : empty;
}

AssetDatabase::AssetDatabase()
{
    m_querySnapshot = std::make_shared<const QuerySnapshot>();
    // Loaders are populated later by AssetRegistry::PopulateAssetDatabaseLoaders()
    // after all IAssetLoader plug-ins have been registered.
    INXLOG_DEBUG("AssetDatabase created (loaders pending)");
}

AssetDatabase::~AssetDatabase()
{
    WaitForPendingWork();
}

std::string AssetDatabase::GetRuntimeArtifactPath(const std::string &guid, ResourceType type) const
{
    const std::string relative = RuntimeArtifactRelativePath(guid, type);
    if (relative.empty() || m_projectRoot.empty())
        return {};
    return FromFsPath(ToFsPath(m_projectRoot) / std::filesystem::u8path(relative));
}

std::string AssetDatabase::GetSkinnedMeshArtifactPath(const std::string &guid) const
{
    const std::string relative =
        RuntimeArtifactRelativePath(guid, ResourceType::Mesh, ImportArtifact::RuntimeArtifactKind::SkinnedMesh);
    if (m_projectRoot.empty())
        return {};
    return FromFsPath(ToFsPath(m_projectRoot) / std::filesystem::u8path(relative));
}

void AssetDatabase::AssertMutationThread(const char *operation) const
{
    if (!m_initialized)
        throw std::logic_error(std::string("AssetDatabase::") + operation + " requires Initialize()");
    if (std::this_thread::get_id() != m_ownerThread)
        throw std::logic_error(std::string("AssetDatabase::") + operation + " must run on its owner thread");
}

void AssetDatabase::AssertNoPendingCommit(const char *operation) const
{
    if (m_pendingRefreshCommit)
        throw std::logic_error(std::string("AssetDatabase::") + operation +
                               " is unavailable while a refresh commit is pending");
}

bool AssetDatabase::IsOwnerThread() const
{
    return m_initialized && std::this_thread::get_id() == m_ownerThread;
}

bool AssetDatabase::CanReadWorkingSet() const
{
    return IsOwnerThread() && !m_pendingRefreshCommit;
}

AssetDatabase::WorkingSet AssetDatabase::TakeWorkingSet()
{
    WorkingSet result;
    result.guidToPath.swap(m_guidToPath);
    result.pathToGuid.swap(m_pathToGuid);
    result.metas.swap(m_metas);
    result.fileStates.swap(m_fileStates);
    result.importResults.swap(m_importResults);
    result.assetIndex = std::move(m_assetIndex);
    m_assetIndex = AssetIndex{};
    result.assetIndexDirty = m_assetIndexDirty;
    m_assetIndexDirty = false;
    return result;
}

void AssetDatabase::InstallWorkingSet(WorkingSet workingSet)
{
    m_guidToPath = std::move(workingSet.guidToPath);
    m_pathToGuid = std::move(workingSet.pathToGuid);
    m_metas = std::move(workingSet.metas);
    m_fileStates = std::move(workingSet.fileStates);
    m_importResults = std::move(workingSet.importResults);
    m_assetIndex = std::move(workingSet.assetIndex);
    m_assetIndexDirty = workingSet.assetIndexDirty;
}

std::shared_ptr<const AssetDatabase::QuerySnapshot> AssetDatabase::LoadQuerySnapshot() const
{
    return std::atomic_load_explicit(&m_querySnapshot, std::memory_order_acquire);
}

void AssetDatabase::PublishQuerySnapshot()
{
    InstallQuerySnapshot(
        BuildQuerySnapshotArtifact(m_guidToPath, m_pathToGuid, m_metas, m_fileStates, m_queryGeneration + 1));
}

void AssetDatabase::PublishQuerySnapshotForPaths(const std::vector<std::string> &paths)
{
    const auto previous = LoadQuerySnapshot();
    if (!previous || !previous->catalog || paths.empty()) {
        PublishQuerySnapshot();
        return;
    }

    auto snapshot = std::make_shared<QuerySnapshot>(*previous);
    snapshot->generation = m_queryGeneration + 1;
    auto catalog = std::make_shared<AssetCatalogSnapshot>(*previous->catalog);
    catalog->m_generation = snapshot->generation;

    std::unordered_set<std::string> touchedDirectories;
    std::unordered_set<std::string> touchedPaths;
    touchedPaths.reserve(paths.size());
    for (const std::string &path : paths) {
        const std::string normalizedPath = FilesystemPathKey(path);
        if (normalizedPath.empty() || !touchedPaths.insert(normalizedPath).second)
            continue;

        const size_t separator = normalizedPath.find_last_of('/');
        touchedDirectories.insert(separator == std::string::npos ? std::string{} : normalizedPath.substr(0, separator));

        const auto oldMapping = snapshot->pathToGuid.find(normalizedPath);
        if (oldMapping != snapshot->pathToGuid.end()) {
            const std::string oldGuid = oldMapping->second;
            snapshot->pathToGuid.erase(oldMapping);
            if (m_guidToPath.find(oldGuid) == m_guidToPath.end()) {
                snapshot->guidToPath.erase(oldGuid);
                snapshot->metas.erase(oldGuid);
            }
        }

        const auto currentMapping = m_pathToGuid.find(normalizedPath);
        if (currentMapping == m_pathToGuid.end())
            continue;
        const std::string &guid = currentMapping->second;
        const auto currentPath = m_guidToPath.find(guid);
        const auto metadata = m_metas.find(guid);
        if (currentPath == m_guidToPath.end() || metadata == m_metas.end() || !metadata->second)
            throw std::logic_error("Incremental query publication found an incomplete asset mapping");
        snapshot->pathToGuid[normalizedPath] = guid;
        snapshot->guidToPath[guid] = currentPath->second;
        snapshot->metas[guid] = metadata->second;
    }

    for (const std::string &directory : touchedDirectories) {
        auto &entries = catalog->m_directories[directory];
        entries.erase(std::remove_if(entries.begin(), entries.end(),
                                     [&touchedPaths](const AssetCatalogEntry &entry) {
                                         return touchedPaths.find(FilesystemPathKey(entry.path)) != touchedPaths.end();
                                     }),
                      entries.end());
    }

    for (const std::string &normalizedPath : touchedPaths) {
        const auto mapping = m_pathToGuid.find(normalizedPath);
        if (mapping == m_pathToGuid.end())
            continue;
        const std::string &guid = mapping->second;
        const auto currentPath = m_guidToPath.find(guid);
        const auto metadata = m_metas.find(guid);
        const auto fileState = m_fileStates.find(normalizedPath);
        if (currentPath == m_guidToPath.end() || metadata == m_metas.end() || !metadata->second ||
            fileState == m_fileStates.end())
            throw std::logic_error("Incremental catalog publication found an incomplete asset mapping");

        AssetCatalogEntry entry;
        entry.guid = guid;
        entry.path = currentPath->second;
        entry.name = FromFsPath(ToFsPath(entry.path).filename());
        entry.sortKey = entry.name;
        for (char &character : entry.sortKey) {
            if (character >= 'A' && character <= 'Z')
                character = static_cast<char>(character + ('a' - 'A'));
        }
        entry.resourceType = metadata->second->GetResourceType();
        entry.source = fileState->second.source;
        const size_t separator = normalizedPath.find_last_of('/');
        const std::string parent = separator == std::string::npos ? std::string{} : normalizedPath.substr(0, separator);
        catalog->m_directories[parent].push_back(std::move(entry));
        touchedDirectories.insert(parent);
    }

    for (const std::string &directory : touchedDirectories) {
        auto found = catalog->m_directories.find(directory);
        if (found == catalog->m_directories.end())
            continue;
        auto &entries = found->second;
        if (entries.empty()) {
            catalog->m_directories.erase(found);
            continue;
        }
        std::sort(entries.begin(), entries.end(), [](const AssetCatalogEntry &left, const AssetCatalogEntry &right) {
            if (left.sortKey != right.sortKey)
                return left.sortKey < right.sortKey;
            return left.path < right.path;
        });
    }

    snapshot->catalog = std::move(catalog);
    InstallQuerySnapshot(std::move(snapshot));
}

void AssetDatabase::InstallQuerySnapshot(std::shared_ptr<QuerySnapshot> snapshot) noexcept
{
    m_queryGeneration = snapshot->generation;
    std::atomic_store_explicit(&m_querySnapshot, std::shared_ptr<const QuerySnapshot>(std::move(snapshot)),
                               std::memory_order_release);
}

void AssetDatabase::Initialize(const std::string &projectRoot)
{
    if (m_initialized)
        throw std::logic_error("AssetDatabase::Initialize may only be called once");
    m_projectRoot = FromFsPath(ToFsPath(projectRoot));
    if (m_projectRoot.empty())
        throw std::invalid_argument("AssetDatabase project root cannot be empty");
    if (!m_projectRoot.empty() && m_projectRoot.back() == '/') {
        m_projectRoot.pop_back();
    }

    const std::filesystem::path assetsPath = ToFsPath(m_projectRoot) / "Assets";
    std::error_code directoryError;
    std::filesystem::create_directories(assetsPath, directoryError);
    if (directoryError)
        throw std::runtime_error("Failed to create project Assets directory: " + directoryError.message());
    m_assetsRoot = FromFsPath(assetsPath);
    m_assetIndexPath = FromFsPath(ToFsPath(m_projectRoot) / "Library" / "AssetIndex.json");
    m_assetTransactionJournalPath = FromFsPath(ToFsPath(m_projectRoot) / "Library" / "AssetRefresh.transaction");
    if (DocumentTransaction::Recover(m_projectRoot, m_assetTransactionJournalPath))
        INXLOG_WARN("AssetDatabase recovered an interrupted metadata transaction");

    // Register built-in importers
    m_importerRegistry.Register(std::make_unique<TextureImporter>());
    m_importerRegistry.Register(std::make_unique<ShaderImporter>());
    m_importerRegistry.Register(std::make_unique<MaterialImporter>());
    m_importerRegistry.Register(std::make_unique<PhysicMaterialImporter>());
    m_importerRegistry.Register(std::make_unique<RenderEffectImporter>());
    m_importerRegistry.Register(std::make_unique<ParticleGraphImporter>());
    m_importerRegistry.Register(std::make_unique<ScriptImporter>());
    m_importerRegistry.Register(std::make_unique<AudioImporter>());
    m_importerRegistry.Register(std::make_unique<ModelImporter>());

    m_ownerThread = std::this_thread::get_id();
    m_initialized = true;
    PublishQuerySnapshot();

    INXLOG_DEBUG("AssetDatabase initialized. ProjectRoot=", m_projectRoot, ", AssetsRoot=", m_assetsRoot);
}

void AssetDatabase::AddScanRoot(const std::string &path)
{
    AssertMutationThread("AddScanRoot");
    AssertNoPendingCommit("AddScanRoot");
    if (m_pendingAssetScan)
        throw std::logic_error("AssetDatabase scan roots cannot change during an asynchronous refresh");
    auto norm = FromFsPath(ToFsPath(path));
    for (const auto &existing : m_extraScanRoots) {
        if (existing == norm)
            return;
    }
    m_extraScanRoots.push_back(std::move(norm));
    INXLOG_DEBUG("AssetDatabase: added extra scan root: ", m_extraScanRoots.back());
}

void AssetDatabase::AddReadOnlyScanRoot(const std::string &path)
{
    AssertMutationThread("AddReadOnlyScanRoot");
    AddScanRoot(path);
    m_readOnlyScanRoots.insert(FromFsPath(ToFsPath(path)));
}

void AssetDatabase::InstallRuntimeAssetCatalog(const std::string &catalogPath)
{
    AssertMutationThread("InstallRuntimeAssetCatalog");
    AssertNoPendingCommit("InstallRuntimeAssetCatalog");

    std::ifstream stream(ToFsPath(catalogPath));
    if (!stream)
        throw std::runtime_error("runtime asset catalog cannot be opened: " + catalogPath);

    const nlohmann::json document = nlohmann::json::parse(stream);
    if (document.value("$schema", std::string{}) != "infernux.runtime_asset_records")
        throw std::runtime_error("runtime asset catalog has an unsupported schema");
    const auto entries = document.find("entries");
    if (entries == document.end() || !entries->is_array())
        throw std::runtime_error("runtime asset catalog has no entry array");

    for (const auto &entry : *entries) {
        if (!entry.is_object())
            throw std::runtime_error("runtime asset catalog entry is not an object");
        const std::string guid = entry.value("guid", std::string{});
        const std::string relativePath = entry.value("runtime_path", std::string{});
        const auto metadataDocument = entry.find("metadata");
        if (guid.empty() || relativePath.empty() || metadataDocument == entry.end() || !metadataDocument->is_object())
            throw std::runtime_error("runtime asset catalog entry identity is incomplete");

        const std::filesystem::path relative = std::filesystem::u8path(relativePath);
        if (relative.is_absolute() || relative.empty())
            throw std::runtime_error("runtime asset catalog path must be project-relative: " + relativePath);
        for (const auto &part : relative) {
            if (part == "..")
                throw std::runtime_error("runtime asset catalog path escapes the project: " + relativePath);
        }

        std::filesystem::path resolvedAssetPath = ToFsPath(m_projectRoot) / relative;
        const auto runtimeArtifacts = entry.find("runtime_artifacts");
        if (runtimeArtifacts != entry.end() && runtimeArtifacts->is_array()) {
            const std::string primaryArtifactId = entry.value("primary_runtime_artifact_id", std::string{});
            constexpr std::string_view builtinPrefix = "Infernux/resources/";
            bool resolvedPrimaryArtifact = false;
            for (const auto &artifact : *runtimeArtifacts) {
                if (!artifact.is_object())
                    continue;
                const std::string artifactId = artifact.value("runtime_artifact_id", std::string{});
                if (!primaryArtifactId.empty() && artifactId != primaryArtifactId)
                    continue;
                const std::string package = artifact.value("package", std::string{});
                const std::string runtimePath = artifact.value("runtime_path", std::string{});
                if (package == "Content.inxpkg") {
                    const std::filesystem::path contentRelative = std::filesystem::u8path(runtimePath);
                    if (contentRelative.empty() || contentRelative.is_absolute())
                        throw std::runtime_error("runtime Content asset path is invalid: " + runtimePath);
                    for (const auto &part : contentRelative) {
                        if (part == "..")
                            throw std::runtime_error("runtime Content asset path escapes the project: " + runtimePath);
                    }
                    const std::filesystem::path candidate = ToFsPath(m_projectRoot) / contentRelative;
                    std::error_code fileError;
                    if (!std::filesystem::is_regular_file(candidate, fileError) || fileError)
                        throw std::runtime_error("runtime Content asset was not extracted below the project root: " +
                                                 runtimePath);
                    resolvedAssetPath = candidate;
                    resolvedPrimaryArtifact = true;
                    break;
                }
                if (package != "Runtime.inxrt" || runtimePath.rfind(builtinPrefix, 0) != 0)
                    continue;

                const std::filesystem::path builtinRelative =
                    std::filesystem::u8path(runtimePath.substr(builtinPrefix.size()));
                if (builtinRelative.empty() || builtinRelative.is_absolute())
                    throw std::runtime_error("runtime built-in asset path is invalid: " + runtimePath);
                for (const auto &part : builtinRelative) {
                    if (part == "..")
                        throw std::runtime_error("runtime built-in asset path escapes its resource root: " +
                                                 runtimePath);
                }

                bool found = false;
                for (const auto &root : m_readOnlyScanRoots) {
                    const std::filesystem::path candidate = ToFsPath(root) / builtinRelative;
                    std::error_code fileError;
                    if (!std::filesystem::is_regular_file(candidate, fileError) || fileError)
                        continue;
                    resolvedAssetPath = candidate;
                    found = true;
                    break;
                }
                if (!found) {
                    throw std::runtime_error("runtime built-in asset was not extracted below a read-only resource "
                                             "root: " +
                                             runtimePath);
                }
                resolvedPrimaryArtifact = true;
                break;
            }
            if (!primaryArtifactId.empty() && !resolvedPrimaryArtifact)
                throw std::runtime_error("runtime asset catalog primary artifact is unavailable: " + primaryArtifactId);
        }

        const std::string path = FromFsPath(resolvedAssetPath);
        const std::string normalizedPath = FilesystemPathKey(path);
        InxResourceMeta metadata;
        metadata.DeserializeDocument(*metadataDocument);
        if (metadata.GetGuid() != guid)
            throw std::runtime_error("runtime asset catalog metadata GUID mismatch: " + guid);
        metadata.UpdateFilePath(path);
        metadata.AddMetadata("read_only", true);

        const auto oldPathGuid = m_pathToGuid.find(normalizedPath);
        if (oldPathGuid != m_pathToGuid.end() && oldPathGuid->second != guid) {
            m_guidToPath.erase(oldPathGuid->second);
            m_metas.erase(oldPathGuid->second);
            m_importResults.erase(oldPathGuid->second);
        }
        const auto oldGuidPath = m_guidToPath.find(guid);
        if (oldGuidPath != m_guidToPath.end())
            m_pathToGuid.erase(FilesystemPathKey(oldGuidPath->second));

        m_guidToPath[guid] = path;
        m_pathToGuid[normalizedPath] = guid;
        m_metas[guid] = std::make_shared<InxResourceMeta>(std::move(metadata));
        m_importResults[guid] = {true, {}};

        CachedFileState fileState;
        fileState.readOnly = true;
        (void)ReadFingerprint(ToFsPath(path), fileState.source);
        m_fileStates[normalizedPath] = fileState;
    }

    PublishQuerySnapshot();
    INXLOG_INFO("AssetDatabase: installed ", entries->size(), " cooked Player asset identities");
}

AssetDatabase::AssetScanRequest AssetDatabase::CaptureScanRequest() const
{
    AssetScanRequest request;
    request.assetIndexPath = m_assetIndexPath;
    request.normalizedProjectRoot = FilesystemPathKey(m_projectRoot);
    request.expectedQueryGeneration = m_queryGeneration;
    if (!m_assetsRoot.empty())
        request.roots.push_back({m_assetsRoot, false});
    for (const auto &extra : m_extraScanRoots) {
        request.roots.push_back(
            {extra, m_readOnlyScanRoots.find(FromFsPath(ToFsPath(extra))) != m_readOnlyScanRoots.end()});
    }
    return request;
}

AssetDatabase::AssetScanArtifact AssetDatabase::BuildScanArtifact(const AssetScanRequest &request)
{
    const auto started = std::chrono::steady_clock::now();
    AssetScanArtifact artifact;
    artifact.producerThread = std::this_thread::get_id();
    try {
        const bool loadedIndex = artifact.index.Load(request.assetIndexPath, request.normalizedProjectRoot);
        (void)loadedIndex;
    } catch (const std::exception &error) {
        artifact.diagnostics.push_back(std::string("discarded invalid AssetIndex: ") + error.what());
        artifact.index.Reset(request.normalizedProjectRoot);
    }

    artifact.files.reserve(artifact.index.Size());
    const std::string normalizedIndexPath = FilesystemPathKey(request.assetIndexPath);
    std::unordered_set<std::string> observedPaths;
    std::unordered_map<std::string, AssetFileFingerprint> metadataFingerprints;
    metadataFingerprints.reserve(artifact.index.Size());
    for (const auto &scanRoot : request.roots) {
        const std::filesystem::path rootPath = ToFsPath(scanRoot.path);
        if (!std::filesystem::exists(rootPath)) {
            artifact.diagnostics.push_back("scan root does not exist: " + scanRoot.path);
            continue;
        }

        for (const auto &entry : std::filesystem::recursive_directory_iterator(rootPath)) {
            std::error_code typeError;
            if (!entry.is_regular_file(typeError) || typeError)
                continue;

            const std::filesystem::path filePath = entry.path();
            if (IsIgnoredImportPath(filePath))
                continue;
            const std::string path = FromFsPath(filePath);
            if (filePath.extension() == ".tmp") {
                if (!scanRoot.readOnly)
                    artifact.orphanedTempFiles.push_back(path);
                continue;
            }
            if (filePath.extension() == ".meta") {
                if (!scanRoot.readOnly) {
                    AssetFileFingerprint fingerprint;
                    if (ReadFingerprint(entry, fingerprint)) {
                        std::filesystem::path sourcePath = filePath;
                        sourcePath.replace_extension();
                        metadataFingerprints[LexicalFilesystemPathKey(FromFsPath(sourcePath))] = fingerprint;
                    }
                }
                continue;
            }

            const std::string normalizedPath = FilesystemPathKey(path);
            if (normalizedPath == normalizedIndexPath)
                continue;
            if (!observedPaths.insert(normalizedPath).second)
                throw std::runtime_error("asset appears in overlapping scan roots: " + path);

            AssetScanFile scanned;
            scanned.path = path;
            scanned.normalizedPath = normalizedPath;
            scanned.readOnly = scanRoot.readOnly;
            if (!ReadFingerprint(entry, scanned.source))
                continue;
            std::string relativePath;
            scanned.identityKey = TryMakeRelativeFilesystemPath(path, scanRoot.path, relativePath)
                                      ? relativePath
                                      : FromFsPath(filePath.filename());
            artifact.files.push_back(std::move(scanned));
        }
    }

    for (auto &file : artifact.files) {
        if (file.readOnly)
            continue;
        const auto metadata = metadataFingerprints.find(LexicalFilesystemPathKey(file.path));
        if (metadata != metadataFingerprints.end())
            file.meta = metadata->second;
    }

    std::sort(artifact.files.begin(), artifact.files.end(), [](const AssetScanFile &left, const AssetScanFile &right) {
        return left.normalizedPath < right.normalizedPath;
    });
    std::sort(artifact.orphanedTempFiles.begin(), artifact.orphanedTempFiles.end());
    artifact.scanMilliseconds =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
    return artifact;
}

void AssetDatabase::BeginRefresh()
{
    AssertMutationThread("BeginRefresh");
    AssertNoPendingCommit("BeginRefresh");
    if (m_pendingAssetScan)
        throw std::logic_error("AssetDatabase refresh is already pending");
    if (!JobSystem::IsAvailable())
        throw std::logic_error("AssetDatabase asynchronous refresh requires JobSystem");

    AssetScanRequest request = CaptureScanRequest();
    auto state = std::make_shared<PendingAssetScan>(request.expectedQueryGeneration);
    m_pendingAssetScan = state;
    try {
        m_pendingAssetScanJob = JobSystem::Get().Schedule([state, request = std::move(request)]() {
            std::optional<AssetScanArtifact> artifact;
            std::exception_ptr failure;
            try {
                artifact = BuildScanArtifact(request);
            } catch (...) {
                failure = std::current_exception();
            }
            {
                std::lock_guard<std::mutex> lock(state->mutex);
                state->artifact = std::move(artifact);
                state->failure = failure;
                state->complete = true;
            }
            state->completedCv.notify_all();
        });
    } catch (...) {
        m_pendingAssetScan.reset();
        m_pendingAssetScanJob = {};
        throw;
    }
}

bool AssetDatabase::TryCommitRefresh()
{
    AssertMutationThread("TryCommitRefresh");
    if (m_pendingRefreshCommit) {
        const auto state = m_pendingRefreshCommit;
        try {
            if (state->phase == PendingRefreshCommit::Phase::MetadataPrepare) {
                if (state->metadataJobs.IsValid() && !state->metadataJobs.IsComplete())
                    return false;
                state->phase = PendingRefreshCommit::Phase::MetadataMerge;
            }
            if (state->phase == PendingRefreshCommit::Phase::MetadataMerge) {
                (void)ContinuePendingMetadataMerge(state);
                return false;
            }
            if (state->phase == PendingRefreshCommit::Phase::Import) {
                if (state->importJobs.IsValid() && !state->importJobs.IsComplete())
                    return false;
                state->phase = PendingRefreshCommit::Phase::ImportMerge;
            }
            if (state->phase == PendingRefreshCommit::Phase::ImportMerge) {
                (void)ContinuePendingImportMerge(state);
                return false;
            }
            if (state->phase == PendingRefreshCommit::Phase::MetadataWrite) {
                if (state->metadataWriteJob.IsValid() && !state->metadataWriteJob.IsComplete())
                    return false;
                if (state->metadataWriteFailure)
                    std::rethrow_exception(state->metadataWriteFailure);
                m_lastRefreshJournalUncompressedBytes = state->journalUncompressedBytes;
                m_lastRefreshJournalBytes = state->journalBytes;
                m_lastRefreshJournalSerializeMilliseconds = state->journalSerializeMilliseconds;
                m_lastRefreshJournalWriteMilliseconds = state->journalWriteMilliseconds;
                m_lastRefreshJournalApplyMilliseconds = state->journalApplyMilliseconds;
                m_lastRefreshMetadataWriteMilliseconds =
                    std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() -
                                                              state->metadataWriteStarted)
                        .count();
                state->phase = PendingRefreshCommit::Phase::FileStateMerge;
                return false;
            }
            if (state->phase == PendingRefreshCommit::Phase::FileStateMerge) {
                (void)ContinuePendingFileStateMerge(state);
                return false;
            }
            if (state->phase == PendingRefreshCommit::Phase::ReadyToBuildIndex)
                BeginPendingIndexBuild(state);
            if (state->phase == PendingRefreshCommit::Phase::IndexBuild) {
                if (state->indexBuildJob.IsValid() && !state->indexBuildJob.IsComplete())
                    return false;
                if (state->indexBuildFailure)
                    std::rethrow_exception(state->indexBuildFailure);
                if (state->indexRebuildRequired) {
                    if (!state->builtAssetIndex)
                        throw std::logic_error("AssetIndex worker completed without an artifact");
                    state->stagedWorkingSet.assetIndex = std::move(*state->builtAssetIndex);
                    state->stagedWorkingSet.assetIndexDirty = false;
                    m_lastRefreshIndexBuildMilliseconds = state->indexBuildMilliseconds;
                    m_lastRefreshIndexSaveMilliseconds = state->indexSaveMilliseconds;
                    m_lastRefreshIndexBuildOnWorker = state->indexProducerThread != m_ownerThread;
                }
                if (!state->builtQuerySnapshot)
                    throw std::logic_error("query snapshot worker completed without an artifact");
                if (!state->builtDependencySnapshot)
                    throw std::logic_error("dependency snapshot worker completed without an artifact");
                m_lastRefreshQueryBuildMilliseconds = state->queryBuildMilliseconds;
                m_lastRefreshQueryBuildOnWorker = state->queryProducerThread != m_ownerThread;
                m_lastRefreshDependencyBuildMilliseconds = state->dependencyBuildMilliseconds;
                m_lastRefreshDependencyBuildOnWorker = state->dependencyProducerThread != m_ownerThread;
                state->phase = PendingRefreshCommit::Phase::ReadyToFinalize;
            }
            FinalizePendingRefreshCommit(state);
        } catch (...) {
            m_pendingRefreshCommit.reset();
            throw;
        }
        m_pendingRefreshCommit.reset();
        return true;
    }

    const auto state = m_pendingAssetScan;
    if (!state)
        throw std::logic_error("AssetDatabase has no pending refresh");

    std::optional<AssetScanArtifact> artifact;
    std::exception_ptr failure;
    {
        std::lock_guard<std::mutex> lock(state->mutex);
        if (!state->complete)
            return false;
        artifact = std::move(state->artifact);
        failure = state->failure;
    }
    m_pendingAssetScan.reset();
    m_pendingAssetScanJob = {};
    if (failure)
        std::rethrow_exception(failure);
    if (!artifact)
        throw std::logic_error("AssetDatabase scan completed without an artifact");
    return CommitScanArtifact(std::move(*artifact), state->expectedQueryGeneration);
}

bool AssetDatabase::IsRefreshPending() const
{
    AssertMutationThread("IsRefreshPending");
    return static_cast<bool>(m_pendingAssetScan) || static_cast<bool>(m_pendingRefreshCommit);
}

void AssetDatabase::WaitForPendingWork() const noexcept
{
    if (const auto scan = m_pendingAssetScan) {
        std::unique_lock<std::mutex> lock(scan->mutex);
        scan->completedCv.wait(lock, [&scan] { return scan->complete; });
        return;
    }
    if (const auto commit = m_pendingRefreshCommit) {
        if (commit->phase == PendingRefreshCommit::Phase::MetadataPrepare && commit->metadataJobs.IsValid() &&
            !commit->metadataJobs.IsComplete() && JobSystem::IsAvailable()) {
            try {
                JobSystem::Get().WaitPassive(commit->metadataJobs);
            } catch (...) {
            }
        } else if (commit->phase == PendingRefreshCommit::Phase::Import && commit->importJobs.IsValid() &&
                   !commit->importJobs.IsComplete() && JobSystem::IsAvailable()) {
            try {
                JobSystem::Get().WaitPassive(commit->importJobs);
            } catch (...) {
            }
        } else if (commit->phase == PendingRefreshCommit::Phase::MetadataWrite) {
            if (commit->metadataWriteJob.IsValid() && !commit->metadataWriteJob.IsComplete() &&
                JobSystem::IsAvailable()) {
                try {
                    JobSystem::Get().WaitPassive(commit->metadataWriteJob);
                } catch (...) {
                }
            }
        } else if (commit->phase == PendingRefreshCommit::Phase::IndexBuild && commit->indexBuildJob.IsValid() &&
                   !commit->indexBuildJob.IsComplete() && JobSystem::IsAvailable()) {
            try {
                JobSystem::Get().WaitPassive(commit->indexBuildJob);
            } catch (...) {
            }
        }
    }
}

void AssetDatabase::Refresh()
{
    AssertMutationThread("Refresh");
    BeginRefresh();
    while (!TryCommitRefresh())
        WaitForPendingWork();
}

void AssetDatabase::PrepareMetadata(WorkerMetadataPrepare &item)
{
    item.producerThread = std::this_thread::get_id();
    try {
        if (!item.loader)
            throw std::logic_error("metadata preparation requires a loader");

        const std::string metaPath = InxResourceMeta::GetMetaFilePath(item.file.path);
        const bool sidecarExists = std::filesystem::is_regular_file(ToFsPath(metaPath));
        InxResourceMeta metadata;
        InxResourceMeta previousMetadata;
        bool previousMetadataLoaded = false;
        std::string preservedGuid = item.fallbackGuid;

        // Read-only package roots never consume sidecars. They are derived
        // project state and may have been produced by a different engine.
        if (sidecarExists && !item.file.readOnly) {
            InxResourceMeta loadedMetadata;
            std::string recoveredGuid;
            std::string loadError;
            if (LoadCurrentMetadataDocument(metaPath, loadedMetadata, recoveredGuid, loadError)) {
                if (!HasValidContentHash(loadedMetadata)) {
                    // Early sidecars did not always persist the source hash.  They
                    // are parseable, but cannot back a current AssetIndex entry.
                    // Rebuild the derived fields while preserving the GUID and any
                    // importer settings carried by the sidecar.
                    item.mode = WorkerMetadataPrepare::Mode::Rebuild;
                    previousMetadata = std::move(loadedMetadata);
                    previousMetadataLoaded = true;
                    preservedGuid = previousMetadata.GetGuid();
                } else if (item.mode == WorkerMetadataPrepare::Mode::LoadExisting ||
                           item.mode == WorkerMetadataPrepare::Mode::CreateOrLoad) {
                    RequireUnchangedFingerprint(item.file.path, item.file.source);
                    item.metadata = std::move(loadedMetadata);
                    return;
                } else {
                    previousMetadata = std::move(loadedMetadata);
                    previousMetadataLoaded = true;
                    preservedGuid = previousMetadata.GetGuid();
                }
            } else {
                if (preservedGuid.empty())
                    preservedGuid = std::move(recoveredGuid);
                RemoveInvalidMetadataDocument(metaPath, loadError);
            }
        }

        const std::vector<char> content = ReadExactSourceBytes(item.file.path);
        const char emptySource = '\0';
        const char *contentData = content.empty() ? &emptySource : content.data();
        item.loader->CreateMeta(contentData, content.size(), item.file.path, metadata);
        metadata.AddMetadata("file_path", InxResourceMeta::NormalizeFilePath(item.file.path));

        if (item.mode == WorkerMetadataPrepare::Mode::Rebuild && previousMetadataLoaded) {
            for (const auto &[key, value] : previousMetadata.GetMetadata()) {
                if (key != "guid" && !metadata.HasKey(key))
                    metadata.AddMetadata(key, value.second);
            }
        }
        if (!preservedGuid.empty()) {
            metadata.AddMetadata("guid", preservedGuid);
        } else if (item.file.readOnly) {
            metadata.AddMetadata("guid", MakeReadOnlyGuid(item.file.identityKey));
            metadata.AddMetadata("read_only", true);
        }

        if (metadata.GetGuid().empty())
            throw std::runtime_error("metadata preparation produced an empty GUID");
        RequireUnchangedFingerprint(item.file.path, item.file.source);
        item.metadata = std::move(metadata);
    } catch (const std::exception &exception) {
        item.error = exception.what();
    } catch (...) {
        item.error = "metadata preparation raised a non-standard exception";
    }
}

bool AssetDatabase::CommitScanArtifact(AssetScanArtifact artifact, uint64_t expectedQueryGeneration)
{
    const auto commitStarted = std::chrono::steady_clock::now();
    if (m_queryGeneration != expectedQueryGeneration)
        throw std::logic_error("AssetDatabase scan artifact is stale after a newer owner-thread mutation");

    m_lastRefreshReusedCount = 0;
    m_lastRefreshImportedCount = 0;
    m_lastRefreshImportedPaths.clear();
    m_lastRefreshImporterTaskCount = 0;
    m_lastRefreshMetadataTaskCount = 0;
    m_lastRefreshWorkerMetadataCount = 0;
    m_lastRefreshWorkerImporterCount = 0;
    m_lastRefreshScannedCount = artifact.files.size();
    m_lastRefreshScanMilliseconds = artifact.scanMilliseconds;
    m_lastRefreshScanOnWorker = artifact.producerThread != m_ownerThread;
    m_lastRefreshRestoreMilliseconds = 0.0;
    m_lastRefreshImportMilliseconds = 0.0;
    m_lastRefreshPrepareMilliseconds = 0.0;
    m_lastRefreshFinalizeMilliseconds = 0.0;
    m_lastRefreshOwnerMergeMaxSliceMilliseconds = 0.0;
    m_lastRefreshOwnerMergeSliceCount = 0;
    m_lastRefreshMetadataWriteMilliseconds = 0.0;
    m_lastRefreshJournalUncompressedBytes = 0;
    m_lastRefreshJournalBytes = 0;
    m_lastRefreshJournalSerializeMilliseconds = 0.0;
    m_lastRefreshJournalWriteMilliseconds = 0.0;
    m_lastRefreshJournalApplyMilliseconds = 0.0;
    m_lastRefreshIndexBuildMilliseconds = 0.0;
    m_lastRefreshIndexSaveMilliseconds = 0.0;
    m_lastRefreshIndexBuildOnWorker = false;
    m_lastRefreshQueryBuildMilliseconds = 0.0;
    m_lastRefreshQueryBuildOnWorker = false;
    m_lastRefreshDependencyBuildMilliseconds = 0.0;
    m_lastRefreshDependencyBuildOnWorker = false;
    m_lastRefreshPublishMilliseconds = 0.0;
    for (const auto &diagnostic : artifact.diagnostics)
        INXLOG_WARN("AssetDatabase: ", diagnostic);
    for (const auto &tempPath : artifact.orphanedTempFiles) {
        std::error_code error;
        std::filesystem::remove(ToFsPath(tempPath), error);
        if (!error)
            INXLOG_DEBUG("AssetDatabase.Refresh: cleaned orphaned temp file: ", tempPath);
    }

    bool unchanged = !m_assetIndexDirty && artifact.files.size() == artifact.index.Size() &&
                     m_guidToPath.size() == artifact.index.Size() && m_pathToGuid.size() == artifact.index.Size() &&
                     m_fileStates.size() == artifact.files.size();
    for (const auto &file : artifact.files) {
        if (!unchanged)
            break;
        const AssetIndexEntry *indexed = artifact.index.Find(file.normalizedPath);
        const auto pathMapping = m_pathToGuid.find(file.normalizedPath);
        const auto fileState = m_fileStates.find(file.normalizedPath);
        if (!indexed || indexed->resourceType != GetResourceTypeForPath(file.path) || indexed->source != file.source ||
            indexed->meta != file.meta || indexed->readOnly != file.readOnly || !indexed->importSucceeded ||
            !HasReusableRuntimeArtifact(*indexed, indexed->resourceType, ToFsPath(m_projectRoot)) ||
            (!file.readOnly && file.meta.size == 0) || pathMapping == m_pathToGuid.end() ||
            pathMapping->second != indexed->guid || fileState == m_fileStates.end() ||
            fileState->second.source != file.source || fileState->second.meta != file.meta ||
            fileState->second.readOnly != file.readOnly) {
            unchanged = false;
            break;
        }
        const auto guidMapping = m_guidToPath.find(indexed->guid);
        const auto importResult = m_importResults.find(indexed->guid);
        if (guidMapping == m_guidToPath.end() || guidMapping->second != file.path ||
            m_metas.find(indexed->guid) == m_metas.end() || importResult == m_importResults.end() ||
            !importResult->second.succeeded) {
            unchanged = false;
        }
    }
    if (unchanged) {
        m_assetIndex = std::move(artifact.index);
        m_lastRefreshReusedCount = artifact.files.size();
        m_lastRefreshCommitMilliseconds =
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - commitStarted).count();
        m_lastRefreshPrepareMilliseconds = m_lastRefreshCommitMilliseconds;
        INXLOG_INFO("AssetDatabase.Refresh completed without owner-state changes. Total assets: ", m_guidToPath.size(),
                    ", scanned: ", m_lastRefreshScannedCount, ", scan_ms: ", m_lastRefreshScanMilliseconds,
                    ", commit_ms: ", m_lastRefreshCommitMilliseconds);
        return true;
    }

    WorkingSet previousWorkingSet = TakeWorkingSet();
    auto restorePreviousWorkingSet = MakeScopeExit([this, &previousWorkingSet] {
        (void)TakeWorkingSet();
        InstallWorkingSet(std::move(previousWorkingSet));
    });
    m_assetIndex = std::move(artifact.index);

    m_guidToPath.clear();
    m_pathToGuid.clear();
    m_metas.clear();
    m_importResults.clear();
    m_fileStates.clear();
    m_fileStates.reserve(artifact.files.size());
    for (const auto &file : artifact.files)
        m_fileStates.emplace(file.normalizedPath, CachedFileState{file.source, file.meta, file.readOnly});

    std::vector<WorkerMetadataPrepare> workerMetadata;
    std::unordered_map<std::string, std::vector<std::string>> restoredDependencies;
    restoredDependencies.reserve(artifact.files.size());
    const auto updateScannedMapping = [this](const std::string &guid, const AssetScanFile &file) {
        if (guid.empty())
            throw std::logic_error("AssetDatabase scan produced an empty GUID");
        m_guidToPath[guid] = file.path;
        m_pathToGuid[file.normalizedPath] = guid;
    };
    const auto restoreStarted = std::chrono::steady_clock::now();
    for (const auto &file : artifact.files) {
        const ResourceType type = GetResourceTypeForPath(file.path);
        if (type == ResourceType::Meta)
            continue;

        const AssetIndexEntry *indexed = m_assetIndex.Find(file.normalizedPath);
        if (indexed && indexed->resourceType == type && indexed->source == file.source && indexed->meta == file.meta &&
            indexed->readOnly == file.readOnly && indexed->importSucceeded &&
            HasReusableRuntimeArtifact(*indexed, type, ToFsPath(m_projectRoot)) &&
            (file.readOnly || file.meta.size > 0)) {
            m_metas[indexed->guid] = std::make_shared<InxResourceMeta>(indexed->metadata);
            updateScannedMapping(indexed->guid, file);
            restoredDependencies.emplace(indexed->guid, indexed->dependencies);
            m_importResults[indexed->guid] = {true, {}};
            ++m_lastRefreshReusedCount;
            continue;
        }

        WorkerMetadataPrepare item;
        item.file = file;
        item.resourceType = type;
        const auto loader = m_loaders.find(type);
        if (loader == m_loaders.end() || !loader->second)
            throw std::logic_error("AssetDatabase metadata preparation has no loader");
        item.loader = loader->second;
        if (indexed && !file.readOnly && indexed->resourceType != type) {
            item.mode = WorkerMetadataPrepare::Mode::Rebuild;
            item.fallbackGuid = indexed->guid;
        } else if (indexed && !file.readOnly && indexed->source == file.source && indexed->meta != file.meta) {
            item.mode = WorkerMetadataPrepare::Mode::LoadExisting;
            item.fallbackGuid = indexed->guid;
        } else if (indexed && !file.readOnly && indexed->source != file.source) {
            item.mode = WorkerMetadataPrepare::Mode::Rebuild;
            item.fallbackGuid = indexed->guid;
        } else {
            item.mode = WorkerMetadataPrepare::Mode::CreateOrLoad;
        }
        workerMetadata.push_back(std::move(item));
        ++m_lastRefreshImportedCount;
        m_lastRefreshImportedPaths.push_back(file.path);
    }
    m_lastRefreshRestoreMilliseconds =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - restoreStarted).count();

    auto state = std::make_shared<PendingRefreshCommit>();
    state->commitStarted = commitStarted;
    state->scanArtifact = std::move(artifact);
    state->workerMetadata = std::move(workerMetadata);
    m_lastRefreshMetadataTaskCount = state->workerMetadata.size();
    state->restoredDependencies = std::move(restoredDependencies);
    state->stagedWorkingSet = TakeWorkingSet();
    InstallWorkingSet(std::move(previousWorkingSet));
    restorePreviousWorkingSet.Release();

    if (state->workerMetadata.size() > std::numeric_limits<uint32_t>::max())
        throw std::overflow_error("AssetDatabase metadata batch exceeds JobSystem capacity");
    m_pendingRefreshCommit = state;
    try {
        state->metadataJobs = JobSystem::Get().ScheduleBatch(
            static_cast<uint32_t>(state->workerMetadata.size()),
            [state](uint32_t index) { return [state, index] { PrepareMetadata(state->workerMetadata[index]); }; });
    } catch (...) {
        m_pendingRefreshCommit.reset();
        throw;
    }
    m_lastRefreshPrepareMilliseconds =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - commitStarted).count();

    return false;
}

bool AssetDatabase::ContinuePendingMetadataMerge(const std::shared_ptr<PendingRefreshCommit> &state)
{
    if (!state || state->phase != PendingRefreshCommit::Phase::MetadataMerge)
        throw std::logic_error("AssetDatabase metadata merge phase has invalid state");

    const auto ownerStarted = std::chrono::steady_clock::now();
    size_t processed = 0;
    auto &workingSet = state->stagedWorkingSet;
    if (state->metadataMergeCursor == 0 && state->pendingImports.empty())
        state->pendingImports.reserve(state->workerMetadata.size());
    while (state->metadataMergeCursor < state->workerMetadata.size() && HasOwnerMergeBudget(ownerStarted, processed)) {
        auto &item = state->workerMetadata[state->metadataMergeCursor++];
        if (item.producerThread != m_ownerThread)
            ++m_lastRefreshWorkerMetadataCount;
        if (!item.error.empty())
            throw std::runtime_error("Metadata preparation failed for '" + item.file.path + "': " + item.error);
        if (!item.metadata)
            throw std::logic_error("Metadata worker completed without an artifact");

        const std::string guid = item.metadata->GetGuid();
        if (guid.empty())
            throw std::logic_error("Metadata worker produced an empty GUID");
        const auto existingGuid = workingSet.guidToPath.find(guid);
        if (existingGuid != workingSet.guidToPath.end() && existingGuid->second != item.file.path)
            throw std::runtime_error("Duplicate asset GUID produced during refresh: " + guid);
        const auto existingPath = workingSet.pathToGuid.find(item.file.normalizedPath);
        if (existingPath != workingSet.pathToGuid.end() && existingPath->second != guid)
            throw std::runtime_error("Asset path produced multiple GUIDs during refresh: " + item.file.path);

        workingSet.metas[guid] = std::make_shared<InxResourceMeta>(std::move(*item.metadata));
        workingSet.guidToPath[guid] = item.file.path;
        workingSet.pathToGuid[item.file.normalizedPath] = guid;
        state->pendingImports.push_back({guid, item.file.path, item.file.normalizedPath, item.file.source,
                                         item.file.meta, item.file.readOnly, !item.file.readOnly});
        ++processed;
    }

    if (state->metadataMergeCursor == state->workerMetadata.size() && state->importRequestCursor == 0 &&
        !state->pendingImports.empty()) {
        state->workerImports.reserve(state->pendingImports.size());
    }
    while (state->metadataMergeCursor == state->workerMetadata.size() &&
           state->importRequestCursor < state->pendingImports.size() && HasOwnerMergeBudget(ownerStarted, processed)) {
        const size_t assetIndex = state->importRequestCursor++;
        const auto &asset = state->pendingImports[assetIndex];
        const std::string extension = FromFsPath(ToFsPath(asset.path).extension());
        AssetImporter *importer = m_importerRegistry.GetImporterForExtension(extension);
        if (!importer) {
            workingSet.importResults[asset.guid] = {true, {}};
            ++processed;
            continue;
        }
        const auto metadata = workingSet.metas.find(asset.guid);
        if (metadata == workingSet.metas.end() || !metadata->second)
            throw std::logic_error("AssetDatabase worker importer has no metadata snapshot");

        WorkerImport item;
        item.assetIndex = assetIndex;
        item.importer = importer;
        item.request.sourcePath = asset.path;
        item.request.guid = asset.guid;
        item.request.resourceType = metadata->second->GetResourceType();
        item.request.metadata = *metadata->second;
        item.request.metadata.AddMetadata("file_path", InxResourceMeta::NormalizeFilePath(asset.path));
        item.expectedSource = asset.source;
        state->workerImports.push_back(std::move(item));
        ++processed;
    }

    const double elapsed =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - ownerStarted).count();
    m_lastRefreshPrepareMilliseconds += elapsed;
    state->ownerMergeMaxSliceMilliseconds = std::max(state->ownerMergeMaxSliceMilliseconds, elapsed);
    ++state->ownerMergeSliceCount;
    if (state->metadataMergeCursor != state->workerMetadata.size() ||
        state->importRequestCursor != state->pendingImports.size())
        return false;

    m_lastRefreshImporterTaskCount = state->workerImports.size();
    if (state->workerImports.size() > std::numeric_limits<uint32_t>::max())
        throw std::overflow_error("AssetDatabase importer batch exceeds JobSystem capacity");

    state->phase = PendingRefreshCommit::Phase::Import;
    state->importStarted = std::chrono::steady_clock::now();
    state->importJobs =
        JobSystem::Get().ScheduleBatch(static_cast<uint32_t>(state->workerImports.size()), [state](uint32_t index) {
            return [state, index] {
                auto &item = state->workerImports[index];
                item.producerThread = std::this_thread::get_id();
                try {
                    item.artifact = item.importer->Import(item.request);
                    ValidateImportedDependencyIdentities(*item.artifact, item.request.sourcePath);
                    RequireUnchangedFingerprint(item.request.sourcePath, item.expectedSource);
                } catch (const std::exception &exception) {
                    item.error = exception.what();
                } catch (...) {
                    item.error = "Importer raised a non-standard exception";
                }
            };
        });
    return true;
}

bool AssetDatabase::ContinuePendingImportMerge(const std::shared_ptr<PendingRefreshCommit> &state)
{
    if (!state || state->phase != PendingRefreshCommit::Phase::ImportMerge)
        throw std::logic_error("AssetDatabase import merge phase has invalid state");

    const auto ownerStarted = std::chrono::steady_clock::now();
    size_t processed = 0;
    auto &workingSet = state->stagedWorkingSet;
    if (!state->importMergeInitialized) {
        state->metadataWrites.reserve(state->pendingImports.size() * 3);
        state->committedDependencies = state->restoredDependencies;
        state->importMergeInitialized = true;
    }

    while (state->importResultMergeCursor < state->workerImports.size() &&
           HasOwnerMergeBudget(ownerStarted, processed)) {
        auto &item = state->workerImports[state->importResultMergeCursor++];
        const auto &asset = state->pendingImports[item.assetIndex];
        if (item.producerThread != m_ownerThread)
            ++m_lastRefreshWorkerImporterCount;
        try {
            if (!item.error.empty())
                throw std::runtime_error(item.error);
            if (!item.artifact)
                throw std::logic_error("Worker importer completed without an artifact");
            auto runtimeArtifactWrites = TakeRuntimeArtifactWrites(item.artifact->runtimeCpuArtifacts, asset.guid,
                                                                   item.request.resourceType, m_projectRoot);
            workingSet.metas[asset.guid] = std::make_shared<InxResourceMeta>(std::move(item.artifact->metadata));
            workingSet.importResults[asset.guid] = {true, {}};
            for (auto &runtimeArtifactWrite : runtimeArtifactWrites)
                state->metadataWrites.push_back(std::move(runtimeArtifactWrite));
        } catch (const std::exception &exception) {
            workingSet.importResults[asset.guid] = {false, exception.what()};
            INXLOG_ERROR("Asset import failed for '", asset.path, "': ", exception.what());
        }
        ++processed;
    }

    while (state->importResultMergeCursor == state->workerImports.size() &&
           state->metadataWritePrepareCursor < state->pendingImports.size() &&
           HasOwnerMergeBudget(ownerStarted, processed)) {
        const auto &asset = state->pendingImports[state->metadataWritePrepareCursor++];
        state->committedDependencies.try_emplace(asset.guid, std::vector<std::string>{});
        if (asset.persistMetadata) {
            const auto metadata = workingSet.metas.find(asset.guid);
            if (metadata == workingSet.metas.end() || !metadata->second) {
                workingSet.importResults[asset.guid] = {false, "Pending import has no metadata to persist"};
            } else {
                const std::string metaPath = InxResourceMeta::GetMetaFilePath(asset.path);
                state->metadataWrites.push_back({metaPath, metadata->second->SerializeDocument().dump(4) + "\n"});
            }
        }
        ++processed;
    }

    while (state->metadataWritePrepareCursor == state->pendingImports.size() &&
           state->dependencyMergeCursor < state->workerImports.size() && HasOwnerMergeBudget(ownerStarted, processed)) {
        const auto &item = state->workerImports[state->dependencyMergeCursor++];
        const auto &asset = state->pendingImports[item.assetIndex];
        const auto result = workingSet.importResults.find(asset.guid);
        if (result != workingSet.importResults.end() && result->second.succeeded && item.artifact &&
            item.artifact->dependenciesAuthoritative) {
            state->committedDependencies[asset.guid] = item.artifact->dependencies;
        }
        ++processed;
    }

    const auto now = std::chrono::steady_clock::now();
    const double elapsed = std::chrono::duration<double, std::milli>(now - ownerStarted).count();
    state->ownerFinalizeMilliseconds += elapsed;
    state->ownerMergeMaxSliceMilliseconds = std::max(state->ownerMergeMaxSliceMilliseconds, elapsed);
    ++state->ownerMergeSliceCount;
    if (state->importResultMergeCursor != state->workerImports.size() ||
        state->metadataWritePrepareCursor != state->pendingImports.size() ||
        state->dependencyMergeCursor != state->workerImports.size())
        return false;

    m_lastRefreshImportMilliseconds = std::chrono::duration<double, std::milli>(now - state->importStarted).count();
    state->metadataWriteStarted = now;
    state->phase = state->metadataWrites.empty() ? PendingRefreshCommit::Phase::FileStateMerge
                                                 : PendingRefreshCommit::Phase::MetadataWrite;
    if (!state->metadataWrites.empty()) {
        const std::string projectRoot = m_projectRoot;
        const std::string journalPath = m_assetTransactionJournalPath;
        const std::string assetIndexPath = m_assetIndexPath;
        state->metadataWriteJob = JobSystem::Get().Schedule([state, projectRoot, journalPath, assetIndexPath,
                                                             writes = std::move(state->metadataWrites)]() mutable {
            try {
                const DocumentTransactionStats stats =
                    DocumentTransaction::Commit(projectRoot, journalPath, std::move(writes), {assetIndexPath});
                state->journalUncompressedBytes = stats.uncompressedBytes;
                state->journalBytes = stats.journalBytes;
                state->journalSerializeMilliseconds = stats.serializeMilliseconds;
                state->journalWriteMilliseconds = stats.journalWriteMilliseconds;
                state->journalApplyMilliseconds = stats.applyMilliseconds;
                state->committedMetadataFingerprints.clear();
                state->committedMetadataFingerprints.reserve(stats.committedFileStates.size());
                for (const auto &file : stats.committedFileStates) {
                    if (file.path.size() > 5 && file.path.compare(file.path.size() - 5, 5, ".meta") == 0)
                        state->committedMetadataFingerprints.emplace(file.path.substr(0, file.path.size() - 5),
                                                                     AssetFileFingerprint{file.size, file.modifiedNs});
                }
            } catch (...) {
                state->metadataWriteFailure = std::current_exception();
            }
        });
    }
    return true;
}

bool AssetDatabase::ContinuePendingFileStateMerge(const std::shared_ptr<PendingRefreshCommit> &state)
{
    if (!state || state->phase != PendingRefreshCommit::Phase::FileStateMerge)
        throw std::logic_error("AssetDatabase file-state merge phase has invalid state");

    const auto ownerStarted = std::chrono::steady_clock::now();
    size_t processed = 0;
    while (state->fileStateMergeCursor < state->pendingImports.size() && HasOwnerMergeBudget(ownerStarted, processed)) {
        const auto &asset = state->pendingImports[state->fileStateMergeCursor++];
        AssetFileFingerprint meta = asset.meta;
        if (!asset.readOnly) {
            const auto committed = state->committedMetadataFingerprints.find(asset.normalizedPath);
            if (committed == state->committedMetadataFingerprints.end())
                throw std::logic_error("metadata transaction did not return a committed fingerprint");
            meta = committed->second;
        }
        state->stagedWorkingSet.fileStates[asset.normalizedPath] = {asset.source, meta, asset.readOnly};
        ++processed;
    }

    const double elapsed =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - ownerStarted).count();
    state->ownerFinalizeMilliseconds += elapsed;
    state->ownerMergeMaxSliceMilliseconds = std::max(state->ownerMergeMaxSliceMilliseconds, elapsed);
    ++state->ownerMergeSliceCount;
    if (state->fileStateMergeCursor != state->pendingImports.size())
        return false;
    state->phase = PendingRefreshCommit::Phase::ReadyToBuildIndex;
    return true;
}

AssetIndex AssetDatabase::BuildDerivedIndexArtifact(
    const WorkingSet &workingSet, const std::unordered_map<std::string, std::vector<std::string>> &dependenciesByGuid,
    const std::string &normalizedProjectRoot)
{
    AssetIndex index;
    index.Reset(normalizedProjectRoot);
    for (const auto &[normalizedPath, guid] : workingSet.pathToGuid) {
        const auto path = workingSet.guidToPath.find(guid);
        if (path == workingSet.guidToPath.end())
            throw std::logic_error("AssetIndex build found an incomplete GUID/path mapping");
        const auto metadata = workingSet.metas.find(guid);
        if (metadata == workingSet.metas.end() || !metadata->second)
            throw std::logic_error("AssetIndex build found a missing metadata artifact");
        const auto fileState = workingSet.fileStates.find(normalizedPath);
        if (fileState == workingSet.fileStates.end())
            throw std::logic_error("AssetIndex build found a missing file-state artifact");

        AssetIndexEntry entry;
        entry.normalizedPath = normalizedPath;
        entry.guid = guid;
        entry.resourceType = metadata->second->GetResourceType();
        const std::string artifactRelativePath = RuntimeArtifactRelativePath(guid, entry.resourceType);
        if (!artifactRelativePath.empty()) {
            std::error_code artifactError;
            const auto artifactPath = ToFsPath(normalizedProjectRoot) / std::filesystem::u8path(artifactRelativePath);
            if (std::filesystem::is_regular_file(artifactPath, artifactError) && !artifactError)
                entry.artifactPath = artifactRelativePath;
        }
        entry.source = fileState->second.source;
        entry.meta = fileState->second.meta;
        entry.readOnly = fileState->second.readOnly;
        entry.metadata = *metadata->second;
        if (entry.metadata.HasKey("content_hash"))
            entry.contentHash = entry.metadata.GetDataAs<std::string>("content_hash");
        const auto dependencies = dependenciesByGuid.find(guid);
        if (dependencies != dependenciesByGuid.end())
            entry.dependencies = dependencies->second;
        const auto importResult = workingSet.importResults.find(guid);
        if (importResult != workingSet.importResults.end()) {
            entry.importSucceeded = importResult->second.succeeded;
            entry.importError = importResult->second.error;
        }
        if (entry.importSucceeded && entry.contentHash.empty())
            throw std::logic_error("AssetIndex build found a successful import without a content hash");
        index.Upsert(std::move(entry));
    }
    return index;
}

std::shared_ptr<AssetDatabase::QuerySnapshot> AssetDatabase::BuildQuerySnapshotArtifact(
    const std::unordered_map<std::string, std::string> &guidToPath,
    const std::unordered_map<std::string, std::string> &pathToGuid,
    const std::unordered_map<std::string, std::shared_ptr<InxResourceMeta>> &metas,
    const std::unordered_map<std::string, CachedFileState> &fileStates, uint64_t generation)
{
    auto snapshot = std::make_shared<QuerySnapshot>();
    snapshot->generation = generation;
    snapshot->guidToPath = guidToPath;
    snapshot->pathToGuid = pathToGuid;
    snapshot->metas.reserve(metas.size());
    for (const auto &[guid, metadata] : metas) {
        if (!metadata)
            throw std::logic_error("Query snapshot build found null metadata");
        snapshot->metas.emplace(guid, metadata);
    }

    auto catalog = std::make_shared<AssetCatalogSnapshot>();
    catalog->m_generation = generation;
    catalog->m_directories.reserve(pathToGuid.size());
    for (const auto &[normalizedPath, guid] : pathToGuid) {
        const auto mappedPath = guidToPath.find(guid);
        if (mappedPath == guidToPath.end())
            throw std::logic_error("Query snapshot path map references an unknown GUID");
        const auto metadata = metas.find(guid);
        if (metadata == metas.end() || !metadata->second)
            throw std::logic_error("Query snapshot path map references missing metadata");
        const auto fileState = fileStates.find(normalizedPath);
        if (fileState == fileStates.end())
            throw std::logic_error("Query snapshot path map references missing file state");

        AssetCatalogEntry entry;
        entry.guid = guid;
        entry.path = mappedPath->second;
        entry.name = FromFsPath(ToFsPath(entry.path).filename());
        entry.sortKey = entry.name;
        for (char &character : entry.sortKey) {
            if (character >= 'A' && character <= 'Z')
                character = static_cast<char>(character + ('a' - 'A'));
        }
        entry.resourceType = metadata->second->GetResourceType();
        entry.source = fileState->second.source;
        const size_t separator = normalizedPath.find_last_of('/');
        const std::string parent = separator == std::string::npos ? std::string{} : normalizedPath.substr(0, separator);
        catalog->m_directories[parent].push_back(std::move(entry));
    }
    for (auto &[directory, entries] : catalog->m_directories) {
        (void)directory;
        std::sort(entries.begin(), entries.end(), [](const AssetCatalogEntry &left, const AssetCatalogEntry &right) {
            if (left.sortKey != right.sortKey)
                return left.sortKey < right.sortKey;
            return left.path < right.path;
        });
    }
    snapshot->catalog = std::move(catalog);
    return snapshot;
}

void AssetDatabase::BeginPendingIndexBuild(const std::shared_ptr<PendingRefreshCommit> &state)
{
    if (!state || state->phase != PendingRefreshCommit::Phase::ReadyToBuildIndex)
        throw std::logic_error("AssetDatabase index build phase has invalid state");

    const bool reusedLoadedIndex =
        state->pendingImports.empty() && m_lastRefreshReusedCount == state->stagedWorkingSet.assetIndex.Size() &&
        state->stagedWorkingSet.guidToPath.size() == state->stagedWorkingSet.assetIndex.Size() &&
        state->scanArtifact.files.size() == state->stagedWorkingSet.assetIndex.Size();
    state->indexRebuildRequired = !reusedLoadedIndex;
    const std::string normalizedProjectRoot = FilesystemPathKey(m_projectRoot);
    const std::string assetIndexPath = m_assetIndexPath;
    const uint64_t queryGeneration = m_queryGeneration + 1;
    state->expectedDependencyGeneration = AssetDependencyGraph::Instance().GetAssetGeneration();
    const uint64_t dependencyGeneration = state->expectedDependencyGeneration + 1;
    state->phase = PendingRefreshCommit::Phase::IndexBuild;
    state->indexBuildJob = JobSystem::Get().Schedule([state, normalizedProjectRoot, assetIndexPath, queryGeneration,
                                                      dependencyGeneration] {
        try {
            if (state->indexRebuildRequired) {
                state->indexProducerThread = std::this_thread::get_id();
                const auto buildStarted = std::chrono::steady_clock::now();
                AssetIndex index = BuildDerivedIndexArtifact(state->stagedWorkingSet, state->committedDependencies,
                                                             normalizedProjectRoot);
                state->indexBuildMilliseconds =
                    std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - buildStarted).count();
                const auto saveStarted = std::chrono::steady_clock::now();
                index.Save(assetIndexPath);
                state->indexSaveMilliseconds =
                    std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - saveStarted).count();
                state->builtAssetIndex = std::move(index);
            }

            state->queryProducerThread = std::this_thread::get_id();
            const auto queryStarted = std::chrono::steady_clock::now();
            state->builtQuerySnapshot = BuildQuerySnapshotArtifact(
                state->stagedWorkingSet.guidToPath, state->stagedWorkingSet.pathToGuid, state->stagedWorkingSet.metas,
                state->stagedWorkingSet.fileStates, queryGeneration);
            state->queryBuildMilliseconds =
                std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - queryStarted).count();

            state->dependencyProducerThread = std::this_thread::get_id();
            const auto dependencyStarted = std::chrono::steady_clock::now();
            state->builtDependencySnapshot =
                AssetDependencyGraph::BuildAssetSnapshot(state->committedDependencies, dependencyGeneration);
            state->dependencyBuildMilliseconds =
                std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - dependencyStarted).count();
        } catch (...) {
            state->indexBuildFailure = std::current_exception();
        }
    });
}

void AssetDatabase::FinalizePendingRefreshCommit(const std::shared_ptr<PendingRefreshCommit> &state)
{
    if (!state)
        throw std::invalid_argument("AssetDatabase refresh commit state cannot be null");
    if (state->phase != PendingRefreshCommit::Phase::ReadyToFinalize)
        throw std::logic_error("AssetDatabase refresh commit is not ready to finalize");
    if (!state->builtQuerySnapshot || state->builtQuerySnapshot->generation != m_queryGeneration + 1 ||
        !state->builtQuerySnapshot->catalog ||
        state->builtQuerySnapshot->catalog->GetGeneration() != state->builtQuerySnapshot->generation)
        throw std::logic_error("AssetDatabase refresh query artifact has an invalid generation");
    if (!state->builtDependencySnapshot ||
        state->builtDependencySnapshot->GetGeneration() != state->expectedDependencyGeneration + 1 ||
        AssetDependencyGraph::Instance().GetAssetGeneration() != state->expectedDependencyGeneration)
        throw std::logic_error("AssetDatabase refresh dependency artifact is stale");
    const auto finalizeStarted = std::chrono::steady_clock::now();
    WorkingSet previousWorkingSet = TakeWorkingSet();
    InstallWorkingSet(std::move(state->stagedWorkingSet));
    auto restorePreviousWorkingSet = MakeScopeExit([this, &previousWorkingSet] {
        (void)TakeWorkingSet();
        InstallWorkingSet(std::move(previousWorkingSet));
    });

    AssetDependencyGraph::Instance().InstallAssetSnapshot(std::move(state->builtDependencySnapshot));
    m_assetIndexDirty = false;
    const auto publishStarted = std::chrono::steady_clock::now();
    InstallQuerySnapshot(std::move(state->builtQuerySnapshot));
    m_lastRefreshPublishMilliseconds =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - publishStarted).count();
    m_lastRefreshCommitMilliseconds =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - state->commitStarted).count();
    m_lastRefreshOwnerMergeMaxSliceMilliseconds = state->ownerMergeMaxSliceMilliseconds;
    m_lastRefreshOwnerMergeSliceCount = state->ownerMergeSliceCount;
    m_lastRefreshFinalizeMilliseconds =
        state->ownerFinalizeMilliseconds +
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - finalizeStarted).count();
    restorePreviousWorkingSet.Release();
    // INXLOG_INFO("AssetDatabase.Refresh completed. Total assets: ", m_guidToPath.size(),
    //             ", scanned: ", m_lastRefreshScannedCount, ", scan_ms: ", m_lastRefreshScanMilliseconds,
    //             ", restore_ms: ", m_lastRefreshRestoreMilliseconds, ", import_ms: ", m_lastRefreshImportMilliseconds,
    //             ", index_build_ms: ", m_lastRefreshIndexBuildMilliseconds,
    //             ", index_save_ms: ", m_lastRefreshIndexSaveMilliseconds,
    //             ", query_build_ms: ", m_lastRefreshQueryBuildMilliseconds,
    //             ", dependency_build_ms: ", m_lastRefreshDependencyBuildMilliseconds,
    //             ", owner_merge_max_slice_ms: ", m_lastRefreshOwnerMergeMaxSliceMilliseconds,
    //             ", owner_merge_slices: ", m_lastRefreshOwnerMergeSliceCount,
    //             ", publish_ms: ", m_lastRefreshPublishMilliseconds, ", prepare_ms: ",
    //             m_lastRefreshPrepareMilliseconds,
    //             ", metadata_write_ms: ", m_lastRefreshMetadataWriteMilliseconds,
    //             ", finalize_ms: ", m_lastRefreshFinalizeMilliseconds, ", commit_ms: ",
    //             m_lastRefreshCommitMilliseconds);
}

bool AssetDatabase::IsReadOnlyPath(const std::string &normalizedPath) const
{
    for (const auto &root : m_readOnlyScanRoots) {
        if (IsFilesystemPathWithin(normalizedPath, root))
            return true;
    }
    return false;
}

void AssetDatabase::RebuildDerivedIndex()
{
    WorkingSet workingSet = TakeWorkingSet();
    auto restoreWorkingSet = MakeScopeExit([this, &workingSet] { InstallWorkingSet(std::move(workingSet)); });
    std::vector<std::string> indexedGuids;
    indexedGuids.reserve(workingSet.guidToPath.size());
    for (const auto &[guid, path] : workingSet.guidToPath) {
        (void)path;
        indexedGuids.push_back(guid);
    }
    const auto dependencySets = AssetDependencyGraph::Instance().GetDependenciesBatch(indexedGuids);
    std::unordered_map<std::string, std::vector<std::string>> dependenciesByGuid;
    dependenciesByGuid.reserve(dependencySets.size());
    for (const auto &[guid, dependencies] : dependencySets)
        dependenciesByGuid.emplace(guid, std::vector<std::string>(dependencies.begin(), dependencies.end()));
    workingSet.assetIndex = BuildDerivedIndexArtifact(workingSet, dependenciesByGuid, FilesystemPathKey(m_projectRoot));
    InstallWorkingSet(std::move(workingSet));
    restoreWorkingSet.Release();
}

void AssetDatabase::FlushDerivedIndex()
{
    AssertMutationThread("FlushDerivedIndex");
    while (m_pendingAssetScan || m_pendingRefreshCommit) {
        WaitForPendingWork();
        TryCommitRefresh();
    }
    if (!m_assetIndexDirty)
        return;
    RebuildDerivedIndex();
    m_assetIndex.Save(m_assetIndexPath);
    m_assetIndexDirty = false;
}

AssetMutationResult AssetDatabase::ImportAsset(const std::string &path)
{
    AssertMutationThread("ImportAsset");
    AssertNoPendingCommit("ImportAsset");
    AssetMutationResult result;
    result.operation = "import";
    result.path = path;
    result.resourceType = GetResourceTypeForPath(path);
    std::filesystem::path fsPath = ToFsPath(path);
    if (!std::filesystem::exists(fsPath) || !std::filesystem::is_regular_file(fsPath)) {
        result.errorCode = AssetMutationErrorCode::InvalidPath;
        result.error = "asset path is not a regular file";
        return result;
    }

    if (IsIgnoredImportPath(fsPath)) {
        result.errorCode = AssetMutationErrorCode::UnsupportedType;
        result.error = "Python bytecode and cache paths are not importable assets";
        return result;
    }

    if (IsMetaFile(fsPath)) {
        result.errorCode = AssetMutationErrorCode::UnsupportedType;
        result.error = "metadata sidecars cannot be imported as assets";
        return result;
    }

    const ResourceType type = result.resourceType;
    if (type == ResourceType::Meta) {
        result.errorCode = AssetMutationErrorCode::UnsupportedType;
        result.error = "metadata resources cannot be imported directly";
        return result;
    }

    const std::string normalizedPath = FilesystemPathKey(path);
    const bool readOnly = IsReadOnlyPath(normalizedPath);
    std::string guid = CreateOrLoadMetadata(path, type, readOnly, !readOnly, normalizedPath);
    if (guid.empty()) {
        result.errorCode = AssetMutationErrorCode::ImportFailed;
        result.error = "asset metadata could not be created or loaded";
        return result;
    }
    result.guid = guid;

    UpdateMapping(guid, path);
    // Python owns script compilation, dependency analysis, and live class
    // publication. ScriptImporter has no artifact or dependency output, so
    // routing a new script through the generic importer only rewrites the
    // metadata sidecar that CreateOrLoadMetadata just persisted and commits a
    // heavyweight filesystem transaction on the editor thread.
    const bool imported = type == ResourceType::Script ? (m_importResults[guid] = {true, {}}, true)
                                                       : RunImporter(guid, path, false, !readOnly);
    if (!imported) {
        const auto importResult = m_importResults.find(guid);
        const std::string importError = importResult != m_importResults.end() && !importResult->second.error.empty()
                                            ? importResult->second.error
                                            : "asset importer failed";
        m_metas.erase(guid);
        RemoveMappingByGuid(guid);
        m_importResults.erase(guid);
        result.errorCode = AssetMutationErrorCode::ImportFailed;
        result.error = importError;
        return result;
    }
    UpdateCachedFileState(path, readOnly);
    m_assetIndexDirty = true;
    PublishQuerySnapshotForPaths({path});
    result.succeeded = true;
    result.databaseCommitted = true;
    result.changed = true;
    result.queryGeneration = GetQueryGeneration();
    // Import may restore a previously deleted GUID. Incoming dependency edges
    // intentionally survive deletion, so publish availability only after the
    // catalog and importer outputs have committed. Existing runtime references
    // can then resolve their cache without any Project-panel-specific repair.
    AssetDependencyGraph::Instance().NotifyEvent(guid, type, AssetEvent::Modified);
    return result;
}

AssetMutationResult AssetDatabase::ReimportAsset(const std::string &path)
{
    AssertMutationThread("ReimportAsset");
    AssertNoPendingCommit("ReimportAsset");
    AssetMutationResult result;
    result.operation = "reimport";
    result.path = path;
    result.resourceType = GetResourceTypeForPath(path);
    const std::filesystem::path fsPath = ToFsPath(path);
    if (IsIgnoredImportPath(fsPath)) {
        result.errorCode = AssetMutationErrorCode::UnsupportedType;
        result.error = "Python bytecode and cache paths are not importable assets";
        return result;
    }
    if (!std::filesystem::is_regular_file(fsPath) || IsMetaFile(fsPath) || result.resourceType == ResourceType::Meta) {
        result.errorCode = AssetMutationErrorCode::InvalidPath;
        result.error = "registered asset path is not a supported regular file";
        return result;
    }

    const std::string guid = GetGuidFromPath(path);
    if (guid.empty()) {
        result.errorCode = AssetMutationErrorCode::NotFound;
        result.error = "asset is not registered";
        return result;
    }
    result.guid = guid;

    if (result.resourceType == ResourceType::Script) {
        // Python owns script parsing, dependency analysis, and live class
        // publication. Rebuilding native script metadata here reparsed the
        // same source with several std::regex passes on the render thread,
        // even though a source edit cannot change this asset's identity or
        // catalog position. Keep the source fingerprint authoritative and let
        // explicit/full refreshes materialize the derived sidecar fields.
        //
        // ScriptDependencyGraph also owns Python-to-Python propagation. A
        // second AssetDependencyGraph notification duplicated that traversal
        // and could synchronously wake unrelated runtime callbacks before the
        // validated script transaction was ready to publish.
        UpdateCachedFileState(path, IsReadOnlyPath(FilesystemPathKey(path)));
        m_assetIndexDirty = true;
        result.succeeded = true;
        result.databaseCommitted = true;
        result.changed = true;
        result.queryGeneration = GetQueryGeneration();
        return result;
    }

    const auto previousMeta = GetMetaByGuid(guid);
    if (!previousMeta)
        throw std::logic_error("Registered asset has no metadata snapshot");
    const auto restoreMetadata = [&]() {
        m_metas[guid] = std::make_shared<InxResourceMeta>(*previousMeta);
        const std::string metaPath = InxResourceMeta::GetMetaFilePath(path);
        if (!previousMeta->SaveToFile(metaPath))
            throw std::runtime_error("Failed to restore metadata after reimport failure: " + metaPath);
    };

    std::string rebuiltGuid;
    try {
        rebuiltGuid = RebuildMetadata(path);
    } catch (...) {
        restoreMetadata();
        throw;
    }
    if (rebuiltGuid.empty() || rebuiltGuid != guid) {
        if (!rebuiltGuid.empty() && rebuiltGuid != guid)
            m_metas.erase(rebuiltGuid);
        restoreMetadata();
        result.errorCode = AssetMutationErrorCode::ImportFailed;
        result.error = "metadata rebuild did not preserve the registered GUID";
        return result;
    }
    UpdateMapping(guid, path);
    if (!RunImporter(guid, path, true)) {
        const auto importResult = m_importResults.find(guid);
        const std::string importError = importResult != m_importResults.end() && !importResult->second.error.empty()
                                            ? importResult->second.error
                                            : "asset importer failed";
        restoreMetadata();
        result.errorCode = AssetMutationErrorCode::ImportFailed;
        result.error = importError;
        return result;
    }
    UpdateCachedFileState(path, IsReadOnlyPath(FilesystemPathKey(path)));
    m_assetIndexDirty = true;
    PublishQuerySnapshotForPaths({path});
    AssetDependencyGraph::Instance().NotifyEvent(guid, GetResourceTypeForPath(path), AssetEvent::Modified);
    result.succeeded = true;
    result.databaseCommitted = true;
    result.changed = true;
    result.queryGeneration = GetQueryGeneration();
    return result;
}

AssetMutationResult AssetDatabase::DeleteAsset(const std::string &path)
{
    AssertMutationThread("DeleteAsset");
    AssertNoPendingCommit("DeleteAsset");
    std::string guid = GetGuidFromPath(path);
    ResourceType type = GetResourceTypeForPath(path);
    AssetMutationResult result;
    result.operation = "delete";
    result.guid = guid;
    result.path = path;
    result.resourceType = type;

    if (!guid.empty()) {
        std::vector<std::string> artifactPaths;
        artifactPaths.push_back(GetRuntimeArtifactPath(guid, type));
        if (type == ResourceType::Mesh)
            artifactPaths.push_back(GetSkinnedMeshArtifactPath(guid));
        for (const std::string &artifactPath : artifactPaths) {
            if (artifactPath.empty())
                continue;
            std::error_code artifactError;
            std::filesystem::remove(ToFsPath(artifactPath), artifactError);
            if (artifactError)
                throw std::runtime_error("Failed to remove runtime CPU artifact: " + artifactError.message());
        }
    }

    // Notify dependents BEFORE removing from maps (they need to resolve guid→path)
    if (!guid.empty()) {
        AssetDependencyGraph::Instance().NotifyEvent(guid, type, AssetEvent::Deleted);
        AssetDependencyGraph::Instance().RemoveAsset(guid);
    }

    DeleteMetadata(path);

    if (!guid.empty()) {
        RemoveMappingByGuid(guid);
    } else {
        RemoveMappingByPath(path);
    }
    if (!guid.empty())
        m_importResults.erase(guid);
    m_fileStates.erase(FilesystemPathKey(path));
    m_assetIndexDirty = true;
    PublishQuerySnapshotForPaths({path});
    result.succeeded = true;
    result.databaseCommitted = true;
    result.changed = !guid.empty();
    result.queryGeneration = GetQueryGeneration();
    return result;
}

AssetMutationResult AssetDatabase::MoveAsset(const std::string &oldPath, const std::string &newPath)
{
    AssertMutationThread("MoveAsset");
    AssertNoPendingCommit("MoveAsset");
    AssetMutationResult result;
    result.operation = "move";
    result.path = newPath;
    result.previousPath = oldPath;
    result.resourceType = GetResourceTypeForPath(newPath);
    const std::string normalizedOldPath = FilesystemPathKey(oldPath);
    std::string guid = GetGuidFromPath(oldPath);
    if (guid.empty()) {
        // Try to recover guid from old .meta file
        const std::string metaPath = InxResourceMeta::GetMetaFilePath(oldPath);
        if (std::filesystem::exists(ToFsPath(metaPath))) {
            InxResourceMeta meta;
            if (meta.LoadFromFile(metaPath)) {
                guid = meta.GetGuid();
            }
        }
    }
    MoveMetadata(oldPath, newPath);

    if (!guid.empty()) {
        UpdateMapping(guid, newPath);
        RemoveMappingByPath(oldPath);
        m_fileStates.erase(normalizedOldPath);
        UpdateCachedFileState(newPath, IsReadOnlyPath(FilesystemPathKey(newPath)));
        m_assetIndexDirty = true;
        PublishQuerySnapshotForPaths({oldPath, newPath});
        // Notify dependents — GUID unchanged, but path changed
        ResourceType type = GetResourceTypeForPath(newPath);
        AssetDependencyGraph::Instance().NotifyEvent(guid, type, AssetEvent::Moved);
        result.succeeded = true;
        result.databaseCommitted = true;
        result.changed = true;
        result.guid = std::move(guid);
        result.resourceType = type;
        result.queryGeneration = GetQueryGeneration();
        return result;
    }

    // If GUID not found, attempt to re-import
    result = ImportAsset(newPath);
    result.operation = "move";
    result.previousPath = oldPath;
    if (!result)
        result.error = "move could not recover metadata and reimport failed: " + result.error;
    return result;
}

std::vector<AssetMutationResult>
AssetDatabase::MoveAssetsBatch(const std::vector<std::pair<std::string, std::string>> &moves)
{
    AssertMutationThread("MoveAssetsBatch");
    AssertNoPendingCommit("MoveAssetsBatch");
    if (moves.empty())
        return {};

    struct PreparedMove
    {
        std::string oldPath;
        std::string newPath;
        std::string oldKey;
        std::string newKey;
        std::string guid;
        ResourceType resourceType = ResourceType::DefaultText;
    };

    auto failure = [](const std::string &oldPath, const std::string &newPath, AssetMutationErrorCode code,
                      const std::string &message) {
        AssetMutationResult result;
        result.operation = "move_batch";
        result.previousPath = oldPath;
        result.path = newPath;
        result.errorCode = code;
        result.error = message;
        return std::vector<AssetMutationResult>{std::move(result)};
    };

    std::vector<PreparedMove> prepared;
    prepared.reserve(moves.size());
    std::unordered_set<std::string> sourceKeys;
    std::unordered_set<std::string> destinationKeys;
    for (const auto &[oldPath, newPath] : moves) {
        const std::string oldKey = FilesystemPathKey(oldPath);
        const std::string newKey = FilesystemPathKey(newPath);
        if (oldKey.empty() || newKey.empty() || oldKey == newKey)
            return failure(oldPath, newPath, AssetMutationErrorCode::InvalidPath,
                           "asset relocation requires two different paths");
        if (!sourceKeys.insert(oldKey).second || !destinationKeys.insert(newKey).second)
            return failure(oldPath, newPath, AssetMutationErrorCode::InvalidPath,
                           "asset relocation contains duplicate source or destination paths");
        const std::string guid = GetGuidFromPath(oldPath);
        if (guid.empty())
            return failure(oldPath, newPath, AssetMutationErrorCode::NotFound,
                           "asset relocation source has no registered GUID");
        prepared.push_back({oldPath, newPath, oldKey, newKey, guid, GetResourceTypeForPath(newPath)});
    }

    for (const PreparedMove &move : prepared) {
        const auto occupied = m_pathToGuid.find(move.newKey);
        if (occupied != m_pathToGuid.end() && occupied->second != move.guid && sourceKeys.count(move.newKey) == 0)
            return failure(move.oldPath, move.newPath, AssetMutationErrorCode::InvalidPath,
                           "asset relocation destination is occupied by another GUID");
    }

    const auto guidToPathBefore = m_guidToPath;
    const auto pathToGuidBefore = m_pathToGuid;
    const auto metasBefore = m_metas;
    const auto fileStatesBefore = m_fileStates;
    const bool assetIndexDirtyBefore = m_assetIndexDirty;
    std::vector<const PreparedMove *> metadataMoved;
    metadataMoved.reserve(prepared.size());
    try {
        for (const PreparedMove &move : prepared) {
            MoveMetadata(move.oldPath, move.newPath);
            metadataMoved.push_back(&move);
            UpdateMapping(move.guid, move.newPath);
            RemoveMappingByPath(move.oldPath);
            m_fileStates.erase(move.oldKey);
            UpdateCachedFileState(move.newPath, IsReadOnlyPath(move.newKey));
        }
        m_assetIndexDirty = true;
        std::vector<std::string> changedPaths;
        changedPaths.reserve(prepared.size() * 2);
        for (const PreparedMove &move : prepared) {
            changedPaths.push_back(move.oldPath);
            changedPaths.push_back(move.newPath);
        }
        PublishQuerySnapshotForPaths(changedPaths);
    } catch (const std::exception &exc) {
        for (auto it = metadataMoved.rbegin(); it != metadataMoved.rend(); ++it) {
            try {
                MoveMetadata((*it)->newPath, (*it)->oldPath);
            } catch (...) {
            }
        }
        m_guidToPath = guidToPathBefore;
        m_pathToGuid = pathToGuidBefore;
        m_metas = metasBefore;
        m_fileStates = fileStatesBefore;
        m_assetIndexDirty = assetIndexDirtyBefore;
        return failure(prepared.front().oldPath, prepared.front().newPath, AssetMutationErrorCode::RuntimeApplyFailed,
                       std::string("asset relocation batch failed: ") + exc.what());
    }

    std::vector<AssetMutationResult> results;
    results.reserve(prepared.size());
    for (const PreparedMove &move : prepared) {
        AssetDependencyGraph::Instance().NotifyEvent(move.guid, move.resourceType, AssetEvent::Moved);
        AssetMutationResult result;
        result.succeeded = true;
        result.databaseCommitted = true;
        result.changed = true;
        result.operation = "move_batch";
        result.guid = move.guid;
        result.path = move.newPath;
        result.previousPath = move.oldPath;
        result.resourceType = move.resourceType;
        result.queryGeneration = GetQueryGeneration();
        results.push_back(std::move(result));
    }
    return results;
}

bool AssetDatabase::ContainsGuid(const std::string &guid) const
{
    if (CanReadWorkingSet())
        return m_guidToPath.find(guid) != m_guidToPath.end();
    const auto snapshot = LoadQuerySnapshot();
    return snapshot->guidToPath.find(guid) != snapshot->guidToPath.end();
}

bool AssetDatabase::ContainsPath(const std::string &path) const
{
    std::string norm = FilesystemPathKey(path);
    if (CanReadWorkingSet())
        return m_pathToGuid.find(norm) != m_pathToGuid.end();
    const auto snapshot = LoadQuerySnapshot();
    return snapshot->pathToGuid.find(norm) != snapshot->pathToGuid.end();
}

std::string AssetDatabase::GetGuidFromPath(const std::string &path) const
{
    const std::string normalized = FilesystemPathKey(path);
    if (CanReadWorkingSet()) {
        const auto it = m_pathToGuid.find(normalized);
        return it != m_pathToGuid.end() ? it->second : "";
    }
    const auto snapshot = LoadQuerySnapshot();
    const auto it = snapshot->pathToGuid.find(normalized);
    return it != snapshot->pathToGuid.end() ? it->second : "";
}

std::string AssetDatabase::GetPathFromGuid(const std::string &guid) const
{
    if (CanReadWorkingSet()) {
        const auto it = m_guidToPath.find(guid);
        return it != m_guidToPath.end() ? it->second : "";
    }
    const auto snapshot = LoadQuerySnapshot();
    const auto it = snapshot->guidToPath.find(guid);
    return it != snapshot->guidToPath.end() ? it->second : "";
}

std::shared_ptr<const InxResourceMeta> AssetDatabase::GetMetaByGuid(const std::string &guid) const
{
    if (CanReadWorkingSet()) {
        const auto it = m_metas.find(guid);
        return it != m_metas.end() ? it->second : nullptr;
    }
    const auto snapshot = LoadQuerySnapshot();
    const auto it = snapshot->metas.find(guid);
    return it != snapshot->metas.end() ? it->second : nullptr;
}

std::shared_ptr<const InxResourceMeta> AssetDatabase::GetMetaByPath(const std::string &path) const
{
    if (path.empty())
        return nullptr;

    const std::string normalized = FilesystemPathKey(path);
    if (CanReadWorkingSet()) {
        const auto pathIt = m_pathToGuid.find(normalized);
        if (pathIt == m_pathToGuid.end())
            return nullptr;
        const auto metaIt = m_metas.find(pathIt->second);
        return metaIt != m_metas.end() ? metaIt->second : nullptr;
    }
    const auto snapshot = LoadQuerySnapshot();
    const auto pathIt = snapshot->pathToGuid.find(normalized);
    if (pathIt == snapshot->pathToGuid.end())
        return nullptr;
    const auto metaIt = snapshot->metas.find(pathIt->second);
    return metaIt != snapshot->metas.end() ? metaIt->second : nullptr;
}

std::vector<std::string> AssetDatabase::GetAllGuids() const
{
    std::vector<std::string> result;
    if (CanReadWorkingSet()) {
        result.reserve(m_guidToPath.size());
        for (const auto &[guid, path] : m_guidToPath) {
            (void)path;
            result.push_back(guid);
        }
        return result;
    }
    const auto snapshot = LoadQuerySnapshot();
    result.reserve(snapshot->guidToPath.size());
    for (const auto &pair : snapshot->guidToPath) {
        result.push_back(pair.first);
    }
    return result;
}

std::vector<std::string> AssetDatabase::GetAllAssetPaths() const
{
    std::vector<std::string> result;
    if (CanReadWorkingSet()) {
        result.reserve(m_guidToPath.size());
        for (const auto &[guid, path] : m_guidToPath) {
            (void)guid;
            result.push_back(path);
        }
        return result;
    }
    const auto snapshot = LoadQuerySnapshot();
    result.reserve(snapshot->guidToPath.size());
    for (const auto &[guid, path] : snapshot->guidToPath) {
        (void)guid;
        result.push_back(path);
    }
    return result;
}

std::shared_ptr<const AssetCatalogSnapshot> AssetDatabase::GetCatalogSnapshot() const
{
    return LoadQuerySnapshot()->catalog;
}

std::vector<AssetCatalogEntry> AssetDatabase::GetDirectoryCatalog(const std::string &directory) const
{
    const auto catalog = GetCatalogSnapshot();
    if (!catalog)
        return {};
    return catalog->GetDirectory(FilesystemPathKey(directory));
}

uint64_t AssetDatabase::GetQueryGeneration() const
{
    return LoadQuerySnapshot()->generation;
}

size_t AssetDatabase::GetAssetCount() const
{
    return LoadQuerySnapshot()->guidToPath.size();
}

bool AssetDatabase::IsAssetPath(const std::string &path) const
{
    return !m_assetsRoot.empty() && IsFilesystemPathWithin(path, m_assetsRoot);
}

bool AssetDatabase::IsIgnoredImportPath(const std::filesystem::path &path)
{
    const auto lowercase = [](std::string value) {
        std::transform(value.begin(), value.end(), value.begin(),
                       [](unsigned char character) { return static_cast<char>(std::tolower(character)); });
        return value;
    };
    for (const auto &component : path) {
        if (lowercase(FromFsPath(component)) == "__pycache__")
            return true;
    }

    const std::string extension = lowercase(FromFsPath(path.extension()));
    return extension == ".pyc" || extension == ".pyo";
}

bool AssetDatabase::IsMetaFile(const std::filesystem::path &path) const
{
    return FromFsPath(path.extension()) == ".meta";
}

void AssetDatabase::UpdateMapping(const std::string &guid, const std::string &path)
{
    if (guid.empty() || path.empty())
        return;

    const std::string resolvedPath = ResolveFilesystemPath(path);
    std::string norm = FilesystemPathKey(resolvedPath);

    const auto previousPath = m_guidToPath.find(guid);
    if (previousPath != m_guidToPath.end()) {
        for (auto it = m_pathToGuid.begin(); it != m_pathToGuid.end();) {
            if (it->second == guid && it->first != norm)
                it = m_pathToGuid.erase(it);
            else
                ++it;
        }
    }

    const auto previousGuid = m_pathToGuid.find(norm);
    if (previousGuid != m_pathToGuid.end() && previousGuid->second != guid) {
        const auto reverse = m_guidToPath.find(previousGuid->second);
        if (reverse != m_guidToPath.end() && FilesystemPathKey(reverse->second) == norm)
            m_guidToPath.erase(reverse);
    }

    m_guidToPath[guid] = resolvedPath;
    m_pathToGuid[norm] = guid;
}

void AssetDatabase::RemoveMappingByGuid(const std::string &guid)
{
    for (auto it = m_pathToGuid.begin(); it != m_pathToGuid.end();) {
        if (it->second == guid)
            it = m_pathToGuid.erase(it);
        else
            ++it;
    }
    m_guidToPath.erase(guid);
}

void AssetDatabase::RemoveMappingByPath(const std::string &path)
{
    std::string norm = FilesystemPathKey(path);
    auto it = m_pathToGuid.find(norm);
    if (it != m_pathToGuid.end()) {
        m_pathToGuid.erase(it);
    }
}

void AssetDatabase::UpdateCachedFileState(const std::string &path, bool readOnly)
{
    CachedFileState state;
    if (!ReadFingerprint(ToFsPath(path), state.source)) {
        m_fileStates.erase(FilesystemPathKey(path));
        return;
    }
    state.readOnly = readOnly;
    if (!readOnly)
        ReadFingerprint(ToFsPath(InxResourceMeta::GetMetaFilePath(path)), state.meta);
    m_fileStates[FilesystemPathKey(path)] = state;
}

bool AssetDatabase::RunImporter(const std::string &guid, const std::string &path, bool isReimport, bool persistMetadata)
{
    if (guid.empty() || path.empty())
        throw std::invalid_argument("AssetDatabase importer request requires GUID and path");

    std::string ext = FromFsPath(ToFsPath(path).extension());
    AssetImporter *importer = m_importerRegistry.GetImporterForExtension(ext);
    if (!importer) {
        m_importResults[guid] = {true, {}};
        return true;
    }

    const auto metaIt = m_metas.find(guid);
    if (metaIt == m_metas.end() || !metaIt->second)
        throw std::logic_error("AssetDatabase importer request has no metadata snapshot");

    ImportRequest request;
    request.sourcePath = path;
    request.guid = guid;
    request.resourceType = GetResourceTypeForPath(path);
    request.metadata = *metaIt->second;
    request.isReimport = isReimport;

    std::string error;
    try {
        ImportArtifact artifact = isReimport ? importer->Reimport(request) : importer->Import(request);
        ValidateImportedDependencyIdentities(artifact, request.sourcePath);
        std::vector<DocumentTransactionEntry> writes;
        writes.reserve(1 + artifact.runtimeCpuArtifacts.size());
        if (persistMetadata) {
            const std::string metaPath = InxResourceMeta::GetMetaFilePath(path);
            if (metaPath.empty())
                throw std::runtime_error("Failed to resolve importer metadata path");
            writes.push_back({metaPath, artifact.metadata.SerializeDocument().dump(4) + "\n"});
        }
        auto runtimeArtifactWrites =
            TakeRuntimeArtifactWrites(artifact.runtimeCpuArtifacts, guid, request.resourceType, m_projectRoot);
        for (auto &runtimeArtifactWrite : runtimeArtifactWrites)
            writes.push_back(std::move(runtimeArtifactWrite));
        if (!writes.empty()) {
            if (IsFilesystemPathWithin(path, m_projectRoot)) {
                (void)DocumentTransaction::Commit(m_projectRoot, m_assetTransactionJournalPath, std::move(writes),
                                                  {m_assetIndexPath});
            } else {
                for (auto &write : writes) {
                    const std::filesystem::path parent = ToFsPath(write.path).parent_path();
                    if (!parent.empty()) {
                        std::error_code directoryError;
                        std::filesystem::create_directories(parent, directoryError);
                        if (directoryError)
                            throw std::runtime_error("Failed to create external import target directory: " +
                                                     directoryError.message());
                    }
                    (void)DocumentStore::Instance().WriteAndWait(write.path, std::move(write.content));
                }
                std::error_code indexError;
                std::filesystem::remove(ToFsPath(m_assetIndexPath), indexError);
                if (indexError)
                    throw std::runtime_error("Failed to invalidate AssetIndex after external import: " +
                                             indexError.message());
            }
        }
        if (artifact.dependenciesAuthoritative) {
            const std::unordered_set<std::string> dependencies(artifact.dependencies.begin(),
                                                               artifact.dependencies.end());
            AssetDependencyGraph::Instance().SetAssetDependencies(guid, dependencies);
        }
        metaIt->second = std::make_shared<InxResourceMeta>(std::move(artifact.metadata));
        m_importResults[guid] = {true, {}};
        return true;
    } catch (const std::exception &exception) {
        error = exception.what();
    } catch (...) {
        error = "Importer raised a non-standard exception";
    }
    m_importResults[guid] = {false, error};
    INXLOG_ERROR("Asset import failed for '", path, "': ", error);
    return false;
}

// ============================================================================
// Resource management
// ============================================================================

bool AssetDatabase::ReadFile(const std::string &filePath, std::vector<char> &content) const
{
    std::ifstream file(ToFsPath(filePath), std::ios::in | std::ios::binary);
    if (!file.is_open()) {
        INXLOG_ERROR("Failed to open file: ", filePath);
        content.clear();
        return false;
    }

    try {
        file.seekg(0, std::ios::end);
        if (file.fail()) {
            INXLOG_ERROR("Failed to seek to end of file: ", filePath);
            content.clear();
            return false;
        }

        std::streampos fileSize = file.tellg();
        if (fileSize == std::streampos(-1)) {
            INXLOG_ERROR("Failed to get file size: ", filePath);
            content.clear();
            return false;
        }

        file.seekg(0, std::ios::beg);
        if (file.fail()) {
            INXLOG_ERROR("Failed to seek to beginning of file: ", filePath);
            content.clear();
            return false;
        }

        content.reserve(static_cast<size_t>(fileSize));
        content.assign((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());

        if (file.bad() || file.fail()) {
            INXLOG_ERROR("Error occurred while reading file: ", filePath);
            content.clear();
            return false;
        }

        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("Exception while reading file: ", filePath, " - ", e.what());
        content.clear();
        return false;
    }
}

ResourceType AssetDatabase::GetResourcesType(const std::string &extensionName) const
{
    std::string ext = extensionName;
    std::transform(ext.begin(), ext.end(), ext.begin(), ::tolower);

    if (ext == ".vert" || ext == ".frag") {
        return ResourceType::Shader;
    }
    if (ext == ".mat") {
        return ResourceType::Material;
    }
    if (ext == ".physicmaterial") {
        return ResourceType::PhysicMaterial;
    }
    if (ext == ".effect" || ext == ".effectgroup") {
        return ResourceType::RenderEffect;
    }
    if (ext == ".particlegraph") {
        return ResourceType::ParticleGraph;
    }
    if (ext == ".meta") {
        return ResourceType::Meta;
    }
    if (ext == ".py") {
        return ResourceType::Script;
    }
    if (asset_formats::Contains(asset_formats::kTextureExtensions, ext)) {
        return ResourceType::Texture;
    }
    if (asset_formats::Contains(asset_formats::kAudioExtensions, ext)) {
        return ResourceType::Audio;
    }
    if (asset_formats::Contains(asset_formats::kMeshExtensions, ext)) {
        return ResourceType::Mesh;
    }
    static const std::unordered_set<std::string> textExtensions = {
        ".txt", ".md",  ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".html",
        ".htm", ".css", ".js",   ".ts",  ".lua",  ".cs",  ".cpp",  ".c",   ".h",   ".hpp"};
    if (textExtensions.find(ext) != textExtensions.end()) {
        return ResourceType::DefaultText;
    }
    static const std::unordered_set<std::string> binaryExtensions = {
        ".exe", ".dll", ".so",  ".dylib", ".bin", ".dat", ".wav", ".mp3", ".ogg", ".flac", ".mp4", ".avi",
        ".mkv", ".mov", ".zip", ".rar",   ".7z",  ".tar", ".gz",  ".pdf", ".ttf", ".otf",  ".woff"};
    if (binaryExtensions.find(ext) != binaryExtensions.end()) {
        return ResourceType::DefaultBinary;
    }
    return ResourceType::DefaultText;
}

ResourceType AssetDatabase::GetResourceTypeForPath(const std::string &filePath) const
{
    std::filesystem::path path = ToFsPath(filePath);
    std::string ext = FromFsPath(path.extension());
    return GetResourcesType(ext);
}

std::string AssetDatabase::CreateOrLoadMetadata(const std::string &filePath, ResourceType type, bool readOnly,
                                                bool persistMetadata, const std::string &identityKey)
{
    INXLOG_DEBUG("Registering resource: filePath = ", filePath, ", type = ", static_cast<int>(type));

    if (filePath.empty()) {
        INXLOG_ERROR("Received empty filePath!");
        return "";
    }

    auto loader = m_loaders.find(type);
    if (loader == m_loaders.end()) {
        INXLOG_ERROR("Resource type not supported: ", static_cast<int>(type));
        return "";
    }

    std::vector<char> content;
    if (!ReadFile(filePath, content)) {
        INXLOG_ERROR("Failed to read file for resource registration: ", filePath);
        return "";
    }

    if (content.size() == 0)
        content.emplace_back(0);
    const char *contentPtr = content.data();

    InxResourceMeta metaFile;
    std::string metaFilePath = InxResourceMeta::GetMetaFilePath(filePath);
    bool loadedExistingMeta = false;
    std::string preservedGuid = GetGuidFromPath(filePath);
    if (!readOnly && std::filesystem::is_regular_file(ToFsPath(metaFilePath))) {
        std::string recoveredGuid;
        std::string loadError;
        loadedExistingMeta = LoadCurrentMetadataDocument(metaFilePath, metaFile, recoveredGuid, loadError);
        if (!loadedExistingMeta) {
            if (preservedGuid.empty())
                preservedGuid = std::move(recoveredGuid);
            RemoveInvalidMetadataDocument(metaFilePath, loadError);
        }
    }
    if (!loadedExistingMeta) {
        m_loaders[type]->CreateMeta(contentPtr, content.size(), filePath, metaFile);
        if (readOnly) {
            metaFile.AddMetadata("guid", MakeReadOnlyGuid(identityKey));
            metaFile.AddMetadata("read_only", true);
        } else {
            if (!preservedGuid.empty())
                metaFile.AddMetadata("guid", preservedGuid);
        }
        if (!readOnly && persistMetadata) {
            if (!metaFile.SaveToFile(metaFilePath))
                throw std::runtime_error("Failed to persist asset metadata: " + metaFilePath);
        }
    } else {
        if (metaFile.GetResourceType() != type)
            throw std::runtime_error("Asset metadata resource_type does not match its file extension: " + metaFilePath);
    }

    std::string guid = metaFile.GetGuid();
    m_metas[guid] = std::make_shared<InxResourceMeta>(metaFile);
    UpdateMapping(guid, filePath);
    INXLOG_DEBUG("Resource metadata registered with GUID: ", guid);

    return guid;
}

std::string AssetDatabase::RebuildMetadata(const std::string &path, bool persistMetadata)
{
    namespace fs = std::filesystem;
    fs::path filePath = ToFsPath(path);

    if (!fs::exists(filePath)) {
        INXLOG_WARN("Asset metadata rebuild skipped; file does not exist: ", path);
        return {};
    }

    std::string ext = FromFsPath(filePath.extension());
    ResourceType type = GetResourcesType(ext);

    if (type == ResourceType::Meta) {
        return {};
    }

    std::string metaPath = InxResourceMeta::GetMetaFilePath(path);

    std::vector<char> content;
    if (!ReadFile(path, content)) {
        INXLOG_ERROR("Asset metadata rebuild failed to read file: ", path);
        return {};
    }
    if (content.empty()) {
        content.emplace_back(0);
    }

    InxResourceMeta meta;
    std::string existingGuid;

    fs::path fsMetaPath = ToFsPath(metaPath);

    if (fs::exists(fsMetaPath) && meta.LoadFromFile(metaPath)) {
        existingGuid = meta.GetGuid();
    }
    if (existingGuid.empty()) {
        existingGuid = GetGuidFromPath(path);
    }

    auto loaderIt = m_loaders.find(type);
    if (loaderIt == m_loaders.end()) {
        INXLOG_ERROR("Asset metadata rebuild has no loader for type: ", static_cast<int>(type));
        return {};
    }

    InxResourceMeta newMeta;
    loaderIt->second->CreateMeta(content.data(), content.size(), path, newMeta);
    newMeta.AddMetadata("file_path", InxResourceMeta::NormalizeFilePath(path));

    if (fs::exists(fsMetaPath) && meta.GetMetadata().size() > 0) {
        for (const auto &[key, metaPair] : meta.GetMetadata()) {
            (void)metaPair;
            if (key == "guid") {
                continue;
            }
            newMeta.CopyMetadataIfMissing(meta, key);
        }
    }

    if (!existingGuid.empty()) {
        newMeta.AddMetadata("guid", existingGuid);
    }

    if (persistMetadata && !newMeta.SaveToFile(metaPath))
        throw std::runtime_error("Failed to persist rebuilt asset metadata: " + metaPath);

    std::string guid = newMeta.GetGuid();
    m_metas[guid] = std::make_shared<InxResourceMeta>(newMeta);
    return guid;
}

void AssetDatabase::DeleteMetadata(const std::string &path)
{
    namespace fs = std::filesystem;

    const auto pathIt = m_pathToGuid.find(FilesystemPathKey(path));
    if (pathIt != m_pathToGuid.end())
        m_metas.erase(pathIt->second);

    std::string metaPath = InxResourceMeta::GetMetaFilePath(path);
    auto metaFsPath = ToFsPath(metaPath);
    if (fs::exists(metaFsPath)) {
        fs::remove(metaFsPath);
        INXLOG_DEBUG("AssetDatabase: deleted metadata file: ", metaPath);
    }
}

void AssetDatabase::MoveMetadata(const std::string &oldPath, const std::string &newPath)
{
    namespace fs = std::filesystem;

    std::string oldMetaPath = InxResourceMeta::GetMetaFilePath(oldPath);
    std::string newMetaPath = InxResourceMeta::GetMetaFilePath(newPath);

    InxResourceMeta meta;
    std::string existingGuid;

    auto oldMetaFsPath = ToFsPath(oldMetaPath);
    if (fs::exists(oldMetaFsPath) && meta.LoadFromFile(oldMetaPath)) {
        existingGuid = meta.GetGuid();

        meta.UpdateFilePath(newPath);
        if (!meta.SaveToFile(newMetaPath))
            throw std::runtime_error("Failed to persist moved asset metadata: " + newMetaPath);
        std::error_code removeError;
        if (!fs::remove(oldMetaFsPath, removeError) || removeError) {
            std::error_code rollbackError;
            fs::remove(ToFsPath(newMetaPath), rollbackError);
            throw std::runtime_error("Failed to remove old asset metadata: " + oldMetaPath);
        }

        auto it = m_metas.find(existingGuid);
        if (it != m_metas.end()) {
            auto movedMeta = std::make_shared<InxResourceMeta>(*it->second);
            movedMeta->UpdateFilePath(newPath);
            it->second = std::move(movedMeta);
        }

        INXLOG_INFO("AssetDatabase: moved metadata ", oldPath, " -> ", newPath, " (guid preserved: ", existingGuid,
                    ")");
    } else {
        std::string ext = FromFsPath(ToFsPath(newPath).extension());
        ResourceType type = GetResourcesType(ext);

        if (type != ResourceType::Meta) {
            CreateOrLoadMetadata(newPath, type, false, true, FilesystemPathKey(newPath));
            INXLOG_INFO("AssetDatabase: created metadata at moved path: ", newPath);
        }
    }
}

namespace
{

// Shader ids were migrated from snake_case / hyphenated / "Infernux/"-prefixed
// forms to "Title Case With Spaces". Materials saved before the migration
// still reference the old spellings, so lookups fall back to a normalized
// comparison: lowercase, separators stripped, legacy namespace prefix dropped.
std::string NormalizeShaderIdForLookup(const std::string &shaderId)
{
    std::string normalized;
    normalized.reserve(shaderId.size());
    for (const char character : shaderId) {
        if (character == ' ' || character == '_' || character == '-')
            continue;
        normalized.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(character))));
    }
    constexpr std::string_view legacyPrefix = "infernux/";
    if (normalized.rfind(legacyPrefix, 0) == 0)
        normalized.erase(0, legacyPrefix.size());
    return normalized;
}

} // namespace

std::string AssetDatabase::FindShaderPathById(const std::string &shaderId, const std::string &shaderType) const
{
    std::string expectedExt;
    if (shaderType == "vertex" || shaderType == ".vert" || shaderType == "vert") {
        expectedExt = ".vert";
    } else if (shaderType == "fragment" || shaderType == ".frag" || shaderType == "frag") {
        expectedExt = ".frag";
    } else {
        return "";
    }

    const std::string normalizedQuery = NormalizeShaderIdForLookup(shaderId);
    std::string normalizedMatch;

    const auto snapshot = LoadQuerySnapshot();
    for (const auto &[guid, meta] : snapshot->metas) {
        if (!meta)
            continue;

        if (!meta->HasKey("type"))
            continue;
        std::string type = meta->GetDataAs<std::string>("type");
        bool matchesType =
            (expectedExt == ".vert" && type == "vertex") || (expectedExt == ".frag" && type == "fragment");
        if (!matchesType)
            continue;

        if (meta->HasKey("shader_id") && meta->HasKey("file_path")) {
            std::string metaShaderId = meta->GetDataAs<std::string>("shader_id");
            if (metaShaderId == shaderId) {
                return meta->GetDataAs<std::string>("file_path");
            }
            if (normalizedMatch.empty() && NormalizeShaderIdForLookup(metaShaderId) == normalizedQuery) {
                normalizedMatch = meta->GetDataAs<std::string>("file_path");
            }
        }
    }

    return normalizedMatch;
}

} // namespace infernux
