#pragma once

#include "RhiHandles.h"

#include <cstdint>

namespace infernux::rhi
{

using SubmissionSerial = uint64_t;
inline constexpr SubmissionSerial InvalidSubmissionSerial = 0;

enum class QueueRole : uint8_t
{
    Graphics,
    Compute,
    Transfer,
    Present,
    Count,
};

enum class SubmissionDomain : uint8_t
{
    Frame,
    AsyncUpload,
    AsyncReadback,
    Preview,
    Background,
};

struct SubmissionTicket
{
    DeviceId device = InvalidDeviceId;
    QueueRole queue = QueueRole::Graphics;
    SubmissionSerial serial = InvalidSubmissionSerial;

    [[nodiscard]] constexpr bool IsValid() const noexcept
    {
        return device != InvalidDeviceId && serial != InvalidSubmissionSerial;
    }
};

} // namespace infernux::rhi
