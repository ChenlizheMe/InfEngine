#include <function/renderer/particle/ParticleGpuBillboardRenderer.h>
#include <function/renderer/particle/ParticleGpuDrawRegistry.h>
#include <function/renderer/particle/ParticleGpuRuntime.h>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <array>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>

namespace
{

using namespace infernux;

struct FakeDevice final : rhi::Device
{
    std::vector<rhi::BufferDesc> buffers;
    uint32_t shaderCreates = 0;
    std::vector<uint32_t> layoutEntryCounts;
    std::vector<uint32_t> groupBufferCounts;
    std::vector<rhi::GraphicsPipelineDesc> graphicsPipelineDescs;
    uint32_t layoutCreates = 0;
    uint32_t groupCreates = 0;
    uint32_t graphicsPipelineCreates = 0;
    uint32_t pipelineCreates = 0;
    uint32_t bufferReleases = 0;
    uint32_t shaderReleases = 0;
    uint32_t layoutReleases = 0;
    uint32_t groupReleases = 0;
    uint32_t graphicsPipelineReleases = 0;
    uint32_t pipelineReleases = 0;
    uint32_t writes = 0;
    uint32_t nextIndex = 1;

    rhi::BufferHandle CreateBuffer(const rhi::BufferDesc &desc) override
    {
        buffers.push_back(desc);
        return {nextIndex++, 1};
    }

    rhi::ShaderModuleHandle CreateShaderModule(const rhi::ShaderModuleDesc &desc) override
    {
        assert(desc.spirv && desc.wordCount > 0);
        ++shaderCreates;
        return {nextIndex++, 1};
    }

    rhi::BindingLayoutHandle CreateBindingLayout(const rhi::BindingLayoutDesc &desc) override
    {
        layoutEntryCounts.push_back(desc.entryCount);
        ++layoutCreates;
        return {nextIndex++, 1};
    }

    rhi::BindGroupHandle CreateBindGroup(const rhi::BindGroupDesc &desc) override
    {
        assert(desc.layout.IsValid());
        groupBufferCounts.push_back(desc.bufferCount);
        ++groupCreates;
        return {nextIndex++, 1};
    }

    rhi::ComputePipelineHandle CreateComputePipeline(const rhi::ComputePipelineDesc &desc) override
    {
        assert(desc.computeShader.IsValid() && desc.bindingLayoutCount == 1 && desc.pushConstantBytes == 32);
        ++pipelineCreates;
        return {nextIndex++, 1};
    }

    rhi::GraphicsPipelineHandle CreateGraphicsPipeline(const rhi::GraphicsPipelineDesc &desc) override
    {
        graphicsPipelineDescs.push_back(desc);
        ++graphicsPipelineCreates;
        return {nextIndex++, 1};
    }

    bool WriteBuffer(rhi::BufferHandle handle, uint64_t offset, const void *data, uint64_t byteSize) override
    {
        assert(handle.IsValid() && offset == 0 && data && byteSize == sizeof(particle::GpuParticleTransforms));
        ++writes;
        return true;
    }

    void Release(rhi::BufferHandle handle) noexcept override
    {
        bufferReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::ShaderModuleHandle handle) noexcept override
    {
        shaderReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::BindingLayoutHandle handle) noexcept override
    {
        layoutReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::BindGroupHandle handle) noexcept override
    {
        groupReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::GraphicsPipelineHandle handle) noexcept override
    {
        graphicsPipelineReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::ComputePipelineHandle handle) noexcept override
    {
        pipelineReleases += handle.IsValid() ? 1u : 0u;
    }
};

struct CommandTrace
{
    std::vector<rhi::ComputePipelineHandle> pipelines;
    std::vector<rhi::BindGroupHandle> groups;
    std::vector<particle::GpuParticlePushConstants> constants;
    std::vector<uint32_t> dispatches;

    static void BindPipeline(void *context, rhi::ComputePipelineHandle pipeline)
    {
        static_cast<CommandTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *context, rhi::ComputePipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex == 0);
        static_cast<CommandTrace *>(context)->groups.push_back(group);
    }
    static void PushConstants(void *context, rhi::ComputePipelineHandle, uint32_t byteSize, const void *data)
    {
        assert(byteSize == sizeof(particle::GpuParticlePushConstants));
        particle::GpuParticlePushConstants value;
        std::memcpy(&value, data, sizeof(value));
        static_cast<CommandTrace *>(context)->constants.push_back(value);
    }
    static void Dispatch(void *context, uint32_t x, uint32_t y, uint32_t z)
    {
        assert(y == 1 && z == 1);
        static_cast<CommandTrace *>(context)->dispatches.push_back(x);
    }
    static void DispatchIndirect(void *, rhi::BufferHandle, uint64_t)
    {
    }
};

struct GraphicsTrace
{
    std::vector<rhi::GraphicsPipelineHandle> pipelines;
    std::vector<rhi::BindGroupHandle> groups;
    std::vector<particle::GpuBillboardViewConstants> constants;
    std::vector<rhi::BufferHandle> indirectBuffers;

    static void BindPipeline(void *context, rhi::GraphicsPipelineHandle pipeline)
    {
        static_cast<GraphicsTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *context, rhi::GraphicsPipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex == 0);
        static_cast<GraphicsTrace *>(context)->groups.push_back(group);
    }
    static void PushConstants(void *context, rhi::GraphicsPipelineHandle, rhi::ShaderStage stages, uint32_t byteSize,
                              const void *data)
    {
        assert(stages == rhi::ShaderStage::Vertex && byteSize == sizeof(particle::GpuBillboardViewConstants));
        particle::GpuBillboardViewConstants value;
        std::memcpy(&value, data, sizeof(value));
        static_cast<GraphicsTrace *>(context)->constants.push_back(value);
    }
    static void Draw(void *, uint32_t, uint32_t, uint32_t, uint32_t)
    {
    }
    static void DrawIndirect(void *context, rhi::BufferHandle buffer, uint64_t offset, uint32_t drawCount,
                             uint32_t stride)
    {
        assert(offset == 0 && drawCount == 1 && stride == 16);
        static_cast<GraphicsTrace *>(context)->indirectBuffers.push_back(buffer);
    }
};

} // namespace

int main()
{
    FakeDevice device;
    std::array<std::array<uint32_t, 4>, static_cast<size_t>(particle::GpuKernelStage::Count)> words{};
    particle::GpuEmitterDesc desc;
    desc.capacity = 1000;
    desc.stateStride = 64;
    for (size_t index = 0; index < words.size(); ++index) {
        words[index][0] = 0x07230203;
        desc.kernels[index] = {words[index].data(), words[index].size()};
    }

    particle::ParticleGpuRuntime runtime;
    assert(runtime.Create(device, desc));
    assert(runtime.IsValid() && runtime.Capacity() == 1000 && runtime.StateStride() == 64);
    assert(device.buffers.size() == 6);
    assert(device.buffers[0].byteSize == 64000);
    assert(device.buffers[3].byteSize == 48000);
    assert(rhi::HasBufferUsage(device.buffers[4].usage, rhi::BufferUsageFlags::Indirect));
    assert(device.buffers[5].memory == rhi::BufferMemory::Upload);
    assert(device.shaderCreates == 5 && device.shaderReleases == 5);
    assert(device.layoutCreates == 1 && device.groupCreates == 1 && device.pipelineCreates == 5);

    particle::GpuParticleTransforms transforms;
    assert(runtime.UpdateTransforms(transforms));
    assert(device.writes == 1);

    CommandTrace trace;
    const rhi::ComputeCommandEncoder::DispatchTable dispatch = {&CommandTrace::BindPipeline, &CommandTrace::BindGroup,
                                                                &CommandTrace::PushConstants, &CommandTrace::Dispatch,
                                                                &CommandTrace::DispatchIndirect};
    const rhi::ComputeCommandEncoder encoder(&trace, &dispatch);
    runtime.RecordBootstrap(encoder, 7);
    runtime.RecordInit(encoder, 300, 100, 2, 7, 9, 1.0f / 60.0f);
    runtime.RecordUpdate(encoder, 7, 9, 1.0f / 60.0f);
    runtime.RecordRenderReset(encoder);
    runtime.RecordRendering(encoder, 7, 9);
    assert(trace.pipelines.size() == 5 && trace.groups.size() == 5 && trace.constants.size() == 5);
    assert(trace.dispatches == std::vector<uint32_t>({4, 2, 4, 1, 4}));
    assert(trace.constants[1].spawnBaseId == 100 && trace.constants[1].spawnGeneration == 2);
    assert(trace.constants[2].simulationStep == 9);

    const auto instanceBuffer = runtime.InstanceBuffer();
    const auto indirectBuffer = runtime.IndirectBuffer();
    std::array<uint32_t, 4> billboardVertex = {0x07230203};
    std::array<uint32_t, 4> billboardFragment = {0x07230203};
    particle::GpuBillboardRendererDesc billboardDesc;
    billboardDesc.vertexShader = {billboardVertex.data(), billboardVertex.size()};
    billboardDesc.fragmentShader = {billboardFragment.data(), billboardFragment.size()};
    billboardDesc.instances = instanceBuffer;
    billboardDesc.fallbackMaterial.renderQueue = 3100;
    billboardDesc.material = std::make_shared<InxMaterial>("live-particle-material");
    auto liveMaterialState = billboardDesc.material->GetRenderState();
    liveMaterialState.renderQueue = 3100;
    liveMaterialState.blendEnable = true;
    liveMaterialState.depthWriteEnable = false;
    billboardDesc.material->SetRenderState(liveMaterialState);

    particle::ParticleGpuBillboardRenderer billboard;
    assert(billboard.Create(device, billboardDesc));
    assert(billboard.IsValid() && billboard.RenderQueue() == 3100 && billboard.InstanceBuffer() == instanceBuffer);
    assert(device.layoutEntryCounts == std::vector<uint32_t>({6, 1}));
    assert(device.groupBufferCounts == std::vector<uint32_t>({6, 1}));

    MaterialPassPipelineDescriptor forwardPass;
    forwardPass.colorFormats = {rhi::PixelFormat::RGBA16SFloat};
    forwardPass.depthFormat = rhi::PixelFormat::D32SFloat;
    forwardPass.samples = rhi::SampleCount::Four;
    GraphicsTrace graphicsTrace;
    const rhi::GraphicsCommandEncoder::Dispatch graphicsDispatch = {
        &GraphicsTrace::BindPipeline, &GraphicsTrace::BindGroup, &GraphicsTrace::PushConstants, &GraphicsTrace::Draw,
        &GraphicsTrace::DrawIndirect};
    const rhi::GraphicsCommandEncoder graphicsEncoder(&graphicsTrace, &graphicsDispatch);
    particle::GpuBillboardViewConstants view;
    view.cameraRight[0] = 1.0f;
    view.cameraUp[1] = 1.0f;
    const rhi::RenderTargetLayoutHandle firstTarget{100, 1};
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(device.graphicsPipelineCreates == 1 && device.graphicsPipelineDescs.size() == 1);
    const auto &graphicsDesc = device.graphicsPipelineDescs.front();
    assert(graphicsDesc.pushConstantBytes == sizeof(view));
    assert(graphicsDesc.samples == rhi::SampleCount::Four);
    assert(graphicsDesc.depth.testEnabled && !graphicsDesc.depth.writeEnabled);
    assert(graphicsDesc.colorTargetCount == 1 && graphicsDesc.colorTargets[0].blendEnabled);
    assert(graphicsTrace.pipelines.size() == 2 && graphicsTrace.groups.size() == 2 &&
           graphicsTrace.constants.size() == 2 && graphicsTrace.indirectBuffers.size() == 2);

    billboardDesc.material->SetRenderQueue(3150);
    assert(billboard.RenderQueue() == 3150);
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(device.graphicsPipelineCreates == 1 && device.graphicsPipelineReleases == 0);
    liveMaterialState = billboardDesc.material->GetRenderState();
    liveMaterialState.blendEnable = false;
    liveMaterialState.depthWriteEnable = true;
    billboardDesc.material->SetRenderState(liveMaterialState);
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(device.graphicsPipelineCreates == 2 && device.graphicsPipelineReleases == 0);
    const auto &updatedGraphicsDesc = device.graphicsPipelineDescs.back();
    assert(!updatedGraphicsDesc.colorTargets[0].blendEnabled && updatedGraphicsDesc.depth.writeEnabled);
    liveMaterialState.blendEnable = true;
    liveMaterialState.depthWriteEnable = false;
    billboardDesc.material->SetRenderState(liveMaterialState);
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(device.graphicsPipelineCreates == 2 && device.graphicsPipelineReleases == 0);

    MaterialPassPipelineDescriptor unsupportedPass = forwardPass;
    unsupportedPass.target = ShaderCompileTarget::GBuffer;
    assert(!billboard.RecordDraw(graphicsEncoder, firstTarget, unsupportedPass, indirectBuffer, view));
    billboard.Destroy();
    assert(!billboard.IsValid() && device.graphicsPipelineReleases == 2);

    auto registeredBillboard = std::make_shared<particle::ParticleGpuBillboardRenderer>();
    assert(registeredBillboard->Create(device, billboardDesc));
    particle::ParticleGpuDrawRegistry registry;
    const uint64_t initialRevision = registry.Revision();
    assert(registry.Set({77, runtime.Capacity(), instanceBuffer, indirectBuffer, registeredBillboard}));
    assert(registry.Revision() == initialRevision + 1 && registry.Size() == 1);
    const auto visibleEntries = registry.Snapshot(3000, 3200);
    assert(visibleEntries.size() == 1 && visibleEntries[0].id == 77);
    assert(registry.Snapshot(0, 2999).empty());
    assert(registry.Remove(77) && !registry.Remove(77) && registry.Size() == 0);
    registeredBillboard.reset();

    runtime.Destroy();
    assert(!runtime.IsValid() && runtime.StateStride() == 0);
    assert(device.pipelineReleases == 5 && device.groupReleases == 3 && device.layoutReleases == 3);
    assert(device.shaderReleases == 9);
    assert(device.bufferReleases == 6);
    return 0;
}
