/**
 * @file VkPipelineManager.cpp
 * @brief Implementation of Vulkan pipeline management
 */

#include "VkPipelineManager.h"
#include "../InxRenderStruct.h"
#include "VkRenderUtils.h"
#include <core/error/InxError.h>
#include <platform/filesystem/InxPath.h>

#include <algorithm>
#include <fstream>

namespace infernux
{
namespace vk
{

// ============================================================================
// Constructor / Destructor / Move
// ============================================================================

VkPipelineManager::~VkPipelineManager()
{
    Destroy();
}

VkPipelineManager::VkPipelineManager(VkPipelineManager &&other) noexcept
    : m_device(other.m_device), m_shaderModules(std::move(other.m_shaderModules)),
      m_renderPasses(std::move(other.m_renderPasses)), m_pipelineLayouts(std::move(other.m_pipelineLayouts)),
      m_pipelines(std::move(other.m_pipelines)), m_descriptorSetLayouts(std::move(other.m_descriptorSetLayouts))
{
    other.m_device = VK_NULL_HANDLE;
}

VkPipelineManager &VkPipelineManager::operator=(VkPipelineManager &&other) noexcept
{
    if (this != &other) {
        Destroy();

        m_device = other.m_device;
        m_shaderModules = std::move(other.m_shaderModules);
        m_renderPasses = std::move(other.m_renderPasses);
        m_pipelineLayouts = std::move(other.m_pipelineLayouts);
        m_pipelines = std::move(other.m_pipelines);
        m_descriptorSetLayouts = std::move(other.m_descriptorSetLayouts);

        other.m_device = VK_NULL_HANDLE;
    }
    return *this;
}

// ============================================================================
// Initialization
// ============================================================================

void VkPipelineManager::Initialize(VkDevice device)
{
    m_device = device;
}

void VkPipelineManager::Destroy() noexcept
{
    if (m_device == VK_NULL_HANDLE) {
        return;
    }

    // Wait for device idle (skip during engine shutdown — already drained)
    if (!m_skipWaitIdle) {
        vkDeviceWaitIdle(m_device);
    }

    // Destroy pipelines
    for (auto pipeline : m_pipelines) {
        if (pipeline != VK_NULL_HANDLE) {
            vkDestroyPipeline(m_device, pipeline, nullptr);
        }
    }
    m_pipelines.clear();

    // Destroy pipeline layouts
    for (auto layout : m_pipelineLayouts) {
        if (layout != VK_NULL_HANDLE) {
            vkDestroyPipelineLayout(m_device, layout, nullptr);
        }
    }
    m_pipelineLayouts.clear();

    // Destroy render passes
    for (auto renderPass : m_renderPasses) {
        if (renderPass != VK_NULL_HANDLE) {
            vkDestroyRenderPass(m_device, renderPass, nullptr);
        }
    }
    m_renderPasses.clear();

    // Destroy shader modules
    for (auto module : m_shaderModules) {
        if (module != VK_NULL_HANDLE) {
            vkDestroyShaderModule(m_device, module, nullptr);
        }
    }
    m_shaderModules.clear();

    // Destroy descriptor set layouts
    for (auto layout : m_descriptorSetLayouts) {
        if (layout != VK_NULL_HANDLE) {
            vkDestroyDescriptorSetLayout(m_device, layout, nullptr);
        }
    }
    m_descriptorSetLayouts.clear();

    m_device = VK_NULL_HANDLE;
}

// ============================================================================
// Shader Management
// ============================================================================

VkShaderModule VkPipelineManager::CreateShaderModule(const std::vector<uint32_t> &code)
{
    if (code.empty()) {
        INXLOG_ERROR("Cannot create shader module from empty code");
        return VK_NULL_HANDLE;
    }

    VkShaderModuleCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
    createInfo.codeSize = code.size() * sizeof(uint32_t);
    createInfo.pCode = code.data();

    VkShaderModule shaderModule;
    VkResult result = vkCreateShaderModule(m_device, &createInfo, nullptr, &shaderModule);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create shader module: ", VkResultToString(result));
        return VK_NULL_HANDLE;
    }

    m_shaderModules.push_back(shaderModule);
    return shaderModule;
}

VkShaderModule VkPipelineManager::LoadShaderModule(const std::string &filePath)
{
    auto code = ReadShaderFile(filePath);
    if (code.empty()) {
        return VK_NULL_HANDLE;
    }
    return CreateShaderModule(code);
}

void VkPipelineManager::DestroyShaderModule(VkShaderModule module)
{
    if (module == VK_NULL_HANDLE) {
        return;
    }

    // Remove from tracked list
    auto it = std::find(m_shaderModules.begin(), m_shaderModules.end(), module);
    if (it != m_shaderModules.end()) {
        m_shaderModules.erase(it);
    }

    vkDestroyShaderModule(m_device, module, nullptr);
}

// ============================================================================
// Render Pass Management
// ============================================================================

VkRenderPass VkPipelineManager::CreateRenderPass(const RenderPassConfig &config)
{
    std::vector<VkAttachmentDescription> attachments;
    std::vector<VkAttachmentReference> colorAttachmentRefs;
    VkAttachmentReference depthAttachmentRef{};
    bool hasDepthRef = false;

    // Color attachment(s) — MRT support
    // When colorFormats has multiple entries, create one attachment per MRT target.
    // Otherwise, fall back to the single colorFormat field.
    if (config.hasColor) {
        uint32_t colorCount = 1;
        if (config.colorFormats.size() > 1) {
            colorCount = static_cast<uint32_t>(config.colorFormats.size());
        }

        for (uint32_t i = 0; i < colorCount; ++i) {
            VkFormat fmt = (i < config.colorFormats.size()) ? config.colorFormats[i] : config.colorFormat;

            VkAttachmentDescription colorAttachment{};
            colorAttachment.format = fmt;
            colorAttachment.samples = config.samples;
            colorAttachment.loadOp = config.clearColor ? VK_ATTACHMENT_LOAD_OP_CLEAR : VK_ATTACHMENT_LOAD_OP_LOAD;
            colorAttachment.storeOp = (config.hasResolve && i == 0 && !config.storeColor)
                                          ? VK_ATTACHMENT_STORE_OP_DONT_CARE
                                          : VK_ATTACHMENT_STORE_OP_STORE;
            colorAttachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
            colorAttachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
            // RenderGraph inserts explicit barriers before each pass and keeps
            // tracked color attachments in COLOR_ATTACHMENT_OPTIMAL between
            // passes. Keep the render pass initialLayout aligned with that
            // tracked state even when the loadOp is CLEAR.
            colorAttachment.initialLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
            colorAttachment.finalLayout =
                (config.hasResolve && i == 0) ? VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL : config.colorFinalLayout;
            attachments.push_back(colorAttachment);

            VkAttachmentReference colorRef{};
            colorRef.attachment = static_cast<uint32_t>(attachments.size() - 1);
            colorRef.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
            colorAttachmentRefs.push_back(colorRef);
        }
    }

    // Depth attachment (optional)
    if (config.hasDepth && config.depthFormat != VK_FORMAT_UNDEFINED) {
        VkAttachmentDescription depthAttachment{};
        depthAttachment.format = config.depthFormat;
        depthAttachment.samples = config.samples;
        depthAttachment.loadOp = config.clearDepth ? VK_ATTACHMENT_LOAD_OP_CLEAR : VK_ATTACHMENT_LOAD_OP_LOAD;
        depthAttachment.storeOp = config.storeDepth ? VK_ATTACHMENT_STORE_OP_STORE : VK_ATTACHMENT_STORE_OP_DONT_CARE;
        depthAttachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        depthAttachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        if (config.readOnlyDepth) {
            // Depth is a read-only input: image was left in READ_ONLY_OPTIMAL by the
            // previous writer (storeDepth=true → finalLayout=DEPTH_STENCIL_READ_ONLY_OPTIMAL).
            // Preserve that layout throughout and after this pass.
            depthAttachment.initialLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL;
            depthAttachment.finalLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL;
        } else {
            // Match the explicit RenderGraph barriers. CLEAR decides whether
            // previous contents matter; it does not require UNDEFINED here.
            depthAttachment.initialLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
            depthAttachment.finalLayout = config.storeDepth ? VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL
                                                            : VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
        }
        attachments.push_back(depthAttachment);

        depthAttachmentRef.attachment = static_cast<uint32_t>(attachments.size() - 1);
        depthAttachmentRef.layout = config.readOnlyDepth ? VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL
                                                         : VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
        hasDepthRef = true;
    }

    // Resolve attachment (for MSAA resolve)
    std::vector<VkAttachmentReference> resolveAttachmentRefs;
    if (config.hasResolve && config.samples > VK_SAMPLE_COUNT_1_BIT) {
        VkAttachmentDescription resolveAttachment{};
        resolveAttachment.format =
            (config.resolveFormat != VK_FORMAT_UNDEFINED) ? config.resolveFormat : config.colorFormat;
        resolveAttachment.samples = VK_SAMPLE_COUNT_1_BIT;
        resolveAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        resolveAttachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        resolveAttachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        resolveAttachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        resolveAttachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        resolveAttachment.finalLayout = config.resolveFinalLayout;
        attachments.push_back(resolveAttachment);

        VkAttachmentReference resolveRef{};
        resolveRef.attachment = static_cast<uint32_t>(attachments.size() - 1);
        resolveRef.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
        resolveAttachmentRefs.push_back(resolveRef);
    }

    // Subpass
    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = static_cast<uint32_t>(colorAttachmentRefs.size());
    subpass.pColorAttachments = colorAttachmentRefs.data();
    subpass.pDepthStencilAttachment = hasDepthRef ? &depthAttachmentRef : nullptr;
    subpass.pResolveAttachments = resolveAttachmentRefs.empty() ? nullptr : resolveAttachmentRefs.data();

    // Subpass dependency
    VkSubpassDependency dependency = vkrender::MakePipelineCompatibleSubpassDependency();
    // Always use WRITE in the dependency so all render passes share the same
    // dependency signature. This keeps pipelines compiled against
    // m_internalRenderPass compatible with every render-graph pass.

    // Create render pass
    VkRenderPassCreateInfo renderPassInfo{};
    renderPassInfo.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
    renderPassInfo.attachmentCount = static_cast<uint32_t>(attachments.size());
    renderPassInfo.pAttachments = attachments.data();
    renderPassInfo.subpassCount = 1;
    renderPassInfo.pSubpasses = &subpass;
    renderPassInfo.dependencyCount = 1;
    renderPassInfo.pDependencies = &dependency;

    VkRenderPass renderPass;
    VkResult result = vkCreateRenderPass(m_device, &renderPassInfo, nullptr, &renderPass);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create render pass: ", VkResultToString(result));
        return VK_NULL_HANDLE;
    }

    m_renderPasses.push_back(renderPass);
    return renderPass;
}

VkRenderPass VkPipelineManager::CreateSimpleRenderPass(VkFormat colorFormat, VkFormat depthFormat)
{
    RenderPassConfig config;
    config.colorFormat = colorFormat;
    config.depthFormat = depthFormat;
    config.hasDepth = (depthFormat != VK_FORMAT_UNDEFINED);
    return CreateRenderPass(config);
}

void VkPipelineManager::DestroyRenderPass(VkRenderPass renderPass)
{
    if (renderPass == VK_NULL_HANDLE) {
        return;
    }

    auto it = std::find(m_renderPasses.begin(), m_renderPasses.end(), renderPass);
    if (it != m_renderPasses.end()) {
        m_renderPasses.erase(it);
    }

    vkDestroyRenderPass(m_device, renderPass, nullptr);
}

bool VkPipelineManager::DetachRenderPass(VkRenderPass renderPass) noexcept
{
    if (renderPass == VK_NULL_HANDLE) {
        return false;
    }

    const auto it = std::find(m_renderPasses.begin(), m_renderPasses.end(), renderPass);
    if (it == m_renderPasses.end()) {
        return false;
    }

    m_renderPasses.erase(it);
    return true;
}

// ============================================================================
// Pipeline Layout Management
// ============================================================================

VkPipelineLayout VkPipelineManager::CreatePipelineLayout(const std::vector<VkDescriptorSetLayout> &descriptorSetLayouts,
                                                         const std::vector<VkPushConstantRange> &pushConstantRanges)
{
    VkPipelineLayoutCreateInfo layoutInfo{};
    layoutInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
    layoutInfo.setLayoutCount = static_cast<uint32_t>(descriptorSetLayouts.size());
    layoutInfo.pSetLayouts = descriptorSetLayouts.empty() ? nullptr : descriptorSetLayouts.data();
    layoutInfo.pushConstantRangeCount = static_cast<uint32_t>(pushConstantRanges.size());
    layoutInfo.pPushConstantRanges = pushConstantRanges.empty() ? nullptr : pushConstantRanges.data();

    VkPipelineLayout layout;
    VkResult result = vkCreatePipelineLayout(m_device, &layoutInfo, nullptr, &layout);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create pipeline layout: ", VkResultToString(result));
        return VK_NULL_HANDLE;
    }

    m_pipelineLayouts.push_back(layout);
    return layout;
}

void VkPipelineManager::DestroyPipelineLayout(VkPipelineLayout layout)
{
    if (layout == VK_NULL_HANDLE) {
        return;
    }

    auto it = std::find(m_pipelineLayouts.begin(), m_pipelineLayouts.end(), layout);
    if (it != m_pipelineLayouts.end()) {
        m_pipelineLayouts.erase(it);
    }

    vkDestroyPipelineLayout(m_device, layout, nullptr);
}

// ============================================================================
// Graphics Pipeline Management
// ============================================================================

PipelineResult VkPipelineManager::CreateGraphicsPipeline(const PipelineConfig &config)
{
    PipelineResult result{};

    // Load/create shader modules
    VkShaderModule vertModule = VK_NULL_HANDLE;
    VkShaderModule fragModule = VK_NULL_HANDLE;

    if (!config.vertexShaderCode.empty()) {
        vertModule = CreateShaderModule(config.vertexShaderCode);
    } else if (!config.vertexShaderPath.empty()) {
        vertModule = LoadShaderModule(config.vertexShaderPath);
    }

    if (!config.fragmentShaderCode.empty()) {
        fragModule = CreateShaderModule(config.fragmentShaderCode);
    } else if (!config.fragmentShaderPath.empty()) {
        fragModule = LoadShaderModule(config.fragmentShaderPath);
    }

    if (vertModule == VK_NULL_HANDLE || fragModule == VK_NULL_HANDLE) {
        INXLOG_ERROR("Failed to load shader modules");
        if (vertModule != VK_NULL_HANDLE)
            DestroyShaderModule(vertModule);
        if (fragModule != VK_NULL_HANDLE)
            DestroyShaderModule(fragModule);
        return result;
    }

    // Shader stages
    VkPipelineShaderStageCreateInfo vertStageInfo{};
    vertStageInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    vertStageInfo.stage = VK_SHADER_STAGE_VERTEX_BIT;
    vertStageInfo.module = vertModule;
    vertStageInfo.pName = "main";

    VkPipelineShaderStageCreateInfo fragStageInfo{};
    fragStageInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    fragStageInfo.stage = VK_SHADER_STAGE_FRAGMENT_BIT;
    fragStageInfo.module = fragModule;
    fragStageInfo.pName = "main";

    VkPipelineShaderStageCreateInfo shaderStages[] = {vertStageInfo, fragStageInfo};

    // Vertex input
    VkPipelineVertexInputStateCreateInfo vertexInputInfo{};
    vertexInputInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
    vertexInputInfo.vertexBindingDescriptionCount = static_cast<uint32_t>(config.vertexInput.bindings.size());
    vertexInputInfo.pVertexBindingDescriptions =
        config.vertexInput.bindings.empty() ? nullptr : config.vertexInput.bindings.data();
    vertexInputInfo.vertexAttributeDescriptionCount = static_cast<uint32_t>(config.vertexInput.attributes.size());
    vertexInputInfo.pVertexAttributeDescriptions =
        config.vertexInput.attributes.empty() ? nullptr : config.vertexInput.attributes.data();

    // Input assembly
    VkPipelineInputAssemblyStateCreateInfo inputAssembly{};
    inputAssembly.sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO;
    inputAssembly.topology = config.topology;
    inputAssembly.primitiveRestartEnable = VK_FALSE;

    // Viewport and scissor (dynamic)
    VkViewport viewport{};
    viewport.x = 0.0f;
    viewport.y = 0.0f;
    viewport.width = static_cast<float>(config.extent.width);
    viewport.height = static_cast<float>(config.extent.height);
    viewport.minDepth = 0.0f;
    viewport.maxDepth = 1.0f;

    VkRect2D scissor{};
    scissor.offset = {0, 0};
    scissor.extent = config.extent;

    VkPipelineViewportStateCreateInfo viewportState{};
    viewportState.sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO;
    viewportState.viewportCount = 1;
    viewportState.pViewports = &viewport;
    viewportState.scissorCount = 1;
    viewportState.pScissors = &scissor;

    // Rasterization
    VkPipelineRasterizationStateCreateInfo rasterizer{};
    rasterizer.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
    rasterizer.depthClampEnable = config.depthClampEnable ? VK_TRUE : VK_FALSE;
    rasterizer.rasterizerDiscardEnable = VK_FALSE;
    rasterizer.polygonMode = config.polygonMode;
    rasterizer.lineWidth = config.lineWidth;
    rasterizer.cullMode = config.cullMode;
    rasterizer.frontFace = config.frontFace;
    rasterizer.depthBiasEnable = VK_FALSE;

    // Multisampling
    VkPipelineMultisampleStateCreateInfo multisampling{};
    multisampling.sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO;
    multisampling.sampleShadingEnable = VK_FALSE;
    multisampling.rasterizationSamples = config.samples;

    // Depth/stencil
    VkPipelineDepthStencilStateCreateInfo depthStencil{};
    depthStencil.sType = VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
    depthStencil.depthTestEnable = config.depthTestEnable ? VK_TRUE : VK_FALSE;
    depthStencil.depthWriteEnable = config.depthWriteEnable ? VK_TRUE : VK_FALSE;
    depthStencil.depthCompareOp = config.depthCompareOp;
    depthStencil.depthBoundsTestEnable = VK_FALSE;
    depthStencil.stencilTestEnable = VK_FALSE;

    // Color blending
    VkPipelineColorBlendAttachmentState colorBlendAttachment{};
    colorBlendAttachment.colorWriteMask =
        VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT | VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;
    colorBlendAttachment.blendEnable = config.blendEnable ? VK_TRUE : VK_FALSE;
    colorBlendAttachment.srcColorBlendFactor = config.srcColorBlend;
    colorBlendAttachment.dstColorBlendFactor = config.dstColorBlend;
    colorBlendAttachment.colorBlendOp = VK_BLEND_OP_ADD;
    colorBlendAttachment.srcAlphaBlendFactor = config.srcAlphaBlend;
    colorBlendAttachment.dstAlphaBlendFactor = config.dstAlphaBlend;
    colorBlendAttachment.alphaBlendOp = VK_BLEND_OP_ADD;

    VkPipelineColorBlendStateCreateInfo colorBlending{};
    colorBlending.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;
    colorBlending.logicOpEnable = VK_FALSE;
    colorBlending.attachmentCount = 1;
    colorBlending.pAttachments = &colorBlendAttachment;

    // Dynamic state
    VkPipelineDynamicStateCreateInfo dynamicState{};
    dynamicState.sType = VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO;
    dynamicState.dynamicStateCount = static_cast<uint32_t>(config.dynamicStates.size());
    dynamicState.pDynamicStates = config.dynamicStates.empty() ? nullptr : config.dynamicStates.data();

    // Pipeline layout
    VkPipelineLayout layout = config.layout;
    if (layout == VK_NULL_HANDLE) {
        layout = CreatePipelineLayout(config.descriptorSetLayouts, config.pushConstantRanges);
        if (layout == VK_NULL_HANDLE) {
            DestroyShaderModule(vertModule);
            DestroyShaderModule(fragModule);
            return result;
        }
        result.layoutOwned = true;
    }
    result.layout = layout;

    // Create pipeline
    VkGraphicsPipelineCreateInfo pipelineInfo{};
    pipelineInfo.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
    pipelineInfo.stageCount = 2;
    pipelineInfo.pStages = shaderStages;
    pipelineInfo.pVertexInputState = &vertexInputInfo;
    pipelineInfo.pInputAssemblyState = &inputAssembly;
    pipelineInfo.pViewportState = &viewportState;
    pipelineInfo.pRasterizationState = &rasterizer;
    pipelineInfo.pMultisampleState = &multisampling;
    pipelineInfo.pDepthStencilState = &depthStencil;
    pipelineInfo.pColorBlendState = &colorBlending;
    pipelineInfo.pDynamicState = &dynamicState;
    pipelineInfo.layout = layout;
    pipelineInfo.renderPass = config.renderPass;
    pipelineInfo.subpass = config.subpass;
    pipelineInfo.basePipelineHandle = VK_NULL_HANDLE;

    VkResult vkResult =
        vkCreateGraphicsPipelines(m_device, VK_NULL_HANDLE, 1, &pipelineInfo, nullptr, &result.pipeline);

    // Cleanup shader modules (no longer needed after pipeline creation)
    DestroyShaderModule(vertModule);
    DestroyShaderModule(fragModule);

    if (vkResult != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create graphics pipeline: ", VkResultToString(vkResult));
        if (result.layoutOwned) {
            DestroyPipelineLayout(layout);
        }
        result = {};
        return result;
    }

    m_pipelines.push_back(result.pipeline);
    return result;
}

void VkPipelineManager::DestroyPipeline(VkPipeline pipeline)
{
    if (pipeline == VK_NULL_HANDLE) {
        return;
    }

    auto it = std::find(m_pipelines.begin(), m_pipelines.end(), pipeline);
    if (it != m_pipelines.end()) {
        m_pipelines.erase(it);
    }

    vkDestroyPipeline(m_device, pipeline, nullptr);
}

void VkPipelineManager::DestroyPipelineResult(PipelineResult &result)
{
    DestroyPipeline(result.pipeline);
    if (result.layoutOwned && result.layout != VK_NULL_HANDLE) {
        DestroyPipelineLayout(result.layout);
    }
    result = {};
}

// ============================================================================
// Descriptor Set Layout Management
// ============================================================================

VkDescriptorSetLayout
VkPipelineManager::CreateDescriptorSetLayout(const std::vector<VkDescriptorSetLayoutBinding> &bindings)
{
    VkDescriptorSetLayoutCreateInfo layoutInfo{};
    layoutInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    layoutInfo.bindingCount = static_cast<uint32_t>(bindings.size());
    layoutInfo.pBindings = bindings.empty() ? nullptr : bindings.data();

    VkDescriptorSetLayout layout;
    VkResult result = vkCreateDescriptorSetLayout(m_device, &layoutInfo, nullptr, &layout);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create descriptor set layout: ", VkResultToString(result));
        return VK_NULL_HANDLE;
    }

    m_descriptorSetLayouts.push_back(layout);
    return layout;
}

void VkPipelineManager::DestroyDescriptorSetLayout(VkDescriptorSetLayout layout)
{
    if (layout == VK_NULL_HANDLE) {
        return;
    }

    auto it = std::find(m_descriptorSetLayouts.begin(), m_descriptorSetLayouts.end(), layout);
    if (it != m_descriptorSetLayouts.end()) {
        m_descriptorSetLayouts.erase(it);
    }

    vkDestroyDescriptorSetLayout(m_device, layout, nullptr);
}

// ============================================================================
// Standard Vertex Input Configurations
// ============================================================================

VertexInputConfig VkPipelineManager::GetStandardMeshVertexInput()
{
    VertexInputConfig config;

    config.bindings.push_back(Vertex::getBindingDescription());
    const auto attributes = Vertex::getAttributeDescriptions();
    config.attributes.insert(config.attributes.end(), attributes.begin(), attributes.end());

    return config;
}

VertexInputConfig VkPipelineManager::GetUIVertexInput()
{
    VertexInputConfig config;

    // Single binding for interleaved vertex data
    VkVertexInputBindingDescription binding{};
    binding.binding = 0;
    binding.stride = sizeof(float) * (2 + 2 + 4); // pos(2) + texCoord(2) + color(4)
    binding.inputRate = VK_VERTEX_INPUT_RATE_VERTEX;
    config.bindings.push_back(binding);

    // Position attribute
    VkVertexInputAttributeDescription posAttr{};
    posAttr.binding = 0;
    posAttr.location = 0;
    posAttr.format = VK_FORMAT_R32G32_SFLOAT;
    posAttr.offset = 0;
    config.attributes.push_back(posAttr);

    // TexCoord attribute
    VkVertexInputAttributeDescription texCoordAttr{};
    texCoordAttr.binding = 0;
    texCoordAttr.location = 1;
    texCoordAttr.format = VK_FORMAT_R32G32_SFLOAT;
    texCoordAttr.offset = sizeof(float) * 2;
    config.attributes.push_back(texCoordAttr);

    // Color attribute
    VkVertexInputAttributeDescription colorAttr{};
    colorAttr.binding = 0;
    colorAttr.location = 2;
    colorAttr.format = VK_FORMAT_R32G32B32A32_SFLOAT;
    colorAttr.offset = sizeof(float) * 4;
    config.attributes.push_back(colorAttr);

    return config;
}

// ============================================================================
// Internal Methods
// ============================================================================

std::vector<uint32_t> VkPipelineManager::ReadShaderFile(const std::string &filePath)
{
    std::ifstream file(ToFsPath(filePath), std::ios::ate | std::ios::binary);
    if (!file.is_open()) {
        INXLOG_ERROR("Failed to open shader file: ", filePath);
        return {};
    }

    size_t fileSize = static_cast<size_t>(file.tellg());
    if (fileSize % sizeof(uint32_t) != 0) {
        INXLOG_ERROR("Invalid SPIR-V file size: ", fileSize, " bytes");
        return {};
    }

    std::vector<uint32_t> buffer(fileSize / sizeof(uint32_t));
    file.seekg(0);
    file.read(reinterpret_cast<char *>(buffer.data()), fileSize);
    file.close();

    return buffer;
}

} // namespace vk
} // namespace infernux
