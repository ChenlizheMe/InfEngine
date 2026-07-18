#pragma once

#include <core/types/ShaderTypes.h>
#include <function/resources/ShaderAsset/ShaderDescriptor.h>

#include <cstdint>
#include <string>
#include <utility>
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

struct ShaderStageVariant
{
    ShaderCompileTarget target = ShaderCompileTarget::Forward;
    std::vector<char> spirv;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return target >= ShaderCompileTarget::Forward && target < ShaderCompileTarget::Count && !spirv.empty() &&
               spirv.size() % sizeof(uint32_t) == 0;
    }
};

/// Compiled shader asset — the product of InxShaderLoader compilation.
///
/// Holds explicit pass variants for one authored stage and the render-state
/// annotations parsed from the source file.
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

    /// Compiled variants keyed by semantic render target. Forward is required
    /// for a loadable legacy stage; optional targets are emitted as supported.
    std::vector<ShaderStageVariant> variants;

    /// Render-state annotations (fragment shaders only)
    ShaderRenderMeta renderMeta;

    [[nodiscard]] const ShaderStageVariant *FindVariant(ShaderCompileTarget target) const noexcept
    {
        for (const auto &variant : variants) {
            if (variant.target == target)
                return &variant;
        }
        return nullptr;
    }

    [[nodiscard]] bool HasVariant(ShaderCompileTarget target) const noexcept
    {
        const auto *variant = FindVariant(target);
        return variant != nullptr && variant->IsValid();
    }

    bool SetVariant(ShaderCompileTarget target, std::vector<char> spirv)
    {
        ShaderStageVariant replacement{target, std::move(spirv)};
        if (!replacement.IsValid())
            return false;
        for (auto &variant : variants) {
            if (variant.target == target) {
                variant = std::move(replacement);
                return true;
            }
        }
        variants.push_back(std::move(replacement));
        return true;
    }

    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept
    {
        size_t bytes = sizeof(*this) + shaderId.capacity() + shaderType.capacity() + filePath.capacity() +
                       descriptor.GetRuntimeMemoryBytes() - sizeof(descriptor) +
                       variants.capacity() * sizeof(ShaderStageVariant) + renderMeta.cullMode.capacity() +
                       renderMeta.depthWrite.capacity() + renderMeta.depthTest.capacity() +
                       renderMeta.blend.capacity() + renderMeta.passTag.capacity() + renderMeta.stencil.capacity() +
                       renderMeta.alphaClip.capacity();
        for (const auto &variant : variants)
            bytes += variant.spirv.capacity();
        return bytes;
    }
};

} // namespace infernux
