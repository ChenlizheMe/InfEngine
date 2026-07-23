#include "DescriptorBindTrace.h"

#if INFERNUX_VULKAN_VALIDATION_LAYERS

#include <algorithm>
#include <atomic>
#include <mutex>

#include <core/error/InxError.h>

namespace infernux::vkdebug
{
namespace
{

// A single editor frame can contain thousands of material, shadow, fullscreen,
// and particle binds. Keep a full-frame diagnostic window so validation errors
// reported at queue submission can still be matched to the recording site.
constexpr size_t kDescriptorBindTraceHistorySize = 8192;

std::mutex g_descriptorBindTraceMutex;
std::array<DescriptorBindTraceSnapshot, kDescriptorBindTraceHistorySize> g_descriptorBindTraceHistory{};
size_t g_descriptorBindTraceHistoryWriteIndex = 0;
DescriptorBindTraceSnapshot g_lastDescriptorBindSnapshot{};
std::atomic<uint64_t> g_descriptorBindTraceSequence{0};
std::atomic<int> g_descriptorBindSuspiciousWarnCount{0};

} // namespace

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
    for (uint32_t i = 0; i < copyCount; ++i) {
        snapshot.descriptorSetRaws[i] = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(descriptorSets[i]));
    }

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
        const size_t idx = (g_descriptorBindTraceHistoryWriteIndex + kDescriptorBindTraceHistorySize - 1 - i) %
                           kDescriptorBindTraceHistorySize;
        const auto &snapshot = g_descriptorBindTraceHistory[idx];
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

void CmdBindDescriptorSetsTracked(const char *site, VkCommandBuffer cmdBuf, VkPipelineBindPoint pipelineBindPoint,
                                  VkPipelineLayout layout, uint32_t firstSet, uint32_t descriptorSetCount,
                                  const VkDescriptorSet *descriptorSets, uint32_t dynamicOffsetCount,
                                  const uint32_t *dynamicOffsets)
{
    RecordDescriptorBind(site, cmdBuf, layout, firstSet, descriptorSetCount, descriptorSets);
    vkCmdBindDescriptorSets(cmdBuf, pipelineBindPoint, layout, firstSet, descriptorSetCount, descriptorSets,
                            dynamicOffsetCount, dynamicOffsets);
}

} // namespace infernux::vkdebug

#endif
