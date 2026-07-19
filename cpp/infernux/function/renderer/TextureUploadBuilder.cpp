#include "TextureUploadBuilder.h"

#include <limits>
#include <stdexcept>

namespace infernux
{
namespace
{

rhi::PixelFormat ToRhiFormat(TextureFormat format)
{
    switch (format) {
    case TextureFormat::Rgba8UNorm:
        return rhi::PixelFormat::RGBA8UNorm;
    case TextureFormat::Rgba8Srgb:
        return rhi::PixelFormat::RGBA8Srgb;
    case TextureFormat::Rgba32Float:
        return rhi::PixelFormat::RGBA32SFloat;
    case TextureFormat::BC1RgbaUNorm:
        return rhi::PixelFormat::BC1RgbaUNorm;
    case TextureFormat::BC1RgbaSrgb:
        return rhi::PixelFormat::BC1RgbaSrgb;
    case TextureFormat::BC3UNorm:
        return rhi::PixelFormat::BC3UNorm;
    case TextureFormat::BC3Srgb:
        return rhi::PixelFormat::BC3Srgb;
    case TextureFormat::BC4UNorm:
        return rhi::PixelFormat::BC4UNorm;
    case TextureFormat::BC5UNorm:
        return rhi::PixelFormat::BC5UNorm;
    case TextureFormat::BC6HUFloat:
        return rhi::PixelFormat::BC6HUFloat;
    case TextureFormat::BC7UNorm:
        return rhi::PixelFormat::BC7UNorm;
    case TextureFormat::BC7Srgb:
        return rhi::PixelFormat::BC7Srgb;
    case TextureFormat::Rgba4UNormPack16:
        return rhi::PixelFormat::RGBA4UNormPack16;
    case TextureFormat::Rgba16UNorm:
        return rhi::PixelFormat::RGBA16UNorm;
    case TextureFormat::Rgba16Float:
        return rhi::PixelFormat::RGBA16SFloat;
    }
    throw std::invalid_argument("texture upload received an unsupported imported format");
}

} // namespace

TextureUploadBatch::TextureUploadBatch(const TextureCpuData &cpuData, const rhi::SamplerDesc &sampler)
{
    if (!cpuData.IsValid() || cpuData.mipLevels.size() > std::numeric_limits<uint32_t>::max())
        throw std::invalid_argument("texture upload requires valid imported mip data");

    const auto &baseMip = cpuData.mipLevels.front();
    m_request.texture.dimension = cpuData.dimension == TextureDimension::Texture3D
                                      ? rhi::TextureDimension::Texture3D
                                      : rhi::TextureDimension::Texture2D;
    m_request.texture.width = baseMip.width;
    m_request.texture.height = baseMip.height;
    m_request.texture.depthOrLayers =
        cpuData.dimension == TextureDimension::Texture3D ? baseMip.depth : 1U;
    m_request.texture.mipLevels = static_cast<uint32_t>(cpuData.mipLevels.size());
    m_request.texture.format = ToRhiFormat(cpuData.format);
    m_request.texture.usage = rhi::TextureUsageFlags::Sampled | rhi::TextureUsageFlags::TransferDestination;
    m_request.view.dimension = cpuData.dimension == TextureDimension::Texture3D
                                   ? rhi::TextureViewDimension::Texture3D
                                   : rhi::TextureViewDimension::Texture2D;
    m_request.view.format = m_request.texture.format;
    m_request.view.mipCount = m_request.texture.mipLevels;
    m_request.sampler = sampler;

    m_subresources.reserve(cpuData.mipLevels.size());
    for (uint32_t mipLevel = 0; mipLevel < cpuData.mipLevels.size(); ++mipLevel) {
        const auto &mip = cpuData.mipLevels[mipLevel];
        if (mip.byteOffset > cpuData.bytes.size() || mip.byteSize > cpuData.bytes.size() - mip.byteOffset)
            throw std::invalid_argument("texture upload mip byte range is outside imported storage");
        m_subresources.push_back({cpuData.bytes.data() + mip.byteOffset,
                                  static_cast<size_t>(mip.byteSize),
                                  mipLevel,
                                  0,
                                  1,
                                  mip.width,
                                  mip.height,
                                  mip.depth,
                                  static_cast<size_t>(mip.rowPitch),
                                  static_cast<size_t>(mip.slicePitch)});
    }
}

} // namespace infernux
