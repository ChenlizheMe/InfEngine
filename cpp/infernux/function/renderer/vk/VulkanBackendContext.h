#pragma once

#include "VkSwapchainManager.h"
#include "VulkanDeviceManager.h"
#include "VulkanQueueManager.h"

namespace infernux::vk
{

/// Composition root for one Vulkan logical device and its optional window
/// presentation domain. Offscreen views use Device() but never depend on the
/// swapchain; Presentation() is explicitly tied to the same DeviceId.
class VulkanBackendContext
{
  public:
    VulkanBackendContext() = default;
    ~VulkanBackendContext() = default;

    VulkanBackendContext(const VulkanBackendContext &) = delete;
    VulkanBackendContext &operator=(const VulkanBackendContext &) = delete;
    VulkanBackendContext(VulkanBackendContext &&) = delete;
    VulkanBackendContext &operator=(VulkanBackendContext &&) = delete;

    [[nodiscard]] VkDeviceContext &Device() noexcept
    {
        return m_devices.Primary();
    }
    [[nodiscard]] const VkDeviceContext &Device() const noexcept
    {
        return m_devices.Primary();
    }
    [[nodiscard]] VulkanDeviceManager &Devices() noexcept
    {
        return m_devices;
    }
    [[nodiscard]] const VulkanDeviceManager &Devices() const noexcept
    {
        return m_devices;
    }
    [[nodiscard]] VkSwapchainManager &Presentation() noexcept
    {
        return m_presentation;
    }
    [[nodiscard]] VulkanQueueManager &Queues() noexcept
    {
        return m_queues;
    }
    [[nodiscard]] const VulkanQueueManager &Queues() const noexcept
    {
        return m_queues;
    }
    [[nodiscard]] const VkSwapchainManager &Presentation() const noexcept
    {
        return m_presentation;
    }

    [[nodiscard]] bool OwnsConsistentPresentation() const noexcept
    {
        const auto &device = m_devices.Primary();
        return !m_presentation.IsValid() || (device.IsValid() && m_presentation.GetDeviceId() == device.GetDeviceId());
    }

    void SetShuttingDown(bool value) noexcept
    {
        m_devices.Primary().SetShuttingDown(value);
        m_presentation.SetSkipWaitIdle(value);
    }

  private:
    VulkanDeviceManager m_devices;
    VulkanQueueManager m_queues;
    VkSwapchainManager m_presentation;
};

} // namespace infernux::vk
