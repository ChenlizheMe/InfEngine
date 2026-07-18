#include <function/renderer/rhi/RhiCommand.h>

#include <cassert>
#include <cstdint>

using namespace infernux::rhi;

namespace
{

struct RecordedCommands
{
    GraphicsPipelineHandle pipeline;
    BindGroupHandle group;
    uint32_t setIndex = 0;
    ShaderStage pushStages = ShaderStage::None;
    uint32_t pushSize = 0;
    uint32_t drawVertices = 0;
    BufferHandle indirectBuffer;
    uint64_t indirectOffset = 0;
    uint32_t indirectCount = 0;
    ComputePipelineHandle computePipeline;
    uint32_t dispatchX = 0;
    uint32_t dispatchY = 0;
    uint32_t dispatchZ = 0;
};

void BindPipeline(void *context, GraphicsPipelineHandle pipeline)
{
    static_cast<RecordedCommands *>(context)->pipeline = pipeline;
}

void BindGroup(void *context, GraphicsPipelineHandle, uint32_t setIndex, BindGroupHandle group)
{
    auto &recorded = *static_cast<RecordedCommands *>(context);
    recorded.setIndex = setIndex;
    recorded.group = group;
}

void PushConstants(void *context, GraphicsPipelineHandle, ShaderStage stages, uint32_t byteSize, const void *)
{
    auto &recorded = *static_cast<RecordedCommands *>(context);
    recorded.pushStages = stages;
    recorded.pushSize = byteSize;
}

void Draw(void *context, uint32_t vertexCount, uint32_t, uint32_t, uint32_t)
{
    static_cast<RecordedCommands *>(context)->drawVertices = vertexCount;
}

void DrawIndirect(void *context, BufferHandle buffer, uint64_t offset, uint32_t drawCount, uint32_t)
{
    auto &recorded = *static_cast<RecordedCommands *>(context);
    recorded.indirectBuffer = buffer;
    recorded.indirectOffset = offset;
    recorded.indirectCount = drawCount;
}

void BindComputePipeline(void *context, ComputePipelineHandle pipeline)
{
    static_cast<RecordedCommands *>(context)->computePipeline = pipeline;
}

void BindComputeGroup(void *, ComputePipelineHandle, uint32_t, BindGroupHandle)
{
}

void PushComputeConstants(void *, ComputePipelineHandle, uint32_t, const void *)
{
}

void DispatchCompute(void *context, uint32_t x, uint32_t y, uint32_t z)
{
    auto &recorded = *static_cast<RecordedCommands *>(context);
    recorded.dispatchX = x;
    recorded.dispatchY = y;
    recorded.dispatchZ = z;
}

void DispatchComputeIndirect(void *, BufferHandle, uint64_t)
{
}

} // namespace

int main()
{
    const GraphicsPipelineHandle pipeline{4, 2};
    const ComputePipelineHandle computePipeline{5, 2};
    const BindGroupHandle group{7, 3};
    const BufferHandle indirectBuffer{8, 4};
    assert(pipeline.IsValid());
    assert(group.IsValid());
    assert(GraphicsPipelineHandle{} != pipeline);

    RecordedCommands recorded;
    const GraphicsCommandEncoder::Dispatch dispatch{BindPipeline, BindGroup, PushConstants, Draw, DrawIndirect};
    const GraphicsCommandEncoder encoder(&recorded, &dispatch);
    const float constants[4] = {};

    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 2, group);
    encoder.PushConstants(pipeline, ShaderStage::Fragment, sizeof(constants), constants);
    encoder.Draw(3);
    encoder.DrawIndirect(indirectBuffer, 32, 2);

    assert(recorded.pipeline == pipeline);
    assert(recorded.group == group);
    assert(recorded.setIndex == 2);
    assert(recorded.pushStages == ShaderStage::Fragment);
    assert(recorded.pushSize == sizeof(constants));
    assert(recorded.drawVertices == 3);
    assert(recorded.indirectBuffer == indirectBuffer);
    assert(recorded.indirectOffset == 32);
    assert(recorded.indirectCount == 2);

    const ComputeCommandEncoder::DispatchTable computeDispatch{
        BindComputePipeline, BindComputeGroup, PushComputeConstants, DispatchCompute, DispatchComputeIndirect};
    const ComputeCommandEncoder computeEncoder(&recorded, &computeDispatch);
    computeEncoder.BindPipeline(computePipeline);
    computeEncoder.Dispatch(4, 2, 1);
    assert(recorded.computePipeline == computePipeline);
    assert(recorded.dispatchX == 4);
    assert(recorded.dispatchY == 2);
    assert(recorded.dispatchZ == 1);

    GraphicsPipelineDesc desc;
    desc.vertexShader = {1, 1};
    desc.fragmentShader = {2, 1};
    desc.renderTargetLayout = {3, 1};
    desc.samples = SampleCount::Four;
    desc.colorTargets[0].format = PixelFormat::RGBA16SFloat;
    desc.colorTargetCount = 1;
    desc.pushConstantStages = ShaderStage::Fragment;
    desc.pushConstantBytes = sizeof(constants);
    assert(desc.vertexShader.IsValid());
    assert(desc.colorTargets[0].format == PixelFormat::RGBA16SFloat);

    ComputePipelineDesc computeDesc;
    computeDesc.computeShader = {9, 1};
    computeDesc.bindingLayouts[0] = {10, 1};
    computeDesc.bindingLayoutCount = 1;
    assert(computeDesc.computeShader.IsValid());
    return 0;
}
