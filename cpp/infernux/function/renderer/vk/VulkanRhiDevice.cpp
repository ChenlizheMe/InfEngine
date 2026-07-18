#include "VulkanRhiDevice.h"

#include "DescriptorBindTrace.h"
#include "RhiVulkanTypes.h"

#include <array>
#include <cstring>

namespace infernux::vk
{

namespace
{

VkBufferUsageFlags ToVkBufferUsage(rhi::BufferUsageFlags usage)
{
    VkBufferUsageFlags result = 0;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::Storage))
        result |= VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::Uniform))
        result |= VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::Vertex))
        result |= VK_BUFFER_USAGE_VERTEX_BUFFER_BIT;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::Index))
        result |= VK_BUFFER_USAGE_INDEX_BUFFER_BIT;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::Indirect))
        result |= VK_BUFFER_USAGE_INDIRECT_BUFFER_BIT;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::TransferSource))
        result |= VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
    if (rhi::HasBufferUsage(usage, rhi::BufferUsageFlags::TransferDestination))
        result |= VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    return result;
}

VkDescriptorType ToVkDescriptorType(rhi::BindingType type)
{
    switch (type) {
    case rhi::BindingType::UniformBuffer:
        return VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    case rhi::BindingType::StorageBuffer:
        return VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    case rhi::BindingType::SampledTexture:
        return VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE;
    case rhi::BindingType::StorageTexture:
        return VK_DESCRIPTOR_TYPE_STORAGE_IMAGE;
    case rhi::BindingType::Sampler:
        return VK_DESCRIPTOR_TYPE_SAMPLER;
    case rhi::BindingType::CombinedTextureSampler:
        return VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    }
    return VK_DESCRIPTOR_TYPE_MAX_ENUM;
}

VkPrimitiveTopology ToVkTopology(rhi::PrimitiveTopology topology)
{
    switch (topology) {
    case rhi::PrimitiveTopology::TriangleList:
        return VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
    case rhi::PrimitiveTopology::TriangleStrip:
        return VK_PRIMITIVE_TOPOLOGY_TRIANGLE_STRIP;
    case rhi::PrimitiveTopology::LineList:
        return VK_PRIMITIVE_TOPOLOGY_LINE_LIST;
    }
    return VK_PRIMITIVE_TOPOLOGY_MAX_ENUM;
}

VkCullModeFlags ToVkCullMode(rhi::CullMode mode)
{
    switch (mode) {
    case rhi::CullMode::None:
        return VK_CULL_MODE_NONE;
    case rhi::CullMode::Front:
        return VK_CULL_MODE_FRONT_BIT;
    case rhi::CullMode::Back:
        return VK_CULL_MODE_BACK_BIT;
    }
    return VK_CULL_MODE_FLAG_BITS_MAX_ENUM;
}

VkFrontFace ToVkFrontFace(rhi::FrontFace face)
{
    return face == rhi::FrontFace::Clockwise ? VK_FRONT_FACE_CLOCKWISE : VK_FRONT_FACE_COUNTER_CLOCKWISE;
}

VkCompareOp ToVkCompareOp(rhi::CompareFunction function)
{
    switch (function) {
    case rhi::CompareFunction::Never:
        return VK_COMPARE_OP_NEVER;
    case rhi::CompareFunction::Less:
        return VK_COMPARE_OP_LESS;
    case rhi::CompareFunction::Equal:
        return VK_COMPARE_OP_EQUAL;
    case rhi::CompareFunction::LessEqual:
        return VK_COMPARE_OP_LESS_OR_EQUAL;
    case rhi::CompareFunction::Greater:
        return VK_COMPARE_OP_GREATER;
    case rhi::CompareFunction::NotEqual:
        return VK_COMPARE_OP_NOT_EQUAL;
    case rhi::CompareFunction::GreaterEqual:
        return VK_COMPARE_OP_GREATER_OR_EQUAL;
    case rhi::CompareFunction::Always:
        return VK_COMPARE_OP_ALWAYS;
    }
    return VK_COMPARE_OP_MAX_ENUM;
}

} // namespace

const rhi::GraphicsCommandEncoder::Dispatch VulkanRhiDevice::s_graphicsDispatch = {
    &VulkanRhiDevice::BindPipeline, &VulkanRhiDevice::BindGroup, &VulkanRhiDevice::PushConstants,
    &VulkanRhiDevice::Draw, &VulkanRhiDevice::DrawIndirect};

const rhi::ComputeCommandEncoder::DispatchTable VulkanRhiDevice::s_computeDispatch = {
    &VulkanRhiDevice::BindComputePipeline, &VulkanRhiDevice::BindComputeGroup, &VulkanRhiDevice::PushComputeConstants,
    &VulkanRhiDevice::Dispatch, &VulkanRhiDevice::DispatchIndirect};

const rhi::TransferCommandEncoder::DispatchTable VulkanRhiDevice::s_transferDispatch = {
    &VulkanRhiDevice::CopyBuffer,
    &VulkanRhiDevice::CopyTexture,
};

VulkanRhiDevice::~VulkanRhiDevice()
{
    DestroyOwnedResources();
}

void VulkanRhiDevice::Reset(VkDevice device, VmaAllocator allocator) noexcept
{
    DestroyOwnedResources();
    m_device = device;
    m_allocator = allocator;
    ResetSlots(m_buffers, m_freeBuffer);
    ResetSlots(m_textures, m_freeTexture);
    ResetSlots(m_textureViews, m_freeTextureView);
    ResetSlots(m_samplers, m_freeSampler);
    ResetSlots(m_shaderModules, m_freeShaderModule);
    ResetSlots(m_bindingLayouts, m_freeBindingLayout);
    ResetSlots(m_bindGroups, m_freeBindGroup);
    ResetSlots(m_graphicsPipelines, m_freeGraphicsPipeline);
    ResetSlots(m_computePipelines, m_freeComputePipeline);
    ResetSlots(m_renderTargetLayouts, m_freeRenderTargetLayout);
}

rhi::BufferHandle VulkanRhiDevice::RegisterBuffer(VkBuffer buffer, uint64_t byteSize)
{
    return buffer == VK_NULL_HANDLE
               ? rhi::BufferHandle{}
               : Register<rhi::BufferHandle>(m_buffers, m_freeBuffer, BufferPayload{buffer, {}, nullptr, byteSize});
}

rhi::TextureHandle VulkanRhiDevice::RegisterTexture(VkImage image)
{
    return image == VK_NULL_HANDLE ? rhi::TextureHandle{}
                                   : Register<rhi::TextureHandle>(m_textures, m_freeTexture, image);
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
    return module == VK_NULL_HANDLE
               ? rhi::ShaderModuleHandle{}
               : Register<rhi::ShaderModuleHandle>(m_shaderModules, m_freeShaderModule, ShaderModulePayload{module});
}

rhi::BindingLayoutHandle VulkanRhiDevice::RegisterBindingLayout(VkDescriptorSetLayout layout)
{
    return layout == VK_NULL_HANDLE ? rhi::BindingLayoutHandle{}
                                    : Register<rhi::BindingLayoutHandle>(m_bindingLayouts, m_freeBindingLayout,
                                                                         BindingLayoutPayload{layout});
}

rhi::BindGroupHandle VulkanRhiDevice::RegisterBindGroup(VkDescriptorSet set)
{
    return set == VK_NULL_HANDLE ? rhi::BindGroupHandle{}
                                 : Register<rhi::BindGroupHandle>(m_bindGroups, m_freeBindGroup, BindGroupPayload{set});
}

rhi::BufferHandle VulkanRhiDevice::CreateBuffer(const rhi::BufferDesc &desc)
{
    const VkBufferUsageFlags usage = ToVkBufferUsage(desc.usage);
    if (m_device == VK_NULL_HANDLE || m_allocator == VK_NULL_HANDLE || desc.byteSize == 0 || usage == 0 ||
        desc.initialDataBytes > desc.byteSize || (desc.initialDataBytes > 0 && desc.initialData == nullptr) ||
        (desc.initialDataBytes > 0 && desc.memory == rhi::BufferMemory::DeviceLocal))
        return {};

    VkBufferCreateInfo bufferInfo{};
    bufferInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bufferInfo.size = desc.byteSize;
    bufferInfo.usage = usage;
    bufferInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;

    VmaAllocationCreateInfo allocationInfo{};
    switch (desc.memory) {
    case rhi::BufferMemory::DeviceLocal:
        allocationInfo.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;
        break;
    case rhi::BufferMemory::Upload:
        allocationInfo.usage = VMA_MEMORY_USAGE_AUTO;
        allocationInfo.flags =
            VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT;
        break;
    case rhi::BufferMemory::Readback:
        allocationInfo.usage = VMA_MEMORY_USAGE_AUTO;
        allocationInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT;
        break;
    }

    VkBuffer buffer = VK_NULL_HANDLE;
    VmaAllocation allocation = VK_NULL_HANDLE;
    VmaAllocationInfo resultInfo{};
    if (vmaCreateBuffer(m_allocator, &bufferInfo, &allocationInfo, &buffer, &allocation, &resultInfo) != VK_SUCCESS)
        return {};

    if (desc.initialDataBytes > 0) {
        if (!resultInfo.pMappedData) {
            vmaDestroyBuffer(m_allocator, buffer, allocation);
            return {};
        }
        std::memcpy(resultInfo.pMappedData, desc.initialData, static_cast<size_t>(desc.initialDataBytes));
        vmaFlushAllocation(m_allocator, allocation, 0, desc.initialDataBytes);
    }

    return Register<rhi::BufferHandle>(m_buffers, m_freeBuffer,
                                       BufferPayload{buffer, allocation, resultInfo.pMappedData, desc.byteSize, true});
}

rhi::ShaderModuleHandle VulkanRhiDevice::CreateShaderModule(const rhi::ShaderModuleDesc &desc)
{
    if (m_device == VK_NULL_HANDLE || !desc.spirv || desc.wordCount == 0)
        return {};
    VkShaderModuleCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
    createInfo.codeSize = desc.wordCount * sizeof(uint32_t);
    createInfo.pCode = desc.spirv;
    VkShaderModule module = VK_NULL_HANDLE;
    if (vkCreateShaderModule(m_device, &createInfo, nullptr, &module) != VK_SUCCESS)
        return {};
    return Register<rhi::ShaderModuleHandle>(m_shaderModules, m_freeShaderModule, ShaderModulePayload{module, true});
}

rhi::BindingLayoutHandle VulkanRhiDevice::CreateBindingLayout(const rhi::BindingLayoutDesc &desc)
{
    if (m_device == VK_NULL_HANDLE || desc.entryCount > desc.entries.size())
        return {};
    std::array<VkDescriptorSetLayoutBinding, rhi::BindingLayoutDesc::MaxEntries> bindings{};
    for (uint32_t index = 0; index < desc.entryCount; ++index) {
        const auto &entry = desc.entries[index];
        const VkDescriptorType type = ToVkDescriptorType(entry.type);
        if (type == VK_DESCRIPTOR_TYPE_MAX_ENUM || entry.count == 0 || entry.visibility == rhi::ShaderStage::None)
            return {};
        bindings[index] = {entry.binding, type, entry.count, rhi::ToVkShaderStages(entry.visibility), nullptr};
    }
    VkDescriptorSetLayoutCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    createInfo.bindingCount = desc.entryCount;
    createInfo.pBindings = desc.entryCount > 0 ? bindings.data() : nullptr;
    VkDescriptorSetLayout layout = VK_NULL_HANDLE;
    if (vkCreateDescriptorSetLayout(m_device, &createInfo, nullptr, &layout) != VK_SUCCESS)
        return {};
    return Register<rhi::BindingLayoutHandle>(m_bindingLayouts, m_freeBindingLayout,
                                              BindingLayoutPayload{layout, true});
}

rhi::BindGroupHandle VulkanRhiDevice::CreateBindGroup(const rhi::BindGroupDesc &desc)
{
    if (m_device == VK_NULL_HANDLE || !desc.layout.IsValid() || desc.bufferCount > desc.buffers.size() ||
        desc.textureCount > desc.textures.size())
        return {};
    const VkDescriptorSetLayout layout = Resolve(desc.layout);
    if (layout == VK_NULL_HANDLE)
        return {};
    VkDescriptorPool pool = VK_NULL_HANDLE;
    const VkDescriptorSet set = AllocateDescriptorSet(layout, pool);
    if (set == VK_NULL_HANDLE)
        return {};

    std::array<VkDescriptorBufferInfo, rhi::BindGroupDesc::MaxBufferBindings> infos{};
    std::array<VkDescriptorImageInfo, rhi::BindGroupDesc::MaxTextureBindings> imageInfos{};
    std::array<VkWriteDescriptorSet, rhi::BindGroupDesc::MaxBufferBindings + rhi::BindGroupDesc::MaxTextureBindings>
        writes{};
    for (uint32_t index = 0; index < desc.bufferCount; ++index) {
        const auto &binding = desc.buffers[index];
        if (binding.type != rhi::BindingType::StorageBuffer && binding.type != rhi::BindingType::UniformBuffer) {
            vkFreeDescriptorSets(m_device, pool, 1, &set);
            return {};
        }
        const auto *buffer = Resolve(m_buffers, binding.buffer);
        if (!buffer || buffer->buffer == VK_NULL_HANDLE ||
            (buffer->byteSize > 0 &&
             (binding.offset >= buffer->byteSize ||
              (binding.byteSize > 0 && binding.byteSize > buffer->byteSize - binding.offset)))) {
            vkFreeDescriptorSets(m_device, pool, 1, &set);
            return {};
        }
        infos[index] = {buffer->buffer, binding.offset, binding.byteSize > 0 ? binding.byteSize : VK_WHOLE_SIZE};
        writes[index].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        writes[index].dstSet = set;
        writes[index].dstBinding = binding.binding;
        writes[index].descriptorCount = 1;
        writes[index].descriptorType = ToVkDescriptorType(binding.type);
        writes[index].pBufferInfo = &infos[index];
    }
    for (uint32_t index = 0; index < desc.textureCount; ++index) {
        const auto &binding = desc.textures[index];
        if (binding.type != rhi::BindingType::SampledTexture && binding.type != rhi::BindingType::StorageTexture &&
            binding.type != rhi::BindingType::Sampler && binding.type != rhi::BindingType::CombinedTextureSampler) {
            vkFreeDescriptorSets(m_device, pool, 1, &set);
            return {};
        }
        const bool needsTexture = binding.type != rhi::BindingType::Sampler;
        const bool needsSampler =
            binding.type == rhi::BindingType::Sampler || binding.type == rhi::BindingType::CombinedTextureSampler;
        const VkImageView texture = needsTexture ? Resolve(binding.texture) : VK_NULL_HANDLE;
        const VkSampler sampler = needsSampler ? Resolve(binding.sampler) : VK_NULL_HANDLE;
        if ((needsTexture && texture == VK_NULL_HANDLE) || (needsSampler && sampler == VK_NULL_HANDLE)) {
            vkFreeDescriptorSets(m_device, pool, 1, &set);
            return {};
        }
        auto &imageInfo = imageInfos[index];
        imageInfo.imageView = texture;
        imageInfo.sampler = sampler;
        imageInfo.imageLayout = binding.type == rhi::BindingType::StorageTexture
                                    ? VK_IMAGE_LAYOUT_GENERAL
                                    : (binding.depthRead ? VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL
                                                         : VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);
        auto &write = writes[desc.bufferCount + index];
        write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        write.dstSet = set;
        write.dstBinding = binding.binding;
        write.descriptorCount = 1;
        write.descriptorType = ToVkDescriptorType(binding.type);
        write.pImageInfo = &imageInfo;
    }
    const uint32_t writeCount = desc.bufferCount + desc.textureCount;
    if (writeCount > 0)
        vkUpdateDescriptorSets(m_device, writeCount, writes.data(), 0, nullptr);
    return Register<rhi::BindGroupHandle>(m_bindGroups, m_freeBindGroup, BindGroupPayload{set, pool, true});
}

bool VulkanRhiDevice::WriteBuffer(rhi::BufferHandle handle, uint64_t offset, const void *data, uint64_t byteSize)
{
    const auto *buffer = Resolve(m_buffers, handle);
    if (!buffer || !buffer->owned || !buffer->mappedData || !data || byteSize == 0 || offset > buffer->byteSize ||
        byteSize > buffer->byteSize - offset)
        return false;
    std::memcpy(static_cast<std::byte *>(buffer->mappedData) + offset, data, static_cast<size_t>(byteSize));
    return vmaFlushAllocation(m_allocator, buffer->allocation, offset, byteSize) == VK_SUCCESS;
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

rhi::ComputePipelineHandle VulkanRhiDevice::CreateComputePipeline(const rhi::ComputePipelineDesc &desc)
{
    if (m_device == VK_NULL_HANDLE || !desc.computeShader.IsValid() ||
        desc.bindingLayoutCount > desc.bindingLayouts.size() || (desc.pushConstantBytes % 4u) != 0u)
        return {};

    const VkShaderModule shader = Resolve(desc.computeShader);
    if (shader == VK_NULL_HANDLE)
        return {};

    std::array<VkDescriptorSetLayout, rhi::ComputePipelineDesc::MaxBindingLayouts> layouts{};
    for (uint32_t index = 0; index < desc.bindingLayoutCount; ++index) {
        layouts[index] = Resolve(desc.bindingLayouts[index]);
        if (layouts[index] == VK_NULL_HANDLE)
            return {};
    }

    VkPushConstantRange pushConstants{};
    if (desc.pushConstantBytes > 0) {
        pushConstants.stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
        pushConstants.offset = 0;
        pushConstants.size = desc.pushConstantBytes;
    }

    VkPipelineLayoutCreateInfo layoutInfo{};
    layoutInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
    layoutInfo.setLayoutCount = desc.bindingLayoutCount;
    layoutInfo.pSetLayouts = desc.bindingLayoutCount > 0 ? layouts.data() : nullptr;
    layoutInfo.pushConstantRangeCount = desc.pushConstantBytes > 0 ? 1u : 0u;
    layoutInfo.pPushConstantRanges = desc.pushConstantBytes > 0 ? &pushConstants : nullptr;

    VkPipelineLayout layout = VK_NULL_HANDLE;
    if (vkCreatePipelineLayout(m_device, &layoutInfo, nullptr, &layout) != VK_SUCCESS)
        return {};

    VkPipelineShaderStageCreateInfo stage{};
    stage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
    stage.module = shader;
    stage.pName = "main";

    VkComputePipelineCreateInfo pipelineInfo{};
    pipelineInfo.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO;
    pipelineInfo.stage = stage;
    pipelineInfo.layout = layout;

    VkPipeline pipeline = VK_NULL_HANDLE;
    if (vkCreateComputePipelines(m_device, VK_NULL_HANDLE, 1, &pipelineInfo, nullptr, &pipeline) != VK_SUCCESS) {
        vkDestroyPipelineLayout(m_device, layout, nullptr);
        return {};
    }

    return Register<rhi::ComputePipelineHandle>(m_computePipelines, m_freeComputePipeline,
                                                GraphicsPipelinePayload{pipeline, layout, true, true});
}

rhi::GraphicsPipelineHandle VulkanRhiDevice::CreateGraphicsPipeline(const rhi::GraphicsPipelineDesc &desc)
{
    if (m_device == VK_NULL_HANDLE || !desc.vertexShader.IsValid() || !desc.fragmentShader.IsValid() ||
        !desc.renderTargetLayout.IsValid() || desc.bindingLayoutCount > desc.bindingLayouts.size() ||
        desc.colorTargetCount > desc.colorTargets.size() || (desc.pushConstantBytes % 4u) != 0u ||
        (desc.pushConstantBytes > 0 && desc.pushConstantStages == rhi::ShaderStage::None))
        return {};

    const VkShaderModule vertex = Resolve(desc.vertexShader);
    const VkShaderModule fragment = Resolve(desc.fragmentShader);
    const VkRenderPass renderPass = Resolve(desc.renderTargetLayout);
    if (vertex == VK_NULL_HANDLE || fragment == VK_NULL_HANDLE || renderPass == VK_NULL_HANDLE)
        return {};

    std::array<VkDescriptorSetLayout, rhi::GraphicsPipelineDesc::MaxBindingLayouts> layouts{};
    for (uint32_t index = 0; index < desc.bindingLayoutCount; ++index) {
        layouts[index] = Resolve(desc.bindingLayouts[index]);
        if (layouts[index] == VK_NULL_HANDLE)
            return {};
    }

    VkPushConstantRange pushConstants{};
    if (desc.pushConstantBytes > 0) {
        pushConstants.stageFlags = rhi::ToVkShaderStages(desc.pushConstantStages);
        pushConstants.size = desc.pushConstantBytes;
    }
    VkPipelineLayoutCreateInfo layoutInfo{};
    layoutInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
    layoutInfo.setLayoutCount = desc.bindingLayoutCount;
    layoutInfo.pSetLayouts = desc.bindingLayoutCount > 0 ? layouts.data() : nullptr;
    layoutInfo.pushConstantRangeCount = desc.pushConstantBytes > 0 ? 1u : 0u;
    layoutInfo.pPushConstantRanges = desc.pushConstantBytes > 0 ? &pushConstants : nullptr;
    VkPipelineLayout layout = VK_NULL_HANDLE;
    if (vkCreatePipelineLayout(m_device, &layoutInfo, nullptr, &layout) != VK_SUCCESS)
        return {};

    std::array<VkPipelineShaderStageCreateInfo, 2> stages{};
    stages[0].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    stages[0].stage = VK_SHADER_STAGE_VERTEX_BIT;
    stages[0].module = vertex;
    stages[0].pName = "main";
    stages[1].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    stages[1].stage = VK_SHADER_STAGE_FRAGMENT_BIT;
    stages[1].module = fragment;
    stages[1].pName = "main";

    VkPipelineVertexInputStateCreateInfo vertexInput{};
    vertexInput.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
    VkPipelineInputAssemblyStateCreateInfo inputAssembly{};
    inputAssembly.sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO;
    inputAssembly.topology = ToVkTopology(desc.topology);

    VkPipelineViewportStateCreateInfo viewport{};
    viewport.sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO;
    viewport.viewportCount = 1;
    viewport.scissorCount = 1;

    VkPipelineRasterizationStateCreateInfo raster{};
    raster.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
    raster.polygonMode = desc.raster.wireframe ? VK_POLYGON_MODE_LINE : VK_POLYGON_MODE_FILL;
    raster.cullMode = ToVkCullMode(desc.raster.cullMode);
    raster.frontFace = ToVkFrontFace(desc.raster.frontFace);
    raster.lineWidth = 1.0f;

    VkPipelineMultisampleStateCreateInfo multisample{};
    multisample.sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO;
    multisample.rasterizationSamples = rhi::ToVkSampleCount(desc.samples);

    VkPipelineDepthStencilStateCreateInfo depth{};
    depth.sType = VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
    depth.depthTestEnable = desc.depth.testEnabled ? VK_TRUE : VK_FALSE;
    depth.depthWriteEnable = desc.depth.writeEnabled ? VK_TRUE : VK_FALSE;
    depth.depthCompareOp = ToVkCompareOp(desc.depth.compare);

    std::array<VkPipelineColorBlendAttachmentState, rhi::GraphicsPipelineDesc::MaxColorTargets> attachments{};
    for (uint32_t index = 0; index < desc.colorTargetCount; ++index) {
        const auto &target = desc.colorTargets[index];
        if (!rhi::IsValidPixelFormat(target.format) || rhi::IsDepthFormat(target.format)) {
            vkDestroyPipelineLayout(m_device, layout, nullptr);
            return {};
        }
        auto &attachment = attachments[index];
        attachment.blendEnable = target.blendEnabled ? VK_TRUE : VK_FALSE;
        attachment.srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;
        attachment.dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
        attachment.colorBlendOp = VK_BLEND_OP_ADD;
        attachment.srcAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
        attachment.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
        attachment.alphaBlendOp = VK_BLEND_OP_ADD;
        attachment.colorWriteMask = target.writeMask;
    }
    VkPipelineColorBlendStateCreateInfo blend{};
    blend.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;
    blend.attachmentCount = desc.colorTargetCount;
    blend.pAttachments = desc.colorTargetCount > 0 ? attachments.data() : nullptr;

    constexpr std::array<VkDynamicState, 2> dynamicStates = {VK_DYNAMIC_STATE_VIEWPORT, VK_DYNAMIC_STATE_SCISSOR};
    VkPipelineDynamicStateCreateInfo dynamic{};
    dynamic.sType = VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO;
    dynamic.dynamicStateCount = static_cast<uint32_t>(dynamicStates.size());
    dynamic.pDynamicStates = dynamicStates.data();

    VkGraphicsPipelineCreateInfo pipelineInfo{};
    pipelineInfo.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
    pipelineInfo.stageCount = static_cast<uint32_t>(stages.size());
    pipelineInfo.pStages = stages.data();
    pipelineInfo.pVertexInputState = &vertexInput;
    pipelineInfo.pInputAssemblyState = &inputAssembly;
    pipelineInfo.pViewportState = &viewport;
    pipelineInfo.pRasterizationState = &raster;
    pipelineInfo.pMultisampleState = &multisample;
    pipelineInfo.pDepthStencilState = &depth;
    pipelineInfo.pColorBlendState = &blend;
    pipelineInfo.pDynamicState = &dynamic;
    pipelineInfo.layout = layout;
    pipelineInfo.renderPass = renderPass;

    VkPipeline pipeline = VK_NULL_HANDLE;
    if (vkCreateGraphicsPipelines(m_device, VK_NULL_HANDLE, 1, &pipelineInfo, nullptr, &pipeline) != VK_SUCCESS) {
        vkDestroyPipelineLayout(m_device, layout, nullptr);
        return {};
    }
    return Register<rhi::GraphicsPipelineHandle>(m_graphicsPipelines, m_freeGraphicsPipeline,
                                                 GraphicsPipelinePayload{pipeline, layout, true, true});
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
    const auto *payload = Resolve(m_buffers, handle);
    if (payload && payload->owned && m_allocator != VK_NULL_HANDLE && payload->buffer != VK_NULL_HANDLE)
        vmaDestroyBuffer(m_allocator, payload->buffer, payload->allocation);
    Release(m_buffers, m_freeBuffer, handle);
}

void VulkanRhiDevice::Release(rhi::TextureHandle handle) noexcept
{
    Release(m_textures, m_freeTexture, handle);
}
void VulkanRhiDevice::Release(rhi::SamplerHandle handle) noexcept
{
    Release(m_samplers, m_freeSampler, handle);
}
void VulkanRhiDevice::Release(rhi::ShaderModuleHandle handle) noexcept
{
    const auto *payload = Resolve(m_shaderModules, handle);
    if (payload && payload->owned && m_device != VK_NULL_HANDLE && payload->module != VK_NULL_HANDLE)
        vkDestroyShaderModule(m_device, payload->module, nullptr);
    Release(m_shaderModules, m_freeShaderModule, handle);
}
void VulkanRhiDevice::Release(rhi::BindingLayoutHandle handle) noexcept
{
    const auto *payload = Resolve(m_bindingLayouts, handle);
    if (payload && payload->owned && m_device != VK_NULL_HANDLE && payload->layout != VK_NULL_HANDLE)
        vkDestroyDescriptorSetLayout(m_device, payload->layout, nullptr);
    Release(m_bindingLayouts, m_freeBindingLayout, handle);
}
void VulkanRhiDevice::Release(rhi::BindGroupHandle handle) noexcept
{
    const auto *payload = Resolve(m_bindGroups, handle);
    if (payload && payload->owned && m_device != VK_NULL_HANDLE && payload->set != VK_NULL_HANDLE &&
        payload->pool != VK_NULL_HANDLE)
        vkFreeDescriptorSets(m_device, payload->pool, 1, &payload->set);
    Release(m_bindGroups, m_freeBindGroup, handle);
}
void VulkanRhiDevice::Release(rhi::GraphicsPipelineHandle handle) noexcept
{
    const auto *payload = ResolvePipeline(handle);
    if (payload && m_device != VK_NULL_HANDLE) {
        if (payload->ownsPipeline && payload->pipeline != VK_NULL_HANDLE)
            vkDestroyPipeline(m_device, payload->pipeline, nullptr);
        if (payload->ownsLayout && payload->layout != VK_NULL_HANDLE)
            vkDestroyPipelineLayout(m_device, payload->layout, nullptr);
    }
    Release(m_graphicsPipelines, m_freeGraphicsPipeline, handle);
}
void VulkanRhiDevice::Release(rhi::ComputePipelineHandle handle) noexcept
{
    const auto *payload = ResolvePipeline(handle);
    if (payload && m_device != VK_NULL_HANDLE) {
        if (payload->ownsPipeline && payload->pipeline != VK_NULL_HANDLE)
            vkDestroyPipeline(m_device, payload->pipeline, nullptr);
        if (payload->ownsLayout && payload->layout != VK_NULL_HANDLE)
            vkDestroyPipelineLayout(m_device, payload->layout, nullptr);
    }
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
    return payload ? payload->buffer : VK_NULL_HANDLE;
}

VkImage VulkanRhiDevice::Resolve(rhi::TextureHandle handle) const noexcept
{
    const auto *image = Resolve(m_textures, handle);
    return image ? *image : VK_NULL_HANDLE;
}
VkSampler VulkanRhiDevice::Resolve(rhi::SamplerHandle handle) const noexcept
{
    const auto *payload = Resolve(m_samplers, handle);
    return payload ? *payload : VK_NULL_HANDLE;
}
VkShaderModule VulkanRhiDevice::Resolve(rhi::ShaderModuleHandle handle) const noexcept
{
    const auto *payload = Resolve(m_shaderModules, handle);
    return payload ? payload->module : VK_NULL_HANDLE;
}
VkDescriptorSetLayout VulkanRhiDevice::Resolve(rhi::BindingLayoutHandle handle) const noexcept
{
    const auto *payload = Resolve(m_bindingLayouts, handle);
    return payload ? payload->layout : VK_NULL_HANDLE;
}
VkDescriptorSet VulkanRhiDevice::Resolve(rhi::BindGroupHandle handle) const noexcept
{
    const auto *payload = Resolve(m_bindGroups, handle);
    return payload ? payload->set : VK_NULL_HANDLE;
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

VkDescriptorPool VulkanRhiDevice::CreateDescriptorPool()
{
    if (m_device == VK_NULL_HANDLE)
        return VK_NULL_HANDLE;
    const std::array<VkDescriptorPoolSize, 6> sizes = {
        VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 4096},
        VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 1024},
        VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE, 1024},
        VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 1024},
        VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_SAMPLER, 1024},
        VkDescriptorPoolSize{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 4096},
    };
    VkDescriptorPoolCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
    createInfo.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
    createInfo.maxSets = 512;
    createInfo.poolSizeCount = static_cast<uint32_t>(sizes.size());
    createInfo.pPoolSizes = sizes.data();
    VkDescriptorPool pool = VK_NULL_HANDLE;
    return vkCreateDescriptorPool(m_device, &createInfo, nullptr, &pool) == VK_SUCCESS ? pool : VK_NULL_HANDLE;
}

VkDescriptorSet VulkanRhiDevice::AllocateDescriptorSet(VkDescriptorSetLayout layout, VkDescriptorPool &pool)
{
    VkDescriptorSetAllocateInfo allocateInfo{};
    allocateInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
    allocateInfo.descriptorSetCount = 1;
    allocateInfo.pSetLayouts = &layout;
    for (VkDescriptorPool candidate : m_ownedDescriptorPools) {
        allocateInfo.descriptorPool = candidate;
        VkDescriptorSet set = VK_NULL_HANDLE;
        if (vkAllocateDescriptorSets(m_device, &allocateInfo, &set) == VK_SUCCESS) {
            pool = candidate;
            return set;
        }
    }
    const VkDescriptorPool created = CreateDescriptorPool();
    if (created == VK_NULL_HANDLE)
        return VK_NULL_HANDLE;
    m_ownedDescriptorPools.push_back(created);
    allocateInfo.descriptorPool = created;
    VkDescriptorSet set = VK_NULL_HANDLE;
    if (vkAllocateDescriptorSets(m_device, &allocateInfo, &set) != VK_SUCCESS)
        return VK_NULL_HANDLE;
    pool = created;
    return set;
}

void VulkanRhiDevice::DestroyOwnedResources() noexcept
{
    if (m_device == VK_NULL_HANDLE)
        return;
    for (auto &slot : m_graphicsPipelines) {
        if (!slot.occupied)
            continue;
        if (slot.payload.ownsPipeline && slot.payload.pipeline != VK_NULL_HANDLE)
            vkDestroyPipeline(m_device, slot.payload.pipeline, nullptr);
        if (slot.payload.ownsLayout && slot.payload.layout != VK_NULL_HANDLE)
            vkDestroyPipelineLayout(m_device, slot.payload.layout, nullptr);
        slot.payload.ownsPipeline = false;
        slot.payload.ownsLayout = false;
    }
    for (auto &slot : m_computePipelines) {
        if (!slot.occupied)
            continue;
        if (slot.payload.ownsPipeline && slot.payload.pipeline != VK_NULL_HANDLE)
            vkDestroyPipeline(m_device, slot.payload.pipeline, nullptr);
        if (slot.payload.ownsLayout && slot.payload.layout != VK_NULL_HANDLE)
            vkDestroyPipelineLayout(m_device, slot.payload.layout, nullptr);
        slot.payload.ownsPipeline = false;
        slot.payload.ownsLayout = false;
    }
    for (auto &slot : m_bindGroups) {
        if (slot.occupied && slot.payload.owned && slot.payload.set != VK_NULL_HANDLE &&
            slot.payload.pool != VK_NULL_HANDLE)
            vkFreeDescriptorSets(m_device, slot.payload.pool, 1, &slot.payload.set);
        slot.payload.owned = false;
    }
    for (auto &slot : m_bindingLayouts) {
        if (slot.occupied && slot.payload.owned && slot.payload.layout != VK_NULL_HANDLE)
            vkDestroyDescriptorSetLayout(m_device, slot.payload.layout, nullptr);
        slot.payload.owned = false;
    }
    for (auto &slot : m_shaderModules) {
        if (slot.occupied && slot.payload.owned && slot.payload.module != VK_NULL_HANDLE)
            vkDestroyShaderModule(m_device, slot.payload.module, nullptr);
        slot.payload.owned = false;
    }
    if (m_allocator != VK_NULL_HANDLE) {
        for (auto &slot : m_buffers) {
            if (slot.occupied && slot.payload.owned && slot.payload.buffer != VK_NULL_HANDLE)
                vmaDestroyBuffer(m_allocator, slot.payload.buffer, slot.payload.allocation);
            slot.payload.owned = false;
        }
    }
    for (VkDescriptorPool pool : m_ownedDescriptorPools) {
        if (pool != VK_NULL_HANDLE)
            vkDestroyDescriptorPool(m_device, pool, nullptr);
    }
    m_ownedDescriptorPools.clear();
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

rhi::TransferCommandEncoder VulkanRhiDevice::MakeTransferCommandEncoder(VulkanTransferCommandContext &context,
                                                                        VkCommandBuffer commandBuffer) noexcept
{
    context = {this, commandBuffer};
    return {&context, &s_transferDispatch};
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

void VulkanRhiDevice::CopyBuffer(void *context, rhi::BufferHandle source, rhi::BufferHandle destination,
                                 const rhi::BufferCopyRegion &region)
{
    auto &command = *static_cast<VulkanTransferCommandContext *>(context);
    const VkBuffer nativeSource = command.device ? command.device->Resolve(source) : VK_NULL_HANDLE;
    const VkBuffer nativeDestination = command.device ? command.device->Resolve(destination) : VK_NULL_HANDLE;
    if (command.commandBuffer == VK_NULL_HANDLE || nativeSource == VK_NULL_HANDLE ||
        nativeDestination == VK_NULL_HANDLE || region.byteSize == 0)
        return;
    const VkBufferCopy copy{region.sourceOffset, region.destinationOffset, region.byteSize};
    vkCmdCopyBuffer(command.commandBuffer, nativeSource, nativeDestination, 1, &copy);
}

void VulkanRhiDevice::CopyTexture(void *context, rhi::TextureHandle source, rhi::TextureHandle destination,
                                  const rhi::TextureCopyRegion &region)
{
    auto &command = *static_cast<VulkanTransferCommandContext *>(context);
    const VkImage nativeSource = command.device ? command.device->Resolve(source) : VK_NULL_HANDLE;
    const VkImage nativeDestination = command.device ? command.device->Resolve(destination) : VK_NULL_HANDLE;
    if (command.commandBuffer == VK_NULL_HANDLE || nativeSource == VK_NULL_HANDLE ||
        nativeDestination == VK_NULL_HANDLE || region.width == 0 || region.height == 0 || region.depth == 0)
        return;

    VkImageAspectFlags aspect = VK_IMAGE_ASPECT_COLOR_BIT;
    switch (region.aspect) {
    case rhi::TextureAspect::Color:
        break;
    case rhi::TextureAspect::Depth:
        aspect = VK_IMAGE_ASPECT_DEPTH_BIT;
        break;
    case rhi::TextureAspect::Stencil:
        aspect = VK_IMAGE_ASPECT_STENCIL_BIT;
        break;
    case rhi::TextureAspect::DepthStencil:
        aspect = VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT;
        break;
    }

    VkImageCopy copy{};
    copy.srcSubresource = {aspect, region.sourceMip, region.sourceLayer, 1};
    copy.dstSubresource = {aspect, region.destinationMip, region.destinationLayer, 1};
    copy.extent = {region.width, region.height, region.depth};
    vkCmdCopyImage(command.commandBuffer, nativeSource, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, nativeDestination,
                   VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &copy);
}

} // namespace infernux::vk
