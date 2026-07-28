#include "FullscreenRenderer.h"

#include "InxVkCoreModular.h"
#include "shader/ShaderProgram.h"
#include "vk/RhiVulkanTypes.h"
#include "vk/VkPipelineHelpers.h"
#include "vk/VkSwapchainManager.h"
#include "vk/VulkanRhiDevice.h"

#include <algorithm>
#include <core/error/InxError.h>
#include <unordered_map>
#include <vector>

namespace infernux
{

namespace
{

VkDescriptorSetLayout CreateDescriptorSetLayout(VkDevice device,
                                                const std::vector<VkDescriptorSetLayoutBinding> &bindings)
{
    VkDescriptorSetLayoutCreateInfo info{};
    info.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    info.bindingCount = static_cast<uint32_t>(bindings.size());
    info.pBindings = bindings.empty() ? nullptr : bindings.data();

    VkDescriptorSetLayout layout = VK_NULL_HANDLE;
    return vkCreateDescriptorSetLayout(device, &info, nullptr, &layout) == VK_SUCCESS ? layout : VK_NULL_HANDLE;
}

VkSamplerCreateInfo MakeSamplerCreateInfo(VkFilter filter, VkSamplerMipmapMode mipmapMode)
{
    VkSamplerCreateInfo info{};
    info.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
    info.magFilter = filter;
    info.minFilter = filter;
    info.mipmapMode = mipmapMode;
    info.addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    info.addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    info.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    info.maxAnisotropy = 1.0f;
    return info;
}

} // namespace

bool FullscreenPipelineKey::operator==(const FullscreenPipelineKey &other) const noexcept
{
    return shaderName == other.shaderName && renderTargetLayout == other.renderTargetLayout &&
           samples == other.samples && colorFormat == other.colorFormat && inputTextureCount == other.inputTextureCount;
}

size_t FullscreenPipelineKeyHash::operator()(const FullscreenPipelineKey &key) const noexcept
{
    size_t hash = std::hash<std::string>{}(key.shaderName);
    const auto combine = [&hash](size_t value) { hash ^= value + 0x9e3779b9U + (hash << 6) + (hash >> 2); };
    combine(rhi::HandleHash<rhi::RenderTargetLayoutTag>{}(key.renderTargetLayout));
    combine(static_cast<size_t>(key.samples));
    combine(static_cast<size_t>(key.colorFormat));
    combine(key.inputTextureCount);
    return hash;
}

struct FullscreenRenderer::Impl
{
    struct NativePipelineEntry
    {
        FullscreenPipelineEntry rhi;
        VkPipeline pipeline = VK_NULL_HANDLE;
        VkPipelineLayout layout = VK_NULL_HANDLE;
        VkDescriptorSetLayout inputLayout = VK_NULL_HANDLE;
        VkDescriptorSetLayout emptyGapLayout = VK_NULL_HANDLE;
    };

    InxVkCoreModular *core = nullptr;
    vk::VulkanRhiDevice *rhiDevice = nullptr;
    VkDevice device = VK_NULL_HANDLE;
    VkSampler linearSampler = VK_NULL_HANDLE;
    VkSampler nearestSampler = VK_NULL_HANDLE;
    rhi::SamplerHandle linearSamplerHandle;
    rhi::SamplerHandle nearestSamplerHandle;
    std::unordered_map<FullscreenPipelineKey, NativePipelineEntry, FullscreenPipelineKeyHash> pipelines;
    std::vector<std::vector<rhi::BindGroupHandle>> frameBindGroups;
    std::vector<rhi::BindGroupHandle> globalsGroups;

    [[nodiscard]] uint32_t CurrentFrame() const
    {
        if (!core || frameBindGroups.empty())
            return 0;
        return core->GetCurrentFrameSlot() % static_cast<uint32_t>(frameBindGroups.size());
    }

    void ReleaseBindGroups(uint32_t frame)
    {
        if (!rhiDevice || frame >= frameBindGroups.size())
            return;
        for (const auto handle : frameBindGroups[frame])
            rhiDevice->Release(handle);
        frameBindGroups[frame].clear();
    }

    void DestroyPipeline(NativePipelineEntry &entry)
    {
        if (rhiDevice) {
            rhiDevice->Release(entry.rhi.pipeline);
            rhiDevice->Release(entry.rhi.inputLayout);
        }
        if (entry.pipeline != VK_NULL_HANDLE)
            vkDestroyPipeline(device, entry.pipeline, nullptr);
        if (entry.layout != VK_NULL_HANDLE)
            vkDestroyPipelineLayout(device, entry.layout, nullptr);
        if (entry.inputLayout != VK_NULL_HANDLE)
            vkDestroyDescriptorSetLayout(device, entry.inputLayout, nullptr);
        if (entry.emptyGapLayout != VK_NULL_HANDLE)
            vkDestroyDescriptorSetLayout(device, entry.emptyGapLayout, nullptr);
        entry = {};
    }

    void RetirePipeline(NativePipelineEntry entry)
    {
        if (rhiDevice) {
            rhiDevice->Release(entry.rhi.pipeline);
            rhiDevice->Release(entry.rhi.inputLayout);
        }

        const VkDevice retiredDevice = device;
        const VkPipeline pipeline = entry.pipeline;
        const VkPipelineLayout layout = entry.layout;
        const VkDescriptorSetLayout inputLayout = entry.inputLayout;
        const VkDescriptorSetLayout emptyGapLayout = entry.emptyGapLayout;
        auto destroy = [retiredDevice, pipeline, layout, inputLayout, emptyGapLayout] {
            if (pipeline != VK_NULL_HANDLE)
                vkDestroyPipeline(retiredDevice, pipeline, nullptr);
            if (layout != VK_NULL_HANDLE)
                vkDestroyPipelineLayout(retiredDevice, layout, nullptr);
            if (inputLayout != VK_NULL_HANDLE)
                vkDestroyDescriptorSetLayout(retiredDevice, inputLayout, nullptr);
            if (emptyGapLayout != VK_NULL_HANDLE)
                vkDestroyDescriptorSetLayout(retiredDevice, emptyGapLayout, nullptr);
        };

        if (core && core->GetRetirementQueue().HasSerialSource())
            core->GetRetirementQueue().Retire(std::move(destroy));
        else
            destroy();
    }

    NativePipelineEntry CreatePipeline(const FullscreenPipelineKey &key)
    {
        NativePipelineEntry entry;
        const VkRenderPass renderPass = rhiDevice ? rhiDevice->Resolve(key.renderTargetLayout) : VK_NULL_HANDLE;
        if (!core || renderPass == VK_NULL_HANDLE)
            return entry;

        std::vector<VkDescriptorSetLayoutBinding> bindings;
        bindings.reserve(key.inputTextureCount);
        for (uint32_t i = 0; i < key.inputTextureCount; ++i) {
            VkDescriptorSetLayoutBinding binding{};
            binding.binding = i;
            binding.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
            binding.descriptorCount = 1;
            binding.stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
            bindings.push_back(binding);
        }

        entry.inputLayout = CreateDescriptorSetLayout(device, bindings);
        if (entry.inputLayout == VK_NULL_HANDLE)
            return entry;

        VkPushConstantRange pushRange{};
        pushRange.stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
        pushRange.size = sizeof(FullscreenPushConstants);

        std::vector<VkDescriptorSetLayout> setLayouts = {entry.inputLayout};
        const VkDescriptorSetLayout perViewLayout = ShaderProgram::GetPerViewDescSetLayout();
        const VkDescriptorSetLayout globalsLayout = ShaderProgram::GetGlobalsDescSetLayout();
        if (perViewLayout != VK_NULL_HANDLE || globalsLayout != VK_NULL_HANDLE) {
            if (perViewLayout != VK_NULL_HANDLE) {
                setLayouts.push_back(perViewLayout);
            } else {
                entry.emptyGapLayout = CreateDescriptorSetLayout(device, {});
                if (entry.emptyGapLayout == VK_NULL_HANDLE) {
                    DestroyPipeline(entry);
                    return {};
                }
                setLayouts.push_back(entry.emptyGapLayout);
            }
        }
        if (globalsLayout != VK_NULL_HANDLE)
            setLayouts.push_back(globalsLayout);

        VkPipelineLayoutCreateInfo layoutInfo{};
        layoutInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
        layoutInfo.setLayoutCount = static_cast<uint32_t>(setLayouts.size());
        layoutInfo.pSetLayouts = setLayouts.data();
        layoutInfo.pushConstantRangeCount = 1;
        layoutInfo.pPushConstantRanges = &pushRange;
        if (vkCreatePipelineLayout(device, &layoutInfo, nullptr, &entry.layout) != VK_SUCCESS) {
            DestroyPipeline(entry);
            return {};
        }

        VkShaderModule vertex = core->GetShaderModule(key.shaderName, "vertex");
        if (vertex == VK_NULL_HANDLE)
            vertex = core->GetShaderModule("Fullscreen Triangle", "vertex");
        const VkShaderModule fragment = core->GetShaderModule(key.shaderName, "fragment");
        if (vertex == VK_NULL_HANDLE || fragment == VK_NULL_HANDLE) {
            INXLOG_ERROR("FullscreenRenderer: missing shader modules for '", key.shaderName, "'");
            DestroyPipeline(entry);
            return {};
        }

        const auto shaderStages = vkrender::MakeVertFragStages(vertex, fragment);
        VkPipelineVertexInputStateCreateInfo vertexInput{};
        vertexInput.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
        const auto inputAssembly = vkrender::MakeTriangleListInputAssembly();
        vkrender::DynamicViewportScissorState dynamicState;

        VkPipelineRasterizationStateCreateInfo rasterizer{};
        rasterizer.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
        rasterizer.polygonMode = VK_POLYGON_MODE_FILL;
        rasterizer.cullMode = VK_CULL_MODE_NONE;
        rasterizer.frontFace = VK_FRONT_FACE_CLOCKWISE;
        rasterizer.lineWidth = 1.0f;

        const auto multisampling = vkrender::MakeMultisampleState(rhi::ToVkSampleCount(key.samples));
        VkPipelineDepthStencilStateCreateInfo depthStencil{};
        depthStencil.sType = VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;

        VkPipelineColorBlendAttachmentState blendAttachment{};
        blendAttachment.colorWriteMask =
            VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT | VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;
        VkPipelineColorBlendStateCreateInfo colorBlending{};
        colorBlending.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;
        colorBlending.attachmentCount = 1;
        colorBlending.pAttachments = &blendAttachment;

        VkGraphicsPipelineCreateInfo pipelineInfo{};
        pipelineInfo.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
        pipelineInfo.stageCount = static_cast<uint32_t>(shaderStages.size());
        pipelineInfo.pStages = shaderStages.data();
        pipelineInfo.pVertexInputState = &vertexInput;
        pipelineInfo.pInputAssemblyState = &inputAssembly;
        pipelineInfo.pViewportState = &dynamicState.viewportState;
        pipelineInfo.pRasterizationState = &rasterizer;
        pipelineInfo.pMultisampleState = &multisampling;
        pipelineInfo.pDepthStencilState = &depthStencil;
        pipelineInfo.pColorBlendState = &colorBlending;
        pipelineInfo.pDynamicState = &dynamicState.dynamicState;
        pipelineInfo.layout = entry.layout;
        pipelineInfo.renderPass = renderPass;

        if (vkCreateGraphicsPipelines(device, VK_NULL_HANDLE, 1, &pipelineInfo, nullptr, &entry.pipeline) !=
            VK_SUCCESS) {
            INXLOG_ERROR("FullscreenRenderer: failed to create pipeline for '", key.shaderName, "'");
            DestroyPipeline(entry);
            return {};
        }

        entry.rhi.pipeline = rhiDevice->RegisterGraphicsPipeline(entry.pipeline, entry.layout);
        entry.rhi.inputLayout = rhiDevice->RegisterBindingLayout(entry.inputLayout);
        entry.rhi.hasPerView = perViewLayout != VK_NULL_HANDLE;
        entry.rhi.hasGlobals = globalsLayout != VK_NULL_HANDLE;
        return entry;
    }
};

FullscreenRenderer::FullscreenRenderer() = default;
FullscreenRenderer::~FullscreenRenderer()
{
    Destroy();
}

void FullscreenRenderer::Initialize(InxVkCoreModular *vkCore)
{
    Destroy();
    if (!vkCore)
        return;

    m_impl = std::make_unique<Impl>();
    m_impl->core = vkCore;
    m_impl->device = vkCore->GetDevice();
    m_impl->rhiDevice = &vkCore->GetDeviceContext().GetRhiDevice();

    auto linearInfo = MakeSamplerCreateInfo(VK_FILTER_LINEAR, VK_SAMPLER_MIPMAP_MODE_LINEAR);
    auto nearestInfo = MakeSamplerCreateInfo(VK_FILTER_NEAREST, VK_SAMPLER_MIPMAP_MODE_NEAREST);
    if (vkCreateSampler(m_impl->device, &linearInfo, nullptr, &m_impl->linearSampler) != VK_SUCCESS ||
        vkCreateSampler(m_impl->device, &nearestInfo, nullptr, &m_impl->nearestSampler) != VK_SUCCESS) {
        INXLOG_ERROR("FullscreenRenderer: failed to create samplers");
        Destroy();
        return;
    }
    m_impl->linearSamplerHandle = m_impl->rhiDevice->RegisterSampler(m_impl->linearSampler);
    m_impl->nearestSamplerHandle = m_impl->rhiDevice->RegisterSampler(m_impl->nearestSampler);

    const uint32_t frames = std::max(1u, vkCore->GetMaxFramesInFlight());
    m_impl->frameBindGroups.resize(frames);
    m_impl->globalsGroups.resize(frames);
}

void FullscreenRenderer::Destroy()
{
    if (!m_impl)
        return;
    if (m_impl->device != VK_NULL_HANDLE)
        vkDeviceWaitIdle(m_impl->device);

    for (uint32_t frame = 0; frame < m_impl->frameBindGroups.size(); ++frame)
        m_impl->ReleaseBindGroups(frame);
    if (m_impl->rhiDevice) {
        for (const auto group : m_impl->globalsGroups)
            m_impl->rhiDevice->Release(group);
        m_impl->rhiDevice->Release(m_impl->linearSamplerHandle);
        m_impl->rhiDevice->Release(m_impl->nearestSamplerHandle);
    }
    for (auto &[key, entry] : m_impl->pipelines)
        m_impl->DestroyPipeline(entry);
    if (m_impl->linearSampler != VK_NULL_HANDLE)
        vkDestroySampler(m_impl->device, m_impl->linearSampler, nullptr);
    if (m_impl->nearestSampler != VK_NULL_HANDLE)
        vkDestroySampler(m_impl->device, m_impl->nearestSampler, nullptr);
    m_impl.reset();
}

const FullscreenPipelineEntry &FullscreenRenderer::EnsurePipeline(const FullscreenPipelineKey &key)
{
    static const FullscreenPipelineEntry invalid;
    if (!m_impl)
        return invalid;
    const auto found = m_impl->pipelines.find(key);
    if (found != m_impl->pipelines.end())
        return found->second.rhi;
    auto [it, inserted] = m_impl->pipelines.emplace(key, m_impl->CreatePipeline(key));
    return it->second.rhi;
}

void FullscreenRenderer::InvalidateShader(const std::string &shaderName)
{
    if (!m_impl || shaderName.empty())
        return;

    size_t retired = 0;
    for (auto entry = m_impl->pipelines.begin(); entry != m_impl->pipelines.end();) {
        if (entry->first.shaderName != shaderName) {
            ++entry;
            continue;
        }
        auto pipeline = std::move(entry->second);
        entry = m_impl->pipelines.erase(entry);
        m_impl->RetirePipeline(std::move(pipeline));
        ++retired;
    }
    if (retired > 0)
        INXLOG_INFO("FullscreenRenderer: retired ", retired, " pipeline revision(s) for shader '", shaderName, "'");
}

rhi::BindGroupHandle FullscreenRenderer::AllocateBindGroup(rhi::BindingLayoutHandle layout,
                                                           const FullscreenTextureInput *inputs, uint32_t inputCount,
                                                           rhi::SamplerHandle colorSampler)
{
    if (!m_impl || m_impl->frameBindGroups.empty())
        return {};
    const VkDescriptorSetLayout nativeLayout = m_impl->rhiDevice->Resolve(layout);
    const uint32_t frame = m_impl->CurrentFrame();
    if (nativeLayout == VK_NULL_HANDLE)
        return {};

    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = layout;
    groupDesc.lifetime = rhi::BindGroupLifetime::FrameTransient;
    groupDesc.textureCount = inputCount;
    if (!inputs || inputCount > groupDesc.textures.size())
        return {};
    for (uint32_t i = 0; i < inputCount; ++i) {
        const auto &input = inputs[i];
        const bool nearestSampling = input.depthRead || rhi::IsIntegerFormat(input.format);
        auto &texture = groupDesc.textures[i];
        texture.binding = i;
        texture.type = rhi::BindingType::CombinedTextureSampler;
        texture.texture = input.view;
        texture.sampler = nearestSampling ? m_impl->nearestSamplerHandle : colorSampler;
        texture.depthRead = input.depthRead;
        if (!texture.texture.IsValid() || !texture.sampler.IsValid())
            return {};
    }

    const auto handle = m_impl->rhiDevice->CreateBindGroup(groupDesc);
    if (handle.IsValid())
        m_impl->frameBindGroups[frame].push_back(handle);
    return handle;
}

void FullscreenRenderer::Draw(rhi::GraphicsCommandEncoder &encoder, const FullscreenPipelineEntry &entry,
                              rhi::BindGroupHandle inputGroup, rhi::BindGroupHandle perViewGroup,
                              const FullscreenPushConstants &pushConstants, uint32_t pushConstantSize)
{
    if (!m_impl || !entry.pipeline.IsValid())
        return;
    encoder.BindPipeline(entry.pipeline);
    if (inputGroup.IsValid())
        encoder.BindGroup(entry.pipeline, 0, inputGroup);
    if (entry.hasPerView && perViewGroup.IsValid())
        encoder.BindGroup(entry.pipeline, 1, perViewGroup);

    if (entry.hasGlobals) {
        const uint32_t frame = m_impl->CurrentFrame();
        const VkDescriptorSet currentGlobals = m_impl->core->GetCurrentGlobalsDescSet();
        auto &globalsHandle = m_impl->globalsGroups[frame];
        if (m_impl->rhiDevice->Resolve(globalsHandle) != currentGlobals) {
            m_impl->rhiDevice->Release(globalsHandle);
            globalsHandle = m_impl->rhiDevice->RegisterBindGroup(currentGlobals);
        }
        if (globalsHandle.IsValid())
            encoder.BindGroup(entry.pipeline, 2, globalsHandle);
    }
    encoder.PushConstants(entry.pipeline, rhi::ShaderStage::Fragment, pushConstantSize, pushConstants.values);
    encoder.Draw(3);
}

void FullscreenRenderer::ResetPool()
{
    if (!m_impl || m_impl->frameBindGroups.empty())
        return;
    const uint32_t frame = m_impl->CurrentFrame();
    m_impl->ReleaseBindGroups(frame);
}

rhi::SamplerHandle FullscreenRenderer::GetLinearSampler() const noexcept
{
    return m_impl ? m_impl->linearSamplerHandle : rhi::SamplerHandle{};
}

} // namespace infernux
