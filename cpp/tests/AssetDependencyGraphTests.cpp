#include <function/resources/AssetDependencyGraph.h>

#include <chrono>
#include <iostream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

namespace
{
void Require(bool condition, const char *message)
{
    if (!condition)
        throw std::runtime_error(message);
}
} // namespace

int main()
{
    try {
        using namespace infernux;
        auto &graph = AssetDependencyGraph::Instance();
        graph.Clear();

        constexpr size_t userCount = 10'000;
        std::unordered_map<std::string, std::vector<std::string>> dependencies;
        dependencies.reserve(userCount);
        for (size_t index = 0; index < userCount; ++index) {
            const std::string suffix = std::to_string(index);
            dependencies.emplace("asset-" + suffix,
                                 std::vector<std::string>{"dependency-a-" + suffix, "dependency-b-" + suffix});
        }

        const auto initial = graph.GetAssetSnapshot();
        const auto started = std::chrono::steady_clock::now();
        const auto built = AssetDependencyGraph::BuildAssetSnapshot(dependencies, 1);
        const double buildMilliseconds =
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
        Require(buildMilliseconds < 2'000.0, "10k dependency snapshot build exceeded 2 seconds");
        Require(built->GetEdgeCount() == userCount * 2, "dependency snapshot edge count mismatch");
        Require(built->GetNodeCount() == userCount * 3, "dependency snapshot node count mismatch");

        graph.AddRuntimeDependency("runtime-object", "dependency-a-0");
        graph.InstallAssetSnapshot(built);
        Require(graph.GetAssetGeneration() == 1, "asset dependency generation did not publish");
        Require(graph.HasDependency("asset-0", "dependency-a-0"), "asset edge was not published");
        Require(graph.HasDependency("runtime-object", "dependency-a-0"), "runtime overlay was replaced");
        const auto dependents = graph.GetDependents("dependency-a-0");
        Require(dependents.count("asset-0") == 1 && dependents.count("runtime-object") == 1,
                "asset/runtime dependent union is incomplete");
        Require(initial->GetGeneration() == 0 && initial->GetEdgeCount() == 0,
                "retained dependency snapshot changed after publication");

        graph.RemoveAsset("dependency-a-0");
        Require(graph.HasDependency("asset-0", "dependency-a-0"),
                "asset deletion discarded a serialized missing reference");
        Require(graph.HasDependency("runtime-object", "dependency-a-0"),
                "asset deletion discarded a runtime missing reference");

        bool rejectedStale = false;
        try {
            graph.InstallAssetSnapshot(built);
        } catch (const std::logic_error &) {
            rejectedStale = true;
        }
        Require(rejectedStale, "stale dependency generation was accepted");

        auto replacement = dependencies;
        replacement.erase("asset-0");
        graph.InstallAssetSnapshot(AssetDependencyGraph::BuildAssetSnapshot(replacement, 3));
        Require(!graph.HasDependency("asset-0", "dependency-a-0"), "replacement retained a removed asset edge");
        Require(graph.HasDependency("runtime-object", "dependency-a-0"), "replacement removed runtime usage");
        Require(built->GetEdgeCount() == userCount * 2, "retained generation changed after replacement");

        graph.ClearRuntimeDependenciesOf("runtime-object");
        graph.Clear();
        std::cout << "AssetDependencyGraph tests passed; build_ms=" << buildMilliseconds << '\n';
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "AssetDependencyGraph tests failed: " << error.what() << '\n';
        return 1;
    }
}
