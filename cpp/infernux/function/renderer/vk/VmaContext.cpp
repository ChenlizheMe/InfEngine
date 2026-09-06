/**
 * @file VmaContext.cpp
 * @brief VMA implementation — defines VMA_IMPLEMENTATION exactly once.
 *
 * This translation unit compiles the VMA library code.
 * All other files include <vk_mem_alloc.h> for declarations only.
 */

// VMA_IMPLEMENTATION must be defined in exactly one .cpp file
#define VMA_IMPLEMENTATION

// Volk-backed targets resolve commands dynamically; native desktop builds use
// the platform Vulkan loader directly.
#if defined(INFERNUX_USE_VOLK)
#define VMA_STATIC_VULKAN_FUNCTIONS 0
#define VMA_DYNAMIC_VULKAN_FUNCTIONS 1
#else
#define VMA_STATIC_VULKAN_FUNCTIONS 1
#define VMA_DYNAMIC_VULKAN_FUNCTIONS 0
#endif

#include "VmaContext.h"
#include <core/log/InxLog.h>

namespace infernux
{
namespace vk
{

VmaAllocator CreateVmaAllocator(VkInstance instance, VkPhysicalDevice physicalDevice, VkDevice device)
{
    VmaAllocatorCreateInfo createInfo{};
    createInfo.instance = instance;
    createInfo.physicalDevice = physicalDevice;
    createInfo.device = device;
    createInfo.vulkanApiVersion = VK_API_VERSION_1_2;

#if defined(INFERNUX_USE_VOLK)
    VmaVulkanFunctions vulkanFunctions{};
    vulkanFunctions.vkGetInstanceProcAddr = vkGetInstanceProcAddr;
    vulkanFunctions.vkGetDeviceProcAddr = vkGetDeviceProcAddr;
    createInfo.pVulkanFunctions = &vulkanFunctions;
#endif

    // Enable VK_KHR_dedicated_allocation (promoted to Vulkan 1.1 core)
    // VMA uses this automatically for large allocations (shadow maps, etc.)
    createInfo.flags = VMA_ALLOCATOR_CREATE_KHR_DEDICATED_ALLOCATION_BIT;

    VmaAllocator allocator = VK_NULL_HANDLE;
    VkResult result = vmaCreateAllocator(&createInfo, &allocator);

    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create VMA allocator, VkResult=", static_cast<int>(result));
        return VK_NULL_HANDLE;
    }

    // Log VMA memory budget
    VmaBudget budgets[VK_MAX_MEMORY_HEAPS];
    vmaGetHeapBudgets(allocator, budgets);

    VkPhysicalDeviceMemoryProperties memProps;
    vkGetPhysicalDeviceMemoryProperties(physicalDevice, &memProps);

    INXLOG_INFO("VMA allocator created successfully");
    for (uint32_t i = 0; i < memProps.memoryHeapCount; ++i) {
        const auto &heap = memProps.memoryHeaps[i];
        bool isDeviceLocal = (heap.flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) != 0;
        INXLOG_INFO("  Heap ", i, ": ", heap.size / (1024 * 1024), " MB",
                    isDeviceLocal ? " [DEVICE_LOCAL]" : " [HOST]");
    }

    return allocator;
}

void DestroyVmaAllocator(VmaAllocator allocator)
{
    if (allocator != VK_NULL_HANDLE) {
        // Print leak statistics in debug
        VmaTotalStatistics stats;
        vmaCalculateStatistics(allocator, &stats);
        if (stats.total.statistics.allocationCount > 0) {
            INXLOG_WARN("VMA: Destroying allocator with ", stats.total.statistics.allocationCount,
                        " live allocations (", stats.total.statistics.allocationBytes, " bytes) — possible leak");
        }

        vmaDestroyAllocator(allocator);
        INXLOG_INFO("VMA allocator destroyed");
    }
}

VmaRuntimeStatistics QueryVmaRuntimeStatistics(VmaAllocator allocator, VkPhysicalDevice physicalDevice)
{
    VmaTotalStatistics statistics{};
    vmaCalculateStatistics(allocator, &statistics);

    VmaRuntimeStatistics result;
    result.allocationBytes = statistics.total.statistics.allocationBytes;
    result.blockBytes = statistics.total.statistics.blockBytes;
    result.allocationCount = statistics.total.statistics.allocationCount;

    VkPhysicalDeviceMemoryProperties properties{};
    vkGetPhysicalDeviceMemoryProperties(physicalDevice, &properties);
    VmaBudget budgets[VK_MAX_MEMORY_HEAPS]{};
    vmaGetHeapBudgets(allocator, budgets);
    for (uint32_t index = 0; index < properties.memoryHeapCount; ++index) {
        if ((properties.memoryHeaps[index].flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) == 0)
            continue;
        result.deviceLocalAllocationBytes += statistics.memoryHeap[index].statistics.allocationBytes;
        result.deviceLocalUsageBytes += budgets[index].usage;
        result.deviceLocalBudgetBytes += budgets[index].budget;
    }
    return result;
}

} // namespace vk
} // namespace infernux
