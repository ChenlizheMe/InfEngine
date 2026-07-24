#include <core/threading/JobSystem.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetDatabase/AssetIndex.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxFileLoader/InxDefaultLoader.hpp>
#include <platform/filesystem/InxPath.h>

#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

namespace
{
void Require(bool condition, const char *message)
{
    if (!condition)
        throw std::runtime_error(message);
}

void WriteText(const std::filesystem::path &path, const std::string &text)
{
    std::filesystem::create_directories(path.parent_path());
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    output.write(text.data(), static_cast<std::streamsize>(text.size()));
    Require(output.good(), "failed to write asset database refresh fixture");
}

void TestProjectRelativeDependencyOnInitialRefresh()
{
    const auto root = std::filesystem::temp_directory_path() / "infernux-asset-refresh-relative-dependency";
    std::filesystem::remove_all(root);
    const auto rendering = root / "Assets" / "Rendering";
    const auto bloom = rendering / "Bloom.effect";
    const auto group = rendering / "Default Post Processing.effectgroup";

    WriteText(bloom, R"({
  "$schema": "infernux.render_effect",
  "dependencies": [],
  "feature_type": "infernux.post.bloom",
  "parameters": {}
})");
    WriteText(group, R"({
  "$schema": "infernux.render_effect_group",
  "entries": [
    {
      "asset": {
        "guid": "",
        "path_hint": "Assets/Rendering/Bloom.effect"
      },
      "enabled": true,
      "entry_id": "bloom",
      "overrides": {}
    }
  ]
})");

    infernux::JobSystem::Initialize(2);
    try {
        {
            auto database = std::make_unique<infernux::AssetDatabase>();
            database->Initialize(infernux::FromFsPath(root));
            auto &registry = infernux::AssetRegistry::Instance();
            registry.Initialize(std::move(database));
            registry.RegisterLoader(
                infernux::ResourceType::RenderEffect,
                std::make_unique<infernux::InxDefaultTextLoader>(infernux::ResourceType::RenderEffect));
            registry.PopulateAssetDatabaseLoaders();
            auto *assetDatabase = registry.GetAssetDatabase();
            assetDatabase->Refresh();

            const std::string bloomGuid = assetDatabase->GetGuidFromPath(infernux::FromFsPath(bloom));
            const std::string groupGuid = assetDatabase->GetGuidFromPath(infernux::FromFsPath(group));
            Require(!bloomGuid.empty(), "initial refresh did not register the referenced effect");
            Require(!groupGuid.empty(), "initial refresh did not register the effect group");

            infernux::AssetIndex index;
            const auto indexPath = root / "Library" / "AssetIndex.json";
            Require(
                index.Load(infernux::FromFsPath(indexPath), infernux::FilesystemPathKey(infernux::FromFsPath(root))),
                "initial refresh did not persist the asset index");
            const auto *entry = index.Find(infernux::FilesystemPathKey(infernux::FromFsPath(group)));
            Require(entry != nullptr, "effect group is absent from the initial asset index");
            Require(entry->importSucceeded, "effect group project-relative dependency failed initial import");
            Require(entry->dependencies.size() == 1 && entry->dependencies.front() == bloomGuid,
                    "effect group dependency resolved to the wrong GUID");
            registry.Shutdown();
        }
        infernux::JobSystem::Shutdown();
    } catch (...) {
        if (infernux::AssetRegistry::Instance().IsInitialized())
            infernux::AssetRegistry::Instance().Shutdown();
        infernux::JobSystem::Shutdown();
        std::filesystem::remove_all(root);
        throw;
    }
    std::filesystem::remove_all(root);
}
} // namespace

int main()
{
    try {
        TestProjectRelativeDependencyOnInitialRefresh();
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "Asset database refresh test failed: " << error.what() << '\n';
        return 1;
    }
}
