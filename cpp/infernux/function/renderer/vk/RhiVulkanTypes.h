#pragma once

#include "../rhi/RhiDescriptors.h"
#include "../rhi/RhiUpload.h"

#include <vulkan/vulkan.h>

namespace infernux::rhi
{

[[nodiscard]] inline VkShaderStageFlags ToVkShaderStages(ShaderStage stages) noexcept
{
    VkShaderStageFlags result = 0;
    if (HasShaderStage(stages, ShaderStage::Vertex))
        result |= VK_SHADER_STAGE_VERTEX_BIT;
    if (HasShaderStage(stages, ShaderStage::Fragment))
        result |= VK_SHADER_STAGE_FRAGMENT_BIT;
    if (HasShaderStage(stages, ShaderStage::Compute))
        result |= VK_SHADER_STAGE_COMPUTE_BIT;
    return result;
}

[[nodiscard]] constexpr VkFormat ToVkFormat(PixelFormat format) noexcept
{
    switch (format) {
    case PixelFormat::R8UNorm:
        return VK_FORMAT_R8_UNORM;
    case PixelFormat::RG8UNorm:
        return VK_FORMAT_R8G8_UNORM;
    case PixelFormat::RGBA8UNorm:
        return VK_FORMAT_R8G8B8A8_UNORM;
    case PixelFormat::RGBA8Srgb:
        return VK_FORMAT_R8G8B8A8_SRGB;
    case PixelFormat::BGRA8UNorm:
        return VK_FORMAT_B8G8R8A8_UNORM;
    case PixelFormat::BGRA8Srgb:
        return VK_FORMAT_B8G8R8A8_SRGB;
    case PixelFormat::R16SFloat:
        return VK_FORMAT_R16_SFLOAT;
    case PixelFormat::RG16SFloat:
        return VK_FORMAT_R16G16_SFLOAT;
    case PixelFormat::RGBA16SFloat:
        return VK_FORMAT_R16G16B16A16_SFLOAT;
    case PixelFormat::RGBA16UNorm:
        return VK_FORMAT_R16G16B16A16_UNORM;
    case PixelFormat::R32SFloat:
        return VK_FORMAT_R32_SFLOAT;
    case PixelFormat::RG32UInt:
        return VK_FORMAT_R32G32_UINT;
    case PixelFormat::RGBA32SFloat:
        return VK_FORMAT_R32G32B32A32_SFLOAT;
    case PixelFormat::RGB10A2UNorm:
        return VK_FORMAT_A2R10G10B10_UNORM_PACK32;
    case PixelFormat::RGBA4UNormPack16:
        return VK_FORMAT_R4G4B4A4_UNORM_PACK16;
    case PixelFormat::BC1RgbaUNorm:
        return VK_FORMAT_BC1_RGBA_UNORM_BLOCK;
    case PixelFormat::BC1RgbaSrgb:
        return VK_FORMAT_BC1_RGBA_SRGB_BLOCK;
    case PixelFormat::BC3UNorm:
        return VK_FORMAT_BC3_UNORM_BLOCK;
    case PixelFormat::BC3Srgb:
        return VK_FORMAT_BC3_SRGB_BLOCK;
    case PixelFormat::BC4UNorm:
        return VK_FORMAT_BC4_UNORM_BLOCK;
    case PixelFormat::BC5UNorm:
        return VK_FORMAT_BC5_UNORM_BLOCK;
    case PixelFormat::BC6HUFloat:
        return VK_FORMAT_BC6H_UFLOAT_BLOCK;
    case PixelFormat::BC7UNorm:
        return VK_FORMAT_BC7_UNORM_BLOCK;
    case PixelFormat::BC7Srgb:
        return VK_FORMAT_BC7_SRGB_BLOCK;
    case PixelFormat::D32SFloat:
        return VK_FORMAT_D32_SFLOAT;
    case PixelFormat::D24UNormS8UInt:
        return VK_FORMAT_D24_UNORM_S8_UINT;
    case PixelFormat::Count:
    case PixelFormat::Undefined:
        return VK_FORMAT_UNDEFINED;
    }
    return VK_FORMAT_UNDEFINED;
}

[[nodiscard]] constexpr PixelFormat FromVkFormat(VkFormat format) noexcept
{
    switch (format) {
    case VK_FORMAT_R8_UNORM:
        return PixelFormat::R8UNorm;
    case VK_FORMAT_R8G8_UNORM:
        return PixelFormat::RG8UNorm;
    case VK_FORMAT_R8G8B8A8_UNORM:
        return PixelFormat::RGBA8UNorm;
    case VK_FORMAT_R8G8B8A8_SRGB:
        return PixelFormat::RGBA8Srgb;
    case VK_FORMAT_B8G8R8A8_UNORM:
        return PixelFormat::BGRA8UNorm;
    case VK_FORMAT_B8G8R8A8_SRGB:
        return PixelFormat::BGRA8Srgb;
    case VK_FORMAT_R16_SFLOAT:
        return PixelFormat::R16SFloat;
    case VK_FORMAT_R16G16_SFLOAT:
        return PixelFormat::RG16SFloat;
    case VK_FORMAT_R16G16B16A16_SFLOAT:
        return PixelFormat::RGBA16SFloat;
    case VK_FORMAT_R16G16B16A16_UNORM:
        return PixelFormat::RGBA16UNorm;
    case VK_FORMAT_R32_SFLOAT:
        return PixelFormat::R32SFloat;
    case VK_FORMAT_R32G32_UINT:
        return PixelFormat::RG32UInt;
    case VK_FORMAT_R32G32B32A32_SFLOAT:
        return PixelFormat::RGBA32SFloat;
    case VK_FORMAT_A2R10G10B10_UNORM_PACK32:
        return PixelFormat::RGB10A2UNorm;
    case VK_FORMAT_R4G4B4A4_UNORM_PACK16:
        return PixelFormat::RGBA4UNormPack16;
    case VK_FORMAT_BC1_RGBA_UNORM_BLOCK:
        return PixelFormat::BC1RgbaUNorm;
    case VK_FORMAT_BC1_RGBA_SRGB_BLOCK:
        return PixelFormat::BC1RgbaSrgb;
    case VK_FORMAT_BC3_UNORM_BLOCK:
        return PixelFormat::BC3UNorm;
    case VK_FORMAT_BC3_SRGB_BLOCK:
        return PixelFormat::BC3Srgb;
    case VK_FORMAT_BC4_UNORM_BLOCK:
        return PixelFormat::BC4UNorm;
    case VK_FORMAT_BC5_UNORM_BLOCK:
        return PixelFormat::BC5UNorm;
    case VK_FORMAT_BC6H_UFLOAT_BLOCK:
        return PixelFormat::BC6HUFloat;
    case VK_FORMAT_BC7_UNORM_BLOCK:
        return PixelFormat::BC7UNorm;
    case VK_FORMAT_BC7_SRGB_BLOCK:
        return PixelFormat::BC7Srgb;
    case VK_FORMAT_D32_SFLOAT:
        return PixelFormat::D32SFloat;
    case VK_FORMAT_D24_UNORM_S8_UINT:
        return PixelFormat::D24UNormS8UInt;
    default:
        return PixelFormat::Undefined;
    }
}

[[nodiscard]] constexpr SampleCount FromVkSampleCount(VkSampleCountFlagBits samples) noexcept
{
    switch (samples) {
    case VK_SAMPLE_COUNT_2_BIT:
        return SampleCount::Two;
    case VK_SAMPLE_COUNT_4_BIT:
        return SampleCount::Four;
    case VK_SAMPLE_COUNT_8_BIT:
        return SampleCount::Eight;
    case VK_SAMPLE_COUNT_1_BIT:
    default:
        return SampleCount::One;
    }
}

[[nodiscard]] constexpr VkSampleCountFlagBits ToVkSampleCount(SampleCount samples) noexcept
{
    switch (samples) {
    case SampleCount::One:
        return VK_SAMPLE_COUNT_1_BIT;
    case SampleCount::Two:
        return VK_SAMPLE_COUNT_2_BIT;
    case SampleCount::Four:
        return VK_SAMPLE_COUNT_4_BIT;
    case SampleCount::Eight:
        return VK_SAMPLE_COUNT_8_BIT;
    }
    return VK_SAMPLE_COUNT_1_BIT;
}

[[nodiscard]] constexpr VkBufferUsageFlags ToVkBufferUsage(BufferUsage usage) noexcept
{
    switch (usage) {
    case BufferUsage::Vertex:
        return VK_BUFFER_USAGE_VERTEX_BUFFER_BIT;
    case BufferUsage::Index:
        return VK_BUFFER_USAGE_INDEX_BUFFER_BIT;
    case BufferUsage::Storage:
        return VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
    }
    return 0;
}

} // namespace infernux::rhi
