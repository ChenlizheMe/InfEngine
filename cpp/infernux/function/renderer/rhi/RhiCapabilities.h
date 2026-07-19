#pragma once

#include "RhiQuery.h"
#include "RhiTypes.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <string_view>

namespace infernux::rhi
{

enum class BackendType : uint8_t
{
    Unknown = 0,
    Vulkan,
    WebGPU,
};

enum class AdapterType : uint8_t
{
    Unknown = 0,
    Integrated,
    Discrete,
    Virtual,
    Cpu,
};

enum class FormatFeature : uint16_t
{
    None = 0,
    Sampled = 1u << 0,
    FilterLinear = 1u << 1,
    Storage = 1u << 2,
    ColorAttachment = 1u << 3,
    DepthStencilAttachment = 1u << 4,
    TransferSource = 1u << 5,
    TransferDestination = 1u << 6,
    BlitSource = 1u << 7,
    BlitDestination = 1u << 8,
};

[[nodiscard]] constexpr FormatFeature operator|(FormatFeature lhs, FormatFeature rhs) noexcept
{
    return static_cast<FormatFeature>(static_cast<uint16_t>(lhs) | static_cast<uint16_t>(rhs));
}

constexpr FormatFeature &operator|=(FormatFeature &lhs, FormatFeature rhs) noexcept
{
    lhs = lhs | rhs;
    return lhs;
}

[[nodiscard]] constexpr bool HasAllFormatFeatures(FormatFeature available, FormatFeature required) noexcept
{
    return (static_cast<uint16_t>(available) & static_cast<uint16_t>(required)) == static_cast<uint16_t>(required);
}

using SampleCountMask = uint8_t;

[[nodiscard]] constexpr SampleCountMask SampleCountBit(SampleCount samples) noexcept
{
    switch (samples) {
    case SampleCount::One:
        return 1u << 0;
    case SampleCount::Two:
        return 1u << 1;
    case SampleCount::Four:
        return 1u << 2;
    case SampleCount::Eight:
        return 1u << 3;
    }
    return 0;
}

enum class CapabilityDiagnosticCode : uint8_t
{
    None = 0,
    InvalidFormat,
    UnsupportedFormatFeatures,
    UnsupportedSampleCount,
};

struct CapabilityCheck
{
    CapabilityDiagnosticCode code = CapabilityDiagnosticCode::None;
    uint64_t required = 0;
    uint64_t available = 0;

    [[nodiscard]] constexpr bool IsSupported() const noexcept
    {
        return code == CapabilityDiagnosticCode::None;
    }
};

struct FormatCapabilities
{
    PixelFormat format = PixelFormat::Undefined;
    FormatFeature optimalTiling = FormatFeature::None;
    SampleCountMask sampleCounts = 0;
};

struct DeviceLimits
{
    uint32_t maxTextureDimension1D = 0;
    uint32_t maxTextureDimension2D = 0;
    uint32_t maxTextureDimension3D = 0;
    uint32_t maxTextureArrayLayers = 0;
    uint32_t maxColorAttachments = 0;
    uint32_t maxPushConstantBytes = 0;
    uint32_t maxSampledTexturesPerStage = 0;
    uint32_t maxStorageBuffersPerStage = 0;
    float maxSamplerAnisotropy = 1.0f;
    uint32_t maxComputeWorkgroupCount[3] = {};
    uint32_t maxComputeWorkgroupSize[3] = {};
    uint32_t maxComputeWorkgroupInvocations = 0;
};

struct DeviceFeatures
{
    bool samplerAnisotropy = false;
    bool fillModeNonSolid = false;
    bool wideLines = false;
    bool descriptorIndexing = false;
    bool timelineSemaphore = false;
    bool dedicatedTransferQueue = false;
};

struct DeviceCapabilities
{
    static constexpr size_t AdapterNameCapacity = 128;

    BackendType backend = BackendType::Unknown;
    AdapterType adapterType = AdapterType::Unknown;
    std::array<char, AdapterNameCapacity> adapterName{};
    uint32_t vendorId = 0;
    uint32_t deviceId = 0;
    uint32_t driverVersion = 0;
    uint32_t apiVersionMajor = 0;
    uint32_t apiVersionMinor = 0;
    uint32_t apiVersionPatch = 0;
    DeviceLimits limits;
    DeviceFeatures features;
    TimestampQueryCapabilities timestampQueries;
    std::array<FormatCapabilities, kPixelFormatCount> formats{};

    void SetAdapterName(std::string_view value) noexcept
    {
        adapterName.fill('\0');
        const size_t count = std::min(value.size(), adapterName.size() - 1);
        std::copy_n(value.data(), count, adapterName.data());
    }

    [[nodiscard]] std::string_view AdapterName() const noexcept
    {
        const auto end = std::find(adapterName.begin(), adapterName.end(), '\0');
        return {adapterName.data(), static_cast<size_t>(end - adapterName.begin())};
    }

    [[nodiscard]] const FormatCapabilities *FindFormat(PixelFormat format) const noexcept
    {
        if (!IsValidPixelFormat(format)) {
            return nullptr;
        }
        const size_t index = static_cast<size_t>(format);
        return index < formats.size() ? &formats[index] : nullptr;
    }

    [[nodiscard]] CapabilityCheck CheckFormat(PixelFormat format, FormatFeature required) const noexcept
    {
        const auto *capability = FindFormat(format);
        if (!capability) {
            return {CapabilityDiagnosticCode::InvalidFormat, static_cast<uint64_t>(required), 0};
        }
        if (!HasAllFormatFeatures(capability->optimalTiling, required)) {
            return {CapabilityDiagnosticCode::UnsupportedFormatFeatures, static_cast<uint64_t>(required),
                    static_cast<uint64_t>(capability->optimalTiling)};
        }
        return {};
    }

    [[nodiscard]] CapabilityCheck CheckSampleCount(PixelFormat format, SampleCount samples) const noexcept
    {
        const auto *capability = FindFormat(format);
        const SampleCountMask required = SampleCountBit(samples);
        if (!capability) {
            return {CapabilityDiagnosticCode::InvalidFormat, required, 0};
        }
        if ((capability->sampleCounts & required) == 0) {
            return {CapabilityDiagnosticCode::UnsupportedSampleCount, required, capability->sampleCounts};
        }
        return {};
    }
};

} // namespace infernux::rhi
