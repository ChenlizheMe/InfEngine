#pragma once

#include <function/resources/AssetRegistry/IAssetLoader.h>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <set>
#include <string>

namespace infernux
{

/**
 * @brief Loader for Python script files (.py).
 *
 * This loader:
 * - Generates stable GUID for script assets
 * - Creates meta files for script tracking
 *
 * Note: Python scripts are not "loaded" into C++ memory, they are
 * dynamically imported by Python runtime. This loader only handles
 * metadata generation for the asset database.
 */
class InxPythonScriptLoader : public IAssetLoader
{
  public:
    InxPythonScriptLoader();

    // -- IAssetLoader meta interface --
    void CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                    InxResourceMeta &metaData) const override;

    // -- IAssetLoader runtime interface (no-op for scripts) --
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
            throw std::logic_error("Python script loader cannot own a C++ runtime payload");
        return 0;
    }
    std::set<std::string> ScanDependencies(const std::string & /*filePath*/, AssetDatabase * /*adb*/) override
    {
        return {};
    }
};

} // namespace infernux
