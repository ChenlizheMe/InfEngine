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

} // namespace

int main()
{
    const GraphicsPipelineHandle pipeline{4, 2};
    const BindGroupHandle group{7, 3};
    assert(pipeline.IsValid());
    assert(group.IsValid());
    assert(GraphicsPipelineHandle{} != pipeline);

    RecordedCommands recorded;
    const GraphicsCommandEncoder::Dispatch dispatch{BindPipeline, BindGroup, PushConstants, Draw};
    const GraphicsCommandEncoder encoder(&recorded, &dispatch);
    const float constants[4] = {};

    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 2, group);
    encoder.PushConstants(pipeline, ShaderStage::Fragment, sizeof(constants), constants);
    encoder.Draw(3);

    assert(recorded.pipeline == pipeline);
    assert(recorded.group == group);
    assert(recorded.setIndex == 2);
    assert(recorded.pushStages == ShaderStage::Fragment);
    assert(recorded.pushSize == sizeof(constants));
    assert(recorded.drawVertices == 3);

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
    return 0;
}
