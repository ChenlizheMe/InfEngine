#pragma once

#include "rhi/RhiCapabilities.h"

#include <cstdint>

namespace infernux
{

enum class MsaaRequestStatus : uint8_t
{
    NoRequest = 0,
    Accepted,
    InvalidSceneRequest,
    InvalidGameRequest,
    ConflictingRequests,
};

struct MsaaRequestResolution
{
    MsaaRequestStatus status = MsaaRequestStatus::NoRequest;
    int samples = 0;

    [[nodiscard]] constexpr bool IsAccepted() const noexcept
    {
        return status == MsaaRequestStatus::Accepted;
    }
};

[[nodiscard]] constexpr bool IsValidMsaaSampleCount(int samples) noexcept
{
    return samples == 1 || samples == 2 || samples == 4 || samples == 8;
}

[[nodiscard]] constexpr rhi::SampleCount ToRhiSampleCount(int samples) noexcept
{
    switch (samples) {
    case 2:
        return rhi::SampleCount::Two;
    case 4:
        return rhi::SampleCount::Four;
    case 8:
        return rhi::SampleCount::Eight;
    case 1:
    default:
        return rhi::SampleCount::One;
    }
}

[[nodiscard]] constexpr MsaaRequestResolution ResolveMsaaRequests(int sceneSamples, int gameSamples) noexcept
{
    if (sceneSamples != 0 && !IsValidMsaaSampleCount(sceneSamples))
        return {MsaaRequestStatus::InvalidSceneRequest, 0};
    if (gameSamples != 0 && !IsValidMsaaSampleCount(gameSamples))
        return {MsaaRequestStatus::InvalidGameRequest, 0};
    if (sceneSamples == 0 && gameSamples == 0)
        return {};
    if (sceneSamples != 0 && gameSamples != 0 && sceneSamples != gameSamples)
        return {MsaaRequestStatus::ConflictingRequests, 0};
    return {MsaaRequestStatus::Accepted, gameSamples != 0 ? gameSamples : sceneSamples};
}

[[nodiscard]] constexpr rhi::SampleCountMask AllMsaaSampleCounts() noexcept
{
    return rhi::SampleCountBit(rhi::SampleCount::One) | rhi::SampleCountBit(rhi::SampleCount::Two) |
           rhi::SampleCountBit(rhi::SampleCount::Four) | rhi::SampleCountBit(rhi::SampleCount::Eight);
}

[[nodiscard]] inline rhi::SampleCountMask GetAttachmentSampleCountMask(const rhi::DeviceCapabilities &capabilities,
                                                                       rhi::PixelFormat colorFormat,
                                                                       rhi::PixelFormat depthFormat) noexcept
{
    const auto *color = capabilities.FindFormat(colorFormat);
    const auto *depth = capabilities.FindFormat(depthFormat);
    if (!color || !depth)
        return 0;
    return color->sampleCounts & depth->sampleCounts & AllMsaaSampleCounts();
}

[[nodiscard]] constexpr bool SupportsMsaaSampleCount(rhi::SampleCountMask mask, int samples) noexcept
{
    return IsValidMsaaSampleCount(samples) && (mask & rhi::SampleCountBit(ToRhiSampleCount(samples))) != 0;
}

[[nodiscard]] constexpr rhi::SampleCountMask GetSceneTargetSampleCountMask(rhi::SampleCountMask multisampledColorMask,
                                                                           rhi::SampleCountMask resolveColorMask,
                                                                           rhi::SampleCountMask depthMask) noexcept
{
    if (!SupportsMsaaSampleCount(resolveColorMask, 1))
        return 0;
    return multisampledColorMask & depthMask & AllMsaaSampleCounts();
}

[[nodiscard]] constexpr int SelectSupportedMsaaAtOrBelow(rhi::SampleCountMask mask, int preferred) noexcept
{
    if (preferred >= 8 && SupportsMsaaSampleCount(mask, 8))
        return 8;
    if (preferred >= 4 && SupportsMsaaSampleCount(mask, 4))
        return 4;
    if (preferred >= 2 && SupportsMsaaSampleCount(mask, 2))
        return 2;
    if (preferred >= 1 && SupportsMsaaSampleCount(mask, 1))
        return 1;
    return 0;
}

} // namespace infernux
