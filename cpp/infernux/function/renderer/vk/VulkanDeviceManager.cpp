#include "VulkanDeviceManager.h"

#include <algorithm>
#include <array>

namespace infernux::vk
{
namespace
{

VulkanAdapterId MakeAdapterId(const VkPhysicalDeviceIDProperties &identity,
                              const VkPhysicalDeviceProperties &properties) noexcept
{
    constexpr uint64_t offset = 1469598103934665603ull;
    constexpr uint64_t prime = 1099511628211ull;
    uint64_t hash = offset;
    bool hasUuid = false;
    for (uint8_t byte : identity.deviceUUID) {
        hasUuid |= byte != 0;
        hash = (hash ^ byte) * prime;
    }
    if (!hasUuid) {
        const std::array<uint32_t, 3> fallback = {properties.vendorID, properties.deviceID, properties.driverVersion};
        for (uint32_t value : fallback) {
            for (uint32_t shift = 0; shift < 32; shift += 8)
                hash = (hash ^ static_cast<uint8_t>(value >> shift)) * prime;
        }
    }
    return hash == InvalidVulkanAdapterId ? 1 : hash;
}

} // namespace

bool VulkanDeviceManager::InitializePrimaryInstance(const DeviceConfig &config)
{
    if (!m_primary.InitializeInstance(config))
        return false;
    RefreshAdapterInventory(VK_NULL_HANDLE);
    return true;
}

bool VulkanDeviceManager::InitializePrimaryDevice(VkSurfaceKHR surface, const DeviceConfig &config)
{
    RefreshAdapterInventory(surface);
    if (!m_primary.InitializeDevice(surface, config))
        return false;

    for (auto &adapter : m_adapters)
        adapter.selectedPrimary = adapter.native == m_primary.GetPhysicalDevice();
    BindPrimaryRoles();
    return true;
}

void VulkanDeviceManager::Destroy() noexcept
{
    m_roleDevices = {};
    m_adapters.clear();
    m_primary.Destroy();
}

const VulkanAdapterInfo *VulkanDeviceManager::FindAdapter(VulkanAdapterId id) const noexcept
{
    const auto found = std::find_if(m_adapters.begin(), m_adapters.end(),
                                    [id](const VulkanAdapterInfo &adapter) { return adapter.id == id; });
    return found == m_adapters.end() ? nullptr : &*found;
}

rhi::DeviceId VulkanDeviceManager::DeviceForRole(DeviceWorkloadRole role) const noexcept
{
    const size_t index = static_cast<size_t>(role);
    return index < m_roleDevices.size() ? m_roleDevices[index] : rhi::InvalidDeviceId;
}

void VulkanDeviceManager::RefreshAdapterInventory(VkSurfaceKHR surface)
{
    m_adapters.clear();
    const VkInstance instance = m_primary.GetInstance();
    if (instance == VK_NULL_HANDLE)
        return;

    uint32_t count = 0;
    if (vkEnumeratePhysicalDevices(instance, &count, nullptr) != VK_SUCCESS || count == 0)
        return;
    std::vector<VkPhysicalDevice> devices(count);
    if (vkEnumeratePhysicalDevices(instance, &count, devices.data()) != VK_SUCCESS)
        return;

    m_adapters.reserve(count);
    for (VkPhysicalDevice device : devices) {
        VkPhysicalDeviceIDProperties identity{};
        identity.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES;
        VkPhysicalDeviceProperties2 properties2{};
        properties2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2;
        properties2.pNext = &identity;
        vkGetPhysicalDeviceProperties2(device, &properties2);

        VulkanAdapterInfo info;
        info.id = MakeAdapterId(identity, properties2.properties);
        info.native = device;
        info.name = properties2.properties.deviceName;
        info.type = properties2.properties.deviceType;
        info.vendorId = properties2.properties.vendorID;
        info.deviceId = properties2.properties.deviceID;
        info.apiVersion = properties2.properties.apiVersion;
        info.driverVersion = properties2.properties.driverVersion;

        VkPhysicalDeviceMemoryProperties memory{};
        vkGetPhysicalDeviceMemoryProperties(device, &memory);
        for (uint32_t heap = 0; heap < memory.memoryHeapCount; ++heap) {
            if ((memory.memoryHeaps[heap].flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) != 0)
                info.deviceLocalBytes += memory.memoryHeaps[heap].size;
        }

        uint32_t familyCount = 0;
        vkGetPhysicalDeviceQueueFamilyProperties(device, &familyCount, nullptr);
        std::vector<VkQueueFamilyProperties> families(familyCount);
        vkGetPhysicalDeviceQueueFamilyProperties(device, &familyCount, families.data());
        for (uint32_t family = 0; family < familyCount; ++family) {
            const VkQueueFlags flags = families[family].queueFlags;
            info.graphics |= (flags & VK_QUEUE_GRAPHICS_BIT) != 0;
            info.compute |= (flags & VK_QUEUE_COMPUTE_BIT) != 0;
            info.transfer |= (flags & VK_QUEUE_TRANSFER_BIT) != 0;
            info.dedicatedCompute |= (flags & VK_QUEUE_COMPUTE_BIT) != 0 && (flags & VK_QUEUE_GRAPHICS_BIT) == 0;
            info.dedicatedTransfer |=
                (flags & VK_QUEUE_TRANSFER_BIT) != 0 && (flags & (VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT)) == 0;
            if (surface != VK_NULL_HANDLE) {
                VkBool32 supported = VK_FALSE;
                if (vkGetPhysicalDeviceSurfaceSupportKHR(device, family, surface, &supported) == VK_SUCCESS)
                    info.presentation |= supported == VK_TRUE;
            }
        }
        m_adapters.push_back(std::move(info));
    }
}

void VulkanDeviceManager::BindPrimaryRoles() noexcept
{
    m_roleDevices = {};
    if (!m_primary.IsValid())
        return;
    const rhi::DeviceId id = m_primary.GetDeviceId();
    const auto found = std::find_if(m_adapters.begin(), m_adapters.end(),
                                    [](const VulkanAdapterInfo &adapter) { return adapter.selectedPrimary; });
    if (found == m_adapters.end())
        return;
    if (found->presentation)
        m_roleDevices[static_cast<size_t>(DeviceWorkloadRole::Presentation)] = id;
    if (found->graphics)
        m_roleDevices[static_cast<size_t>(DeviceWorkloadRole::PrimaryGraphics)] = id;
    if (found->compute)
        m_roleDevices[static_cast<size_t>(DeviceWorkloadRole::AsyncCompute)] = id;
    if (found->transfer)
        m_roleDevices[static_cast<size_t>(DeviceWorkloadRole::Transfer)] = id;
}

} // namespace infernux::vk
