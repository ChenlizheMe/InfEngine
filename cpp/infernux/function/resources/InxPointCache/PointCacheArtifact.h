#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace infernux
{

enum class PointCacheChannelType : uint32_t
{
    Float = 0,
    Float2,
    Float3,
    Float4,
    UInt,
    Count,
};

enum class PointCacheChannelSemantic : uint32_t
{
    Custom = 0,
    Position,
    Normal,
    Color,
    Id,
    Count,
};

struct PointCacheChannel
{
    std::string name;
    PointCacheChannelType type = PointCacheChannelType::Float;
    PointCacheChannelSemantic semantic = PointCacheChannelSemantic::Custom;
    uint64_t byteOffset = 0;
    uint32_t elementStride = 0;
};

struct PointCacheCpuData
{
    std::string stableId;
    std::string name;
    std::string bakeBasis;
    uint32_t pointCount = 0;
    std::vector<PointCacheChannel> channels;
    std::vector<uint8_t> bytes;

    [[nodiscard]] const PointCacheChannel *FindChannel(std::string_view channelName) const noexcept;
    [[nodiscard]] bool IsValid() const noexcept;
};

class PointCacheArtifact final
{
  public:
    static constexpr uint32_t FormatVersion = 1;

    [[nodiscard]] static std::string Serialize(const PointCacheCpuData &cache, std::string_view sourceContentHash);
    [[nodiscard]] static PointCacheCpuData Deserialize(std::string_view bytes,
                                                       std::string_view expectedSourceContentHash);
};

[[nodiscard]] uint32_t PointCacheChannelElementStride(PointCacheChannelType type) noexcept;

} // namespace infernux
