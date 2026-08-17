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
    std::unique_ptr<vk::VkImageHandle> color;
    std::unique_ptr<vk::VkImageHandle> depth;
    VkImageLayout colorLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    VkImageLayout depthLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    uint32_t width = 0;
    uint32_t height = 0;
    VkFormat depthFormat = VK_FORMAT_UNDEFINED;
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
    const auto dynamicCommands =
        m_core ? rhi::ResolveDynamicRenderingCommands(m_core->GetDevice()) : rhi::DynamicRenderingCommands{};
    const bool dynamicRenderingAvailable =
        m_core && m_core->GetDeviceContext().GetRhiDevice().GetCapabilityState().dynamicRendering.enabled &&
        dynamicCommands.IsValid();
    if (m_target && m_target->color && m_target->depth && width == m_target->width && height == m_target->height &&
        dynamicRenderingAvailable)
        return true;
    if (!dynamicRenderingAvailable || width == 0 || height == 0)
        return false;

    auto candidate = std::make_unique<TargetGeneration>();
    auto &resources = m_core->GetResourceManager();
    candidate->color = resources.CreateImage(width, height, kPickingFormat,
                                             VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT);
    candidate->depthFormat = m_core->GetDeviceContext().FindDepthFormat();
    candidate->depth = resources.CreateDepthBuffer(width, height, candidate->depthFormat);
    if (!candidate->color || !candidate->color->CreateView(kPickingFormat, VK_IMAGE_ASPECT_COLOR_BIT) ||
        !candidate->depth)
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

    const auto dynamicCommands = rhi::ResolveDynamicRenderingCommands(m_core->GetDevice());
    if (!dynamicCommands.IsValid()) {
        PublishFailure(request.id, "Dynamic Rendering commands are unavailable for scene picking");
        return;
    }
    VkImageMemoryBarrier barriers[2]{};
    barriers[0] = vkrender::MakeImageBarrier(
        target.color->GetImage(), target.colorLayout, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
        VK_IMAGE_ASPECT_COLOR_BIT,
        target.colorLayout == VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL ? VK_ACCESS_TRANSFER_READ_BIT : 0,
        VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT);
    barriers[1] = vkrender::MakeImageBarrier(
        target.depth->GetImage(), target.depthLayout, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL,
        rhi::ToVkImageAspectMask(target.depthFormat),
        target.depthLayout == VK_IMAGE_LAYOUT_UNDEFINED ? 0 : VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT,
        VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT);
    vkCmdPipelineBarrier(commandBuffer,
                         VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT |
                             VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT,
                         VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT, 0,
                         0, nullptr, 0, nullptr, 2, barriers);
    target.colorLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    target.depthLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;

    VkRenderingAttachmentInfo colorAttachment{};
    colorAttachment.sType = VK_STRUCTURE_TYPE_RENDERING_ATTACHMENT_INFO;
    colorAttachment.imageView = target.color->GetView();
    colorAttachment.imageLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    colorAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    colorAttachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    colorAttachment.clearValue.color.uint32[0] = 0;
    colorAttachment.clearValue.color.uint32[1] = 0;
    VkRenderingAttachmentInfo depthAttachment{};
    depthAttachment.sType = VK_STRUCTURE_TYPE_RENDERING_ATTACHMENT_INFO;
    depthAttachment.imageView = target.depth->GetView();
    depthAttachment.imageLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    depthAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    depthAttachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    depthAttachment.clearValue.depthStencil = {1.0f, 0};
    VkRenderingInfo rendering{};
    rendering.sType = VK_STRUCTURE_TYPE_RENDERING_INFO;
    rendering.renderArea.extent = {target.width, target.height};
    rendering.layerCount = 1;
    rendering.colorAttachmentCount = 1;
    rendering.pColorAttachments = &colorAttachment;
    rendering.pDepthAttachment = &depthAttachment;
    rendering.pStencilAttachment =
        rhi::IsStencilFormat(rhi::FromVkFormat(target.depthFormat)) ? &depthAttachment : nullptr;
    dynamicCommands.begin(commandBuffer, &rendering);

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
    pickingPass.renderingMode = MaterialPassRenderingMode::DynamicRendering;
    m_core->DrawSceneFiltered(commandBuffer, target.width, target.height, perViewGroup, viewMatrix, 0, 5000,
                              "front_to_back", {}, {}, &pickingPass);
    if (!particleEntries.empty()) {
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
                    encoder, {}, pickingPass, entry.indirectArguments, view, entry.ownerObjectId, entry.renderIndices);
            }
        }
    }
    dynamicCommands.end(commandBuffer);

    const VkImageMemoryBarrier renderToTransfer = vkrender::MakeImageBarrier(
        target.color->GetImage(), VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
        VK_IMAGE_ASPECT_COLOR_BIT, VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
    vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                         0, 0, nullptr, 0, nullptr, 1, &renderToTransfer);
    target.colorLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;

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
