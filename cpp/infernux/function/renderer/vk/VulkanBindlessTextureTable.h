#pragma once

#include "VkDescriptorManager.h"
#include <function/renderer/rhi/RhiCapabilities.h>
#include <function/renderer/rhi/RhiDevice.h>
#include <function/renderer/rhi/RhiResourceIndex.h>

#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux::rhi
{
class TextureGpuView;
}

namespace infernux::vk
{

/// One device-global combined image/sampler table. Publication is serialized
/// on the render/upload owner; shader-visible slots remain stable until their
/// device-wide completion epoch has retired.
class VulkanBindlessTextureTable final
{
  public:
    static constexpr uint32_t DefaultRequestedCapacity = 16384;

    struct Stats final
    {
        bool ready = false;
        uint32_t capacity = 0;
        uint64_t descriptorWrites = 0;
        rhi::ResourceIndexAllocator::Stats indices{};
    };

    VulkanBindlessTextureTable() = default;
    ~VulkanBindlessTextureTable();

    VulkanBindlessTextureTable(const VulkanBindlessTextureTable &) = delete;
    VulkanBindlessTextureTable &operator=(const VulkanBindlessTextureTable &) = delete;

    [[nodiscard]] static uint32_t SelectCapacity(const rhi::DeviceLimits &limits,
                                                 uint32_t requested = DefaultRequestedCapacity) noexcept;
    [[nodiscard]] static constexpr bool CanUseShaderABI(const rhi::DeviceCapabilityState &capabilities,
                                                        bool tableReady) noexcept
    {
        return tableReady && capabilities.bindless.IsEnabled();
    }
    [[nodiscard]] static constexpr bool IsOrphanedPublication(bool viewExpired, bool resourceLive) noexcept
    {
        return viewExpired && !resourceLive;
    }
    [[nodiscard]] bool Initialize(VkDevice device, VkDescriptorManager &descriptorManager,
                                  const rhi::DeviceCapabilityState &capabilities, const rhi::DeviceLimits &limits,
                                  VkImageView fallbackView, VkSampler fallbackSampler,
                                  std::shared_ptr<const void> fallbackOwner,
                                  uint32_t requestedCapacity = DefaultRequestedCapacity);
    void DestroyAfterDeviceIdle() noexcept;

    [[nodiscard]] rhi::ResourceIndex Publish(VkImageView view, VkSampler sampler, std::shared_ptr<const void> owner);
    /// Publishes one immutable GPU view and records its stable slot on the
    /// view itself. The table retains only the view's GPU owner, so expired
    /// publications can be retired on the normal completion-epoch sweep.
    [[nodiscard]] rhi::ResourceIndex PublishTextureView(const std::shared_ptr<const rhi::TextureGpuView> &view,
                                                        VkImageView imageView, VkSampler sampler);
    [[nodiscard]] rhi::ResourceIndex Replace(rhi::ResourceIndex current, VkImageView view, VkSampler sampler,
                                             std::shared_ptr<const void> owner, rhi::SubmissionSerial retireAfter);
    [[nodiscard]] bool MarkUsed(rhi::ResourceIndex resource, rhi::SubmissionSerial serial) noexcept;
    /// Mark the shared global descriptor lease used by a submission.
    void MarkSetUsed(rhi::SubmissionSerial serial) noexcept;
    [[nodiscard]] bool RetireAfter(rhi::ResourceIndex resource, rhi::SubmissionSerial serial) noexcept;
    [[nodiscard]] size_t Collect(rhi::SubmissionSerial completedSerial) noexcept;

    [[nodiscard]] bool IsReady() const noexcept;
    [[nodiscard]] VkDescriptorSetLayout GetLayout() const noexcept;
    [[nodiscard]] VkDescriptorSet GetSet() const noexcept;
    [[nodiscard]] uint32_t ResolveShaderIndex(rhi::ResourceIndex resource) const noexcept;
    [[nodiscard]] Stats GetStats() const noexcept;

  private:
    struct Publication final
    {
        std::weak_ptr<const rhi::TextureGpuView> view;
        rhi::ResourceIndex resource;
    };

    void WriteDescriptor(uint32_t index, VkImageView view, VkSampler sampler) noexcept;

    mutable std::mutex m_mutex;
    VkDevice m_device = VK_NULL_HANDLE;
    VkDescriptorManager *m_descriptorManager = nullptr;
    VkDescriptorSetLayout m_layout = VK_NULL_HANDLE;
    VkDescriptorSet m_set = VK_NULL_HANDLE;
    DescriptorLease m_descriptorLease{};
    std::unique_ptr<rhi::ResourceIndexAllocator> m_indices;
    std::vector<std::shared_ptr<const void>> m_owners;
    std::vector<Publication> m_publications;
    std::vector<uint32_t> m_reclaimed;
    uint64_t m_descriptorWrites = 0;
    uint64_t m_tableEpoch = 0;
};

} // namespace infernux::vk
