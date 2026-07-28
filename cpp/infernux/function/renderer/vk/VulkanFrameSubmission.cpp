#include "VulkanFrameSubmission.h"

#include <core/error/InxError.h>

namespace infernux::vk
{

void VulkanFrameSubmission::Reset(uint32_t firstWorkItemId)
{
    m_composer.Reset(firstWorkItemId);
    m_recorders.clear();
    m_lastWork.fill(0);
}

uint32_t VulkanFrameSubmission::AddWork(rhi::DeviceId device, rhi::QueueRole queue, rhi::SubmissionDomain domain,
                                        rhi::RenderViewId view, rhi::PipelineStage waitStages,
                                        std::vector<uint32_t> dependencies, Recorder recorder)
{
    const uint32_t workItem =
        m_composer.AddWork(device, queue, domain, view, waitStages, std::move(dependencies), true);
    if (recorder)
        m_recorders.emplace(workItem, std::move(recorder));
    if (queue != rhi::QueueRole::Count)
        m_lastWork[static_cast<size_t>(queue)] = workItem;
    return workItem;
}

rhi::ComposedSubmissionRange
VulkanFrameSubmission::AppendRenderGraph(RenderGraph &graph, const std::vector<uint32_t> &externalDependencies,
                                         BatchHook beforeBatch, GraphHook afterGraph)
{
    const auto &source = graph.GetSubmissionPlan();
    rhi::ComposedSubmissionRange range = m_composer.Append(source, externalDependencies);
    for (size_t batchIndex = 0; batchIndex < range.workItems.size(); ++batchIndex) {
        const uint32_t workItem = range.workItems[batchIndex];
        m_recorders.emplace(
            workItem,
            [&graph, batchIndex, batchCount = range.workItems.size(), beforeBatch, afterGraph](VkCommandBuffer cmd) {
                if (beforeBatch && !beforeBatch(static_cast<uint32_t>(batchIndex), cmd))
                    return false;
                if (batchIndex == 0)
                    graph.BeginExecution();
                if (!graph.RecordSubmissionBatch(static_cast<uint32_t>(batchIndex), cmd))
                    return false;
                return batchIndex + 1 != batchCount || !afterGraph || afterGraph(cmd);
            });
        const auto queue = source.batches[batchIndex].queue;
        if (queue != rhi::QueueRole::Count)
            m_lastWork[static_cast<size_t>(queue)] = workItem;
    }
    return range;
}

bool VulkanFrameSubmission::Build(rhi::SubmissionPlan &output, std::string &error) const
{
    return m_composer.Build(output, error);
}

bool VulkanFrameSubmission::RecordBatch(const rhi::SubmissionPlan &plan, uint32_t batchIndex,
                                        VkCommandBuffer commandBuffer) const
{
    if (batchIndex >= plan.batches.size() || commandBuffer == VK_NULL_HANDLE)
        return false;
    for (const uint32_t workItem : plan.batches[batchIndex].workItems) {
        const auto recorder = m_recorders.find(workItem);
        if (recorder == m_recorders.end()) {
            INXLOG_ERROR("VulkanFrameSubmission has no recorder for work item ", workItem);
            return false;
        }
        if (!recorder->second(commandBuffer))
            return false;
    }
    return true;
}

uint32_t VulkanFrameSubmission::LastWork(rhi::QueueRole queue) const noexcept
{
    if (queue == rhi::QueueRole::Count)
        return 0;
    return m_lastWork[static_cast<size_t>(queue)];
}

} // namespace infernux::vk
