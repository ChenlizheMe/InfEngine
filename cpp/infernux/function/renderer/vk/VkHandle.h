/**
 * @file VkHandle.h
 * @brief RAII wrappers for Vulkan handles
 *
 * Provides type-safe, RAII-managed wrappers for Vulkan handles.
 * These wrappers ensure proper cleanup of Vulkan resources when they go out of scope.
 *
 * Design principles:
 * - Zero overhead when optimized (inline destructor calls)
 * - Move-only semantics (no accidental copies)
 * - Explicit ownership transfer
 * - Compatible with standard containers
 */

#pragma once

#include "../rhi/RhiDevice.h"
#include "VkTypes.h"
#include <core/log/InxLog.h>
#include <memory>
#include <vector>
#include <vk_mem_alloc.h>

namespace infernux
{
namespace vk
{

// ============================================================================
// VkHandle - Generic RAII wrapper for Vulkan handles
// ============================================================================

/**
 * @brief RAII wrapper for Vulkan handles that require a VkDevice for destruction
 *
 * @tparam HandleType The Vulkan handle type (e.g., VkBuffer, VkImage)
 * @tparam DestroyFunc The destruction function signature
 *
 * Example usage:
 * @code
 * VkHandle<VkBuffer> buffer;
 * buffer.Create(device, vkCreateBuffer, vkDestroyBuffer, &createInfo);
 * // buffer is automatically destroyed when it goes out of scope
 * @endcode
 */
template <typename HandleType> class VkHandle
{
  public:
    using DestroyFunc = void (*)(VkDevice, HandleType, const VkAllocationCallbacks *);

    VkHandle() = default;

    /**
     * @brief Construct with an existing handle
     * @param device The Vulkan device that owns this handle
     * @param handle The Vulkan handle to manage
     * @param destroyFunc The function to call for destruction
     */
    VkHandle(VkDevice device, HandleType handle, DestroyFunc destroyFunc) noexcept
        : m_device(device), m_handle(handle), m_destroyFunc(destroyFunc)
    {
    }

    ~VkHandle()
    {
        Destroy();
    }

    // Move-only semantics
    VkHandle(const VkHandle &) = delete;
    VkHandle &operator=(const VkHandle &) = delete;

    VkHandle(VkHandle &&other) noexcept
        : m_device(other.m_device), m_handle(other.m_handle), m_destroyFunc(other.m_destroyFunc)
    {
        other.m_handle = VK_NULL_HANDLE;
        other.m_device = VK_NULL_HANDLE;
    }

    VkHandle &operator=(VkHandle &&other) noexcept
    {
        if (this != &other) {
            Destroy();
            m_device = other.m_device;
            m_handle = other.m_handle;
            m_destroyFunc = other.m_destroyFunc;
            other.m_handle = VK_NULL_HANDLE;
            other.m_device = VK_NULL_HANDLE;
        }
        return *this;
    }

    /**
     * @brief Set the handle and destruction function
     * @param device The Vulkan device
     * @param handle The handle to manage
     * @param destroyFunc The destruction function
     */
    void Set(VkDevice device, HandleType handle, DestroyFunc destroyFunc) noexcept
    {
        Destroy();
        m_device = device;
        m_handle = handle;
        m_destroyFunc = destroyFunc;
    }

    /**
     * @brief Destroy the managed handle
     */
    void Destroy() noexcept
    {
        if (m_handle != VK_NULL_HANDLE && m_device != VK_NULL_HANDLE && m_destroyFunc != nullptr) {
            m_destroyFunc(m_device, m_handle, nullptr);
            m_handle = VK_NULL_HANDLE;
        }
    }

    /**
     * @brief Release ownership of the handle without destroying it
     * @return The raw handle
     */
    [[nodiscard]] HandleType Release() noexcept
    {
        HandleType temp = m_handle;
        m_handle = VK_NULL_HANDLE;
        return temp;
    }

    /**
     * @brief Get the raw handle
     * @return The managed Vulkan handle
     */
    [[nodiscard]] HandleType Get() const noexcept
    {
        return m_handle;
    }

    /**
     * @brief Get pointer to the handle (for Vulkan creation functions)
     * @return Pointer to the handle storage
     */
    [[nodiscard]] HandleType *GetAddressOf() noexcept
    {
        return &m_handle;
    }

    /**
     * @brief Check if the handle is valid
     * @return true if the handle is not VK_NULL_HANDLE
     */
    [[nodiscard]] bool IsValid() const noexcept
    {
        return m_handle != VK_NULL_HANDLE;
    }

    /**
     * @brief Implicit conversion to the raw handle type
     */
    operator HandleType() const noexcept
    {
        return m_handle;
    }

    /**
     * @brief Boolean conversion for validity check
     */
    explicit operator bool() const noexcept
    {
        return IsValid();
    }

  private:
    VkDevice m_device = VK_NULL_HANDLE;
    HandleType m_handle = VK_NULL_HANDLE;
    DestroyFunc m_destroyFunc = nullptr;
};

// ============================================================================
// VkBuffer RAII Wrapper
// ============================================================================

/**
 * @brief RAII wrapper for VkBuffer with associated memory
 *
 * Manages both the buffer and its backing memory allocation.
 * Ensures proper destruction order (buffer before memory).
 */
class VkBufferHandle
{
  public:
    VkBufferHandle() = default;
    ~VkBufferHandle()
    {
        Destroy();
    }

    // Move-only
    VkBufferHandle(const VkBufferHandle &) = delete;
    VkBufferHandle &operator=(const VkBufferHandle &) = delete;
    VkBufferHandle(VkBufferHandle &&other) noexcept;
    VkBufferHandle &operator=(VkBufferHandle &&other) noexcept;

    /**
     * @brief Create a buffer with VMA memory allocation
     * @param allocator VMA allocator
     * @param device The Vulkan device (for view creation etc.)
     * @param size Buffer size in bytes
     * @param usage Buffer usage flags
     * @param properties Memory property flags
     * @return true if creation succeeded
     */
    bool Create(VmaAllocator allocator, VkDevice device, VkDeviceSize size, VkBufferUsageFlags usage,
                VkMemoryPropertyFlags properties, const std::vector<uint32_t> &queueFamilies = {},
                std::shared_ptr<rhi::DeviceLifetime> lifetime = {});

    /**
     * @brief Destroy the buffer and free memory
     */
    void Destroy() noexcept;

    /**
     * @brief Map the buffer memory for CPU access
     * @return Pointer to mapped memory, or nullptr on failure
     */
    [[nodiscard]] void *Map();

    /**
     * @brief Map a range of buffer memory
     * @param offset Offset from start of buffer
     * @param size Size to map (or VK_WHOLE_SIZE)
     * @return Pointer to mapped memory, or nullptr on failure
     */
    [[nodiscard]] void *Map(VkDeviceSize offset, VkDeviceSize size);

    /**
     * @brief Unmap previously mapped memory
     */
    void Unmap() noexcept;

    /**
     * @brief Copy data to the buffer (must be host-visible)
     * @param data Source data pointer
     * @param size Size of data to copy
     * @param offset Offset in buffer
     */
    void CopyFrom(const void *data, VkDeviceSize size, VkDeviceSize offset = 0);

    // Accessors
    [[nodiscard]] VkBuffer GetBuffer() const noexcept
    {
        return m_buffer;
    }
    [[nodiscard]] VmaAllocation GetAllocation() const noexcept
    {
        return m_allocation;
    }
    [[nodiscard]] VkDeviceSize GetSize() const noexcept
    {
        return m_size;
    }
    [[nodiscard]] bool IsValid() const noexcept
    {
        return m_buffer != VK_NULL_HANDLE && (!m_lifetime || m_lifetime->alive.load(std::memory_order_acquire));
    }
    [[nodiscard]] bool IsMapped() const noexcept
    {
        return m_mappedPtr != nullptr;
    }
    [[nodiscard]] void *GetMappedPtr() const noexcept
    {
        return m_mappedPtr;
    }

    operator VkBuffer() const noexcept
    {
        return m_buffer;
    }
    explicit operator bool() const noexcept
    {
        return IsValid();
    }

  private:
    VmaAllocator m_allocator = VK_NULL_HANDLE;
    VkDevice m_device = VK_NULL_HANDLE;
    VkBuffer m_buffer = VK_NULL_HANDLE;
    VmaAllocation m_allocation = VK_NULL_HANDLE;
    VkDeviceSize m_size = 0;
    void *m_mappedPtr = nullptr;
    std::shared_ptr<rhi::DeviceLifetime> m_lifetime;
};

// ============================================================================
// VkImage RAII Wrapper
// ============================================================================

/**
 * @brief RAII wrapper for VkImage with associated memory and optional view
 *
 * Manages image, backing memory, and optionally an image view.
 * Ensures proper destruction order.
 */
class VkImageHandle
{
  public:
    VkImageHandle() = default;
    ~VkImageHandle()
    {
        Destroy();
    }

    // Move-only
    VkImageHandle(const VkImageHandle &) = delete;
    VkImageHandle &operator=(const VkImageHandle &) = delete;
    VkImageHandle(VkImageHandle &&other) noexcept;
    VkImageHandle &operator=(VkImageHandle &&other) noexcept;

    /**
     * @brief Create an image with VMA memory allocation
     * @param allocator VMA allocator
     * @param device The Vulkan device
     * @param width Image width
     * @param height Image height
     * @param format Image format
     * @param tiling Image tiling mode
     * @param usage Image usage flags
     * @param properties Memory property flags
     * @return true if creation succeeded
     */
    bool Create(VmaAllocator allocator, VkDevice device, uint32_t width, uint32_t height, VkFormat format,
                VkImageTiling tiling, VkImageUsageFlags usage, VkMemoryPropertyFlags properties,
                VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT, uint32_t mipLevels = 1,
                std::shared_ptr<rhi::DeviceLifetime> lifetime = {});

    /**
     * @brief Create an image with explicit cross-queue concurrent sharing.
     *
     * Identical to Create() above except the resulting VkImage uses
     * VK_SHARING_MODE_CONCURRENT across @p sharedQueueFamilies, which
     * lets transfer-queue uploads be sampled from the graphics queue
     * without an explicit queue family ownership transfer barrier. Pays
     * a small driver-level performance cost (variable by vendor) in
     * exchange for that simplicity. Recommended ONLY for resources that
     * actually cross queues — purely graphics-side render targets should
     * stick to the EXCLUSIVE overload.
     *
     * Pre: @p sharedQueueFamilies must contain at least 2 distinct
     * family indices; otherwise CONCURRENT is not legal and the call
     * silently downgrades to EXCLUSIVE.
     */
    bool CreateConcurrent(VmaAllocator allocator, VkDevice device, uint32_t width, uint32_t height, VkFormat format,
                          VkImageTiling tiling, VkImageUsageFlags usage, VkMemoryPropertyFlags properties,
                          const std::vector<uint32_t> &sharedQueueFamilies,
                          VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT, uint32_t mipLevels = 1,
                          std::shared_ptr<rhi::DeviceLifetime> lifetime = {});

    /**
     * @brief Create an image view for this image
     * @param format View format
     * @param aspectFlags Image aspect flags
     * @param mipLevels Number of mip levels
     * @return true if creation succeeded
     */
    bool CreateView(VkFormat format, VkImageAspectFlags aspectFlags, uint32_t mipLevels = 1);

    /**
     * @brief Destroy the image, view, and memory
     */
    void Destroy() noexcept;

    // Accessors
    [[nodiscard]] VkImage GetImage() const noexcept
    {
        return m_image;
    }
    [[nodiscard]] VkImageView GetView() const noexcept
    {
        return m_view;
    }
    [[nodiscard]] VmaAllocation GetAllocation() const noexcept
    {
        return m_allocation;
    }
    [[nodiscard]] uint32_t GetWidth() const noexcept
    {
        return m_width;
    }
    [[nodiscard]] uint32_t GetHeight() const noexcept
    {
        return m_height;
    }
    [[nodiscard]] VkFormat GetFormat() const noexcept
    {
        return m_format;
    }
    [[nodiscard]] uint32_t GetMipLevels() const noexcept
    {
        return m_mipLevels;
    }
    [[nodiscard]] bool IsValid() const noexcept
    {
        return m_image != VK_NULL_HANDLE && (!m_lifetime || m_lifetime->alive.load(std::memory_order_acquire));
    }
    [[nodiscard]] bool HasView() const noexcept
    {
        return m_view != VK_NULL_HANDLE;
    }

    operator VkImage() const noexcept
    {
        return m_image;
    }
    explicit operator bool() const noexcept
    {
        return IsValid();
    }

  private:
    VmaAllocator m_allocator = VK_NULL_HANDLE;
    VkDevice m_device = VK_NULL_HANDLE;
    VkImage m_image = VK_NULL_HANDLE;
    VkImageView m_view = VK_NULL_HANDLE;
    VmaAllocation m_allocation = VK_NULL_HANDLE;
    uint32_t m_width = 0;
    uint32_t m_height = 0;
    uint32_t m_mipLevels = 1;
    VkFormat m_format = VK_FORMAT_UNDEFINED;
    std::shared_ptr<rhi::DeviceLifetime> m_lifetime;
};

// ============================================================================
// VkSampler RAII Wrapper
// ============================================================================

/**
 * @brief RAII wrapper for VkSampler
 */
class VkSamplerHandle
{
  public:
    VkSamplerHandle() = default;
    ~VkSamplerHandle()
    {
        Destroy();
    }

    // Move-only
    VkSamplerHandle(const VkSamplerHandle &) = delete;
    VkSamplerHandle &operator=(const VkSamplerHandle &) = delete;
    VkSamplerHandle(VkSamplerHandle &&other) noexcept;
    VkSamplerHandle &operator=(VkSamplerHandle &&other) noexcept;

    /**
     * @brief Create a sampler with configurable settings
     * @param device The Vulkan device
     * @param physicalDevice Physical device for max anisotropy query
     * @param filter Magnification and minification filter
     * @param addressMode Address mode for UVW
     * @param mipLevels Number of mip levels (controls maxLod)
     * @param aniso Anisotropy level (0 = disabled, 1..16 clamped to device max)
     * @return true if creation succeeded
     */
    bool Create(VkDevice device, VkPhysicalDevice physicalDevice, VkFilter filter = VK_FILTER_LINEAR,
                VkSamplerAddressMode addressMode = VK_SAMPLER_ADDRESS_MODE_REPEAT, uint32_t mipLevels = 1,
                int aniso = -1);
    bool Create(VkDevice device, VkPhysicalDevice physicalDevice, VkFilter minFilter, VkFilter magFilter,
                VkSamplerMipmapMode mipFilter, VkSamplerAddressMode addressMode, uint32_t mipLevels, int aniso);

    /**
     * @brief Destroy the sampler
     */
    void Destroy() noexcept;

    [[nodiscard]] VkSampler Get() const noexcept
    {
        return m_sampler;
    }
    [[nodiscard]] bool IsValid() const noexcept
    {
        return m_sampler != VK_NULL_HANDLE;
    }

    operator VkSampler() const noexcept
    {
        return m_sampler;
    }
    explicit operator bool() const noexcept
    {
        return IsValid();
    }

  private:
    VkDevice m_device = VK_NULL_HANDLE;
    VkSampler m_sampler = VK_NULL_HANDLE;
};

// ============================================================================
// Utility Functions
// ============================================================================

} // namespace vk
} // namespace infernux
