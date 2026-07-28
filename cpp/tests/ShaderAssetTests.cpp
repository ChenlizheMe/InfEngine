#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <function/resources/ShaderAsset/ShaderAsset.h>

#include <cassert>
#include <iostream>
#include <vector>

namespace
{

std::vector<char> Spirv(size_t words, char value)
{
    return std::vector<char>(words * sizeof(uint32_t), value);
}

} // namespace

int main()
{
    infernux::ShaderAsset asset;
    asset.shaderId = "Tests/VariantAsset";
    asset.shaderType = "fragment";

    assert(!asset.HasVariant(infernux::ShaderCompileTarget::Forward));
    assert(asset.SetVariant(infernux::ShaderCompileTarget::Forward, Spirv(1, 1)));
    assert(asset.HasVariant(infernux::ShaderCompileTarget::Forward));
    assert(asset.variants.size() == 1);

    assert(asset.SetVariant(infernux::ShaderCompileTarget::Forward, Spirv(2, 2)));
    assert(asset.variants.size() == 1);
    assert(asset.FindVariant(infernux::ShaderCompileTarget::Forward)->spirv.size() == 2 * sizeof(uint32_t));

    assert(asset.SetVariant(infernux::ShaderCompileTarget::Shadow, Spirv(1, 3)));
    assert(asset.SetVariant(infernux::ShaderCompileTarget::GBuffer, Spirv(1, 4)));
    assert(asset.variants.size() == 3);
    assert(asset.HasVariant(infernux::ShaderCompileTarget::Shadow));
    assert(asset.HasVariant(infernux::ShaderCompileTarget::GBuffer));

    assert(!asset.SetVariant(infernux::ShaderCompileTarget::Depth, {}));
    assert(!asset.SetVariant(infernux::ShaderCompileTarget::Count, Spirv(1, 5)));
    assert(asset.variants.size() == 3);
    assert(asset.GetRuntimeMemoryBytes() >= sizeof(asset) + 4 * sizeof(uint32_t));

    infernux::InxShaderLoader compiler(true, false, false, false, false, true, false, false, false, false);
    const std::string source = "#version 450\nvoid main() { gl_Position = vec4(0.0); }\n";
    infernux::InxResourceMeta metadata;
    compiler.CreateMeta(source.data(), source.size(), "Tests/CacheReset.vert", metadata);
    const std::string cachePath = metadata.GetDataAs<std::string>("file_path");
    const auto compiled = compiler.CompileVertexGlsl(source, cachePath);
    assert(!compiled.empty());
    assert(infernux::InxShaderLoader::TakeCompiledVariants(cachePath).empty());
    assert(infernux::InxShaderLoader::TakeCompiledVariants(cachePath).empty());

    std::cout << "Shader asset tests passed\n";
    return 0;
}
