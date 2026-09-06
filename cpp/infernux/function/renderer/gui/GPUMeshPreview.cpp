/**
 * @file GPUMeshPreview.cpp
 * @brief GPU mesh preview — renders an arbitrary InxMesh with per-submesh
 *        materials into a small offscreen attachment set and reads back RGBA8
 *        pixels for editor thumbnails
 * (model / prefab previews).
 */

#include "GPUMeshPreview.h"
#include "InxError.h"
#include <backends/imgui_impl_vulkan.h>
#include <function/renderer/EngineGlobals.h>
#include <function/renderer/InxRenderStruct.h>
#include <function/renderer/InxVkCoreModular.h>
#include <function/renderer/MaterialPipelineManager.h>
#include <function/renderer/RenderInstanceHistory.h>
#include <function/renderer/shader/ShaderProgram.h>
#include <function/renderer/vk/DescriptorBindTrace.h>
#include <function/renderer/vk/RhiVulkanTypes.h>
#include <function/renderer/vk/VkRenderUtils.h>
#include <function/renderer/vk/VkResourceManager.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/scene/Light.h>
#include <function/scene/LightingData.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <core/log/InxLog.h>
#include <cstring>
#include <glm/glm.hpp>
#include <glm/gtc/matrix_transform.hpp>
#include <stdexcept>

namespace infernux
{

namespace
{
constexpr float kMeshPreviewFovDeg = 30.0f;
constexpr int kPreviewSupersampleFactor = 2;

/// @brief Downsample RGBA image from srcSize to dstSize using box filter.
void DownsampleRGBABox(const std::vector<unsigned char> &srcPixels, int srcSize, int dstSize,
                       std::vector<unsigned char> &dstPixels)
{
    if (srcSize <= 0 || dstSize <= 0 || srcPixels.empty()) {
        dstPixels.clear();
        return;
    }
    if (srcSize == dstSize) {
        dstPixels = srcPixels;
        return;
    }

    dstPixels.resize(static_cast<size_t>(dstSize) * dstSize * 4);
    const float scale = static_cast<float>(srcSize) / static_cast<float>(dstSize);

    for (int dy = 0; dy < dstSize; ++dy) {
        for (int dx = 0; dx < dstSize; ++dx) {
            const int sx0 = static_cast<int>(dx * scale);
            const int sy0 = static_cast<int>(dy * scale);
            const int sx1 = std::min(static_cast<int>((dx + 1) * scale), srcSize);
            const int sy1 = std::min(static_cast<int>((dy + 1) * scale), srcSize);
            float r = 0, g = 0, b = 0, a = 0;
            int count = 0;
            for (int sy = sy0; sy < sy1; ++sy) {
                for (int sx = sx0; sx < sx1; ++sx) {
                    const size_t idx = (static_cast<size_t>(sy) * srcSize + sx) * 4;
                    r += srcPixels[idx + 0];
                    g += srcPixels[idx + 1];
                    b += srcPixels[idx + 2];
                    a += srcPixels[idx + 3];
                    ++count;
                }
            }
            if (count > 0) {
                const float inv = 1.0f / count;
                const size_t dstIdx = (static_cast<size_t>(dy) * dstSize + dx) * 4;
                dstPixels[dstIdx + 0] = static_cast<unsigned char>(r * inv + 0.5f);
                dstPixels[dstIdx + 1] = static_cast<unsigned char>(g * inv + 0.5f);
                dstPixels[dstIdx + 2] = static_cast<unsigned char>(b * inv + 0.5f);
                dstPixels[dstIdx + 3] = static_cast<unsigned char>(a * inv + 0.5f);
            }
        }
    }
}

/// @brief Compute camera transform that fits a bounding box into the viewport.
struct FitCameraResult
{
    glm::mat4 view;
    glm::mat4 proj;
    glm::vec3 cameraPos;
};

FitCameraResult FitCameraToBounds(const glm::vec3 &boundsMin, const glm::vec3 &boundsMax, float fovDeg)
{
    const glm::vec3 center = (boundsMin + boundsMax) * 0.5f;
    const glm::vec3 extent = boundsMax - boundsMin;
    const float maxExtent = std::max({extent.x, extent.y, extent.z, 0.001f});

    // Camera looks from front-top-right, raised higher above center
    const glm::vec3 viewDir = glm::normalize(glm::vec3(-0.6f, -0.5f, -0.7f));
    const float halfFov = glm::radians(fovDeg) * 0.5f;
    // Bounding sphere radius from actual diagonal, with padding
    const float radius = glm::length(extent) * 0.5f;
    // Tighter framing (was 1.15) so model / prefab thumbnails sit closer and larger.
    const float distance = (radius / std::sin(halfFov)) * 0.98f;

    // Shift look-at target slightly upward so the model isn't bottom-heavy
    const glm::vec3 lookAt = center + glm::vec3(0.0f, maxExtent * 0.05f, 0.0f);
    const glm::vec3 cameraPos = lookAt - viewDir * distance;

    FitCameraResult result;
    result.cameraPos = cameraPos;
    result.view = glm::lookAt(cameraPos, lookAt, glm::vec3(0.0f, 1.0f, 0.0f));
    result.proj = glm::perspective(glm::radians(fovDeg), 1.0f, distance * 0.01f, distance * 4.0f);
    result.proj[1][1] *= -1.0f; // Vulkan Y-flip
    return result;
}

} // anonymous namespace

// ============================================================================
// Constructor / Destructor
// ============================================================================

GPUMeshPreview::GPUMeshPreview(InxVkCoreModular *vkCore) : m_vkCore(vkCore)
{
    m_renderView.id = rhi::AllocateRenderViewId();
    m_renderView.device = vkCore ? vkCore->GetDeviceContext().GetDeviceId() : rhi::InvalidDeviceId;
    m_renderView.kind = rhi::RenderViewKind::Preview;
    m_renderView.output = rhi::RenderOutputKind::OffscreenTexture;
    m_renderView.revision = 1;
}

GPUMeshPreview::~GPUMeshPreview()
{
    if (!m_vkCore)
        return;
    DestroyImGuiDisplayDescriptor();
    DestroyAttachments();
    DestroyViewResources();
}

// ============================================================================
// RenderToPixels
// ============================================================================

std::shared_ptr<vk::ImageReadbackTicket>
GPUMeshPreview::BeginRenderToPixels(const InxMesh &mesh, const std::vector<std::shared_ptr<InxMaterial>> &materials,
                                    int size)
{
    if (!m_vkCore || size <= 0)
        return nullptr;
    // Auto-fit the camera to the mesh AABB, then delegate to the explicit-camera path.
    auto cam = FitCameraToBounds(mesh.GetBoundsMin(), mesh.GetBoundsMax(), kMeshPreviewFovDeg);
    return BeginRenderToPixelsCamera(mesh, materials, size, cam.view, cam.proj, cam.cameraPos);
}

std::shared_ptr<vk::ImageReadbackTicket> GPUMeshPreview::BeginRenderToPixelsCamera(
    const InxMesh &mesh, const std::vector<std::shared_ptr<InxMaterial>> &materials, int size, const glm::mat4 &view,
    const glm::mat4 &proj, const glm::vec3 &cameraPos, bool cloneMaterials)
{
    if (!m_vkCore || size <= 0)
        return nullptr;
    if (m_activeReadback && !m_activeReadback->IsDone())
        return nullptr;
    m_activeReadback.reset();
    if (m_activeSubmission && !m_activeSubmission->IsComplete())
        return nullptr;
    m_activeSubmission.reset();

    const auto &vertices = mesh.GetVertices();
    const auto &indices = mesh.GetIndices();
    if (vertices.empty() || indices.empty())
        return nullptr;

    const int renderSize = std::max(size, size * kPreviewSupersampleFactor);

    if (!EnsureResources(renderSize))
        return nullptr;

    // ── Upload mesh geometry to temporary GPU buffers ────────────────
    auto &rm = m_vkCore->GetResourceManager();
    auto vbo = rm.CreateVertexBuffer(vertices.data(), vertices.size() * sizeof(Vertex));
    auto ibo = rm.CreateIndexBuffer(indices.data(), indices.size() * sizeof(uint32_t));
    if (!vbo || !ibo)
        return nullptr;

    // ── Prepare per-submesh material pipelines ───────────────────────
    // Get a default material for submeshes without an assigned material.
    auto defaultMat = AssetRegistry::Instance().GetBuiltinMaterial("DefaultLit");

    struct SubmeshBinding
    {
        const SubMesh *submesh = nullptr;
        std::shared_ptr<InxMaterial> ownedMaterial; // keep-alive
        VkPipeline pipeline = VK_NULL_HANDLE;
        VkPipelineLayout pipelineLayout = VK_NULL_HANDLE;
        VkDescriptorSet materialDescSet = VK_NULL_HANDLE;
        const ShaderProgram *program = nullptr;
    };

    std::vector<SubmeshBinding> bindings;
    bindings.reserve(mesh.GetSubMeshCount());

    for (uint32_t si = 0; si < mesh.GetSubMeshCount(); ++si) {
        const SubMesh &sm = mesh.GetSubMesh(si);
        if (sm.indexCount == 0)
            continue;

        // Pick material for this submesh
        std::shared_ptr<InxMaterial> srcMat;
        if (sm.materialSlot < materials.size() && materials[sm.materialSlot])
            srcMat = materials[sm.materialSlot];
        else
            srcMat = defaultMat;

        if (!srcMat)
            continue;

        // Prepare pipeline. Thumbnails clone for isolation; live previews pass
        // persistent dedicated clones (cloneMaterials=false) so the cached pipeline
        // is reused every frame instead of rebuilt (Clone() makes a unique key each
        // call → cache miss → vkCreateGraphicsPipelines per frame → playback lag).
        std::shared_ptr<InxMaterial> previewMat;
        if (cloneMaterials) {
            previewMat = srcMat->Clone();
            if (!previewMat)
                continue;
            previewMat->ClearAllPassPipelines();
        } else {
            previewMat = srcMat;
        }
        bool pipelineReady = m_vkCore->RefreshPreviewMaterialPipeline(previewMat, previewMat->GetVertShaderName(),
                                                                      previewMat->GetFragShaderName(), false);
        if (!pipelineReady && srcMat != defaultMat && defaultMat) {
            // A malformed embedded material must not hide the whole FBX
            // submesh. Keep the complete model thumbnail useful with the
            // engine's known-good preview material.
            previewMat = cloneMaterials ? defaultMat->Clone() : defaultMat;
            if (previewMat && cloneMaterials)
                previewMat->ClearAllPassPipelines();
            pipelineReady =
                previewMat && m_vkCore->RefreshPreviewMaterialPipeline(previewMat, previewMat->GetVertShaderName(),
                                                                       previewMat->GetFragShaderName(), false);
        }
        if (!pipelineReady || !previewMat)
            continue;

        MaterialPassRenderData *rd = m_vkCore->GetOrCreatePreviewMaterialPass(previewMat);
        if (!rd || !rd->isValid || rd->descriptorSet == VK_NULL_HANDLE)
            continue;

        previewMat->SetPassPipeline(ShaderCompileTarget::Forward, rd->pipeline);
        previewMat->SetPassPipelineLayout(ShaderCompileTarget::Forward, rd->pipelineLayout);
        previewMat->SetPassDescriptorSet(ShaderCompileTarget::Forward, rd->descriptorSet);
        previewMat->SetPassShaderProgram(ShaderCompileTarget::Forward, rd->shaderProgram);

        SubmeshBinding b;
        b.submesh = &sm;
        b.ownedMaterial = previewMat;
        b.pipeline = rd->pipeline;
        b.pipelineLayout = rd->pipelineLayout;
        b.materialDescSet = rd->descriptorSet;
        b.program = rd->shaderProgram.get();
        bindings.push_back(std::move(b));
    }

    if (bindings.empty())
        return nullptr;

    // Update UBO data for each preview material
    for (auto &b : bindings)
        m_vkCore->UpdateMaterialUBO(*b.ownedMaterial);

    // Texture synchronization may publish a copy-on-write material descriptor
    // and retire the handle captured above. Re-read every binding after the
    // update so this command buffer never skips all geometry on stale handles.
    bindings.erase(std::remove_if(bindings.begin(), bindings.end(),
                                  [&](SubmeshBinding &binding) {
                                      MaterialPassRenderData *rd =
                                          m_vkCore->GetOrCreatePreviewMaterialPass(binding.ownedMaterial);
                                      if (!rd || !rd->isValid || rd->pipeline == VK_NULL_HANDLE ||
                                          rd->pipelineLayout == VK_NULL_HANDLE || rd->descriptorSet == VK_NULL_HANDLE ||
                                          !rd->shaderProgram)
                                          return true;
                                      binding.pipeline = rd->pipeline;
                                      binding.pipelineLayout = rd->pipelineLayout;
                                      binding.materialDescSet = rd->descriptorSet;
                                      binding.program = rd->shaderProgram.get();
                                      return false;
                                  }),
                   bindings.end());
    if (bindings.empty())
        return nullptr;

    // ── Scene UBO ────────────────────────────────────────────────────
    // Use identity model matrix; the mesh vertices are in local space
    // and the camera is positioned to look at the mesh bounds.
    glm::mat4 modelMat = glm::mat4(1.0f);

    UniformBufferObject sceneUBO{};
    sceneUBO.model = modelMat;
    sceneUBO.view = view;
    sceneUBO.proj = proj;
    sceneUBO.previousViewProj = proj * view;
    sceneUBO.inverseViewProj = glm::inverse(proj * view);
    sceneUBO.projectionParams = glm::vec4(0.1f, 100.0f, 0.01f, 0.001f);
    sceneUBO.zBufferParams = glm::vec4(-999.0f, 1000.0f, -9.99f, 10.0f);

    // ── Lighting UBO ─────────────────────────────────────────────────
    ShaderLightingUBO lightingUBO{};
    memset(&lightingUBO, 0, sizeof(lightingUBO));
    lightingUBO.lightCounts = glm::ivec4(2, 0, 0, 0);
    lightingUBO.ambientColor = glm::vec4(0.08f, 0.08f, 0.09f, 1.0f);
    lightingUBO.ambientSkyColor = glm::vec4(0.20f, 0.22f, 0.26f, 0.55f);
    lightingUBO.ambientEquatorColor = glm::vec4(0.10f, 0.11f, 0.13f, 1.0f);
    lightingUBO.ambientGroundColor = glm::vec4(0.05f, 0.04f, 0.035f, 0.30f);
    lightingUBO.cameraPos = glm::vec4(cameraPos, 1.0f);

    lightingUBO.directionalLights[0].direction = glm::vec4(glm::normalize(glm::vec3(-0.7f, -1.0f, -0.5f)), 0.0f);
    lightingUBO.directionalLights[0].color = glm::vec4(1.8f, 1.71f, 1.62f, 1.8f);
    lightingUBO.directionalLights[0].metadata =
        glm::uvec4(~0u, static_cast<uint32_t>(LightInfluenceDomain::Geometry), 0u, 0u);

    lightingUBO.directionalLights[1].direction = glm::vec4(glm::normalize(glm::vec3(0.5f, 0.3f, -0.7f)), 0.0f);
    lightingUBO.directionalLights[1].color = glm::vec4(0.36f, 0.42f, 0.51f, 0.6f);
    lightingUBO.directionalLights[1].metadata =
        glm::uvec4(~0u, static_cast<uint32_t>(LightInfluenceDomain::Geometry), 0u, 0u);

    // ── Engine globals UBO ───────────────────────────────────────────
    EngineGlobalsUBO globalsUBO{};
    memset(&globalsUBO, 0, sizeof(globalsUBO));
    globalsUBO.screenParams =
        glm::vec4(static_cast<float>(renderSize), static_cast<float>(renderSize), 1.0f / renderSize, 1.0f / renderSize);
    globalsUBO.worldSpaceCameraPos = glm::vec4(cameraPos, 1.0f);

    const VkBuffer sceneUBOBuf = m_previewSceneUbo->GetBuffer();
    const VkBuffer lightingUBOBuf = m_previewLightingUbo->GetBuffer();
    const VkBuffer globalsUBOBuf = m_previewGlobalsUbo->GetBuffer();
    const VkBuffer instanceSSBOBuf = m_previewInstanceBuffer->GetBuffer();

    VkDescriptorSet shadowDesc = VK_NULL_HANDLE;
    VkDescriptorSet globalsDesc = VK_NULL_HANDLE;
    const bool requiresPerViewSet = std::any_of(bindings.begin(), bindings.end(), [](const auto &binding) {
        return binding.program && binding.program->HasDeclaredDescriptorSet(1);
    });
    const bool requiresGlobalsSet = std::any_of(bindings.begin(), bindings.end(), [](const auto &binding) {
        return binding.program && binding.program->HasDeclaredDescriptorSet(2);
    });

    if (requiresPerViewSet) {
        if (!m_fallbackShadowDescLease.IsValid()) {
            m_fallbackShadowDescLease = m_vkCore->AllocatePerViewDescriptorLease();
            m_fallbackShadowDescSet = m_fallbackShadowDescLease.set;
            if (m_fallbackShadowDescLease.IsValid()) {
                m_vkCore->UpdatePerViewLightingBuffer(m_fallbackShadowDescSet, lightingUBOBuf,
                                                      sizeof(ShaderLightingUBO));
                m_vkCore->UpdatePerViewCameraBuffer(m_fallbackShadowDescSet, sceneUBOBuf, sizeof(UniformBufferObject));
            }
        }
        shadowDesc = m_fallbackShadowDescSet;
        if (shadowDesc == VK_NULL_HANDLE)
            return nullptr;
    }

    if (requiresGlobalsSet) {
        globalsDesc = m_previewGlobalsSet;
    }

    // ── Record command buffer ────────────────────────────────────────
    auto readbackRecorder = rm.BeginGraphicsImageReadback(static_cast<uint32_t>(renderSize),
                                                          static_cast<uint32_t>(renderSize), m_colorFormat);
    VkCommandBuffer cmd = readbackRecorder.GetCommandBuffer();

    vkCmdUpdateBuffer(cmd, sceneUBOBuf, 0, sizeof(sceneUBO), &sceneUBO);
    vkCmdUpdateBuffer(cmd, lightingUBOBuf, 0, sizeof(lightingUBO), &lightingUBO);
    vkCmdUpdateBuffer(cmd, globalsUBOBuf, 0, sizeof(globalsUBO), &globalsUBO);
    vkCmdUpdateBuffer(cmd, instanceSSBOBuf, 0, sizeof(modelMat), &modelMat);
    const std::array<uint32_t, 4> emptySkinInstance{};
    const glm::mat4 identityBone(1.0f);
    vkCmdUpdateBuffer(cmd, m_previewSkinInstanceBuffer->GetBuffer(), 0, sizeof(emptySkinInstance),
                      emptySkinInstance.data());
    vkCmdUpdateBuffer(cmd, m_previewSkinPaletteBuffer->GetBuffer(), 0, sizeof(identityBone), &identityBone);
    GPUInstanceAuxData instanceAux{};
    instanceAux.previousModel = modelMat;
    instanceAux.layerMask = ~0u;
    vkCmdUpdateBuffer(cmd, m_previewInstanceAuxBuffer->GetBuffer(), 0, sizeof(instanceAux), &instanceAux);

    // Barrier: make UBO writes visible
    VkMemoryBarrier uboBarrier{};
    uboBarrier.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
    uboBarrier.srcAccessMask = VK_ACCESS_HOST_WRITE_BIT | VK_ACCESS_TRANSFER_WRITE_BIT;
    uboBarrier.dstAccessMask = VK_ACCESS_UNIFORM_READ_BIT | VK_ACCESS_SHADER_READ_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_HOST_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT,
                         VK_PIPELINE_STAGE_VERTEX_SHADER_BIT | VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 1, &uboBarrier,
                         0, nullptr, 0, nullptr);

    // ── Begin the RHI-selected graphics attachment path ─────────────
    VkClearValue clearValues[2];
    clearValues[0].color = {{0.0f, 0.0f, 0.0f, 0.0f}};
    clearValues[1].depthStencil = {1.0f, 0};

    vkrender::TransitionTrackedImageLayout(cmd, m_msaaColor.GetImage(), VK_IMAGE_ASPECT_COLOR_BIT, m_msaaColorLayout,
                                           VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL);
    if (m_depth.HasView()) {
        vkrender::TransitionTrackedImageLayout(cmd, m_depth.GetImage(), rhi::ToVkImageAspectMask(m_depthFormat),
                                               m_depthLayout, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL);
    }

    VkRenderingAttachmentInfo colorAttachment{};
    colorAttachment.sType = VK_STRUCTURE_TYPE_RENDERING_ATTACHMENT_INFO;
    colorAttachment.imageView = m_msaaColor.GetView();
    colorAttachment.imageLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    colorAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    colorAttachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    colorAttachment.clearValue = clearValues[0];

    VkRenderingAttachmentInfo depthAttachment{};
    if (m_depth.HasView()) {
        depthAttachment.sType = VK_STRUCTURE_TYPE_RENDERING_ATTACHMENT_INFO;
        depthAttachment.imageView = m_depth.GetView();
        depthAttachment.imageLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
        depthAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        depthAttachment.storeOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        depthAttachment.clearValue = clearValues[1];
    }

    VkRenderingInfo renderingInfo{};
    renderingInfo.sType = VK_STRUCTURE_TYPE_RENDERING_INFO;
    renderingInfo.renderArea.extent = {static_cast<uint32_t>(renderSize), static_cast<uint32_t>(renderSize)};
    renderingInfo.layerCount = 1;
    renderingInfo.colorAttachmentCount = 1;
    renderingInfo.pColorAttachments = &colorAttachment;
    renderingInfo.pDepthAttachment = m_depth.HasView() ? &depthAttachment : nullptr;
    m_dynamicRenderingCommands.begin(cmd, &renderingInfo);

    VkViewport viewport{};
    viewport.width = static_cast<float>(renderSize);
    viewport.height = static_cast<float>(renderSize);
    viewport.minDepth = 0.0f;
    viewport.maxDepth = 1.0f;
    vkCmdSetViewport(cmd, 0, 1, &viewport);

    VkRect2D scissor{};
    scissor.extent = {static_cast<uint32_t>(renderSize), static_cast<uint32_t>(renderSize)};
    vkCmdSetScissor(cmd, 0, 1, &scissor);

    // Bind mesh geometry
    VkBuffer vboBuf = vbo->GetBuffer();
    VkDeviceSize offsets[] = {0};
    vkCmdBindVertexBuffers(cmd, 0, 1, &vboBuf, offsets);
    vkCmdBindIndexBuffer(cmd, ibo->GetBuffer(), 0, VK_INDEX_TYPE_UINT32);

    // Push constants
    struct PushConstants
    {
        glm::mat4 model;
        glm::mat4 normalMat;
    };
    PushConstants pushData{};
    pushData.model = modelMat;
    pushData.normalMat = glm::transpose(glm::inverse(modelMat));

    // ── Draw each submesh ────────────────────────────────────────────
    bool anyDrawn = false;
    for (auto &b : bindings) {
        if (!m_vkCore->GetMaterialPipelineManager().IsDescriptorSetLive(b.materialDescSet))
            continue;

        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, b.pipeline);

        vkdebug::CmdBindDescriptorSetsTracked("GPUMeshPreview.Set0", cmd, VK_PIPELINE_BIND_POINT_GRAPHICS,
                                              b.pipelineLayout, 0, 1, &b.materialDescSet, 0, nullptr);

        if (b.program->HasDeclaredDescriptorSet(1) && shadowDesc != VK_NULL_HANDLE) {
            vkdebug::CmdBindDescriptorSetsTracked("GPUMeshPreview.Set1", cmd, VK_PIPELINE_BIND_POINT_GRAPHICS,
                                                  b.pipelineLayout, 1, 1, &shadowDesc, 0, nullptr);
        }

        if (b.program->HasDeclaredDescriptorSet(2) && globalsDesc != VK_NULL_HANDLE) {
            vkdebug::CmdBindDescriptorSetsTracked("GPUMeshPreview.Set2", cmd, VK_PIPELINE_BIND_POINT_GRAPHICS,
                                                  b.pipelineLayout, 2, 1, &globalsDesc, 0, nullptr);
        }

        if (b.program->UsesBindlessTextureABI()) {
            auto &rhiDevice = m_vkCore->GetDeviceContext().GetRhiDevice();
            const auto bindlessBinding = rhiDevice.GetBindlessTextureTableBinding();
            const VkDescriptorSet bindlessSet = rhiDevice.Resolve(bindlessBinding.group);
            if (bindlessSet == VK_NULL_HANDLE) {
                static int missingBindlessSetErrorCount = 0;
                if (missingBindlessSetErrorCount++ < 8)
                    INXLOG_ERROR("Mesh preview pass skipped because the device-global texture table is unavailable");
                continue;
            } else {
                vkdebug::CmdBindDescriptorSetsTracked("GPUMeshPreview.BindlessTextures", cmd,
                                                      VK_PIPELINE_BIND_POINT_GRAPHICS, b.pipelineLayout,
                                                      ShaderProgram::BindlessTextureSet, 1, &bindlessSet, 0, nullptr);
                if (const auto *indices =
                        m_vkCore->GetMaterialPipelineManager().GetDescriptorManager().GetBindlessTextureIndices(
                            b.ownedMaterial->GetMaterialKey())) {
                    rhiDevice.MarkBindlessTexturesUsed(indices->empty() ? nullptr : indices->data(), indices->size());
                }
            }
        }

        vkCmdPushConstants(cmd, b.pipelineLayout, VK_SHADER_STAGE_VERTEX_BIT, 0, sizeof(PushConstants), &pushData);

        vkCmdDrawIndexed(cmd, b.submesh->indexCount, 1, b.submesh->indexStart, 0, 0);
        anyDrawn = true;
    }

    m_dynamicRenderingCommands.end(cmd);
    m_msaaColorLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    if (m_depth.HasView())
        m_depthLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;

    // ── MSAA resolve + readback ──────────────────────────────────────
    if (m_sampleCount != VK_SAMPLE_COUNT_1_BIT) {
        vkrender::TransitionTrackedImageLayout(cmd, m_msaaColor.GetImage(), VK_IMAGE_ASPECT_COLOR_BIT,
                                               m_msaaColorLayout, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);

        vkrender::TransitionTrackedImageLayout(cmd, m_resolveColor.GetImage(), VK_IMAGE_ASPECT_COLOR_BIT,
                                               m_resolveColorLayout, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);

        VkImageResolve resolveRegion{};
        resolveRegion.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
        resolveRegion.dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
        resolveRegion.extent = {static_cast<uint32_t>(renderSize), static_cast<uint32_t>(renderSize), 1};
        vkCmdResolveImage(cmd, m_msaaColor.GetImage(), VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, m_resolveColor.GetImage(),
                          VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &resolveRegion);

        vkrender::TransitionTrackedImageLayout(cmd, m_resolveColor.GetImage(), VK_IMAGE_ASPECT_COLOR_BIT,
                                               m_resolveColorLayout, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
    } else {
        vkrender::TransitionTrackedImageLayout(cmd, m_msaaColor.GetImage(), VK_IMAGE_ASPECT_COLOR_BIT,
                                               m_msaaColorLayout, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
    }

    VkImage srcImage = (m_sampleCount != VK_SAMPLE_COUNT_1_BIT) ? m_resolveColor.GetImage() : m_msaaColor.GetImage();

    VkBufferImageCopy copyRegion{};
    copyRegion.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    copyRegion.imageExtent = {static_cast<uint32_t>(renderSize), static_cast<uint32_t>(renderSize), 1};
    vkCmdCopyImageToBuffer(cmd, srcImage, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, readbackRecorder.GetStagingBuffer(), 1,
                           &copyRegion);

    auto vertexLease = std::shared_ptr<vk::VkBufferHandle>(std::move(vbo));
    auto indexLease = std::shared_ptr<vk::VkBufferHandle>(std::move(ibo));
    std::vector<std::shared_ptr<InxMaterial>> materialLeases;
    materialLeases.reserve(bindings.size());
    for (const auto &binding : bindings)
        materialLeases.push_back(binding.ownedMaterial);
    m_activeReadback =
        readbackRecorder.Submit([vertexLease = std::move(vertexLease), indexLease = std::move(indexLease),
                                 materialLeases = std::move(materialLeases)]() mutable {
            materialLeases.clear();
            indexLease.reset();
            vertexLease.reset();
        });
    // A descriptor can still be retired between binding preparation and
    // command recording. Finish the submission so command-buffer and resource
    // ownership remain valid, but let the caller retry instead of publishing
    // the transparent clear as a successful thumbnail.
    return anyDrawn ? m_activeReadback : nullptr;
}

bool GPUMeshPreview::TryCompleteRenderToPixels(const std::shared_ptr<vk::ImageReadbackTicket> &ticket, int outputSize,
                                               std::vector<unsigned char> &outPixels)
{
    if (!ticket || !ticket->IsDone() || ticket->GetStatus() != vk::ImageReadbackStatus::Completed || outputSize <= 0)
        return false;

    const int renderSize = static_cast<int>(ticket->GetWidth());
    if (renderSize <= 0 || ticket->GetHeight() != ticket->GetWidth() || ticket->GetChannelCount() != 4)
        return false;

    // ── Readback pixels ──────────────────────────────────────────────
    const int pixelCount = renderSize * renderSize;
    std::vector<unsigned char> renderPixels(static_cast<size_t>(pixelCount) * 4, 0);

    const auto &raw = ticket->GetData();

    if (ticket->GetElementType() == "float16") {
        const uint16_t *src = reinterpret_cast<const uint16_t *>(raw.data());
        auto halfToFloat = [](uint16_t h) -> float {
            uint32_t sign = (h >> 15) & 0x1;
            uint32_t exponent = (h >> 10) & 0x1F;
            uint32_t mantissa = h & 0x3FF;
            if (exponent == 0) {
                if (mantissa == 0)
                    return sign ? -0.0f : 0.0f;
                float val = (mantissa / 1024.0f) * std::pow(2.0f, -14.0f);
                return sign ? -val : val;
            }
            if (exponent == 31)
                return mantissa ? 0.0f : (sign ? -1e30f : 1e30f);
            float val = std::pow(2.0f, static_cast<float>(exponent) - 15.0f) * (1.0f + mantissa / 1024.0f);
            return sign ? -val : val;
        };

        auto linearToSrgb = [](float c) -> float {
            if (c <= 0.0031308f)
                return c * 12.92f;
            return 1.055f * std::pow(c, 1.0f / 2.4f) - 0.055f;
        };

        for (int i = 0; i < pixelCount; ++i) {
            float r = halfToFloat(src[i * 4 + 0]);
            float g = halfToFloat(src[i * 4 + 1]);
            float b = halfToFloat(src[i * 4 + 2]);
            float a = std::clamp(halfToFloat(src[i * 4 + 3]), 0.0f, 1.0f);

            // Reinhard tonemap
            r = r / (1.0f + r);
            g = g / (1.0f + g);
            b = b / (1.0f + b);

            r = linearToSrgb(r);
            g = linearToSrgb(g);
            b = linearToSrgb(b);

            renderPixels[i * 4 + 0] = static_cast<unsigned char>(std::clamp(r, 0.0f, 1.0f) * 255.0f + 0.5f);
            renderPixels[i * 4 + 1] = static_cast<unsigned char>(std::clamp(g, 0.0f, 1.0f) * 255.0f + 0.5f);
            renderPixels[i * 4 + 2] = static_cast<unsigned char>(std::clamp(b, 0.0f, 1.0f) * 255.0f + 0.5f);
            renderPixels[i * 4 + 3] = static_cast<unsigned char>(a * 255.0f + 0.5f);
        }
    } else {
        if (raw.size() != renderPixels.size())
            return false;
        std::memcpy(renderPixels.data(), raw.data(), renderPixels.size());
    }

    DownsampleRGBABox(renderPixels, renderSize, outputSize, outPixels);
    if (m_activeReadback == ticket)
        m_activeReadback.reset();
    return true;
}

// ============================================================================
// Resource management (mirrors GPUMaterialPreview)
// ============================================================================

bool GPUMeshPreview::EnsureResources(int size)
{
    auto &mpm = m_vkCore->GetMaterialPipelineManager();
    VkFormat colorFormat = mpm.GetColorFormat();
    VkFormat depthFormat = mpm.GetDepthFormat();
    VkSampleCountFlagBits sampleCount = mpm.GetSampleCount();
    const auto dynamicCommands = rhi::ResolveDynamicRenderingCommands(m_vkCore->GetDevice());
    if (!m_vkCore->GetDeviceContext().GetRhiDevice().GetCapabilityState().dynamicRendering.enabled ||
        !dynamicCommands.IsValid()) {
        INXLOG_ERROR("GPUMeshPreview: dynamic rendering is required for mesh previews");
        return false;
    }

    const bool renderConfigChanged =
        (m_currentSize != 0) &&
        (colorFormat != m_colorFormat || depthFormat != m_depthFormat || sampleCount != m_sampleCount);

    if (renderConfigChanged) {
        DestroyImGuiDisplayDescriptor();
        DestroyAttachments();
    }

    m_colorFormat = colorFormat;
    m_depthFormat = depthFormat;
    m_sampleCount = sampleCount;
    m_dynamicRenderingCommands = dynamicCommands;

    if (m_currentSize != size) {
        DestroyImGuiDisplayDescriptor();
        DestroyAttachments();
        CreateAttachments(size);
        m_currentSize = size;
        if (m_msaaColor.IsValid())
            PublishRenderView();
    }

    return m_msaaColor.IsValid() && EnsureViewResources();
}

bool GPUMeshPreview::EnsureViewResources()
{
    if (m_previewGlobalsSet != VK_NULL_HANDLE)
        return true;

    auto &rm = m_vkCore->GetResourceManager();
    m_previewSceneUbo = rm.CreateUniformBuffer(sizeof(UniformBufferObject));
    m_previewLightingUbo = rm.CreateUniformBuffer(sizeof(ShaderLightingUBO));
    m_previewGlobalsUbo = rm.CreateUniformBuffer(sizeof(EngineGlobalsUBO));
    m_previewInstanceBuffer = rm.CreateStorageBuffer(sizeof(glm::mat4), false);
    m_previewSkinInstanceBuffer = rm.CreateStorageBuffer(64, false);
    m_previewSkinPaletteBuffer = rm.CreateStorageBuffer(sizeof(glm::mat4), false);
    m_previewInstanceAuxBuffer = rm.CreateStorageBuffer(sizeof(GPUInstanceAuxData), false);
    if (!m_previewSceneUbo || !m_previewLightingUbo || !m_previewGlobalsUbo || !m_previewInstanceBuffer ||
        !m_previewSkinInstanceBuffer || !m_previewSkinPaletteBuffer || !m_previewInstanceAuxBuffer)
        throw std::runtime_error("Failed to allocate isolated mesh-preview view buffers");

    const VkDevice device = m_vkCore->GetDevice();
    const VkDescriptorSetLayout layout = m_vkCore->GetGlobalsDescSetLayout();
    if (layout == VK_NULL_HANDLE)
        throw std::logic_error("Mesh preview requires the renderer globals descriptor layout");
    m_previewGlobalsLease = m_vkCore->GetDeviceContext().GetRhiDevice().GetDescriptorManager().Allocate(
        layout, vk::DescriptorArena::ViewPersistent);
    if (!m_previewGlobalsLease.IsValid())
        throw std::runtime_error("Failed to allocate isolated mesh-preview globals descriptor set");
    m_previewGlobalsSet = m_previewGlobalsLease.set;

    VkDescriptorBufferInfo buffers[5]{};
    buffers[0] = {m_previewGlobalsUbo->GetBuffer(), 0, sizeof(EngineGlobalsUBO)};
    buffers[1] = {m_previewInstanceBuffer->GetBuffer(), 0, VK_WHOLE_SIZE};
    buffers[2] = {m_previewSkinInstanceBuffer->GetBuffer(), 0, VK_WHOLE_SIZE};
    buffers[3] = {m_previewSkinPaletteBuffer->GetBuffer(), 0, VK_WHOLE_SIZE};
    buffers[4] = {m_previewInstanceAuxBuffer->GetBuffer(), 0, VK_WHOLE_SIZE};
    VkWriteDescriptorSet writes[5]{};
    for (uint32_t binding = 0; binding < 5; ++binding) {
        writes[binding].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        writes[binding].dstSet = m_previewGlobalsSet;
        writes[binding].dstBinding = binding;
        writes[binding].descriptorCount = 1;
        writes[binding].descriptorType =
            binding == 0 ? VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER : VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        writes[binding].pBufferInfo = &buffers[binding];
    }
    vkUpdateDescriptorSets(device, 5, writes, 0, nullptr);
    return true;
}

void GPUMeshPreview::DestroyViewResources()
{
    m_activeSubmission.reset();
    m_activeReadback.reset();
    if (m_vkCore) {
        auto &descriptorManager = m_vkCore->GetDeviceContext().GetRhiDevice().GetDescriptorManager();
        descriptorManager.Retire(m_fallbackShadowDescLease);
        descriptorManager.Retire(m_previewGlobalsLease);
    }
    m_fallbackShadowDescLease = {};
    m_fallbackShadowDescSet = VK_NULL_HANDLE;
    m_previewGlobalsLease = {};
    m_previewGlobalsSet = VK_NULL_HANDLE;
    m_previewSkinPaletteBuffer.reset();
    m_previewSkinInstanceBuffer.reset();
    m_previewInstanceAuxBuffer.reset();
    m_previewInstanceBuffer.reset();
    m_previewGlobalsUbo.reset();
    m_previewLightingUbo.reset();
    m_previewSceneUbo.reset();
}

void GPUMeshPreview::CreateAttachments(int size)
{
    VkDevice device = m_vkCore->GetDevice();
    VmaAllocator allocator = m_vkCore->GetDeviceContext().GetVmaAllocator();
    uint32_t w = static_cast<uint32_t>(size);
    uint32_t h = static_cast<uint32_t>(size);

    m_msaaColor.Create(allocator, device, w, h, m_colorFormat, VK_IMAGE_TILING_OPTIMAL,
                       VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT |
                           (m_sampleCount == VK_SAMPLE_COUNT_1_BIT ? VK_IMAGE_USAGE_SAMPLED_BIT : 0),
                       VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, m_sampleCount);
    m_msaaColor.CreateView(m_colorFormat, VK_IMAGE_ASPECT_COLOR_BIT);

    if (m_sampleCount != VK_SAMPLE_COUNT_1_BIT) {
        m_resolveColor.Create(allocator, device, w, h, m_colorFormat, VK_IMAGE_TILING_OPTIMAL,
                              VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT |
                                  VK_IMAGE_USAGE_SAMPLED_BIT,
                              VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, VK_SAMPLE_COUNT_1_BIT);
        m_resolveColor.CreateView(m_colorFormat, VK_IMAGE_ASPECT_COLOR_BIT);
    }

    if (m_depthFormat != VK_FORMAT_UNDEFINED) {
        m_depth.Create(allocator, device, w, h, m_depthFormat, VK_IMAGE_TILING_OPTIMAL,
                       VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, m_sampleCount);
        m_depth.CreateView(m_depthFormat, rhi::ToVkImageAspectMask(m_depthFormat));
    }
}

void GPUMeshPreview::DestroyAttachments()
{
    UnpublishRenderView();
    m_msaaColor.Destroy();
    m_resolveColor.Destroy();
    m_depth.Destroy();
    m_msaaColorLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    m_resolveColorLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    m_depthLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    m_currentSize = 0;
    m_displayImageShaderReady = false;
}

void GPUMeshPreview::PublishRenderView()
{
    if (!m_vkCore || !m_msaaColor.IsValid())
        return;
    auto &device = m_vkCore->GetDeviceContext().GetRhiDevice();
    if (m_renderView.color.IsValid())
        device.Release(m_renderView.color);
    if (m_renderView.depth.IsValid())
        device.Release(m_renderView.depth);

    const VkImageView displayView =
        m_sampleCount != VK_SAMPLE_COUNT_1_BIT ? m_resolveColor.GetView() : m_msaaColor.GetView();
    m_renderView.device = m_vkCore->GetDeviceContext().GetDeviceId();
    m_renderView.width = static_cast<uint32_t>(m_currentSize);
    m_renderView.height = static_cast<uint32_t>(m_currentSize);
    m_renderView.colorFormat = rhi::FromVkFormat(m_colorFormat);
    m_renderView.depthFormat = rhi::FromVkFormat(m_depthFormat);
    m_renderView.samples = rhi::FromVkSampleCount(m_sampleCount);
    m_renderView.color = device.RegisterTextureView(displayView);
    m_renderView.depth = m_depth.HasView() ? device.RegisterTextureView(m_depth.GetView()) : rhi::TextureViewHandle{};
    ++m_renderView.revision;
}

void GPUMeshPreview::UnpublishRenderView()
{
    if (!m_vkCore)
        return;
    auto &device = m_vkCore->GetDeviceContext().GetRhiDevice();
    if (m_renderView.color.IsValid())
        device.Release(m_renderView.color);
    if (m_renderView.depth.IsValid())
        device.Release(m_renderView.depth);
    const bool wasPublished = m_renderView.color.IsValid() || m_renderView.depth.IsValid() || m_renderView.width != 0;
    m_renderView.color = {};
    m_renderView.depth = {};
    m_renderView.width = 0;
    m_renderView.height = 0;
    if (wasPublished)
        ++m_renderView.revision;
}

void GPUMeshPreview::DestroyImGuiDisplayDescriptor()
{
    if (!m_vkCore)
        return;
    VkDevice device = m_vkCore->GetDevice();
    // In-flight GPU frames (and UI code holding the previously returned
    // ImTextureID) may still reference this descriptor for a few frames.
    // Freeing it immediately makes the ImGui backend bind a destroyed
    // VkDescriptorSet — validation errors and intermittent crashes — so both
    // the descriptor and its sampler retire through the submission retirement queue.
    if (m_displayDescriptorSet != VK_NULL_HANDLE || m_displaySampler != VK_NULL_HANDLE) {
        VkDescriptorSet retiredSet = m_displayDescriptorSet;
        VkSampler retiredSampler = m_displaySampler;
        m_vkCore->RetireGpuResource([device, retiredSet, retiredSampler]() {
            if (retiredSet != VK_NULL_HANDLE && ImGui::GetCurrentContext() != nullptr &&
                ImGui::GetIO().BackendRendererUserData != nullptr)
                ImGui_ImplVulkan_RemoveTexture(retiredSet);
            if (retiredSampler != VK_NULL_HANDLE)
                vkDestroySampler(device, retiredSampler, nullptr);
        });
        m_displayDescriptorSet = VK_NULL_HANDLE;
        m_displaySampler = VK_NULL_HANDLE;
    }
    m_displayImageShaderReady = false;
}

void GPUMeshPreview::EnsureImGuiDisplayDescriptor()
{
    if (m_displayDescriptorSet != VK_NULL_HANDLE)
        return;

    VkDevice device = m_vkCore->GetDevice();
    VkImageView displayView = VK_NULL_HANDLE;
    if (m_sampleCount != VK_SAMPLE_COUNT_1_BIT)
        displayView = m_resolveColor.GetView();
    else
        displayView = m_msaaColor.GetView();
    if (displayView == VK_NULL_HANDLE)
        return;

    auto samplerInfo = vkrender::MakeLinearClampSamplerInfo();
    if (vkCreateSampler(device, &samplerInfo, nullptr, &m_displaySampler) != VK_SUCCESS)
        return;

    m_displayDescriptorSet =
        ImGui_ImplVulkan_AddTexture(m_displaySampler, displayView, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);
}

uint64_t GPUMeshPreview::RenderToImGuiTextureCamera(const InxMesh &mesh,
                                                    const std::vector<std::shared_ptr<InxMaterial>> &materials,
                                                    int size, const glm::mat4 &view, const glm::mat4 &proj,
                                                    const glm::vec3 &cameraPos, bool cloneMaterials)
{
    if (!m_vkCore || size <= 0)
        return 0;
    if (m_activeReadback && !m_activeReadback->IsDone()) {
        // The caller uses zero as backpressure and keeps only its latest
        // request pending. Returning the stale display texture here falsely
        // reports that a new camera/transform state was rendered.
        return 0;
    }
    m_activeReadback.reset();
    if (m_activeSubmission && !m_activeSubmission->IsComplete()) {
        return 0;
    }
    m_activeSubmission.reset();

    const auto &vertices = mesh.GetVertices();
    const auto &indices = mesh.GetIndices();
    if (vertices.empty() || indices.empty())
        return 0;

    const int renderSize = size;
    if (!EnsureResources(renderSize))
        return 0;

    auto &rm = m_vkCore->GetResourceManager();
    auto vbo = rm.CreateVertexBuffer(vertices.data(), vertices.size() * sizeof(Vertex));
    auto ibo = rm.CreateIndexBuffer(indices.data(), indices.size() * sizeof(uint32_t));
    if (!vbo || !ibo)
        return 0;

    auto defaultMat = AssetRegistry::Instance().GetBuiltinMaterial("DefaultLit");
    struct SubmeshBinding
    {
        const SubMesh *submesh = nullptr;
        std::shared_ptr<InxMaterial> ownedMaterial;
        VkPipeline pipeline = VK_NULL_HANDLE;
        VkPipelineLayout pipelineLayout = VK_NULL_HANDLE;
        VkDescriptorSet materialDescSet = VK_NULL_HANDLE;
        const ShaderProgram *program = nullptr;
    };
    std::vector<SubmeshBinding> bindings;
    bindings.reserve(mesh.GetSubMeshCount());
    for (uint32_t si = 0; si < mesh.GetSubMeshCount(); ++si) {
        const SubMesh &sm = mesh.GetSubMesh(si);
        if (sm.indexCount == 0)
            continue;
        std::shared_ptr<InxMaterial> srcMat;
        if (sm.materialSlot < materials.size() && materials[sm.materialSlot])
            srcMat = materials[sm.materialSlot];
        else
            srcMat = defaultMat;
        if (!srcMat)
            continue;
        std::shared_ptr<InxMaterial> previewMat = cloneMaterials ? srcMat->Clone() : srcMat;
        if (!previewMat)
            continue;
        if (cloneMaterials) {
            previewMat->ClearAllPassPipelines();
        }
        bool pipelineReady = m_vkCore->RefreshPreviewMaterialPipeline(previewMat, previewMat->GetVertShaderName(),
                                                                      previewMat->GetFragShaderName(), false);
        if (!pipelineReady && srcMat != defaultMat && defaultMat) {
            previewMat = cloneMaterials ? defaultMat->Clone() : defaultMat;
            if (previewMat && cloneMaterials)
                previewMat->ClearAllPassPipelines();
            pipelineReady =
                previewMat && m_vkCore->RefreshPreviewMaterialPipeline(previewMat, previewMat->GetVertShaderName(),
                                                                       previewMat->GetFragShaderName(), false);
        }
        if (!pipelineReady || !previewMat)
            continue;
        MaterialPassRenderData *rd = m_vkCore->GetOrCreatePreviewMaterialPass(previewMat);
        if (!rd || !rd->isValid || rd->descriptorSet == VK_NULL_HANDLE)
            continue;
        previewMat->SetPassPipeline(ShaderCompileTarget::Forward, rd->pipeline);
        previewMat->SetPassPipelineLayout(ShaderCompileTarget::Forward, rd->pipelineLayout);
        previewMat->SetPassDescriptorSet(ShaderCompileTarget::Forward, rd->descriptorSet);
        previewMat->SetPassShaderProgram(ShaderCompileTarget::Forward, rd->shaderProgram);
        SubmeshBinding b;
        b.submesh = &sm;
        b.ownedMaterial = previewMat;
        b.pipeline = rd->pipeline;
        b.pipelineLayout = rd->pipelineLayout;
        b.materialDescSet = rd->descriptorSet;
        b.program = rd->shaderProgram.get();
        bindings.push_back(std::move(b));
    }
    if (bindings.empty())
        return 0;

    for (auto &b : bindings)
        m_vkCore->UpdateMaterialUBO(*b.ownedMaterial);

    bindings.erase(std::remove_if(bindings.begin(), bindings.end(),
                                  [&](SubmeshBinding &binding) {
                                      MaterialPassRenderData *rd =
                                          m_vkCore->GetOrCreatePreviewMaterialPass(binding.ownedMaterial);
                                      if (!rd || !rd->isValid || rd->pipeline == VK_NULL_HANDLE ||
                                          rd->pipelineLayout == VK_NULL_HANDLE || rd->descriptorSet == VK_NULL_HANDLE ||
                                          !rd->shaderProgram)
                                          return true;
                                      binding.pipeline = rd->pipeline;
                                      binding.pipelineLayout = rd->pipelineLayout;
                                      binding.materialDescSet = rd->descriptorSet;
                                      binding.program = rd->shaderProgram.get();
                                      return false;
                                  }),
                   bindings.end());
    if (bindings.empty())
        return 0;

    const glm::mat4 modelMat = glm::mat4(1.0f);
    UniformBufferObject sceneUBO{};
    sceneUBO.model = modelMat;
    sceneUBO.view = view;
    sceneUBO.proj = proj;
    sceneUBO.previousViewProj = proj * view;
    sceneUBO.inverseViewProj = glm::inverse(proj * view);
    sceneUBO.projectionParams = glm::vec4(0.1f, 100.0f, 0.01f, 0.001f);
    sceneUBO.zBufferParams = glm::vec4(-999.0f, 1000.0f, -9.99f, 10.0f);

    ShaderLightingUBO lightingUBO{};
    memset(&lightingUBO, 0, sizeof(lightingUBO));
    lightingUBO.lightCounts = glm::ivec4(2, 0, 0, 0);
    lightingUBO.ambientColor = glm::vec4(0.08f, 0.08f, 0.09f, 1.0f);
    lightingUBO.ambientSkyColor = glm::vec4(0.20f, 0.22f, 0.26f, 0.55f);
    lightingUBO.ambientEquatorColor = glm::vec4(0.10f, 0.11f, 0.13f, 1.0f);
    lightingUBO.ambientGroundColor = glm::vec4(0.05f, 0.04f, 0.035f, 0.30f);
    lightingUBO.cameraPos = glm::vec4(cameraPos, 1.0f);
    lightingUBO.directionalLights[0].direction = glm::vec4(glm::normalize(glm::vec3(-0.7f, -1.0f, -0.5f)), 0.0f);
    lightingUBO.directionalLights[0].color = glm::vec4(1.8f, 1.71f, 1.62f, 1.8f);
    lightingUBO.directionalLights[0].metadata =
        glm::uvec4(~0u, static_cast<uint32_t>(LightInfluenceDomain::Geometry), 0u, 0u);
    lightingUBO.directionalLights[1].direction = glm::vec4(glm::normalize(glm::vec3(0.5f, 0.3f, -0.7f)), 0.0f);
    lightingUBO.directionalLights[1].color = glm::vec4(0.36f, 0.42f, 0.51f, 0.6f);
    lightingUBO.directionalLights[1].metadata =
        glm::uvec4(~0u, static_cast<uint32_t>(LightInfluenceDomain::Geometry), 0u, 0u);

    EngineGlobalsUBO globalsUBO{};
    memset(&globalsUBO, 0, sizeof(globalsUBO));
    globalsUBO.screenParams =
        glm::vec4(static_cast<float>(renderSize), static_cast<float>(renderSize), 1.0f / renderSize, 1.0f / renderSize);
    globalsUBO.worldSpaceCameraPos = glm::vec4(cameraPos, 1.0f);

    const VkBuffer sceneUBOBuf = m_previewSceneUbo->GetBuffer();
    const VkBuffer lightingUBOBuf = m_previewLightingUbo->GetBuffer();
    const VkBuffer globalsUBOBuf = m_previewGlobalsUbo->GetBuffer();
    const VkBuffer instanceSSBOBuf = m_previewInstanceBuffer->GetBuffer();

    VkDescriptorSet shadowDesc = VK_NULL_HANDLE;
    VkDescriptorSet globalsDesc = VK_NULL_HANDLE;
    const bool requiresPerViewSet = std::any_of(bindings.begin(), bindings.end(), [](const auto &binding) {
        return binding.program && binding.program->HasDeclaredDescriptorSet(1);
    });
    const bool requiresGlobalsSet = std::any_of(bindings.begin(), bindings.end(), [](const auto &binding) {
        return binding.program && binding.program->HasDeclaredDescriptorSet(2);
    });
    if (requiresPerViewSet) {
        if (!m_fallbackShadowDescLease.IsValid()) {
            m_fallbackShadowDescLease = m_vkCore->AllocatePerViewDescriptorLease();
            m_fallbackShadowDescSet = m_fallbackShadowDescLease.set;
            if (m_fallbackShadowDescLease.IsValid()) {
                m_vkCore->UpdatePerViewLightingBuffer(m_fallbackShadowDescSet, lightingUBOBuf,
                                                      sizeof(ShaderLightingUBO));
                m_vkCore->UpdatePerViewCameraBuffer(m_fallbackShadowDescSet, sceneUBOBuf, sizeof(UniformBufferObject));
            }
        }
        shadowDesc = m_fallbackShadowDescSet;
        if (shadowDesc == VK_NULL_HANDLE)
            return 0;
    }
    if (requiresGlobalsSet) {
        globalsDesc = m_previewGlobalsSet;
    }

    VkCommandBuffer cmd = m_vkCore->BeginSingleTimeCommands();
    if (cmd == VK_NULL_HANDLE)
        return 0;

    VkImage displayImage =
        (m_sampleCount != VK_SAMPLE_COUNT_1_BIT) ? m_resolveColor.GetImage() : m_msaaColor.GetImage();
    if (m_sampleCount != VK_SAMPLE_COUNT_1_BIT) {
        vkrender::TransitionTrackedImageLayout(cmd, displayImage, VK_IMAGE_ASPECT_COLOR_BIT, m_resolveColorLayout,
                                               VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
    }

    vkCmdUpdateBuffer(cmd, sceneUBOBuf, 0, sizeof(sceneUBO), &sceneUBO);
    vkCmdUpdateBuffer(cmd, lightingUBOBuf, 0, sizeof(lightingUBO), &lightingUBO);
    vkCmdUpdateBuffer(cmd, globalsUBOBuf, 0, sizeof(globalsUBO), &globalsUBO);
    vkCmdUpdateBuffer(cmd, instanceSSBOBuf, 0, sizeof(modelMat), &modelMat);
    const std::array<uint32_t, 4> emptySkinInstance{};
    const glm::mat4 identityBone(1.0f);
    vkCmdUpdateBuffer(cmd, m_previewSkinInstanceBuffer->GetBuffer(), 0, sizeof(emptySkinInstance),
                      emptySkinInstance.data());
    vkCmdUpdateBuffer(cmd, m_previewSkinPaletteBuffer->GetBuffer(), 0, sizeof(identityBone), &identityBone);
    GPUInstanceAuxData instanceAux{};
    instanceAux.previousModel = modelMat;
    instanceAux.layerMask = ~0u;
    vkCmdUpdateBuffer(cmd, m_previewInstanceAuxBuffer->GetBuffer(), 0, sizeof(instanceAux), &instanceAux);

    VkMemoryBarrier uboBarrier{};
    uboBarrier.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
    uboBarrier.srcAccessMask = VK_ACCESS_HOST_WRITE_BIT | VK_ACCESS_TRANSFER_WRITE_BIT;
    uboBarrier.dstAccessMask = VK_ACCESS_UNIFORM_READ_BIT | VK_ACCESS_SHADER_READ_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_HOST_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT,
                         VK_PIPELINE_STAGE_VERTEX_SHADER_BIT | VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 1, &uboBarrier,
                         0, nullptr, 0, nullptr);

    VkClearValue clearValues[2];
    clearValues[0].color = {{0.0f, 0.0f, 0.0f, 0.0f}};
    clearValues[1].depthStencil = {1.0f, 0};
    vkrender::TransitionTrackedImageLayout(cmd, m_msaaColor.GetImage(), VK_IMAGE_ASPECT_COLOR_BIT, m_msaaColorLayout,
                                           VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL);
    if (m_depth.HasView()) {
        vkrender::TransitionTrackedImageLayout(cmd, m_depth.GetImage(), rhi::ToVkImageAspectMask(m_depthFormat),
                                               m_depthLayout, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL);
    }

    VkRenderingAttachmentInfo colorAttachment{};
    colorAttachment.sType = VK_STRUCTURE_TYPE_RENDERING_ATTACHMENT_INFO;
    colorAttachment.imageView = m_msaaColor.GetView();
    colorAttachment.imageLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    colorAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    colorAttachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    colorAttachment.clearValue = clearValues[0];

    VkRenderingAttachmentInfo depthAttachment{};
    if (m_depth.HasView()) {
        depthAttachment.sType = VK_STRUCTURE_TYPE_RENDERING_ATTACHMENT_INFO;
        depthAttachment.imageView = m_depth.GetView();
        depthAttachment.imageLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
        depthAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        depthAttachment.storeOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        depthAttachment.clearValue = clearValues[1];
    }

    VkRenderingInfo renderingInfo{};
    renderingInfo.sType = VK_STRUCTURE_TYPE_RENDERING_INFO;
    renderingInfo.renderArea.extent = {static_cast<uint32_t>(renderSize), static_cast<uint32_t>(renderSize)};
    renderingInfo.layerCount = 1;
    renderingInfo.colorAttachmentCount = 1;
    renderingInfo.pColorAttachments = &colorAttachment;
    renderingInfo.pDepthAttachment = m_depth.HasView() ? &depthAttachment : nullptr;
    m_dynamicRenderingCommands.begin(cmd, &renderingInfo);

    VkViewport viewport{};
    viewport.width = static_cast<float>(renderSize);
    viewport.height = static_cast<float>(renderSize);
    viewport.minDepth = 0.0f;
    viewport.maxDepth = 1.0f;
    vkCmdSetViewport(cmd, 0, 1, &viewport);
    VkRect2D scissor{};
    scissor.extent = {static_cast<uint32_t>(renderSize), static_cast<uint32_t>(renderSize)};
    vkCmdSetScissor(cmd, 0, 1, &scissor);

    VkBuffer vboBuf = vbo->GetBuffer();
    VkDeviceSize offsets[] = {0};
    vkCmdBindVertexBuffers(cmd, 0, 1, &vboBuf, offsets);
    vkCmdBindIndexBuffer(cmd, ibo->GetBuffer(), 0, VK_INDEX_TYPE_UINT32);

    struct PushConstants
    {
        glm::mat4 model;
        glm::mat4 normalMat;
    };
    PushConstants pushData{};
    pushData.model = modelMat;
    pushData.normalMat = glm::transpose(glm::inverse(modelMat));

    for (auto &b : bindings) {
        if (!m_vkCore->GetMaterialPipelineManager().IsDescriptorSetLive(b.materialDescSet))
            continue;
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, b.pipeline);
        vkdebug::CmdBindDescriptorSetsTracked("GPUMeshPreview.Live.Set0", cmd, VK_PIPELINE_BIND_POINT_GRAPHICS,
                                              b.pipelineLayout, 0, 1, &b.materialDescSet, 0, nullptr);
        if (b.program->HasDeclaredDescriptorSet(1) && shadowDesc != VK_NULL_HANDLE)
            vkdebug::CmdBindDescriptorSetsTracked("GPUMeshPreview.Live.Set1", cmd, VK_PIPELINE_BIND_POINT_GRAPHICS,
                                                  b.pipelineLayout, 1, 1, &shadowDesc, 0, nullptr);
        if (b.program->HasDeclaredDescriptorSet(2) && globalsDesc != VK_NULL_HANDLE)
            vkdebug::CmdBindDescriptorSetsTracked("GPUMeshPreview.Live.Set2", cmd, VK_PIPELINE_BIND_POINT_GRAPHICS,
                                                  b.pipelineLayout, 2, 1, &globalsDesc, 0, nullptr);
        if (b.program->UsesBindlessTextureABI()) {
            auto &rhiDevice = m_vkCore->GetDeviceContext().GetRhiDevice();
            const auto bindlessBinding = rhiDevice.GetBindlessTextureTableBinding();
            const VkDescriptorSet bindlessSet = rhiDevice.Resolve(bindlessBinding.group);
            if (bindlessSet == VK_NULL_HANDLE) {
                static int missingLiveBindlessSetErrorCount = 0;
                if (missingLiveBindlessSetErrorCount++ < 8)
                    INXLOG_ERROR(
                        "Live mesh preview pass skipped because the device-global texture table is unavailable");
                continue;
            } else {
                vkdebug::CmdBindDescriptorSetsTracked("GPUMeshPreview.Live.BindlessTextures", cmd,
                                                      VK_PIPELINE_BIND_POINT_GRAPHICS, b.pipelineLayout,
                                                      ShaderProgram::BindlessTextureSet, 1, &bindlessSet, 0, nullptr);
                if (const auto *indices =
                        m_vkCore->GetMaterialPipelineManager().GetDescriptorManager().GetBindlessTextureIndices(
                            b.ownedMaterial->GetMaterialKey())) {
                    rhiDevice.MarkBindlessTexturesUsed(indices->empty() ? nullptr : indices->data(), indices->size());
                }
            }
        }
        vkCmdPushConstants(cmd, b.pipelineLayout, VK_SHADER_STAGE_VERTEX_BIT, 0, sizeof(PushConstants), &pushData);
        vkCmdDrawIndexed(cmd, b.submesh->indexCount, 1, b.submesh->indexStart, 0, 0);
    }
    m_dynamicRenderingCommands.end(cmd);
    m_msaaColorLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    if (m_depth.HasView())
        m_depthLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;

    if (m_sampleCount != VK_SAMPLE_COUNT_1_BIT) {
        vkrender::TransitionTrackedImageLayout(cmd, m_msaaColor.GetImage(), VK_IMAGE_ASPECT_COLOR_BIT,
                                               m_msaaColorLayout, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
        VkImageResolve resolveRegion{};
        resolveRegion.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
        resolveRegion.dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
        resolveRegion.extent = {static_cast<uint32_t>(renderSize), static_cast<uint32_t>(renderSize), 1};
        vkCmdResolveImage(cmd, m_msaaColor.GetImage(), VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, m_resolveColor.GetImage(),
                          VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &resolveRegion);
    }

    if (m_sampleCount != VK_SAMPLE_COUNT_1_BIT) {
        vkrender::TransitionTrackedImageLayout(cmd, displayImage, VK_IMAGE_ASPECT_COLOR_BIT, m_resolveColorLayout,
                                               VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);
    } else {
        vkrender::TransitionTrackedImageLayout(cmd, displayImage, VK_IMAGE_ASPECT_COLOR_BIT, m_msaaColorLayout,
                                               VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);
    }

    auto vertexLease = std::shared_ptr<vk::VkBufferHandle>(std::move(vbo));
    auto indexLease = std::shared_ptr<vk::VkBufferHandle>(std::move(ibo));
    std::vector<std::shared_ptr<InxMaterial>> materialLeases;
    materialLeases.reserve(bindings.size());
    for (const auto &binding : bindings)
        materialLeases.push_back(binding.ownedMaterial);
    m_activeSubmission = m_vkCore->EndSingleTimeCommandsAsync(
        cmd, [vertexLease = std::move(vertexLease), indexLease = std::move(indexLease),
              materialLeases = std::move(materialLeases)]() mutable {
            materialLeases.clear();
            indexLease.reset();
            vertexLease.reset();
        });
    m_displayImageShaderReady = true;
    EnsureImGuiDisplayDescriptor();
    return reinterpret_cast<uint64_t>(m_displayDescriptorSet);
}

} // namespace infernux
