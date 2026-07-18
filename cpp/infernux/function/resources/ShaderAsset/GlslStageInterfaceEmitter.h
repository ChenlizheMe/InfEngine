#pragma once

#include <function/resources/ShaderAsset/ShaderDescriptor.h>
#include <function/resources/ShaderAsset/ShaderStageLinker.h>
#include <string>

namespace infernux
{

class GlslStageInterfaceEmitter final
{
  public:
    static constexpr uint32_t MaterialDescriptorSet = 0;
    static constexpr uint32_t FirstTextureBinding = 2;
    static constexpr uint32_t MaterialBufferBinding = 14;

    [[nodiscard]] static std::string EmitVertexDeclarations(const ShaderProgramInterfaceArtifact &artifact,
                                                            const ShaderDescriptor &vertex);
    [[nodiscard]] static std::string EmitFragmentDeclarations(const ShaderProgramInterfaceArtifact &artifact);
    [[nodiscard]] static std::string EmitVertexCall(const ShaderProgramInterfaceArtifact &artifact,
                                                    const ShaderDescriptor &vertex);
    [[nodiscard]] static std::string EmitSurfaceCall(const ShaderProgramInterfaceArtifact &artifact);
    [[nodiscard]] static std::string EmitTextureDeclarations(const ShaderProgramInterfaceArtifact &artifact,
                                                             ShaderStageVisibility stage,
                                                             uint32_t descriptorSet = MaterialDescriptorSet,
                                                             uint32_t firstTextureBinding = FirstTextureBinding);
    [[nodiscard]] static std::string EmitMaterialBlockMembers(const ShaderProgramInterfaceArtifact &artifact);
};

} // namespace infernux
