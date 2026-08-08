/**
 * @file VkHandle.cpp
 * @brief Implementation of RAII wrappers for Vulkan handles
 */

#include "VkHandle.h"
#include "AsyncTransferContext.h"
#include <algorithm>
#include <cstring>
#include <shared_mutex>
#include <stdexcept>

namespace infernux
{
namespace vk
{

// ============================================================================
// VkBufferHandle Implementation
// ============================================================================

VkBufferHandle::VkBufferHandle(VkBufferHandle &&other) noexcept
    : m_allocator(other.m_allocator), m_device(other.m_device), m_buffer(other.m_buffer),
      m_allocation(other.m_allocation), m_size(other.m_size), m_mappedPtr(other.m_mappedPtr),
      m_lifetime(std::move(other.m_lifetime))
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
        m_lifetime = std::move(other.m_lifetime);

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
                            VkMemoryPropertyFlags properties, const std::vector<uint32_t> &queueFamilies,
                            std::shared_ptr<rhi::DeviceLifetime> lifetime)
{
    Destroy();

    m_allocator = allocator;
    m_device = device;
    m_size = size;
    m_lifetime = std::move(lifetime);

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
    std::shared_lock<std::shared_mutex> lifetimeLock;
    if (m_lifetime)
        lifetimeLock = std::shared_lock(m_lifetime->gate);
    const bool deviceAlive = !m_lifetime || m_lifetime->alive.load(std::memory_order_acquire);
    if (deviceAlive && m_mappedPtr != nullptr && m_allocation != VK_NULL_HANDLE) {
        vmaUnmapMemory(m_allocator, m_allocation);
    }
    if (deviceAlive && m_buffer != VK_NULL_HANDLE && m_allocator != VK_NULL_HANDLE) {
        vmaDestroyBuffer(m_allocator, m_buffer, m_allocation);
    }
    m_mappedPtr = nullptr;
    m_buffer = VK_NULL_HANDLE;
    m_allocation = VK_NULL_HANDLE;
    m_allocator = VK_NULL_HANDLE;
    m_device = VK_NULL_HANDLE;
    m_size = 0;
    m_lifetime.reset();
}

void *VkBufferHandle::Map()
{
    return Map(0, m_size);
}

void *VkBufferHandle::Map(VkDeviceSize offset, VkDeviceSize size)
{
    std::shared_lock<std::shared_mutex> lifetimeLock;
    if (m_lifetime)
        lifetimeLock = std::shared_lock(m_lifetime->gate);
    if (m_lifetime && !m_lifetime->alive.load(std::memory_order_acquire))
        return nullptr;
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
    std::shared_lock<std::shared_mutex> lifetimeLock;
    if (m_lifetime)
        lifetimeLock = std::shared_lock(m_lifetime->gate);
    if (m_lifetime && !m_lifetime->alive.load(std::memory_order_acquire)) {
        m_mappedPtr = nullptr;
        return;
    }
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
      m_mipLevels(other.m_mipLevels), m_format(other.m_format), m_lifetime(std::move(other.m_lifetime))
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
        m_lifetime = std::move(other.m_lifetime);

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
                           VkSampleCountFlagBits samples, uint32_t mipLevels,
                           std::shared_ptr<rhi::DeviceLifetime> lifetime)
{
    Destroy();

    m_allocator = allocator;
    m_device = device;
    m_width = width;
    m_height = height;
    m_format = format;
    m_mipLevels = mipLevels;
    m_lifetime = std::move(lifetime);

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
                                     VkSampleCountFlagBits samples, uint32_t mipLevels,
                                     std::shared_ptr<rhi::DeviceLifetime> lifetime)
{
    // CONCURRENT sharing requires at least 2 distinct queue families per
    // the Vulkan spec; silently downgrade if the caller can't actually
    // satisfy that (e.g. iGPU with a single queue family). Same fast path
    // as Create() below for that case.
    if (sharedQueueFamilies.size() < 2) {
        return Create(allocator, device, width, height, format, tiling, usage, properties, samples, mipLevels,
                      std::move(lifetime));
    }

    Destroy();

    m_allocator = allocator;
    m_device = device;
    m_width = width;
    m_height = height;
    m_format = format;
    m_mipLevels = mipLevels;
    m_lifetime = std::move(lifetime);

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
    std::shared_lock<std::shared_mutex> lifetimeLock;
    if (m_lifetime)
        lifetimeLock = std::shared_lock(m_lifetime->gate);
    if (m_image == VK_NULL_HANDLE || (m_lifetime && !m_lifetime->alive.load(std::memory_order_acquire))) {
        INXLOG_ERROR("Cannot create view for null image or inactive Vulkan device");
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
    std::shared_lock<std::shared_mutex> lifetimeLock;
    if (m_lifetime)
        lifetimeLock = std::shared_lock(m_lifetime->gate);
    const bool deviceAlive = !m_lifetime || m_lifetime->alive.load(std::memory_order_acquire);
    if (deviceAlive && m_view != VK_NULL_HANDLE && m_device != VK_NULL_HANDLE) {
        vkDestroyImageView(m_device, m_view, nullptr);
    }
    if (deviceAlive && m_image != VK_NULL_HANDLE && m_allocator != VK_NULL_HANDLE) {
        vmaDestroyImage(m_allocator, m_image, m_allocation);
    }
    m_view = VK_NULL_HANDLE;
    m_image = VK_NULL_HANDLE;
    m_allocation = VK_NULL_HANDLE;
    m_allocator = VK_NULL_HANDLE;
    m_device = VK_NULL_HANDLE;
    m_width = 0;
    m_height = 0;
    m_mipLevels = 1;
    m_format = VK_FORMAT_UNDEFINED;
    m_lifetime.reset();
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

} // namespace vk
} // namespace infernux
