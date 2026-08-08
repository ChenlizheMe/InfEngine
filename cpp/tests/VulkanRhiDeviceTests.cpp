#include <function/renderer/vk/RhiVulkanTypes.h>
#include <function/renderer/vk/VulkanRhiDevice.h>

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <cstdint>
#include <atomic>
#include <chrono>
#include <memory>
#include <shared_mutex>
#include <thread>
#include <type_traits>

using namespace infernux;

namespace
{

template <typename NativeHandle> NativeHandle FakeHandle(uintptr_t value)
{
    if constexpr (std::is_pointer_v<NativeHandle>)
        return reinterpret_cast<NativeHandle>(value);
    else
        return static_cast<NativeHandle>(value);
}

bool HasFeatureNode(const VkPhysicalDeviceFeatures2 &root, VkStructureType type)
{
    auto *node = reinterpret_cast<const VkBaseOutStructure *>(root.pNext);
    while (node != nullptr) {
        if (node->sType == type)
            return true;
        node = node->pNext;
    }
    return false;
}

void EnableCompleteBindless(VkPhysicalDeviceVulkan12Features &features)
{
    features.descriptorIndexing = VK_TRUE;
    features.runtimeDescriptorArray = VK_TRUE;
    features.descriptorBindingPartiallyBound = VK_TRUE;
    features.descriptorBindingVariableDescriptorCount = VK_TRUE;
    features.descriptorBindingSampledImageUpdateAfterBind = VK_TRUE;
    features.descriptorBindingUniformBufferUpdateAfterBind = VK_TRUE;
    features.descriptorBindingStorageBufferUpdateAfterBind = VK_TRUE;
    features.descriptorBindingUpdateUnusedWhilePending = VK_TRUE;
}

void EnableCompleteBindless(VkPhysicalDeviceDescriptorIndexingFeaturesEXT &features)
{
    features.runtimeDescriptorArray = VK_TRUE;
    features.descriptorBindingPartiallyBound = VK_TRUE;
    features.descriptorBindingVariableDescriptorCount = VK_TRUE;
    features.shaderSampledImageArrayNonUniformIndexing = VK_TRUE;
    features.descriptorBindingSampledImageUpdateAfterBind = VK_TRUE;
    features.descriptorBindingUniformBufferUpdateAfterBind = VK_TRUE;
    features.descriptorBindingStorageBufferUpdateAfterBind = VK_TRUE;
    features.descriptorBindingUpdateUnusedWhilePending = VK_TRUE;
}

} // namespace

int main()
{
    static_assert(rhi::ToVkFormat(rhi::PixelFormat::RG32UInt) == VK_FORMAT_R32G32_UINT);
    static_assert(rhi::FromVkFormat(VK_FORMAT_R32G32_UINT) == rhi::PixelFormat::RG32UInt);
    static_assert(rhi::ToVkFormat(rhi::PixelFormat::BC5UNorm) == VK_FORMAT_BC5_UNORM_BLOCK);
    static_assert(rhi::FromVkFormat(VK_FORMAT_BC7_SRGB_BLOCK) == rhi::PixelFormat::BC7Srgb);
    static_assert(rhi::ToVkFormat(rhi::PixelFormat::RGBA4UNormPack16) == VK_FORMAT_R4G4B4A4_UNORM_PACK16);
    static_assert(rhi::ToVkFormat(rhi::PixelFormat::RGBA16UNorm) == VK_FORMAT_R16G16B16A16_UNORM);

    vk::VulkanRhiDevice device;
    vk::VulkanRhiDevice secondDevice;
    assert(device.GetDeviceId() != rhi::InvalidDeviceId);
    assert(secondDevice.GetDeviceId() != rhi::InvalidDeviceId);
    assert(device.GetDeviceId() != secondDevice.GetDeviceId());
    assert(!device.CreateGraphicsPipeline({}).IsValid());
    assert(!device.CreateComputePipeline({}).IsValid());

    const auto oldLifetime = device.GetLifetime();
    std::atomic_bool resetStarted{false};
    std::thread resetThread;
    {
        std::shared_lock lifetimeRead(oldLifetime->gate);
        resetThread = std::thread([&] {
            resetStarted.store(true, std::memory_order_release);
            device.Reset();
        });
        while (!resetStarted.load(std::memory_order_acquire))
            std::this_thread::yield();
        // Reset has announced itself but cannot pass the shared lease yet.
        assert(oldLifetime->alive.load(std::memory_order_acquire));
    }
    resetThread.join();
    assert(!oldLifetime->alive.load(std::memory_order_acquire));
    assert(device.GetLifetime() != oldLifetime);
    assert(device.GetLifetime()->alive.load(std::memory_order_acquire));

    rhi::TextureDesc nullTextureDesc;
    nullTextureDesc.format = rhi::PixelFormat::RGBA8UNorm;
    assert(!device.CreateTexture(nullTextureDesc).IsValid());

    rhi::TextureDesc depthTextureDesc;
    depthTextureDesc.format = rhi::PixelFormat::D32SFloat;
    depthTextureDesc.usage = rhi::TextureUsageFlags::Sampled;
    assert(!vk::VulkanRhiDevice::IsValidTextureDesc(depthTextureDesc));
    depthTextureDesc.usage = rhi::TextureUsageFlags::Sampled | rhi::TextureUsageFlags::DepthStencilAttachment;
    assert(vk::VulkanRhiDevice::IsValidTextureDesc(depthTextureDesc));
    depthTextureDesc.usage = rhi::TextureUsageFlags::ColorAttachment | rhi::TextureUsageFlags::DepthStencilAttachment;
    assert(!vk::VulkanRhiDevice::IsValidTextureDesc(depthTextureDesc));

    rhi::TextureDesc colorTextureDesc;
    colorTextureDesc.format = rhi::PixelFormat::RGBA8UNorm;
    colorTextureDesc.usage = rhi::TextureUsageFlags::ColorAttachment;
    assert(vk::VulkanRhiDevice::IsValidTextureDesc(colorTextureDesc));
    colorTextureDesc.usage = rhi::TextureUsageFlags::ColorAttachment | rhi::TextureUsageFlags::DepthStencilAttachment;
    assert(!vk::VulkanRhiDevice::IsValidTextureDesc(colorTextureDesc));

    assert(!device.CreateTextureView({}).IsValid());
    assert(!device.CreateSampler({}).IsValid());

    const VkImage nativeTexture = FakeHandle<VkImage>(0x91);
    const auto texture = device.RegisterTexture(nativeTexture);
    assert(texture.IsValid());
    assert(texture.Device() == device.GetDeviceId());
    assert(device.Resolve(texture) == nativeTexture);
    assert(secondDevice.Resolve(texture) == VK_NULL_HANDLE);
    secondDevice.Release(texture);
    assert(device.Resolve(texture) == nativeTexture);
    device.Release(texture);
    assert(device.Resolve(texture) == VK_NULL_HANDLE);

    const VkSampler nativeSampler = FakeHandle<VkSampler>(0x92);
    const auto sampler = device.RegisterSampler(nativeSampler);
    assert(sampler.IsValid());
    assert(device.Resolve(sampler) == nativeSampler);
    device.Release(sampler);
    assert(device.Resolve(sampler) == VK_NULL_HANDLE);

    const VkImageView firstNative = FakeHandle<VkImageView>(0x101);
    const auto first = device.RegisterTextureView(firstNative);
    assert(first.IsValid());
    assert(device.Resolve(first) == firstNative);

    device.Release(first);
    assert(device.Resolve(first) == VK_NULL_HANDLE);

    const VkImageView secondNative = FakeHandle<VkImageView>(0x202);
    const auto second = device.RegisterTextureView(secondNative);
    assert(second.IsValid());
    assert(second.index == first.index);
    assert(second.generation != first.generation);
    assert(device.Resolve(second) == secondNative);

    device.Release(first);
    assert(device.Resolve(second) == secondNative);

    device.Reset();
    assert(device.Resolve(second) == VK_NULL_HANDLE);

    const VkImageView thirdNative = FakeHandle<VkImageView>(0x303);
    const auto third = device.RegisterTextureView(thirdNative);
    assert(third.index == second.index);
    assert(third.generation != second.generation);
    assert(device.Resolve(third) == thirdNative);

    const VkBuffer nativeBuffer = FakeHandle<VkBuffer>(0x404);
    const auto buffer = device.RegisterBuffer(nativeBuffer);
    assert(buffer.IsValid());
    assert(device.Resolve(buffer) == nativeBuffer);
    device.Release(buffer);
    assert(device.Resolve(buffer) == VK_NULL_HANDLE);

    const VkPipeline nativeCompute = FakeHandle<VkPipeline>(0x505);
    const VkPipelineLayout nativeLayout = FakeHandle<VkPipelineLayout>(0x606);
    const auto compute = device.RegisterComputePipeline(nativeCompute, nativeLayout);
    assert(compute.IsValid());
    device.Release(compute);

    const auto replacementCompute = device.RegisterComputePipeline(nativeCompute, nativeLayout);
    assert(replacementCompute.index == compute.index);
    assert(replacementCompute.generation != compute.generation);

    vk::VulkanCapabilityProbeData coreProbe;
    coreProbe.apiVersion = VK_API_VERSION_1_3;
    coreProbe.properties.apiVersion = VK_API_VERSION_1_3;
    EnableCompleteBindless(coreProbe.vulkan12Features);
    coreProbe.vulkan12Features.timelineSemaphore = VK_TRUE;
    coreProbe.vulkan13Features.dynamicRendering = VK_TRUE;
    coreProbe.vulkan13Features.synchronization2 = VK_TRUE;
    const auto coreSnapshot = vk::VulkanCapabilitySnapshot::FromProbe(coreProbe);
    assert(coreSnapshot.supported.bindless.IsSupported());
    assert(coreSnapshot.supported.timelineSemaphore.supported);
    assert(coreSnapshot.supported.dynamicRendering.supported);
    assert(coreSnapshot.supported.synchronization2.supported);
    assert(coreSnapshot.supported.submit2.supported);
    assert(!coreSnapshot.supported.dynamicRendering.enabled);
    assert(coreSnapshot.properties.apiVersion == VK_API_VERSION_1_3);

    rhi::DeviceCapabilityRequest allModern;
    allModern.descriptorIndexing = true;
    allModern.timelineSemaphore = true;
    allModern.dynamicRendering = true;
    allModern.synchronization2 = true;
    allModern.submit2 = true;
    vk::VulkanDeviceFeatureChain coreChain(coreSnapshot);
    assert(coreChain.Enable(allModern));
    assert(coreChain.GetEnabledState().bindless.IsEnabled());
    assert(coreChain.GetEnabledState().timelineSemaphore.IsEnabled());
    assert(coreChain.GetEnabledState().dynamicRendering.IsEnabled());
    assert(coreChain.GetEnabledState().synchronization2.IsEnabled());
    assert(coreChain.GetEnabledState().submit2.IsEnabled());
    assert(rhi::CheckDeviceCapabilities(coreChain.GetEnabledState(), allModern).IsSupported());
    assert(HasFeatureNode(coreChain.GetFeatures2(), VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES));
    assert(HasFeatureNode(coreChain.GetFeatures2(), VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES));

    vk::VulkanCapabilityProbeData khrProbe;
    khrProbe.apiVersion = VK_API_VERSION_1_1;
    khrProbe.descriptorIndexingExtension = true;
    khrProbe.timelineSemaphoreExtension = true;
    khrProbe.dynamicRenderingExtension = true;
    khrProbe.synchronization2Extension = true;
    EnableCompleteBindless(khrProbe.descriptorIndexingFeaturesEXT);
    khrProbe.timelineSemaphoreFeaturesKHR.timelineSemaphore = VK_TRUE;
    khrProbe.dynamicRenderingFeaturesKHR.dynamicRendering = VK_TRUE;
    khrProbe.synchronization2FeaturesKHR.synchronization2 = VK_TRUE;
    const auto khrSnapshot = vk::VulkanCapabilitySnapshot::FromProbe(khrProbe);
    assert(khrSnapshot.supported.bindless.IsSupported());
    assert(khrSnapshot.supported.timelineSemaphore.supported);
    assert(khrSnapshot.supported.dynamicRendering.supported);
    assert(khrSnapshot.supported.synchronization2.supported);
    vk::VulkanDeviceFeatureChain khrChain(khrSnapshot);
    assert(rhi::CheckDeviceCapabilities(khrChain.GetEnabledState(), allModern).code ==
           rhi::DeviceCapabilityDiagnosticCode::NotEnabled);
    assert(khrChain.Enable(allModern));
    assert(HasFeatureNode(khrChain.GetFeatures2(), VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_INDEXING_FEATURES_EXT));
    assert(HasFeatureNode(khrChain.GetFeatures2(), VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_TIMELINE_SEMAPHORE_FEATURES_KHR));
    assert(HasFeatureNode(khrChain.GetFeatures2(), VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DYNAMIC_RENDERING_FEATURES_KHR));
    assert(HasFeatureNode(khrChain.GetFeatures2(), VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SYNCHRONIZATION_2_FEATURES_KHR));
    assert(rhi::CheckDeviceCapabilities(khrChain.GetEnabledState(), allModern).IsSupported());

    vk::VulkanCapabilityProbeData incompleteExtProbe;
    incompleteExtProbe.apiVersion = VK_API_VERSION_1_1;
    incompleteExtProbe.descriptorIndexingExtension = true;
    incompleteExtProbe.descriptorIndexingFeaturesEXT.runtimeDescriptorArray = VK_TRUE;
    incompleteExtProbe.descriptorIndexingFeaturesEXT.descriptorBindingPartiallyBound = VK_TRUE;
    incompleteExtProbe.descriptorIndexingFeaturesEXT.descriptorBindingVariableDescriptorCount = VK_TRUE;
    const auto incompleteExtSnapshot = vk::VulkanCapabilitySnapshot::FromProbe(incompleteExtProbe);
    assert(!incompleteExtSnapshot.supported.bindless.IsSupported());
    vk::VulkanDeviceFeatureChain incompleteExtChain(incompleteExtSnapshot);
    rhi::DeviceCapabilityRequest incompleteExtRequest;
    incompleteExtRequest.descriptorIndexing = true;
    assert(!incompleteExtChain.Enable(incompleteExtRequest));
    assert(incompleteExtChain.GetFeatures2().pNext == nullptr);

    vk::VulkanCapabilityProbeData incompleteProbe;
    incompleteProbe.apiVersion = VK_API_VERSION_1_3;
    incompleteProbe.vulkan12Features.descriptorIndexing = VK_TRUE;
    const auto incompleteSnapshot = vk::VulkanCapabilitySnapshot::FromProbe(incompleteProbe);
    vk::VulkanDeviceFeatureChain incompleteChain(incompleteSnapshot);
    rhi::DeviceCapabilityRequest bindlessOnly;
    bindlessOnly.descriptorIndexing = true;
    assert(!incompleteChain.Enable(bindlessOnly));
    assert(incompleteChain.GetFailure().code == rhi::DeviceCapabilityDiagnosticCode::IncompleteDescriptorIndexing);
    assert(incompleteChain.GetFeatures2().pNext == nullptr);
    assert(!incompleteChain.GetEnabledState().bindless.IsEnabled());
    return 0;
}
