#include "MeshLoader.h"

#include "InxMesh.h"
#include "MeshArtifact.h"

#include <core/log/InxLog.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <function/resources/InxSkinnedMesh/InxSkinnedMesh.h>
#include <function/resources/InxSkinnedMesh/SkinnedMeshArtifact.h>
#include <platform/filesystem/InxPath.h>

#include <filesystem>
#include <fstream>

namespace infernux
{
#if defined(INFERNUX_RUNTIME_MINIMAL_HOST)
void MeshLoader::CreateMeta(const char *, size_t, const std::string &, InxResourceMeta &) const
{
    throw std::logic_error("Mesh source import is unavailable in a minimal Player host");
}
#endif

namespace
{
std::string ReadArtifactBytes(const std::string &path, std::string_view label)
{
    std::ifstream file(ToFsPath(path), std::ios::binary | std::ios::ate);
    if (!file.is_open())
        throw std::runtime_error("failed to open " + std::string(label));
    const auto size = file.tellg();
    if (size <= 0)
        throw std::runtime_error(std::string(label) + " is empty");
    std::string bytes(static_cast<size_t>(size), '\0');
    file.seekg(0);
    if (!file.read(bytes.data(), size))
        throw std::runtime_error("failed to read " + std::string(label));
    return bytes;
}
} // namespace

RuntimeAssetPayload MeshLoader::Load(const std::string &filePath, const std::string &guid, AssetDatabase *adb)
{
    if (!adb)
        throw std::invalid_argument("MeshLoader requires an AssetDatabase");
    auto metadata = adb->GetMetaByGuid(guid);
    if (!metadata)
        throw std::invalid_argument("MeshLoader could not resolve metadata for GUID: " + guid);
    if (!metadata->HasKey("content_hash"))
        throw std::invalid_argument("MeshLoader metadata has no source content hash");

    const std::string sourceHash = metadata->GetDataAs<std::string>("content_hash");
    const std::string artifactPath = adb->GetRuntimeArtifactPath(guid, ResourceType::Mesh);
    const std::string skinnedArtifactPath = adb->GetSkinnedMeshArtifactPath(guid);
    if (artifactPath.empty() || !std::filesystem::is_regular_file(ToFsPath(artifactPath)))
        throw std::runtime_error("MeshLoader requires a current imported .inxmesh artifact; reimport '" + filePath +
                                 "'");

    auto mesh = MeshArtifact::Deserialize(ReadArtifactBytes(artifactPath, "Mesh artifact"), sourceHash);
    auto skinned = SkinnedMeshArtifact::Deserialize(
        ReadArtifactBytes(skinnedArtifactPath, "skinned Mesh companion artifact"), sourceHash);
    if (skinned) {
        skinned->guid = guid;
        skinned->sourcePath = filePath;
    }
    mesh->SetSkinnedData(std::move(skinned));
    mesh->SetGuid(guid);
    mesh->SetFilePath(filePath);
    return mesh;
}

bool MeshLoader::Reload(const RuntimeAssetPayload &existing, const std::string &filePath, const std::string &guid,
                        AssetDatabase *adb)
{
    INXLOG_INFO("MeshLoader::Reload: '", filePath, "'");
    auto freshData = Load(filePath, guid, adb);
    if (!freshData)
        return false;
    auto loaded = freshData.Get<InxMesh>();
    auto target = existing.Get<InxMesh>();
    if (!target)
        return false;

    target->SetName(loaded->GetName());
    target->SetFilePath(loaded->GetFilePath());
    target->SetData(std::vector<Vertex>(loaded->GetVertices()), std::vector<uint32_t>(loaded->GetIndices()),
                    std::vector<SubMesh>(loaded->GetSubMeshes()));
    target->SetMaterialSlotNames(std::vector<std::string>(loaded->GetMaterialSlotNames()));
    target->SetMaterialSlotData(std::vector<MaterialSlotData>(loaded->GetMaterialSlotData()));
    target->SetNodeNames(std::vector<std::string>(loaded->GetNodeNames()));
    target->SetSkinnedData(loaded->GetSkinnedData());
    INXLOG_INFO("MeshLoader::Reload: updated '", target->GetName(), "' in-place");
    return true;
}

size_t MeshLoader::EstimateRuntimeBytes(const RuntimeAssetPayload &payload) const
{
    const auto mesh = payload.Get<InxMesh>();
    if (!mesh)
        throw std::invalid_argument("MeshLoader cannot estimate an empty runtime payload");
    return mesh->GetRuntimeMemoryBytes();
}

std::set<std::string> MeshLoader::ScanDependencies(const std::string &, AssetDatabase *)
{
    return {};
}

} // namespace infernux
