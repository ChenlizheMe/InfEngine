#pragma once

#include "VkDeviceContext.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace infernux::vk
{

using VulkanAdapterId = uint64_t;
inline constexpr VulkanAdapterId InvalidVulkanAdapterId = 0;

enum class DeviceWorkloadRole : uint8_t
{
    Presentation,
    PrimaryGraphics,
    AsyncCompute,
    Transfer,
    Count,
};

struct VulkanAdapterInfo
{
    VulkanAdapterId id = InvalidVulkanAdapterId;
    VkPhysicalDevice native = VK_NULL_HANDLE;
    std::string name;
    VkPhysicalDeviceType type = VK_PHYSICAL_DEVICE_TYPE_OTHER;
    uint32_t vendorId = 0;
    uint32_t deviceId = 0;
    uint32_t apiVersion = 0;
    uint32_t driverVersion = 0;
    uint64_t deviceLocalBytes = 0;
    bool graphics = false;
    bool compute = false;
    bool dedicatedCompute = false;
    bool transfer = false;
    bool dedicatedTransfer = false;
    bool presentation = false;
    bool selectedPrimary = false;
};

/// Owns adapter inventory, workload-role selection and logical Vulkan devices.
/// The current milestone creates one primary logical device, but callers no
/// longer infer adapter topology or device roles from process-global handles.
class VulkanDeviceManager
{
  public:
    VulkanDeviceManager() = default;
    VulkanDeviceManager(const VulkanDeviceManager &) = delete;
    VulkanDeviceManager &operator=(const VulkanDeviceManager &) = delete;

    bool InitializePrimaryInstance(const DeviceConfig &config);
    bool InitializePrimaryDevice(VkSurfaceKHR surface, const DeviceConfig &config);
    void Destroy() noexcept;

    [[nodiscard]] VkDeviceContext &Primary() noexcept
    {
        return m_primary;
    }
    [[nodiscard]] const VkDeviceContext &Primary() const noexcept
    {
        return m_primary;
    }
    [[nodiscard]] size_t DeviceCount() const noexcept
    {
        return m_primary.IsValid() ? 1u : 0u;
    }
    [[nodiscard]] VkDeviceContext *Find(rhi::DeviceId id) noexcept
    {
        return m_primary.IsValid() && m_primary.GetDeviceId() == id ? &m_primary : nullptr;
    }
    [[nodiscard]] const VkDeviceContext *Find(rhi::DeviceId id) const noexcept
    {
        return m_primary.IsValid() && m_primary.GetDeviceId() == id ? &m_primary : nullptr;
    }

    [[nodiscard]] const std::vector<VulkanAdapterInfo> &Adapters() const noexcept
    {
        return m_adapters;
    }
    [[nodiscard]] const VulkanAdapterInfo *FindAdapter(VulkanAdapterId id) const noexcept;
    [[nodiscard]] rhi::DeviceId DeviceForRole(DeviceWorkloadRole role) const noexcept;

  private:
    void RefreshAdapterInventory(VkSurfaceKHR surface);
    void BindPrimaryRoles() noexcept;

    VkDeviceContext m_primary;
    std::vector<VulkanAdapterInfo> m_adapters;
    std::array<rhi::DeviceId, static_cast<size_t>(DeviceWorkloadRole::Count)> m_roleDevices{};
};

} // namespace infernux::vk
