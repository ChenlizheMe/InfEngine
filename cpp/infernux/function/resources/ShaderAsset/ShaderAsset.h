#pragma once

#include <core/types/ShaderTypes.h>
#include <function/resources/ShaderAsset/ShaderDescriptor.h>

#include <string>
#include <vector>

namespace infernux
{

/// Render-state metadata extracted from shader annotations at compile time.
struct ShaderRenderMeta
{
    std::string cullMode;
    std::string depthWrite;
    std::string depthTest;
    std::string blend;
    int queue = -1;
    std::string passTag;
    std::string stencil;
    std::string alphaClip;
};

/// Compiled shader asset — the product of InxShaderLoader compilation.
///
/// Holds SPIR-V bytecode for all pass variants (forward, shadow, gbuffer)
/// and the render-state annotations parsed from the source file.
/// Loaded and cached by ShaderLoader via AssetRegistry.
struct ShaderAsset
{
    /// Shader identity (e.g. "pbr", "unlit", "surface_water")
    std::string shaderId;

    /// "vertex" or "fragment"
    std::string shaderType;

    /// Source file path (for hot-reload cache key)
    std::string filePath;

    /// Parsed authoring contract retained for stage linking and diagnostics.
    ShaderDescriptor descriptor;

    /// Forward-pass SPIR-V bytecode (always present on success)
    std::vector<char> spirvForward;

    /// Shadow-pass fragment variant SPIR-V (empty if not applicable)
    std::vector<char> spirvShadow;

    /// Shadow-pass vertex variant SPIR-V (empty if not applicable)
    std::vector<char> spirvShadowVertex;

    /// GBuffer-pass fragment variant SPIR-V (empty if not applicable)
    std::vector<char> spirvGBuffer;

    /// Render-state annotations (fragment shaders only)
    ShaderRenderMeta renderMeta;

    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept
    {
        return sizeof(*this) + shaderId.capacity() + shaderType.capacity() + filePath.capacity() +
               descriptor.GetRuntimeMemoryBytes() - sizeof(descriptor) + spirvForward.capacity() +
               spirvShadow.capacity() + spirvShadowVertex.capacity() + spirvGBuffer.capacity() +
               renderMeta.cullMode.capacity() + renderMeta.depthWrite.capacity() + renderMeta.depthTest.capacity() +
               renderMeta.blend.capacity() + renderMeta.passTag.capacity() + renderMeta.stencil.capacity() +
               renderMeta.alphaClip.capacity();
    }
};

} // namespace infernux
