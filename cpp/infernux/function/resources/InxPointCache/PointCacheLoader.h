#pragma once

#include <function/resources/AssetRegistry/IAssetLoader.h>

namespace infernux
{

class PointCacheLoader final : public IAssetLoader
{
  public:
    RuntimeAssetPayload Load(const std::string &filePath, const std::string &guid, AssetDatabase *adb) override;
    [[nodiscard]] bool SupportsWorkerLoad() const noexcept override
    {
        return true;
    }
    bool Reload(const RuntimeAssetPayload &existing, const std::string &filePath, const std::string &guid,
                AssetDatabase *adb) override;
    [[nodiscard]] size_t EstimateRuntimeBytes(const RuntimeAssetPayload &payload) const override;
    std::set<std::string> ScanDependencies(const std::string &filePath, AssetDatabase *adb) override;
    void CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                    InxResourceMeta &metaData) const override;
};

} // namespace infernux
