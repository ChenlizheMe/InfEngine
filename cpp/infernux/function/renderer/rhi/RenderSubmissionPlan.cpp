#include "RenderSubmissionPlan.h"

#include <algorithm>
#include <unordered_map>
#include <unordered_set>

namespace infernux::rhi
{

namespace
{

bool SameBatchIdentity(const SubmissionBatch &batch, const SubmissionWorkItem &item) noexcept
{
    return batch.device == item.device && batch.queue == item.queue && batch.domain == item.domain &&
           batch.view == item.view;
}

} // namespace

void SubmissionPlanComposer::Reset(uint32_t firstWorkItemId) noexcept
{
    m_nextWorkItemId = firstWorkItemId;
    m_workItems.clear();
}

uint32_t SubmissionPlanComposer::AddWork(DeviceId device, QueueRole queue, SubmissionDomain domain, RenderViewId view,
                                         PipelineStage waitStages, std::vector<uint32_t> dependencies,
                                         bool forceBatchBoundary, std::string diagnosticName)
{
    const uint32_t id = m_nextWorkItemId++;
    m_workItems.push_back({id, device, queue, domain, view, waitStages, std::move(dependencies), forceBatchBoundary,
                           std::move(diagnosticName)});
    return id;
}

ComposedSubmissionRange SubmissionPlanComposer::Append(const SubmissionPlan &source,
                                                       const std::vector<uint32_t> &externalDependencies)
{
    ComposedSubmissionRange result;
    if (source.batches.empty())
        return result;

    result.workItems.resize(source.batches.size());
    for (size_t index = 0; index < source.batches.size(); ++index) {
        result.workItems[index] = m_nextWorkItemId++;
    }

    std::vector<bool> hasInternalPredecessor(source.batches.size(), false);
    std::vector<bool> hasInternalConsumer(source.batches.size(), false);
    for (size_t index = 0; index < source.batches.size(); ++index) {
        const SubmissionBatch &batch = source.batches[index];
        std::vector<uint32_t> dependencies;
        dependencies.reserve(batch.waitsFor.size() + externalDependencies.size() + 1);
        if (batch.queuePredecessor != InvalidSubmissionBatchIndex && batch.queuePredecessor < source.batches.size()) {
            dependencies.push_back(result.workItems[batch.queuePredecessor]);
            hasInternalPredecessor[index] = true;
            hasInternalConsumer[batch.queuePredecessor] = true;
        }

        PipelineStage waitStages = PipelineStage::None;
        for (const SubmissionBatchDependency &dependency : batch.waitsFor) {
            if (dependency.sourceBatch >= source.batches.size())
                continue;
            const uint32_t sourceWorkItem = result.workItems[dependency.sourceBatch];
            if (std::find(dependencies.begin(), dependencies.end(), sourceWorkItem) == dependencies.end())
                dependencies.push_back(sourceWorkItem);
            waitStages = waitStages | dependency.waitStages;
            hasInternalPredecessor[index] = true;
            hasInternalConsumer[dependency.sourceBatch] = true;
        }

        if (!hasInternalPredecessor[index]) {
            dependencies.insert(dependencies.end(), externalDependencies.begin(), externalDependencies.end());
            result.roots.push_back(result.workItems[index]);
        }
        if (waitStages == PipelineStage::None)
            waitStages = batch.queue == QueueRole::Compute ? PipelineStage::ComputeShader : PipelineStage::AllCommands;

        m_workItems.push_back({result.workItems[index], batch.device, batch.queue, batch.domain, batch.view, waitStages,
                               std::move(dependencies), true, batch.diagnosticName});
    }

    for (size_t index = 0; index < source.batches.size(); ++index) {
        if (!hasInternalConsumer[index])
            result.terminals.push_back(result.workItems[index]);
    }
    return result;
}

bool SubmissionPlanComposer::Build(SubmissionPlan &output, std::string &error) const
{
    return BuildSubmissionPlan(m_workItems, output, error);
}

bool BuildSubmissionPlan(const std::vector<SubmissionWorkItem> &workItems, SubmissionPlan &output, std::string &error)
{
    output.Clear();
    error.clear();
    if (workItems.empty())
        return true;

    std::unordered_map<uint32_t, uint32_t> itemToBatch;
    itemToBatch.reserve(workItems.size());
    std::unordered_map<uint64_t, uint32_t> previousQueueBatch;

    for (const SubmissionWorkItem &item : workItems) {
        if (item.device == InvalidDeviceId) {
            error = "submission work item " + std::to_string(item.id) + " has no device";
            output.Clear();
            return false;
        }
        if (item.queue == QueueRole::Count) {
            error = "submission work item " + std::to_string(item.id) + " has an invalid queue role";
            output.Clear();
            return false;
        }
        if (itemToBatch.find(item.id) != itemToBatch.end()) {
            error = "duplicate submission work item id " + std::to_string(item.id);
            output.Clear();
            return false;
        }

        if (output.batches.empty() || item.forceBatchBoundary || !SameBatchIdentity(output.batches.back(), item)) {
            SubmissionBatch batch;
            batch.index = static_cast<uint32_t>(output.batches.size());
            batch.device = item.device;
            batch.queue = item.queue;
            batch.domain = item.domain;
            batch.view = item.view;

            const uint64_t queueKey = (static_cast<uint64_t>(item.device) << 8u) | static_cast<uint64_t>(item.queue);
            const auto previous = previousQueueBatch.find(queueKey);
            if (previous != previousQueueBatch.end())
                batch.queuePredecessor = previous->second;
            previousQueueBatch[queueKey] = batch.index;
            output.batches.push_back(std::move(batch));
        }

        SubmissionBatch &targetBatch = output.batches.back();
        targetBatch.workItems.push_back(item.id);
        if (!item.diagnosticName.empty()) {
            if (!targetBatch.diagnosticName.empty())
                targetBatch.diagnosticName += '|';
            targetBatch.diagnosticName += item.diagnosticName;
        }
        itemToBatch.emplace(item.id, targetBatch.index);

        for (const uint32_t dependencyId : item.dependencies) {
            const auto sourceMapping = itemToBatch.find(dependencyId);
            if (sourceMapping == itemToBatch.end()) {
                error = "submission work item " + std::to_string(item.id) + " depends on unknown or later item " +
                        std::to_string(dependencyId);
                output.Clear();
                return false;
            }

            const uint32_t sourceBatchIndex = sourceMapping->second;
            if (sourceBatchIndex == targetBatch.index)
                continue;
            const SubmissionBatch &sourceBatch = output.batches[sourceBatchIndex];
            if (sourceBatch.device != targetBatch.device) {
                error = "submission dependency " + std::to_string(dependencyId) + " -> " + std::to_string(item.id) +
                        " crosses devices without an explicit transfer";
                output.Clear();
                return false;
            }
            if (sourceBatch.queue == targetBatch.queue)
                continue;

            const auto existing = std::find_if(targetBatch.waitsFor.begin(), targetBatch.waitsFor.end(),
                                               [&](const SubmissionBatchDependency &dependency) {
                                                   return dependency.sourceBatch == sourceBatchIndex;
                                               });
            if (existing == targetBatch.waitsFor.end()) {
                targetBatch.waitsFor.push_back({sourceBatchIndex, item.waitStages});
            } else {
                existing->waitStages = existing->waitStages | item.waitStages;
            }
        }
    }

    return true;
}

SubmissionPlanStatistics AnalyzeSubmissionPlan(const SubmissionPlan &plan)
{
    SubmissionPlanStatistics result;
    result.batchCount = static_cast<uint32_t>(plan.batches.size());
    const size_t count = plan.batches.size();
    std::vector<std::vector<bool>> ancestors(count, std::vector<bool>(count, false));

    const auto inherit = [&](size_t batchIndex, uint32_t predecessor) {
        if (predecessor >= batchIndex || predecessor >= count)
            return;
        ancestors[batchIndex][predecessor] = true;
        for (size_t ancestor = 0; ancestor < count; ++ancestor)
            ancestors[batchIndex][ancestor] = ancestors[batchIndex][ancestor] || ancestors[predecessor][ancestor];
    };

    for (size_t index = 0; index < count; ++index) {
        const SubmissionBatch &batch = plan.batches[index];
        switch (batch.queue) {
        case QueueRole::Graphics:
            ++result.graphicsBatchCount;
            break;
        case QueueRole::Compute:
            ++result.computeBatchCount;
            break;
        case QueueRole::Transfer:
            ++result.transferBatchCount;
            break;
        default:
            break;
        }
        inherit(index, batch.queuePredecessor);
        result.crossQueueDependencyCount += static_cast<uint32_t>(batch.waitsFor.size());
        for (const SubmissionBatchDependency &dependency : batch.waitsFor)
            inherit(index, dependency.sourceBatch);
    }

    for (size_t compute = 0; compute < count; ++compute) {
        if (plan.batches[compute].queue != QueueRole::Compute)
            continue;
        for (size_t graphics = 0; graphics < count; ++graphics) {
            if (plan.batches[graphics].queue != QueueRole::Graphics)
                continue;
            if (!ancestors[compute][graphics] && !ancestors[graphics][compute])
                ++result.unorderedComputeGraphicsPairCount;
        }
    }
    return result;
}

} // namespace infernux::rhi
