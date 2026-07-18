#include <function/renderer/vk/VulkanRhiDevice.h>

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
    vk::VulkanRhiDevice device;

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
    return 0;
}
