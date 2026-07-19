#include "ConcreteImporters.h"

#include <core/log/InxLog.h>
#include <function/resources/InxMaterial/MaterialDocumentValidation.h>
#include <function/resources/InxMesh/MeshArtifact.h>
#include <function/resources/InxMesh/MeshLoader.h>
#include <function/resources/InxPointCache/PointCacheArtifact.h>
#include <function/resources/InxSkinnedMesh/SkinnedMeshArtifact.h>
#include <function/resources/InxTexture/TextureArtifact.h>
#include <function/resources/InxTexture/TextureDecoder.h>
#include <platform/filesystem/InxPath.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <fstream>
#include <limits>
#include <nlohmann/json.hpp>
#include <regex>
#include <unordered_set>
#include <vector>

namespace infernux
{

namespace
{
PointCacheChannelType ParsePointCacheChannelType(const std::string &value)
{
    if (value == "f32")
        return PointCacheChannelType::Float;
    if (value == "vec2")
        return PointCacheChannelType::Float2;
    if (value == "vec3")
        return PointCacheChannelType::Float3;
    if (value == "vec4")
        return PointCacheChannelType::Float4;
    if (value == "u32")
        return PointCacheChannelType::UInt;
    throw std::runtime_error("point cache channel has unsupported type '" + value + "'");
}

PointCacheChannelSemantic ParsePointCacheChannelSemantic(const std::string &value)
{
    if (value == "custom")
        return PointCacheChannelSemantic::Custom;
    if (value == "position")
        return PointCacheChannelSemantic::Position;
    if (value == "normal")
        return PointCacheChannelSemantic::Normal;
    if (value == "color")
        return PointCacheChannelSemantic::Color;
    if (value == "id")
        return PointCacheChannelSemantic::Id;
    throw std::runtime_error("point cache channel has unsupported semantic '" + value + "'");
}

void AppendPointCacheU32(std::vector<uint8_t> &bytes, uint32_t value)
{
    for (unsigned shift = 0; shift < 32; shift += 8)
        bytes.push_back(static_cast<uint8_t>((value >> shift) & 0xffU));
}

void AppendPointCacheFloat(std::vector<uint8_t> &bytes, float value)
{
    uint32_t bits = 0;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&bits, &value, sizeof(bits));
    AppendPointCacheU32(bytes, bits);
}

uint64_t AlignPointCacheChannel(uint64_t value)
{
    return (value + 15U) & ~uint64_t{15U};
}
} // namespace

ImportArtifact TextureImporter::Import(const ImportRequest &request) const
{
    ImportArtifact artifact(request.metadata);
    EnsureDefaultSettings(artifact.metadata);
    std::string extension = FromFsPath(ToFsPath(request.sourcePath).extension());
    std::transform(extension.begin(), extension.end(), extension.begin(), ::tolower);
    if (extension == ".inxvfield") {
        artifact.metadata.AddMetadata("texture_type", std::string("vector_field"));
        artifact.metadata.AddMetadata("srgb", false);
        artifact.metadata.AddMetadata("texture_compression", std::string("none"));
        const std::string format = artifact.metadata.GetDataAs<std::string>("texture_format");
        if (format == "auto")
            artifact.metadata.AddMetadata("texture_format", std::string("rgba16_float"));
        else if (format != "rgba16_float" && format != "rgba32_float")
            throw std::invalid_argument("VectorField textures require rgba16_float or rgba32_float storage");
    } else if (artifact.metadata.GetDataAs<std::string>("texture_type") == "vector_field") {
        throw std::invalid_argument("VectorField textures must use the .inxvfield source format");
    }
    if (!artifact.metadata.HasKey("content_hash"))
        throw std::logic_error("TextureImporter metadata has no source content hash");
    const auto cpuData = TextureDecoder::Decode(request.sourcePath, artifact.metadata);
    if (!cpuData || !cpuData->IsValid())
        throw std::runtime_error("TextureImporter failed to build the runtime texture artifact");
    artifact.metadata.AddMetadata("artifact_width", static_cast<int>(cpuData->mipLevels.front().width));
    artifact.metadata.AddMetadata("artifact_height", static_cast<int>(cpuData->mipLevels.front().height));
    artifact.metadata.AddMetadata("artifact_depth", static_cast<int>(cpuData->mipLevels.front().depth));
    artifact.metadata.AddMetadata("artifact_mip_count", static_cast<int>(cpuData->mipLevels.size()));
    artifact.metadata.AddMetadata("artifact_dimension",
                                  std::string(cpuData->dimension == TextureDimension::Texture3D ? "3d" : "2d"));
    artifact.metadata.AddMetadata("artifact_srgb", TextureFormatIsSrgb(cpuData->format));
    artifact.metadata.AddMetadata("artifact_format", std::string(TextureFormatName(cpuData->format)));
    artifact.runtimeCpuArtifacts.push_back(ImportArtifact::RuntimeCpuArtifact{
        ImportArtifact::RuntimeArtifactKind::Primary, ResourceType::Texture,
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
    try {
        material_document_validation::ValidateMaterialDocument(root, request.sourcePath);
    } catch (const std::exception &e) {
        throw std::runtime_error("MaterialImporter rejected '" + request.sourcePath + "': " + e.what());
    }

    // Canonical shader references carry GUID plus stable compiler/path hints.
    auto shadersIt = root.find("shaders");
    if (shadersIt != root.end() && shadersIt->is_object()) {
        for (const auto &key : {"vertex", "fragment"}) {
            auto it = shadersIt->find(key);
            if (it == shadersIt->end())
                continue;
            std::string depGuid;
            const auto guidIt = it->find("guid");
            if (guidIt != it->end() && guidIt->is_string())
                depGuid = guidIt->get<std::string>();
            if (depGuid.empty()) {
                const auto pathIt = it->find("path_hint");
                if (pathIt != it->end() && pathIt->is_string())
                    depGuid = request.resolveAssetGuid(pathIt->get<std::string>());
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
        requireExactKeys(root, {"$schema", "feature_type", "parameters", "dependencies"}, "render effect");
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
        requireExactKeys(root, {"$schema", "entries"}, "render effect group");
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

    requireExactKeys(root, {"$schema", "stable_id", "name", "emitters", "parameters"}, "particle graph");
    if (!root["$schema"].is_string() || root["$schema"].get<std::string>() != "infernux.particle_graph")
        throw std::runtime_error("particle graph has an unsupported $schema");
    if (!root["stable_id"].is_string() || root["stable_id"].get_ref<const std::string &>().empty() ||
        !root["name"].is_string() || root["name"].get_ref<const std::string &>().empty())
        throw std::runtime_error("particle graph stable_id and name must be non-empty strings");
    if (!root["emitters"].is_array() || root["emitters"].empty() || !root["parameters"].is_array())
        throw std::runtime_error("particle graph requires emitters and parameters arrays");

    std::unordered_set<std::string> dependencies;
    const auto readReference = [&](const nlohmann::json &reference, const std::string &location) {
        requireExactKeys(reference, {"guid", "path_hint"}, location);
        if (!reference["guid"].is_string() || !reference["path_hint"].is_string())
            throw std::runtime_error(location + " guid and path_hint must be strings");
        std::string guid = reference["guid"].get<std::string>();
        const std::string pathHint = reference["path_hint"].get<std::string>();
        if (guid.empty() && !pathHint.empty())
            guid = request.resolveAssetGuid(pathHint);
        if (!guid.empty())
            dependencies.insert(guid);
    };
    for (size_t emitterIndex = 0; emitterIndex < root["emitters"].size(); ++emitterIndex) {
        const auto &emitter = root["emitters"][emitterIndex];
        const std::string emitterLocation = "emitters[" + std::to_string(emitterIndex) + "]";
        requireExactKeys(emitter, {"stable_id", "name", "settings", "attributes", "data_interfaces", "stages"},
                         emitterLocation);
        if (!emitter["data_interfaces"].is_array())
            throw std::runtime_error(emitterLocation + ".data_interfaces must be an array");
        for (size_t interfaceIndex = 0; interfaceIndex < emitter["data_interfaces"].size(); ++interfaceIndex) {
            const auto &dataInterface = emitter["data_interfaces"][interfaceIndex];
            const std::string interfaceLocation =
                emitterLocation + ".data_interfaces[" + std::to_string(interfaceIndex) + "]";
            if (!dataInterface.is_object() || !dataInterface.contains("kind") || !dataInterface["kind"].is_string())
                throw std::runtime_error(interfaceLocation + " must be a typed object");
            const std::string kind = dataInterface["kind"].get<std::string>();
            if (kind == "vector_field") {
                requireExactKeys(dataInterface,
                                 {"kind", "stable_id", "name", "texture", "space", "field_to_space", "vector_scale",
                                  "boundary", "filtering"},
                                 interfaceLocation);
                readReference(dataInterface["texture"], interfaceLocation + ".texture");
            } else if (kind == "point_cache") {
                requireExactKeys(dataInterface,
                                 {"kind", "stable_id", "name", "cache", "space", "cache_to_space", "position_channel",
                                  "normal_channel", "color_channel", "id_channel"},
                                 interfaceLocation);
                readReference(dataInterface["cache"], interfaceLocation + ".cache");
            } else {
                throw std::runtime_error(interfaceLocation + " has unsupported kind '" + kind + "'");
            }
        }
        if (!emitter["stages"].is_object())
            throw std::runtime_error(emitterLocation + ".stages must be an object");
        requireExactKeys(emitter["stages"], {"init", "update", "rendering"}, emitterLocation + ".stages");

        for (const char *stageName : {"init", "update", "rendering"}) {
            const auto &stage = emitter["stages"][stageName];
            const std::string stageLocation = emitterLocation + ".stages." + stageName;
            requireExactKeys(stage, {"$schema", "domain", "nodes", "links", "metadata"}, stageLocation);
            if (!stage["nodes"].is_array() || !stage["links"].is_array())
                throw std::runtime_error(stageLocation + " nodes and links must be arrays");
            for (const auto &node : stage["nodes"]) {
                if (!node.is_object() || !node.contains("properties") || !node["properties"].is_object())
                    throw std::runtime_error(stageLocation + " contains an invalid node");
                const auto material = node["properties"].find("material");
                if (material == node["properties"].end())
                    continue;
                readReference(*material, stageLocation + ".material");
            }
        }
    }

    std::vector<std::string> ordered(dependencies.begin(), dependencies.end());
    std::sort(ordered.begin(), ordered.end());
    return ordered;
}

ImportArtifact PointCacheImporter::Import(const ImportRequest &request) const
{
    nlohmann::json root;
    try {
        std::ifstream file(ToFsPath(request.sourcePath));
        if (!file.is_open())
            throw std::runtime_error("failed to open point cache document");
        file >> root;
    } catch (const std::exception &e) {
        throw std::runtime_error("PointCacheImporter failed to parse '" + request.sourcePath + "': " + e.what());
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

    requireExactKeys(root, {"$schema", "stable_id", "name", "bake_basis", "point_count", "channels"}, "point cache");
    if (!root["$schema"].is_string() || root["$schema"].get<std::string>() != "infernux.point_cache")
        throw std::runtime_error("point cache schema is unsupported");
    if (!root["stable_id"].is_string() || root["stable_id"].get_ref<const std::string &>().empty() ||
        !root["name"].is_string() || root["name"].get_ref<const std::string &>().empty() ||
        !root["bake_basis"].is_string() || root["bake_basis"].get_ref<const std::string &>().empty())
        throw std::runtime_error("point cache identity and bake_basis must be non-empty strings");
    const std::string bakeBasis = root["bake_basis"].get<std::string>();
    if (bakeBasis != "right_handed_y_up" && bakeBasis != "right_handed_z_up" && bakeBasis != "left_handed_y_up")
        throw std::runtime_error("point cache bake_basis is unsupported");
    if (!root["point_count"].is_number_unsigned() || root["point_count"].get<uint64_t>() == 0 ||
        root["point_count"].get<uint64_t>() > std::numeric_limits<uint32_t>::max() || !root["channels"].is_array() ||
        root["channels"].empty())
        throw std::runtime_error("point cache point_count and channels are invalid");

    struct SourceChannel
    {
        PointCacheChannel descriptor;
        const nlohmann::json *data = nullptr;
    };
    const uint32_t pointCount = root["point_count"].get<uint32_t>();
    std::vector<SourceChannel> channels;
    channels.reserve(root["channels"].size());
    for (size_t index = 0; index < root["channels"].size(); ++index) {
        const auto &encoded = root["channels"][index];
        const std::string location = "point cache.channels[" + std::to_string(index) + "]";
        requireExactKeys(encoded, {"name", "semantic", "type", "data"}, location);
        if (!encoded["name"].is_string() || encoded["name"].get_ref<const std::string &>().empty() ||
            !encoded["semantic"].is_string() || !encoded["type"].is_string() || !encoded["data"].is_array() ||
            encoded["data"].size() != pointCount)
            throw std::runtime_error(location + " descriptor or data length is invalid");
        PointCacheChannel descriptor;
        descriptor.name = encoded["name"].get<std::string>();
        descriptor.semantic = ParsePointCacheChannelSemantic(encoded["semantic"].get<std::string>());
        descriptor.type = ParsePointCacheChannelType(encoded["type"].get<std::string>());
        descriptor.elementStride = PointCacheChannelElementStride(descriptor.type);
        channels.push_back({std::move(descriptor), &encoded["data"]});
    }
    std::sort(channels.begin(), channels.end(), [](const SourceChannel &left, const SourceChannel &right) {
        return left.descriptor.name < right.descriptor.name;
    });

    PointCacheCpuData cache;
    cache.stableId = root["stable_id"].get<std::string>();
    cache.name = root["name"].get<std::string>();
    cache.bakeBasis = bakeBasis;
    cache.pointCount = pointCount;
    for (auto &source : channels) {
        const uint64_t aligned = AlignPointCacheChannel(cache.bytes.size());
        if (aligned > std::numeric_limits<size_t>::max())
            throw std::runtime_error("point cache payload exceeds addressable memory");
        cache.bytes.resize(static_cast<size_t>(aligned), 0);
        source.descriptor.byteOffset = aligned;
        const uint32_t components = source.descriptor.elementStride / sizeof(uint32_t);
        for (uint32_t point = 0; point < pointCount; ++point) {
            const auto &encodedValue = (*source.data)[point];
            if (source.descriptor.type == PointCacheChannelType::UInt) {
                if (!encodedValue.is_number_unsigned() ||
                    encodedValue.get<uint64_t>() > std::numeric_limits<uint32_t>::max())
                    throw std::runtime_error("point cache uint channel contains an invalid value");
                AppendPointCacheU32(cache.bytes, encodedValue.get<uint32_t>());
                continue;
            }
            if (components == 1) {
                if (!encodedValue.is_number())
                    throw std::runtime_error("point cache float channel contains a non-numeric value");
                const double value = encodedValue.get<double>();
                if (!std::isfinite(value) || std::abs(value) > std::numeric_limits<float>::max())
                    throw std::runtime_error("point cache float channel contains a non-finite value");
                AppendPointCacheFloat(cache.bytes, static_cast<float>(value));
                continue;
            }
            if (!encodedValue.is_array() || encodedValue.size() != components)
                throw std::runtime_error("point cache vector channel has an invalid component count");
            for (uint32_t component = 0; component < components; ++component) {
                if (!encodedValue[component].is_number())
                    throw std::runtime_error("point cache vector channel contains a non-numeric value");
                const double value = encodedValue[component].get<double>();
                if (!std::isfinite(value) || std::abs(value) > std::numeric_limits<float>::max())
                    throw std::runtime_error("point cache vector channel contains a non-finite value");
                AppendPointCacheFloat(cache.bytes, static_cast<float>(value));
            }
        }
        cache.channels.push_back(std::move(source.descriptor));
    }

    ImportArtifact artifact(request.metadata);
    if (!artifact.metadata.HasKey("content_hash"))
        throw std::logic_error("PointCacheImporter metadata has no source content hash");
    artifact.metadata.AddMetadata("artifact_point_count", static_cast<int>(cache.pointCount));
    artifact.metadata.AddMetadata("artifact_channel_count", static_cast<int>(cache.channels.size()));
    artifact.metadata.AddMetadata("artifact_bake_basis", cache.bakeBasis);
    artifact.runtimeCpuArtifacts.push_back(ImportArtifact::RuntimeCpuArtifact{
        ImportArtifact::RuntimeArtifactKind::Primary, ResourceType::PointCache,
        PointCacheArtifact::Serialize(cache, artifact.metadata.GetDataAs<std::string>("content_hash"))});
    return artifact;
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
        ImportArtifact::RuntimeArtifactKind::Primary, ResourceType::Mesh,
        MeshArtifact::Serialize(*imported.mesh, artifact.metadata.GetDataAs<std::string>("content_hash"))});
    const std::string sourceHash = artifact.metadata.GetDataAs<std::string>("content_hash");
    artifact.runtimeCpuArtifacts.push_back(ImportArtifact::RuntimeCpuArtifact{
        ImportArtifact::RuntimeArtifactKind::SkinnedMesh, ResourceType::Mesh,
        imported.skinnedMesh ? SkinnedMeshArtifact::Serialize(*imported.skinnedMesh, sourceHash)
                             : SkinnedMeshArtifact::SerializeEmpty(sourceHash)});

    INXLOG_INFO("ModelImporter: imported '", FromFsPath(ToFsPath(request.sourcePath).filename()), "' — ",
                imported.meshCount, " mesh(es), ", imported.vertexCount, " verts, ", imported.indexCount, " indices, ",
                imported.materialSlots.size(), " material slot(s), ", imported.boneNames.size(), " bone(s), ",
                imported.animationNames.size(), " anim(s)");

    return artifact;
}

} // namespace infernux
