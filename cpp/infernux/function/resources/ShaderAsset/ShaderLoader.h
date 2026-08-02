#pragma once

#include <function/resources/AssetRegistry/AssetRegistry.h>

namespace infernux
{

/**
 * @brief IAssetLoader implementation for shader assets (.vert, .frag).
 *
 * Compiles GLSL source → SPIR-V and produces a ShaderAsset instance
 * containing explicit semantic pass variants keyed by ShaderCompileTarget.
 *
 * Key design points:
 *   - Load() reads the shader source, compiles all variants via InxShaderLoader,
 *     and returns a shared_ptr<ShaderAsset>.
 *   - Reload() recompiles and replaces the ShaderAsset data in-place.
 *   - ScanDependencies() returns {} — shaders have no outgoing asset deps.
 */
class ShaderLoader final : public IAssetLoader
{
  public:
    RuntimeAssetPayload Load(const std::string &filePath, const std::string &guid, AssetDatabase *adb) override;

    bool Reload(const RuntimeAssetPayload &existing, const std::string &filePath, const std::string &guid,
                AssetDatabase *adb) override;
    [[nodiscard]] size_t EstimateRuntimeBytes(const RuntimeAssetPayload &payload) const override;

    std::set<std::string> ScanDependencies(const std::string &filePath, AssetDatabase *adb) override;

    void CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                    InxResourceMeta &metaData) const override;
};

} // namespace infernux
