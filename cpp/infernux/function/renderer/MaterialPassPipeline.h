#pragma once

#include <core/types/ShaderTypes.h>
#include <function/renderer/rhi/RhiDescriptors.h>

#include <cstddef>
#include <cstdint>
#include <functional>
#include <vector>

namespace infernux
{

/// Immutable render-target contract for one material pass. It is deliberately
/// backend-neutral so RenderGraph can key pipelines without leaking Vulkan
/// formats into the user-facing pipeline model.
struct MaterialPassPipelineDescriptor
{
    ShaderCompileTarget target = ShaderCompileTarget::Forward;
    std::vector<rhi::PixelFormat> colorFormats;
    rhi::PixelFormat depthFormat = rhi::PixelFormat::Undefined;
    rhi::SampleCount samples = rhi::SampleCount::One;
    bool depthReadOnly = false;
    [[nodiscard]] rhi::GraphicsRenderingSignature RenderingSignature() const noexcept
    {
        rhi::GraphicsRenderingSignature signature;
        signature.colorFormatCount = static_cast<uint32_t>(colorFormats.size());
        for (size_t index = 0; index < colorFormats.size() && index < signature.colorFormats.size(); ++index)
            signature.colorFormats[index] = colorFormats[index];
        signature.depthFormat = depthFormat;
        signature.stencilFormat = rhi::IsStencilFormat(depthFormat) ? depthFormat : rhi::PixelFormat::Undefined;
        signature.samples = samples;
        return signature;
    }

    [[nodiscard]] bool MatchesRenderingSignature(const rhi::GraphicsRenderingSignature &signature) const noexcept
    {
        return RenderingSignature() == signature;
    }

    void ApplyRenderingContract(rhi::GraphicsPipelineDesc &descriptor) const noexcept
    {
        descriptor.useDynamicRendering = true;
        descriptor.renderTargetLayout = {};
        descriptor.renderingSignature = RenderingSignature();
    }

    [[nodiscard]] bool IsValid() const noexcept
    {
        if (target < ShaderCompileTarget::Forward || target >= ShaderCompileTarget::Count || colorFormats.size() > 8)
            return false;

        for (const rhi::PixelFormat format : colorFormats) {
            if (!rhi::IsValidPixelFormat(format) || rhi::IsDepthFormat(format))
                return false;
        }
        if (depthFormat != rhi::PixelFormat::Undefined && !rhi::IsDepthFormat(depthFormat))
            return false;
        if (depthReadOnly && depthFormat == rhi::PixelFormat::Undefined)
            return false;
        if (!RenderingSignature().IsValid())
            return false;

        switch (target) {
        case ShaderCompileTarget::Depth:
        case ShaderCompileTarget::Shadow:
            return colorFormats.empty() && depthFormat != rhi::PixelFormat::Undefined;
        case ShaderCompileTarget::Picking:
            return colorFormats.size() == 1 && colorFormats.front() == rhi::PixelFormat::RG32UInt &&
                   depthFormat != rhi::PixelFormat::Undefined;
        case ShaderCompileTarget::Forward:
        case ShaderCompileTarget::ForwardPlus:
        case ShaderCompileTarget::GBuffer:
        case ShaderCompileTarget::Motion:
            return !colorFormats.empty();
        case ShaderCompileTarget::Normal:
        case ShaderCompileTarget::BaseColor:
            return colorFormats.size() == 1 && colorFormats.front() == rhi::PixelFormat::RGBA16SFloat &&
                   depthFormat != rhi::PixelFormat::Undefined;
        case ShaderCompileTarget::Count:
            return false;
        }
        return false;
    }

    friend bool operator==(const MaterialPassPipelineDescriptor &lhs,
                           const MaterialPassPipelineDescriptor &rhs) noexcept
    {
        return lhs.target == rhs.target && lhs.colorFormats == rhs.colorFormats && lhs.depthFormat == rhs.depthFormat &&
               lhs.samples == rhs.samples && lhs.depthReadOnly == rhs.depthReadOnly;
    }

    friend bool operator!=(const MaterialPassPipelineDescriptor &lhs,
                           const MaterialPassPipelineDescriptor &rhs) noexcept
    {
        return !(lhs == rhs);
    }
};

struct MaterialPassPipelineDescriptorHash
{
    [[nodiscard]] size_t operator()(const MaterialPassPipelineDescriptor &descriptor) const noexcept
    {
        size_t hash = 0;
        const auto combine = [&hash](size_t value) {
            hash ^= value + static_cast<size_t>(0x9e3779b97f4a7c15ull) + (hash << 6u) + (hash >> 2u);
        };
        combine(static_cast<size_t>(descriptor.target));
        combine(static_cast<size_t>(descriptor.samples));
        combine(static_cast<size_t>(descriptor.depthFormat));
        combine(static_cast<size_t>(descriptor.depthReadOnly));
        combine(descriptor.colorFormats.size());
        for (const rhi::PixelFormat format : descriptor.colorFormats)
            combine(static_cast<size_t>(format));
        return hash;
    }
};

} // namespace infernux
