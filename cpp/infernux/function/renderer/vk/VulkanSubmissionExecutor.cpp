#include "VulkanSubmissionExecutor.h"

#include "DescriptorBindTrace.h"
#include "VkDeviceContext.h"
#include "VulkanQueueManager.h"
#include "VulkanRhiDevice.h"

#include <core/log/InxLog.h>

#include <algorithm>
#include <limits>

namespace infernux::vk
{

namespace
{

constexpr size_t RoleIndex(rhi::QueueRole role) noexcept
{
    return static_cast<size_t>(role);
}

} // namespace

VulkanSubmissionExecutor::~VulkanSubmissionExecutor()
{
    Destroy();
}

bool VulkanSubmissionExecutor::Initialize(VkDeviceContext &device, VulkanQueueManager &queues, uint32_t frameSlotCount)
{
    Destroy();
    if (!device.IsValid() || queues.GetDeviceId() != device.GetDeviceId())
        return false;

    m_deviceContext = &device;
    m_queues = &queues;
    m_device = device.GetDevice();
    m_frames.resize((std::max)(frameSlotCount, 1u));
    m_frameDiagnostics.resize(m_frames.size());
    for (FrameState &frame : m_frames) {
        if (!CreatePools(frame)) {
            Destroy();
            return false;
        }
    }

    if (device.IsTimelineSemaphoreEnabled()) {
        uint32_t maxLane = 0;
        for (const rhi::QueueRole role :
             {rhi::QueueRole::Graphics, rhi::QueueRole::Compute, rhi::QueueRole::Transfer, rhi::QueueRole::Present}) {
            const uint32_t lane = queues.GetSnapshot(role).nativeLane;
            if (lane != UINT32_MAX)
                maxLane = (std::max)(maxLane, lane);
        }
        m_laneTimelines.resize(static_cast<size_t>(maxLane) + 1);

        VkSemaphoreTypeCreateInfo typeInfo{};
        typeInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO;
        typeInfo.semaphoreType = VK_SEMAPHORE_TYPE_TIMELINE;
        typeInfo.initialValue = 0;

        VkSemaphoreCreateInfo createInfo{};
        createInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;
        createInfo.pNext = &typeInfo;
        for (LaneTimeline &timeline : m_laneTimelines) {
            if (vkCreateSemaphore(m_device, &createInfo, nullptr, &timeline.semaphore) != VK_SUCCESS) {
                Destroy();
                return false;
            }
        }
    }
    return true;
}

void VulkanSubmissionExecutor::Destroy() noexcept
{
    if (m_device != VK_NULL_HANDLE) {
        for (LaneTimeline &timeline : m_laneTimelines) {
            if (timeline.semaphore != VK_NULL_HANDLE)
                vkDestroySemaphore(m_device, timeline.semaphore, nullptr);
        }
        for (FrameState &frame : m_frames) {
            for (RolePool &role : frame.roles) {
                if (role.pool != VK_NULL_HANDLE)
                    vkDestroyCommandPool(m_device, role.pool, nullptr);
            }
        }
    }
    m_frames.clear();
    m_frameDiagnostics.clear();
    m_laneTimelines.clear();
    m_device = VK_NULL_HANDLE;
    m_queues = nullptr;
    m_deviceContext = nullptr;
}

VkSemaphore VulkanSubmissionExecutor::GetDependencyTimeline(rhi::QueueRole role) const noexcept
{
    if (!m_queues || role == rhi::QueueRole::Count)
        return VK_NULL_HANDLE;
    const uint32_t lane = m_queues->GetSnapshot(role).nativeLane;
    return lane < m_laneTimelines.size() ? m_laneTimelines[lane].semaphore : VK_NULL_HANDLE;
}

bool VulkanSubmissionExecutor::CreatePools(FrameState &frame)
{
    for (const rhi::QueueRole role : {rhi::QueueRole::Graphics, rhi::QueueRole::Compute, rhi::QueueRole::Transfer}) {
        const auto snapshot = m_queues->GetSnapshot(role);
        if (snapshot.queue == VK_NULL_HANDLE)
            continue;

        VkCommandPoolCreateInfo createInfo{};
        createInfo.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
        createInfo.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT | VK_COMMAND_POOL_CREATE_TRANSIENT_BIT;
        createInfo.queueFamilyIndex = snapshot.family;
        RolePool &target = frame.roles[RoleIndex(role)];
        if (vkCreateCommandPool(m_device, &createInfo, nullptr, &target.pool) != VK_SUCCESS)
            return false;
    }
    return true;
}

VkCommandBuffer VulkanSubmissionExecutor::AcquireCommandBuffer(FrameState &frame, rhi::QueueRole role)
{
    if (role == rhi::QueueRole::Present || role == rhi::QueueRole::Count)
        return VK_NULL_HANDLE;
    RolePool &owner = frame.roles[RoleIndex(role)];
    if (owner.pool == VK_NULL_HANDLE)
        return VK_NULL_HANDLE;
    if (owner.cursor < owner.buffers.size())
        return owner.buffers[owner.cursor++];

    VkCommandBufferAllocateInfo allocInfo{};
    allocInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocInfo.commandPool = owner.pool;
    allocInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocInfo.commandBufferCount = 1;
    VkCommandBuffer commandBuffer = VK_NULL_HANDLE;
    if (vkAllocateCommandBuffers(m_device, &allocInfo, &commandBuffer) != VK_SUCCESS)
        return VK_NULL_HANDLE;
    owner.buffers.push_back(commandBuffer);
    ++owner.cursor;
    return commandBuffer;
}

VkPipelineStageFlags VulkanSubmissionExecutor::ToVkStages(rhi::PipelineStage stages) noexcept
{
    if (stages == rhi::PipelineStage::None)
        return VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
    VkPipelineStageFlags result = 0;
    const auto has = [&](rhi::PipelineStage bit) { return (stages & bit) != rhi::PipelineStage::None; };
    if (has(rhi::PipelineStage::Top))
        result |= VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
    if (has(rhi::PipelineStage::DrawIndirect))
        result |= VK_PIPELINE_STAGE_DRAW_INDIRECT_BIT;
    if (has(rhi::PipelineStage::VertexInput))
        result |= VK_PIPELINE_STAGE_VERTEX_INPUT_BIT;
    if (has(rhi::PipelineStage::VertexShader))
        result |= VK_PIPELINE_STAGE_VERTEX_SHADER_BIT;
    if (has(rhi::PipelineStage::FragmentShader))
        result |= VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT;
    if (has(rhi::PipelineStage::EarlyDepth))
        result |= VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT;
    if (has(rhi::PipelineStage::LateDepth))
        result |= VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
    if (has(rhi::PipelineStage::ColorOutput))
        result |= VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    if (has(rhi::PipelineStage::ComputeShader))
        result |= VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT;
    if (has(rhi::PipelineStage::Transfer))
        result |= VK_PIPELINE_STAGE_TRANSFER_BIT;
    if (has(rhi::PipelineStage::Bottom))
        result |= VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT;
    if (has(rhi::PipelineStage::Host))
        result |= VK_PIPELINE_STAGE_HOST_BIT;
    if (has(rhi::PipelineStage::AllGraphics))
        result |= VK_PIPELINE_STAGE_ALL_GRAPHICS_BIT;
    if (has(rhi::PipelineStage::AllCommands))
        result |= VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
    return result != 0 ? result : VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
}

void VulkanSubmissionExecutor::CancelReservations(const std::vector<rhi::SubmissionTicket> &tickets,
                                                  size_t first) noexcept
{
    if (!m_queues)
        return;
    for (size_t index = first; index < tickets.size(); ++index)
        (void)m_queues->CancelReservation(tickets[index]);
}

VulkanSubmissionExecutor::ExecuteResult VulkanSubmissionExecutor::Execute(uint32_t frameSlot,
                                                                          const rhi::SubmissionPlan &plan,
                                                                          const BatchRecorder &recorder,
                                                                          const ExternalSync &sync)
{
    ExecuteResult output{};
    if (!m_deviceContext || !m_queues || frameSlot >= m_frames.size() || plan.batches.empty() || !recorder)
        return output;
    const auto firstGraphics = std::find_if(plan.batches.begin(), plan.batches.end(),
                                            [](const auto &batch) { return batch.queue == rhi::QueueRole::Graphics; });
    if (firstGraphics == plan.batches.end() || sync.completionFence == VK_NULL_HANDLE ||
        sync.completionEpoch == rhi::InvalidSubmissionSerial) {
        INXLOG_ERROR("VulkanSubmissionExecutor requires Graphics work, a completion fence, and a completion epoch");
        return output;
    }

    const uint32_t finalGraphicsLane = m_queues->GetSnapshot(rhi::QueueRole::Graphics).nativeLane;
    std::vector<bool> coveredByTerminal(plan.batches.size(), false);
    const auto markCovered = [&](auto &&self, uint32_t batchIndex) -> void {
        if (batchIndex >= plan.batches.size() || coveredByTerminal[batchIndex])
            return;
        coveredByTerminal[batchIndex] = true;
        const auto &batch = plan.batches[batchIndex];
        if (batch.queuePredecessor != rhi::InvalidSubmissionBatchIndex)
            self(self, batch.queuePredecessor);
        for (const auto &dependency : batch.waitsFor)
            self(self, dependency.sourceBatch);

        // Submission order is also execution order for logical roles that
        // alias one native VkQueue.
        const uint32_t lane = m_queues->GetSnapshot(batch.queue).nativeLane;
        for (uint32_t previous = 0; previous < batchIndex; ++previous) {
            if (m_queues->GetSnapshot(plan.batches[previous].queue).nativeLane == lane)
                self(self, previous);
        }
    };
    markCovered(markCovered, static_cast<uint32_t>(plan.batches.size() - 1));
    const bool needsTerminalJoin =
        std::any_of(coveredByTerminal.begin(), coveredByTerminal.end(), [](bool covered) { return !covered; });
    bool hasCrossQueueDependency = false;
    for (size_t batchIndex = 0; batchIndex < plan.batches.size(); ++batchIndex) {
        const auto &batch = plan.batches[batchIndex];
        const uint32_t targetLane = m_queues->GetSnapshot(batch.queue).nativeLane;
        for (const auto &dependency : batch.waitsFor) {
            if (dependency.sourceBatch < plan.batches.size() &&
                m_queues->GetSnapshot(plan.batches[dependency.sourceBatch].queue).nativeLane != targetLane) {
                hasCrossQueueDependency = true;
                break;
            }
        }
        // A fallback terminal join needs timeline visibility for independent
        // native lanes that are not covered by the plan's own terminal batch.
        if (batchIndex + 1 < plan.batches.size() && targetLane != finalGraphicsLane)
            hasCrossQueueDependency = true;
    }
    if (hasCrossQueueDependency && m_laneTimelines.empty()) {
        INXLOG_ERROR("VulkanSubmissionExecutor cannot execute cross-queue dependencies without timeline semaphores");
        return output;
    }

    FrameState &frame = m_frames[frameSlot];
    frame.submittedTickets.clear();
    for (RolePool &role : frame.roles) {
        role.cursor = 0;
        if (role.pool != VK_NULL_HANDLE && vkResetCommandPool(m_device, role.pool, 0) != VK_SUCCESS)
            return output;
    }

    std::vector<rhi::SubmissionTicket> tickets;
    std::vector<VkCommandBuffer> commands;
    std::vector<uint64_t> signalValues(plan.batches.size(), 0);
    tickets.reserve(plan.batches.size() + (needsTerminalJoin ? 1u : 0u));
    commands.reserve(plan.batches.size());

    for (size_t batchIndex = 0; batchIndex < plan.batches.size(); ++batchIndex) {
        const rhi::SubmissionBatch &batch = plan.batches[batchIndex];
        if (batch.device != m_deviceContext->GetDeviceId()) {
            INXLOG_ERROR("VulkanSubmissionExecutor rejected a batch for another device");
            CancelReservations(tickets, 0);
            return output;
        }
        rhi::SubmissionTicket ticket = m_queues->Reserve(batch.queue);
        if (!ticket.IsValid()) {
            CancelReservations(tickets, 0);
            return output;
        }
        tickets.push_back(ticket);
    }
    if (needsTerminalJoin) {
        const auto joinTicket = m_queues->Reserve(rhi::QueueRole::Graphics);
        if (!joinTicket.IsValid()) {
            CancelReservations(tickets, 0);
            return output;
        }
        tickets.push_back(joinTicket);
    }
    output.completionTicket = tickets.back();

    FrameDiagnostic &diagnostic = m_frameDiagnostics[frameSlot];
    diagnostic = {};
    diagnostic.previousTimeline = sync.previousFrameTimeline;
    diagnostic.previousTimelineValue = sync.previousFrameTimelineValue;
    diagnostic.uploadTimeline = sync.uploadTimeline;
    diagnostic.uploadTimelineValue = sync.uploadTimelineValue;
    diagnostic.batchRoles.reserve(plan.batches.size());
    diagnostic.batchLanes.reserve(plan.batches.size());
    diagnostic.batchSignalValues.reserve(plan.batches.size());
    diagnostic.batchQueuePredecessors.reserve(plan.batches.size());
    diagnostic.batchWaits.reserve(plan.batches.size());
    diagnostic.batchNames.reserve(plan.batches.size());

    try {
        for (size_t batchIndex = 0; batchIndex < plan.batches.size(); ++batchIndex) {
            const rhi::SubmissionBatch &batch = plan.batches[batchIndex];
            VkCommandBuffer commandBuffer = AcquireCommandBuffer(frame, batch.queue);
            if (commandBuffer == VK_NULL_HANDLE) {
                CancelReservations(tickets, 0);
                return output;
            }
            commands.push_back(commandBuffer);

            VkCommandBufferBeginInfo beginInfo{};
            beginInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
            beginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
            if (vkBeginCommandBuffer(commandBuffer, &beginInfo) != VK_SUCCESS) {
                CancelReservations(tickets, 0);
                return output;
            }

            const rhi::SubmissionTicket ticket = tickets[batchIndex];
            vkdebug::DescriptorRecordingScope descriptorRecording(
                &m_deviceContext->GetRhiDevice().GetDescriptorManager(), sync.completionEpoch);
            if (!recorder(static_cast<uint32_t>(batchIndex), commandBuffer) ||
                vkEndCommandBuffer(commandBuffer) != VK_SUCCESS) {
                CancelReservations(tickets, 0);
                return output;
            }
            const uint32_t lane = m_queues->GetSnapshot(batch.queue).nativeLane;
            if (lane < m_laneTimelines.size())
                signalValues[batchIndex] = m_laneTimelines[lane].nextValue++;
            diagnostic.batchRoles.push_back(batch.queue);
            diagnostic.batchLanes.push_back(lane);
            diagnostic.batchSignalValues.push_back(signalValues[batchIndex]);
            diagnostic.batchQueuePredecessors.push_back(batch.queuePredecessor);
            diagnostic.batchWaits.push_back(batch.waitsFor);
            diagnostic.batchNames.push_back(batch.diagnosticName);
        }
    } catch (...) {
        CancelReservations(tickets, 0);
        throw;
    }

    size_t firstGraphicsBatch = plan.batches.size();
    size_t lastGraphicsBatch = plan.batches.size();
    for (size_t index = 0; index < plan.batches.size(); ++index) {
        if (plan.batches[index].queue == rhi::QueueRole::Graphics) {
            if (firstGraphicsBatch == plan.batches.size())
                firstGraphicsBatch = index;
            lastGraphicsBatch = index;
        }
    }
    const size_t previousFrameWaitBatch = sync.previousFrameWaitAtFirstBatch ? 0 : firstGraphicsBatch;
    std::vector<bool> laneSubmitted((std::max)(m_laneTimelines.size(), static_cast<size_t>(4)), false);

    for (size_t batchIndex = 0; batchIndex < plan.batches.size(); ++batchIndex) {
        const rhi::SubmissionBatch &batch = plan.batches[batchIndex];
        const uint32_t targetLane = m_queues->GetSnapshot(batch.queue).nativeLane;
        std::vector<VkSemaphore> waits;
        std::vector<VkPipelineStageFlags> waitStages;
        std::vector<uint64_t> waitValues;
        bool usesTimeline = false;
        if (batchIndex == firstGraphicsBatch && sync.imageAvailable != VK_NULL_HANDLE) {
            waits.push_back(sync.imageAvailable);
            waitStages.push_back(sync.imageAvailableStages);
            waitValues.push_back(0);
        }
        if (batchIndex == previousFrameWaitBatch && sync.previousFrameTimeline != VK_NULL_HANDLE &&
            sync.previousFrameTimelineValue != 0) {
            waits.push_back(sync.previousFrameTimeline);
            waitStages.push_back(sync.previousFrameStages);
            waitValues.push_back(sync.previousFrameTimelineValue);
            usesTimeline = true;
        }
        const bool firstOnLane = targetLane < laneSubmitted.size() && !laneSubmitted[targetLane];
        if (firstOnLane && sync.uploadTimeline != VK_NULL_HANDLE && sync.uploadTimelineValue != 0) {
            waits.push_back(sync.uploadTimeline);
            waitStages.push_back(VK_PIPELINE_STAGE_ALL_COMMANDS_BIT);
            waitValues.push_back(sync.uploadTimelineValue);
            usesTimeline = true;
        }

        std::vector<uint64_t> laneWaitValues(m_laneTimelines.size(), 0);
        std::vector<VkPipelineStageFlags> laneWaitStages(m_laneTimelines.size(), 0);
        const auto addLaneWait = [&](uint32_t sourceBatch, VkPipelineStageFlags stages) {
            if (sourceBatch >= batchIndex || sourceBatch >= plan.batches.size())
                return false;
            const uint32_t sourceLane = m_queues->GetSnapshot(plan.batches[sourceBatch].queue).nativeLane;
            if (sourceLane == targetLane)
                return true;
            if (sourceLane >= m_laneTimelines.size() || signalValues[sourceBatch] == 0)
                return false;
            laneWaitValues[sourceLane] = (std::max)(laneWaitValues[sourceLane], signalValues[sourceBatch]);
            laneWaitStages[sourceLane] |= stages;
            return true;
        };

        for (const rhi::SubmissionBatchDependency &dependency : batch.waitsFor) {
            if (!addLaneWait(dependency.sourceBatch, ToVkStages(dependency.waitStages))) {
                CancelReservations(tickets, batchIndex);
                return output;
            }
        }

        const bool finalBatch = batchIndex + 1 == plan.batches.size();
        if (finalBatch && !needsTerminalJoin) {
            // Join every lane into the final Graphics fence, including work
            // that is topologically independent from the final render pass.
            for (uint32_t sourceBatch = 0; sourceBatch < batchIndex; ++sourceBatch) {
                if (!addLaneWait(sourceBatch, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT)) {
                    CancelReservations(tickets, batchIndex);
                    return output;
                }
            }
        }

        for (size_t lane = 0; lane < laneWaitValues.size(); ++lane) {
            if (laneWaitValues[lane] == 0)
                continue;
            waits.push_back(m_laneTimelines[lane].semaphore);
            waitStages.push_back(laneWaitStages[lane] != 0 ? laneWaitStages[lane] : VK_PIPELINE_STAGE_ALL_COMMANDS_BIT);
            waitValues.push_back(laneWaitValues[lane]);
            usesTimeline = true;
        }

        std::vector<VkSemaphore> signals;
        std::vector<uint64_t> signalSubmitValues;
        if (targetLane < m_laneTimelines.size() && signalValues[batchIndex] != 0) {
            signals.push_back(m_laneTimelines[targetLane].semaphore);
            signalSubmitValues.push_back(signalValues[batchIndex]);
            usesTimeline = true;
        }
        if (batchIndex == lastGraphicsBatch && sync.renderFinished != VK_NULL_HANDLE) {
            signals.push_back(sync.renderFinished);
            signalSubmitValues.push_back(0);
        }

        VkTimelineSemaphoreSubmitInfo timelineInfo{};
        if (usesTimeline) {
            timelineInfo.sType = VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO;
            timelineInfo.waitSemaphoreValueCount = static_cast<uint32_t>(waitValues.size());
            timelineInfo.pWaitSemaphoreValues = waitValues.data();
            timelineInfo.signalSemaphoreValueCount = static_cast<uint32_t>(signalSubmitValues.size());
            timelineInfo.pSignalSemaphoreValues = signalSubmitValues.data();
        }

        VkSubmitInfo submitInfo{};
        submitInfo.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
        submitInfo.pNext = usesTimeline ? &timelineInfo : nullptr;
        submitInfo.waitSemaphoreCount = static_cast<uint32_t>(waits.size());
        submitInfo.pWaitSemaphores = waits.data();
        submitInfo.pWaitDstStageMask = waitStages.data();
        submitInfo.commandBufferCount = 1;
        submitInfo.pCommandBuffers = &commands[batchIndex];
        submitInfo.signalSemaphoreCount = static_cast<uint32_t>(signals.size());
        submitInfo.pSignalSemaphores = signals.data();

        const VkFence fence = finalBatch && !needsTerminalJoin ? sync.completionFence : VK_NULL_HANDLE;
        const VkResult result = m_queues->SubmitReserved(tickets[batchIndex], submitInfo, fence);
        if (result != VK_SUCCESS) {
            CancelReservations(tickets, batchIndex + 1);
            if (output.submittedAny) {
                (void)m_queues->WaitIdleForAllQueues();
                for (size_t completed = 0; completed < batchIndex; ++completed)
                    m_queues->MarkCompleted(tickets[completed]);
            }
            output.result = result;
            return output;
        }
        output.submittedAny = true;
        frame.submittedTickets.push_back(tickets[batchIndex]);
        if (targetLane < laneSubmitted.size())
            laneSubmitted[targetLane] = true;
        if (finalBatch && !needsTerminalJoin && targetLane < m_laneTimelines.size()) {
            output.completionTimeline = m_laneTimelines[targetLane].semaphore;
            output.completionTimelineValue = signalValues[batchIndex];
        }
    }

    if (needsTerminalJoin) {
        std::vector<VkSemaphore> waits;
        std::vector<VkPipelineStageFlags> waitStages;
        std::vector<uint64_t> waitValues;
        std::vector<uint64_t> joinedLaneValues(m_laneTimelines.size(), 0);
        for (size_t batchIndex = 0; batchIndex < plan.batches.size(); ++batchIndex) {
            const uint32_t lane = m_queues->GetSnapshot(plan.batches[batchIndex].queue).nativeLane;
            if (lane == finalGraphicsLane || lane >= joinedLaneValues.size())
                continue;
            joinedLaneValues[lane] = (std::max)(joinedLaneValues[lane], signalValues[batchIndex]);
        }
        for (size_t lane = 0; lane < joinedLaneValues.size(); ++lane) {
            if (joinedLaneValues[lane] == 0)
                continue;
            waits.push_back(m_laneTimelines[lane].semaphore);
            waitStages.push_back(VK_PIPELINE_STAGE_ALL_COMMANDS_BIT);
            waitValues.push_back(joinedLaneValues[lane]);
        }

        std::vector<VkSemaphore> signals;
        std::vector<uint64_t> signalValuesForSubmit;
        uint64_t joinTimelineValue = 0;
        if (finalGraphicsLane < m_laneTimelines.size()) {
            signals.push_back(m_laneTimelines[finalGraphicsLane].semaphore);
            joinTimelineValue = m_laneTimelines[finalGraphicsLane].nextValue++;
            signalValuesForSubmit.push_back(joinTimelineValue);
        }
        VkTimelineSemaphoreSubmitInfo timelineInfo{};
        timelineInfo.sType = VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO;
        timelineInfo.waitSemaphoreValueCount = static_cast<uint32_t>(waitValues.size());
        timelineInfo.pWaitSemaphoreValues = waitValues.data();
        timelineInfo.signalSemaphoreValueCount = static_cast<uint32_t>(signalValuesForSubmit.size());
        timelineInfo.pSignalSemaphoreValues = signalValuesForSubmit.data();

        VkSubmitInfo submitInfo{};
        submitInfo.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
        submitInfo.pNext = &timelineInfo;
        submitInfo.waitSemaphoreCount = static_cast<uint32_t>(waits.size());
        submitInfo.pWaitSemaphores = waits.data();
        submitInfo.pWaitDstStageMask = waitStages.data();
        submitInfo.signalSemaphoreCount = static_cast<uint32_t>(signals.size());
        submitInfo.pSignalSemaphores = signals.data();

        const auto joinTicket = tickets.back();
        const VkResult joinResult = m_queues->SubmitReserved(joinTicket, submitInfo, sync.completionFence);
        if (joinResult != VK_SUCCESS) {
            if (output.submittedAny) {
                (void)m_queues->WaitIdleForAllQueues();
                for (size_t completed = 0; completed + 1 < tickets.size(); ++completed)
                    m_queues->MarkCompleted(tickets[completed]);
            }
            output.result = joinResult;
            return output;
        }
        output.submittedAny = true;
        frame.submittedTickets.push_back(joinTicket);
        output.completionTimeline = m_laneTimelines[finalGraphicsLane].semaphore;
        output.completionTimelineValue = joinTimelineValue;
    }

    output.result = VK_SUCCESS;
    diagnostic.completionTimeline = output.completionTimeline;
    diagnostic.completionTimelineValue = output.completionTimelineValue;
    return output;
}

void VulkanSubmissionExecutor::LogFrameWaitDiagnostics(uint32_t frameSlot, uint32_t elapsedMilliseconds) const noexcept
{
    if (m_device == VK_NULL_HANDLE || frameSlot >= m_frameDiagnostics.size())
        return;

    const FrameDiagnostic &diagnostic = m_frameDiagnostics[frameSlot];
    const auto counterValue = [this](VkSemaphore semaphore) noexcept {
        uint64_t value = 0;
        if (semaphore == VK_NULL_HANDLE || vkGetSemaphoreCounterValue(m_device, semaphore, &value) != VK_SUCCESS)
            return uint64_t{0};
        return value;
    };

    std::vector<uint64_t> laneCurrent(m_laneTimelines.size(), 0);
    std::string lanes;
    for (size_t lane = 0; lane < m_laneTimelines.size(); ++lane) {
        laneCurrent[lane] = counterValue(m_laneTimelines[lane].semaphore);
        if (!lanes.empty())
            lanes += ',';
        lanes += std::to_string(lane) + "=" + std::to_string(laneCurrent[lane]) + "/" +
                 std::to_string(m_laneTimelines[lane].nextValue > 0 ? m_laneTimelines[lane].nextValue - 1 : 0);
    }

    std::string batches;
    for (size_t index = 0; index < diagnostic.batchRoles.size(); ++index) {
        if (!batches.empty())
            batches += ',';
        const uint32_t lane = diagnostic.batchLanes[index];
        const bool completed = lane < laneCurrent.size() && laneCurrent[lane] >= diagnostic.batchSignalValues[index];
        batches += std::to_string(index) + ":r" + std::to_string(static_cast<uint32_t>(diagnostic.batchRoles[index])) +
                   "/l" + std::to_string(lane) + "=" + std::to_string(diagnostic.batchSignalValues[index]) +
                   (completed ? ":done" : ":pending");
        if (index < diagnostic.batchNames.size() && !diagnostic.batchNames[index].empty())
            batches += ":name='" + diagnostic.batchNames[index] + "'";
        if (index < diagnostic.batchQueuePredecessors.size() &&
            diagnostic.batchQueuePredecessors[index] != rhi::InvalidSubmissionBatchIndex)
            batches += ":qprev=" + std::to_string(diagnostic.batchQueuePredecessors[index]);
        if (index < diagnostic.batchWaits.size() && !diagnostic.batchWaits[index].empty()) {
            batches += ":wait=[";
            bool firstWait = true;
            for (const auto &wait : diagnostic.batchWaits[index]) {
                if (!firstWait)
                    batches += '|';
                firstWait = false;
                batches += "b" + std::to_string(wait.sourceBatch);
                if (wait.sourceBatch < diagnostic.batchSignalValues.size() &&
                    wait.sourceBatch < diagnostic.batchLanes.size()) {
                    const uint32_t sourceLane = diagnostic.batchLanes[wait.sourceBatch];
                    batches += "/l" + std::to_string(sourceLane) + "=" +
                               std::to_string(diagnostic.batchSignalValues[wait.sourceBatch]);
                    if (sourceLane < laneCurrent.size())
                        batches += "/current" + std::to_string(laneCurrent[sourceLane]);
                }
            }
            batches += ']';
        }
    }

    INXLOG_ERROR("VulkanSubmissionExecutor: frame slot ", frameSlot, " stalled for ", elapsedMilliseconds,
                 " ms; lanes(current/submitted)=[", lanes, "] batches=[", batches,
                 "] previous=", counterValue(diagnostic.previousTimeline), '/', diagnostic.previousTimelineValue,
                 " upload=", counterValue(diagnostic.uploadTimeline), '/', diagnostic.uploadTimelineValue,
                 " completion=", counterValue(diagnostic.completionTimeline), '/', diagnostic.completionTimelineValue);
}

void VulkanSubmissionExecutor::CompleteFrame(uint32_t frameSlot) noexcept
{
    if (!m_queues || frameSlot >= m_frames.size())
        return;
    FrameState &frame = m_frames[frameSlot];
    for (const rhi::SubmissionTicket ticket : frame.submittedTickets)
        m_queues->MarkCompleted(ticket);
    frame.submittedTickets.clear();
}

} // namespace infernux::vk
