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
    }
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

bool ParticleGpuRuntime::UpdateTransforms(const GpuParticleTransforms &transforms)
{
    return m_device && m_device->WriteBuffer(TransformBuffer(), 0, &transforms, sizeof(transforms));
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
    encoder.PushConstants(pipeline, sizeof(constants), &constants);
    encoder.Dispatch(GroupCount(invocationCount), 1, 1);
}

uint32_t ParticleGpuRuntime::GroupCount(uint32_t invocationCount) noexcept
{
    return invocationCount == 0 ? 0 : 1 + (invocationCount - 1) / WorkgroupSize;
}

} // namespace infernux::particle
