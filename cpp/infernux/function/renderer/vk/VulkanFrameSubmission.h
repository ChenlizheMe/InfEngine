#pragma once

#include "RenderGraph.h"

#include <function/renderer/rhi/RenderSubmissionPlan.h>

#include <array>
#include <functional>
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux::vk
{

/// Vulkan recording companion for the backend-neutral SubmissionPlanComposer.
/// Every task owns one recorder, while imported RenderGraphs retain their
/// compiler-produced batch boundaries and queue-ownership transitions.
class VulkanFrameSubmission
{
  public:
    using Recorder = std::function<bool(VkCommandBuffer)>;
    using BatchHook = std::function<bool(uint32_t, VkCommandBuffer)>;
    using GraphHook = std::function<bool(VkCommandBuffer)>;

    void Reset(uint32_t firstWorkItemId = 1);

    [[nodiscard]] uint32_t AddWork(rhi::DeviceId device, rhi::QueueRole queue, rhi::SubmissionDomain domain,
                                   rhi::RenderViewId view, rhi::PipelineStage waitStages,
                                   std::vector<uint32_t> dependencies, Recorder recorder);

    [[nodiscard]] rhi::ComposedSubmissionRange AppendRenderGraph(RenderGraph &graph,
                                                                 const std::vector<uint32_t> &externalDependencies = {},
                                                                 BatchHook beforeBatch = {}, GraphHook afterGraph = {});

    [[nodiscard]] bool Build(rhi::SubmissionPlan &output, std::string &error) const;
    [[nodiscard]] bool RecordBatch(const rhi::SubmissionPlan &plan, uint32_t batchIndex,
                                   VkCommandBuffer commandBuffer) const;

    [[nodiscard]] uint32_t LastWork(rhi::QueueRole queue) const noexcept;

  private:
    rhi::SubmissionPlanComposer m_composer;
    std::unordered_map<uint32_t, Recorder> m_recorders;
    std::array<uint32_t, static_cast<size_t>(rhi::QueueRole::Count)> m_lastWork{};
};

} // namespace infernux::vk
