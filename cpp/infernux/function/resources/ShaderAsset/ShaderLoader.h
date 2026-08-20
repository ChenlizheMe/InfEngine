#pragma once

#include <function/resources/AssetRegistry/AssetRegistry.h>

namespace infernux
{

/**
 * @brief IAssetLoader implementation for shader assets (.vert, .frag).
 *
 * Loads authored GLSL and compiles it into a runtime ShaderAsset.
 *
 * Key design points:
 *   - Load() prefers a cooked INXSHADER blob and only compiles GLSL when the
 *     payload is still authoring source.
 *   - Compiled or cooked assets contain explicit semantic pass variants keyed
 *     by ShaderCompileTarget.
 *   - Reload() recompiles and replaces the ShaderAsset data in-place.
 *   - ScanDependencies() returns {} — shaders have no outgoing asset deps.
 */
class ShaderLoader final : public IAssetLoader
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
