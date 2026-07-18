#include "MaterialPipelineManager.h"
#include "InxRenderStruct.h"
#include "VertexInputFilter.h"
#include "vk/VkPipelineHelpers.h"
#include "vk/VkRenderUtils.h"
#include <algorithm>
#include <core/log/InxLog.h>
#include <stdexcept>

namespace infernux
{

namespace
{

/// Clear all Forward-pass Vulkan handles on a material to prevent stale references.
void ClearForwardPassHandles(InxMaterial *material)
{
    material->SetPassPipeline(ShaderCompileTarget::Forward, VK_NULL_HANDLE);
    material->SetPassPipelineLayout(ShaderCompileTarget::Forward, VK_NULL_HANDLE);
    material->SetPassDescriptorSet(ShaderCompileTarget::Forward, VK_NULL_HANDLE);
    material->SetPassShaderProgram(ShaderCompileTarget::Forward, nullptr);
}

size_t FoldProgramKeyHash(size_t pipelineHash, const ShaderProgramKey &programKey)
{
    const size_t programHash = ShaderProgramKeyHash{}(programKey);
    pipelineHash ^= programHash + 0x9e3779b97f4a7c15ull + (pipelineHash << 6) + (pipelineHash >> 2);
    return pipelineHash;
}

} // namespace

// ============================================================================
// Shared helpers
// ============================================================================

VkRenderPass MaterialPipelineManager::BuildCompatibleRenderPass(uint32_t colorAttachmentCount,
                                                                const VkFormat *colorFormats)
{
    std::vector<VkAttachmentDescription> attachments;
    std::vector<VkAttachmentReference> colorRefs;
    VkAttachmentReference depthRef{};
    const bool hasDepth = (m_depthFormat != VK_FORMAT_UNDEFINED);

    for (uint32_t i = 0; i < colorAttachmentCount; ++i) {
        VkAttachmentDescription colorAttachment{};
        colorAttachment.format = colorFormats ? colorFormats[i] : m_colorFormat;
        colorAttachment.samples = m_sampleCount;
        colorAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        colorAttachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        colorAttachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        colorAttachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        colorAttachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        colorAttachment.finalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
        attachments.push_back(colorAttachment);

        VkAttachmentReference ref{};
        ref.attachment = static_cast<uint32_t>(attachments.size() - 1);
        ref.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
        colorRefs.push_back(ref);
    }

    if (hasDepth) {
        VkAttachmentDescription depthAttachment{};
        depthAttachment.format = m_depthFormat;
        depthAttachment.samples = m_sampleCount;
        depthAttachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        depthAttachment.storeOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        depthAttachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        depthAttachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        depthAttachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        depthAttachment.finalLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
        attachments.push_back(depthAttachment);

        depthRef.attachment = static_cast<uint32_t>(attachments.size() - 1);
        depthRef.layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    }

    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = static_cast<uint32_t>(colorRefs.size());
    subpass.pColorAttachments = colorRefs.data();
    subpass.pDepthStencilAttachment = hasDepth ? &depthRef : nullptr;
    subpass.pResolveAttachments = nullptr;

    const VkSubpassDependency dependency = vkrender::MakePipelineCompatibleSubpassDependency();

    VkRenderPassCreateInfo rpInfo{};
    rpInfo.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
    rpInfo.attachmentCount = static_cast<uint32_t>(attachments.size());
    rpInfo.pAttachments = attachments.data();
    rpInfo.subpassCount = 1;
    rpInfo.pSubpasses = &subpass;
    rpInfo.dependencyCount = 1;
    rpInfo.pDependencies = &dependency;

    VkRenderPass rp = VK_NULL_HANDLE;
    if (vkCreateRenderPass(m_device, &rpInfo, nullptr, &rp) != VK_SUCCESS) {
        INXLOG_ERROR("MaterialPipelineManager: Failed to create render pass (", colorAttachmentCount,
                     " color attachments)");
    }
    return rp;
}

void MaterialPipelineManager::SyncMaterialForwardPass(InxMaterial *material, VkPipeline pipeline,
                                                      VkPipelineLayout layout, VkDescriptorSet descSet,
                                                      ShaderProgram *program)
{
    material->SetPassPipeline(ShaderCompileTarget::Forward, pipeline);
    material->SetPassPipelineLayout(ShaderCompileTarget::Forward, layout);
    material->SetPassDescriptorSet(ShaderCompileTarget::Forward, descSet);
    material->SetPassShaderProgram(ShaderCompileTarget::Forward, program);
    material->ClearPipelineDirty();
}

bool MaterialPipelineManager::IsPipelineSharedByOthers(const std::string &excludeName, VkPipeline pipeline) const
{
    for (const auto &[name, data] : m_renderDataMap) {
        if (name == excludeName || !data)
            continue;
        if (data->pipeline == pipeline)
            return true;
    }
    return false;
}

void MaterialPipelineManager::DestroyNonForwardPipelines(InxMaterial *material, bool deferred)
{
    for (int i = 1; i < static_cast<int>(ShaderCompileTarget::Count); ++i) {
        const auto pass = static_cast<ShaderCompileTarget>(i);
        // Shadow pipelines are owned by the shadow pipeline cache in
        // InxVkCoreModular — only clear the material's reference here.
        if (pass == ShaderCompileTarget::Shadow) {
            material->SetPassPipeline(pass, VK_NULL_HANDLE);
            material->SetPassPipelineLayout(pass, VK_NULL_HANDLE);
            material->SetPassShaderProgram(pass, nullptr);
            continue;
        }
        VkPipeline pp = material->GetPassPipeline(pass);
        if (pp != VK_NULL_HANDLE) {
            if (deferred && m_deletionQueue) {
                const VkDevice device = m_device;
                m_deletionQueue->Push([device, pp] { vkDestroyPipeline(device, pp, nullptr); });
            } else {
                vkDestroyPipeline(m_device, pp, nullptr);
            }
            material->SetPassPipeline(pass, VK_NULL_HANDLE);
        }
        material->SetPassPipelineLayout(pass, VK_NULL_HANDLE);
        material->SetPassShaderProgram(pass, nullptr);
    }
}

MaterialPipelineManager::~MaterialPipelineManager()
{
    Shutdown();
}

void MaterialPipelineManager::Initialize(VmaAllocator allocator, VkDevice device, VkPhysicalDevice physicalDevice,
                                         VkFormat colorFormat, VkFormat depthFormat, VkSampleCountFlagBits sampleCount,
                                         ShaderProgramCache &shaderProgramCache, FrameDeletionQueue *deletionQueue,
                                         bool descriptorIndexingEnabled)
{
    m_device = device;
    m_allocator = allocator;
    m_physicalDevice = physicalDevice;
    m_colorFormat = colorFormat;
    m_depthFormat = depthFormat;
    m_sampleCount = sampleCount;
    m_shaderProgramCache = &shaderProgramCache;
    m_deletionQueue = deletionQueue;

    // Create internal compatible render pass for pipeline creation
    CreateInternalRenderPass();

    // Initialize shader program cache
    m_shaderProgramCache->Initialize(device);

    // Plumb the descriptor-indexing decision through to BOTH the layouts
    // (created lazily by ShaderProgram::CreateDescriptorSetLayouts on shader
    // load) AND the pool (created here in Initialize). Setting the static
    // flag before Initialize() guarantees the very first ShaderProgram and
    // the descriptor pool agree on whether UPDATE_AFTER_BIND is on, which
    // Vulkan validation requires.
    ShaderProgram::SetUpdateAfterBindEnabled(descriptorIndexingEnabled);
    m_descriptorManager.SetUpdateAfterBindEnabled(descriptorIndexingEnabled);
    m_descriptorManager.Initialize(allocator, device, physicalDevice);
    m_descriptorManager.SetDeletionQueue(deletionQueue);

    // Create Vulkan pipeline cache for faster recreation
    VkPipelineCacheCreateInfo cacheCreateInfo{};
    cacheCreateInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO;
    if (vkCreatePipelineCache(m_device, &cacheCreateInfo, nullptr, &m_vkPipelineCache) != VK_SUCCESS) {
        INXLOG_WARN("Failed to create VkPipelineCache, pipeline recreation may be slower");
        m_vkPipelineCache = VK_NULL_HANDLE;
    }
}

void MaterialPipelineManager::Shutdown(bool skipWaitIdle)
{
    if (m_device == VK_NULL_HANDLE) {
        return;
    }

    if (!skipWaitIdle) {
        vkDeviceWaitIdle(m_device);
    }

    // Shutdown descriptor manager
    m_descriptorManager.Shutdown();

    // Shutdown shader program cache
    if (m_shaderProgramCache) {
        m_shaderProgramCache->Shutdown();
        m_shaderProgramCache = nullptr;
    }

    // Destroy all pipelines
    for (auto &[hash, pipeline] : m_pipelineCache) {
        if (pipeline != VK_NULL_HANDLE) {
            vkDestroyPipeline(m_device, pipeline, nullptr);
        }
    }
    m_pipelineCache.clear();

    // Clear stale Vulkan handles on material objects before dropping render data.
    // Otherwise materials may keep dangling pipeline/layout/descriptor handles
    // and attempt to use them on subsequent frames after MSAA reinitialization.
    for (auto &[name, data] : m_renderDataMap) {
        (void)name;
        if (!data || !data->material) {
            continue;
        }
        DestroyNonForwardPipelines(data->material.get());
        data->material->CleanupUBO(m_device);
        data->material->ClearAllPassPipelines();
    }

    // Clear render data. Shader modules stored in MaterialRenderData are
    // shallow copies of the handles owned by ShaderProgram, which were
    // already destroyed by ShaderProgramCache::Shutdown() above — do NOT
    // destroy them again here (double-free).
    m_renderDataMap.clear();

    // Destroy Vulkan pipeline cache
    if (m_vkPipelineCache != VK_NULL_HANDLE) {
        vkDestroyPipelineCache(m_device, m_vkPipelineCache, nullptr);
        m_vkPipelineCache = VK_NULL_HANDLE;
    }

    // Destroy internal render pass
    if (m_internalRenderPass != VK_NULL_HANDLE) {
        vkDestroyRenderPass(m_device, m_internalRenderPass, nullptr);
        m_internalRenderPass = VK_NULL_HANDLE;
    }

    // Destroy MRT render passes
    for (auto &[key, rp] : m_mrtRenderPassCache) {
        if (rp != VK_NULL_HANDLE) {
            vkDestroyRenderPass(m_device, rp, nullptr);
        }
    }
    m_mrtRenderPassCache.clear();

    m_defaultRenderData = nullptr;
    m_device = VK_NULL_HANDLE;
    m_allocator = VK_NULL_HANDLE;
    m_physicalDevice = VK_NULL_HANDLE;
    m_deletionQueue = nullptr;
}

VkShaderModule MaterialPipelineManager::CreateShaderModule(const std::vector<char> &code)
{
    if (code.empty()) {
        INXLOG_ERROR("Cannot create shader module from empty code");
        return VK_NULL_HANDLE;
    }

    VkShaderModuleCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
    createInfo.codeSize = code.size();
    createInfo.pCode = reinterpret_cast<const uint32_t *>(code.data());

    VkShaderModule shaderModule;
    if (vkCreateShaderModule(m_device, &createInfo, nullptr, &shaderModule) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create shader module");
        return VK_NULL_HANDLE;
    }

    return shaderModule;
}

MaterialRenderData *MaterialPipelineManager::GetRenderData(const std::string &materialName)
{
    auto it = m_renderDataMap.find(materialName);
    if (it != m_renderDataMap.end() && it->second->isValid) {
        return it->second.get();
    }
    return nullptr;
}

bool MaterialPipelineManager::HasRenderData(const std::string &materialName) const
{
    auto it = m_renderDataMap.find(materialName);
    return it != m_renderDataMap.end() && it->second->isValid;
}

MaterialRenderData *MaterialPipelineManager::GetDefaultRenderData()
{
    return m_defaultRenderData;
}

VkPipeline MaterialPipelineManager::GetCachedPipeline(size_t pipelineHash) const
{
    auto it = m_pipelineCache.find(pipelineHash);
    if (it != m_pipelineCache.end()) {
        return it->second;
    }
    return VK_NULL_HANDLE;
}

// ============================================================================
// New Shader Reflection API
// ============================================================================

MaterialRenderData *MaterialPipelineManager::GetOrCreateRenderDataWithReflection(
    std::shared_ptr<InxMaterial> material, const std::vector<char> &vertShaderCode,
    const std::vector<char> &fragShaderCode, const ShaderProgramKey &programKey, VkBuffer sceneUBO,
    VkDeviceSize sceneUBOSize, VkBuffer lightingUBO, VkDeviceSize lightingUBOSize)
{
    if (!material) {
        INXLOG_ERROR("Cannot create render data for null material");
        return nullptr;
    }

    const std::string name = material->GetMaterialKey();

    // Check if already exists and valid
    auto it = m_renderDataMap.find(name);
    if (it != m_renderDataMap.end()) {
        size_t currentHash = material->GetPipelineHash();
        // Fold MRT attachment count into hash so forward vs. deferred pipelines differ
        currentHash = FoldMRTAttachmentHash(currentHash);
        currentHash = FoldProgramKeyHash(currentHash, programKey);

        if (it->second->isValid) {
            if (it->second->pipelineHash == currentHash) {
                // Sync Vulkan handles to the (possibly new) material object.
                // This is critical when the default material is replaced
                // with a freshly-deserialized one that has null handles.
                SyncMaterialForwardPass(material.get(), it->second->pipeline, it->second->pipelineLayout,
                                        it->second->descriptorSet, it->second->shaderProgram);
                it->second->material = material; // update cached reference
                return it->second.get();
            }
            INXLOG_INFO("Material '", name, "' config changed, recreating pipeline");

            // Immediately clear stale Vulkan handles from the material.
            // If recreation fails below, the draw code will see pipeline == VK_NULL_HANDLE
            // and correctly fall back to the error material instead of rendering with
            // the old (now-incorrect) pipeline/descriptor set.
            ClearForwardPassHandles(material.get());
            it->second->isValid = false;
            // Update stored hash so we know when the user changes config again
            it->second->pipelineHash = currentHash;
        } else {
            // Render data exists but is invalid (previous creation attempt failed).
            // Only retry if the material config actually changed (user might have fixed it).
            if (it->second->pipelineHash == currentHash) {
                // Config unchanged since last failure — don't spam retries every frame.
                return nullptr;
            }
            INXLOG_INFO("Material '", name, "' config changed after failure, retrying pipeline creation");
            it->second->pipelineHash = currentHash;
        }
    }

    // Get or create shader program (with reflection)
    ShaderProgram *program = m_shaderProgramCache->GetOrCreateProgram(programKey, vertShaderCode, fragShaderCode);
    if (!program || !program->IsValid()) {
        INXLOG_ERROR("Failed to get shader program for material: ", name);
        // Store an invalid render data entry so subsequent frames can detect
        // "already failed" and skip silently (no per-frame spam).
        if (m_renderDataMap.find(name) == m_renderDataMap.end()) {
            auto failedData = std::make_unique<MaterialRenderData>();
            failedData->material = material;
            failedData->pipelineHash =
                FoldProgramKeyHash(FoldMRTAttachmentHash(material->GetPipelineHash()), programKey);
            failedData->programKey = programKey;
            failedData->isValid = false;
            m_renderDataMap[name] = std::move(failedData);
        }
        return nullptr;
    }

    // Create new render data
    auto renderData = std::make_unique<MaterialRenderData>();
    renderData->material = material;
    renderData->pipelineHash = material->GetPipelineHash();
    // Fold MRT attachment count into hash (must match the logic in the early-return path above)
    renderData->pipelineHash = FoldMRTAttachmentHash(renderData->pipelineHash);
    renderData->pipelineHash = FoldProgramKeyHash(renderData->pipelineHash, programKey);
    renderData->programKey = programKey;
    renderData->shaderProgram = program;
    renderData->vertModule = program->GetVertexModule();
    renderData->fragModule = program->GetFragmentModule();
    renderData->pipelineLayout = program->GetPipelineLayout();

    // Create or get material descriptor set
    renderData->materialDescSet = m_descriptorManager.GetOrCreateDescriptorSet(
        *material, *program, sceneUBO, sceneUBOSize, lightingUBO, lightingUBOSize);

    if (renderData->materialDescSet) {
        renderData->descriptorSet = renderData->materialDescSet->descriptorSet;
    }

    // Check pipeline cache first (use shader-based key)
    size_t pipelineKey = renderData->pipelineHash;
    VkPipeline cachedPipeline = GetCachedPipeline(pipelineKey);

    if (cachedPipeline != VK_NULL_HANDLE) {
        renderData->pipeline = cachedPipeline;
        renderData->isValid = true;
        INXLOG_DEBUG("Using cached pipeline for material: ", name);
    } else {
        // Create new pipeline using shader program
        renderData->pipeline = CreatePipelineWithProgram(program, material->GetRenderState());

        if (renderData->pipeline == VK_NULL_HANDLE) {
            INXLOG_ERROR("Failed to create pipeline for material: ", name);
            return nullptr;
        }

        renderData->isValid = true;

        // Cache the pipeline — guard against hash collisions that would
        // silently overwrite (and leak) a different VkPipeline handle.
        auto cacheIt = m_pipelineCache.find(pipelineKey);
        if (cacheIt != m_pipelineCache.end() && cacheIt->second != renderData->pipeline) {
            if (!IsPipelineSharedByOthers("", cacheIt->second)) {
                vkDestroyPipeline(m_device, cacheIt->second, nullptr);
            }
        }
        m_pipelineCache[pipelineKey] = renderData->pipeline;
    }

    // Update material with pipeline info
    SyncMaterialForwardPass(material.get(), renderData->pipeline, renderData->pipelineLayout, renderData->descriptorSet,
                            program);

    MaterialRenderData *result = renderData.get();
    m_renderDataMap[name] = std::move(renderData);

    return result;
}

VkPipeline MaterialPipelineManager::CreatePipelineWithProgram(ShaderProgram *program, const RenderState &renderState)
{
    if (!program || !program->IsValid()) {
        INXLOG_ERROR("Invalid shader program for pipeline creation");
        return VK_NULL_HANDLE;
    }

    // Debug log the cull mode being used
    const char *cullModeStr = "UNKNOWN";
    if (renderState.cullMode == VK_CULL_MODE_NONE)
        cullModeStr = "NONE";
    else if (renderState.cullMode == VK_CULL_MODE_FRONT_BIT)
        cullModeStr = "FRONT";
    else if (renderState.cullMode == VK_CULL_MODE_BACK_BIT)
        cullModeStr = "BACK";
    else if (renderState.cullMode == VK_CULL_MODE_FRONT_AND_BACK)
        cullModeStr = "FRONT_AND_BACK";
    INXLOG_DEBUG("CreatePipelineWithProgram: shader=", program->GetShaderId(), ", cullMode=", cullModeStr,
                 ", blendEnable=", renderState.blendEnable ? "true" : "false",
                 ", depthWrite=", renderState.depthWriteEnable ? "true" : "false",
                 ", depthTest=", renderState.depthTestEnable ? "true" : "false",
                 ", renderQueue=", renderState.renderQueue);

    // Shader stages
    auto shaderStages = vkrender::MakeVertFragStages(program->GetVertexModule(), program->GetFragmentModule());

    // Dynamic state
    vkrender::DynamicViewportScissorState dynVpScissor;

    // Vertex input - expose only attributes consumed by this vertex shader.
    // This keeps shaders that do not use skinning inputs from producing Vulkan
    // validation warnings for locations 5/6.
    auto bindingDescription = Vertex::getBindingDescription();
    auto attributeDescriptions = FilterVertexAttributesForReflection(program->GetVertexReflection());

    VkPipelineVertexInputStateCreateInfo vertexInputInfo{};
    vertexInputInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
    vertexInputInfo.vertexBindingDescriptionCount = attributeDescriptions.empty() ? 0u : 1u;
    vertexInputInfo.pVertexBindingDescriptions = attributeDescriptions.empty() ? nullptr : &bindingDescription;
    vertexInputInfo.vertexAttributeDescriptionCount = static_cast<uint32_t>(attributeDescriptions.size());
    vertexInputInfo.pVertexAttributeDescriptions =
        attributeDescriptions.empty() ? nullptr : attributeDescriptions.data();

    // Input assembly
    VkPipelineInputAssemblyStateCreateInfo inputAssembly{};
    inputAssembly.sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO;
    inputAssembly.topology = renderState.topology;
    inputAssembly.primitiveRestartEnable = VK_FALSE;

    // Multisampling
    auto multisampling = vkrender::MakeMultisampleState(m_sampleCount);

    // Rasterizer
    VkPipelineRasterizationStateCreateInfo rasterizer{};
    rasterizer.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
    rasterizer.depthClampEnable = VK_FALSE;
    rasterizer.rasterizerDiscardEnable = VK_FALSE;
    rasterizer.polygonMode = renderState.polygonMode;
    rasterizer.lineWidth = renderState.lineWidth;
    rasterizer.cullMode = renderState.cullMode;
    rasterizer.frontFace = renderState.frontFace;
    rasterizer.depthBiasEnable = renderState.depthBiasEnable ? VK_TRUE : VK_FALSE;
    rasterizer.depthBiasConstantFactor = renderState.depthBiasConstantFactor;
    rasterizer.depthBiasSlopeFactor = renderState.depthBiasSlopeFactor;
    rasterizer.depthBiasClamp = renderState.depthBiasClamp;

    // Color blending — create one blend attachment per color output for MRT.
    // Opaque forward passes also need alpha writes so intermediate scene
    // layers can preserve coverage for later fullscreen composites.
    VkPipelineColorBlendAttachmentState colorBlendAttachment{};
    colorBlendAttachment.colorWriteMask =
        VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT | VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;
    colorBlendAttachment.blendEnable = renderState.blendEnable ? VK_TRUE : VK_FALSE;
    colorBlendAttachment.srcColorBlendFactor = renderState.srcColorBlendFactor;
    colorBlendAttachment.dstColorBlendFactor = renderState.dstColorBlendFactor;
    colorBlendAttachment.colorBlendOp = renderState.colorBlendOp;
    colorBlendAttachment.srcAlphaBlendFactor = renderState.srcAlphaBlendFactor;
    colorBlendAttachment.dstAlphaBlendFactor = renderState.dstAlphaBlendFactor;
    colorBlendAttachment.alphaBlendOp = renderState.alphaBlendOp;

    std::vector<VkPipelineColorBlendAttachmentState> blendAttachments(m_activeColorAttachmentCount,
                                                                      colorBlendAttachment);

    VkPipelineColorBlendStateCreateInfo colorBlending{};
    colorBlending.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;
    colorBlending.logicOpEnable = VK_FALSE;
    colorBlending.attachmentCount = static_cast<uint32_t>(blendAttachments.size());
    colorBlending.pAttachments = blendAttachments.data();

    // Depth stencil
    VkPipelineDepthStencilStateCreateInfo depthStencil{};
    depthStencil.sType = VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
    depthStencil.depthTestEnable = renderState.depthTestEnable ? VK_TRUE : VK_FALSE;
    depthStencil.depthWriteEnable = renderState.depthWriteEnable ? VK_TRUE : VK_FALSE;
    depthStencil.depthCompareOp = renderState.depthCompareOp;
    depthStencil.depthBoundsTestEnable = VK_FALSE;
    depthStencil.stencilTestEnable = renderState.stencilTestEnable ? VK_TRUE : VK_FALSE;
    depthStencil.front = renderState.stencilFront;
    depthStencil.back = renderState.stencilBack;

    // Create pipeline with shader program's layout
    VkGraphicsPipelineCreateInfo pipelineInfo{};
    pipelineInfo.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
    pipelineInfo.stageCount = static_cast<uint32_t>(shaderStages.size());
    pipelineInfo.pStages = shaderStages.data();
    pipelineInfo.pVertexInputState = &vertexInputInfo;
    pipelineInfo.pInputAssemblyState = &inputAssembly;
    pipelineInfo.pViewportState = &dynVpScissor.viewportState;
    pipelineInfo.pRasterizationState = &rasterizer;
    pipelineInfo.pMultisampleState = &multisampling;
    pipelineInfo.pDepthStencilState = &depthStencil;
    pipelineInfo.pColorBlendState = &colorBlending;
    pipelineInfo.pDynamicState = &dynVpScissor.dynamicState;
    pipelineInfo.layout = program->GetPipelineLayout(); // Use program's layout!
    pipelineInfo.renderPass = GetActiveMRTRenderPass();
    pipelineInfo.subpass = 0;
    pipelineInfo.basePipelineHandle = VK_NULL_HANDLE;

    VkPipeline pipeline;
    if (vkCreateGraphicsPipelines(m_device, m_vkPipelineCache, 1, &pipelineInfo, nullptr, &pipeline) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create graphics pipeline with shader program");
        return VK_NULL_HANDLE;
    }

    return pipeline;
}

void MaterialPipelineManager::UpdateMaterialProperties(const std::string &materialName, const InxMaterial &material)
{
    m_descriptorManager.UpdateMaterialUBO(materialName, material);

    // Re-resolve Texture2D properties in case set_texture was called
    auto it = m_renderDataMap.find(materialName);
    if (it != m_renderDataMap.end() && it->second && it->second->shaderProgram) {
        const auto &bindings = it->second->shaderProgram->GetDescriptorBindings();
        m_descriptorManager.ResolveTextureProperties(materialName, material, bindings);
    }
}

void MaterialPipelineManager::BindMaterialTexture(const std::string &materialName, uint32_t binding,
                                                  VkImageView imageView, VkSampler sampler)
{
    m_descriptorManager.BindTexture(materialName, binding, imageView, sampler);
}

void MaterialPipelineManager::SetDefaultTexture(VkImageView imageView, VkSampler sampler)
{
    m_descriptorManager.SetDefaultTexture(imageView, sampler);
}

void MaterialPipelineManager::SetDefaultNormalTexture(VkImageView imageView, VkSampler sampler)
{
    m_descriptorManager.SetDefaultNormalTexture(imageView, sampler);
}

void MaterialPipelineManager::SetMRTConfig(uint32_t colorAttachmentCount, const std::vector<VkFormat> &colorFormats)
{
    m_activeColorAttachmentCount = colorAttachmentCount;
    m_activeColorFormats = colorFormats;
}

void MaterialPipelineManager::ResetMRTConfig()
{
    m_activeColorAttachmentCount = 1;
    m_activeColorFormats.clear();
}

size_t MaterialPipelineManager::FoldMRTAttachmentHash(size_t baseHash) const
{
    size_t h = std::hash<uint32_t>{}(m_activeColorAttachmentCount);
    return baseHash ^ (h + 0x9e3779b9 + (baseHash << 6) + (baseHash >> 2));
}

VkRenderPass MaterialPipelineManager::GetActiveMRTRenderPass()
{
    if (m_activeColorAttachmentCount <= 1) {
        return m_internalRenderPass;
    }

    // Build a hash key for the MRT config
    size_t key = 0;
    auto hashCombine = [&key](size_t val) { key ^= val + 0x9e3779b9 + (key << 6) + (key >> 2); };
    hashCombine(m_activeColorAttachmentCount);
    for (VkFormat f : m_activeColorFormats) {
        hashCombine(static_cast<uint32_t>(f));
    }
    hashCombine(static_cast<uint32_t>(m_depthFormat));
    hashCombine(static_cast<uint32_t>(m_sampleCount));

    auto it = m_mrtRenderPassCache.find(key);
    if (it != m_mrtRenderPassCache.end()) {
        return it->second;
    }

    // Build format array, falling back to default color format for missing entries
    std::vector<VkFormat> formats(m_activeColorAttachmentCount);
    for (uint32_t i = 0; i < m_activeColorAttachmentCount; ++i) {
        formats[i] = (i < m_activeColorFormats.size()) ? m_activeColorFormats[i] : m_colorFormat;
    }

    VkRenderPass rp = BuildCompatibleRenderPass(m_activeColorAttachmentCount, formats.data());
    if (rp == VK_NULL_HANDLE) {
        return m_internalRenderPass; // fallback
    }

    m_mrtRenderPassCache[key] = rp;
    INXLOG_INFO("MaterialPipelineManager: Created MRT render pass with ", m_activeColorAttachmentCount,
                " color attachments");
    return rp;
}

void MaterialPipelineManager::CreateInternalRenderPass()
{
    VkFormat fmt = m_colorFormat;
    m_internalRenderPass = BuildCompatibleRenderPass(1, &fmt);
}

void MaterialPipelineManager::InvalidateMaterialsUsingShader(const std::string &shaderId)
{
    INXLOG_INFO("Invalidating materials using shader: ", shaderId);

    // Helper to extract shader name from path
    auto extractShaderName = [](const std::string &path) -> std::string {
        if (path.empty())
            return "";
        size_t lastSlash = path.find_last_of("/\\");
        std::string fileName = (lastSlash != std::string::npos) ? path.substr(lastSlash + 1) : path;
        size_t dotPos = fileName.find_last_of('.');
        if (dotPos != std::string::npos) {
            return fileName.substr(0, dotPos);
        }
        return fileName;
    };

    std::vector<std::string> materialsToRemove;

    for (auto &[name, data] : m_renderDataMap) {
        if (!data || !data->material)
            continue;

        // Check if this material uses the specified shader (vert or frag)
        const std::string &vertName = data->material->GetVertShaderName();
        const std::string &fragName = data->material->GetFragShaderName();

        // Check exact match or shader name match against both vert and frag
        bool matches = (vertName == shaderId) || (extractShaderName(vertName) == shaderId) || (fragName == shaderId) ||
                       (extractShaderName(fragName) == shaderId);

        if (matches) {
            INXLOG_DEBUG("Material '", name, "' uses shader '", shaderId, "', marking for invalidation");
            materialsToRemove.push_back(name);
        }
    }

    // Remove render data for affected materials (force recreation)
    for (const auto &name : materialsToRemove) {
        RemoveRenderData(name);
    }

    INXLOG_INFO("Invalidated ", materialsToRemove.size(), " materials using shader '", shaderId, "'");
}

void MaterialPipelineManager::InvalidateMaterialsUsingProgramPair(const ShaderStagePair &stages)
{
    if (!stages.IsValid())
        throw std::invalid_argument("Material program-pair invalidation requires both shader identifiers");

    std::vector<std::string> materialsToRemove;
    for (const auto &[name, data] : m_renderDataMap) {
        if (data && data->programKey.stages == stages)
            materialsToRemove.push_back(name);
    }
    for (const auto &name : materialsToRemove)
        RemoveRenderData(name);

    INXLOG_INFO("Invalidated ", materialsToRemove.size(), " materials using shader program '", stages.ToString(), "'");
}

uint32_t MaterialPipelineManager::InvalidateMaterialsUsingTexture(const std::string &textureGuid)
{
    // Texture2D properties hold validated GUIDs or builtin tokens, so equality is sufficient.
    if (textureGuid.empty()) {
        return 0;
    }

    std::vector<std::string> materialsToRemove;
    materialsToRemove.reserve(m_renderDataMap.size());

    for (const auto &[name, data] : m_renderDataMap) {
        if (!data || !data->material) {
            continue;
        }

        bool matches = false;
        for (const auto &[propName, prop] : data->material->GetAllProperties()) {
            (void)propName;
            if (prop.type != MaterialPropertyType::Texture2D) {
                continue;
            }

            const auto *value = std::get_if<std::string>(&prop.value);
            if (value && *value == textureGuid) {
                matches = true;
                break;
            }
        }

        if (matches) {
            materialsToRemove.push_back(name);
        }
    }

    for (const auto &name : materialsToRemove) {
        RemoveRenderData(name);
    }

    if (!materialsToRemove.empty()) {
        INXLOG_INFO("Invalidated ", materialsToRemove.size(), " materials using texture GUID '", textureGuid, "'");
    }

    return static_cast<uint32_t>(materialsToRemove.size());
}

void MaterialPipelineManager::InvalidateAllMaterialPipelines()
{
    uint32_t count = 0;
    for (auto &[name, data] : m_renderDataMap) {
        if (!data || !data->material)
            continue;
        data->material->MarkPipelineDirty();
        ++count;
    }
    // if (count > 0) {
    //     INXLOG_INFO("InvalidateAllMaterialPipelines: marked ", count, " materials dirty");
    // }
}

void MaterialPipelineManager::RemoveRenderData(const std::string &materialName)
{
    // Also remove the cached descriptor set so that pipeline re-creation
    // builds a fresh one with current texture bindings (avoids stale refs).
    m_descriptorManager.RemoveDescriptorSet(materialName);

    auto it = m_renderDataMap.find(materialName);
    if (it == m_renderDataMap.end()) {
        return;
    }

    auto &data = it->second;
    if (data) {
        if (data->material) {
            ClearForwardPassHandles(data->material.get());
            DestroyNonForwardPipelines(data->material.get(), true);
            RetireMaterialUBO(data->material->DetachUBO());
        }

        // Only destroy the pipeline if no other render data shares it.
        if (data->pipeline != VK_NULL_HANDLE) {
            if (!IsPipelineSharedByOthers(materialName, data->pipeline)) {
                for (auto pipeIt = m_pipelineCache.begin(); pipeIt != m_pipelineCache.end();) {
                    if (pipeIt->second == data->pipeline) {
                        pipeIt = m_pipelineCache.erase(pipeIt);
                    } else {
                        ++pipeIt;
                    }
                }
                if (m_deletionQueue) {
                    const VkDevice device = m_device;
                    const VkPipeline pipeline = data->pipeline;
                    m_deletionQueue->Push([device, pipeline] { vkDestroyPipeline(device, pipeline, nullptr); });
                } else {
                    vkDestroyPipeline(m_device, data->pipeline, nullptr);
                }
            }
        }
    }

    m_renderDataMap.erase(it);
    INXLOG_DEBUG("Removed render data for material: ", materialName);
}

void MaterialPipelineManager::RetireMaterialUBO(InxMaterial::DetachedUBO resource)
{
    if (resource.buffer == VK_NULL_HANDLE)
        return;
    if (resource.allocator == VK_NULL_HANDLE || resource.allocation == VK_NULL_HANDLE)
        throw std::logic_error("Material UBO has incomplete VMA ownership");
    if (m_deletionQueue) {
        m_deletionQueue->Push(
            [resource] { vmaDestroyBuffer(resource.allocator, resource.buffer, resource.allocation); });
        return;
    }
    vmaDestroyBuffer(resource.allocator, resource.buffer, resource.allocation);
}

size_t MaterialPipelineManager::CollectUnusedRenderData()
{
    std::vector<std::string> unused;
    unused.reserve(m_renderDataMap.size());
    for (const auto &[name, data] : m_renderDataMap) {
        if (!data || !data->material || data.get() == m_defaultRenderData)
            continue;
        // Registry-owned builtins naturally have another strong owner. Do not
        // use IsBuiltin() as a lifetime signal: runtime materials created from
        // a built-in shader carry that flag too and still need reclamation.
        if (data->material.use_count() == 1)
            unused.push_back(name);
    }
    for (const auto &name : unused)
        RemoveRenderData(name);
    return unused.size();
}

MaterialGpuResidencySnapshot MaterialPipelineManager::GetResidencySnapshot() const
{
    MaterialGpuResidencySnapshot snapshot;
    snapshot.renderDataCount = m_renderDataMap.size();
    snapshot.pipelineCount = m_pipelineCache.size();
    snapshot.descriptorSetCount = m_descriptorManager.GetDescriptorSetCount();
    snapshot.retiredDescriptorSetCount = m_descriptorManager.GetRetiredDescriptorSetCount();
    snapshot.descriptorPoolCount = m_descriptorManager.GetDescriptorPoolCount();
    for (const auto &[key, data] : m_renderDataMap) {
        (void)key;
        if (!data || !data->material)
            continue;
        if (data->material->GetGuid().empty())
            ++snapshot.runtimeMaterialCount;
        else
            ++snapshot.assetMaterialCount;
        const VmaAllocation allocation = data->material->GetUBOAllocation();
        if (allocation == VK_NULL_HANDLE)
            continue;
        VmaAllocationInfo info{};
        vmaGetAllocationInfo(m_allocator, allocation, &info);
        snapshot.uboBytes += info.size;
    }
    return snapshot;
}

} // namespace infernux
