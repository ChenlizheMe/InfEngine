#pragma once

#include <core/types/InxFwdType.h>

#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace infernux
{

enum class AssetEvent
{
    Deleted,
    Modified,
    Moved,
};

using AssetEventCallback =
    std::function<void(const std::string &dependentGuid, const std::string &dependencyGuid, AssetEvent event)>;

class AssetDependencyGraph;

class AssetDependencySnapshot final
{
  public:
    [[nodiscard]] uint64_t GetGeneration() const noexcept
    {
        return m_generation;
    }
    [[nodiscard]] size_t GetEdgeCount() const noexcept
    {
        return m_edgeCount;
    }
    [[nodiscard]] size_t GetNodeCount() const noexcept
    {
        return m_nodes.size();
    }

  private:
    friend class AssetDependencyGraph;
    uint64_t m_generation = 0;
    size_t m_edgeCount = 0;
    std::unordered_map<std::string, std::unordered_set<std::string>> m_dependencies;
    std::unordered_map<std::string, std::unordered_set<std::string>> m_dependents;
    std::unordered_set<std::string> m_nodes;
};

/**
 * Asset-to-asset edges and runtime-object usage have different lifetimes.
 * Asset edges are published as immutable generations; runtime usage remains a
 * small mutable overlay. Queries and lifecycle notifications observe their
 * union without making refresh publication copy runtime state.
 */
class AssetDependencyGraph
{
  public:
    static AssetDependencyGraph &Instance();

    AssetDependencyGraph(const AssetDependencyGraph &) = delete;
    AssetDependencyGraph &operator=(const AssetDependencyGraph &) = delete;

    void AddAssetDependency(const std::string &assetGuid, const std::string &dependencyGuid);
    void RemoveAssetDependency(const std::string &assetGuid, const std::string &dependencyGuid);
    void ClearAssetDependenciesOf(const std::string &assetGuid);
    void SetAssetDependencies(const std::string &assetGuid, const std::unordered_set<std::string> &dependencyGuids);

    [[nodiscard]] static std::shared_ptr<const AssetDependencySnapshot>
    BuildAssetSnapshot(const std::unordered_map<std::string, std::vector<std::string>> &dependenciesByAsset,
                       uint64_t generation);
    void InstallAssetSnapshot(std::shared_ptr<const AssetDependencySnapshot> snapshot);
    [[nodiscard]] std::shared_ptr<const AssetDependencySnapshot> GetAssetSnapshot() const;
    [[nodiscard]] uint64_t GetAssetGeneration() const;

    void AddRuntimeDependency(const std::string &objectGuid, const std::string &assetGuid);
    void RemoveRuntimeDependency(const std::string &objectGuid, const std::string &assetGuid);
    void ClearRuntimeDependenciesOf(const std::string &objectGuid);

    /// Remove dependencies owned by an asset while retaining references to it.
    /// Incoming edges represent serialized missing references and must survive
    /// deletion so a later reimport/Undo can notify and reconnect dependents.
    void RemoveAsset(const std::string &guid);

    [[nodiscard]] std::unordered_set<std::string> GetDependencies(const std::string &guid) const;
    [[nodiscard]] std::unordered_map<std::string, std::unordered_set<std::string>>
    GetDependenciesBatch(const std::vector<std::string> &guids) const;
    [[nodiscard]] std::unordered_set<std::string> GetDependents(const std::string &guid) const;
    [[nodiscard]] bool HasDependency(const std::string &userGuid, const std::string &dependencyGuid) const;

    void RegisterCallback(ResourceType type, AssetEventCallback callback);
    void NotifyEvent(const std::string &guid, ResourceType type, AssetEvent event);

    [[nodiscard]] size_t GetEdgeCount() const;
    [[nodiscard]] size_t GetNodeCount() const;
    void Clear();

  private:
    AssetDependencyGraph();
    ~AssetDependencyGraph() = default;

    void PublishAssetMutation(const std::function<void(AssetDependencySnapshot &)> &mutation);
    static void AddEdge(AssetDependencySnapshot &snapshot, const std::string &userGuid,
                        const std::string &dependencyGuid);
    static void RemoveEdge(AssetDependencySnapshot &snapshot, const std::string &userGuid,
                           const std::string &dependencyGuid);
    static void ClearEdgesOf(AssetDependencySnapshot &snapshot, const std::string &userGuid);
    static void RebuildStatistics(AssetDependencySnapshot &snapshot);

    std::shared_ptr<const AssetDependencySnapshot> m_assetSnapshot;
    mutable std::mutex m_assetWriteMutex;

    std::unordered_map<std::string, std::unordered_set<std::string>> m_runtimeDependencies;
    std::unordered_map<std::string, std::unordered_set<std::string>> m_runtimeDependents;
    std::unordered_map<ResourceType, std::vector<AssetEventCallback>> m_callbacks;
    mutable std::mutex m_runtimeMutex;
};

} // namespace infernux
