#include "ParticleGpuRuntime.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>

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

constexpr uint32_t EventOutputStageBit(GpuKernelStage stage) noexcept
{
    return 1u << static_cast<uint32_t>(stage);
}

constexpr uint32_t ValidEventOutputStageMask = EventOutputStageBit(GpuKernelStage::Init) |
                                               EventOutputStageBit(GpuKernelStage::Update) |
                                               EventOutputStageBit(GpuKernelStage::Rendering);

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
        device->Release(eventInputLayout);
        device->Release(eventOutputLayout);
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
    rhi::BindingLayoutHandle eventInputLayout;
    rhi::BindingLayoutHandle eventOutputLayout;
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
    }

    rhi::Device *device = nullptr;
    rhi::BindingLayoutHandle layout;
    rhi::BindGroupHandle group;
    rhi::BufferHandle metadataBuffer;
    std::vector<GpuPointCacheDesc> pointCaches;
    std::optional<GpuMeshShapeDesc> meshShape;
    std::vector<uint32_t> metadataWords;
    uint32_t interfaceStrideWords = 0;
    uint32_t sampleStrideWords = 0;
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
    return CreateInternal(device, desc, {});
}

bool ParticleGpuRuntime::CreateCompatible(rhi::Device &device, const GpuEmitterDesc &desc,
                                          const ParticleGpuRuntime &previous)
{
    if (!previous.IsValid() || previous.m_device != &device || previous.Capacity() != desc.capacity ||
        previous.StateStride() != desc.stateStride)
        return false;
    return CreateInternal(device, desc, previous.m_residentState);
}

bool ParticleGpuRuntime::AdoptCompatibleRevision(ParticleGpuRuntime &replacement) noexcept
{
    if (!IsValid() || !replacement.IsValid() || m_device != replacement.m_device ||
        m_capacity != replacement.m_capacity || m_stateStride != replacement.m_stateStride ||
        m_residentState != replacement.m_residentState)
        return false;
    std::swap(m_layout, replacement.m_layout);
    std::swap(m_group, replacement.m_group);
    std::swap(m_dataInterfaces, replacement.m_dataInterfaces);
    std::swap(m_vectorFields, replacement.m_vectorFields);
    std::swap(m_emptyDataInterfaceLayout, replacement.m_emptyDataInterfaceLayout);
    std::swap(m_emptyDataInterfaceGroup, replacement.m_emptyDataInterfaceGroup);
    std::swap(m_eventInputLayout, replacement.m_eventInputLayout);
    std::swap(m_eventOutputLayout, replacement.m_eventOutputLayout);
    std::swap(m_eventOutputStageMask, replacement.m_eventOutputStageMask);
    std::swap(m_eventInitPipeline, replacement.m_eventInitPipeline);
    std::swap(m_pipelines, replacement.m_pipelines);
    return true;
}

bool ParticleGpuRuntime::CreateInternal(rhi::Device &device, const GpuEmitterDesc &desc,
                                        std::shared_ptr<ResidentState> residentState)
{
    Destroy();
    uint64_t stateBytes = 0;
    uint64_t instanceBytes = 0;
    if (!CheckedBufferSize(desc.capacity, desc.stateStride, stateBytes) ||
        !CheckedBufferSize(desc.capacity, RenderInstanceStride, instanceBytes) ||
        (desc.eventOutputStageMask & ~ValidEventOutputStageMask) != 0u)
        return false;
    for (const auto &kernel : desc.kernels) {
        if (!kernel.words || kernel.wordCount == 0)
            return false;
    }
    if (!desc.eventInitKernel.words || desc.eventInitKernel.wordCount == 0)
        return false;

    m_device = &device;
    m_capacity = desc.capacity;
    m_stateStride = desc.stateStride;
    m_eventOutputStageMask = desc.eventOutputStageMask;
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
        m_residentState->states = device.CreateBuffer({stateBytes, storage});
        m_residentState->freeList =
            device.CreateBuffer({static_cast<uint64_t>(desc.capacity) * sizeof(uint32_t), storage});
        m_residentState->counters = device.CreateBuffer({16, storage | rhi::BufferUsageFlags::TransferSource});
        m_residentState->instances = device.CreateBuffer({instanceBytes, storage});
        m_residentState->indirect = device.CreateBuffer({16, storage | rhi::BufferUsageFlags::Indirect});
        m_residentState->renderIndices =
            device.CreateBuffer({static_cast<uint64_t>(desc.capacity) * sizeof(uint32_t), storage});
        rhi::BufferDesc transformDesc;
        transformDesc.byteSize = sizeof(GpuParticleTransforms);
        transformDesc.usage = rhi::BufferUsageFlags::Uniform;
        transformDesc.memory = rhi::BufferMemory::Upload;
        m_residentState->transforms = device.CreateBuffer(transformDesc);
        rhi::BindingLayoutDesc eventLayoutDesc;
        for (uint32_t binding = 0; binding < 4; ++binding) {
            eventLayoutDesc.entries[binding] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
        }
        eventLayoutDesc.entryCount = 4;
        m_residentState->eventInputLayout = device.CreateBindingLayout(eventLayoutDesc);
        rhi::BindingLayoutDesc eventOutputLayoutDesc;
        for (uint32_t binding = 0; binding < 3; ++binding) {
            eventOutputLayoutDesc.entries[binding] = {binding, rhi::BindingType::StorageBuffer,
                                                      rhi::ShaderStage::Compute, 1};
        }
        eventOutputLayoutDesc.entryCount = 3;
        m_residentState->eventOutputLayout = device.CreateBindingLayout(eventOutputLayoutDesc);
        if (!StateBuffer().IsValid() || !FreeListBuffer().IsValid() || !CounterBuffer().IsValid() ||
            !InstanceBuffer().IsValid() || !IndirectBuffer().IsValid() || !RenderIndexBuffer().IsValid() ||
            !TransformBuffer().IsValid() || !m_residentState->eventInputLayout.IsValid() ||
            !m_residentState->eventOutputLayout.IsValid()) {
            Destroy();
            return false;
        }
    }

    rhi::BindingLayoutDesc layoutDesc;
    for (uint32_t binding = 0; binding < 5; ++binding)
        layoutDesc.entries[binding] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entries[5] = {5, rhi::BindingType::UniformBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entries[6] = {6, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entryCount = 7;
    m_layout = device.CreateBindingLayout(layoutDesc);
    if (!m_layout.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_layout;
    const std::array<rhi::BufferHandle, 7> buffers = {
        StateBuffer(),    FreeListBuffer(),  CounterBuffer(),     InstanceBuffer(),
        IndirectBuffer(), TransformBuffer(), RenderIndexBuffer(),
    };
    for (uint32_t binding = 0; binding < buffers.size(); ++binding) {
        groupDesc.buffers[binding].binding = binding;
        groupDesc.buffers[binding].type =
            binding == 5 ? rhi::BindingType::UniformBuffer : rhi::BindingType::StorageBuffer;
        groupDesc.buffers[binding].buffer = buffers[binding];
    }
    groupDesc.bufferCount = static_cast<uint32_t>(buffers.size());
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

    m_eventInputLayout = m_residentState->eventInputLayout;
    m_eventOutputLayout = m_residentState->eventOutputLayout;

    const auto &pointCacheLayout = desc.pointCaches;
    if (!pointCacheLayout.pointCaches.empty() || desc.meshShape) {
        const size_t maxPointCaches = desc.meshShape ? 6 : 7;
        if (pointCacheLayout.metadataBinding != 0 || pointCacheLayout.interfaceStrideWords < 32 ||
            pointCacheLayout.sampleStrideWords < 4 ||
            (!pointCacheLayout.pointCaches.empty() && pointCacheLayout.sampleCount == 0) ||
            pointCacheLayout.pointCaches.size() > maxPointCaches) {
            Destroy();
            return false;
        }
        const uint64_t pointCacheMetadataWordCount =
            static_cast<uint64_t>(pointCacheLayout.pointCaches.size()) * pointCacheLayout.interfaceStrideWords +
            static_cast<uint64_t>(pointCacheLayout.sampleCount) * pointCacheLayout.sampleStrideWords;
        const uint64_t metadataWordCount = pointCacheMetadataWordCount + (desc.meshShape ? 4u : 0u);
        if (metadataWordCount == 0 || metadataWordCount > std::numeric_limits<uint32_t>::max()) {
            Destroy();
            return false;
        }

        auto dataInterfaces = std::make_unique<DataInterfaceState>();
        dataInterfaces->device = &device;
        dataInterfaces->pointCaches = pointCacheLayout.pointCaches;
        dataInterfaces->interfaceStrideWords = pointCacheLayout.interfaceStrideWords;
        dataInterfaces->sampleStrideWords = pointCacheLayout.sampleStrideWords;
        dataInterfaces->metadataWords.assign(static_cast<size_t>(metadataWordCount), 0);

        std::array<bool, rhi::BindingLayoutDesc::MaxEntries> usedBindings{};
        usedBindings[pointCacheLayout.metadataBinding] = true;
        std::vector<bool> usedSamples(pointCacheLayout.sampleCount, false);
        rhi::BindingLayoutDesc dataLayoutDesc;
        dataLayoutDesc.entries[0] = {pointCacheLayout.metadataBinding, rhi::BindingType::StorageBuffer,
                                     rhi::ShaderStage::Compute, 1};
        dataLayoutDesc.entryCount = 1;

        rhi::BindGroupDesc dataGroupDesc;
        dataGroupDesc.bufferCount = 1;
        dataGroupDesc.buffers[0].binding = pointCacheLayout.metadataBinding;
        dataGroupDesc.buffers[0].type = rhi::BindingType::StorageBuffer;

        for (size_t index = 0; index < pointCacheLayout.pointCaches.size(); ++index) {
            const auto &pointCache = pointCacheLayout.pointCaches[index];
            if (pointCache.interfaceIndex != index || pointCache.dataBinding >= usedBindings.size() ||
                pointCache.lookupBinding >= usedBindings.size() || usedBindings[pointCache.dataBinding] ||
                usedBindings[pointCache.lookupBinding] || !pointCache.data || !pointCache.data->IsValid() ||
                !pointCache.dataBuffer.IsValid() || !pointCache.lookupBuffer.IsValid() || !pointCache.keepAlive ||
                pointCache.data->bytes.empty() || pointCache.data->bytes.size() % sizeof(uint32_t) != 0) {
                Destroy();
                return false;
            }
            usedBindings[pointCache.dataBinding] = true;
            usedBindings[pointCache.lookupBinding] = true;
            const size_t interfaceBase = index * pointCacheLayout.interfaceStrideWords;
            const glm::mat4 cacheToSpace = RowMajorMatrix(pointCache.cacheToSpace);
            if (!IsFinite(cacheToSpace)) {
                Destroy();
                return false;
            }
            StoreMatrix(dataInterfaces->metadataWords, interfaceBase, cacheToSpace);
            const bool requiresNormal =
                std::any_of(pointCache.samples.begin(), pointCache.samples.end(),
                            [](const GpuPointCacheSampleDesc &sample) { return sample.requiresNormalTransform; });
            const glm::mat3 linear(cacheToSpace);
            const float determinant = glm::determinant(linear);
            if (requiresNormal && (!std::isfinite(determinant) || std::abs(determinant) <= 1.0e-8f)) {
                Destroy();
                return false;
            }
            const glm::mat3 normal = requiresNormal ? glm::transpose(glm::inverse(linear)) : glm::mat3(1.0f);
            if (!IsFinite(normal)) {
                Destroy();
                return false;
            }
            StoreNormalMatrix(dataInterfaces->metadataWords, interfaceBase + 16, normal);
            dataInterfaces->metadataWords[interfaceBase + 28] = pointCache.data->pointCount;
            dataInterfaces->metadataWords[interfaceBase + 29] =
                pointCache.data->idLookupMode == PointCacheIdLookupMode::Hash
                    ? static_cast<uint32_t>(pointCache.data->idLookup.size() - 1)
                    : 0;
            dataInterfaces->metadataWords[interfaceBase + 30] =
                pointCache.data->idLookupMode == PointCacheIdLookupMode::Hash ? 1u : 0u;

            for (const auto &sample : pointCache.samples) {
                const auto *channel = pointCache.data->FindChannel(sample.channel);
                if (sample.sampleIndex >= usedSamples.size() || usedSamples[sample.sampleIndex] || !channel ||
                    channel->type != sample.expectedType || channel->byteOffset % sizeof(uint32_t) != 0 ||
                    channel->elementStride % sizeof(uint32_t) != 0 ||
                    channel->byteOffset / sizeof(uint32_t) > std::numeric_limits<uint32_t>::max()) {
                    Destroy();
                    return false;
                }
                usedSamples[sample.sampleIndex] = true;
                const size_t sampleBase = pointCacheLayout.pointCaches.size() * pointCacheLayout.interfaceStrideWords +
                                          sample.sampleIndex * pointCacheLayout.sampleStrideWords;
                dataInterfaces->metadataWords[sampleBase] =
                    static_cast<uint32_t>(channel->byteOffset / sizeof(uint32_t));
                dataInterfaces->metadataWords[sampleBase + 1] = channel->elementStride / sizeof(uint32_t);
                dataInterfaces->metadataWords[sampleBase + 2] = static_cast<uint32_t>(channel->type);
                dataInterfaces->metadataWords[sampleBase + 3] = pointCache.interfaceIndex;
            }

            const bool hashedLookup = pointCache.data->idLookupMode == PointCacheIdLookupMode::Hash;
            if (hashedLookup && (pointCache.data->idLookup.empty() ||
                                 (pointCache.data->idLookup.size() & (pointCache.data->idLookup.size() - 1)) != 0)) {
                Destroy();
                return false;
            }
            const uint32_t layoutOffset = dataLayoutDesc.entryCount;
            dataLayoutDesc.entries[layoutOffset] = {pointCache.dataBinding, rhi::BindingType::StorageBuffer,
                                                    rhi::ShaderStage::Compute, 1};
            dataLayoutDesc.entries[layoutOffset + 1] = {pointCache.lookupBinding, rhi::BindingType::StorageBuffer,
                                                        rhi::ShaderStage::Compute, 1};
            dataLayoutDesc.entryCount += 2;
            const uint32_t groupOffset = dataGroupDesc.bufferCount;
            dataGroupDesc.buffers[groupOffset] = {pointCache.dataBinding, rhi::BindingType::StorageBuffer,
                                                  pointCache.dataBuffer};
            dataGroupDesc.buffers[groupOffset + 1] = {pointCache.lookupBinding, rhi::BindingType::StorageBuffer,
                                                      pointCache.lookupBuffer};
            dataGroupDesc.bufferCount += 2;
        }
        if (desc.meshShape) {
            const auto &meshShape = *desc.meshShape;
            if (meshShape.metadataOffsetWords != pointCacheMetadataWordCount ||
                meshShape.vertexBinding >= usedBindings.size() || meshShape.triangleBinding >= usedBindings.size() ||
                meshShape.vertexBinding == meshShape.triangleBinding || usedBindings[meshShape.vertexBinding] ||
                usedBindings[meshShape.triangleBinding] || meshShape.vertexCount == 0 || meshShape.triangleCount == 0 ||
                !meshShape.vertices.IsValid() || !meshShape.triangles.IsValid() || !meshShape.keepAlive) {
                Destroy();
                return false;
            }
            usedBindings[meshShape.vertexBinding] = true;
            usedBindings[meshShape.triangleBinding] = true;
            dataInterfaces->metadataWords[meshShape.metadataOffsetWords] = meshShape.vertexCount;
            dataInterfaces->metadataWords[meshShape.metadataOffsetWords + 1] = meshShape.triangleCount;
            dataInterfaces->meshShape = meshShape;
            const uint32_t layoutOffset = dataLayoutDesc.entryCount;
            dataLayoutDesc.entries[layoutOffset] = {meshShape.vertexBinding, rhi::BindingType::StorageBuffer,
                                                    rhi::ShaderStage::Compute, 1};
            dataLayoutDesc.entries[layoutOffset + 1] = {meshShape.triangleBinding, rhi::BindingType::StorageBuffer,
                                                        rhi::ShaderStage::Compute, 1};
            dataLayoutDesc.entryCount += 2;
            const uint32_t groupOffset = dataGroupDesc.bufferCount;
            dataGroupDesc.buffers[groupOffset] = {meshShape.vertexBinding, rhi::BindingType::StorageBuffer,
                                                  meshShape.vertices};
            dataGroupDesc.buffers[groupOffset + 1] = {meshShape.triangleBinding, rhi::BindingType::StorageBuffer,
                                                      meshShape.triangles};
            dataGroupDesc.bufferCount += 2;
        }
        if (std::find(usedSamples.begin(), usedSamples.end(), false) != usedSamples.end()) {
            Destroy();
            return false;
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
    } else if (pointCacheLayout.sampleCount != 0) {
        Destroy();
        return false;
    }

    const auto &vectorFieldLayout = desc.vectorFields;
    if (!vectorFieldLayout.vectorFields.empty()) {
        if (vectorFieldLayout.metadataBinding != 0 || vectorFieldLayout.interfaceStrideWords < 32 ||
            vectorFieldLayout.vectorFields.size() > 15) {
            Destroy();
            return false;
        }
        const uint64_t metadataWordCount =
            static_cast<uint64_t>(vectorFieldLayout.vectorFields.size()) * vectorFieldLayout.interfaceStrideWords;
        if (metadataWordCount == 0 || metadataWordCount > std::numeric_limits<uint32_t>::max()) {
            Destroy();
            return false;
        }

        auto vectorFields = std::make_unique<VectorFieldState>();
        vectorFields->device = &device;
        vectorFields->vectorFields = vectorFieldLayout.vectorFields;
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

    for (size_t index = 0; index < desc.kernels.size(); ++index) {
        const auto shader = device.CreateShaderModule({desc.kernels[index].words, desc.kernels[index].wordCount});
        if (!shader.IsValid()) {
            Destroy();
            return false;
        }
        rhi::ComputePipelineDesc pipelineDesc;
        pipelineDesc.computeShader = shader;
        pipelineDesc.bindingLayouts[0] = m_layout;
        pipelineDesc.bindingLayoutCount = 1;
        if (m_dataInterfaces) {
            pipelineDesc.bindingLayouts[1] = m_dataInterfaces->layout;
            pipelineDesc.bindingLayoutCount = 2;
        }
        if (m_vectorFields) {
            pipelineDesc.bindingLayouts[1] = m_dataInterfaces ? m_dataInterfaces->layout : m_emptyDataInterfaceLayout;
            pipelineDesc.bindingLayouts[2] = m_vectorFields->layout;
            pipelineDesc.bindingLayoutCount = 3;
        }
        if ((m_eventOutputStageMask & EventOutputStageBit(static_cast<GpuKernelStage>(index))) != 0u) {
            if (pipelineDesc.bindingLayoutCount < 3) {
                pipelineDesc.bindingLayouts[1] =
                    m_dataInterfaces ? m_dataInterfaces->layout : m_emptyDataInterfaceLayout;
                pipelineDesc.bindingLayouts[2] = m_vectorFields ? m_vectorFields->layout : m_emptyDataInterfaceLayout;
            }
            pipelineDesc.bindingLayouts[3] = m_eventOutputLayout;
            pipelineDesc.bindingLayoutCount = 4;
        }
        pipelineDesc.pushConstantBytes = sizeof(GpuParticlePushConstants);
        m_pipelines[index] = device.CreateComputePipeline(pipelineDesc);
        device.Release(shader);
        if (!m_pipelines[index].IsValid()) {
            Destroy();
            return false;
        }
    }

    const auto eventInitShader =
        device.CreateShaderModule({desc.eventInitKernel.words, desc.eventInitKernel.wordCount});
    if (!eventInitShader.IsValid()) {
        Destroy();
        return false;
    }
    rhi::ComputePipelineDesc eventPipelineDesc;
    eventPipelineDesc.computeShader = eventInitShader;
    eventPipelineDesc.bindingLayouts[0] = m_layout;
    eventPipelineDesc.bindingLayouts[1] = m_dataInterfaces ? m_dataInterfaces->layout : m_emptyDataInterfaceLayout;
    eventPipelineDesc.bindingLayouts[2] = m_vectorFields ? m_vectorFields->layout : m_emptyDataInterfaceLayout;
    eventPipelineDesc.bindingLayouts[3] = m_eventInputLayout;
    eventPipelineDesc.bindingLayoutCount = 4;
    if (HasEventOutput(GpuKernelStage::Init)) {
        eventPipelineDesc.bindingLayouts[4] = m_eventOutputLayout;
        eventPipelineDesc.bindingLayoutCount = 5;
    }
    eventPipelineDesc.pushConstantBytes = sizeof(GpuParticlePushConstants);
    m_eventInitPipeline = device.CreateComputePipeline(eventPipelineDesc);
    device.Release(eventInitShader);
    if (!m_eventInitPipeline.IsValid()) {
        Destroy();
        return false;
    }
    return true;
}

void ParticleGpuRuntime::Destroy() noexcept
{
    if (m_device) {
        m_device->Release(m_eventInitPipeline);
        for (auto pipeline : m_pipelines)
            m_device->Release(pipeline);
        m_device->Release(m_group);
        m_device->Release(m_layout);
        m_device->Release(m_emptyDataInterfaceGroup);
        m_device->Release(m_emptyDataInterfaceLayout);
    }
    m_dataInterfaces.reset();
    m_vectorFields.reset();
    m_residentState.reset();
    m_device = nullptr;
    m_capacity = 0;
    m_stateStride = 0;
    m_eventOutputStageMask = 0;
    m_layout = {};
    m_group = {};
    m_emptyDataInterfaceLayout = {};
    m_emptyDataInterfaceGroup = {};
    m_eventInputLayout = {};
    m_eventOutputLayout = {};
    m_eventInitPipeline = {};
    m_pipelines.fill({});
}

bool ParticleGpuRuntime::IsValid() const noexcept
{
    if (!m_device || !m_residentState || m_capacity == 0 || !m_group.IsValid() || !m_eventInputLayout.IsValid() ||
        !m_eventOutputLayout.IsValid() || !m_eventInitPipeline.IsValid() || !m_emptyDataInterfaceLayout.IsValid() ||
        !m_emptyDataInterfaceGroup.IsValid())
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
    for (auto pipeline : m_pipelines) {
        if (!pipeline.IsValid())
            return false;
    }
    return true;
}

bool ParticleGpuRuntime::HasEventOutput(GpuKernelStage stage) const noexcept
{
    return (m_eventOutputStageMask & EventOutputStageBit(stage)) != 0u;
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

bool ParticleGpuRuntime::UpdateTransforms(const GpuParticleTransforms &transforms)
{
    if (!m_device || !m_device->WriteBuffer(TransformBuffer(), 0, &transforms, sizeof(transforms)))
        return false;
    return UpdatePointCacheMetadata(transforms) && UpdateVectorFieldMetadata(transforms);
}

bool ParticleGpuRuntime::UpdatePointCacheMetadata(const GpuParticleTransforms &transforms)
{
    if (!m_dataInterfaces)
        return true;
    const glm::mat4 emitterToWorld = glm::make_mat4(transforms.emitterToWorld.data());
    const glm::mat4 worldToSimulation = glm::make_mat4(transforms.worldToSimulation.data());
    if (!IsFinite(emitterToWorld) || !IsFinite(worldToSimulation))
        return false;
    for (size_t index = 0; index < m_dataInterfaces->pointCaches.size(); ++index) {
        const auto &pointCache = m_dataInterfaces->pointCaches[index];
        const glm::mat4 sourceToWorld = pointCache.worldSpace ? glm::mat4(1.0f) : emitterToWorld;
        const glm::mat4 cacheToSimulation = worldToSimulation * sourceToWorld * RowMajorMatrix(pointCache.cacheToSpace);
        if (!IsFinite(cacheToSimulation))
            return false;
        const bool requiresNormal =
            std::any_of(pointCache.samples.begin(), pointCache.samples.end(),
                        [](const GpuPointCacheSampleDesc &sample) { return sample.requiresNormalTransform; });
        const glm::mat3 linear(cacheToSimulation);
        const float determinant = glm::determinant(linear);
        if (requiresNormal && (!std::isfinite(determinant) || std::abs(determinant) <= 1.0e-8f))
            return false;
        const glm::mat3 normal = requiresNormal ? glm::transpose(glm::inverse(linear)) : glm::mat3(1.0f);
        if (!IsFinite(normal))
            return false;
        const size_t interfaceBase = index * m_dataInterfaces->interfaceStrideWords;
        StoreMatrix(m_dataInterfaces->metadataWords, interfaceBase, cacheToSimulation);
        StoreNormalMatrix(m_dataInterfaces->metadataWords, interfaceBase + 16, normal);
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

void ParticleGpuRuntime::RecordBootstrap(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed)
{
    if (!IsValid() || !encoder.IsValid())
        return;
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = m_capacity;
    constants.systemSeed = systemSeed;
    Record(encoder, GpuKernelStage::Bootstrap, constants, m_capacity);
    m_residentState->bootstrapRecorded = true;
}

void ParticleGpuRuntime::RecordInit(const rhi::ComputeCommandEncoder &encoder, uint32_t spawnCount,
                                    uint32_t spawnBaseId, uint32_t spawnGeneration, uint32_t systemSeed,
                                    uint32_t simulationStep, float deltaTime, rhi::BindGroupHandle eventOutput) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = spawnCount;
    constants.spawnBaseId = spawnBaseId;
    constants.spawnGeneration = spawnGeneration;
    constants.systemSeed = systemSeed;
    constants.simulationStep = simulationStep;
    constants.deltaTime = deltaTime;
    constants.reserved = 1;
    Record(encoder, GpuKernelStage::Init, constants, spawnCount, eventOutput);
}

void ParticleGpuRuntime::RecordEventInit(const rhi::ComputeCommandEncoder &encoder, rhi::BindGroupHandle eventInput,
                                         rhi::BufferHandle indirectArguments, uint64_t indirectOffset,
                                         uint32_t channelIndex, uint32_t systemSeed, uint32_t simulationStep,
                                         float deltaTime, rhi::BindGroupHandle eventOutput) const
{
    if (!IsValid() || !encoder.IsValid() || !eventInput.IsValid() || !indirectArguments.IsValid())
        return;
    if (HasEventOutput(GpuKernelStage::Init) && !eventOutput.IsValid())
        return;
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.spawnBaseId = channelIndex;
    constants.systemSeed = systemSeed;
    constants.simulationStep = simulationStep;
    constants.deltaTime = deltaTime;
    constants.reserved = 1;
    encoder.BindPipeline(m_eventInitPipeline);
    encoder.BindGroup(m_eventInitPipeline, 0, m_group);
    encoder.BindGroup(m_eventInitPipeline, 1, m_dataInterfaces ? m_dataInterfaces->group : m_emptyDataInterfaceGroup);
    encoder.BindGroup(m_eventInitPipeline, 2, m_vectorFields ? m_vectorFields->group : m_emptyDataInterfaceGroup);
    encoder.BindGroup(m_eventInitPipeline, 3, eventInput);
    if (HasEventOutput(GpuKernelStage::Init))
        encoder.BindGroup(m_eventInitPipeline, 4, eventOutput);
    encoder.PushConstants(m_eventInitPipeline, sizeof(constants), &constants);
    encoder.DispatchIndirect(indirectArguments, indirectOffset);
}

void ParticleGpuRuntime::RecordUpdate(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed,
                                      uint32_t simulationStep, float deltaTime, rhi::BindGroupHandle eventOutput) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = m_capacity;
    constants.systemSeed = systemSeed;
    constants.simulationStep = simulationStep;
    constants.deltaTime = deltaTime;
    constants.reserved = 1;
    Record(encoder, GpuKernelStage::Update, constants, m_capacity, eventOutput);
}

void ParticleGpuRuntime::RecordRenderReset(const rhi::ComputeCommandEncoder &encoder) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = 1;
    Record(encoder, GpuKernelStage::RenderReset, constants, 1);
}

void ParticleGpuRuntime::RecordRendering(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed,
                                         uint32_t simulationStep, rhi::BindGroupHandle eventOutput,
                                         bool emitEvents) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = m_capacity;
    constants.systemSeed = systemSeed;
    constants.simulationStep = simulationStep;
    constants.reserved = emitEvents ? 1u : 0u;
    Record(encoder, GpuKernelStage::Rendering, constants, m_capacity, eventOutput);
}

void ParticleGpuRuntime::Record(const rhi::ComputeCommandEncoder &encoder, GpuKernelStage stage,
                                const GpuParticlePushConstants &constants, uint32_t invocationCount,
                                rhi::BindGroupHandle eventOutput) const
{
    if (!IsValid() || !encoder.IsValid() || invocationCount == 0)
        return;
    if (HasEventOutput(stage) && !eventOutput.IsValid())
        return;
    const auto pipeline = m_pipelines[StageIndex(stage)];
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, m_group);
    if (m_dataInterfaces)
        encoder.BindGroup(pipeline, 1, m_dataInterfaces->group);
    else if (m_vectorFields)
        encoder.BindGroup(pipeline, 1, m_emptyDataInterfaceGroup);
    if (m_vectorFields)
        encoder.BindGroup(pipeline, 2, m_vectorFields->group);
    if (HasEventOutput(stage))
        encoder.BindGroup(pipeline, 3, eventOutput);
    encoder.PushConstants(pipeline, sizeof(constants), &constants);
    encoder.Dispatch(GroupCount(invocationCount), 1, 1);
}

uint32_t ParticleGpuRuntime::GroupCount(uint32_t invocationCount) noexcept
{
    return invocationCount == 0 ? 0 : 1 + (invocationCount - 1) / WorkgroupSize;
}

} // namespace infernux::particle
