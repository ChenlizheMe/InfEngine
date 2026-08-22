#include "VulkanQueueManager.h"

#include "VkDeviceContext.h"

#include <SDL3/SDL.h>
#include <core/error/InxError.h>

#include <algorithm>

namespace infernux::vk
{

bool VulkanQueueManager::Initialize(const VkDeviceContext &context, uint32_t graphicsFrameSlots)
{
    Destroy();
    if (!context.IsValid() || context.GetDeviceId() == rhi::InvalidDeviceId)
        return false;

    std::lock_guard lock(m_mutex);
    m_device = context.GetDevice();
    m_deviceId = context.GetDeviceId();
    const auto &indices = context.GetQueueIndices();
    const uint32_t graphicsFamily = indices.graphicsFamily.value_or(0);
    const uint32_t computeFamily = indices.computeFamily.value_or(graphicsFamily);
    const uint32_t presentFamily = indices.presentFamily.value_or(graphicsFamily);
    const uint32_t transferFamily = indices.transferFamily.value_or(graphicsFamily);

    m_queues[RoleIndex(rhi::QueueRole::Graphics)].queue = context.GetGraphicsQueue();
    m_queues[RoleIndex(rhi::QueueRole::Graphics)].family = graphicsFamily;
    m_queues[RoleIndex(rhi::QueueRole::Compute)].queue = context.GetComputeQueue();
    m_queues[RoleIndex(rhi::QueueRole::Compute)].family = computeFamily;
    m_queues[RoleIndex(rhi::QueueRole::Transfer)].queue = context.GetTransferQueue();
    m_queues[RoleIndex(rhi::QueueRole::Transfer)].family = transferFamily;
    m_queues[RoleIndex(rhi::QueueRole::Present)].queue = context.GetPresentQueue();
    m_queues[RoleIndex(rhi::QueueRole::Present)].family = presentFamily;

    for (size_t role = 0; role < m_roleLanes.size(); ++role) {
        m_roleLanes[role] = role;
        const VkQueue queue = m_queues[role].queue;
        if (queue == VK_NULL_HANDLE)
            continue;
        for (size_t candidate = 0; candidate < role; ++candidate) {
            if (m_queues[candidate].queue == queue) {
                m_roleLanes[role] = m_roleLanes[candidate];
                break;
            }
        }
    }
    const uint32_t frameSlotCount = (std::max)(graphicsFrameSlots, 1u);
    m_graphicsFrameSlots.resize(frameSlotCount);
    m_graphicsFrameFences.resize(frameSlotCount, VK_NULL_HANDLE);
    VkFenceCreateInfo fenceInfo{};
    fenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    fenceInfo.flags = VK_FENCE_CREATE_SIGNALED_BIT;
    for (uint32_t index = 0; index < frameSlotCount; ++index) {
        if (vkCreateFence(m_device, &fenceInfo, nullptr, &m_graphicsFrameFences[index]) != VK_SUCCESS) {
            for (VkFence fence : m_graphicsFrameFences) {
                if (fence != VK_NULL_HANDLE)
                    vkDestroyFence(m_device, fence, nullptr);
            }
            m_graphicsFrameFences.clear();
            m_graphicsFrameSlots.clear();
            m_device = VK_NULL_HANDLE;
            m_deviceId = rhi::InvalidDeviceId;
            return false;
        }
    }
    return true;
}

void VulkanQueueManager::Destroy() noexcept
{
    {
        std::lock_guard lock(m_mutex);
        if (m_device != VK_NULL_HANDLE) {
            for (VkFence fence : m_graphicsFrameFences) {
                if (fence != VK_NULL_HANDLE)
                    vkDestroyFence(m_device, fence, nullptr);
            }
        }
        m_graphicsFrameFences.clear();
        m_deviceId = rhi::InvalidDeviceId;
        m_device = VK_NULL_HANDLE;
        m_queues = {};
        m_roleLanes = {};
        m_graphicsFrameSlots.clear();
        m_nextCompletionEpoch = 1;
        m_lastReservedCompletionEpoch = 0;
        m_completedCompletionEpoch = 0;
        m_completedOutOfOrderEpochs.clear();
    }
    m_submitCv.notify_all();
}

rhi::SubmissionTicket VulkanQueueManager::Reserve(rhi::QueueRole role)
{
    std::lock_guard lock(m_mutex);
    if (m_deviceId == rhi::InvalidDeviceId || role == rhi::QueueRole::Count)
        return {};
    auto &queue = m_queues[LaneIndex(role)];
    if (queue.queue == VK_NULL_HANDLE)
        return {};
    const auto serial = queue.nextSerial++;
    queue.lastReserved = serial;
    return {m_deviceId, role, serial};
}

VkResult VulkanQueueManager::SubmitReserved(rhi::SubmissionTicket ticket, const VkSubmitInfo &submitInfo,
                                            VkFence fence) noexcept
{
    std::unique_lock lock(m_mutex);
    if (!ticket.IsValid() || ticket.device != m_deviceId || ticket.queue == rhi::QueueRole::Count)
        return VK_ERROR_INITIALIZATION_FAILED;
    auto &queue = m_queues[LaneIndex(ticket.queue)];
    if (queue.queue == VK_NULL_HANDLE || ticket.serial > queue.lastReserved ||
        ticket.serial < queue.nextSubmissionSerial)
        return VK_ERROR_INITIALIZATION_FAILED;

    m_submitCv.wait(lock, [&] { return m_device == VK_NULL_HANDLE || ticket.serial == queue.nextSubmissionSerial; });
    if (m_device == VK_NULL_HANDLE)
        return VK_ERROR_DEVICE_LOST;

    const VkResult result = vkQueueSubmit(queue.queue, 1, &submitInfo, fence);
    ++queue.nextSubmissionSerial;
    if (result == VK_SUCCESS)
        queue.lastSubmitted = ticket.serial;
    lock.unlock();
    m_submitCv.notify_all();
    return result;
}

VkResult VulkanQueueManager::Present(const VkPresentInfoKHR &presentInfo) noexcept
{
    std::lock_guard lock(m_mutex);
    const auto &queue = m_queues[LaneIndex(rhi::QueueRole::Present)];
    if (m_device == VK_NULL_HANDLE || queue.queue == VK_NULL_HANDLE)
        return VK_ERROR_INITIALIZATION_FAILED;
    return vkQueuePresentKHR(queue.queue, &presentInfo);
}

VkResult VulkanQueueManager::WaitIdleForPresentation() noexcept
{
    return WaitIdleQueues(
        {rhi::QueueRole::Graphics, rhi::QueueRole::Present, rhi::QueueRole::Count, rhi::QueueRole::Count}, 2);
}

VkResult VulkanQueueManager::WaitIdleForGraphics() noexcept
{
    return WaitIdleQueues(
        {rhi::QueueRole::Graphics, rhi::QueueRole::Count, rhi::QueueRole::Count, rhi::QueueRole::Count}, 1);
}

VkResult VulkanQueueManager::WaitIdleForAllQueues() noexcept
{
    return WaitIdleQueues(
        {rhi::QueueRole::Graphics, rhi::QueueRole::Compute, rhi::QueueRole::Transfer, rhi::QueueRole::Present}, 4);
}

VkResult VulkanQueueManager::WaitIdleQueues(const std::array<rhi::QueueRole, 4> &roles, size_t roleCount) noexcept
{
    std::unique_lock lock(m_mutex);
    if (m_device == VK_NULL_HANDLE)
        return VK_ERROR_DEVICE_LOST;

    // A reservation may be recording outside the queue lock. Let it publish or
    // cancel before draining, otherwise it could submit work after WaitIdle and
    // still reference resources the caller is about to replace.
    m_submitCv.wait(lock, [&] {
        if (m_device == VK_NULL_HANDLE)
            return true;
        for (size_t index = 0; index < roleCount; ++index) {
            const auto role = roles[index];
            if (role == rhi::QueueRole::Count)
                return true;
            const auto &state = m_queues[LaneIndex(role)];
            if (state.nextSubmissionSerial <= state.lastReserved)
                return false;
        }
        return true;
    });
    if (m_device == VK_NULL_HANDLE)
        return VK_ERROR_DEVICE_LOST;

    std::array<VkQueue, 4> drained{};
    size_t drainedCount = 0;
    for (size_t index = 0; index < roleCount; ++index) {
        const auto role = roles[index];
        if (role == rhi::QueueRole::Count)
            return VK_ERROR_INITIALIZATION_FAILED;
        const VkQueue queue = m_queues[LaneIndex(role)].queue;
        if (queue == VK_NULL_HANDLE)
            return VK_ERROR_INITIALIZATION_FAILED;
        if (std::find(drained.begin(), drained.begin() + static_cast<std::ptrdiff_t>(drainedCount), queue) !=
            drained.begin() + static_cast<std::ptrdiff_t>(drainedCount)) {
            continue;
        }

        const VkResult result = vkQueueWaitIdle(queue);
        if (result != VK_SUCCESS)
            return result;
        drained[drainedCount++] = queue;
    }

    for (auto &state : m_queues) {
        if (std::find(drained.begin(), drained.begin() + static_cast<std::ptrdiff_t>(drainedCount), state.queue) !=
            drained.begin() + static_cast<std::ptrdiff_t>(drainedCount)) {
            state.completed = (std::max)(state.completed, state.lastSubmitted);
        }
    }
    return VK_SUCCESS;
}

bool VulkanQueueManager::CancelReservation(rhi::SubmissionTicket ticket) noexcept
{
    std::unique_lock lock(m_mutex);
    if (!ticket.IsValid() || ticket.device != m_deviceId || ticket.queue == rhi::QueueRole::Count)
        return false;
    auto &queue = m_queues[LaneIndex(ticket.queue)];
    if (ticket.serial > queue.lastReserved || ticket.serial < queue.nextSubmissionSerial)
        return false;
    m_submitCv.wait(lock, [&] { return m_device == VK_NULL_HANDLE || ticket.serial == queue.nextSubmissionSerial; });
    if (m_device == VK_NULL_HANDLE)
        return false;
    ++queue.nextSubmissionSerial;
    lock.unlock();
    m_submitCv.notify_all();
    return true;
}

bool VulkanQueueManager::AssociateFrameSlot(uint32_t frameSlot, rhi::SubmissionTicket ticket) noexcept
{
    std::lock_guard lock(m_mutex);
    if (!ticket.IsValid() || ticket.device != m_deviceId || ticket.queue == rhi::QueueRole::Count ||
        frameSlot >= m_graphicsFrameSlots.size() ||
        m_graphicsFrameSlots[frameSlot].completionEpoch == rhi::InvalidSubmissionSerial)
        return false;
    m_graphicsFrameSlots[frameSlot].terminalTicket = ticket;
    return true;
}

rhi::SubmissionTicket VulkanQueueManager::CompleteFrameSlot(uint32_t frameSlot) noexcept
{
    std::lock_guard lock(m_mutex);
    if (frameSlot >= m_graphicsFrameSlots.size())
        return {};
    auto &slot = m_graphicsFrameSlots[frameSlot];
    const auto ticket = slot.terminalTicket;
    if (ticket.IsValid()) {
        auto &queue = m_queues[LaneIndex(ticket.queue)];
        queue.completed = (std::max)(queue.completed, ticket.serial);
    }
    CompleteCompletionEpochLocked(slot.completionEpoch);
    slot = {};
    return ticket;
}

void VulkanQueueManager::MarkCompleted(rhi::SubmissionTicket ticket) noexcept
{
    std::lock_guard lock(m_mutex);
    if (!ticket.IsValid() || ticket.device != m_deviceId || ticket.queue == rhi::QueueRole::Count)
        return;
    auto &queue = m_queues[LaneIndex(ticket.queue)];
    queue.completed = (std::max)(queue.completed, ticket.serial);
}

bool VulkanQueueManager::WaitForGraphicsFrameSlot(uint32_t frameSlot, const FrameWaitDiagnostic &diagnostic) noexcept
{
    VkDevice device = VK_NULL_HANDLE;
    VkFence fence = VK_NULL_HANDLE;
    {
        std::lock_guard lock(m_mutex);
        if (frameSlot >= m_graphicsFrameFences.size())
            return false;
        device = m_device;
        fence = m_graphicsFrameFences[frameSlot];
    }
    if (device == VK_NULL_HANDLE || fence == VK_NULL_HANDLE)
        return false;

    constexpr uint64_t kPollTimeoutNs = 50'000'000;
    constexpr uint32_t kInitialDiagnosticPolls = 20;
    constexpr uint32_t kRepeatedDiagnosticPolls = 100;
    uint32_t timeoutPolls = 0;
    while (true) {
        const VkResult result = vkWaitForFences(device, 1, &fence, VK_TRUE, kPollTimeoutNs);
        if (result == VK_SUCCESS)
            return true;
        if (result != VK_TIMEOUT) {
            INXLOG_ERROR("VulkanQueueManager: graphics frame fence wait failed with VkResult ",
                         static_cast<int>(result));
            return false;
        }
        ++timeoutPolls;
        if (timeoutPolls == kInitialDiagnosticPolls ||
            (timeoutPolls > kInitialDiagnosticPolls &&
             (timeoutPolls - kInitialDiagnosticPolls) % kRepeatedDiagnosticPolls == 0)) {
            FrameSlotState state{};
            {
                std::lock_guard lock(m_mutex);
                if (frameSlot < m_graphicsFrameSlots.size())
                    state = m_graphicsFrameSlots[frameSlot];
            }
            INXLOG_ERROR("VulkanQueueManager: graphics frame slot ", frameSlot, " has not completed after ",
                         timeoutPolls * 50, " ms; completion_epoch=", state.completionEpoch,
                         " terminal_role=", static_cast<uint32_t>(state.terminalTicket.queue),
                         " terminal_serial=", state.terminalTicket.serial);
            if (diagnostic) {
                try {
                    diagnostic(timeoutPolls * 50);
                } catch (...) {
                    INXLOG_ERROR("VulkanQueueManager: frame wait diagnostic callback failed");
                }
            }
        }
        SDL_PumpEvents();
    }
}

bool VulkanQueueManager::ResetGraphicsFrameFence(uint32_t frameSlot) noexcept
{
    std::lock_guard lock(m_mutex);
    if (m_device == VK_NULL_HANDLE || frameSlot >= m_graphicsFrameFences.size() ||
        m_graphicsFrameFences[frameSlot] == VK_NULL_HANDLE ||
        m_graphicsFrameSlots[frameSlot].completionEpoch != rhi::InvalidSubmissionSerial)
        return false;
    if (vkResetFences(m_device, 1, &m_graphicsFrameFences[frameSlot]) != VK_SUCCESS)
        return false;
    m_graphicsFrameSlots[frameSlot].completionEpoch = ReserveCompletionEpochLocked();
    return true;
}

bool VulkanQueueManager::AbandonGraphicsFrameSlot(uint32_t frameSlot) noexcept
{
    std::lock_guard lock(m_mutex);
    if (m_device == VK_NULL_HANDLE || frameSlot >= m_graphicsFrameFences.size())
        return false;

    auto &slot = m_graphicsFrameSlots[frameSlot];
    CompleteCompletionEpochLocked(slot.completionEpoch);
    slot = {};

    VkFenceCreateInfo fenceInfo{};
    fenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    fenceInfo.flags = VK_FENCE_CREATE_SIGNALED_BIT;
    VkFence replacement = VK_NULL_HANDLE;
    if (vkCreateFence(m_device, &fenceInfo, nullptr, &replacement) != VK_SUCCESS)
        return false;

    if (m_graphicsFrameFences[frameSlot] != VK_NULL_HANDLE)
        vkDestroyFence(m_device, m_graphicsFrameFences[frameSlot], nullptr);
    m_graphicsFrameFences[frameSlot] = replacement;
    return true;
}

VkFence VulkanQueueManager::GetGraphicsFrameFence(uint32_t frameSlot) const noexcept
{
    std::lock_guard lock(m_mutex);
    return frameSlot < m_graphicsFrameFences.size() ? m_graphicsFrameFences[frameSlot] : VK_NULL_HANDLE;
}

rhi::SubmissionSerial VulkanQueueManager::GetFrameCompletionEpoch(uint32_t frameSlot) const noexcept
{
    std::lock_guard lock(m_mutex);
    return frameSlot < m_graphicsFrameSlots.size() ? m_graphicsFrameSlots[frameSlot].completionEpoch
                                                   : rhi::InvalidSubmissionSerial;
}

rhi::SubmissionSerial VulkanQueueManager::GetLastReservedCompletionEpoch() const noexcept
{
    std::lock_guard lock(m_mutex);
    return m_lastReservedCompletionEpoch;
}

rhi::SubmissionSerial VulkanQueueManager::GetCompletedCompletionEpoch() const noexcept
{
    std::lock_guard lock(m_mutex);
    return m_completedCompletionEpoch;
}

rhi::SubmissionSerial VulkanQueueManager::ReserveCompletionEpoch() noexcept
{
    std::lock_guard lock(m_mutex);
    return m_deviceId != rhi::InvalidDeviceId ? ReserveCompletionEpochLocked() : rhi::InvalidSubmissionSerial;
}

void VulkanQueueManager::CompleteCompletionEpoch(rhi::SubmissionSerial epoch) noexcept
{
    std::lock_guard lock(m_mutex);
    CompleteCompletionEpochLocked(epoch);
}

rhi::SubmissionSerial VulkanQueueManager::ReserveCompletionEpochLocked() noexcept
{
    const auto epoch = m_nextCompletionEpoch++;
    m_lastReservedCompletionEpoch = epoch;
    return epoch;
}

void VulkanQueueManager::CompleteCompletionEpochLocked(rhi::SubmissionSerial epoch) noexcept
{
    if (epoch == rhi::InvalidSubmissionSerial || epoch <= m_completedCompletionEpoch)
        return;
    m_completedOutOfOrderEpochs.insert(epoch);
    while (!m_completedOutOfOrderEpochs.empty()) {
        const auto next = m_completedOutOfOrderEpochs.begin();
        if (*next != m_completedCompletionEpoch + 1)
            break;
        m_completedCompletionEpoch = *next;
        m_completedOutOfOrderEpochs.erase(next);
    }
}

VulkanQueueManager::QueueSnapshot VulkanQueueManager::GetSnapshot(rhi::QueueRole role) const noexcept
{
    std::lock_guard lock(m_mutex);
    if (role == rhi::QueueRole::Count)
        return {};
    const auto &queue = m_queues[LaneIndex(role)];
    return {queue.queue,        queue.family,        static_cast<uint32_t>(LaneIndex(role)),
            queue.lastReserved, queue.lastSubmitted, queue.completed};
}

rhi::SubmissionSerial VulkanQueueManager::GetCompletedSerial(rhi::QueueRole role) const noexcept
{
    return GetSnapshot(role).completed;
}

} // namespace infernux::vk
