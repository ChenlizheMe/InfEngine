#include "DescriptorBindTrace.h"

#include "VkDescriptorManager.h"

#include <vector>

#if INFERNUX_VULKAN_VALIDATION_LAYERS
#include <algorithm>
#include <atomic>
#include <mutex>

#include <core/error/InxError.h>
#endif

namespace infernux::vkdebug
{
namespace
{

thread_local vk::VkDescriptorManager *g_recordingDescriptorManager = nullptr;
thread_local rhi::SubmissionSerial g_recordingSubmissionSerial = rhi::InvalidSubmissionSerial;
struct RecordingContext
{
    vk::VkDescriptorManager *manager = nullptr;
    rhi::SubmissionSerial serial = rhi::InvalidSubmissionSerial;
};
thread_local std::vector<RecordingContext> g_recordingContextStack;

#if INFERNUX_VULKAN_VALIDATION_LAYERS
// Validation can report a bad bind at submission time, so retain a complete
// frame-sized diagnostic history in validation builds only.
constexpr size_t kDescriptorBindTraceHistorySize = 8192;
std::mutex g_descriptorBindTraceMutex;
std::array<DescriptorBindTraceSnapshot, kDescriptorBindTraceHistorySize> g_descriptorBindTraceHistory{};
size_t g_descriptorBindTraceHistoryWriteIndex = 0;
DescriptorBindTraceSnapshot g_lastDescriptorBindSnapshot{};
std::atomic<uint64_t> g_descriptorBindTraceSequence{0};
std::atomic<int> g_descriptorBindSuspiciousWarnCount{0};
#endif

} // namespace

void SetDescriptorRecordingContext(vk::VkDescriptorManager *manager, rhi::SubmissionSerial submissionSerial) noexcept
{
    g_recordingDescriptorManager = manager;
    g_recordingSubmissionSerial = submissionSerial;
}

void ClearDescriptorRecordingContext() noexcept
{
    g_recordingDescriptorManager = nullptr;
    g_recordingSubmissionSerial = rhi::InvalidSubmissionSerial;
}

void PushDescriptorRecordingContext(vk::VkDescriptorManager *manager, rhi::SubmissionSerial submissionSerial) noexcept
{
    g_recordingContextStack.push_back({g_recordingDescriptorManager, g_recordingSubmissionSerial});
    SetDescriptorRecordingContext(manager, submissionSerial);
}

void PopDescriptorRecordingContext() noexcept
{
    if (g_recordingContextStack.empty()) {
        ClearDescriptorRecordingContext();
        return;
    }
    const auto previous = g_recordingContextStack.back();
    g_recordingContextStack.pop_back();
    g_recordingDescriptorManager = previous.manager;
    g_recordingSubmissionSerial = previous.serial;
}

rhi::SubmissionSerial GetDescriptorRecordingSubmissionSerial() noexcept
{
    return g_recordingSubmissionSerial;
}

#if INFERNUX_VULKAN_VALIDATION_LAYERS
void RecordDescriptorBind(const char *site, VkCommandBuffer cmdBuf, VkPipelineLayout layout, uint32_t firstSet,
                          uint32_t descriptorSetCount, const VkDescriptorSet *descriptorSets)
{
    DescriptorBindTraceSnapshot snapshot;
    snapshot.sequence = g_descriptorBindTraceSequence.fetch_add(1, std::memory_order_relaxed) + 1;
    snapshot.site = site;
    snapshot.commandBufferRaw = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(cmdBuf));
    snapshot.pipelineLayoutRaw = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(layout));
    snapshot.firstSet = firstSet;
    snapshot.descriptorSetCount = descriptorSetCount;

    const uint32_t copyCount =
        std::min<uint32_t>(descriptorSetCount, static_cast<uint32_t>(snapshot.descriptorSetRaws.size()));
    for (uint32_t i = 0; i < copyCount; ++i)
        snapshot.descriptorSetRaws[i] = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(descriptorSets[i]));

    {
        std::lock_guard<std::mutex> lock(g_descriptorBindTraceMutex);
        g_lastDescriptorBindSnapshot = snapshot;
        g_descriptorBindTraceHistory[g_descriptorBindTraceHistoryWriteIndex] = snapshot;
        g_descriptorBindTraceHistoryWriteIndex =
            (g_descriptorBindTraceHistoryWriteIndex + 1) % kDescriptorBindTraceHistorySize;
    }

    for (uint32_t i = 0; i < copyCount; ++i) {
        const uint64_t raw = snapshot.descriptorSetRaws[i];
        if (!IsSuspiciousDescriptorRaw(raw))
            continue;
        const int warnIndex = g_descriptorBindSuspiciousWarnCount.fetch_add(1, std::memory_order_relaxed);
        if (warnIndex < 48) {
            INXLOG_WARN("[VkBindTrace] suspicious descriptor raw=0x", raw, " site=", (site ? site : "<null>"),
                        " firstSet=", firstSet, " localIndex=", i, " count=", descriptorSetCount, " cmd=0x",
                        snapshot.commandBufferRaw, " layout=0x", snapshot.pipelineLayoutRaw);
        }
    }
}

DescriptorBindTraceSnapshot GetLastDescriptorBindSnapshot()
{
    std::lock_guard<std::mutex> lock(g_descriptorBindTraceMutex);
    return g_lastDescriptorBindSnapshot;
}

bool FindRecentDescriptorBindByRaw(uint64_t descriptorRaw, DescriptorBindTraceSnapshot &outMatch,
                                   uint32_t &outLocalIndex)
{
    if (descriptorRaw == 0ull)
        return false;
    std::lock_guard<std::mutex> lock(g_descriptorBindTraceMutex);
    for (size_t i = 0; i < kDescriptorBindTraceHistorySize; ++i) {
        const size_t index = (g_descriptorBindTraceHistoryWriteIndex + kDescriptorBindTraceHistorySize - 1 - i) %
                             kDescriptorBindTraceHistorySize;
        const auto &snapshot = g_descriptorBindTraceHistory[index];
        if (snapshot.sequence == 0)
            continue;
        const uint32_t count =
            std::min<uint32_t>(snapshot.descriptorSetCount, static_cast<uint32_t>(snapshot.descriptorSetRaws.size()));
        for (uint32_t local = 0; local < count; ++local) {
            if (snapshot.descriptorSetRaws[local] == descriptorRaw) {
                outMatch = snapshot;
                outLocalIndex = local;
                return true;
            }
        }
    }
    return false;
}
#endif

void CmdBindDescriptorSetsTracked(const char *site, VkCommandBuffer cmdBuf, VkPipelineBindPoint pipelineBindPoint,
                                  VkPipelineLayout layout, uint32_t firstSet, uint32_t descriptorSetCount,
                                  const VkDescriptorSet *descriptorSets, uint32_t dynamicOffsetCount,
                                  const uint32_t *dynamicOffsets)
{
    if (g_recordingDescriptorManager && descriptorSets) {
        for (uint32_t index = 0; index < descriptorSetCount; ++index)
            g_recordingDescriptorManager->MarkUsed(descriptorSets[index], g_recordingSubmissionSerial);
    }
    RecordDescriptorBind(site, cmdBuf, layout, firstSet, descriptorSetCount, descriptorSets);
    vkCmdBindDescriptorSets(cmdBuf, pipelineBindPoint, layout, firstSet, descriptorSetCount, descriptorSets,
                            dynamicOffsetCount, dynamicOffsets);
}

} // namespace infernux::vkdebug
