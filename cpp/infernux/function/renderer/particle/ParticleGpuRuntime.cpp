#include "ParticleGpuRuntime.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <unordered_set>

#include <glm/glm.hpp>
#include <glm/gtc/type_ptr.hpp>

namespace infernux::particle
{

namespace
{

constexpr size_t StageIndex(GpuKernelStage stage) noexcept
{
    return static_cast<size_t>(stage);
}

bool CheckedBufferSize(uint32_t capacity, uint32_t stride, uint64_t &result) noexcept
{
    result = static_cast<uint64_t>(capacity) * stride;
    return capacity > 0 && stride > 0 && result <= std::numeric_limits<uint64_t>::max();
}

uint32_t FloatBits(float value) noexcept
{
    uint32_t result = 0;
    std::memcpy(&result, &value, sizeof(result));
    return result;
}

glm::mat4 RowMajorMatrix(const std::array<float, 16> &values) noexcept
{
    glm::mat4 result(1.0f);
    for (uint32_t row = 0; row < 4; ++row) {
        for (uint32_t column = 0; column < 4; ++column)
            result[column][row] = values[row * 4 + column];
    }
    return result;
}

bool IsFinite(const glm::mat4 &value) noexcept
{
    for (uint32_t column = 0; column < 4; ++column) {
        for (uint32_t row = 0; row < 4; ++row) {
            if (!std::isfinite(value[column][row]))
                return false;
        }
    }
    return true;
}

bool IsFinite(const glm::mat3 &value) noexcept
{
    for (uint32_t column = 0; column < 3; ++column) {
        for (uint32_t row = 0; row < 3; ++row) {
            if (!std::isfinite(value[column][row]))
                return false;
        }
    }
    return true;
}

void StoreMatrix(std::vector<uint32_t> &words, size_t base, const glm::mat4 &matrix)
{
    for (uint32_t column = 0; column < 4; ++column) {
        for (uint32_t row = 0; row < 4; ++row)
            words[base + column * 4 + row] = FloatBits(matrix[column][row]);
    }
}

void StoreNormalMatrix(std::vector<uint32_t> &words, size_t base, const glm::mat3 &matrix)
{
    for (uint32_t column = 0; column < 3; ++column) {
        for (uint32_t row = 0; row < 3; ++row)
            words[base + column * 4 + row] = FloatBits(matrix[column][row]);
        words[base + column * 4 + 3] = 0;
    }
}

} // namespace

struct ParticleGpuRuntime::ResidentState
{
    ~ResidentState()
    {
        if (!device)
            return;
        device->Release(transforms);
        device->Release(simulationControl);
        device->Release(renderIndices);
        device->Release(indirect);
        device->Release(instances);
        device->Release(counters);
        device->Release(freeList);
        device->Release(states);
    }

    rhi::Device *device = nullptr;
    rhi::BufferHandle states;
    rhi::BufferHandle freeList;
    rhi::BufferHandle counters;
    rhi::BufferHandle instances;
    rhi::BufferHandle indirect;
    rhi::BufferHandle renderIndices;
    rhi::BufferHandle transforms;
    rhi::BufferHandle simulationControl;
    bool bootstrapRecorded = false;
};

struct ParticleGpuRuntime::DataInterfaceState
{
    ~DataInterfaceState()
    {
        if (!device)
            return;
        device->Release(group);
        device->Release(layout);
        device->Release(metadataBuffer);
        for (const auto buffer : ownedBuffers)
            device->Release(buffer);
    }

    rhi::Device *device = nullptr;
    rhi::BindingLayoutHandle layout;
    rhi::BindGroupHandle group;
    rhi::BufferHandle metadataBuffer;
    std::vector<rhi::BufferHandle> ownedBuffers;
    std::vector<GpuMeshInterfaceDesc> meshInterfaces;
    std::vector<uint32_t> metadataWords;
};

struct ParticleGpuRuntime::VectorFieldState
{
    ~VectorFieldState()
    {
        if (!device)
            return;
        device->Release(group);
        device->Release(layout);
        device->Release(metadataBuffer);
    }

    rhi::Device *device = nullptr;
    rhi::BindingLayoutHandle layout;
    rhi::BindGroupHandle group;
    rhi::BufferHandle metadataBuffer;
    std::vector<GpuVectorFieldDesc> vectorFields;
    std::vector<GpuTexture2DParameterDesc> textureParameters;
    std::vector<uint32_t> metadataWords;
    uint32_t interfaceStrideWords = 0;
};

ParticleGpuRuntime::ParticleGpuRuntime() = default;

ParticleGpuRuntime::~ParticleGpuRuntime()
{
    Destroy();
}

bool ParticleGpuRuntime::Create(rhi::Device &device, const GpuEmitterDesc &desc)
{
    return CreateInternal(device, desc, {}, nullptr, nullptr);
}

bool ParticleGpuRuntime::CreateCompatible(rhi::Device &device, const GpuEmitterDesc &desc,
                                          const ParticleGpuRuntime &previous)
{
    if (!previous.IsValid() || previous.m_device != &device || previous.Capacity() != desc.capacity ||
        previous.StateStride() != desc.stateStride || previous.EventTypeCount() != desc.eventTypeCount ||
        previous.CollisionEnabled() != desc.collisionEnabled)
        return false;
    if ((desc.continuation.capacity == 0) != !previous.HasContinuations())
        return false;
    return CreateInternal(device, desc, previous.m_residentState, previous.m_continuation.get(),
                          previous.m_contacts.get());
}

bool ParticleGpuRuntime::AdoptCompatibleRevision(ParticleGpuRuntime &replacement) noexcept
{
    if (!IsValid() || !replacement.IsValid() || m_device != replacement.m_device ||
        m_capacity != replacement.m_capacity || m_stateStride != replacement.m_stateStride ||
        m_eventTypeCount != replacement.m_eventTypeCount || m_collisionEnabled != replacement.m_collisionEnabled ||
        m_residentState != replacement.m_residentState)
        return false;
    std::swap(m_layout, replacement.m_layout);
    std::swap(m_group, replacement.m_group);
    std::swap(m_dataInterfaces, replacement.m_dataInterfaces);
    std::swap(m_vectorFields, replacement.m_vectorFields);
    std::swap(m_emptyDataInterfaceLayout, replacement.m_emptyDataInterfaceLayout);
    std::swap(m_emptyDataInterfaceGroup, replacement.m_emptyDataInterfaceGroup);
    std::swap(m_graphSpawnLayout, replacement.m_graphSpawnLayout);
    std::swap(m_collisionSceneLayout, replacement.m_collisionSceneLayout);
    std::swap(m_collisionSceneGroup, replacement.m_collisionSceneGroup);
    std::swap(m_pipelines, replacement.m_pipelines);
    std::swap(m_continuation, replacement.m_continuation);
    std::swap(m_contacts, replacement.m_contacts);
    return true;
}

bool ParticleGpuRuntime::CreateInternal(rhi::Device &device, const GpuEmitterDesc &desc,
                                        std::shared_ptr<ResidentState> residentState,
                                        const ParticleGpuContinuationRuntime *previousContinuation,
                                        const ParticleGpuContactRuntime *previousContacts)
{
    Destroy();
    uint64_t stateBytes = 0;
    uint64_t instanceBytes = 0;
    if (!CheckedBufferSize(desc.capacity, desc.stateStride, stateBytes) ||
        !CheckedBufferSize(desc.capacity, RenderInstanceStride, instanceBytes))
        return false;
    for (const auto &kernel : desc.kernels) {
        if (!kernel.words || kernel.wordCount == 0)
            return false;
    }
    m_device = &device;
    m_capacity = desc.capacity;
    m_stateStride = desc.stateStride;
    m_eventTypeCount = desc.eventTypeCount;
    m_collisionEnabled = desc.collisionEnabled;
    if (residentState) {
        if (residentState->device != &device) {
            Destroy();
            return false;
        }
        m_residentState = std::move(residentState);
    } else {
        m_residentState = std::make_shared<ResidentState>();
        m_residentState->device = &device;
        const auto storage = rhi::BufferUsageFlags::Storage;
        // State snapshots are copied asynchronously into a readback buffer by
        // ParticleGpuSystemManager diagnostics. Keep the transfer capability
        // on the resident allocation itself; the copy source cannot acquire
        // usage flags retroactively when a diagnostic request arrives.
        m_residentState->states = device.CreateBuffer({stateBytes, storage | rhi::BufferUsageFlags::TransferSource});
        m_residentState->freeList =
            device.CreateBuffer({static_cast<uint64_t>(desc.capacity) * sizeof(uint32_t), storage});
        m_residentState->counters =
            device.CreateBuffer({CounterBufferByteSize(), storage | rhi::BufferUsageFlags::TransferSource});
        const auto createRenderExport = [&](uint64_t bytes, rhi::BufferUsageFlags usage) {
            rhi::BufferDesc bufferDesc;
            bufferDesc.byteSize = bytes;
            bufferDesc.usage = usage;
            bufferDesc.queueAccess = rhi::QueueAccessFlags::Graphics | rhi::QueueAccessFlags::Compute;
            return device.CreateBuffer(bufferDesc);
        };
        m_residentState->instances = createRenderExport(instanceBytes, storage);
        m_residentState->indirect = createRenderExport(16, storage | rhi::BufferUsageFlags::Indirect);
        m_residentState->renderIndices =
            createRenderExport(static_cast<uint64_t>(desc.capacity) * sizeof(uint32_t), storage);
        rhi::BufferDesc transformDesc;
        transformDesc.byteSize = sizeof(GpuParticleTransforms);
        transformDesc.usage = rhi::BufferUsageFlags::Uniform;
        transformDesc.memory = rhi::BufferMemory::Upload;
        m_residentState->transforms = device.CreateBuffer(transformDesc);
        rhi::BufferDesc simulationControlDesc;
        simulationControlDesc.byteSize = sizeof(GpuParticleSimulationControl);
        simulationControlDesc.usage = rhi::BufferUsageFlags::Storage | rhi::BufferUsageFlags::TransferSource;
        simulationControlDesc.queueAccess = rhi::QueueAccessFlags::Graphics | rhi::QueueAccessFlags::Compute;
        // The first Bounds Prepare dispatch initializes all four words. Keeping
        // this buffer device-local avoids a permanently host-visible control
        // allocation and respects the RHI rule that DeviceLocal buffers cannot
        // be created with direct initialData.
        m_residentState->simulationControl = device.CreateBuffer(simulationControlDesc);
        if (!StateBuffer().IsValid() || !FreeListBuffer().IsValid() || !CounterBuffer().IsValid() ||
            !InstanceBuffer().IsValid() || !IndirectBuffer().IsValid() || !RenderIndexBuffer().IsValid() ||
            !TransformBuffer().IsValid() || !SimulationControlBuffer().IsValid()) {
            Destroy();
            return false;
        }
    }

    rhi::BindingLayoutDesc layoutDesc;
    if (desc.collisionEnabled &&
        (!desc.collisionSceneHeader.IsValid() || !desc.collisionSceneColliders.IsValid() ||
         !desc.collisionSceneGridOffsets.IsValid() || !desc.collisionSceneGridColliderIndices.IsValid() ||
         !desc.collisionSceneMeshVertices.IsValid() || !desc.collisionSceneMeshIndices.IsValid() ||
         !desc.collisionSceneMeshBvhNodes.IsValid())) {
        Destroy();
        return false;
    }
    for (uint32_t binding = 0; binding < 7; ++binding) {
        layoutDesc.entries[layoutDesc.entryCount++] = {
            binding, binding == 5 ? rhi::BindingType::UniformBuffer : rhi::BindingType::StorageBuffer,
            rhi::ShaderStage::Compute, 1};
    }
    layoutDesc.entries[layoutDesc.entryCount++] = {15, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    m_layout = device.CreateBindingLayout(layoutDesc);
    if (!m_layout.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_layout;
    const std::array<rhi::BufferHandle, 7> coreBuffers = {StateBuffer(),      FreeListBuffer(), CounterBuffer(),
                                                          InstanceBuffer(),   IndirectBuffer(), TransformBuffer(),
                                                          RenderIndexBuffer()};
    for (uint32_t binding = 0; binding < coreBuffers.size(); ++binding) {
        auto &entry = groupDesc.buffers[groupDesc.bufferCount++];
        entry = {binding, binding == 5 ? rhi::BindingType::UniformBuffer : rhi::BindingType::StorageBuffer,
                 coreBuffers[binding]};
    }
    groupDesc.buffers[groupDesc.bufferCount++] = {15, rhi::BindingType::StorageBuffer, SimulationControlBuffer()};
    m_group = device.CreateBindGroup(groupDesc);
    if (!m_group.IsValid()) {
        Destroy();
        return false;
    }

    m_emptyDataInterfaceLayout = device.CreateBindingLayout({});
    rhi::BindGroupDesc emptyGroupDesc;
    emptyGroupDesc.layout = m_emptyDataInterfaceLayout;
    m_emptyDataInterfaceGroup = device.CreateBindGroup(emptyGroupDesc);
    if (!m_emptyDataInterfaceLayout.IsValid() || !m_emptyDataInterfaceGroup.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc graphSpawnLayoutDesc;
    graphSpawnLayoutDesc.entries[0] = {0, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    graphSpawnLayoutDesc.entries[1] = {1, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    graphSpawnLayoutDesc.entries[2] = {2, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    graphSpawnLayoutDesc.entries[3] = {3, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    graphSpawnLayoutDesc.entries[4] = {4, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    graphSpawnLayoutDesc.entryCount = 5;
    m_graphSpawnLayout = device.CreateBindingLayout(graphSpawnLayoutDesc);
    if (!m_graphSpawnLayout.IsValid()) {
        Destroy();
        return false;
    }

    if (desc.collisionEnabled) {
        rhi::BindingLayoutDesc collisionLayoutDesc;
        for (uint32_t binding = 0; binding < 7; ++binding)
            collisionLayoutDesc.entries[collisionLayoutDesc.entryCount++] = {binding, rhi::BindingType::StorageBuffer,
                                                                             rhi::ShaderStage::Compute, 1};
        m_collisionSceneLayout = device.CreateBindingLayout(collisionLayoutDesc);
        if (!m_collisionSceneLayout.IsValid()) {
            Destroy();
            return false;
        }

        const std::array<rhi::BufferHandle, 7> collisionBuffers = {
            desc.collisionSceneHeader,       desc.collisionSceneColliders,
            desc.collisionSceneGridOffsets,  desc.collisionSceneGridColliderIndices,
            desc.collisionSceneMeshVertices, desc.collisionSceneMeshIndices,
            desc.collisionSceneMeshBvhNodes};
        rhi::BindGroupDesc collisionGroupDesc;
        collisionGroupDesc.layout = m_collisionSceneLayout;
        for (uint32_t binding = 0; binding < collisionBuffers.size(); ++binding)
            collisionGroupDesc.buffers[collisionGroupDesc.bufferCount++] = {binding, rhi::BindingType::StorageBuffer,
                                                                            collisionBuffers[binding]};
        m_collisionSceneGroup = device.CreateBindGroup(collisionGroupDesc);
        if (!m_collisionSceneGroup.IsValid()) {
            Destroy();
            return false;
        }
    }

    if (!desc.meshInterfaces.empty()) {
        constexpr uint32_t metadataBinding = 0;
        auto dataInterfaces = std::make_unique<DataInterfaceState>();
        dataInterfaces->device = &device;
        dataInterfaces->metadataWords.assign(desc.meshInterfaces.size() * 32u, 0);

        std::array<bool, rhi::BindingLayoutDesc::MaxEntries> usedBindings{};
        usedBindings[metadataBinding] = true;
        rhi::BindingLayoutDesc dataLayoutDesc;
        dataLayoutDesc.entries[0] = {metadataBinding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
        dataLayoutDesc.entryCount = 1;

        rhi::BindGroupDesc dataGroupDesc;
        dataGroupDesc.bufferCount = 1;
        dataGroupDesc.buffers[0].binding = metadataBinding;
        dataGroupDesc.buffers[0].type = rhi::BindingType::StorageBuffer;

        std::unordered_set<std::string> stableIds;
        dataInterfaces->meshInterfaces.reserve(desc.meshInterfaces.size());
        for (size_t index = 0; index < desc.meshInterfaces.size(); ++index) {
            const auto &mesh = desc.meshInterfaces[index];
            const uint32_t expectedMetadataOffset = static_cast<uint32_t>(index) * 32u;
            const std::array<uint32_t, 4> bindings = {mesh.vertexBinding, mesh.triangleBinding, mesh.influenceBinding,
                                                      mesh.paletteBinding};
            const bool duplicateBinding =
                std::unordered_set<uint32_t>(bindings.begin(), bindings.end()).size() != bindings.size();
            if (mesh.stableId.empty() || !stableIds.insert(mesh.stableId).second || mesh.interfaceIndex != index ||
                mesh.metadataOffsetWords != expectedMetadataOffset ||
                std::any_of(
                    bindings.begin(), bindings.end(),
                    [&](uint32_t binding) { return binding >= usedBindings.size() || usedBindings[binding]; }) ||
                duplicateBinding || mesh.vertexCount == 0 || mesh.triangleCount == 0 || mesh.edgeCount == 0 ||
                !mesh.vertices.IsValid() || !mesh.triangles.IsValid() || !mesh.keepAlive ||
                (mesh.boneCount == 0 && (!mesh.initialPalette.empty() || mesh.influences.IsValid())) ||
                (mesh.boneCount != 0 && (!mesh.influences.IsValid() || mesh.initialPalette.size() != mesh.boneCount))) {
                Destroy();
                return false;
            }
            for (const uint32_t binding : bindings)
                usedBindings[binding] = true;
            dataInterfaces->metadataWords[expectedMetadataOffset] = mesh.vertexCount;
            dataInterfaces->metadataWords[expectedMetadataOffset + 1u] = mesh.triangleCount;
            dataInterfaces->metadataWords[expectedMetadataOffset + 2u] = mesh.edgeCount;
            dataInterfaces->metadataWords[expectedMetadataOffset + 3u] = mesh.boneCount;
            auto runtimeMesh = mesh;

            std::array<uint32_t, 8> dummyInfluence{};
            dummyInfluence[4] = FloatBits(1.0f);
            const glm::mat4 identity(1.0f);
            const void *paletteData = mesh.boneCount == 0 ? static_cast<const void *>(&identity)
                                                          : static_cast<const void *>(mesh.initialPalette.data());
            const uint64_t paletteBytes =
                mesh.boneCount == 0 ? sizeof(identity) : mesh.initialPalette.size() * sizeof(glm::mat4);
            if (mesh.boneCount == 0) {
                rhi::BufferDesc influenceDesc;
                influenceDesc.byteSize = sizeof(dummyInfluence);
                influenceDesc.usage = rhi::BufferUsageFlags::Storage;
                influenceDesc.memory = rhi::BufferMemory::Upload;
                influenceDesc.initialData = dummyInfluence.data();
                influenceDesc.initialDataBytes = influenceDesc.byteSize;
                runtimeMesh.influences = device.CreateBuffer(influenceDesc);
                if (!runtimeMesh.influences.IsValid()) {
                    Destroy();
                    return false;
                }
                dataInterfaces->ownedBuffers.push_back(runtimeMesh.influences);
            }
            rhi::BufferDesc paletteDesc;
            paletteDesc.byteSize = paletteBytes;
            paletteDesc.usage = rhi::BufferUsageFlags::Storage;
            paletteDesc.memory = rhi::BufferMemory::Upload;
            paletteDesc.initialData = paletteData;
            paletteDesc.initialDataBytes = paletteBytes;
            runtimeMesh.palette = device.CreateBuffer(paletteDesc);
            if (!runtimeMesh.palette.IsValid()) {
                Destroy();
                return false;
            }
            dataInterfaces->ownedBuffers.push_back(runtimeMesh.palette);
            dataInterfaces->meshInterfaces.push_back(runtimeMesh);
            dataLayoutDesc.entries[dataLayoutDesc.entryCount++] = {mesh.vertexBinding, rhi::BindingType::StorageBuffer,
                                                                   rhi::ShaderStage::Compute, 1};
            dataLayoutDesc.entries[dataLayoutDesc.entryCount++] = {
                mesh.triangleBinding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
            dataLayoutDesc.entries[dataLayoutDesc.entryCount++] = {
                mesh.influenceBinding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
            dataLayoutDesc.entries[dataLayoutDesc.entryCount++] = {mesh.paletteBinding, rhi::BindingType::StorageBuffer,
                                                                   rhi::ShaderStage::Compute, 1};
            dataGroupDesc.buffers[dataGroupDesc.bufferCount++] = {mesh.vertexBinding, rhi::BindingType::StorageBuffer,
                                                                  mesh.vertices};
            dataGroupDesc.buffers[dataGroupDesc.bufferCount++] = {mesh.triangleBinding, rhi::BindingType::StorageBuffer,
                                                                  mesh.triangles};
            dataGroupDesc.buffers[dataGroupDesc.bufferCount++] = {
                mesh.influenceBinding, rhi::BindingType::StorageBuffer, runtimeMesh.influences};
            dataGroupDesc.buffers[dataGroupDesc.bufferCount++] = {mesh.paletteBinding, rhi::BindingType::StorageBuffer,
                                                                  runtimeMesh.palette};
        }

        rhi::BufferDesc metadataBufferDesc;
        metadataBufferDesc.byteSize = dataInterfaces->metadataWords.size() * sizeof(uint32_t);
        metadataBufferDesc.usage = rhi::BufferUsageFlags::Storage;
        metadataBufferDesc.memory = rhi::BufferMemory::Upload;
        metadataBufferDesc.initialData = dataInterfaces->metadataWords.data();
        metadataBufferDesc.initialDataBytes = metadataBufferDesc.byteSize;
        dataInterfaces->metadataBuffer = device.CreateBuffer(metadataBufferDesc);
        if (!dataInterfaces->metadataBuffer.IsValid()) {
            Destroy();
            return false;
        }
        dataGroupDesc.buffers[0].buffer = dataInterfaces->metadataBuffer;
        dataInterfaces->layout = device.CreateBindingLayout(dataLayoutDesc);
        if (!dataInterfaces->layout.IsValid()) {
            Destroy();
            return false;
        }
        dataGroupDesc.layout = dataInterfaces->layout;
        dataInterfaces->group = device.CreateBindGroup(dataGroupDesc);
        if (!dataInterfaces->group.IsValid()) {
            Destroy();
            return false;
        }
        m_dataInterfaces = std::move(dataInterfaces);
    }

    const auto &vectorFieldLayout = desc.vectorFields;
    if (!vectorFieldLayout.vectorFields.empty() || !vectorFieldLayout.textureParameters.empty()) {
        if (vectorFieldLayout.metadataBinding != 0 || vectorFieldLayout.interfaceStrideWords < 32 ||
            vectorFieldLayout.vectorFields.size() + vectorFieldLayout.textureParameters.size() > 15) {
            Destroy();
            return false;
        }
        const uint64_t metadataWordCount = std::max<uint64_t>(
            4, static_cast<uint64_t>(vectorFieldLayout.vectorFields.size()) * vectorFieldLayout.interfaceStrideWords);
        if (metadataWordCount > std::numeric_limits<uint32_t>::max()) {
            Destroy();
            return false;
        }

        auto vectorFields = std::make_unique<VectorFieldState>();
        vectorFields->device = &device;
        vectorFields->vectorFields = vectorFieldLayout.vectorFields;
        vectorFields->textureParameters = vectorFieldLayout.textureParameters;
        vectorFields->interfaceStrideWords = vectorFieldLayout.interfaceStrideWords;
        vectorFields->metadataWords.assign(static_cast<size_t>(metadataWordCount), 0);

        std::array<bool, rhi::BindingLayoutDesc::MaxEntries> usedBindings{};
        usedBindings[vectorFieldLayout.metadataBinding] = true;
        rhi::BindingLayoutDesc layoutDesc;
        layoutDesc.entries[0] = {vectorFieldLayout.metadataBinding, rhi::BindingType::StorageBuffer,
                                 rhi::ShaderStage::Compute, 1};
        layoutDesc.entryCount = 1;
        rhi::BindGroupDesc groupDesc;
        groupDesc.bufferCount = 1;
        groupDesc.buffers[0].binding = vectorFieldLayout.metadataBinding;
        groupDesc.buffers[0].type = rhi::BindingType::StorageBuffer;

        for (size_t index = 0; index < vectorFieldLayout.vectorFields.size(); ++index) {
            const auto &field = vectorFieldLayout.vectorFields[index];
            if (field.interfaceIndex != index || field.textureBinding >= usedBindings.size() ||
                usedBindings[field.textureBinding] || !field.texture.IsValid() || !field.sampler.IsValid() ||
                !field.keepAlive || !std::isfinite(field.vectorScale) ||
                !IsFinite(RowMajorMatrix(field.fieldToSpace))) {
                Destroy();
                return false;
            }
            usedBindings[field.textureBinding] = true;
            const size_t base = index * vectorFieldLayout.interfaceStrideWords;
            StoreMatrix(vectorFields->metadataWords, base, glm::mat4(1.0f));
            StoreNormalMatrix(vectorFields->metadataWords, base + 16, glm::mat3(1.0f));
            vectorFields->metadataWords[base + 28] = FloatBits(field.vectorScale);
            layoutDesc.entries[layoutDesc.entryCount++] = {
                field.textureBinding, rhi::BindingType::CombinedTextureSampler, rhi::ShaderStage::Compute, 1};
            groupDesc.textures[groupDesc.textureCount++] = {
                field.textureBinding, rhi::BindingType::CombinedTextureSampler, field.texture, field.sampler};
        }
        for (size_t index = 0; index < vectorFieldLayout.textureParameters.size(); ++index) {
            const auto &parameter = vectorFieldLayout.textureParameters[index];
            if (parameter.resourceIndex != index || parameter.textureBinding >= usedBindings.size() ||
                usedBindings[parameter.textureBinding] || !parameter.texture.IsValid() ||
                !parameter.sampler.IsValid() || !parameter.keepAlive) {
                Destroy();
                return false;
            }
            usedBindings[parameter.textureBinding] = true;
            layoutDesc.entries[layoutDesc.entryCount++] = {
                parameter.textureBinding, rhi::BindingType::CombinedTextureSampler, rhi::ShaderStage::Compute, 1};
            groupDesc.textures[groupDesc.textureCount++] = {parameter.textureBinding,
                                                            rhi::BindingType::CombinedTextureSampler, parameter.texture,
                                                            parameter.sampler};
        }

        rhi::BufferDesc metadataDesc;
        metadataDesc.byteSize = vectorFields->metadataWords.size() * sizeof(uint32_t);
        metadataDesc.usage = rhi::BufferUsageFlags::Storage;
        metadataDesc.memory = rhi::BufferMemory::Upload;
        metadataDesc.initialData = vectorFields->metadataWords.data();
        metadataDesc.initialDataBytes = metadataDesc.byteSize;
        vectorFields->metadataBuffer = device.CreateBuffer(metadataDesc);
        if (!vectorFields->metadataBuffer.IsValid()) {
            Destroy();
            return false;
        }
        groupDesc.buffers[0].buffer = vectorFields->metadataBuffer;
        vectorFields->layout = device.CreateBindingLayout(layoutDesc);
        if (!vectorFields->layout.IsValid()) {
            Destroy();
            return false;
        }
        groupDesc.layout = vectorFields->layout;
        vectorFields->group = device.CreateBindGroup(groupDesc);
        if (!vectorFields->group.IsValid()) {
            Destroy();
            return false;
        }
        m_vectorFields = std::move(vectorFields);
    }

    if (desc.collisionEnabled) {
        m_contacts = std::make_unique<ParticleGpuContactRuntime>();
        const bool contactsCreated =
            previousContacts
                ? m_contacts->CreateCompatible(device, desc.capacity, desc.continuation.capacity, *previousContacts)
                : m_contacts->Create(device, desc.capacity, desc.continuation.capacity);
        if (!contactsCreated) {
            Destroy();
            return false;
        }
    } else if (previousContacts) {
        Destroy();
        return false;
    }

    if (desc.continuation.capacity > 0) {
        auto continuationDesc = desc.continuation;
        continuationDesc.particleCapacity = desc.capacity;
        continuationDesc.ownerLayout = m_layout;
        continuationDesc.ownerGroup = m_group;
        continuationDesc.dataInterfaceLayout = m_dataInterfaces ? m_dataInterfaces->layout : m_emptyDataInterfaceLayout;
        continuationDesc.dataInterfaceGroup = m_dataInterfaces ? m_dataInterfaces->group : m_emptyDataInterfaceGroup;
        continuationDesc.vectorFieldLayout = m_vectorFields ? m_vectorFields->layout : m_emptyDataInterfaceLayout;
        continuationDesc.vectorFieldGroup = m_vectorFields ? m_vectorFields->group : m_emptyDataInterfaceGroup;
        continuationDesc.graphSpawnLayout = m_graphSpawnLayout;
        continuationDesc.emptyLayout = m_emptyDataInterfaceLayout;
        continuationDesc.emptyGroup = m_emptyDataInterfaceGroup;
        continuationDesc.collisionSceneLayout =
            m_collisionEnabled ? m_collisionSceneLayout : m_emptyDataInterfaceLayout;
        continuationDesc.collisionSceneGroup = m_collisionEnabled ? m_collisionSceneGroup : m_emptyDataInterfaceGroup;
        continuationDesc.contactLayout = m_contacts ? m_contacts->Layout() : m_emptyDataInterfaceLayout;
        continuationDesc.contactGroup = m_contacts ? m_contacts->Group() : m_emptyDataInterfaceGroup;
        m_continuation = std::make_unique<ParticleGpuContinuationRuntime>();
        const bool continuationCreated =
            previousContinuation ? m_continuation->CreateCompatible(device, continuationDesc, *previousContinuation)
                                 : m_continuation->Create(device, continuationDesc);
        if (!continuationCreated) {
            Destroy();
            return false;
        }
    } else if (previousContinuation) {
        Destroy();
        return false;
    }

    for (size_t index = 0; index < desc.kernels.size(); ++index) {
        const auto shader = device.CreateShaderModule({desc.kernels[index].words, desc.kernels[index].wordCount});
        if (!shader.IsValid()) {
            Destroy();
            return false;
        }
        rhi::ComputePipelineDesc pipelineDesc;
        pipelineDesc.computeShader = shader;
        pipelineDesc.bindingLayouts[0] = m_layout;
        pipelineDesc.bindingLayouts[1] = m_dataInterfaces ? m_dataInterfaces->layout : m_emptyDataInterfaceLayout;
        pipelineDesc.bindingLayouts[2] = m_vectorFields ? m_vectorFields->layout : m_emptyDataInterfaceLayout;
        pipelineDesc.bindingLayouts[3] = m_graphSpawnLayout;
        pipelineDesc.bindingLayouts[4] = m_emptyDataInterfaceLayout;
        pipelineDesc.bindingLayouts[5] = m_continuation ? m_continuation->Layout() : m_emptyDataInterfaceLayout;
        pipelineDesc.bindingLayouts[6] = m_collisionEnabled ? m_collisionSceneLayout : m_emptyDataInterfaceLayout;
        pipelineDesc.bindingLayouts[7] = m_contacts ? m_contacts->Layout() : m_emptyDataInterfaceLayout;
        pipelineDesc.bindingLayoutCount = 8;
        pipelineDesc.pushConstantBytes = sizeof(GpuParticlePushConstants);
        m_pipelines[index] = device.CreateComputePipeline(pipelineDesc);
        device.Release(shader);
        if (!m_pipelines[index].IsValid()) {
            Destroy();
            return false;
        }
    }

    return true;
}

void ParticleGpuRuntime::Destroy() noexcept
{
    m_continuation.reset();
    m_contacts.reset();
    if (m_device) {
        for (auto pipeline : m_pipelines)
            m_device->Release(pipeline);
        m_device->Release(m_group);
        m_device->Release(m_layout);
        m_device->Release(m_emptyDataInterfaceGroup);
        m_device->Release(m_emptyDataInterfaceLayout);
        m_device->Release(m_graphSpawnLayout);
        m_device->Release(m_collisionSceneGroup);
        m_device->Release(m_collisionSceneLayout);
    }
    m_dataInterfaces.reset();
    m_vectorFields.reset();
    m_residentState.reset();
    m_device = nullptr;
    m_capacity = 0;
    m_stateStride = 0;
    m_eventTypeCount = 0;
    m_collisionEnabled = false;
    m_contactPrepareRecordCalls = 0;
    m_contactSolveRecordCalls = 0;
    m_contactDispatchRecordCalls = 0;
    m_layout = {};
    m_group = {};
    m_emptyDataInterfaceLayout = {};
    m_emptyDataInterfaceGroup = {};
    m_graphSpawnLayout = {};
    m_collisionSceneLayout = {};
    m_collisionSceneGroup = {};
    m_pipelines.fill({});
}

bool ParticleGpuRuntime::IsValid() const noexcept
{
    if (!m_device || !m_residentState || m_capacity == 0 || !m_group.IsValid() ||
        !m_emptyDataInterfaceLayout.IsValid() || !m_emptyDataInterfaceGroup.IsValid() || !m_graphSpawnLayout.IsValid())
        return false;
    if (m_dataInterfaces && (!m_dataInterfaces->layout.IsValid() || !m_dataInterfaces->group.IsValid() ||
                             !m_dataInterfaces->metadataBuffer.IsValid()))
        return false;
    if (m_vectorFields && (!m_vectorFields->layout.IsValid() || !m_vectorFields->group.IsValid() ||
                           !m_vectorFields->metadataBuffer.IsValid()))
        return false;
    if (m_vectorFields && !m_dataInterfaces &&
        (!m_emptyDataInterfaceLayout.IsValid() || !m_emptyDataInterfaceGroup.IsValid()))
        return false;
    if (m_continuation && !m_continuation->IsValid())
        return false;
    if (m_contacts && !m_contacts->IsValid())
        return false;
    if (m_collisionEnabled && (!m_collisionSceneLayout.IsValid() || !m_collisionSceneGroup.IsValid()))
        return false;
    if (m_collisionEnabled != static_cast<bool>(m_contacts))
        return false;
    for (auto pipeline : m_pipelines) {
        if (!pipeline.IsValid())
            return false;
    }
    return true;
}

bool ParticleGpuRuntime::HasContinuations() const noexcept
{
    return m_continuation && m_continuation->IsValid();
}

const GpuParticleContinuationResources &ParticleGpuRuntime::ContinuationResources() const noexcept
{
    static const GpuParticleContinuationResources Empty;
    return m_continuation ? m_continuation->Resources() : Empty;
}

GpuParticleContinuationTelemetry ParticleGpuRuntime::ContinuationTelemetry() const noexcept
{
    return m_continuation ? m_continuation->Telemetry() : GpuParticleContinuationTelemetry{};
}

bool ParticleGpuRuntime::SharesContinuationStateWith(const ParticleGpuRuntime &other) const noexcept
{
    return m_continuation && other.m_continuation && m_continuation->SharesStorageWith(*other.m_continuation);
}

void ParticleGpuRuntime::RequestContinuationReset() noexcept
{
    if (m_continuation)
        m_continuation->RequestReset();
}

bool ParticleGpuRuntime::RecordContinuationPrepare(const rhi::ComputeCommandEncoder &encoder, uint32_t simulationStep,
                                                   uint64_t elapsedTimeTicks)
{
    return m_continuation && m_continuation->RecordPrepare(encoder, simulationStep, elapsedTimeTicks);
}

bool ParticleGpuRuntime::RecordContinuationClassify(const rhi::ComputeCommandEncoder &encoder, uint32_t simulationStep,
                                                    uint64_t elapsedTimeTicks) const
{
    return m_continuation && m_continuation->RecordClassify(encoder, simulationStep, elapsedTimeTicks);
}

bool ParticleGpuRuntime::RecordContinuationDispatch(const rhi::ComputeCommandEncoder &encoder, uint32_t simulationStep,
                                                    uint64_t elapsedTimeTicks, uint32_t systemSeed, float deltaTime,
                                                    rhi::BindGroupHandle graphSpawnGroup) const
{
    return m_continuation && m_continuation->RecordDispatch(encoder, simulationStep, elapsedTimeTicks, systemSeed,
                                                            deltaTime, graphSpawnGroup);
}

bool ParticleGpuRuntime::HasContactRuntime() const noexcept
{
    return m_contacts && m_contacts->IsValid();
}

const GpuParticleContactResources &ParticleGpuRuntime::ContactResources() const noexcept
{
    static const GpuParticleContactResources Empty;
    return m_contacts ? m_contacts->Resources() : Empty;
}

GpuParticleContactTelemetry ParticleGpuRuntime::ContactTelemetry() const noexcept
{
    return m_contacts ? m_contacts->Telemetry() : GpuParticleContactTelemetry{};
}

bool ParticleGpuRuntime::SharesContactStateWith(const ParticleGpuRuntime &other) const noexcept
{
    return m_contacts && other.m_contacts && m_contacts->SharesStorageWith(*other.m_contacts);
}

bool ParticleGpuRuntime::SharesStateWith(const ParticleGpuRuntime &other) const noexcept
{
    return m_residentState && m_residentState == other.m_residentState;
}

bool ParticleGpuRuntime::NeedsBootstrap() const noexcept
{
    return !m_residentState || !m_residentState->bootstrapRecorded;
}

void ParticleGpuRuntime::RequestBootstrap() noexcept
{
    if (m_residentState)
        m_residentState->bootstrapRecorded = false;
}

void ParticleGpuRuntime::MarkStateInitialized() noexcept
{
    if (m_residentState)
        m_residentState->bootstrapRecorded = true;
}

bool ParticleGpuRuntime::UpdateSkinnedMeshSources(const std::vector<GpuSkinnedMeshFrameData> &sources)
{
    if (!m_device)
        return false;
    if (!m_dataInterfaces)
        return sources.empty();
    for (const auto &source : sources) {
        if (!source.currentPalette || source.currentPalette->empty() || !IsFinite(RowMajorMatrix(source.sourceToWorld)))
            return false;
        const auto found = std::find_if(
            m_dataInterfaces->meshInterfaces.begin(), m_dataInterfaces->meshInterfaces.end(),
            [&](const GpuMeshInterfaceDesc &mesh) { return mesh.interfaceIndex == source.interfaceIndex; });
        if (found == m_dataInterfaces->meshInterfaces.end() || found->boneCount == 0 ||
            source.currentPalette->size() != found->boneCount || !found->palette.IsValid())
            return false;
        found->worldSpace = true;
        found->meshToSpace = source.sourceToWorld;
        if (found->poseRevision == source.poseRevision)
            continue;
        const uint64_t byteSize = source.currentPalette->size() * sizeof(glm::mat4);
        if (!m_device->WriteBuffer(found->palette, 0, source.currentPalette->data(), byteSize))
            return false;
        found->poseRevision = source.poseRevision;
    }
    return true;
}

bool ParticleGpuRuntime::UpdateTransforms(const GpuParticleTransforms &transforms)
{
    if (!m_device || !m_device->WriteBuffer(TransformBuffer(), 0, &transforms, sizeof(transforms)))
        return false;
    return UpdateMeshInterfaceMetadata(transforms) && UpdateVectorFieldMetadata(transforms);
}

bool ParticleGpuRuntime::UpdateMeshInterfaceMetadata(const GpuParticleTransforms &transforms)
{
    if (!m_dataInterfaces)
        return true;
    const glm::mat4 emitterToWorld = glm::make_mat4(transforms.emitterToWorld.data());
    const glm::mat4 worldToSimulation = glm::make_mat4(transforms.worldToSimulation.data());
    if (!IsFinite(emitterToWorld) || !IsFinite(worldToSimulation))
        return false;
    for (const auto &mesh : m_dataInterfaces->meshInterfaces) {
        const glm::mat4 sourceToWorld = mesh.worldSpace ? glm::mat4(1.0f) : emitterToWorld;
        const glm::mat4 meshToSimulation = worldToSimulation * sourceToWorld * RowMajorMatrix(mesh.meshToSpace);
        const glm::mat3 linear(meshToSimulation);
        const float determinant = glm::determinant(linear);
        if (!IsFinite(meshToSimulation) || !std::isfinite(determinant) || std::abs(determinant) <= 1.0e-8f)
            return false;
        const glm::mat3 normalToSimulation = glm::transpose(glm::inverse(linear));
        if (!IsFinite(normalToSimulation))
            return false;
        const size_t base = mesh.metadataOffsetWords;
        StoreMatrix(m_dataInterfaces->metadataWords, base + 4u, meshToSimulation);
        StoreNormalMatrix(m_dataInterfaces->metadataWords, base + 20u, normalToSimulation);
    }
    return m_device->WriteBuffer(m_dataInterfaces->metadataBuffer, 0, m_dataInterfaces->metadataWords.data(),
                                 m_dataInterfaces->metadataWords.size() * sizeof(uint32_t));
}

bool ParticleGpuRuntime::UpdateVectorFieldMetadata(const GpuParticleTransforms &transforms)
{
    if (!m_vectorFields)
        return true;
    const glm::mat4 emitterToWorld = glm::make_mat4(transforms.emitterToWorld.data());
    const glm::mat4 worldToSimulation = glm::make_mat4(transforms.worldToSimulation.data());
    if (!IsFinite(emitterToWorld) || !IsFinite(worldToSimulation))
        return false;
    for (size_t index = 0; index < m_vectorFields->vectorFields.size(); ++index) {
        const auto &field = m_vectorFields->vectorFields[index];
        const glm::mat4 sourceToWorld = field.worldSpace ? glm::mat4(1.0f) : emitterToWorld;
        const glm::mat4 fieldToSimulation = worldToSimulation * sourceToWorld * RowMajorMatrix(field.fieldToSpace);
        const float determinant = glm::determinant(fieldToSimulation);
        if (!IsFinite(fieldToSimulation) || !std::isfinite(determinant) || std::abs(determinant) <= 1.0e-8f)
            return false;
        const glm::mat4 simulationToField = glm::inverse(fieldToSimulation);
        const glm::mat3 fieldLinear(fieldToSimulation);
        const glm::mat3 directionToSimulation = field.kind == GpuVectorFieldDesc::Kind::SignedDistanceField
                                                    ? glm::transpose(glm::inverse(fieldLinear))
                                                    : fieldLinear;
        float scalar = field.vectorScale;
        if (field.kind == GpuVectorFieldDesc::Kind::SignedDistanceField) {
            const float minimumScale =
                (std::min)({glm::length(fieldLinear[0]), glm::length(fieldLinear[1]), glm::length(fieldLinear[2])});
            scalar *= minimumScale;
        }
        if (!IsFinite(simulationToField) || !IsFinite(directionToSimulation) || !std::isfinite(scalar) ||
            (field.kind == GpuVectorFieldDesc::Kind::SignedDistanceField && scalar <= 0.0f))
            return false;
        const size_t base = index * m_vectorFields->interfaceStrideWords;
        StoreMatrix(m_vectorFields->metadataWords, base, simulationToField);
        StoreNormalMatrix(m_vectorFields->metadataWords, base + 16, directionToSimulation);
        m_vectorFields->metadataWords[base + 28] = FloatBits(scalar);
    }
    return m_device->WriteBuffer(m_vectorFields->metadataBuffer, 0, m_vectorFields->metadataWords.data(),
                                 m_vectorFields->metadataWords.size() * sizeof(uint32_t));
}

rhi::BufferHandle ParticleGpuRuntime::StateBuffer() const noexcept
{
    return m_residentState ? m_residentState->states : rhi::BufferHandle{};
}

rhi::BufferHandle ParticleGpuRuntime::FreeListBuffer() const noexcept
{
    return m_residentState ? m_residentState->freeList : rhi::BufferHandle{};
}

rhi::BufferHandle ParticleGpuRuntime::CounterBuffer() const noexcept
{
    return m_residentState ? m_residentState->counters : rhi::BufferHandle{};
}

rhi::BufferHandle ParticleGpuRuntime::InstanceBuffer() const noexcept
{
    return m_residentState ? m_residentState->instances : rhi::BufferHandle{};
}

rhi::BufferHandle ParticleGpuRuntime::IndirectBuffer() const noexcept
{
    return m_residentState ? m_residentState->indirect : rhi::BufferHandle{};
}

rhi::BufferHandle ParticleGpuRuntime::RenderIndexBuffer() const noexcept
{
    return m_residentState ? m_residentState->renderIndices : rhi::BufferHandle{};
}

rhi::BufferHandle ParticleGpuRuntime::TransformBuffer() const noexcept
{
    return m_residentState ? m_residentState->transforms : rhi::BufferHandle{};
}

rhi::BufferHandle ParticleGpuRuntime::SimulationControlBuffer() const noexcept
{
    return m_residentState ? m_residentState->simulationControl : rhi::BufferHandle{};
}

void ParticleGpuRuntime::RecordBootstrap(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed,
                                         rhi::BindGroupHandle graphSpawnGroup)
{
    if (!IsValid() || !encoder.IsValid())
        return;
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = (std::max)(m_capacity, m_eventTypeCount);
    constants.systemSeed = systemSeed;
    Record(encoder, GpuKernelStage::Bootstrap, constants, constants.invocationCount, graphSpawnGroup);
    m_residentState->bootstrapRecorded = true;
}

void ParticleGpuRuntime::RecordInitIndirect(const rhi::ComputeCommandEncoder &encoder, uint32_t cpuSpawnCount,
                                            uint32_t spawnBaseId, uint32_t spawnGeneration, uint32_t systemSeed,
                                            uint32_t simulationStep, float deltaTime,
                                            rhi::BindGroupHandle graphSpawnGroup, rhi::BufferHandle spawnMetadata,
                                            uint64_t indirectOffset) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    // Until the generated Init kernel consumes set=3,binding=1 directly, this
    // remains the CPU count as a conservative compatibility guard. The GPU
    // indirect command is authoritative for dispatch size.
    constants.invocationCount = cpuSpawnCount;
    constants.spawnBaseId = spawnBaseId;
    constants.spawnGeneration = spawnGeneration;
    constants.systemSeed = systemSeed;
    constants.simulationStep = simulationStep;
    constants.deltaTime = deltaTime;
    Record(encoder, GpuKernelStage::Init, constants, 1, graphSpawnGroup, spawnMetadata, indirectOffset);
}

void ParticleGpuRuntime::RecordUpdate(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed,
                                      uint32_t simulationStep, float deltaTime, rhi::BindGroupHandle graphSpawnGroup,
                                      bool collectCollisionDiagnostics) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = m_capacity;
    constants.systemSeed = systemSeed;
    constants.simulationStep = simulationStep;
    constants.deltaTime = deltaTime;
    constants.diagnosticFlags = collectCollisionDiagnostics ? 1u : 0u;
    Record(encoder, GpuKernelStage::Update, constants, m_capacity, graphSpawnGroup);
}

void ParticleGpuRuntime::RecordContactPrepare(const rhi::ComputeCommandEncoder &encoder, uint32_t simulationStep,
                                              rhi::BindGroupHandle graphSpawnGroup, bool resetAll) const
{
    if (!HasContactRuntime() || !encoder.IsValid())
        return;
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    const auto telemetry = ContactTelemetry();
    constants.invocationCount =
        (std::max)({m_capacity, telemetry.contactHashCapacity, telemetry.continuationJoinCapacity});
    constants.simulationStep = simulationStep;
    constants.diagnosticFlags = resetAll ? 4u : 0u;
    Record(encoder, GpuKernelStage::ContactPrepare, constants, constants.invocationCount, graphSpawnGroup);
    ++m_contactPrepareRecordCalls;
}

void ParticleGpuRuntime::RecordContactSolve(const rhi::ComputeCommandEncoder &encoder, uint32_t simulationStep,
                                            rhi::BindGroupHandle graphSpawnGroup) const
{
    if (!HasContactRuntime() || !encoder.IsValid())
        return;
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = m_capacity;
    constants.simulationStep = simulationStep;
    Record(encoder, GpuKernelStage::ContactSolve, constants, m_capacity, graphSpawnGroup);
    ++m_contactSolveRecordCalls;
}

void ParticleGpuRuntime::RecordContactDispatch(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed,
                                               uint32_t simulationStep, float deltaTime,
                                               rhi::BindGroupHandle graphSpawnGroup,
                                               bool collectCollisionDiagnostics) const
{
    if (!HasContactRuntime() || !encoder.IsValid())
        return;
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = m_capacity;
    constants.systemSeed = systemSeed;
    constants.simulationStep = simulationStep;
    constants.deltaTime = deltaTime;
    constants.diagnosticFlags = collectCollisionDiagnostics ? 1u : 0u;
    Record(encoder, GpuKernelStage::ContactDispatch, constants, m_capacity, graphSpawnGroup,
           ContactResources().dispatchIndirect, 0);
    ++m_contactDispatchRecordCalls;
}

void ParticleGpuRuntime::RecordRenderReset(const rhi::ComputeCommandEncoder &encoder,
                                           rhi::BindGroupHandle graphSpawnGroup, bool resetCollisionDiagnostics) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = 1;
    constants.diagnosticFlags = resetCollisionDiagnostics ? 2u : 0u;
    Record(encoder, GpuKernelStage::RenderReset, constants, 1, graphSpawnGroup);
}

void ParticleGpuRuntime::RecordRendering(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed,
                                         uint32_t simulationStep, rhi::BindGroupHandle graphSpawnGroup) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = m_capacity;
    constants.systemSeed = systemSeed;
    constants.simulationStep = simulationStep;
    Record(encoder, GpuKernelStage::Rendering, constants, m_capacity, graphSpawnGroup);
}

void ParticleGpuRuntime::Record(const rhi::ComputeCommandEncoder &encoder, GpuKernelStage stage,
                                const GpuParticlePushConstants &constants, uint32_t invocationCount,
                                rhi::BindGroupHandle graphSpawnGroup, rhi::BufferHandle indirectArguments,
                                uint64_t indirectOffset) const
{
    if (!IsValid() || !encoder.IsValid() || invocationCount == 0 || !graphSpawnGroup.IsValid())
        return;
    const auto pipeline = m_pipelines[StageIndex(stage)];
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, m_group);
    encoder.BindGroup(pipeline, 1, m_dataInterfaces ? m_dataInterfaces->group : m_emptyDataInterfaceGroup);
    encoder.BindGroup(pipeline, 2, m_vectorFields ? m_vectorFields->group : m_emptyDataInterfaceGroup);
    encoder.BindGroup(pipeline, 3, graphSpawnGroup);
    encoder.BindGroup(pipeline, 4, m_emptyDataInterfaceGroup);
    encoder.BindGroup(pipeline, 5, m_continuation ? m_continuation->Group() : m_emptyDataInterfaceGroup);
    encoder.BindGroup(pipeline, 6, m_collisionEnabled ? m_collisionSceneGroup : m_emptyDataInterfaceGroup);
    encoder.BindGroup(pipeline, 7, m_contacts ? m_contacts->Group() : m_emptyDataInterfaceGroup);
    encoder.PushConstants(pipeline, sizeof(constants), &constants);
    if (indirectArguments.IsValid())
        encoder.DispatchIndirect(indirectArguments, indirectOffset);
    else
        encoder.Dispatch(GroupCount(invocationCount), 1, 1);
}

uint32_t ParticleGpuRuntime::GroupCount(uint32_t invocationCount) noexcept
{
    return invocationCount == 0 ? 0 : 1 + (invocationCount - 1) / WorkgroupSize;
}

} // namespace infernux::particle
