#pragma once

#include "../rhi/RhiTypes.h"
#include "../rhi/RhiUpload.h"

#include <vulkan/vulkan.h>

namespace infernux::rhi
{

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
    case PixelFormat::R16SFloat:
        return VK_FORMAT_R16_SFLOAT;
    case PixelFormat::RG16SFloat:
        return VK_FORMAT_R16G16_SFLOAT;
    case PixelFormat::RGBA16SFloat:
        return VK_FORMAT_R16G16B16A16_SFLOAT;
    case PixelFormat::R32SFloat:
        return VK_FORMAT_R32_SFLOAT;
    case PixelFormat::RGBA32SFloat:
        return VK_FORMAT_R32G32B32A32_SFLOAT;
    case PixelFormat::RGB10A2UNorm:
        return VK_FORMAT_A2R10G10B10_UNORM_PACK32;
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
