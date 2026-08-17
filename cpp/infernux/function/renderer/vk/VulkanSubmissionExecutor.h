#pragma once

#include <function/renderer/rhi/RenderSubmissionPlan.h>

#include <array>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux::vk
{

class VkDeviceContext;
class VulkanQueueManager;

/// Records and submits one backend-neutral submission plan on a Vulkan device.
/// Command pools are frame-slot and queue-role owned; queue ordering and GPU
/// completion are delegated to VulkanQueueManager.
class VulkanSubmissionExecutor
{
  public:
    using BatchRecorder = std::function<bool(uint32_t batchIndex, VkCommandBuffer commandBuffer)>;

    struct ExternalSync
    {
        VkSemaphore imageAvailable = VK_NULL_HANDLE;
        VkPipelineStageFlags imageAvailableStages = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
        VkSemaphore uploadTimeline = VK_NULL_HANDLE;
        uint64_t uploadTimelineValue = 0;
        VkSemaphore renderFinished = VK_NULL_HANDLE;
        VkFence completionFence = VK_NULL_HANDLE;
        VkSemaphore previousFrameTimeline = VK_NULL_HANDLE;
        uint64_t previousFrameTimelineValue = 0;
        VkPipelineStageFlags previousFrameStages = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
        // Generation-prime compute can reuse resident resources from the
        // previous graph. In that case the dependency must gate the first
        // submitted batch, rather than only the first Graphics batch.
        bool previousFrameWaitAtFirstBatch = false;
        rhi::SubmissionSerial completionEpoch = rhi::InvalidSubmissionSerial;
    };

    struct ExecuteResult
    {
        VkResult result = VK_ERROR_INITIALIZATION_FAILED;
        rhi::SubmissionTicket completionTicket{};
        VkSemaphore completionTimeline = VK_NULL_HANDLE;
        uint64_t completionTimelineValue = 0;
        bool submittedAny = false;

        [[nodiscard]] bool Succeeded() const noexcept
        {
            return result == VK_SUCCESS && completionTicket.IsValid();
        }
    };

    VulkanSubmissionExecutor() = default;
    ~VulkanSubmissionExecutor();
    VulkanSubmissionExecutor(const VulkanSubmissionExecutor &) = delete;
    VulkanSubmissionExecutor &operator=(const VulkanSubmissionExecutor &) = delete;

    bool Initialize(VkDeviceContext &device, VulkanQueueManager &queues, uint32_t frameSlotCount);
    void Destroy() noexcept;

    [[nodiscard]] ExecuteResult Execute(uint32_t frameSlot, const rhi::SubmissionPlan &plan,
                                        const BatchRecorder &recorder, const ExternalSync &sync);

    /// Publish completion for every queue ticket that contributed to a frame
    /// after the frame completion fence has signaled.
    void CompleteFrame(uint32_t frameSlot) noexcept;

    /// Emit timeline counter evidence for a frame that has exceeded its fence
    /// budget. This is intentionally read-only and safe to call while waiting.
    void LogFrameWaitDiagnostics(uint32_t frameSlot, uint32_t elapsedMilliseconds) const noexcept;

    [[nodiscard]] VkSemaphore GetDependencyTimeline(rhi::QueueRole role) const noexcept;

  private:
    struct RolePool
    {
        VkCommandPool pool = VK_NULL_HANDLE;
        std::vector<VkCommandBuffer> buffers;
        uint32_t cursor = 0;
    };

    struct FrameState
    {
        std::array<RolePool, static_cast<size_t>(rhi::QueueRole::Count)> roles{};
        std::vector<rhi::SubmissionTicket> submittedTickets;
    };

    struct LaneTimeline
    {
        VkSemaphore semaphore = VK_NULL_HANDLE;
        uint64_t nextValue = 1;
    };

    struct FrameDiagnostic
    {
        std::vector<rhi::QueueRole> batchRoles;
        std::vector<uint32_t> batchLanes;
        std::vector<uint64_t> batchSignalValues;
        std::vector<uint32_t> batchQueuePredecessors;
        std::vector<std::vector<rhi::SubmissionBatchDependency>> batchWaits;
        std::vector<std::string> batchNames;
        VkSemaphore previousTimeline = VK_NULL_HANDLE;
        uint64_t previousTimelineValue = 0;
        VkSemaphore uploadTimeline = VK_NULL_HANDLE;
        uint64_t uploadTimelineValue = 0;
        VkSemaphore completionTimeline = VK_NULL_HANDLE;
        uint64_t completionTimelineValue = 0;
    };

    [[nodiscard]] bool CreatePools(FrameState &frame);
    [[nodiscard]] VkCommandBuffer AcquireCommandBuffer(FrameState &frame, rhi::QueueRole role);
    [[nodiscard]] static VkPipelineStageFlags ToVkStages(rhi::PipelineStage stages) noexcept;
    void CancelReservations(const std::vector<rhi::SubmissionTicket> &tickets, size_t first) noexcept;

    VkDeviceContext *m_deviceContext = nullptr;
    VulkanQueueManager *m_queues = nullptr;
    VkDevice m_device = VK_NULL_HANDLE;
    std::vector<LaneTimeline> m_laneTimelines;
    std::vector<FrameState> m_frames;
    std::vector<FrameDiagnostic> m_frameDiagnostics;
};

} // namespace infernux::vk
