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
    BufferHandle copySourceBuffer;
    BufferHandle copyDestinationBuffer;
    BufferCopyRegion bufferCopy;
    TextureHandle copySourceTexture;
    TextureHandle copyDestinationTexture;
    TextureCopyRegion textureCopy;
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

void CopyBuffer(void *context, BufferHandle source, BufferHandle destination, const BufferCopyRegion &region)
{
    auto &recorded = *static_cast<RecordedCommands *>(context);
    recorded.copySourceBuffer = source;
    recorded.copyDestinationBuffer = destination;
    recorded.bufferCopy = region;
}

void CopyTexture(void *context, TextureHandle source, TextureHandle destination, const TextureCopyRegion &region)
{
    auto &recorded = *static_cast<RecordedCommands *>(context);
    recorded.copySourceTexture = source;
    recorded.copyDestinationTexture = destination;
    recorded.textureCopy = region;
}

} // namespace

int main()
{
    constexpr auto shaderStages = PipelineStage::VertexShader | PipelineStage::FragmentShader;
    static_assert(HasAny(shaderStages, PipelineStage::VertexShader));
    static_assert(HasAny(shaderStages, PipelineStage::FragmentShader));
    static_assert(!HasAny(shaderStages, PipelineStage::ComputeShader));

    constexpr auto storageAccess = Access::ShaderRead | Access::ShaderWrite;
    static_assert(HasAny(storageAccess, Access::ShaderRead));
    static_assert(HasAny(storageAccess, Access::ShaderWrite));
    static_assert(!HasAny(storageAccess, Access::TransferWrite));

    constexpr auto textureUsage = TextureUsageFlags::Sampled | TextureUsageFlags::TransferDestination;
    static_assert(HasTextureUsage(textureUsage, TextureUsageFlags::Sampled));
    static_assert(HasTextureUsage(textureUsage, TextureUsageFlags::TransferDestination));
    static_assert(!HasTextureUsage(textureUsage, TextureUsageFlags::Storage));

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

    const BufferHandle copySourceBuffer{11, 1};
    const BufferHandle copyDestinationBuffer{12, 1};
    const TextureHandle copySourceTexture{13, 1};
    const TextureHandle copyDestinationTexture{14, 1};
    const TransferCommandEncoder::DispatchTable transferDispatch{CopyBuffer, CopyTexture};
    const TransferCommandEncoder transferEncoder(&recorded, &transferDispatch);
    transferEncoder.CopyBuffer(copySourceBuffer, copyDestinationBuffer, {16, 32, 128});
    transferEncoder.CopyTexture(copySourceTexture, copyDestinationTexture,
                                {TextureAspect::Depth, 1, 2, 3, 4, 64, 32, 1});
    assert(recorded.copySourceBuffer == copySourceBuffer);
    assert(recorded.copyDestinationBuffer == copyDestinationBuffer);
    assert(recorded.bufferCopy.sourceOffset == 16);
    assert(recorded.bufferCopy.destinationOffset == 32);
    assert(recorded.bufferCopy.byteSize == 128);
    assert(recorded.copySourceTexture == copySourceTexture);
    assert(recorded.copyDestinationTexture == copyDestinationTexture);
    assert(recorded.textureCopy.aspect == TextureAspect::Depth);
    assert(recorded.textureCopy.sourceMip == 1);
    assert(recorded.textureCopy.destinationLayer == 4);
    assert(recorded.textureCopy.width == 64);
    assert(recorded.textureCopy.height == 32);

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

    TextureDesc textureDesc;
    textureDesc.dimension = TextureDimension::Texture3D;
    textureDesc.width = 32;
    textureDesc.height = 16;
    textureDesc.depthOrLayers = 8;
    textureDesc.mipLevels = 4;
    textureDesc.format = PixelFormat::RGBA16SFloat;
    textureDesc.usage = textureUsage;
    assert(textureDesc.dimension == TextureDimension::Texture3D);
    assert(textureDesc.depthOrLayers == 8);

    TextureViewDesc viewDesc;
    viewDesc.texture = {19, 1};
    viewDesc.dimension = TextureViewDimension::Texture3D;
    viewDesc.mipCount = 4;
    assert(viewDesc.texture.IsValid());

    SamplerDesc samplerDesc;
    samplerDesc.addressW = AddressMode::ClampToEdge;
    samplerDesc.maxLod = 3.0f;
    samplerDesc.maxAnisotropy = 4.0f;
    assert(samplerDesc.maxLod == 3.0f);

    BindGroupDesc groupDesc;
    groupDesc.layout = {15, 1};
    groupDesc.buffers[0] = {0, BindingType::StorageBuffer, {16, 1}, 0, 256};
    groupDesc.bufferCount = 1;
    groupDesc.textures[0] = {1, BindingType::CombinedTextureSampler, {17, 1}, {18, 1}, false};
    groupDesc.textureCount = 1;
    assert(groupDesc.textures[0].texture.IsValid());
    assert(groupDesc.textures[0].sampler.IsValid());
    return 0;
}
