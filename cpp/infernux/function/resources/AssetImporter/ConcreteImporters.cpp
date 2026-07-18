#include "ConcreteImporters.h"

#include <core/log/InxLog.h>
#include <function/resources/InxMesh/MeshArtifact.h>
#include <function/resources/InxMesh/MeshLoader.h>
#include <function/resources/InxSkinnedMesh/SkinnedMeshArtifact.h>
#include <function/resources/InxTexture/TextureArtifact.h>
#include <function/resources/InxTexture/TextureDecoder.h>
#include <platform/filesystem/InxPath.h>

#include <algorithm>
#include <fstream>
#include <limits>
#include <nlohmann/json.hpp>
#include <unordered_set>
#include <vector>

namespace infernux
{

ImportArtifact TextureImporter::Import(const ImportRequest &request) const
{
    ImportArtifact artifact(request.metadata);
    EnsureDefaultSettings(artifact.metadata);
    if (!artifact.metadata.HasKey("content_hash"))
        throw std::logic_error("TextureImporter metadata has no source content hash");
    const auto cpuData = TextureDecoder::Decode(request.sourcePath, artifact.metadata);
    if (!cpuData || !cpuData->IsValid())
        throw std::runtime_error("TextureImporter failed to build the runtime texture artifact");
    artifact.metadata.AddMetadata("artifact_width", static_cast<int>(cpuData->mipLevels.front().width));
    artifact.metadata.AddMetadata("artifact_height", static_cast<int>(cpuData->mipLevels.front().height));
    artifact.metadata.AddMetadata("artifact_mip_count", static_cast<int>(cpuData->mipLevels.size()));
    artifact.metadata.AddMetadata(
        "artifact_pixel_storage",
        std::string(cpuData->storage == TexturePixelStorage::Rgba8 ? "rgba8" : "rgba32_float"));
    artifact.runtimeCpuArtifacts.push_back(ImportArtifact::RuntimeCpuArtifact{
        ImportArtifact::RuntimeArtifactKind::Primary, ResourceType::Texture, TextureArtifact::FormatVersion,
        TextureArtifact::Serialize(*cpuData, artifact.metadata.GetDataAs<std::string>("content_hash"))});
    return artifact;
}

ImportArtifact MaterialImporter::Import(const ImportRequest &request) const
{
    ImportArtifact artifact(request.metadata);
    artifact.dependencies = ScanDependencies(request);
    artifact.dependenciesAuthoritative = true;
    return artifact;
}

std::vector<std::string> MaterialImporter::ScanDependencies(const ImportRequest &request) const
{
    if (!request.resolveAssetGuid)
        throw std::logic_error("MaterialImporter request has no dependency resolver");
    std::unordered_set<std::string> deps;

    nlohmann::json root;
    try {
        std::ifstream file(ToFsPath(request.sourcePath));
        if (!file.is_open())
            throw std::runtime_error("failed to open material document");
        file >> root;
    } catch (const std::exception &e) {
        throw std::runtime_error("MaterialImporter failed to parse '" + request.sourcePath + "': " + e.what());
    } catch (...) {
        throw std::runtime_error("MaterialImporter failed to parse '" + request.sourcePath + "'");
    }

    // Shader dependencies. Material v4 stores stable GUID references while v3
    // stored a path or shader ID string.
    auto shadersIt = root.find("shaders");
    if (shadersIt != root.end() && shadersIt->is_object()) {
        for (const auto &key : {"vertex", "fragment"}) {
            auto it = shadersIt->find(key);
            if (it == shadersIt->end())
                continue;
            std::string depGuid;
            if (it->is_object()) {
                const auto guidIt = it->find("guid");
                if (guidIt != it->end() && guidIt->is_string())
                    depGuid = guidIt->get<std::string>();
                if (depGuid.empty()) {
                    const auto pathIt = it->find("path_hint");
                    if (pathIt != it->end() && pathIt->is_string())
                        depGuid = request.resolveAssetGuid(pathIt->get<std::string>());
                }
            } else if (it->is_string()) {
                depGuid = request.resolveAssetGuid(it->get<std::string>());
            }
            if (!depGuid.empty())
                deps.insert(depGuid);
        }
    }

    // Texture dependencies (properties with type == 6 == Texture2D)
    auto propsIt = root.find("properties");
    if (propsIt != root.end() && propsIt->is_object()) {
        for (auto &[propName, propVal] : propsIt->items()) {
            if (!propVal.is_object())
                continue;
            auto typeIt = propVal.find("type");
            if (typeIt == propVal.end() || !typeIt->is_number_integer())
                continue;
            int ptype = typeIt->get<int>();
            if (ptype != 6) // 6 == Texture2D
                continue;
            auto guidIt = propVal.find("guid");
            if (guidIt != propVal.end() && guidIt->is_string()) {
                std::string texGuid = guidIt->get<std::string>();
                if (!texGuid.empty())
                    deps.insert(texGuid);
            }
        }
    }

    std::vector<std::string> ordered(deps.begin(), deps.end());
    std::sort(ordered.begin(), ordered.end());
    return ordered;
}

// ============================================================================
// ModelImporter — scan model file with Assimp and extract metadata into .meta
// ============================================================================

ImportArtifact ModelImporter::Import(const ImportRequest &request) const
{
    ImportArtifact artifact(request.metadata);
    EnsureDefaultSettings(artifact.metadata);
    auto imported = MeshLoader::ImportSourceDetailed(request.sourcePath, request.guid, artifact.metadata);
    if (!imported.mesh)
        throw std::logic_error("ModelImporter detailed source import returned no runtime mesh");

    const auto checkedMetadataInt = [](uint64_t value, std::string_view field) {
        if (value > static_cast<uint64_t>(std::numeric_limits<int>::max()))
            throw std::overflow_error("ModelImporter metadata count exceeds int range: " + std::string(field));
        return static_cast<int>(value);
    };
    const auto joinCsv = [](const std::vector<std::string> &values) {
        std::string joined;
        for (size_t index = 0; index < values.size(); ++index) {
            if (index > 0)
                joined += ',';
            joined += values[index];
        }
        return joined;
    };

    // ── Write metadata to .meta ─────────────────────────────────────────

    artifact.metadata.AddMetadata("mesh_count", checkedMetadataInt(imported.meshCount, "mesh_count"));
    artifact.metadata.AddMetadata("vertex_count", checkedMetadataInt(imported.vertexCount, "vertex_count"));
    artifact.metadata.AddMetadata("index_count", checkedMetadataInt(imported.indexCount, "index_count"));
    artifact.metadata.AddMetadata("material_slot_count",
                                  checkedMetadataInt(imported.materialSlots.size(), "material_slot_count"));

    // Store material slot names as a comma-separated string for .meta
    // (InxResourceMeta uses std::any; a string is the simplest portable choice)
    artifact.metadata.AddMetadata("material_slots", joinCsv(imported.materialSlots));

    artifact.metadata.AddMetadata("bone_count", checkedMetadataInt(imported.boneNames.size(), "bone_count"));
    artifact.metadata.AddMetadata("bone_names_csv", joinCsv(imported.boneNames));

    artifact.metadata.AddMetadata("animation_count",
                                  checkedMetadataInt(imported.animationNames.size(), "animation_count"));
    artifact.metadata.AddMetadata("animation_names_csv", joinCsv(imported.animationNames));

    if (!artifact.metadata.HasKey("content_hash"))
        throw std::logic_error("ModelImporter metadata has no source content hash");
    artifact.runtimeCpuArtifacts.push_back(ImportArtifact::RuntimeCpuArtifact{
        ImportArtifact::RuntimeArtifactKind::Primary, ResourceType::Mesh, MeshArtifact::FormatVersion,
        MeshArtifact::Serialize(*imported.mesh, artifact.metadata.GetDataAs<std::string>("content_hash"))});
    const std::string sourceHash = artifact.metadata.GetDataAs<std::string>("content_hash");
    artifact.runtimeCpuArtifacts.push_back(ImportArtifact::RuntimeCpuArtifact{
        ImportArtifact::RuntimeArtifactKind::SkinnedMesh, ResourceType::Mesh, SkinnedMeshArtifact::FormatVersion,
        imported.skinnedMesh ? SkinnedMeshArtifact::Serialize(*imported.skinnedMesh, sourceHash)
                             : SkinnedMeshArtifact::SerializeEmpty(sourceHash)});

    INXLOG_INFO("ModelImporter: imported '", FromFsPath(ToFsPath(request.sourcePath).filename()), "' — ",
                imported.meshCount, " mesh(es), ", imported.vertexCount, " verts, ", imported.indexCount, " indices, ",
                imported.materialSlots.size(), " material slot(s), ", imported.boneNames.size(), " bone(s), ",
                imported.animationNames.size(), " anim(s)");

    return artifact;
}

} // namespace infernux
