#include "ScenePickingService.h"

#include "InxVkCoreModular.h"
#include "MaterialPassPipeline.h"
#include "particle/ParticleGpuDrawRegistry.h"
#include "vk/RhiVulkanTypes.h"
#include "vk/VkHandle.h"
#include "vk/VkRenderUtils.h"
#include "vk/VkResourceManager.h"
#include "vk/VulkanRhiDevice.h"
#include <function/scene/Camera.h>
#include <function/scene/SceneManager.h>

#include <glm/gtc/matrix_inverse.hpp>

#include <algorithm>
#include <cstring>

namespace infernux
{

namespace
{
constexpr VkFormat kPickingFormat = VK_FORMAT_R32G32_UINT;
constexpr VkDeviceSize kPickingPixelBytes = sizeof(uint32_t) * 2u;
} // namespace

struct ScenePickingService::TargetGeneration
{
    InxVkCoreModular *core = nullptr;
    std::unique_ptr<vk::VkImageHandle> color;
    std::unique_ptr<vk::VkImageHandle> depth;
    VkRenderPass renderPass = VK_NULL_HANDLE;
    VkFramebuffer framebuffer = VK_NULL_HANDLE;
    uint32_t width = 0;
    uint32_t height = 0;
    VkFormat depthFormat = VK_FORMAT_UNDEFINED;
    rhi::RenderTargetLayoutHandle renderTargetLayout;

    ~TargetGeneration()
    {
        if (!core)
            return;
        if (renderTargetLayout.IsValid())
            core->GetDeviceContext().GetRhiDevice().Release(renderTargetLayout);
        if (core->GetDevice() != VK_NULL_HANDLE) {
            if (framebuffer != VK_NULL_HANDLE)
                vkDestroyFramebuffer(core->GetDevice(), framebuffer, nullptr);
            if (renderPass != VK_NULL_HANDLE)
                vkDestroyRenderPass(core->GetDevice(), renderPass, nullptr);
        }
    }
};

ScenePickingService::ScenePickingService() = default;

ScenePickingService::~ScenePickingService()
{
    Destroy();
}

void ScenePickingService::Initialize(InxVkCoreModular *core)
{
    m_core = core;
}

void ScenePickingService::Destroy()
{
    if (m_state) {
        std::scoped_lock lock(m_state->mutex);
        for (auto &[id, snapshot] : m_state->snapshots) {
            (void)id;
            if (snapshot.status == ScenePickStatus::Pending) {
                snapshot.status = ScenePickStatus::Cancelled;
                snapshot.error = "Scene picking service stopped";
            }
        }
    }
    m_hasPending = false;
    DestroyTarget();
    m_particleDrawRegistry = nullptr;
    m_core = nullptr;
}

uint64_t ScenePickingService::Request(float x, float y, float viewportWidth, float viewportHeight)
{
    if (!m_core || viewportWidth <= 0.0f || viewportHeight <= 0.0f)
        return 0;

    if (m_hasPending) {
        std::scoped_lock lock(m_state->mutex);
        auto found = m_state->snapshots.find(m_pending.id);
        if (found != m_state->snapshots.end()) {
            found->second.status = ScenePickStatus::Cancelled;
            found->second.error = "Superseded by a newer scene pick request";
        }
    }

    const uint64_t id = m_nextRequestId++;
    m_pending = {id, x, y, viewportWidth, viewportHeight};
    m_hasPending = true;

    std::scoped_lock lock(m_state->mutex);
    m_state->snapshots[id] = {id, ScenePickStatus::Pending, 0, {}};
    if (m_state->snapshots.size() > 64) {
        for (auto it = m_state->snapshots.begin(); it != m_state->snapshots.end();) {
            if (it->first != id && it->second.status != ScenePickStatus::Pending)
                it = m_state->snapshots.erase(it);
            else
                ++it;
            if (m_state->snapshots.size() <= 64)
                break;
        }
    }
    return id;
}

ScenePickSnapshot ScenePickingService::Query(uint64_t requestId) const
{
    if (!m_state || requestId == 0)
        return {requestId, ScenePickStatus::Unknown, 0, "Unknown scene pick request"};
    std::scoped_lock lock(m_state->mutex);
    const auto found = m_state->snapshots.find(requestId);
    if (found == m_state->snapshots.end())
        return {requestId, ScenePickStatus::Unknown, 0, "Unknown scene pick request"};
    return found->second;
}

bool ScenePickingService::HasPendingRecord() const noexcept
{
    return m_hasPending;
}

void ScenePickingService::PublishFailure(uint64_t requestId, const std::string &error)
{
    std::scoped_lock lock(m_state->mutex);
    auto &snapshot = m_state->snapshots[requestId];
    snapshot.requestId = requestId;
    snapshot.status = ScenePickStatus::Failed;
    snapshot.error = error;
}

bool ScenePickingService::EnsureTarget(uint32_t width, uint32_t height)
{
    if (m_target && m_target->color && m_target->depth && m_target->renderPass != VK_NULL_HANDLE &&
        m_target->framebuffer != VK_NULL_HANDLE && width == m_target->width && height == m_target->height)
        return true;
    if (!m_core || width == 0 || height == 0)
        return false;

    auto candidate = std::make_unique<TargetGeneration>();
    candidate->core = m_core;
    auto &resources = m_core->GetResourceManager();
    candidate->color = resources.CreateImage(width, height, kPickingFormat,
                                             VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT);
    candidate->depthFormat = m_core->GetDeviceContext().FindDepthFormat();
    candidate->depth = resources.CreateDepthBuffer(width, height, candidate->depthFormat);
    if (!candidate->color || !candidate->color->CreateView(kPickingFormat, VK_IMAGE_ASPECT_COLOR_BIT) ||
        !candidate->depth)
        return false;

    VkAttachmentDescription attachments[2]{};
    attachments[0].format = kPickingFormat;
    attachments[0].samples = VK_SAMPLE_COUNT_1_BIT;
    attachments[0].loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    attachments[0].storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    attachments[0].stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    attachments[0].stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    attachments[0].initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    attachments[0].finalLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    attachments[1].format = candidate->depthFormat;
    attachments[1].samples = VK_SAMPLE_COUNT_1_BIT;
    attachments[1].loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    attachments[1].storeOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    attachments[1].stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    attachments[1].stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    attachments[1].initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    attachments[1].finalLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;

    VkAttachmentReference colorRef{0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkAttachmentReference depthRef{1, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL};
    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = 1;
    subpass.pColorAttachments = &colorRef;
    subpass.pDepthStencilAttachment = &depthRef;
    const VkSubpassDependency dependency = vkrender::MakePipelineCompatibleSubpassDependency();

    VkRenderPassCreateInfo renderPassInfo{};
    renderPassInfo.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
    renderPassInfo.attachmentCount = 2;
    renderPassInfo.pAttachments = attachments;
    renderPassInfo.subpassCount = 1;
    renderPassInfo.pSubpasses = &subpass;
    renderPassInfo.dependencyCount = 1;
    renderPassInfo.pDependencies = &dependency;
    if (vkCreateRenderPass(m_core->GetDevice(), &renderPassInfo, nullptr, &candidate->renderPass) != VK_SUCCESS)
        return false;
    candidate->renderTargetLayout =
        m_core->GetDeviceContext().GetRhiDevice().RegisterRenderTargetLayout(candidate->renderPass);
    if (!candidate->renderTargetLayout.IsValid())
        return false;

    const VkImageView views[] = {candidate->color->GetView(), candidate->depth->GetView()};
    VkFramebufferCreateInfo framebufferInfo{};
    framebufferInfo.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
    framebufferInfo.renderPass = candidate->renderPass;
    framebufferInfo.attachmentCount = 2;
    framebufferInfo.pAttachments = views;
    framebufferInfo.width = width;
    framebufferInfo.height = height;
    framebufferInfo.layers = 1;
    if (vkCreateFramebuffer(m_core->GetDevice(), &framebufferInfo, nullptr, &candidate->framebuffer) != VK_SUCCESS)
        return false;

    candidate->width = width;
    candidate->height = height;
    if (m_target) {
        const rhi::SubmissionSerial cutoverEpoch =
            m_core->GetBackendContext().Queues().GetLastReservedCompletionEpoch();
        std::shared_ptr<TargetGeneration> retired(m_target.release());
        m_core->GetRetirementQueue().RetireAfter(cutoverEpoch, [retired = std::move(retired)] {});
    }
    m_target = std::move(candidate);
    return true;
}

void ScenePickingService::DestroyTarget()
{
    if (!m_target)
        return;
    if (m_core && !m_core->IsShuttingDown()) {
        const rhi::SubmissionSerial cutoverEpoch =
            m_core->GetBackendContext().Queues().GetLastReservedCompletionEpoch();
        std::shared_ptr<TargetGeneration> retired(m_target.release());
        m_core->GetRetirementQueue().RetireAfter(cutoverEpoch, [retired = std::move(retired)] {});
    } else {
        m_target.reset();
    }
}

void ScenePickingService::Record(VkCommandBuffer commandBuffer, uint32_t targetWidth, uint32_t targetHeight,
                                 rhi::BindGroupHandle perViewGroup, const glm::mat4 &viewMatrix)
{
    if (!m_hasPending || !m_core || commandBuffer == VK_NULL_HANDLE)
        return;

    const RequestData request = m_pending;
    m_hasPending = false;
    if (!EnsureTarget(targetWidth, targetHeight)) {
        PublishFailure(request.id, "Failed to create GPU scene picking target");
        return;
    }
    TargetGeneration &target = *m_target;

    auto stagingUnique = m_core->GetResourceManager().CreateStagingBuffer(kPickingPixelBytes);
    if (!stagingUnique) {
        PublishFailure(request.id, "Failed to allocate GPU scene picking readback buffer");
        return;
    }
    auto staging = std::shared_ptr<vk::VkBufferHandle>(std::move(stagingUnique));
    void *mapped = staging->Map();
    if (!mapped) {
        PublishFailure(request.id, "Failed to map GPU scene picking readback buffer");
        return;
    }
    std::memset(mapped, 0, static_cast<size_t>(kPickingPixelBytes));

    const auto particleEntries = m_particleDrawRegistry ? m_particleDrawRegistry->Snapshot(0, 5000)
                                                        : std::vector<particle::GpuParticleDrawEntry>{};
    if (!particleEntries.empty()) {
        VkMemoryBarrier particleBarrier{};
        particleBarrier.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
        particleBarrier.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
        particleBarrier.dstAccessMask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_INDIRECT_COMMAND_READ_BIT;
        vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                             VK_PIPELINE_STAGE_VERTEX_SHADER_BIT | VK_PIPELINE_STAGE_DRAW_INDIRECT_BIT, 0, 1,
                             &particleBarrier, 0, nullptr, 0, nullptr);
    }

    VkClearValue clears[2]{};
    clears[0].color.uint32[0] = 0;
    clears[0].color.uint32[1] = 0;
    clears[1].depthStencil = {1.0f, 0};
    VkRenderPassBeginInfo begin{};
    begin.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
    begin.renderPass = target.renderPass;
    begin.framebuffer = target.framebuffer;
    begin.renderArea.extent = {target.width, target.height};
    begin.clearValueCount = 2;
    begin.pClearValues = clears;
    vkCmdBeginRenderPass(commandBuffer, &begin, VK_SUBPASS_CONTENTS_INLINE);

    VkViewport viewport{};
    viewport.width = static_cast<float>(target.width);
    viewport.height = static_cast<float>(target.height);
    viewport.minDepth = 0.0f;
    viewport.maxDepth = 1.0f;
    vkCmdSetViewport(commandBuffer, 0, 1, &viewport);

    VkRect2D scissor{};
    scissor.extent = {target.width, target.height};
    vkCmdSetScissor(commandBuffer, 0, 1, &scissor);

    MaterialPassPipelineDescriptor pickingPass;
    pickingPass.target = ShaderCompileTarget::Picking;
    pickingPass.colorFormats = {rhi::PixelFormat::RG32UInt};
    pickingPass.depthFormat = rhi::FromVkFormat(target.depthFormat);
    pickingPass.samples = rhi::SampleCount::One;
    m_core->DrawSceneFiltered(commandBuffer, target.width, target.height, perViewGroup, viewMatrix, 0, 5000,
                              "front_to_back", {}, {}, &pickingPass);
    if (!particleEntries.empty() && target.renderTargetLayout.IsValid()) {
        Camera *camera = SceneManager::Instance().GetEditorCameraController().GetCamera();
        if (camera) {
            particle::GpuParticleViewConstants view;
            const glm::mat4 cameraView = camera->GetViewMatrix();
            const glm::mat4 viewProjection = camera->GetProjectionMatrix() * cameraView;
            const glm::mat4 inverseView = glm::inverse(cameraView);
            std::memcpy(view.viewProjection.data(), &viewProjection[0][0], sizeof(viewProjection));
            std::memcpy(view.cameraRight.data(), &inverseView[0][0], sizeof(glm::vec4));
            std::memcpy(view.cameraUp.data(), &inverseView[1][0], sizeof(glm::vec4));
            std::memcpy(view.alignmentReference.data(), &inverseView[3][0], sizeof(glm::vec4));
            vk::VulkanGraphicsCommandContext graphicsContext;
            auto encoder =
                m_core->GetDeviceContext().GetRhiDevice().MakeGraphicsCommandEncoder(graphicsContext, commandBuffer);
            for (const auto &entry : particleEntries) {
                if (!entry.renderer || entry.ownerObjectId == 0)
                    continue;
                [[maybe_unused]] const bool recorded = entry.renderer->RecordPickingDraw(
                    encoder, target.renderTargetLayout, pickingPass, entry.indirectArguments, view, entry.ownerObjectId,
                    entry.renderIndices);
            }
        }
    }
    vkCmdEndRenderPass(commandBuffer);

    VkMemoryBarrier renderToTransfer{};
    renderToTransfer.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
    renderToTransfer.srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    renderToTransfer.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                         0, 1, &renderToTransfer, 0, nullptr, 0, nullptr);

    const float normalizedX = std::clamp(request.x / request.viewportWidth, 0.0f, 0.999999f);
    const float normalizedY = std::clamp(request.y / request.viewportHeight, 0.0f, 0.999999f);
    const int32_t pixelX = static_cast<int32_t>(normalizedX * static_cast<float>(target.width));
    const int32_t pixelY = static_cast<int32_t>(normalizedY * static_cast<float>(target.height));
    VkBufferImageCopy copy{};
    copy.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    copy.imageOffset = {pixelX, pixelY, 0};
    copy.imageExtent = {1, 1, 1};
    vkCmdCopyImageToBuffer(commandBuffer, target.color->GetImage(), VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                           staging->GetBuffer(), 1, &copy);

    const std::shared_ptr<SharedState> state = m_state;
    m_core->GetRetirementQueue().Retire([state, staging, requestId = request.id] {
        ScenePickSnapshot result{requestId, ScenePickStatus::Failed, 0, "GPU scene picking readback was unavailable"};
        if (const auto *words = static_cast<const uint32_t *>(staging->GetMappedPtr())) {
            result.objectId = static_cast<uint64_t>(words[0]) | (static_cast<uint64_t>(words[1]) << 32u);
            result.status = ScenePickStatus::Completed;
            result.error.clear();
        }
        std::scoped_lock lock(state->mutex);
        const auto found = state->snapshots.find(requestId);
        if (found != state->snapshots.end() && found->second.status == ScenePickStatus::Pending)
            found->second = std::move(result);
    });
}

} // namespace infernux
