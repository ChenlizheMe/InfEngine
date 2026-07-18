#include "VulkanRhiDevice.h"

#include "DescriptorBindTrace.h"
#include "RhiVulkanTypes.h"

namespace infernux::vk
{

const rhi::GraphicsCommandEncoder::Dispatch VulkanRhiDevice::s_graphicsDispatch = {
    &VulkanRhiDevice::BindPipeline, &VulkanRhiDevice::BindGroup, &VulkanRhiDevice::PushConstants,
    &VulkanRhiDevice::Draw, &VulkanRhiDevice::DrawIndirect};

const rhi::ComputeCommandEncoder::DispatchTable VulkanRhiDevice::s_computeDispatch = {
    &VulkanRhiDevice::BindComputePipeline, &VulkanRhiDevice::BindComputeGroup, &VulkanRhiDevice::PushComputeConstants,
    &VulkanRhiDevice::Dispatch, &VulkanRhiDevice::DispatchIndirect};

void VulkanRhiDevice::Reset(VkDevice device) noexcept
{
    m_device = device;
    ResetSlots(m_buffers, m_freeBuffer);
    ResetSlots(m_textureViews, m_freeTextureView);
    ResetSlots(m_samplers, m_freeSampler);
    ResetSlots(m_shaderModules, m_freeShaderModule);
    ResetSlots(m_bindingLayouts, m_freeBindingLayout);
    ResetSlots(m_bindGroups, m_freeBindGroup);
    ResetSlots(m_graphicsPipelines, m_freeGraphicsPipeline);
    ResetSlots(m_computePipelines, m_freeComputePipeline);
    ResetSlots(m_renderTargetLayouts, m_freeRenderTargetLayout);
}

rhi::BufferHandle VulkanRhiDevice::RegisterBuffer(VkBuffer buffer)
{
    return buffer == VK_NULL_HANDLE ? rhi::BufferHandle{}
                                    : Register<rhi::BufferHandle>(m_buffers, m_freeBuffer, buffer);
}

template <typename HandleType, typename Payload>
HandleType VulkanRhiDevice::Register(std::vector<Slot<Payload>> &slots, uint32_t &freeHead, const Payload &payload)
{
    if (freeHead != UINT32_MAX) {
        const uint32_t index = freeHead;
        auto &slot = slots[index];
        freeHead = slot.nextFree;
        slot.nextFree = UINT32_MAX;
        slot.payload = payload;
        slot.occupied = true;
        return {index, slot.generation};
    }

    const uint32_t index = static_cast<uint32_t>(slots.size());
    slots.push_back({payload, 1, UINT32_MAX, true});
    return {index, 1};
}

template <typename HandleType, typename Payload>
void VulkanRhiDevice::Release(std::vector<Slot<Payload>> &slots, uint32_t &freeHead, HandleType handle) noexcept
{
    if (!handle.IsValid() || handle.index >= slots.size())
        return;

    auto &slot = slots[handle.index];
    if (!slot.occupied || slot.generation != handle.generation)
        return;

    slot.payload = {};
    slot.occupied = false;
    ++slot.generation;
    if (slot.generation == 0)
        slot.generation = 1;
    slot.nextFree = freeHead;
    freeHead = handle.index;
}

template <typename Payload>
void VulkanRhiDevice::ResetSlots(std::vector<Slot<Payload>> &slots, uint32_t &freeHead) noexcept
{
    freeHead = UINT32_MAX;
    for (size_t i = slots.size(); i > 0; --i) {
        auto &slot = slots[i - 1];
        slot.payload = {};
        slot.occupied = false;
        ++slot.generation;
        if (slot.generation == 0)
            slot.generation = 1;
        slot.nextFree = freeHead;
        freeHead = static_cast<uint32_t>(i - 1);
    }
}

template <typename HandleType, typename Payload>
const Payload *VulkanRhiDevice::Resolve(const std::vector<Slot<Payload>> &slots, HandleType handle) noexcept
{
    if (!handle.IsValid() || handle.index >= slots.size())
        return nullptr;
    const auto &slot = slots[handle.index];
    return slot.occupied && slot.generation == handle.generation ? &slot.payload : nullptr;
}

rhi::TextureViewHandle VulkanRhiDevice::RegisterTextureView(VkImageView view)
{
    return view == VK_NULL_HANDLE ? rhi::TextureViewHandle{}
                                  : Register<rhi::TextureViewHandle>(m_textureViews, m_freeTextureView, view);
}

rhi::SamplerHandle VulkanRhiDevice::RegisterSampler(VkSampler sampler)
{
    return sampler == VK_NULL_HANDLE ? rhi::SamplerHandle{}
                                     : Register<rhi::SamplerHandle>(m_samplers, m_freeSampler, sampler);
}

rhi::ShaderModuleHandle VulkanRhiDevice::RegisterShaderModule(VkShaderModule module)
{
    return module == VK_NULL_HANDLE ? rhi::ShaderModuleHandle{}
                                    : Register<rhi::ShaderModuleHandle>(m_shaderModules, m_freeShaderModule, module);
}

rhi::BindingLayoutHandle VulkanRhiDevice::RegisterBindingLayout(VkDescriptorSetLayout layout)
{
    return layout == VK_NULL_HANDLE ? rhi::BindingLayoutHandle{}
                                    : Register<rhi::BindingLayoutHandle>(m_bindingLayouts, m_freeBindingLayout, layout);
}

rhi::BindGroupHandle VulkanRhiDevice::RegisterBindGroup(VkDescriptorSet set)
{
    return set == VK_NULL_HANDLE ? rhi::BindGroupHandle{}
                                 : Register<rhi::BindGroupHandle>(m_bindGroups, m_freeBindGroup, set);
}

rhi::GraphicsPipelineHandle VulkanRhiDevice::RegisterGraphicsPipeline(VkPipeline pipeline, VkPipelineLayout layout)
{
    return pipeline == VK_NULL_HANDLE || layout == VK_NULL_HANDLE
               ? rhi::GraphicsPipelineHandle{}
               : Register<rhi::GraphicsPipelineHandle>(m_graphicsPipelines, m_freeGraphicsPipeline,
                                                       GraphicsPipelinePayload{pipeline, layout});
}

rhi::ComputePipelineHandle VulkanRhiDevice::RegisterComputePipeline(VkPipeline pipeline, VkPipelineLayout layout)
{
    return pipeline == VK_NULL_HANDLE || layout == VK_NULL_HANDLE
               ? rhi::ComputePipelineHandle{}
               : Register<rhi::ComputePipelineHandle>(m_computePipelines, m_freeComputePipeline,
                                                      GraphicsPipelinePayload{pipeline, layout});
}

rhi::RenderTargetLayoutHandle VulkanRhiDevice::RegisterRenderTargetLayout(VkRenderPass renderPass)
{
    return renderPass == VK_NULL_HANDLE
               ? rhi::RenderTargetLayoutHandle{}
               : Register<rhi::RenderTargetLayoutHandle>(m_renderTargetLayouts, m_freeRenderTargetLayout, renderPass);
}

void VulkanRhiDevice::Release(rhi::TextureViewHandle handle) noexcept
{
    Release(m_textureViews, m_freeTextureView, handle);
}
void VulkanRhiDevice::Release(rhi::BufferHandle handle) noexcept
{
    Release(m_buffers, m_freeBuffer, handle);
}
void VulkanRhiDevice::Release(rhi::SamplerHandle handle) noexcept
{
    Release(m_samplers, m_freeSampler, handle);
}
void VulkanRhiDevice::Release(rhi::ShaderModuleHandle handle) noexcept
{
    Release(m_shaderModules, m_freeShaderModule, handle);
}
void VulkanRhiDevice::Release(rhi::BindingLayoutHandle handle) noexcept
{
    Release(m_bindingLayouts, m_freeBindingLayout, handle);
}
void VulkanRhiDevice::Release(rhi::BindGroupHandle handle) noexcept
{
    Release(m_bindGroups, m_freeBindGroup, handle);
}
void VulkanRhiDevice::Release(rhi::GraphicsPipelineHandle handle) noexcept
{
    Release(m_graphicsPipelines, m_freeGraphicsPipeline, handle);
}
void VulkanRhiDevice::Release(rhi::ComputePipelineHandle handle) noexcept
{
    Release(m_computePipelines, m_freeComputePipeline, handle);
}
void VulkanRhiDevice::Release(rhi::RenderTargetLayoutHandle handle) noexcept
{
    Release(m_renderTargetLayouts, m_freeRenderTargetLayout, handle);
}

VkImageView VulkanRhiDevice::Resolve(rhi::TextureViewHandle handle) const noexcept
{
    const auto *payload = Resolve(m_textureViews, handle);
    return payload ? *payload : VK_NULL_HANDLE;
}
VkBuffer VulkanRhiDevice::Resolve(rhi::BufferHandle handle) const noexcept
{
    const auto *payload = Resolve(m_buffers, handle);
    return payload ? *payload : VK_NULL_HANDLE;
}
VkSampler VulkanRhiDevice::Resolve(rhi::SamplerHandle handle) const noexcept
{
    const auto *payload = Resolve(m_samplers, handle);
    return payload ? *payload : VK_NULL_HANDLE;
}
VkShaderModule VulkanRhiDevice::Resolve(rhi::ShaderModuleHandle handle) const noexcept
{
    const auto *payload = Resolve(m_shaderModules, handle);
    return payload ? *payload : VK_NULL_HANDLE;
}
VkDescriptorSetLayout VulkanRhiDevice::Resolve(rhi::BindingLayoutHandle handle) const noexcept
{
    const auto *payload = Resolve(m_bindingLayouts, handle);
    return payload ? *payload : VK_NULL_HANDLE;
}
VkDescriptorSet VulkanRhiDevice::Resolve(rhi::BindGroupHandle handle) const noexcept
{
    const auto *payload = Resolve(m_bindGroups, handle);
    return payload ? *payload : VK_NULL_HANDLE;
}
VkRenderPass VulkanRhiDevice::Resolve(rhi::RenderTargetLayoutHandle handle) const noexcept
{
    const auto *payload = Resolve(m_renderTargetLayouts, handle);
    return payload ? *payload : VK_NULL_HANDLE;
}

const VulkanRhiDevice::GraphicsPipelinePayload *
VulkanRhiDevice::ResolvePipeline(rhi::GraphicsPipelineHandle handle) const noexcept
{
    return Resolve(m_graphicsPipelines, handle);
}

const VulkanRhiDevice::GraphicsPipelinePayload *
VulkanRhiDevice::ResolvePipeline(rhi::ComputePipelineHandle handle) const noexcept
{
    return Resolve(m_computePipelines, handle);
}

rhi::GraphicsCommandEncoder VulkanRhiDevice::MakeGraphicsCommandEncoder(VulkanGraphicsCommandContext &context,
                                                                        VkCommandBuffer commandBuffer) noexcept
{
    context.device = this;
    context.commandBuffer = commandBuffer;
    context.boundPipeline = {};
    return {&context, &s_graphicsDispatch};
}

rhi::ComputeCommandEncoder VulkanRhiDevice::MakeComputeCommandEncoder(VulkanComputeCommandContext &context,
                                                                      VkCommandBuffer commandBuffer) noexcept
{
    context.device = this;
    context.commandBuffer = commandBuffer;
    context.boundPipeline = {};
    return {&context, &s_computeDispatch};
}

void VulkanRhiDevice::BindPipeline(void *context, rhi::GraphicsPipelineHandle pipeline)
{
    auto &command = *static_cast<VulkanGraphicsCommandContext *>(context);
    command.boundPipeline = {};
    const auto *native = command.device ? command.device->ResolvePipeline(pipeline) : nullptr;
    if (native && command.commandBuffer != VK_NULL_HANDLE) {
        vkCmdBindPipeline(command.commandBuffer, VK_PIPELINE_BIND_POINT_GRAPHICS, native->pipeline);
        command.boundPipeline = pipeline;
    }
}

void VulkanRhiDevice::BindGroup(void *context, rhi::GraphicsPipelineHandle pipeline, uint32_t setIndex,
                                rhi::BindGroupHandle group)
{
    auto &command = *static_cast<VulkanGraphicsCommandContext *>(context);
    const auto *nativePipeline = command.device ? command.device->ResolvePipeline(pipeline) : nullptr;
    const VkDescriptorSet nativeGroup = command.device ? command.device->Resolve(group) : VK_NULL_HANDLE;
    if (nativePipeline && nativeGroup != VK_NULL_HANDLE && command.commandBuffer != VK_NULL_HANDLE &&
        command.boundPipeline == pipeline) {
        vkdebug::CmdBindDescriptorSetsTracked("RHI.GraphicsCommandEncoder.BindGroup", command.commandBuffer,
                                              VK_PIPELINE_BIND_POINT_GRAPHICS, nativePipeline->layout, setIndex, 1,
                                              &nativeGroup, 0, nullptr);
    }
}

void VulkanRhiDevice::PushConstants(void *context, rhi::GraphicsPipelineHandle pipeline, rhi::ShaderStage stages,
                                    uint32_t byteSize, const void *data)
{
    auto &command = *static_cast<VulkanGraphicsCommandContext *>(context);
    const auto *native = command.device ? command.device->ResolvePipeline(pipeline) : nullptr;
    if (native && command.commandBuffer != VK_NULL_HANDLE && command.boundPipeline == pipeline && byteSize > 0 &&
        data) {
        vkCmdPushConstants(command.commandBuffer, native->layout, rhi::ToVkShaderStages(stages), 0, byteSize, data);
    }
}

void VulkanRhiDevice::Draw(void *context, uint32_t vertexCount, uint32_t instanceCount, uint32_t firstVertex,
                           uint32_t firstInstance)
{
    auto &command = *static_cast<VulkanGraphicsCommandContext *>(context);
    if (command.commandBuffer != VK_NULL_HANDLE && command.boundPipeline.IsValid())
        vkCmdDraw(command.commandBuffer, vertexCount, instanceCount, firstVertex, firstInstance);
}

void VulkanRhiDevice::DrawIndirect(void *context, rhi::BufferHandle arguments, uint64_t offset, uint32_t drawCount,
                                   uint32_t stride)
{
    auto &command = *static_cast<VulkanGraphicsCommandContext *>(context);
    const VkBuffer native = command.device ? command.device->Resolve(arguments) : VK_NULL_HANDLE;
    if (command.commandBuffer != VK_NULL_HANDLE && command.boundPipeline.IsValid() && native != VK_NULL_HANDLE &&
        drawCount > 0)
        vkCmdDrawIndirect(command.commandBuffer, native, offset, drawCount, stride);
}

void VulkanRhiDevice::BindComputePipeline(void *context, rhi::ComputePipelineHandle pipeline)
{
    auto &command = *static_cast<VulkanComputeCommandContext *>(context);
    command.boundPipeline = {};
    const auto *native = command.device ? command.device->ResolvePipeline(pipeline) : nullptr;
    if (native && command.commandBuffer != VK_NULL_HANDLE) {
        vkCmdBindPipeline(command.commandBuffer, VK_PIPELINE_BIND_POINT_COMPUTE, native->pipeline);
        command.boundPipeline = pipeline;
    }
}

void VulkanRhiDevice::BindComputeGroup(void *context, rhi::ComputePipelineHandle pipeline, uint32_t setIndex,
                                       rhi::BindGroupHandle group)
{
    auto &command = *static_cast<VulkanComputeCommandContext *>(context);
    const auto *nativePipeline = command.device ? command.device->ResolvePipeline(pipeline) : nullptr;
    const VkDescriptorSet nativeGroup = command.device ? command.device->Resolve(group) : VK_NULL_HANDLE;
    if (nativePipeline && nativeGroup != VK_NULL_HANDLE && command.commandBuffer != VK_NULL_HANDLE &&
        command.boundPipeline == pipeline) {
        vkdebug::CmdBindDescriptorSetsTracked("RHI.ComputeCommandEncoder.BindGroup", command.commandBuffer,
                                              VK_PIPELINE_BIND_POINT_COMPUTE, nativePipeline->layout, setIndex, 1,
                                              &nativeGroup, 0, nullptr);
    }
}

void VulkanRhiDevice::PushComputeConstants(void *context, rhi::ComputePipelineHandle pipeline, uint32_t byteSize,
                                           const void *data)
{
    auto &command = *static_cast<VulkanComputeCommandContext *>(context);
    const auto *native = command.device ? command.device->ResolvePipeline(pipeline) : nullptr;
    if (native && command.commandBuffer != VK_NULL_HANDLE && command.boundPipeline == pipeline && byteSize > 0 && data)
        vkCmdPushConstants(command.commandBuffer, native->layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, byteSize, data);
}

void VulkanRhiDevice::Dispatch(void *context, uint32_t groupCountX, uint32_t groupCountY, uint32_t groupCountZ)
{
    auto &command = *static_cast<VulkanComputeCommandContext *>(context);
    if (command.commandBuffer != VK_NULL_HANDLE && command.boundPipeline.IsValid() && groupCountX > 0 &&
        groupCountY > 0 && groupCountZ > 0)
        vkCmdDispatch(command.commandBuffer, groupCountX, groupCountY, groupCountZ);
}

void VulkanRhiDevice::DispatchIndirect(void *context, rhi::BufferHandle arguments, uint64_t offset)
{
    auto &command = *static_cast<VulkanComputeCommandContext *>(context);
    const VkBuffer native = command.device ? command.device->Resolve(arguments) : VK_NULL_HANDLE;
    if (command.commandBuffer != VK_NULL_HANDLE && command.boundPipeline.IsValid() && native != VK_NULL_HANDLE)
        vkCmdDispatchIndirect(command.commandBuffer, native, offset);
}

} // namespace infernux::vk
