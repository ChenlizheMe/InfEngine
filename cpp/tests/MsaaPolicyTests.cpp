#include <function/renderer/MsaaPolicy.h>

#include <cassert>

using namespace infernux;

int main()
{
    for (const int samples : {1, 2, 4, 8}) {
        assert(IsValidMsaaSampleCount(samples));
        const auto same = ResolveMsaaRequests(samples, samples);
        assert(same.IsAccepted());
        assert(same.samples == samples);

        const auto sceneOnly = ResolveMsaaRequests(samples, 0);
        assert(sceneOnly.IsAccepted());
        assert(sceneOnly.samples == samples);

        const auto gameOnly = ResolveMsaaRequests(0, samples);
        assert(gameOnly.IsAccepted());
        assert(gameOnly.samples == samples);
    }

    assert(!IsValidMsaaSampleCount(0));
    assert(!IsValidMsaaSampleCount(3));
    assert(ResolveMsaaRequests(0, 0).status == MsaaRequestStatus::NoRequest);
    assert(ResolveMsaaRequests(3, 4).status == MsaaRequestStatus::InvalidSceneRequest);
    assert(ResolveMsaaRequests(4, 3).status == MsaaRequestStatus::InvalidGameRequest);

    const auto conflict = ResolveMsaaRequests(4, 8);
    assert(conflict.status == MsaaRequestStatus::ConflictingRequests);
    assert(!conflict.IsAccepted());
    assert(conflict.samples == 0);

    rhi::DeviceCapabilities capabilities;
    auto &color = capabilities.formats[static_cast<size_t>(rhi::PixelFormat::RGBA16SFloat)];
    color.format = rhi::PixelFormat::RGBA16SFloat;
    color.sampleCounts = AllMsaaSampleCounts();
    auto &depth = capabilities.formats[static_cast<size_t>(rhi::PixelFormat::D32SFloat)];
    depth.format = rhi::PixelFormat::D32SFloat;
    depth.sampleCounts = rhi::SampleCountBit(rhi::SampleCount::One) | rhi::SampleCountBit(rhi::SampleCount::Two) |
                         rhi::SampleCountBit(rhi::SampleCount::Four);

    const auto common =
        GetAttachmentSampleCountMask(capabilities, rhi::PixelFormat::RGBA16SFloat, rhi::PixelFormat::D32SFloat);
    assert(SupportsMsaaSampleCount(common, 1));
    assert(SupportsMsaaSampleCount(common, 2));
    assert(SupportsMsaaSampleCount(common, 4));
    assert(!SupportsMsaaSampleCount(common, 8));
    assert(GetSceneTargetSampleCountMask(AllMsaaSampleCounts(), common, common) == common);
    assert(GetSceneTargetSampleCountMask(AllMsaaSampleCounts(), 0, common) == 0);
    assert(SelectSupportedMsaaAtOrBelow(common, 8) == 4);
    assert(SelectSupportedMsaaAtOrBelow(common, 2) == 2);
    assert(GetAttachmentSampleCountMask(capabilities, rhi::PixelFormat::RGBA16SFloat, rhi::PixelFormat::Undefined) ==
           0);
    return 0;
}
