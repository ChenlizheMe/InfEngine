#pragma once

#include <cstdint>
#include <string>

namespace infernux
{

enum class ScenePickStatus : uint8_t
{
    Pending,
    Completed,
    Failed,
    Cancelled,
    Unknown
};

struct ScenePickSnapshot
{
    uint64_t requestId = 0;
    ScenePickStatus status = ScenePickStatus::Unknown;
    uint64_t objectId = 0;
    std::string error;
};

} // namespace infernux
