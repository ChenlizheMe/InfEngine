#pragma once

#include "RenderViewContext.h"
#include "RhiSubmission.h"
#include "RhiTypes.h"

#include <cstdint>
#include <initializer_list>
#include <string>
#include <vector>

namespace infernux::rhi
{

inline constexpr uint32_t InvalidSubmissionBatchIndex = UINT32_MAX;

/// Backend-neutral work item emitted by a render-graph compiler in topological
/// order. Dependencies contain work-item ids, not vector indices.
struct SubmissionWorkItem
{
    uint32_t id = 0;
    DeviceId device = InvalidDeviceId;
    QueueRole queue = QueueRole::Graphics;
    SubmissionDomain domain = SubmissionDomain::Frame;
    RenderViewId view = InvalidRenderViewId;
    PipelineStage waitStages = PipelineStage::AllCommands;
    std::vector<uint32_t> dependencies;
    bool forceBatchBoundary = false;
    std::string diagnosticName;
};

struct SubmissionBatchDependency
{
    uint32_t sourceBatch = InvalidSubmissionBatchIndex;
    PipelineStage waitStages = PipelineStage::AllCommands;
};

/// A maximal consecutive run of work targeting one device/queue/domain/view.
/// queuePredecessor expresses serial ordering on the same native queue;
/// waitsFor contains only dependencies that require cross-queue synchronization.
struct SubmissionBatch
{
    uint32_t index = InvalidSubmissionBatchIndex;
    DeviceId device = InvalidDeviceId;
    QueueRole queue = QueueRole::Graphics;
    SubmissionDomain domain = SubmissionDomain::Frame;
    RenderViewId view = InvalidRenderViewId;
    uint32_t queuePredecessor = InvalidSubmissionBatchIndex;
    std::vector<uint32_t> workItems;
    std::vector<SubmissionBatchDependency> waitsFor;
    std::string diagnosticName;
};

struct SubmissionPlan
{
    std::vector<SubmissionBatch> batches;

    void Clear()
    {
        batches.clear();
    }
};

struct SubmissionPlanStatistics
{
    uint32_t batchCount = 0;
    uint32_t graphicsBatchCount = 0;
    uint32_t computeBatchCount = 0;
    uint32_t transferBatchCount = 0;
    uint32_t crossQueueDependencyCount = 0;
    uint32_t unorderedComputeGraphicsPairCount = 0;
};

/// Inspect dependency topology without assuming how a backend maps logical
/// queue roles onto native queues.
[[nodiscard]] SubmissionPlanStatistics AnalyzeSubmissionPlan(const SubmissionPlan &plan);

/// Result of importing one already-compiled submission plan into a larger
/// frame plan. Each source batch becomes one indivisible work item so its
/// command recording and queue-ownership barriers remain intact.
struct ComposedSubmissionRange
{
    std::vector<uint32_t> workItems;
    std::vector<uint32_t> roots;
    std::vector<uint32_t> terminals;

    [[nodiscard]] bool Empty() const noexcept
    {
        return workItems.empty();
    }
};

/// Compose independently compiled render graphs and standalone GPU work into
/// one topologically ordered frame contract. This class is backend-neutral;
/// Vulkan command recorders are associated with the returned work-item ids by
/// the backend integration layer.
class SubmissionPlanComposer
{
  public:
    void Reset(uint32_t firstWorkItemId = 1) noexcept;

    [[nodiscard]] uint32_t AddWork(DeviceId device, QueueRole queue, SubmissionDomain domain, RenderViewId view,
                                   PipelineStage waitStages, std::vector<uint32_t> dependencies = {},
                                   bool forceBatchBoundary = true, std::string diagnosticName = {});

    [[nodiscard]] ComposedSubmissionRange Append(const SubmissionPlan &source,
                                                 const std::vector<uint32_t> &externalDependencies = {});

    [[nodiscard]] bool Build(SubmissionPlan &output, std::string &error) const;

    [[nodiscard]] const std::vector<SubmissionWorkItem> &WorkItems() const noexcept
    {
        return m_workItems;
    }

  private:
    uint32_t m_nextWorkItemId = 1;
    std::vector<SubmissionWorkItem> m_workItems;
};

/// Compile topologically ordered work into queue submission batches. The
/// planner rejects forward, unknown, and implicit cross-device dependencies;
/// cross-device traffic must be represented by an explicit transfer contract.
[[nodiscard]] bool BuildSubmissionPlan(const std::vector<SubmissionWorkItem> &workItems, SubmissionPlan &output,
                                       std::string &error);

} // namespace infernux::rhi
