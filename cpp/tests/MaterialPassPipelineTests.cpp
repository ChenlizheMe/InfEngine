#include <function/renderer/MaterialPassPipeline.h>

#include <cassert>
#include <unordered_set>

using infernux::MaterialPassPipelineDescriptor;
using infernux::MaterialPassPipelineDescriptorHash;
using infernux::MaterialPassRenderingMode;
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

    auto dynamicForward = forward;
    dynamicForward.renderingMode = MaterialPassRenderingMode::DynamicRendering;
    assert(dynamicForward.IsValid());
    assert(dynamicForward != forward);
    assert(MaterialPassPipelineDescriptorHash{}(dynamicForward) != MaterialPassPipelineDescriptorHash{}(forward));
    const auto forwardSignature = dynamicForward.RenderingSignature();
    assert(forwardSignature.IsValid());
    assert(forwardSignature.colorFormatCount == 1);
    assert(forwardSignature.colorFormats[0] == PixelFormat::RGBA16SFloat);
    assert(forwardSignature.depthFormat == PixelFormat::D32SFloat);
    assert(forwardSignature.samples == SampleCount::Four);
    assert(dynamicForward.MatchesRenderingSignature(forwardSignature));
    auto mismatchedForwardSignature = forwardSignature;
    mismatchedForwardSignature.samples = SampleCount::One;
    assert(!dynamicForward.MatchesRenderingSignature(mismatchedForwardSignature));

    auto dynamicGBuffer = gbuffer;
    dynamicGBuffer.renderingMode = MaterialPassRenderingMode::DynamicRendering;
    assert(dynamicGBuffer.IsValid());
    assert(dynamicGBuffer != gbuffer);
    assert(MaterialPassPipelineDescriptorHash{}(dynamicGBuffer) != MaterialPassPipelineDescriptorHash{}(gbuffer));
    const auto gbufferSignature = dynamicGBuffer.RenderingSignature();
    assert(gbufferSignature.IsValid());
    assert(gbufferSignature.colorFormatCount == gbuffer.colorFormats.size());
    assert(gbufferSignature.colorFormats[4] == PixelFormat::RG32UInt);
    assert(gbufferSignature.samples == SampleCount::One);

    auto dynamicShadow = depth;
    dynamicShadow.target = ShaderCompileTarget::Shadow;
    dynamicShadow.renderingMode = MaterialPassRenderingMode::DynamicRendering;
    assert(dynamicShadow.IsValid());
    assert(dynamicShadow.RenderingSignature().colorFormatCount == 0);
    assert(dynamicShadow.RenderingSignature().depthFormat == PixelFormat::D32SFloat);

    auto dynamicPicking = picking;
    dynamicPicking.renderingMode = MaterialPassRenderingMode::DynamicRendering;
    assert(dynamicPicking.IsValid());
    assert(dynamicPicking.RenderingSignature().colorFormats[0] == PixelFormat::RG32UInt);
    assert(dynamicPicking.RenderingSignature().depthFormat == PixelFormat::D32SFloat);

    auto dynamicMotion = motion;
    dynamicMotion.renderingMode = MaterialPassRenderingMode::DynamicRendering;
    dynamicMotion.depthReadOnly = true;
    assert(dynamicMotion.IsValid());
    assert(dynamicMotion.RenderingSignature().colorFormats[0] == PixelFormat::RG16SFloat);
    assert(dynamicMotion.RenderingSignature().depthFormat == PixelFormat::D32SFloat);

    infernux::rhi::GraphicsPipelineDesc dynamicPipeline;
    dynamicPipeline.samples = dynamicForward.samples;
    dynamicPipeline.colorTargets[0].format = dynamicForward.colorFormats[0];
    dynamicPipeline.colorTargetCount = 1;
    dynamicPipeline.depth.testEnabled = true;
    dynamicForward.ApplyRenderingContract(dynamicPipeline, {77, 1});
    assert(dynamicPipeline.useDynamicRendering);
    assert(!dynamicPipeline.renderTargetLayout.IsValid());
    assert(dynamicPipeline.HasValidRenderingContract());

    infernux::rhi::GraphicsPipelineDesc legacyPipeline = dynamicPipeline;
    legacyPipeline.useDynamicRendering = false;
    forward.ApplyRenderingContract(legacyPipeline, {77, 1});
    assert(!legacyPipeline.useDynamicRendering);
    assert(legacyPipeline.renderTargetLayout.IsValid());
    assert(legacyPipeline.renderingSignature.IsEmpty());
    assert(legacyPipeline.HasValidRenderingContract());
    legacyPipeline.renderingSignature = forwardSignature;
    assert(!legacyPipeline.HasValidRenderingContract());

    infernux::rhi::GraphicsPipelineDesc dynamicGBufferPipeline;
    dynamicGBufferPipeline.samples = dynamicGBuffer.samples;
    dynamicGBufferPipeline.colorTargetCount = static_cast<uint32_t>(dynamicGBuffer.colorFormats.size());
    for (size_t index = 0; index < dynamicGBuffer.colorFormats.size(); ++index)
        dynamicGBufferPipeline.colorTargets[index].format = dynamicGBuffer.colorFormats[index];
    dynamicGBufferPipeline.depth.testEnabled = true;
    dynamicGBuffer.ApplyRenderingContract(dynamicGBufferPipeline, {99, 1});
    assert(dynamicGBufferPipeline.HasValidRenderingContract());

    auto depthStencil = dynamicForward;
    depthStencil.depthFormat = PixelFormat::D24UNormS8UInt;
    const auto depthStencilSignature = depthStencil.RenderingSignature();
    assert(depthStencilSignature.depthFormat == PixelFormat::D24UNormS8UInt);
    assert(depthStencilSignature.stencilFormat == PixelFormat::D24UNormS8UInt);
    return 0;
}
