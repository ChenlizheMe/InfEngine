/**
 * @file VkDeviceContext.h
 * @brief Vulkan device context management - Instance, Physical Device, Logical Device, Queues
 *
 * This class encapsulates all device-level Vulkan objects with proper RAII management.
 * It provides a clean interface for device initialization and resource management.
 *
 * Architecture Notes:
 * - Single responsibility: Device and queue management only
 * - RAII: All Vulkan objects are automatically cleaned up on destruction
 * - Extensible: DeviceConfig allows customization of required features/extensions
 *
 * Usage:
 *   VkDeviceContext context;
 *   DeviceConfig config;
 *   config.appName = "MyApp";
 *   config.enableValidation = true;
 *   if (!context.Initialize(windowHandle, config)) { // error }
 *   // Use context.GetDevice(), context.GetGraphicsQueue(), etc.
 */

#pragma once

#include "VkTypes.h"
#include <array>
#include <function/renderer/rhi/RhiCapabilities.h>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>
#include <vk_mem_alloc.h>

struct SDL_Window;

namespace infernux
{
namespace vk
{

class VulkanRhiDevice;

/**
 * @brief Manages Vulkan instance, physical device, logical device, and queues
 *
 * This class follows RAII principles - initialization is explicit via Initialize(),
 * but cleanup is automatic via destructor.
 */
class VkDeviceContext
{
  public:
    /// @brief Default constructor - creates uninitialized context
    VkDeviceContext() = default;

    /// @brief Destructor - automatically cleans up all Vulkan objects
    ~VkDeviceContext();

    // Non-copyable, movable
    VkDeviceContext(const VkDeviceContext &) = delete;
    VkDeviceContext &operator=(const VkDeviceContext &) = delete;
    VkDeviceContext(VkDeviceContext &&other) noexcept;
    VkDeviceContext &operator=(VkDeviceContext &&other) noexcept;

    // ========================================================================
    // Initialization
    // ========================================================================

    /**
     * @brief Initialize the Vulkan device context
     *
     * This creates the Vulkan instance, selects a physical device,
     * creates the logical device, and retrieves queue handles.
     *
     * @param window SDL window handle for surface creation
     * @param config Device configuration options
     * @return true if initialization succeeded
     */
    bool Initialize(SDL_Window *window, const DeviceConfig &config = DeviceConfig{});

    /**
     * @brief Initialize only the Vulkan instance (split initialization - step 1)
     *
     * Use this when the surface needs to be created externally (e.g., by SDL).
     * After calling this, create the surface externally and call InitializeDevice().
     *
     * @param config Device configuration options
     * @return true if instance creation succeeded
     */
    bool InitializeInstance(const DeviceConfig &config = DeviceConfig{});

    /**
     * @brief Complete initialization with an external surface (split initialization - step 2)
     *
     * Call this after InitializeInstance() and external surface creation.
     *
     * @param surface Vulkan surface handle (created externally)
     * @param config Device configuration options (should match InitializeInstance)
     * @return true if device creation succeeded
     */
    bool InitializeDevice(VkSurfaceKHR surface, const DeviceConfig &config = DeviceConfig{});

    /**
     * @brief Wait for device to become idle
     *
     * Call this before cleanup or when synchronization is needed.
     * Skipped when shutting down (caller already drained the GPU).
     */
    void WaitIdle() const;
    [[nodiscard]] uint64_t GetWaitIdleCount() const noexcept
    {
        return m_waitIdleCount;
    }

    /// @brief Shutdown coordination — when true, WaitIdle() is a no-op.
    void SetShuttingDown(bool v)
    {
        m_shuttingDown = v;
    }
    bool IsShuttingDown() const
    {
        return m_shuttingDown;
    }

    /**
     * @brief Cleanup all Vulkan objects
     *
     * Called automatically by destructor, but can be called manually
     * for explicit cleanup control.
     */
    void Destroy() noexcept;

    // ========================================================================
    // Accessors
    // ========================================================================

    /// @brief Check if context is initialized and valid
    [[nodiscard]] bool IsValid() const
    {
        return m_device != VK_NULL_HANDLE;
    }

    /// @brief Get Vulkan instance handle
    [[nodiscard]] VkInstance GetInstance() const
    {
        return m_instance;
    }

    /// @brief Get physical device handle
    [[nodiscard]] VkPhysicalDevice GetPhysicalDevice() const
    {
        return m_physicalDevice;
    }

    /// @brief Get logical device handle
    [[nodiscard]] VkDevice GetDevice() const
    {
        return m_device;
    }

    /// @brief Get window surface handle
    [[nodiscard]] VkSurfaceKHR GetSurface() const
    {
        return m_surface;
    }

    /// @brief Get graphics queue handle
    [[nodiscard]] VkQueue GetGraphicsQueue() const
    {
        return m_graphicsQueue;
    }

    /// @brief Get present queue handle
    [[nodiscard]] VkQueue GetPresentQueue() const
    {
        return m_presentQueue;
    }

    /// @brief Get the transfer queue handle.
    /// On GPUs with a dedicated transfer-only queue family this returns
    /// a queue from that family (parallel with graphics). On GPUs without
    /// dedicated transfer this returns the graphics queue itself, so call
    /// sites can always dispatch uploads through here without branching.
    /// Use HasDedicatedTransferQueue() to know whether async submission is
    /// actually parallel.
    [[nodiscard]] VkQueue GetTransferQueue() const
    {
        return m_transferQueue;
    }

    /// @brief True iff the transfer queue is on a different family than
    /// graphics — i.e. submissions to it run truly in parallel with the
    /// main 3D queue and do NOT contend for graphics throughput.
    [[nodiscard]] bool HasDedicatedTransferQueue() const
    {
        return m_hasDedicatedTransferQueue;
    }

    /// @brief Get queue family indices
    [[nodiscard]] const QueueFamilyIndices &GetQueueIndices() const
    {
        return m_queueIndices;
    }

    /// @brief Get physical device properties
    [[nodiscard]] const VkPhysicalDeviceProperties &GetDeviceProperties() const
    {
        return m_deviceProperties;
    }

    /// @brief Get physical device features
    [[nodiscard]] const VkPhysicalDeviceFeatures &GetDeviceFeatures() const
    {
        return m_deviceFeatures;
    }

    [[nodiscard]] const rhi::DeviceCapabilities &GetCapabilities() const noexcept
    {
        return m_capabilities;
    }

    [[nodiscard]] VulkanRhiDevice &GetRhiDevice() noexcept
    {
        return *m_rhiDevice;
    }

    [[nodiscard]] const VulkanRhiDevice &GetRhiDevice() const noexcept
    {
        return *m_rhiDevice;
    }

    /// @brief Whether descriptor-indexing UPDATE_AFTER_BIND was enabled at
    /// device creation time. Callers should branch on this before opting in
    /// to non-stalling descriptor updates so headless/legacy GPUs continue
    /// to fall back to the GPU-drain path.
    [[nodiscard]] bool IsDescriptorIndexingEnabled() const
    {
        return m_descriptorIndexingEnabled;
    }

    /// @brief Whether timeline semaphores were enabled at device creation —
    /// reserved for future async-transfer integrations.
    [[nodiscard]] bool IsTimelineSemaphoreEnabled() const
    {
        return m_timelineSemaphoreEnabled;
    }

    /// @brief Get the VMA allocator handle
    [[nodiscard]] VmaAllocator GetVmaAllocator() const
    {
        return m_vmaAllocator;
    }

    // ========================================================================
    // Utility Methods
    // ========================================================================

    /**
     * @brief Query swapchain support details for the physical device
     * @return Swapchain capabilities, formats, and present modes
     */
    [[nodiscard]] SwapchainSupportDetails QuerySwapchainSupport() const;

    /**
     * @brief Find a supported format from a list of candidates
     *
     * @param candidates List of formats to try (in order of preference)
     * @param tiling Image tiling mode
     * @param features Required format features
     * @return VK_FORMAT_UNDEFINED if no suitable format found
     */
    [[nodiscard]] VkFormat FindSupportedFormat(const std::vector<VkFormat> &candidates, VkImageTiling tiling,
                                               VkFormatFeatureFlags features) const;

    /**
     * @brief Find a suitable depth format
     * @return Best available depth format, or VK_FORMAT_UNDEFINED
     */
    [[nodiscard]] VkFormat FindDepthFormat() const;

    /**
     * @brief Find a depth format suitable for shadow maps (must support both attachment and sampling).
     * @return Best available depth format with sampled image support, or VK_FORMAT_UNDEFINED
     */
    [[nodiscard]] VkFormat FindShadowMapDepthFormat() const;

    /**
     * @brief Check if a format supports stencil component
     * @param format Format to check
     * @return true if format has stencil component
     */
    [[nodiscard]] static bool HasStencilComponent(VkFormat format);

  private:
    // ========================================================================
    // Internal Initialization Methods
    // ========================================================================

    /// @brief Create Vulkan instance with required extensions
    bool CreateInstance(const DeviceConfig &config);

    /// @brief Setup debug messenger (if validation enabled)
    bool SetupDebugMessenger();

    /// @brief Create window surface via SDL
    bool CreateSurface(SDL_Window *window);

    /// @brief Select the best physical device
    bool PickPhysicalDevice(const DeviceConfig &config);

    /// @brief Create logical device and retrieve queues
    bool CreateLogicalDevice(const DeviceConfig &config);

    /// Build the backend-neutral capability snapshot after device features
    /// and queue selection have been finalized.
    void BuildCapabilities();

    /// @brief Find queue families for a physical device
    QueueFamilyIndices FindQueueFamilies(VkPhysicalDevice device) const;

    /// @brief Check if a physical device meets requirements
    bool IsDeviceSuitable(VkPhysicalDevice device, const DeviceConfig &config) const;

    /// @brief Rate a physical device for selection (higher is better)
    int RateDeviceSuitability(VkPhysicalDevice device) const;

    /// @brief Check if device supports required extensions
    bool CheckDeviceExtensionSupport(VkPhysicalDevice device, const std::vector<const char *> &extensions) const;

    /// @brief Get required instance extensions
    std::vector<const char *> GetRequiredExtensions(bool enableValidation) const;

    /// @brief Check validation layer support
    bool CheckValidationLayerSupport() const;

  private:
    // ========================================================================
    // Vulkan Objects (RAII - destroyed in Destroy())
    // ========================================================================

    VkInstance m_instance = VK_NULL_HANDLE;
    VkDebugUtilsMessengerEXT m_debugMessenger = VK_NULL_HANDLE;
    VkSurfaceKHR m_surface = VK_NULL_HANDLE;
    VkPhysicalDevice m_physicalDevice = VK_NULL_HANDLE; // Not destroyed - owned by instance
    VkDevice m_device = VK_NULL_HANDLE;

    // VMA allocator (destroyed before device)
    VmaAllocator m_vmaAllocator = VK_NULL_HANDLE;

    // Queue handles (not destroyed - owned by device)
    VkQueue m_graphicsQueue = VK_NULL_HANDLE;
    VkQueue m_presentQueue = VK_NULL_HANDLE;
    VkQueue m_transferQueue = VK_NULL_HANDLE; ///< Either dedicated DMA queue or alias for m_graphicsQueue
    bool m_hasDedicatedTransferQueue = false; ///< True iff transferFamily != graphicsFamily

    // ========================================================================
    // Device Information Cache
    // ========================================================================

    QueueFamilyIndices m_queueIndices{};
    VkPhysicalDeviceProperties m_deviceProperties{};
    VkPhysicalDeviceFeatures m_deviceFeatures{};
    rhi::DeviceCapabilities m_capabilities{};
    std::unique_ptr<VulkanRhiDevice> m_rhiDevice;

    // Vulkan 1.2 capability flags resolved at device creation. Callers gate
    // optional fast paths on these so the engine still runs correctly on
    // older / restricted GPUs that do not advertise the features.
    bool m_descriptorIndexingEnabled = false;
    bool m_timelineSemaphoreEnabled = false;

    // ========================================================================
    // Configuration
    // ========================================================================

    bool m_validationEnabled = false;
    mutable bool m_shuttingDown = false;
    mutable uint64_t m_waitIdleCount = 0;

    static constexpr std::array<const char *, 1> VALIDATION_LAYERS = {"VK_LAYER_KHRONOS_validation"};

    static constexpr std::array<const char *, 1> DEVICE_EXTENSIONS = {VK_KHR_SWAPCHAIN_EXTENSION_NAME};
};

} // namespace vk
} // namespace infernux
