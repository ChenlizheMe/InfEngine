/**
 * @file OutlineRenderer.cpp
 * @brief Post-process selection outline renderer implementation
 *
 * Extracted from InxVkCoreModular.cpp during editor/renderer separation.
 */

#include "OutlineRenderer.h"
#include "InxVkCoreModular.h"
#include "MaterialDescriptor.h"
#include "MaterialPipelineManager.h"
#include "SceneRenderTarget.h"
#include "VertexInputFilter.h"
#include "shader/ShaderProgram.h"
#include "shader/ShaderReflection.h"
#include <core/types/ColorSpace.h>
#include "vk/DescriptorBindTrace.h"
#include "vk/VkPipelineHelpers.h"
#include "vk/VkRenderUtils.h"
#include <function/resources/InxMaterial/InxMaterial.h>

#include <core/error/InxError.h>
#include <glm/gtc/matrix_inverse.hpp>
#include <glm/gtc/matrix_transform.hpp>

#include <vk_mem_alloc.h>

#include <array>
#include <cstring>
#include <vector>

namespace infernux
{

namespace
{

constexpr uint32_t kOutlineSceneUBOBinding = 0;
constexpr uint32_t kOutlineVertexMaterialUBOBinding = 14;

using infernux::vkrender::MakeMultisampleState;
using infernux::vkrender::MakeShaderStageInfo;
using infernux::vkrender::MakeTriangleListInputAssembly;
using DynamicViewportState = infernux::vkrender::DynamicViewportScissorState;

struct MeshVertexInputState
{
    VkVertexInputBindingDescription bindingDesc{};
    std::vector<VkVertexInputAttributeDescription> attrDescs;
    VkPipelineVertexInputStateCreateInfo createInfo{};

    MeshVertexInputState()
        : bindingDesc(Vertex::getBindingDescription()),
          attrDescs(FilterVertexAttributesForReflection(ShaderReflection{}))
    {
        initCreateInfo();
    }

    explicit MeshVertexInputState(const ShaderReflection &vertexReflection)
        : bindingDesc(Vertex::getBindingDescription()), attrDescs(FilterVertexAttributesForReflection(vertexReflection))
    {
        initCreateInfo();
    }

    void initCreateInfo()
    {
        createInfo = {};
        createInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
        createInfo.vertexBindingDescriptionCount = attrDescs.empty() ? 0u : 1u;
        createInfo.pVertexBindingDescriptions = attrDescs.empty() ? nullptr : &bindingDesc;
        createInfo.vertexAttributeDescriptionCount = static_cast<uint32_t>(attrDescs.size());
        createInfo.pVertexAttributeDescriptions = attrDescs.empty() ? nullptr : attrDescs.data();
    }
};

VkPipelineRasterizationStateCreateInfo MakeRasterizationState(VkCullModeFlags cullMode)
{
    VkPipelineRasterizationStateCreateInfo raster{};
    raster.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
    raster.polygonMode = VK_POLYGON_MODE_FILL;
    raster.lineWidth = 1.0f;
    raster.cullMode = cullMode;
    raster.frontFace = VK_FRONT_FACE_CLOCKWISE;
    return raster;
}

VkPipelineDepthStencilStateCreateInfo MakeDepthStencilState(VkBool32 depthTestEnable, VkBool32 depthWriteEnable)
{
    VkPipelineDepthStencilStateCreateInfo depthStencil{};
    depthStencil.sType = VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
    depthStencil.depthTestEnable = depthTestEnable;
    depthStencil.depthWriteEnable = depthWriteEnable;
    return depthStencil;
}

VkPipelineColorBlendAttachmentState MakeOpaqueColorBlendAttachment()
{
    VkPipelineColorBlendAttachmentState colorBlendAttach{};
    colorBlendAttach.colorWriteMask =
        VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT | VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;
    colorBlendAttach.blendEnable = VK_FALSE;
    return colorBlendAttach;
}

VkPipelineColorBlendAttachmentState MakeAlphaBlendAttachment()
{
    VkPipelineColorBlendAttachmentState colorBlendAttach = MakeOpaqueColorBlendAttachment();
    colorBlendAttach.blendEnable = VK_TRUE;
    colorBlendAttach.srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;
    colorBlendAttach.dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    colorBlendAttach.colorBlendOp = VK_BLEND_OP_ADD;
    colorBlendAttach.srcAlphaBlendFactor = VK_BLEND_FACTOR_ZERO;
    colorBlendAttach.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
    colorBlendAttach.alphaBlendOp = VK_BLEND_OP_ADD;
    return colorBlendAttach;
}

VkPipelineColorBlendStateCreateInfo MakeColorBlendState(const VkPipelineColorBlendAttachmentState &attachment)
{
    VkPipelineColorBlendStateCreateInfo colorBlend{};
    colorBlend.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;
    colorBlend.attachmentCount = 1;
    colorBlend.pAttachments = &attachment;
    return colorBlend;
}

} // namespace

// ============================================================================
// Destructor
// ============================================================================

OutlineRenderer::~OutlineRenderer()
{
    Cleanup();
}

// ============================================================================
// Lifecycle
// ============================================================================

bool OutlineRenderer::Initialize(InxVkCoreModular *core, SceneRenderTarget *sceneTarget)
{
    if (m_resourcesReady)
        return true;

    if (!core || !sceneTarget || !sceneTarget->IsReady()) {
        INXLOG_WARN("OutlineRenderer::Initialize: core or SceneRenderTarget not ready");
        return false;
    }

    m_core = core;
    m_sceneRenderTarget = sceneTarget;

    // Check if outline shaders are loaded
    if (!m_core->HasShader("Outline Mask", "vertex") || !m_core->HasShader("Outline Mask", "fragment") ||
        !m_core->HasShader("Outline Composite", "vertex") || !m_core->HasShader("Outline Composite", "fragment")) {
        INXLOG_WARN("OutlineRenderer::Initialize: outline shaders not loaded yet");
        return false;
    }

    CreateOutlineMaskRenderPass();
    CreateOutlineCompositeRenderPass();
    CreateOutlineFramebuffers();
    CreateOutlineDescriptorResources();
    CreateOutlinePipelines();
    CreateOutlineMaterialResources();

    m_resourcesReady = true;
    return true;
}

void OutlineRenderer::Cleanup()
{
    VkDevice device = m_core ? m_core->GetDevice() : VK_NULL_HANDLE;
    if (device == VK_NULL_HANDLE)
        return;

    if (!m_core->IsShuttingDown()) {
        m_core->GetDeviceContext().WaitIdle();
    }

    vkrender::SafeDestroy(device, m_outlineMaskPipeline);
    vkrender::SafeDestroy(device, m_outlineMaskPipelineLayout);
    vkrender::SafeDestroy(device, m_outlineCompositePipeline);
    vkrender::SafeDestroy(device, m_outlineCompositePipelineLayout);
    vkrender::SafeDestroy(device, m_outlineMaskDescSetLayout);
    vkrender::SafeDestroy(device, m_outlineCompositeDescSetLayout);
    vkrender::SafeDestroy(device, m_outlineDescPool);

    // Per-material outline resources
    for (auto &[key, pipeline] : m_perMtlOutlinePipelines) {
        if (pipeline != VK_NULL_HANDLE)
            vkDestroyPipeline(device, pipeline, nullptr);
    }
    m_perMtlOutlinePipelines.clear();
    m_perMtlOutlineDescSets.clear(); // freed with pool below
    m_outlineGlobalsDescSets.clear();

    VmaAllocator allocator = m_core->GetDeviceContext().GetVmaAllocator();
    for (auto &instBuf : m_outlineInstanceBufs) {
        if (instBuf.buffer != VK_NULL_HANDLE)
            vmaDestroyBuffer(allocator, instBuf.buffer, instBuf.allocation);
    }
    m_outlineInstanceBufs.clear();
    for (auto &skinBuf : m_outlineSkinInstanceBufs) {
        if (skinBuf.buffer != VK_NULL_HANDLE)
            vmaDestroyBuffer(allocator, skinBuf.buffer, skinBuf.allocation);
    }
    m_outlineSkinInstanceBufs.clear();
    for (auto &skinBuf : m_outlineSkinPaletteBufs) {
        if (skinBuf.buffer != VK_NULL_HANDLE)
            vmaDestroyBuffer(allocator, skinBuf.buffer, skinBuf.allocation);
    }
    m_outlineSkinPaletteBufs.clear();
    for (auto &auxBuf : m_outlineInstanceAuxBufs) {
        if (auxBuf.buffer != VK_NULL_HANDLE)
            vmaDestroyBuffer(allocator, auxBuf.buffer, auxBuf.allocation);
    }
    m_outlineInstanceAuxBufs.clear();

    vkrender::SafeDestroy(device, m_outlineMtlDescPool);
    vkrender::SafeDestroy(device, m_outlineMtlPipelineLayout);
    vkrender::SafeDestroy(device, m_outlineMtlSet0Layout);
    vkrender::SafeDestroy(device, m_outlineMaskFramebuffer);
    vkrender::SafeDestroy(device, m_outlineCompositeFramebuffer);
    vkrender::SafeDestroy(device, m_outlineMaskRenderPass);
    vkrender::SafeDestroy(device, m_outlineCompositeRenderPass);

    m_outlineMaskDescSet = VK_NULL_HANDLE;
    m_outlineCompositeDescSet = VK_NULL_HANDLE;
    m_resourcesReady = false;
}

void OutlineRenderer::OnResize(uint32_t width, uint32_t height)
{
    if (!m_resourcesReady)
        return;

    VkDevice device = m_core->GetDevice();
    m_core->GetDeviceContext().WaitIdle();

    // Destroy old framebuffers
    if (m_outlineMaskFramebuffer != VK_NULL_HANDLE) {
        vkDestroyFramebuffer(device, m_outlineMaskFramebuffer, nullptr);
        m_outlineMaskFramebuffer = VK_NULL_HANDLE;
    }
    if (m_outlineCompositeFramebuffer != VK_NULL_HANDLE) {
        vkDestroyFramebuffer(device, m_outlineCompositeFramebuffer, nullptr);
        m_outlineCompositeFramebuffer = VK_NULL_HANDLE;
    }

    CreateOutlineFramebuffers();

    // Update composite descriptor set with new mask image view
    VkDescriptorImageInfo imageInfo{};
    imageInfo.sampler = m_sceneRenderTarget->GetOutlineMaskSampler();
    imageInfo.imageView = m_sceneRenderTarget->GetOutlineMaskImageView();
    imageInfo.imageLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    vkrender::UpdateDescriptorSetWithImage(device, m_outlineCompositeDescSet, 0, imageInfo);
}

// ============================================================================
// Rendering
// ============================================================================

bool OutlineRenderer::RecordCommands(VkCommandBuffer cmdBuf, const std::vector<DrawCall> &drawCalls)
{
    if (!m_resourcesReady || !HasActiveOutline() || !m_sceneRenderTarget)
        return false;

    RenderOutlineMask(cmdBuf, drawCalls);
    RenderOutlineComposite(cmdBuf);
    return true;
}

void OutlineRenderer::RecordNoOutlineBarrier(VkCommandBuffer cmdBuf)
{
    (void)cmdBuf;
}

// ============================================================================
// Internal: Vulkan Resource Creation
// ============================================================================

void OutlineRenderer::CreateOutlineMaskRenderPass()
{
    // Single color attachment: mask (R8G8B8A8_UNORM, clear to black, store for later sampling).
    // No depth attachment — the SceneRenderTarget depth is NOT shared with the scene RenderGraph
    // (it creates a transient depth internally), so we cannot do occlusion testing.
    // This matches Blender behavior: selection outline is always visible (X-ray).
    VkAttachmentDescription colorAttachment{};
    colorAttachment.format = VK_FORMAT_R8G8B8A8_UNORM;
    colorAttachment.samples = VK_SAMPLE_COUNT_1_BIT;
    colorAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    colorAttachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    colorAttachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    colorAttachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    colorAttachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    colorAttachment.finalLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;

    VkAttachmentReference colorRef{};
    colorRef.attachment = 0;
    colorRef.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = 1;
    subpass.pColorAttachments = &colorRef;
    subpass.pDepthStencilAttachment = nullptr; // No depth

    VkSubpassDependency dependency{};
    dependency.srcSubpass = VK_SUBPASS_EXTERNAL;
    dependency.dstSubpass = 0;
    dependency.srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependency.dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependency.srcAccessMask = 0;
    dependency.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;

    VkRenderPassCreateInfo rpInfo{};
    rpInfo.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
    rpInfo.attachmentCount = 1;
    rpInfo.pAttachments = &colorAttachment;
    rpInfo.subpassCount = 1;
    rpInfo.pSubpasses = &subpass;
    rpInfo.dependencyCount = 1;
    rpInfo.pDependencies = &dependency;

    if (vkCreateRenderPass(m_core->GetDevice(), &rpInfo, nullptr, &m_outlineMaskRenderPass) != VK_SUCCESS) {
        INXLOG_ERROR("OutlineRenderer: Failed to create outline mask render pass");
    }
}

void OutlineRenderer::CreateOutlineCompositeRenderPass()
{
    // Single color attachment: scene color (load existing, alpha-blend outline on top)
    // Must match SceneRenderTarget HDR color format (R16G16B16A16_SFLOAT).
    VkAttachmentDescription colorAttachment{};
    colorAttachment.format = VK_FORMAT_R16G16B16A16_SFLOAT;
    colorAttachment.samples = VK_SAMPLE_COUNT_1_BIT;
    colorAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_LOAD; // Preserve scene color
    colorAttachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    colorAttachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    colorAttachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    colorAttachment.initialLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    colorAttachment.finalLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;

    VkAttachmentReference colorRef{};
    colorRef.attachment = 0;
    colorRef.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = 1;
    subpass.pColorAttachments = &colorRef;

    std::array<VkSubpassDependency, 2> dependencies{};

    // Synchronize both scene-color reuse and outline-mask sampling at pass begin.
    dependencies[0].srcSubpass = VK_SUBPASS_EXTERNAL;
    dependencies[0].dstSubpass = 0;
    dependencies[0].srcStageMask = VK_PIPELINE_STAGE_TRANSFER_BIT | VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT |
                                   VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependencies[0].dstStageMask =
        VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT | VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependencies[0].srcAccessMask =
        VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    dependencies[0].dstAccessMask =
        VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    dependencies[0].dependencyFlags = VK_DEPENDENCY_BY_REGION_BIT;

    // Make the composited scene image visible to downstream sampling (ImGui).
    dependencies[1].srcSubpass = 0;
    dependencies[1].dstSubpass = VK_SUBPASS_EXTERNAL;
    dependencies[1].srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependencies[1].dstStageMask = VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT;
    dependencies[1].srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    dependencies[1].dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
    dependencies[1].dependencyFlags = VK_DEPENDENCY_BY_REGION_BIT;

    VkRenderPassCreateInfo rpInfo{};
    rpInfo.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
    rpInfo.attachmentCount = 1;
    rpInfo.pAttachments = &colorAttachment;
    rpInfo.subpassCount = 1;
    rpInfo.pSubpasses = &subpass;
    rpInfo.dependencyCount = static_cast<uint32_t>(dependencies.size());
    rpInfo.pDependencies = dependencies.data();

    if (vkCreateRenderPass(m_core->GetDevice(), &rpInfo, nullptr, &m_outlineCompositeRenderPass) != VK_SUCCESS) {
        INXLOG_ERROR("OutlineRenderer: Failed to create outline composite render pass");
    }
}

void OutlineRenderer::CreateOutlineFramebuffers()
{
    uint32_t w = m_sceneRenderTarget->GetWidth();
    uint32_t h = m_sceneRenderTarget->GetHeight();

    // Mask framebuffer: mask color only (no depth — always-visible outline)
    {
        VkImageView attachment = m_sceneRenderTarget->GetOutlineMaskImageView();

        VkFramebufferCreateInfo fbInfo{};
        fbInfo.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
        fbInfo.renderPass = m_outlineMaskRenderPass;
        fbInfo.attachmentCount = 1;
        fbInfo.pAttachments = &attachment;
        fbInfo.width = w;
        fbInfo.height = h;
        fbInfo.layers = 1;

        if (vkCreateFramebuffer(m_core->GetDevice(), &fbInfo, nullptr, &m_outlineMaskFramebuffer) != VK_SUCCESS) {
            INXLOG_ERROR("OutlineRenderer: Failed to create outline mask framebuffer");
        }
    }

    // Composite framebuffer: scene color
    {
        VkImageView attachment = m_sceneRenderTarget->GetColorImageView();

        VkFramebufferCreateInfo fbInfo{};
        fbInfo.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
        fbInfo.renderPass = m_outlineCompositeRenderPass;
        fbInfo.attachmentCount = 1;
        fbInfo.pAttachments = &attachment;
        fbInfo.width = w;
        fbInfo.height = h;
        fbInfo.layers = 1;

        if (vkCreateFramebuffer(m_core->GetDevice(), &fbInfo, nullptr, &m_outlineCompositeFramebuffer) != VK_SUCCESS) {
            INXLOG_ERROR("OutlineRenderer: Failed to create outline composite framebuffer");
        }
    }
}

void OutlineRenderer::CreateOutlineDescriptorResources()
{
    VkDevice device = m_core->GetDevice();

    // --- Descriptor pool (2 sets: mask UBO + composite sampler) ---
    std::array<VkDescriptorPoolSize, 2> poolSizes{};
    poolSizes[0].type = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    poolSizes[0].descriptorCount = 1;
    poolSizes[1].type = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    poolSizes[1].descriptorCount = 1;

    VkDescriptorPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
    poolInfo.poolSizeCount = static_cast<uint32_t>(poolSizes.size());
    poolInfo.pPoolSizes = poolSizes.data();
    poolInfo.maxSets = 2;

    if (vkCreateDescriptorPool(device, &poolInfo, nullptr, &m_outlineDescPool) != VK_SUCCESS) {
        INXLOG_ERROR("OutlineRenderer: Failed to create outline descriptor pool");
        return;
    }

    // --- Mask descriptor set layout: binding 0 = UBO (scene VP matrices) ---
    {
        const VkDescriptorSetLayoutBinding uboBinding = vkrender::MakeDescriptorSetLayoutBinding(
            kOutlineSceneUBOBinding, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, VK_SHADER_STAGE_VERTEX_BIT);

        vkrender::CreateDescriptorSetLayout(device, &uboBinding, 1, m_outlineMaskDescSetLayout);
        vkrender::AllocateDescriptorSet(device, m_outlineDescPool, m_outlineMaskDescSetLayout, m_outlineMaskDescSet);

        // Write scene UBO to binding 0
        VkDescriptorBufferInfo bufferInfo{};
        bufferInfo.buffer = m_core->GetSceneUbo();
        bufferInfo.offset = 0;
        bufferInfo.range = sizeof(UniformBufferObject);
        vkrender::UpdateDescriptorSetWithBuffer(device, m_outlineMaskDescSet, kOutlineSceneUBOBinding,
                                                VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, bufferInfo);
    }

    // --- Composite descriptor set layout: binding 0 = sampler (mask texture) ---
    {
        const VkDescriptorSetLayoutBinding samplerBinding = vkrender::MakeDescriptorSetLayoutBinding(
            0, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, VK_SHADER_STAGE_FRAGMENT_BIT);

        vkrender::CreateDescriptorSetLayout(device, &samplerBinding, 1, m_outlineCompositeDescSetLayout);
        vkrender::AllocateDescriptorSet(device, m_outlineDescPool, m_outlineCompositeDescSetLayout,
                                        m_outlineCompositeDescSet);

        // Write mask texture to binding 0
        VkDescriptorImageInfo imageInfo{};
        imageInfo.sampler = m_sceneRenderTarget->GetOutlineMaskSampler();
        imageInfo.imageView = m_sceneRenderTarget->GetOutlineMaskImageView();
        imageInfo.imageLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        vkrender::UpdateDescriptorSetWithImage(device, m_outlineCompositeDescSet, 0, imageInfo);
    }
}

void OutlineRenderer::CreateOutlinePipelines()
{
    VkDevice device = m_core->GetDevice();

    // ========================================================================
    // Mask Pipeline — renders selected object as white silhouette
    // ========================================================================
    {
        // Pipeline layout: 1 descriptor set (UBO at binding 0) + push constants (128 bytes, vertex)
        VkPushConstantRange pushRange{};
        pushRange.stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
        pushRange.offset = 0;
        pushRange.size = 128; // 2 x mat4

        VkPipelineLayoutCreateInfo layoutInfo{};
        layoutInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
        layoutInfo.setLayoutCount = 1;
        layoutInfo.pSetLayouts = &m_outlineMaskDescSetLayout;
        layoutInfo.pushConstantRangeCount = 1;
        layoutInfo.pPushConstantRanges = &pushRange;

        vkCreatePipelineLayout(device, &layoutInfo, nullptr, &m_outlineMaskPipelineLayout);

        std::array<VkPipelineShaderStageCreateInfo, 2> stages = {
            MakeShaderStageInfo(VK_SHADER_STAGE_VERTEX_BIT, m_core->GetShaderModule("Outline Mask", "vertex")),
            MakeShaderStageInfo(VK_SHADER_STAGE_FRAGMENT_BIT, m_core->GetShaderModule("Outline Mask", "fragment")),
        };

        ShaderReflection outlineMaskVertRefl;
        const auto *outlineMaskVertSpv = m_core->GetShaderCache().FindVertCode("Outline Mask");
        if (!outlineMaskVertSpv || !outlineMaskVertRefl.Reflect(*outlineMaskVertSpv, VK_SHADER_STAGE_VERTEX_BIT)) {
            outlineMaskVertRefl.Clear();
        }

        m_outlineMaskPipeline = CreateMaskPipeline(stages.data(), m_outlineMaskPipelineLayout, outlineMaskVertRefl);
        if (m_outlineMaskPipeline == VK_NULL_HANDLE) {
            INXLOG_ERROR("OutlineRenderer: Failed to create outline mask pipeline");
        }
    }

    // ========================================================================
    // Composite Pipeline — fullscreen edge detection + alpha blend
    // ========================================================================
    {
        // Pipeline layout: 1 descriptor set (sampler at binding 0) + push constants (32 bytes, fragment)
        VkPushConstantRange pushRange{};
        pushRange.stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
        pushRange.offset = 0;
        pushRange.size = 32; // vec4 color + vec2 texelSize + float width + float padding

        VkPipelineLayoutCreateInfo layoutInfo{};
        layoutInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
        layoutInfo.setLayoutCount = 1;
        layoutInfo.pSetLayouts = &m_outlineCompositeDescSetLayout;
        layoutInfo.pushConstantRangeCount = 1;
        layoutInfo.pPushConstantRanges = &pushRange;

        vkCreatePipelineLayout(device, &layoutInfo, nullptr, &m_outlineCompositePipelineLayout);

        std::array<VkPipelineShaderStageCreateInfo, 2> stages = {
            MakeShaderStageInfo(VK_SHADER_STAGE_VERTEX_BIT, m_core->GetShaderModule("Outline Composite", "vertex")),
            MakeShaderStageInfo(VK_SHADER_STAGE_FRAGMENT_BIT, m_core->GetShaderModule("Outline Composite", "fragment")),
        };

        // No vertex input (fullscreen triangle is procedural)
        VkPipelineVertexInputStateCreateInfo vertexInput{};
        vertexInput.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;

        VkPipelineInputAssemblyStateCreateInfo inputAssembly = MakeTriangleListInputAssembly();
        DynamicViewportState viewportState;
        VkPipelineRasterizationStateCreateInfo raster = MakeRasterizationState(VK_CULL_MODE_NONE);
        VkPipelineDepthStencilStateCreateInfo depthStencil = MakeDepthStencilState(VK_FALSE, VK_FALSE);
        VkPipelineMultisampleStateCreateInfo multisampling = MakeMultisampleState(VK_SAMPLE_COUNT_1_BIT);
        VkPipelineColorBlendAttachmentState colorBlendAttach = MakeAlphaBlendAttachment();
        VkPipelineColorBlendStateCreateInfo colorBlend = MakeColorBlendState(colorBlendAttach);

        VkGraphicsPipelineCreateInfo pipelineInfo{};
        pipelineInfo.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
        pipelineInfo.stageCount = static_cast<uint32_t>(stages.size());
        pipelineInfo.pStages = stages.data();
        pipelineInfo.pVertexInputState = &vertexInput;
        pipelineInfo.pInputAssemblyState = &inputAssembly;
        pipelineInfo.pViewportState = &viewportState.viewportState;
        pipelineInfo.pRasterizationState = &raster;
        pipelineInfo.pMultisampleState = &multisampling;
        pipelineInfo.pDepthStencilState = &depthStencil;
        pipelineInfo.pColorBlendState = &colorBlend;
        pipelineInfo.pDynamicState = &viewportState.dynamicState;
        pipelineInfo.layout = m_outlineCompositePipelineLayout;
        pipelineInfo.renderPass = m_outlineCompositeRenderPass;
        pipelineInfo.subpass = 0;

        if (vkCreateGraphicsPipelines(device, VK_NULL_HANDLE, 1, &pipelineInfo, nullptr, &m_outlineCompositePipeline) !=
            VK_SUCCESS) {
            INXLOG_ERROR("OutlineRenderer: Failed to create outline composite pipeline");
        }
    }
}

// ============================================================================
// Per-material outline mask pipeline resources
// ============================================================================

void OutlineRenderer::CreateOutlineMaterialResources()
{
    VkDevice device = m_core->GetDevice();
    VmaAllocator allocator = m_core->GetDeviceContext().GetVmaAllocator();
    uint32_t framesInFlight = m_core->GetMaxFramesInFlight();

    // --- Set 0 layout: binding 0 (scene UBO, vertex) + vertex material UBO ---
    {
        std::array<VkDescriptorSetLayoutBinding, 2> bindings = {
            vkrender::MakeDescriptorSetLayoutBinding(kOutlineSceneUBOBinding, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER,
                                                     VK_SHADER_STAGE_VERTEX_BIT),
            vkrender::MakeDescriptorSetLayoutBinding(kOutlineVertexMaterialUBOBinding,
                                                     VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, VK_SHADER_STAGE_VERTEX_BIT),
        };

        vkrender::CreateDescriptorSetLayout(device, bindings.data(), static_cast<uint32_t>(bindings.size()),
                                            m_outlineMtlSet0Layout);
    }

    // --- Pipeline layout: [set0, active camera view, globalsSet2] + push constants ---
    {
        VkPushConstantRange pushRange{};
        pushRange.stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
        pushRange.offset = 0;
        pushRange.size = 128; // 2 x mat4

        VkDescriptorSetLayout setLayouts[3] = {m_outlineMtlSet0Layout, m_core->GetPerViewDescSetLayout(),
                                               m_core->GetGlobalsDescSetLayout()};

        VkPipelineLayoutCreateInfo layoutInfo{};
        layoutInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
        layoutInfo.setLayoutCount = 3;
        layoutInfo.pSetLayouts = setLayouts;
        layoutInfo.pushConstantRangeCount = 1;
        layoutInfo.pPushConstantRanges = &pushRange;

        vkCreatePipelineLayout(device, &layoutInfo, nullptr, &m_outlineMtlPipelineLayout);
    }

    // --- Descriptor pool (per-material set 0 + per-frame globals set 2) ---
    {
        VkDescriptorPoolSize poolSizes[2]{};
        poolSizes[0].type = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
        poolSizes[0].descriptorCount = 64 + framesInFlight; // 32 materials × 2 bindings + globals UBOs
        poolSizes[1].type = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        // Globals set 2 owns instance, skin metadata, skin palette, and particle buffers per frame.
        poolSizes[1].descriptorCount = framesInFlight * 4;

        VkDescriptorPoolCreateInfo poolInfo{};
        poolInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
        poolInfo.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
        poolInfo.maxSets = 32 + framesInFlight;
        poolInfo.poolSizeCount = 2;
        poolInfo.pPoolSizes = poolSizes;

        vkCreateDescriptorPool(device, &poolInfo, nullptr, &m_outlineMtlDescPool);
    }

    // --- Per-frame outline instance buffers (1 mat4 each, host-visible) ---
    m_outlineInstanceBufs.resize(framesInFlight);
    m_outlineSkinInstanceBufs.resize(framesInFlight);
    m_outlineSkinPaletteBufs.resize(framesInFlight);
    m_outlineInstanceAuxBufs.resize(framesInFlight);
    for (uint32_t i = 0; i < framesInFlight; ++i) {
        VkBufferCreateInfo bufInfo{};
        bufInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
        bufInfo.size = sizeof(glm::mat4);
        bufInfo.usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
        bufInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;

        VmaAllocationCreateInfo allocCreateInfo{};
        allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO;
        allocCreateInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT;
        allocCreateInfo.requiredFlags = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;

        VmaAllocationInfo vmaAllocInfo{};
        vmaCreateBuffer(allocator, &bufInfo, &allocCreateInfo, &m_outlineInstanceBufs[i].buffer,
                        &m_outlineInstanceBufs[i].allocation, &vmaAllocInfo);
        m_outlineInstanceBufs[i].mapped = vmaAllocInfo.pMappedData;

        // Write identity as initial value
        glm::mat4 identity(1.0f);
        std::memcpy(m_outlineInstanceBufs[i].mapped, &identity, sizeof(glm::mat4));

        bufInfo.size = sizeof(GPUInstanceAuxData);
        VmaAllocationInfo auxAllocationInfo{};
        vmaCreateBuffer(allocator, &bufInfo, &allocCreateInfo, &m_outlineInstanceAuxBufs[i].buffer,
                        &m_outlineInstanceAuxBufs[i].allocation, &auxAllocationInfo);
        m_outlineInstanceAuxBufs[i].mapped = auxAllocationInfo.pMappedData;
        if (m_outlineInstanceAuxBufs[i].mapped) {
            GPUInstanceAuxData aux{};
            aux.previousModel = identity;
            aux.layerMask = ~0u;
            std::memcpy(m_outlineInstanceAuxBufs[i].mapped, &aux, sizeof(aux));
        }

        EnsureOutlineSkinBufferCapacity(i, 1);
    }

    // --- Per-frame outline globals descriptor sets ---
    {
        VkDescriptorSetLayout globalsLayout = m_core->GetGlobalsDescSetLayout();
        std::vector<VkDescriptorSetLayout> layouts(framesInFlight, globalsLayout);

        VkDescriptorSetAllocateInfo allocInfo{};
        allocInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
        allocInfo.descriptorPool = m_outlineMtlDescPool;
        allocInfo.descriptorSetCount = framesInFlight;
        allocInfo.pSetLayouts = layouts.data();

        m_outlineGlobalsDescSets.resize(framesInFlight);
        vkAllocateDescriptorSets(device, &allocInfo, m_outlineGlobalsDescSets.data());

        for (uint32_t i = 0; i < framesInFlight; ++i) {
            // Binding 0: globals UBO (same as engine frame i)
            VkDescriptorBufferInfo uboBufInfo{};
            uboBufInfo.buffer = m_core->GetGlobalsBuffer(i);
            uboBufInfo.offset = 0;
            uboBufInfo.range = VK_WHOLE_SIZE;
            vkrender::UpdateDescriptorSetWithBuffer(device, m_outlineGlobalsDescSets[i], 0,
                                                    VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, uboBufInfo);

            // Binding 1: outline instance buffer (1 mat4)
            VkDescriptorBufferInfo ssboBufInfo{};
            ssboBufInfo.buffer = m_outlineInstanceBufs[i].buffer;
            ssboBufInfo.offset = 0;
            ssboBufInfo.range = sizeof(glm::mat4);
            vkrender::UpdateDescriptorSetWithBuffer(device, m_outlineGlobalsDescSets[i], 1,
                                                    VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, ssboBufInfo);

            if (i < m_outlineSkinInstanceBufs.size() && i < m_outlineSkinPaletteBufs.size()) {
                if (m_outlineSkinInstanceBufs[i].buffer != VK_NULL_HANDLE) {
                    VkDescriptorBufferInfo skinInstInfo{};
                    skinInstInfo.buffer = m_outlineSkinInstanceBufs[i].buffer;
                    skinInstInfo.offset = 0;
                    skinInstInfo.range = VK_WHOLE_SIZE;
                    vkrender::UpdateDescriptorSetWithBuffer(device, m_outlineGlobalsDescSets[i], 2,
                                                            VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, skinInstInfo);
                }

                if (m_outlineSkinPaletteBufs[i].buffer != VK_NULL_HANDLE) {
                    VkDescriptorBufferInfo skinPaletteInfo{};
                    skinPaletteInfo.buffer = m_outlineSkinPaletteBufs[i].buffer;
                    skinPaletteInfo.offset = 0;
                    skinPaletteInfo.range = VK_WHOLE_SIZE;
                    vkrender::UpdateDescriptorSetWithBuffer(device, m_outlineGlobalsDescSets[i], 3,
                                                            VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, skinPaletteInfo);
                }
            }

            if (i < m_outlineInstanceAuxBufs.size() && m_outlineInstanceAuxBufs[i].buffer != VK_NULL_HANDLE) {
                VkDescriptorBufferInfo instanceAuxInfo{};
                instanceAuxInfo.buffer = m_outlineInstanceAuxBufs[i].buffer;
                instanceAuxInfo.offset = 0;
                instanceAuxInfo.range = sizeof(GPUInstanceAuxData);
                vkrender::UpdateDescriptorSetWithBuffer(device, m_outlineGlobalsDescSets[i], 4,
                                                        VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, instanceAuxInfo);
            }
        }
    }
}

void OutlineRenderer::EnsureOutlineSkinBufferCapacity(uint32_t frameIndex, size_t boneMatrixCount)
{
    if (!m_core || frameIndex >= m_outlineSkinInstanceBufs.size() || frameIndex >= m_outlineSkinPaletteBufs.size())
        return;

    VkDevice device = m_core->GetDevice();
    VmaAllocator allocator = m_core->GetDeviceContext().GetVmaAllocator();
    if (device == VK_NULL_HANDLE || allocator == VK_NULL_HANDLE)
        return;

    auto createStorageBuffer = [&](OutlineSkinBuf &buf, size_t elementCount, size_t elementSize) {
        const VkDeviceSize byteSize = static_cast<VkDeviceSize>(std::max<size_t>(1, elementCount) * elementSize);
        VkBufferCreateInfo bufInfo{};
        bufInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
        bufInfo.size = byteSize;
        bufInfo.usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
        bufInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;

        VmaAllocationCreateInfo allocCreateInfo{};
        allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO;
        allocCreateInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT;
        allocCreateInfo.requiredFlags = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;

        VmaAllocationInfo allocInfo{};
        if (vmaCreateBuffer(allocator, &bufInfo, &allocCreateInfo, &buf.buffer, &buf.allocation, &allocInfo) ==
            VK_SUCCESS) {
            buf.mapped = allocInfo.pMappedData;
            buf.capacity = elementCount;
        }
    };

    auto &skinInstance = m_outlineSkinInstanceBufs[frameIndex];
    if (skinInstance.buffer == VK_NULL_HANDLE)
        createStorageBuffer(skinInstance, 1, sizeof(GPUSkinInstanceData));

    auto &skinPalette = m_outlineSkinPaletteBufs[frameIndex];
    const size_t requiredBones = std::max<size_t>(1, boneMatrixCount);
    if (skinPalette.buffer == VK_NULL_HANDLE || skinPalette.capacity < requiredBones) {
        if (skinPalette.buffer != VK_NULL_HANDLE)
            vmaDestroyBuffer(allocator, skinPalette.buffer, skinPalette.allocation);
        skinPalette = OutlineSkinBuf{};
        createStorageBuffer(skinPalette, std::max<size_t>(requiredBones, 64), sizeof(glm::mat4));
    }

    if (frameIndex < m_outlineGlobalsDescSets.size() && m_outlineGlobalsDescSets[frameIndex] != VK_NULL_HANDLE) {
        if (skinInstance.buffer != VK_NULL_HANDLE) {
            VkDescriptorBufferInfo skinInstInfo{};
            skinInstInfo.buffer = skinInstance.buffer;
            skinInstInfo.offset = 0;
            skinInstInfo.range = VK_WHOLE_SIZE;
            vkrender::UpdateDescriptorSetWithBuffer(device, m_outlineGlobalsDescSets[frameIndex], 2,
                                                    VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, skinInstInfo);
        }

        if (skinPalette.buffer != VK_NULL_HANDLE) {
            VkDescriptorBufferInfo skinPaletteInfo{};
            skinPaletteInfo.buffer = skinPalette.buffer;
            skinPaletteInfo.offset = 0;
            skinPaletteInfo.range = VK_WHOLE_SIZE;
            vkrender::UpdateDescriptorSetWithBuffer(device, m_outlineGlobalsDescSets[frameIndex], 3,
                                                    VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, skinPaletteInfo);
        }
    }
}

VkPipeline OutlineRenderer::CreateMaskPipeline(const VkPipelineShaderStageCreateInfo stages[2], VkPipelineLayout layout,
                                               const ShaderReflection &vertexReflection)
{
    MeshVertexInputState vertexInput(vertexReflection);
    VkPipelineInputAssemblyStateCreateInfo inputAssembly = MakeTriangleListInputAssembly();
    DynamicViewportState viewportState;
    VkPipelineRasterizationStateCreateInfo raster = MakeRasterizationState(VK_CULL_MODE_NONE);
    VkPipelineDepthStencilStateCreateInfo depthStencil = MakeDepthStencilState(VK_FALSE, VK_FALSE);
    VkPipelineMultisampleStateCreateInfo multisampling = MakeMultisampleState(VK_SAMPLE_COUNT_1_BIT);
    VkPipelineColorBlendAttachmentState colorBlendAttach = MakeOpaqueColorBlendAttachment();
    VkPipelineColorBlendStateCreateInfo colorBlend = MakeColorBlendState(colorBlendAttach);

    VkGraphicsPipelineCreateInfo pipelineInfo{};
    pipelineInfo.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
    pipelineInfo.stageCount = 2;
    pipelineInfo.pStages = stages;
    pipelineInfo.pVertexInputState = &vertexInput.createInfo;
    pipelineInfo.pInputAssemblyState = &inputAssembly;
    pipelineInfo.pViewportState = &viewportState.viewportState;
    pipelineInfo.pRasterizationState = &raster;
    pipelineInfo.pMultisampleState = &multisampling;
    pipelineInfo.pDepthStencilState = &depthStencil;
    pipelineInfo.pColorBlendState = &colorBlend;
    pipelineInfo.pDynamicState = &viewportState.dynamicState;
    pipelineInfo.layout = layout;
    pipelineInfo.renderPass = m_outlineMaskRenderPass;
    pipelineInfo.subpass = 0;

    VkPipeline pipeline = VK_NULL_HANDLE;
    vkCreateGraphicsPipelines(m_core->GetDevice(), VK_NULL_HANDLE, 1, &pipelineInfo, nullptr, &pipeline);
    return pipeline;
}

VkPipeline OutlineRenderer::GetOrCreateMtlOutlinePipeline(InxMaterial *material)
{
    std::string key = material->GetMaterialKey();
    if (key.empty())
        key = material->GetName();

    auto it = m_perMtlOutlinePipelines.find(key);
    if (it != m_perMtlOutlinePipelines.end())
        return it->second;

    ShaderProgram *program = material->GetPassShaderProgram(ShaderCompileTarget::Forward);
    if (!program)
        return VK_NULL_HANDLE;

    VkShaderModule vertModule = program->GetVertexModule();
    VkShaderModule fragModule = m_core->GetShaderModule("Outline Mask", "fragment");
    if (vertModule == VK_NULL_HANDLE || fragModule == VK_NULL_HANDLE)
        return VK_NULL_HANDLE;

    VkDevice device = m_core->GetDevice();

    std::array<VkPipelineShaderStageCreateInfo, 2> stages = {
        MakeShaderStageInfo(VK_SHADER_STAGE_VERTEX_BIT, vertModule),
        MakeShaderStageInfo(VK_SHADER_STAGE_FRAGMENT_BIT, fragModule),
    };

    VkPipeline pipeline = CreateMaskPipeline(stages.data(), m_outlineMtlPipelineLayout, program->GetVertexReflection());
    if (pipeline == VK_NULL_HANDLE) {
        INXLOG_WARN("OutlineRenderer: Failed to create per-material outline pipeline for '", material->GetName(), "'");
        return VK_NULL_HANDLE;
    }

    m_perMtlOutlinePipelines[key] = pipeline;
    INXLOG_DEBUG("OutlineRenderer: Created per-material outline pipeline for '", material->GetName(), "'");
    return pipeline;
}

VkDescriptorSet OutlineRenderer::GetOrCreateMtlOutlineDescSet(InxMaterial *material)
{
    std::string key = material->GetMaterialKey();
    if (key.empty())
        key = material->GetName();

    auto it = m_perMtlOutlineDescSets.find(key);
    if (it != m_perMtlOutlineDescSets.end())
        return it->second;

    // Get forward render data to access the vertex material UBO buffer when the shader uses one.
    // Skin-only materials still need a valid set 0 so they can use the real vertex shader with set 2 skin data.
    MaterialRenderData *renderData = m_core->GetMaterialPipelineManager().GetRenderData(key);

    VkDevice device = m_core->GetDevice();

    VkDescriptorSet descSet = VK_NULL_HANDLE;
    if (!vkrender::AllocateDescriptorSet(device, m_outlineMtlDescPool, m_outlineMtlSet0Layout, descSet)) {
        INXLOG_WARN("OutlineRenderer: Failed to allocate per-material outline descriptor set");
        return VK_NULL_HANDLE;
    }

    // Binding 0: scene UBO (same as the fixed outline mask)
    VkDescriptorBufferInfo sceneBufInfo{};
    sceneBufInfo.buffer = m_core->GetSceneUbo();
    sceneBufInfo.offset = 0;
    sceneBufInfo.range = sizeof(UniformBufferObject);
    vkrender::UpdateDescriptorSetWithBuffer(device, descSet, kOutlineSceneUBOBinding, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER,
                                            sceneBufInfo);

    // Vertex material UBO. Bind a harmless fallback buffer for shaders that do not declare/use this binding;
    // the set layout still requires a valid descriptor, and skinned outlines must not fall back to the fixed path.
    VkDescriptorBufferInfo vertMatBufInfo{};
    if (renderData && renderData->materialDescSet && renderData->materialDescSet->vertexMaterialUBO &&
        renderData->materialDescSet->vertexMaterialUBO->IsValid()) {
        vertMatBufInfo.buffer = renderData->materialDescSet->vertexMaterialUBO->GetBuffer();
        vertMatBufInfo.offset = 0;
        vertMatBufInfo.range = renderData->materialDescSet->vertexMaterialUBO->GetSize();
    } else {
        vertMatBufInfo.buffer = m_core->GetSceneUbo();
        vertMatBufInfo.offset = 0;
        vertMatBufInfo.range = sizeof(UniformBufferObject);
    }
    vkrender::UpdateDescriptorSetWithBuffer(device, descSet, kOutlineVertexMaterialUBOBinding,
                                            VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, vertMatBufInfo);

    m_perMtlOutlineDescSets[key] = descSet;
    return descSet;
}

// ============================================================================
// Internal: Shared render-pass begin helper
// ============================================================================

void OutlineRenderer::BeginRenderPassWithFullViewport(VkCommandBuffer cmdBuf, VkRenderPass rp, VkFramebuffer fb,
                                                      const VkClearValue &clearVal)
{
    uint32_t w = m_sceneRenderTarget->GetWidth();
    uint32_t h = m_sceneRenderTarget->GetHeight();

    VkRenderPassBeginInfo rpBegin{};
    rpBegin.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
    rpBegin.renderPass = rp;
    rpBegin.framebuffer = fb;
    rpBegin.renderArea.offset = {0, 0};
    rpBegin.renderArea.extent = {w, h};
    rpBegin.clearValueCount = 1;
    rpBegin.pClearValues = &clearVal;

    vkCmdBeginRenderPass(cmdBuf, &rpBegin, VK_SUBPASS_CONTENTS_INLINE);

    VkViewport viewport{};
    viewport.x = 0.0f;
    viewport.y = 0.0f;
    viewport.width = static_cast<float>(w);
    viewport.height = static_cast<float>(h);
    viewport.minDepth = 0.0f;
    viewport.maxDepth = 1.0f;
    vkCmdSetViewport(cmdBuf, 0, 1, &viewport);

    VkRect2D scissor{};
    scissor.offset = {0, 0};
    scissor.extent = {w, h};
    vkCmdSetScissor(cmdBuf, 0, 1, &scissor);
}

// ============================================================================
// Internal: Mask Pass
// ============================================================================

void OutlineRenderer::RenderOutlineMask(VkCommandBuffer cmdBuf, const std::vector<DrawCall> &drawCalls)
{
    uint32_t w = m_sceneRenderTarget->GetWidth();
    uint32_t h = m_sceneRenderTarget->GetHeight();
    if (m_outlineInstanceBufs.empty())
        return;

    uint32_t frameIdx = m_core->GetSwapchain().GetCurrentFrame() % static_cast<uint32_t>(m_outlineInstanceBufs.size());
    size_t maxSelectedBones = 1;
    for (const auto &dc : drawCalls) {
        if (IsOutlinedObject(dc.objectId) && dc.skinBoneMatrices)
            maxSelectedBones = std::max(maxSelectedBones, dc.skinBoneMatrices->size());
    }
    EnsureOutlineSkinBufferCapacity(frameIdx, maxSelectedBones);

    VkClearValue clearValue{};
    clearValue.color = {{0.0f, 0.0f, 0.0f, 0.0f}};
    BeginRenderPassWithFullViewport(cmdBuf, m_outlineMaskRenderPass, m_outlineMaskFramebuffer, clearValue);

    // Render the selected object
    for (const auto &dc : drawCalls) {
        if (!IsOutlinedObject(dc.objectId))
            continue;

        // Get per-object buffer via core accessor
        VkBuffer vertBuf = m_core->GetObjectVertexBuffer(dc.objectId);
        VkBuffer idxBuf = m_core->GetObjectIndexBuffer(dc.objectId);

        if (vertBuf == VK_NULL_HANDLE || idxBuf == VK_NULL_HANDLE)
            continue;

        VkBuffer vertBuffers[] = {vertBuf};
        VkDeviceSize offsets[] = {0};
        vkCmdBindVertexBuffers(cmdBuf, 0, 1, vertBuffers, offsets);
        vkCmdBindIndexBuffer(cmdBuf, idxBuf, 0, VK_INDEX_TYPE_UINT32);

        // Push per-object model matrix + normal matrix
        struct PushConstants
        {
            glm::mat4 model;
            glm::mat4 normalMat;
        };

        PushConstants pushData;
        pushData.model = dc.worldMatrix;
        glm::mat3 normalMat3 = glm::transpose(glm::inverse(glm::mat3(dc.worldMatrix)));
        pushData.normalMat = glm::mat4(normalMat3);

        // Check if the material has a custom vertex shader with vertex deformation
        bool usePerMaterialPipeline = false;
        if (dc.material) {
            ShaderProgram *fwdProgram = dc.material->GetPassShaderProgram(ShaderCompileTarget::Forward);
            if (fwdProgram && (fwdProgram->HasVertexMaterialUBO() || dc.skinBoneMatrices)) {
                VkPipeline mtlPipeline = GetOrCreateMtlOutlinePipeline(dc.material.get());
                VkDescriptorSet mtlDescSet = GetOrCreateMtlOutlineDescSet(dc.material.get());

                if (mtlPipeline != VK_NULL_HANDLE && mtlDescSet != VK_NULL_HANDLE) {
                    const uint64_t descRaw = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(mtlDescSet));
                    const uint32_t lo = static_cast<uint32_t>(descRaw & 0xffffffffull);
                    const uint32_t hi = static_cast<uint32_t>((descRaw >> 32) & 0xffffffffull);
                    const bool suspiciousDesc = (hi == lo && lo != 0u && lo <= 0x000fffffu);
                    if (suspiciousDesc) {
                        static int badOutlineDescWarnCount = 0;
                        if (badOutlineDescWarnCount++ < 24) {
                            INXLOG_WARN("[OutlineRenderer] suspicious per-material set0 desc=0x", descRaw,
                                        " material='", dc.material->GetMaterialKey(), "' name='",
                                        dc.material->GetName(), "' -- fallback to fixed outline path");
                        }
                    } else {
                        // Write the selected object's transform and skin palette as instance 0 for this isolated pass.
                        std::memcpy(m_outlineInstanceBufs[frameIdx].mapped, &dc.worldMatrix, sizeof(glm::mat4));
                        if (frameIdx < m_outlineInstanceAuxBufs.size() && m_outlineInstanceAuxBufs[frameIdx].mapped) {
                            GPUInstanceAuxData aux{};
                            aux.previousModel = dc.worldMatrix;
                            aux.objectId = PackGPUObjectId(dc.pickingObjectId != 0 ? dc.pickingObjectId : dc.objectId);
                            aux.layerMask = dc.layerMask;
                            std::memcpy(m_outlineInstanceAuxBufs[frameIdx].mapped, &aux, sizeof(aux));
                        }
                        GPUSkinInstanceData skinData{};
                        if (dc.skinBoneMatrices && !dc.skinBoneMatrices->empty() &&
                            frameIdx < m_outlineSkinInstanceBufs.size() && frameIdx < m_outlineSkinPaletteBufs.size()) {
                            auto &skinInstance = m_outlineSkinInstanceBufs[frameIdx];
                            auto &skinPalette = m_outlineSkinPaletteBufs[frameIdx];
                            if (skinInstance.mapped && skinPalette.mapped &&
                                skinPalette.capacity >= dc.skinBoneMatrices->size()) {
                                skinData.boneOffset = 0;
                                skinData.boneCount = static_cast<uint32_t>(dc.skinBoneMatrices->size());
                                skinData.flags = kGPUSkinFlagEnabled;
                                std::memcpy(skinPalette.mapped, dc.skinBoneMatrices->data(),
                                            dc.skinBoneMatrices->size() * sizeof(glm::mat4));
                            }
                        }
                        if (frameIdx < m_outlineSkinInstanceBufs.size() && m_outlineSkinInstanceBufs[frameIdx].mapped)
                            std::memcpy(m_outlineSkinInstanceBufs[frameIdx].mapped, &skinData,
                                        sizeof(GPUSkinInstanceData));

                        // Bind per-material pipeline
                        vkCmdBindPipeline(cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS, mtlPipeline);

                        // Set 0: scene UBO + vertex material UBO
                        vkdebug::CmdBindDescriptorSetsTracked(
                            "OutlineRenderer.RenderOutlineMask.Set0", cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS,
                            m_outlineMtlPipelineLayout, 0, 1, &mtlDescSet, 0, nullptr);

                        VkDescriptorSet perViewDescSet = m_core->GetActiveShadowDescriptorSet();
                        if (perViewDescSet == VK_NULL_HANDLE)
                            continue;
                        vkdebug::CmdBindDescriptorSetsTracked(
                            "OutlineRenderer.RenderOutlineMask.Set1", cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS,
                            m_outlineMtlPipelineLayout, 1, 1, &perViewDescSet, 0, nullptr);

                        // Set 2: outline globals (globals UBO + outline instance buffer)
                        VkDescriptorSet globalsDescSet = m_outlineGlobalsDescSets[frameIdx];
                        vkdebug::CmdBindDescriptorSetsTracked(
                            "OutlineRenderer.RenderOutlineMask.Set2", cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS,
                            m_outlineMtlPipelineLayout, 2, 1, &globalsDescSet, 0, nullptr);

                        vkCmdPushConstants(cmdBuf, m_outlineMtlPipelineLayout, VK_SHADER_STAGE_VERTEX_BIT, 0,
                                           sizeof(PushConstants), &pushData);

                        // Draw with firstInstance=0 so gl_InstanceIndex=0 reads instanceModels[0]
                        vkCmdDrawIndexed(cmdBuf, dc.indexCount, 1, dc.indexStart, 0, 0);
                        usePerMaterialPipeline = true;
                    }
                }
            }
        }

        // Fallback: original fixed outline mask pipeline (no vertex deformation)
        if (!usePerMaterialPipeline) {
            vkCmdBindPipeline(cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS, m_outlineMaskPipeline);
            vkdebug::CmdBindDescriptorSetsTracked("OutlineRenderer.RenderOutlineMask.FallbackSet0", cmdBuf,
                                                  VK_PIPELINE_BIND_POINT_GRAPHICS, m_outlineMaskPipelineLayout, 0, 1,
                                                  &m_outlineMaskDescSet, 0, nullptr);
            vkCmdPushConstants(cmdBuf, m_outlineMaskPipelineLayout, VK_SHADER_STAGE_VERTEX_BIT, 0,
                               sizeof(PushConstants), &pushData);
            vkCmdDrawIndexed(cmdBuf, dc.indexCount, 1, dc.indexStart, 0, 0);
        }
    }

    vkCmdEndRenderPass(cmdBuf);
}

// ============================================================================
// Internal: Composite Pass
// ============================================================================

void OutlineRenderer::RenderOutlineComposite(VkCommandBuffer cmdBuf)
{
    uint32_t w = m_sceneRenderTarget->GetWidth();
    uint32_t h = m_sceneRenderTarget->GetHeight();

    VkClearValue dummyClear{};
    dummyClear.color = {{0.0f, 0.0f, 0.0f, 1.0f}};
    BeginRenderPassWithFullViewport(cmdBuf, m_outlineCompositeRenderPass, m_outlineCompositeFramebuffer, dummyClear);

    // Bind composite pipeline
    vkCmdBindPipeline(cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS, m_outlineCompositePipeline);

    // Bind composite descriptor set (mask texture sampler)
    vkdebug::CmdBindDescriptorSetsTracked("OutlineRenderer.RenderOutlineComposite.Set0", cmdBuf,
                                          VK_PIPELINE_BIND_POINT_GRAPHICS, m_outlineCompositePipelineLayout, 0, 1,
                                          &m_outlineCompositeDescSet, 0, nullptr);

    // Push constants: outline color, texel size, outline width
    struct CompositePushConstants
    {
        glm::vec4 outlineColor;
        glm::vec2 texelSize;
        float outlineWidth;
        float _padding;
    };

    CompositePushConstants pushData;
    // Authored sRGB -> linear: the composite writes into the linear scene
    // buffer, which is sRGB-encoded later by the display encode pass.
    pushData.outlineColor = inx::color::SrgbToLinear(m_outlineColor);
    pushData.texelSize = glm::vec2(1.0f / static_cast<float>(w), 1.0f / static_cast<float>(h));
    pushData.outlineWidth = m_outlinePixelWidth;
    pushData._padding = 0.0f;

    vkCmdPushConstants(cmdBuf, m_outlineCompositePipelineLayout, VK_SHADER_STAGE_FRAGMENT_BIT, 0,
                       sizeof(CompositePushConstants), &pushData);

    // Draw fullscreen triangle (3 vertices, no vertex buffer)
    vkCmdDraw(cmdBuf, 3, 1, 0, 0);

    vkCmdEndRenderPass(cmdBuf);
}

} // namespace infernux
