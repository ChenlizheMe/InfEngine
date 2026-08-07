/**
 * @file VkDeviceContext.cpp
 * @brief Implementation of Vulkan device context management
 */

#include "VkDeviceContext.h"
#include "DescriptorBindTrace.h"
#include "RhiVulkanTypes.h"
#include "VmaContext.h"
#include "VulkanRhiDevice.h"
#include <core/error/InxError.h>

#include <SDL3/SDL.h>
#include <SDL3/SDL_vulkan.h>

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <set>
#include <sstream>

namespace infernux
{
namespace vk
{

namespace
{
infernux::rhi::AdapterType ToRhiAdapterType(VkPhysicalDeviceType type)
{
    using infernux::rhi::AdapterType;
    switch (type) {
    case VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU:
        return AdapterType::Integrated;
    case VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU:
        return AdapterType::Discrete;
    case VK_PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU:
        return AdapterType::Virtual;
    case VK_PHYSICAL_DEVICE_TYPE_CPU:
        return AdapterType::Cpu;
    default:
        return AdapterType::Unknown;
    }
}

infernux::rhi::SampleCountMask ToRhiSampleCountMask(VkSampleCountFlags samples)
{
    using namespace infernux::rhi;
    SampleCountMask result = 0;
    if ((samples & VK_SAMPLE_COUNT_1_BIT) != 0)
        result |= SampleCountBit(SampleCount::One);
    if ((samples & VK_SAMPLE_COUNT_2_BIT) != 0)
        result |= SampleCountBit(SampleCount::Two);
    if ((samples & VK_SAMPLE_COUNT_4_BIT) != 0)
        result |= SampleCountBit(SampleCount::Four);
    if ((samples & VK_SAMPLE_COUNT_8_BIT) != 0)
        result |= SampleCountBit(SampleCount::Eight);
    return result;
}

infernux::rhi::FormatFeature ToRhiFormatFeatures(VkFormatFeatureFlags features)
{
    using infernux::rhi::FormatFeature;
    FormatFeature result = FormatFeature::None;
    if ((features & VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT) != 0)
        result |= FormatFeature::Sampled;
    if ((features & VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT) != 0)
        result |= FormatFeature::FilterLinear;
    if ((features & VK_FORMAT_FEATURE_STORAGE_IMAGE_BIT) != 0)
        result |= FormatFeature::Storage;
    if ((features & VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT) != 0)
        result |= FormatFeature::ColorAttachment;
    if ((features & VK_FORMAT_FEATURE_DEPTH_STENCIL_ATTACHMENT_BIT) != 0)
        result |= FormatFeature::DepthStencilAttachment;
    if ((features & VK_FORMAT_FEATURE_TRANSFER_SRC_BIT) != 0)
        result |= FormatFeature::TransferSource;
    if ((features & VK_FORMAT_FEATURE_TRANSFER_DST_BIT) != 0)
        result |= FormatFeature::TransferDestination;
    if ((features & VK_FORMAT_FEATURE_BLIT_SRC_BIT) != 0)
        result |= FormatFeature::BlitSource;
    if ((features & VK_FORMAT_FEATURE_BLIT_DST_BIT) != 0)
        result |= FormatFeature::BlitDestination;
    return result;
}

bool ExtractDescriptorRawFromValidationMessage(const char *message, uint64_t &outRaw)
{
    outRaw = 0ull;
    if (!message)
        return false;

    const char *p = std::strstr(message, "Object 0x");
    if (!p)
        p = std::strstr(message, "(VkDescriptorSet 0x");
    if (!p)
        return false;

    const char *hex = std::strstr(p, "0x");
    if (!hex)
        return false;

    char *end = nullptr;
    const unsigned long long parsed = std::strtoull(hex + 2, &end, 16);
    if (end == hex + 2)
        return false;

    outRaw = static_cast<uint64_t>(parsed);
    return true;
}

bool IsIntentionalSupersetVertexInterfaceWarning(const VkDebugUtilsMessengerCallbackDataEXT *callbackData)
{
    if (callbackData == nullptr || callbackData->pMessage == nullptr)
        return false;
    const char *message = callbackData->pMessage;
    return std::strstr(message, "VK_SHADER_STAGE_VERTEX_BIT declared to output location") != nullptr &&
           std::strstr(message, "but is not an Input declared by VK_SHADER_STAGE_FRAGMENT_BIT") != nullptr;
}
} // namespace

// ============================================================================
// Debug Callback
// ============================================================================

static VKAPI_ATTR VkBool32 VKAPI_CALL DebugCallback(VkDebugUtilsMessageSeverityFlagBitsEXT messageSeverity,
                                                    VkDebugUtilsMessageTypeFlagsEXT messageType,
                                                    const VkDebugUtilsMessengerCallbackDataEXT *pCallbackData,
                                                    void *pUserData)
{
    // Filter by severity
    if (messageSeverity >= VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT) {
        INXLOG_ERROR("Vulkan Validation Error: ", pCallbackData->pMessage);

        const bool descriptorBindFailure = pCallbackData && pCallbackData->pMessage &&
                                           std::strstr(pCallbackData->pMessage, "vkCmdBindDescriptorSets()") != nullptr;
        const bool descriptorDrawFailure =
            pCallbackData && pCallbackData->pMessage &&
            (std::strstr(pCallbackData->pMessage, "uses set ") != nullptr ||
             std::strstr(pCallbackData->pMessage, "descriptor set must have been bound") != nullptr ||
             std::strstr(pCallbackData->pMessage, "has never been updated") != nullptr);
        if (descriptorBindFailure || descriptorDrawFailure) {
            const auto lastBind = infernux::vkdebug::GetLastDescriptorBindSnapshot();
            if (lastBind.sequence > 0) {
                INXLOG_ERROR("[VkBindTrace] lastTrackedBind seq=", lastBind.sequence,
                             " site=", (lastBind.site ? lastBind.site : "<null>"), " firstSet=", lastBind.firstSet,
                             " count=", lastBind.descriptorSetCount, " cmd=0x", lastBind.commandBufferRaw, " layout=0x",
                             lastBind.pipelineLayoutRaw, " set[0]=0x", lastBind.descriptorSetRaws[0], " set[1]=0x",
                             lastBind.descriptorSetRaws[1], " set[2]=0x", lastBind.descriptorSetRaws[2], " set[3]=0x",
                             lastBind.descriptorSetRaws[3]);
            } else {
                INXLOG_ERROR("[VkBindTrace] no tracked bind call has been recorded yet");
            }

            uint64_t badRaw = 0ull;
            if (ExtractDescriptorRawFromValidationMessage(pCallbackData->pMessage, badRaw)) {
                std::ostringstream badRawHex;
                badRawHex << std::hex << badRaw;
                infernux::vkdebug::DescriptorBindTraceSnapshot matched{};
                uint32_t localIndex = 0;
                if (infernux::vkdebug::FindRecentDescriptorBindByRaw(badRaw, matched, localIndex)) {
                    INXLOG_ERROR("[VkBindTrace] matchedRaw=0x", badRawHex.str(),
                                 " at site=", (matched.site ? matched.site : "<null>"), " seq=", matched.sequence,
                                 " firstSet=", matched.firstSet, " localIndex=", localIndex,
                                 " absoluteSet=", (matched.firstSet + localIndex), " cmd=0x", matched.commandBufferRaw,
                                 " layout=0x", matched.pipelineLayoutRaw);
                } else {
                    INXLOG_ERROR("[VkBindTrace] no recorded bind matched invalid descriptor raw=0x", badRawHex.str());
                }
            }

            if (pCallbackData->cmdBufLabelCount > 0 && pCallbackData->pCmdBufLabels) {
                for (uint32_t i = 0; i < pCallbackData->cmdBufLabelCount; ++i) {
                    const char *name = pCallbackData->pCmdBufLabels[i].pLabelName;
                    if (name && name[0] != '\0') {
                        INXLOG_ERROR("[VkBindTrace] cmdBufLabel[", i, "]='", name, "'");
                    }
                }
            }
        }
    } else if (messageSeverity >= VK_DEBUG_UTILS_MESSAGE_SEVERITY_WARNING_BIT_EXT) {
        // Geometry programs intentionally share a stable superset vertex ABI.
        // Unlit/specialized fragments need not consume every varying; Vulkan
        // permits that interface and the driver discards unused outputs.
        if (IsIntentionalSupersetVertexInterfaceWarning(pCallbackData))
            return VK_FALSE;
        INXLOG_WARN("Vulkan Validation Warning: ", pCallbackData->pMessage);
    } else if (messageSeverity >= VK_DEBUG_UTILS_MESSAGE_SEVERITY_INFO_BIT_EXT) {
        INXLOG_INFO("Vulkan Validation Info: ", pCallbackData->pMessage);
    }

    return VK_FALSE; // Don't abort the call
}

// Helper to load extension function
static VkResult CreateDebugUtilsMessengerEXT(VkInstance instance, const VkDebugUtilsMessengerCreateInfoEXT *pCreateInfo,
                                             const VkAllocationCallbacks *pAllocator,
                                             VkDebugUtilsMessengerEXT *pDebugMessenger)
{
    auto func = reinterpret_cast<PFN_vkCreateDebugUtilsMessengerEXT>(
        vkGetInstanceProcAddr(instance, "vkCreateDebugUtilsMessengerEXT"));
    if (func != nullptr) {
        return func(instance, pCreateInfo, pAllocator, pDebugMessenger);
    }
    return VK_ERROR_EXTENSION_NOT_PRESENT;
}

static void DestroyDebugUtilsMessengerEXT(VkInstance instance, VkDebugUtilsMessengerEXT debugMessenger,
                                          const VkAllocationCallbacks *pAllocator)
{
    auto func = reinterpret_cast<PFN_vkDestroyDebugUtilsMessengerEXT>(
        vkGetInstanceProcAddr(instance, "vkDestroyDebugUtilsMessengerEXT"));
    if (func != nullptr) {
        func(instance, debugMessenger, pAllocator);
    }
}

// ============================================================================
// Constructor / Destructor / Move
// ============================================================================

VkDeviceContext::~VkDeviceContext()
{
    Destroy();
}

VkDeviceContext::VkDeviceContext(VkDeviceContext &&other) noexcept
    : m_instance(other.m_instance), m_debugMessenger(other.m_debugMessenger), m_surface(other.m_surface),
      m_physicalDevice(other.m_physicalDevice), m_device(other.m_device), m_vmaAllocator(other.m_vmaAllocator),
      m_graphicsQueue(other.m_graphicsQueue), m_computeQueue(other.m_computeQueue),
      m_presentQueue(other.m_presentQueue), m_transferQueue(other.m_transferQueue),
      m_hasDedicatedTransferQueue(other.m_hasDedicatedTransferQueue),
      m_hasIndependentComputeQueue(other.m_hasIndependentComputeQueue), m_queueIndices(other.m_queueIndices),
      m_deviceProperties(other.m_deviceProperties), m_deviceFeatures(other.m_deviceFeatures),
      m_capabilities(other.m_capabilities), m_rhiDevice(std::move(other.m_rhiDevice)),
      m_descriptorIndexingEnabled(other.m_descriptorIndexingEnabled),
      m_timelineSemaphoreEnabled(other.m_timelineSemaphoreEnabled), m_validationEnabled(other.m_validationEnabled),
      m_shuttingDown(other.m_shuttingDown), m_waitIdleCount(other.m_waitIdleCount)
{
    other.m_instance = VK_NULL_HANDLE;
    other.m_debugMessenger = VK_NULL_HANDLE;
    other.m_surface = VK_NULL_HANDLE;
    other.m_physicalDevice = VK_NULL_HANDLE;
    other.m_device = VK_NULL_HANDLE;
    other.m_vmaAllocator = VK_NULL_HANDLE;
    other.m_graphicsQueue = VK_NULL_HANDLE;
    other.m_computeQueue = VK_NULL_HANDLE;
    other.m_presentQueue = VK_NULL_HANDLE;
    other.m_transferQueue = VK_NULL_HANDLE;
    other.m_hasDedicatedTransferQueue = false;
    other.m_hasIndependentComputeQueue = false;
    other.m_capabilities = {};
    other.m_descriptorIndexingEnabled = false;
    other.m_timelineSemaphoreEnabled = false;
    other.m_shuttingDown = false;
    other.m_waitIdleCount = 0;
}

VkDeviceContext &VkDeviceContext::operator=(VkDeviceContext &&other) noexcept
{
    if (this != &other) {
        Destroy();

        m_instance = other.m_instance;
        m_debugMessenger = other.m_debugMessenger;
        m_surface = other.m_surface;
        m_physicalDevice = other.m_physicalDevice;
        m_device = other.m_device;
        m_vmaAllocator = other.m_vmaAllocator;
        m_graphicsQueue = other.m_graphicsQueue;
        m_computeQueue = other.m_computeQueue;
        m_presentQueue = other.m_presentQueue;
        m_transferQueue = other.m_transferQueue;
        m_hasDedicatedTransferQueue = other.m_hasDedicatedTransferQueue;
        m_hasIndependentComputeQueue = other.m_hasIndependentComputeQueue;
        m_queueIndices = other.m_queueIndices;
        m_deviceProperties = other.m_deviceProperties;
        m_deviceFeatures = other.m_deviceFeatures;
        m_capabilities = other.m_capabilities;
        m_rhiDevice = std::move(other.m_rhiDevice);
        m_descriptorIndexingEnabled = other.m_descriptorIndexingEnabled;
        m_timelineSemaphoreEnabled = other.m_timelineSemaphoreEnabled;
        m_validationEnabled = other.m_validationEnabled;
        m_shuttingDown = other.m_shuttingDown;
        m_waitIdleCount = other.m_waitIdleCount;

        other.m_instance = VK_NULL_HANDLE;
        other.m_debugMessenger = VK_NULL_HANDLE;
        other.m_surface = VK_NULL_HANDLE;
        other.m_physicalDevice = VK_NULL_HANDLE;
        other.m_device = VK_NULL_HANDLE;
        other.m_vmaAllocator = VK_NULL_HANDLE;
        other.m_graphicsQueue = VK_NULL_HANDLE;
        other.m_computeQueue = VK_NULL_HANDLE;
        other.m_presentQueue = VK_NULL_HANDLE;
        other.m_transferQueue = VK_NULL_HANDLE;
        other.m_hasDedicatedTransferQueue = false;
        other.m_hasIndependentComputeQueue = false;
        other.m_capabilities = {};
        other.m_descriptorIndexingEnabled = false;
        other.m_timelineSemaphoreEnabled = false;
        other.m_shuttingDown = false;
        other.m_waitIdleCount = 0;
    }
    return *this;
}

// ============================================================================
// Public Methods
// ============================================================================

bool VkDeviceContext::Initialize(SDL_Window *window, const DeviceConfig &config)
{
    INXLOG_INFO("Initializing Vulkan device context...");

    // Step 1: Create instance
    if (!CreateInstance(config)) {
        INXLOG_ERROR("Failed to create Vulkan instance");
        return false;
    }

    // Step 2: Setup debug messenger (if validation enabled)
    if (m_validationEnabled && !SetupDebugMessenger()) {
        INXLOG_WARN("Failed to setup debug messenger");
        // Not fatal, continue
    }

    // Step 3: Create surface
    if (!CreateSurface(window)) {
        INXLOG_ERROR("Failed to create window surface");
        return false;
    }

    // Step 4: Pick physical device
    if (!PickPhysicalDevice(config)) {
        INXLOG_ERROR("Failed to find suitable GPU");
        return false;
    }

    // Step 5: Create logical device
    if (!CreateLogicalDevice(config)) {
        INXLOG_ERROR("Failed to create logical device");
        return false;
    }
    BuildCapabilities();

    // Step 6: Create VMA allocator
    m_vmaAllocator = CreateVmaAllocator(m_instance, m_physicalDevice, m_device);
    if (m_vmaAllocator == VK_NULL_HANDLE) {
        INXLOG_ERROR("Failed to create VMA allocator");
        return false;
    }
    m_rhiDevice = std::make_unique<VulkanRhiDevice>(
        m_device, m_vmaAllocator, m_capabilities, m_queueIndices.graphicsFamily.value(),
        m_queueIndices.computeFamily.value(), m_queueIndices.transferFamily.value());

    INXLOG_INFO("Vulkan device context initialized successfully");
    INXLOG_INFO("  GPU: ", m_deviceProperties.deviceName);
    INXLOG_INFO("  API Version: ", VK_VERSION_MAJOR(m_deviceProperties.apiVersion), ".",
                VK_VERSION_MINOR(m_deviceProperties.apiVersion), ".", VK_VERSION_PATCH(m_deviceProperties.apiVersion));

    return true;
}

bool VkDeviceContext::InitializeInstance(const DeviceConfig &config)
{
    INXLOG_INFO("Initializing Vulkan instance (split mode)...");

    // Step 1: Create instance
    if (!CreateInstance(config)) {
        INXLOG_ERROR("Failed to create Vulkan instance");
        return false;
    }

    // Step 2: Setup debug messenger (if validation enabled)
    if (m_validationEnabled && !SetupDebugMessenger()) {
        INXLOG_WARN("Failed to setup debug messenger");
        // Not fatal, continue
    }

    INXLOG_INFO("Vulkan instance created successfully");
    return true;
}

bool VkDeviceContext::InitializeDevice(VkSurfaceKHR surface, const DeviceConfig &config)
{
    if (m_instance == VK_NULL_HANDLE) {
        INXLOG_ERROR("Instance not initialized. Call InitializeInstance first.");
        return false;
    }

    if (surface == VK_NULL_HANDLE) {
        INXLOG_ERROR("Invalid surface handle");
        return false;
    }

    INXLOG_INFO("Initializing Vulkan device with external surface...");

    // Store surface (we don't own it - created externally)
    m_surface = surface;

    // Step 1: Pick physical device
    if (!PickPhysicalDevice(config)) {
        INXLOG_ERROR("Failed to find suitable GPU");
        return false;
    }

    // Step 2: Create logical device
    if (!CreateLogicalDevice(config)) {
        INXLOG_ERROR("Failed to create logical device");
        return false;
    }
    BuildCapabilities();

    // Step 3: Create VMA allocator
    m_vmaAllocator = CreateVmaAllocator(m_instance, m_physicalDevice, m_device);
    if (m_vmaAllocator == VK_NULL_HANDLE) {
        INXLOG_ERROR("Failed to create VMA allocator");
        return false;
    }
    m_rhiDevice = std::make_unique<VulkanRhiDevice>(
        m_device, m_vmaAllocator, m_capabilities, m_queueIndices.graphicsFamily.value(),
        m_queueIndices.computeFamily.value(), m_queueIndices.transferFamily.value());

    INXLOG_INFO("Vulkan device initialized successfully");
    INXLOG_INFO("  GPU: ", m_deviceProperties.deviceName);
    INXLOG_INFO("  API Version: ", VK_VERSION_MAJOR(m_deviceProperties.apiVersion), ".",
                VK_VERSION_MINOR(m_deviceProperties.apiVersion), ".", VK_VERSION_PATCH(m_deviceProperties.apiVersion));

    return true;
}

void VkDeviceContext::WaitIdle() const
{
    if (m_device != VK_NULL_HANDLE && !m_shuttingDown) {
        ++m_waitIdleCount;
        vkDeviceWaitIdle(m_device);
    }
}

rhi::DeviceId VkDeviceContext::GetDeviceId() const noexcept
{
    return m_rhiDevice ? m_rhiDevice->GetDeviceId() : rhi::InvalidDeviceId;
}

void VkDeviceContext::Destroy() noexcept
{
    // Wait for device to be idle before cleanup (skip if already drained)
    if (!m_shuttingDown) {
        WaitIdle();
    }
    m_rhiDevice.reset();

    // Destroy in reverse order of creation
    // VMA must be destroyed before VkDevice
    if (m_vmaAllocator != VK_NULL_HANDLE) {
        DestroyVmaAllocator(m_vmaAllocator);
        m_vmaAllocator = VK_NULL_HANDLE;
    }

    if (m_device != VK_NULL_HANDLE) {
        vkDestroyDevice(m_device, nullptr);
        m_device = VK_NULL_HANDLE;
        m_graphicsQueue = VK_NULL_HANDLE;
        m_computeQueue = VK_NULL_HANDLE;
        m_presentQueue = VK_NULL_HANDLE;
        m_transferQueue = VK_NULL_HANDLE;
        m_hasIndependentComputeQueue = false;
        m_hasDedicatedTransferQueue = false;
    }

    m_physicalDevice = VK_NULL_HANDLE;
    m_capabilities = {};

    if (m_surface != VK_NULL_HANDLE && m_instance != VK_NULL_HANDLE) {
        vkDestroySurfaceKHR(m_instance, m_surface, nullptr);
        m_surface = VK_NULL_HANDLE;
    }

    if (m_debugMessenger != VK_NULL_HANDLE && m_instance != VK_NULL_HANDLE) {
        DestroyDebugUtilsMessengerEXT(m_instance, m_debugMessenger, nullptr);
        m_debugMessenger = VK_NULL_HANDLE;
    }

    if (m_instance != VK_NULL_HANDLE) {
        vkDestroyInstance(m_instance, nullptr);
        m_instance = VK_NULL_HANDLE;
    }
}

// ============================================================================
// Utility Methods
// ============================================================================

SwapchainSupportDetails VkDeviceContext::QuerySwapchainSupport() const
{
    SwapchainSupportDetails details;

    if (m_physicalDevice == VK_NULL_HANDLE || m_surface == VK_NULL_HANDLE) {
        return details;
    }

    // Capabilities
    vkGetPhysicalDeviceSurfaceCapabilitiesKHR(m_physicalDevice, m_surface, &details.capabilities);

    // Formats
    uint32_t formatCount = 0;
    vkGetPhysicalDeviceSurfaceFormatsKHR(m_physicalDevice, m_surface, &formatCount, nullptr);
    if (formatCount > 0) {
        details.formats.resize(formatCount);
        vkGetPhysicalDeviceSurfaceFormatsKHR(m_physicalDevice, m_surface, &formatCount, details.formats.data());
    }

    // Present modes
    uint32_t presentModeCount = 0;
    vkGetPhysicalDeviceSurfacePresentModesKHR(m_physicalDevice, m_surface, &presentModeCount, nullptr);
    if (presentModeCount > 0) {
        details.presentModes.resize(presentModeCount);
        vkGetPhysicalDeviceSurfacePresentModesKHR(m_physicalDevice, m_surface, &presentModeCount,
                                                  details.presentModes.data());
    }

    return details;
}

VkFormat VkDeviceContext::FindSupportedFormat(const std::vector<VkFormat> &candidates, VkImageTiling tiling,
                                              VkFormatFeatureFlags features) const
{
    for (VkFormat format : candidates) {
        VkFormatProperties props;
        vkGetPhysicalDeviceFormatProperties(m_physicalDevice, format, &props);

        if (tiling == VK_IMAGE_TILING_LINEAR && (props.linearTilingFeatures & features) == features) {
            return format;
        }
        if (tiling == VK_IMAGE_TILING_OPTIMAL && (props.optimalTilingFeatures & features) == features) {
            return format;
        }
    }

    return VK_FORMAT_UNDEFINED;
}

VkFormat VkDeviceContext::FindDepthFormat() const
{
    return FindSupportedFormat({VK_FORMAT_D32_SFLOAT, VK_FORMAT_D32_SFLOAT_S8_UINT, VK_FORMAT_D24_UNORM_S8_UINT},
                               VK_IMAGE_TILING_OPTIMAL, VK_FORMAT_FEATURE_DEPTH_STENCIL_ATTACHMENT_BIT);
}

VkFormat VkDeviceContext::FindSampledDepthFormat() const
{
    return FindSupportedFormat({VK_FORMAT_D32_SFLOAT, VK_FORMAT_D32_SFLOAT_S8_UINT, VK_FORMAT_D24_UNORM_S8_UINT},
                               VK_IMAGE_TILING_OPTIMAL,
                               VK_FORMAT_FEATURE_DEPTH_STENCIL_ATTACHMENT_BIT | VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT);
}

VkFormat VkDeviceContext::FindShadowMapDepthFormat() const
{
    return FindSampledDepthFormat();
}

bool VkDeviceContext::HasStencilComponent(VkFormat format)
{
    return format == VK_FORMAT_D32_SFLOAT_S8_UINT || format == VK_FORMAT_D24_UNORM_S8_UINT;
}

// ============================================================================
// Internal Initialization Methods
// ============================================================================

bool VkDeviceContext::CreateInstance(const DeviceConfig &config)
{
    m_validationEnabled = config.enableValidationLayers;

    // Check validation layer support
    if (m_validationEnabled && !CheckValidationLayerSupport()) {
        INXLOG_WARN("Validation layers requested but not available");
        m_validationEnabled = false;
    }

    // Application info
    VkApplicationInfo appInfo{};
    appInfo.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    appInfo.pApplicationName = config.appName;
    appInfo.applicationVersion = VK_MAKE_VERSION(1, 0, 0);
    appInfo.pEngineName = config.engineName;
    appInfo.engineVersion = VK_MAKE_VERSION(1, 0, 0);
    appInfo.apiVersion = VK_API_VERSION_1_2;

    // Get required extensions
    auto extensions = GetRequiredExtensions(m_validationEnabled);

    // Instance create info
    VkInstanceCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    createInfo.pApplicationInfo = &appInfo;
    createInfo.enabledExtensionCount = static_cast<uint32_t>(extensions.size());
    createInfo.ppEnabledExtensionNames = extensions.data();

#ifdef __APPLE__
    // MoltenVK portability subset: required for macOS Vulkan drivers
    createInfo.flags |= VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR;
#endif

    // Debug messenger create info (for instance creation/destruction debugging)
    VkDebugUtilsMessengerCreateInfoEXT debugCreateInfo{};

    if (m_validationEnabled) {
        createInfo.enabledLayerCount = static_cast<uint32_t>(VALIDATION_LAYERS.size());
        createInfo.ppEnabledLayerNames = VALIDATION_LAYERS.data();

        debugCreateInfo.sType = VK_STRUCTURE_TYPE_DEBUG_UTILS_MESSENGER_CREATE_INFO_EXT;
        debugCreateInfo.messageSeverity =
            VK_DEBUG_UTILS_MESSAGE_SEVERITY_WARNING_BIT_EXT | VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT;
        debugCreateInfo.messageType = VK_DEBUG_UTILS_MESSAGE_TYPE_GENERAL_BIT_EXT |
                                      VK_DEBUG_UTILS_MESSAGE_TYPE_VALIDATION_BIT_EXT |
                                      VK_DEBUG_UTILS_MESSAGE_TYPE_PERFORMANCE_BIT_EXT;
        debugCreateInfo.pfnUserCallback = DebugCallback;

        createInfo.pNext = &debugCreateInfo;
    } else {
        createInfo.enabledLayerCount = 0;
        createInfo.pNext = nullptr;
    }

    VkResult result = vkCreateInstance(&createInfo, nullptr, &m_instance);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("vkCreateInstance failed: ", VkResultToString(result));
        return false;
    }

    return true;
}

bool VkDeviceContext::SetupDebugMessenger()
{
    if (!m_validationEnabled) {
        return true;
    }

    VkDebugUtilsMessengerCreateInfoEXT createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_DEBUG_UTILS_MESSENGER_CREATE_INFO_EXT;
    createInfo.messageSeverity =
        VK_DEBUG_UTILS_MESSAGE_SEVERITY_VERBOSE_BIT_EXT | VK_DEBUG_UTILS_MESSAGE_SEVERITY_INFO_BIT_EXT |
        VK_DEBUG_UTILS_MESSAGE_SEVERITY_WARNING_BIT_EXT | VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT;
    createInfo.messageType = VK_DEBUG_UTILS_MESSAGE_TYPE_GENERAL_BIT_EXT |
                             VK_DEBUG_UTILS_MESSAGE_TYPE_VALIDATION_BIT_EXT |
                             VK_DEBUG_UTILS_MESSAGE_TYPE_PERFORMANCE_BIT_EXT;
    createInfo.pfnUserCallback = DebugCallback;
    createInfo.pUserData = nullptr;

    VkResult result = CreateDebugUtilsMessengerEXT(m_instance, &createInfo, nullptr, &m_debugMessenger);
    return result == VK_SUCCESS;
}

bool VkDeviceContext::CreateSurface(SDL_Window *window)
{
    if (!SDL_Vulkan_CreateSurface(window, m_instance, nullptr, &m_surface)) {
        INXLOG_ERROR("SDL_Vulkan_CreateSurface failed: ", SDL_GetError());
        return false;
    }
    return true;
}

bool VkDeviceContext::PickPhysicalDevice(const DeviceConfig &config)
{
    uint32_t deviceCount = 0;
    vkEnumeratePhysicalDevices(m_instance, &deviceCount, nullptr);

    if (deviceCount == 0) {
        INXLOG_ERROR("No GPUs with Vulkan support found");
        return false;
    }

    std::vector<VkPhysicalDevice> devices(deviceCount);
    vkEnumeratePhysicalDevices(m_instance, &deviceCount, devices.data());

    // Find the best suitable device
    int bestScore = 0;
    VkPhysicalDevice bestDevice = VK_NULL_HANDLE;

    for (const auto &device : devices) {
        if (IsDeviceSuitable(device, config)) {
            int score = RateDeviceSuitability(device);
            if (score > bestScore) {
                bestScore = score;
                bestDevice = device;
            }
        }
    }

    if (bestDevice == VK_NULL_HANDLE) {
        INXLOG_ERROR("No suitable GPU found");
        return false;
    }

    m_physicalDevice = bestDevice;
    m_queueIndices = FindQueueFamilies(m_physicalDevice);

    // Cache device properties
    vkGetPhysicalDeviceProperties(m_physicalDevice, &m_deviceProperties);
    vkGetPhysicalDeviceFeatures(m_physicalDevice, &m_deviceFeatures);

    return true;
}

bool VkDeviceContext::CreateLogicalDevice(const DeviceConfig &config)
{
    // Get unique queue families
    auto uniqueQueueFamilies = m_queueIndices.GetUniqueIndices();

    // Create queue create infos
    std::vector<VkDeviceQueueCreateInfo> queueCreateInfos;
    std::vector<std::array<float, 2>> queuePriorities(uniqueQueueFamilies.size(), {1.0f, 1.0f});

    for (size_t familyIndex = 0; familyIndex < uniqueQueueFamilies.size(); ++familyIndex) {
        const uint32_t queueFamily = uniqueQueueFamilies[familyIndex];
        VkDeviceQueueCreateInfo queueCreateInfo{};
        queueCreateInfo.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
        queueCreateInfo.queueFamilyIndex = queueFamily;
        queueCreateInfo.queueCount =
            queueFamily == m_queueIndices.computeFamily.value() ? m_queueIndices.computeQueueIndex + 1u : 1u;
        queueCreateInfo.pQueuePriorities = queuePriorities[familyIndex].data();
        queueCreateInfos.push_back(queueCreateInfo);
    }

    // ────────────────────────────────────────────────────────────────────
    // Device features — use the Vulkan 1.2 pNext chain so we can opt into
    // descriptor-indexing capabilities (UPDATE_AFTER_BIND, partially bound,
    // etc.) that let mid-frame descriptor writes proceed without a full
    // GPU drain.
    // ────────────────────────────────────────────────────────────────────

    // Query everything the GPU supports through the Vulkan 1.2 chain so we
    // can selectively enable only what we need.
    VkPhysicalDeviceVulkan12Features supported12{};
    supported12.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES;
    VkPhysicalDeviceFeatures2 supportedFeatures2{};
    supportedFeatures2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;
    supportedFeatures2.pNext = &supported12;
    vkGetPhysicalDeviceFeatures2(m_physicalDevice, &supportedFeatures2);
    const VkPhysicalDeviceFeatures &supportedFeatures = supportedFeatures2.features;

    VkPhysicalDeviceFeatures deviceFeatures{};
    deviceFeatures.samplerAnisotropy = VK_TRUE;
    deviceFeatures.fillModeNonSolid = VK_TRUE;              // For wireframe
    deviceFeatures.depthBiasClamp = VK_TRUE;                // For shadow depth bias clamping
    deviceFeatures.wideLines = supportedFeatures.wideLines; // For debug lines (when available)

    // Vulkan 1.2 features — opt into descriptor-indexing capabilities only
    // when the driver advertises them. UPDATE_AFTER_BIND is what unlocks
    // non-stalling material descriptor updates (see MaterialDescriptor.cpp).
    VkPhysicalDeviceVulkan12Features features12{};
    features12.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES;
    features12.descriptorIndexing = supported12.descriptorIndexing;
    features12.descriptorBindingPartiallyBound = supported12.descriptorBindingPartiallyBound;
    features12.descriptorBindingVariableDescriptorCount = supported12.descriptorBindingVariableDescriptorCount;
    features12.descriptorBindingSampledImageUpdateAfterBind = supported12.descriptorBindingSampledImageUpdateAfterBind;
    features12.descriptorBindingUniformBufferUpdateAfterBind =
        supported12.descriptorBindingUniformBufferUpdateAfterBind;
    features12.descriptorBindingStorageBufferUpdateAfterBind =
        supported12.descriptorBindingStorageBufferUpdateAfterBind;
    features12.descriptorBindingUpdateUnusedWhilePending = supported12.descriptorBindingUpdateUnusedWhilePending;
    features12.runtimeDescriptorArray = supported12.runtimeDescriptorArray;
    // Timeline semaphores enable lock-free producer/consumer sync between
    // upload tasks and the render thread without per-fence allocation.
    features12.timelineSemaphore = supported12.timelineSemaphore;

    m_descriptorIndexingEnabled = (features12.descriptorBindingSampledImageUpdateAfterBind == VK_TRUE);
    m_timelineSemaphoreEnabled = (features12.timelineSemaphore == VK_TRUE);

    VkPhysicalDeviceFeatures2 enabledFeatures2{};
    enabledFeatures2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;
    enabledFeatures2.features = deviceFeatures;
    enabledFeatures2.pNext = &features12;

    // Build device extension list
    std::vector<const char *> deviceExtensions(DEVICE_EXTENSIONS.begin(), DEVICE_EXTENSIONS.end());
#ifdef __APPLE__
    deviceExtensions.push_back("VK_KHR_portability_subset");
#endif

    // Device create info — enabledFeatures lives inside pNext (features2),
    // so pEnabledFeatures must be NULL per the Vulkan spec.
    VkDeviceCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
    createInfo.pNext = &enabledFeatures2;
    createInfo.queueCreateInfoCount = static_cast<uint32_t>(queueCreateInfos.size());
    createInfo.pQueueCreateInfos = queueCreateInfos.data();
    createInfo.pEnabledFeatures = nullptr;
    createInfo.enabledExtensionCount = static_cast<uint32_t>(deviceExtensions.size());
    createInfo.ppEnabledExtensionNames = deviceExtensions.data();

    // Validation layers (deprecated for devices, but included for older implementations)
    if (m_validationEnabled) {
        createInfo.enabledLayerCount = static_cast<uint32_t>(VALIDATION_LAYERS.size());
        createInfo.ppEnabledLayerNames = VALIDATION_LAYERS.data();
    } else {
        createInfo.enabledLayerCount = 0;
    }

    VkResult result = vkCreateDevice(m_physicalDevice, &createInfo, nullptr, &m_device);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("vkCreateDevice failed: ", VkResultToString(result));
        return false;
    }

    // Get queue handles
    vkGetDeviceQueue(m_device, m_queueIndices.graphicsFamily.value(), 0, &m_graphicsQueue);
    vkGetDeviceQueue(m_device, m_queueIndices.computeFamily.value(), m_queueIndices.computeQueueIndex, &m_computeQueue);
    vkGetDeviceQueue(m_device, m_queueIndices.presentFamily.value(), 0, &m_presentQueue);
    m_hasIndependentComputeQueue = m_computeQueue != VK_NULL_HANDLE && m_computeQueue != m_graphicsQueue;
    if (m_hasIndependentComputeQueue) {
        INXLOG_INFO("Async compute queue enabled (family ", m_queueIndices.computeFamily.value(), ", queue ",
                    m_queueIndices.computeQueueIndex, ")");
    }

    // Transfer queue: if the GPU advertised a dedicated transfer-only family
    // (FindQueueFamilies's second pass), grab that queue handle so async
    // upload work runs in parallel with the 3D queue. Otherwise alias to
    // the graphics queue so call sites can always dispatch uploads through
    // GetTransferQueue() without branching.
    const uint32_t transferFamily = m_queueIndices.transferFamily.value_or(m_queueIndices.graphicsFamily.value());
    m_hasDedicatedTransferQueue = (transferFamily != m_queueIndices.graphicsFamily.value());
    if (m_hasDedicatedTransferQueue) {
        vkGetDeviceQueue(m_device, transferFamily, 0, &m_transferQueue);
        INXLOG_INFO("Async transfer queue enabled (family ", transferFamily, ", dedicated DMA path)");
    } else {
        m_transferQueue = m_graphicsQueue;
    }

    return true;
}

void VkDeviceContext::BuildCapabilities()
{
    using namespace rhi;

    m_capabilities = {};
    if (m_physicalDevice == VK_NULL_HANDLE) {
        return;
    }

    auto &capabilities = m_capabilities;
    capabilities.backend = BackendType::Vulkan;
    capabilities.adapterType = ToRhiAdapterType(m_deviceProperties.deviceType);
    capabilities.SetAdapterName(m_deviceProperties.deviceName);
    capabilities.vendorId = m_deviceProperties.vendorID;
    capabilities.deviceId = m_deviceProperties.deviceID;
    capabilities.driverVersion = m_deviceProperties.driverVersion;
    capabilities.apiVersionMajor = VK_VERSION_MAJOR(m_deviceProperties.apiVersion);
    capabilities.apiVersionMinor = VK_VERSION_MINOR(m_deviceProperties.apiVersion);
    capabilities.apiVersionPatch = VK_VERSION_PATCH(m_deviceProperties.apiVersion);

    const auto &limits = m_deviceProperties.limits;
    capabilities.limits.maxTextureDimension1D = limits.maxImageDimension1D;
    capabilities.limits.maxTextureDimension2D = limits.maxImageDimension2D;
    capabilities.limits.maxTextureDimension3D = limits.maxImageDimension3D;
    capabilities.limits.maxTextureArrayLayers = limits.maxImageArrayLayers;
    capabilities.limits.maxColorAttachments = limits.maxColorAttachments;
    capabilities.limits.maxPushConstantBytes = limits.maxPushConstantsSize;
    capabilities.limits.maxSampledTexturesPerStage = limits.maxPerStageDescriptorSampledImages;
    capabilities.limits.maxStorageBuffersPerStage = limits.maxPerStageDescriptorStorageBuffers;
    capabilities.limits.maxSamplerAnisotropy = limits.maxSamplerAnisotropy;
    std::copy_n(limits.maxComputeWorkGroupCount, 3, capabilities.limits.maxComputeWorkgroupCount);
    std::copy_n(limits.maxComputeWorkGroupSize, 3, capabilities.limits.maxComputeWorkgroupSize);
    capabilities.limits.maxComputeWorkgroupInvocations = limits.maxComputeWorkGroupInvocations;

    capabilities.features.samplerAnisotropy = m_deviceFeatures.samplerAnisotropy == VK_TRUE;
    capabilities.features.fillModeNonSolid = m_deviceFeatures.fillModeNonSolid == VK_TRUE;
    capabilities.features.wideLines = m_deviceFeatures.wideLines == VK_TRUE;
    capabilities.features.descriptorIndexing = m_descriptorIndexingEnabled;
    capabilities.features.timelineSemaphore = m_timelineSemaphoreEnabled;
    capabilities.features.independentComputeQueue = m_hasIndependentComputeQueue;
    capabilities.features.dedicatedTransferQueue = m_hasDedicatedTransferQueue;

    uint32_t queueFamilyCount = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(m_physicalDevice, &queueFamilyCount, nullptr);
    std::vector<VkQueueFamilyProperties> queueProperties(queueFamilyCount);
    vkGetPhysicalDeviceQueueFamilyProperties(m_physicalDevice, &queueFamilyCount, queueProperties.data());
    if (m_queueIndices.graphicsFamily.has_value() && m_queueIndices.graphicsFamily.value() < queueProperties.size()) {
        const uint32_t validBits = queueProperties[m_queueIndices.graphicsFamily.value()].timestampValidBits;
        capabilities.timestampQueries.supported = validBits > 0;
        capabilities.timestampQueries.graphicsAndCompute = limits.timestampComputeAndGraphics == VK_TRUE;
        capabilities.timestampQueries.validBits = validBits;
        capabilities.timestampQueries.nanosecondsPerTick = limits.timestampPeriod;
    }

    for (size_t i = 1; i < kPixelFormatCount; ++i) {
        const auto format = static_cast<PixelFormat>(i);
        auto &formatCapabilities = capabilities.formats[i];
        formatCapabilities.format = format;

        VkFormatProperties properties{};
        const VkFormat vkFormat = ToVkFormat(format);
        vkGetPhysicalDeviceFormatProperties(m_physicalDevice, vkFormat, &properties);
        formatCapabilities.optimalTiling = ToRhiFormatFeatures(properties.optimalTilingFeatures);

        VkImageUsageFlags attachmentUsage = 0;
        if (HasAllFormatFeatures(formatCapabilities.optimalTiling, FormatFeature::ColorAttachment)) {
            attachmentUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
        } else if (HasAllFormatFeatures(formatCapabilities.optimalTiling, FormatFeature::DepthStencilAttachment)) {
            attachmentUsage = VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;
        }

        if (attachmentUsage != 0) {
            VkImageFormatProperties imageProperties{};
            if (vkGetPhysicalDeviceImageFormatProperties(m_physicalDevice, vkFormat, VK_IMAGE_TYPE_2D,
                                                         VK_IMAGE_TILING_OPTIMAL, attachmentUsage, 0,
                                                         &imageProperties) == VK_SUCCESS) {
                formatCapabilities.sampleCounts = ToRhiSampleCountMask(imageProperties.sampleCounts);
            }
        } else {
            formatCapabilities.sampleCounts = SampleCountBit(SampleCount::One);
        }
    }
}

rhi::SampleCountMask VkDeviceContext::GetImageSampleCountMask(VkFormat format, VkImageUsageFlags usage) const noexcept
{
    if (m_physicalDevice == VK_NULL_HANDLE || format == VK_FORMAT_UNDEFINED || usage == 0)
        return 0;

    VkImageFormatProperties properties{};
    if (vkGetPhysicalDeviceImageFormatProperties(m_physicalDevice, format, VK_IMAGE_TYPE_2D, VK_IMAGE_TILING_OPTIMAL,
                                                 usage, 0, &properties) != VK_SUCCESS) {
        return 0;
    }
    return ToRhiSampleCountMask(properties.sampleCounts);
}

QueueFamilyIndices VkDeviceContext::FindQueueFamilies(VkPhysicalDevice device) const
{
    QueueFamilyIndices indices;

    uint32_t queueFamilyCount = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(device, &queueFamilyCount, nullptr);

    std::vector<VkQueueFamilyProperties> queueFamilies(queueFamilyCount);
    vkGetPhysicalDeviceQueueFamilyProperties(device, &queueFamilyCount, queueFamilies.data());

    // First pass: pick the canonical graphics+present queue family. Most
    // discrete GPUs put graphics, present, transfer and compute on family 0.
    for (uint32_t i = 0; i < queueFamilyCount; i++) {
        const auto flags = queueFamilies[i].queueFlags;

        if ((flags & VK_QUEUE_GRAPHICS_BIT) && !indices.graphicsFamily.has_value()) {
            indices.graphicsFamily = i;
            // The graphics queue is always implicitly transfer capable per the
            // Vulkan spec — seed it as the conservative fallback so
            // FindQueueFamilies always returns a complete set even on devices
            // that expose only a single queue family.
            indices.transferFamily = i;
            if ((flags & VK_QUEUE_COMPUTE_BIT) != 0)
                indices.computeFamily = i;
        }

        VkBool32 presentSupport = VK_FALSE;
        vkGetPhysicalDeviceSurfaceSupportKHR(device, i, m_surface, &presentSupport);
        if (presentSupport && !indices.presentFamily.has_value()) {
            indices.presentFamily = i;
        }
    }

    // Prefer a compute-only family. If none exists, reserve a second queue
    // from the graphics family when the adapter exposes one.
    for (uint32_t i = 0; i < queueFamilyCount; ++i) {
        const auto flags = queueFamilies[i].queueFlags;
        if ((flags & VK_QUEUE_COMPUTE_BIT) != 0 && (flags & VK_QUEUE_GRAPHICS_BIT) == 0) {
            indices.computeFamily = i;
            indices.computeQueueIndex = 0;
            break;
        }
    }
    if (indices.computeFamily == indices.graphicsFamily && indices.graphicsFamily.has_value() &&
        queueFamilies[indices.graphicsFamily.value()].queueCount > 1) {
        indices.computeQueueIndex = 1;
    }

    // Second pass: prefer a DEDICATED transfer-only family if one exists.
    // Discrete NVIDIA GPUs typically expose family index 1 (or 2) as a
    // pure DMA / copy engine — using it for asset uploads runs in true
    // parallel with the main 3D queue. Falls back to the graphics family
    // (already seeded above) when no dedicated family is advertised.
    for (uint32_t i = 0; i < queueFamilyCount; i++) {
        const auto flags = queueFamilies[i].queueFlags;
        const bool hasTransfer = (flags & VK_QUEUE_TRANSFER_BIT) != 0;
        const bool hasGraphics = (flags & VK_QUEUE_GRAPHICS_BIT) != 0;
        const bool hasCompute = (flags & VK_QUEUE_COMPUTE_BIT) != 0;
        if (hasTransfer && !hasGraphics && !hasCompute) {
            indices.transferFamily = i;
            break;
        }
    }

    return indices;
}

bool VkDeviceContext::IsDeviceSuitable(VkPhysicalDevice device, const DeviceConfig &config) const
{
    // Check queue families
    QueueFamilyIndices indices = FindQueueFamilies(device);
    if (!indices.IsComplete()) {
        return false;
    }

    // Check extension support
    std::vector<const char *> requiredExtensions(DEVICE_EXTENSIONS.begin(), DEVICE_EXTENSIONS.end());
#ifdef __APPLE__
    requiredExtensions.push_back("VK_KHR_portability_subset");
#endif

    if (!CheckDeviceExtensionSupport(device, requiredExtensions)) {
        return false;
    }

    // Check swapchain support
    SwapchainSupportDetails swapchainSupport;
    vkGetPhysicalDeviceSurfaceCapabilitiesKHR(device, m_surface, &swapchainSupport.capabilities);

    uint32_t formatCount = 0;
    vkGetPhysicalDeviceSurfaceFormatsKHR(device, m_surface, &formatCount, nullptr);

    uint32_t presentModeCount = 0;
    vkGetPhysicalDeviceSurfacePresentModesKHR(device, m_surface, &presentModeCount, nullptr);

    if (formatCount == 0 || presentModeCount == 0) {
        return false;
    }

    // Check required features
    VkPhysicalDeviceFeatures supportedFeatures;
    vkGetPhysicalDeviceFeatures(device, &supportedFeatures);

    if (!supportedFeatures.samplerAnisotropy) {
        return false;
    }

    return true;
}

int VkDeviceContext::RateDeviceSuitability(VkPhysicalDevice device) const
{
    VkPhysicalDeviceProperties deviceProperties;
    VkPhysicalDeviceFeatures deviceFeatures;
    vkGetPhysicalDeviceProperties(device, &deviceProperties);
    vkGetPhysicalDeviceFeatures(device, &deviceFeatures);

    int score = 0;

    // Discrete GPUs have significant performance advantage
    if (deviceProperties.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU) {
        score += 1000;
    } else if (deviceProperties.deviceType == VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU) {
        score += 100;
    }

    // Maximum possible size of textures affects graphics quality
    score += deviceProperties.limits.maxImageDimension2D;

    // More VRAM is better
    VkPhysicalDeviceMemoryProperties memProps;
    vkGetPhysicalDeviceMemoryProperties(device, &memProps);
    for (uint32_t i = 0; i < memProps.memoryHeapCount; i++) {
        if (memProps.memoryHeaps[i].flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) {
            score += static_cast<int>(memProps.memoryHeaps[i].size / (1024 * 1024)); // MB
        }
    }

    return score;
}

bool VkDeviceContext::CheckDeviceExtensionSupport(VkPhysicalDevice device,
                                                  const std::vector<const char *> &extensions) const
{
    uint32_t extensionCount = 0;
    vkEnumerateDeviceExtensionProperties(device, nullptr, &extensionCount, nullptr);

    std::vector<VkExtensionProperties> availableExtensions(extensionCount);
    vkEnumerateDeviceExtensionProperties(device, nullptr, &extensionCount, availableExtensions.data());

    std::set<std::string> requiredExtensions(extensions.begin(), extensions.end());

    for (const auto &extension : availableExtensions) {
        requiredExtensions.erase(extension.extensionName);
    }

    return requiredExtensions.empty();
}

std::vector<const char *> VkDeviceContext::GetRequiredExtensions(bool enableValidation) const
{
    // Get SDL required extensions
    uint32_t sdlExtensionCount = 0;
    const char *const *sdlExtensions = SDL_Vulkan_GetInstanceExtensions(&sdlExtensionCount);

    std::vector<const char *> extensions;
    if (sdlExtensions) {
        extensions.insert(extensions.end(), sdlExtensions, sdlExtensions + sdlExtensionCount);
    }

    // Add debug utils if validation enabled
    if (enableValidation) {
        extensions.push_back(VK_EXT_DEBUG_UTILS_EXTENSION_NAME);
    }

#ifdef __APPLE__
    // MoltenVK requires portability enumeration to expose Vulkan drivers
    extensions.push_back(VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME);
#endif

    return extensions;
}

bool VkDeviceContext::CheckValidationLayerSupport() const
{
    uint32_t layerCount = 0;
    vkEnumerateInstanceLayerProperties(&layerCount, nullptr);

    std::vector<VkLayerProperties> availableLayers(layerCount);
    vkEnumerateInstanceLayerProperties(&layerCount, availableLayers.data());

    for (const char *layerName : VALIDATION_LAYERS) {
        bool layerFound = false;
        for (const auto &layerProperties : availableLayers) {
            if (strcmp(layerName, layerProperties.layerName) == 0) {
                layerFound = true;
                break;
            }
        }
        if (!layerFound) {
            return false;
        }
    }

    return true;
}

} // namespace vk
} // namespace infernux
