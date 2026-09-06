#include "VulkanRhiDevice.h"

#include "DescriptorBindTrace.h"
#include "RhiVulkanTypes.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <mutex>

namespace infernux::vk
{

namespace
{

VkBufferUsageFlags ToVkBufferUsage(rhi::BufferUsageFlags usage)
{
    VkBufferUsageFlags result = 0;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::Storage))
        result |= VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::Uniform))
        result |= VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::Vertex))
        result |= VK_BUFFER_USAGE_VERTEX_BUFFER_BIT;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::Index))
        result |= VK_BUFFER_USAGE_INDEX_BUFFER_BIT;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::Indirect))
        result |= VK_BUFFER_USAGE_INDIRECT_BUFFER_BIT;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::TransferSource))
        result |= VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::TransferDestination))
        result |= VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    return result;
}

VkImageUsageFlags ToVkTextureUsage(rhi::TextureUsageFlags usage)
{
    VkImageUsageFlags result = 0;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::Sampled))
        result |= VK_IMAGE_USAGE_SAMPLED_BIT;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::Storage))
        result |= VK_IMAGE_USAGE_STORAGE_BIT;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::ColorAttachment))
        result |= VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::DepthStencilAttachment))
        result |= VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::TransferSource))
        result |= VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::TransferDestination))
        result |= VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    return result;
}

rhi::FormatFeature RequiredFormatFeatures(rhi::TextureUsageFlags usage)
{
    rhi::FormatFeature required = rhi::FormatFeature::None;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::Sampled))
        required |= rhi::FormatFeature::Sampled;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::Storage))
        required |= rhi::FormatFeature::Storage;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::ColorAttachment))
        required |= rhi::FormatFeature::ColorAttachment;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::DepthStencilAttachment))
        required |= rhi::FormatFeature::DepthStencilAttachment;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::TransferSource))
        required |= rhi::FormatFeature::TransferSource;
    if (rhi::HasTextureUsage(usage, rhi::TextureUsageFlags::TransferDestination))
        required |= rhi::FormatFeature::TransferDestination;
    return required;
}

VkImageType ToVkImageType(rhi::TextureDimension dimension)
{
    switch (dimension) {
    case rhi::TextureDimension::Texture1D:
        return VK_IMAGE_TYPE_1D;
    case rhi::TextureDimension::Texture2D:
        return VK_IMAGE_TYPE_2D;
    case rhi::TextureDimension::Texture3D:
        return VK_IMAGE_TYPE_3D;
    }
    return VK_IMAGE_TYPE_MAX_ENUM;
}

VkImageViewType ToVkImageViewType(rhi::TextureViewDimension dimension)
{
    switch (dimension) {
    case rhi::TextureViewDimension::Texture1D:
        return VK_IMAGE_VIEW_TYPE_1D;
    case rhi::TextureViewDimension::Texture1DArray:
        return VK_IMAGE_VIEW_TYPE_1D_ARRAY;
    case rhi::TextureViewDimension::Texture2D:
        return VK_IMAGE_VIEW_TYPE_2D;
    case rhi::TextureViewDimension::Texture2DArray:
        return VK_IMAGE_VIEW_TYPE_2D_ARRAY;
    case rhi::TextureViewDimension::Texture3D:
        return VK_IMAGE_VIEW_TYPE_3D;
    case rhi::TextureViewDimension::Cube:
        return VK_IMAGE_VIEW_TYPE_CUBE;
    case rhi::TextureViewDimension::CubeArray:
        return VK_IMAGE_VIEW_TYPE_CUBE_ARRAY;
    }
    return VK_IMAGE_VIEW_TYPE_MAX_ENUM;
}

VkImageAspectFlags ToVkImageAspect(rhi::TextureAspect aspect)
{
    switch (aspect) {
    case rhi::TextureAspect::Color:
        return VK_IMAGE_ASPECT_COLOR_BIT;
    case rhi::TextureAspect::Depth:
        return VK_IMAGE_ASPECT_DEPTH_BIT;
    case rhi::TextureAspect::Stencil:
        return VK_IMAGE_ASPECT_STENCIL_BIT;
    case rhi::TextureAspect::DepthStencil:
        return VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT;
    }
    return 0;
}

VkFilter ToVkFilter(rhi::FilterMode filter)
{
    return filter == rhi::FilterMode::Nearest ? VK_FILTER_NEAREST : VK_FILTER_LINEAR;
}

VkSamplerMipmapMode ToVkMipFilter(rhi::FilterMode filter)
{
    return filter == rhi::FilterMode::Nearest ? VK_SAMPLER_MIPMAP_MODE_NEAREST : VK_SAMPLER_MIPMAP_MODE_LINEAR;
}

VkSamplerAddressMode ToVkAddressMode(rhi::AddressMode address)
{
    switch (address) {
    case rhi::AddressMode::Repeat:
        return VK_SAMPLER_ADDRESS_MODE_REPEAT;
    case rhi::AddressMode::MirroredRepeat:
        return VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT;
    case rhi::AddressMode::ClampToEdge:
        return VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    }
    return VK_SAMPLER_ADDRESS_MODE_MAX_ENUM;
}

VkDescriptorType ToVkDescriptorType(rhi::BindingType type)
{
    switch (type) {
    case rhi::BindingType::UniformBuffer:
        return VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    case rhi::BindingType::StorageBuffer:
        return VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    case rhi::BindingType::SampledTexture:
        return VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE;
    case rhi::BindingType::StorageTexture:
        return VK_DESCRIPTOR_TYPE_STORAGE_IMAGE;
    case rhi::BindingType::Sampler:
        return VK_DESCRIPTOR_TYPE_SAMPLER;
    case rhi::BindingType::CombinedTextureSampler:
        return VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    }
    return VK_DESCRIPTOR_TYPE_MAX_ENUM;
}

VkPrimitiveTopology ToVkTopology(rhi::PrimitiveTopology topology)
{
    switch (topology) {
    case rhi::PrimitiveTopology::TriangleList:
        return VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
    case rhi::PrimitiveTopology::TriangleStrip:
        return VK_PRIMITIVE_TOPOLOGY_TRIANGLE_STRIP;
    case rhi::PrimitiveTopology::LineList:
        return VK_PRIMITIVE_TOPOLOGY_LINE_LIST;
    }
    return VK_PRIMITIVE_TOPOLOGY_MAX_ENUM;
}

VkCullModeFlags ToVkCullMode(rhi::CullMode mode)
{
    switch (mode) {
    case rhi::CullMode::None:
        return VK_CULL_MODE_NONE;
    case rhi::CullMode::Front:
        return VK_CULL_MODE_FRONT_BIT;
    case rhi::CullMode::Back:
        return VK_CULL_MODE_BACK_BIT;
    }
    return VK_CULL_MODE_FLAG_BITS_MAX_ENUM;
}

VkFrontFace ToVkFrontFace(rhi::FrontFace face)
{
    return face == rhi::FrontFace::Clockwise ? VK_FRONT_FACE_CLOCKWISE : VK_FRONT_FACE_COUNTER_CLOCKWISE;
}

VkCompareOp ToVkCompareOp(rhi::CompareFunction function)
{
    switch (function) {
    case rhi::CompareFunction::Never:
        return VK_COMPARE_OP_NEVER;
    case rhi::CompareFunction::Less:
        return VK_COMPARE_OP_LESS;
    case rhi::CompareFunction::Equal:
        return VK_COMPARE_OP_EQUAL;
    case rhi::CompareFunction::LessEqual:
        return VK_COMPARE_OP_LESS_OR_EQUAL;
    case rhi::CompareFunction::Greater:
        return VK_COMPARE_OP_GREATER;
    case rhi::CompareFunction::NotEqual:
        return VK_COMPARE_OP_NOT_EQUAL;
    case rhi::CompareFunction::GreaterEqual:
        return VK_COMPARE_OP_GREATER_OR_EQUAL;
    case rhi::CompareFunction::Always:
        return VK_COMPARE_OP_ALWAYS;
    }
    return VK_COMPARE_OP_MAX_ENUM;
}

bool IsCoreVersionAtLeast(uint32_t version, uint32_t major, uint32_t minor) noexcept
{
    return VK_VERSION_MAJOR(version) > major ||
           (VK_VERSION_MAJOR(version) == major && VK_VERSION_MINOR(version) >= minor);
}

bool HasExtension(const std::vector<VkExtensionProperties> &extensions, const char *name) noexcept
{
    return std::any_of(extensions.begin(), extensions.end(), [name](const VkExtensionProperties &extension) {
        return std::strcmp(extension.extensionName, name) == 0;
    });
}

template <typename Feature> void AppendFeature(void *&tail, VkPhysicalDeviceFeatures2 &root, Feature &feature) noexcept
{
    auto *node = reinterpret_cast<VkBaseOutStructure *>(&feature);
    node->pNext = nullptr;
    if (tail == nullptr)
        root.pNext = node;
    else
        reinterpret_cast<VkBaseOutStructure *>(tail)->pNext = node;
    tail = node;
}

template <typename Property>
void AppendProperty(void *&tail, VkPhysicalDeviceProperties2 &root, Property &property) noexcept
{
    auto *node = reinterpret_cast<VkBaseOutStructure *>(&property);
    node->pNext = nullptr;
    if (tail == nullptr)
        root.pNext = node;
    else
        reinterpret_cast<VkBaseOutStructure *>(tail)->pNext = node;
    tail = node;
}

void SetEnabled(rhi::DeviceCapabilityStatus &status) noexcept
{
    status.enabled = status.supported;
}

} // namespace

VulkanCapabilityProbeData VulkanCapabilitySnapshot::QueryProbe(VkPhysicalDevice physicalDevice,
                                                               uint32_t apiVersionLimit)
{
    VulkanCapabilityProbeData probe{};
    if (physicalDevice == VK_NULL_HANDLE)
        return probe;

    vkGetPhysicalDeviceProperties(physicalDevice, &probe.properties);
    // A physical device may expose a newer core API than the application
    // negotiated for its instance. Promoted features above that effective
    // version must still use their KHR/EXT feature structs and entry points.
    probe.apiVersion = std::min(probe.properties.apiVersion, apiVersionLimit);
    vkGetPhysicalDeviceFeatures(physicalDevice, &probe.coreFeatures);

    uint32_t extensionCount = 0;
    if (vkEnumerateDeviceExtensionProperties(physicalDevice, nullptr, &extensionCount, nullptr) != VK_SUCCESS)
        return probe;
    std::vector<VkExtensionProperties> extensions(extensionCount);
    if (extensionCount != 0 &&
        vkEnumerateDeviceExtensionProperties(physicalDevice, nullptr, &extensionCount, extensions.data()) != VK_SUCCESS)
        return probe;

    probe.descriptorIndexingExtension = HasExtension(extensions, VK_EXT_DESCRIPTOR_INDEXING_EXTENSION_NAME);
    probe.timelineSemaphoreExtension = HasExtension(extensions, VK_KHR_TIMELINE_SEMAPHORE_EXTENSION_NAME);
    probe.dynamicRenderingExtension = HasExtension(extensions, VK_KHR_DYNAMIC_RENDERING_EXTENSION_NAME);
    probe.synchronization2Extension = HasExtension(extensions, VK_KHR_SYNCHRONIZATION_2_EXTENSION_NAME);

    if (IsCoreVersionAtLeast(probe.apiVersion, 1, 1)) {
        VkPhysicalDeviceFeatures2 features2{};
        features2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;
        probe.vulkan12Features.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES;
        probe.vulkan13Features.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES;
        probe.descriptorIndexingFeaturesEXT.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_INDEXING_FEATURES_EXT;
        probe.timelineSemaphoreFeaturesKHR.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_TIMELINE_SEMAPHORE_FEATURES_KHR;
        probe.dynamicRenderingFeaturesKHR.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DYNAMIC_RENDERING_FEATURES_KHR;
        probe.synchronization2FeaturesKHR.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SYNCHRONIZATION_2_FEATURES_KHR;
        void *tail = nullptr;
        if (IsCoreVersionAtLeast(probe.apiVersion, 1, 2))
            AppendFeature(tail, features2, probe.vulkan12Features);
        if (IsCoreVersionAtLeast(probe.apiVersion, 1, 3))
            AppendFeature(tail, features2, probe.vulkan13Features);
        if (!IsCoreVersionAtLeast(probe.apiVersion, 1, 2) && probe.descriptorIndexingExtension)
            AppendFeature(tail, features2, probe.descriptorIndexingFeaturesEXT);
        if (!IsCoreVersionAtLeast(probe.apiVersion, 1, 2) && probe.timelineSemaphoreExtension)
            AppendFeature(tail, features2, probe.timelineSemaphoreFeaturesKHR);
        if (!IsCoreVersionAtLeast(probe.apiVersion, 1, 3) && probe.dynamicRenderingExtension)
            AppendFeature(tail, features2, probe.dynamicRenderingFeaturesKHR);
        if (!IsCoreVersionAtLeast(probe.apiVersion, 1, 3) && probe.synchronization2Extension)
            AppendFeature(tail, features2, probe.synchronization2FeaturesKHR);
        vkGetPhysicalDeviceFeatures2(physicalDevice, &features2);
        probe.coreFeatures = features2.features;

        VkPhysicalDeviceProperties2 properties2{};
        properties2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2;
        probe.vulkan12Properties.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_PROPERTIES;
        probe.vulkan13Properties.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_PROPERTIES;
        void *propertyTail = nullptr;
        if (IsCoreVersionAtLeast(probe.apiVersion, 1, 2))
            AppendProperty(propertyTail, properties2, probe.vulkan12Properties);
        if (IsCoreVersionAtLeast(probe.apiVersion, 1, 3))
            AppendProperty(propertyTail, properties2, probe.vulkan13Properties);
        vkGetPhysicalDeviceProperties2(physicalDevice, &properties2);
        probe.properties = properties2.properties;
    }
    return probe;
}

VulkanCapabilitySnapshot VulkanCapabilitySnapshot::FromProbe(const VulkanCapabilityProbeData &probe) noexcept
{
    VulkanCapabilitySnapshot result{};
    result.apiVersion = probe.apiVersion;
    result.properties = probe.properties;
    result.vulkan12Properties = probe.vulkan12Properties;
    result.vulkan13Properties = probe.vulkan13Properties;
    result.descriptorIndexingExtension = probe.descriptorIndexingExtension;
    result.timelineSemaphoreExtension = probe.timelineSemaphoreExtension;
    result.dynamicRenderingExtension = probe.dynamicRenderingExtension;
    result.synchronization2Extension = probe.synchronization2Extension;

    const bool core12 = IsCoreVersionAtLeast(probe.apiVersion, 1, 2);
    const bool core13 = IsCoreVersionAtLeast(probe.apiVersion, 1, 3);
    const auto &v12 = probe.vulkan12Features;
    const auto &v13 = probe.vulkan13Features;
    const auto &descriptor = probe.descriptorIndexingFeaturesEXT;
    const bool descriptorIndexing = core12 ? v12.descriptorIndexing == VK_TRUE
                                           : probe.descriptorIndexingExtension &&
                                                 descriptor.runtimeDescriptorArray == VK_TRUE &&
                                                 descriptor.descriptorBindingPartiallyBound == VK_TRUE &&
                                                 descriptor.descriptorBindingVariableDescriptorCount == VK_TRUE &&
                                                 descriptor.shaderSampledImageArrayNonUniformIndexing == VK_TRUE;
    const bool runtimeDescriptorArray =
        core12 ? v12.runtimeDescriptorArray == VK_TRUE
               : probe.descriptorIndexingExtension && descriptor.runtimeDescriptorArray == VK_TRUE;
    const bool sampledImageNonUniform =
        core12 ? v12.shaderSampledImageArrayNonUniformIndexing == VK_TRUE
               : probe.descriptorIndexingExtension && descriptor.shaderSampledImageArrayNonUniformIndexing == VK_TRUE;
    const bool partiallyBound =
        core12 ? v12.descriptorBindingPartiallyBound == VK_TRUE
               : probe.descriptorIndexingExtension && descriptor.descriptorBindingPartiallyBound == VK_TRUE;
    const bool variableCount =
        core12 ? v12.descriptorBindingVariableDescriptorCount == VK_TRUE
               : probe.descriptorIndexingExtension && descriptor.descriptorBindingVariableDescriptorCount == VK_TRUE;
    const bool sampledUpdateAfterBind = core12 ? v12.descriptorBindingSampledImageUpdateAfterBind == VK_TRUE
                                               : probe.descriptorIndexingExtension &&
                                                     descriptor.descriptorBindingSampledImageUpdateAfterBind == VK_TRUE;
    const bool storageUpdateAfterBind =
        core12
            ? v12.descriptorBindingStorageBufferUpdateAfterBind == VK_TRUE
            : probe.descriptorIndexingExtension && descriptor.descriptorBindingStorageBufferUpdateAfterBind == VK_TRUE;
    const bool uniformUpdateAfterBind =
        core12
            ? v12.descriptorBindingUniformBufferUpdateAfterBind == VK_TRUE
            : probe.descriptorIndexingExtension && descriptor.descriptorBindingUniformBufferUpdateAfterBind == VK_TRUE;
    const bool updateUnused =
        core12 ? v12.descriptorBindingUpdateUnusedWhilePending == VK_TRUE
               : probe.descriptorIndexingExtension && descriptor.descriptorBindingUpdateUnusedWhilePending == VK_TRUE;

    result.supported.bindless.descriptorIndexing = {descriptorIndexing, false};
    result.supported.bindless.runtimeDescriptorArray = {runtimeDescriptorArray, false};
    result.supported.bindless.shaderSampledImageArrayNonUniformIndexing = {sampledImageNonUniform, false};
    result.supported.bindless.descriptorBindingPartiallyBound = {partiallyBound, false};
    result.supported.bindless.descriptorBindingVariableDescriptorCount = {variableCount, false};
    result.supported.bindless.descriptorBindingSampledImageUpdateAfterBind = {sampledUpdateAfterBind, false};
    result.supported.bindless.descriptorBindingUniformBufferUpdateAfterBind = {uniformUpdateAfterBind, false};
    result.supported.bindless.descriptorBindingStorageBufferUpdateAfterBind = {storageUpdateAfterBind, false};
    result.supported.bindless.descriptorBindingUpdateUnusedWhilePending = {updateUnused, false};

    const bool timeline =
        core12 ? v12.timelineSemaphore == VK_TRUE
               : probe.timelineSemaphoreExtension && probe.timelineSemaphoreFeaturesKHR.timelineSemaphore == VK_TRUE;
    const bool dynamicRendering =
        core13 ? v13.dynamicRendering == VK_TRUE
               : probe.dynamicRenderingExtension && probe.dynamicRenderingFeaturesKHR.dynamicRendering == VK_TRUE;
    const bool synchronization2 =
        core13 ? v13.synchronization2 == VK_TRUE
               : probe.synchronization2Extension && probe.synchronization2FeaturesKHR.synchronization2 == VK_TRUE;
    result.supported.timelineSemaphore = {timeline, false};
    result.supported.dynamicRendering = {dynamicRendering, false};
    result.supported.synchronization2 = {synchronization2, false};
    result.supported.submit2 = {synchronization2, false};
    return result;
}

VulkanDeviceFeatureChain::VulkanDeviceFeatureChain(const VulkanCapabilitySnapshot &supported) noexcept
    : m_supported(supported)
{
    ResetChain();
}

void VulkanDeviceFeatureChain::ResetChain() noexcept
{
    m_features2 = {};
    m_features2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;
    m_vulkan12 = {};
    m_vulkan12.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES;
    m_vulkan13 = {};
    m_vulkan13.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES;
    m_descriptorIndexingEXT = {};
    m_descriptorIndexingEXT.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_INDEXING_FEATURES_EXT;
    m_timelineSemaphoreKHR = {};
    m_timelineSemaphoreKHR.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_TIMELINE_SEMAPHORE_FEATURES_KHR;
    m_dynamicRenderingKHR = {};
    m_dynamicRenderingKHR.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DYNAMIC_RENDERING_FEATURES_KHR;
    m_synchronization2KHR = {};
    m_synchronization2KHR.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SYNCHRONIZATION_2_FEATURES_KHR;
    m_lastFeature = nullptr;
    m_vulkan12Linked = false;
    m_vulkan13Linked = false;
    m_descriptorIndexingEXTLinked = false;
    m_timelineSemaphoreKHRLinked = false;
    m_dynamicRenderingKHRLinked = false;
    m_synchronization2KHRLinked = false;
    m_enabled = m_supported.supported;
    m_enabled.bindless.descriptorIndexing.enabled = false;
    m_enabled.bindless.runtimeDescriptorArray.enabled = false;
    m_enabled.bindless.shaderSampledImageArrayNonUniformIndexing.enabled = false;
    m_enabled.bindless.descriptorBindingPartiallyBound.enabled = false;
    m_enabled.bindless.descriptorBindingVariableDescriptorCount.enabled = false;
    m_enabled.bindless.descriptorBindingSampledImageUpdateAfterBind.enabled = false;
    m_enabled.bindless.descriptorBindingUniformBufferUpdateAfterBind.enabled = false;
    m_enabled.bindless.descriptorBindingStorageBufferUpdateAfterBind.enabled = false;
    m_enabled.bindless.descriptorBindingUpdateUnusedWhilePending.enabled = false;
    m_enabled.dynamicRendering.enabled = false;
    m_enabled.timelineSemaphore.enabled = false;
    m_enabled.synchronization2.enabled = false;
    m_enabled.submit2.enabled = false;
}

void VulkanDeviceFeatureChain::Link(void *feature) noexcept
{
    auto *node = reinterpret_cast<VkBaseOutStructure *>(feature);
    node->pNext = nullptr;
    if (m_lastFeature == nullptr)
        m_features2.pNext = node;
    else
        reinterpret_cast<VkBaseOutStructure *>(m_lastFeature)->pNext = node;
    m_lastFeature = node;
}

bool VulkanDeviceFeatureChain::EnableDescriptorIndexing() noexcept
{
    if (!m_supported.supported.bindless.IsSupported()) {
        m_failure = {rhi::DeviceCapabilityDiagnosticCode::IncompleteDescriptorIndexing,
                     rhi::DeviceCapability::DescriptorIndexing};
        return false;
    }
    const bool core12 = IsCoreVersionAtLeast(m_supported.apiVersion, 1, 2);
    if (core12) {
        if (!m_vulkan12Linked) {
            Link(&m_vulkan12);
            m_vulkan12Linked = true;
        }
        m_vulkan12.descriptorIndexing = VK_TRUE;
        m_vulkan12.runtimeDescriptorArray = VK_TRUE;
        m_vulkan12.shaderSampledImageArrayNonUniformIndexing = VK_TRUE;
        m_vulkan12.descriptorBindingPartiallyBound = VK_TRUE;
        m_vulkan12.descriptorBindingVariableDescriptorCount = VK_TRUE;
        m_vulkan12.descriptorBindingSampledImageUpdateAfterBind = VK_TRUE;
        m_vulkan12.descriptorBindingUniformBufferUpdateAfterBind =
            m_supported.supported.bindless.descriptorBindingUniformBufferUpdateAfterBind.supported;
        m_vulkan12.descriptorBindingStorageBufferUpdateAfterBind =
            m_supported.supported.bindless.descriptorBindingStorageBufferUpdateAfterBind.supported;
        m_vulkan12.descriptorBindingUpdateUnusedWhilePending =
            m_supported.supported.bindless.descriptorBindingUpdateUnusedWhilePending.supported;
    } else if (m_supported.descriptorIndexingExtension) {
        if (!m_descriptorIndexingEXTLinked) {
            Link(&m_descriptorIndexingEXT);
            m_descriptorIndexingEXTLinked = true;
        }
        m_descriptorIndexingEXT.runtimeDescriptorArray = VK_TRUE;
        m_descriptorIndexingEXT.shaderSampledImageArrayNonUniformIndexing = VK_TRUE;
        m_descriptorIndexingEXT.descriptorBindingPartiallyBound = VK_TRUE;
        m_descriptorIndexingEXT.descriptorBindingVariableDescriptorCount = VK_TRUE;
        m_descriptorIndexingEXT.descriptorBindingSampledImageUpdateAfterBind = VK_TRUE;
        m_descriptorIndexingEXT.descriptorBindingUniformBufferUpdateAfterBind =
            m_supported.supported.bindless.descriptorBindingUniformBufferUpdateAfterBind.supported;
        m_descriptorIndexingEXT.descriptorBindingStorageBufferUpdateAfterBind =
            m_supported.supported.bindless.descriptorBindingStorageBufferUpdateAfterBind.supported;
        m_descriptorIndexingEXT.descriptorBindingUpdateUnusedWhilePending =
            m_supported.supported.bindless.descriptorBindingUpdateUnusedWhilePending.supported;
    } else {
        m_failure = {rhi::DeviceCapabilityDiagnosticCode::Unsupported, rhi::DeviceCapability::DescriptorIndexing};
        return false;
    }
    m_enabled.bindless = m_supported.supported.bindless;
    SetEnabled(m_enabled.bindless.descriptorIndexing);
    SetEnabled(m_enabled.bindless.runtimeDescriptorArray);
    SetEnabled(m_enabled.bindless.shaderSampledImageArrayNonUniformIndexing);
    SetEnabled(m_enabled.bindless.descriptorBindingPartiallyBound);
    SetEnabled(m_enabled.bindless.descriptorBindingVariableDescriptorCount);
    SetEnabled(m_enabled.bindless.descriptorBindingSampledImageUpdateAfterBind);
    SetEnabled(m_enabled.bindless.descriptorBindingUniformBufferUpdateAfterBind);
    SetEnabled(m_enabled.bindless.descriptorBindingStorageBufferUpdateAfterBind);
    SetEnabled(m_enabled.bindless.descriptorBindingUpdateUnusedWhilePending);
    return true;
}

bool VulkanDeviceFeatureChain::EnableTimelineSemaphore() noexcept
{
    if (!m_supported.supported.timelineSemaphore.supported) {
        m_failure = {rhi::DeviceCapabilityDiagnosticCode::Unsupported, rhi::DeviceCapability::TimelineSemaphore};
        return false;
    }
    if (IsCoreVersionAtLeast(m_supported.apiVersion, 1, 2)) {
        if (!m_vulkan12Linked) {
            Link(&m_vulkan12);
            m_vulkan12Linked = true;
        }
        m_vulkan12.timelineSemaphore = VK_TRUE;
    } else if (m_supported.timelineSemaphoreExtension) {
        if (!m_timelineSemaphoreKHRLinked) {
            Link(&m_timelineSemaphoreKHR);
            m_timelineSemaphoreKHRLinked = true;
        }
        m_timelineSemaphoreKHR.timelineSemaphore = VK_TRUE;
    } else {
        m_failure = {rhi::DeviceCapabilityDiagnosticCode::Unsupported, rhi::DeviceCapability::TimelineSemaphore};
        return false;
    }
    m_enabled.timelineSemaphore = {true, true};
    return true;
}

bool VulkanDeviceFeatureChain::EnableDynamicRendering() noexcept
{
    if (!m_supported.supported.dynamicRendering.supported) {
        m_failure = {rhi::DeviceCapabilityDiagnosticCode::Unsupported, rhi::DeviceCapability::DynamicRendering};
        return false;
    }
    if (IsCoreVersionAtLeast(m_supported.apiVersion, 1, 3)) {
        if (!m_vulkan13Linked) {
            Link(&m_vulkan13);
            m_vulkan13Linked = true;
        }
        m_vulkan13.dynamicRendering = VK_TRUE;
    } else if (m_supported.dynamicRenderingExtension) {
        if (!m_dynamicRenderingKHRLinked) {
            Link(&m_dynamicRenderingKHR);
            m_dynamicRenderingKHRLinked = true;
        }
        m_dynamicRenderingKHR.dynamicRendering = VK_TRUE;
    } else {
        m_failure = {rhi::DeviceCapabilityDiagnosticCode::Unsupported, rhi::DeviceCapability::DynamicRendering};
        return false;
    }
    m_enabled.dynamicRendering = {true, true};
    return true;
}

bool VulkanDeviceFeatureChain::EnableSynchronization2() noexcept
{
    if (!m_supported.supported.synchronization2.supported) {
        m_failure = {rhi::DeviceCapabilityDiagnosticCode::Unsupported, rhi::DeviceCapability::Synchronization2};
        return false;
    }
    if (IsCoreVersionAtLeast(m_supported.apiVersion, 1, 3)) {
        if (!m_vulkan13Linked) {
            Link(&m_vulkan13);
            m_vulkan13Linked = true;
        }
        m_vulkan13.synchronization2 = VK_TRUE;
    } else if (m_supported.synchronization2Extension) {
        if (!m_synchronization2KHRLinked) {
            Link(&m_synchronization2KHR);
            m_synchronization2KHRLinked = true;
        }
        m_synchronization2KHR.synchronization2 = VK_TRUE;
    } else {
        m_failure = {rhi::DeviceCapabilityDiagnosticCode::Unsupported, rhi::DeviceCapability::Synchronization2};
        return false;
    }
    m_enabled.synchronization2 = {true, true};
    return true;
}

bool VulkanDeviceFeatureChain::Enable(const rhi::DeviceCapabilityRequest &request) noexcept
{
    ResetChain();
    m_failure = {};
    const auto reject = [this]() noexcept {
        const auto failure = m_failure;
        ResetChain();
        m_failure = failure;
        return false;
    };
    if (request.descriptorIndexing && !EnableDescriptorIndexing())
        return reject();
    if (request.timelineSemaphore && !EnableTimelineSemaphore())
        return reject();
    if (request.dynamicRendering && !EnableDynamicRendering())
        return reject();
    if ((request.synchronization2 || request.submit2) && !EnableSynchronization2())
        return reject();
    if (request.submit2) {
        m_enabled.submit2 = {true, true};
    }
    return true;
}

bool VulkanRhiDevice::IsValidTextureDesc(const rhi::TextureDesc &desc) noexcept
{
    if (!rhi::IsValidPixelFormat(desc.format) || desc.width == 0 || desc.height == 0 || desc.depthOrLayers == 0 ||
        desc.mipLevels == 0 || desc.mipLevels > 32 || desc.usage == rhi::TextureUsageFlags::None)
        return false;
    if (desc.dimension == rhi::TextureDimension::Texture1D && desc.height != 1)
        return false;
    if (desc.samples != rhi::SampleCount::One &&
        (desc.dimension != rhi::TextureDimension::Texture2D || desc.mipLevels != 1 || desc.cubeCompatible))
        return false;
    if (desc.cubeCompatible &&
        (desc.dimension != rhi::TextureDimension::Texture2D || desc.depthOrLayers < 6 || desc.depthOrLayers % 6 != 0))
        return false;
    if (rhi::IsDepthFormat(desc.format))
        return rhi::HasTextureUsage(desc.usage, rhi::TextureUsageFlags::DepthStencilAttachment) &&
               !rhi::HasTextureUsage(desc.usage, rhi::TextureUsageFlags::ColorAttachment);
    return !rhi::HasTextureUsage(desc.usage, rhi::TextureUsageFlags::DepthStencilAttachment);
}

const rhi::GraphicsCommandEncoder::Dispatch VulkanRhiDevice::s_graphicsDispatch = {
    &VulkanRhiDevice::BindPipeline, &VulkanRhiDevice::BindGroup, &VulkanRhiDevice::PushConstants,
    &VulkanRhiDevice::Draw, &VulkanRhiDevice::DrawIndirect};

const rhi::ComputeCommandEncoder::DispatchTable VulkanRhiDevice::s_computeDispatch = {
    &VulkanRhiDevice::BindComputePipeline, &VulkanRhiDevice::BindComputeGroup, &VulkanRhiDevice::PushComputeConstants,
    &VulkanRhiDevice::Dispatch, &VulkanRhiDevice::DispatchIndirect};

const rhi::TransferCommandEncoder::DispatchTable VulkanRhiDevice::s_transferDispatch = {
    &VulkanRhiDevice::CopyBuffer,
    &VulkanRhiDevice::CopyTexture,
    &VulkanRhiDevice::ResolveTexture,
};

VulkanRhiDevice::VulkanRhiDevice() : m_deviceId(rhi::AllocateDeviceId())
{
}

VulkanRhiDevice::VulkanRhiDevice(VkDevice device, VmaAllocator allocator, const rhi::DeviceCaps &capabilities,
                                 uint32_t graphicsQueueFamily, uint32_t computeQueueFamily,
                                 uint32_t transferQueueFamily,
                                 const rhi::DeviceCapabilityState &capabilityState) noexcept
    : m_deviceId(rhi::AllocateDeviceId()), m_device(device), m_allocator(allocator), m_capabilities(capabilities),
      m_graphicsQueueFamily(graphicsQueueFamily), m_computeQueueFamily(computeQueueFamily),
      m_transferQueueFamily(transferQueueFamily), m_capabilityState(capabilityState),
      m_descriptorManager(device, m_deviceId)
{
}

VulkanRhiDevice::~VulkanRhiDevice()
{
    if (m_lifetime) {
        std::unique_lock lock(m_lifetime->gate);
        m_lifetime->alive.store(false, std::memory_order_release);
        DestroyOwnedResources();
        return;
    }
    DestroyOwnedResources();
}

void VulkanRhiDevice::UseSubmissionSerials(std::function<rhi::SubmissionSerial()> retirementSerialSource)
{
    if (!retirementSerialSource)
        throw std::invalid_argument("Vulkan RHI retirement requires a completion epoch source");
    m_descriptorManager.UseSubmissionSerials(retirementSerialSource);
    m_resourceRetirementQueue.BindSerialSource(std::move(retirementSerialSource));
}

void VulkanRhiDevice::Reset(VkDevice device, VmaAllocator allocator, const rhi::DeviceCaps &capabilities,
                            uint32_t graphicsQueueFamily, uint32_t computeQueueFamily, uint32_t transferQueueFamily,
                            const rhi::DeviceCapabilityState &capabilityState) noexcept
{
    if (m_lifetime) {
        std::unique_lock lock(m_lifetime->gate);
        m_lifetime->alive.store(false, std::memory_order_release);
        DestroyOwnedResources();
    } else {
        DestroyOwnedResources();
    }
    m_lifetime = std::make_shared<rhi::DeviceLifetime>();
    m_device = device;
    m_allocator = allocator;
    m_capabilities = capabilities;
    m_capabilityState = capabilityState;
    m_graphicsQueueFamily = graphicsQueueFamily;
    m_computeQueueFamily = computeQueueFamily;
    m_transferQueueFamily = transferQueueFamily;
    m_descriptorManager.Reset(device, m_deviceId);
    ResetSlots(m_buffers, m_freeBuffer);
    ResetSlots(m_textures, m_freeTexture);
    ResetSlots(m_textureViews, m_freeTextureView);
    ResetSlots(m_samplers, m_freeSampler);
    ResetSlots(m_shaderModules, m_freeShaderModule);
    ResetSlots(m_bindingLayouts, m_freeBindingLayout);
    ResetSlots(m_bindGroups, m_freeBindGroup);
    ResetSlots(m_graphicsPipelines, m_freeGraphicsPipeline);
    ResetSlots(m_computePipelines, m_freeComputePipeline);
    ResetSlots(m_renderTargetLayouts, m_freeRenderTargetLayout);
}

rhi::BufferHandle VulkanRhiDevice::RegisterBuffer(VkBuffer buffer, uint64_t byteSize)
{
    return buffer == VK_NULL_HANDLE
               ? rhi::BufferHandle{}
               : Register<rhi::BufferHandle>(
                     m_buffers, m_freeBuffer,
                     BufferPayload{buffer, {}, nullptr, byteSize, false, rhi::BufferMemory::DeviceLocal, false});
}

rhi::TextureHandle VulkanRhiDevice::RegisterTexture(VkImage image)
{
    return image == VK_NULL_HANDLE ? rhi::TextureHandle{}
                                   : Register<rhi::TextureHandle>(m_textures, m_freeTexture, TexturePayload{image});
}

template <typename HandleType, typename Payload>
HandleType VulkanRhiDevice::Register(std::vector<Slot<Payload>> &slots, uint32_t &freeHead, const Payload &payload)
{
    if (freeHead != UINT32_MAX) {
        const uint32_t index = freeHead;
        auto &slot = slots[index];
        freeHead = slot.nextFree;
        slot.nextFree = UINT32_MAX;
        slot.payload = payload;
        slot.occupied = true;
        return {index, rhi::ComposeHandleGeneration(m_deviceId, slot.generation)};
    }

    const uint32_t index = static_cast<uint32_t>(slots.size());
    slots.push_back({payload, 1, UINT32_MAX, true});
    return {index, rhi::ComposeHandleGeneration(m_deviceId, 1)};
}

template <typename HandleType, typename Payload>
void VulkanRhiDevice::Release(std::vector<Slot<Payload>> &slots, uint32_t &freeHead, HandleType handle) noexcept
{
    if (!handle.IsValid() || handle.Device() != m_deviceId || handle.index >= slots.size())
        return;

    auto &slot = slots[handle.index];
    if (!slot.occupied || slot.generation != handle.Version())
        return;

    slot.payload = {};
    slot.occupied = false;
    ++slot.generation;
    if (slot.generation == 0)
        slot.generation = 1;
    slot.nextFree = freeHead;
    freeHead = handle.index;
}

template <typename Payload>
void VulkanRhiDevice::ResetSlots(std::vector<Slot<Payload>> &slots, uint32_t &freeHead) noexcept
{
    freeHead = UINT32_MAX;
    for (size_t i = slots.size(); i > 0; --i) {
        auto &slot = slots[i - 1];
        slot.payload = {};
        slot.occupied = false;
        ++slot.generation;
        if (slot.generation == 0)
            slot.generation = 1;
        slot.nextFree = freeHead;
        freeHead = static_cast<uint32_t>(i - 1);
    }
}

template <typename HandleType, typename Payload>
const Payload *VulkanRhiDevice::Resolve(const std::vector<Slot<Payload>> &slots, HandleType handle) const noexcept
{
    if (!handle.IsValid() || handle.Device() != m_deviceId || handle.index >= slots.size())
        return nullptr;
    const auto &slot = slots[handle.index];
    return slot.occupied && slot.generation == handle.Version() ? &slot.payload : nullptr;
}

rhi::TextureViewHandle VulkanRhiDevice::RegisterTextureView(VkImageView view)
{
    return view == VK_NULL_HANDLE
               ? rhi::TextureViewHandle{}
               : Register<rhi::TextureViewHandle>(m_textureViews, m_freeTextureView, TextureViewPayload{view});
}

rhi::SamplerHandle VulkanRhiDevice::RegisterSampler(VkSampler sampler)
{
    return sampler == VK_NULL_HANDLE ? rhi::SamplerHandle{}
                                     : Register<rhi::SamplerHandle>(m_samplers, m_freeSampler, SamplerPayload{sampler});
}

rhi::ShaderModuleHandle VulkanRhiDevice::RegisterShaderModule(VkShaderModule module)
{
    return module == VK_NULL_HANDLE
               ? rhi::ShaderModuleHandle{}
               : Register<rhi::ShaderModuleHandle>(m_shaderModules, m_freeShaderModule, ShaderModulePayload{module});
}

rhi::BindingLayoutHandle VulkanRhiDevice::RegisterBindingLayout(VkDescriptorSetLayout layout)
{
    return layout == VK_NULL_HANDLE ? rhi::BindingLayoutHandle{}
                                    : Register<rhi::BindingLayoutHandle>(m_bindingLayouts, m_freeBindingLayout,
                                                                         BindingLayoutPayload{layout});
}

rhi::BindGroupHandle VulkanRhiDevice::RegisterBindGroup(VkDescriptorSet set)
{
    return set == VK_NULL_HANDLE
               ? rhi::BindGroupHandle{}
               : Register<rhi::BindGroupHandle>(m_bindGroups, m_freeBindGroup,
                                                BindGroupPayload{{0, m_deviceId, DescriptorArena::Persistent, 0, 0, set,
                                                                  VK_NULL_HANDLE, VK_NULL_HANDLE}});
}

bool VulkanRhiDevice::ConfigureBindlessTextureTable(VkDescriptorSetLayout layout, VkDescriptorSet set,
                                                    BindlessTexturePublisher publisher,
                                                    BindlessTextureUsageMarker usageMarker)
{
    ClearBindlessTextureTable();
    if (layout == VK_NULL_HANDLE || set == VK_NULL_HANDLE || !publisher || !usageMarker)
        return false;

    const rhi::BindingLayoutHandle rhiLayout = RegisterBindingLayout(layout);
    const rhi::BindGroupHandle rhiGroup = RegisterBindGroup(set);
    if (!rhiLayout.IsValid() || !rhiGroup.IsValid()) {
        Release(rhiGroup);
        Release(rhiLayout);
        return false;
    }

    m_bindlessTextureTableBinding = {rhiLayout, rhiGroup};
    m_bindlessTexturePublisher = std::move(publisher);
    m_bindlessTextureUsageMarker = std::move(usageMarker);
    return true;
}

void VulkanRhiDevice::ClearBindlessTextureTable() noexcept
{
    m_bindlessTexturePublisher = {};
    m_bindlessTextureUsageMarker = {};
    Release(m_bindlessTextureTableBinding.group);
    Release(m_bindlessTextureTableBinding.layout);
    m_bindlessTextureTableBinding = {};
}

rhi::BindlessTextureTableBinding VulkanRhiDevice::GetBindlessTextureTableBinding() const noexcept
{
    return m_bindlessTextureTableBinding;
}

rhi::ResourceIndex
VulkanRhiDevice::PublishBindlessTexture(const std::shared_ptr<const rhi::TextureGpuView> &texture) noexcept
{
    return m_bindlessTexturePublisher ? m_bindlessTexturePublisher(texture) : rhi::ResourceIndex{};
}

void VulkanRhiDevice::MarkBindlessTexturesUsed(const rhi::ResourceIndex *resources, size_t count) noexcept
{
    const rhi::SubmissionSerial recordingSerial = vkdebug::GetDescriptorRecordingSubmissionSerial();
    if (m_bindlessTextureUsageMarker && (resources || count == 0) && recordingSerial != rhi::InvalidSubmissionSerial) {
        m_bindlessTextureUsageMarker(resources, count, recordingSerial);
    }
}

rhi::BufferHandle VulkanRhiDevice::CreateBuffer(const rhi::BufferDesc &desc)
{
    const VkBufferUsageFlags usage = ToVkBufferUsage(desc.usage);
    if (m_device == VK_NULL_HANDLE || m_allocator == VK_NULL_HANDLE || desc.byteSize == 0 || usage == 0 ||
        desc.initialDataBytes > desc.byteSize || (desc.initialDataBytes > 0 && desc.initialData == nullptr) ||
        (desc.initialDataBytes > 0 && desc.memory == rhi::BufferMemory::DeviceLocal))
        return {};

    VkBufferCreateInfo bufferInfo{};
    bufferInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bufferInfo.size = desc.byteSize;
    bufferInfo.usage = usage;
    std::array<uint32_t, 3> requestedFamilies{};
    uint32_t requestedFamilyCount = 0;
    const auto appendFamily = [&](uint32_t family) {
        if (std::find(requestedFamilies.begin(), requestedFamilies.begin() + requestedFamilyCount, family) ==
            requestedFamilies.begin() + requestedFamilyCount) {
            requestedFamilies[requestedFamilyCount++] = family;
        }
    };
    if (rhi::HasQueueAccess(desc.queueAccess, rhi::QueueAccessFlags::Graphics))
        appendFamily(m_graphicsQueueFamily);
    if (rhi::HasQueueAccess(desc.queueAccess, rhi::QueueAccessFlags::Compute))
        appendFamily(m_computeQueueFamily);
    if (rhi::HasQueueAccess(desc.queueAccess, rhi::QueueAccessFlags::Transfer))
        appendFamily(m_transferQueueFamily);
    bufferInfo.sharingMode = requestedFamilyCount > 1 ? VK_SHARING_MODE_CONCURRENT : VK_SHARING_MODE_EXCLUSIVE;
    bufferInfo.queueFamilyIndexCount = requestedFamilyCount > 1 ? requestedFamilyCount : 0;
    bufferInfo.pQueueFamilyIndices = requestedFamilyCount > 1 ? requestedFamilies.data() : nullptr;

    VmaAllocationCreateInfo allocationInfo{};
    switch (desc.memory) {
    case rhi::BufferMemory::DeviceLocal:
        allocationInfo.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;
        break;
    case rhi::BufferMemory::Upload:
        allocationInfo.usage = VMA_MEMORY_USAGE_AUTO;
        allocationInfo.flags =
            VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT;
        break;
    case rhi::BufferMemory::Readback:
        allocationInfo.usage = VMA_MEMORY_USAGE_AUTO;
        allocationInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT;
        break;
    }

    VkBuffer buffer = VK_NULL_HANDLE;
    VmaAllocation allocation = VK_NULL_HANDLE;
    VmaAllocationInfo resultInfo{};
    if (vmaCreateBuffer(m_allocator, &bufferInfo, &allocationInfo, &buffer, &allocation, &resultInfo) != VK_SUCCESS)
        return {};

    if (desc.initialDataBytes > 0) {
        if (!resultInfo.pMappedData) {
            vmaDestroyBuffer(m_allocator, buffer, allocation);
            return {};
        }
        std::memcpy(resultInfo.pMappedData, desc.initialData, static_cast<size_t>(desc.initialDataBytes));
        vmaFlushAllocation(m_allocator, allocation, 0, desc.initialDataBytes);
    }

    return Register<rhi::BufferHandle>(m_buffers, m_freeBuffer,
                                       BufferPayload{buffer, allocation, resultInfo.pMappedData, desc.byteSize, true,
                                                     desc.memory, requestedFamilyCount > 1});
}

bool VulkanRhiDevice::ReadBuffer(rhi::BufferHandle handle, uint64_t offset, void *data, uint64_t byteSize)
{
    const auto *payload = Resolve(m_buffers, handle);
    if (!payload || !payload->owned || payload->memory != rhi::BufferMemory::Readback ||
        payload->allocation == VK_NULL_HANDLE || !payload->mappedData || !data || byteSize == 0 ||
        offset > payload->byteSize || byteSize > payload->byteSize - offset)
        return false;
    if (vmaInvalidateAllocation(m_allocator, payload->allocation, offset, byteSize) != VK_SUCCESS)
        return false;
    std::memcpy(data, static_cast<const uint8_t *>(payload->mappedData) + offset, static_cast<size_t>(byteSize));
    return true;
}

rhi::TextureHandle VulkanRhiDevice::CreateTexture(const rhi::TextureDesc &desc)
{
    if (m_device == VK_NULL_HANDLE || m_allocator == VK_NULL_HANDLE || !IsValidTextureDesc(desc))
        return {};

    if (m_capabilities.backend != rhi::BackendType::Unknown) {
        const auto &limits = m_capabilities.limits;
        const bool dimensionsSupported =
            (desc.dimension == rhi::TextureDimension::Texture1D && desc.width <= limits.maxTextureDimension1D &&
             desc.depthOrLayers <= limits.maxTextureArrayLayers) ||
            (desc.dimension == rhi::TextureDimension::Texture2D && desc.width <= limits.maxTextureDimension2D &&
             desc.height <= limits.maxTextureDimension2D && desc.depthOrLayers <= limits.maxTextureArrayLayers) ||
            (desc.dimension == rhi::TextureDimension::Texture3D && desc.width <= limits.maxTextureDimension3D &&
             desc.height <= limits.maxTextureDimension3D && desc.depthOrLayers <= limits.maxTextureDimension3D);
        if (!dimensionsSupported ||
            !m_capabilities.CheckFormat(desc.format, RequiredFormatFeatures(desc.usage)).IsSupported() ||
            !m_capabilities.CheckSampleCount(desc.format, desc.samples).IsSupported())
            return {};
    }

    const VkFormat format = rhi::ToVkFormat(desc.format);
    const VkImageUsageFlags usage = ToVkTextureUsage(desc.usage);
    const VkImageType imageType = ToVkImageType(desc.dimension);
    if (format == VK_FORMAT_UNDEFINED || usage == 0 || imageType == VK_IMAGE_TYPE_MAX_ENUM)
        return {};

    VkImageCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    createInfo.flags = desc.cubeCompatible ? VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT : 0;
    if (desc.mutableFormat)
        createInfo.flags |= VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT;
    createInfo.imageType = imageType;
    createInfo.format = format;
    createInfo.extent = {desc.width, desc.height,
                         desc.dimension == rhi::TextureDimension::Texture3D ? desc.depthOrLayers : 1U};
    createInfo.mipLevels = desc.mipLevels;
    createInfo.arrayLayers = desc.dimension == rhi::TextureDimension::Texture3D ? 1U : desc.depthOrLayers;
    createInfo.samples = rhi::ToVkSampleCount(desc.samples);
    createInfo.tiling = VK_IMAGE_TILING_OPTIMAL;
    createInfo.usage = usage;
    const uint32_t queueFamilies[] = {m_graphicsQueueFamily, m_transferQueueFamily};
    const bool sharedWithTransfer = HasTextureUsage(desc.usage, rhi::TextureUsageFlags::TransferDestination) &&
                                    m_graphicsQueueFamily != m_transferQueueFamily;
    createInfo.sharingMode = sharedWithTransfer ? VK_SHARING_MODE_CONCURRENT : VK_SHARING_MODE_EXCLUSIVE;
    createInfo.queueFamilyIndexCount = sharedWithTransfer ? 2U : 0U;
    createInfo.pQueueFamilyIndices = sharedWithTransfer ? queueFamilies : nullptr;
    createInfo.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;

    VmaAllocationCreateInfo allocationInfo{};
    allocationInfo.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;
    VkImage image = VK_NULL_HANDLE;
    VmaAllocation allocation = VK_NULL_HANDLE;
    if (vmaCreateImage(m_allocator, &createInfo, &allocationInfo, &image, &allocation, nullptr) != VK_SUCCESS)
        return {};
    return Register<rhi::TextureHandle>(m_textures, m_freeTexture,
                                        TexturePayload{image, allocation, desc, true, sharedWithTransfer});
}

rhi::TextureViewHandle VulkanRhiDevice::CreateTextureView(const rhi::TextureViewDesc &desc)
{
    const TexturePayload *texture = Resolve(m_textures, desc.texture);
    if (m_device == VK_NULL_HANDLE || !texture || texture->image == VK_NULL_HANDLE || desc.mipCount == 0 ||
        desc.layerCount == 0 || desc.baseMip >= texture->desc.mipLevels ||
        desc.mipCount > texture->desc.mipLevels - desc.baseMip)
        return {};

    const rhi::PixelFormat format = desc.format == rhi::PixelFormat::Undefined ? texture->desc.format : desc.format;
    if (format != texture->desc.format &&
        (!texture->desc.mutableFormat || !rhi::AreColorSpaceViewFormatsCompatible(texture->desc.format, format)))
        return {};
    const bool depth = rhi::IsDepthFormat(format);
    if ((!depth && desc.aspect != rhi::TextureAspect::Color) || (depth && desc.aspect == rhi::TextureAspect::Color) ||
        (format == rhi::PixelFormat::D32SFloat &&
         (desc.aspect == rhi::TextureAspect::Stencil || desc.aspect == rhi::TextureAspect::DepthStencil)))
        return {};

    const uint32_t availableLayers =
        texture->desc.dimension == rhi::TextureDimension::Texture3D ? 1U : texture->desc.depthOrLayers;
    if (desc.baseLayer >= availableLayers || desc.layerCount > availableLayers - desc.baseLayer)
        return {};

    bool dimensionValid = false;
    switch (desc.dimension) {
    case rhi::TextureViewDimension::Texture1D:
        dimensionValid = texture->desc.dimension == rhi::TextureDimension::Texture1D && desc.layerCount == 1;
        break;
    case rhi::TextureViewDimension::Texture1DArray:
        dimensionValid = texture->desc.dimension == rhi::TextureDimension::Texture1D;
        break;
    case rhi::TextureViewDimension::Texture2D:
        dimensionValid = texture->desc.dimension == rhi::TextureDimension::Texture2D && desc.layerCount == 1;
        break;
    case rhi::TextureViewDimension::Texture2DArray:
        dimensionValid = texture->desc.dimension == rhi::TextureDimension::Texture2D;
        break;
    case rhi::TextureViewDimension::Texture3D:
        dimensionValid =
            texture->desc.dimension == rhi::TextureDimension::Texture3D && desc.baseLayer == 0 && desc.layerCount == 1;
        break;
    case rhi::TextureViewDimension::Cube:
        dimensionValid = texture->desc.cubeCompatible && desc.layerCount == 6 && desc.baseLayer % 6 == 0;
        break;
    case rhi::TextureViewDimension::CubeArray:
        dimensionValid =
            texture->desc.cubeCompatible && desc.layerCount >= 6 && desc.layerCount % 6 == 0 && desc.baseLayer % 6 == 0;
        break;
    }
    if (!dimensionValid)
        return {};

    VkImageViewCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
    createInfo.image = texture->image;
    createInfo.viewType = ToVkImageViewType(desc.dimension);
    createInfo.format = rhi::ToVkFormat(format);
    createInfo.subresourceRange = {ToVkImageAspect(desc.aspect), desc.baseMip, desc.mipCount, desc.baseLayer,
                                   desc.layerCount};
    VkImageView view = VK_NULL_HANDLE;
    if (createInfo.viewType == VK_IMAGE_VIEW_TYPE_MAX_ENUM || createInfo.subresourceRange.aspectMask == 0 ||
        vkCreateImageView(m_device, &createInfo, nullptr, &view) != VK_SUCCESS)
        return {};
    return Register<rhi::TextureViewHandle>(m_textureViews, m_freeTextureView, TextureViewPayload{view, true});
}

rhi::SamplerHandle VulkanRhiDevice::CreateSampler(const rhi::SamplerDesc &desc)
{
    if (m_device == VK_NULL_HANDLE || !std::isfinite(desc.minLod) || !std::isfinite(desc.maxLod) ||
        !std::isfinite(desc.maxAnisotropy) || desc.minLod < 0.0f || desc.maxLod < desc.minLod ||
        desc.maxAnisotropy < 1.0f)
        return {};
    if (desc.maxAnisotropy > 1.0f &&
        (!m_capabilities.features.samplerAnisotropy || desc.maxAnisotropy > m_capabilities.limits.maxSamplerAnisotropy))
        return {};
    VkSamplerCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
    createInfo.magFilter = ToVkFilter(desc.magFilter);
    createInfo.minFilter = ToVkFilter(desc.minFilter);
    createInfo.mipmapMode = ToVkMipFilter(desc.mipFilter);
    createInfo.addressModeU = ToVkAddressMode(desc.addressU);
    createInfo.addressModeV = ToVkAddressMode(desc.addressV);
    createInfo.addressModeW = ToVkAddressMode(desc.addressW);
    createInfo.anisotropyEnable = desc.maxAnisotropy > 1.0f ? VK_TRUE : VK_FALSE;
    createInfo.maxAnisotropy = desc.maxAnisotropy;
    createInfo.compareEnable = VK_FALSE;
    createInfo.minLod = desc.minLod;
    createInfo.maxLod = desc.maxLod;
    createInfo.borderColor = VK_BORDER_COLOR_FLOAT_TRANSPARENT_BLACK;
    VkSampler sampler = VK_NULL_HANDLE;
    if (vkCreateSampler(m_device, &createInfo, nullptr, &sampler) != VK_SUCCESS)
        return {};
    return Register<rhi::SamplerHandle>(m_samplers, m_freeSampler, SamplerPayload{sampler, true});
}

rhi::ShaderModuleHandle VulkanRhiDevice::CreateShaderModule(const rhi::ShaderModuleDesc &desc)
{
    if (m_device == VK_NULL_HANDLE || desc.language != rhi::ShaderSourceLanguage::SpirV || !desc.code ||
        desc.byteSize == 0 || desc.byteSize % sizeof(uint32_t) != 0)
        return {};
    VkShaderModuleCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
    createInfo.codeSize = desc.byteSize;
    createInfo.pCode = static_cast<const uint32_t *>(desc.code);
    VkShaderModule module = VK_NULL_HANDLE;
    if (vkCreateShaderModule(m_device, &createInfo, nullptr, &module) != VK_SUCCESS)
        return {};
    return Register<rhi::ShaderModuleHandle>(m_shaderModules, m_freeShaderModule, ShaderModulePayload{module, true});
}

rhi::BindingLayoutHandle VulkanRhiDevice::CreateBindingLayout(const rhi::BindingLayoutDesc &desc)
{
    if (m_device == VK_NULL_HANDLE || desc.entryCount > desc.entries.size())
        return {};
    std::array<VkDescriptorSetLayoutBinding, rhi::BindingLayoutDesc::MaxEntries> bindings{};
    for (uint32_t index = 0; index < desc.entryCount; ++index) {
        const auto &entry = desc.entries[index];
        const VkDescriptorType type = ToVkDescriptorType(entry.type);
        if (type == VK_DESCRIPTOR_TYPE_MAX_ENUM || entry.count == 0 || entry.visibility == rhi::ShaderStage::None)
            return {};
        bindings[index] = {entry.binding, type, entry.count, rhi::ToVkShaderStages(entry.visibility), nullptr};
    }
    VkDescriptorSetLayoutCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    createInfo.bindingCount = desc.entryCount;
    createInfo.pBindings = desc.entryCount > 0 ? bindings.data() : nullptr;
    VkDescriptorSetLayout layout = VK_NULL_HANDLE;
    if (vkCreateDescriptorSetLayout(m_device, &createInfo, nullptr, &layout) != VK_SUCCESS)
        return {};
    return Register<rhi::BindingLayoutHandle>(m_bindingLayouts, m_freeBindingLayout,
                                              BindingLayoutPayload{layout, true});
}

rhi::BindGroupHandle VulkanRhiDevice::CreateBindGroup(const rhi::BindGroupDesc &desc)
{
    if (m_device == VK_NULL_HANDLE || !desc.layout.IsValid() || desc.bufferCount > desc.buffers.size() ||
        desc.textureCount > desc.textures.size())
        return {};
    const VkDescriptorSetLayout layout = Resolve(desc.layout);
    if (layout == VK_NULL_HANDLE)
        return {};
    DescriptorArena arena = DescriptorArena::Persistent;
    if (desc.lifetime == rhi::BindGroupLifetime::FrameTransient)
        arena = DescriptorArena::FrameTransient;
    else if (desc.lifetime == rhi::BindGroupLifetime::ViewPersistent)
        arena = DescriptorArena::ViewPersistent;
    const auto lease = m_descriptorManager.Allocate(layout, arena);
    const VkDescriptorSet set = lease.set;
    if (!lease.IsValid())
        return {};

    std::array<VkDescriptorBufferInfo, rhi::BindGroupDesc::MaxBufferBindings> infos{};
    std::array<VkDescriptorImageInfo, rhi::BindGroupDesc::MaxTextureBindings> imageInfos{};
    std::array<VkWriteDescriptorSet, rhi::BindGroupDesc::MaxBufferBindings + rhi::BindGroupDesc::MaxTextureBindings>
        writes{};
    for (uint32_t index = 0; index < desc.bufferCount; ++index) {
        const auto &binding = desc.buffers[index];
        if (binding.type != rhi::BindingType::StorageBuffer && binding.type != rhi::BindingType::UniformBuffer) {
            m_descriptorManager.Retire(lease);
            return {};
        }
        const auto *buffer = Resolve(m_buffers, binding.buffer);
        if (!buffer || buffer->buffer == VK_NULL_HANDLE ||
            (buffer->byteSize > 0 &&
             (binding.offset >= buffer->byteSize ||
              (binding.byteSize > 0 && binding.byteSize > buffer->byteSize - binding.offset)))) {
            m_descriptorManager.Retire(lease);
            return {};
        }
        infos[index] = {buffer->buffer, binding.offset, binding.byteSize > 0 ? binding.byteSize : VK_WHOLE_SIZE};
        writes[index].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        writes[index].dstSet = set;
        writes[index].dstBinding = binding.binding;
        writes[index].descriptorCount = 1;
        writes[index].descriptorType = ToVkDescriptorType(binding.type);
        writes[index].pBufferInfo = &infos[index];
    }
    for (uint32_t index = 0; index < desc.textureCount; ++index) {
        const auto &binding = desc.textures[index];
        if (binding.type != rhi::BindingType::SampledTexture && binding.type != rhi::BindingType::StorageTexture &&
            binding.type != rhi::BindingType::Sampler && binding.type != rhi::BindingType::CombinedTextureSampler) {
            m_descriptorManager.Retire(lease);
            return {};
        }
        const bool needsTexture = binding.type != rhi::BindingType::Sampler;
        const bool needsSampler =
            binding.type == rhi::BindingType::Sampler || binding.type == rhi::BindingType::CombinedTextureSampler;
        const VkImageView texture = needsTexture ? Resolve(binding.texture) : VK_NULL_HANDLE;
        const VkSampler sampler = needsSampler ? Resolve(binding.sampler) : VK_NULL_HANDLE;
        if ((needsTexture && texture == VK_NULL_HANDLE) || (needsSampler && sampler == VK_NULL_HANDLE)) {
            m_descriptorManager.Retire(lease);
            return {};
        }
        auto &imageInfo = imageInfos[index];
        imageInfo.imageView = texture;
        imageInfo.sampler = sampler;
        imageInfo.imageLayout = binding.type == rhi::BindingType::StorageTexture
                                    ? VK_IMAGE_LAYOUT_GENERAL
                                    : (binding.depthRead ? VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL
                                                         : VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);
        auto &write = writes[desc.bufferCount + index];
        write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        write.dstSet = set;
        write.dstBinding = binding.binding;
        write.descriptorCount = 1;
        write.descriptorType = ToVkDescriptorType(binding.type);
        write.pImageInfo = &imageInfo;
    }
    const uint32_t writeCount = desc.bufferCount + desc.textureCount;
    if (writeCount > 0)
        vkUpdateDescriptorSets(m_device, writeCount, writes.data(), 0, nullptr);
    return Register<rhi::BindGroupHandle>(m_bindGroups, m_freeBindGroup, BindGroupPayload{lease, true});
}

bool VulkanRhiDevice::WriteBuffer(rhi::BufferHandle handle, uint64_t offset, const void *data, uint64_t byteSize)
{
    const auto *buffer = Resolve(m_buffers, handle);
    if (!buffer || !buffer->owned || !buffer->mappedData || !data || byteSize == 0 || offset > buffer->byteSize ||
        byteSize > buffer->byteSize - offset)
        return false;
    std::memcpy(static_cast<std::byte *>(buffer->mappedData) + offset, data, static_cast<size_t>(byteSize));
    return vmaFlushAllocation(m_allocator, buffer->allocation, offset, byteSize) == VK_SUCCESS;
}

rhi::GraphicsPipelineHandle VulkanRhiDevice::RegisterGraphicsPipeline(VkPipeline pipeline, VkPipelineLayout layout)
{
    return pipeline == VK_NULL_HANDLE || layout == VK_NULL_HANDLE
               ? rhi::GraphicsPipelineHandle{}
               : Register<rhi::GraphicsPipelineHandle>(m_graphicsPipelines, m_freeGraphicsPipeline,
                                                       GraphicsPipelinePayload{pipeline, layout});
}

rhi::ComputePipelineHandle VulkanRhiDevice::RegisterComputePipeline(VkPipeline pipeline, VkPipelineLayout layout)
{
    return pipeline == VK_NULL_HANDLE || layout == VK_NULL_HANDLE
               ? rhi::ComputePipelineHandle{}
               : Register<rhi::ComputePipelineHandle>(m_computePipelines, m_freeComputePipeline,
                                                      GraphicsPipelinePayload{pipeline, layout});
}

rhi::ComputePipelineHandle VulkanRhiDevice::CreateComputePipeline(const rhi::ComputePipelineDesc &desc)
{
    if (m_device == VK_NULL_HANDLE || !desc.computeShader.IsValid() ||
        desc.bindingLayoutCount > desc.bindingLayouts.size() || (desc.pushConstantBytes % 4u) != 0u)
        return {};

    const VkShaderModule shader = Resolve(desc.computeShader);
    if (shader == VK_NULL_HANDLE)
        return {};

    std::array<VkDescriptorSetLayout, rhi::ComputePipelineDesc::MaxBindingLayouts> layouts{};
    for (uint32_t index = 0; index < desc.bindingLayoutCount; ++index) {
        layouts[index] = Resolve(desc.bindingLayouts[index]);
        if (layouts[index] == VK_NULL_HANDLE)
            return {};
    }

    VkPushConstantRange pushConstants{};
    if (desc.pushConstantBytes > 0) {
        pushConstants.stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
        pushConstants.offset = 0;
        pushConstants.size = desc.pushConstantBytes;
    }

    VkPipelineLayoutCreateInfo layoutInfo{};
    layoutInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
    layoutInfo.setLayoutCount = desc.bindingLayoutCount;
    layoutInfo.pSetLayouts = desc.bindingLayoutCount > 0 ? layouts.data() : nullptr;
    layoutInfo.pushConstantRangeCount = desc.pushConstantBytes > 0 ? 1u : 0u;
    layoutInfo.pPushConstantRanges = desc.pushConstantBytes > 0 ? &pushConstants : nullptr;

    VkPipelineLayout layout = VK_NULL_HANDLE;
    if (vkCreatePipelineLayout(m_device, &layoutInfo, nullptr, &layout) != VK_SUCCESS)
        return {};

    VkPipelineShaderStageCreateInfo stage{};
    stage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
    stage.module = shader;
    stage.pName = "main";

    VkComputePipelineCreateInfo pipelineInfo{};
    pipelineInfo.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO;
    pipelineInfo.stage = stage;
    pipelineInfo.layout = layout;

    VkPipeline pipeline = VK_NULL_HANDLE;
    if (vkCreateComputePipelines(m_device, VK_NULL_HANDLE, 1, &pipelineInfo, nullptr, &pipeline) != VK_SUCCESS) {
        vkDestroyPipelineLayout(m_device, layout, nullptr);
        return {};
    }

    return Register<rhi::ComputePipelineHandle>(m_computePipelines, m_freeComputePipeline,
                                                GraphicsPipelinePayload{pipeline, layout, true, true});
}

rhi::GraphicsPipelineHandle VulkanRhiDevice::CreateGraphicsPipeline(const rhi::GraphicsPipelineDesc &desc)
{
    if (m_device == VK_NULL_HANDLE || !desc.vertexShader.IsValid() || !desc.fragmentShader.IsValid() ||
        !desc.HasValidRenderingContract() || desc.bindingLayoutCount > desc.bindingLayouts.size() ||
        desc.colorTargetCount > desc.colorTargets.size() || (desc.pushConstantBytes % 4u) != 0u ||
        (desc.pushConstantBytes > 0 && desc.pushConstantStages == rhi::ShaderStage::None))
        return {};

    std::array<VkFormat, rhi::GraphicsRenderingSignature::MaxColorTargets> dynamicColorFormats{};
    VkPipelineRenderingCreateInfo dynamicRenderingInfo{};
    if (desc.useDynamicRendering &&
        (!m_capabilityState.dynamicRendering.IsEnabled() || desc.renderingSignature.viewMask != 0 ||
         !rhi::BuildVkPipelineRenderingInfo(desc.renderingSignature, dynamicColorFormats, dynamicRenderingInfo)))
        return {};

    const VkShaderModule vertex = Resolve(desc.vertexShader);
    const VkShaderModule fragment = Resolve(desc.fragmentShader);
    const VkRenderPass renderPass = desc.useDynamicRendering ? VK_NULL_HANDLE : Resolve(desc.renderTargetLayout);
    if (vertex == VK_NULL_HANDLE || fragment == VK_NULL_HANDLE ||
        (!desc.useDynamicRendering && renderPass == VK_NULL_HANDLE))
        return {};

    std::array<VkDescriptorSetLayout, rhi::GraphicsPipelineDesc::MaxBindingLayouts> layouts{};
    for (uint32_t index = 0; index < desc.bindingLayoutCount; ++index) {
        layouts[index] = Resolve(desc.bindingLayouts[index]);
        if (layouts[index] == VK_NULL_HANDLE)
            return {};
    }

    VkPushConstantRange pushConstants{};
    if (desc.pushConstantBytes > 0) {
        pushConstants.stageFlags = rhi::ToVkShaderStages(desc.pushConstantStages);
        pushConstants.size = desc.pushConstantBytes;
    }
    VkPipelineLayoutCreateInfo layoutInfo{};
    layoutInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
    layoutInfo.setLayoutCount = desc.bindingLayoutCount;
    layoutInfo.pSetLayouts = desc.bindingLayoutCount > 0 ? layouts.data() : nullptr;
    layoutInfo.pushConstantRangeCount = desc.pushConstantBytes > 0 ? 1u : 0u;
    layoutInfo.pPushConstantRanges = desc.pushConstantBytes > 0 ? &pushConstants : nullptr;
    VkPipelineLayout layout = VK_NULL_HANDLE;
    if (vkCreatePipelineLayout(m_device, &layoutInfo, nullptr, &layout) != VK_SUCCESS)
        return {};

    std::array<VkPipelineShaderStageCreateInfo, 2> stages{};
    stages[0].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    stages[0].stage = VK_SHADER_STAGE_VERTEX_BIT;
    stages[0].module = vertex;
    stages[0].pName = "main";
    stages[1].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    stages[1].stage = VK_SHADER_STAGE_FRAGMENT_BIT;
    stages[1].module = fragment;
    stages[1].pName = "main";

    VkPipelineVertexInputStateCreateInfo vertexInput{};
    vertexInput.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
    VkPipelineInputAssemblyStateCreateInfo inputAssembly{};
    inputAssembly.sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO;
    inputAssembly.topology = ToVkTopology(desc.topology);

    VkPipelineViewportStateCreateInfo viewport{};
    viewport.sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO;
    viewport.viewportCount = 1;
    viewport.scissorCount = 1;

    VkPipelineRasterizationStateCreateInfo raster{};
    raster.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
    raster.polygonMode = desc.raster.wireframe ? VK_POLYGON_MODE_LINE : VK_POLYGON_MODE_FILL;
    raster.cullMode = ToVkCullMode(desc.raster.cullMode);
    raster.frontFace = ToVkFrontFace(desc.raster.frontFace);
    raster.lineWidth = 1.0f;

    VkPipelineMultisampleStateCreateInfo multisample{};
    multisample.sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO;
    multisample.rasterizationSamples = rhi::ToVkSampleCount(desc.samples);

    VkPipelineDepthStencilStateCreateInfo depth{};
    depth.sType = VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
    depth.depthTestEnable = desc.depth.testEnabled ? VK_TRUE : VK_FALSE;
    depth.depthWriteEnable = desc.depth.writeEnabled ? VK_TRUE : VK_FALSE;
    depth.depthCompareOp = ToVkCompareOp(desc.depth.compare);

    std::array<VkPipelineColorBlendAttachmentState, rhi::GraphicsPipelineDesc::MaxColorTargets> attachments{};
    for (uint32_t index = 0; index < desc.colorTargetCount; ++index) {
        const auto &target = desc.colorTargets[index];
        if (!rhi::IsValidPixelFormat(target.format) || rhi::IsDepthFormat(target.format)) {
            vkDestroyPipelineLayout(m_device, layout, nullptr);
            return {};
        }
        auto &attachment = attachments[index];
        attachment.blendEnable = target.blendEnabled ? VK_TRUE : VK_FALSE;
        attachment.srcColorBlendFactor = target.premultipliedAlpha ? VK_BLEND_FACTOR_ONE : VK_BLEND_FACTOR_SRC_ALPHA;
        attachment.dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
        attachment.colorBlendOp = VK_BLEND_OP_ADD;
        attachment.srcAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
        attachment.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
        attachment.alphaBlendOp = VK_BLEND_OP_ADD;
        attachment.colorWriteMask = target.writeMask;
    }
    VkPipelineColorBlendStateCreateInfo blend{};
    blend.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;
    blend.attachmentCount = desc.colorTargetCount;
    blend.pAttachments = desc.colorTargetCount > 0 ? attachments.data() : nullptr;

    constexpr std::array<VkDynamicState, 2> dynamicStates = {VK_DYNAMIC_STATE_VIEWPORT, VK_DYNAMIC_STATE_SCISSOR};
    VkPipelineDynamicStateCreateInfo dynamic{};
    dynamic.sType = VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO;
    dynamic.dynamicStateCount = static_cast<uint32_t>(dynamicStates.size());
    dynamic.pDynamicStates = dynamicStates.data();

    VkGraphicsPipelineCreateInfo pipelineInfo{};
    pipelineInfo.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
    pipelineInfo.pNext = desc.useDynamicRendering ? &dynamicRenderingInfo : nullptr;
    pipelineInfo.stageCount = static_cast<uint32_t>(stages.size());
    pipelineInfo.pStages = stages.data();
    pipelineInfo.pVertexInputState = &vertexInput;
    pipelineInfo.pInputAssemblyState = &inputAssembly;
    pipelineInfo.pViewportState = &viewport;
    pipelineInfo.pRasterizationState = &raster;
    pipelineInfo.pMultisampleState = &multisample;
    pipelineInfo.pDepthStencilState = &depth;
    pipelineInfo.pColorBlendState = &blend;
    pipelineInfo.pDynamicState = &dynamic;
    pipelineInfo.layout = layout;
    pipelineInfo.renderPass = renderPass;
    pipelineInfo.subpass = 0;

    VkPipeline pipeline = VK_NULL_HANDLE;
    if (vkCreateGraphicsPipelines(m_device, VK_NULL_HANDLE, 1, &pipelineInfo, nullptr, &pipeline) != VK_SUCCESS) {
        vkDestroyPipelineLayout(m_device, layout, nullptr);
        return {};
    }
    return Register<rhi::GraphicsPipelineHandle>(m_graphicsPipelines, m_freeGraphicsPipeline,
                                                 GraphicsPipelinePayload{pipeline, layout, true, true});
}

rhi::RenderTargetLayoutHandle VulkanRhiDevice::RegisterRenderTargetLayout(VkRenderPass renderPass)
{
    return renderPass == VK_NULL_HANDLE
               ? rhi::RenderTargetLayoutHandle{}
               : Register<rhi::RenderTargetLayoutHandle>(m_renderTargetLayouts, m_freeRenderTargetLayout, renderPass);
}

void VulkanRhiDevice::Release(rhi::TextureViewHandle handle) noexcept
{
    const auto *payload = Resolve(m_textureViews, handle);
    if (payload && payload->owned && m_device != VK_NULL_HANDLE && payload->view != VK_NULL_HANDLE) {
        const VkDevice device = m_device;
        const VkImageView view = payload->view;
        RetireNativeResource([device, view] { vkDestroyImageView(device, view, nullptr); });
    }
    Release(m_textureViews, m_freeTextureView, handle);
}
void VulkanRhiDevice::Release(rhi::BufferHandle handle) noexcept
{
    const auto *payload = Resolve(m_buffers, handle);
    if (payload && payload->owned && m_allocator != VK_NULL_HANDLE && payload->buffer != VK_NULL_HANDLE) {
        const VmaAllocator allocator = m_allocator;
        const VkBuffer buffer = payload->buffer;
        const VmaAllocation allocation = payload->allocation;
        RetireNativeResource([allocator, buffer, allocation] { vmaDestroyBuffer(allocator, buffer, allocation); });
    }
    Release(m_buffers, m_freeBuffer, handle);
}

void VulkanRhiDevice::Release(rhi::TextureHandle handle) noexcept
{
    const auto *payload = Resolve(m_textures, handle);
    if (payload && payload->owned && m_allocator != VK_NULL_HANDLE && payload->image != VK_NULL_HANDLE) {
        const VmaAllocator allocator = m_allocator;
        const VkImage image = payload->image;
        const VmaAllocation allocation = payload->allocation;
        RetireNativeResource([allocator, image, allocation] { vmaDestroyImage(allocator, image, allocation); });
    }
    Release(m_textures, m_freeTexture, handle);
}
void VulkanRhiDevice::Release(rhi::SamplerHandle handle) noexcept
{
    const auto *payload = Resolve(m_samplers, handle);
    if (payload && payload->owned && m_device != VK_NULL_HANDLE && payload->sampler != VK_NULL_HANDLE) {
        const VkDevice device = m_device;
        const VkSampler sampler = payload->sampler;
        RetireNativeResource([device, sampler] { vkDestroySampler(device, sampler, nullptr); });
    }
    Release(m_samplers, m_freeSampler, handle);
}
void VulkanRhiDevice::Release(rhi::ShaderModuleHandle handle) noexcept
{
    const auto *payload = Resolve(m_shaderModules, handle);
    if (payload && payload->owned && m_device != VK_NULL_HANDLE && payload->module != VK_NULL_HANDLE) {
        const VkDevice device = m_device;
        const VkShaderModule module = payload->module;
        RetireNativeResource([device, module] { vkDestroyShaderModule(device, module, nullptr); });
    }
    Release(m_shaderModules, m_freeShaderModule, handle);
}
void VulkanRhiDevice::Release(rhi::BindingLayoutHandle handle) noexcept
{
    const auto *payload = Resolve(m_bindingLayouts, handle);
    if (payload && payload->owned && m_device != VK_NULL_HANDLE && payload->layout != VK_NULL_HANDLE) {
        const VkDevice device = m_device;
        const VkDescriptorSetLayout layout = payload->layout;
        RetireNativeResource([device, layout] { vkDestroyDescriptorSetLayout(device, layout, nullptr); });
    }
    Release(m_bindingLayouts, m_freeBindingLayout, handle);
}
void VulkanRhiDevice::Release(rhi::BindGroupHandle handle) noexcept
{
    const auto *payload = Resolve(m_bindGroups, handle);
    if (payload && payload->owned && m_device != VK_NULL_HANDLE)
        m_descriptorManager.Retire(payload->lease, vkdebug::GetDescriptorRecordingSubmissionSerial());
    Release(m_bindGroups, m_freeBindGroup, handle);
}
void VulkanRhiDevice::Release(rhi::GraphicsPipelineHandle handle) noexcept
{
    const auto *payload = ResolvePipeline(handle);
    if (payload && m_device != VK_NULL_HANDLE) {
        const VkDevice device = m_device;
        const VkPipeline pipeline = payload->ownsPipeline ? payload->pipeline : VK_NULL_HANDLE;
        const VkPipelineLayout layout = payload->ownsLayout ? payload->layout : VK_NULL_HANDLE;
        if (pipeline != VK_NULL_HANDLE || layout != VK_NULL_HANDLE) {
            RetireNativeResource([device, pipeline, layout] {
                if (pipeline != VK_NULL_HANDLE)
                    vkDestroyPipeline(device, pipeline, nullptr);
                if (layout != VK_NULL_HANDLE)
                    vkDestroyPipelineLayout(device, layout, nullptr);
            });
        }
    }
    Release(m_graphicsPipelines, m_freeGraphicsPipeline, handle);
}
void VulkanRhiDevice::Release(rhi::ComputePipelineHandle handle) noexcept
{
    const auto *payload = ResolvePipeline(handle);
    if (payload && m_device != VK_NULL_HANDLE) {
        const VkDevice device = m_device;
        const VkPipeline pipeline = payload->ownsPipeline ? payload->pipeline : VK_NULL_HANDLE;
        const VkPipelineLayout layout = payload->ownsLayout ? payload->layout : VK_NULL_HANDLE;
        if (pipeline != VK_NULL_HANDLE || layout != VK_NULL_HANDLE) {
            RetireNativeResource([device, pipeline, layout] {
                if (pipeline != VK_NULL_HANDLE)
                    vkDestroyPipeline(device, pipeline, nullptr);
                if (layout != VK_NULL_HANDLE)
                    vkDestroyPipelineLayout(device, layout, nullptr);
            });
        }
    }
    Release(m_computePipelines, m_freeComputePipeline, handle);
}
void VulkanRhiDevice::Release(rhi::RenderTargetLayoutHandle handle) noexcept
{
    Release(m_renderTargetLayouts, m_freeRenderTargetLayout, handle);
}

VkImageView VulkanRhiDevice::Resolve(rhi::TextureViewHandle handle) const noexcept
{
    const auto *payload = Resolve(m_textureViews, handle);
    return payload ? payload->view : VK_NULL_HANDLE;
}
VkBuffer VulkanRhiDevice::Resolve(rhi::BufferHandle handle) const noexcept
{
    const auto *payload = Resolve(m_buffers, handle);
    return payload ? payload->buffer : VK_NULL_HANDLE;
}

bool VulkanRhiDevice::UsesConcurrentQueueSharing(rhi::BufferHandle handle) const noexcept
{
    const auto *payload = Resolve(m_buffers, handle);
    return payload && payload->concurrentQueueSharing;
}

VkImage VulkanRhiDevice::Resolve(rhi::TextureHandle handle) const noexcept
{
    const auto *payload = Resolve(m_textures, handle);
    return payload ? payload->image : VK_NULL_HANDLE;
}

bool VulkanRhiDevice::UsesConcurrentQueueSharing(rhi::TextureHandle handle) const noexcept
{
    const auto *payload = Resolve(m_textures, handle);
    return payload && payload->concurrentQueueSharing;
}

VkSampler VulkanRhiDevice::Resolve(rhi::SamplerHandle handle) const noexcept
{
    const auto *payload = Resolve(m_samplers, handle);
    return payload ? payload->sampler : VK_NULL_HANDLE;
}
VkShaderModule VulkanRhiDevice::Resolve(rhi::ShaderModuleHandle handle) const noexcept
{
    const auto *payload = Resolve(m_shaderModules, handle);
    return payload ? payload->module : VK_NULL_HANDLE;
}
VkDescriptorSetLayout VulkanRhiDevice::Resolve(rhi::BindingLayoutHandle handle) const noexcept
{
    const auto *payload = Resolve(m_bindingLayouts, handle);
    return payload ? payload->layout : VK_NULL_HANDLE;
}
VkDescriptorSet VulkanRhiDevice::Resolve(rhi::BindGroupHandle handle) const noexcept
{
    const auto *payload = Resolve(m_bindGroups, handle);
    return payload ? payload->lease.set : VK_NULL_HANDLE;
}
VkRenderPass VulkanRhiDevice::Resolve(rhi::RenderTargetLayoutHandle handle) const noexcept
{
    const auto *payload = Resolve(m_renderTargetLayouts, handle);
    return payload ? *payload : VK_NULL_HANDLE;
}

const VulkanRhiDevice::GraphicsPipelinePayload *
VulkanRhiDevice::ResolvePipeline(rhi::GraphicsPipelineHandle handle) const noexcept
{
    return Resolve(m_graphicsPipelines, handle);
}

const VulkanRhiDevice::GraphicsPipelinePayload *
VulkanRhiDevice::ResolvePipeline(rhi::ComputePipelineHandle handle) const noexcept
{
    return Resolve(m_computePipelines, handle);
}

void VulkanRhiDevice::RetireNativeResource(std::function<void()> deleter) noexcept
{
    if (!deleter)
        return;
    if (!m_resourceRetirementQueue.HasSerialSource()) {
        deleter();
        return;
    }

    // The completion-epoch source is installed immediately after queue setup.
    // Preserve a fallback copy so an unexpected source exception cannot leak
    // the Vulkan object during teardown or a partial initialization failure.
    auto fallback = deleter;
    try {
        m_resourceRetirementQueue.Retire(std::move(deleter));
    } catch (...) {
        fallback();
    }
}

void VulkanRhiDevice::DestroyOwnedResources() noexcept
{
    m_bindlessTexturePublisher = {};
    m_bindlessTextureUsageMarker = {};
    m_bindlessTextureTableBinding = {};
    // VkDeviceContext drains the device before destroying the RHI adapter.
    // Flush resources retired by earlier releases before destroying objects
    // that are still present in the live slot arrays.
    m_resourceRetirementQueue.FlushAll();
    if (m_device == VK_NULL_HANDLE)
        return;
    for (auto &slot : m_textureViews) {
        if (slot.occupied && slot.payload.owned && slot.payload.view != VK_NULL_HANDLE)
            vkDestroyImageView(m_device, slot.payload.view, nullptr);
        slot.payload.owned = false;
    }
    for (auto &slot : m_samplers) {
        if (slot.occupied && slot.payload.owned && slot.payload.sampler != VK_NULL_HANDLE)
            vkDestroySampler(m_device, slot.payload.sampler, nullptr);
        slot.payload.owned = false;
    }
    for (auto &slot : m_graphicsPipelines) {
        if (!slot.occupied)
            continue;
        if (slot.payload.ownsPipeline && slot.payload.pipeline != VK_NULL_HANDLE)
            vkDestroyPipeline(m_device, slot.payload.pipeline, nullptr);
        if (slot.payload.ownsLayout && slot.payload.layout != VK_NULL_HANDLE)
            vkDestroyPipelineLayout(m_device, slot.payload.layout, nullptr);
        slot.payload.ownsPipeline = false;
        slot.payload.ownsLayout = false;
    }
    for (auto &slot : m_computePipelines) {
        if (!slot.occupied)
            continue;
        if (slot.payload.ownsPipeline && slot.payload.pipeline != VK_NULL_HANDLE)
            vkDestroyPipeline(m_device, slot.payload.pipeline, nullptr);
        if (slot.payload.ownsLayout && slot.payload.layout != VK_NULL_HANDLE)
            vkDestroyPipelineLayout(m_device, slot.payload.layout, nullptr);
        slot.payload.ownsPipeline = false;
        slot.payload.ownsLayout = false;
    }
    for (auto &slot : m_bindGroups) {
        if (slot.occupied && slot.payload.owned && slot.payload.lease.IsValid())
            m_descriptorManager.Retire(slot.payload.lease, vkdebug::GetDescriptorRecordingSubmissionSerial());
        slot.payload.owned = false;
    }
    for (auto &slot : m_bindingLayouts) {
        if (slot.occupied && slot.payload.owned && slot.payload.layout != VK_NULL_HANDLE)
            vkDestroyDescriptorSetLayout(m_device, slot.payload.layout, nullptr);
        slot.payload.owned = false;
    }
    for (auto &slot : m_shaderModules) {
        if (slot.occupied && slot.payload.owned && slot.payload.module != VK_NULL_HANDLE)
            vkDestroyShaderModule(m_device, slot.payload.module, nullptr);
        slot.payload.owned = false;
    }
    if (m_allocator != VK_NULL_HANDLE) {
        for (auto &slot : m_textures) {
            if (slot.occupied && slot.payload.owned && slot.payload.image != VK_NULL_HANDLE)
                vmaDestroyImage(m_allocator, slot.payload.image, slot.payload.allocation);
            slot.payload.owned = false;
        }
        for (auto &slot : m_buffers) {
            if (slot.occupied && slot.payload.owned && slot.payload.buffer != VK_NULL_HANDLE)
                vmaDestroyBuffer(m_allocator, slot.payload.buffer, slot.payload.allocation);
            slot.payload.owned = false;
        }
    }
    m_descriptorManager.Destroy();
}

rhi::GraphicsCommandEncoder VulkanRhiDevice::MakeGraphicsCommandEncoder(VulkanGraphicsCommandContext &context,
                                                                        VkCommandBuffer commandBuffer) noexcept
{
    context.device = this;
    context.commandBuffer = commandBuffer;
    context.ResetBindingState();
    context.ResetMetrics();
    return {&context, &s_graphicsDispatch};
}

rhi::ComputeCommandEncoder VulkanRhiDevice::MakeComputeCommandEncoder(VulkanComputeCommandContext &context,
                                                                      VkCommandBuffer commandBuffer) noexcept
{
    context.device = this;
    context.commandBuffer = commandBuffer;
    context.ResetBindingState();
    context.ResetMetrics();
    return {&context, &s_computeDispatch};
}

rhi::TransferCommandEncoder VulkanRhiDevice::MakeTransferCommandEncoder(VulkanTransferCommandContext &context,
                                                                        VkCommandBuffer commandBuffer) noexcept
{
    context = {this, commandBuffer};
    return {&context, &s_transferDispatch};
}

void VulkanRhiDevice::BindPipeline(void *context, rhi::GraphicsPipelineHandle pipeline)
{
    auto &command = *static_cast<VulkanGraphicsCommandContext *>(context);
    const auto *native = command.device ? command.device->ResolvePipeline(pipeline) : nullptr;
    if (!native || command.commandBuffer == VK_NULL_HANDLE) {
        command.ResetBindingState();
        return;
    }
    if (command.boundPipeline == pipeline) {
#if INFERNUX_FRAME_PROFILE
        ++command.pipelineBindSkips;
#endif
        return;
    }
    vkCmdBindPipeline(command.commandBuffer, VK_PIPELINE_BIND_POINT_GRAPHICS, native->pipeline);
#if INFERNUX_FRAME_PROFILE
    ++command.pipelineBinds;
#endif
    command.boundPipeline = pipeline;
    command.boundGroups.fill({});
}

void VulkanRhiDevice::BindGroup(void *context, rhi::GraphicsPipelineHandle pipeline, uint32_t setIndex,
                                rhi::BindGroupHandle group)
{
    auto &command = *static_cast<VulkanGraphicsCommandContext *>(context);
    const auto *nativePipeline = command.device ? command.device->ResolvePipeline(pipeline) : nullptr;
    const VkDescriptorSet nativeGroup = command.device ? command.device->Resolve(group) : VK_NULL_HANDLE;
    if (nativePipeline && nativeGroup != VK_NULL_HANDLE && command.commandBuffer != VK_NULL_HANDLE &&
        command.boundPipeline == pipeline) {
        if (setIndex < command.boundGroups.size() && command.boundGroups[setIndex] == group) {
#if INFERNUX_FRAME_PROFILE
            ++command.groupBindSkips;
#endif
            return;
        }
        vkdebug::CmdBindDescriptorSetsTracked("RHI.GraphicsCommandEncoder.BindGroup", command.commandBuffer,
                                              VK_PIPELINE_BIND_POINT_GRAPHICS, nativePipeline->layout, setIndex, 1,
                                              &nativeGroup, 0, nullptr);
#if INFERNUX_FRAME_PROFILE
        ++command.groupBinds;
#endif
        if (setIndex < command.boundGroups.size())
            command.boundGroups[setIndex] = group;
        const auto *payload = command.device->Resolve(command.device->m_bindGroups, group);
        if (payload)
            command.device->m_descriptorManager.MarkUsed(payload->lease,
                                                         vkdebug::GetDescriptorRecordingSubmissionSerial());
    }
}

void VulkanRhiDevice::PushConstants(void *context, rhi::GraphicsPipelineHandle pipeline, rhi::ShaderStage stages,
                                    uint32_t byteSize, const void *data)
{
    auto &command = *static_cast<VulkanGraphicsCommandContext *>(context);
    const auto *native = command.device ? command.device->ResolvePipeline(pipeline) : nullptr;
    if (native && command.commandBuffer != VK_NULL_HANDLE && command.boundPipeline == pipeline && byteSize > 0 &&
        data) {
        vkCmdPushConstants(command.commandBuffer, native->layout, rhi::ToVkShaderStages(stages), 0, byteSize, data);
    }
}

void VulkanRhiDevice::Draw(void *context, uint32_t vertexCount, uint32_t instanceCount, uint32_t firstVertex,
                           uint32_t firstInstance)
{
    auto &command = *static_cast<VulkanGraphicsCommandContext *>(context);
    if (command.commandBuffer != VK_NULL_HANDLE && command.boundPipeline.IsValid())
        vkCmdDraw(command.commandBuffer, vertexCount, instanceCount, firstVertex, firstInstance);
}

void VulkanRhiDevice::DrawIndirect(void *context, rhi::BufferHandle arguments, uint64_t offset, uint32_t drawCount,
                                   uint32_t stride)
{
    auto &command = *static_cast<VulkanGraphicsCommandContext *>(context);
    const VkBuffer native = command.device ? command.device->Resolve(arguments) : VK_NULL_HANDLE;
    if (command.commandBuffer != VK_NULL_HANDLE && command.boundPipeline.IsValid() && native != VK_NULL_HANDLE &&
        drawCount > 0)
        vkCmdDrawIndirect(command.commandBuffer, native, offset, drawCount, stride);
}

void VulkanRhiDevice::BindComputePipeline(void *context, rhi::ComputePipelineHandle pipeline)
{
    auto &command = *static_cast<VulkanComputeCommandContext *>(context);
    const auto *native = command.device ? command.device->ResolvePipeline(pipeline) : nullptr;
    if (!native || command.commandBuffer == VK_NULL_HANDLE) {
        command.ResetBindingState();
        return;
    }
    if (command.boundPipeline == pipeline) {
#if INFERNUX_FRAME_PROFILE
        ++command.pipelineBindSkips;
#endif
        return;
    }
    vkCmdBindPipeline(command.commandBuffer, VK_PIPELINE_BIND_POINT_COMPUTE, native->pipeline);
#if INFERNUX_FRAME_PROFILE
    ++command.pipelineBinds;
#endif
    command.boundPipeline = pipeline;
    command.boundGroups.fill({});
}

void VulkanRhiDevice::BindComputeGroup(void *context, rhi::ComputePipelineHandle pipeline, uint32_t setIndex,
                                       rhi::BindGroupHandle group)
{
    auto &command = *static_cast<VulkanComputeCommandContext *>(context);
    const auto *nativePipeline = command.device ? command.device->ResolvePipeline(pipeline) : nullptr;
    const VkDescriptorSet nativeGroup = command.device ? command.device->Resolve(group) : VK_NULL_HANDLE;
    if (nativePipeline && nativeGroup != VK_NULL_HANDLE && command.commandBuffer != VK_NULL_HANDLE &&
        command.boundPipeline == pipeline) {
        if (setIndex < command.boundGroups.size() && command.boundGroups[setIndex] == group) {
#if INFERNUX_FRAME_PROFILE
            ++command.groupBindSkips;
#endif
            return;
        }
        vkdebug::CmdBindDescriptorSetsTracked("RHI.ComputeCommandEncoder.BindGroup", command.commandBuffer,
                                              VK_PIPELINE_BIND_POINT_COMPUTE, nativePipeline->layout, setIndex, 1,
                                              &nativeGroup, 0, nullptr);
#if INFERNUX_FRAME_PROFILE
        ++command.groupBinds;
#endif
        if (setIndex < command.boundGroups.size())
            command.boundGroups[setIndex] = group;
        const auto *payload = command.device->Resolve(command.device->m_bindGroups, group);
        if (payload)
            command.device->m_descriptorManager.MarkUsed(payload->lease,
                                                         vkdebug::GetDescriptorRecordingSubmissionSerial());
    }
}

void VulkanRhiDevice::PushComputeConstants(void *context, rhi::ComputePipelineHandle pipeline, uint32_t byteSize,
                                           const void *data)
{
    auto &command = *static_cast<VulkanComputeCommandContext *>(context);
    const auto *native = command.device ? command.device->ResolvePipeline(pipeline) : nullptr;
    if (native && command.commandBuffer != VK_NULL_HANDLE && command.boundPipeline == pipeline && byteSize > 0 && data)
        vkCmdPushConstants(command.commandBuffer, native->layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, byteSize, data);
}

void VulkanRhiDevice::Dispatch(void *context, uint32_t groupCountX, uint32_t groupCountY, uint32_t groupCountZ)
{
    auto &command = *static_cast<VulkanComputeCommandContext *>(context);
    if (command.commandBuffer != VK_NULL_HANDLE && command.boundPipeline.IsValid() && groupCountX > 0 &&
        groupCountY > 0 && groupCountZ > 0)
        vkCmdDispatch(command.commandBuffer, groupCountX, groupCountY, groupCountZ);
}

void VulkanRhiDevice::DispatchIndirect(void *context, rhi::BufferHandle arguments, uint64_t offset)
{
    auto &command = *static_cast<VulkanComputeCommandContext *>(context);
    const VkBuffer native = command.device ? command.device->Resolve(arguments) : VK_NULL_HANDLE;
    if (command.commandBuffer != VK_NULL_HANDLE && command.boundPipeline.IsValid() && native != VK_NULL_HANDLE)
        vkCmdDispatchIndirect(command.commandBuffer, native, offset);
}

void VulkanRhiDevice::CopyBuffer(void *context, rhi::BufferHandle source, rhi::BufferHandle destination,
                                 const rhi::BufferCopyRegion &region)
{
    auto &command = *static_cast<VulkanTransferCommandContext *>(context);
    const VkBuffer nativeSource = command.device ? command.device->Resolve(source) : VK_NULL_HANDLE;
    const VkBuffer nativeDestination = command.device ? command.device->Resolve(destination) : VK_NULL_HANDLE;
    if (command.commandBuffer == VK_NULL_HANDLE || nativeSource == VK_NULL_HANDLE ||
        nativeDestination == VK_NULL_HANDLE || region.byteSize == 0)
        return;
    const VkBufferCopy copy{region.sourceOffset, region.destinationOffset, region.byteSize};
    vkCmdCopyBuffer(command.commandBuffer, nativeSource, nativeDestination, 1, &copy);
}

void VulkanRhiDevice::CopyTexture(void *context, rhi::TextureHandle source, rhi::TextureHandle destination,
                                  const rhi::TextureCopyRegion &region)
{
    auto &command = *static_cast<VulkanTransferCommandContext *>(context);
    const VkImage nativeSource = command.device ? command.device->Resolve(source) : VK_NULL_HANDLE;
    const VkImage nativeDestination = command.device ? command.device->Resolve(destination) : VK_NULL_HANDLE;
    if (command.commandBuffer == VK_NULL_HANDLE || nativeSource == VK_NULL_HANDLE ||
        nativeDestination == VK_NULL_HANDLE || region.width == 0 || region.height == 0 || region.depth == 0)
        return;

    VkImageAspectFlags aspect = VK_IMAGE_ASPECT_COLOR_BIT;
    switch (region.aspect) {
    case rhi::TextureAspect::Color:
        break;
    case rhi::TextureAspect::Depth:
        aspect = VK_IMAGE_ASPECT_DEPTH_BIT;
        break;
    case rhi::TextureAspect::Stencil:
        aspect = VK_IMAGE_ASPECT_STENCIL_BIT;
        break;
    case rhi::TextureAspect::DepthStencil:
        aspect = VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT;
        break;
    }

    VkImageCopy copy{};
    copy.srcSubresource = {aspect, region.sourceMip, region.sourceLayer, 1};
    copy.dstSubresource = {aspect, region.destinationMip, region.destinationLayer, 1};
    copy.extent = {region.width, region.height, region.depth};
    vkCmdCopyImage(command.commandBuffer, nativeSource, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, nativeDestination,
                   VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &copy);
}

void VulkanRhiDevice::ResolveTexture(void *context, rhi::TextureHandle source, rhi::TextureHandle destination,
                                     const rhi::TextureResolveRegion &region)
{
    auto &command = *static_cast<VulkanTransferCommandContext *>(context);
    const VkImage nativeSource = command.device ? command.device->Resolve(source) : VK_NULL_HANDLE;
    const VkImage nativeDestination = command.device ? command.device->Resolve(destination) : VK_NULL_HANDLE;
    if (command.commandBuffer == VK_NULL_HANDLE || nativeSource == VK_NULL_HANDLE ||
        nativeDestination == VK_NULL_HANDLE || nativeSource == nativeDestination || region.width == 0 ||
        region.height == 0 || region.depth == 0)
        return;

    VkImageAspectFlags aspect = VK_IMAGE_ASPECT_COLOR_BIT;
    switch (region.aspect) {
    case rhi::TextureAspect::Color:
        break;
    case rhi::TextureAspect::Depth:
        aspect = VK_IMAGE_ASPECT_DEPTH_BIT;
        break;
    case rhi::TextureAspect::Stencil:
        aspect = VK_IMAGE_ASPECT_STENCIL_BIT;
        break;
    case rhi::TextureAspect::DepthStencil:
        aspect = VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT;
        break;
    }

    VkImageResolve resolve{};
    resolve.srcSubresource = {aspect, region.sourceMip, region.sourceLayer, 1};
    resolve.dstSubresource = {aspect, region.destinationMip, region.destinationLayer, 1};
    resolve.extent = {region.width, region.height, region.depth};
    vkCmdResolveImage(command.commandBuffer, nativeSource, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, nativeDestination,
                      VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &resolve);
}

} // namespace infernux::vk
