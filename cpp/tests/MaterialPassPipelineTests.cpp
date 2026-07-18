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
        {PixelFormat::RGBA8UNorm, PixelFormat::RGBA16SFloat, PixelFormat::RGBA8UNorm},
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
    descriptors.insert(forward);
    assert(descriptors.size() == 5);

    auto differentSamples = forward;
    differentSamples.samples = SampleCount::One;
    assert(differentSamples != forward);
    assert(MaterialPassPipelineDescriptorHash{}(differentSamples) != MaterialPassPipelineDescriptorHash{}(forward));
    return 0;
}
