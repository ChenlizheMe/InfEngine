/**
 * @file VkSwapchainManager.cpp
 * @brief Implementation of Vulkan swapchain management
 */

// Prevent Windows min/max macros from conflicting with std::min/std::max
// (handled globally by core/config/InxPlatform.h via InxPath.h)

#include "VkSwapchainManager.h"
#include "VkDeviceContext.h"
#include <SDL3/SDL.h>
#include <core/error/InxError.h>

#include <algorithm>
#include <limits>

namespace infernux
{
namespace vk
{

namespace
{

void WaitForFencePumpingEvents(VkDevice device, VkFence fence)
{
    constexpr uint64_t kPollTimeoutNs = 50'000'000; // 50 ms
    while (true) {
        VkResult result = vkWaitForFences(device, 1, &fence, VK_TRUE, kPollTimeoutNs);
        if (result == VK_SUCCESS) {
            return;
        }
        if (result != VK_TIMEOUT) {
            static int s_fenceFailLogs = 0;
            if (s_fenceFailLogs < 3) {
                INXLOG_ERROR("vkWaitForFences failed while waiting for frame fence: ", VkResultToString(result));
            } else if (s_fenceFailLogs == 3) {
                INXLOG_ERROR("Further frame-fence wait failures suppressed (device likely lost)");
            }
            ++s_fenceFailLogs;
            return;
        }
        SDL_PumpEvents();
    }
}

} // namespace

// ============================================================================
// Constructor / Destructor / Move
// ============================================================================

VkSwapchainManager::~VkSwapchainManager()
{
    Destroy();
}

VkSwapchainManager::VkSwapchainManager(VkSwapchainManager &&other) noexcept
    : m_device(other.m_device), m_swapchain(other.m_swapchain), m_presentQueue(other.m_presentQueue),
      m_images(std::move(other.m_images)), m_imageViews(std::move(other.m_imageViews)),
      m_imageFormat(other.m_imageFormat), m_extent(other.m_extent), m_syncObjects(std::move(other.m_syncObjects)),
      m_renderFinishedSemaphores(std::move(other.m_renderFinishedSemaphores)), m_currentFrame(other.m_currentFrame)
{
    other.m_device = VK_NULL_HANDLE;
    other.m_swapchain = VK_NULL_HANDLE;
    other.m_presentQueue = VK_NULL_HANDLE;
    other.m_imageFormat = VK_FORMAT_UNDEFINED;
    other.m_extent = {};
    other.m_currentFrame = 0;
}

VkSwapchainManager &VkSwapchainManager::operator=(VkSwapchainManager &&other) noexcept
{
    if (this != &other) {
        Destroy();

        m_device = other.m_device;
        m_swapchain = other.m_swapchain;
        m_presentQueue = other.m_presentQueue;
        m_images = std::move(other.m_images);
        m_imageViews = std::move(other.m_imageViews);
        m_imageFormat = other.m_imageFormat;
        m_extent = other.m_extent;
        m_syncObjects = std::move(other.m_syncObjects);
        m_renderFinishedSemaphores = std::move(other.m_renderFinishedSemaphores);
        m_currentFrame = other.m_currentFrame;

        other.m_device = VK_NULL_HANDLE;
        other.m_swapchain = VK_NULL_HANDLE;
        other.m_presentQueue = VK_NULL_HANDLE;
        other.m_imageFormat = VK_FORMAT_UNDEFINED;
        other.m_extent = {};
        other.m_currentFrame = 0;
    }
    return *this;
}

// ============================================================================
// Lifecycle Management
// ============================================================================

bool VkSwapchainManager::Create(const VkDeviceContext &context, uint32_t width, uint32_t height)
{
    m_device = context.GetDevice();
    m_presentQueue = context.GetPresentQueue();

    if (!CreateSwapchainCore(context, width, height, VK_NULL_HANDLE))
        return false;

    // Create sync objects (only on initial creation)
    if (!CreateSyncObjects()) {
        return false;
    }

    INXLOG_INFO("Swapchain created: ", m_extent.width, "x", m_extent.height, ", ", m_images.size(), " images, format ",
                static_cast<int>(m_imageFormat));

    return true;
}

bool VkSwapchainManager::Recreate(const VkDeviceContext &context, uint32_t width, uint32_t height)
{
    // Wait for device to be idle
    context.WaitIdle();

    // Cleanup old swapchain (but keep sync objects)
    CleanupSwapchain();

    // Handle minimized window
    SwapchainSupportDetails swapchainSupport = context.QuerySwapchainSupport();
    if (swapchainSupport.capabilities.currentExtent.width == 0 ||
        swapchainSupport.capabilities.currentExtent.height == 0) {
        INXLOG_WARN("Swapchain recreation skipped: zero extent (minimized window?)");
        return false;
    }

    if (!CreateSwapchainCore(context, width, height, VK_NULL_HANDLE))
        return false;

    if (!CreateRenderFinishedSemaphores()) {
        return false;
    }

    return true;
}

bool VkSwapchainManager::CreateSwapchainCore(const VkDeviceContext &context, uint32_t width, uint32_t height,
                                             VkSwapchainKHR oldSwapchain)
{
    // Query swapchain support
    SwapchainSupportDetails swapchainSupport = context.QuerySwapchainSupport();

    // Choose optimal settings
    VkSurfaceFormatKHR surfaceFormat = ChooseSurfaceFormat(swapchainSupport.formats);
    VkPresentModeKHR presentMode = ChoosePresentMode(swapchainSupport.presentModes);
    VkExtent2D extent = ChooseExtent(swapchainSupport.capabilities, width, height);

    // Choose image count (prefer triple buffering)
    uint32_t imageCount = swapchainSupport.capabilities.minImageCount + 1;
    if (swapchainSupport.capabilities.maxImageCount > 0 && imageCount > swapchainSupport.capabilities.maxImageCount) {
        imageCount = swapchainSupport.capabilities.maxImageCount;
    }

    // Create swapchain
    VkSwapchainCreateInfoKHR createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR;
    createInfo.surface = context.GetSurface();
    createInfo.minImageCount = imageCount;
    createInfo.imageFormat = surfaceFormat.format;
    createInfo.imageColorSpace = surfaceFormat.colorSpace;
    createInfo.imageExtent = extent;
    createInfo.imageArrayLayers = 1;
    createInfo.imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;

    // Handle queue family sharing
    const QueueFamilyIndices &indices = context.GetQueueIndices();
    uint32_t queueFamilyIndices[] = {indices.graphicsFamily.value(), indices.presentFamily.value()};

    if (indices.graphicsFamily != indices.presentFamily) {
        createInfo.imageSharingMode = VK_SHARING_MODE_CONCURRENT;
        createInfo.queueFamilyIndexCount = 2;
        createInfo.pQueueFamilyIndices = queueFamilyIndices;
    } else {
        createInfo.imageSharingMode = VK_SHARING_MODE_EXCLUSIVE;
        createInfo.queueFamilyIndexCount = 0;
        createInfo.pQueueFamilyIndices = nullptr;
    }

    createInfo.preTransform = swapchainSupport.capabilities.currentTransform;
    createInfo.compositeAlpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
    createInfo.presentMode = presentMode;
    createInfo.clipped = VK_TRUE;
    createInfo.oldSwapchain = oldSwapchain;

    VkResult result = vkCreateSwapchainKHR(m_device, &createInfo, nullptr, &m_swapchain);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create swapchain: ", VkResultToString(result));
        return false;
    }

    // Store format and extent
    m_imageFormat = surfaceFormat.format;
    m_extent = extent;

    // Get swapchain images
    uint32_t actualImageCount = 0;
    vkGetSwapchainImagesKHR(m_device, m_swapchain, &actualImageCount, nullptr);
    m_images.resize(actualImageCount);
    vkGetSwapchainImagesKHR(m_device, m_swapchain, &actualImageCount, m_images.data());

    // Create image views
    if (!CreateImageViews()) {
        return false;
    }

    return true;
}

void VkSwapchainManager::Destroy() noexcept
{
    if (m_device == VK_NULL_HANDLE) {
        return;
    }

    // Wait for device idle (skip during engine shutdown — already drained)
    if (!m_skipWaitIdle) {
        vkDeviceWaitIdle(m_device);
    }

    // Cleanup swapchain
    CleanupSwapchain();

    // Cleanup sync objects
    for (auto &sync : m_syncObjects) {
        if (sync.imageAvailableSemaphore != VK_NULL_HANDLE) {
            vkDestroySemaphore(m_device, sync.imageAvailableSemaphore, nullptr);
        }
        if (sync.inFlightFence != VK_NULL_HANDLE) {
            vkDestroyFence(m_device, sync.inFlightFence, nullptr);
        }
    }
    m_syncObjects.clear();
    DestroyRenderFinishedSemaphores();

    m_device = VK_NULL_HANDLE;
    m_presentQueue = VK_NULL_HANDLE;
    m_currentFrame = 0;
}

// ============================================================================
// Frame Operations
// ============================================================================

SwapchainResult VkSwapchainManager::AcquireNextImage(uint32_t &imageIndex)
{
    // Wait for the previous frame to finish (unless already waited by caller)
    if (m_fenceAlreadyWaited) {
        m_fenceAlreadyWaited = false;
    } else {
        WaitForFrame();
    }

    // Acquire the next image
    // Use a finite timeout (500 ms) so we never hang forever when the
    // window is occluded or the compositor is busy (e.g. Alt+Tab).
    constexpr uint64_t kAcquireTimeoutNs = 500'000'000; // 500 ms
    VkResult result =
        vkAcquireNextImageKHR(m_device, m_swapchain, kAcquireTimeoutNs,
                              m_syncObjects[m_currentFrame].imageAvailableSemaphore, VK_NULL_HANDLE, &imageIndex);

    if (result == VK_ERROR_OUT_OF_DATE_KHR) {
        return SwapchainResult::NeedRecreate;
    }
    if (result == VK_SUBOPTIMAL_KHR) {
        // Suboptimal but usable - mark for recreation but continue
        return SwapchainResult::NeedRecreate;
    }
    if (result == VK_TIMEOUT || result == VK_NOT_READY) {
        // Window is likely occluded / behind another window — skip frame
        return SwapchainResult::NeedRecreate;
    }
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to acquire swapchain image: ", VkResultToString(result));
        return SwapchainResult::Error;
    }

    return SwapchainResult::Success;
}

SwapchainResult VkSwapchainManager::Present(uint32_t imageIndex)
{
    if (imageIndex >= m_renderFinishedSemaphores.size()) {
        INXLOG_ERROR("Present received invalid image index ", imageIndex, " for ", m_renderFinishedSemaphores.size(),
                     " render-finished semaphores");
        return SwapchainResult::Error;
    }

    VkSemaphore signalSemaphores[] = {m_renderFinishedSemaphores[imageIndex]};

    VkPresentInfoKHR presentInfo{};
    presentInfo.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR;
    presentInfo.waitSemaphoreCount = 1;
    presentInfo.pWaitSemaphores = signalSemaphores;

    VkSwapchainKHR swapchains[] = {m_swapchain};
    presentInfo.swapchainCount = 1;
    presentInfo.pSwapchains = swapchains;
    presentInfo.pImageIndices = &imageIndex;
    presentInfo.pResults = nullptr;

    VkResult result = vkQueuePresentKHR(m_presentQueue, &presentInfo);

    if (result == VK_ERROR_OUT_OF_DATE_KHR || result == VK_SUBOPTIMAL_KHR) {
        return SwapchainResult::NeedRecreate;
    }
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to present swapchain image: ", VkResultToString(result));
        return SwapchainResult::Error;
    }

    return SwapchainResult::Success;
}

void VkSwapchainManager::WaitForFrame()
{
    WaitForFencePumpingEvents(m_device, m_syncObjects[m_currentFrame].inFlightFence);
}

void VkSwapchainManager::ResetCurrentFence()
{
    vkResetFences(m_device, 1, &m_syncObjects[m_currentFrame].inFlightFence);
}

void VkSwapchainManager::AdvanceFrame()
{
    m_currentFrame = (m_currentFrame + 1) % MAX_FRAMES_IN_FLIGHT;
}

// ============================================================================
// Accessors
// ============================================================================

VkImageView VkSwapchainManager::GetImageView(size_t index) const
{
    if (index < m_imageViews.size()) {
        return m_imageViews[index];
    }
    return VK_NULL_HANDLE;
}

const FrameSyncObjects &VkSwapchainManager::GetCurrentSyncObjects() const
{
    return m_syncObjects[m_currentFrame];
}

VkSemaphore VkSwapchainManager::GetImageAvailableSemaphore() const
{
    return m_syncObjects[m_currentFrame].imageAvailableSemaphore;
}

VkSemaphore VkSwapchainManager::GetRenderFinishedSemaphore(uint32_t imageIndex) const
{
    if (imageIndex >= m_renderFinishedSemaphores.size()) {
        return VK_NULL_HANDLE;
    }
    return m_renderFinishedSemaphores[imageIndex];
}

VkFence VkSwapchainManager::GetInFlightFence() const
{
    return m_syncObjects[m_currentFrame].inFlightFence;
}

// ============================================================================
// Internal Methods
// ============================================================================

VkSurfaceFormatKHR VkSwapchainManager::ChooseSurfaceFormat(const std::vector<VkSurfaceFormatKHR> &formats) const
{
    // Prefer UNORM with BGRA8 (gamma is applied in the post-process shader)
    for (const auto &format : formats) {
        if (format.format == VK_FORMAT_B8G8R8A8_UNORM && format.colorSpace == VK_COLOR_SPACE_SRGB_NONLINEAR_KHR) {
            return format;
        }
    }

    // Fallback: just use the first available
    return formats[0];
}

VkPresentModeKHR VkSwapchainManager::ChoosePresentMode(const std::vector<VkPresentModeKHR> &modes) const
{
    // Try the user-preferred mode first
    for (const auto &mode : modes) {
        if (mode == m_preferredPresentMode) {
            return mode;
        }
    }

    // Fallback chain: MAILBOX → FIFO (always available)
    for (const auto &mode : modes) {
        if (mode == VK_PRESENT_MODE_MAILBOX_KHR) {
            return mode;
        }
    }

    return VK_PRESENT_MODE_FIFO_KHR;
}

VkExtent2D VkSwapchainManager::ChooseExtent(const VkSurfaceCapabilitiesKHR &capabilities, uint32_t requestedWidth,
                                            uint32_t requestedHeight) const
{
    // If currentExtent is not the special value, use it
    if (capabilities.currentExtent.width != std::numeric_limits<uint32_t>::max()) {
        return capabilities.currentExtent;
    }

    // Otherwise, clamp requested size to capabilities
    VkExtent2D actualExtent = {requestedWidth, requestedHeight};
    actualExtent.width =
        std::clamp(actualExtent.width, capabilities.minImageExtent.width, capabilities.maxImageExtent.width);
    actualExtent.height =
        std::clamp(actualExtent.height, capabilities.minImageExtent.height, capabilities.maxImageExtent.height);
    return actualExtent;
}

bool VkSwapchainManager::CreateImageViews()
{
    m_imageViews.resize(m_images.size());

    for (size_t i = 0; i < m_images.size(); i++) {
        VkImageViewCreateInfo viewInfo{};
        viewInfo.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        viewInfo.image = m_images[i];
        viewInfo.viewType = VK_IMAGE_VIEW_TYPE_2D;
        viewInfo.format = m_imageFormat;
        viewInfo.components.r = VK_COMPONENT_SWIZZLE_IDENTITY;
        viewInfo.components.g = VK_COMPONENT_SWIZZLE_IDENTITY;
        viewInfo.components.b = VK_COMPONENT_SWIZZLE_IDENTITY;
        viewInfo.components.a = VK_COMPONENT_SWIZZLE_IDENTITY;
        viewInfo.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        viewInfo.subresourceRange.baseMipLevel = 0;
        viewInfo.subresourceRange.levelCount = 1;
        viewInfo.subresourceRange.baseArrayLayer = 0;
        viewInfo.subresourceRange.layerCount = 1;

        VkResult result = vkCreateImageView(m_device, &viewInfo, nullptr, &m_imageViews[i]);
        if (result != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create image view ", i, ": ", VkResultToString(result));
            return false;
        }
    }

    return true;
}

bool VkSwapchainManager::CreateSyncObjects()
{
    m_syncObjects.resize(MAX_FRAMES_IN_FLIGHT);

    VkSemaphoreCreateInfo semaphoreInfo{};
    semaphoreInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;

    VkFenceCreateInfo fenceInfo{};
    fenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    fenceInfo.flags = VK_FENCE_CREATE_SIGNALED_BIT; // Start signaled for first frame

    for (size_t i = 0; i < MAX_FRAMES_IN_FLIGHT; i++) {
        if (vkCreateSemaphore(m_device, &semaphoreInfo, nullptr, &m_syncObjects[i].imageAvailableSemaphore) !=
                VK_SUCCESS ||
            vkCreateFence(m_device, &fenceInfo, nullptr, &m_syncObjects[i].inFlightFence) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create sync objects for frame ", i);
            return false;
        }
    }

    return CreateRenderFinishedSemaphores();
}

bool VkSwapchainManager::CreateRenderFinishedSemaphores()
{
    DestroyRenderFinishedSemaphores();

    m_renderFinishedSemaphores.resize(m_images.size(), VK_NULL_HANDLE);

    VkSemaphoreCreateInfo semaphoreInfo{};
    semaphoreInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;

    for (size_t i = 0; i < m_renderFinishedSemaphores.size(); ++i) {
        if (vkCreateSemaphore(m_device, &semaphoreInfo, nullptr, &m_renderFinishedSemaphores[i]) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create render-finished semaphore for swapchain image ", i);
            DestroyRenderFinishedSemaphores();
            return false;
        }
    }

    return true;
}

void VkSwapchainManager::DestroyRenderFinishedSemaphores() noexcept
{
    for (VkSemaphore &semaphore : m_renderFinishedSemaphores) {
        if (semaphore != VK_NULL_HANDLE) {
            vkDestroySemaphore(m_device, semaphore, nullptr);
            semaphore = VK_NULL_HANDLE;
        }
    }
    m_renderFinishedSemaphores.clear();
}

void VkSwapchainManager::CleanupSwapchain()
{
    DestroyRenderFinishedSemaphores();

    // Destroy image views
    for (auto &imageView : m_imageViews) {
        if (imageView != VK_NULL_HANDLE) {
            vkDestroyImageView(m_device, imageView, nullptr);
        }
    }
    m_imageViews.clear();
    m_images.clear();

    // Destroy swapchain
    if (m_swapchain != VK_NULL_HANDLE) {
        vkDestroySwapchainKHR(m_device, m_swapchain, nullptr);
        m_swapchain = VK_NULL_HANDLE;
    }
}

} // namespace vk
} // namespace infernux
