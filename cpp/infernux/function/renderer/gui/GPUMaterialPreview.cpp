/**
 * @file GPUMaterialPreview.cpp
 * @brief GPU material preview — renders a sphere using the material's real
 *        Vulkan pipeline and reads back RGBA8 pixels for editor thumbnails.
 */

#include "GPUMaterialPreview.h"
#include "InxError.h"
#include <function/renderer/EngineGlobals.h>
#include <function/renderer/InxRenderStruct.h>
#include <function/renderer/InxVkCoreModular.h>
#include <function/renderer/MaterialPipelineManager.h>
#include <function/renderer/RenderInstanceHistory.h>
#include <function/renderer/shader/ShaderProgram.h>
#include <function/renderer/vk/DescriptorBindTrace.h>
#include <function/renderer/vk/VkRenderUtils.h>
#include <function/renderer/vk/VkResourceManager.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/scene/Light.h>
#include <function/scene/LightingData.h>
#include <function/scene/PrimitiveMeshes.h>

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
constexpr float kPreviewMatteR = 0.0f;
constexpr float kPreviewMatteG = 0.0f;
constexpr float kPreviewMatteB = 0.0f;
constexpr float kPreviewMatteA = 0.0f;
constexpr float kPreviewCameraDistance = 1.82f; // slightly closer so the material sphere fills a bit more
constexpr float kPreviewModelScale = 1.28f;
constexpr int kPreviewSupersampleFactor = 2;

float ComputePreviewCoverage(int pixelX, int pixelY, int renderSize)
{
    if (renderSize <= 0)
        return 0.0f;

    constexpr float kBaseSphereRadius = 0.5f;
    constexpr float kVerticalFovDegrees = 45.0f;

    const float sphereRadius = kBaseSphereRadius * kPreviewModelScale;
    const float cameraDistance = std::max(kPreviewCameraDistance, sphereRadius + 1e-4f);
    const float cotHalfFov = 1.0f / std::tan(glm::radians(kVerticalFovDegrees) * 0.5f);
    const float tangentDepth =
        std::sqrt(std::max(cameraDistance * cameraDistance - sphereRadius * sphereRadius, 1e-6f));
    const float ndcRadius = cotHalfFov * (sphereRadius / tangentDepth);
    const float radius01 = 0.5f * ndcRadius;
    const float feather = 1.5f / static_cast<float>(renderSize);

    const float fx = (static_cast<float>(pixelX) + 0.5f) / static_cast<float>(renderSize) - 0.5f;
    const float fy = (static_cast<float>(pixelY) + 0.5f) / static_cast<float>(renderSize) - 0.5f;
    const float dist = std::sqrt(fx * fx + fy * fy);
    return std::clamp((radius01 + feather - dist) / (2.0f * feather), 0.0f, 1.0f);
}

glm::vec3 RemovePreviewMatte(const glm::vec3 &rgb, float alpha)
{
    if (alpha <= 0.0f || alpha >= 0.999f)
        return rgb;

    const glm::vec3 matte(kPreviewMatteR, kPreviewMatteG, kPreviewMatteB);
    return glm::max((rgb - matte * (1.0f - alpha)) / alpha, glm::vec3(0.0f));
}

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

    dstPixels.assign(static_cast<size_t>(dstSize) * dstSize * 4, 0);

    const float scale = static_cast<float>(srcSize) / static_cast<float>(dstSize);
    for (int y = 0; y < dstSize; ++y) {
        const int y0 = std::max(0, static_cast<int>(std::floor(y * scale)));
        const int y1 = std::min(srcSize, static_cast<int>(std::ceil((y + 1) * scale)));
        for (int x = 0; x < dstSize; ++x) {
            const int x0 = std::max(0, static_cast<int>(std::floor(x * scale)));
            const int x1 = std::min(srcSize, static_cast<int>(std::ceil((x + 1) * scale)));

            int sampleCount = 0;
            int accum[4] = {0, 0, 0, 0};
            for (int sy = y0; sy < y1; ++sy) {
                for (int sx = x0; sx < x1; ++sx) {
                    const size_t srcIndex = (static_cast<size_t>(sy) * srcSize + sx) * 4;
                    accum[0] += srcPixels[srcIndex + 0];
                    accum[1] += srcPixels[srcIndex + 1];
                    accum[2] += srcPixels[srcIndex + 2];
                    accum[3] += srcPixels[srcIndex + 3];
                    ++sampleCount;
                }
            }

            if (sampleCount <= 0)
                continue;

            const size_t dstIndex = (static_cast<size_t>(y) * dstSize + x) * 4;
            dstPixels[dstIndex + 0] = static_cast<unsigned char>(accum[0] / sampleCount);
            dstPixels[dstIndex + 1] = static_cast<unsigned char>(accum[1] / sampleCount);
            dstPixels[dstIndex + 2] = static_cast<unsigned char>(accum[2] / sampleCount);
            dstPixels[dstIndex + 3] = static_cast<unsigned char>(accum[3] / sampleCount);
        }
    }
}
} // namespace

// ============================================================================
// Construction / Destruction
// ============================================================================

GPUMaterialPreview::GPUMaterialPreview(InxVkCoreModular *vkCore) : m_vkCore(vkCore)
{
}

GPUMaterialPreview::~GPUMaterialPreview()
{
    VkDevice device = m_vkCore ? m_vkCore->GetDevice() : VK_NULL_HANDLE;
    if (device == VK_NULL_HANDLE)
        return;

    DestroyFramebuffer();

    if (m_renderPass != VK_NULL_HANDLE)
        vkDestroyRenderPass(device, m_renderPass, nullptr);

    m_sphereVBO.reset();
    m_sphereIBO.reset();
    DestroyViewResources();
}

// ============================================================================
// RenderToPixels — main entry point
// ============================================================================

std::shared_ptr<vk::ImageReadbackTicket> GPUMaterialPreview::BeginRenderToPixels(InxMaterial &material, int size)
{
    if (!m_vkCore || size <= 0)
        return nullptr;
    if (m_activeReadback && !m_activeReadback->IsDone())
        return nullptr;
    m_activeReadback.reset();

    const int renderSize = std::max(size, size * kPreviewSupersampleFactor);
    const glm::mat4 previewModel = glm::scale(glm::mat4(1.0f), glm::vec3(kPreviewModelScale));
    const glm::mat4 previewNormal = glm::transpose(glm::inverse(previewModel));

    std::vector<std::shared_ptr<InxMaterial>> ownedPreviewMaterials;
    std::vector<std::shared_ptr<InxMaterial>> previewPassMaterials;

    // Always isolate preview rendering from the caller's material instance.
    // Even opaque previews can otherwise overwrite or invalidate the live
    // material's forward pass handles while the main scene is still drawing.
    auto basePreviewMaterial = material.Clone();
    if (!basePreviewMaterial) {
        return nullptr;
    }
    basePreviewMaterial->ClearAllPassPipelines();
    if (!EnsureResources(renderSize))
        return nullptr;
    if (!m_vkCore->RefreshPreviewMaterialPipeline(
            basePreviewMaterial, basePreviewMaterial->GetVertShaderName(), basePreviewMaterial->GetFragShaderName(),
            m_previewSceneUbo->GetBuffer(), m_previewLightingUbo->GetBuffer(), false)) {
        INXLOG_DEBUG("GPUMaterialPreview: material domain has no sphere preview pipeline");
        return nullptr;
    }
    ownedPreviewMaterials.push_back(basePreviewMaterial);
    previewPassMaterials.push_back(basePreviewMaterial);

    const RenderState &baseState = basePreviewMaterial->GetRenderState();
    if (baseState.blendEnable) {
        auto buildPreviewPass = [&](VkCullModeFlags cullMode, bool overrideCull) -> std::shared_ptr<InxMaterial> {
            // Preview passes must not reuse the source material's GUID/name key,
            // otherwise descriptor-cache replacement can invalidate the live set
            // still used by the main scene.
            auto passMaterial = basePreviewMaterial->Clone();
            RenderState state = passMaterial->GetRenderState();
            if (overrideCull)
                state.cullMode = cullMode;

            // Preview path uses transparent clear color (alpha=0), so alpha must
            // be written by the material pass instead of preserving destination alpha.
            state.srcAlphaBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;
            state.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
            state.alphaBlendOp = VK_BLEND_OP_ADD;

            passMaterial->SetRenderState(state);
            passMaterial->MarkOverride(RenderStateOverride::BlendMode);
            if (overrideCull)
                passMaterial->MarkOverride(RenderStateOverride::CullMode);
            passMaterial->ClearAllPassPipelines();

            if (!m_vkCore->RefreshPreviewMaterialPipeline(
                    passMaterial, passMaterial->GetVertShaderName(), passMaterial->GetFragShaderName(),
                    m_previewSceneUbo->GetBuffer(), m_previewLightingUbo->GetBuffer(), false)) {
                return nullptr;
            }
            return passMaterial;
        };

        if (!baseState.depthWriteEnable && baseState.cullMode == VK_CULL_MODE_NONE) {
            auto backFacePass = buildPreviewPass(VK_CULL_MODE_FRONT_BIT, true);
            auto frontFacePass = buildPreviewPass(VK_CULL_MODE_BACK_BIT, true);
            if (backFacePass && frontFacePass) {
                ownedPreviewMaterials.push_back(backFacePass);
                ownedPreviewMaterials.push_back(frontFacePass);
                previewPassMaterials.clear();
                previewPassMaterials.push_back(backFacePass);
                previewPassMaterials.push_back(frontFacePass);
            } else {
                INXLOG_DEBUG("GPUMaterialPreview: transparent two-pass preview unavailable, using original pipeline");
            }
        } else {
            auto singlePass = buildPreviewPass(baseState.cullMode, false);
            if (singlePass) {
                ownedPreviewMaterials.push_back(singlePass);
                previewPassMaterials.clear();
                previewPassMaterials.push_back(singlePass);
            } else {
                INXLOG_DEBUG(
                    "GPUMaterialPreview: transparent preview alpha override unavailable, using original pipeline");
            }
        }
    }

    struct PreviewPassBinding
    {
        std::shared_ptr<InxMaterial> material;
        VkPipeline pipeline = VK_NULL_HANDLE;
        VkPipelineLayout pipelineLayout = VK_NULL_HANDLE;
        VkDescriptorSet materialDescSet = VK_NULL_HANDLE;
        ShaderProgram *program = nullptr;
    };

    auto refreshPassBinding = [&](PreviewPassBinding &binding) -> bool {
        InxMaterial *passMat = binding.material.get();
        if (!passMat) {
            return false;
        }

        MaterialRenderData *rd = m_vkCore->GetMaterialPipelineManager().GetRenderData(passMat->GetMaterialKey());
        if (!rd || !rd->isValid || rd->descriptorSet == VK_NULL_HANDLE) {
            if (!m_vkCore->RefreshPreviewMaterialPipeline(binding.material, passMat->GetVertShaderName(),
                                                          passMat->GetFragShaderName(), m_previewSceneUbo->GetBuffer(),
                                                          m_previewLightingUbo->GetBuffer(), false)) {
                return false;
            }
            rd = m_vkCore->GetMaterialPipelineManager().GetRenderData(passMat->GetMaterialKey());
        }

        if (rd && rd->isValid && rd->descriptorSet != VK_NULL_HANDLE) {
            passMat->SetPassPipeline(ShaderCompileTarget::Forward, rd->pipeline);
            passMat->SetPassPipelineLayout(ShaderCompileTarget::Forward, rd->pipelineLayout);
            passMat->SetPassDescriptorSet(ShaderCompileTarget::Forward, rd->descriptorSet);
            passMat->SetPassShaderProgram(ShaderCompileTarget::Forward, rd->shaderProgram);
        }

        binding.pipeline = passMat->GetPassPipeline(ShaderCompileTarget::Forward);
        binding.pipelineLayout = passMat->GetPassPipelineLayout(ShaderCompileTarget::Forward);
        binding.materialDescSet = passMat->GetPassDescriptorSet(ShaderCompileTarget::Forward);
        binding.program = passMat->GetPassShaderProgram(ShaderCompileTarget::Forward);

        return binding.pipeline != VK_NULL_HANDLE && binding.pipelineLayout != VK_NULL_HANDLE &&
               binding.materialDescSet != VK_NULL_HANDLE && binding.program != nullptr;
    };

    std::vector<PreviewPassBinding> passBindings;
    passBindings.reserve(previewPassMaterials.size());
    for (const auto &passMat : previewPassMaterials) {
        PreviewPassBinding binding{};
        binding.material = passMat;
        if (!refreshPassBinding(binding)) {
            INXLOG_WARN("GPUMaterialPreview: preview pass pipeline not ready");
            return nullptr;
        }
        passBindings.push_back(binding);
    }

    // Update material UBO so GPU-side data is current
    for (auto &binding : passBindings)
        m_vkCore->UpdateMaterialUBO(*binding.material);

    // ------------------------------------------------------------------
    // Prepare preview scene UBO (camera looking at sphere)
    // ------------------------------------------------------------------
    float aspect = 1.0f;
    glm::mat4 view = glm::lookAt(glm::vec3(0.0f, 0.0f, kPreviewCameraDistance), glm::vec3(0.0f, 0.0f, 0.0f),
                                 glm::vec3(0.0f, 1.0f, 0.0f));
    glm::mat4 proj = glm::perspective(glm::radians(45.0f), aspect, 0.1f, 100.0f);
    proj[1][1] *= -1.0f; // Vulkan Y-flip

    UniformBufferObject sceneUBO{};
    sceneUBO.model = previewModel;
    sceneUBO.view = view;
    sceneUBO.proj = proj;
    sceneUBO.previousViewProj = proj * view;

    // ------------------------------------------------------------------
    // Prepare preview lighting UBO.
    // Use a soft two-light rig plus ambient probe so aggressive normal-map
    // values still read as a sphere instead of collapsing into a tiny bright
    // patch under a single hard key light.
    // ------------------------------------------------------------------
    ShaderLightingUBO lightingUBO{};
    memset(&lightingUBO, 0, sizeof(lightingUBO));
    lightingUBO.lightCounts = glm::ivec4(2, 0, 0, 0);
    lightingUBO.ambientColor = glm::vec4(0.06f, 0.06f, 0.07f, 1.0f);
    lightingUBO.ambientSkyColor = glm::vec4(0.18f, 0.19f, 0.22f, 0.55f);
    lightingUBO.ambientEquatorColor = glm::vec4(0.09f, 0.10f, 0.12f, 1.0f);
    lightingUBO.ambientGroundColor = glm::vec4(0.04f, 0.035f, 0.03f, 0.30f);
    lightingUBO.cameraPos = glm::vec4(0.0f, 0.0f, kPreviewCameraDistance, 1.0f);

    // Main key light — top-right-front
    lightingUBO.directionalLights[0].direction = glm::vec4(glm::normalize(glm::vec3(-0.8f, -1.0f, -0.6f)), 0.0f);
    // color.rgb = color * intensity, color.a = intensity
    lightingUBO.directionalLights[0].color = glm::vec4(2.0f * 1.0f, 2.0f * 0.95f, 2.0f * 0.9f, 2.0f);
    lightingUBO.directionalLights[0].metadata =
        glm::uvec4(~0u, static_cast<uint32_t>(LightInfluenceDomain::Geometry), 0u, 0u);

    // Fill light — back-left to keep the sphere readable under harsh normals.
    lightingUBO.directionalLights[1].direction = glm::vec4(glm::normalize(glm::vec3(0.6f, 0.3f, -0.8f)), 0.0f);
    lightingUBO.directionalLights[1].color = glm::vec4(0.6f * 0.6f, 0.6f * 0.7f, 0.6f * 0.85f, 0.6f);
    lightingUBO.directionalLights[1].metadata =
        glm::uvec4(~0u, static_cast<uint32_t>(LightInfluenceDomain::Geometry), 0u, 0u);

    // ------------------------------------------------------------------
    // Prepare engine globals UBO (minimal)
    // ------------------------------------------------------------------
    EngineGlobalsUBO globalsUBO{};
    memset(&globalsUBO, 0, sizeof(globalsUBO));
    globalsUBO.screenParams =
        glm::vec4(static_cast<float>(renderSize), static_cast<float>(renderSize), 1.0f / renderSize, 1.0f / renderSize);
    globalsUBO.worldSpaceCameraPos = glm::vec4(0.0f, 0.0f, kPreviewCameraDistance, 1.0f);

    const VkBuffer sceneUBOBuf = m_previewSceneUbo->GetBuffer();
    const VkBuffer lightingUBOBuf = m_previewLightingUbo->GetBuffer();
    const VkBuffer globalsUBOBuf = m_previewGlobalsUbo->GetBuffer();
    const VkBuffer instanceSSBOBuf = m_previewInstanceBuffer->GetBuffer();
    VkDescriptorSet shadowDesc = VK_NULL_HANDLE;
    VkDescriptorSet globalsDesc = VK_NULL_HANDLE;

    const bool requiresPerViewSet = std::any_of(passBindings.begin(), passBindings.end(), [](const auto &binding) {
        return binding.program && binding.program->HasDeclaredDescriptorSet(1);
    });
    const bool requiresGlobalsSet = std::any_of(passBindings.begin(), passBindings.end(), [](const auto &binding) {
        return binding.program && binding.program->HasDeclaredDescriptorSet(2);
    });

    if (requiresPerViewSet) {
        if (m_fallbackShadowDescSet == VK_NULL_HANDLE) {
            m_fallbackShadowDescSet = m_vkCore->AllocatePerViewDescriptorSet();
            if (m_fallbackShadowDescSet != VK_NULL_HANDLE)
                m_vkCore->UpdatePerViewLightingBuffer(m_fallbackShadowDescSet, lightingUBOBuf,
                                                      sizeof(ShaderLightingUBO));
        }
        shadowDesc = m_fallbackShadowDescSet;
        if (shadowDesc == VK_NULL_HANDLE) {
            INXLOG_WARN("GPUMaterialPreview: shader requires set 1 but no shadow descriptor is available");
            return nullptr;
        }
    }

    if (requiresGlobalsSet)
        globalsDesc = m_previewGlobalsSet;

    auto &resourceManager = m_vkCore->GetResourceManager();
    auto readbackRecorder = resourceManager.BeginGraphicsImageReadback(
        static_cast<uint32_t>(renderSize), static_cast<uint32_t>(renderSize), m_colorFormat);
    VkCommandBuffer cmd = readbackRecorder.GetCommandBuffer();

    vkCmdUpdateBuffer(cmd, sceneUBOBuf, 0, sizeof(sceneUBO), &sceneUBO);
    vkCmdUpdateBuffer(cmd, lightingUBOBuf, 0, sizeof(lightingUBO), &lightingUBO);
    vkCmdUpdateBuffer(cmd, globalsUBOBuf, 0, sizeof(globalsUBO), &globalsUBO);
    vkCmdUpdateBuffer(cmd, instanceSSBOBuf, 0, sizeof(previewModel), &previewModel);
    const std::array<uint32_t, 4> emptySkinInstance{};
    const glm::mat4 identityBone(1.0f);
    vkCmdUpdateBuffer(cmd, m_previewSkinInstanceBuffer->GetBuffer(), 0, sizeof(emptySkinInstance),
                      emptySkinInstance.data());
    vkCmdUpdateBuffer(cmd, m_previewSkinPaletteBuffer->GetBuffer(), 0, sizeof(identityBone), &identityBone);
    GPUInstanceAuxData instanceAux{};
    instanceAux.previousModel = previewModel;
    instanceAux.layerMask = ~0u;
    vkCmdUpdateBuffer(cmd, m_previewInstanceAuxBuffer->GetBuffer(), 0, sizeof(instanceAux), &instanceAux);

    // Memory barrier: make UBO writes visible to shaders
    VkMemoryBarrier uboBarrier{};
    uboBarrier.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
    uboBarrier.srcAccessMask = VK_ACCESS_HOST_WRITE_BIT | VK_ACCESS_TRANSFER_WRITE_BIT;
    uboBarrier.dstAccessMask = VK_ACCESS_UNIFORM_READ_BIT | VK_ACCESS_SHADER_READ_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_HOST_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT,
                         VK_PIPELINE_STAGE_VERTEX_SHADER_BIT | VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 1, &uboBarrier,
                         0, nullptr, 0, nullptr);

    // ------------------------------------------------------------------
    // Begin render pass
    // ------------------------------------------------------------------
    VkClearValue clearValues[2];
    clearValues[0].color = {{kPreviewMatteR, kPreviewMatteG, kPreviewMatteB, kPreviewMatteA}};
    clearValues[1].depthStencil = {1.0f, 0};

    VkRenderPassBeginInfo rpBegin{};
    rpBegin.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
    rpBegin.renderPass = m_renderPass;
    rpBegin.framebuffer = m_framebuffer;
    rpBegin.renderArea.offset = {0, 0};
    rpBegin.renderArea.extent = {static_cast<uint32_t>(renderSize), static_cast<uint32_t>(renderSize)};
    rpBegin.clearValueCount = 2;
    rpBegin.pClearValues = clearValues;

    vkCmdBeginRenderPass(cmd, &rpBegin, VK_SUBPASS_CONTENTS_INLINE);

    // Viewport & scissor
    VkViewport viewport{};
    viewport.x = 0.0f;
    viewport.y = 0.0f;
    viewport.width = static_cast<float>(renderSize);
    viewport.height = static_cast<float>(renderSize);
    viewport.minDepth = 0.0f;
    viewport.maxDepth = 1.0f;
    vkCmdSetViewport(cmd, 0, 1, &viewport);

    VkRect2D scissor{};
    scissor.offset = {0, 0};
    scissor.extent = {static_cast<uint32_t>(renderSize), static_cast<uint32_t>(renderSize)};
    vkCmdSetScissor(cmd, 0, 1, &scissor);

    // Bind sphere geometry
    VkBuffer vbo = m_sphereVBO->GetBuffer();
    VkDeviceSize offsets[] = {0};
    vkCmdBindVertexBuffers(cmd, 0, 1, &vbo, offsets);
    vkCmdBindIndexBuffer(cmd, m_sphereIBO->GetBuffer(), 0, VK_INDEX_TYPE_UINT32);

    // Push constants (identity model + normalMat)
    struct PushConstants
    {
        glm::mat4 model;
        glm::mat4 normalMat;
    };
    PushConstants pushData{};
    pushData.model = previewModel;
    pushData.normalMat = previewNormal;
    bool anyDrawn = false;
    for (auto &binding : passBindings) {
        const uint64_t descRaw = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(binding.materialDescSet));
        if ((descRaw & 0xffffull) == 0x00dfull || (descRaw & 0xfffffull) == 0x002b6ull) {
            INXLOG_WARN("GPUMaterialPreview: suspicious set0 descriptor=", descRaw, " material='",
                        binding.material ? binding.material->GetMaterialKey() : std::string(), "'");
        }

        if (binding.materialDescSet != VK_NULL_HANDLE &&
            !m_vkCore->GetMaterialPipelineManager().IsDescriptorSetLive(binding.materialDescSet)) {
            static int deadPreviewDescWarnCount = 0;
            if (deadPreviewDescWarnCount++ < 8) {
                INXLOG_WARN("GPUMaterialPreview: DEAD set0 descriptor=", descRaw, " material='",
                            binding.material ? binding.material->GetMaterialKey() : std::string(), "' - skipping pass");
            }
            continue;
        }

        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, binding.pipeline);

        // Set 0 — material (scene UBO + lighting UBO + material UBO + textures)
        vkdebug::CmdBindDescriptorSetsTracked("GPUMaterialPreview.RenderToPixels.Set0", cmd,
                                              VK_PIPELINE_BIND_POINT_GRAPHICS, binding.pipelineLayout, 0, 1,
                                              &binding.materialDescSet, 0, nullptr);

        // Set 1 — shadow (if the shader declares it)
        if (binding.program->HasDeclaredDescriptorSet(1)) {
            vkdebug::CmdBindDescriptorSetsTracked("GPUMaterialPreview.RenderToPixels.Set1", cmd,
                                                  VK_PIPELINE_BIND_POINT_GRAPHICS, binding.pipelineLayout, 1, 1,
                                                  &shadowDesc, 0, nullptr);
        }

        // Set 2 — engine globals (if the shader declares it)
        if (binding.program->HasDeclaredDescriptorSet(2)) {
            vkdebug::CmdBindDescriptorSetsTracked("GPUMaterialPreview.RenderToPixels.Set2", cmd,
                                                  VK_PIPELINE_BIND_POINT_GRAPHICS, binding.pipelineLayout, 2, 1,
                                                  &globalsDesc, 0, nullptr);
        }

        vkCmdPushConstants(cmd, binding.pipelineLayout, VK_SHADER_STAGE_VERTEX_BIT, 0, sizeof(PushConstants),
                           &pushData);
        vkCmdDrawIndexed(cmd, m_sphereIndexCount, 1, 0, 0, 0);
        anyDrawn = true;
    }

    vkCmdEndRenderPass(cmd);

    // ------------------------------------------------------------------
    // Resolve MSAA → single-sample resolve image
    // ------------------------------------------------------------------
    if (m_sampleCount != VK_SAMPLE_COUNT_1_BIT) {
        // Transition MSAA color → TRANSFER_SRC
        VkImageMemoryBarrier msaaBarrier = vkrender::MakeImageBarrier(
            m_msaaColor.GetImage(), VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_IMAGE_ASPECT_COLOR_BIT, VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
        vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0,
                             nullptr, 0, nullptr, 1, &msaaBarrier);

        // Transition resolve → TRANSFER_DST
        VkImageMemoryBarrier resolveBarrier = vkrender::MakeImageBarrier(
            m_resolveColor.GetImage(), VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
            VK_IMAGE_ASPECT_COLOR_BIT, 0, VK_ACCESS_TRANSFER_WRITE_BIT);
        vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0,
                             nullptr, 1, &resolveBarrier);

        // Resolve
        VkImageResolve resolveRegion{};
        resolveRegion.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
        resolveRegion.dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
        resolveRegion.extent = {static_cast<uint32_t>(renderSize), static_cast<uint32_t>(renderSize), 1};
        vkCmdResolveImage(cmd, m_msaaColor.GetImage(), VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, m_resolveColor.GetImage(),
                          VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &resolveRegion);

        // Transition resolve → TRANSFER_SRC for readback
        VkImageMemoryBarrier readbackBarrier = vkrender::MakeImageBarrier(
            m_resolveColor.GetImage(), VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_IMAGE_ASPECT_COLOR_BIT, VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
        vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0,
                             nullptr, 1, &readbackBarrier);
    } else {
        // No MSAA — transition MSAA color (which is really 1x) → TRANSFER_SRC
        VkImageMemoryBarrier barrier = vkrender::MakeImageBarrier(
            m_msaaColor.GetImage(), VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_IMAGE_ASPECT_COLOR_BIT, VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
        vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0,
                             nullptr, 0, nullptr, 1, &barrier);
    }

    // ------------------------------------------------------------------
    // Copy resolved image → staging buffer
    // ------------------------------------------------------------------
    VkImage srcImage = (m_sampleCount != VK_SAMPLE_COUNT_1_BIT) ? m_resolveColor.GetImage() : m_msaaColor.GetImage();

    VkBufferImageCopy copyRegion{};
    copyRegion.bufferOffset = 0;
    copyRegion.bufferRowLength = 0;
    copyRegion.bufferImageHeight = 0;
    copyRegion.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    copyRegion.imageOffset = {0, 0, 0};
    copyRegion.imageExtent = {static_cast<uint32_t>(renderSize), static_cast<uint32_t>(renderSize), 1};
    vkCmdCopyImageToBuffer(cmd, srcImage, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, readbackRecorder.GetStagingBuffer(), 1,
                           &copyRegion);

    // If every pass was skipped (e.g. a descriptor set went dead while the user was
    // editing the material), the sphere wasn't drawn. Report failure so the caller
    // falls back to the CPU renderer instead of returning a blank/transparent image
    // (which made Project-panel and Inspector previews intermittently disappear).
    if (!anyDrawn)
        return nullptr;

    m_activeReadback = readbackRecorder.Submit(
        [materialLeases = std::move(ownedPreviewMaterials)]() mutable { materialLeases.clear(); });
    return m_activeReadback;
}

bool GPUMaterialPreview::TryCompleteRenderToPixels(const std::shared_ptr<vk::ImageReadbackTicket> &ticket,
                                                   int outputSize, std::vector<unsigned char> &outPixels)
{
    if (!ticket || !ticket->IsDone() || ticket->GetStatus() != vk::ImageReadbackStatus::Completed || outputSize <= 0)
        return false;

    const int renderSize = static_cast<int>(ticket->GetWidth());
    if (renderSize <= 0 || ticket->GetHeight() != ticket->GetWidth() || ticket->GetChannelCount() != 4)
        return false;

    // ------------------------------------------------------------------
    // Readback: HDR R16G16B16A16_SFLOAT → RGBA8 with Reinhard tonemap
    // ------------------------------------------------------------------
    const int pixelCount = renderSize * renderSize;
    std::vector<unsigned char> renderPixels(static_cast<size_t>(pixelCount) * 4, 0);

    const auto &raw = ticket->GetData();

    if (ticket->GetElementType() == "float16") {
        // HDR → sRGB RGBA8
        const uint16_t *src = reinterpret_cast<const uint16_t *>(raw.data());
        auto halfToFloat = [](uint16_t h) -> float {
            uint32_t sign = (h >> 15) & 0x1;
            uint32_t exponent = (h >> 10) & 0x1F;
            uint32_t mantissa = h & 0x3FF;
            if (exponent == 0) {
                if (mantissa == 0)
                    return sign ? -0.0f : 0.0f;
                // Subnormal
                float val = (mantissa / 1024.0f) * std::pow(2.0f, -14.0f);
                return sign ? -val : val;
            }
            if (exponent == 31)
                return mantissa ? 0.0f : (sign ? -1e30f : 1e30f); // NaN/Inf
            float val = std::pow(2.0f, static_cast<float>(exponent) - 15.0f) * (1.0f + mantissa / 1024.0f);
            return sign ? -val : val;
        };

        auto linearToSrgb = [](float c) -> float {
            if (c <= 0.0031308f)
                return c * 12.92f;
            return 1.055f * std::pow(c, 1.0f / 2.4f) - 0.055f;
        };

        for (int i = 0; i < pixelCount; ++i) {
            const int x = i % renderSize;
            const int y = i / renderSize;
            float r = halfToFloat(src[i * 4 + 0]);
            float g = halfToFloat(src[i * 4 + 1]);
            float b = halfToFloat(src[i * 4 + 2]);
            const float srcAlpha = std::clamp(halfToFloat(src[i * 4 + 3]), 0.0f, 1.0f);
            const float coverage = ComputePreviewCoverage(x, y, renderSize);
            const float a = std::clamp(srcAlpha * coverage, 0.0f, 1.0f);

            if (a <= 0.0f) {
                renderPixels[i * 4 + 0] = 0;
                renderPixels[i * 4 + 1] = 0;
                renderPixels[i * 4 + 2] = 0;
                renderPixels[i * 4 + 3] = 0;
                continue;
            }

            glm::vec3 linearRgb = RemovePreviewMatte(glm::vec3(r, g, b), a);
            r = linearRgb.r;
            g = linearRgb.g;
            b = linearRgb.b;

            // Reinhard tonemap
            r = r / (1.0f + r);
            g = g / (1.0f + g);
            b = b / (1.0f + b);

            // Linear → sRGB
            r = linearToSrgb(r);
            g = linearToSrgb(g);
            b = linearToSrgb(b);

            renderPixels[i * 4 + 0] = static_cast<unsigned char>(std::min(std::max(r, 0.0f), 1.0f) * 255.0f + 0.5f);
            renderPixels[i * 4 + 1] = static_cast<unsigned char>(std::min(std::max(g, 0.0f), 1.0f) * 255.0f + 0.5f);
            renderPixels[i * 4 + 2] = static_cast<unsigned char>(std::min(std::max(b, 0.0f), 1.0f) * 255.0f + 0.5f);
            renderPixels[i * 4 + 3] = static_cast<unsigned char>(std::min(std::max(a, 0.0f), 1.0f) * 255.0f + 0.5f);
        }
    } else {
        // Fallback: assume RGBA8 or similar. Convert the exact matte clear back
        // to transparent so Project Panel thumbnails remain visible.
        if (raw.size() != renderPixels.size())
            return false;
        const unsigned char *src = raw.data();
        const unsigned char matteR = static_cast<unsigned char>(kPreviewMatteR * 255.0f + 0.5f);
        const unsigned char matteG = static_cast<unsigned char>(kPreviewMatteG * 255.0f + 0.5f);
        const unsigned char matteB = static_cast<unsigned char>(kPreviewMatteB * 255.0f + 0.5f);

        for (int i = 0; i < pixelCount; ++i) {
            const int x = i % renderSize;
            const int y = i / renderSize;
            unsigned char r = src[i * 4 + 0];
            unsigned char g = src[i * 4 + 1];
            unsigned char b = src[i * 4 + 2];
            const float srcAlpha = static_cast<float>(src[i * 4 + 3]) / 255.0f;
            const float coverage = ComputePreviewCoverage(x, y, renderSize);
            const float a = std::clamp(srcAlpha * coverage, 0.0f, 1.0f);

            if (a <= 0.0f) {
                renderPixels[i * 4 + 0] = 0;
                renderPixels[i * 4 + 1] = 0;
                renderPixels[i * 4 + 2] = 0;
                renderPixels[i * 4 + 3] = 0;
                continue;
            }

            const float rf = static_cast<float>(r) / 255.0f;
            const float gf = static_cast<float>(g) / 255.0f;
            const float bf = static_cast<float>(b) / 255.0f;
            glm::vec3 linearRgb = RemovePreviewMatte(glm::vec3(rf, gf, bf), a);

            renderPixels[i * 4 + 0] = static_cast<unsigned char>(std::clamp(linearRgb.r, 0.0f, 1.0f) * 255.0f + 0.5f);
            renderPixels[i * 4 + 1] = static_cast<unsigned char>(std::clamp(linearRgb.g, 0.0f, 1.0f) * 255.0f + 0.5f);
            renderPixels[i * 4 + 2] = static_cast<unsigned char>(std::clamp(linearRgb.b, 0.0f, 1.0f) * 255.0f + 0.5f);
            renderPixels[i * 4 + 3] = static_cast<unsigned char>(a * 255.0f + 0.5f);
        }
    }

    DownsampleRGBABox(renderPixels, renderSize, outputSize, outPixels);
    if (m_activeReadback == ticket)
        m_activeReadback.reset();
    return true;
}

// ============================================================================
// Resource management
// ============================================================================

bool GPUMaterialPreview::EnsureViewResources()
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
        throw std::runtime_error("Failed to allocate isolated material-preview view buffers");

    VkDescriptorPoolSize poolSizes[2]{};
    poolSizes[0].type = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    poolSizes[0].descriptorCount = 1;
    poolSizes[1].type = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    poolSizes[1].descriptorCount = 4;
    VkDescriptorPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
    poolInfo.maxSets = 1;
    poolInfo.poolSizeCount = 2;
    poolInfo.pPoolSizes = poolSizes;
    const VkDevice device = m_vkCore->GetDevice();
    if (vkCreateDescriptorPool(device, &poolInfo, nullptr, &m_previewGlobalsPool) != VK_SUCCESS)
        throw std::runtime_error("Failed to create isolated material-preview globals descriptor pool");

    const VkDescriptorSetLayout layout = m_vkCore->GetGlobalsDescSetLayout();
    if (layout == VK_NULL_HANDLE)
        throw std::logic_error("Material preview requires the renderer globals descriptor layout");
    VkDescriptorSetAllocateInfo allocateInfo{};
    allocateInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
    allocateInfo.descriptorPool = m_previewGlobalsPool;
    allocateInfo.descriptorSetCount = 1;
    allocateInfo.pSetLayouts = &layout;
    if (vkAllocateDescriptorSets(device, &allocateInfo, &m_previewGlobalsSet) != VK_SUCCESS)
        throw std::runtime_error("Failed to allocate isolated material-preview globals descriptor set");

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

void GPUMaterialPreview::DestroyViewResources()
{
    m_activeReadback.reset();
    m_previewGlobalsSet = VK_NULL_HANDLE;
    if (m_previewGlobalsPool != VK_NULL_HANDLE) {
        vkDestroyDescriptorPool(m_vkCore->GetDevice(), m_previewGlobalsPool, nullptr);
        m_previewGlobalsPool = VK_NULL_HANDLE;
    }
    m_previewSkinPaletteBuffer.reset();
    m_previewSkinInstanceBuffer.reset();
    m_previewInstanceAuxBuffer.reset();
    m_previewInstanceBuffer.reset();
    m_previewGlobalsUbo.reset();
    m_previewLightingUbo.reset();
    m_previewSceneUbo.reset();
}

bool GPUMaterialPreview::EnsureResources(int size)
{
    // Cache format info from MaterialPipelineManager
    auto &mpm = m_vkCore->GetMaterialPipelineManager();
    VkFormat colorFormat = mpm.GetColorFormat();
    VkFormat depthFormat = mpm.GetDepthFormat();
    VkSampleCountFlagBits sampleCount = mpm.GetSampleCount();

    const bool renderConfigChanged =
        (m_renderPass != VK_NULL_HANDLE) &&
        (colorFormat != m_colorFormat || depthFormat != m_depthFormat || sampleCount != m_sampleCount);

    if (renderConfigChanged) {
        DestroyFramebuffer();
        vkDestroyRenderPass(m_vkCore->GetDevice(), m_renderPass, nullptr);
        m_renderPass = VK_NULL_HANDLE;
    }

    m_colorFormat = colorFormat;
    m_depthFormat = depthFormat;
    m_sampleCount = sampleCount;

    if (m_renderPass == VK_NULL_HANDLE)
        CreateRenderPass();

    if (m_renderPass == VK_NULL_HANDLE)
        return false;

    if (!m_sphereVBO || !m_sphereIBO)
        CreateSphereBuffers();

    if (!m_sphereVBO || !m_sphereIBO)
        return false;

    if (m_currentSize != size) {
        DestroyFramebuffer();
        CreateFramebuffer(size);
        m_currentSize = size;
    }

    return m_framebuffer != VK_NULL_HANDLE && EnsureViewResources();
}

void GPUMaterialPreview::CreateRenderPass()
{
    VkDevice device = m_vkCore->GetDevice();

    // Must match MaterialPipelineManager::CreateInternalRenderPass exactly
    // for pipeline compatibility.
    std::vector<VkAttachmentDescription> attachments;
    std::vector<VkAttachmentReference> colorRefs;
    VkAttachmentReference depthRef{};

    // Attachment 0: MSAA color
    VkAttachmentDescription colorAtt{};
    colorAtt.format = m_colorFormat;
    colorAtt.samples = m_sampleCount;
    colorAtt.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    colorAtt.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    colorAtt.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    colorAtt.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    colorAtt.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    colorAtt.finalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    attachments.push_back(colorAtt);

    VkAttachmentReference colorRef{};
    colorRef.attachment = 0;
    colorRef.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    colorRefs.push_back(colorRef);

    // Attachment 1: depth
    bool hasDepth = (m_depthFormat != VK_FORMAT_UNDEFINED);
    if (hasDepth) {
        VkAttachmentDescription depthAtt{};
        depthAtt.format = m_depthFormat;
        depthAtt.samples = m_sampleCount;
        depthAtt.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        depthAtt.storeOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        depthAtt.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        depthAtt.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        depthAtt.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        depthAtt.finalLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
        attachments.push_back(depthAtt);

        depthRef.attachment = 1;
        depthRef.layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    }

    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = static_cast<uint32_t>(colorRefs.size());
    subpass.pColorAttachments = colorRefs.data();
    subpass.pDepthStencilAttachment = hasDepth ? &depthRef : nullptr;
    subpass.pResolveAttachments = nullptr;

    // Must match MaterialPipelineManager subpass dependency for compatibility.
    const VkSubpassDependency dependency = vkrender::MakePipelineCompatibleSubpassDependency();

    VkRenderPassCreateInfo rpInfo{};
    rpInfo.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
    rpInfo.attachmentCount = static_cast<uint32_t>(attachments.size());
    rpInfo.pAttachments = attachments.data();
    rpInfo.subpassCount = 1;
    rpInfo.pSubpasses = &subpass;
    rpInfo.dependencyCount = 1;
    rpInfo.pDependencies = &dependency;

    if (vkCreateRenderPass(device, &rpInfo, nullptr, &m_renderPass) != VK_SUCCESS) {
        INXLOG_ERROR("GPUMaterialPreview: failed to create render pass");
        m_renderPass = VK_NULL_HANDLE;
    }
}

void GPUMaterialPreview::CreateFramebuffer(int size)
{
    VkDevice device = m_vkCore->GetDevice();
    VmaAllocator allocator = m_vkCore->GetDeviceContext().GetVmaAllocator();
    uint32_t w = static_cast<uint32_t>(size);
    uint32_t h = static_cast<uint32_t>(size);

    // MSAA color image
    m_msaaColor.Create(allocator, device, w, h, m_colorFormat, VK_IMAGE_TILING_OPTIMAL,
                       VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT,
                       VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, m_sampleCount);
    m_msaaColor.CreateView(m_colorFormat, VK_IMAGE_ASPECT_COLOR_BIT);

    // Resolve image (1x sample, for MSAA resolve + readback)
    if (m_sampleCount != VK_SAMPLE_COUNT_1_BIT) {
        m_resolveColor.Create(allocator, device, w, h, m_colorFormat, VK_IMAGE_TILING_OPTIMAL,
                              VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT,
                              VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, VK_SAMPLE_COUNT_1_BIT);
    }

    // Depth image (MSAA)
    if (m_depthFormat != VK_FORMAT_UNDEFINED) {
        m_depth.Create(allocator, device, w, h, m_depthFormat, VK_IMAGE_TILING_OPTIMAL,
                       VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, m_sampleCount);
        m_depth.CreateView(m_depthFormat, VK_IMAGE_ASPECT_DEPTH_BIT);
    }

    // Framebuffer
    std::vector<VkImageView> fbAttachments;
    fbAttachments.push_back(m_msaaColor.GetView());
    if (m_depth.IsValid())
        fbAttachments.push_back(m_depth.GetView());

    VkFramebufferCreateInfo fbInfo{};
    fbInfo.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
    fbInfo.renderPass = m_renderPass;
    fbInfo.attachmentCount = static_cast<uint32_t>(fbAttachments.size());
    fbInfo.pAttachments = fbAttachments.data();
    fbInfo.width = w;
    fbInfo.height = h;
    fbInfo.layers = 1;

    if (vkCreateFramebuffer(device, &fbInfo, nullptr, &m_framebuffer) != VK_SUCCESS) {
        INXLOG_ERROR("GPUMaterialPreview: failed to create framebuffer");
        m_framebuffer = VK_NULL_HANDLE;
        return;
    }
}

void GPUMaterialPreview::DestroyFramebuffer()
{
    VkDevice device = m_vkCore->GetDevice();
    if (m_framebuffer != VK_NULL_HANDLE) {
        vkDestroyFramebuffer(device, m_framebuffer, nullptr);
        m_framebuffer = VK_NULL_HANDLE;
    }
    m_msaaColor.Destroy();
    m_resolveColor.Destroy();
    m_depth.Destroy();
    m_currentSize = 0;
}

void GPUMaterialPreview::CreateSphereBuffers()
{
    const auto &vertices = PrimitiveMeshes::GetSphereVertices();
    const auto &indices = PrimitiveMeshes::GetSphereIndices();

    if (vertices.empty() || indices.empty()) {
        INXLOG_ERROR("GPUMaterialPreview: sphere mesh is empty");
        return;
    }

    auto &rm = m_vkCore->GetResourceManager();
    m_sphereVBO = rm.CreateVertexBuffer(vertices.data(), vertices.size() * sizeof(Vertex));
    m_sphereIBO = rm.CreateIndexBuffer(indices.data(), indices.size() * sizeof(uint32_t));
    m_sphereIndexCount = static_cast<uint32_t>(indices.size());
}

} // namespace infernux
