#include "VulkanBindlessTextureTable.h"

#include <function/renderer/rhi/RhiTexture.h>

#include <algorithm>
#include <atomic>

namespace infernux::vk
{
namespace
{
std::atomic<uint64_t> g_nextBindlessTextureTableEpoch{1};
}

VulkanBindlessTextureTable::~VulkanBindlessTextureTable()
{
    DestroyAfterDeviceIdle();
}

uint32_t VulkanBindlessTextureTable::SelectCapacity(const rhi::DeviceLimits &limits, uint32_t requested) noexcept
{
    if (requested < 2 || limits.maxUpdateAfterBindDescriptors < 2 || limits.maxUpdateAfterBindResourcesPerStage < 2 ||
        limits.maxUpdateAfterBindSamplersPerStage < 2 || limits.maxUpdateAfterBindSampledTexturesPerStage < 2 ||
        limits.maxUpdateAfterBindSamplersPerSet < 2 || limits.maxUpdateAfterBindSampledTexturesPerSet < 2)
        return 0;
    return (std::min)({requested, limits.maxUpdateAfterBindDescriptors, limits.maxUpdateAfterBindResourcesPerStage,
                       limits.maxUpdateAfterBindSamplersPerStage, limits.maxUpdateAfterBindSampledTexturesPerStage,
                       limits.maxUpdateAfterBindSamplersPerSet, limits.maxUpdateAfterBindSampledTexturesPerSet});
}

bool VulkanBindlessTextureTable::Initialize(VkDevice device, VkDescriptorManager &descriptorManager,
                                            const rhi::DeviceCapabilityState &capabilities,
                                            const rhi::DeviceLimits &limits, VkImageView fallbackView,
                                            VkSampler fallbackSampler, std::shared_ptr<const void> fallbackOwner,
                                            uint32_t requestedCapacity)
{
    std::lock_guard lock(m_mutex);
    if (m_device != VK_NULL_HANDLE || device == VK_NULL_HANDLE || !capabilities.bindless.IsEnabled() ||
        fallbackView == VK_NULL_HANDLE || fallbackSampler == VK_NULL_HANDLE || !fallbackOwner)
        return false;

    const uint32_t capacity = SelectCapacity(limits, requestedCapacity);
    if (capacity < 2)
        return false;

    VkDescriptorSetLayoutBinding binding{};
    binding.binding = 0;
    binding.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    binding.descriptorCount = capacity;
    binding.stageFlags = VK_SHADER_STAGE_ALL;

    VkDescriptorBindingFlags bindingFlags = VK_DESCRIPTOR_BINDING_PARTIALLY_BOUND_BIT |
                                            VK_DESCRIPTOR_BINDING_VARIABLE_DESCRIPTOR_COUNT_BIT |
                                            VK_DESCRIPTOR_BINDING_UPDATE_AFTER_BIND_BIT;
    if (capabilities.bindless.descriptorBindingUpdateUnusedWhilePending.IsEnabled())
        bindingFlags |= VK_DESCRIPTOR_BINDING_UPDATE_UNUSED_WHILE_PENDING_BIT;
    VkDescriptorSetLayoutBindingFlagsCreateInfo bindingFlagsInfo{};
    bindingFlagsInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_BINDING_FLAGS_CREATE_INFO;
    bindingFlagsInfo.bindingCount = 1;
    bindingFlagsInfo.pBindingFlags = &bindingFlags;

    VkDescriptorSetLayoutCreateInfo layoutInfo{};
    layoutInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    layoutInfo.pNext = &bindingFlagsInfo;
    layoutInfo.flags = VK_DESCRIPTOR_SET_LAYOUT_CREATE_UPDATE_AFTER_BIND_POOL_BIT;
    layoutInfo.bindingCount = 1;
    layoutInfo.pBindings = &binding;
    if (vkCreateDescriptorSetLayout(device, &layoutInfo, nullptr, &m_layout) != VK_SUCCESS)
        return false;

    const DescriptorLease descriptorLease = descriptorManager.AllocateBindlessTextureSet(m_layout, capacity);
    if (!descriptorLease.IsValid()) {
        vkDestroyDescriptorSetLayout(device, m_layout, nullptr);
        m_layout = VK_NULL_HANDLE;
        return false;
    }

    m_device = device;
    m_descriptorManager = &descriptorManager;
    m_descriptorLease = descriptorLease;
    m_set = descriptorLease.set;
    m_indices = std::make_unique<rhi::ResourceIndexAllocator>(capacity);
    m_tableEpoch = g_nextBindlessTextureTableEpoch.fetch_add(1, std::memory_order_relaxed);
    if (m_tableEpoch == 0)
        m_tableEpoch = g_nextBindlessTextureTableEpoch.fetch_add(1, std::memory_order_relaxed);
    m_owners.resize(capacity);
    m_owners[0] = std::move(fallbackOwner);

    // Make every slot valid from the first draw. Capacity exhaustion and stale
    // generations resolve to slot zero; accidental unused indices also sample
    // the same deterministic fallback instead of an uninitialized descriptor.
    std::vector<VkDescriptorImageInfo> fallbackInfos(
        capacity, VkDescriptorImageInfo{fallbackSampler, fallbackView, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL});
    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = m_set;
    write.dstBinding = 0;
    write.descriptorCount = capacity;
    write.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    write.pImageInfo = fallbackInfos.data();
    vkUpdateDescriptorSets(m_device, 1, &write, 0, nullptr);
    m_descriptorWrites = capacity;
    return true;
}

void VulkanBindlessTextureTable::DestroyAfterDeviceIdle() noexcept
{
    std::vector<std::shared_ptr<const void>> owners;
    VkDevice device = VK_NULL_HANDLE;
    VkDescriptorSetLayout layout = VK_NULL_HANDLE;
    VkDescriptorManager *descriptorManager = nullptr;
    DescriptorLease descriptorLease{};
    {
        std::lock_guard lock(m_mutex);
        owners = std::move(m_owners);
        m_publications.clear();
        m_reclaimed.clear();
        m_indices.reset();
        device = m_device;
        layout = m_layout;
        descriptorManager = m_descriptorManager;
        descriptorLease = m_descriptorLease;
        m_device = VK_NULL_HANDLE;
        m_descriptorManager = nullptr;
        m_layout = VK_NULL_HANDLE;
        m_set = VK_NULL_HANDLE;
        m_descriptorLease = {};
        m_descriptorWrites = 0;
        m_tableEpoch = 0;
    }
    owners.clear();
    if (descriptorManager != nullptr)
        descriptorManager->Retire(descriptorLease);
    if (device != VK_NULL_HANDLE) {
        if (layout != VK_NULL_HANDLE)
            vkDestroyDescriptorSetLayout(device, layout, nullptr);
    }
}

rhi::ResourceIndex VulkanBindlessTextureTable::Publish(VkImageView view, VkSampler sampler,
                                                       std::shared_ptr<const void> owner)
{
    std::lock_guard lock(m_mutex);
    if (!m_indices || view == VK_NULL_HANDLE || sampler == VK_NULL_HANDLE || !owner)
        return {};
    const rhi::ResourceIndex resource = m_indices->Allocate();
    if (!resource.IsValid())
        return {};
    WriteDescriptor(resource.index, view, sampler);
    m_owners[resource.index] = std::move(owner);
    return resource;
}

rhi::ResourceIndex
VulkanBindlessTextureTable::PublishTextureView(const std::shared_ptr<const rhi::TextureGpuView> &view,
                                               VkImageView imageView, VkSampler sampler)
{
    if (!view || !view->IsValid() || imageView == VK_NULL_HANDLE || sampler == VK_NULL_HANDLE)
        return {};

    std::lock_guard lock(m_mutex);
    if (!m_indices)
        return {};

    const rhi::ResourceIndex existing = view->GetBindlessResourceIndex(m_tableEpoch);
    if (existing.IsValid() && m_indices->IsLive(existing))
        return existing;

    const rhi::ResourceIndex resource = m_indices->Allocate();
    if (!resource.IsValid())
        return {};

    WriteDescriptor(resource.index, imageView, sampler);
    m_owners[resource.index] = view->GetOwner();
    if (!view->SetBindlessResourceIndex(m_tableEpoch, resource)) {
        (void)m_indices->Cancel(resource);
        m_owners[resource.index].reset();
        return {};
    }
    m_publications.push_back({view, resource});
    return resource;
}

rhi::ResourceIndex VulkanBindlessTextureTable::Replace(rhi::ResourceIndex current, VkImageView view, VkSampler sampler,
                                                       std::shared_ptr<const void> owner,
                                                       rhi::SubmissionSerial retireAfter)
{
    if (current.IsValid() && !current.IsFallback() && retireAfter == rhi::InvalidSubmissionSerial)
        return {};
    const rhi::ResourceIndex replacement = Publish(view, sampler, std::move(owner));
    if (!replacement.IsValid())
        return {};
    if (current.IsValid() && !current.IsFallback() && !RetireAfter(current, retireAfter)) {
        (void)RetireAfter(replacement, retireAfter);
        return {};
    }
    return replacement;
}

bool VulkanBindlessTextureTable::MarkUsed(rhi::ResourceIndex resource, rhi::SubmissionSerial serial) noexcept
{
    std::lock_guard lock(m_mutex);
    return m_indices && m_indices->MarkUsed(resource, serial);
}

bool VulkanBindlessTextureTable::RetireAfter(rhi::ResourceIndex resource, rhi::SubmissionSerial serial) noexcept
{
    std::lock_guard lock(m_mutex);
    return m_indices && m_indices->RetireAfter(resource, serial);
}

void VulkanBindlessTextureTable::MarkSetUsed(rhi::SubmissionSerial serial) noexcept
{
    if (serial == rhi::InvalidSubmissionSerial)
        return;
    std::lock_guard lock(m_mutex);
    if (m_descriptorManager != nullptr)
        m_descriptorManager->MarkUsed(m_descriptorLease, serial);
}

size_t VulkanBindlessTextureTable::Collect(rhi::SubmissionSerial completedSerial) noexcept
{
    std::vector<std::shared_ptr<const void>> released;
    size_t collected = 0;
    {
        std::lock_guard lock(m_mutex);
        if (!m_indices)
            return 0;

        for (auto it = m_publications.begin(); it != m_publications.end();) {
            const bool viewExpired = it->view.expired();
            if (!viewExpired) {
                ++it;
                continue;
            }
            if (IsOrphanedPublication(viewExpired, m_indices->IsLive(it->resource))) {
                it = m_publications.erase(it);
                continue;
            }
            if (!m_indices->RetireAfter(it->resource, completedSerial)) {
                ++it;
                continue;
            }
            it = m_publications.erase(it);
        }

        m_reclaimed.clear();
        collected = m_indices->CollectIndices(completedSerial, &m_reclaimed);
        released.reserve(m_reclaimed.size());
        for (const uint32_t index : m_reclaimed) {
            if (index < m_owners.size())
                released.push_back(std::move(m_owners[index]));
        }
    }
    released.clear();
    return collected;
}

bool VulkanBindlessTextureTable::IsReady() const noexcept
{
    std::lock_guard lock(m_mutex);
    return m_device != VK_NULL_HANDLE && m_set != VK_NULL_HANDLE && m_indices;
}

VkDescriptorSetLayout VulkanBindlessTextureTable::GetLayout() const noexcept
{
    std::lock_guard lock(m_mutex);
    return m_layout;
}

VkDescriptorSet VulkanBindlessTextureTable::GetSet() const noexcept
{
    std::lock_guard lock(m_mutex);
    return m_set;
}

uint32_t VulkanBindlessTextureTable::ResolveShaderIndex(rhi::ResourceIndex resource) const noexcept
{
    std::lock_guard lock(m_mutex);
    return m_indices ? m_indices->ResolveShaderIndex(resource) : rhi::ResourceIndex::FallbackIndex;
}

VulkanBindlessTextureTable::Stats VulkanBindlessTextureTable::GetStats() const noexcept
{
    std::lock_guard lock(m_mutex);
    Stats stats;
    stats.ready = m_device != VK_NULL_HANDLE && m_set != VK_NULL_HANDLE && m_indices;
    stats.capacity = static_cast<uint32_t>(m_owners.size());
    stats.descriptorWrites = m_descriptorWrites;
    if (m_indices)
        stats.indices = m_indices->GetStats();
    return stats;
}

void VulkanBindlessTextureTable::WriteDescriptor(uint32_t index, VkImageView view, VkSampler sampler) noexcept
{
    VkDescriptorImageInfo imageInfo{sampler, view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = m_set;
    write.dstBinding = 0;
    write.dstArrayElement = index;
    write.descriptorCount = 1;
    write.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    write.pImageInfo = &imageInfo;
    vkUpdateDescriptorSets(m_device, 1, &write, 0, nullptr);
    ++m_descriptorWrites;
}

} // namespace infernux::vk
