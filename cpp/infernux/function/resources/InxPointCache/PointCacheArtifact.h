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

enum class PointCacheIdLookupMode : uint32_t
{
    Identity = 0,
    Hash,
};

struct PointCacheIdLookupEntry
{
    uint32_t stableId = 0;
    uint32_t pointIndex = UINT32_MAX;
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
    PointCacheIdLookupMode idLookupMode = PointCacheIdLookupMode::Identity;
    std::vector<PointCacheIdLookupEntry> idLookup;

    [[nodiscard]] const PointCacheChannel *FindChannel(std::string_view channelName) const noexcept;
    void RebuildIdLookup();
    [[nodiscard]] uint32_t FindPointIndex(uint32_t stableId) const noexcept;
    [[nodiscard]] bool IsValid() const noexcept;
};

class PointCacheArtifact final
{
  public:
    [[nodiscard]] static bool HasCurrentHeader(std::string_view bytes) noexcept;
    [[nodiscard]] static std::string Serialize(const PointCacheCpuData &cache, std::string_view sourceContentHash);
    [[nodiscard]] static PointCacheCpuData Deserialize(std::string_view bytes,
                                                       std::string_view expectedSourceContentHash);
};

[[nodiscard]] uint32_t PointCacheChannelElementStride(PointCacheChannelType type) noexcept;

} // namespace infernux
