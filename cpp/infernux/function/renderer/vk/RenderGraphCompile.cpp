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

// ============================================================================
// Pass Culling & Resource Lifetimes
// ============================================================================

void RenderGraph::CullPasses()
{
    // Mark all passes as potentially culled
    for (auto &pass : m_passes) {
        pass.refCount = 0;
        pass.culled = true;
    }

    // Find passes that write to the output
    std::queue<uint32_t> workQueue;

    for (uint32_t i = 0; i < m_passes.size(); i++) {
        for (const auto &write : m_passes[i].writes) {
            if (write.handle == m_output || write.handle == m_backbuffer) {
                m_passes[i].culled = false;
                m_passes[i].refCount = 1;
                workQueue.push(i);
                break;
            }
        }
    }

    // Backward propagation
    while (!workQueue.empty()) {
        uint32_t passId = workQueue.front();
        workQueue.pop();

        const auto &pass = m_passes[passId];

        // Find passes that produce resources this pass reads
        for (const auto &read : pass.reads) {
            for (uint32_t i = 0; i < m_passes.size(); i++) {
                if (i == passId)
                    continue;

                for (const auto &write : m_passes[i].writes) {
                    if (write.handle.id == read.handle.id) {
                        if (m_passes[i].culled) {
                            m_passes[i].culled = false;
                            workQueue.push(i);
                        }
                        m_passes[i].refCount++;
                    }
                }
            }
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

    for (uint32_t i = 0; i < m_passes.size(); i++) {
        if (m_passes[i].culled)
            continue;

        for (const auto &read : m_passes[i].reads) {
            auto &resource = m_resources[read.handle.id];
            resource.firstPass = std::min(resource.firstPass, i);
            resource.lastPass = std::max(resource.lastPass, i);
            resource.refCount++;
        }

        for (const auto &write : m_passes[i].writes) {
            auto &resource = m_resources[write.handle.id];
            resource.firstPass = std::min(resource.firstPass, i);
            resource.lastPass = std::max(resource.lastPass, i);
            resource.refCount++;
        }
    }
}

// ============================================================================
// Topological Sort (Kahn's Algorithm)
// ============================================================================

void RenderGraph::TopologicalSort()
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
        return;
    }

    // Build adjacency list: edge A→B means pass A must execute before pass B
    // (A writes a resource that B reads)
    std::unordered_map<uint32_t, std::vector<uint32_t>> adjacency; // passId → [dependent passes]
    std::unordered_map<uint32_t, uint32_t> inDegree;

    for (uint32_t passId : activePasses) {
        adjacency[passId] = {};
        inDegree[passId] = 0;
    }

    // ====================================================================
    // Build a resource_id → writer_pass_id map first, then connect reader
    // passes from that map. This avoids the previous deeply nested scan.
    // ====================================================================

    // A single resource may be written by multiple passes (e.g. depth
    // written and then rewritten by a later pass).  Store ALL writers.
    std::unordered_map<uint32_t, std::vector<uint32_t>> resourceWriters; // resource_id → [writer pass ids]
    std::unordered_map<uint32_t, std::vector<uint32_t>> depthWriters;    // depth resource_id → [writer pass ids]

    for (uint32_t writePassId : activePasses) {
        const auto &writePass = m_passes[writePassId];
        for (const auto &write : writePass.writes) {
            resourceWriters[write.handle.id].push_back(writePassId);
        }
        if (writePass.depthOutput.IsValid()) {
            depthWriters[writePass.depthOutput.id].push_back(writePassId);
        }
    }

    // For each reader pass, look up writers via the map — O(1) per resource
    // For each reader pass, look up writers via the map.
    // IMPORTANT: Only create edges from writers declared BEFORE the reader
    // (writePassId < readPassId).  Without this constraint, a resource that
    // is written by an early pass, read by a middle pass, and written again
    // by a late pass would create a spurious backward edge late→middle,
    // forming a cycle.  The render graph does not version resources, so
    // declaration order is used as a proxy for the intended data-flow
    // timeline.  This matches the Python-side declaration order which is
    // always the logical execution order.
    for (uint32_t readPassId : activePasses) {
        const auto &readPass = m_passes[readPassId];

        for (const auto &read : readPass.reads) {
            auto it = resourceWriters.find(read.handle.id);
            if (it != resourceWriters.end()) {
                for (uint32_t writePassId : it->second) {
                    if (writePassId != readPassId && writePassId < readPassId) {
                        adjacency[writePassId].push_back(readPassId);
                        inDegree[readPassId]++;
                    }
                }
            }
        }

        // depthInput: if a pass reads depth from another pass
        if (readPass.depthInput.IsValid()) {
            auto it = depthWriters.find(readPass.depthInput.id);
            if (it != depthWriters.end()) {
                for (uint32_t writePassId : it->second) {
                    if (writePassId != readPassId && writePassId < readPassId) {
                        adjacency[writePassId].push_back(readPassId);
                        inDegree[readPassId]++;
                    }
                }
            }
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
                     activePasses.size(), " passes. Falling back to declaration order.");
        m_executionOrder.clear();
        for (uint32_t passId : activePasses) {
            m_executionOrder.push_back(passId);
        }
    }
}

// ============================================================================
// Helper Functions
// ============================================================================

VkImageLayout RenderGraph::UsageToLayout(ResourceUsage usage, ResourceType type)
{
    if (static_cast<int>(usage & ResourceUsage::ColorOutput) != 0)
        return VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    if (static_cast<int>(usage & ResourceUsage::DepthOutput) != 0)
        return VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    if (static_cast<int>(usage & ResourceUsage::DepthRead) != 0)
        return VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    if (static_cast<int>(usage & ResourceUsage::ShaderRead) != 0)
        return VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    if (static_cast<int>(usage & ResourceUsage::Transfer) != 0)
        return VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    if (static_cast<int>(usage & ResourceUsage::ReadWrite) != 0)
        return VK_IMAGE_LAYOUT_GENERAL;
    return VK_IMAGE_LAYOUT_UNDEFINED;
}

VkAccessFlags RenderGraph::UsageToAccessMask(ResourceUsage usage)
{
    VkAccessFlags flags = 0;
    if (static_cast<int>(usage & ResourceUsage::ColorOutput) != 0)
        flags |= VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    if (static_cast<int>(usage & ResourceUsage::DepthOutput) != 0)
        flags |= VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
    if (static_cast<int>(usage & ResourceUsage::DepthRead) != 0)
        flags |= VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_READ_BIT;
    if (static_cast<int>(usage & ResourceUsage::ShaderRead) != 0)
        flags |= VK_ACCESS_SHADER_READ_BIT;
    if (static_cast<int>(usage & ResourceUsage::Transfer) != 0)
        flags |= VK_ACCESS_TRANSFER_READ_BIT;
    if (static_cast<int>(usage & (ResourceUsage::ReadWrite)) != 0)
        flags |= VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT;
    if (static_cast<int>(usage & ResourceUsage::Read) != 0 && static_cast<int>(usage & ResourceUsage::Write) == 0 &&
        flags == 0)
        flags = VK_ACCESS_SHADER_READ_BIT;
    return flags;
}

VkPipelineStageFlags RenderGraph::UsageToStageFlags(ResourceUsage usage)
{
    VkPipelineStageFlags flags = 0;
    if (static_cast<int>(usage & ResourceUsage::ColorOutput) != 0)
        flags |= VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    if (static_cast<int>(usage & ResourceUsage::DepthOutput) != 0)
        flags |= VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
    if (static_cast<int>(usage & ResourceUsage::DepthRead) != 0)
        flags |= VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
    if (static_cast<int>(usage & ResourceUsage::ShaderRead) != 0)
        flags |= VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT;
    if (static_cast<int>(usage & ResourceUsage::Transfer) != 0)
        flags |= VK_PIPELINE_STAGE_TRANSFER_BIT;
    if (flags == 0)
        flags = VK_PIPELINE_STAGE_ALL_GRAPHICS_BIT;
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
                                         bool clearColor, bool clearDepth, bool storeDepth,
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
            // Buffers are allocated individually (no aliasing benefit for buffers)
            VkBufferCreateInfo bufferInfo{};
            bufferInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
            bufferInfo.size = resource.bufferDesc.size;
            bufferInfo.usage = resource.bufferDesc.usage;
            bufferInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;

            if (vkCreateBuffer(device, &bufferInfo, nullptr, &resource.allocatedBuffer) != VK_SUCCESS) {
                INXLOG_ERROR("Failed to create buffer for resource: ", resource.name);
                return false;
            }

            VmaAllocator allocator = m_context->GetVmaAllocator();
            VmaAllocationCreateInfo allocCreateInfo{};
            allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;

            VmaAllocationInfo vmaAllocInfo;
            if (vmaAllocateMemoryForBuffer(allocator, resource.allocatedBuffer, &allocCreateInfo,
                                           &resource.allocatedMemory, &vmaAllocInfo) != VK_SUCCESS) {
                INXLOG_ERROR("Failed to allocate memory for buffer: ", resource.name);
                return false;
            }

            vkBindBufferMemory(device, resource.allocatedBuffer, vmaAllocInfo.deviceMemory, vmaAllocInfo.offset);
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
        config.colorFinalLayout = m_backbufferFinalLayout;

        // Use RenderPass cache
        // Include MRT attachment count + formats in cache key
        size_t cacheKey = HashRenderPassConfig(
            colorFormat, depthFormat, sampleCount, config.clearColor, config.clearDepth, config.storeDepth,
            config.colorFinalLayout, config.hasResolve, config.resolveFormat, hasColorOutputs, config.readOnlyDepth);
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
    VkPipelineStageFlags srcStageMask = 0;
    VkPipelineStageFlags dstStageMask = 0;

    // Helper: generate barrier for a resource access
    auto addBarrier = [&](const ResourceAccess &access, bool isDepthInput = false) {
        if (access.handle.id >= m_resources.size())
            return;

        const auto &resource = m_resources[access.handle.id];
        if (resource.type == ResourceType::Buffer)
            return;

        VkImage image = resource.isExternal ? resource.externalImage : resource.allocatedImage;
        if (image == VK_NULL_HANDLE)
            return;

        // Look up previous state (direct index — vectors kept in sync with m_resources)
        const auto &prevState = m_resourceStates[access.handle.id];
        VkImageLayout oldLayout = prevState.layout;
        VkAccessFlags srcAccessMask = prevState.accessMask;
        VkPipelineStageFlags srcStages = prevState.stages;
        if (srcStages == 0)
            srcStages = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;

        VkImageLayout newLayout = access.layout;

        // Skip barrier if layout is already correct and no write hazard
        if (oldLayout == newLayout && (static_cast<int>(access.usage & ResourceUsage::Write) == 0) &&
            (srcAccessMask & (VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT |
                              VK_ACCESS_SHADER_WRITE_BIT | VK_ACCESS_TRANSFER_WRITE_BIT)) == 0) {
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
        barrier.dstAccessMask = access.access;

        m_barrierScratch.push_back(barrier);
        srcStageMask |= srcStages;
        dstStageMask |= access.stages;
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

    if (hasReadWriteOverlap && !m_barrierScratch.empty()) {
        if (srcStageMask == 0)
            srcStageMask = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
        if (dstStageMask == 0)
            dstStageMask = VK_PIPELINE_STAGE_ALL_GRAPHICS_BIT;

        vkCmdPipelineBarrier(cmdBuffer, srcStageMask, dstStageMask, 0, 0, nullptr, 0, nullptr,
                             static_cast<uint32_t>(m_barrierScratch.size()), m_barrierScratch.data());

        m_barrierScratch.clear();
        srcStageMask = 0;
        dstStageMask = 0;
    }

    for (const auto &read : pass.reads) {
        if (read.handle.id < m_resources.size()) {
            m_resourceStates[read.handle.id] = {read.layout, read.access, read.stages, passIndex};
        }
    }

    // Process write accesses
    for (const auto &write : pass.writes) {
        addBarrier(write);
    }

    if (!m_barrierScratch.empty()) {
        if (srcStageMask == 0)
            srcStageMask = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
        if (dstStageMask == 0)
            dstStageMask = VK_PIPELINE_STAGE_ALL_GRAPHICS_BIT;

        vkCmdPipelineBarrier(cmdBuffer, srcStageMask, dstStageMask, 0, 0, nullptr, 0, nullptr,
                             static_cast<uint32_t>(m_barrierScratch.size()), m_barrierScratch.data());
    }

    // Update resource states after this pass executes.
    //
    // Write accesses override read state (a pass that both reads and writes
    // a resource leaves it in the write layout).
    for (const auto &write : pass.writes) {
        if (write.handle.id < m_resources.size()) {
            VkImageLayout postPassLayout = write.layout;

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
                        postPassLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL;
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
        m_resourceStates[pass.depthInput.id] = {
            VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL, VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_READ_BIT,
            VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT, passIndex};
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

    // RHI view registrations are graph-resource lifetime aliases, including
    // external views. Release them before the no-transient early-out and
    // before any owned VkImageView is destroyed.
    if (m_rhiDevice) {
        for (auto &resource : m_resources) {
            m_rhiDevice->Release(resource.rhiView);
            resource.rhiView = {};
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

    if (!m_context->IsShuttingDown()) {
        vkDeviceWaitIdle(device);
        SDL_PumpEvents();
    }

    // Framebuffers reference VkImageViews that we are about to destroy.
    // Cached framebuffers become INVALID when their referenced views are
    // destroyed (Vulkan spec: "a framebuffer is invalid if one of its
    // referenced VkImageViews was destroyed").  If the driver recycles
    // VkImageView handles, a subsequent Compile could return a stale
    // framebuffer from the cache via hash collision, causing device lost.
    // Flush the entire framebuffer cache to prevent this.
    for (auto &[key, entry] : m_framebufferCache) {
        if (entry.framebuffer != VK_NULL_HANDLE) {
            vkDestroyFramebuffer(device, entry.framebuffer, nullptr);
        }
    }
    m_framebufferCache.clear();

    // Clear pass framebuffer references.
    for (auto &pass : m_passes) {
        pass.framebuffer = VK_NULL_HANDLE;
    }

    // Free allocated transient resources (images/buffers/memory)
    for (auto &resource : m_resources) {
        if (resource.isExternal)
            continue;

        if (resource.allocatedView != VK_NULL_HANDLE) {
            vkDestroyImageView(device, resource.allocatedView, nullptr);
            resource.allocatedView = VK_NULL_HANDLE;
        }

        if (resource.allocatedImage != VK_NULL_HANDLE) {
            vkDestroyImage(device, resource.allocatedImage, nullptr);
            resource.allocatedImage = VK_NULL_HANDLE;
        }

        if (resource.allocatedBuffer != VK_NULL_HANDLE) {
            vkDestroyBuffer(device, resource.allocatedBuffer, nullptr);
            resource.allocatedBuffer = VK_NULL_HANDLE;
        }

        if (resource.allocatedMemory != VK_NULL_HANDLE) {
            vmaFreeMemory(m_context->GetVmaAllocator(), resource.allocatedMemory);
            resource.allocatedMemory = VK_NULL_HANDLE;
        }
    }

    // Free aliased memory heaps (not owned by any single resource)
    VmaAllocator allocator = m_context->GetVmaAllocator();
    for (VmaAllocation heap : m_aliasedMemoryHeaps) {
        if (heap != VK_NULL_HANDLE) {
            vmaFreeMemory(allocator, heap);
        }
    }
    m_aliasedMemoryHeaps.clear();
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

} // namespace vk
} // namespace infernux
