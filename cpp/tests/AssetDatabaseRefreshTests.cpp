#include <core/threading/JobSystem.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetDatabase/AssetIndex.h>
#include <function/resources/AssetImporter/ImporterRegistry.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxFileLoader/InxDefaultLoader.hpp>
#include <function/resources/InxFileLoader/InxPythonScriptLoader.hpp>
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

class MixedCaseImporter final : public infernux::AssetImporter
{
  public:
    explicit MixedCaseImporter(std::string extension) : m_extension(std::move(extension))
    {
    }

    [[nodiscard]] infernux::ResourceType GetResourceType() const override
    {
        return infernux::ResourceType::Mesh;
    }

    [[nodiscard]] std::vector<std::string> GetSupportedExtensions() const override
    {
        return {m_extension};
    }

    [[nodiscard]] infernux::ImportArtifact Import(const infernux::ImportRequest &request) const override
    {
        return infernux::ImportArtifact(request.metadata);
    }

  private:
    std::string m_extension;
};

void TestImporterExtensionsAreCaseInsensitive()
{
    infernux::ImporterRegistry registry;
    registry.Register(std::make_unique<MixedCaseImporter>(".FbX"));
    auto *const importer = registry.GetImporterForExtension(".fbx");
    Require(importer != nullptr, "lowercase extension did not resolve a mixed-case importer registration");
    Require(registry.GetImporterForExtension(".FBX") == importer,
            "uppercase source extension did not resolve the registered importer");

    bool duplicateRejected = false;
    try {
        registry.Register(std::make_unique<MixedCaseImporter>(".FBX"));
    } catch (const std::logic_error &) {
        duplicateRejected = true;
    }
    Require(duplicateRejected, "case-only duplicate importer registration was accepted");
}

void TestPathOnlyDependencyIsRejectedOnInitialRefresh()
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
            Require(!entry->importSucceeded, "path-only dependency unexpectedly passed initial import");
            Require(entry->importError.find("must provide a GUID") != std::string::npos,
                    "path-only dependency rejection did not explain the GUID-only contract");
            Require(entry->dependencies.empty(), "rejected path-only dependency was published to the graph");
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

void TestScriptReimportRefreshesContentHashAndPreservesGuid()
{
    const auto root = std::filesystem::temp_directory_path() / "infernux-script-reimport-content-hash";
    std::filesystem::remove_all(root);
    const auto script = root / "Assets" / "Scripts" / "Gameplay.py";
    WriteText(script, "VALUE = 1\n");

    infernux::JobSystem::Initialize(2);
    try {
        auto database = std::make_unique<infernux::AssetDatabase>();
        database->Initialize(infernux::FromFsPath(root));
        auto &registry = infernux::AssetRegistry::Instance();
        registry.Initialize(std::move(database));
        registry.RegisterLoader(infernux::ResourceType::Script, std::make_unique<infernux::InxPythonScriptLoader>());
        registry.PopulateAssetDatabaseLoaders();
        auto *assetDatabase = registry.GetAssetDatabase();
        assetDatabase->Refresh();

        const std::string scriptPath = infernux::FromFsPath(script);
        const std::string originalGuid = assetDatabase->GetGuidFromPath(scriptPath);
        const auto originalMetadata = assetDatabase->GetMetaByGuid(originalGuid);
        Require(!originalGuid.empty() && originalMetadata != nullptr,
                "initial script refresh did not publish metadata");
        const std::string originalHash = originalMetadata->GetDataAs<std::string>("content_hash");

        // Preserve byte count so the regression cannot be hidden by a size-only check.
        WriteText(script, "VALUE = 2\n");
        const auto result = assetDatabase->ReimportAsset(scriptPath);
        Require(result.succeeded, "script reimport failed");
        Require(result.guid == originalGuid, "script reimport changed its GUID");
        const auto rebuiltMetadata = assetDatabase->GetMetaByGuid(originalGuid);
        Require(rebuiltMetadata != nullptr, "script reimport removed its metadata");
        Require(rebuiltMetadata->GetDataAs<std::string>("content_hash") != originalHash,
                "script reimport retained the stale source hash");
        Require(rebuiltMetadata->GetDataAs<size_t>("file_size") == std::string("VALUE = 2\n").size(),
                "script reimport retained the stale source size");

        assetDatabase->FlushDerivedIndex();
        infernux::AssetIndex index;
        Require(index.Load(infernux::FromFsPath(root / "Library" / "AssetIndex.json"),
                           infernux::FilesystemPathKey(infernux::FromFsPath(root))),
                "script reimport did not persist the derived index");
        const auto *entry = index.Find(infernux::FilesystemPathKey(scriptPath));
        Require(entry != nullptr && entry->guid == originalGuid, "script reimport index lost the original identity");
        Require(entry->contentHash == rebuiltMetadata->GetDataAs<std::string>("content_hash"),
                "script reimport index did not publish the rebuilt source hash");

        registry.Shutdown();
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

void TestLegacySidecarWithoutContentHashIsRebuilt()
{
    const auto root = std::filesystem::temp_directory_path() / "infernux-asset-refresh-legacy-sidecar";
    std::filesystem::remove_all(root);
    const auto script = root / "Assets" / "Scripts" / "Legacy.py";
    const auto sidecar = root / "Assets" / "Scripts" / "Legacy.py.meta";
    WriteText(script, "class Legacy:\n    pass\n");
    WriteText(sidecar, R"({
  "metadata": {
    "file_extension": {"type": "string", "value": ".py"},
    "file_path": {"type": "string", "value": "Assets/Scripts/Legacy.py"},
    "file_type": {"type": "string", "value": "script"},
    "guid": {"type": "string", "value": "1234567890abcdef1234567890abcdef"},
    "language": {"type": "string", "value": "python"},
    "resource_type": {"type": "enum infernux::ResourceType", "value": "Script"}
  }
})");

    infernux::JobSystem::Initialize(2);
    try {
        {
            auto database = std::make_unique<infernux::AssetDatabase>();
            database->Initialize(infernux::FromFsPath(root));
            auto &registry = infernux::AssetRegistry::Instance();
            registry.Initialize(std::move(database));
            registry.RegisterLoader(infernux::ResourceType::Script,
                                    std::make_unique<infernux::InxPythonScriptLoader>());
            registry.PopulateAssetDatabaseLoaders();
            auto *assetDatabase = registry.GetAssetDatabase();
            assetDatabase->Refresh();

            infernux::AssetIndex index;
            const auto indexPath = root / "Library" / "AssetIndex.json";
            Require(
                index.Load(infernux::FromFsPath(indexPath), infernux::FilesystemPathKey(infernux::FromFsPath(root))),
                "legacy sidecar refresh did not persist the asset index");
            const auto *entry = index.Find(infernux::FilesystemPathKey(infernux::FromFsPath(script)));
            Require(entry != nullptr, "legacy script is absent from the rebuilt asset index");
            Require(entry->guid == "1234567890abcdef1234567890abcdef",
                    "legacy sidecar rebuild did not preserve its GUID");
            Require(entry->importSucceeded, "legacy script import failed during metadata rebuild");
            Require(!entry->contentHash.empty(), "legacy script retained an empty content hash");
            Require(entry->metadata.HasKey("content_hash"), "rebuilt metadata did not publish a content hash");
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

void TestProjectPackagesScanRootSharesTheGuidCatalog()
{
    const auto root = std::filesystem::temp_directory_path() / "infernux-asset-refresh-packages-root";
    std::filesystem::remove_all(root);
    const auto assetScript = root / "Assets" / "Scripts" / "Gameplay.py";
    const auto packageScript = root / "Packages" / "vendor" / "tool" / "Runtime" / "Lifecycle.py";
    WriteText(assetScript, "class Gameplay:\n    pass\n");
    WriteText(packageScript, "class Lifecycle:\n    pass\n");

    infernux::JobSystem::Initialize(2);
    try {
        auto database = std::make_unique<infernux::AssetDatabase>();
        database->Initialize(infernux::FromFsPath(root));
        auto &registry = infernux::AssetRegistry::Instance();
        registry.Initialize(std::move(database));
        registry.RegisterLoader(infernux::ResourceType::Script, std::make_unique<infernux::InxPythonScriptLoader>());
        registry.PopulateAssetDatabaseLoaders();
        auto *assetDatabase = registry.GetAssetDatabase();
        assetDatabase->AddScanRoot(infernux::FromFsPath(root / "Packages"));
        assetDatabase->Refresh();

        const std::string assetGuid = assetDatabase->GetGuidFromPath(infernux::FromFsPath(assetScript));
        const std::string packageGuid = assetDatabase->GetGuidFromPath(infernux::FromFsPath(packageScript));
        Require(!assetGuid.empty(), "Assets script was not registered in the shared catalog");
        Require(!packageGuid.empty(), "Packages script was not registered in the shared catalog");
        Require(assetGuid != packageGuid, "Assets and Packages scripts received the same GUID");
        Require(assetDatabase->GetPathFromGuid(packageGuid) == infernux::FromFsPath(packageScript),
                "Packages GUID did not resolve to its current path");
        Require(std::filesystem::is_regular_file(packageScript.string() + ".meta"),
                "Packages scan root did not persist stable GUID metadata");

        registry.Shutdown();
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

void TestStartupCatalogSurvivesLiveIndexInvalidation()
{
    const auto root = std::filesystem::temp_directory_path() / "infernux-asset-startup-catalog";
    std::filesystem::remove_all(root);
    const auto script = root / "Assets" / "Scripts" / "Startup.py";
    WriteText(script, "class Startup:\n    pass\n");

    infernux::JobSystem::Initialize(2);
    try {
        std::string scriptGuid;
        {
            auto database = std::make_unique<infernux::AssetDatabase>();
            database->Initialize(infernux::FromFsPath(root));
            auto &registry = infernux::AssetRegistry::Instance();
            registry.Initialize(std::move(database));
            registry.RegisterLoader(infernux::ResourceType::Script,
                                    std::make_unique<infernux::InxPythonScriptLoader>());
            registry.PopulateAssetDatabaseLoaders();
            auto *assetDatabase = registry.GetAssetDatabase();
            assetDatabase->Refresh();
            scriptGuid = assetDatabase->GetGuidFromPath(infernux::FromFsPath(script));
            Require(!scriptGuid.empty(), "initial refresh did not register the startup fixture");
            registry.Shutdown();
        }

        const auto liveIndex = root / "Library" / "AssetIndex.json";
        const auto startupIndex = root / "Library" / "AssetIndex.startup-cache.json";
        Require(std::filesystem::is_regular_file(liveIndex), "initial refresh did not publish the live index");
        Require(std::filesystem::is_regular_file(startupIndex), "initial refresh did not publish the startup index");
        std::filesystem::remove(startupIndex);
        {
            infernux::AssetDatabase legacyRestore;
            legacyRestore.Initialize(infernux::FromFsPath(root));
            Require(legacyRestore.RestoreCachedCatalog(), "legacy live index did not restore");
            Require(std::filesystem::is_regular_file(startupIndex),
                    "legacy live index did not seed the startup fallback");
        }
        std::filesystem::remove(liveIndex);

        {
            infernux::AssetDatabase restored;
            restored.Initialize(infernux::FromFsPath(root));
            Require(restored.RestoreCachedCatalog(), "startup catalog did not restore after live index invalidation");
            Require(restored.GetGuidFromPath(infernux::FromFsPath(script)) == scriptGuid,
                    "startup catalog did not preserve the committed asset identity");
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

void TestRuntimeAssetCatalogInstallsStableIdentityWithoutSidecar()
{
    const auto root = std::filesystem::temp_directory_path() / "infernux-runtime-asset-catalog";
    std::filesystem::remove_all(root);
    const auto material = root / "Assets" / "Materials" / "Runtime.mat";
    const auto catalog = root / "Library" / "RuntimeAssetRecords.json";
    WriteText(material, "{}\n");
    WriteText(catalog, R"({
  "$schema": "infernux.runtime_asset_records",
  "entries": [
    {
      "guid": "runtime-material-guid",
      "runtime_path": "Assets/Materials/Runtime.mat",
      "metadata": {
        "metadata": {
          "content_hash": {"type": "string", "value": "runtime-hash"},
          "file_path": {"type": "string", "value": "Assets/Materials/Runtime.mat"},
          "guid": {"type": "string", "value": "runtime-material-guid"},
          "resource_type": {"type": "enum infernux::ResourceType", "value": "Material"}
        }
      }
    }
  ]
})");

    infernux::AssetDatabase database;
    database.Initialize(infernux::FromFsPath(root));
    database.InstallRuntimeAssetCatalog(infernux::FromFsPath(catalog));

    const std::string materialPath = infernux::FromFsPath(material);
    Require(database.GetGuidFromPath(materialPath) == "runtime-material-guid",
            "runtime asset catalog did not install the stable path mapping");
    Require(database.GetPathFromGuid("runtime-material-guid") == materialPath,
            "runtime asset catalog did not install the stable GUID mapping");
    const auto metadata = database.GetMetaByGuid("runtime-material-guid");
    Require(metadata != nullptr, "runtime asset catalog did not install metadata");
    Require(metadata->GetResourceType() == infernux::ResourceType::Material,
            "runtime asset catalog changed the resource type");
    Require(metadata->HasKey("read_only") && metadata->GetDataAs<bool>("read_only"),
            "runtime asset catalog identity is not read-only");
    std::filesystem::remove_all(root);
}

void TestRuntimeAssetCatalogResolvesBuiltInArchiveResources()
{
    const auto root = std::filesystem::temp_directory_path() / "infernux-runtime-builtin-catalog";
    std::filesystem::remove_all(root);
    const auto resources = root / "runtime" / "Infernux" / "resources";
    const auto shader = resources / "shaders" / "standard.vert";
    const auto catalog = root / "content" / "Library" / "RuntimeAssetRecords.json";
    WriteText(shader, "#version 450\nvoid main() {}\n");
    WriteText(catalog, R"({
  "$schema": "infernux.runtime_asset_records",
  "entries": [
    {
      "guid": "runtime-standard-vertex-guid",
      "runtime_path": "Library/Resources/shaders/standard.vert",
      "runtime_artifacts": [
        {
          "package": "Runtime.inxrt",
          "runtime_path": "Infernux/resources/shaders/standard.vert"
        }
      ],
      "metadata": {
        "metadata": {
          "content_hash": {"type": "string", "value": "runtime-shader-hash"},
          "file_path": {"type": "string", "value": "Library/Resources/shaders/standard.vert"},
          "guid": {"type": "string", "value": "runtime-standard-vertex-guid"},
          "resource_type": {"type": "enum infernux::ResourceType", "value": "Shader"},
          "shader_id": {"type": "string", "value": "Standard"},
          "type": {"type": "string", "value": "vertex"}
        }
      }
    }
  ]
})");

    infernux::AssetDatabase database;
    database.Initialize(infernux::FromFsPath(root / "content"));
    database.AddReadOnlyScanRoot(infernux::FromFsPath(resources));
    database.InstallRuntimeAssetCatalog(infernux::FromFsPath(catalog));

    const std::string shaderPath = infernux::FromFsPath(shader);
    Require(database.GetPathFromGuid("runtime-standard-vertex-guid") == shaderPath,
            "runtime built-in identity did not resolve to the extracted resource path");
    Require(database.GetGuidFromPath(shaderPath) == "runtime-standard-vertex-guid",
            "runtime built-in extracted path did not preserve its cooked GUID");
    Require(database.FindShaderPathById("Standard", "vertex") == shaderPath,
            "runtime shader lookup returned the non-existent authoring cache path");
    std::filesystem::remove_all(root);
}

void TestRuntimeAssetCatalogResolvesPrimaryContentArtifact()
{
    const auto root = std::filesystem::temp_directory_path() / "infernux-runtime-content-catalog";
    std::filesystem::remove_all(root);
    const auto cookedMaterial = root / "Library" / "Artifacts" / "Document" / "runtime-material-guid.mat";
    const auto catalog = root / "Library" / "RuntimeAssetRecords.json";
    WriteText(cookedMaterial, "{}\n");
    WriteText(catalog, R"({
  "$schema": "infernux.runtime_asset_records",
  "entries": [
    {
      "guid": "runtime-material-guid",
      "runtime_path": "Assets/Materials/Runtime.mat",
      "primary_runtime_artifact_id": "content-material-artifact",
      "runtime_artifacts": [
        {
          "runtime_artifact_id": "content-material-artifact",
          "package": "Content.inxpkg",
          "runtime_path": "Library/Artifacts/Document/runtime-material-guid.mat"
        }
      ],
      "metadata": {
        "metadata": {
          "content_hash": {"type": "string", "value": "runtime-hash"},
          "file_path": {"type": "string", "value": "Assets/Materials/Runtime.mat"},
          "guid": {"type": "string", "value": "runtime-material-guid"},
          "resource_type": {"type": "enum infernux::ResourceType", "value": "Material"}
        }
      }
    }
  ]
})");

    infernux::AssetDatabase database;
    database.Initialize(infernux::FromFsPath(root));
    database.InstallRuntimeAssetCatalog(infernux::FromFsPath(catalog));

    const std::string cookedPath = infernux::FromFsPath(cookedMaterial);
    Require(database.GetPathFromGuid("runtime-material-guid") == cookedPath,
            "runtime identity did not resolve to its primary Content artifact");
    Require(database.GetGuidFromPath(cookedPath) == "runtime-material-guid",
            "primary Content artifact did not preserve its cooked GUID");
    Require(database.GetGuidFromPath(infernux::FromFsPath(root / "Assets" / "Materials" / "Runtime.mat")).empty(),
            "runtime identity retained a missing authoring path");
    std::filesystem::remove_all(root);
}
} // namespace

int main()
{
    try {
        TestImporterExtensionsAreCaseInsensitive();
        TestPathOnlyDependencyIsRejectedOnInitialRefresh();
        TestScriptReimportRefreshesContentHashAndPreservesGuid();
        TestLegacySidecarWithoutContentHashIsRebuilt();
        TestProjectPackagesScanRootSharesTheGuidCatalog();
        TestStartupCatalogSurvivesLiveIndexInvalidation();
        TestRuntimeAssetCatalogInstallsStableIdentityWithoutSidecar();
        TestRuntimeAssetCatalogResolvesBuiltInArchiveResources();
        TestRuntimeAssetCatalogResolvesPrimaryContentArtifact();
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "Asset database refresh test failed: " << error.what() << '\n';
        return 1;
    }
}
