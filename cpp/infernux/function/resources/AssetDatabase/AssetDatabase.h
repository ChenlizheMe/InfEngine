#pragma once

#include <core/threading/JobSystem.h>
#include <core/types/InxFwdType.h>
#include <function/resources/AssetDatabase/AssetIndex.h>
#include <function/resources/AssetDependencyGraph.h>
#include <function/resources/AssetImporter/ImporterRegistry.h>
#include <function/resources/AssetRegistry/IAssetLoader.h>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <platform/filesystem/DocumentTransaction.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <exception>
#include <filesystem>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace infernux
{

class InxRenderer;

enum class AssetMutationErrorCode : uint8_t
{
    None,
    InvalidPath,
    NotFound,
    UnsupportedType,
    ImportFailed,
    RuntimeApplyFailed,
};

struct AssetMutationResult
{
    bool succeeded = false;
    bool databaseCommitted = false;
    bool changed = false;
    std::string operation;
    std::string guid;
    std::string path;
    std::string previousPath;
    ResourceType resourceType = ResourceType::DefaultText;
    AssetMutationErrorCode errorCode = AssetMutationErrorCode::None;
    std::string error;
    uint64_t queryGeneration = 0;

    [[nodiscard]] explicit operator bool() const noexcept
    {
        return succeeded;
    }
};

struct AssetCatalogEntry
{
    std::string guid;
    std::string path;
    std::string name;
    std::string sortKey;
    ResourceType resourceType = ResourceType::DefaultText;
    AssetFileFingerprint source;
};

class AssetCatalogSnapshot final
{
  public:
    [[nodiscard]] uint64_t GetGeneration() const noexcept
    {
        return m_generation;
    }
    [[nodiscard]] const std::vector<AssetCatalogEntry> &GetDirectory(const std::string &normalizedDirectory) const;
    [[nodiscard]] const std::unordered_map<std::string, std::vector<AssetCatalogEntry>> &GetDirectories() const noexcept
    {
        return m_directories;
    }

  private:
    friend class AssetDatabase;
    uint64_t m_generation = 0;
    std::unordered_map<std::string, std::vector<AssetCatalogEntry>> m_directories;
};

/**
 * @brief Central asset database for the project.
 *
 * Responsibilities:
 * - Import assets and generate .meta files
 * - Maintain GUID <-> path mappings
 * - Provide Asset CRUD operations for editor and file watcher
 * - Own the ImporterRegistry and drive dependency scanning on import
 * - Dispatch AssetEvent notifications via AssetDependencyGraph
 * - Manage resource loaders, metadata cache and
 * compiled resource cache
 *
 * Durable-reference invariant:
 * - Persistent asset dependencies are identified by GUID
 * only.
 * - Paths and path_hint values are mutable authoring/display metadata.
 * - Importers, loaders, runtime code
 * and dependency publication must never
 *   call GetGuidFromPath() to repair or infer a stored dependency.
 * -
 * Path-to-GUID conversion is permitted only at an explicit editor authoring
 *   boundary, before the reference is
 * persisted.
 */
class AssetDatabase
{
  public:
    AssetDatabase();
    ~AssetDatabase();

    /// @brief Initialize database with project root path.
    /// Also creates and registers all built-in importers.
    void Initialize(const std::string &projectRoot);

    /// @brief Refresh all assets by scanning the Assets folder
    void Refresh();

    /// @brief Schedule filesystem enumeration and fingerprint collection.
    /// The worker never mutates AssetDatabase, metadata, or dependency state.
    void BeginRefresh();

    /// @brief Advance scan, worker import, and owner commit without blocking.
    /// @return false while either worker phase is incomplete.
    bool TryCommitRefresh();

    [[nodiscard]] bool IsRefreshPending() const;
    [[nodiscard]] bool IsOwnerThread() const;

    /// @brief Persist the current derived index if runtime CRUD made it dirty.
    void FlushDerivedIndex();

    /// @brief Add an extra directory to scan during Refresh (e.g. Library/Resources).
    void AddScanRoot(const std::string &path);

    /// @brief Add an immutable packaged-resource root. Missing metadata is kept in memory.
    void AddReadOnlyScanRoot(const std::string &path);

    /// @brief Overlay the cooked Player asset identities after the immutable
    /// project payload has been scanned. The catalog replaces editor .meta
    /// sidecars inside Player builds and is never persisted back to disk.
    void InstallRuntimeAssetCatalog(const std::string &catalogPath);

    /// @brief Import an asset and create/update its meta.
    /// Runs the appropriate AssetImporter to scan dependencies.
    [[nodiscard]] AssetMutationResult ImportAsset(const std::string &path);

    /// @brief Re-run metadata/dependency import for an already registered asset.
    /// Preserves the existing GUID and returns false for missing or unregistered paths.
    [[nodiscard]] AssetMutationResult ReimportAsset(const std::string &path);

    /// @brief Delete asset and meta.
    /// Notifies dependents via AssetDependencyGraph::NotifyEvent(Deleted).
    [[nodiscard]] AssetMutationResult DeleteAsset(const std::string &path);

    /// @brief Move/rename asset preserving GUID.
    /// Notifies dependents via AssetDependencyGraph::NotifyEvent(Moved).
    [[nodiscard]] AssetMutationResult MoveAsset(const std::string &oldPath, const std::string &newPath);

    /// Commit a preflighted relocation batch with one catalog publication.
    [[nodiscard]] std::vector<AssetMutationResult>
    MoveAssetsBatch(const std::vector<std::pair<std::string, std::string>> &moves);

    /// @brief Check if a GUID exists
    [[nodiscard]] bool ContainsGuid(const std::string &guid) const;

    /// @brief Check if a path exists in database
    [[nodiscard]] bool ContainsPath(const std::string &path) const;

    /// @brief Get GUID by path (empty if not found).
    ///
    /// This is an authoring/discovery API for imports, file watchers, pickers
    /// and drag/drop. It is forbidden as a fallback for resolving a persistent
    /// asset reference or publishing a dependency edge; those operations must
    /// already carry the authoritative GUID.
    [[nodiscard]] std::string GetGuidFromPath(const std::string &path) const;

    /// @brief Resolve the current mutable location of an authoritative GUID.
    /// Returns empty when the referenced asset is missing.
    [[nodiscard]] std::string GetPathFromGuid(const std::string &guid) const;

    /// @brief Get immutable metadata by GUID. The shared result remains valid
    /// across concurrent index publication and asset deletion.
    [[nodiscard]] std::shared_ptr<const InxResourceMeta> GetMetaByGuid(const std::string &guid) const;

    /// @brief Get immutable metadata by path.
    [[nodiscard]] std::shared_ptr<const InxResourceMeta> GetMetaByPath(const std::string &path) const;

    /// @brief Get all GUIDs
    [[nodiscard]] std::vector<std::string> GetAllGuids() const;

    /// @brief Get all asset paths from one published database generation.
    [[nodiscard]] std::vector<std::string> GetAllAssetPaths() const;

    /// @brief Acquire one immutable path/type/fingerprint catalog generation.
    [[nodiscard]] std::shared_ptr<const AssetCatalogSnapshot> GetCatalogSnapshot() const;
    [[nodiscard]] std::vector<AssetCatalogEntry> GetDirectoryCatalog(const std::string &directory) const;

    /// @brief Published query snapshot generation and asset count.
    [[nodiscard]] uint64_t GetQueryGeneration() const;
    [[nodiscard]] size_t GetAssetCount() const;
    [[nodiscard]] size_t GetLastRefreshReusedCount() const noexcept
    {
        return m_lastRefreshReusedCount;
    }
    [[nodiscard]] size_t GetLastRefreshImportedCount() const noexcept
    {
        return m_lastRefreshImportedCount;
    }
    [[nodiscard]] const std::vector<std::string> &GetLastRefreshImportedPaths() const noexcept
    {
        return m_lastRefreshImportedPaths;
    }
    [[nodiscard]] size_t GetLastRefreshImporterTaskCount() const noexcept
    {
        return m_lastRefreshImporterTaskCount;
    }
    [[nodiscard]] size_t GetLastRefreshMetadataTaskCount() const noexcept
    {
        return m_lastRefreshMetadataTaskCount;
    }
    [[nodiscard]] size_t GetLastRefreshWorkerMetadataCount() const noexcept
    {
        return m_lastRefreshWorkerMetadataCount;
    }
    [[nodiscard]] size_t GetLastRefreshWorkerImporterCount() const noexcept
    {
        return m_lastRefreshWorkerImporterCount;
    }
    [[nodiscard]] size_t GetLastRefreshScannedCount() const noexcept
    {
        return m_lastRefreshScannedCount;
    }
    [[nodiscard]] double GetLastRefreshScanMilliseconds() const noexcept
    {
        return m_lastRefreshScanMilliseconds;
    }
    [[nodiscard]] double GetLastRefreshCommitMilliseconds() const noexcept
    {
        return m_lastRefreshCommitMilliseconds;
    }
    [[nodiscard]] double GetLastRefreshPrepareMilliseconds() const noexcept
    {
        return m_lastRefreshPrepareMilliseconds;
    }
    [[nodiscard]] double GetLastRefreshFinalizeMilliseconds() const noexcept
    {
        return m_lastRefreshFinalizeMilliseconds;
    }
    [[nodiscard]] double GetLastRefreshOwnerMergeMaxSliceMilliseconds() const noexcept
    {
        return m_lastRefreshOwnerMergeMaxSliceMilliseconds;
    }
    [[nodiscard]] size_t GetLastRefreshOwnerMergeSliceCount() const noexcept
    {
        return m_lastRefreshOwnerMergeSliceCount;
    }
    [[nodiscard]] double GetLastRefreshMetadataWriteMilliseconds() const noexcept
    {
        return m_lastRefreshMetadataWriteMilliseconds;
    }
    [[nodiscard]] uint64_t GetLastRefreshJournalUncompressedBytes() const noexcept
    {
        return m_lastRefreshJournalUncompressedBytes;
    }
    [[nodiscard]] uint64_t GetLastRefreshJournalBytes() const noexcept
    {
        return m_lastRefreshJournalBytes;
    }
    [[nodiscard]] double GetLastRefreshJournalSerializeMilliseconds() const noexcept
    {
        return m_lastRefreshJournalSerializeMilliseconds;
    }
    [[nodiscard]] double GetLastRefreshJournalWriteMilliseconds() const noexcept
    {
        return m_lastRefreshJournalWriteMilliseconds;
    }
    [[nodiscard]] double GetLastRefreshJournalApplyMilliseconds() const noexcept
    {
        return m_lastRefreshJournalApplyMilliseconds;
    }
    [[nodiscard]] bool WasLastRefreshScanOnWorker() const noexcept
    {
        return m_lastRefreshScanOnWorker;
    }
    [[nodiscard]] double GetLastRefreshRestoreMilliseconds() const noexcept
    {
        return m_lastRefreshRestoreMilliseconds;
    }
    [[nodiscard]] double GetLastRefreshImportMilliseconds() const noexcept
    {
        return m_lastRefreshImportMilliseconds;
    }
    [[nodiscard]] double GetLastRefreshIndexBuildMilliseconds() const noexcept
    {
        return m_lastRefreshIndexBuildMilliseconds;
    }
    [[nodiscard]] double GetLastRefreshIndexSaveMilliseconds() const noexcept
    {
        return m_lastRefreshIndexSaveMilliseconds;
    }
    [[nodiscard]] bool WasLastRefreshIndexBuildOnWorker() const noexcept
    {
        return m_lastRefreshIndexBuildOnWorker;
    }
    [[nodiscard]] double GetLastRefreshQueryBuildMilliseconds() const noexcept
    {
        return m_lastRefreshQueryBuildMilliseconds;
    }
    [[nodiscard]] bool WasLastRefreshQueryBuildOnWorker() const noexcept
    {
        return m_lastRefreshQueryBuildOnWorker;
    }
    [[nodiscard]] double GetLastRefreshDependencyBuildMilliseconds() const noexcept
    {
        return m_lastRefreshDependencyBuildMilliseconds;
    }
    [[nodiscard]] bool WasLastRefreshDependencyBuildOnWorker() const noexcept
    {
        return m_lastRefreshDependencyBuildOnWorker;
    }
    [[nodiscard]] double GetLastRefreshPublishMilliseconds() const noexcept
    {
        return m_lastRefreshPublishMilliseconds;
    }
    [[nodiscard]] const std::string &GetAssetIndexPath() const noexcept
    {
        return m_assetIndexPath;
    }

    /// @brief Check if path is within Assets folder
    [[nodiscard]] bool IsAssetPath(const std::string &path) const;

    /// @brief Get project root
    [[nodiscard]] const std::string &GetProjectRoot() const
    {
        return m_projectRoot;
    }

    /// @brief Get assets root
    [[nodiscard]] const std::string &GetAssetsRoot() const
    {
        return m_assetsRoot;
    }

    /// @brief Resolve the deterministic project-local runtime CPU artifact path.
    /// Returns empty for resource types that do not produce a runtime artifact.
    [[nodiscard]] std::string GetRuntimeArtifactPath(const std::string &guid, ResourceType type) const;
    [[nodiscard]] std::string GetSkinnedMeshArtifactPath(const std::string &guid) const;

    /// @brief Access the dependency graph (singleton shorthand)
    [[nodiscard]] static AssetDependencyGraph &GetDependencyGraph()
    {
        return AssetDependencyGraph::Instance();
    }

    /// @brief Access the importer registry
    [[nodiscard]] ImporterRegistry &GetImporterRegistry()
    {
        return m_importerRegistry;
    }

    // ========================================================================
    // Resource management
    // ========================================================================

    /// @brief Read exact source bytes without platform newline translation.
    bool ReadFile(const std::string &filePath, std::vector<char> &content) const;

    /// @brief Find shader file path by shader_id
    [[nodiscard]] std::string FindShaderPathById(const std::string &shaderId, const std::string &shaderType) const;

    /// @brief Get resource type by file extension
    [[nodiscard]] ResourceType GetResourcesType(const std::string &extensionName) const;

    /// @brief Get resource type from a file path
    [[nodiscard]] ResourceType GetResourceTypeForPath(const std::string &filePath) const;

  private:
    struct AssetScanRoot
    {
        std::string path;
        bool readOnly = false;
    };

    struct AssetScanRequest
    {
        std::vector<AssetScanRoot> roots;
        std::string assetIndexPath;
        std::string normalizedProjectRoot;
        uint64_t expectedQueryGeneration = 0;
    };

    struct AssetScanFile
    {
        std::string path;
        std::string normalizedPath;
        std::string identityKey;
        AssetFileFingerprint source;
        AssetFileFingerprint meta;
        bool readOnly = false;
    };

    struct AssetScanArtifact
    {
        AssetIndex index;
        std::vector<AssetScanFile> files;
        std::vector<std::string> orphanedTempFiles;
        std::vector<std::string> diagnostics;
        std::thread::id producerThread;
        double scanMilliseconds = 0.0;
    };

    struct PendingAssetScan
    {
        explicit PendingAssetScan(uint64_t generation) : expectedQueryGeneration(generation)
        {
        }

        std::mutex mutex;
        std::condition_variable completedCv;
        std::optional<AssetScanArtifact> artifact;
        std::exception_ptr failure;
        uint64_t expectedQueryGeneration = 0;
        bool complete = false;
    };

    struct CachedFileState
    {
        AssetFileFingerprint source;
        AssetFileFingerprint meta;
        bool readOnly = false;
    };

    struct ImportResultState
    {
        bool succeeded = true;
        std::string error;
    };

    struct WorkingSet
    {
        std::unordered_map<std::string, std::string> guidToPath;
        std::unordered_map<std::string, std::string> pathToGuid;
        std::unordered_map<std::string, std::shared_ptr<InxResourceMeta>> metas;
        std::unordered_map<std::string, CachedFileState> fileStates;
        std::unordered_map<std::string, ImportResultState> importResults;
        AssetIndex assetIndex;
        bool assetIndexDirty = false;
    };

    struct PendingAsset
    {
        std::string guid;
        std::string path;
        std::string normalizedPath;
        AssetFileFingerprint source;
        AssetFileFingerprint meta;
        bool readOnly = false;
        bool persistMetadata = true;
    };

    struct WorkerImport
    {
        size_t assetIndex = 0;
        AssetImporter *importer = nullptr;
        ImportRequest request;
        AssetFileFingerprint expectedSource;
        std::optional<ImportArtifact> artifact;
        std::string error;
        std::thread::id producerThread;
    };

    struct WorkerMetadataPrepare
    {
        enum class Mode : uint8_t
        {
            LoadExisting,
            Rebuild,
            CreateOrLoad,
        };

        AssetScanFile file;
        ResourceType resourceType = ResourceType::DefaultText;
        const IAssetLoader *loader = nullptr;
        std::string fallbackGuid;
        std::optional<InxResourceMeta> metadata;
        std::string error;
        std::thread::id producerThread;
        Mode mode = Mode::CreateOrLoad;
    };

    struct QuerySnapshot
    {
        uint64_t generation = 0;
        std::unordered_map<std::string, std::string> guidToPath;
        std::unordered_map<std::string, std::string> pathToGuid;
        std::unordered_map<std::string, std::shared_ptr<const InxResourceMeta>> metas;
        std::shared_ptr<const AssetCatalogSnapshot> catalog;
    };

    struct PendingRefreshCommit
    {
        enum class Phase : uint8_t
        {
            MetadataPrepare,
            MetadataMerge,
            Import,
            ImportMerge,
            MetadataWrite,
            FileStateMerge,
            ReadyToBuildIndex,
            IndexBuild,
            ReadyToFinalize,
        };

        AssetScanArtifact scanArtifact;
        std::vector<WorkerMetadataPrepare> workerMetadata;
        std::vector<PendingAsset> pendingImports;
        std::vector<WorkerImport> workerImports;
        std::unordered_map<std::string, std::vector<std::string>> restoredDependencies;
        WorkingSet stagedWorkingSet;
        std::unordered_map<std::string, std::vector<std::string>> committedDependencies;
        std::vector<DocumentTransactionEntry> metadataWrites;
        JobHandle metadataJobs;
        JobHandle importJobs;
        JobHandle metadataWriteJob;
        JobHandle indexBuildJob;
        std::exception_ptr metadataWriteFailure;
        std::exception_ptr indexBuildFailure;
        std::optional<AssetIndex> builtAssetIndex;
        std::shared_ptr<QuerySnapshot> builtQuerySnapshot;
        std::shared_ptr<const AssetDependencySnapshot> builtDependencySnapshot;
        bool indexRebuildRequired = false;
        uint64_t journalUncompressedBytes = 0;
        uint64_t journalBytes = 0;
        double journalSerializeMilliseconds = 0.0;
        double journalWriteMilliseconds = 0.0;
        double journalApplyMilliseconds = 0.0;
        double indexBuildMilliseconds = 0.0;
        double indexSaveMilliseconds = 0.0;
        double queryBuildMilliseconds = 0.0;
        double dependencyBuildMilliseconds = 0.0;
        std::thread::id indexProducerThread;
        std::thread::id queryProducerThread;
        std::thread::id dependencyProducerThread;
        uint64_t expectedDependencyGeneration = 0;
        std::unordered_map<std::string, AssetFileFingerprint> committedMetadataFingerprints;
        std::chrono::steady_clock::time_point commitStarted;
        std::chrono::steady_clock::time_point importStarted;
        std::chrono::steady_clock::time_point metadataWriteStarted;
        size_t metadataMergeCursor = 0;
        size_t importRequestCursor = 0;
        size_t importResultMergeCursor = 0;
        size_t metadataWritePrepareCursor = 0;
        size_t dependencyMergeCursor = 0;
        size_t fileStateMergeCursor = 0;
        bool importMergeInitialized = false;
        double ownerFinalizeMilliseconds = 0.0;
        double ownerMergeMaxSliceMilliseconds = 0.0;
        size_t ownerMergeSliceCount = 0;
        Phase phase = Phase::MetadataPrepare;
    };

    void AssertMutationThread(const char *operation) const;
    void AssertNoPendingCommit(const char *operation) const;
    [[nodiscard]] bool CanReadWorkingSet() const;
    [[nodiscard]] WorkingSet TakeWorkingSet();
    void InstallWorkingSet(WorkingSet workingSet);
    void PublishQuerySnapshot();
    void PublishQuerySnapshotForPaths(const std::vector<std::string> &paths);
    void InstallQuerySnapshot(std::shared_ptr<QuerySnapshot> snapshot) noexcept;
    [[nodiscard]] std::shared_ptr<const QuerySnapshot> LoadQuerySnapshot() const;
    [[nodiscard]] AssetScanRequest CaptureScanRequest() const;
    [[nodiscard]] static AssetScanArtifact BuildScanArtifact(const AssetScanRequest &request);
    static void PrepareMetadata(WorkerMetadataPrepare &item);
    [[nodiscard]] static AssetIndex
    BuildDerivedIndexArtifact(const WorkingSet &workingSet,
                              const std::unordered_map<std::string, std::vector<std::string>> &dependenciesByGuid,
                              const std::string &normalizedProjectRoot);
    [[nodiscard]] static std::shared_ptr<QuerySnapshot>
    BuildQuerySnapshotArtifact(const std::unordered_map<std::string, std::string> &guidToPath,
                               const std::unordered_map<std::string, std::string> &pathToGuid,
                               const std::unordered_map<std::string, std::shared_ptr<InxResourceMeta>> &metas,
                               const std::unordered_map<std::string, CachedFileState> &fileStates, uint64_t generation);
    [[nodiscard]] bool CommitScanArtifact(AssetScanArtifact artifact, uint64_t expectedQueryGeneration);
    [[nodiscard]] bool ContinuePendingMetadataMerge(const std::shared_ptr<PendingRefreshCommit> &state);
    [[nodiscard]] bool ContinuePendingImportMerge(const std::shared_ptr<PendingRefreshCommit> &state);
    [[nodiscard]] bool ContinuePendingFileStateMerge(const std::shared_ptr<PendingRefreshCommit> &state);
    void BeginPendingIndexBuild(const std::shared_ptr<PendingRefreshCommit> &state);
    void FinalizePendingRefreshCommit(const std::shared_ptr<PendingRefreshCommit> &state);
    void WaitForPendingWork() const noexcept;
    [[nodiscard]] static bool IsIgnoredImportPath(const std::filesystem::path &path);
    [[nodiscard]] bool IsMetaFile(const std::filesystem::path &path) const;
    void UpdateMapping(const std::string &guid, const std::string &path);
    void RemoveMappingByGuid(const std::string &guid);
    void RemoveMappingByPath(const std::string &path);
    void UpdateCachedFileState(const std::string &path, bool readOnly);

    /// Run the matching importer for this asset (dependency scanning etc.)
    bool RunImporter(const std::string &guid, const std::string &path, bool isReimport, bool persistMetadata = true);

    std::string CreateOrLoadMetadata(const std::string &filePath, ResourceType type, bool readOnly,
                                     bool persistMetadata, const std::string &identityKey);
    [[nodiscard]] std::string RebuildMetadata(const std::string &filePath, bool persistMetadata = true);
    void DeleteMetadata(const std::string &filePath);
    void MoveMetadata(const std::string &oldFilePath, const std::string &newFilePath);
    void RebuildDerivedIndex();
    [[nodiscard]] bool IsReadOnlyPath(const std::string &normalizedPath) const;

    std::string m_projectRoot;
    std::string m_assetsRoot;
    std::vector<std::string> m_extraScanRoots;
    std::unordered_set<std::string> m_readOnlyScanRoots;
    std::thread::id m_ownerThread;
    bool m_initialized = false;

    // GUID -> path
    std::unordered_map<std::string, std::string> m_guidToPath;
    // normalized path -> GUID
    std::unordered_map<std::string, std::string> m_pathToGuid;

    // Asset importer registry (populated in Initialize)
    ImporterRegistry m_importerRegistry;

    // Resource loaders (one per ResourceType, used for meta creation/loading)
    // Non-owning pointers — ownership is in AssetRegistry::m_loaders.
    std::unordered_map<ResourceType, IAssetLoader *> m_loaders;
    // GUID -> metadata cache
    std::unordered_map<std::string, std::shared_ptr<InxResourceMeta>> m_metas;

    std::unordered_map<std::string, CachedFileState> m_fileStates;

    // Readers atomically acquire one immutable generation. Only the owner
    // thread mutates the working maps above and publishes completed commits.
    std::shared_ptr<const QuerySnapshot> m_querySnapshot;
    uint64_t m_queryGeneration = 0;

    AssetIndex m_assetIndex;
    std::string m_assetIndexPath;
    std::string m_assetTransactionJournalPath;
    std::unordered_map<std::string, ImportResultState> m_importResults;
    size_t m_lastRefreshReusedCount = 0;
    size_t m_lastRefreshImportedCount = 0;
    std::vector<std::string> m_lastRefreshImportedPaths;
    size_t m_lastRefreshImporterTaskCount = 0;
    size_t m_lastRefreshMetadataTaskCount = 0;
    size_t m_lastRefreshWorkerMetadataCount = 0;
    size_t m_lastRefreshWorkerImporterCount = 0;
    size_t m_lastRefreshScannedCount = 0;
    double m_lastRefreshScanMilliseconds = 0.0;
    double m_lastRefreshCommitMilliseconds = 0.0;
    double m_lastRefreshPrepareMilliseconds = 0.0;
    double m_lastRefreshFinalizeMilliseconds = 0.0;
    double m_lastRefreshOwnerMergeMaxSliceMilliseconds = 0.0;
    size_t m_lastRefreshOwnerMergeSliceCount = 0;
    double m_lastRefreshMetadataWriteMilliseconds = 0.0;
    uint64_t m_lastRefreshJournalUncompressedBytes = 0;
    uint64_t m_lastRefreshJournalBytes = 0;
    double m_lastRefreshJournalSerializeMilliseconds = 0.0;
    double m_lastRefreshJournalWriteMilliseconds = 0.0;
    double m_lastRefreshJournalApplyMilliseconds = 0.0;
    double m_lastRefreshRestoreMilliseconds = 0.0;
    double m_lastRefreshImportMilliseconds = 0.0;
    double m_lastRefreshIndexBuildMilliseconds = 0.0;
    double m_lastRefreshIndexSaveMilliseconds = 0.0;
    bool m_lastRefreshIndexBuildOnWorker = false;
    double m_lastRefreshQueryBuildMilliseconds = 0.0;
    bool m_lastRefreshQueryBuildOnWorker = false;
    double m_lastRefreshDependencyBuildMilliseconds = 0.0;
    bool m_lastRefreshDependencyBuildOnWorker = false;
    double m_lastRefreshPublishMilliseconds = 0.0;
    bool m_lastRefreshScanOnWorker = false;
    bool m_assetIndexDirty = false;
    std::shared_ptr<PendingAssetScan> m_pendingAssetScan;
    JobHandle m_pendingAssetScanJob;
    std::shared_ptr<PendingRefreshCommit> m_pendingRefreshCommit;

  public:
    /// @brief Set a meta-creation loader for a resource type.
    /// Called by AssetRegistry after all loaders are registered.
    void SetMetaLoader(ResourceType type, IAssetLoader *loader)
    {
        AssertMutationThread("SetMetaLoader");
        AssertNoPendingCommit("SetMetaLoader");
        if (!loader)
            throw std::invalid_argument("AssetDatabase meta loader cannot be null");
        m_loaders[type] = loader;
    }
};

} // namespace infernux
