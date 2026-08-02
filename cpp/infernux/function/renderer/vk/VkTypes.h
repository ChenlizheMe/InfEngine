/**
 * @file VkTypes.h
 * @brief Vulkan type definitions, forward declarations, and common structures
 *
 * This file contains:
 * - Forward declarations for Vulkan types
 * - Common structures used across the Vulkan subsystem
 * - Smart pointer type aliases for RAII wrappers
 *
 * @note Include this file instead of vulkan.h when only type declarations are needed
 */

#pragma once

#include <core/config/InxPlatform.h>
#ifdef INX_PLATFORM_WINDOWS
#define VK_USE_PLATFORM_WIN32_KHR
#elif defined(INX_PLATFORM_MACOS)
#define VK_USE_PLATFORM_METAL_EXT
#endif

#include <vulkan/vulkan.h>

#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux
{
namespace vk
{

// ============================================================================
// Forward Declarations
// ============================================================================

class VkDeviceContext;
class VkSwapchainManager;
class VkPipelineManager;
class VkResourceManager;
class RenderGraph;
class RenderPass;

// ============================================================================
// Configuration Structures
// ============================================================================

/**
 * @brief Queue family indices for different operation types
 */
struct QueueFamilyIndices
{
    std::optional<uint32_t> graphicsFamily; ///< Graphics queue family index
    std::optional<uint32_t> computeFamily;  ///< Compute queue family index
    std::optional<uint32_t> presentFamily;  ///< Present queue family index
    std::optional<uint32_t> transferFamily; ///< Transfer queue family index
    uint32_t graphicsQueueIndex = 0;
    uint32_t computeQueueIndex = 0;

    /**
     * @brief Check if all required queue families are available
     * @return true if all queue families have valid indices
     */
    [[nodiscard]] bool IsComplete() const noexcept
    {
        return graphicsFamily.has_value() && computeFamily.has_value() && presentFamily.has_value() &&
               transferFamily.has_value();
    }

    /**
     * @brief Get unique queue family indices
     * @return Vector of unique queue family indices
     */
    [[nodiscard]] std::vector<uint32_t> GetUniqueIndices() const;
};

/**
 * @brief Swapchain support details
 */
struct SwapchainSupportDetails
{
    VkSurfaceCapabilitiesKHR capabilities{};    ///< Surface capabilities
    std::vector<VkSurfaceFormatKHR> formats;    ///< Available surface formats
    std::vector<VkPresentModeKHR> presentModes; ///< Available present modes

    /**
     * @brief Check if swapchain is adequate for use
     * @return true if at least one format and present mode are available
     */
    [[nodiscard]] bool IsAdequate() const noexcept
    {
        return !formats.empty() && !presentModes.empty();
    }
};

/**
 * @brief Queue configuration for device creation
 */
struct QueueConfig
{
    uint32_t graphicsQueueCount = 1; ///< Number of graphics queues to create
    uint32_t computeQueueCount = 1;  ///< Number of compute queues to create
    uint32_t presentQueueCount = 1;  ///< Number of present queues to create
    uint32_t transferQueueCount = 1; ///< Number of transfer queues to create
};

/**
 * @brief Device initialization configuration
 */
struct DeviceConfig
{
    const char *appName = "Infernux App"; ///< Application name
    uint32_t appVersionMajor = 1;         ///< Application major version
    uint32_t appVersionMinor = 0;         ///< Application minor version
    uint32_t appVersionPatch = 0;         ///< Application patch version
    const char *engineName = "Infernux";  ///< Engine name
    uint32_t engineVersionMajor = 0;      ///< Engine major version
    uint32_t engineVersionMinor = 1;      ///< Engine minor version
    uint32_t engineVersionPatch = 0;      ///< Engine patch version
    bool enableValidationLayers =
        false; ///< Set true at startup when built with INFERNUX_VULKAN_VALIDATION_LAYERS (Debug / RelWithDebInfo)
    QueueConfig queueConfig; ///< Queue configuration
};

/**
 * @brief Swapchain configuration
 */
struct SwapchainConfig
{
    uint32_t preferredImageCount = 3; ///< Preferred number of swapchain images
    VkPresentModeKHR preferredPresentMode = VK_PRESENT_MODE_MAILBOX_KHR;
    VkFormat preferredFormat = VK_FORMAT_B8G8R8A8_UNORM;
    VkColorSpaceKHR preferredColorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
};

// ============================================================================
// Deletion Queue for Deferred Resource Cleanup
// ============================================================================

/**
 * @brief Deferred deletion queue for Vulkan resources
 *
 * Collects cleanup functions and executes them in LIFO order during destruction.
 * This pattern ensures proper cleanup order for dependent resources.
 *
 * Usage:
 * @code
 * DeletionQueue queue;
 * queue.Push([device, buffer]() { vkDestroyBuffer(device, buffer, nullptr); });
 * // ... later, or in destructor
 * queue.Flush();
 * @endcode
 */
class DeletionQueue
{
  public:
    using DeletorFunc = std::function<void()>;

    DeletionQueue() = default;
    ~DeletionQueue()
    {
        Flush();
    }

    // Non-copyable, moveable
    DeletionQueue(const DeletionQueue &) = delete;
    DeletionQueue &operator=(const DeletionQueue &) = delete;
    DeletionQueue(DeletionQueue &&) noexcept = default;
    DeletionQueue &operator=(DeletionQueue &&) noexcept = default;

    /**
     * @brief Add a deletion function to the queue
     * @param func The cleanup function to execute later
     */
    void Push(DeletorFunc &&func)
    {
        m_deletors.push_back(std::move(func));
    }

    /**
     * @brief Execute all deletion functions in reverse order and clear the queue
     */
    void Flush()
    {
        for (auto it = m_deletors.rbegin(); it != m_deletors.rend(); ++it) {
            (*it)();
        }
        m_deletors.clear();
    }

    /**
     * @brief Check if the queue is empty
     * @return true if no deletors are queued
     */
    [[nodiscard]] bool Empty() const noexcept
    {
        return m_deletors.empty();
    }

    /**
     * @brief Get the number of queued deletors
     * @return Number of deletion functions in the queue
     */
    [[nodiscard]] size_t Size() const noexcept
    {
        return m_deletors.size();
    }

  private:
    std::vector<DeletorFunc> m_deletors;
};

// ============================================================================
// Utility Functions
// ============================================================================

/**
 * @brief Convert VkResult to human-readable string
 * @param result The VkResult value
 * @return String representation of the result
 */
[[nodiscard]] const char *VkResultToString(VkResult result);

/**
 * @brief Check if a VkResult indicates success
 * @param result The VkResult to check
 * @return true if result is VK_SUCCESS
 */
[[nodiscard]] inline bool VkSucceeded(VkResult result) noexcept
{
    return result == VK_SUCCESS;
}

/**
 * @brief Check if a VkResult indicates failure
 * @param result The VkResult to check
 * @return true if result is not VK_SUCCESS
 */
[[nodiscard]] inline bool VkFailed(VkResult result) noexcept
{
    return result != VK_SUCCESS;
}

} // namespace vk
} // namespace infernux
