/**
 * @file RenderGraphCompile.cpp
 * @brief RenderGraph compilation pipeline — pass culling, topological sort, resource allocation,
 *        Vulkan render-pass / framebuffer creation, barrier insertion, and cache management.
 *
 * Part of the RenderGraph implementation (see also RenderGraph.cpp for the public API surface).
 */

#include "RenderGraph.h"
#include "VkDeviceContext.h"
#include "VkPipelineManager.h"
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

    // Log culled passes for debugging — these passes are unreachable from
    // the output and will not execute.
    for (uint32_t i = 0; i < m_passes.size(); i++) {
        if (m_passes[i].culled) {
            INXLOG_WARN("RenderGraph::CullPasses - Pass '", m_passes[i].name, "' (index ", i,
                        ") was culled (no path to output). "
                        "Check that downstream passes read this pass's outputs.");
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
            if (producer != producers.end() && producer->second != readPassId)
                adjacency[producer->second].push_back(readPassId);
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
            if (readerPassId != nextProducer->second)
                adjacency[readerPassId].push_back(nextProducer->second);
        }
    }

    // Deduplicate edges
    for (auto &[passId, deps] : adjacency) {
        std::sort(deps.begin(), deps.end());
        deps.erase(std::unique(deps.begin(), deps.end()), deps.end());
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
            }
        }
        m_executionOrder.clear();
        return false;
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
    signature.push_back(static_cast<uint64_t>(m_backbufferFinalLayout));

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
        entry.passes.push_back({pass.refCount, pass.culled, pass.cullReason});
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
// RenderPass / Framebuffer Caching
// ============================================================================

size_t RenderGraph::HashRenderPassConfig(VkFormat colorFmt, VkFormat depthFmt, VkSampleCountFlagBits samples,
                                         bool clearColor, bool clearDepth, bool storeColor, bool storeDepth,
                                         VkImageLayout colorFinalLayout, bool hasResolve, VkFormat resolveFormat,
                                         bool hasColorAttachments, bool readOnlyDepth)
{
    size_t h = 0;
    auto hashCombine = [&h](size_t val) { h ^= val + 0x9e3779b9 + (h << 6) + (h >> 2); };
    hashCombine(std::hash<bool>{}(hasColorAttachments));
    hashCombine(std::hash<uint32_t>{}(static_cast<uint32_t>(colorFmt)));
    hashCombine(std::hash<uint32_t>{}(static_cast<uint32_t>(depthFmt)));
    hashCombine(std::hash<uint32_t>{}(static_cast<uint32_t>(samples)));
    hashCombine(std::hash<bool>{}(clearColor));
    hashCombine(std::hash<bool>{}(clearDepth));
    hashCombine(std::hash<bool>{}(storeColor));
    hashCombine(std::hash<bool>{}(storeDepth));
    hashCombine(std::hash<bool>{}(readOnlyDepth));
    hashCombine(std::hash<uint32_t>{}(static_cast<uint32_t>(colorFinalLayout)));
    hashCombine(std::hash<bool>{}(hasResolve));
    if (hasResolve) {
        hashCombine(std::hash<uint32_t>{}(static_cast<uint32_t>(resolveFormat)));
    }
    return h;
}

size_t RenderGraph::HashFramebuffer(VkRenderPass renderPass, const std::vector<VkImageView> &attachments,
                                    uint32_t width, uint32_t height)
{
    size_t h = 0;
    auto hashCombine = [&h](size_t val) { h ^= val + 0x9e3779b9 + (h << 6) + (h >> 2); };
    hashCombine(std::hash<uint64_t>{}(reinterpret_cast<uint64_t>(renderPass)));
    for (auto view : attachments) {
        hashCombine(std::hash<uint64_t>{}(reinterpret_cast<uint64_t>(view)));
    }
    hashCombine(std::hash<uint32_t>{}(width));
    hashCombine(std::hash<uint32_t>{}(height));
    return h;
}

void RenderGraph::FlushUnusedCaches()
{
    if (!m_context)
        return;

    VkDevice device = m_context->GetDevice();

    // Increment unused counter for framebuffers not used this frame
    for (auto &[key, entry] : m_framebufferCache) {
        entry.unusedFrames++;
    }

    // Reset counter for used entries
    for (size_t key : m_usedFramebufferKeys) {
        auto it = m_framebufferCache.find(key);
        if (it != m_framebufferCache.end()) {
            it->second.unusedFrames = 0;
        }
    }

    // Remove entries unused for more than 60 frames
    constexpr uint32_t GC_THRESHOLD = 60;
    for (auto it = m_framebufferCache.begin(); it != m_framebufferCache.end();) {
        if (it->second.unusedFrames > GC_THRESHOLD) {
            if (it->second.framebuffer != VK_NULL_HANDLE) {
                vkDestroyFramebuffer(device, it->second.framebuffer, nullptr);
            }
            it = m_framebufferCache.erase(it);
        } else {
            ++it;
        }
    }
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

    // ========================================================================
    // Memory aliasing for transient images
    //
    // Group transient images with non-overlapping lifetimes and compatible
    // memory types onto shared VkDeviceMemory allocations.  This reduces
    // memory consumption and allocation count — critical on mobile/tiled
    // GPUs and for large render graphs with many intermediate targets.
    //
    // Algorithm: greedy interval colouring with size-descending pre-sort.
    //
    // Pre-sort fix (P2): the previous code processed requests in arbitrary
    // order, so a small resource could create a heap that was too small
    // for a later large resource with a non-overlapping lifetime.  By
    // sorting allocation requests largest-first, the heap is always
    // created with the maximum size and subsequent smaller resources
    // can alias into it unconditionally (as long as lifetimes don't overlap
    // and the memory type matches).
    // ========================================================================

    // Sort allocation requests by size descending so the largest resource
    // creates the heap first.
    std::sort(imageAllocRequests.begin(), imageAllocRequests.end(),
              [](const AllocationRequest &a, const AllocationRequest &b) { return a.memReqs.size > b.memReqs.size; });

    struct MemoryHeap
    {
        VmaAllocation allocation = VK_NULL_HANDLE;
        VkDeviceMemory memory = VK_NULL_HANDLE; // Cached from VmaAllocationInfo (for vkBindImageMemory)
        VkDeviceSize size = 0;
        uint32_t memoryTypeIndex = 0;
        VkDeviceSize alignment = 0;

        // Lifetime intervals currently occupying this heap.
        // Each pair is (firstPass, lastPass) from the resource.
        std::vector<std::pair<uint32_t, uint32_t>> occupants;
    };

    std::vector<MemoryHeap> heaps;

    auto lifetimesOverlap = [](uint32_t aFirst, uint32_t aLast, uint32_t bFirst, uint32_t bLast) -> bool {
        return aFirst <= bLast && bFirst <= aLast;
    };

    for (auto &req : imageAllocRequests) {
        auto &resource = m_resources[req.resourceIndex];
        bool placed = false;

        // Only alias transient resources with valid lifetimes
        if (resource.textureDesc.isTransient && resource.firstPass <= resource.lastPass) {
            for (auto &heap : heaps) {
                // Must be same memory type
                if (heap.memoryTypeIndex != req.memoryTypeIndex)
                    continue;

                // Check lifetime overlap with all occupants
                bool overlaps = false;
                for (auto &[oFirst, oLast] : heap.occupants) {
                    if (lifetimesOverlap(resource.firstPass, resource.lastPass, oFirst, oLast)) {
                        overlaps = true;
                        break;
                    }
                }

                if (!overlaps) {
                    // With size-descending pre-sort the heap was created by
                    // the largest resource, so any later (smaller) resource
                    // is guaranteed to fit.  Non-overlapping lifetimes allow
                    // binding at offset 0 (resources never coexist).
                    if (req.memReqs.size > heap.size) {
                        // Shouldn't happen after pre-sort, but guard anyway.
                        continue;
                    }

                    // Compute aligned offset within the heap
                    VkDeviceSize offset =
                        ((heap.size - req.memReqs.size) / req.memReqs.alignment) * req.memReqs.alignment;
                    // Actually, place sequentially: find the next aligned offset
                    // after existing placements.  For aliased resources with
                    // non-overlapping lifetimes, offset 0 is valid (they never
                    // coexist).
                    offset = 0; // Non-overlapping → same base address is safe

                    if (vkBindImageMemory(device, resource.allocatedImage, heap.memory, offset) != VK_SUCCESS) {
                        continue;
                    }

                    resource.allocatedMemory = VK_NULL_HANDLE; // Don't free — owned by heap
                    heap.occupants.push_back({resource.firstPass, resource.lastPass});
                    placed = true;
                    break;
                }
            }
        }

        if (!placed) {
            // Allocate new memory for this resource via VMA (becomes a new heap candidate).
            // Use dedicated allocation so the entire VkDeviceMemory is owned by this heap,
            // allowing aliased images to bind at offset 0.
            VmaAllocator allocator = m_context->GetVmaAllocator();
            VmaAllocationCreateInfo allocCreateInfo{};
            // vmaAllocateMemory (raw) doesn't have resource creation info,
            // so AUTO modes can't infer the correct memory type.
            // Use legacy GPU_ONLY which maps directly to DEVICE_LOCAL.
            allocCreateInfo.usage = VMA_MEMORY_USAGE_GPU_ONLY;
            allocCreateInfo.flags = VMA_ALLOCATION_CREATE_DEDICATED_MEMORY_BIT;

            VmaAllocation allocation = VK_NULL_HANDLE;
            VmaAllocationInfo vmaAllocInfo;
            if (vmaAllocateMemory(allocator, &req.memReqs, &allocCreateInfo, &allocation, &vmaAllocInfo) !=
                VK_SUCCESS) {
                INXLOG_ERROR("Failed to allocate memory for resource: ", resource.name);
                return false;
            }

            if (vkBindImageMemory(device, resource.allocatedImage, vmaAllocInfo.deviceMemory, 0) != VK_SUCCESS) {
                vmaFreeMemory(allocator, allocation);
                INXLOG_ERROR("Failed to bind image memory for resource: ", resource.name);
                return false;
            }

            resource.allocatedMemory = allocation;

            // Register as a new heap for potential future aliasing
            if (resource.textureDesc.isTransient && resource.firstPass <= resource.lastPass) {
                MemoryHeap heap;
                heap.allocation = allocation;
                heap.memory = vmaAllocInfo.deviceMemory;
                heap.size = req.memReqs.size;
                heap.memoryTypeIndex = req.memoryTypeIndex;
                heap.alignment = req.memReqs.alignment;
                heap.occupants.push_back({resource.firstPass, resource.lastPass});
                heaps.push_back(std::move(heap));
            }
        }

        // Create image view (regardless of aliasing)
        VkImageViewCreateInfo viewInfo{};
        viewInfo.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        viewInfo.image = resource.allocatedImage;
        viewInfo.viewType = VK_IMAGE_VIEW_TYPE_2D;
        viewInfo.format = resource.textureDesc.format;

        if (resource.type == ResourceType::DepthStencil) {
            viewInfo.subresourceRange.aspectMask = VK_IMAGE_ASPECT_DEPTH_BIT;
        } else {
            // Detect depth-formatted textures registered as Texture2D
            VkFormat fmt = resource.textureDesc.format;
            bool isDepthFmt = (fmt == VK_FORMAT_D32_SFLOAT || fmt == VK_FORMAT_D24_UNORM_S8_UINT ||
                               fmt == VK_FORMAT_D16_UNORM || fmt == VK_FORMAT_D32_SFLOAT_S8_UINT);
            viewInfo.subresourceRange.aspectMask = isDepthFmt ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT;
        }

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

    // Track aliased memory heaps for cleanup
    for (auto &heap : heaps) {
        // Heaps whose allocation is also stored in a resource's allocatedMemory
        // will be freed by FreeResources(). Heaps that were reused by
        // aliased resources (allocatedMemory == VK_NULL_HANDLE on the aliasee)
        // need separate tracking.
        bool ownedByResource = false;
        for (const auto &resource : m_resources) {
            if (resource.allocatedMemory == heap.allocation) {
                ownedByResource = true;
                break;
            }
        }
        if (!ownedByResource) {
            m_aliasedMemoryHeaps.push_back(heap.allocation);
        }
    }

    // Transient buffers use the same interval-colouring rule as images, but
    // remain in separate heaps because Vulkan memory requirements are
    // resource-class specific. Distinct VkBuffer handles alias only the raw
    // allocation and therefore preserve their declared usages.
    std::sort(bufferAllocRequests.begin(), bufferAllocRequests.end(),
              [](const AllocationRequest &a, const AllocationRequest &b) { return a.memReqs.size > b.memReqs.size; });

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
// Vulkan RenderPass & Framebuffer Creation
// ============================================================================

bool RenderGraph::CreateVulkanRenderPasses()
{
    if (!m_pipelineManager) {
        return false;
    }

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

        // Determine whether this pass has color outputs
        bool hasColorOutputs = !pass.colorOutputs.empty();

        // Determine color format and sample count
        VkFormat colorFormat = VK_FORMAT_B8G8R8A8_UNORM;
        VkSampleCountFlagBits sampleCount = VK_SAMPLE_COUNT_1_BIT;
        if (hasColorOutputs && pass.colorOutputs[0].IsValid()) {
            const auto &resource = m_resources[pass.colorOutputs[0].id];
            colorFormat = resource.textureDesc.format;
            sampleCount = resource.textureDesc.samples;
        }

        // Determine depth format from effective depth
        VkFormat depthFormat = VK_FORMAT_UNDEFINED;
        if (effectiveDepth.IsValid()) {
            const auto &resource = m_resources[effectiveDepth.id];
            depthFormat = resource.textureDesc.format;
        }

        // Determine whether depth must be stored for later passes
        bool needStoreDepth = false;
        if (effectiveDepth.IsValid()) {
            needStoreDepth = IsResourceUsedAfter(effectiveDepth.id, pass.id);
        }

        RenderPassConfig config;
        config.colorFormat = colorFormat;
        config.hasColor = hasColorOutputs;
        config.depthFormat = depthFormat;
        config.hasDepth = effectiveDepth.IsValid();
        config.clearColor = pass.clearColorEnabled;
        config.clearDepth = pass.clearDepthEnabled;
        config.storeColor = !hasColorOutputs || IsResourceUsedAfter(pass.colorOutputs[0].id, pass.id);
        config.storeDepth = needStoreDepth;
        // Read-only depth: the pass reads depth (depthInput) but never writes it (no depthOutput).
        // This requires DEPTH_STENCIL_READ_ONLY_OPTIMAL layouts throughout.
        config.readOnlyDepth = pass.depthInput.IsValid() && !pass.depthOutput.IsValid();
        config.samples = sampleCount;

        // MRT: Collect per-attachment formats from ALL color outputs.
        // When a pass writes to multiple color targets (GBuffer, etc.),
        // each attachment may have a different format.
        if (pass.colorOutputs.size() > 1) {
            for (const auto &co : pass.colorOutputs) {
                if (co.IsValid()) {
                    config.colorFormats.push_back(m_resources[co.id].textureDesc.format);
                } else {
                    config.colorFormats.push_back(colorFormat);
                }
            }
        }

        // MSAA resolve support
        if (pass.resolveOutput.IsValid() && sampleCount > VK_SAMPLE_COUNT_1_BIT) {
            const auto &resolveResource = m_resources[pass.resolveOutput.id];
            config.hasResolve = true;
            config.resolveFormat = resolveResource.textureDesc.format;
            config.resolveFinalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
            pass.hasResolveAttachment = true;
        }

        // Color final layout — COLOR_ATTACHMENT_OPTIMAL for offscreen scene targets,
        // PRESENT_SRC_KHR for swapchain targets (set via SetBackbufferFinalLayout)
        config.colorFinalLayout = ToVkImageLayout(m_backbufferFinalLayout);

        // Use RenderPass cache
        // Include MRT attachment count + formats in cache key
        size_t cacheKey =
            HashRenderPassConfig(colorFormat, depthFormat, sampleCount, config.clearColor, config.clearDepth,
                                 config.storeColor, config.storeDepth, config.colorFinalLayout, config.hasResolve,
                                 config.resolveFormat, hasColorOutputs, config.readOnlyDepth);
        // Fold MRT info into cache key
        {
            auto hashCombine = [&cacheKey](size_t val) {
                cacheKey ^= val + 0x9e3779b9 + (cacheKey << 6) + (cacheKey >> 2);
            };
            hashCombine(config.colorFormats.size());
            for (VkFormat f : config.colorFormats) {
                hashCombine(static_cast<uint32_t>(f));
            }
        }

        auto cacheIt = m_renderPassCache.find(cacheKey);
        if (cacheIt != m_renderPassCache.end()) {
            pass.vulkanRenderPass = cacheIt->second;
            const auto layoutIt = m_renderTargetLayoutCache.find(cacheKey);
            if (layoutIt != m_renderTargetLayoutCache.end())
                pass.renderTargetLayout = layoutIt->second;
        } else {
            pass.vulkanRenderPass = m_pipelineManager->CreateRenderPass(config);
            if (pass.vulkanRenderPass == VK_NULL_HANDLE) {
                INXLOG_ERROR("Failed to create render pass for: ", pass.name);
                return false;
            }
            m_renderPassCache[cacheKey] = pass.vulkanRenderPass;
            pass.renderTargetLayout = m_rhiDevice ? m_rhiDevice->RegisterRenderTargetLayout(pass.vulkanRenderPass)
                                                  : rhi::RenderTargetLayoutHandle{};
            m_renderTargetLayoutCache[cacheKey] = pass.renderTargetLayout;
        }
        m_usedRenderPassKeys.push_back(cacheKey);
    }

    return true;
}

bool RenderGraph::CreateFramebuffers()
{
    if (!m_context) {
        return false;
    }

    VkDevice device = m_context->GetDevice();

    for (auto &pass : m_passes) {
        if (pass.culled || pass.type != PassType::Graphics || pass.vulkanRenderPass == VK_NULL_HANDLE) {
            continue;
        }

        std::vector<VkImageView> attachments;

        // Add color attachments
        for (const auto &colorOutput : pass.colorOutputs) {
            if (colorOutput.IsValid()) {
                VkImageView view = ResolveTextureView(colorOutput);
                if (view != VK_NULL_HANDLE) {
                    attachments.push_back(view);
                }
            }
        }

        // Add depth attachment (write or read-only)
        ResourceHandle effectiveDepth = GetEffectiveDepth(pass);
        if (effectiveDepth.IsValid()) {
            VkImageView view = ResolveTextureView(effectiveDepth);
            if (view != VK_NULL_HANDLE) {
                attachments.push_back(view);
            }
        }

        // Add resolve attachment (must be after depth to match render pass attachment order)
        if (pass.hasResolveAttachment && pass.resolveOutput.IsValid()) {
            VkImageView view = ResolveTextureView(pass.resolveOutput);
            if (view != VK_NULL_HANDLE) {
                attachments.push_back(view);
            }
        }

        if (attachments.empty()) {
            continue;
        }

        // Use framebuffer cache
        size_t fbKey =
            HashFramebuffer(pass.vulkanRenderPass, attachments, pass.renderArea.width, pass.renderArea.height);
        auto cacheIt = m_framebufferCache.find(fbKey);
        if (cacheIt != m_framebufferCache.end() && cacheIt->second.framebuffer != VK_NULL_HANDLE) {
            pass.framebuffer = cacheIt->second.framebuffer;
            cacheIt->second.unusedFrames = 0;
        } else {
            VkFramebufferCreateInfo framebufferInfo{};
            framebufferInfo.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
            framebufferInfo.renderPass = pass.vulkanRenderPass;
            framebufferInfo.attachmentCount = static_cast<uint32_t>(attachments.size());
            framebufferInfo.pAttachments = attachments.data();
            framebufferInfo.width = pass.renderArea.width;
            framebufferInfo.height = pass.renderArea.height;
            framebufferInfo.layers = 1;

            VkFramebuffer fb = VK_NULL_HANDLE;
            if (vkCreateFramebuffer(device, &framebufferInfo, nullptr, &fb) != VK_SUCCESS) {
                INXLOG_ERROR("Failed to create framebuffer for pass: ", pass.name);
                return false;
            }
            pass.framebuffer = fb;
            m_framebufferCache[fbKey] = {fb, 0};
        }
        m_usedFramebufferKeys.push_back(fbKey);
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

        const bool isGfx = (pass.type == PassType::Graphics && pass.vulkanRenderPass != VK_NULL_HANDLE);
        if (!isGfx)
            continue;

        // VkRenderPassBeginInfo
        auto &bi = pass.cachedBeginInfo;
        bi.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
        bi.pNext = nullptr;
        bi.renderPass = pass.vulkanRenderPass;
        bi.framebuffer = pass.framebuffer;
        bi.renderArea.offset = {0, 0};
        bi.renderArea.extent = pass.renderArea;

        // Clear values: [color × N] [depth?] [resolve?]
        uint32_t idx = 0;
        for (size_t ci = 0; ci < pass.colorOutputs.size() && idx < 10; ++ci) {
            pass.cachedClearValues[idx].color = pass.clearColor;
            ++idx;
        }
        ResourceHandle effectiveDepth = GetEffectiveDepth(pass);
        if (effectiveDepth.IsValid() && idx < 10) {
            pass.cachedClearValues[idx].depthStencil = pass.clearDepth;
            ++idx;
        }
        if (pass.hasResolveAttachment && idx < 10) {
            pass.cachedClearValues[idx].color = {{0.0f, 0.0f, 0.0f, 0.0f}};
            ++idx;
        }
        pass.cachedClearValueCount = idx;
        bi.clearValueCount = idx;
        bi.pClearValues = pass.cachedClearValues;

        // Viewport
        pass.cachedViewport.x = 0.0f;
        pass.cachedViewport.y = 0.0f;
        pass.cachedViewport.width = static_cast<float>(pass.renderArea.width);
        pass.cachedViewport.height = static_cast<float>(pass.renderArea.height);
        pass.cachedViewport.minDepth = 0.0f;
        pass.cachedViewport.maxDepth = 1.0f;

        // Scissor
        pass.cachedScissor.offset = {0, 0};
        pass.cachedScissor.extent = pass.renderArea;
    }
}

// ============================================================================
// Barrier Insertion
// ============================================================================

void RenderGraph::InsertBarriers(VkCommandBuffer cmdBuffer, uint32_t passIndex)
{
    // Precise barrier insertion with tracked resource layouts
    const auto &pass = m_passes[passIndex];

    m_barrierScratch.clear();
    m_bufferBarrierScratch.clear();
    VkPipelineStageFlags srcStageMask = 0;
    VkPipelineStageFlags dstStageMask = 0;

    // Helper: generate barrier for a resource access
    auto addBarrier = [&](const ResourceAccess &access, bool isDepthInput = false) {
        if (static_cast<int>(access.usage & ResourceUsage::VersionDependency) != 0)
            return;
        if (access.handle.id >= m_resources.size())
            return;

        const auto &resource = m_resources[access.handle.id];
        if (resource.type == ResourceType::RendererList)
            return;

        // Look up previous state (direct index — vectors kept in sync with m_resources)
        const auto &prevState = m_resourceStates[access.handle.id];
        const VkAccessFlags srcAccessMask = ToVkAccessFlags(prevState.accessMask);
        VkPipelineStageFlags srcStages = ToVkPipelineStages(prevState.stages);
        if (srcStages == 0)
            srcStages = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;

        if (resource.type == ResourceType::Buffer) {
            const VkBuffer buffer = resource.isExternal ? resource.externalBuffer : resource.allocatedBuffer;
            if (buffer == VK_NULL_HANDLE)
                return;

            const bool previousWrite =
                rhi::HasAny(prevState.accessMask, rhi::Access::ShaderWrite | rhi::Access::TransferWrite |
                                                      rhi::Access::MemoryWrite | rhi::Access::HostWrite);
            const bool currentWrite = (static_cast<int>(access.usage & ResourceUsage::Write) != 0);
            if (!previousWrite && !currentWrite)
                return;

            VkBufferMemoryBarrier barrier{};
            barrier.sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
            barrier.srcAccessMask = srcAccessMask;
            barrier.dstAccessMask = ToVkAccessFlags(access.access);
            barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            barrier.buffer = buffer;
            barrier.offset = 0;
            barrier.size = resource.bufferDesc.size;
            m_bufferBarrierScratch.push_back(barrier);
            srcStageMask |= srcStages;
            dstStageMask |= ToVkPipelineStages(access.stages);
            return;
        }

        VkImage image = resource.isExternal ? resource.externalImage : resource.allocatedImage;
        if (image == VK_NULL_HANDLE)
            return;

        const VkImageLayout oldLayout = ToVkImageLayout(prevState.layout);
        const VkImageLayout newLayout = ToVkImageLayout(access.layout);

        // Skip barrier if layout is already correct and no write hazard
        if (oldLayout == newLayout && (static_cast<int>(access.usage & ResourceUsage::Write) == 0) &&
            !rhi::HasAny(prevState.accessMask, rhi::Access::ColorWrite | rhi::Access::DepthWrite |
                                                   rhi::Access::ShaderWrite | rhi::Access::TransferWrite |
                                                   rhi::Access::MemoryWrite | rhi::Access::HostWrite)) {
            return;
        }

        VkImageMemoryBarrier barrier{};
        barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
        barrier.oldLayout = oldLayout;
        barrier.newLayout = newLayout;
        barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        barrier.image = image;

        // Determine correct aspect mask for the barrier.
        // Depth-formatted images (including Texture2D with depth format,
        // e.g. shadow maps) must use DEPTH_BIT, not COLOR_BIT.
        bool isDepthResource = (resource.type == ResourceType::DepthStencil || isDepthInput);
        if (!isDepthResource) {
            VkFormat fmt = resource.textureDesc.format;
            isDepthResource = (fmt == VK_FORMAT_D32_SFLOAT || fmt == VK_FORMAT_D24_UNORM_S8_UINT ||
                               fmt == VK_FORMAT_D16_UNORM || fmt == VK_FORMAT_D32_SFLOAT_S8_UINT);
        }

        if (isDepthResource) {
            barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_DEPTH_BIT;
        } else {
            barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        }

        barrier.subresourceRange.baseMipLevel = 0;
        barrier.subresourceRange.levelCount = 1;
        barrier.subresourceRange.baseArrayLayer = 0;
        barrier.subresourceRange.layerCount = 1;
        barrier.srcAccessMask = srcAccessMask;
        barrier.dstAccessMask = ToVkAccessFlags(access.access);

        m_barrierScratch.push_back(barrier);
        srcStageMask |= srcStages;
        dstStageMask |= ToVkPipelineStages(access.stages);
    };

    // Process read accesses
    for (const auto &read : pass.reads) {
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

        vkCmdPipelineBarrier(cmdBuffer, srcStageMask, dstStageMask, 0, 0, nullptr,
                             static_cast<uint32_t>(m_bufferBarrierScratch.size()), m_bufferBarrierScratch.data(),
                             static_cast<uint32_t>(m_barrierScratch.size()), m_barrierScratch.data());

        m_barrierScratch.clear();
        m_bufferBarrierScratch.clear();
        srcStageMask = 0;
        dstStageMask = 0;
    }

    for (const auto &read : pass.reads) {
        if (static_cast<int>(read.usage & ResourceUsage::VersionDependency) != 0)
            continue;
        if (read.handle.id < m_resources.size()) {
            m_resourceStates[read.handle.id] = {read.layout, read.access, read.stages, passIndex};
        }
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

        vkCmdPipelineBarrier(cmdBuffer, srcStageMask, dstStageMask, 0, 0, nullptr,
                             static_cast<uint32_t>(m_bufferBarrierScratch.size()), m_bufferBarrierScratch.data(),
                             static_cast<uint32_t>(m_barrierScratch.size()), m_barrierScratch.data());
    }

    // Update resource states after this pass executes.
    //
    // Write accesses override read state (a pass that both reads and writes
    // a resource leaves it in the write layout).
    for (const auto &write : pass.writes) {
        if (write.handle.id < m_resources.size()) {
            rhi::TextureLayout postPassLayout = write.layout;

            // For graphics passes, vkCmdEndRenderPass performs an implicit
            // layout transition from the subpass layout to the attachment's
            // finalLayout.  The tracked state must reflect this ACTUAL
            // post-pass layout, not the subpass layout.
            if (pass.type == PassType::Graphics && pass.vulkanRenderPass != VK_NULL_HANDLE) {
                bool isDepthWrite = (static_cast<int>(write.usage & ResourceUsage::DepthOutput) != 0);
                if (isDepthWrite) {
                    // Mirror CreateVulkanRenderPasses / CreateRenderPass logic:
                    // storeDepth=true  → finalLayout = DEPTH_STENCIL_READ_ONLY_OPTIMAL
                    // storeDepth=false → finalLayout = DEPTH_STENCIL_ATTACHMENT_OPTIMAL
                    bool usedLater = IsResourceUsedAfter(write.handle.id, pass.id);
                    if (usedLater) {
                        postPassLayout = rhi::TextureLayout::DepthStencilReadOnly;
                    }
                }
            }

            m_resourceStates[write.handle.id] = {postPassLayout, write.access, write.stages, passIndex};
        }
    }

    // For read-only depth (depthInput without depthOutput), the render pass
    // uses DEPTH_STENCIL_READ_ONLY_OPTIMAL throughout (initial & final layout
    // are both READ_ONLY_OPTIMAL when readOnlyDepth=true in CreateRenderPass).
    if (pass.depthInput.IsValid() && !pass.depthOutput.IsValid()) {
        m_resourceStates[pass.depthInput.id] = {rhi::TextureLayout::DepthStencilReadOnly, rhi::Access::DepthRead,
                                                rhi::PipelineStage::EarlyDepth | rhi::PipelineStage::LateDepth,
                                                passIndex};
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

    // ========================================================================
    // Fix 6: Early-out when there are no transient resources to free.
    // The GUI RenderGraph contains only the external backbuffer; calling
    // vkDeviceWaitIdle every frame just to clear empty vectors is wasteful.
    // ========================================================================
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

    if (!hasTransientResources && m_aliasedMemoryHeaps.empty()) {
        // No GPU resources to destroy — just clear pass framebuffer references
        for (auto &pass : m_passes) {
            pass.framebuffer = VK_NULL_HANDLE;
        }
        return;
    }

    std::vector<VkFramebuffer> framebuffers;
    framebuffers.reserve(m_framebufferCache.size());
    for (auto &[key, entry] : m_framebufferCache) {
        if (entry.framebuffer != VK_NULL_HANDLE)
            framebuffers.push_back(entry.framebuffer);
    }
    m_framebufferCache.clear();

    for (auto &pass : m_passes)
        pass.framebuffer = VK_NULL_HANDLE;

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

    allocationSet.insert(m_aliasedMemoryHeaps.begin(), m_aliasedMemoryHeaps.end());
    m_aliasedMemoryHeaps.clear();

    std::vector<VmaAllocation> allocations(allocationSet.begin(), allocationSet.end());
    const VmaAllocator allocator = m_context->GetVmaAllocator();
    auto destroyRetired = [device, allocator, framebuffers = std::move(framebuffers),
                           imageViews = std::move(imageViews), images = std::move(images), buffers = std::move(buffers),
                           allocations = std::move(allocations)]() mutable {
        for (VkFramebuffer framebuffer : framebuffers)
            vkDestroyFramebuffer(device, framebuffer, nullptr);
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
        m_deletionQueue->Push(std::move(destroyRetired));
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
    allocations.insert(m_aliasedMemoryHeaps.begin(), m_aliasedMemoryHeaps.end());

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
    allocations.insert(m_aliasedMemoryHeaps.begin(), m_aliasedMemoryHeaps.end());
    return allocations.size();
}

} // namespace vk
} // namespace infernux
