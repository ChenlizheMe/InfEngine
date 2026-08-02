#pragma once

#include <function/renderer/ProfileConfig.h>

#if INFERNUX_FRAME_PROFILE

#include <function/renderer/rhi/RhiQuery.h>

#include <array>
#include <cstdint>
#include <limits>
#include <string_view>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux::vk
{

class VkDeviceContext;

class GpuTimestampQueries
{
  public:
    GpuTimestampQueries() = default;
    ~GpuTimestampQueries();

    GpuTimestampQueries(const GpuTimestampQueries &) = delete;
    GpuTimestampQueries &operator=(const GpuTimestampQueries &) = delete;
    GpuTimestampQueries(GpuTimestampQueries &&) = delete;
    GpuTimestampQueries &operator=(GpuTimestampQueries &&) = delete;

    bool Initialize(const VkDeviceContext &context, uint32_t framesInFlight,
                    uint32_t maxRegionsPerFrame = rhi::kGpuTimestampMaxRegions);
    void Destroy() noexcept;

    void BeginFrame(VkCommandBuffer commandBuffer, uint32_t frameIndex);
    [[nodiscard]] rhi::TimestampRegionHandle BeginRegion(VkCommandBuffer commandBuffer, std::string_view name,
                                                         VkPipelineStageFlagBits stage);
    void EndRegion(VkCommandBuffer commandBuffer, rhi::TimestampRegionHandle region, VkPipelineStageFlagBits stage);
    void FinishFrame(uint32_t frameIndex) noexcept;
    void MarkSubmitted(uint32_t frameIndex) noexcept;
    bool CollectCompletedFrame(uint32_t frameIndex);

    [[nodiscard]] const rhi::TimestampQueryCapabilities &Capabilities() const noexcept
    {
        return m_capabilities;
    }

    [[nodiscard]] const rhi::GpuTimestampFrame &LatestFrame() const noexcept
    {
        return m_latestFrame;
    }

  private:
    struct RegionRecord
    {
        std::array<char, rhi::kGpuTimestampNameCapacity> name{};
        uint32_t beginQuery = 0;
        uint32_t endQuery = 0;
        bool ended = false;
    };

    struct FrameState
    {
        VkQueryPool pool = VK_NULL_HANDLE;
        std::vector<RegionRecord> regions;
        uint32_t queryCount = 0;
        uint32_t regionCount = 0;
        uint64_t serial = 0;
        bool recorded = false;
        bool pending = false;
    };

    struct QueryValue
    {
        uint64_t value = 0;
        uint64_t available = 0;
    };

    [[nodiscard]] FrameState *CurrentRecordingFrame() noexcept;
    VkDevice m_device = VK_NULL_HANDLE;
    std::vector<FrameState> m_frames;
    std::vector<QueryValue> m_queryScratch;
    rhi::TimestampQueryCapabilities m_capabilities;
    rhi::GpuTimestampFrame m_latestFrame;
    uint32_t m_recordingFrame = std::numeric_limits<uint32_t>::max();
    uint32_t m_maxQueriesPerFrame = 0;
    uint64_t m_nextSerial = 1;
};

} // namespace infernux::vk

#endif
