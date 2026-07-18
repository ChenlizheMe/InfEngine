#include "ParticleGpuRuntime.h"

#include <limits>

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

} // namespace

ParticleGpuRuntime::~ParticleGpuRuntime()
{
    Destroy();
}

bool ParticleGpuRuntime::Create(rhi::Device &device, const GpuEmitterDesc &desc)
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
    const auto storage = rhi::BufferUsageFlags::Storage;
    m_states = device.CreateBuffer({stateBytes, storage});
    m_freeList = device.CreateBuffer({static_cast<uint64_t>(desc.capacity) * sizeof(uint32_t), storage});
    m_counters = device.CreateBuffer({16, storage});
    m_instances = device.CreateBuffer({instanceBytes, storage});
    m_indirect = device.CreateBuffer({16, storage | rhi::BufferUsageFlags::Indirect});
    rhi::BufferDesc transformDesc;
    transformDesc.byteSize = sizeof(GpuParticleTransforms);
    transformDesc.usage = rhi::BufferUsageFlags::Uniform;
    transformDesc.memory = rhi::BufferMemory::Upload;
    m_transforms = device.CreateBuffer(transformDesc);
    if (!m_states.IsValid() || !m_freeList.IsValid() || !m_counters.IsValid() || !m_instances.IsValid() ||
        !m_indirect.IsValid() || !m_transforms.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindingLayoutDesc layoutDesc;
    for (uint32_t binding = 0; binding < 5; ++binding)
        layoutDesc.entries[binding] = {binding, rhi::BindingType::StorageBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entries[5] = {5, rhi::BindingType::UniformBuffer, rhi::ShaderStage::Compute, 1};
    layoutDesc.entryCount = 6;
    m_layout = device.CreateBindingLayout(layoutDesc);
    if (!m_layout.IsValid()) {
        Destroy();
        return false;
    }

    rhi::BindGroupDesc groupDesc;
    groupDesc.layout = m_layout;
    const std::array<rhi::BufferHandle, 6> buffers = {m_states,    m_freeList, m_counters,
                                                      m_instances, m_indirect, m_transforms};
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
        m_device->Release(m_transforms);
        m_device->Release(m_indirect);
        m_device->Release(m_instances);
        m_device->Release(m_counters);
        m_device->Release(m_freeList);
        m_device->Release(m_states);
    }
    m_device = nullptr;
    m_capacity = 0;
    m_states = {};
    m_freeList = {};
    m_counters = {};
    m_instances = {};
    m_indirect = {};
    m_transforms = {};
    m_layout = {};
    m_group = {};
    m_pipelines.fill({});
}

bool ParticleGpuRuntime::IsValid() const noexcept
{
    if (!m_device || m_capacity == 0 || !m_group.IsValid())
        return false;
    for (auto pipeline : m_pipelines) {
        if (!pipeline.IsValid())
            return false;
    }
    return true;
}

bool ParticleGpuRuntime::UpdateTransforms(const GpuParticleTransforms &transforms)
{
    return m_device && m_device->WriteBuffer(m_transforms, 0, &transforms, sizeof(transforms));
}

void ParticleGpuRuntime::RecordBootstrap(const rhi::ComputeCommandEncoder &encoder, uint32_t systemSeed) const
{
    GpuParticlePushConstants constants;
    constants.capacity = m_capacity;
    constants.invocationCount = m_capacity;
    constants.systemSeed = systemSeed;
    Record(encoder, GpuKernelStage::Bootstrap, constants, m_capacity);
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
    encoder.PushConstants(pipeline, sizeof(constants), &constants);
    encoder.Dispatch(GroupCount(invocationCount), 1, 1);
}

uint32_t ParticleGpuRuntime::GroupCount(uint32_t invocationCount) noexcept
{
    return invocationCount == 0 ? 0 : (invocationCount + WorkgroupSize - 1) / WorkgroupSize;
}

} // namespace infernux::particle
