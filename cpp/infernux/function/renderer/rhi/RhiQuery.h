#pragma once

#include <algorithm>
#include <array>
#include <cstdint>
#include <limits>
#include <string_view>

namespace infernux::rhi
{

constexpr uint32_t kGpuTimestampNameCapacity = 64;
constexpr uint32_t kGpuTimestampMaxRegions = 64;

[[nodiscard]] constexpr uint64_t TimestampTickDelta(uint64_t begin, uint64_t end, uint32_t validBits) noexcept
{
    if (validBits == 0) {
        return 0;
    }
    if (validBits >= 64) {
        return end - begin;
    }
    return (end - begin) & ((uint64_t{1} << validBits) - 1);
}

struct TimestampRegionHandle
{
    static constexpr uint16_t Invalid = std::numeric_limits<uint16_t>::max();

    uint16_t index = Invalid;

    [[nodiscard]] constexpr bool IsValid() const noexcept
    {
        return index != Invalid;
    }
};

struct TimestampQueryCapabilities
{
    bool supported = false;
    bool graphicsAndCompute = false;
    uint32_t validBits = 0;
    double nanosecondsPerTick = 0.0;
    uint32_t maxRegionsPerFrame = 0;
};

struct GpuTimestampSample
{
    std::array<char, kGpuTimestampNameCapacity> name{};
    double milliseconds = 0.0;

    void SetName(std::string_view value) noexcept
    {
        name.fill('\0');
        const size_t count = std::min(value.size(), name.size() - 1);
        std::copy_n(value.data(), count, name.data());
    }

    [[nodiscard]] std::string_view Name() const noexcept
    {
        const auto end = std::find(name.begin(), name.end(), '\0');
        return {name.data(), static_cast<size_t>(end - name.begin())};
    }
};

struct GpuTimestampFrame
{
    uint64_t serial = 0;
    uint32_t sampleCount = 0;
    bool available = false;
    std::array<GpuTimestampSample, kGpuTimestampMaxRegions> samples{};

    void Reset(uint64_t frameSerial = 0) noexcept
    {
        serial = frameSerial;
        sampleCount = 0;
        available = false;
    }

    [[nodiscard]] const GpuTimestampSample *Find(std::string_view name) const noexcept
    {
        for (uint32_t i = 0; i < sampleCount; ++i) {
            if (samples[i].Name() == name) {
                return &samples[i];
            }
        }
        return nullptr;
    }
};

} // namespace infernux::rhi
