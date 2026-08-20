#include <function/renderer/rhi/RenderSubmissionPlan.h>
#include <function/renderer/rhi/RhiCommand.h>
#include <function/renderer/vk/RhiVulkanTypes.h>

#include <cassert>
#include <cstdint>
#include <string>
#include <vector>

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
    TextureHandle resolveSourceTexture;
    TextureHandle resolveDestinationTexture;
    TextureResolveRegion textureResolve;
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

void ResolveTexture(void *context, TextureHandle source, TextureHandle destination, const TextureResolveRegion &region)
{
    auto &recorded = *static_cast<RecordedCommands *>(context);
    recorded.resolveSourceTexture = source;
    recorded.resolveDestinationTexture = destination;
    recorded.textureResolve = region;
}

} // namespace

int main()
{
    static_assert(infernux::rhi::ToVkImageAspectMask(VK_FORMAT_R8G8B8A8_UNORM) == VK_IMAGE_ASPECT_COLOR_BIT);
    static_assert(infernux::rhi::ToVkImageAspectMask(VK_FORMAT_D32_SFLOAT) == VK_IMAGE_ASPECT_DEPTH_BIT);
    static_assert(infernux::rhi::ToVkImageAspectMask(VK_FORMAT_S8_UINT) == VK_IMAGE_ASPECT_STENCIL_BIT);
    static_assert(infernux::rhi::ToVkImageAspectMask(VK_FORMAT_D24_UNORM_S8_UINT) ==
                  (VK_IMAGE_ASPECT_DEPTH_BIT | VK_IMAGE_ASPECT_STENCIL_BIT));
    static_assert(!infernux::rhi::SelectDynamicRenderingPath(false, true, true));
    static_assert(!infernux::rhi::SelectDynamicRenderingPath(true, false, false));
    static_assert(infernux::rhi::SelectDynamicRenderingPath(true, true, false));
    static_assert(infernux::rhi::SelectDynamicRenderingPath(true, false, true));

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
    const TransferCommandEncoder::DispatchTable transferDispatch{CopyBuffer, CopyTexture, ResolveTexture};
    const TransferCommandEncoder transferEncoder(&recorded, &transferDispatch);
    transferEncoder.CopyBuffer(copySourceBuffer, copyDestinationBuffer, {16, 32, 128});
    transferEncoder.CopyTexture(copySourceTexture, copyDestinationTexture,
                                {TextureAspect::Depth, 1, 2, 3, 4, 64, 32, 1});
    transferEncoder.ResolveTexture(copySourceTexture, copyDestinationTexture,
                                   {TextureAspect::Color, 0, 1, 0, 2, 128, 64, 1});
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
    assert(recorded.resolveSourceTexture == copySourceTexture);
    assert(recorded.resolveDestinationTexture == copyDestinationTexture);
    assert(recorded.textureResolve.sourceLayer == 1);
    assert(recorded.textureResolve.destinationLayer == 2);
    assert(recorded.textureResolve.width == 128);
    assert(recorded.textureResolve.height == 64);

    GraphicsPipelineDesc desc;
    desc.vertexShader = {1, 1};
    desc.fragmentShader = {2, 1};
    desc.renderTargetLayout = {3, 1};
    desc.samples = SampleCount::Four;
    desc.colorTargets[0].format = PixelFormat::RGBA16SFloat;
    desc.colorTargets[0].premultipliedAlpha = true;
    desc.colorTargetCount = 1;
    desc.pushConstantStages = ShaderStage::Fragment;
    desc.pushConstantBytes = sizeof(constants);
    assert(desc.vertexShader.IsValid());
    assert(desc.HasValidRenderingContract());
    assert(desc.colorTargets[0].format == PixelFormat::RGBA16SFloat);
    assert(desc.colorTargets[0].premultipliedAlpha);

    GraphicsPipelineDesc contaminatedLegacyDesc = desc;
    contaminatedLegacyDesc.renderingSignature.colorFormats[0] = PixelFormat::RGBA16SFloat;
    contaminatedLegacyDesc.renderingSignature.colorFormatCount = 1;
    assert(!contaminatedLegacyDesc.HasValidRenderingContract());

    GraphicsPipelineDesc dynamicDesc = desc;
    dynamicDesc.useDynamicRendering = true;
    dynamicDesc.renderTargetLayout = {};
    dynamicDesc.renderingSignature.colorFormats[0] = PixelFormat::RGBA16SFloat;
    dynamicDesc.renderingSignature.colorFormatCount = 1;
    dynamicDesc.renderingSignature.depthFormat = PixelFormat::D32SFloat;
    dynamicDesc.renderingSignature.samples = SampleCount::Four;
    dynamicDesc.depth.testEnabled = true;
    assert(dynamicDesc.HasValidRenderingContract());
    dynamicDesc.renderingSignature.samples = SampleCount::One;
    assert(!dynamicDesc.HasValidRenderingContract());
    dynamicDesc.renderingSignature.samples = SampleCount::Four;
    dynamicDesc.renderTargetLayout = {3, 1};
    assert(!dynamicDesc.HasValidRenderingContract());

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
    assert(!IsIntegerFormat(textureDesc.format));
    assert(IsSrgbFormat(PixelFormat::RGBA8Srgb));
    assert(LinearColorFormat(PixelFormat::RGBA8Srgb) == PixelFormat::RGBA8UNorm);
    assert(AreColorSpaceViewFormatsCompatible(PixelFormat::RGBA8Srgb, PixelFormat::RGBA8UNorm));
    assert(!AreColorSpaceViewFormatsCompatible(PixelFormat::RGBA8Srgb, PixelFormat::BGRA8UNorm));
    assert(IsIntegerFormat(PixelFormat::RG32UInt));
    assert(!IsIntegerFormat(PixelFormat::R32SFloat));

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

    const DeviceId planDevice = 3;
    const RenderViewId planView = 9;
    const std::vector<SubmissionWorkItem> workItems = {
        {0, planDevice, QueueRole::Graphics, SubmissionDomain::Frame, planView, PipelineStage::ColorOutput, {}},
        {1, planDevice, QueueRole::Compute, SubmissionDomain::Frame, planView, PipelineStage::ComputeShader, {0}},
        {2, planDevice, QueueRole::Compute, SubmissionDomain::Frame, planView, PipelineStage::ComputeShader, {1}},
        {3, planDevice, QueueRole::Graphics, SubmissionDomain::Frame, planView, PipelineStage::FragmentShader, {2}},
        {4, planDevice, QueueRole::Transfer, SubmissionDomain::Background, planView, PipelineStage::Transfer, {0}},
    };
    SubmissionPlan plan;
    std::string planError;
    assert(BuildSubmissionPlan(workItems, plan, planError));
    assert(plan.batches.size() == 4);
    assert((plan.batches[0].workItems == std::vector<uint32_t>{0}));
    assert((plan.batches[1].workItems == std::vector<uint32_t>{1, 2}));
    assert(plan.batches[1].waitsFor.size() == 1 && plan.batches[1].waitsFor[0].sourceBatch == 0);
    assert(plan.batches[2].queuePredecessor == 0);
    assert(plan.batches[2].waitsFor.size() == 1 && plan.batches[2].waitsFor[0].sourceBatch == 1);
    assert(plan.batches[3].waitsFor.size() == 1 && plan.batches[3].waitsFor[0].sourceBatch == 0);

    auto forcedBoundary = workItems;
    forcedBoundary[2].forceBatchBoundary = true;
    assert(BuildSubmissionPlan(forcedBoundary, plan, planError));
    assert(plan.batches.size() == 5);
    assert((plan.batches[1].workItems == std::vector<uint32_t>{1}));
    assert((plan.batches[2].workItems == std::vector<uint32_t>{2}));
    assert(plan.batches[2].queuePredecessor == 1);

    auto invalidOrder = workItems;
    invalidOrder[0].dependencies = {4};
    assert(!BuildSubmissionPlan(invalidOrder, plan, planError));
    assert(!planError.empty());

    auto crossDevice = workItems;
    crossDevice[1].device = 4;
    assert(!BuildSubmissionPlan(crossDevice, plan, planError));
    assert(planError.find("crosses devices") != std::string::npos);

    SubmissionPlan nestedPlan;
    const std::vector<SubmissionWorkItem> nestedWork = {
        {30, planDevice, QueueRole::Compute, SubmissionDomain::Frame, planView, PipelineStage::ComputeShader, {}},
        {31, planDevice, QueueRole::Graphics, SubmissionDomain::Frame, planView, PipelineStage::VertexShader, {30}},
        {32, planDevice, QueueRole::Graphics, SubmissionDomain::Frame, planView, PipelineStage::FragmentShader, {31}},
    };
    assert(BuildSubmissionPlan(nestedWork, nestedPlan, planError));

    SubmissionPlanComposer composer;
    const uint32_t setup = composer.AddWork(planDevice, QueueRole::Graphics, SubmissionDomain::Frame,
                                            InvalidRenderViewId, PipelineStage::AllGraphics);
    const auto imported = composer.Append(nestedPlan, {setup});
    assert(imported.workItems.size() == nestedPlan.batches.size());
    assert(imported.roots.size() == 1);
    assert(imported.terminals.size() == 1);
    assert(composer.Build(plan, planError));
    assert(plan.batches.size() == 1 + nestedPlan.batches.size());
    assert(plan.batches[1].queue == QueueRole::Compute);
    assert(!plan.batches[1].waitsFor.empty());
    assert(plan.batches[1].waitsFor.front().sourceBatch == 0);
    assert(plan.batches[2].queue == QueueRole::Graphics);
    assert(!plan.batches[2].waitsFor.empty());
    assert(plan.batches[2].waitsFor.front().sourceBatch == 1);

    const std::vector<SubmissionWorkItem> parallelWork = {
        {100, planDevice, QueueRole::Graphics, SubmissionDomain::Frame, planView, PipelineStage::AllGraphics, {}},
        {101, planDevice, QueueRole::Compute, SubmissionDomain::Frame, planView, PipelineStage::ComputeShader, {100}},
        {102, planDevice, QueueRole::Graphics, SubmissionDomain::Frame, planView, PipelineStage::AllGraphics, {101}},
        {103,
         planDevice,
         QueueRole::Compute,
         SubmissionDomain::Frame,
         InvalidRenderViewId,
         PipelineStage::ComputeShader,
         {}},
    };
    assert(BuildSubmissionPlan(parallelWork, plan, planError));
    const SubmissionPlanStatistics statistics = AnalyzeSubmissionPlan(plan);
    assert(statistics.batchCount == 4);
    assert(statistics.graphicsBatchCount == 2);
    assert(statistics.computeBatchCount == 2);
    assert(statistics.crossQueueDependencyCount == 2);
    // Logical Compute work is serialized on its own queue. The final Compute
    // batch can overlap only the final Graphics batch after their shared
    // predecessor completes.
    assert(statistics.unorderedComputeGraphicsPairCount == 1);
    return 0;
}
