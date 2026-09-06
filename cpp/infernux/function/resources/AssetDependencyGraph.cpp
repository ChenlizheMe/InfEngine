#include "AssetDependencyGraph.h"

#include <core/log/InxLog.h>

#include <algorithm>
#include <atomic>
#include <stdexcept>

namespace infernux
{

namespace
{
void RequireEdge(const std::string &userGuid, const std::string &dependencyGuid)
{
    if (userGuid.empty() || dependencyGuid.empty())
        throw std::invalid_argument("Dependency edges require non-empty GUIDs");
    if (userGuid == dependencyGuid)
        throw std::invalid_argument("An object cannot depend on itself");
}
} // namespace

AssetDependencyGraph::AssetDependencyGraph() : m_assetSnapshot(std::make_shared<const AssetDependencySnapshot>())
{
}

AssetDependencyGraph &AssetDependencyGraph::Instance()
{
    // Explicit engine cleanup owns lifetime state; avoid static teardown order.
    static AssetDependencyGraph *instance = new AssetDependencyGraph();
    return *instance;
}

std::shared_ptr<const AssetDependencySnapshot> AssetDependencyGraph::GetAssetSnapshot() const
{
    return std::atomic_load_explicit(&m_assetSnapshot, std::memory_order_acquire);
}

uint64_t AssetDependencyGraph::GetAssetGeneration() const
{
    return GetAssetSnapshot()->GetGeneration();
}

void AssetDependencyGraph::AddEdge(AssetDependencySnapshot &snapshot, const std::string &userGuid,
                                   const std::string &dependencyGuid)
{
    RequireEdge(userGuid, dependencyGuid);
    if (snapshot.m_dependencies[userGuid].insert(dependencyGuid).second)
        snapshot.m_dependents[dependencyGuid].insert(userGuid);
}

void AssetDependencyGraph::RemoveEdge(AssetDependencySnapshot &snapshot, const std::string &userGuid,
                                      const std::string &dependencyGuid)
{
    const auto forward = snapshot.m_dependencies.find(userGuid);
    if (forward != snapshot.m_dependencies.end()) {
        forward->second.erase(dependencyGuid);
        if (forward->second.empty())
            snapshot.m_dependencies.erase(forward);
    }
    const auto reverse = snapshot.m_dependents.find(dependencyGuid);
    if (reverse != snapshot.m_dependents.end()) {
        reverse->second.erase(userGuid);
        if (reverse->second.empty())
            snapshot.m_dependents.erase(reverse);
    }
}

void AssetDependencyGraph::ClearEdgesOf(AssetDependencySnapshot &snapshot, const std::string &userGuid)
{
    const auto forward = snapshot.m_dependencies.find(userGuid);
    if (forward == snapshot.m_dependencies.end())
        return;
    for (const auto &dependencyGuid : forward->second) {
        const auto reverse = snapshot.m_dependents.find(dependencyGuid);
        if (reverse == snapshot.m_dependents.end())
            continue;
        reverse->second.erase(userGuid);
        if (reverse->second.empty())
            snapshot.m_dependents.erase(reverse);
    }
    snapshot.m_dependencies.erase(forward);
}

void AssetDependencyGraph::RebuildStatistics(AssetDependencySnapshot &snapshot)
{
    snapshot.m_edgeCount = 0;
    snapshot.m_nodes.clear();
    for (const auto &[userGuid, dependencies] : snapshot.m_dependencies) {
        snapshot.m_nodes.insert(userGuid);
        snapshot.m_edgeCount += dependencies.size();
        snapshot.m_nodes.insert(dependencies.begin(), dependencies.end());
    }
}

void AssetDependencyGraph::PublishAssetMutation(const std::function<void(AssetDependencySnapshot &)> &mutation)
{
    std::lock_guard<std::mutex> lock(m_assetWriteMutex);
    const auto current = GetAssetSnapshot();
    auto next = std::make_shared<AssetDependencySnapshot>(*current);
    mutation(*next);
    next->m_generation = current->m_generation + 1;
    RebuildStatistics(*next);
    std::atomic_store_explicit(&m_assetSnapshot, std::shared_ptr<const AssetDependencySnapshot>(std::move(next)),
                               std::memory_order_release);
}

void AssetDependencyGraph::AddAssetDependency(const std::string &assetGuid, const std::string &dependencyGuid)
{
    PublishAssetMutation([&](AssetDependencySnapshot &snapshot) { AddEdge(snapshot, assetGuid, dependencyGuid); });
}

void AssetDependencyGraph::RemoveAssetDependency(const std::string &assetGuid, const std::string &dependencyGuid)
{
    PublishAssetMutation([&](AssetDependencySnapshot &snapshot) { RemoveEdge(snapshot, assetGuid, dependencyGuid); });
}

void AssetDependencyGraph::ClearAssetDependenciesOf(const std::string &assetGuid)
{
    if (assetGuid.empty())
        throw std::invalid_argument("Asset GUID cannot be empty");
    PublishAssetMutation([&](AssetDependencySnapshot &snapshot) { ClearEdgesOf(snapshot, assetGuid); });
}

void AssetDependencyGraph::SetAssetDependencies(const std::string &assetGuid,
                                                const std::unordered_set<std::string> &dependencyGuids)
{
    if (assetGuid.empty())
        throw std::invalid_argument("Asset GUID cannot be empty");
    PublishAssetMutation([&](AssetDependencySnapshot &snapshot) {
        ClearEdgesOf(snapshot, assetGuid);
        for (const auto &dependencyGuid : dependencyGuids)
            AddEdge(snapshot, assetGuid, dependencyGuid);
    });
}

std::shared_ptr<const AssetDependencySnapshot> AssetDependencyGraph::BuildAssetSnapshot(
    const std::unordered_map<std::string, std::vector<std::string>> &dependenciesByAsset, uint64_t generation)
{
    if (generation == 0)
        throw std::invalid_argument("Asset dependency snapshot generation must be positive");
    auto snapshot = std::make_shared<AssetDependencySnapshot>();
    snapshot->m_generation = generation;
    snapshot->m_dependencies.reserve(dependenciesByAsset.size());
    for (const auto &[assetGuid, dependencies] : dependenciesByAsset) {
        if (assetGuid.empty())
            throw std::invalid_argument("Asset dependency snapshot contains an empty asset GUID");
        for (const auto &dependencyGuid : dependencies)
            AddEdge(*snapshot, assetGuid, dependencyGuid);
    }
    RebuildStatistics(*snapshot);
    return snapshot;
}

void AssetDependencyGraph::InstallAssetSnapshot(std::shared_ptr<const AssetDependencySnapshot> snapshot)
{
    if (!snapshot)
        throw std::invalid_argument("Asset dependency snapshot cannot be null");
    std::lock_guard<std::mutex> lock(m_assetWriteMutex);
    const auto current = GetAssetSnapshot();
    if (snapshot->GetGeneration() != current->GetGeneration() + 1)
        throw std::logic_error("Asset dependency snapshot generation is stale");
    std::atomic_store_explicit(&m_assetSnapshot, std::move(snapshot), std::memory_order_release);
}

void AssetDependencyGraph::AddRuntimeDependency(const std::string &objectGuid, const std::string &assetGuid)
{
    RequireEdge(objectGuid, assetGuid);
    std::lock_guard<std::mutex> lock(m_runtimeMutex);
    if (m_runtimeDependencies[objectGuid].insert(assetGuid).second)
        m_runtimeDependents[assetGuid].insert(objectGuid);
}

void AssetDependencyGraph::RemoveRuntimeDependency(const std::string &objectGuid, const std::string &assetGuid)
{
    std::lock_guard<std::mutex> lock(m_runtimeMutex);
    const auto forward = m_runtimeDependencies.find(objectGuid);
    if (forward != m_runtimeDependencies.end()) {
        forward->second.erase(assetGuid);
        if (forward->second.empty())
            m_runtimeDependencies.erase(forward);
    }
    const auto reverse = m_runtimeDependents.find(assetGuid);
    if (reverse != m_runtimeDependents.end()) {
        reverse->second.erase(objectGuid);
        if (reverse->second.empty())
            m_runtimeDependents.erase(reverse);
    }
}

void AssetDependencyGraph::ClearRuntimeDependenciesOf(const std::string &objectGuid)
{
    std::lock_guard<std::mutex> lock(m_runtimeMutex);
    const auto forward = m_runtimeDependencies.find(objectGuid);
    if (forward == m_runtimeDependencies.end())
        return;
    for (const auto &assetGuid : forward->second) {
        const auto reverse = m_runtimeDependents.find(assetGuid);
        if (reverse == m_runtimeDependents.end())
            continue;
        reverse->second.erase(objectGuid);
        if (reverse->second.empty())
            m_runtimeDependents.erase(reverse);
    }
    m_runtimeDependencies.erase(forward);
}

void AssetDependencyGraph::RemoveAsset(const std::string &guid)
{
    if (guid.empty())
        throw std::invalid_argument("Asset GUID cannot be empty");
    PublishAssetMutation([&](AssetDependencySnapshot &snapshot) { ClearEdgesOf(snapshot, guid); });

    std::lock_guard<std::mutex> lock(m_runtimeMutex);
    const auto outgoing = m_runtimeDependencies.find(guid);
    if (outgoing != m_runtimeDependencies.end()) {
        for (const auto &assetGuid : outgoing->second) {
            const auto reverse = m_runtimeDependents.find(assetGuid);
            if (reverse == m_runtimeDependents.end())
                continue;
            reverse->second.erase(guid);
            if (reverse->second.empty())
                m_runtimeDependents.erase(reverse);
        }
        m_runtimeDependencies.erase(outgoing);
    }
}

std::unordered_set<std::string> AssetDependencyGraph::GetDependencies(const std::string &guid) const
{
    std::unordered_set<std::string> result;
    const auto snapshot = GetAssetSnapshot();
    const auto asset = snapshot->m_dependencies.find(guid);
    if (asset != snapshot->m_dependencies.end())
        result = asset->second;
    std::lock_guard<std::mutex> lock(m_runtimeMutex);
    const auto runtime = m_runtimeDependencies.find(guid);
    if (runtime != m_runtimeDependencies.end())
        result.insert(runtime->second.begin(), runtime->second.end());
    return result;
}

std::unordered_map<std::string, std::unordered_set<std::string>>
AssetDependencyGraph::GetDependenciesBatch(const std::vector<std::string> &guids) const
{
    std::unordered_map<std::string, std::unordered_set<std::string>> result;
    result.reserve(guids.size());
    const auto snapshot = GetAssetSnapshot();
    std::lock_guard<std::mutex> lock(m_runtimeMutex);
    for (const auto &guid : guids) {
        auto &dependencies = result[guid];
        const auto asset = snapshot->m_dependencies.find(guid);
        if (asset != snapshot->m_dependencies.end())
            dependencies.insert(asset->second.begin(), asset->second.end());
        const auto runtime = m_runtimeDependencies.find(guid);
        if (runtime != m_runtimeDependencies.end())
            dependencies.insert(runtime->second.begin(), runtime->second.end());
        if (dependencies.empty())
            result.erase(guid);
    }
    return result;
}

std::unordered_set<std::string> AssetDependencyGraph::GetDependents(const std::string &guid) const
{
    std::unordered_set<std::string> result;
    const auto snapshot = GetAssetSnapshot();
    const auto asset = snapshot->m_dependents.find(guid);
    if (asset != snapshot->m_dependents.end())
        result = asset->second;
    std::lock_guard<std::mutex> lock(m_runtimeMutex);
    const auto runtime = m_runtimeDependents.find(guid);
    if (runtime != m_runtimeDependents.end())
        result.insert(runtime->second.begin(), runtime->second.end());
    return result;
}

bool AssetDependencyGraph::HasDependency(const std::string &userGuid, const std::string &dependencyGuid) const
{
    const auto snapshot = GetAssetSnapshot();
    const auto asset = snapshot->m_dependencies.find(userGuid);
    if (asset != snapshot->m_dependencies.end() && asset->second.count(dependencyGuid) != 0)
        return true;
    std::lock_guard<std::mutex> lock(m_runtimeMutex);
    const auto runtime = m_runtimeDependencies.find(userGuid);
    return runtime != m_runtimeDependencies.end() && runtime->second.count(dependencyGuid) != 0;
}

void AssetDependencyGraph::RegisterCallback(ResourceType type, AssetEventCallback callback)
{
    if (!callback)
        throw std::invalid_argument("Asset dependency callback cannot be empty");
    std::lock_guard<std::mutex> lock(m_runtimeMutex);
    m_callbacks[type].push_back(std::move(callback));
}

void AssetDependencyGraph::NotifyEvent(const std::string &guid, ResourceType type, AssetEvent event)
{
    std::unordered_set<std::string> dependents;
    const auto snapshot = GetAssetSnapshot();
    const auto asset = snapshot->m_dependents.find(guid);
    if (asset != snapshot->m_dependents.end())
        dependents = asset->second;

    std::vector<AssetEventCallback> callbacks;
    {
        std::lock_guard<std::mutex> lock(m_runtimeMutex);
        const auto runtime = m_runtimeDependents.find(guid);
        if (runtime != m_runtimeDependents.end())
            dependents.insert(runtime->second.begin(), runtime->second.end());
        const auto registered = m_callbacks.find(type);
        if (registered != m_callbacks.end())
            callbacks = registered->second;
    }

    if (dependents.empty() || callbacks.empty())
        return;
    for (const auto &dependentGuid : dependents)
        for (const auto &callback : callbacks)
            callback(dependentGuid, guid, event);
}

size_t AssetDependencyGraph::GetEdgeCount() const
{
    size_t count = GetAssetSnapshot()->GetEdgeCount();
    std::lock_guard<std::mutex> lock(m_runtimeMutex);
    for (const auto &[objectGuid, dependencies] : m_runtimeDependencies) {
        (void)objectGuid;
        count += dependencies.size();
    }
    return count;
}

size_t AssetDependencyGraph::GetNodeCount() const
{
    auto nodes = GetAssetSnapshot()->m_nodes;
    std::lock_guard<std::mutex> lock(m_runtimeMutex);
    for (const auto &[objectGuid, dependencies] : m_runtimeDependencies) {
        nodes.insert(objectGuid);
        nodes.insert(dependencies.begin(), dependencies.end());
    }
    return nodes.size();
}

void AssetDependencyGraph::Clear()
{
    {
        std::lock_guard<std::mutex> lock(m_assetWriteMutex);
        std::atomic_store_explicit(&m_assetSnapshot, std::make_shared<const AssetDependencySnapshot>(),
                                   std::memory_order_release);
    }
    std::lock_guard<std::mutex> lock(m_runtimeMutex);
    m_runtimeDependencies.clear();
    m_runtimeDependents.clear();
    m_callbacks.clear();
}

} // namespace infernux
