#include "PointCacheLoader.h"

#include "InxPointCache.h"
#include "PointCacheArtifact.h"

#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/InxFileLoader/InxDefaultLoader.hpp>
#include <platform/filesystem/InxPath.h>

#include <filesystem>
#include <fstream>
#include <limits>
#include <stdexcept>

namespace infernux
{
namespace
{
std::string ReadArtifact(const std::string &path)
{
    std::ifstream file(ToFsPath(path), std::ios::binary | std::ios::ate);
    if (!file.is_open())
        throw std::runtime_error("failed to open point cache artifact");
    const auto size = file.tellg();
    if (size <= 0)
        throw std::runtime_error("point cache artifact is empty");
    const auto byteCount = static_cast<uint64_t>(size);
    if (byteCount > static_cast<uint64_t>(std::numeric_limits<size_t>::max()) ||
        byteCount > static_cast<uint64_t>(std::numeric_limits<std::streamsize>::max()))
        throw std::runtime_error("point cache artifact is too large to load");
    std::string bytes(static_cast<size_t>(byteCount), '\0');
    file.seekg(0);
    file.read(bytes.data(), static_cast<std::streamsize>(byteCount));
    if (!file)
        throw std::runtime_error("failed to read point cache artifact");
    return bytes;
}
} // namespace

RuntimeAssetPayload PointCacheLoader::Load(const std::string &filePath, const std::string &guid, AssetDatabase *adb)
{
    if (!adb)
        throw std::invalid_argument("PointCacheLoader requires an AssetDatabase");
    if (filePath.empty() || guid.empty())
        throw std::invalid_argument("PointCacheLoader requires a path and GUID");
    const auto metadata = adb->GetMetaByGuid(guid);
    if (!metadata || !metadata->HasKey("content_hash"))
        throw std::invalid_argument("PointCacheLoader requires imported metadata with a content hash");
    const std::string artifactPath = adb->GetRuntimeArtifactPath(guid, ResourceType::PointCache);
    if (artifactPath.empty() || !std::filesystem::is_regular_file(ToFsPath(artifactPath)))
        throw std::runtime_error("PointCacheLoader requires a current .inxpcache artifact; reimport the source asset");

    auto cpuData = std::make_shared<PointCacheCpuData>(
        PointCacheArtifact::Deserialize(ReadArtifact(artifactPath), metadata->GetDataAs<std::string>("content_hash")));
    auto pointCache = std::make_shared<InxPointCache>();
    pointCache->SetGuid(guid);
    pointCache->SetFilePath(filePath);
    pointCache->SetName(cpuData->name);
    pointCache->SetCpuData(std::move(cpuData));
    return pointCache;
}

bool PointCacheLoader::Reload(const RuntimeAssetPayload &existing, const std::string &filePath, const std::string &guid,
                              AssetDatabase *adb)
{
    const auto pointCache = existing.Get<InxPointCache>();
    if (!pointCache)
        return false;
    const auto fresh = Load(filePath, guid, adb).Get<InxPointCache>();
    pointCache->SetGuid(fresh->GetGuid());
    pointCache->SetFilePath(fresh->GetFilePath());
    pointCache->SetName(fresh->GetName());
    pointCache->SetCpuData(fresh->GetCpuData());
    return true;
}

size_t PointCacheLoader::EstimateRuntimeBytes(const RuntimeAssetPayload &payload) const
{
    const auto pointCache = payload.Get<InxPointCache>();
    if (!pointCache)
        throw std::invalid_argument("PointCacheLoader cannot estimate an empty runtime payload");
    return pointCache->GetRuntimeMemoryBytes();
}

std::set<std::string> PointCacheLoader::ScanDependencies(const std::string &, AssetDatabase *)
{
    return {};
}

void PointCacheLoader::CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                                  InxResourceMeta &metaData) const
{
    InxDefaultTextLoader(ResourceType::PointCache).CreateMeta(content, contentSize, filePath, metaData);
}

} // namespace infernux
