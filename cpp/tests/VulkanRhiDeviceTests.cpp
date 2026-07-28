#include <function/renderer/vk/RhiVulkanTypes.h>
#include <function/renderer/vk/VulkanRhiDevice.h>

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <cstdint>
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
    return 0;
}
