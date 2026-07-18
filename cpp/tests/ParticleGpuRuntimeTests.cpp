#include <function/renderer/particle/ParticleGpuRuntime.h>

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
    uint32_t layoutCreates = 0;
    uint32_t groupCreates = 0;
    uint32_t pipelineCreates = 0;
    uint32_t bufferReleases = 0;
    uint32_t shaderReleases = 0;
    uint32_t layoutReleases = 0;
    uint32_t groupReleases = 0;
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
        assert(desc.entryCount == 6);
        ++layoutCreates;
        return {nextIndex++, 1};
    }

    rhi::BindGroupHandle CreateBindGroup(const rhi::BindGroupDesc &desc) override
    {
        assert(desc.layout.IsValid() && desc.bufferCount == 6);
        ++groupCreates;
        return {nextIndex++, 1};
    }

    rhi::ComputePipelineHandle CreateComputePipeline(const rhi::ComputePipelineDesc &desc) override
    {
        assert(desc.computeShader.IsValid() && desc.bindingLayoutCount == 1 && desc.pushConstantBytes == 32);
        ++pipelineCreates;
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

    runtime.Destroy();
    assert(!runtime.IsValid() && runtime.StateStride() == 0);
    assert(device.pipelineReleases == 5 && device.groupReleases == 1 && device.layoutReleases == 1);
    assert(device.bufferReleases == 6);
    return 0;
}
