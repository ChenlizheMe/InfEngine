/**
 * @file VkHandle.cpp
 * @brief Implementation of RAII wrappers for Vulkan handles
 */

#include "VkHandle.h"
#include "AsyncTransferContext.h"
#include <SDL3/SDL.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <stdexcept>

namespace infernux
{
namespace vk
{

namespace
{

void WaitForFencePumpingEvents(VkDevice device, VkFence fence, const char *context)
{
    constexpr uint64_t kPollTimeoutNs = 50'000'000; // 50 ms
    while (true) {
        VkResult result = vkWaitForFences(device, 1, &fence, VK_TRUE, kPollTimeoutNs);
        if (result == VK_SUCCESS) {
            return;
        }
        if (result != VK_TIMEOUT) {
            INXLOG_ERROR(context, ": vkWaitForFences failed: ", result);
            return;
        }
        SDL_PumpEvents();
    }
}

} // namespace

// ============================================================================
// VkBufferHandle Implementation
// ============================================================================

VkBufferHandle::VkBufferHandle(VkBufferHandle &&other) noexcept
    : m_allocator(other.m_allocator), m_device(other.m_device), m_buffer(other.m_buffer),
      m_allocation(other.m_allocation), m_size(other.m_size), m_mappedPtr(other.m_mappedPtr)
{
    other.m_allocator = VK_NULL_HANDLE;
    other.m_device = VK_NULL_HANDLE;
    other.m_buffer = VK_NULL_HANDLE;
    other.m_allocation = VK_NULL_HANDLE;
    other.m_mappedPtr = nullptr;
    other.m_size = 0;
}

VkBufferHandle &VkBufferHandle::operator=(VkBufferHandle &&other) noexcept
{
    if (this != &other) {
        Destroy();
        m_allocator = other.m_allocator;
        m_device = other.m_device;
        m_buffer = other.m_buffer;
        m_allocation = other.m_allocation;
        m_size = other.m_size;
        m_mappedPtr = other.m_mappedPtr;

        other.m_allocator = VK_NULL_HANDLE;
        other.m_device = VK_NULL_HANDLE;
        other.m_buffer = VK_NULL_HANDLE;
        other.m_allocation = VK_NULL_HANDLE;
        other.m_mappedPtr = nullptr;
        other.m_size = 0;
    }
    return *this;
}

bool VkBufferHandle::Create(VmaAllocator allocator, VkDevice device, VkDeviceSize size, VkBufferUsageFlags usage,
                            VkMemoryPropertyFlags properties, const std::vector<uint32_t> &queueFamilies)
{
    Destroy();

    m_allocator = allocator;
    m_device = device;
    m_size = size;

    // Create buffer via VMA
    VkBufferCreateInfo bufferInfo{};
    bufferInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bufferInfo.size = size;
    bufferInfo.usage = usage;
    if (queueFamilies.size() > 1) {
        bufferInfo.sharingMode = VK_SHARING_MODE_CONCURRENT;
        bufferInfo.queueFamilyIndexCount = static_cast<uint32_t>(queueFamilies.size());
        bufferInfo.pQueueFamilyIndices = queueFamilies.data();
    } else {
        bufferInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    }

    VmaAllocationCreateInfo allocCreateInfo{};

    // Choose VMA usage and flags based on memory property requirements.
    // NOTE: With VMA 3.x AUTO* modes, do NOT set requiredFlags to DEVICE_LOCAL_BIT;
    // AUTO_PREFER_DEVICE handles device-local preference internally.
    // Only set requiredFlags for host properties (HOST_VISIBLE, HOST_COHERENT, etc.).
    if (properties & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) {
        if (properties & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT) {
            // Resizable-BAR / device-local + host-visible
            allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO;
            allocCreateInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT;
            allocCreateInfo.requiredFlags = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT;
        } else {
            // Pure GPU-local (vertex/index buffers after staging copy)
            allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;
        }
    } else if (properties & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT) {
        allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO;
        allocCreateInfo.requiredFlags = properties; // HOST_VISIBLE + HOST_COHERENT etc.
        // Staging buffers: sequential write; UBOs: random access
        if (usage & VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT) {
            allocCreateInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT;
        } else {
            allocCreateInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT;
        }
    } else {
        allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO;
    }

    VkResult result = vmaCreateBuffer(allocator, &bufferInfo, &allocCreateInfo, &m_buffer, &m_allocation, nullptr);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create buffer with VMA (VkResult: {})", static_cast<int>(result));
        m_buffer = VK_NULL_HANDLE;
        m_allocation = VK_NULL_HANDLE;
        return false;
    }

    return true;
}

void VkBufferHandle::Destroy() noexcept
{
    if (m_mappedPtr != nullptr) {
        Unmap();
    }
    if (m_buffer != VK_NULL_HANDLE && m_allocator != VK_NULL_HANDLE) {
        vmaDestroyBuffer(m_allocator, m_buffer, m_allocation);
        m_buffer = VK_NULL_HANDLE;
        m_allocation = VK_NULL_HANDLE;
    }
    m_size = 0;
}

void *VkBufferHandle::Map()
{
    return Map(0, m_size);
}

void *VkBufferHandle::Map(VkDeviceSize offset, VkDeviceSize size)
{
    if (m_mappedPtr != nullptr) {
        return m_mappedPtr;
    }
    if (m_allocation == VK_NULL_HANDLE) {
        return nullptr;
    }

    if (vmaMapMemory(m_allocator, m_allocation, &m_mappedPtr) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to map buffer memory via VMA");
        return nullptr;
    }
    // VMA maps the entire allocation; adjust pointer for offset
    if (offset > 0) {
        m_mappedPtr = static_cast<char *>(m_mappedPtr) + offset;
    }
    return m_mappedPtr;
}

void VkBufferHandle::Unmap() noexcept
{
    if (m_mappedPtr != nullptr && m_allocation != VK_NULL_HANDLE) {
        vmaUnmapMemory(m_allocator, m_allocation);
        m_mappedPtr = nullptr;
    }
}

void VkBufferHandle::CopyFrom(const void *data, VkDeviceSize size, VkDeviceSize offset)
{
    if (!data || size == 0)
        throw std::invalid_argument("buffer copy requires non-empty source data");
    if (offset > m_size || size > m_size - offset)
        throw std::out_of_range("buffer copy exceeds the destination allocation");
    void *mapped = Map(offset, size);
    if (!mapped)
        throw std::runtime_error("failed to map destination buffer for CPU copy");
    std::memcpy(mapped, data, static_cast<size_t>(size));
    Unmap();
}

// ============================================================================
// VkImageHandle Implementation
// ============================================================================

VkImageHandle::VkImageHandle(VkImageHandle &&other) noexcept
    : m_allocator(other.m_allocator), m_device(other.m_device), m_image(other.m_image), m_view(other.m_view),
      m_allocation(other.m_allocation), m_width(other.m_width), m_height(other.m_height),
      m_mipLevels(other.m_mipLevels), m_format(other.m_format)
{
    other.m_allocator = VK_NULL_HANDLE;
    other.m_device = VK_NULL_HANDLE;
    other.m_image = VK_NULL_HANDLE;
    other.m_view = VK_NULL_HANDLE;
    other.m_allocation = VK_NULL_HANDLE;
    other.m_width = 0;
    other.m_height = 0;
    other.m_mipLevels = 1;
    other.m_format = VK_FORMAT_UNDEFINED;
}

VkImageHandle &VkImageHandle::operator=(VkImageHandle &&other) noexcept
{
    if (this != &other) {
        Destroy();
        m_allocator = other.m_allocator;
        m_device = other.m_device;
        m_image = other.m_image;
        m_view = other.m_view;
        m_allocation = other.m_allocation;
        m_width = other.m_width;
        m_height = other.m_height;
        m_mipLevels = other.m_mipLevels;
        m_format = other.m_format;

        other.m_allocator = VK_NULL_HANDLE;
        other.m_device = VK_NULL_HANDLE;
        other.m_image = VK_NULL_HANDLE;
        other.m_view = VK_NULL_HANDLE;
        other.m_allocation = VK_NULL_HANDLE;
        other.m_width = 0;
        other.m_height = 0;
        other.m_mipLevels = 1;
        other.m_format = VK_FORMAT_UNDEFINED;
    }
    return *this;
}

bool VkImageHandle::Create(VmaAllocator allocator, VkDevice device, uint32_t width, uint32_t height, VkFormat format,
                           VkImageTiling tiling, VkImageUsageFlags usage, VkMemoryPropertyFlags properties,
                           VkSampleCountFlagBits samples, uint32_t mipLevels)
{
    Destroy();

    m_allocator = allocator;
    m_device = device;
    m_width = width;
    m_height = height;
    m_format = format;
    m_mipLevels = mipLevels;

    // Create image via VMA
    VkImageCreateInfo imageInfo{};
    imageInfo.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    imageInfo.imageType = VK_IMAGE_TYPE_2D;
    imageInfo.format = format;
    imageInfo.extent.width = width;
    imageInfo.extent.height = height;
    imageInfo.extent.depth = 1;
    imageInfo.mipLevels = mipLevels;
    imageInfo.arrayLayers = 1;
    imageInfo.tiling = tiling;
    imageInfo.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    imageInfo.usage = usage;
    imageInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    imageInfo.samples = samples;

    VmaAllocationCreateInfo allocCreateInfo{};

    if (properties & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) {
        // AUTO_PREFER_DEVICE handles device-local preference; no requiredFlags needed.
        allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;
    } else {
        allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO;
    }

    VkResult result = vmaCreateImage(allocator, &imageInfo, &allocCreateInfo, &m_image, &m_allocation, nullptr);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create image with VMA (VkResult: {})", static_cast<int>(result));
        m_image = VK_NULL_HANDLE;
        m_allocation = VK_NULL_HANDLE;
        return false;
    }

    return true;
}

bool VkImageHandle::CreateConcurrent(VmaAllocator allocator, VkDevice device, uint32_t width, uint32_t height,
                                     VkFormat format, VkImageTiling tiling, VkImageUsageFlags usage,
                                     VkMemoryPropertyFlags properties, const std::vector<uint32_t> &sharedQueueFamilies,
                                     VkSampleCountFlagBits samples, uint32_t mipLevels)
{
    // CONCURRENT sharing requires at least 2 distinct queue families per
    // the Vulkan spec; silently downgrade if the caller can't actually
    // satisfy that (e.g. iGPU with a single queue family). Same fast path
    // as Create() below for that case.
    if (sharedQueueFamilies.size() < 2) {
        return Create(allocator, device, width, height, format, tiling, usage, properties, samples, mipLevels);
    }

    Destroy();

    m_allocator = allocator;
    m_device = device;
    m_width = width;
    m_height = height;
    m_format = format;
    m_mipLevels = mipLevels;

    VkImageCreateInfo imageInfo{};
    imageInfo.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    imageInfo.imageType = VK_IMAGE_TYPE_2D;
    imageInfo.format = format;
    imageInfo.extent.width = width;
    imageInfo.extent.height = height;
    imageInfo.extent.depth = 1;
    imageInfo.mipLevels = mipLevels;
    imageInfo.arrayLayers = 1;
    imageInfo.tiling = tiling;
    imageInfo.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    imageInfo.usage = usage;
    imageInfo.sharingMode = VK_SHARING_MODE_CONCURRENT;
    imageInfo.queueFamilyIndexCount = static_cast<uint32_t>(sharedQueueFamilies.size());
    imageInfo.pQueueFamilyIndices = sharedQueueFamilies.data();
    imageInfo.samples = samples;

    VmaAllocationCreateInfo allocCreateInfo{};
    if (properties & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) {
        allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;
    } else {
        allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO;
    }

    VkResult result = vmaCreateImage(allocator, &imageInfo, &allocCreateInfo, &m_image, &m_allocation, nullptr);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create concurrent image with VMA (VkResult: ", static_cast<int>(result), ")");
        m_image = VK_NULL_HANDLE;
        m_allocation = VK_NULL_HANDLE;
        return false;
    }

    return true;
}

bool VkImageHandle::CreateView(VkFormat format, VkImageAspectFlags aspectFlags, uint32_t mipLevels)
{
    if (m_image == VK_NULL_HANDLE) {
        INXLOG_ERROR("Cannot create view for null image");
        return false;
    }

    // Destroy existing view
    if (m_view != VK_NULL_HANDLE) {
        vkDestroyImageView(m_device, m_view, nullptr);
        m_view = VK_NULL_HANDLE;
    }

    VkImageViewCreateInfo viewInfo{};
    viewInfo.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
    viewInfo.image = m_image;
    viewInfo.viewType = VK_IMAGE_VIEW_TYPE_2D;
    viewInfo.format = format;
    viewInfo.subresourceRange.aspectMask = aspectFlags;
    viewInfo.subresourceRange.baseMipLevel = 0;
    viewInfo.subresourceRange.levelCount = mipLevels;
    viewInfo.subresourceRange.baseArrayLayer = 0;
    viewInfo.subresourceRange.layerCount = 1;

    if (vkCreateImageView(m_device, &viewInfo, nullptr, &m_view) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create image view");
        return false;
    }

    return true;
}

void VkImageHandle::Destroy() noexcept
{
    if (m_view != VK_NULL_HANDLE && m_device != VK_NULL_HANDLE) {
        vkDestroyImageView(m_device, m_view, nullptr);
        m_view = VK_NULL_HANDLE;
    }
    if (m_image != VK_NULL_HANDLE && m_allocator != VK_NULL_HANDLE) {
        vmaDestroyImage(m_allocator, m_image, m_allocation);
        m_image = VK_NULL_HANDLE;
        m_allocation = VK_NULL_HANDLE;
    }
    m_width = 0;
    m_height = 0;
    m_mipLevels = 1;
    m_format = VK_FORMAT_UNDEFINED;
}

// ============================================================================
// VkSamplerHandle Implementation
// ============================================================================

VkSamplerHandle::VkSamplerHandle(VkSamplerHandle &&other) noexcept
    : m_device(other.m_device), m_sampler(other.m_sampler)
{
    other.m_sampler = VK_NULL_HANDLE;
    other.m_device = VK_NULL_HANDLE;
}

VkSamplerHandle &VkSamplerHandle::operator=(VkSamplerHandle &&other) noexcept
{
    if (this != &other) {
        Destroy();
        m_device = other.m_device;
        m_sampler = other.m_sampler;
        other.m_sampler = VK_NULL_HANDLE;
        other.m_device = VK_NULL_HANDLE;
    }
    return *this;
}

bool VkSamplerHandle::Create(VkDevice device, VkPhysicalDevice physicalDevice, VkFilter filter,
                             VkSamplerAddressMode addressMode, uint32_t mipLevels, int aniso)
{
    return Create(device, physicalDevice, filter, filter,
                  filter == VK_FILTER_NEAREST ? VK_SAMPLER_MIPMAP_MODE_NEAREST : VK_SAMPLER_MIPMAP_MODE_LINEAR,
                  addressMode, mipLevels, aniso);
}

bool VkSamplerHandle::Create(VkDevice device, VkPhysicalDevice physicalDevice, VkFilter minFilter, VkFilter magFilter,
                             VkSamplerMipmapMode mipFilter, VkSamplerAddressMode addressMode, uint32_t mipLevels,
                             int aniso)
{
    Destroy();

    m_device = device;

    // Get device limits for anisotropy clamping
    VkPhysicalDeviceProperties properties{};
    vkGetPhysicalDeviceProperties(physicalDevice, &properties);

    // -1 selects the device maximum; 0/1 disable anisotropy because a factor
    // of one is equivalent to ordinary filtering.
    bool anisoEnabled = aniso < 0 || aniso > 1;
    float maxAniso = 1.0f;
    if (anisoEnabled) {
        float requested = (aniso < 0) ? properties.limits.maxSamplerAnisotropy : static_cast<float>(aniso);
        maxAniso = (std::min)(requested, properties.limits.maxSamplerAnisotropy);
    }

    VkSamplerCreateInfo samplerInfo{};
    samplerInfo.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
    samplerInfo.magFilter = magFilter;
    samplerInfo.minFilter = minFilter;
    samplerInfo.addressModeU = addressMode;
    samplerInfo.addressModeV = addressMode;
    samplerInfo.addressModeW = addressMode;
    samplerInfo.anisotropyEnable = anisoEnabled ? VK_TRUE : VK_FALSE;
    samplerInfo.maxAnisotropy = maxAniso;
    samplerInfo.borderColor = VK_BORDER_COLOR_INT_OPAQUE_BLACK;
    samplerInfo.unnormalizedCoordinates = VK_FALSE;
    samplerInfo.compareEnable = VK_FALSE;
    samplerInfo.compareOp = VK_COMPARE_OP_ALWAYS;
    samplerInfo.mipmapMode = mipFilter;
    samplerInfo.minLod = 0.0f;
    samplerInfo.maxLod = static_cast<float>(mipLevels > 0 ? mipLevels - 1U : 0U);

    if (vkCreateSampler(device, &samplerInfo, nullptr, &m_sampler) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create sampler");
        return false;
    }

    return true;
}

void VkSamplerHandle::Destroy() noexcept
{
    if (m_sampler != VK_NULL_HANDLE && m_device != VK_NULL_HANDLE) {
        vkDestroySampler(m_device, m_sampler, nullptr);
        m_sampler = VK_NULL_HANDLE;
    }
}

// ============================================================================
// VkTexture Implementation
// ============================================================================

bool VkTexture::CreateFromPixelsImmediate(VmaAllocator allocator, VkDevice device, VkPhysicalDevice physicalDevice,
                                          VkCommandPool cmdPool, VkQueue graphicsQueue, const unsigned char *pixels,
                                          uint32_t width, uint32_t height, VkFormat format, bool generateMipmaps,
                                          VkFilter filter, VkSamplerAddressMode addressMode, int aniso)
{
    // Compute per-pixel byte size based on format
    uint32_t bytesPerPixel = 4; // Default: RGBA8
    if (format == VK_FORMAT_R32G32B32A32_SFLOAT) {
        bytesPerPixel = 16;
    } else if (format == VK_FORMAT_R16G16B16A16_SFLOAT) {
        bytesPerPixel = 8;
    }
    VkDeviceSize imageSize = static_cast<VkDeviceSize>(width) * height * bytesPerPixel;

    // Compute mip levels
    uint32_t mipLevels = 1;
    if (generateMipmaps && width > 1 && height > 1) {
        mipLevels = static_cast<uint32_t>(std::floor(std::log2((std::max)(width, height)))) + 1;
    }

    // Create staging buffer
    VkBufferHandle stagingBuffer;
    if (!stagingBuffer.Create(allocator, device, imageSize, VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
                              VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) {
        return false;
    }

    // Copy pixel data to staging buffer
    stagingBuffer.CopyFrom(pixels, imageSize, 0);

    // Create image — need TRANSFER_SRC for mipmap blit chain
    VkImageUsageFlags usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_SAMPLED_BIT;
    if (mipLevels > 1) {
        usage |= VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    }

    if (!m_image.Create(allocator, device, width, height, format, VK_IMAGE_TILING_OPTIMAL, usage,
                        VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, VK_SAMPLE_COUNT_1_BIT, mipLevels)) {
        return false;
    }

    VkCommandBufferAllocateInfo allocInfo{};
    allocInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocInfo.commandPool = cmdPool;
    allocInfo.commandBufferCount = 1;

    VkCommandBuffer cmdBuffer;
    vkAllocateCommandBuffers(device, &allocInfo, &cmdBuffer);

    VkCommandBufferBeginInfo beginInfo{};
    beginInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    beginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    vkBeginCommandBuffer(cmdBuffer, &beginInfo);

    // Transition ALL mip levels to TRANSFER_DST
    VkImageMemoryBarrier barrier{};
    barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    barrier.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.image = m_image.GetImage();
    barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    barrier.subresourceRange.baseMipLevel = 0;
    barrier.subresourceRange.levelCount = mipLevels;
    barrier.subresourceRange.baseArrayLayer = 0;
    barrier.subresourceRange.layerCount = 1;
    barrier.srcAccessMask = 0;
    barrier.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;

    vkCmdPipelineBarrier(cmdBuffer, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0,
                         nullptr, 1, &barrier);

    // Copy buffer to mip level 0
    VkBufferImageCopy region{};
    region.bufferOffset = 0;
    region.bufferRowLength = 0;
    region.bufferImageHeight = 0;
    region.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    region.imageSubresource.mipLevel = 0;
    region.imageSubresource.baseArrayLayer = 0;
    region.imageSubresource.layerCount = 1;
    region.imageOffset = {0, 0, 0};
    region.imageExtent = {width, height, 1};

    vkCmdCopyBufferToImage(cmdBuffer, stagingBuffer.GetBuffer(), m_image.GetImage(),
                           VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);

    // Generate mipmaps via blit chain
    if (mipLevels > 1) {
        int32_t mipWidth = static_cast<int32_t>(width);
        int32_t mipHeight = static_cast<int32_t>(height);

        for (uint32_t i = 1; i < mipLevels; ++i) {
            // Transition level i-1 from TRANSFER_DST to TRANSFER_SRC
            barrier.subresourceRange.baseMipLevel = i - 1;
            barrier.subresourceRange.levelCount = 1;
            barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
            barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
            barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
            barrier.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;

            vkCmdPipelineBarrier(cmdBuffer, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0,
                                 nullptr, 0, nullptr, 1, &barrier);

            // Blit from level i-1 to level i
            VkImageBlit blit{};
            blit.srcSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            blit.srcSubresource.mipLevel = i - 1;
            blit.srcSubresource.baseArrayLayer = 0;
            blit.srcSubresource.layerCount = 1;
            blit.srcOffsets[0] = {0, 0, 0};
            blit.srcOffsets[1] = {mipWidth, mipHeight, 1};

            int32_t nextWidth = mipWidth > 1 ? mipWidth / 2 : 1;
            int32_t nextHeight = mipHeight > 1 ? mipHeight / 2 : 1;

            blit.dstSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            blit.dstSubresource.mipLevel = i;
            blit.dstSubresource.baseArrayLayer = 0;
            blit.dstSubresource.layerCount = 1;
            blit.dstOffsets[0] = {0, 0, 0};
            blit.dstOffsets[1] = {nextWidth, nextHeight, 1};

            vkCmdBlitImage(cmdBuffer, m_image.GetImage(), VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, m_image.GetImage(),
                           VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &blit, VK_FILTER_LINEAR);

            // Transition level i-1 from TRANSFER_SRC to SHADER_READ_ONLY
            barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
            barrier.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
            barrier.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
            barrier.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;

            vkCmdPipelineBarrier(cmdBuffer, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 0,
                                 nullptr, 0, nullptr, 1, &barrier);

            mipWidth = nextWidth;
            mipHeight = nextHeight;
        }

        // Transition the last mip level from TRANSFER_DST to SHADER_READ_ONLY
        barrier.subresourceRange.baseMipLevel = mipLevels - 1;
        barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        barrier.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        barrier.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;

        vkCmdPipelineBarrier(cmdBuffer, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 0,
                             nullptr, 0, nullptr, 1, &barrier);
    } else {
        // No mipmaps — transition level 0 to SHADER_READ_ONLY
        barrier.subresourceRange.baseMipLevel = 0;
        barrier.subresourceRange.levelCount = 1;
        barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        barrier.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        barrier.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;

        vkCmdPipelineBarrier(cmdBuffer, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 0,
                             nullptr, 0, nullptr, 1, &barrier);
    }

    vkEndCommandBuffer(cmdBuffer);

    // Use a fence instead of vkQueueWaitIdle to avoid stalling ALL GPU work
    VkFenceCreateInfo fenceInfo{};
    fenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    VkFence uploadFence = VK_NULL_HANDLE;
    vkCreateFence(device, &fenceInfo, nullptr, &uploadFence);

    VkSubmitInfo submitInfo{};
    submitInfo.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submitInfo.commandBufferCount = 1;
    submitInfo.pCommandBuffers = &cmdBuffer;

    vkQueueSubmit(graphicsQueue, 1, &submitInfo, uploadFence);
    WaitForFencePumpingEvents(device, uploadFence, "VkTexture::CreateFromPixels upload");

    vkDestroyFence(device, uploadFence, nullptr);
    vkFreeCommandBuffers(device, cmdPool, 1, &cmdBuffer);

    // Create image view with all mip levels
    if (!m_image.CreateView(format, VK_IMAGE_ASPECT_COLOR_BIT, mipLevels)) {
        return false;
    }

    // Create sampler with mip LOD range
    if (!m_sampler.Create(device, physicalDevice, filter, addressMode, mipLevels, aniso)) {
        return false;
    }

    m_residentBytes = 0;
    uint32_t mipWidth = width;
    uint32_t mipHeight = height;
    for (uint32_t level = 0; level < mipLevels; ++level) {
        m_residentBytes += static_cast<VkDeviceSize>(mipWidth) * mipHeight * bytesPerPixel;
        mipWidth = (std::max)(1U, mipWidth / 2U);
        mipHeight = (std::max)(1U, mipHeight / 2U);
    }
    return true;
}

bool VkTexture::CreateSolidColor(VmaAllocator allocator, VkDevice device, VkPhysicalDevice physicalDevice,
                                 VkCommandPool cmdPool, VkQueue graphicsQueue, uint32_t width, uint32_t height,
                                 uint8_t r, uint8_t g, uint8_t b, uint8_t a, VkFormat format)
{
    std::vector<unsigned char> pixels(width * height * 4);
    for (size_t i = 0; i < width * height; ++i) {
        pixels[i * 4 + 0] = r;
        pixels[i * 4 + 1] = g;
        pixels[i * 4 + 2] = b;
        pixels[i * 4 + 3] = a;
    }

    // Solid color textures are typically 1×1 — no mipmaps needed
    return CreateFromPixelsImmediate(allocator, device, physicalDevice, cmdPool, graphicsQueue, pixels.data(), width,
                                     height, format, false);
}

} // namespace vk
} // namespace infernux
