#pragma once

#include <vulkan/vulkan.h>

namespace infernux::vkrender
{

inline constexpr VkPipelineStageFlags kShaderUniformReadStages =
    VK_PIPELINE_STAGE_VERTEX_SHADER_BIT | VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT;

// ── Image / View / Sampler creation helpers ─────────────────────────────

inline VkImageCreateInfo MakeImageCreateInfo2D(uint32_t width, uint32_t height, VkFormat format,
                                               VkImageUsageFlags usage,
                                               VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT)
{
    VkImageCreateInfo info{};
    info.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    info.imageType = VK_IMAGE_TYPE_2D;
    info.extent = {width, height, 1};
    info.mipLevels = 1;
    info.arrayLayers = 1;
    info.format = format;
    info.tiling = VK_IMAGE_TILING_OPTIMAL;
    info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    info.usage = usage;
    info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    info.samples = samples;
    return info;
}

inline VkImageViewCreateInfo MakeImageViewCreateInfo2D(VkImage image, VkFormat format, VkImageAspectFlags aspectMask)
{
    VkImageViewCreateInfo info{};
    info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
    info.image = image;
    info.viewType = VK_IMAGE_VIEW_TYPE_2D;
    info.format = format;
    info.subresourceRange.aspectMask = aspectMask;
    info.subresourceRange.baseMipLevel = 0;
    info.subresourceRange.levelCount = 1;
    info.subresourceRange.baseArrayLayer = 0;
    info.subresourceRange.layerCount = 1;
    return info;
}

inline VkSamplerCreateInfo MakeLinearClampSamplerInfo(VkBorderColor borderColor = VK_BORDER_COLOR_INT_OPAQUE_BLACK)
{
    VkSamplerCreateInfo info{};
    info.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
    info.magFilter = VK_FILTER_LINEAR;
    info.minFilter = VK_FILTER_LINEAR;
    info.addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    info.addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    info.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    info.anisotropyEnable = VK_FALSE;
    info.maxAnisotropy = 1.0f;
    info.borderColor = borderColor;
    info.unnormalizedCoordinates = VK_FALSE;
    info.compareEnable = VK_FALSE;
    info.compareOp = VK_COMPARE_OP_ALWAYS;
    info.mipmapMode = VK_SAMPLER_MIPMAP_MODE_LINEAR;
    info.mipLodBias = 0.0f;
    info.minLod = 0.0f;
    info.maxLod = 0.0f;
    return info;
}

inline VkImageMemoryBarrier MakeImageBarrier(VkImage image, VkImageLayout oldLayout, VkImageLayout newLayout,
                                             VkImageAspectFlags aspectMask, VkAccessFlags srcAccess,
                                             VkAccessFlags dstAccess)
{
    VkImageMemoryBarrier b{};
    b.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    b.oldLayout = oldLayout;
    b.newLayout = newLayout;
    b.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.image = image;
    b.subresourceRange.aspectMask = aspectMask;
    b.subresourceRange.baseMipLevel = 0;
    b.subresourceRange.levelCount = 1;
    b.subresourceRange.baseArrayLayer = 0;
    b.subresourceRange.layerCount = 1;
    b.srcAccessMask = srcAccess;
    b.dstAccessMask = dstAccess;
    return b;
}

struct ImageLayoutAccess
{
    VkPipelineStageFlags stage = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
    VkAccessFlags access = 0;
};

inline ImageLayoutAccess GetImageLayoutAccess(VkImageLayout layout)
{
    switch (layout) {
    case VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL:
        return {VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                VK_ACCESS_COLOR_ATTACHMENT_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT};
    case VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL:
        return {VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT,
                VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_READ_BIT | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT};
    case VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL:
        return {VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT |
                    VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
                VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_READ_BIT | VK_ACCESS_SHADER_READ_BIT};
    case VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL:
        return {VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, VK_ACCESS_SHADER_READ_BIT};
    case VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL:
        return {VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_READ_BIT};
    case VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL:
        return {VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_TRANSFER_WRITE_BIT};
    case VK_IMAGE_LAYOUT_GENERAL:
        return {VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_MEMORY_WRITE_BIT};
    case VK_IMAGE_LAYOUT_PRESENT_SRC_KHR:
        return {VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0};
    case VK_IMAGE_LAYOUT_UNDEFINED:
        return {VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, 0};
    default:
        return {VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_MEMORY_WRITE_BIT};
    }
}

inline void TransitionTrackedImageLayout(VkCommandBuffer commandBuffer, VkImage image, VkImageAspectFlags aspectMask,
                                         VkImageLayout &currentLayout, VkImageLayout nextLayout)
{
    if (commandBuffer == VK_NULL_HANDLE || image == VK_NULL_HANDLE || currentLayout == nextLayout)
        return;

    const auto source = GetImageLayoutAccess(currentLayout);
    const auto destination = GetImageLayoutAccess(nextLayout);
    const auto barrier =
        MakeImageBarrier(image, currentLayout, nextLayout, aspectMask, source.access, destination.access);
    vkCmdPipelineBarrier(commandBuffer, source.stage, destination.stage, 0, 0, nullptr, 0, nullptr, 1, &barrier);
    currentLayout = nextLayout;
}

// ── Safe-destroy helpers (VkDevice-owned handles) ───────────────────────

inline void SafeDestroy(VkDevice device, VkSampler &h)
{
    if (h != VK_NULL_HANDLE) {
        vkDestroySampler(device, h, nullptr);
        h = VK_NULL_HANDLE;
    }
}

inline void SafeDestroy(VkDevice device, VkImageView &h)
{
    if (h != VK_NULL_HANDLE) {
        vkDestroyImageView(device, h, nullptr);
        h = VK_NULL_HANDLE;
    }
}

inline void SafeDestroy(VkDevice device, VkPipeline &h)
{
    if (h != VK_NULL_HANDLE) {
        vkDestroyPipeline(device, h, nullptr);
        h = VK_NULL_HANDLE;
    }
}

inline void SafeDestroy(VkDevice device, VkPipelineLayout &h)
{
    if (h != VK_NULL_HANDLE) {
        vkDestroyPipelineLayout(device, h, nullptr);
        h = VK_NULL_HANDLE;
    }
}

inline void SafeDestroy(VkDevice device, VkDescriptorSetLayout &h)
{
    if (h != VK_NULL_HANDLE) {
        vkDestroyDescriptorSetLayout(device, h, nullptr);
        h = VK_NULL_HANDLE;
    }
}

inline void SafeDestroy(VkDevice device, VkFramebuffer &h)
{
    if (h != VK_NULL_HANDLE) {
        vkDestroyFramebuffer(device, h, nullptr);
        h = VK_NULL_HANDLE;
    }
}

inline void SafeDestroy(VkDevice device, VkRenderPass &h)
{
    if (h != VK_NULL_HANDLE) {
        vkDestroyRenderPass(device, h, nullptr);
        h = VK_NULL_HANDLE;
    }
}

// ── Existing helpers ────────────────────────────────────────────────────

inline VkSubpassDependency MakePipelineCompatibleSubpassDependency()
{
    VkSubpassDependency dependency{};
    dependency.srcSubpass = VK_SUBPASS_EXTERNAL;
    dependency.dstSubpass = 0;
    dependency.srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT |
                              VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
    dependency.srcAccessMask = VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
    dependency.dstStageMask =
        VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT;
    dependency.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
    return dependency;
}

inline VkDescriptorSetLayoutBinding MakeDescriptorSetLayoutBinding(uint32_t binding, VkDescriptorType descriptorType,
                                                                   VkShaderStageFlags stageFlags,
                                                                   uint32_t descriptorCount = 1)
{
    VkDescriptorSetLayoutBinding layoutBinding{};
    layoutBinding.binding = binding;
    layoutBinding.descriptorType = descriptorType;
    layoutBinding.descriptorCount = descriptorCount;
    layoutBinding.stageFlags = stageFlags;
    return layoutBinding;
}

inline bool CreateDescriptorSetLayout(VkDevice device, const VkDescriptorSetLayoutBinding *bindings,
                                      uint32_t bindingCount, VkDescriptorSetLayout &outLayout)
{
    VkDescriptorSetLayoutCreateInfo layoutInfo{};
    layoutInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    layoutInfo.bindingCount = bindingCount;
    layoutInfo.pBindings = bindingCount > 0 ? bindings : nullptr;

    outLayout = VK_NULL_HANDLE;
    return vkCreateDescriptorSetLayout(device, &layoutInfo, nullptr, &outLayout) == VK_SUCCESS;
}

inline void UpdateDescriptorSetWithBuffer(VkDevice device, VkDescriptorSet descriptorSet, uint32_t binding,
                                          VkDescriptorType descriptorType, const VkDescriptorBufferInfo &bufferInfo,
                                          uint32_t descriptorCount = 1)
{
    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = descriptorSet;
    write.dstBinding = binding;
    write.dstArrayElement = 0;
    write.descriptorType = descriptorType;
    write.descriptorCount = descriptorCount;
    write.pBufferInfo = &bufferInfo;
    vkUpdateDescriptorSets(device, 1, &write, 0, nullptr);
}

inline void UpdateDescriptorSetWithImage(VkDevice device, VkDescriptorSet descriptorSet, uint32_t binding,
                                         const VkDescriptorImageInfo &imageInfo,
                                         VkDescriptorType descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
                                         uint32_t descriptorCount = 1)
{
    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = descriptorSet;
    write.dstBinding = binding;
    write.dstArrayElement = 0;
    write.descriptorType = descriptorType;
    write.descriptorCount = descriptorCount;
    write.pImageInfo = &imageInfo;
    vkUpdateDescriptorSets(device, 1, &write, 0, nullptr);
}

inline void CmdBarrierUniformReadToTransferWrite(VkCommandBuffer cmdBuf)
{
    VkMemoryBarrier barrier{};
    barrier.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
    barrier.srcAccessMask = VK_ACCESS_UNIFORM_READ_BIT;
    barrier.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    vkCmdPipelineBarrier(cmdBuf, kShaderUniformReadStages, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 1, &barrier, 0, nullptr,
                         0, nullptr);
}

inline void CmdBarrierTransferWriteToUniformRead(VkCommandBuffer cmdBuf)
{
    VkMemoryBarrier barrier{};
    barrier.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
    barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    barrier.dstAccessMask = VK_ACCESS_UNIFORM_READ_BIT;
    vkCmdPipelineBarrier(cmdBuf, VK_PIPELINE_STAGE_TRANSFER_BIT, kShaderUniformReadStages, 0, 1, &barrier, 0, nullptr,
                         0, nullptr);
}

} // namespace infernux::vkrender
