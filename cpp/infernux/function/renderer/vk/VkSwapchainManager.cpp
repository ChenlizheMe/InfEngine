/**
 * @file VkSwapchainManager.cpp
 * @brief Implementation of Vulkan swapchain management
 */

// Prevent Windows min/max macros from conflicting with std::min/std::max
// (handled globally by core/config/InxPlatform.h via InxPath.h)

#include "VkSwapchainManager.h"
#include "VkDeviceContext.h"
#include "VulkanQueueManager.h"
#include <core/error/InxError.h>

#include <algorithm>
#include <limits>

namespace infernux
{
namespace vk
{

// ============================================================================
// Constructor / Destructor / Move
// ============================================================================

VkSwapchainManager::~VkSwapchainManager()
{
    Destroy();
}

VkSwapchainManager::VkSwapchainManager(VkSwapchainManager &&other) noexcept
    : m_skipWaitIdle(other.m_skipWaitIdle), m_deviceId(other.m_deviceId),
      m_preferredPresentMode(other.m_preferredPresentMode), m_device(other.m_device),
      m_generation(std::move(other.m_generation)),
      m_imageAvailableSemaphores(std::move(other.m_imageAvailableSemaphores))
{
    other.m_deviceId = rhi::InvalidDeviceId;
    other.m_device = VK_NULL_HANDLE;
    other.m_generation = {};
}

VkSwapchainManager &VkSwapchainManager::operator=(VkSwapchainManager &&other) noexcept
{
    if (this != &other) {
        Destroy();

        m_skipWaitIdle = other.m_skipWaitIdle;
        m_deviceId = other.m_deviceId;
        m_preferredPresentMode = other.m_preferredPresentMode;
        m_device = other.m_device;
        m_generation = std::move(other.m_generation);
        m_imageAvailableSemaphores = std::move(other.m_imageAvailableSemaphores);

        other.m_deviceId = rhi::InvalidDeviceId;
        other.m_device = VK_NULL_HANDLE;
        other.m_generation = {};
    }
    return *this;
}

// ============================================================================
// Lifecycle Management
// ============================================================================

bool VkSwapchainManager::Create(const VkDeviceContext &context, uint32_t width, uint32_t height)
{
    m_deviceId = context.GetDeviceId();
    m_device = context.GetDevice();

    SwapchainGeneration candidate;
    if (!BuildGeneration(context, width, height, VK_NULL_HANDLE, candidate))
        return false;

    if (!CreateSyncObjects()) {
        DestroyGeneration(candidate);
        return false;
    }
    m_generation = std::move(candidate);

    INXLOG_INFO("Swapchain created: ", m_generation.extent.width, "x", m_generation.extent.height, ", ",
                m_generation.images.size(), " images, format ", static_cast<int>(m_generation.imageFormat));

    return true;
}

bool VkSwapchainManager::Recreate(const VkDeviceContext &context, VulkanQueueManager &queues, uint32_t width,
                                  uint32_t height, const BeforeGenerationCommit &beforeCommit)
{
    if (m_deviceId == rhi::InvalidDeviceId || m_deviceId != context.GetDeviceId()) {
        INXLOG_ERROR("Swapchain recreation rejected: presentation device identity changed");
        return false;
    }
    SwapchainSupportDetails swapchainSupport = context.QuerySwapchainSupport();
    if (swapchainSupport.capabilities.currentExtent.width == 0 ||
        swapchainSupport.capabilities.currentExtent.height == 0) {
        INXLOG_WARN("Swapchain recreation skipped: zero extent (minimized window?)");
        return false;
    }

    // Swapchain resources are referenced only by graphics/present queues. Do
    // not stall unrelated transfer/compute work on this logical device.
    if (queues.WaitIdleForPresentation() != VK_SUCCESS) {
        INXLOG_ERROR("Swapchain recreation failed while draining presentation queues");
        return false;
    }

    SwapchainGeneration candidate;
    if (!BuildGeneration(context, width, height, m_generation.swapchain, candidate)) {
        return false;
    }

    // The new generation is complete before external framebuffers and aliases
    // are released. This is the single commit point: ordinary creation
    // failures leave the published generation untouched.
    if (beforeCommit) {
        beforeCommit();
    }

    SwapchainGeneration retired = std::move(m_generation);
    m_generation = std::move(candidate);
    DestroyGeneration(retired);

    INXLOG_INFO("Swapchain generation committed: ", m_generation.extent.width, "x", m_generation.extent.height, ", ",
                m_generation.images.size(), " images");
    return true;
}

bool VkSwapchainManager::BuildGeneration(const VkDeviceContext &context, uint32_t width, uint32_t height,
                                         VkSwapchainKHR oldSwapchain, SwapchainGeneration &generation)
{
    // Query swapchain support
    SwapchainSupportDetails swapchainSupport = context.QuerySwapchainSupport();
    if (swapchainSupport.formats.empty() || swapchainSupport.presentModes.empty()) {
        INXLOG_ERROR("Cannot build swapchain generation: surface exposes no usable format or present mode");
        return false;
    }

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

    VkResult result = vkCreateSwapchainKHR(m_device, &createInfo, nullptr, &generation.swapchain);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create swapchain: ", VkResultToString(result));
        return false;
    }

    // Store format and extent
    generation.imageFormat = surfaceFormat.format;
    generation.extent = extent;

    // Get swapchain images
    uint32_t actualImageCount = 0;
    result = vkGetSwapchainImagesKHR(m_device, generation.swapchain, &actualImageCount, nullptr);
    if (result != VK_SUCCESS || actualImageCount == 0) {
        INXLOG_ERROR("Failed to query swapchain image count: ", VkResultToString(result));
        DestroyGeneration(generation);
        return false;
    }
    generation.images.resize(actualImageCount);
    result = vkGetSwapchainImagesKHR(m_device, generation.swapchain, &actualImageCount, generation.images.data());
    if (result != VK_SUCCESS && result != VK_INCOMPLETE) {
        INXLOG_ERROR("Failed to query swapchain images: ", VkResultToString(result));
        DestroyGeneration(generation);
        return false;
    }
    generation.images.resize(actualImageCount);

    if (!CreateImageViews(generation) || !CreateRenderFinishedSemaphores(generation)) {
        DestroyGeneration(generation);
        return false;
    }

    return true;
}

void VkSwapchainManager::Destroy() noexcept
{
    if (m_device == VK_NULL_HANDLE) {
        m_deviceId = rhi::InvalidDeviceId;
        return;
    }

    // Wait for device idle (skip during engine shutdown — already drained)
    if (!m_skipWaitIdle) {
        vkDeviceWaitIdle(m_device);
    }

    // Cleanup swapchain
    DestroyGeneration(m_generation);

    for (VkSemaphore semaphore : m_imageAvailableSemaphores) {
        if (semaphore != VK_NULL_HANDLE)
            vkDestroySemaphore(m_device, semaphore, nullptr);
    }
    m_imageAvailableSemaphores.clear();
    m_device = VK_NULL_HANDLE;
    m_deviceId = rhi::InvalidDeviceId;
}

// ============================================================================
// Frame Operations
// ============================================================================

SwapchainResult VkSwapchainManager::AcquireNextImage(uint32_t frameSlot, uint32_t &imageIndex)
{
    if (frameSlot >= m_imageAvailableSemaphores.size()) {
        INXLOG_ERROR("AcquireNextImage received invalid frame slot ", frameSlot, " for ",
                     m_imageAvailableSemaphores.size(), " acquire semaphores");
        return SwapchainResult::Error;
    }
    // Acquire the next image
    // Use a finite timeout (500 ms) so we never hang forever when the
    // window is occluded or the compositor is busy (e.g. Alt+Tab).
    constexpr uint64_t kAcquireTimeoutNs = 500'000'000; // 500 ms
    VkResult result = vkAcquireNextImageKHR(m_device, m_generation.swapchain, kAcquireTimeoutNs,
                                            m_imageAvailableSemaphores[frameSlot], VK_NULL_HANDLE, &imageIndex);

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

SwapchainResult VkSwapchainManager::Present(VulkanQueueManager &queues, uint32_t imageIndex)
{
    if (imageIndex >= m_generation.renderFinishedSemaphores.size()) {
        INXLOG_ERROR("Present received invalid image index ", imageIndex, " for ",
                     m_generation.renderFinishedSemaphores.size(), " render-finished semaphores");
        return SwapchainResult::Error;
    }

    VkSemaphore signalSemaphores[] = {m_generation.renderFinishedSemaphores[imageIndex]};

    VkPresentInfoKHR presentInfo{};
    presentInfo.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR;
    presentInfo.waitSemaphoreCount = 1;
    presentInfo.pWaitSemaphores = signalSemaphores;

    VkSwapchainKHR swapchains[] = {m_generation.swapchain};
    presentInfo.swapchainCount = 1;
    presentInfo.pSwapchains = swapchains;
    presentInfo.pImageIndices = &imageIndex;
    presentInfo.pResults = nullptr;

    VkResult result = queues.Present(presentInfo);

    if (result == VK_ERROR_OUT_OF_DATE_KHR || result == VK_SUBOPTIMAL_KHR) {
        return SwapchainResult::NeedRecreate;
    }
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to present swapchain image: ", VkResultToString(result));
        return SwapchainResult::Error;
    }

    return SwapchainResult::Success;
}

// ============================================================================
// Accessors
// ============================================================================

VkImageView VkSwapchainManager::GetImageView(size_t index) const
{
    if (index < m_generation.imageViews.size()) {
        return m_generation.imageViews[index];
    }
    return VK_NULL_HANDLE;
}

VkSemaphore VkSwapchainManager::GetImageAvailableSemaphore(uint32_t frameSlot) const
{
    return frameSlot < m_imageAvailableSemaphores.size() ? m_imageAvailableSemaphores[frameSlot] : VK_NULL_HANDLE;
}

VkSemaphore VkSwapchainManager::GetRenderFinishedSemaphore(uint32_t imageIndex) const
{
    if (imageIndex >= m_generation.renderFinishedSemaphores.size()) {
        return VK_NULL_HANDLE;
    }
    return m_generation.renderFinishedSemaphores[imageIndex];
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

bool VkSwapchainManager::CreateImageViews(SwapchainGeneration &generation)
{
    generation.imageViews.resize(generation.images.size(), VK_NULL_HANDLE);

    for (size_t i = 0; i < generation.images.size(); i++) {
        VkImageViewCreateInfo viewInfo{};
        viewInfo.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        viewInfo.image = generation.images[i];
        viewInfo.viewType = VK_IMAGE_VIEW_TYPE_2D;
        viewInfo.format = generation.imageFormat;
        viewInfo.components.r = VK_COMPONENT_SWIZZLE_IDENTITY;
        viewInfo.components.g = VK_COMPONENT_SWIZZLE_IDENTITY;
        viewInfo.components.b = VK_COMPONENT_SWIZZLE_IDENTITY;
        viewInfo.components.a = VK_COMPONENT_SWIZZLE_IDENTITY;
        viewInfo.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        viewInfo.subresourceRange.baseMipLevel = 0;
        viewInfo.subresourceRange.levelCount = 1;
        viewInfo.subresourceRange.baseArrayLayer = 0;
        viewInfo.subresourceRange.layerCount = 1;

        VkResult result = vkCreateImageView(m_device, &viewInfo, nullptr, &generation.imageViews[i]);
        if (result != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create image view ", i, ": ", VkResultToString(result));
            return false;
        }
    }

    return true;
}

bool VkSwapchainManager::CreateSyncObjects()
{
    std::vector<VkSemaphore> candidate(MAX_FRAMES_IN_FLIGHT, VK_NULL_HANDLE);

    VkSemaphoreCreateInfo semaphoreInfo{};
    semaphoreInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;

    for (size_t i = 0; i < MAX_FRAMES_IN_FLIGHT; i++) {
        if (vkCreateSemaphore(m_device, &semaphoreInfo, nullptr, &candidate[i]) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create image-acquire semaphore for frame ", i);
            for (VkSemaphore semaphore : candidate) {
                if (semaphore != VK_NULL_HANDLE) {
                    vkDestroySemaphore(m_device, semaphore, nullptr);
                }
            }
            return false;
        }
    }

    m_imageAvailableSemaphores = std::move(candidate);
    return true;
}

bool VkSwapchainManager::CreateRenderFinishedSemaphores(SwapchainGeneration &generation)
{
    generation.renderFinishedSemaphores.resize(generation.images.size(), VK_NULL_HANDLE);

    VkSemaphoreCreateInfo semaphoreInfo{};
    semaphoreInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;

    for (size_t i = 0; i < generation.renderFinishedSemaphores.size(); ++i) {
        if (vkCreateSemaphore(m_device, &semaphoreInfo, nullptr, &generation.renderFinishedSemaphores[i]) !=
            VK_SUCCESS) {
            INXLOG_ERROR("Failed to create render-finished semaphore for swapchain image ", i);
            return false;
        }
    }

    return true;
}

void VkSwapchainManager::DestroyGeneration(SwapchainGeneration &generation) noexcept
{
    for (VkSemaphore &semaphore : generation.renderFinishedSemaphores) {
        if (semaphore != VK_NULL_HANDLE) {
            vkDestroySemaphore(m_device, semaphore, nullptr);
            semaphore = VK_NULL_HANDLE;
        }
    }
    generation.renderFinishedSemaphores.clear();

    for (auto &imageView : generation.imageViews) {
        if (imageView != VK_NULL_HANDLE) {
            vkDestroyImageView(m_device, imageView, nullptr);
        }
    }
    generation.imageViews.clear();
    generation.images.clear();

    if (generation.swapchain != VK_NULL_HANDLE) {
        vkDestroySwapchainKHR(m_device, generation.swapchain, nullptr);
        generation.swapchain = VK_NULL_HANDLE;
    }
    generation.imageFormat = VK_FORMAT_UNDEFINED;
    generation.extent = {};
}

} // namespace vk
} // namespace infernux
