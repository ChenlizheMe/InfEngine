#pragma once

#include <array>
#include <cstdint>

#include <vulkan/vulkan.h>

#include <function/renderer/rhi/RhiSubmission.h>

namespace infernux::vk
{
class VkDescriptorManager;
}

namespace infernux::vkdebug
{

#ifndef INFERNUX_VULKAN_VALIDATION_LAYERS
#define INFERNUX_VULKAN_VALIDATION_LAYERS 0
#endif

struct DescriptorBindTraceSnapshot
{
    uint64_t sequence = 0;
    const char *site = nullptr;
    uint64_t commandBufferRaw = 0;
    uint64_t pipelineLayoutRaw = 0;
    uint32_t firstSet = 0;
    uint32_t descriptorSetCount = 0;
    std::array<uint64_t, 4> descriptorSetRaws{};
};

void SetDescriptorRecordingContext(vk::VkDescriptorManager *manager, rhi::SubmissionSerial submissionSerial) noexcept;
void ClearDescriptorRecordingContext() noexcept;
void PushDescriptorRecordingContext(vk::VkDescriptorManager *manager, rhi::SubmissionSerial submissionSerial) noexcept;
void PopDescriptorRecordingContext() noexcept;
[[nodiscard]] rhi::SubmissionSerial GetDescriptorRecordingSubmissionSerial() noexcept;

class DescriptorRecordingScope
{
  public:
    DescriptorRecordingScope(vk::VkDescriptorManager *manager, rhi::SubmissionSerial submissionSerial) noexcept
    {
        PushDescriptorRecordingContext(manager, submissionSerial);
    }
    ~DescriptorRecordingScope()
    {
        PopDescriptorRecordingContext();
    }
    DescriptorRecordingScope(const DescriptorRecordingScope &) = delete;
    DescriptorRecordingScope &operator=(const DescriptorRecordingScope &) = delete;
};

inline bool IsSuspiciousDescriptorRaw(uint64_t raw)
{
    if (raw == 0ull)
        return false;

    const uint32_t lo = static_cast<uint32_t>(raw & 0xffffffffull);
    const uint32_t hi = static_cast<uint32_t>((raw >> 32) & 0xffffffffull);
    return hi == lo && lo <= 0x000fffffu;
}

#if INFERNUX_VULKAN_VALIDATION_LAYERS

void RecordDescriptorBind(const char *site, VkCommandBuffer cmdBuf, VkPipelineLayout layout, uint32_t firstSet,
                          uint32_t descriptorSetCount, const VkDescriptorSet *descriptorSets);

[[nodiscard]] DescriptorBindTraceSnapshot GetLastDescriptorBindSnapshot();

[[nodiscard]] bool FindRecentDescriptorBindByRaw(uint64_t descriptorRaw, DescriptorBindTraceSnapshot &outMatch,
                                                 uint32_t &outLocalIndex);

#else

inline void RecordDescriptorBind(const char *, VkCommandBuffer, VkPipelineLayout, uint32_t, uint32_t,
                                 const VkDescriptorSet *)
{
}

[[nodiscard]] inline DescriptorBindTraceSnapshot GetLastDescriptorBindSnapshot()
{
    return {};
}

[[nodiscard]] inline bool FindRecentDescriptorBindByRaw(uint64_t, DescriptorBindTraceSnapshot &, uint32_t &)
{
    return false;
}

#endif

void CmdBindDescriptorSetsTracked(const char *site, VkCommandBuffer cmdBuf, VkPipelineBindPoint pipelineBindPoint,
                                  VkPipelineLayout layout, uint32_t firstSet, uint32_t descriptorSetCount,
                                  const VkDescriptorSet *descriptorSets, uint32_t dynamicOffsetCount,
                                  const uint32_t *dynamicOffsets);

} // namespace infernux::vkdebug
