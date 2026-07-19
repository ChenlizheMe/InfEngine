#include "PointCacheArtifact.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <unordered_set>

namespace infernux
{
namespace
{
constexpr std::string_view Magic = "INXPOINT";
constexpr uint32_t EndianMarker = 0x01020304U;
constexpr uint32_t MaximumPointCount = 100'000'000U;
constexpr uint32_t MaximumChannelCount = 256U;
constexpr uint32_t MaximumStringBytes = 16U * 1024U;
constexpr uint64_t MaximumPayloadBytes = 1ULL << 30;

uint64_t Fnv1a64(std::string_view bytes)
{
    uint64_t hash = 14695981039346656037ULL;
    for (const unsigned char byte : bytes) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }
    return hash;
}

void AppendU32(std::string &out, uint32_t value)
{
    for (unsigned shift = 0; shift < 32; shift += 8)
        out.push_back(static_cast<char>((value >> shift) & 0xffU));
}

void AppendU64(std::string &out, uint64_t value)
{
    for (unsigned shift = 0; shift < 64; shift += 8)
        out.push_back(static_cast<char>((value >> shift) & 0xffU));
}

void AppendString(std::string &out, std::string_view value, const char *label)
{
    if (value.empty() || value.size() > MaximumStringBytes)
        throw std::invalid_argument(std::string("point cache artifact has an invalid ") + label);
    AppendU32(out, static_cast<uint32_t>(value.size()));
    out.append(value);
}

class Reader final
{
  public:
    explicit Reader(std::string_view bytes) : m_bytes(bytes)
    {
    }

    [[nodiscard]] uint32_t ReadU32()
    {
        Require(sizeof(uint32_t));
        uint32_t value = 0;
        for (unsigned shift = 0; shift < 32; shift += 8)
            value |= static_cast<uint32_t>(static_cast<unsigned char>(m_bytes[m_cursor++])) << shift;
        return value;
    }

    [[nodiscard]] uint64_t ReadU64()
    {
        Require(sizeof(uint64_t));
        uint64_t value = 0;
        for (unsigned shift = 0; shift < 64; shift += 8)
            value |= static_cast<uint64_t>(static_cast<unsigned char>(m_bytes[m_cursor++])) << shift;
        return value;
    }

    [[nodiscard]] std::string ReadString(const char *label)
    {
        const uint32_t size = ReadU32();
        if (size == 0 || size > MaximumStringBytes)
            throw std::invalid_argument(std::string("point cache artifact has an invalid ") + label);
        Require(size);
        std::string value(m_bytes.substr(m_cursor, size));
        m_cursor += size;
        return value;
    }

    [[nodiscard]] std::string_view ReadBytes(uint64_t size)
    {
        if (size > std::numeric_limits<size_t>::max())
            throw std::invalid_argument("point cache artifact payload exceeds addressable memory");
        Require(static_cast<size_t>(size));
        const auto value = m_bytes.substr(m_cursor, static_cast<size_t>(size));
        m_cursor += static_cast<size_t>(size);
        return value;
    }

    [[nodiscard]] bool AtEnd() const noexcept
    {
        return m_cursor == m_bytes.size();
    }

  private:
    void Require(size_t size) const
    {
        if (size > m_bytes.size() - m_cursor)
            throw std::invalid_argument("point cache artifact is truncated");
    }

    std::string_view m_bytes;
    size_t m_cursor = 0;
};

uint32_t ReadPayloadU32(const std::vector<uint8_t> &bytes, uint64_t offset)
{
    uint32_t value = 0;
    for (unsigned shift = 0; shift < 32; shift += 8)
        value |= static_cast<uint32_t>(bytes[static_cast<size_t>(offset++)]) << shift;
    return value;
}

float ReadPayloadFloat(const std::vector<uint8_t> &bytes, uint64_t offset)
{
    const uint32_t bits = ReadPayloadU32(bytes, offset);
    float value = 0.0F;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&value, &bits, sizeof(value));
    return value;
}

void ValidatePointCache(const PointCacheCpuData &cache)
{
    if (cache.stableId.empty() || cache.name.empty() || cache.bakeBasis.empty())
        throw std::invalid_argument("point cache artifact identity and bake basis must be non-empty");
    if (cache.pointCount == 0 || cache.pointCount > MaximumPointCount || cache.channels.empty() ||
        cache.channels.size() > MaximumChannelCount || cache.bytes.empty() || cache.bytes.size() > MaximumPayloadBytes)
        throw std::invalid_argument("point cache artifact dimensions exceed the format limits");

    std::unordered_set<std::string> names;
    std::unordered_set<uint32_t> nonCustomSemantics;
    const PointCacheChannel *idChannel = nullptr;
    bool hasPosition = false;
    uint64_t previousEnd = 0;
    for (const auto &channel : cache.channels) {
        const uint32_t expectedStride = PointCacheChannelElementStride(channel.type);
        if (channel.name.empty() || !names.insert(channel.name).second || expectedStride == 0 ||
            channel.elementStride != expectedStride || channel.semantic >= PointCacheChannelSemantic::Count ||
            channel.byteOffset % 16U != 0)
            throw std::invalid_argument("point cache artifact contains an invalid channel descriptor");
        if (channel.semantic != PointCacheChannelSemantic::Custom &&
            !nonCustomSemantics.insert(static_cast<uint32_t>(channel.semantic)).second)
            throw std::invalid_argument("point cache artifact contains duplicate channel semantics");
        const uint64_t channelBytes = static_cast<uint64_t>(cache.pointCount) * channel.elementStride;
        if (channel.byteOffset < previousEnd || channel.byteOffset > cache.bytes.size() ||
            channelBytes > cache.bytes.size() - channel.byteOffset)
            throw std::invalid_argument("point cache artifact channel range is invalid");
        previousEnd = channel.byteOffset + channelBytes;

        switch (channel.semantic) {
        case PointCacheChannelSemantic::Position:
            if (channel.type != PointCacheChannelType::Float3)
                throw std::invalid_argument("point cache position channel must use float3");
            hasPosition = true;
            break;
        case PointCacheChannelSemantic::Normal:
            if (channel.type != PointCacheChannelType::Float3)
                throw std::invalid_argument("point cache normal channel must use float3");
            break;
        case PointCacheChannelSemantic::Color:
            if (channel.type != PointCacheChannelType::Float4)
                throw std::invalid_argument("point cache color channel must use float4");
            break;
        case PointCacheChannelSemantic::Id:
            if (channel.type != PointCacheChannelType::UInt)
                throw std::invalid_argument("point cache id channel must use uint");
            idChannel = &channel;
            break;
        case PointCacheChannelSemantic::Custom:
        case PointCacheChannelSemantic::Count:
            break;
        }

        if (channel.type != PointCacheChannelType::UInt) {
            const uint32_t components = channel.elementStride / sizeof(float);
            for (uint32_t point = 0; point < cache.pointCount; ++point) {
                const uint64_t base = channel.byteOffset + static_cast<uint64_t>(point) * channel.elementStride;
                for (uint32_t component = 0; component < components; ++component) {
                    if (!std::isfinite(ReadPayloadFloat(cache.bytes, base + component * sizeof(float))))
                        throw std::invalid_argument("point cache artifact contains a non-finite float");
                }
            }
        }
    }
    if (!hasPosition || !idChannel)
        throw std::invalid_argument("point cache artifact requires position and id channel semantics");

    std::unordered_set<uint32_t> stableIds;
    stableIds.reserve(cache.pointCount);
    for (uint32_t point = 0; point < cache.pointCount; ++point) {
        const uint64_t offset = idChannel->byteOffset + static_cast<uint64_t>(point) * idChannel->elementStride;
        if (!stableIds.insert(ReadPayloadU32(cache.bytes, offset)).second)
            throw std::invalid_argument("point cache artifact stable point IDs must be unique");
    }
}
} // namespace

uint32_t PointCacheChannelElementStride(PointCacheChannelType type) noexcept
{
    switch (type) {
    case PointCacheChannelType::Float:
    case PointCacheChannelType::UInt:
        return 4;
    case PointCacheChannelType::Float2:
        return 8;
    case PointCacheChannelType::Float3:
        return 12;
    case PointCacheChannelType::Float4:
        return 16;
    case PointCacheChannelType::Count:
        return 0;
    }
    return 0;
}

const PointCacheChannel *PointCacheCpuData::FindChannel(std::string_view channelName) const noexcept
{
    for (const auto &channel : channels) {
        if (channel.name == channelName)
            return &channel;
    }
    return nullptr;
}

void PointCacheCpuData::RebuildIdLookup()
{
    const auto idChannel = std::find_if(channels.begin(), channels.end(), [](const PointCacheChannel &channel) {
        return channel.semantic == PointCacheChannelSemantic::Id;
    });
    if (idChannel == channels.end() || idChannel->type != PointCacheChannelType::UInt)
        throw std::invalid_argument("point cache cannot build an ID lookup without a uint ID channel");

    bool identity = true;
    for (uint32_t point = 0; point < pointCount; ++point) {
        const uint64_t offset = idChannel->byteOffset + static_cast<uint64_t>(point) * idChannel->elementStride;
        if (ReadPayloadU32(bytes, offset) != point) {
            identity = false;
            break;
        }
    }
    idLookup.clear();
    if (identity) {
        idLookupMode = PointCacheIdLookupMode::Identity;
        return;
    }

    uint64_t minimumCapacity = static_cast<uint64_t>(pointCount) + (static_cast<uint64_t>(pointCount) + 1U) / 2U;
    uint64_t capacity = 1;
    while (capacity < minimumCapacity)
        capacity <<= 1U;
    if (capacity > static_cast<uint64_t>(std::numeric_limits<uint32_t>::max()))
        throw std::invalid_argument("point cache ID lookup exceeds the supported size");

    idLookupMode = PointCacheIdLookupMode::Hash;
    idLookup.assign(static_cast<size_t>(capacity), PointCacheIdLookupEntry{});
    const uint32_t mask = static_cast<uint32_t>(capacity - 1U);
    for (uint32_t point = 0; point < pointCount; ++point) {
        const uint64_t offset = idChannel->byteOffset + static_cast<uint64_t>(point) * idChannel->elementStride;
        const uint32_t stableId = ReadPayloadU32(bytes, offset);
        uint32_t slot = (stableId * 0x9e3779b1U) & mask;
        while (idLookup[slot].pointIndex != UINT32_MAX)
            slot = (slot + 1U) & mask;
        idLookup[slot] = {stableId, point};
    }
}

uint32_t PointCacheCpuData::FindPointIndex(uint32_t stableId) const noexcept
{
    if (idLookupMode == PointCacheIdLookupMode::Identity)
        return stableId < pointCount ? stableId : UINT32_MAX;
    if (idLookup.empty())
        return UINT32_MAX;
    const uint32_t mask = static_cast<uint32_t>(idLookup.size() - 1U);
    uint32_t slot = (stableId * 0x9e3779b1U) & mask;
    for (size_t probe = 0; probe < idLookup.size(); ++probe) {
        const auto &entry = idLookup[slot];
        if (entry.pointIndex == UINT32_MAX)
            return UINT32_MAX;
        if (entry.stableId == stableId)
            return entry.pointIndex;
        slot = (slot + 1U) & mask;
    }
    return UINT32_MAX;
}

bool PointCacheCpuData::IsValid() const noexcept
{
    try {
        ValidatePointCache(*this);
        return true;
    } catch (...) {
        return false;
    }
}

std::string PointCacheArtifact::Serialize(const PointCacheCpuData &cache, std::string_view sourceContentHash)
{
    ValidatePointCache(cache);
    std::string bytes(Magic);
    AppendU32(bytes, FormatVersion);
    AppendU32(bytes, EndianMarker);
    AppendString(bytes, sourceContentHash, "source content hash");
    AppendString(bytes, cache.stableId, "stable ID");
    AppendString(bytes, cache.name, "name");
    AppendString(bytes, cache.bakeBasis, "bake basis");
    AppendU32(bytes, cache.pointCount);
    AppendU32(bytes, static_cast<uint32_t>(cache.channels.size()));
    for (const auto &channel : cache.channels) {
        AppendString(bytes, channel.name, "channel name");
        AppendU32(bytes, static_cast<uint32_t>(channel.type));
        AppendU32(bytes, static_cast<uint32_t>(channel.semantic));
        AppendU64(bytes, channel.byteOffset);
        AppendU32(bytes, channel.elementStride);
    }
    AppendU64(bytes, cache.bytes.size());
    bytes.append(reinterpret_cast<const char *>(cache.bytes.data()), cache.bytes.size());
    AppendU64(bytes, Fnv1a64(bytes));
    return bytes;
}

PointCacheCpuData PointCacheArtifact::Deserialize(std::string_view bytes, std::string_view expectedSourceContentHash)
{
    if (bytes.size() < Magic.size() + sizeof(uint32_t) * 4 + sizeof(uint64_t) * 2 ||
        bytes.substr(0, Magic.size()) != Magic)
        throw std::invalid_argument("point cache artifact has an invalid header");
    const size_t checksumOffset = bytes.size() - sizeof(uint64_t);
    Reader checksumReader(bytes.substr(checksumOffset));
    if (checksumReader.ReadU64() != Fnv1a64(bytes.substr(0, checksumOffset)))
        throw std::invalid_argument("point cache artifact checksum mismatch");

    Reader reader(bytes.substr(Magic.size(), checksumOffset - Magic.size()));
    if (reader.ReadU32() != FormatVersion)
        throw std::invalid_argument("point cache artifact uses an unsupported format version");
    if (reader.ReadU32() != EndianMarker)
        throw std::invalid_argument("point cache artifact has an invalid endian marker");
    if (reader.ReadString("source content hash") != expectedSourceContentHash)
        throw std::invalid_argument("point cache artifact does not match the imported source content");

    PointCacheCpuData cache;
    cache.stableId = reader.ReadString("stable ID");
    cache.name = reader.ReadString("name");
    cache.bakeBasis = reader.ReadString("bake basis");
    cache.pointCount = reader.ReadU32();
    const uint32_t channelCount = reader.ReadU32();
    if (channelCount == 0 || channelCount > MaximumChannelCount)
        throw std::invalid_argument("point cache artifact has an invalid channel count");
    cache.channels.reserve(channelCount);
    for (uint32_t index = 0; index < channelCount; ++index) {
        PointCacheChannel channel;
        channel.name = reader.ReadString("channel name");
        channel.type = static_cast<PointCacheChannelType>(reader.ReadU32());
        channel.semantic = static_cast<PointCacheChannelSemantic>(reader.ReadU32());
        channel.byteOffset = reader.ReadU64();
        channel.elementStride = reader.ReadU32();
        cache.channels.push_back(std::move(channel));
    }
    const uint64_t payloadSize = reader.ReadU64();
    if (payloadSize == 0 || payloadSize > MaximumPayloadBytes)
        throw std::invalid_argument("point cache artifact has an invalid payload size");
    const auto payload = reader.ReadBytes(payloadSize);
    cache.bytes.assign(payload.begin(), payload.end());
    if (!reader.AtEnd())
        throw std::invalid_argument("point cache artifact contains trailing data");
    ValidatePointCache(cache);
    cache.RebuildIdLookup();
    return cache;
}

} // namespace infernux
