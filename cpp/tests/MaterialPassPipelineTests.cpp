#include <function/renderer/MaterialPassPipeline.h>

#include <cassert>
#include <unordered_set>

using infernux::MaterialPassPipelineDescriptor;
using infernux::MaterialPassPipelineDescriptorHash;
using infernux::ShaderCompileTarget;
using infernux::rhi::PixelFormat;
using infernux::rhi::SampleCount;

int main()
{
    const MaterialPassPipelineDescriptor forward{
        ShaderCompileTarget::Forward,
        {PixelFormat::RGBA16SFloat},
        PixelFormat::D32SFloat,
        SampleCount::Four,
    };
    assert(forward.IsValid());

    const MaterialPassPipelineDescriptor gbuffer{
        ShaderCompileTarget::GBuffer,
        {PixelFormat::RGBA16SFloat, PixelFormat::RGBA16SFloat, PixelFormat::RGBA8UNorm, PixelFormat::RGBA16SFloat,
         PixelFormat::RG32UInt},
        PixelFormat::D32SFloat,
        SampleCount::One,
    };
    assert(gbuffer.IsValid());

    const MaterialPassPipelineDescriptor depth{
        ShaderCompileTarget::Depth,
        {},
        PixelFormat::D32SFloat,
        SampleCount::One,
    };
    assert(depth.IsValid());

    const MaterialPassPipelineDescriptor picking{
        ShaderCompileTarget::Picking,
        {PixelFormat::RG32UInt},
        PixelFormat::D32SFloat,
        SampleCount::One,
    };
    assert(picking.IsValid());

    auto invalidPicking = picking;
    invalidPicking.colorFormats = {PixelFormat::RGBA8UNorm};
    assert(!invalidPicking.IsValid());

    const MaterialPassPipelineDescriptor motion{
        ShaderCompileTarget::Motion,
        {PixelFormat::RG16SFloat},
        PixelFormat::D32SFloat,
        SampleCount::One,
    };
    assert(motion.IsValid());

    const MaterialPassPipelineDescriptor normal{
        ShaderCompileTarget::Normal, {PixelFormat::RGBA16SFloat}, PixelFormat::D32SFloat, SampleCount::One, true,
    };
    assert(normal.IsValid());

    const MaterialPassPipelineDescriptor baseColor{
        ShaderCompileTarget::BaseColor, {PixelFormat::RGBA16SFloat}, PixelFormat::D32SFloat, SampleCount::One, true,
    };
    assert(baseColor.IsValid());
    auto invalidBaseColor = baseColor;
    invalidBaseColor.colorFormats = {PixelFormat::RGBA8UNorm};
    assert(!invalidBaseColor.IsValid());

    auto invalidDepth = depth;
    invalidDepth.colorFormats = {PixelFormat::RGBA8UNorm};
    assert(!invalidDepth.IsValid());

    auto invalidColor = forward;
    invalidColor.colorFormats = {PixelFormat::D32SFloat};
    assert(!invalidColor.IsValid());

    std::unordered_set<MaterialPassPipelineDescriptor, MaterialPassPipelineDescriptorHash> descriptors;
    descriptors.insert(forward);
    descriptors.insert(gbuffer);
    descriptors.insert(depth);
    descriptors.insert(picking);
    descriptors.insert(motion);
    descriptors.insert(normal);
    descriptors.insert(baseColor);
    descriptors.insert(forward);
    assert(descriptors.size() == 7);

    auto differentSamples = forward;
    differentSamples.samples = SampleCount::One;
    assert(differentSamples != forward);
    assert(MaterialPassPipelineDescriptorHash{}(differentSamples) != MaterialPassPipelineDescriptorHash{}(forward));

    auto readOnlyDepth = forward;
    readOnlyDepth.depthReadOnly = true;
    assert(readOnlyDepth.IsValid());
    assert(readOnlyDepth != forward);
    assert(MaterialPassPipelineDescriptorHash{}(readOnlyDepth) != MaterialPassPipelineDescriptorHash{}(forward));

    auto invalidReadOnlyDepth = forward;
    invalidReadOnlyDepth.depthFormat = PixelFormat::Undefined;
    invalidReadOnlyDepth.depthReadOnly = true;
    assert(!invalidReadOnlyDepth.IsValid());

    const auto forwardSignature = forward.RenderingSignature();
    assert(forwardSignature.IsValid());
    assert(forwardSignature.colorFormatCount == 1);
    assert(forwardSignature.colorFormats[0] == PixelFormat::RGBA16SFloat);
    assert(forwardSignature.depthFormat == PixelFormat::D32SFloat);
    assert(forwardSignature.samples == SampleCount::Four);
    assert(forward.MatchesRenderingSignature(forwardSignature));
    auto mismatchedForwardSignature = forwardSignature;
    mismatchedForwardSignature.samples = SampleCount::One;
    assert(!forward.MatchesRenderingSignature(mismatchedForwardSignature));

    const auto gbufferSignature = gbuffer.RenderingSignature();
    assert(gbufferSignature.IsValid());
    assert(gbufferSignature.colorFormatCount == gbuffer.colorFormats.size());
    assert(gbufferSignature.colorFormats[4] == PixelFormat::RG32UInt);
    assert(gbufferSignature.samples == SampleCount::One);

    auto shadow = depth;
    shadow.target = ShaderCompileTarget::Shadow;
    assert(shadow.IsValid());
    assert(shadow.RenderingSignature().colorFormatCount == 0);
    assert(shadow.RenderingSignature().depthFormat == PixelFormat::D32SFloat);

    assert(picking.RenderingSignature().colorFormats[0] == PixelFormat::RG32UInt);
    assert(picking.RenderingSignature().depthFormat == PixelFormat::D32SFloat);

    auto readOnlyMotion = motion;
    readOnlyMotion.depthReadOnly = true;
    assert(readOnlyMotion.IsValid());
    assert(readOnlyMotion.RenderingSignature().colorFormats[0] == PixelFormat::RG16SFloat);
    assert(readOnlyMotion.RenderingSignature().depthFormat == PixelFormat::D32SFloat);

    infernux::rhi::GraphicsPipelineDesc pipeline;
    pipeline.samples = forward.samples;
    pipeline.colorTargets[0].format = forward.colorFormats[0];
    pipeline.colorTargetCount = 1;
    pipeline.depth.testEnabled = true;
    forward.ApplyRenderingContract(pipeline);
    assert(pipeline.useDynamicRendering);
    assert(!pipeline.renderTargetLayout.IsValid());
    assert(pipeline.HasValidRenderingContract());

    infernux::rhi::GraphicsPipelineDesc gbufferPipeline;
    gbufferPipeline.samples = gbuffer.samples;
    gbufferPipeline.colorTargetCount = static_cast<uint32_t>(gbuffer.colorFormats.size());
    for (size_t index = 0; index < gbuffer.colorFormats.size(); ++index)
        gbufferPipeline.colorTargets[index].format = gbuffer.colorFormats[index];
    gbufferPipeline.depth.testEnabled = true;
    gbuffer.ApplyRenderingContract(gbufferPipeline);
    assert(gbufferPipeline.HasValidRenderingContract());

    auto depthStencil = forward;
    depthStencil.depthFormat = PixelFormat::D24UNormS8UInt;
    const auto depthStencilSignature = depthStencil.RenderingSignature();
    assert(depthStencilSignature.depthFormat == PixelFormat::D24UNormS8UInt);
    assert(depthStencilSignature.stencilFormat == PixelFormat::D24UNormS8UInt);
    return 0;
}
