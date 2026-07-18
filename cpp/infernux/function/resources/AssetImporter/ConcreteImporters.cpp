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
#include <regex>
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

ImportArtifact RenderEffectImporter::Import(const ImportRequest &request) const
{
    ImportArtifact artifact(request.metadata);
    artifact.dependencies = ScanDependencies(request);
    artifact.dependenciesAuthoritative = true;
    return artifact;
}

std::vector<std::string> RenderEffectImporter::ScanDependencies(const ImportRequest &request) const
{
    if (!request.resolveAssetGuid)
        throw std::logic_error("RenderEffectImporter request has no dependency resolver");

    nlohmann::json root;
    try {
        std::ifstream file(ToFsPath(request.sourcePath));
        if (!file.is_open())
            throw std::runtime_error("failed to open render effect document");
        file >> root;
    } catch (const std::exception &e) {
        throw std::runtime_error("RenderEffectImporter failed to parse '" + request.sourcePath + "': " + e.what());
    }

    const auto requireExactKeys = [](const nlohmann::json &value, std::initializer_list<const char *> expected,
                                     const std::string &location) {
        if (!value.is_object())
            throw std::runtime_error(location + " must be an object");
        std::unordered_set<std::string> keys;
        for (const char *key : expected)
            keys.emplace(key);
        if (value.size() != keys.size())
            throw std::runtime_error(location + " contains missing or unknown fields");
        for (const auto &[key, ignored] : value.items()) {
            (void)ignored;
            if (keys.find(key) == keys.end())
                throw std::runtime_error(location + " contains unknown field '" + key + "'");
        }
    };

    if (!root.is_object())
        throw std::runtime_error("render effect document root must be an object");
    if (!root.contains("$schema") || !root["$schema"].is_string())
        throw std::runtime_error("render effect $schema must be a string");
    if (!root.contains("$version") || !root["$version"].is_number_integer() || root["$version"].get<int>() != 1)
        throw std::runtime_error("render effect $version must be 1");

    std::unordered_set<std::string> dependencies;
    const auto readReference = [&](const nlohmann::json &reference, const std::string &location) {
        requireExactKeys(reference, {"guid", "path_hint"}, location);
        if (!reference["guid"].is_string() || !reference["path_hint"].is_string())
            throw std::runtime_error(location + " guid and path_hint must be strings");
        std::string guid = reference["guid"].get<std::string>();
        const std::string pathHint = reference["path_hint"].get<std::string>();
        if (guid.empty() && !pathHint.empty())
            guid = request.resolveAssetGuid(pathHint);
        if (guid.empty())
            throw std::runtime_error(location + " could not resolve an asset GUID");
        dependencies.insert(std::move(guid));
    };

    const std::string schema = root["$schema"].get<std::string>();
    if (schema == "infernux.render_effect") {
        requireExactKeys(root, {"$schema", "$version", "feature_type", "parameters", "dependencies"}, "render effect");
        static const std::regex featureTypePattern("^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$");
        if (!root["feature_type"].is_string() ||
            !std::regex_match(root["feature_type"].get_ref<const std::string &>(), featureTypePattern))
            throw std::runtime_error("render effect feature_type must be a lowercase namespaced identifier");
        if (!root["parameters"].is_object())
            throw std::runtime_error("render effect parameters must be an object");
        if (!root["dependencies"].is_array())
            throw std::runtime_error("render effect dependencies must be an array");
        for (size_t index = 0; index < root["dependencies"].size(); ++index)
            readReference(root["dependencies"][index], "dependencies[" + std::to_string(index) + "]");
    } else if (schema == "infernux.render_effect_group") {
        requireExactKeys(root, {"$schema", "$version", "entries"}, "render effect group");
        if (!root["entries"].is_array())
            throw std::runtime_error("render effect group entries must be an array");
        std::unordered_set<std::string> entryIds;
        for (size_t index = 0; index < root["entries"].size(); ++index) {
            const auto &entry = root["entries"][index];
            const std::string location = "entries[" + std::to_string(index) + "]";
            requireExactKeys(entry, {"entry_id", "asset", "enabled", "overrides"}, location);
            if (!entry["entry_id"].is_string() || entry["entry_id"].get_ref<const std::string &>().empty())
                throw std::runtime_error(location + ".entry_id must be a non-empty string");
            if (!entryIds.insert(entry["entry_id"].get<std::string>()).second)
                throw std::runtime_error("render effect group entry_id values must be unique");
            if (!entry["enabled"].is_boolean())
                throw std::runtime_error(location + ".enabled must be a boolean");
            if (!entry["overrides"].is_object())
                throw std::runtime_error(location + ".overrides must be an object");
            readReference(entry["asset"], location + ".asset");
        }
    } else {
        throw std::runtime_error("unsupported render effect schema '" + schema + "'");
    }

    std::vector<std::string> ordered(dependencies.begin(), dependencies.end());
    std::sort(ordered.begin(), ordered.end());
    return ordered;
}

ImportArtifact ParticleGraphImporter::Import(const ImportRequest &request) const
{
    ImportArtifact artifact(request.metadata);
    artifact.dependencies = ScanDependencies(request);
    artifact.dependenciesAuthoritative = true;
    return artifact;
}

std::vector<std::string> ParticleGraphImporter::ScanDependencies(const ImportRequest &request) const
{
    if (!request.resolveAssetGuid)
        throw std::logic_error("ParticleGraphImporter request has no dependency resolver");

    nlohmann::json root;
    try {
        std::ifstream file(ToFsPath(request.sourcePath));
        if (!file.is_open())
            throw std::runtime_error("failed to open particle graph document");
        file >> root;
    } catch (const std::exception &e) {
        throw std::runtime_error("ParticleGraphImporter failed to parse '" + request.sourcePath + "': " + e.what());
    }

    const auto requireExactKeys = [](const nlohmann::json &value, std::initializer_list<const char *> expected,
                                     const std::string &location) {
        if (!value.is_object())
            throw std::runtime_error(location + " must be an object");
        std::unordered_set<std::string> keys;
        for (const char *key : expected)
            keys.emplace(key);
        if (value.size() != keys.size())
            throw std::runtime_error(location + " contains missing or unknown fields");
        for (const auto &[key, ignored] : value.items()) {
            (void)ignored;
            if (keys.find(key) == keys.end())
                throw std::runtime_error(location + " contains unknown field '" + key + "'");
        }
    };

    requireExactKeys(root, {"$schema", "$version", "stable_id", "name", "emitters", "parameters"}, "particle graph");
    if (!root["$schema"].is_string() || root["$schema"].get<std::string>() != "infernux.particle_graph")
        throw std::runtime_error("particle graph has an unsupported $schema");
    if (!root["$version"].is_number_integer() || root["$version"].get<int>() != 1)
        throw std::runtime_error("particle graph $version must be 1");
    if (!root["stable_id"].is_string() || root["stable_id"].get_ref<const std::string &>().empty() ||
        !root["name"].is_string() || root["name"].get_ref<const std::string &>().empty())
        throw std::runtime_error("particle graph stable_id and name must be non-empty strings");
    if (!root["emitters"].is_array() || root["emitters"].empty() || !root["parameters"].is_array())
        throw std::runtime_error("particle graph requires emitters and parameters arrays");

    std::unordered_set<std::string> dependencies;
    for (size_t emitterIndex = 0; emitterIndex < root["emitters"].size(); ++emitterIndex) {
        const auto &emitter = root["emitters"][emitterIndex];
        const std::string emitterLocation = "emitters[" + std::to_string(emitterIndex) + "]";
        requireExactKeys(emitter, {"stable_id", "name", "settings", "attributes", "stages"}, emitterLocation);
        if (!emitter["stages"].is_object())
            throw std::runtime_error(emitterLocation + ".stages must be an object");
        requireExactKeys(emitter["stages"], {"init", "update", "rendering"}, emitterLocation + ".stages");

        for (const char *stageName : {"init", "update", "rendering"}) {
            const auto &stage = emitter["stages"][stageName];
            const std::string stageLocation = emitterLocation + ".stages." + stageName;
            requireExactKeys(stage, {"$schema", "$version", "domain", "nodes", "links", "metadata"}, stageLocation);
            if (!stage["nodes"].is_array() || !stage["links"].is_array())
                throw std::runtime_error(stageLocation + " nodes and links must be arrays");
            for (const auto &node : stage["nodes"]) {
                if (!node.is_object() || !node.contains("properties") || !node["properties"].is_object())
                    throw std::runtime_error(stageLocation + " contains an invalid node");
                const auto material = node["properties"].find("material");
                if (material == node["properties"].end())
                    continue;
                requireExactKeys(*material, {"guid", "path_hint"}, stageLocation + ".material");
                if (!(*material)["guid"].is_string() || !(*material)["path_hint"].is_string())
                    throw std::runtime_error(stageLocation + ".material guid and path_hint must be strings");
                std::string guid = (*material)["guid"].get<std::string>();
                const std::string pathHint = (*material)["path_hint"].get<std::string>();
                if (guid.empty() && !pathHint.empty())
                    guid = request.resolveAssetGuid(pathHint);
                if (!guid.empty())
                    dependencies.insert(guid);
            }
        }
    }

    std::vector<std::string> ordered(dependencies.begin(), dependencies.end());
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
