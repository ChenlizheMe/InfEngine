#include <function/renderer/rhi/RenderSubmissionPlan.h>
#include <function/renderer/vk/DescriptorBindTrace.h>
#include <function/renderer/vk/VkDeviceContext.h>
#include <function/renderer/vk/VkTypes.h>
#include <function/renderer/vk/VulkanQueueManager.h>
#include <function/renderer/vk/VulkanRhiDevice.h>
#include <function/renderer/vk/VulkanSubmissionExecutor.h>

#include <SDL3/SDL.h>

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <stdexcept>
#include <string>
#include <vector>

using namespace infernux;

int main()
{
    assert(SDL_Init(SDL_INIT_VIDEO));
    SDL_Window *window =
        SDL_CreateWindow("Infernux Vulkan Submission Executor Test", 64, 64, SDL_WINDOW_VULKAN | SDL_WINDOW_HIDDEN);
    assert(window != nullptr);

    vk::VkDeviceContext context;
    vk::DeviceConfig config;
    config.appName = "Infernux Vulkan Submission Executor Test";
    config.enableValidationLayers = true;
    assert(context.Initialize(window, config));

    vk::VulkanQueueManager queues;
    assert(queues.Initialize(context, 2));
    assert(queues.GetLastReservedCompletionEpoch() == 0);
    assert(queues.GetCompletedCompletionEpoch() == 0);
    assert(queues.ResetGraphicsFrameFence(0));
    const auto abandonedEpoch = queues.GetFrameCompletionEpoch(0);
    assert(abandonedEpoch != rhi::InvalidSubmissionSerial);
    assert(queues.GetLastReservedCompletionEpoch() == abandonedEpoch);
    assert(!queues.ResetGraphicsFrameFence(0));
    assert(queues.AbandonGraphicsFrameSlot(0));
    assert(queues.GetCompletedCompletionEpoch() == abandonedEpoch);
    assert(queues.GetFrameCompletionEpoch(0) == rhi::InvalidSubmissionSerial);
    const auto earlierEpoch = queues.ReserveCompletionEpoch();
    const auto laterEpoch = queues.ReserveCompletionEpoch();
    queues.CompleteCompletionEpoch(laterEpoch);
    assert(queues.GetCompletedCompletionEpoch() == abandonedEpoch);
    queues.CompleteCompletionEpoch(earlierEpoch);
    assert(queues.GetCompletedCompletionEpoch() == laterEpoch);
    const auto graphicsLane = queues.GetSnapshot(rhi::QueueRole::Graphics).nativeLane;
    const auto computeLane = queues.GetSnapshot(rhi::QueueRole::Compute).nativeLane;
    const auto transferLane = queues.GetSnapshot(rhi::QueueRole::Transfer).nativeLane;
    assert((graphicsLane != computeLane) == context.HasIndependentComputeQueue());

    vk::VulkanSubmissionExecutor executor;
    assert(executor.Initialize(context, queues, 2));
    assert((executor.GetDependencyTimeline(rhi::QueueRole::Graphics) !=
            executor.GetDependencyTimeline(rhi::QueueRole::Compute)) == context.HasIndependentComputeQueue());
    if (transferLane != graphicsLane) {
        assert(executor.GetDependencyTimeline(rhi::QueueRole::Transfer) !=
               executor.GetDependencyTimeline(rhi::QueueRole::Graphics));
    }

    std::vector<rhi::SubmissionWorkItem> work(3);
    work[0] = {0,
               context.GetDeviceId(),
               rhi::QueueRole::Graphics,
               rhi::SubmissionDomain::Frame,
               rhi::InvalidRenderViewId,
               rhi::PipelineStage::AllCommands,
               {}};
    work[1] = {1,
               context.GetDeviceId(),
               rhi::QueueRole::Compute,
               rhi::SubmissionDomain::Frame,
               rhi::InvalidRenderViewId,
               rhi::PipelineStage::ComputeShader,
               {0}};
    work[2] = {2,
               context.GetDeviceId(),
               rhi::QueueRole::Graphics,
               rhi::SubmissionDomain::Frame,
               rhi::InvalidRenderViewId,
               rhi::PipelineStage::DrawIndirect,
               {1}};

    rhi::SubmissionPlan plan;
    std::string error;
    assert(rhi::BuildSubmissionPlan(work, plan, error));
    assert(plan.batches.size() == 3);

    VkFenceCreateInfo fenceInfo{};
    fenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    VkFence completionFence = VK_NULL_HANDLE;
    assert(vkCreateFence(context.GetDevice(), &fenceInfo, nullptr, &completionFence) == VK_SUCCESS);

    vk::VulkanSubmissionExecutor::ExternalSync sync{};
    assert(queues.ResetGraphicsFrameFence(0));
    sync.completionFence = queues.GetGraphicsFrameFence(0);
    sync.completionEpoch = queues.GetFrameCompletionEpoch(0);
    uint32_t recorded = 0;
    const auto result = executor.Execute(
        0, plan,
        [&](uint32_t batchIndex, VkCommandBuffer commandBuffer) {
            assert(batchIndex == recorded);
            assert(commandBuffer != VK_NULL_HANDLE);
            assert(vkdebug::GetDescriptorRecordingSubmissionSerial() == sync.completionEpoch);
            ++recorded;
            return true;
        },
        sync);
    assert(result.Succeeded());
    assert(vkdebug::GetDescriptorRecordingSubmissionSerial() == rhi::InvalidSubmissionSerial);
    assert(queues.AssociateFrameSlot(0, result.completionTicket));
    assert(recorded == plan.batches.size());
    assert(queues.WaitForGraphicsFrameSlot(0));

    executor.CompleteFrame(0);
    assert(queues.CompleteFrameSlot(0).IsValid());
    assert(queues.GetCompletedCompletionEpoch() == sync.completionEpoch);
    assert(queues.GetCompletedSerial(rhi::QueueRole::Graphics) == result.completionTicket.serial);
    assert(queues.GetCompletedSerial(rhi::QueueRole::Compute) > 0);

    sync.completionFence = completionFence;

    if (transferLane != graphicsLane) {
        assert(vkResetFences(context.GetDevice(), 1, &completionFence) == VK_SUCCESS);
        sync.completionEpoch = queues.ReserveCompletionEpoch();
        std::vector<rhi::SubmissionWorkItem> independentWork(2);
        independentWork[0] = {10,
                              context.GetDeviceId(),
                              rhi::QueueRole::Transfer,
                              rhi::SubmissionDomain::Frame,
                              rhi::InvalidRenderViewId,
                              rhi::PipelineStage::Transfer,
                              {}};
        independentWork[1] = {11,
                              context.GetDeviceId(),
                              rhi::QueueRole::Graphics,
                              rhi::SubmissionDomain::Frame,
                              rhi::InvalidRenderViewId,
                              rhi::PipelineStage::AllGraphics,
                              {}};
        rhi::SubmissionPlan independentPlan;
        assert(rhi::BuildSubmissionPlan(independentWork, independentPlan, error));
        const auto joined = executor.Execute(
            1, independentPlan, [](uint32_t, VkCommandBuffer commandBuffer) { return commandBuffer != VK_NULL_HANDLE; },
            sync);
        assert(joined.Succeeded());
        assert(vkWaitForFences(context.GetDevice(), 1, &completionFence, VK_TRUE, 5'000'000'000ull) == VK_SUCCESS);
        executor.CompleteFrame(1);
        queues.CompleteCompletionEpoch(sync.completionEpoch);
        assert(queues.GetCompletedSerial(rhi::QueueRole::Transfer) > 0);
    }

    if (computeLane != graphicsLane) {
        assert(vkResetFences(context.GetDevice(), 1, &completionFence) == VK_SUCCESS);
        sync.completionEpoch = queues.ReserveCompletionEpoch();
        const std::vector<rhi::SubmissionWorkItem> overlappedWork = {
            {20,
             context.GetDeviceId(),
             rhi::QueueRole::Compute,
             rhi::SubmissionDomain::Frame,
             rhi::InvalidRenderViewId,
             rhi::PipelineStage::ComputeShader,
             {}},
            {21,
             context.GetDeviceId(),
             rhi::QueueRole::Graphics,
             rhi::SubmissionDomain::Frame,
             rhi::InvalidRenderViewId,
             rhi::PipelineStage::AllGraphics,
             {}},
            {22,
             context.GetDeviceId(),
             rhi::QueueRole::Compute,
             rhi::SubmissionDomain::Frame,
             rhi::InvalidRenderViewId,
             rhi::PipelineStage::ComputeShader,
             {20, 21}},
        };
        rhi::SubmissionPlan overlappedPlan;
        assert(rhi::BuildSubmissionPlan(overlappedWork, overlappedPlan, error));
        assert(overlappedPlan.batches.back().queue == rhi::QueueRole::Compute);
        const auto overlapped = executor.Execute(
            0, overlappedPlan, [](uint32_t, VkCommandBuffer commandBuffer) { return commandBuffer != VK_NULL_HANDLE; },
            sync);
        assert(overlapped.Succeeded());
        assert(overlapped.completionTicket.queue == rhi::QueueRole::Compute);
        assert(overlapped.completionTimeline != VK_NULL_HANDLE && overlapped.completionTimelineValue != 0);
        assert(vkWaitForFences(context.GetDevice(), 1, &completionFence, VK_TRUE, 5'000'000'000ull) == VK_SUCCESS);
        executor.CompleteFrame(0);
        queues.CompleteCompletionEpoch(sync.completionEpoch);

        assert(vkResetFences(context.GetDevice(), 1, &completionFence) == VK_SUCCESS);
        sync.completionEpoch = queues.ReserveCompletionEpoch();
        rhi::SubmissionPlan nextFramePlan;
        assert(rhi::BuildSubmissionPlan({{23,
                                          context.GetDeviceId(),
                                          rhi::QueueRole::Graphics,
                                          rhi::SubmissionDomain::Frame,
                                          rhi::InvalidRenderViewId,
                                          rhi::PipelineStage::AllGraphics,
                                          {}}},
                                        nextFramePlan, error));
        sync.previousFrameTimeline = overlapped.completionTimeline;
        sync.previousFrameTimelineValue = overlapped.completionTimelineValue;
        const auto nextFrame = executor.Execute(
            1, nextFramePlan, [](uint32_t, VkCommandBuffer commandBuffer) { return commandBuffer != VK_NULL_HANDLE; },
            sync);
        assert(nextFrame.Succeeded());
        assert(vkWaitForFences(context.GetDevice(), 1, &completionFence, VK_TRUE, 5'000'000'000ull) == VK_SUCCESS);
        executor.CompleteFrame(1);
        queues.CompleteCompletionEpoch(sync.completionEpoch);
        sync.previousFrameTimeline = VK_NULL_HANDLE;
        sync.previousFrameTimelineValue = 0;

        // A graph-generation prime starts on Compute while preserving GPU
        // resident state from the previous generation. Verify that the
        // previous generation dependency gates that first Compute batch.
        VkSemaphoreTypeCreateInfo gateType{};
        gateType.sType = VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO;
        gateType.semaphoreType = VK_SEMAPHORE_TYPE_TIMELINE;
        gateType.initialValue = 0;
        VkSemaphoreCreateInfo gateInfo{};
        gateInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;
        gateInfo.pNext = &gateType;
        VkSemaphore generationGate = VK_NULL_HANDLE;
        assert(vkCreateSemaphore(context.GetDevice(), &gateInfo, nullptr, &generationGate) == VK_SUCCESS);

        assert(vkResetFences(context.GetDevice(), 1, &completionFence) == VK_SUCCESS);
        sync.completionEpoch = queues.ReserveCompletionEpoch();
        sync.previousFrameTimeline = generationGate;
        sync.previousFrameTimelineValue = 1;
        sync.previousFrameWaitAtFirstBatch = true;
        const auto generationPrime = executor.Execute(
            0, overlappedPlan, [](uint32_t, VkCommandBuffer commandBuffer) { return commandBuffer != VK_NULL_HANDLE; },
            sync);
        assert(generationPrime.Succeeded());
        assert(vkWaitForFences(context.GetDevice(), 1, &completionFence, VK_TRUE, 1'000'000ull) == VK_TIMEOUT);

        VkSemaphoreSignalInfo signalInfo{};
        signalInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_SIGNAL_INFO;
        signalInfo.semaphore = generationGate;
        signalInfo.value = 1;
        assert(vkSignalSemaphore(context.GetDevice(), &signalInfo) == VK_SUCCESS);
        assert(vkWaitForFences(context.GetDevice(), 1, &completionFence, VK_TRUE, 5'000'000'000ull) == VK_SUCCESS);
        executor.CompleteFrame(0);
        queues.CompleteCompletionEpoch(sync.completionEpoch);
        vkDestroySemaphore(context.GetDevice(), generationGate, nullptr);
        sync.previousFrameTimeline = VK_NULL_HANDLE;
        sync.previousFrameTimelineValue = 0;
        sync.previousFrameWaitAtFirstBatch = false;
    }

    rhi::SubmissionPlan failedPlan;
    assert(rhi::BuildSubmissionPlan({work.front()}, failedPlan, error));
    sync.completionEpoch = queues.ReserveCompletionEpoch();
    const auto failed = executor.Execute(
        1, failedPlan,
        [&](uint32_t, VkCommandBuffer) {
            assert(vkdebug::GetDescriptorRecordingSubmissionSerial() == sync.completionEpoch);
            return false;
        },
        sync);
    assert(!failed.Succeeded());
    assert(vkdebug::GetDescriptorRecordingSubmissionSerial() == rhi::InvalidSubmissionSerial);
    queues.CompleteCompletionEpoch(sync.completionEpoch);
    const auto afterFailure = queues.Reserve(rhi::QueueRole::Graphics);
    assert(afterFailure.IsValid());
    assert(queues.CancelReservation(afterFailure));

    context.WaitIdle();
    vkDestroyFence(context.GetDevice(), completionFence, nullptr);
    executor.Destroy();
    queues.Destroy();
    context.Destroy();
    SDL_DestroyWindow(window);
    SDL_Quit();
    return 0;
}
