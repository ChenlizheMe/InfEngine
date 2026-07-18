#include "ParticleRenderGraph.h"

#include <cmath>

namespace infernux::particle
{

namespace
{

constexpr uint64_t CounterBufferBytes = 16;
constexpr uint64_t IndirectBufferBytes = 16;

std::string StageName(const std::string &prefix, const char *stage)
{
    return prefix + "/" + stage;
}

} // namespace

bool ParticleRenderGraph::Attach(vk::RenderGraph &graph, ParticleGpuRuntime &runtime, const std::string &namePrefix)
{
    if (IsAttached() || !runtime.IsValid() || namePrefix.empty() || runtime.StateStride() == 0)
        return false;

    m_runtime = &runtime;
    vk::ResourceHandle states;
    vk::ResourceHandle freeList;
    vk::ResourceHandle counters;
    vk::ResourceHandle instances;
    vk::ResourceHandle indirect;
    vk::ResourceHandle transforms;

    graph.AddComputePass(StageName(namePrefix, "Bootstrap"), [&](vk::PassBuilder &builder) {
        const uint64_t capacity = runtime.Capacity();
        states = builder.ImportBuffer(StageName(namePrefix, "States"), runtime.StateBuffer(),
                                      capacity * runtime.StateStride());
        freeList = builder.ImportBuffer(StageName(namePrefix, "FreeList"), runtime.FreeListBuffer(),
                                        capacity * sizeof(uint32_t));
        counters = builder.ImportBuffer(StageName(namePrefix, "Counters"), runtime.CounterBuffer(), CounterBufferBytes);
        instances = builder.ImportBuffer(StageName(namePrefix, "Instances"), runtime.InstanceBuffer(),
                                         capacity * ParticleGpuRuntime::RenderInstanceStride);
        indirect =
            builder.ImportBuffer(StageName(namePrefix, "Indirect"), runtime.IndirectBuffer(), IndirectBufferBytes);
        transforms = builder.ImportBuffer(StageName(namePrefix, "Transforms"), runtime.TransformBuffer(),
                                          sizeof(GpuParticleTransforms));
        if (!states.IsValid() || !freeList.IsValid() || !counters.IsValid() || !instances.IsValid() ||
            !indirect.IsValid() || !transforms.IsValid())
            return vk::PassExecuteCallback{};

        states = builder.ReadWrite(states, rhi::PipelineStage::ComputeShader);
        freeList = builder.ReadWrite(freeList, rhi::PipelineStage::ComputeShader);
        counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
        indirect = builder.ReadWrite(indirect, rhi::PipelineStage::ComputeShader);
        return vk::PassExecuteCallback{[this](vk::RenderContext &context) {
            if (!m_framePending || !m_bootstrapPending || !m_runtime)
                return;
            m_runtime->RecordBootstrap(context.GetComputeCommandEncoder(), m_request.systemSeed);
            m_bootstrapPending = false;
        }};
    });

    if (!states.IsValid() || !freeList.IsValid() || !counters.IsValid() || !instances.IsValid() ||
        !indirect.IsValid() || !transforms.IsValid()) {
        m_runtime = nullptr;
        return false;
    }

    graph.AddComputePass(StageName(namePrefix, "Init"), [&](vk::PassBuilder &builder) {
        states = builder.ReadWrite(states, rhi::PipelineStage::ComputeShader);
        freeList = builder.ReadWrite(freeList, rhi::PipelineStage::ComputeShader);
        counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
        builder.ReadUniformBuffer(transforms);
        return [this](vk::RenderContext &context) {
            if (!m_framePending || !m_request.simulate || m_request.spawnCount == 0 || !m_runtime)
                return;
            m_runtime->RecordInit(context.GetComputeCommandEncoder(), m_request.spawnCount, m_request.spawnBaseId,
                                  m_request.spawnGeneration, m_request.systemSeed, m_request.simulationStep,
                                  m_request.deltaTime);
        };
    });

    graph.AddComputePass(StageName(namePrefix, "Update"), [&](vk::PassBuilder &builder) {
        states = builder.ReadWrite(states, rhi::PipelineStage::ComputeShader);
        freeList = builder.ReadWrite(freeList, rhi::PipelineStage::ComputeShader);
        counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
        builder.ReadUniformBuffer(transforms);
        return [this](vk::RenderContext &context) {
            if (!m_framePending || !m_request.simulate || !m_runtime)
                return;
            m_runtime->RecordUpdate(context.GetComputeCommandEncoder(), m_request.systemSeed, m_request.simulationStep,
                                    m_request.deltaTime);
        };
    });

    graph.AddComputePass(StageName(namePrefix, "RenderReset"), [&](vk::PassBuilder &builder) {
        counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
        indirect = builder.ReadWrite(indirect, rhi::PipelineStage::ComputeShader);
        return [this](vk::RenderContext &context) {
            if (!m_framePending || !m_request.render || !m_runtime)
                return;
            m_runtime->RecordRenderReset(context.GetComputeCommandEncoder());
        };
    });

    graph.AddComputePass(StageName(namePrefix, "Rendering"), [&](vk::PassBuilder &builder) {
        states = builder.ReadWrite(states, rhi::PipelineStage::ComputeShader);
        freeList = builder.ReadWrite(freeList, rhi::PipelineStage::ComputeShader);
        counters = builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
        instances = builder.ReadWrite(instances, rhi::PipelineStage::ComputeShader);
        indirect = builder.ReadWrite(indirect, rhi::PipelineStage::ComputeShader);
        builder.ReadUniformBuffer(transforms);
        return [this](vk::RenderContext &context) {
            if (!m_framePending)
                return;
            if (m_request.render && m_runtime) {
                m_runtime->RecordRendering(context.GetComputeCommandEncoder(), m_request.systemSeed,
                                           m_request.simulationStep);
            }
            m_lastConsumedFrame = m_request.frameIndex;
            m_hasConsumedFrame = true;
            m_framePending = false;
        };
    });

    m_outputs = {instances, indirect};
    return m_outputs.IsValid();
}

bool ParticleRenderGraph::BeginFrame(const GpuParticleFrameRequest &request) noexcept
{
    if (!IsAttached() || !m_runtime->IsValid() || !std::isfinite(request.deltaTime) || request.deltaTime < 0.0f ||
        (m_framePending && request.frameIndex == m_request.frameIndex) ||
        (m_hasConsumedFrame && request.frameIndex == m_lastConsumedFrame))
        return false;
    m_request = request;
    m_framePending = true;
    return true;
}

} // namespace infernux::particle
