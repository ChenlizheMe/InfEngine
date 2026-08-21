/**
 * @file VkCoreGlobals.cpp
 * @brief InxVkCoreModular — Engine globals UBO (set 2, binding 0)
 *
 * Split from InxVkCoreModular.cpp for maintainability.
 * Contains: StageGlobals, CreateGlobalsBuffers,
 *           CreateGlobalsDescriptorResources, DestroyGlobalsDescriptorResources,
 *           CmdUpdateGlobals.
 */

#include "InxError.h"
#include "InxVkCoreModular.h"
#include "vk/VkRenderUtils.h"

#include <function/renderer/shader/ShaderProgram.h>

#include <cstring>

namespace infernux
{

// ============================================================================
// Public API
// ============================================================================

void InxVkCoreModular::StageGlobals(const EngineGlobalsUBO &globals)
{
    m_stagedGlobals = globals;
    m_globalsDirty = true;
}

// ============================================================================
// Buffer creation
// ============================================================================

void InxVkCoreModular::CreateGlobalsBuffers()
{
    constexpr VkDeviceSize bufferSize = sizeof(EngineGlobalsUBO);

    m_globalsBuffers.resize(m_maxFramesInFlight);
    for (size_t i = 0; i < m_maxFramesInFlight; ++i) {
        m_globalsBuffers[i] = m_resourceManager.CreateUniformBuffer(bufferSize);
    }

    INXLOG_INFO("Created engine globals UBO buffers: ", bufferSize, " bytes x ", m_maxFramesInFlight, " frames");
}

// ============================================================================
// Descriptor resources (layout + pool + sets)
// ============================================================================

bool InxVkCoreModular::CreateGlobalsDescriptorResources()
{
    VkDevice device = GetDevice();
    if (device == VK_NULL_HANDLE)
        return false;

    // Layout: set 2, binding 0 = uniform buffer (globals UBO), vertex + fragment
    //         set 2, binding 1 = storage buffer (instance models), vertex only
    //         set 2, binding 2 = storage buffer (skin instance metadata), vertex only
    //         set 2, binding 3 = storage buffer (bone palettes), vertex only
    //         set 2, binding 4 = optional previous transform + object ID stream, vertex only
    VkDescriptorSetLayoutBinding bindings[5]{};

    bindings[0].binding = 0;
    bindings[0].descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    bindings[0].descriptorCount = 1;
    bindings[0].stageFlags = VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT;
    bindings[0].pImmutableSamplers = nullptr;

    bindings[1].binding = 1;
    bindings[1].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    bindings[1].descriptorCount = 1;
    bindings[1].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
    bindings[1].pImmutableSamplers = nullptr;

    bindings[2].binding = 2;
    bindings[2].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    bindings[2].descriptorCount = 1;
    bindings[2].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
    bindings[2].pImmutableSamplers = nullptr;

    bindings[3].binding = 3;
    bindings[3].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    bindings[3].descriptorCount = 1;
    bindings[3].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
    bindings[3].pImmutableSamplers = nullptr;

    bindings[4].binding = 4;
    bindings[4].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    bindings[4].descriptorCount = 1;
    bindings[4].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
    bindings[4].pImmutableSamplers = nullptr;

    VkDescriptorSetLayoutCreateInfo layoutInfo{};
    layoutInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    layoutInfo.bindingCount = 5;
    layoutInfo.pBindings = bindings;

    if (vkCreateDescriptorSetLayout(device, &layoutInfo, nullptr, &m_globalsDescSetLayout) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create globals descriptor set layout");
        return false;
    }

    // Publish to ShaderProgram so all pipelines pick up the shared layout at set 2
    ShaderProgram::SetGlobalsDescSetLayout(m_globalsDescSetLayout);

    auto &descriptorManager = m_backend.Device().GetRhiDevice().GetDescriptorManager();
    m_globalsDescriptorLeases.clear();
    m_globalsDescriptorLeases.reserve(m_maxFramesInFlight);
    m_globalsDescSets.resize(m_maxFramesInFlight);
    for (size_t index = 0; index < m_maxFramesInFlight; ++index) {
        auto lease = descriptorManager.Allocate(m_globalsDescSetLayout, vk::DescriptorArena::Persistent);
        if (!lease.IsValid()) {
            INXLOG_ERROR("Failed to allocate globals descriptor set ", index);
            for (const auto &allocated : m_globalsDescriptorLeases)
                descriptorManager.Retire(allocated);
            m_globalsDescriptorLeases.clear();
            m_globalsDescSets.clear();
            vkDestroyDescriptorSetLayout(device, m_globalsDescSetLayout, nullptr);
            m_globalsDescSetLayout = VK_NULL_HANDLE;
            ShaderProgram::SetGlobalsDescSetLayout(VK_NULL_HANDLE);
            return false;
        }
        m_globalsDescSets[index] = lease.set;
        m_globalsDescriptorLeases.push_back(lease);
    }

    // Write each descriptor set to point at the corresponding globals buffer
    // and a placeholder instance SSBO
    m_instanceBuffers.resize(m_maxFramesInFlight);
    m_skinInstanceBuffers.resize(m_maxFramesInFlight);
    m_skinPaletteBuffers.resize(m_maxFramesInFlight);
    m_instanceAuxBuffers.resize(m_maxFramesInFlight);
    for (size_t i = 0; i < m_maxFramesInFlight; ++i) {
        // Create initial instance buffer for this frame
        const VkDeviceSize initialBytes = INSTANCE_BUFFER_INITIAL_CAPACITY * sizeof(glm::mat4);
        m_instanceBuffers[i].buffer = m_resourceManager.CreateStorageBuffer(initialBytes, /*deviceLocal=*/false);
        m_instanceBuffers[i].capacity = INSTANCE_BUFFER_INITIAL_CAPACITY;

        const VkDeviceSize skinInstanceBytes = SKIN_INSTANCE_BUFFER_INITIAL_CAPACITY * sizeof(GPUSkinInstanceData);
        m_skinInstanceBuffers[i].buffer =
            m_resourceManager.CreateStorageBuffer(skinInstanceBytes, /*deviceLocal=*/false);
        m_skinInstanceBuffers[i].capacity = SKIN_INSTANCE_BUFFER_INITIAL_CAPACITY;

        const VkDeviceSize skinPaletteBytes = SKIN_PALETTE_BUFFER_INITIAL_CAPACITY * sizeof(glm::mat4);
        m_skinPaletteBuffers[i].buffer = m_resourceManager.CreateStorageBuffer(skinPaletteBytes, /*deviceLocal=*/false);
        m_skinPaletteBuffers[i].capacity = SKIN_PALETTE_BUFFER_INITIAL_CAPACITY;

        const VkDeviceSize instanceAuxBytes = INSTANCE_AUX_BUFFER_INITIAL_CAPACITY * sizeof(GPUInstanceAuxData);
        m_instanceAuxBuffers[i].buffer = m_resourceManager.CreateStorageBuffer(instanceAuxBytes, /*deviceLocal=*/false);
        m_instanceAuxBuffers[i].capacity = INSTANCE_AUX_BUFFER_INITIAL_CAPACITY;

        if (!m_instanceBuffers[i].buffer || !m_skinInstanceBuffers[i].buffer || !m_skinPaletteBuffers[i].buffer ||
            !m_instanceAuxBuffers[i].buffer) {
            INXLOG_ERROR("Failed to create globals SSBOs for frame ", i);
            continue;
        }

        m_instanceBuffers[i].mapped = m_instanceBuffers[i].buffer->Map();
        m_skinInstanceBuffers[i].mapped = m_skinInstanceBuffers[i].buffer->Map();
        m_skinPaletteBuffers[i].mapped = m_skinPaletteBuffers[i].buffer->Map();
        m_instanceAuxBuffers[i].mapped = m_instanceAuxBuffers[i].buffer->Map();

        VkWriteDescriptorSet writes[5]{};

        // Binding 0: Globals UBO
        VkDescriptorBufferInfo uboBufInfo{};
        if (m_globalsBuffers[i]) {
            uboBufInfo.buffer = m_globalsBuffers[i]->GetBuffer();
            uboBufInfo.offset = 0;
            uboBufInfo.range = sizeof(EngineGlobalsUBO);
        }

        writes[0].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        writes[0].dstSet = m_globalsDescSets[i];
        writes[0].dstBinding = 0;
        writes[0].dstArrayElement = 0;
        writes[0].descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
        writes[0].descriptorCount = 1;
        writes[0].pBufferInfo = &uboBufInfo;

        // Binding 1: Instance SSBO
        VkDescriptorBufferInfo ssboBufInfo{};
        ssboBufInfo.buffer = m_instanceBuffers[i].buffer->GetBuffer();
        ssboBufInfo.offset = 0;
        ssboBufInfo.range = VK_WHOLE_SIZE;

        writes[1].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        writes[1].dstSet = m_globalsDescSets[i];
        writes[1].dstBinding = 1;
        writes[1].dstArrayElement = 0;
        writes[1].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        writes[1].descriptorCount = 1;
        writes[1].pBufferInfo = &ssboBufInfo;

        VkDescriptorBufferInfo skinInstanceInfo{};
        skinInstanceInfo.buffer = m_skinInstanceBuffers[i].buffer->GetBuffer();
        skinInstanceInfo.offset = 0;
        skinInstanceInfo.range = VK_WHOLE_SIZE;

        writes[2].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        writes[2].dstSet = m_globalsDescSets[i];
        writes[2].dstBinding = 2;
        writes[2].dstArrayElement = 0;
        writes[2].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        writes[2].descriptorCount = 1;
        writes[2].pBufferInfo = &skinInstanceInfo;

        VkDescriptorBufferInfo skinPaletteInfo{};
        skinPaletteInfo.buffer = m_skinPaletteBuffers[i].buffer->GetBuffer();
        skinPaletteInfo.offset = 0;
        skinPaletteInfo.range = VK_WHOLE_SIZE;

        writes[3].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        writes[3].dstSet = m_globalsDescSets[i];
        writes[3].dstBinding = 3;
        writes[3].dstArrayElement = 0;
        writes[3].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        writes[3].descriptorCount = 1;
        writes[3].pBufferInfo = &skinPaletteInfo;

        VkDescriptorBufferInfo instanceAuxInfo{};
        instanceAuxInfo.buffer = m_instanceAuxBuffers[i].buffer->GetBuffer();
        instanceAuxInfo.offset = 0;
        instanceAuxInfo.range = VK_WHOLE_SIZE;

        writes[4].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        writes[4].dstSet = m_globalsDescSets[i];
        writes[4].dstBinding = 4;
        writes[4].dstArrayElement = 0;
        writes[4].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        writes[4].descriptorCount = 1;
        writes[4].pBufferInfo = &instanceAuxInfo;

        vkUpdateDescriptorSets(device, 5, writes, 0, nullptr);
    }

    INXLOG_INFO("Created globals descriptor set layout, pool, and ", m_maxFramesInFlight,
                " sets (set 2) with instance, skinning, and optional auxiliary SSBOs");
    return true;
}

void InxVkCoreModular::DestroyGlobalsDescriptorResources()
{
    VkDevice device = GetDevice();
    if (device == VK_NULL_HANDLE)
        return;

    m_instanceBuffers.clear();
    m_skinInstanceBuffers.clear();
    m_skinPaletteBuffers.clear();
    m_instanceAuxBuffers.clear();
    m_instanceHistory.Clear();
    m_globalsDescSets.clear();
    auto &descriptorManager = m_backend.Device().GetRhiDevice().GetDescriptorManager();
    for (const auto &lease : m_globalsDescriptorLeases)
        descriptorManager.Retire(lease);
    m_globalsDescriptorLeases.clear();
    if (m_globalsDescSetLayout != VK_NULL_HANDLE) {
        ShaderProgram::SetGlobalsDescSetLayout(VK_NULL_HANDLE);
        vkDestroyDescriptorSetLayout(device, m_globalsDescSetLayout, nullptr);
        m_globalsDescSetLayout = VK_NULL_HANDLE;
    }
}

// ============================================================================
// Instance buffer management
// ============================================================================

void InxVkCoreModular::EnsureInstanceBufferCapacity(uint32_t frameIndex, size_t instanceCount)
{
    if (frameIndex >= m_instanceBuffers.size())
        return;

    auto &frame = m_instanceBuffers[frameIndex];
    if (frame.capacity >= instanceCount && frame.buffer) {
        if (!frame.mapped) {
            frame.mapped = frame.buffer->Map();
        }
        return;
    }

    std::unique_ptr<vk::VkBufferHandle> oldBuffer = std::move(frame.buffer);
    void *oldMapped = frame.mapped;
    frame.mapped = nullptr;

    // Grow to next power-of-two that fits
    size_t newCapacity = frame.capacity > 0 ? frame.capacity : INSTANCE_BUFFER_INITIAL_CAPACITY;
    while (newCapacity < instanceCount)
        newCapacity *= 2;

    const VkDeviceSize newBytes = newCapacity * sizeof(glm::mat4);
    auto newBuffer = m_resourceManager.CreateStorageBuffer(newBytes, /*deviceLocal=*/false);
    if (!newBuffer) {
        INXLOG_ERROR("Failed to grow instance buffer to ", newCapacity, " instances (", newBytes, " bytes)");
        frame.buffer = std::move(oldBuffer);
        frame.mapped = oldMapped;
        return;
    }

    void *newMapped = newBuffer->Map();

    // Preserve existing data written earlier in this frame
    if (oldMapped && newMapped && m_instanceWriteOffset > 0) {
        std::memcpy(newMapped, oldMapped, m_instanceWriteOffset * sizeof(glm::mat4));
    }

    frame.buffer = std::move(newBuffer);
    frame.capacity = newCapacity;
    frame.mapped = newMapped;

    // NOTE: Do NOT call UpdateInstanceBufferDescriptor() here.
    // This function can be called mid-recording, and updating a descriptor set
    // that is already bound in the command buffer invalidates it.
    // PreallocateInstances() publishes a descriptor revision only when the
    // backing buffer actually changes.

    // Old instance buffers may still be referenced by commands already
    // recorded earlier in this same command buffer. Defer final destruction
    // until the submission retirement queue says all in-flight use is complete.
    if (oldBuffer) {
        auto retiredBuffer = std::shared_ptr<vk::VkBufferHandle>(oldBuffer.release());
        m_deletionQueue.Retire([retiredBuffer]() mutable { retiredBuffer.reset(); });
    }
}

void InxVkCoreModular::EnsureSkinBuffersCapacity(uint32_t frameIndex, size_t skinInstanceCount, size_t boneMatrixCount)
{
    if (frameIndex >= m_skinInstanceBuffers.size() || frameIndex >= m_skinPaletteBuffers.size())
        return;

    auto growBuffer = [&](SkinBufferFrame &frame, size_t requiredCount, size_t initialCapacity, size_t elementSize,
                          const char *label) {
        if (frame.capacity >= requiredCount && frame.buffer) {
            if (!frame.mapped)
                frame.mapped = frame.buffer->Map();
            return;
        }

        std::unique_ptr<vk::VkBufferHandle> oldBuffer = std::move(frame.buffer);
        void *oldMapped = frame.mapped;
        frame.mapped = nullptr;

        size_t newCapacity = frame.capacity > 0 ? static_cast<size_t>(frame.capacity) : initialCapacity;
        while (newCapacity < requiredCount)
            newCapacity *= 2;

        auto newBuffer =
            m_resourceManager.CreateStorageBuffer(static_cast<VkDeviceSize>(newCapacity * elementSize), false);
        if (!newBuffer) {
            INXLOG_ERROR("Failed to grow ", label, " buffer to ", newCapacity, " elements");
            frame.buffer = std::move(oldBuffer);
            frame.mapped = oldMapped;
            return;
        }

        void *newMapped = newBuffer->Map();
        if (oldMapped && newMapped) {
            const size_t preserved =
                std::min(static_cast<size_t>(frame.capacity), requiredCount) * static_cast<size_t>(elementSize);
            std::memcpy(newMapped, oldMapped, preserved);
        }

        frame.buffer = std::move(newBuffer);
        frame.capacity = static_cast<VkDeviceSize>(newCapacity);
        frame.mapped = newMapped;

        if (oldBuffer) {
            auto retiredBuffer = std::shared_ptr<vk::VkBufferHandle>(oldBuffer.release());
            m_deletionQueue.Retire([retiredBuffer]() mutable { retiredBuffer.reset(); });
        }
    };

    growBuffer(m_skinInstanceBuffers[frameIndex], skinInstanceCount, SKIN_INSTANCE_BUFFER_INITIAL_CAPACITY,
               sizeof(GPUSkinInstanceData), "skin instance");
    growBuffer(m_skinPaletteBuffers[frameIndex], boneMatrixCount, SKIN_PALETTE_BUFFER_INITIAL_CAPACITY,
               sizeof(glm::mat4), "skin palette");
}

void InxVkCoreModular::EnsureInstanceAuxBufferCapacity(uint32_t frameIndex, size_t instanceCount)
{
    if (frameIndex >= m_instanceAuxBuffers.size())
        return;

    auto &frame = m_instanceAuxBuffers[frameIndex];
    if (frame.capacity >= instanceCount && frame.buffer) {
        if (!frame.mapped)
            frame.mapped = frame.buffer->Map();
        return;
    }

    std::unique_ptr<vk::VkBufferHandle> oldBuffer = std::move(frame.buffer);
    void *oldMapped = frame.mapped;
    frame.mapped = nullptr;
    size_t newCapacity =
        frame.capacity > 0 ? static_cast<size_t>(frame.capacity) : INSTANCE_AUX_BUFFER_INITIAL_CAPACITY;
    while (newCapacity < instanceCount)
        newCapacity *= 2;

    auto newBuffer =
        m_resourceManager.CreateStorageBuffer(newCapacity * sizeof(GPUInstanceAuxData), /*deviceLocal=*/false);
    if (!newBuffer) {
        INXLOG_ERROR("Failed to grow instance auxiliary buffer to ", newCapacity, " instances");
        frame.buffer = std::move(oldBuffer);
        frame.mapped = oldMapped;
        return;
    }

    void *newMapped = newBuffer->Map();
    if (oldMapped && newMapped && frame.capacity > 0)
        std::memcpy(newMapped, oldMapped, static_cast<size_t>(frame.capacity) * sizeof(GPUInstanceAuxData));

    frame.buffer = std::move(newBuffer);
    frame.capacity = static_cast<VkDeviceSize>(newCapacity);
    frame.mapped = newMapped;

    if (oldBuffer) {
        auto retiredBuffer = std::shared_ptr<vk::VkBufferHandle>(oldBuffer.release());
        m_deletionQueue.Retire([retiredBuffer]() mutable { retiredBuffer.reset(); });
    }
}

void InxVkCoreModular::ResetPerFrameGpuStreamOffsets()
{
    if (m_lastInstanceFrame != m_currentFrame) {
        m_instanceWriteOffset = 0;
        m_skinPaletteWriteOffset = 0;
        m_skinPaletteFrameCache.clear();
        m_lastInstanceFrame = m_currentFrame;
    }
}

void InxVkCoreModular::PreallocateInstances(size_t requiredInstances)
{
    const uint32_t frameIndex = m_currentFrame % m_maxFramesInFlight;

    ResetPerFrameGpuStreamOffsets();

    if (requiredInstances == 0)
        return;

    const VkBuffer previousInstanceBuffer =
        m_instanceBuffers[frameIndex].buffer ? m_instanceBuffers[frameIndex].buffer->GetBuffer() : VK_NULL_HANDLE;
    const VkBuffer previousSkinInstanceBuffer = m_skinInstanceBuffers[frameIndex].buffer
                                                    ? m_skinInstanceBuffers[frameIndex].buffer->GetBuffer()
                                                    : VK_NULL_HANDLE;
    const VkBuffer previousSkinPaletteBuffer =
        m_skinPaletteBuffers[frameIndex].buffer ? m_skinPaletteBuffers[frameIndex].buffer->GetBuffer() : VK_NULL_HANDLE;
    const VkBuffer previousInstanceAuxBuffer =
        m_instanceAuxBuffers[frameIndex].buffer ? m_instanceAuxBuffers[frameIndex].buffer->GetBuffer() : VK_NULL_HANDLE;

    EnsureInstanceBufferCapacity(frameIndex, requiredInstances);
    EnsureSkinBuffersCapacity(frameIndex, requiredInstances, SKIN_PALETTE_BUFFER_INITIAL_CAPACITY);
    EnsureInstanceAuxBufferCapacity(frameIndex, requiredInstances);

    // Descriptor sets may also be referenced by asynchronous preview command
    // buffers. Even rewriting an identical binding invalidates those recorded
    // commands, so only publish a descriptor revision when storage changed.
    const bool instanceChanged = m_instanceBuffers[frameIndex].buffer &&
                                 previousInstanceBuffer != m_instanceBuffers[frameIndex].buffer->GetBuffer();
    const bool skinChanged = (m_skinInstanceBuffers[frameIndex].buffer &&
                              previousSkinInstanceBuffer != m_skinInstanceBuffers[frameIndex].buffer->GetBuffer()) ||
                             (m_skinPaletteBuffers[frameIndex].buffer &&
                              previousSkinPaletteBuffer != m_skinPaletteBuffers[frameIndex].buffer->GetBuffer());
    const bool instanceAuxChanged = m_instanceAuxBuffers[frameIndex].buffer &&
                                    previousInstanceAuxBuffer != m_instanceAuxBuffers[frameIndex].buffer->GetBuffer();
    if (instanceChanged || skinChanged || instanceAuxChanged)
        (void)PublishGlobalsDescriptorRevision(frameIndex);
}

bool InxVkCoreModular::WriteInstanceMatrix(uint32_t frameIndex, uint32_t instanceIndex, const glm::mat4 &matrix)
{
    if (frameIndex >= m_instanceBuffers.size())
        return false;

    const size_t requiredCount = static_cast<size_t>(instanceIndex) + 1;
    const VkBuffer previousBuffer =
        m_instanceBuffers[frameIndex].buffer ? m_instanceBuffers[frameIndex].buffer->GetBuffer() : VK_NULL_HANDLE;

    EnsureInstanceBufferCapacity(frameIndex, requiredCount);

    auto &frame = m_instanceBuffers[frameIndex];
    if (!frame.buffer)
        return false;

    if (previousBuffer != frame.buffer->GetBuffer())
        UpdateInstanceBufferDescriptor(frameIndex);

    void *mapped = frame.mapped;
    if (!mapped) {
        mapped = frame.buffer->Map();
        frame.mapped = mapped;
    }
    if (!mapped)
        return false;

    auto *matrices = static_cast<glm::mat4 *>(mapped);
    matrices[instanceIndex] = matrix;
    return true;
}

void InxVkCoreModular::UpdateInstanceBufferDescriptor(uint32_t frameIndex)
{
    (void)PublishGlobalsDescriptorRevision(frameIndex);
}

bool InxVkCoreModular::PublishGlobalsDescriptorRevision(uint32_t frameIndex)
{
    VkDevice device = GetDevice();
    if (device == VK_NULL_HANDLE || m_globalsDescSetLayout == VK_NULL_HANDLE ||
        frameIndex >= m_globalsDescSets.size() || frameIndex >= m_globalsDescriptorLeases.size() ||
        frameIndex >= m_globalsBuffers.size() || frameIndex >= m_instanceBuffers.size() ||
        frameIndex >= m_skinInstanceBuffers.size() || frameIndex >= m_skinPaletteBuffers.size() ||
        frameIndex >= m_instanceAuxBuffers.size())
        return false;

    const auto &globals = m_globalsBuffers[frameIndex];
    const auto &instances = m_instanceBuffers[frameIndex];
    const auto &skinInstances = m_skinInstanceBuffers[frameIndex];
    const auto &skinPalette = m_skinPaletteBuffers[frameIndex];
    const auto &instanceAux = m_instanceAuxBuffers[frameIndex];
    if (!globals || !instances.buffer || !skinInstances.buffer || !skinPalette.buffer || !instanceAux.buffer)
        return false;

    auto &descriptorManager = m_backend.Device().GetRhiDevice().GetDescriptorManager();
    const auto replacement = descriptorManager.Allocate(m_globalsDescSetLayout, vk::DescriptorArena::Persistent);
    if (!replacement.IsValid()) {
        INXLOG_ERROR("Failed to allocate globals descriptor revision for frame ", frameIndex);
        return false;
    }

    VkDescriptorBufferInfo bufferInfos[5]{};
    bufferInfos[0] = {globals->GetBuffer(), 0, sizeof(EngineGlobalsUBO)};
    bufferInfos[1] = {instances.buffer->GetBuffer(), 0, VK_WHOLE_SIZE};
    bufferInfos[2] = {skinInstances.buffer->GetBuffer(), 0, VK_WHOLE_SIZE};
    bufferInfos[3] = {skinPalette.buffer->GetBuffer(), 0, VK_WHOLE_SIZE};
    bufferInfos[4] = {instanceAux.buffer->GetBuffer(), 0, VK_WHOLE_SIZE};

    constexpr VkDescriptorType descriptorTypes[5] = {
        VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
        VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER};
    VkWriteDescriptorSet writes[5]{};
    for (uint32_t binding = 0; binding < 5; ++binding) {
        writes[binding].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        writes[binding].dstSet = replacement.set;
        writes[binding].dstBinding = binding;
        writes[binding].descriptorType = descriptorTypes[binding];
        writes[binding].descriptorCount = 1;
        writes[binding].pBufferInfo = &bufferInfos[binding];
    }
    vkUpdateDescriptorSets(device, 5, writes, 0, nullptr);

    const auto retired = m_globalsDescriptorLeases[frameIndex];
    m_globalsDescriptorLeases[frameIndex] = replacement;
    m_globalsDescSets[frameIndex] = replacement.set;
    descriptorManager.Retire(retired);
    return true;
}

void InxVkCoreModular::UpdateSkinBufferDescriptors(uint32_t frameIndex)
{
    (void)PublishGlobalsDescriptorRevision(frameIndex);
}

void InxVkCoreModular::UpdateInstanceAuxBufferDescriptor(uint32_t frameIndex)
{
    (void)PublishGlobalsDescriptorRevision(frameIndex);
}

void InxVkCoreModular::PrepareInstanceAuxiliary(uint64_t frameSerial, size_t totalInstances)
{
    if (totalInstances == 0)
        return;

    const uint32_t frameIndex = m_currentFrame % m_maxFramesInFlight;
    m_instanceHistory.BeginFrame(frameSerial);

    const VkBuffer previousBuffer = frameIndex < m_instanceAuxBuffers.size() && m_instanceAuxBuffers[frameIndex].buffer
                                        ? m_instanceAuxBuffers[frameIndex].buffer->GetBuffer()
                                        : VK_NULL_HANDLE;
    EnsureInstanceAuxBufferCapacity(frameIndex, totalInstances);
    if (frameIndex < m_instanceAuxBuffers.size() && m_instanceAuxBuffers[frameIndex].buffer &&
        previousBuffer != m_instanceAuxBuffers[frameIndex].buffer->GetBuffer())
        UpdateInstanceAuxBufferDescriptor(frameIndex);
}

bool InxVkCoreModular::WriteInstanceAuxiliary(uint32_t frameIndex, uint32_t instanceIndex,
                                              const RenderDrawIdentity &identity, const glm::mat4 &currentModel,
                                              uint64_t objectId, uint32_t layerMask)
{
    if (frameIndex >= m_instanceAuxBuffers.size())
        return false;

    auto &frame = m_instanceAuxBuffers[frameIndex];
    if (!frame.buffer || frame.capacity <= instanceIndex)
        return false;
    if (!frame.mapped)
        frame.mapped = frame.buffer->Map();
    if (!frame.mapped)
        return false;

    static_cast<GPUInstanceAuxData *>(frame.mapped)[instanceIndex] =
        m_instanceHistory.Resolve(identity, currentModel, objectId, layerMask);
    return true;
}

// ============================================================================
// Command-buffer inline update
// ============================================================================

void InxVkCoreModular::CmdUpdateGlobals(VkCommandBuffer cmdBuf)
{
    if (!m_globalsDirty)
        return;

    // Update ALL per-frame globals buffers so every frame-in-flight sees the
    // latest values.  Each m_globalsDescSets[i] points at m_globalsBuffers[i],
    // so we must write to every buffer — not just buffer 0.
    for (size_t i = 0; i < m_globalsBuffers.size(); ++i) {
        if (!m_globalsBuffers[i])
            continue;

        VkBuffer buffer = m_globalsBuffers[i]->GetBuffer();

        // Barrier: ensure previous shader reads from the globals UBO are complete
        vkrender::CmdBarrierUniformReadToTransferWrite(cmdBuf);

        // Update the globals UBO inline in the command buffer
        // vkCmdUpdateBuffer has a 65536-byte limit; EngineGlobalsUBO is 128 bytes.
        vkCmdUpdateBuffer(cmdBuf, buffer, 0, sizeof(EngineGlobalsUBO), &m_stagedGlobals);

        // Barrier: ensure write is visible before subsequent shader reads
        vkrender::CmdBarrierTransferWriteToUniformRead(cmdBuf);
    }

    m_globalsDirty = false;
}

} // namespace infernux
