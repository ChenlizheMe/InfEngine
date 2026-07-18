/**
 * @file RenderGraph.cpp
 * @brief RenderGraph public API — RenderContext, PassBuilder, lifecycle, Compile/Execute
 *        orchestration, and resource resolution.
 *
 * Compilation internals (culling, sorting, allocation, barriers, caching) are in
 * RenderGraphCompile.cpp.
 */

#include "RenderGraph.h"
#include "VkDeviceContext.h"
#include "VkPipelineManager.h"
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

void RenderGraph::ResetExecuteProfileSnapshot()
{
    s_executeProfile = {};
    s_callbackProfiles.clear();
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
    }
}

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

VkBuffer RenderContext::GetBuffer(ResourceHandle handle) const
{
    return m_graph ? m_graph->ResolveBuffer(handle) : VK_NULL_HANDLE;
}

rhi::BufferHandle RenderContext::GetBufferHandle(ResourceHandle handle) const
{
    return m_graph ? m_graph->ResolveRhiBuffer(handle) : rhi::BufferHandle{};
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

    return handle;
}

ResourceHandle PassBuilder::ImportBuffer(const std::string &name, VkBuffer buffer, VkDeviceSize size)
{
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
    resource.rhiBuffer = m_graph->m_rhiDevice ? m_graph->m_rhiDevice->RegisterBuffer(buffer) : rhi::BufferHandle{};

    return handle;
}

ResourceHandle PassBuilder::Read(ResourceHandle handle, VkPipelineStageFlags stages)
{
    if (!m_graph->Owns(handle)) {
        return handle;
    }

    auto &pass = m_graph->m_passes[m_passId];

    ResourceAccess access;
    access.handle = handle;
    access.usage = ResourceUsage::Read | ResourceUsage::ShaderRead;
    access.stages = stages;
    access.access = VK_ACCESS_SHADER_READ_BIT;
    access.layout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;

    pass.reads.push_back(access);

    return handle;
}

ResourceHandle PassBuilder::ReadSampledDepth(ResourceHandle handle, VkPipelineStageFlags stages)
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
    access.access = VK_ACCESS_SHADER_READ_BIT;
    access.layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL;

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
    pass.reads.push_back(
        {handle, ResourceUsage::Read | ResourceUsage::VersionDependency, 0, 0, VK_IMAGE_LAYOUT_UNDEFINED});

    ResourceAccess access;
    access.handle = newHandle;
    access.usage = ResourceUsage::Write | ResourceUsage::ColorOutput;
    access.stages = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    access.access = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    access.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

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

    pass.reads.push_back(
        {handle, ResourceUsage::Read | ResourceUsage::VersionDependency, 0, 0, VK_IMAGE_LAYOUT_UNDEFINED});

    ResourceAccess access;
    access.handle = newHandle;
    access.usage = ResourceUsage::Write | ResourceUsage::DepthOutput;
    access.stages = VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
    access.access = VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
    access.layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;

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
    access.stages = VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
    access.access = VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_READ_BIT;
    // Read-only depth: the render pass uses DEPTH_STENCIL_READ_ONLY_OPTIMAL
    // for both the subpass attachment and initialLayout/finalLayout.
    // The barrier must transition to this layout (not ATTACHMENT_OPTIMAL).
    access.layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL;

    pass.reads.push_back(access);
    pass.depthInput = handle;

    return handle; // No version bump â€” read-only
}

ResourceHandle PassBuilder::ReadWrite(ResourceHandle handle, VkPipelineStageFlags stages)
{
    if (!m_graph->Owns(handle)) {
        return {};
    }

    ResourceHandle newHandle = m_graph->AdvanceResourceVersion(handle);
    if (!newHandle.IsValid())
        return {};

    auto &pass = m_graph->m_passes[m_passId];

    pass.reads.push_back({handle, ResourceUsage::Read, stages, VK_ACCESS_SHADER_READ_BIT, VK_IMAGE_LAYOUT_GENERAL});
    pass.writes.push_back(
        {newHandle, ResourceUsage::Write, stages, VK_ACCESS_SHADER_WRITE_BIT, VK_IMAGE_LAYOUT_GENERAL});

    return newHandle;
}

ResourceHandle PassBuilder::ReadStorageBuffer(ResourceHandle handle)
{
    if (!m_graph->Owns(handle) || m_graph->m_resources[handle.id].type != ResourceType::Buffer)
        return handle;

    auto &pass = m_graph->m_passes[m_passId];
    pass.reads.push_back({handle, ResourceUsage::Read | ResourceUsage::ShaderRead, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                          VK_ACCESS_SHADER_READ_BIT, VK_IMAGE_LAYOUT_UNDEFINED});
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
    pass.writes.push_back({next, ResourceUsage::Write, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_ACCESS_SHADER_WRITE_BIT,
                           VK_IMAGE_LAYOUT_UNDEFINED});
    return next;
}

ResourceHandle PassBuilder::ReadIndirectBuffer(ResourceHandle handle)
{
    if (!m_graph->Owns(handle) || m_graph->m_resources[handle.id].type != ResourceType::Buffer)
        return handle;

    auto &pass = m_graph->m_passes[m_passId];
    pass.reads.push_back({handle, ResourceUsage::Read | ResourceUsage::IndirectArgument,
                          VK_PIPELINE_STAGE_DRAW_INDIRECT_BIT, VK_ACCESS_INDIRECT_COMMAND_READ_BIT,
                          VK_IMAGE_LAYOUT_UNDEFINED});
    return handle;
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
    access.stages = VK_PIPELINE_STAGE_TRANSFER_BIT;
    access.access = VK_ACCESS_TRANSFER_READ_BIT;
    access.layout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;

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
    access.stages = VK_PIPELINE_STAGE_TRANSFER_BIT;
    access.access = VK_ACCESS_TRANSFER_WRITE_BIT;
    access.layout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;

    pass.writes.push_back(access);

    return newHandle;
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
    access.stages = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    access.access = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    access.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
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
      m_rhiDevice(std::exchange(other.m_rhiDevice, nullptr)),
      m_pipelineManager(std::exchange(other.m_pipelineManager, nullptr)), m_passes(std::move(other.m_passes)),
      m_resources(std::move(other.m_resources)), m_resourceVersions(std::move(other.m_resourceVersions)),
      m_executionOrder(std::move(other.m_executionOrder)), m_backbuffer(other.m_backbuffer), m_output(other.m_output),
      m_backbufferFinalLayout(other.m_backbufferFinalLayout), m_compiled(std::exchange(other.m_compiled, false)),
      m_resourceStates(std::move(other.m_resourceStates)),
      m_initialResourceStates(std::move(other.m_initialResourceStates)),
      m_barrierScratch(std::move(other.m_barrierScratch)),
      m_bufferBarrierScratch(std::move(other.m_bufferBarrierScratch)),
      m_clearValueScratch(std::move(other.m_clearValueScratch)), m_renderPassCache(std::move(other.m_renderPassCache)),
      m_renderTargetLayoutCache(std::move(other.m_renderTargetLayoutCache)),
      m_framebufferCache(std::move(other.m_framebufferCache)),
      m_usedRenderPassKeys(std::move(other.m_usedRenderPassKeys)),
      m_usedFramebufferKeys(std::move(other.m_usedFramebufferKeys)),
      m_aliasedMemoryHeaps(std::move(other.m_aliasedMemoryHeaps))
{
    other.m_backbuffer = {};
    other.m_output = {};
    other.m_backbufferFinalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
}

RenderGraph &RenderGraph::operator=(RenderGraph &&other) noexcept
{
    if (this != &other) {
        Destroy();

        m_identity = std::move(other.m_identity);
        m_context = std::exchange(other.m_context, nullptr);
        m_rhiDevice = std::exchange(other.m_rhiDevice, nullptr);
        m_pipelineManager = std::exchange(other.m_pipelineManager, nullptr);
        m_passes = std::move(other.m_passes);
        m_resources = std::move(other.m_resources);
        m_resourceVersions = std::move(other.m_resourceVersions);
        m_executionOrder = std::move(other.m_executionOrder);
        m_backbuffer = other.m_backbuffer;
        m_output = other.m_output;
        m_backbufferFinalLayout = other.m_backbufferFinalLayout;
        m_compiled = std::exchange(other.m_compiled, false);
        m_resourceStates = std::move(other.m_resourceStates);
        m_initialResourceStates = std::move(other.m_initialResourceStates);
        m_barrierScratch = std::move(other.m_barrierScratch);
        m_bufferBarrierScratch = std::move(other.m_bufferBarrierScratch);
        m_clearValueScratch = std::move(other.m_clearValueScratch);
        m_renderPassCache = std::move(other.m_renderPassCache);
        m_renderTargetLayoutCache = std::move(other.m_renderTargetLayoutCache);
        m_framebufferCache = std::move(other.m_framebufferCache);
        m_usedRenderPassKeys = std::move(other.m_usedRenderPassKeys);
        m_usedFramebufferKeys = std::move(other.m_usedFramebufferKeys);
        m_aliasedMemoryHeaps = std::move(other.m_aliasedMemoryHeaps);

        other.m_backbuffer = {};
        other.m_output = {};
        other.m_backbufferFinalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    }
    return *this;
}

void RenderGraph::Initialize(VkDeviceContext *context, VkPipelineManager *pipelineManager)
{
    m_context = context;
    m_rhiDevice = context ? &context->GetRhiDevice() : nullptr;
    m_pipelineManager = pipelineManager;
}

void RenderGraph::Reset()
{
    // Only free per-frame resources, not cached VkRenderPass/VkFramebuffer objects
    // FreeResources() destroys per-frame framebuffers and transient images.
    // RenderPass cache and framebuffer cache persist across frames.
    FreeResources();
    m_passes.clear();
    m_resources.clear();
    m_resourceVersions.clear();
    m_executionOrder.clear();
    m_resourceStates.clear();
    m_initialResourceStates.clear();
    m_usedRenderPassKeys.clear();
    m_usedFramebufferKeys.clear();
    m_backbuffer = {};
    m_backbufferFinalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    m_output = {};
    m_compiled = false;
    m_identity.AdvanceEpoch();

    // GC: flush unused cache entries periodically
    FlushUnusedCaches();
}

void RenderGraph::Destroy()
{
    FreeResources();

    // Destroy cached render passes
    if (m_context) {
        VkDevice device = m_context->GetDevice();
        for (auto &[key, rp] : m_renderPassCache) {
            if (m_rhiDevice) {
                const auto layoutIt = m_renderTargetLayoutCache.find(key);
                if (layoutIt != m_renderTargetLayoutCache.end())
                    m_rhiDevice->Release(layoutIt->second);
            }
            if (rp != VK_NULL_HANDLE) {
                vkDestroyRenderPass(device, rp, nullptr);
            }
        }
        for (auto &[key, entry] : m_framebufferCache) {
            if (entry.framebuffer != VK_NULL_HANDLE) {
                vkDestroyFramebuffer(device, entry.framebuffer, nullptr);
            }
        }
    }
    m_renderPassCache.clear();
    m_renderTargetLayoutCache.clear();
    m_framebufferCache.clear();

    m_passes.clear();
    m_resources.clear();
    m_resourceVersions.clear();
    m_executionOrder.clear();
    m_resourceStates.clear();
    m_initialResourceStates.clear();
    m_context = nullptr;
    m_rhiDevice = nullptr;
    m_pipelineManager = nullptr;
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
    m_passes.push_back(std::move(passData));

    PassBuilder builder(this, handle.id);
    m_passes[handle.id].executeCallback = setup(builder);
    return handle;
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

    m_passes.push_back(std::move(passData));

    PassBuilder builder(this, handle.id);
    auto executeCallback = setup(builder);
    m_passes[handle.id].executeCallback = std::move(executeCallback);

    return handle;
}

ResourceHandle RenderGraph::SetBackbuffer(VkImage image, VkImageView view, VkFormat format, uint32_t width,
                                          uint32_t height, VkSampleCountFlagBits samples, VkImageLayout initialLayout)
{
    ResourceHandle handle;
    handle.scope = m_identity.Current();
    handle.id = static_cast<uint32_t>(m_resources.size());
    handle.version = 0;

    ResourceData resource;
    resource.name = "Backbuffer";
    resource.type = ResourceType::Texture2D;
    resource.textureDesc.name = "Backbuffer";
    resource.textureDesc.width = width;
    resource.textureDesc.height = height;
    resource.textureDesc.format = format;
    resource.textureDesc.samples = samples;
    resource.textureDesc.isTransient = false;
    resource.isExternal = true;
    resource.externalImage = image;
    resource.externalView = view;
    resource.rhiView = m_rhiDevice ? m_rhiDevice->RegisterTextureView(view) : rhi::TextureViewHandle{};

    m_resources.push_back(std::move(resource));
    m_resourceVersions.push_back(0);
    m_resourceStates.resize(m_resources.size());
    m_initialResourceStates.resize(m_resources.size());
    m_backbuffer = handle;

    ResourceState initialState{};
    if (initialLayout != VK_IMAGE_LAYOUT_MAX_ENUM) {
        // Caller-specified initial layout (e.g. UNDEFINED for swapchain images)
        initialState.layout = initialLayout;
        initialState.accessMask = 0;
        initialState.stages = VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT;
    } else if (samples == VK_SAMPLE_COUNT_1_BIT) {
        initialState.layout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        initialState.accessMask = VK_ACCESS_SHADER_READ_BIT;
        initialState.stages = VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT;
    } else {
        initialState.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
        initialState.accessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
        initialState.stages = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
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

    m_resources.push_back(std::move(resource));
    m_resourceVersions.push_back(0);
    m_resourceStates.resize(m_resources.size());
    m_initialResourceStates.resize(m_resources.size());

    ResourceState initialState{};
    initialState.layout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    initialState.accessMask = VK_ACCESS_SHADER_READ_BIT;
    initialState.stages = VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT;
    m_resourceStates[handle.id] = initialState;
    m_initialResourceStates[handle.id] = initialState;

    return handle;
}

void RenderGraph::SetResourceInitialState(ResourceHandle handle, VkImageLayout layout, VkAccessFlags accessMask,
                                          VkPipelineStageFlags stages)
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

bool RenderGraph::UpdatePassClearColor(const std::string &passName, float r, float g, float b, float a)
{
    for (auto &pass : m_passes) {
        if (pass.name == passName) {
            pass.clearColor = {{r, g, b, a}};
            // Refresh cached clear values for color outputs
            for (uint32_t i = 0; i < static_cast<uint32_t>(pass.colorOutputs.size()) && i < pass.cachedClearValueCount;
                 ++i) {
                pass.cachedClearValues[i].color = {{r, g, b, a}};
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
            // Refresh cached depth clear value (slot right after color outputs)
            uint32_t depthSlot = static_cast<uint32_t>(pass.colorOutputs.size());
            if (depthSlot < pass.cachedClearValueCount) {
                pass.cachedClearValues[depthSlot].depthStencil = {depth, stencil};
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

bool RenderGraph::Compile()
{
    if (m_passes.empty()) {
        INXLOG_WARN("RenderGraph::Compile - No passes to compile");
        return true;
    }

    // Step 1: Cull unused passes
    CullPasses();

    // Step 2: Compute resource lifetimes
    ComputeResourceLifetimes();

    // Step 3: Topological sort via Kahn's algorithm
    if (!TopologicalSort()) {
        return false;
    }

    // Step 4: Allocate transient resources
    if (!AllocateResources()) {
        return false;
    }

    // Step 5: Create Vulkan render passes
    if (!CreateVulkanRenderPasses()) {
        return false;
    }

    // Step 6: Create framebuffers
    if (!CreateFramebuffers()) {
        return false;
    }

    // Step 7: Pre-compute per-pass Execute() data (beginInfo, viewport, etc.)
    PrecomputeExecuteData();

    m_compiled = true;
    return true;
}

void RenderGraph::Execute(VkCommandBuffer commandBuffer)
{
    if (!m_compiled) {
        INXLOG_ERROR("RenderGraph::Execute - Graph not compiled");
        return;
    }

    // One-shot diagnostic: log detailed per-pass info for first N executions
    static int s_execDiagCount = 0;
    const bool diagEnabled = (s_execDiagCount < 2);
    if (diagEnabled) {
        ++s_execDiagCount;
    }

#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    ++s_executeProfile.executeCalls;
#endif

    // Reset resource states to initial (flat vector copy — POD memcpy).
    m_resourceStates = m_initialResourceStates;

    RenderContext context(commandBuffer, this);

    for (uint32_t passIndex : m_executionOrder) {
        auto &pass = m_passes[passIndex];

        if (pass.culled) {
            continue;
        }

#if INFERNUX_FRAME_PROFILE
        ++s_executeProfile.passCount;
#endif

        const bool isGraphicsPass = (pass.type == PassType::Graphics && pass.vulkanRenderPass != VK_NULL_HANDLE);
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
            // pClearValues points into pass.cachedClearValues (stable after Compile)
            vkCmdBeginRenderPass(commandBuffer, &pass.cachedBeginInfo, VK_SUBPASS_CONTENTS_INLINE);
            context.SetViewport(pass.cachedViewport);
            context.SetScissor(pass.cachedScissor);
#if INFERNUX_FRAME_PROFILE
            stageNow = Clock::now();
            s_executeProfile.beginPassMs += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
#endif
        }

        // Execute pass callback
        if (pass.executeCallback) {
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
            vkCmdEndRenderPass(commandBuffer);
#if INFERNUX_FRAME_PROFILE
            stageNow = Clock::now();
            s_executeProfile.endPassMs += std::chrono::duration<double, std::milli>(stageNow - stageStart).count();
#endif
        }
    }
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
        infos.push_back({pass.name, pass.type, pass.culled, pass.cullReason});
    }
    return infos;
}

std::string RenderGraph::GetDebugString() const
{
    std::ostringstream oss;
    oss << "RenderGraph (" << m_passes.size() << " passes, " << m_resources.size() << " resources)\n";

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
                oss << m_resources[read.handle.id].name << " ";
            }
            oss << "\n";
        }

        if (!pass.writes.empty()) {
            oss << "    Writes: ";
            for (const auto &write : pass.writes) {
                oss << m_resources[write.handle.id].name << " ";
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

VkRenderPass RenderGraph::GetPassRenderPass(const std::string &passName) const
{
    for (const auto &pass : m_passes) {
        if (pass.name == passName && pass.vulkanRenderPass != VK_NULL_HANDLE) {
            return pass.vulkanRenderPass;
        }
    }
    return VK_NULL_HANDLE;
}

VkRenderPass RenderGraph::GetPassRenderPass(PassHandle pass) const
{
    if (!Owns(pass) || pass.id >= m_passes.size()) {
        return VK_NULL_HANDLE;
    }
    return m_passes[pass.id].vulkanRenderPass;
}

rhi::RenderTargetLayoutHandle RenderGraph::GetPassRenderTargetLayout(const std::string &passName) const
{
    for (const auto &pass : m_passes) {
        if (pass.name == passName)
            return pass.renderTargetLayout;
    }
    return {};
}

rhi::RenderTargetLayoutHandle RenderGraph::GetPassRenderTargetLayout(PassHandle pass) const
{
    if (!Owns(pass) || pass.id >= m_passes.size())
        return {};
    return m_passes[pass.id].renderTargetLayout;
}

VkRenderPass RenderGraph::GetCompatibleRenderPass() const
{
    // Return the first non-culled graphics pass render pass
    // This is suitable for pipeline creation since all scene passes
    // share the same attachment format
    for (const auto &pass : m_passes) {
        if (!pass.culled && pass.type == PassType::Graphics && pass.vulkanRenderPass != VK_NULL_HANDLE) {
            return pass.vulkanRenderPass;
        }
    }
    return VK_NULL_HANDLE;
}

} // namespace vk
} // namespace infernux
