#include <function/renderer/ProfileConfig.h>

#if INFERNUX_FRAME_PROFILE

#include "GpuTimestampQueries.h"

#include "VkDeviceContext.h"

#include <algorithm>
#include <limits>

namespace infernux::vk
{

GpuTimestampQueries::~GpuTimestampQueries()
{
    Destroy();
}

bool GpuTimestampQueries::Initialize(const VkDeviceContext &context, uint32_t framesInFlight,
                                     uint32_t maxRegionsPerFrame)
{
    Destroy();

    m_device = context.GetDevice();
    if (m_device == VK_NULL_HANDLE || framesInFlight == 0) {
        return false;
    }

    const auto &properties = context.GetDeviceProperties();
    const auto &queueIndices = context.GetQueueIndices();
    if (!queueIndices.graphicsFamily.has_value()) {
        m_device = VK_NULL_HANDLE;
        return false;
    }

    uint32_t queueFamilyCount = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(context.GetPhysicalDevice(), &queueFamilyCount, nullptr);
    std::vector<VkQueueFamilyProperties> queueProperties(queueFamilyCount);
    vkGetPhysicalDeviceQueueFamilyProperties(context.GetPhysicalDevice(), &queueFamilyCount, queueProperties.data());

    const uint32_t graphicsFamily = queueIndices.graphicsFamily.value();
    if (graphicsFamily >= queueProperties.size() || queueProperties[graphicsFamily].timestampValidBits == 0) {
        m_device = VK_NULL_HANDLE;
        return false;
    }

    const uint32_t regionCapacity = std::clamp(maxRegionsPerFrame, 1u, rhi::kGpuTimestampMaxRegions);
    m_maxQueriesPerFrame = regionCapacity * 2;
    m_frames.resize(framesInFlight);
    m_queryScratch.resize(m_maxQueriesPerFrame);

    VkQueryPoolCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;
    createInfo.queryType = VK_QUERY_TYPE_TIMESTAMP;
    createInfo.queryCount = m_maxQueriesPerFrame;

    for (auto &frame : m_frames) {
        frame.regions.resize(regionCapacity);
        if (vkCreateQueryPool(m_device, &createInfo, nullptr, &frame.pool) != VK_SUCCESS) {
            Destroy();
            return false;
        }
    }

    m_capabilities.supported = true;
    m_capabilities.graphicsAndCompute = properties.limits.timestampComputeAndGraphics == VK_TRUE;
    m_capabilities.validBits = queueProperties[graphicsFamily].timestampValidBits;
    m_capabilities.nanosecondsPerTick = properties.limits.timestampPeriod;
    m_capabilities.maxRegionsPerFrame = regionCapacity;
    return true;
}

void GpuTimestampQueries::Destroy() noexcept
{
    if (m_device != VK_NULL_HANDLE) {
        for (auto &frame : m_frames) {
            if (frame.pool != VK_NULL_HANDLE) {
                vkDestroyQueryPool(m_device, frame.pool, nullptr);
                frame.pool = VK_NULL_HANDLE;
            }
        }
    }
    m_frames.clear();
    m_queryScratch.clear();
    m_capabilities = {};
    m_latestFrame.Reset();
    m_recordingFrame = std::numeric_limits<uint32_t>::max();
    m_maxQueriesPerFrame = 0;
    m_device = VK_NULL_HANDLE;
}

void GpuTimestampQueries::BeginFrame(VkCommandBuffer commandBuffer, uint32_t frameIndex)
{
    m_recordingFrame = std::numeric_limits<uint32_t>::max();
    if (!m_capabilities.supported || commandBuffer == VK_NULL_HANDLE || frameIndex >= m_frames.size()) {
        return;
    }

    auto &frame = m_frames[frameIndex];
    frame.queryCount = 0;
    frame.regionCount = 0;
    frame.serial = m_nextSerial++;
    frame.recorded = false;
    vkCmdResetQueryPool(commandBuffer, frame.pool, 0, m_maxQueriesPerFrame);
    m_recordingFrame = frameIndex;
}

rhi::TimestampRegionHandle GpuTimestampQueries::BeginRegion(VkCommandBuffer commandBuffer, std::string_view name,
                                                            VkPipelineStageFlagBits stage)
{
    auto *frame = CurrentRecordingFrame();
    if (!frame || commandBuffer == VK_NULL_HANDLE || frame->queryCount + 2 > m_maxQueriesPerFrame ||
        frame->regionCount >= frame->regions.size()) {
        return {};
    }

    const uint32_t regionIndex = frame->regionCount++;
    auto &region = frame->regions[regionIndex];
    region.name.fill('\0');
    const size_t copyCount = std::min(name.size(), region.name.size() - 1);
    std::copy_n(name.data(), copyCount, region.name.data());
    region.beginQuery = frame->queryCount++;
    region.endQuery = 0;
    region.ended = false;
    vkCmdWriteTimestamp(commandBuffer, stage, frame->pool, region.beginQuery);
    return {static_cast<uint16_t>(regionIndex)};
}

void GpuTimestampQueries::EndRegion(VkCommandBuffer commandBuffer, rhi::TimestampRegionHandle regionHandle,
                                    VkPipelineStageFlagBits stage)
{
    auto *frame = CurrentRecordingFrame();
    if (!frame || commandBuffer == VK_NULL_HANDLE || !regionHandle.IsValid() ||
        regionHandle.index >= frame->regionCount || frame->queryCount >= m_maxQueriesPerFrame) {
        return;
    }

    auto &region = frame->regions[regionHandle.index];
    if (region.ended) {
        return;
    }
    region.endQuery = frame->queryCount++;
    region.ended = true;
    vkCmdWriteTimestamp(commandBuffer, stage, frame->pool, region.endQuery);
}

void GpuTimestampQueries::FinishFrame(uint32_t frameIndex) noexcept
{
    if (frameIndex < m_frames.size() && m_recordingFrame == frameIndex) {
        m_frames[frameIndex].recorded = m_frames[frameIndex].queryCount > 0;
    }
    m_recordingFrame = std::numeric_limits<uint32_t>::max();
}

void GpuTimestampQueries::MarkSubmitted(uint32_t frameIndex) noexcept
{
    if (frameIndex < m_frames.size()) {
        m_frames[frameIndex].pending = m_frames[frameIndex].recorded;
    }
}

bool GpuTimestampQueries::CollectCompletedFrame(uint32_t frameIndex)
{
    if (!m_capabilities.supported || frameIndex >= m_frames.size()) {
        return false;
    }

    auto &frame = m_frames[frameIndex];
    if (!frame.pending || frame.queryCount == 0) {
        return false;
    }

    std::fill_n(m_queryScratch.begin(), frame.queryCount, QueryValue{});
    const VkResult result = vkGetQueryPoolResults(
        m_device, frame.pool, 0, frame.queryCount, sizeof(QueryValue) * frame.queryCount, m_queryScratch.data(),
        sizeof(QueryValue), VK_QUERY_RESULT_64_BIT | VK_QUERY_RESULT_WITH_AVAILABILITY_BIT);
    frame.pending = false;
    if (result != VK_SUCCESS) {
        return false;
    }

    m_latestFrame.Reset(frame.serial);
    const uint32_t regionCount = std::min(frame.regionCount, static_cast<uint32_t>(frame.regions.size()));
    for (uint32_t i = 0; i < regionCount && m_latestFrame.sampleCount < m_latestFrame.samples.size(); ++i) {
        const auto &region = frame.regions[i];
        if (!region.ended || region.beginQuery >= frame.queryCount || region.endQuery >= frame.queryCount) {
            continue;
        }
        const auto &begin = m_queryScratch[region.beginQuery];
        const auto &end = m_queryScratch[region.endQuery];
        if (begin.available == 0 || end.available == 0) {
            continue;
        }

        auto &sample = m_latestFrame.samples[m_latestFrame.sampleCount++];
        sample.name = region.name;
        sample.milliseconds =
            static_cast<double>(rhi::TimestampTickDelta(begin.value, end.value, m_capabilities.validBits)) *
            m_capabilities.nanosecondsPerTick / 1.0e6;
    }
    m_latestFrame.available = m_latestFrame.sampleCount > 0;
    return m_latestFrame.available;
}

GpuTimestampQueries::FrameState *GpuTimestampQueries::CurrentRecordingFrame() noexcept
{
    if (m_recordingFrame >= m_frames.size()) {
        return nullptr;
    }
    return &m_frames[m_recordingFrame];
}

} // namespace infernux::vk

#endif
