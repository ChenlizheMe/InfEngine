/**
 * @file RenderGraphCompile.cpp
 * @brief RenderGraph compilation pipeline — pass culling, topological sort, resource allocation,
 *        Dynamic Rendering attachment compilation and barrier insertion.
 *
 * Part of the RenderGraph implementation (see also RenderGraph.cpp for the public API surface).
 */

#include "RenderGraph.h"
#include "RhiVulkanTypes.h"
#include "VkDeviceContext.h"
#include <SDL3/SDL.h>
#include <core/error/InxError.h>

#include <algorithm>
#include <queue>
#include <unordered_map>
#include <unordered_set>

namespace infernux
{
namespace vk
{

namespace
{

VkPipelineStageFlags ToVkPipelineStages(rhi::PipelineStage stages)
{
    VkPipelineStageFlags result = 0;
    if (rhi::HasAny(stages, rhi::PipelineStage::Top))
        result |= VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::DrawIndirect))
        result |= VK_PIPELINE_STAGE_DRAW_INDIRECT_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::VertexInput))
        result |= VK_PIPELINE_STAGE_VERTEX_INPUT_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::VertexShader))
        result |= VK_PIPELINE_STAGE_VERTEX_SHADER_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::FragmentShader))
        result |= VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::EarlyDepth))
        result |= VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::LateDepth))
        result |= VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::ColorOutput))
        result |= VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::ComputeShader))
        result |= VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::Transfer))
        result |= VK_PIPELINE_STAGE_TRANSFER_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::Bottom))
        result |= VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::Host))
        result |= VK_PIPELINE_STAGE_HOST_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::AllGraphics))
        result |= VK_PIPELINE_STAGE_ALL_GRAPHICS_BIT;
    if (rhi::HasAny(stages, rhi::PipelineStage::AllCommands))
        result |= VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
    return result;
}

bool QueueFamilySupportsStages(VkQueueFlags queueFlags, VkPipelineStageFlags stages)
{
    constexpr VkPipelineStageFlags graphicsOnly =
        VK_PIPELINE_STAGE_VERTEX_INPUT_BIT | VK_PIPELINE_STAGE_VERTEX_SHADER_BIT |
        VK_PIPELINE_STAGE_TESSELLATION_CONTROL_SHADER_BIT | VK_PIPELINE_STAGE_TESSELLATION_EVALUATION_SHADER_BIT |
        VK_PIPELINE_STAGE_GEOMETRY_SHADER_BIT | VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT |
        VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT |
        VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_ALL_GRAPHICS_BIT;
    if ((stages & graphicsOnly) != 0 && (queueFlags & VK_QUEUE_GRAPHICS_BIT) == 0)
        return false;
    if ((stages & VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT) != 0 && (queueFlags & VK_QUEUE_COMPUTE_BIT) == 0)
        return false;
    if ((stages & VK_PIPELINE_STAGE_DRAW_INDIRECT_BIT) != 0 &&
        (queueFlags & (VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT)) == 0)
        return false;
    if ((stages & VK_PIPELINE_STAGE_TRANSFER_BIT) != 0 &&
        (queueFlags & (VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT | VK_QUEUE_TRANSFER_BIT)) == 0)
        return false;
    return true;
}

VkAccessFlags ToVkAccessFlags(rhi::Access access)
{
    VkAccessFlags result = 0;
    if (rhi::HasAny(access, rhi::Access::IndirectRead))
        result |= VK_ACCESS_INDIRECT_COMMAND_READ_BIT;
    if (rhi::HasAny(access, rhi::Access::IndexRead))
        result |= VK_ACCESS_INDEX_READ_BIT;
    if (rhi::HasAny(access, rhi::Access::VertexRead))
        result |= VK_ACCESS_VERTEX_ATTRIBUTE_READ_BIT;
    if (rhi::HasAny(access, rhi::Access::UniformRead))
        result |= VK_ACCESS_UNIFORM_READ_BIT;
    if (rhi::HasAny(access, rhi::Access::ShaderRead))
        result |= VK_ACCESS_SHADER_READ_BIT;
    if (rhi::HasAny(access, rhi::Access::ShaderWrite))
        result |= VK_ACCESS_SHADER_WRITE_BIT;
    if (rhi::HasAny(access, rhi::Access::ColorRead))
        result |= VK_ACCESS_COLOR_ATTACHMENT_READ_BIT;
    if (rhi::HasAny(access, rhi::Access::ColorWrite))
        result |= VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    if (rhi::HasAny(access, rhi::Access::DepthRead))
        result |= VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_READ_BIT;
    if (rhi::HasAny(access, rhi::Access::DepthWrite))
        result |= VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
    if (rhi::HasAny(access, rhi::Access::TransferRead))
        result |= VK_ACCESS_TRANSFER_READ_BIT;
    if (rhi::HasAny(access, rhi::Access::TransferWrite))
        result |= VK_ACCESS_TRANSFER_WRITE_BIT;
    if (rhi::HasAny(access, rhi::Access::HostRead))
        result |= VK_ACCESS_HOST_READ_BIT;
    if (rhi::HasAny(access, rhi::Access::HostWrite))
        result |= VK_ACCESS_HOST_WRITE_BIT;
    if (rhi::HasAny(access, rhi::Access::MemoryRead))
        result |= VK_ACCESS_MEMORY_READ_BIT;
    if (rhi::HasAny(access, rhi::Access::MemoryWrite))
        result |= VK_ACCESS_MEMORY_WRITE_BIT;
    return result;
}

VkImageLayout ToVkImageLayout(rhi::TextureLayout layout)
{
    switch (layout) {
    case rhi::TextureLayout::General:
        return VK_IMAGE_LAYOUT_GENERAL;
    case rhi::TextureLayout::ColorAttachment:
        return VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    case rhi::TextureLayout::DepthStencilAttachment:
        return VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    case rhi::TextureLayout::DepthStencilReadOnly:
        return VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL;
    case rhi::TextureLayout::ShaderReadOnly:
        return VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    case rhi::TextureLayout::TransferSource:
        return VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    case rhi::TextureLayout::TransferDestination:
        return VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    case rhi::TextureLayout::Present:
        return VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    case rhi::TextureLayout::Automatic:
    case rhi::TextureLayout::Undefined:
    default:
        return VK_IMAGE_LAYOUT_UNDEFINED;
    }
}

} // namespace

void RenderGraph::IssuePipelineBarriers(VkCommandBuffer commandBuffer, VkPipelineStageFlags sourceStages,
                                        VkPipelineStageFlags destinationStages)
{
    if (m_barrierScratch.empty() && m_bufferBarrierScratch.empty())
        return;

    m_barrier2Scratch.clear();
    m_bufferBarrier2Scratch.clear();
    m_barrier2Scratch.reserve(m_barrierScratch.size());
    m_bufferBarrier2Scratch.reserve(m_bufferBarrierScratch.size());

    const VkPipelineStageFlags2 sourceStages2 = static_cast<VkPipelineStageFlags2>(sourceStages);
    const VkPipelineStageFlags2 destinationStages2 = static_cast<VkPipelineStageFlags2>(destinationStages);

    for (const VkBufferMemoryBarrier &barrier : m_bufferBarrierScratch) {
        VkBufferMemoryBarrier2 converted{};
        converted.sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER_2;
        converted.srcStageMask = sourceStages2;
        converted.srcAccessMask = static_cast<VkAccessFlags2>(barrier.srcAccessMask);
        converted.dstStageMask = destinationStages2;
        converted.dstAccessMask = static_cast<VkAccessFlags2>(barrier.dstAccessMask);
        converted.srcQueueFamilyIndex = barrier.srcQueueFamilyIndex;
        converted.dstQueueFamilyIndex = barrier.dstQueueFamilyIndex;
        converted.buffer = barrier.buffer;
        converted.offset = barrier.offset;
        converted.size = barrier.size;
        m_bufferBarrier2Scratch.push_back(converted);
    }

    for (const VkImageMemoryBarrier &barrier : m_barrierScratch) {
        VkImageMemoryBarrier2 converted{};
        converted.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER_2;
        converted.srcStageMask = sourceStages2;
        converted.srcAccessMask = static_cast<VkAccessFlags2>(barrier.srcAccessMask);
        converted.dstStageMask = destinationStages2;
        converted.dstAccessMask = static_cast<VkAccessFlags2>(barrier.dstAccessMask);
        converted.oldLayout = barrier.oldLayout;
        converted.newLayout = barrier.newLayout;
        converted.srcQueueFamilyIndex = barrier.srcQueueFamilyIndex;
        converted.dstQueueFamilyIndex = barrier.dstQueueFamilyIndex;
        converted.image = barrier.image;
        converted.subresourceRange = barrier.subresourceRange;
        m_barrier2Scratch.push_back(converted);
    }

    VkDependencyInfo dependency{};
    dependency.sType = VK_STRUCTURE_TYPE_DEPENDENCY_INFO;
    dependency.bufferMemoryBarrierCount = static_cast<uint32_t>(m_bufferBarrier2Scratch.size());
    dependency.pBufferMemoryBarriers = m_bufferBarrier2Scratch.data();
    dependency.imageMemoryBarrierCount = static_cast<uint32_t>(m_barrier2Scratch.size());
    dependency.pImageMemoryBarriers = m_barrier2Scratch.data();
    m_cmdPipelineBarrier2(commandBuffer, &dependency);
}

// ============================================================================
// Pass Culling & Resource Lifetimes
// ============================================================================

void RenderGraph::CullPasses()
{
    // Mark all passes as potentially culled
    for (auto &pass : m_passes) {
        pass.refCount = 0;
        pass.culled = true;
        pass.cullReason = PassCullReason::Unreachable;
    }

    // A resource version has exactly one producer. Build this once so both
    // output rooting and backward propagation use the SSA-style handle rather
    // than conflating every write to the same physical allocation.
    std::unordered_map<ResourceHandle, uint32_t, ResourceHandleHash> producers;
    for (uint32_t passId = 0; passId < m_passes.size(); ++passId) {
        for (const auto &write : m_passes[passId].writes) {
            producers.emplace(write.handle, passId);
        }
    }

    // Find the pass that writes the selected output version.
    std::queue<uint32_t> workQueue;
    auto retainRoot = [&](uint32_t passId, PassCullReason reason) {
        auto &pass = m_passes[passId];
        if (pass.culled) {
            pass.culled = false;
            pass.refCount = 1;
            pass.cullReason = reason;
            workQueue.push(passId);
        } else {
            ++pass.refCount;
            if (reason == PassCullReason::GraphOutput)
                pass.cullReason = reason;
        }
    };

    ResourceHandle root = m_output.IsValid() ? m_output : m_backbuffer;
    auto rootProducer = producers.find(root);
    if (rootProducer != producers.end())
        retainRoot(rootProducer->second, PassCullReason::GraphOutput);

    for (uint32_t passId = 0; passId < m_passes.size(); ++passId) {
        const auto &pass = m_passes[passId];
        if (pass.hasSideEffect) {
            retainRoot(passId, PassCullReason::SideEffect);
            continue;
        }
        const bool writesExternal = std::any_of(pass.writes.begin(), pass.writes.end(), [&](const auto &write) {
            return write.handle.id < m_resources.size() && m_resources[write.handle.id].isExternal;
        });
        if (writesExternal)
            retainRoot(passId, PassCullReason::ExternalWrite);
    }

    // Backward propagation
    while (!workQueue.empty()) {
        uint32_t passId = workQueue.front();
        workQueue.pop();

        const auto &pass = m_passes[passId];

        // Find the exact producer of every version this pass reads.
        for (const auto &read : pass.reads) {
            auto producer = producers.find(read.handle);
            if (producer == producers.end() || producer->second == passId)
                continue;
            auto &producerPass = m_passes[producer->second];
            if (producerPass.culled) {
                producerPass.culled = false;
                producerPass.cullReason = PassCullReason::Dependency;
                workQueue.push(producer->second);
            }
            producerPass.refCount++;
        }
    }

    // Dead-pass elimination is expected for optional branches such as motion
    // vectors when no mounted effect consumes them. Keep it available to
    // graph diagnostics without presenting normal compilation as a warning.
    for (uint32_t i = 0; i < m_passes.size(); i++) {
        if (m_passes[i].culled) {
            INXLOG_DEBUG("RenderGraph::CullPasses - Pass '", m_passes[i].name, "' (index ", i,
                         ") was culled (no path to output)");
        }
    }
}

void RenderGraph::ComputeResourceLifetimes()
{
    for (auto &resource : m_resources) {
        resource.firstPass = UINT32_MAX;
        resource.lastPass = 0;
        resource.refCount = 0;
    }

    for (uint32_t executionIndex = 0; executionIndex < m_executionOrder.size(); ++executionIndex) {
        const auto &pass = m_passes[m_executionOrder[executionIndex]];
        for (const auto &read : pass.reads) {
            auto &resource = m_resources[read.handle.id];
            resource.firstPass = std::min(resource.firstPass, executionIndex);
            resource.lastPass = std::max(resource.lastPass, executionIndex);
            resource.refCount++;
        }

        for (const auto &write : pass.writes) {
            auto &resource = m_resources[write.handle.id];
            resource.firstPass = std::min(resource.firstPass, executionIndex);
            resource.lastPass = std::max(resource.lastPass, executionIndex);
            resource.refCount++;
        }
    }
}

// ============================================================================
// Topological Sort (Kahn's Algorithm)
// ============================================================================

bool RenderGraph::TopologicalSort()
{
    m_executionOrder.clear();
    for (auto &pass : m_passes)
        pass.dependsOn.clear();

    // Collect non-culled pass indices
    std::vector<uint32_t> activePasses;
    for (uint32_t i = 0; i < static_cast<uint32_t>(m_passes.size()); i++) {
        if (!m_passes[i].culled) {
            activePasses.push_back(i);
        }
    }

    if (activePasses.empty()) {
        return true;
    }

    // Build adjacency list: edge A→B means pass A must execute before pass B
    // (A writes a resource that B reads)
    std::unordered_map<uint32_t, std::vector<uint32_t>> adjacency; // passId → [dependent passes]
    std::unordered_map<uint32_t, uint32_t> inDegree;

    for (uint32_t passId : activePasses) {
        adjacency[passId] = {};
        inDegree[passId] = 0;
    }

    std::unordered_map<ResourceHandle, uint32_t, ResourceHandleHash> producers;
    std::unordered_map<ResourceHandle, std::vector<uint32_t>, ResourceHandleHash> readers;
    for (uint32_t writePassId : activePasses) {
        const auto &writePass = m_passes[writePassId];
        for (const auto &write : writePass.writes) {
            producers.emplace(write.handle, writePassId);
        }
    }
    for (uint32_t readPassId : activePasses) {
        const auto &readPass = m_passes[readPassId];
        for (const auto &read : readPass.reads) {
            readers[read.handle].push_back(readPassId);
            auto producer = producers.find(read.handle);
            if (producer != producers.end() && producer->second != readPassId) {
                adjacency[producer->second].push_back(readPassId);
                m_passes[readPassId].dependsOn.push_back(producer->second);
            }
        }
    }

    // Multiple versions share one physical allocation. Readers of version N
    // must complete before the producer of N+1 overwrites that allocation.
    // This anti-dependency is what makes reading an older branch deterministic
    // even when the newer write was declared first.
    for (const auto &[version, versionReaders] : readers) {
        if (version.version == std::numeric_limits<uint32_t>::max())
            continue;
        ResourceHandle nextVersion = version;
        ++nextVersion.version;
        auto nextProducer = producers.find(nextVersion);
        if (nextProducer == producers.end())
            continue;
        for (uint32_t readerPassId : versionReaders) {
            if (readerPassId != nextProducer->second) {
                adjacency[readerPassId].push_back(nextProducer->second);
                m_passes[nextProducer->second].dependsOn.push_back(readerPassId);
            }
        }
    }

    for (const auto &[later, earlier] : m_explicitPassDependencies) {
        if (inDegree.find(later) == inDegree.end() || inDegree.find(earlier) == inDegree.end())
            continue;
        adjacency[earlier].push_back(later);
        m_passes[later].dependsOn.push_back(earlier);
    }

    // Deduplicate edges
    for (auto &[passId, deps] : adjacency) {
        std::sort(deps.begin(), deps.end());
        deps.erase(std::unique(deps.begin(), deps.end()), deps.end());
    }
    for (auto &pass : m_passes) {
        std::sort(pass.dependsOn.begin(), pass.dependsOn.end());
        pass.dependsOn.erase(std::unique(pass.dependsOn.begin(), pass.dependsOn.end()), pass.dependsOn.end());
    }

    // Recount in-degrees after dedup
    for (uint32_t passId : activePasses) {
        inDegree[passId] = 0;
    }
    for (const auto &[passId, deps] : adjacency) {
        for (uint32_t dep : deps) {
            inDegree[dep]++;
        }
    }

    // Kahn's algorithm — use a priority queue to break ties by pass priority
    // (lower declaration order = higher priority as tiebreaker)
    auto cmp = [](const std::pair<int, uint32_t> &a, const std::pair<int, uint32_t> &b) {
        return a.first > b.first; // min-heap on declaration order
    };
    std::priority_queue<std::pair<int, uint32_t>, std::vector<std::pair<int, uint32_t>>, decltype(cmp)> readyQueue(cmp);

    for (uint32_t passId : activePasses) {
        if (inDegree[passId] == 0) {
            readyQueue.push({static_cast<int>(passId), passId});
        }
    }

    while (!readyQueue.empty()) {
        auto [priority, passId] = readyQueue.top();
        readyQueue.pop();

        m_executionOrder.push_back(passId);

        for (uint32_t dep : adjacency[passId]) {
            inDegree[dep]--;
            if (inDegree[dep] == 0) {
                readyQueue.push({static_cast<int>(dep), dep});
            }
        }
    }

    // Check for cycles
    if (m_executionOrder.size() != activePasses.size()) {
        INXLOG_ERROR("RenderGraph::TopologicalSort - Cycle detected! Sorted ", m_executionOrder.size(), " of ",
                     activePasses.size(), " passes. Versioned graph compilation was rejected.");
        for (uint32_t passId : activePasses) {
            if (inDegree[passId] != 0) {
                INXLOG_ERROR("  unresolved pass [", passId, "] '", m_passes[passId].name, "' has remaining in-degree ",
                             inDegree[passId]);
                for (const uint32_t predecessor : m_passes[passId].dependsOn) {
                    if (predecessor < m_passes.size() && inDegree[predecessor] != 0)
                        INXLOG_ERROR("    waits on unresolved [", predecessor, "] '", m_passes[predecessor].name, "'");
                }
            }
        }
        m_executionOrder.clear();
        return false;
    }
    return true;
}

bool RenderGraph::CompileSubmissionPlan()
{
    std::vector<rhi::SubmissionWorkItem> workItems;
    workItems.reserve(m_executionOrder.size());
    for (const uint32_t passIndex : m_executionOrder) {
        if (passIndex >= m_passes.size() || m_passes[passIndex].culled)
            continue;
        const auto &pass = m_passes[passIndex];
        rhi::PipelineStage waitStages = rhi::PipelineStage::None;
        for (const auto &read : pass.reads)
            waitStages = waitStages | read.stages;
        if (waitStages == rhi::PipelineStage::None) {
            switch (pass.type) {
            case PassType::Graphics:
                waitStages = rhi::PipelineStage::AllGraphics;
                break;
            case PassType::Compute:
                waitStages = rhi::PipelineStage::ComputeShader;
                break;
            case PassType::Transfer:
                waitStages = rhi::PipelineStage::Transfer;
                break;
            case PassType::Present:
                waitStages = rhi::PipelineStage::Bottom;
                break;
            }
        }
        workItems.push_back({pass.id, pass.device, pass.queue, pass.submissionDomain, pass.view, waitStages,
                             pass.dependsOn, pass.forceSubmissionBoundary, pass.name});
    }

    std::string error;
    if (!rhi::BuildSubmissionPlan(workItems, m_submissionPlan, error)) {
        INXLOG_ERROR("RenderGraph::CompileSubmissionPlan - ", error);
        return false;
    }
    return true;
}

bool RenderGraph::CompileQueueOwnershipTransfers()
{
    m_queueOwnershipTransfers.clear();
    m_queueOwnershipTransferInfos.clear();
    m_batchOutgoingOwnershipTransfers.clear();
    m_batchOutgoingOwnershipTransfers.resize(m_submissionPlan.batches.size());
    for (auto &transfers : m_externalOutgoingOwnershipTransfers)
        transfers.clear();

    std::vector<uint32_t> passToBatch(m_passes.size(), rhi::InvalidSubmissionBatchIndex);
    for (const auto &batch : m_submissionPlan.batches) {
        for (const uint32_t passId : batch.workItems) {
            if (passId < passToBatch.size())
                passToBatch[passId] = batch.index;
        }
    }

    std::vector<ResourceState> states = m_initialResourceStates;
    states.resize(m_resources.size());

    std::vector<VkQueueFamilyProperties> queueFamilies;
    if (m_context && m_context->GetPhysicalDevice() != VK_NULL_HANDLE) {
        uint32_t familyCount = 0;
        vkGetPhysicalDeviceQueueFamilyProperties(m_context->GetPhysicalDevice(), &familyCount, nullptr);
        queueFamilies.resize(familyCount);
        vkGetPhysicalDeviceQueueFamilyProperties(m_context->GetPhysicalDevice(), &familyCount, queueFamilies.data());
    }

    const auto stagesSupportedBy = [&](uint32_t family, rhi::PipelineStage stages) {
        return family == VK_QUEUE_FAMILY_IGNORED || family >= queueFamilies.size() ||
               QueueFamilySupportsStages(queueFamilies[family].queueFlags, ToVkPipelineStages(stages));
    };

    auto bindingFor = [&](rhi::QueueRole role) -> NativeQueueBinding {
        if (role == rhi::QueueRole::Count)
            return {};
        return m_queueTopology[static_cast<size_t>(role)];
    };

    auto processAccess = [&](const RenderPassData &pass, const ResourceAccess &access, bool isWrite) -> bool {
        if ((access.usage & ResourceUsage::VersionDependency) != ResourceUsage::None ||
            access.handle.id >= m_resources.size())
            return true;
        const auto &resource = m_resources[access.handle.id];
        if (resource.type == ResourceType::RendererList)
            return true;

        const bool present = (access.usage & ResourceUsage::Present) != ResourceUsage::None;
        const rhi::QueueRole targetQueue = present ? rhi::QueueRole::Present : pass.queue;
        const NativeQueueBinding targetBinding = bindingFor(targetQueue);
        if (!targetBinding.IsValid()) {
            INXLOG_ERROR("RenderGraph::CompileQueueOwnershipTransfers - Pass '", pass.name,
                         "' targets an unavailable native queue");
            return false;
        }
        ResourceState &state = states[access.handle.id];
        const bool hasPreviousOwner =
            state.queueFamily != VK_QUEUE_FAMILY_IGNORED && state.nativeQueueLane != UINT32_MAX;
        const bool laneChange = hasPreviousOwner && state.nativeQueueLane != targetBinding.lane;
        const bool familyChange = hasPreviousOwner && state.queueFamily != targetBinding.family;
        if (hasPreviousOwner && state.writerPassId == UINT32_MAX &&
            !stagesSupportedBy(state.queueFamily, state.stages)) {
            INXLOG_ERROR("RenderGraph::CompileQueueOwnershipTransfers - Resource '", resource.name,
                         "' has an initial pipeline stage unsupported by its owner queue family");
            return false;
        }

        const uint32_t sourceBatch = state.writerPassId < passToBatch.size() ? passToBatch[state.writerPassId]
                                                                             : rhi::InvalidSubmissionBatchIndex;
        const uint32_t targetBatch =
            pass.id < passToBatch.size() ? passToBatch[pass.id] : rhi::InvalidSubmissionBatchIndex;
        if (!present && laneChange && sourceBatch != rhi::InvalidSubmissionBatchIndex &&
            targetBatch != rhi::InvalidSubmissionBatchIndex && sourceBatch < targetBatch) {
            auto &waits = m_submissionPlan.batches[targetBatch].waitsFor;
            const auto existing = std::find_if(waits.begin(), waits.end(),
                                               [&](const auto &wait) { return wait.sourceBatch == sourceBatch; });
            if (existing == waits.end())
                waits.push_back({sourceBatch, access.stages});
            else
                existing->waitStages = existing->waitStages | access.stages;
        }

        // PresentRead is itself the release operation recorded on Graphics;
        // vkQueuePresentKHR and renderFinished form the consumer side.
        if (!present && familyChange && !resource.concurrentQueueSharing) {
            if (targetBatch == rhi::InvalidSubmissionBatchIndex) {
                INXLOG_ERROR("RenderGraph::CompileQueueOwnershipTransfers - Missing target batch for pass '", pass.name,
                             "'");
                return false;
            }

            QueueOwnershipTransfer transfer;
            transfer.info.resourceId = access.handle.id;
            transfer.info.sourcePass = state.writerPassId;
            transfer.info.targetPass = pass.id;
            transfer.info.sourceBatch = sourceBatch;
            transfer.info.targetBatch = targetBatch;
            transfer.info.sourceFamily = state.queueFamily;
            transfer.info.targetFamily = targetBinding.family;
            transfer.sourceState = state;
            transfer.targetAccess = access;

            const uint32_t transferIndex = static_cast<uint32_t>(m_queueOwnershipTransfers.size());
            m_queueOwnershipTransfers.push_back(transfer);
            m_queueOwnershipTransferInfos.push_back(transfer.info);
            if (sourceBatch != rhi::InvalidSubmissionBatchIndex) {
                if (sourceBatch >= m_batchOutgoingOwnershipTransfers.size() || sourceBatch >= targetBatch) {
                    INXLOG_ERROR("RenderGraph::CompileQueueOwnershipTransfers - Invalid ownership order for resource '",
                                 resource.name, "'");
                    return false;
                }
                m_batchOutgoingOwnershipTransfers[sourceBatch].push_back(transferIndex);
            } else if (transfer.sourceState.queue != rhi::QueueRole::Count) {
                m_externalOutgoingOwnershipTransfers[static_cast<size_t>(transfer.sourceState.queue)].push_back(
                    transferIndex);
            }
        }

        state.layout = access.layout;
        state.accessMask = access.access;
        state.stages = access.stages;
        state.writerPassId = pass.id;
        state.queue = targetQueue;
        state.queueFamily = targetBinding.family;
        state.nativeQueueLane = targetBinding.lane;

        return true;
    };

    for (const uint32_t passId : m_executionOrder) {
        if (passId >= m_passes.size() || m_passes[passId].culled)
            continue;
        const auto &pass = m_passes[passId];
        for (const auto &read : pass.reads) {
            if (!processAccess(pass, read, false))
                return false;
        }
        for (const auto &write : pass.writes) {
            if (!processAccess(pass, write, true))
                return false;
        }
        if (pass.depthInput.IsValid() && !pass.depthOutput.IsValid() && pass.depthInput.id < states.size()) {
            ResourceState &state = states[pass.depthInput.id];
            state.layout = rhi::TextureLayout::DepthStencilReadOnly;
            state.accessMask = rhi::Access::DepthRead;
            state.stages = rhi::PipelineStage::EarlyDepth | rhi::PipelineStage::LateDepth;
        }
    }
    return true;
}

std::vector<uint64_t> RenderGraph::BuildStructuralSignature() const
{
    std::vector<uint64_t> signature;
    signature.reserve(16 + m_resources.size() * 16 + m_passes.size() * 24);

    auto appendString = [&](const std::string &value) {
        signature.push_back(value.size());
        uint64_t chunk = 0;
        uint32_t shift = 0;
        for (const unsigned char byte : value) {
            chunk |= static_cast<uint64_t>(byte) << shift;
            shift += 8;
            if (shift == 64) {
                signature.push_back(chunk);
                chunk = 0;
                shift = 0;
            }
        }
        if (shift != 0)
            signature.push_back(chunk);
    };
    auto appendHandle = [&](ResourceHandle handle) {
        signature.push_back(handle.IsValid() ? handle.id : UINT32_MAX);
        signature.push_back(handle.IsValid() ? handle.version : UINT32_MAX);
    };
    auto appendAccess = [&](const ResourceAccess &resourceAccess) {
        appendHandle(resourceAccess.handle);
        signature.push_back(static_cast<uint64_t>(resourceAccess.usage));
        signature.push_back(static_cast<uint64_t>(resourceAccess.stages));
        signature.push_back(static_cast<uint64_t>(resourceAccess.access));
        signature.push_back(static_cast<uint64_t>(resourceAccess.layout));
    };

    signature.push_back(0x494e585247535452ull); // "INXRGSTR"
    signature.push_back(m_resources.size());
    signature.push_back(m_passes.size());
    appendHandle(m_backbuffer);
    appendHandle(m_output);

    for (size_t index = 0; index < m_resources.size(); ++index) {
        const auto &resource = m_resources[index];
        signature.push_back(0x52534f5552434500ull); // "RSOURCE"
        appendString(resource.name);
        signature.push_back(static_cast<uint64_t>(resource.type));
        signature.push_back(resource.isExternal);
        signature.push_back(index < m_resourceVersions.size() ? m_resourceVersions[index] : 0);

        appendString(resource.textureDesc.name);
        signature.push_back(resource.textureDesc.width);
        signature.push_back(resource.textureDesc.height);
        signature.push_back(resource.textureDesc.depth);
        signature.push_back(resource.textureDesc.mipLevels);
        signature.push_back(resource.textureDesc.arrayLayers);
        signature.push_back(static_cast<uint64_t>(resource.textureDesc.format));
        signature.push_back(static_cast<uint64_t>(resource.textureDesc.samples));
        signature.push_back(resource.textureDesc.isTransient);

        appendString(resource.bufferDesc.name);
        signature.push_back(resource.bufferDesc.size);
        signature.push_back(resource.bufferDesc.usage);
        signature.push_back(resource.bufferDesc.isTransient);
    }

    for (const auto &pass : m_passes) {
        signature.push_back(0x5041535300000000ull); // "PASS"
        appendString(pass.name);
        signature.push_back(pass.id);
        signature.push_back(static_cast<uint64_t>(pass.type));
        signature.push_back(pass.device);
        signature.push_back(static_cast<uint64_t>(pass.queue));
        signature.push_back(static_cast<uint64_t>(pass.submissionDomain));
        signature.push_back(pass.forceSubmissionBoundary ? 1u : 0u);
        signature.push_back(pass.view);
        signature.push_back(pass.hasSideEffect);
        signature.push_back(pass.renderArea.width);
        signature.push_back(pass.renderArea.height);
        signature.push_back(pass.clearColorEnabled);
        signature.push_back(pass.clearDepthEnabled);
        signature.push_back(pass.hasResolveAttachment);
        signature.push_back(pass.skipCallbackWhenRendererListsEmpty);

        signature.push_back(pass.reads.size());
        for (const auto &read : pass.reads)
            appendAccess(read);
        signature.push_back(pass.writes.size());
        for (const auto &write : pass.writes)
            appendAccess(write);

        signature.push_back(pass.colorOutputs.size());
        for (const auto output : pass.colorOutputs)
            appendHandle(output);
        appendHandle(pass.depthOutput);
        appendHandle(pass.depthInput);
        appendHandle(pass.resolveOutput);
    }

    signature.push_back(0x4558504445505300ull); // "EXPDPS"
    signature.push_back(m_explicitPassDependencies.size());
    for (const auto &[later, earlier] : m_explicitPassDependencies) {
        signature.push_back(later);
        signature.push_back(earlier);
    }

    return signature;
}

bool RenderGraph::RestoreStructuralCompilation(const std::vector<uint64_t> &signature)
{
    const auto found = std::find_if(m_structuralCompileCache.begin(), m_structuralCompileCache.end(),
                                    [&](const auto &entry) { return entry.signature == signature; });
    if (found == m_structuralCompileCache.end() || found->passes.size() != m_passes.size() ||
        found->resources.size() != m_resources.size()) {
        ++m_structuralCacheMisses;
        return false;
    }

    for (size_t index = 0; index < m_passes.size(); ++index) {
        m_passes[index].refCount = found->passes[index].refCount;
        m_passes[index].culled = found->passes[index].culled;
        m_passes[index].cullReason = found->passes[index].cullReason;
        m_passes[index].dependsOn = found->passes[index].dependencies;
    }
    for (size_t index = 0; index < m_resources.size(); ++index) {
        m_resources[index].firstPass = found->resources[index].firstPass;
        m_resources[index].lastPass = found->resources[index].lastPass;
        m_resources[index].refCount = found->resources[index].refCount;
    }
    m_executionOrder = found->executionOrder;
    ++m_structuralCacheHits;
    return true;
}

void RenderGraph::StoreStructuralCompilation(std::vector<uint64_t> signature)
{
    StructuralCompileCacheEntry entry;
    entry.signature = std::move(signature);
    entry.executionOrder = m_executionOrder;
    entry.passes.reserve(m_passes.size());
    for (const auto &pass : m_passes)
        entry.passes.push_back({pass.refCount, pass.culled, pass.cullReason, pass.dependsOn});
    entry.resources.reserve(m_resources.size());
    for (const auto &resource : m_resources)
        entry.resources.push_back({resource.firstPass, resource.lastPass, resource.refCount});

    if (m_structuralCompileCache.size() == kStructuralCacheCapacity)
        m_structuralCompileCache.erase(m_structuralCompileCache.begin());
    m_structuralCompileCache.push_back(std::move(entry));
}

// ============================================================================
// Helper Functions
// ============================================================================

rhi::TextureLayout RenderGraph::UsageToLayout(ResourceUsage usage, ResourceType type)
{
    if (static_cast<int>(usage & ResourceUsage::ColorOutput) != 0)
        return rhi::TextureLayout::ColorAttachment;
    if (static_cast<int>(usage & ResourceUsage::DepthOutput) != 0)
        return rhi::TextureLayout::DepthStencilAttachment;
    if (static_cast<int>(usage & ResourceUsage::DepthRead) != 0)
        return rhi::TextureLayout::DepthStencilAttachment;
    if (static_cast<int>(usage & ResourceUsage::ShaderRead) != 0)
        return rhi::TextureLayout::ShaderReadOnly;
    if (static_cast<int>(usage & ResourceUsage::Transfer) != 0)
        return rhi::TextureLayout::TransferSource;
    if (static_cast<int>(usage & ResourceUsage::Storage) != 0)
        return rhi::TextureLayout::General;
    if (static_cast<int>(usage & ResourceUsage::ReadWrite) != 0)
        return rhi::TextureLayout::General;
    return rhi::TextureLayout::Undefined;
}

rhi::Access RenderGraph::UsageToAccessMask(ResourceUsage usage)
{
    rhi::Access flags = rhi::Access::None;
    if (static_cast<int>(usage & ResourceUsage::ColorOutput) != 0)
        flags = flags | rhi::Access::ColorWrite;
    if (static_cast<int>(usage & ResourceUsage::DepthOutput) != 0)
        flags = flags | rhi::Access::DepthWrite;
    if (static_cast<int>(usage & ResourceUsage::DepthRead) != 0)
        flags = flags | rhi::Access::DepthRead;
    if (static_cast<int>(usage & ResourceUsage::ShaderRead) != 0)
        flags = flags | rhi::Access::ShaderRead;
    if (static_cast<int>(usage & ResourceUsage::Transfer) != 0)
        flags = flags | rhi::Access::TransferRead;
    if (static_cast<int>(usage & ResourceUsage::IndirectArgument) != 0)
        flags = flags | rhi::Access::IndirectRead;
    if (static_cast<int>(usage & ResourceUsage::Storage) != 0)
        flags = flags | rhi::Access::ShaderRead | rhi::Access::ShaderWrite;
    if (static_cast<int>(usage & (ResourceUsage::ReadWrite)) != 0)
        flags = flags | rhi::Access::ShaderRead | rhi::Access::ShaderWrite;
    if (static_cast<int>(usage & ResourceUsage::Read) != 0 && static_cast<int>(usage & ResourceUsage::Write) == 0 &&
        flags == rhi::Access::None)
        flags = rhi::Access::ShaderRead;
    return flags;
}

rhi::PipelineStage RenderGraph::UsageToStageFlags(ResourceUsage usage)
{
    rhi::PipelineStage flags = rhi::PipelineStage::None;
    if (static_cast<int>(usage & ResourceUsage::ColorOutput) != 0)
        flags = flags | rhi::PipelineStage::ColorOutput;
    if (static_cast<int>(usage & ResourceUsage::DepthOutput) != 0)
        flags = flags | rhi::PipelineStage::EarlyDepth | rhi::PipelineStage::LateDepth;
    if (static_cast<int>(usage & ResourceUsage::DepthRead) != 0)
        flags = flags | rhi::PipelineStage::EarlyDepth | rhi::PipelineStage::LateDepth;
    if (static_cast<int>(usage & ResourceUsage::ShaderRead) != 0)
        flags = flags | rhi::PipelineStage::FragmentShader;
    if (static_cast<int>(usage & ResourceUsage::Transfer) != 0)
        flags = flags | rhi::PipelineStage::Transfer;
    if (static_cast<int>(usage & ResourceUsage::IndirectArgument) != 0)
        flags = flags | rhi::PipelineStage::DrawIndirect;
    if (static_cast<int>(usage & ResourceUsage::Storage) != 0)
        flags = flags | rhi::PipelineStage::ComputeShader;
    if (flags == rhi::PipelineStage::None)
        flags = rhi::PipelineStage::AllGraphics;
    return flags;
}

ResourceHandle RenderGraph::GetEffectiveDepth(const RenderPassData &pass)
{
    if (pass.depthOutput.IsValid())
        return pass.depthOutput;
    return pass.depthInput;
}

bool RenderGraph::IsResourceUsedAfter(uint32_t resourceId, uint32_t passIndex) const
{
    if (resourceId >= m_resources.size())
        return false;
    // Check execution order: is resource referenced by any pass after passIndex?
    bool foundCurrent = false;
    for (uint32_t idx : m_executionOrder) {
        if (idx == passIndex) {
            foundCurrent = true;
            continue;
        }
        if (!foundCurrent)
            continue;

        const auto &pass = m_passes[idx];
        for (const auto &read : pass.reads) {
            if (read.handle.id == resourceId)
                return true;
        }
        for (const auto &write : pass.writes) {
            if (write.handle.id == resourceId)
                return true;
        }
        if (pass.depthInput.IsValid() && pass.depthInput.id == resourceId)
            return true;
    }
    return false;
}

// ============================================================================
// Resource Allocation (with Memory Aliasing)
// ============================================================================

bool RenderGraph::AllocateResources()
{
    if (!m_context) {
        return false;
    }

    VkDevice device = m_context->GetDevice();
    VkPhysicalDevice physDevice = m_context->GetPhysicalDevice();

    // ========================================================================
    // Create VkImages/VkBuffers and gather memory requirements
    // ========================================================================

    struct AllocationRequest
    {
        uint32_t resourceIndex;
        VkMemoryRequirements memReqs;
        uint32_t memoryTypeIndex;
    };

    std::vector<AllocationRequest> imageAllocRequests;
    std::vector<AllocationRequest> bufferAllocRequests;

    for (uint32_t ri = 0; ri < static_cast<uint32_t>(m_resources.size()); ++ri) {
        auto &resource = m_resources[ri];

        // Skip external, unreferenced, or already-allocated resources
        if (resource.isExternal || resource.refCount == 0)
            continue;
        if (resource.allocatedImage != VK_NULL_HANDLE || resource.allocatedBuffer != VK_NULL_HANDLE)
            continue;

        if (resource.type == ResourceType::Texture2D || resource.type == ResourceType::DepthStencil) {
            VkImageCreateInfo imageInfo{};
            imageInfo.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
            imageInfo.imageType = VK_IMAGE_TYPE_2D;
            imageInfo.format = resource.textureDesc.format;
            imageInfo.extent.width = resource.textureDesc.width;
            imageInfo.extent.height = resource.textureDesc.height;
            imageInfo.extent.depth = 1;
            imageInfo.mipLevels = resource.textureDesc.mipLevels;
            imageInfo.arrayLayers = resource.textureDesc.arrayLayers;
            imageInfo.samples = resource.textureDesc.samples;
            imageInfo.tiling = VK_IMAGE_TILING_OPTIMAL;
            // Keep render-graph images independently backed. The previous
            // interval allocator bound different transient images to the same
            // memory without emitting aliasing dependencies when ownership
            // moved from one image to the next. That is undefined on Vulkan
            // and is especially visible as stale/random tiles on mobile GPUs.

            if (resource.type == ResourceType::DepthStencil) {
                imageInfo.usage = VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;
                // If any pass reads this depth as a shader input (e.g. SSAO
                // sampling the scene depth via sampler2D), enable SAMPLED_BIT.
                for (const auto &p : m_passes) {
                    if (p.culled)
                        continue;
                    for (const auto &acc : p.reads) {
                        if (acc.handle.id == ri && (static_cast<int>(acc.usage & ResourceUsage::ShaderRead) != 0)) {
                            imageInfo.usage |= VK_IMAGE_USAGE_SAMPLED_BIT;
                        }
                    }
                }
            } else {
                // Detect depth-formatted textures registered as Texture2D
                // (e.g. shadow maps pre-registered via RegisterTransientTexture).
                bool isDepthFormat = (resource.textureDesc.format == VK_FORMAT_D32_SFLOAT ||
                                      resource.textureDesc.format == VK_FORMAT_D24_UNORM_S8_UINT ||
                                      resource.textureDesc.format == VK_FORMAT_D16_UNORM ||
                                      resource.textureDesc.format == VK_FORMAT_D32_SFLOAT_S8_UINT);
                if (isDepthFormat) {
                    imageInfo.usage = VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT;
                } else {
                    imageInfo.usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT;
                }
            }

            for (const auto &pass : m_passes) {
                if (pass.culled)
                    continue;
                const auto usesStorage = [ri](const ResourceAccess &access) {
                    return access.handle.id == ri && static_cast<int>(access.usage & ResourceUsage::Storage) != 0;
                };
                if (std::any_of(pass.reads.begin(), pass.reads.end(), usesStorage) ||
                    std::any_of(pass.writes.begin(), pass.writes.end(), usesStorage)) {
                    imageInfo.usage |= VK_IMAGE_USAGE_STORAGE_BIT;
                    break;
                }
            }

            // Add transfer usage flags if any pass uses this resource for
            // transfer operations (blit/copy source or destination).
            for (const auto &pass : m_passes) {
                if (pass.culled)
                    continue;
                for (const auto &acc : pass.reads) {
                    if (acc.handle.id == ri && (static_cast<int>(acc.usage & ResourceUsage::Transfer) != 0))
                        imageInfo.usage |= VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
                }
                for (const auto &acc : pass.writes) {
                    if (acc.handle.id == ri && (static_cast<int>(acc.usage & ResourceUsage::Transfer) != 0))
                        imageInfo.usage |= VK_IMAGE_USAGE_TRANSFER_DST_BIT;
                }
            }

            imageInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
            imageInfo.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;

            if (vkCreateImage(device, &imageInfo, nullptr, &resource.allocatedImage) != VK_SUCCESS) {
                INXLOG_ERROR("Failed to create image for resource: ", resource.name);
                return false;
            }

            VkMemoryRequirements memReqs;
            vkGetImageMemoryRequirements(device, resource.allocatedImage, &memReqs);

            // Use VMA to find the memory type index for aliasing grouping
            VmaAllocationCreateInfo probeAllocInfo{};
            probeAllocInfo.usage = VMA_MEMORY_USAGE_GPU_ONLY;
            uint32_t memTypeIndex = 0;
            vmaFindMemoryTypeIndexForImageInfo(m_context->GetVmaAllocator(), &imageInfo, &probeAllocInfo,
                                               &memTypeIndex);

            imageAllocRequests.push_back({ri, memReqs, memTypeIndex});

        } else if (resource.type == ResourceType::Buffer) {
            VkBufferCreateInfo bufferInfo{};
            bufferInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
            bufferInfo.size = resource.bufferDesc.size;
            bufferInfo.usage = resource.bufferDesc.usage;
            bufferInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;

            if (vkCreateBuffer(device, &bufferInfo, nullptr, &resource.allocatedBuffer) != VK_SUCCESS) {
                resource.allocatedBuffer = VK_NULL_HANDLE;
                INXLOG_ERROR("Failed to create buffer resource '", resource.name, "'");
                return false;
            }

            VkMemoryRequirements memReqs{};
            vkGetBufferMemoryRequirements(device, resource.allocatedBuffer, &memReqs);

            VmaAllocationCreateInfo probeAllocInfo{};
            probeAllocInfo.usage = VMA_MEMORY_USAGE_GPU_ONLY;
            uint32_t memTypeIndex = 0;
            if (vmaFindMemoryTypeIndexForBufferInfo(m_context->GetVmaAllocator(), &bufferInfo, &probeAllocInfo,
                                                    &memTypeIndex) != VK_SUCCESS) {
                INXLOG_ERROR("Failed to select memory for buffer resource '", resource.name, "'");
                return false;
            }
            bufferAllocRequests.push_back({ri, memReqs, memTypeIndex});
        }
    }

    // Allocate every graph image independently. Reintroducing image-memory
    // aliasing requires an explicit alias set in the compiled graph plus a
    // full execution and memory dependency at every alias hand-off; lifetime
    // interval overlap alone is not a sufficient Vulkan synchronization
    // contract.
    for (auto &req : imageAllocRequests) {
        auto &resource = m_resources[req.resourceIndex];
        VmaAllocator allocator = m_context->GetVmaAllocator();
        VmaAllocationCreateInfo allocCreateInfo{};
        // vmaAllocateMemory (raw) cannot infer the memory type from an image
        // create descriptor, so select device-local memory explicitly.
        allocCreateInfo.usage = VMA_MEMORY_USAGE_GPU_ONLY;
        allocCreateInfo.flags = VMA_ALLOCATION_CREATE_DEDICATED_MEMORY_BIT;

        VmaAllocation allocation = VK_NULL_HANDLE;
        VmaAllocationInfo vmaAllocInfo{};
        if (vmaAllocateMemory(allocator, &req.memReqs, &allocCreateInfo, &allocation, &vmaAllocInfo) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to allocate memory for resource: ", resource.name);
            return false;
        }
        if (vkBindImageMemory(device, resource.allocatedImage, vmaAllocInfo.deviceMemory, 0) != VK_SUCCESS) {
            vmaFreeMemory(allocator, allocation);
            INXLOG_ERROR("Failed to bind image memory for resource: ", resource.name);
            return false;
        }
        resource.allocatedMemory = allocation;

        // Create image view (regardless of aliasing)
        VkImageViewCreateInfo viewInfo{};
        viewInfo.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        viewInfo.image = resource.allocatedImage;
        viewInfo.viewType = VK_IMAGE_VIEW_TYPE_2D;
        viewInfo.format = resource.textureDesc.format;

        viewInfo.subresourceRange.aspectMask = rhi::ToVkImageAspectMask(resource.textureDesc.format);

        viewInfo.subresourceRange.baseMipLevel = 0;
        viewInfo.subresourceRange.levelCount = resource.textureDesc.mipLevels;
        viewInfo.subresourceRange.baseArrayLayer = 0;
        viewInfo.subresourceRange.layerCount = resource.textureDesc.arrayLayers;

        if (vkCreateImageView(device, &viewInfo, nullptr, &resource.allocatedView) != VK_SUCCESS) {
            INXLOG_ERROR("Failed to create image view for resource: ", resource.name);
            return false;
        }
        resource.rhiView =
            m_rhiDevice ? m_rhiDevice->RegisterTextureView(resource.allocatedView) : rhi::TextureViewHandle{};
        resource.rhiTexture =
            m_rhiDevice ? m_rhiDevice->RegisterTexture(resource.allocatedImage) : rhi::TextureHandle{};
    }

    // Transient buffers retain interval-based allocation reuse. Unlike tiled
    // images they have no image-layout metadata, and the graph already emits
    // the storage/indirect access barriers represented by their passes.
    std::sort(bufferAllocRequests.begin(), bufferAllocRequests.end(),
              [](const AllocationRequest &a, const AllocationRequest &b) { return a.memReqs.size > b.memReqs.size; });

    const auto lifetimesOverlap = [](uint32_t aFirst, uint32_t aLast, uint32_t bFirst, uint32_t bLast) {
        return aFirst <= bLast && bFirst <= aLast;
    };

    struct BufferMemoryHeap
    {
        VmaAllocation allocation = VK_NULL_HANDLE;
        VkDeviceMemory memory = VK_NULL_HANDLE;
        VkDeviceSize size = 0;
        uint32_t memoryTypeIndex = 0;
        std::vector<std::pair<uint32_t, uint32_t>> occupants;
    };
    std::vector<BufferMemoryHeap> bufferHeaps;

    for (const auto &req : bufferAllocRequests) {
        auto &resource = m_resources[req.resourceIndex];
        bool placed = false;

        if (resource.bufferDesc.isTransient && resource.firstPass <= resource.lastPass) {
            for (auto &heap : bufferHeaps) {
                if (heap.memoryTypeIndex != req.memoryTypeIndex || req.memReqs.size > heap.size)
                    continue;

                const bool overlaps = std::any_of(heap.occupants.begin(), heap.occupants.end(), [&](const auto &life) {
                    return lifetimesOverlap(resource.firstPass, resource.lastPass, life.first, life.second);
                });
                if (overlaps)
                    continue;

                if (vkBindBufferMemory(device, resource.allocatedBuffer, heap.memory, 0) != VK_SUCCESS)
                    continue;

                resource.allocatedMemory = VK_NULL_HANDLE;
                heap.occupants.push_back({resource.firstPass, resource.lastPass});
                placed = true;
                break;
            }
        }

        if (!placed) {
            VmaAllocationCreateInfo allocCreateInfo{};
            allocCreateInfo.usage = VMA_MEMORY_USAGE_GPU_ONLY;
            allocCreateInfo.flags = VMA_ALLOCATION_CREATE_DEDICATED_MEMORY_BIT;

            VmaAllocation allocation = VK_NULL_HANDLE;
            VmaAllocationInfo allocationInfo{};
            if (vmaAllocateMemory(m_context->GetVmaAllocator(), &req.memReqs, &allocCreateInfo, &allocation,
                                  &allocationInfo) != VK_SUCCESS) {
                INXLOG_ERROR("Failed to allocate memory for buffer resource: ", resource.name);
                return false;
            }
            if (vkBindBufferMemory(device, resource.allocatedBuffer, allocationInfo.deviceMemory, 0) != VK_SUCCESS) {
                vmaFreeMemory(m_context->GetVmaAllocator(), allocation);
                INXLOG_ERROR("Failed to bind memory for buffer resource: ", resource.name);
                return false;
            }

            resource.allocatedMemory = allocation;
            if (resource.bufferDesc.isTransient && resource.firstPass <= resource.lastPass) {
                BufferMemoryHeap heap;
                heap.allocation = allocation;
                heap.memory = allocationInfo.deviceMemory;
                heap.size = req.memReqs.size;
                heap.memoryTypeIndex = req.memoryTypeIndex;
                heap.occupants.push_back({resource.firstPass, resource.lastPass});
                bufferHeaps.push_back(std::move(heap));
            }
        }

        resource.rhiBuffer = m_rhiDevice
                                 ? m_rhiDevice->RegisterBuffer(resource.allocatedBuffer, resource.bufferDesc.size)
                                 : rhi::BufferHandle{};
    }

    return true;
}

// ============================================================================
// Dynamic Rendering Attachment Compilation
// ============================================================================

bool RenderGraph::CompileGraphicsAttachments()
{
    for (auto &pass : m_passes) {
        if (pass.culled || pass.type != PassType::Graphics) {
            continue;
        }

        // Determine effective depth (write takes priority over read-only)
        ResourceHandle effectiveDepth = GetEffectiveDepth(pass);

        // Skip if no outputs at all
        if (pass.colorOutputs.empty() && !effectiveDepth.IsValid()) {
            continue;
        }

        const bool hasColorOutputs = !pass.colorOutputs.empty();
        VkSampleCountFlagBits sampleCount = VK_SAMPLE_COUNT_1_BIT;
        if (hasColorOutputs && pass.colorOutputs[0].IsValid()) {
            const auto &resource = m_resources[pass.colorOutputs[0].id];
            sampleCount = resource.textureDesc.samples;
        }

        if (effectiveDepth.IsValid()) {
            const auto &resource = m_resources[effectiveDepth.id];
            if (!hasColorOutputs)
                sampleCount = resource.textureDesc.samples;
        }

        pass.hasResolveAttachment = false;
        if (pass.resolveOutput.IsValid()) {
            if (pass.colorOutputs.size() != 1 || !pass.colorOutputs[0].IsValid() ||
                pass.resolveOutput.id >= m_resources.size()) {
                INXLOG_ERROR("RenderGraph pass '", pass.name,
                             "' requires exactly one valid color attachment for MSAA resolve");
                return false;
            }
            const auto &colorResource = m_resources[pass.colorOutputs[0].id];
            const auto &resolveResource = m_resources[pass.resolveOutput.id];
            if (colorResource.textureDesc.samples == VK_SAMPLE_COUNT_1_BIT ||
                resolveResource.textureDesc.samples != VK_SAMPLE_COUNT_1_BIT ||
                colorResource.textureDesc.format != resolveResource.textureDesc.format ||
                colorResource.textureDesc.width != resolveResource.textureDesc.width ||
                colorResource.textureDesc.height != resolveResource.textureDesc.height) {
                INXLOG_ERROR("RenderGraph pass '", pass.name,
                             "' has an incompatible Dynamic Rendering resolve attachment");
                return false;
            }
            pass.hasResolveAttachment = true;
        }

        // Publish the attachment contract used by Dynamic Rendering pipelines.
        pass.renderingSignature = {};
        auto &signature = pass.renderingSignature;
        signature.samples = rhi::FromVkSampleCount(sampleCount);
        signature.colorFormatCount = static_cast<uint32_t>(pass.colorOutputs.size());
        if (signature.colorFormatCount > signature.colorFormats.size()) {
            INXLOG_ERROR("RenderGraph pass '", pass.name, "' exceeds the color attachment limit");
            return false;
        }
        for (uint32_t index = 0; index < signature.colorFormatCount; ++index) {
            const ResourceHandle output = pass.colorOutputs[index];
            if (!output.IsValid() || output.id >= m_resources.size()) {
                INXLOG_ERROR("RenderGraph pass '", pass.name, "' has an invalid color attachment");
                return false;
            }
            const auto &resource = m_resources[output.id];
            if (resource.textureDesc.samples != sampleCount) {
                INXLOG_ERROR("RenderGraph pass '", pass.name, "' mixes color attachment sample counts");
                return false;
            }
            signature.colorFormats[index] = rhi::FromVkFormat(resource.textureDesc.format);
        }
        if (effectiveDepth.IsValid()) {
            const auto &resource = m_resources[effectiveDepth.id];
            if (resource.textureDesc.samples != sampleCount) {
                INXLOG_ERROR("RenderGraph pass '", pass.name, "' mixes color and depth sample counts");
                return false;
            }
            signature.depthFormat = rhi::FromVkFormat(resource.textureDesc.format);
            if (rhi::IsStencilFormat(signature.depthFormat))
                signature.stencilFormat = signature.depthFormat;
        }
        if (!signature.IsValid()) {
            INXLOG_ERROR("RenderGraph pass '", pass.name, "' produced an invalid attachment signature");
            return false;
        }

        if (!m_cmdBeginRendering || !m_cmdEndRendering) {
            INXLOG_ERROR("RenderGraph cannot compile graphics pass '", pass.name,
                         "' without Vulkan Dynamic Rendering commands");
            return false;
        }
    }

    return true;
}

// ============================================================================
// Pre-compute per-pass Execute data (called once at end of Compile)
// ============================================================================

void RenderGraph::PrecomputeExecuteData()
{
    for (auto &pass : m_passes) {
        if (pass.culled)
            continue;

        if (pass.type != PassType::Graphics || !pass.renderingSignature.IsValid())
            continue;

        pass.cachedViewport.x = 0.0f;
        pass.cachedViewport.y = 0.0f;
        pass.cachedViewport.width = static_cast<float>(pass.renderArea.width);
        pass.cachedViewport.height = static_cast<float>(pass.renderArea.height);
        pass.cachedViewport.minDepth = 0.0f;
        pass.cachedViewport.maxDepth = 1.0f;
        pass.cachedScissor.offset = {0, 0};
        pass.cachedScissor.extent = pass.renderArea;

        uint32_t colorCount = 0;
        for (const ResourceHandle output : pass.colorOutputs) {
            if (!output.IsValid() || output.id >= m_resources.size() ||
                colorCount >= pass.cachedRenderingColorAttachments.size())
                continue;
            const auto &resource = m_resources[output.id];
            VkRenderingAttachmentInfo &attachment = pass.cachedRenderingColorAttachments[colorCount++];
            attachment = {};
            attachment.sType = VK_STRUCTURE_TYPE_RENDERING_ATTACHMENT_INFO;
            attachment.imageView = resource.isExternal ? resource.externalView : resource.allocatedView;
            attachment.imageLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
            attachment.loadOp = pass.clearColorEnabled ? VK_ATTACHMENT_LOAD_OP_CLEAR : VK_ATTACHMENT_LOAD_OP_LOAD;
            attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
            attachment.clearValue.color = pass.clearColor;
            if (colorCount == 1 && pass.hasResolveAttachment && pass.resolveOutput.IsValid() &&
                pass.resolveOutput.id < m_resources.size()) {
                const auto &resolve = m_resources[pass.resolveOutput.id];
                attachment.resolveMode = rhi::IsIntegerFormat(rhi::FromVkFormat(resource.textureDesc.format))
                                             ? VK_RESOLVE_MODE_SAMPLE_ZERO_BIT
                                             : VK_RESOLVE_MODE_AVERAGE_BIT;
                attachment.resolveImageView = resolve.isExternal ? resolve.externalView : resolve.allocatedView;
                attachment.resolveImageLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
            }
        }

        pass.cachedRenderingDepthAttachment = {};
        const ResourceHandle depth = GetEffectiveDepth(pass);
        if (depth.IsValid() && depth.id < m_resources.size()) {
            const auto &resource = m_resources[depth.id];
            auto &attachment = pass.cachedRenderingDepthAttachment;
            attachment.sType = VK_STRUCTURE_TYPE_RENDERING_ATTACHMENT_INFO;
            attachment.imageView = resource.isExternal ? resource.externalView : resource.allocatedView;
            const bool writableDepth = pass.depthOutput.IsValid();
            attachment.imageLayout = writableDepth ? VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL
                                                   : VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL;
            attachment.loadOp =
                writableDepth && pass.clearDepthEnabled ? VK_ATTACHMENT_LOAD_OP_CLEAR : VK_ATTACHMENT_LOAD_OP_LOAD;
            attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
            attachment.clearValue.depthStencil = pass.clearDepth;
        }

        auto &rendering = pass.cachedRenderingInfo;
        rendering = {};
        rendering.sType = VK_STRUCTURE_TYPE_RENDERING_INFO;
        rendering.renderArea = {{0, 0}, pass.renderArea};
        rendering.layerCount = 1;
        rendering.colorAttachmentCount = colorCount;
        rendering.pColorAttachments = colorCount > 0 ? pass.cachedRenderingColorAttachments.data() : nullptr;
        rendering.pDepthAttachment = pass.cachedRenderingDepthAttachment.imageView != VK_NULL_HANDLE
                                         ? &pass.cachedRenderingDepthAttachment
                                         : nullptr;
        rendering.pStencilAttachment =
            rendering.pDepthAttachment && rhi::IsStencilFormat(pass.renderingSignature.stencilFormat)
                ? &pass.cachedRenderingDepthAttachment
                : nullptr;
    }
}

// ============================================================================
// Barrier Insertion
// ============================================================================

void RenderGraph::InsertBarriers(VkCommandBuffer cmdBuffer, uint32_t passIndex)
{
    const auto &pass = m_passes[passIndex];

    m_barrierScratch.clear();
    m_bufferBarrierScratch.clear();
    VkPipelineStageFlags srcStageMask = 0;
    VkPipelineStageFlags dstStageMask = 0;

    const auto bindingFor = [&](rhi::QueueRole role) -> NativeQueueBinding {
        if (role == rhi::QueueRole::Count)
            return {};
        return m_queueTopology[static_cast<size_t>(role)];
    };
    const rhi::QueueRole recordingQueue = m_recordingSubmissionBatches ? pass.queue : m_immediateRecordingQueue;

    const auto aspectMaskFor = [](const ResourceData &resource, bool isDepthInput) {
        bool isDepthResource = resource.type == ResourceType::DepthStencil || isDepthInput;
        if (!isDepthResource) {
            const VkFormat format = resource.textureDesc.format;
            isDepthResource = format == VK_FORMAT_D32_SFLOAT || format == VK_FORMAT_D24_UNORM_S8_UINT ||
                              format == VK_FORMAT_D16_UNORM || format == VK_FORMAT_D32_SFLOAT_S8_UINT;
        }
        return isDepthResource ? rhi::ToVkImageAspectMask(resource.textureDesc.format) : VK_IMAGE_ASPECT_COLOR_BIT;
    };

    auto addBarrier = [&](const ResourceAccess &access, bool isDepthInput = false) {
        if ((access.usage & ResourceUsage::VersionDependency) != ResourceUsage::None)
            return;
        if (access.handle.id >= m_resources.size())
            return;

        const auto &resource = m_resources[access.handle.id];
        if (resource.type == ResourceType::RendererList)
            return;

        const auto &prevState = m_resourceStates[access.handle.id];
        const bool present = (access.usage & ResourceUsage::Present) != ResourceUsage::None;
        const rhi::QueueRole targetQueue = present ? rhi::QueueRole::Present : recordingQueue;
        const NativeQueueBinding targetBinding = bindingFor(targetQueue);
        const bool hasPreviousOwner = prevState.queueFamily != VK_QUEUE_FAMILY_IGNORED &&
                                      prevState.nativeQueueLane != UINT32_MAX && targetBinding.IsValid();
        const bool laneChange = hasPreviousOwner && prevState.nativeQueueLane != targetBinding.lane;
        const bool familyChange = hasPreviousOwner && prevState.queueFamily != targetBinding.family;
        const bool ownershipTransfer =
            m_recordingSubmissionBatches && laneChange && familyChange && !resource.concurrentQueueSharing;

        VkAccessFlags srcAccessMask = ToVkAccessFlags(prevState.accessMask);
        VkAccessFlags dstAccessMask = ToVkAccessFlags(access.access);
        VkPipelineStageFlags srcStages = ToVkPipelineStages(prevState.stages);
        VkPipelineStageFlags dstStages = ToVkPipelineStages(access.stages);
        if (!present && laneChange) {
            // A timeline wait makes the source writes available. The target
            // command buffer only performs visibility/layout acquisition.
            srcAccessMask = 0;
            srcStages = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
        } else if (present) {
            // PresentRead is the producer-side release/layout transition.
            dstAccessMask = 0;
            dstStages = VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT;
        }
        if (srcStages == 0)
            srcStages = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
        if (dstStages == 0)
            dstStages = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;

        if (resource.type == ResourceType::Buffer) {
            const VkBuffer buffer = resource.isExternal ? resource.externalBuffer : resource.allocatedBuffer;
            if (buffer == VK_NULL_HANDLE)
                return;

            const bool previousWrite =
                rhi::HasAny(prevState.accessMask, rhi::Access::ShaderWrite | rhi::Access::TransferWrite |
                                                      rhi::Access::MemoryWrite | rhi::Access::HostWrite);
            const bool currentWrite = (static_cast<int>(access.usage & ResourceUsage::Write) != 0);
            if (!previousWrite && !currentWrite && !laneChange)
                return;

            VkBufferMemoryBarrier barrier{};
            barrier.sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
            barrier.srcAccessMask = srcAccessMask;
            barrier.dstAccessMask = dstAccessMask;
            barrier.srcQueueFamilyIndex = ownershipTransfer ? prevState.queueFamily : VK_QUEUE_FAMILY_IGNORED;
            barrier.dstQueueFamilyIndex = ownershipTransfer ? targetBinding.family : VK_QUEUE_FAMILY_IGNORED;
            barrier.buffer = buffer;
            barrier.offset = 0;
            barrier.size = resource.bufferDesc.size;
            m_bufferBarrierScratch.push_back(barrier);
            srcStageMask |= srcStages;
            dstStageMask |= dstStages;
            return;
        }

        VkImage image = resource.isExternal ? resource.externalImage : resource.allocatedImage;
        if (image == VK_NULL_HANDLE)
            return;

        const VkImageLayout oldLayout = ToVkImageLayout(prevState.layout);
        const VkImageLayout newLayout = ToVkImageLayout(access.layout);

        // Skip barrier if layout is already correct and no write hazard
        if (oldLayout == newLayout && (static_cast<int>(access.usage & ResourceUsage::Write) == 0) && !laneChange &&
            !rhi::HasAny(prevState.accessMask, rhi::Access::ColorWrite | rhi::Access::DepthWrite |
                                                   rhi::Access::ShaderWrite | rhi::Access::TransferWrite |
                                                   rhi::Access::MemoryWrite | rhi::Access::HostWrite)) {
            return;
        }

        VkImageMemoryBarrier barrier{};
        barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
        barrier.oldLayout = oldLayout;
        barrier.newLayout = newLayout;
        barrier.srcQueueFamilyIndex = ownershipTransfer ? prevState.queueFamily : VK_QUEUE_FAMILY_IGNORED;
        barrier.dstQueueFamilyIndex = ownershipTransfer ? targetBinding.family : VK_QUEUE_FAMILY_IGNORED;
        barrier.image = image;
        barrier.subresourceRange.aspectMask = aspectMaskFor(resource, isDepthInput);
        barrier.subresourceRange.baseMipLevel = 0;
        barrier.subresourceRange.levelCount = 1;
        barrier.subresourceRange.baseArrayLayer = 0;
        barrier.subresourceRange.layerCount = 1;
        barrier.srcAccessMask = srcAccessMask;
        barrier.dstAccessMask = dstAccessMask;

        m_barrierScratch.push_back(barrier);
        srcStageMask |= srcStages;
        dstStageMask |= dstStages;
    };

    auto updateState = [&](const ResourceAccess &access, rhi::TextureLayout layout) {
        if (access.handle.id >= m_resourceStates.size())
            return;
        const bool present = (access.usage & ResourceUsage::Present) != ResourceUsage::None;
        const rhi::QueueRole targetQueue = present ? rhi::QueueRole::Present : recordingQueue;
        const NativeQueueBinding targetBinding = bindingFor(targetQueue);
        auto &state = m_resourceStates[access.handle.id];
        state.layout = layout;
        state.accessMask = access.access;
        state.stages = access.stages;
        state.writerPassId = passIndex;
        state.queue = targetQueue;
        state.queueFamily = targetBinding.family;
        state.nativeQueueLane = targetBinding.lane;
    };

    // A resource may be bound to several shader names in one pass. Those
    // bindings are distinct, but the image transition is not: emitting the
    // same oldLayout -> newLayout barrier twice in one dependency info makes
    // the second barrier observe the layout produced by the first one.
    for (size_t readIndex = 0; readIndex < pass.reads.size(); ++readIndex) {
        const auto &read = pass.reads[readIndex];
        const bool duplicateTransition =
            std::any_of(pass.reads.begin(), pass.reads.begin() + readIndex, [&](const ResourceAccess &previous) {
                return previous.handle.id == read.handle.id && previous.layout == read.layout &&
                       (previous.usage & ResourceUsage::VersionDependency) == ResourceUsage::None &&
                       (read.usage & ResourceUsage::VersionDependency) == ResourceUsage::None;
            });
        if (duplicateTransition)
            continue;
        bool isDepthInput = (static_cast<int>(read.usage & ResourceUsage::DepthRead) != 0);
        addBarrier(read, isDepthInput);
    }

    // Check read-write overlap without heap allocation
    bool hasReadWriteOverlap = false;
    for (const auto &write : pass.writes) {
        for (const auto &read : pass.reads) {
            if (write.handle.id == read.handle.id) {
                hasReadWriteOverlap = true;
                break;
            }
        }
        if (hasReadWriteOverlap)
            break;
    }

    if (hasReadWriteOverlap && (!m_barrierScratch.empty() || !m_bufferBarrierScratch.empty())) {
        if (srcStageMask == 0)
            srcStageMask = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
        if (dstStageMask == 0)
            dstStageMask = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;

        IssuePipelineBarriers(cmdBuffer, srcStageMask, dstStageMask);

        m_barrierScratch.clear();
        m_bufferBarrierScratch.clear();
        srcStageMask = 0;
        dstStageMask = 0;
    }

    for (const auto &read : pass.reads) {
        if ((read.usage & ResourceUsage::VersionDependency) != ResourceUsage::None)
            continue;
        updateState(read, read.layout);
    }

    // Process write accesses
    for (const auto &write : pass.writes) {
        addBarrier(write);
    }

    if (!m_barrierScratch.empty() || !m_bufferBarrierScratch.empty()) {
        if (srcStageMask == 0)
            srcStageMask = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
        if (dstStageMask == 0)
            dstStageMask = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;

        IssuePipelineBarriers(cmdBuffer, srcStageMask, dstStageMask);
    }

    // Update resource states after this pass executes.
    //
    // Write accesses override read state (a pass that both reads and writes
    // a resource leaves it in the write layout).
    for (const auto &write : pass.writes) {
        if (write.handle.id < m_resources.size()) {
            updateState(write, write.layout);
        }
    }

    // Read-only depth remains in its declared sampled attachment layout.
    if (pass.depthInput.IsValid() && !pass.depthOutput.IsValid()) {
        auto &state = m_resourceStates[pass.depthInput.id];
        state.layout = rhi::TextureLayout::DepthStencilReadOnly;
        state.accessMask = rhi::Access::DepthRead;
        state.stages = rhi::PipelineStage::EarlyDepth | rhi::PipelineStage::LateDepth;
    }
}

void RenderGraph::InsertQueueOwnershipReleases(VkCommandBuffer cmdBuffer, uint32_t batchIndex)
{
    const std::vector<uint32_t> *outgoing = nullptr;
    if (batchIndex == rhi::InvalidSubmissionBatchIndex) {
        const rhi::QueueRole recordingQueue = m_immediateRecordingQueue;
        if (recordingQueue == rhi::QueueRole::Count)
            return;
        outgoing = &m_externalOutgoingOwnershipTransfers[static_cast<size_t>(recordingQueue)];
    } else {
        if (batchIndex >= m_batchOutgoingOwnershipTransfers.size())
            return;
        outgoing = &m_batchOutgoingOwnershipTransfers[batchIndex];
    }

    m_barrierScratch.clear();
    m_bufferBarrierScratch.clear();
    VkPipelineStageFlags srcStageMask = 0;

    const auto aspectMaskFor = [](const ResourceData &resource) {
        const VkFormat format = resource.textureDesc.format;
        const bool depth = resource.type == ResourceType::DepthStencil || format == VK_FORMAT_D32_SFLOAT ||
                           format == VK_FORMAT_D24_UNORM_S8_UINT || format == VK_FORMAT_D16_UNORM ||
                           format == VK_FORMAT_D32_SFLOAT_S8_UINT;
        return depth ? rhi::ToVkImageAspectMask(resource.textureDesc.format) : VK_IMAGE_ASPECT_COLOR_BIT;
    };

    for (const uint32_t transferIndex : *outgoing) {
        if (transferIndex >= m_queueOwnershipTransfers.size())
            continue;
        const auto &transfer = m_queueOwnershipTransfers[transferIndex];
        if (transfer.info.resourceId >= m_resources.size())
            continue;
        const auto &resource = m_resources[transfer.info.resourceId];
        if (resource.concurrentQueueSharing)
            continue;

        VkPipelineStageFlags sourceStages = ToVkPipelineStages(transfer.sourceState.stages);
        if (sourceStages == 0)
            sourceStages = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
        srcStageMask |= sourceStages;

        if (resource.type == ResourceType::Buffer) {
            const VkBuffer buffer = resource.isExternal ? resource.externalBuffer : resource.allocatedBuffer;
            if (buffer == VK_NULL_HANDLE)
                continue;
            VkBufferMemoryBarrier barrier{};
            barrier.sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
            barrier.srcAccessMask = ToVkAccessFlags(transfer.sourceState.accessMask);
            barrier.dstAccessMask = 0;
            barrier.srcQueueFamilyIndex = transfer.info.sourceFamily;
            barrier.dstQueueFamilyIndex = transfer.info.targetFamily;
            barrier.buffer = buffer;
            barrier.offset = 0;
            barrier.size = resource.bufferDesc.size;
            m_bufferBarrierScratch.push_back(barrier);
            continue;
        }

        const VkImage image = resource.isExternal ? resource.externalImage : resource.allocatedImage;
        if (image == VK_NULL_HANDLE)
            continue;
        VkImageMemoryBarrier barrier{};
        barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
        barrier.srcAccessMask = ToVkAccessFlags(transfer.sourceState.accessMask);
        barrier.dstAccessMask = 0;
        barrier.oldLayout = ToVkImageLayout(transfer.sourceState.layout);
        barrier.newLayout = ToVkImageLayout(transfer.targetAccess.layout);
        barrier.srcQueueFamilyIndex = transfer.info.sourceFamily;
        barrier.dstQueueFamilyIndex = transfer.info.targetFamily;
        barrier.image = image;
        barrier.subresourceRange.aspectMask = aspectMaskFor(resource);
        barrier.subresourceRange.baseMipLevel = 0;
        barrier.subresourceRange.levelCount = 1;
        barrier.subresourceRange.baseArrayLayer = 0;
        barrier.subresourceRange.layerCount = 1;
        m_barrierScratch.push_back(barrier);
    }

    if (!m_barrierScratch.empty() || !m_bufferBarrierScratch.empty()) {
        if (srcStageMask == 0)
            srcStageMask = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
        IssuePipelineBarriers(cmdBuffer, srcStageMask, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT);
    }
}

// ============================================================================
// Resource Cleanup
// ============================================================================

void RenderGraph::FreeResources()
{
    if (!m_context) {
        return;
    }

    VkDevice device = m_context->GetDevice();

    // RHI registrations are graph-resource lifetime aliases, including
    // external resources. Release them before the no-transient early-out and
    // before any owned native resource is destroyed.
    if (m_rhiDevice) {
        for (auto &resource : m_resources) {
            m_rhiDevice->Release(resource.rhiView);
            resource.rhiView = {};
            m_rhiDevice->Release(resource.rhiTexture);
            resource.rhiTexture = {};
            m_rhiDevice->Release(resource.rhiBuffer);
            resource.rhiBuffer = {};
        }
    }

    bool hasTransientResources = false;
    for (const auto &resource : m_resources) {
        if (resource.isExternal)
            continue;
        if (resource.allocatedView != VK_NULL_HANDLE || resource.allocatedImage != VK_NULL_HANDLE ||
            resource.allocatedBuffer != VK_NULL_HANDLE || resource.allocatedMemory != VK_NULL_HANDLE) {
            hasTransientResources = true;
            break;
        }
    }

    if (!hasTransientResources) {
        return;
    }

    std::vector<VkImageView> imageViews;
    std::vector<VkImage> images;
    std::vector<VkBuffer> buffers;
    std::unordered_set<VmaAllocation> allocationSet;
    for (auto &resource : m_resources) {
        if (resource.isExternal)
            continue;

        if (resource.allocatedView != VK_NULL_HANDLE) {
            imageViews.push_back(resource.allocatedView);
            resource.allocatedView = VK_NULL_HANDLE;
        }
        if (resource.allocatedImage != VK_NULL_HANDLE) {
            images.push_back(resource.allocatedImage);
            resource.allocatedImage = VK_NULL_HANDLE;
        }
        if (resource.allocatedBuffer != VK_NULL_HANDLE) {
            buffers.push_back(resource.allocatedBuffer);
            resource.allocatedBuffer = VK_NULL_HANDLE;
        }
        if (resource.allocatedMemory != VK_NULL_HANDLE) {
            allocationSet.insert(resource.allocatedMemory);
            resource.allocatedMemory = VK_NULL_HANDLE;
        }
    }

    std::vector<VmaAllocation> allocations(allocationSet.begin(), allocationSet.end());
    const VmaAllocator allocator = m_context->GetVmaAllocator();
    auto destroyRetired = [device, allocator, imageViews = std::move(imageViews), images = std::move(images),
                           buffers = std::move(buffers), allocations = std::move(allocations)]() mutable {
        for (VkImageView view : imageViews)
            vkDestroyImageView(device, view, nullptr);
        for (VkImage image : images)
            vkDestroyImage(device, image, nullptr);
        for (VkBuffer buffer : buffers)
            vkDestroyBuffer(device, buffer, nullptr);
        for (VmaAllocation allocation : allocations)
            vmaFreeMemory(allocator, allocation);
    };

    if (m_deletionQueue && !m_context->IsShuttingDown()) {
        m_deletionQueue->Retire(std::move(destroyRetired));
    } else {
        if (!m_context->IsShuttingDown()) {
            vkDeviceWaitIdle(device);
            SDL_PumpEvents();
        }
        destroyRetired();
    }
}

uint64_t RenderGraph::GetTransientResidentBytes() const
{
    if (!m_context)
        return 0;
    const VmaAllocator allocator = m_context->GetVmaAllocator();
    std::unordered_set<VmaAllocation> allocations;
    for (const auto &resource : m_resources) {
        if (!resource.isExternal && resource.allocatedMemory != VK_NULL_HANDLE)
            allocations.insert(resource.allocatedMemory);
    }
    uint64_t bytes = 0;
    for (const VmaAllocation allocation : allocations) {
        VmaAllocationInfo info{};
        vmaGetAllocationInfo(allocator, allocation, &info);
        bytes += info.size;
    }
    return bytes;
}

size_t RenderGraph::GetTransientAllocationCount() const
{
    std::unordered_set<VmaAllocation> allocations;
    for (const auto &resource : m_resources) {
        if (!resource.isExternal && resource.allocatedMemory != VK_NULL_HANDLE)
            allocations.insert(resource.allocatedMemory);
    }
    return allocations.size();
}

} // namespace vk
} // namespace infernux
