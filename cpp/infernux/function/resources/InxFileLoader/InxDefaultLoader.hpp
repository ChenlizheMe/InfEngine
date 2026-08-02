#include <function/resources/AssetRegistry/IAssetLoader.h>
#include <function/resources/InxResource/InxResourceMeta.h>

namespace infernux
{
class InxDefaultTextLoader : public IAssetLoader
{
  public:
    explicit InxDefaultTextLoader(ResourceType resourceType = ResourceType::DefaultText);

    void CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                    InxResourceMeta &metaData) const override;

    RuntimeAssetPayload Load(const std::string & /*filePath*/, const std::string & /*guid*/,
                             AssetDatabase * /*adb*/) override
    {
        return nullptr;
    }
    bool Reload(const RuntimeAssetPayload & /*existing*/, const std::string & /*filePath*/,
                const std::string & /*guid*/, AssetDatabase * /*adb*/) override
    {
        return false;
    }
    [[nodiscard]] size_t EstimateRuntimeBytes(const RuntimeAssetPayload &payload) const override
    {
        if (payload)
            throw std::logic_error("default text loader cannot own a runtime payload");
        return 0;
    }
    std::set<std::string> ScanDependencies(const std::string & /*filePath*/, AssetDatabase * /*adb*/) override
    {
        return {};
    }

  private:
    ResourceType m_resourceType;
};

class InxDefaultBinaryLoader : public IAssetLoader
{
  public:
    InxDefaultBinaryLoader();

    void CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                    InxResourceMeta &metaData) const override;

    RuntimeAssetPayload Load(const std::string & /*filePath*/, const std::string & /*guid*/,
                             AssetDatabase * /*adb*/) override
    {
        return nullptr;
    }
    bool Reload(const RuntimeAssetPayload & /*existing*/, const std::string & /*filePath*/,
                const std::string & /*guid*/, AssetDatabase * /*adb*/) override
    {
        return false;
    }
    [[nodiscard]] size_t EstimateRuntimeBytes(const RuntimeAssetPayload &payload) const override
    {
        if (payload)
            throw std::logic_error("default binary loader cannot own a runtime payload");
        return 0;
    }
    std::set<std::string> ScanDependencies(const std::string & /*filePath*/, AssetDatabase * /*adb*/) override
    {
        return {};
    }

  private:
    /// @brief Get binary file type based on file extension
    /// @param extension The file extension
    /// @return String describing the binary file type
    std::string GetBinaryTypeFromExtension(const std::string &extension) const;
};
} // namespace infernux
