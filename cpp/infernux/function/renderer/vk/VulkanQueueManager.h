#pragma once

#include <function/renderer/rhi/RhiSubmission.h>

#include <array>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <set>
#include <vector>
#include <vulkan/vulkan.h>

namespace infernux::vk
{

class VkDeviceContext;

/// Owns queue identity and GPU completion accounting for one Vulkan device.
/// The first implementation uses the existing per-frame graphics fences as
/// completion sources; callers no longer infer resource safety from frame age.
class VulkanQueueManager
{
  public:
    struct QueueSnapshot
    {
        VkQueue queue = VK_NULL_HANDLE;
        uint32_t family = 0;
        uint32_t nativeLane = UINT32_MAX;
        rhi::SubmissionSerial lastReserved = 0;
        rhi::SubmissionSerial lastSubmitted = 0;
        rhi::SubmissionSerial completed = 0;
    };

    VulkanQueueManager() = default;
    VulkanQueueManager(const VulkanQueueManager &) = delete;
    VulkanQueueManager &operator=(const VulkanQueueManager &) = delete;

    bool Initialize(const VkDeviceContext &context, uint32_t graphicsFrameSlots);
    void Destroy() noexcept;

    [[nodiscard]] rhi::SubmissionTicket Reserve(rhi::QueueRole role);
    /// Serializes host access to VkQueue and preserves reservation order.
    /// A failed Vulkan submit consumes the reservation without advancing GPU completion.
    [[nodiscard]] VkResult SubmitReserved(rhi::SubmissionTicket ticket, const VkSubmitInfo &submitInfo,
                                          VkFence fence) noexcept;
    /// Consumes a reservation that will never reach Vulkan, preserving the
    /// submission order without claiming that any GPU work completed.
    bool CancelReservation(rhi::SubmissionTicket ticket) noexcept;
    /// Serializes presentation with every submit, including aliased graphics/present queues.
    [[nodiscard]] VkResult Present(const VkPresentInfoKHR &presentInfo) noexcept;
    /// Drain only queues that can reference presentation resources. This is
    /// used for swapchain replacement and deliberately does not idle the device.
    [[nodiscard]] VkResult WaitIdleForPresentation() noexcept;
    /// Drain graphics submissions for a destructive graphics-resource
    /// reconfiguration such as changing MSAA attachments and pipelines.
    [[nodiscard]] VkResult WaitIdleForGraphics() noexcept;
    /// Error-recovery path: drain every distinct queue owned by this device.
    [[nodiscard]] VkResult WaitIdleForAllQueues() noexcept;
    bool AssociateFrameSlot(uint32_t frameSlot, rhi::SubmissionTicket ticket) noexcept;
    [[nodiscard]] rhi::SubmissionTicket CompleteFrameSlot(uint32_t frameSlot) noexcept;
    void MarkCompleted(rhi::SubmissionTicket ticket) noexcept;
    [[nodiscard]] bool WaitForGraphicsFrameSlot(uint32_t frameSlot) noexcept;
    [[nodiscard]] bool ResetGraphicsFrameFence(uint32_t frameSlot) noexcept;
    /// Restores a reusable, signaled fence after a submission failed before
    /// Vulkan took ownership of the frame slot.
    [[nodiscard]] bool AbandonGraphicsFrameSlot(uint32_t frameSlot) noexcept;
    [[nodiscard]] VkFence GetGraphicsFrameFence(uint32_t frameSlot) const noexcept;
    [[nodiscard]] rhi::SubmissionSerial GetFrameCompletionEpoch(uint32_t frameSlot) const noexcept;

    /// Queue serials are local to one native lane. Resource lifetime instead
    /// uses this device-wide epoch, completed only after a terminal frame fence
    /// proves that every queue contributing to the frame has finished.
    [[nodiscard]] rhi::SubmissionSerial GetLastReservedCompletionEpoch() const noexcept;
    [[nodiscard]] rhi::SubmissionSerial GetCompletedCompletionEpoch() const noexcept;
    [[nodiscard]] rhi::SubmissionSerial ReserveCompletionEpoch() noexcept;
    void CompleteCompletionEpoch(rhi::SubmissionSerial epoch) noexcept;

    [[nodiscard]] QueueSnapshot GetSnapshot(rhi::QueueRole role) const noexcept;
    [[nodiscard]] rhi::SubmissionSerial GetCompletedSerial(rhi::QueueRole role) const noexcept;
    [[nodiscard]] rhi::DeviceId GetDeviceId() const noexcept
    {
        return m_deviceId;
    }

  private:
    struct QueueState
    {
        VkQueue queue = VK_NULL_HANDLE;
        uint32_t family = 0;
        rhi::SubmissionSerial nextSerial = 1;
        rhi::SubmissionSerial lastReserved = 0;
        rhi::SubmissionSerial lastSubmitted = 0;
        rhi::SubmissionSerial completed = 0;
        rhi::SubmissionSerial nextSubmissionSerial = 1;
    };

    struct FrameSlotState
    {
        rhi::SubmissionTicket terminalTicket{};
        rhi::SubmissionSerial completionEpoch = rhi::InvalidSubmissionSerial;
    };

    [[nodiscard]] static constexpr size_t RoleIndex(rhi::QueueRole role) noexcept
    {
        return static_cast<size_t>(role);
    }

    [[nodiscard]] size_t LaneIndex(rhi::QueueRole role) const noexcept
    {
        return m_roleLanes[RoleIndex(role)];
    }

    [[nodiscard]] VkResult WaitIdleQueues(const std::array<rhi::QueueRole, 4> &roles, size_t roleCount) noexcept;
    [[nodiscard]] rhi::SubmissionSerial ReserveCompletionEpochLocked() noexcept;
    void CompleteCompletionEpochLocked(rhi::SubmissionSerial epoch) noexcept;

    mutable std::mutex m_mutex;
    std::condition_variable m_submitCv;
    VkDevice m_device = VK_NULL_HANDLE;
    rhi::DeviceId m_deviceId = rhi::InvalidDeviceId;
    std::array<QueueState, static_cast<size_t>(rhi::QueueRole::Count)> m_queues{};
    std::array<size_t, static_cast<size_t>(rhi::QueueRole::Count)> m_roleLanes{};
    std::vector<FrameSlotState> m_graphicsFrameSlots;
    std::vector<VkFence> m_graphicsFrameFences;
    rhi::SubmissionSerial m_nextCompletionEpoch = 1;
    rhi::SubmissionSerial m_lastReservedCompletionEpoch = 0;
    rhi::SubmissionSerial m_completedCompletionEpoch = 0;
    std::set<rhi::SubmissionSerial> m_completedOutOfOrderEpochs;
};

} // namespace infernux::vk
