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
        for (auto buffer : lookupBuffers)
            device->Release(buffer);
        for (auto buffer : dataBuffers)
            device->Release(buffer);
        device->Release(metadataBuffer);
    }

    rhi::Device *device = nullptr;
    rhi::BindingLayoutHandle layout;
    rhi::BindGroupHandle group;
    rhi::BufferHandle metadataBuffer;
    std::vector<rhi::BufferHandle> dataBuffers;
    std::vector<rhi::BufferHandle> lookupBuffers;
    std::vector<GpuPointCacheDesc> pointCaches;
    std::vector<uint32_t> metadataWords;
    uint32_t interfaceStrideWords = 0;
    uint32_t sampleStrideWords = 0;
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

bool ParticleGpuRuntime::CreateInternal(rhi::Device &device, const GpuEmitterDesc &desc,
                                        std::shared_ptr<ResidentState> residentState)
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
        m_residentState->counters = device.CreateBuffer({16, storage});
        m_residentState->instances = device.CreateBuffer({instanceBytes, storage});
        m_residentState->indirect = device.CreateBuffer({16, storage | rhi::BufferUsageFlags::Indirect});
        m_residentState->renderIndices =
            device.CreateBuffer({static_cast<uint64_t>(desc.capacity) * sizeof(uint32_t), storage});
        rhi::BufferDesc transformDesc;
        transformDesc.byteSize = sizeof(GpuParticleTransforms);
        transformDesc.usage = rhi::BufferUsageFlags::Uniform;
        transformDesc.memory = rhi::BufferMemory::Upload;
        m_residentState->transforms = device.CreateBuffer(transformDesc);
        if (!StateBuffer().IsValid() || !FreeListBuffer().IsValid() || !CounterBuffer().IsValid() ||
            !InstanceBuffer().IsValid() || !IndirectBuffer().IsValid() || !RenderIndexBuffer().IsValid() ||
            !TransformBuffer().IsValid()) {
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

    const auto &pointCacheLayout = desc.pointCaches;
    if (!pointCacheLayout.pointCaches.empty()) {
        if (pointCacheLayout.metadataBinding != 0 || pointCacheLayout.interfaceStrideWords < 32 ||
            pointCacheLayout.sampleStrideWords < 4 || pointCacheLayout.sampleCount == 0 ||
            pointCacheLayout.pointCaches.size() > 7) {
            Destroy();
            return false;
        }
        const uint64_t metadataWordCount =
            static_cast<uint64_t>(pointCacheLayout.pointCaches.size()) * pointCacheLayout.interfaceStrideWords +
            static_cast<uint64_t>(pointCacheLayout.sampleCount) * pointCacheLayout.sampleStrideWords;
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
        dataInterfaces->dataBuffers.reserve(pointCacheLayout.pointCaches.size());
        dataInterfaces->lookupBuffers.reserve(pointCacheLayout.pointCaches.size());

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

            rhi::BufferDesc dataBufferDesc;
            dataBufferDesc.byteSize = pointCache.data->bytes.size();
            dataBufferDesc.usage = rhi::BufferUsageFlags::Storage;
            dataBufferDesc.memory = rhi::BufferMemory::Upload;
            dataBufferDesc.initialData = pointCache.data->bytes.data();
            dataBufferDesc.initialDataBytes = pointCache.data->bytes.size();
            const auto dataBuffer = device.CreateBuffer(dataBufferDesc);

            const PointCacheIdLookupEntry identityLookup = {0, UINT32_MAX};
            const bool hashedLookup = pointCache.data->idLookupMode == PointCacheIdLookupMode::Hash;
            if (hashedLookup && (pointCache.data->idLookup.empty() ||
                                 (pointCache.data->idLookup.size() & (pointCache.data->idLookup.size() - 1)) != 0)) {
                Destroy();
                return false;
            }
            rhi::BufferDesc lookupBufferDesc;
            lookupBufferDesc.byteSize = hashedLookup
                                            ? pointCache.data->idLookup.size() * sizeof(PointCacheIdLookupEntry)
                                            : sizeof(identityLookup);
            lookupBufferDesc.usage = rhi::BufferUsageFlags::Storage;
            lookupBufferDesc.memory = rhi::BufferMemory::Upload;
            lookupBufferDesc.initialData =
                hashedLookup ? static_cast<const void *>(pointCache.data->idLookup.data()) : &identityLookup;
            lookupBufferDesc.initialDataBytes = lookupBufferDesc.byteSize;
            const auto lookupBuffer = device.CreateBuffer(lookupBufferDesc);
            if (!dataBuffer.IsValid() || !lookupBuffer.IsValid()) {
                device.Release(dataBuffer);
                device.Release(lookupBuffer);
                Destroy();
                return false;
            }
            dataInterfaces->dataBuffers.push_back(dataBuffer);
            dataInterfaces->lookupBuffers.push_back(lookupBuffer);

            const uint32_t layoutOffset = dataLayoutDesc.entryCount;
            dataLayoutDesc.entries[layoutOffset] = {pointCache.dataBinding, rhi::BindingType::StorageBuffer,
                                                    rhi::ShaderStage::Compute, 1};
            dataLayoutDesc.entries[layoutOffset + 1] = {pointCache.lookupBinding, rhi::BindingType::StorageBuffer,
                                                        rhi::ShaderStage::Compute, 1};
            dataLayoutDesc.entryCount += 2;
            const uint32_t groupOffset = dataGroupDesc.bufferCount;
            dataGroupDesc.buffers[groupOffset] = {pointCache.dataBinding, rhi::BindingType::StorageBuffer, dataBuffer};
            dataGroupDesc.buffers[groupOffset + 1] = {pointCache.lookupBinding, rhi::BindingType::StorageBuffer,
                                                      lookupBuffer};
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
    if (m_device) {
        for (auto pipeline : m_pipelines)
            m_device->Release(pipeline);
        m_device->Release(m_group);
        m_device->Release(m_layout);
    }
    m_dataInterfaces.reset();
    m_residentState.reset();
    m_device = nullptr;
    m_capacity = 0;
    m_stateStride = 0;
    m_layout = {};
    m_group = {};
    m_pipelines.fill({});
}

bool ParticleGpuRuntime::IsValid() const noexcept
{
    if (!m_device || !m_residentState || m_capacity == 0 || !m_group.IsValid())
        return false;
    if (m_dataInterfaces && (!m_dataInterfaces->layout.IsValid() || !m_dataInterfaces->group.IsValid() ||
                             !m_dataInterfaces->metadataBuffer.IsValid()))
        return false;
    for (auto pipeline : m_pipelines) {
        if (!pipeline.IsValid())
            return false;
    }
    return true;
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
    return UpdatePointCacheMetadata(transforms);
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
                                    uint32_t simulationStep, float deltaTime) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = spawnCount;
    constants.spawnBaseId = spawnBaseId;
    constants.spawnGeneration = spawnGeneration;
    constants.systemSeed = systemSeed;
    constants.simulationStep = simulationStep;
    constants.deltaTime = deltaTime;
    Record(encoder, GpuKernelStage::Init, constants, spawnCount);
}

void ParticleGpuRuntime::RecordUpdate(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed,
                                      uint32_t simulationStep, float deltaTime) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = m_capacity;
    constants.systemSeed = systemSeed;
    constants.simulationStep = simulationStep;
    constants.deltaTime = deltaTime;
    Record(encoder, GpuKernelStage::Update, constants, m_capacity);
}

void ParticleGpuRuntime::RecordRenderReset(const rhi::ComputeCommandEncoder &encoder) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = 1;
    Record(encoder, GpuKernelStage::RenderReset, constants, 1);
}

void ParticleGpuRuntime::RecordRendering(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed,
                                         uint32_t simulationStep) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = m_capacity;
    constants.systemSeed = systemSeed;
    constants.simulationStep = simulationStep;
    Record(encoder, GpuKernelStage::Rendering, constants, m_capacity);
}

void ParticleGpuRuntime::Record(const rhi::ComputeCommandEncoder &encoder, GpuKernelStage stage,
                                const GpuParticlePushConstants &constants, uint32_t invocationCount) const
{
    if (!IsValid() || !encoder.IsValid() || invocationCount == 0)
        return;
    const auto pipeline = m_pipelines[StageIndex(stage)];
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, m_group);
    if (m_dataInterfaces)
        encoder.BindGroup(pipeline, 1, m_dataInterfaces->group);
    encoder.PushConstants(pipeline, sizeof(constants), &constants);
    encoder.Dispatch(GroupCount(invocationCount), 1, 1);
}

uint32_t ParticleGpuRuntime::GroupCount(uint32_t invocationCount) noexcept
{
    return invocationCount == 0 ? 0 : 1 + (invocationCount - 1) / WorkgroupSize;
}

} // namespace infernux::particle
