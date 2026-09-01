/**
 * @file RenderGraph.cpp
 * @brief RenderGraph public API — RenderContext, PassBuilder, lifecycle, Compile/Execute
 *        orchestration, and resource resolution.
 *
 * Compilation internals (culling, sorting, allocation, barriers, caching) are in
 * RenderGraphCompile.cpp.
 */

#include "RenderGraph.h"
#include "RhiVulkanTypes.h"
#include "VkDeviceContext.h"
#include "VulkanQueueManager.h"
#include <core/error/InxError.h>
#include <function/renderer/ProfileConfig.h>

#include <algorithm>
#include <chrono>
#include <sstream>
#include <utility>

namespace infernux
{
namespace vk
{

namespace
{
constexpr size_t QueueRoleIndex(rhi::QueueRole role) noexcept
{
    return static_cast<size_t>(role);
}

rhi::QueueRole QueueRoleForPass(PassType type) noexcept
{
    switch (type) {
    case PassType::Graphics:
        return rhi::QueueRole::Graphics;
    case PassType::Compute:
        return rhi::QueueRole::Compute;
    case PassType::Transfer:
        return rhi::QueueRole::Transfer;
    case PassType::Present:
        // A RenderGraph Present pass records the final image-layout transition.
        // vkQueuePresentKHR itself is owned by PresentationManager and happens
        // after command submission; a presentation-only queue cannot record or
        // execute pipeline barriers.
        return rhi::QueueRole::Graphics;
    }
    return rhi::QueueRole::Graphics;
}

} // namespace

#if INFERNUX_FRAME_PROFILE
RenderGraph::ExecuteProfileSnapshot RenderGraph::GetExecuteProfileSnapshot()
{
    return s_executeProfile;
}

std::vector<RenderGraph::PassCallbackProfileEntry> RenderGraph::GetTopCallbackProfiles(size_t maxEntries)
{
    std::vector<PassCallbackProfileEntry> result;
    result.reserve(s_callbackProfiles.size());
    for (const auto &entry : s_callbackProfiles) {
        result.push_back(entry.second);
    }

    std::sort(result.begin(), result.end(), [](const PassCallbackProfileEntry &a, const PassCallbackProfileEntry &b) {
        if (a.totalMs != b.totalMs)
            return a.totalMs > b.totalMs;
        return a.name < b.name;
    });

    if (result.size() > maxEntries) {
        result.resize(maxEntries);
    }
    return result;
}

std::vector<RenderGraph::ParticlePassProfileEntry> RenderGraph::GetParticlePassProfiles(size_t maxEntries)
{
    std::vector<ParticlePassProfileEntry> result;
    result.reserve(s_particlePassProfiles.size());
    for (const auto &entry : s_particlePassProfiles)
        result.push_back(entry.second);

    std::sort(result.begin(), result.end(), [](const ParticlePassProfileEntry &a, const ParticlePassProfileEntry &b) {
        if (a.workgroups != b.workgroups)
            return a.workgroups > b.workgroups;
        if (a.inputCount != b.inputCount)
            return a.inputCount > b.inputCount;
        return a.name < b.name;
    });
    if (result.size() > maxEntries)
        result.resize(maxEntries);
    return result;
}

void RenderGraph::ResetExecuteProfileSnapshot()
{
    s_executeProfile = {};
    s_callbackProfiles.clear();
    s_particlePassProfiles.clear();
}

void RenderGraph::RecordParticleDispatch(uint32_t passId, uint32_t groupCountX, uint32_t groupCountY,
                                         uint32_t groupCountZ, uint64_t inputCount, bool indirect)
{
    if (passId >= m_passes.size())
        return;
    const auto &pass = m_passes[passId];
    if (pass.name.find("GpuParticle") == std::string::npos)
        return;

    auto &profile = s_particlePassProfiles[pass.name];
    profile.name = pass.name;
    ++profile.dispatchCalls;
    if (indirect)
        ++profile.indirectDispatchCalls;
    else
        ++profile.directDispatchCalls;
    profile.workgroups += static_cast<uint64_t>(groupCountX) * groupCountY * groupCountZ;
    profile.inputCount += inputCount;
}
#endif

// ============================================================================
// RenderContext Implementation
// ============================================================================

RenderContext::RenderContext(VkCommandBuffer cmdBuffer, RenderGraph *graph) : m_cmdBuffer(cmdBuffer), m_graph(graph)
{
    if (m_graph && m_graph->m_rhiDevice) {
        m_graphicsEncoder = m_graph->m_rhiDevice->MakeGraphicsCommandEncoder(m_graphicsCommandContext, cmdBuffer);
        m_computeEncoder = m_graph->m_rhiDevice->MakeComputeCommandEncoder(m_computeCommandContext, cmdBuffer);
        m_transferEncoder = m_graph->m_rhiDevice->MakeTransferCommandEncoder(m_transferCommandContext, cmdBuffer);
    }
}

#if INFERNUX_FRAME_PROFILE
void RenderContext::RecordComputeDispatch(uint32_t groupCountX, uint32_t groupCountY, uint32_t groupCountZ,
                                          uint64_t inputCount, bool indirect)
{
    if (m_graph && m_activePassId != UINT32_MAX)
        m_graph->RecordParticleDispatch(m_activePassId, groupCountX, groupCountY, groupCountZ, inputCount, indirect);
}

void RenderContext::RecordParticleDraw(bool indirect)
{
    if (!m_graph || m_activePassId == UINT32_MAX || m_activePassId >= m_graph->m_passes.size())
        return;
    const auto &pass = m_graph->m_passes[m_activePassId];
    auto &profile = RenderGraph::s_particlePassProfiles[pass.name];
    profile.name = pass.name;
    ++profile.passCalls;
    ++profile.drawCalls;
    if (indirect)
        ++profile.indirectDrawCalls;
}
#endif

void RenderContext::SetViewport(const VkViewport &viewport)
{
    m_viewport = viewport;
    vkCmdSetViewport(m_cmdBuffer, 0, 1, &m_viewport);
}

void RenderContext::SetScissor(const VkRect2D &scissor)
{
    m_scissor = scissor;
    vkCmdSetScissor(m_cmdBuffer, 0, 1, &m_scissor);
}

void RenderContext::BindPipeline(VkPipeline pipeline)
{
    m_graphicsCommandContext.ResetBindingState();
    vkCmdBindPipeline(m_cmdBuffer, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline);
}

void RenderContext::Draw(uint32_t vertexCount, uint32_t instanceCount, uint32_t firstVertex, uint32_t firstInstance)
{
    vkCmdDraw(m_cmdBuffer, vertexCount, instanceCount, firstVertex, firstInstance);
}

void RenderContext::DrawIndexed(uint32_t indexCount, uint32_t instanceCount, uint32_t firstIndex, int32_t vertexOffset,
                                uint32_t firstInstance)
{
    vkCmdDrawIndexed(m_cmdBuffer, indexCount, instanceCount, firstIndex, vertexOffset, firstInstance);
}

void RenderContext::NextSubpass()
{
    m_graphicsCommandContext.ResetBindingState();
    vkCmdNextSubpass(m_cmdBuffer, VK_SUBPASS_CONTENTS_INLINE);
}

VkImageView RenderContext::GetTexture(ResourceHandle handle) const
{
    return m_graph ? m_graph->ResolveTextureView(handle) : VK_NULL_HANDLE;
}

rhi::TextureViewHandle RenderContext::GetTextureView(ResourceHandle handle) const
{
    return m_graph ? m_graph->ResolveRhiTextureView(handle) : rhi::TextureViewHandle{};
}

rhi::TextureHandle RenderContext::GetTextureHandle(ResourceHandle handle) const
{
    return m_graph ? m_graph->ResolveRhiTexture(handle) : rhi::TextureHandle{};
}

VkBuffer RenderContext::GetBuffer(ResourceHandle handle) const
{
    return m_graph ? m_graph->ResolveBuffer(handle) : VK_NULL_HANDLE;
}

rhi::BufferHandle RenderContext::GetBufferHandle(ResourceHandle handle) const
{
    return m_graph ? m_graph->ResolveRhiBuffer(handle) : rhi::BufferHandle{};
}

const RendererList *RenderContext::GetRendererList(ResourceHandle handle) const
{
    return m_graph ? m_graph->ResolveRendererList(handle) : nullptr;
}

// ============================================================================
// PassBuilder Implementation
// ============================================================================

PassBuilder::PassBuilder(RenderGraph *graph, uint32_t passId) : m_graph(graph), m_passId(passId)
{
}

ResourceHandle PassBuilder::CreateTexture(const std::string &name, uint32_t width, uint32_t height, VkFormat format,
                                          VkSampleCountFlagBits samples)
{
    ResourceHandle handle = m_graph->CreateResource(name, ResourceType::Texture2D);
    if (!m_graph->Owns(handle)) {
        return handle;
    }

    auto &resource = m_graph->m_resources[handle.id];
    resource.textureDesc.name = name;
    resource.textureDesc.width = width;
    resource.textureDesc.height = height;
    resource.textureDesc.format = format;
    resource.textureDesc.samples = samples;
    resource.textureDesc.isTransient = true;

    return handle;
}

ResourceHandle PassBuilder::CreateDepthStencil(const std::string &name, uint32_t width, uint32_t height,
                                               VkFormat format, VkSampleCountFlagBits samples)
{
    ResourceHandle handle = m_graph->CreateResource(name, ResourceType::DepthStencil);
    if (!m_graph->Owns(handle)) {
        return handle;
    }

    auto &resource = m_graph->m_resources[handle.id];
    resource.textureDesc.name = name;
    resource.textureDesc.width = width;
    resource.textureDesc.height = height;
    resource.textureDesc.format = format;
    resource.textureDesc.samples = samples;
    resource.textureDesc.isTransient = true;

    return handle;
}

ResourceHandle PassBuilder::CreateBuffer(const std::string &name, VkDeviceSize size, VkBufferUsageFlags usage)
{
    ResourceHandle handle = m_graph->CreateResource(name, ResourceType::Buffer);
    if (!m_graph->Owns(handle)) {
        return handle;
    }

    auto &resource = m_graph->m_resources[handle.id];
    resource.bufferDesc.name = name;
    resource.bufferDesc.size = size;
    resource.bufferDesc.usage = usage;
    resource.bufferDesc.isTransient = true;

    return handle;
}

ResourceHandle PassBuilder::ImportTexture(const std::string &name, VkImage image, VkImageView view, VkFormat format,
                                          uint32_t width, uint32_t height)
{
    ResourceHandle handle = m_graph->CreateResource(name, ResourceType::Texture2D);
    if (!m_graph->Owns(handle)) {
        return handle;
    }

    auto &resource = m_graph->m_resources[handle.id];
    resource.textureDesc.name = name;
    resource.textureDesc.width = width;
    resource.textureDesc.height = height;
    resource.textureDesc.format = format;
    resource.textureDesc.isTransient = false;
    resource.isExternal = true;
    resource.externalImage = image;
    resource.externalView = view;
    resource.rhiView =
        m_graph->m_rhiDevice ? m_graph->m_rhiDevice->RegisterTextureView(view) : rhi::TextureViewHandle{};
    resource.rhiTexture = m_graph->m_rhiDevice ? m_graph->m_rhiDevice->RegisterTexture(image) : rhi::TextureHandle{};

    return handle;
}

ResourceHandle PassBuilder::ImportBuffer(const std::string &name, VkBuffer buffer, VkDeviceSize size)
{
    if (buffer != VK_NULL_HANDLE) {
        for (uint32_t resourceId = 0; resourceId < m_graph->m_resources.size(); ++resourceId) {
            auto &resource = m_graph->m_resources[resourceId];
            if (!resource.isExternal || resource.type != ResourceType::Buffer || resource.externalBuffer != buffer)
                continue;
            if (size > resource.bufferDesc.size) {
                resource.bufferDesc.size = size;
                if (m_graph->m_rhiDevice) {
                    m_graph->m_rhiDevice->Release(resource.rhiBuffer);
                    resource.rhiBuffer = m_graph->m_rhiDevice->RegisterBuffer(buffer, size);
                    if (!resource.rhiBuffer.IsValid())
                        return {};
                }
            }
            return {m_graph->m_identity.Current(), resourceId, m_graph->m_resourceVersions[resourceId]};
        }
    }

    ResourceHandle handle = m_graph->CreateResource(name, ResourceType::Buffer);
    if (!m_graph->Owns(handle)) {
        return handle;
    }

    auto &resource = m_graph->m_resources[handle.id];
    resource.bufferDesc.name = name;
    resource.bufferDesc.size = size;
    resource.bufferDesc.isTransient = false;
    resource.isExternal = true;
    resource.externalBuffer = buffer;
    resource.rhiBuffer =
        m_graph->m_rhiDevice ? m_graph->m_rhiDevice->RegisterBuffer(buffer, size) : rhi::BufferHandle{};

    return handle;
}

ResourceHandle PassBuilder::ImportBuffer(const std::string &name, rhi::BufferHandle buffer, uint64_t size)
{
    if (!buffer.IsValid() || size == 0 || !m_graph->m_rhiDevice)
        return {};
    const VkBuffer nativeBuffer = m_graph->m_rhiDevice->Resolve(buffer);
    if (nativeBuffer == VK_NULL_HANDLE)
        return {};
    const ResourceHandle handle = ImportBuffer(name, nativeBuffer, static_cast<VkDeviceSize>(size));
    if (m_graph->Owns(handle))
        m_graph->m_resources[handle.id].concurrentQueueSharing =
            m_graph->m_rhiDevice->UsesConcurrentQueueSharing(buffer);
    return handle;
}

ResourceHandle PassBuilder::Read(ResourceHandle handle, rhi::PipelineStage stages)
{
    if (!m_graph->Owns(handle)) {
        return handle;
    }

    auto &pass = m_graph->m_passes[m_passId];

    ResourceAccess access;
    access.handle = handle;
    access.usage = ResourceUsage::Read | ResourceUsage::ShaderRead;
    access.stages = stages;
    access.access = rhi::Access::ShaderRead;
    access.layout = rhi::TextureLayout::ShaderReadOnly;

    pass.reads.push_back(access);

    return handle;
}

ResourceHandle PassBuilder::ReadSampledDepth(ResourceHandle handle, rhi::PipelineStage stages)
{
    if (!m_graph->Owns(handle)) {
        return handle;
    }

    auto &pass = m_graph->m_passes[m_passId];

    ResourceAccess access;
    access.handle = handle;
    // ShaderRead is required so AllocateResources adds VK_IMAGE_USAGE_SAMPLED_BIT
    // to DepthStencil images; without it the GPU cannot sample the depth texture.
    access.usage = ResourceUsage::Read | ResourceUsage::DepthRead | ResourceUsage::ShaderRead;
    access.stages = stages;
    access.access = rhi::Access::ShaderRead;
    access.layout = rhi::TextureLayout::DepthStencilReadOnly;

    pass.reads.push_back(access);

    return handle;
}

ResourceHandle PassBuilder::WriteColor(ResourceHandle handle, uint32_t attachmentIndex)
{
    if (!m_graph->Owns(handle)) {
        return {};
    }

    ResourceHandle newHandle = m_graph->AdvanceResourceVersion(handle);
    if (!newHandle.IsValid())
        return {};

    auto &pass = m_graph->m_passes[m_passId];

    // Model the previous attachment version as a graph-only input. A later
    // SetClearColor() removes this dependency when the pass fully overwrites it.
    pass.reads.push_back({handle, ResourceUsage::Read | ResourceUsage::VersionDependency, rhi::PipelineStage::None,
                          rhi::Access::None, rhi::TextureLayout::Undefined});

    ResourceAccess access;
    access.handle = newHandle;
    access.usage = ResourceUsage::Write | ResourceUsage::ColorOutput;
    access.stages = rhi::PipelineStage::ColorOutput;
    access.access = rhi::Access::ColorWrite;
    access.layout = rhi::TextureLayout::ColorAttachment;

    pass.writes.push_back(access);

    // Ensure color outputs vector is large enough
    if (pass.colorOutputs.size() <= attachmentIndex) {
        pass.colorOutputs.resize(attachmentIndex + 1);
    }
    pass.colorOutputs[attachmentIndex] = newHandle;

    return newHandle;
}

ResourceHandle PassBuilder::WriteDepth(ResourceHandle handle)
{
    if (!m_graph->Owns(handle)) {
        return {};
    }

    ResourceHandle newHandle = m_graph->AdvanceResourceVersion(handle);
    if (!newHandle.IsValid())
        return {};

    auto &pass = m_graph->m_passes[m_passId];

    pass.reads.push_back({handle, ResourceUsage::Read | ResourceUsage::VersionDependency, rhi::PipelineStage::None,
                          rhi::Access::None, rhi::TextureLayout::Undefined});

    ResourceAccess access;
    access.handle = newHandle;
    access.usage = ResourceUsage::Write | ResourceUsage::DepthOutput;
    access.stages = rhi::PipelineStage::EarlyDepth | rhi::PipelineStage::LateDepth;
    access.access = rhi::Access::DepthWrite;
    access.layout = rhi::TextureLayout::DepthStencilAttachment;

    pass.writes.push_back(access);
    pass.depthOutput = newHandle;

    return newHandle;
}

ResourceHandle PassBuilder::ReadDepth(ResourceHandle handle)
{
    if (!m_graph->Owns(handle)) {
        return handle;
    }

    auto &pass = m_graph->m_passes[m_passId];

    ResourceAccess access;
    access.handle = handle;
    access.usage = ResourceUsage::Read | ResourceUsage::DepthRead;
    access.stages = rhi::PipelineStage::EarlyDepth | rhi::PipelineStage::LateDepth;
    access.access = rhi::Access::DepthRead;
    // Read-only depth: the render pass uses DEPTH_STENCIL_READ_ONLY_OPTIMAL
    // for both the subpass attachment and initialLayout/finalLayout.
    // The barrier must transition to this layout (not ATTACHMENT_OPTIMAL).
    access.layout = rhi::TextureLayout::DepthStencilReadOnly;

    pass.reads.push_back(access);
    pass.depthInput = handle;

    return handle; // No version bump â€” read-only
}

ResourceHandle PassBuilder::ReadWrite(ResourceHandle handle, rhi::PipelineStage stages)
{
    if (!m_graph->Owns(handle)) {
        return {};
    }

    ResourceHandle newHandle = m_graph->AdvanceResourceVersion(handle);
    if (!newHandle.IsValid())
        return {};

    auto &pass = m_graph->m_passes[m_passId];

    pass.reads.push_back({handle, ResourceUsage::Read, stages, rhi::Access::ShaderRead, rhi::TextureLayout::General});
    pass.writes.push_back(
        {newHandle, ResourceUsage::Write, stages, rhi::Access::ShaderWrite, rhi::TextureLayout::General});

    return newHandle;
}

ResourceHandle PassBuilder::ReadStorageBuffer(ResourceHandle handle, rhi::PipelineStage stages)
{
    if (!m_graph->Owns(handle) || m_graph->m_resources[handle.id].type != ResourceType::Buffer)
        return handle;

    auto &pass = m_graph->m_passes[m_passId];
    pass.reads.push_back({handle, ResourceUsage::Read | ResourceUsage::ShaderRead, stages, rhi::Access::ShaderRead,
                          rhi::TextureLayout::Undefined});
    return handle;
}

ResourceHandle PassBuilder::ReadUniformBuffer(ResourceHandle handle)
{
    if (!m_graph->Owns(handle) || m_graph->m_resources[handle.id].type != ResourceType::Buffer)
        return handle;

    auto &pass = m_graph->m_passes[m_passId];
    pass.reads.push_back({handle, ResourceUsage::Read | ResourceUsage::ShaderRead, rhi::PipelineStage::ComputeShader,
                          rhi::Access::UniformRead, rhi::TextureLayout::Undefined});
    return handle;
}

ResourceHandle PassBuilder::WriteStorageBuffer(ResourceHandle handle)
{
    if (!m_graph->Owns(handle) || m_graph->m_resources[handle.id].type != ResourceType::Buffer)
        return {};

    ResourceHandle next = m_graph->AdvanceResourceVersion(handle);
    if (!next.IsValid())
        return {};

    auto &pass = m_graph->m_passes[m_passId];
    pass.writes.push_back({next, ResourceUsage::Write, rhi::PipelineStage::ComputeShader, rhi::Access::ShaderWrite,
                           rhi::TextureLayout::Undefined});
    return next;
}

ResourceHandle PassBuilder::WriteStorageTexture(ResourceHandle handle)
{
    if (!m_graph->Owns(handle) || m_graph->m_resources[handle.id].type == ResourceType::Buffer ||
        m_graph->m_resources[handle.id].type == ResourceType::RendererList) {
        return {};
    }

    ResourceHandle next = m_graph->AdvanceResourceVersion(handle);
    if (!next.IsValid())
        return {};

    auto &pass = m_graph->m_passes[m_passId];
    pass.reads.push_back({handle, ResourceUsage::Read | ResourceUsage::VersionDependency, rhi::PipelineStage::None,
                          rhi::Access::None, rhi::TextureLayout::Undefined});
    pass.writes.push_back({next, ResourceUsage::Write | ResourceUsage::Storage, rhi::PipelineStage::ComputeShader,
                           rhi::Access::ShaderWrite, rhi::TextureLayout::General});
    return next;
}

ResourceHandle PassBuilder::ReadIndirectBuffer(ResourceHandle handle)
{
    if (!m_graph->Owns(handle) || m_graph->m_resources[handle.id].type != ResourceType::Buffer)
        return handle;

    auto &pass = m_graph->m_passes[m_passId];
    pass.reads.push_back({handle, ResourceUsage::Read | ResourceUsage::IndirectArgument,
                          rhi::PipelineStage::DrawIndirect, rhi::Access::IndirectRead, rhi::TextureLayout::Undefined});
    return handle;
}

ResourceHandle PassBuilder::ReadRendererList(ResourceHandle handle)
{
    if (!m_graph->Owns(handle) || m_graph->m_resources[handle.id].type != ResourceType::RendererList)
        return handle;

    auto &pass = m_graph->m_passes[m_passId];
    pass.reads.push_back({handle, ResourceUsage::Read | ResourceUsage::RendererListRead, rhi::PipelineStage::None,
                          rhi::Access::None, rhi::TextureLayout::Undefined});
    pass.rendererListInputs.push_back(handle);
    return handle;
}

void PassBuilder::SkipCallbackWhenRendererListsEmpty(bool enabled)
{
    m_graph->m_passes[m_passId].skipCallbackWhenRendererListsEmpty = enabled;
}

ResourceHandle PassBuilder::TransferRead(ResourceHandle handle)
{
    if (!m_graph->Owns(handle)) {
        return handle;
    }

    auto &pass = m_graph->m_passes[m_passId];

    ResourceAccess access;
    access.handle = handle;
    access.usage = ResourceUsage::Read | ResourceUsage::Transfer;
    access.stages = rhi::PipelineStage::Transfer;
    access.access = rhi::Access::TransferRead;
    access.layout = rhi::TextureLayout::TransferSource;

    pass.reads.push_back(access);

    return handle;
}

ResourceHandle PassBuilder::TransferWrite(ResourceHandle handle)
{
    if (!m_graph->Owns(handle)) {
        return {};
    }

    ResourceHandle newHandle = m_graph->AdvanceResourceVersion(handle);
    if (!newHandle.IsValid())
        return {};

    auto &pass = m_graph->m_passes[m_passId];

    ResourceAccess access;
    access.handle = newHandle;
    access.usage = ResourceUsage::Write | ResourceUsage::Transfer;
    access.stages = rhi::PipelineStage::Transfer;
    access.access = rhi::Access::TransferWrite;
    access.layout = rhi::TextureLayout::TransferDestination;

    pass.writes.push_back(access);

    return newHandle;
}

ResourceHandle PassBuilder::PrepareColorAttachment(ResourceHandle handle)
{
    if (!m_graph->Owns(handle) || m_graph->m_resources[handle.id].type == ResourceType::Buffer ||
        m_graph->m_resources[handle.id].type == ResourceType::RendererList) {
        return handle;
    }

    auto &pass = m_graph->m_passes[m_passId];
    pass.reads.push_back({handle, ResourceUsage::Read, rhi::PipelineStage::ColorOutput, rhi::Access::ColorRead,
                          rhi::TextureLayout::ColorAttachment});
    return handle;
}

ResourceHandle PassBuilder::PrepareDepthStencilAttachment(ResourceHandle handle)
{
    if (!m_graph->Owns(handle) || m_graph->m_resources[handle.id].type == ResourceType::Buffer ||
        m_graph->m_resources[handle.id].type == ResourceType::RendererList) {
        return handle;
    }

    auto &pass = m_graph->m_passes[m_passId];
    pass.reads.push_back({handle, ResourceUsage::Read, rhi::PipelineStage::EarlyDepth | rhi::PipelineStage::LateDepth,
                          rhi::Access::DepthRead, rhi::TextureLayout::DepthStencilAttachment});
    return handle;
}

ResourceHandle PassBuilder::PresentRead(ResourceHandle handle)
{
    if (!m_graph->Owns(handle) || m_graph->m_resources[handle.id].type == ResourceType::Buffer ||
        m_graph->m_resources[handle.id].type == ResourceType::RendererList) {
        return handle;
    }

    auto &pass = m_graph->m_passes[m_passId];
    pass.reads.push_back({handle, ResourceUsage::Read | ResourceUsage::Present, rhi::PipelineStage::Bottom,
                          rhi::Access::MemoryRead, rhi::TextureLayout::Present});
    pass.hasSideEffect = true;
    return handle;
}

ResourceHandle PassBuilder::WriteResolve(ResourceHandle handle)
{
    if (!m_graph->Owns(handle)) {
        return {};
    }

    ResourceHandle newHandle = m_graph->AdvanceResourceVersion(handle);
    if (!newHandle.IsValid())
        return {};

    auto &pass = m_graph->m_passes[m_passId];
    pass.resolveOutput = newHandle;

    // Track as a write so dependency/lifetime analysis picks it up
    ResourceAccess access;
    access.handle = newHandle;
    access.usage = ResourceUsage::Write | ResourceUsage::ColorOutput;
    access.stages = rhi::PipelineStage::ColorOutput;
    access.access = rhi::Access::ColorWrite;
    access.layout = rhi::TextureLayout::ColorAttachment;
    pass.writes.push_back(access);

    return newHandle;
}

void PassBuilder::SetSideEffect(bool enabled)
{
    m_graph->m_passes[m_passId].hasSideEffect = enabled;
}

void PassBuilder::SetRenderArea(uint32_t width, uint32_t height)
{
    m_graph->m_passes[m_passId].renderArea = {width, height};
}

void PassBuilder::SetQueueRole(rhi::QueueRole queue)
{
    if (!m_graph || m_passId >= m_graph->m_passes.size() || queue == rhi::QueueRole::Count)
        return;
    auto &pass = m_graph->m_passes[m_passId];
    const bool compatible = queue == rhi::QueueRole::Graphics ||
                            (queue == rhi::QueueRole::Compute && pass.type == PassType::Compute) ||
                            (queue == rhi::QueueRole::Transfer && pass.type == PassType::Transfer);
    if (!compatible) {
        INXLOG_ERROR("RenderGraph pass '", pass.name, "' rejected incompatible queue override");
        return;
    }
    pass.queue = queue;
}

void PassBuilder::SetClearColor(float r, float g, float b, float a)
{
    auto &pass = m_graph->m_passes[m_passId];
    pass.clearColor = {{r, g, b, a}};
    pass.clearColorEnabled = true;
    pass.reads.erase(std::remove_if(pass.reads.begin(), pass.reads.end(),
                                    [&](const ResourceAccess &read) {
                                        if (static_cast<int>(read.usage & ResourceUsage::VersionDependency) == 0)
                                            return false;
                                        return std::any_of(pass.colorOutputs.begin(), pass.colorOutputs.end(),
                                                           [&](ResourceHandle output) {
                                                               return output.IsValid() && output.id == read.handle.id &&
                                                                      output.version == read.handle.version + 1;
                                                           });
                                    }),
                     pass.reads.end());
}

void PassBuilder::SetClearDepth(float depth, uint32_t stencil)
{
    auto &pass = m_graph->m_passes[m_passId];
    pass.clearDepth = {depth, stencil};
    pass.clearDepthEnabled = true;
    if (pass.depthOutput.IsValid()) {
        pass.reads.erase(std::remove_if(pass.reads.begin(), pass.reads.end(),
                                        [&](const ResourceAccess &read) {
                                            return static_cast<int>(read.usage & ResourceUsage::VersionDependency) !=
                                                       0 &&
                                                   read.handle.id == pass.depthOutput.id &&
                                                   pass.depthOutput.version == read.handle.version + 1;
                                        }),
                         pass.reads.end());
    }
}

// ============================================================================
// RenderGraph Implementation
// ============================================================================

RenderGraph::RenderGraph() = default;

RenderGraph::~RenderGraph()
{
    Destroy();
}

RenderGraph::RenderGraph(RenderGraph &&other) noexcept
    : m_identity(std::move(other.m_identity)), m_context(std::exchange(other.m_context, nullptr)),
      m_deviceId(std::exchange(other.m_deviceId, rhi::InvalidDeviceId)),
      m_renderViewId(std::exchange(other.m_renderViewId, rhi::InvalidRenderViewId)),
      m_rhiDevice(std::exchange(other.m_rhiDevice, nullptr)),
      m_deletionQueue(std::exchange(other.m_deletionQueue, nullptr)), m_queueTopology(other.m_queueTopology),
      m_passes(std::move(other.m_passes)), m_resources(std::move(other.m_resources)),
      m_resourceVersions(std::move(other.m_resourceVersions)), m_executionOrder(std::move(other.m_executionOrder)),
      m_submissionPlan(std::move(other.m_submissionPlan)),
      m_queueOwnershipTransfers(std::move(other.m_queueOwnershipTransfers)),
      m_queueOwnershipTransferInfos(std::move(other.m_queueOwnershipTransferInfos)),
      m_batchOutgoingOwnershipTransfers(std::move(other.m_batchOutgoingOwnershipTransfers)),
      m_backbuffer(other.m_backbuffer), m_output(other.m_output), m_compiled(std::exchange(other.m_compiled, false)),
      m_structuralCompileCache(std::move(other.m_structuralCompileCache)),
      m_structuralCacheHits(other.m_structuralCacheHits), m_structuralCacheMisses(other.m_structuralCacheMisses),
      m_resourceStates(std::move(other.m_resourceStates)),
      m_initialResourceStates(std::move(other.m_initialResourceStates)),
      m_barrierScratch(std::move(other.m_barrierScratch)),
      m_bufferBarrierScratch(std::move(other.m_bufferBarrierScratch)),
      m_barrier2Scratch(std::move(other.m_barrier2Scratch)),
      m_bufferBarrier2Scratch(std::move(other.m_bufferBarrier2Scratch)),
      m_cmdPipelineBarrier2(std::exchange(other.m_cmdPipelineBarrier2, nullptr)),
      m_cmdBeginRendering(std::exchange(other.m_cmdBeginRendering, nullptr)),
      m_cmdEndRendering(std::exchange(other.m_cmdEndRendering, nullptr))
{
    other.m_backbuffer = {};
    other.m_output = {};
}

RenderGraph &RenderGraph::operator=(RenderGraph &&other) noexcept
{
    if (this != &other) {
        Destroy();

        m_identity = std::move(other.m_identity);
        m_context = std::exchange(other.m_context, nullptr);
        m_deviceId = std::exchange(other.m_deviceId, rhi::InvalidDeviceId);
        m_renderViewId = std::exchange(other.m_renderViewId, rhi::InvalidRenderViewId);
        m_rhiDevice = std::exchange(other.m_rhiDevice, nullptr);
        m_deletionQueue = std::exchange(other.m_deletionQueue, nullptr);
        m_queueTopology = other.m_queueTopology;
        m_passes = std::move(other.m_passes);
        m_resources = std::move(other.m_resources);
        m_resourceVersions = std::move(other.m_resourceVersions);
        m_executionOrder = std::move(other.m_executionOrder);
        m_submissionPlan = std::move(other.m_submissionPlan);
        m_queueOwnershipTransfers = std::move(other.m_queueOwnershipTransfers);
        m_queueOwnershipTransferInfos = std::move(other.m_queueOwnershipTransferInfos);
        m_batchOutgoingOwnershipTransfers = std::move(other.m_batchOutgoingOwnershipTransfers);
        m_backbuffer = other.m_backbuffer;
        m_output = other.m_output;
        m_compiled = std::exchange(other.m_compiled, false);
        m_resourceStates = std::move(other.m_resourceStates);
        m_initialResourceStates = std::move(other.m_initialResourceStates);
        m_barrierScratch = std::move(other.m_barrierScratch);
        m_bufferBarrierScratch = std::move(other.m_bufferBarrierScratch);
        m_barrier2Scratch = std::move(other.m_barrier2Scratch);
        m_bufferBarrier2Scratch = std::move(other.m_bufferBarrier2Scratch);
        m_cmdPipelineBarrier2 = std::exchange(other.m_cmdPipelineBarrier2, nullptr);
        m_cmdBeginRendering = std::exchange(other.m_cmdBeginRendering, nullptr);
        m_cmdEndRendering = std::exchange(other.m_cmdEndRendering, nullptr);
        m_structuralCompileCache = std::move(other.m_structuralCompileCache);
        m_structuralCacheHits = other.m_structuralCacheHits;
        m_structuralCacheMisses = other.m_structuralCacheMisses;

        other.m_backbuffer = {};
        other.m_output = {};
    }
    return *this;
}

void RenderGraph::Initialize(VkDeviceContext *context, GpuRetirementQueue *deletionQueue,
                             const VulkanQueueManager *queueManager)
{
    m_context = context;
    m_deviceId = context ? context->GetDeviceId() : rhi::InvalidDeviceId;
    m_rhiDevice = context ? &context->GetRhiDevice() : nullptr;
    m_deletionQueue = deletionQueue;
    m_cmdPipelineBarrier2 = nullptr;
    m_cmdBeginRendering = nullptr;
    m_cmdEndRendering = nullptr;
    if (context && m_rhiDevice && m_rhiDevice->GetCapabilityState().synchronization2.enabled) {
        m_cmdPipelineBarrier2 = reinterpret_cast<PFN_vkCmdPipelineBarrier2>(
            vkGetDeviceProcAddr(context->GetDevice(), "vkCmdPipelineBarrier2"));
        if (!m_cmdPipelineBarrier2) {
            m_cmdPipelineBarrier2 = reinterpret_cast<PFN_vkCmdPipelineBarrier2>(
                vkGetDeviceProcAddr(context->GetDevice(), "vkCmdPipelineBarrier2KHR"));
        }
    }
    if (context && m_rhiDevice) {
        const rhi::DynamicRenderingCommands commands = rhi::ResolveDynamicRenderingCommands(context->GetDevice());
        if (!m_rhiDevice->GetCapabilityState().dynamicRendering.IsEnabled() || !commands.IsValid())
            throw std::runtime_error("RenderGraph requires Vulkan Dynamic Rendering");
        m_cmdBeginRendering = commands.begin;
        m_cmdEndRendering = commands.end;
    }

    std::array<NativeQueueBinding, static_cast<size_t>(rhi::QueueRole::Count)> topology{};
    if (queueManager && context && queueManager->GetDeviceId() == context->GetDeviceId()) {
        for (const rhi::QueueRole role :
             {rhi::QueueRole::Graphics, rhi::QueueRole::Compute, rhi::QueueRole::Transfer, rhi::QueueRole::Present}) {
            const auto snapshot = queueManager->GetSnapshot(role);
            topology[QueueRoleIndex(role)] = {snapshot.family, snapshot.nativeLane};
        }
    } else if (context) {
        const auto &indices = context->GetQueueIndices();
        const uint32_t graphicsFamily = indices.graphicsFamily.value_or(0);
        const uint32_t computeFamily = indices.computeFamily.value_or(graphicsFamily);
        const uint32_t transferFamily = indices.transferFamily.value_or(graphicsFamily);
        const uint32_t presentFamily = indices.presentFamily.value_or(graphicsFamily);
        const std::array<VkQueue, static_cast<size_t>(rhi::QueueRole::Count)> queues = {
            context->GetGraphicsQueue(), context->GetComputeQueue(), context->GetTransferQueue(),
            context->GetPresentQueue()};
        const std::array<uint32_t, static_cast<size_t>(rhi::QueueRole::Count)> families = {
            graphicsFamily, computeFamily, transferFamily, presentFamily};
        uint32_t nextLane = 0;
        for (size_t roleIndex = 0; roleIndex < queues.size(); ++roleIndex) {
            if (queues[roleIndex] == VK_NULL_HANDLE)
                continue;
            uint32_t lane = UINT32_MAX;
            for (size_t previous = 0; previous < roleIndex; ++previous) {
                if (queues[previous] == queues[roleIndex]) {
                    lane = topology[previous].lane;
                    break;
                }
            }
            if (lane == UINT32_MAX)
                lane = nextLane++;
            topology[roleIndex] = {families[roleIndex], lane};
        }
    }
    SetQueueTopology(topology);
}

void RenderGraph::SetQueueTopology(
    const std::array<NativeQueueBinding, static_cast<size_t>(rhi::QueueRole::Count)> &topology)
{
    if (m_queueTopology == topology)
        return;
    m_queueTopology = topology;
    m_compiled = false;
}

void RenderGraph::SetRenderView(const rhi::RenderViewContext &view)
{
    if (view.device != rhi::InvalidDeviceId && m_deviceId != rhi::InvalidDeviceId && view.device != m_deviceId) {
        INXLOG_ERROR("RenderGraph: render view belongs to device ", view.device, " but graph belongs to device ",
                     m_deviceId);
        return;
    }
    m_renderViewId = view.id;
}

void RenderGraph::Reset()
{
    FreeResources();
    m_passes.clear();
    m_explicitPassDependencies.clear();
    m_resources.clear();
    m_resourceVersions.clear();
    m_executionOrder.clear();
    m_submissionPlan.Clear();
    m_queueOwnershipTransfers.clear();
    m_queueOwnershipTransferInfos.clear();
    m_batchOutgoingOwnershipTransfers.clear();
    m_resourceStates.clear();
    m_initialResourceStates.clear();
    m_backbuffer = {};
    m_output = {};
    m_compiled = false;
    m_identity.AdvanceEpoch();
}

void RenderGraph::Destroy()
{
    FreeResources();

    m_structuralCompileCache.clear();
    m_structuralCacheHits = 0;
    m_structuralCacheMisses = 0;

    m_passes.clear();
    m_explicitPassDependencies.clear();
    m_resources.clear();
    m_resourceVersions.clear();
    m_executionOrder.clear();
    m_submissionPlan.Clear();
    m_queueOwnershipTransfers.clear();
    m_queueOwnershipTransferInfos.clear();
    m_batchOutgoingOwnershipTransfers.clear();
    m_resourceStates.clear();
    m_initialResourceStates.clear();
    m_context = nullptr;
    m_deviceId = rhi::InvalidDeviceId;
    m_renderViewId = rhi::InvalidRenderViewId;
    m_rhiDevice = nullptr;
    m_deletionQueue = nullptr;
    m_identity.AdvanceEpoch();
}

PassHandle RenderGraph::AddPass(const std::string &name, PassSetupCallback setup)
{
    PassHandle handle;
    handle.scope = m_identity.Current();
    handle.id = static_cast<uint32_t>(m_passes.size());

    RenderPassData passData;
    passData.name = name;
    passData.id = handle.id;
    passData.type = PassType::Graphics;
    passData.device = m_deviceId;
    passData.queue = QueueRoleForPass(passData.type);
    passData.view = m_renderViewId;

    m_passes.push_back(std::move(passData));

    // Run setup callback
    PassBuilder builder(this, handle.id);
    auto executeCallback = setup(builder);
    m_passes[handle.id].executeCallback = std::move(executeCallback);

    return handle;
}

PassHandle RenderGraph::AddComputePass(const std::string &name, PassSetupCallback setup)
{
    PassHandle handle;
    handle.scope = m_identity.Current();
    handle.id = static_cast<uint32_t>(m_passes.size());

    RenderPassData passData;
    passData.name = name;
    passData.id = handle.id;
    passData.type = PassType::Compute;
    passData.device = m_deviceId;
    passData.queue = QueueRoleForPass(passData.type);
    passData.view = m_renderViewId;
    m_passes.push_back(std::move(passData));

    PassBuilder builder(this, handle.id);
    m_passes[handle.id].executeCallback = setup(builder);
    return handle;
}

void RenderGraph::SetSubmissionBoundaryBefore(PassHandle pass)
{
    if (!Owns(pass) || pass.id >= m_passes.size()) {
        INXLOG_ERROR("RenderGraph::SetSubmissionBoundaryBefore rejected a foreign pass handle");
        return;
    }
    m_passes[pass.id].forceSubmissionBoundary = true;
}

void RenderGraph::AddPassDependency(PassHandle later, PassHandle earlier)
{
    if (!Owns(later) || !Owns(earlier) || later.id >= m_passes.size() || earlier.id >= m_passes.size()) {
        INXLOG_ERROR("RenderGraph::AddPassDependency rejected a foreign pass handle");
        return;
    }
    if (later.id == earlier.id) {
        INXLOG_ERROR("RenderGraph::AddPassDependency rejected a self-dependency on pass ", later.id);
        return;
    }
    const auto dependency = std::make_pair(later.id, earlier.id);
    if (std::find(m_explicitPassDependencies.begin(), m_explicitPassDependencies.end(), dependency) ==
        m_explicitPassDependencies.end())
        m_explicitPassDependencies.push_back(dependency);
}

PassHandle RenderGraph::AddTransferPass(const std::string &name, PassSetupCallback setup)
{
    PassHandle handle;
    handle.scope = m_identity.Current();
    handle.id = static_cast<uint32_t>(m_passes.size());

    RenderPassData passData;
    passData.name = name;
    passData.id = handle.id;
    passData.type = PassType::Transfer;
    passData.device = m_deviceId;
    passData.queue = QueueRoleForPass(passData.type);
    passData.view = m_renderViewId;

    m_passes.push_back(std::move(passData));

    PassBuilder builder(this, handle.id);
    auto executeCallback = setup(builder);
    m_passes[handle.id].executeCallback = std::move(executeCallback);

    return handle;
}

PassHandle RenderGraph::AddPresentPass(const std::string &name, PassSetupCallback setup)
{
    PassHandle handle;
    handle.scope = m_identity.Current();
    handle.id = static_cast<uint32_t>(m_passes.size());

    RenderPassData passData;
    passData.name = name;
    passData.id = handle.id;
    passData.type = PassType::Present;
    passData.device = m_deviceId;
    passData.queue = QueueRoleForPass(passData.type);
    passData.view = m_renderViewId;
    m_passes.push_back(std::move(passData));

    PassBuilder builder(this, handle.id);
    m_passes[handle.id].executeCallback = setup(builder);
    return handle;
}

ResourceHandle RenderGraph::SetBackbuffer(VkImage image, VkImageView view, VkFormat format, uint32_t width,
                                          uint32_t height, VkSampleCountFlagBits samples,
                                          rhi::TextureLayout initialLayout)
{
    ResourceHandle handle;
    handle.scope = m_identity.Current();
    handle.id = static_cast<uint32_t>(m_resources.size());
    handle.version = 0;

    ResourceData resource;
    resource.ownerDevice = m_deviceId;
    resource.name = "Backbuffer";
    resource.type = ResourceType::Texture2D;
    resource.textureDesc.name = "Backbuffer";
    resource.textureDesc.width = width;
    resource.textureDesc.height = height;
    resource.textureDesc.format = format;
    resource.textureDesc.samples = samples;
    resource.textureDesc.isTransient = false;
    resource.isExternal = true;
    const auto graphicsBinding = m_queueTopology[QueueRoleIndex(rhi::QueueRole::Graphics)];
    const auto presentBinding = m_queueTopology[QueueRoleIndex(rhi::QueueRole::Present)];
    resource.concurrentQueueSharing =
        graphicsBinding.IsValid() && presentBinding.IsValid() && graphicsBinding.family != presentBinding.family;
    resource.externalImage = image;
    resource.externalView = view;
    resource.rhiView = m_rhiDevice ? m_rhiDevice->RegisterTextureView(view) : rhi::TextureViewHandle{};
    resource.rhiTexture = m_rhiDevice ? m_rhiDevice->RegisterTexture(image) : rhi::TextureHandle{};

    m_resources.push_back(std::move(resource));
    m_resourceVersions.push_back(0);
    m_resourceStates.resize(m_resources.size());
    m_initialResourceStates.resize(m_resources.size());
    m_backbuffer = handle;

    ResourceState initialState{};
    const rhi::QueueRole initialOwner =
        initialLayout == rhi::TextureLayout::Present ? rhi::QueueRole::Present : rhi::QueueRole::Graphics;
    const auto initialBinding = m_queueTopology[QueueRoleIndex(initialOwner)];
    initialState.queue = initialOwner;
    initialState.queueFamily = initialBinding.family;
    initialState.nativeQueueLane = initialBinding.lane;
    if (initialLayout != rhi::TextureLayout::Automatic) {
        // Caller-specified initial layout (e.g. UNDEFINED for swapchain images)
        initialState.layout = initialLayout;
        initialState.accessMask = rhi::Access::None;
        initialState.stages = rhi::PipelineStage::Top;
    } else if (samples == VK_SAMPLE_COUNT_1_BIT) {
        initialState.layout = rhi::TextureLayout::ShaderReadOnly;
        initialState.accessMask = rhi::Access::ShaderRead;
        initialState.stages = rhi::PipelineStage::FragmentShader;
    } else {
        initialState.layout = rhi::TextureLayout::ColorAttachment;
        initialState.accessMask = rhi::Access::ColorWrite;
        initialState.stages = rhi::PipelineStage::ColorOutput;
    }
    m_resourceStates[handle.id] = initialState;
    m_initialResourceStates[handle.id] = initialState;

    return handle;
}

ResourceHandle RenderGraph::ImportResolveTarget(VkImage image, VkImageView view, VkFormat format, uint32_t width,
                                                uint32_t height)
{
    ResourceHandle handle;
    handle.scope = m_identity.Current();
    handle.id = static_cast<uint32_t>(m_resources.size());
    handle.version = 0;

    ResourceData resource;
    resource.ownerDevice = m_deviceId;
    resource.name = "ResolveTarget";
    resource.type = ResourceType::Texture2D;
    resource.textureDesc.name = "ResolveTarget";
    resource.textureDesc.width = width;
    resource.textureDesc.height = height;
    resource.textureDesc.format = format;
    resource.textureDesc.samples = VK_SAMPLE_COUNT_1_BIT;
    resource.textureDesc.isTransient = false;
    resource.isExternal = true;
    resource.externalImage = image;
    resource.externalView = view;
    resource.rhiView = m_rhiDevice ? m_rhiDevice->RegisterTextureView(view) : rhi::TextureViewHandle{};
    resource.rhiTexture = m_rhiDevice ? m_rhiDevice->RegisterTexture(image) : rhi::TextureHandle{};

    m_resources.push_back(std::move(resource));
    m_resourceVersions.push_back(0);
    m_resourceStates.resize(m_resources.size());
    m_initialResourceStates.resize(m_resources.size());

    ResourceState initialState{};
    const auto graphicsBinding = m_queueTopology[QueueRoleIndex(rhi::QueueRole::Graphics)];
    initialState.queue = rhi::QueueRole::Graphics;
    initialState.queueFamily = graphicsBinding.family;
    initialState.nativeQueueLane = graphicsBinding.lane;
    initialState.layout = rhi::TextureLayout::ShaderReadOnly;
    initialState.accessMask = rhi::Access::ShaderRead;
    initialState.stages = rhi::PipelineStage::FragmentShader;
    m_resourceStates[handle.id] = initialState;
    m_initialResourceStates[handle.id] = initialState;

    return handle;
}

ResourceHandle RenderGraph::ImportTexture(const std::string &name, VkImage image, VkImageView view, VkFormat format,
                                          uint32_t width, uint32_t height, VkSampleCountFlagBits samples)
{
    ResourceHandle handle;
    handle.scope = m_identity.Current();
    handle.id = static_cast<uint32_t>(m_resources.size());
    handle.version = 0;

    ResourceData resource;
    resource.ownerDevice = m_deviceId;
    resource.name = name;
    resource.type = ResourceType::Texture2D;
    resource.textureDesc.name = name;
    resource.textureDesc.width = width;
    resource.textureDesc.height = height;
    resource.textureDesc.format = format;
    resource.textureDesc.samples = samples;
    resource.textureDesc.isTransient = false;
    resource.isExternal = true;
    resource.externalImage = image;
    resource.externalView = view;
    resource.rhiView = m_rhiDevice ? m_rhiDevice->RegisterTextureView(view) : rhi::TextureViewHandle{};
    resource.rhiTexture = m_rhiDevice ? m_rhiDevice->RegisterTexture(image) : rhi::TextureHandle{};

    m_resources.push_back(std::move(resource));
    m_resourceVersions.push_back(0);
    m_resourceStates.resize(m_resources.size());
    m_initialResourceStates.resize(m_resources.size());
    SetResourceInitialState(handle, rhi::TextureLayout::Undefined, rhi::Access::None, rhi::PipelineStage::Top);
    return handle;
}

ResourceHandle RenderGraph::ImportTexture(const std::string &name, rhi::TextureHandle texture,
                                          rhi::TextureViewHandle view, VkFormat format, uint32_t width, uint32_t height,
                                          VkSampleCountFlagBits samples)
{
    if (!m_rhiDevice || !texture.IsValid() || !view.IsValid())
        return {};

    const VkImage image = m_rhiDevice->Resolve(texture);
    const VkImageView imageView = m_rhiDevice->Resolve(view);
    if (image == VK_NULL_HANDLE || imageView == VK_NULL_HANDLE)
        return {};

    const ResourceHandle handle = ImportTexture(name, image, imageView, format, width, height, samples);
    if (Owns(handle))
        m_resources[handle.id].concurrentQueueSharing = m_rhiDevice->UsesConcurrentQueueSharing(texture);
    return handle;
}

bool RenderGraph::UpdateImportedTexture(ResourceHandle handle, VkImage image, VkImageView view)
{
    if (!Owns(handle) || image == VK_NULL_HANDLE || view == VK_NULL_HANDLE)
        return false;
    auto &resource = m_resources[handle.id];
    if (!resource.isExternal || resource.type != ResourceType::Texture2D)
        return false;
    if (resource.externalImage == image && resource.externalView == view)
        return true;

    if (m_rhiDevice) {
        m_rhiDevice->Release(resource.rhiView);
        m_rhiDevice->Release(resource.rhiTexture);
        resource.rhiView = m_rhiDevice->RegisterTextureView(view);
        resource.rhiTexture = m_rhiDevice->RegisterTexture(image);
        if (!resource.rhiView.IsValid() || !resource.rhiTexture.IsValid())
            return false;
    }
    resource.externalImage = image;
    resource.externalView = view;
    return true;
}

ResourceHandle RenderGraph::ImportRendererList(const std::string &name, const RendererList *rendererList)
{
    ResourceHandle handle = CreateResource(name, ResourceType::RendererList);
    auto &resource = m_resources[handle.id];
    resource.isExternal = true;
    resource.externalRendererList = rendererList;
    return handle;
}

void RenderGraph::SetResourceInitialState(ResourceHandle handle, rhi::TextureLayout layout, rhi::Access accessMask,
                                          rhi::PipelineStage stages, rhi::QueueRole ownerQueue)
{
    if (!Owns(handle)) {
        return;
    }

    if (handle.id >= m_initialResourceStates.size() || handle.id >= m_resourceStates.size()) {
        return;
    }

    ResourceState state{};
    state.layout = layout;
    state.accessMask = accessMask;
    state.stages = stages;
    state.writerPassId = UINT32_MAX;
    const rhi::QueueRole queue = ownerQueue == rhi::QueueRole::Count ? rhi::QueueRole::Graphics : ownerQueue;
    const auto binding = m_queueTopology[QueueRoleIndex(queue)];
    state.queue = queue;
    state.queueFamily = binding.family;
    state.nativeQueueLane = binding.lane;

    m_initialResourceStates[handle.id] = state;
    m_resourceStates[handle.id] = state;
}

void RenderGraph::SetOutput(ResourceHandle handle)
{
    m_output = Owns(handle) ? handle : ResourceHandle{};
}

ResourceHandle RenderGraph::RegisterTransientTexture(const std::string &name, uint32_t width, uint32_t height,
                                                     VkFormat format, VkSampleCountFlagBits samples, bool isTransient)
{
    ResourceHandle handle = CreateResource(name, ResourceType::Texture2D);
    if (Owns(handle)) {
        auto &res = m_resources[handle.id];
        res.textureDesc.name = name;
        res.textureDesc.width = width;
        res.textureDesc.height = height;
        res.textureDesc.format = format;
        res.textureDesc.samples = samples;
        res.textureDesc.isTransient = isTransient;
    }
    return handle;
}

ResourceHandle RenderGraph::RegisterTransientBuffer(const std::string &name, VkDeviceSize size,
                                                    VkBufferUsageFlags usage)
{
    ResourceHandle handle = CreateResource(name, ResourceType::Buffer);
    if (Owns(handle)) {
        auto &resource = m_resources[handle.id];
        resource.bufferDesc.name = name;
        resource.bufferDesc.size = size;
        resource.bufferDesc.usage = usage;
        resource.bufferDesc.isTransient = true;
    }
    return handle;
}

bool RenderGraph::UpdatePassClearColor(const std::string &passName, float r, float g, float b, float a)
{
    for (auto &pass : m_passes) {
        if (pass.name == passName) {
            pass.clearColor = {{r, g, b, a}};
            const uint32_t colorCount =
                std::min<uint32_t>(static_cast<uint32_t>(pass.colorOutputs.size()),
                                   static_cast<uint32_t>(pass.cachedRenderingColorAttachments.size()));
            for (uint32_t i = 0; i < colorCount; ++i) {
                pass.cachedRenderingColorAttachments[i].clearValue.color = pass.clearColor;
            }
            return true;
        }
    }
    return false;
}

bool RenderGraph::UpdatePassClearDepth(const std::string &passName, float depth, uint32_t stencil)
{
    for (auto &pass : m_passes) {
        if (pass.name == passName) {
            pass.clearDepth = {depth, stencil};
            if (pass.cachedRenderingDepthAttachment.imageView != VK_NULL_HANDLE) {
                pass.cachedRenderingDepthAttachment.clearValue.depthStencil = pass.clearDepth;
            }
            return true;
        }
    }
    return false;
}

ResourceHandle RenderGraph::CreateResource(const std::string &name, ResourceType type)
{
    ResourceHandle handle;
    handle.scope = m_identity.Current();
    handle.id = static_cast<uint32_t>(m_resources.size());
    handle.version = 0;

    ResourceData resource;
    resource.ownerDevice = m_deviceId;
    resource.name = name;
    resource.type = type;

    m_resources.push_back(std::move(resource));
    m_resourceVersions.push_back(0);
    m_resourceStates.resize(m_resources.size());
    m_initialResourceStates.resize(m_resources.size());

    return handle;
}

ResourceHandle RenderGraph::AdvanceResourceVersion(ResourceHandle handle)
{
    if (!Owns(handle)) {
        INXLOG_ERROR("RenderGraph: cannot write a resource handle owned by another graph or epoch");
        return {};
    }

    uint32_t &latestVersion = m_resourceVersions[handle.id];
    if (handle.version != latestVersion) {
        INXLOG_ERROR("RenderGraph: resource '", m_resources[handle.id].name, "' write uses stale version ",
                     handle.version, " while latest is ", latestVersion);
        return {};
    }
    if (latestVersion == std::numeric_limits<uint32_t>::max()) {
        INXLOG_ERROR("RenderGraph: resource version overflow for '", m_resources[handle.id].name, "'");
        return {};
    }

    ++latestVersion;
    handle.version = latestVersion;
    return handle;
}

bool RenderGraph::CompileExecutionOrder()
{
    if (m_passes.empty())
        return true;
    CullPasses();
    return TopologicalSort();
}

bool RenderGraph::Compile()
{
    if (m_passes.empty()) {
        INXLOG_WARN("RenderGraph::Compile - No passes to compile");
        return true;
    }
    if (m_deviceId == rhi::InvalidDeviceId) {
        INXLOG_ERROR("RenderGraph::Compile - Graph has no owning render device");
        return false;
    }
    for (const auto &pass : m_passes) {
        if (pass.device != m_deviceId) {
            INXLOG_ERROR("RenderGraph::Compile - Pass '", pass.name, "' belongs to a different device");
            return false;
        }
    }
    for (const auto &resource : m_resources) {
        if (resource.ownerDevice != m_deviceId) {
            INXLOG_ERROR("RenderGraph::Compile - Resource '", resource.name, "' belongs to a different device");
            return false;
        }
    }

    const auto structuralSignature = BuildStructuralSignature();
    if (!RestoreStructuralCompilation(structuralSignature)) {
        if (!CompileExecutionOrder()) {
            return false;
        }

        // Step 3: Compute lifetimes in final execution order so transient
        // aliasing does not depend on declaration order.
        ComputeResourceLifetimes();
        StoreStructuralCompilation(structuralSignature);
    }

    // Step 4: Compile backend-neutral queue batches and dependency waits.
    if (!CompileSubmissionPlan())
        return false;

    // Step 5: Allocate transient resources
    if (!AllocateResources()) {
        return false;
    }

    // Step 6: Compile Dynamic Rendering attachment contracts.
    if (!CompileGraphicsAttachments()) {
        return false;
    }

    // Step 7: Pre-compute per-pass Execute() data.
    PrecomputeExecuteData();

    // Step 8: Compile the release/acquire pairs required when an exclusive
    // resource crosses native Vulkan queue families.
    if (!CompileQueueOwnershipTransfers())
        return false;

    m_compiled = true;
    return true;
}

void RenderGraph::BeginExecution()
{
    if (!m_compiled) {
        INXLOG_ERROR("RenderGraph::BeginExecution - Graph not compiled");
        return;
    }

#if INFERNUX_FRAME_PROFILE
    ++s_executeProfile.executeCalls;
#endif

    m_recordingSubmissionBatches = true;

    // Resource state is shared by the ordered batch recording session. The
    // Vulkan executor records batches in plan order, so every later command
    // buffer observes the state produced by the preceding batch. Preserve the
    // final state of graph-owned resources across frames: unlike imported
    // resources, they are the same physical allocations on every execution.
    PrepareExecutionResourceStates();
}

bool RenderGraph::RecordSubmissionBatch(uint32_t batchIndex, VkCommandBuffer commandBuffer)
{
    if (!m_compiled || batchIndex >= m_submissionPlan.batches.size() || commandBuffer == VK_NULL_HANDLE) {
        INXLOG_ERROR("RenderGraph::RecordSubmissionBatch - invalid execution request");
        return false;
    }
    RecordPasses(commandBuffer, m_submissionPlan.batches[batchIndex].workItems);
    InsertQueueOwnershipReleases(commandBuffer, batchIndex);
    return true;
}

bool RenderGraph::HasExternalQueueOwnershipReleases(rhi::QueueRole sourceQueue) const noexcept
{
    return sourceQueue != rhi::QueueRole::Count &&
           !m_externalOutgoingOwnershipTransfers[static_cast<size_t>(sourceQueue)].empty();
}

bool RenderGraph::RecordExternalQueueOwnershipReleases(rhi::QueueRole sourceQueue, VkCommandBuffer commandBuffer)
{
    if (!m_compiled || sourceQueue == rhi::QueueRole::Count || commandBuffer == VK_NULL_HANDLE)
        return false;
    const rhi::QueueRole previousRecordingQueue = m_immediateRecordingQueue;
    m_immediateRecordingQueue = sourceQueue;
    InsertQueueOwnershipReleases(commandBuffer, rhi::InvalidSubmissionBatchIndex);
    m_immediateRecordingQueue = previousRecordingQueue;
    return true;
}

void RenderGraph::Execute(VkCommandBuffer commandBuffer, rhi::QueueRole recordingQueue)
{
    if (!m_compiled) {
        INXLOG_ERROR("RenderGraph::Execute - Graph not compiled");
        return;
    }

    m_recordingSubmissionBatches = false;
    m_immediateRecordingQueue = recordingQueue == rhi::QueueRole::Count ? rhi::QueueRole::Graphics : recordingQueue;
    PrepareExecutionResourceStates();
#if INFERNUX_FRAME_PROFILE
    ++s_executeProfile.executeCalls;
#endif
    RecordPasses(commandBuffer, m_executionOrder);
}

void RenderGraph::PrepareExecutionResourceStates()
{
    if (m_resourceStates.size() != m_resources.size() || m_initialResourceStates.size() != m_resources.size()) {
        m_resourceStates = m_initialResourceStates;
        return;
    }

    for (size_t index = 0; index < m_resources.size(); ++index) {
        const auto &resource = m_resources[index];
        const auto &previous = m_resourceStates[index];

        // Imported images/buffers may change identity or layout between
        // executions, and renderer lists have no GPU state to carry. An
        // explicit SetResourceInitialState() also resets writerPassId and must
        // take precedence over a state retained from an earlier execution.
        if (resource.isExternal || resource.type == ResourceType::RendererList || previous.writerPassId == UINT32_MAX) {
            m_resourceStates[index] = m_initialResourceStates[index];
        }
    }
}

void RenderGraph::ContinueExecution(VkCommandBuffer commandBuffer, rhi::QueueRole recordingQueue)
{
    if (!m_compiled) {
        INXLOG_ERROR("RenderGraph::ContinueExecution - Graph not compiled");
        return;
    }

    m_recordingSubmissionBatches = false;
    m_immediateRecordingQueue = recordingQueue == rhi::QueueRole::Count ? rhi::QueueRole::Graphics : recordingQueue;
#if INFERNUX_FRAME_PROFILE
    ++s_executeProfile.executeCalls;
#endif
    RecordPasses(commandBuffer, m_executionOrder);
}

void RenderGraph::RecordPasses(VkCommandBuffer commandBuffer, const std::vector<uint32_t> &passIndices)
{
    if (commandBuffer == VK_NULL_HANDLE)
        return;

    // One-shot diagnostic: log detailed per-pass info for first N executions
    static int s_execDiagCount = 0;
    const bool diagEnabled = (s_execDiagCount < 2);
    if (diagEnabled) {
        ++s_execDiagCount;
    }

#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
#endif

    RenderContext context(commandBuffer, this);

    for (uint32_t passIndex : passIndices) {
        if (passIndex >= m_passes.size()) {
            INXLOG_ERROR("RenderGraph::RecordPasses - invalid pass id ", passIndex);
            continue;
        }
        auto &pass = m_passes[passIndex];

        if (pass.culled) {
            continue;
        }

#if INFERNUX_FRAME_PROFILE
        context.m_activePassId = passIndex;
        ++s_executeProfile.passCount;
        if (pass.name.find("GpuParticle") != std::string::npos) {
            auto &particleProfile = s_particlePassProfiles[pass.name];
            particleProfile.name = pass.name;
            ++particleProfile.passCalls;
            // This is the graph's barrier phase count, not a GPU query or a
            // synchronous readback. It identifies passes whose declared
            // resources entered the barrier planner.
            if (!pass.reads.empty() || !pass.writes.empty())
                ++particleProfile.barrierPhases;
        }
#endif

        const bool isGraphicsPass = pass.type == PassType::Graphics && pass.renderingSignature.IsValid();
#if INFERNUX_FRAME_PROFILE
        if (isGraphicsPass) {
            ++s_executeProfile.graphicsPassCount;
        }
        if (pass.type == PassType::Compute) {
            ++s_executeProfile.computePassCount;
        }
#endif

        // Insert barriers
#if INFERNUX_FRAME_PROFILE
        auto stageStart = Clock::now();
#endif
        InsertBarriers(commandBuffer, passIndex);

        // Per-pass diagnostic
        if (diagEnabled) {
            std::string clearInfo = "no-clear";
            if (pass.clearColorEnabled) {
                auto &cv = pass.clearColor;
                clearInfo = "clear=(" + std::to_string(cv.float32[0]) + "," + std::to_string(cv.float32[1]) + "," +
                            std::to_string(cv.float32[2]) + "," + std::to_string(cv.float32[3]) + ")";
            }
            std::string writeInfo;
            for (const auto &w : pass.writes) {
                if (!writeInfo.empty())
                    writeInfo += ",";
                if (w.handle.id < m_resources.size()) {
                    writeInfo += m_resources[w.handle.id].name + "(id=" + std::to_string(w.handle.id) + ")";
                }
            }
            std::string readInfo;
            for (const auto &r : pass.reads) {
                if (!readInfo.empty())
                    readInfo += ",";
                if (r.handle.id < m_resources.size()) {
                    readInfo += m_resources[r.handle.id].name + "(id=" + std::to_string(r.handle.id) + ")";
                }
            }
            INXLOG_DEBUG("RenderGraph::Execute pass[", passIndex, "] '", pass.name, "' writes=[", writeInfo,
                         "] reads=[", readInfo, "] ", clearInfo);
        }
#if INFERNUX_FRAME_PROFILE
        auto stageNow = Clock::now();
        s_executeProfile.barrierMs += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
        ++s_executeProfile.barrierCallCount;
#endif

        // Begin render pass (for graphics passes) — use pre-computed data
        if (isGraphicsPass) {
#if INFERNUX_FRAME_PROFILE
            stageStart = Clock::now();
#endif
            m_cmdBeginRendering(commandBuffer, &pass.cachedRenderingInfo);
            context.SetViewport(pass.cachedViewport);
            context.SetScissor(pass.cachedScissor);
#if INFERNUX_FRAME_PROFILE
            stageNow = Clock::now();
            s_executeProfile.beginPassMs += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
#endif
        }

        // Execute pass callback
        bool executeCallback = static_cast<bool>(pass.executeCallback);
        if (executeCallback && pass.skipCallbackWhenRendererListsEmpty) {
            executeCallback =
                std::any_of(pass.rendererListInputs.begin(), pass.rendererListInputs.end(), [&](ResourceHandle handle) {
                    const RendererList *rendererList = ResolveRendererList(handle);
                    return rendererList && !rendererList->Empty();
                });
        }
        if (executeCallback) {
            // RHI bind elision is deliberately scoped to one pass callback.
            // Raw command-buffer access invalidates it again through
            // RenderContext::GetCommandBuffer().
            context.InvalidateRhiBindingState();
#if INFERNUX_FRAME_PROFILE
            stageStart = Clock::now();
#endif
            try {
                pass.executeCallback(context);
            } catch (const std::exception &e) {
                INXLOG_ERROR("RenderGraph::Execute - Pass '", pass.name, "' callback threw exception: ", e.what());
            } catch (...) {
                INXLOG_ERROR("RenderGraph::Execute - Pass '", pass.name, "' callback threw unknown exception");
            }
#if INFERNUX_FRAME_PROFILE
            stageNow = Clock::now();
            const double callbackMs = std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
            s_executeProfile.callbackMs += callbackMs;
            auto &profile = s_callbackProfiles[pass.name];
            profile.name = pass.name;
            profile.totalMs += callbackMs;
            ++profile.calls;
#endif
        }

        // End render pass
        if (isGraphicsPass) {
#if INFERNUX_FRAME_PROFILE
            stageStart = Clock::now();
#endif
            m_cmdEndRendering(commandBuffer);
#if INFERNUX_FRAME_PROFILE
            stageNow = Clock::now();
            s_executeProfile.endPassMs += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
#endif
        }
    }
#if INFERNUX_FRAME_PROFILE
    s_executeProfile.rhiPipelineBinds +=
        context.m_graphicsCommandContext.pipelineBinds + context.m_computeCommandContext.pipelineBinds;
    s_executeProfile.rhiPipelineBindSkips +=
        context.m_graphicsCommandContext.pipelineBindSkips + context.m_computeCommandContext.pipelineBindSkips;
    s_executeProfile.rhiGroupBinds +=
        context.m_graphicsCommandContext.groupBinds + context.m_computeCommandContext.groupBinds;
    s_executeProfile.rhiGroupBindSkips +=
        context.m_graphicsCommandContext.groupBindSkips + context.m_computeCommandContext.groupBindSkips;
#endif
}

std::vector<std::string> RenderGraph::GetExecutionPassNames() const
{
    std::vector<std::string> names;
    names.reserve(m_executionOrder.size());
    for (uint32_t passIndex : m_executionOrder) {
        if (passIndex < m_passes.size() && !m_passes[passIndex].culled)
            names.push_back(m_passes[passIndex].name);
    }
    return names;
}

std::vector<PassCompileInfo> RenderGraph::GetPassCompileInfos() const
{
    std::vector<PassCompileInfo> infos;
    infos.reserve(m_passes.size());
    for (const auto &pass : m_passes) {
        infos.push_back({pass.name, pass.type, pass.culled, pass.cullReason, pass.device, pass.queue,
                         pass.submissionDomain, pass.view});
    }
    return infos;
}

std::string RenderGraph::GetDebugString() const
{
    std::ostringstream oss;
    oss << "RenderGraph (" << m_passes.size() << " passes, " << m_resources.size() << " resources)\n";
    oss << "Structural cache: " << m_structuralCacheHits << " hits, " << m_structuralCacheMisses << " misses, "
        << m_structuralCompileCache.size() << " entries\n";

    oss << "\nPasses:\n";
    for (const auto &pass : m_passes) {
        oss << "  [" << pass.id << "] " << pass.name;
        const char *reason = "unreachable";
        switch (pass.cullReason) {
        case PassCullReason::GraphOutput:
            reason = "graph-output";
            break;
        case PassCullReason::SideEffect:
            reason = "side-effect";
            break;
        case PassCullReason::ExternalWrite:
            reason = "external-write";
            break;
        case PassCullReason::Dependency:
            reason = "dependency";
            break;
        case PassCullReason::Unreachable:
            break;
        }
        oss << (pass.culled ? " (CULLED: " : " (RETAINED: ") << reason << ")";
        oss << "\n";

        if (!pass.reads.empty()) {
            oss << "    Reads: ";
            for (const auto &read : pass.reads) {
                oss << m_resources[read.handle.id].name << "#" << read.handle.version;
                if (static_cast<int>(read.usage & ResourceUsage::VersionDependency) != 0)
                    oss << "[order]";
                oss << " ";
            }
            oss << "\n";
        }

        if (!pass.writes.empty()) {
            oss << "    Writes: ";
            for (const auto &write : pass.writes) {
                oss << m_resources[write.handle.id].name << "#" << write.handle.version << " ";
            }
            oss << "\n";
        }
    }

    oss << "\nResources:\n";
    for (const auto &resource : m_resources) {
        oss << "  " << resource.name;
        if (resource.isExternal) {
            oss << " (external)";
        }
        oss << " - first pass: " << resource.firstPass << ", last pass: " << resource.lastPass;
        oss << "\n";
    }

    return oss.str();
}

VkImageView RenderGraph::ResolveTextureView(ResourceHandle handle) const
{
    if (!Owns(handle) || handle.id >= m_resources.size()) {
        return VK_NULL_HANDLE;
    }

    const auto &resource = m_resources[handle.id];
    if (resource.isExternal) {
        return resource.externalView;
    }
    return resource.allocatedView;
}

rhi::TextureViewHandle RenderGraph::ResolveRhiTextureView(ResourceHandle handle) const
{
    if (!Owns(handle) || handle.id >= m_resources.size()) {
        return {};
    }
    return m_resources[handle.id].rhiView;
}

rhi::TextureHandle RenderGraph::ResolveRhiTexture(ResourceHandle handle) const
{
    if (!Owns(handle) || handle.id >= m_resources.size())
        return {};
    return m_resources[handle.id].rhiTexture;
}

VkBuffer RenderGraph::ResolveBuffer(ResourceHandle handle) const
{
    if (!Owns(handle) || handle.id >= m_resources.size()) {
        return VK_NULL_HANDLE;
    }

    const auto &resource = m_resources[handle.id];
    if (resource.isExternal) {
        return resource.externalBuffer;
    }
    return resource.allocatedBuffer;
}

rhi::BufferHandle RenderGraph::ResolveRhiBuffer(ResourceHandle handle) const
{
    if (!Owns(handle) || handle.id >= m_resources.size())
        return {};
    return m_resources[handle.id].rhiBuffer;
}

const RendererList *RenderGraph::ResolveRendererList(ResourceHandle handle) const
{
    if (!Owns(handle) || handle.id >= m_resources.size())
        return nullptr;
    const auto &resource = m_resources[handle.id];
    return resource.type == ResourceType::RendererList ? resource.externalRendererList : nullptr;
}

rhi::GraphicsRenderingSignature RenderGraph::GetPassRenderingSignature(const std::string &passName) const noexcept
{
    const auto pass = std::find_if(m_passes.begin(), m_passes.end(),
                                   [&](const RenderPassData &candidate) { return candidate.name == passName; });
    return pass != m_passes.end() ? pass->renderingSignature : rhi::GraphicsRenderingSignature{};
}

PassRenderingContract RenderGraph::GetPassRenderingContract(const std::string &passName) const noexcept
{
    const auto pass = std::find_if(m_passes.begin(), m_passes.end(),
                                   [&](const RenderPassData &candidate) { return candidate.name == passName; });
    if (pass == m_passes.end())
        return {};

    PassRenderingContract contract;
    contract.found = true;
    contract.culled = pass->culled;
    contract.depthReadOnly = pass->depthInput.IsValid() && !pass->depthOutput.IsValid();
    contract.depthAttachment = GetEffectiveDepth(*pass);
    contract.attachments = pass->renderingSignature;
    return contract;
}

} // namespace vk
} // namespace infernux
