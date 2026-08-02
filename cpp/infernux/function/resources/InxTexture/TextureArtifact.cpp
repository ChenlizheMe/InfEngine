#include "TextureArtifact.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>

namespace infernux
{
namespace
{
constexpr std::string_view Magic = "INXTEXTURE";
constexpr uint32_t EndianMarker = 0x01020304U;
constexpr uint32_t MaximumDimension = 65'536;
constexpr uint32_t MaximumMipLevels = 32;
constexpr uint64_t MaximumPayloadBytes = 1ULL << 30;
constexpr uint32_t MaximumHashBytes = 1024;

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

void AppendFloat(std::string &out, float value)
{
    uint32_t bits = 0;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&bits, &value, sizeof(bits));
    AppendU32(out, bits);
}

void AppendString(std::string &out, std::string_view value)
{
    if (value.empty() || value.size() > MaximumHashBytes)
        throw std::invalid_argument("texture artifact requires a bounded source content hash");
    AppendU32(out, static_cast<uint32_t>(value.size()));
    out.append(value);
}

class Reader final
{
  public:
    explicit Reader(std::string_view bytes) : m_bytes(bytes)
    {
    }

    uint32_t ReadU32()
    {
        Require(sizeof(uint32_t));
        uint32_t value = 0;
        for (unsigned shift = 0; shift < 32; shift += 8)
            value |= static_cast<uint32_t>(static_cast<unsigned char>(m_bytes[m_cursor++])) << shift;
        return value;
    }

    uint64_t ReadU64()
    {
        Require(sizeof(uint64_t));
        uint64_t value = 0;
        for (unsigned shift = 0; shift < 64; shift += 8)
            value |= static_cast<uint64_t>(static_cast<unsigned char>(m_bytes[m_cursor++])) << shift;
        return value;
    }

    float ReadFloat()
    {
        const uint32_t bits = ReadU32();
        float value = 0.0f;
        static_assert(sizeof(bits) == sizeof(value));
        std::memcpy(&value, &bits, sizeof(value));
        return value;
    }

    std::string ReadString()
    {
        const uint32_t size = ReadU32();
        if (size == 0 || size > MaximumHashBytes)
            throw std::invalid_argument("texture artifact has an invalid source hash size");
        Require(size);
        std::string value(m_bytes.substr(m_cursor, size));
        m_cursor += size;
        return value;
    }

    std::string_view ReadBytes(uint64_t size)
    {
        if (size > std::numeric_limits<size_t>::max())
            throw std::invalid_argument("texture artifact payload exceeds addressable memory");
        Require(static_cast<size_t>(size));
        const auto value = m_bytes.substr(m_cursor, static_cast<size_t>(size));
        m_cursor += static_cast<size_t>(size);
        return value;
    }

    bool AtEnd() const noexcept
    {
        return m_cursor == m_bytes.size();
    }

  private:
    void Require(size_t size) const
    {
        if (size > m_bytes.size() - m_cursor)
            throw std::invalid_argument("texture artifact is truncated");
    }

    std::string_view m_bytes;
    size_t m_cursor = 0;
};

bool IsValidDimension(TextureDimension dimension)
{
    return dimension == TextureDimension::Texture2D || dimension == TextureDimension::Texture3D;
}

bool IsValidSemantic(TextureSemantic semantic)
{
    return semantic >= TextureSemantic::Color && semantic <= TextureSemantic::SignedDistanceField;
}

bool IsValidFormat(TextureFormat format)
{
    return format >= TextureFormat::Rgba8UNorm && format <= TextureFormat::Rgba16Float;
}

struct MipLayout
{
    uint64_t rowPitch = 0;
    uint64_t slicePitch = 0;
    uint64_t byteSize = 0;
};

MipLayout ComputeMipLayout(uint32_t width, uint32_t height, uint32_t depth, TextureFormat format)
{
    const uint32_t bytesPerTexel = TextureFormatBytesPerTexel(format);
    const uint32_t blockBytes = TextureFormatBlockBytes(format);
    uint64_t rowPitch = 0;
    uint64_t rows = 0;
    if (bytesPerTexel != 0) {
        rowPitch = static_cast<uint64_t>(width) * bytesPerTexel;
        rows = height;
    } else if (blockBytes != 0) {
        rowPitch = static_cast<uint64_t>((std::max)(1U, (width + 3U) / 4U)) * blockBytes;
        rows = (std::max)(1U, (height + 3U) / 4U);
    } else {
        throw std::invalid_argument("texture artifact has an unsupported concrete format");
    }
    if (rows != 0 && rowPitch > MaximumPayloadBytes / rows)
        throw std::invalid_argument("texture artifact mip row layout exceeds the payload limit");
    const uint64_t slicePitch = rowPitch * rows;
    if (depth != 0 && slicePitch > MaximumPayloadBytes / depth)
        throw std::invalid_argument("texture artifact mip volume exceeds the payload limit");
    return {rowPitch, slicePitch, slicePitch * depth};
}

void ValidateTexture(const TextureCpuData &texture)
{
    if (!texture.IsValid() || !IsValidDimension(texture.dimension) || !IsValidSemantic(texture.semantic) ||
        !IsValidFormat(texture.format) || texture.mipLevels.size() > MaximumMipLevels ||
        texture.bytes.size() > MaximumPayloadBytes)
        throw std::invalid_argument("texture artifact has invalid payload dimensions");
    if (!std::all_of(texture.bakeBasis.begin(), texture.bakeBasis.end(),
                     [](float value) { return std::isfinite(value); }) ||
        !std::all_of(texture.valueMin.begin(), texture.valueMin.end(),
                     [](float value) { return std::isfinite(value); }) ||
        !std::all_of(texture.valueMax.begin(), texture.valueMax.end(),
                     [](float value) { return std::isfinite(value); }))
        throw std::invalid_argument("texture artifact metadata must contain finite values");
    for (size_t channel = 0; channel < texture.valueMin.size(); ++channel) {
        if (texture.valueMin[channel] > texture.valueMax[channel])
            throw std::invalid_argument("texture artifact value range is inverted");
    }
    uint64_t expectedOffset = 0;
    uint32_t previousWidth = 0;
    uint32_t previousHeight = 0;
    uint32_t previousDepth = 0;
    for (size_t index = 0; index < texture.mipLevels.size(); ++index) {
        const auto &mip = texture.mipLevels[index];
        if (mip.width == 0 || mip.height == 0 || mip.depth == 0 || mip.width > MaximumDimension ||
            mip.height > MaximumDimension || mip.depth > MaximumDimension ||
            (texture.dimension == TextureDimension::Texture2D && mip.depth != 1))
            throw std::invalid_argument("texture artifact has an invalid mip dimension");
        if (index > 0 &&
            (mip.width != (std::max)(1U, previousWidth / 2U) || mip.height != (std::max)(1U, previousHeight / 2U) ||
             mip.depth != (texture.dimension == TextureDimension::Texture3D ? (std::max)(1U, previousDepth / 2U) : 1U)))
            throw std::invalid_argument("texture artifact has a non-contiguous mip chain");
        const MipLayout expected = ComputeMipLayout(mip.width, mip.height, mip.depth, texture.format);
        if (mip.byteOffset != expectedOffset || mip.byteSize != expected.byteSize ||
            mip.rowPitch != expected.rowPitch || mip.slicePitch != expected.slicePitch ||
            expected.byteSize > MaximumPayloadBytes - expectedOffset)
            throw std::invalid_argument("texture artifact has an invalid mip byte range");
        expectedOffset += expected.byteSize;
        previousWidth = mip.width;
        previousHeight = mip.height;
        previousDepth = mip.depth;
    }
    if (expectedOffset != texture.bytes.size())
        throw std::invalid_argument("texture artifact payload size does not match its mip chain");
}
} // namespace

std::string TextureArtifact::Serialize(const TextureCpuData &texture, std::string_view sourceContentHash)
{
    ValidateTexture(texture);
    std::string bytes(Magic);
    AppendU32(bytes, EndianMarker);
    AppendString(bytes, sourceContentHash);
    AppendU32(bytes, static_cast<uint32_t>(texture.dimension));
    AppendU32(bytes, static_cast<uint32_t>(texture.semantic));
    AppendU32(bytes, static_cast<uint32_t>(texture.format));
    for (float value : texture.bakeBasis)
        AppendFloat(bytes, value);
    for (float value : texture.valueMin)
        AppendFloat(bytes, value);
    for (float value : texture.valueMax)
        AppendFloat(bytes, value);
    AppendU32(bytes, static_cast<uint32_t>(texture.mipLevels.size()));
    for (const auto &mip : texture.mipLevels) {
        AppendU32(bytes, mip.width);
        AppendU32(bytes, mip.height);
        AppendU32(bytes, mip.depth);
        AppendU64(bytes, mip.byteSize);
        AppendU64(bytes, mip.rowPitch);
        AppendU64(bytes, mip.slicePitch);
    }
    AppendU64(bytes, texture.bytes.size());
    bytes.append(reinterpret_cast<const char *>(texture.bytes.data()), texture.bytes.size());
    AppendU64(bytes, Fnv1a64(bytes));
    return bytes;
}

std::shared_ptr<const TextureCpuData> TextureArtifact::Deserialize(std::string_view bytes,
                                                                   std::string_view expectedSourceContentHash)
{
    if (bytes.size() < Magic.size() + sizeof(uint32_t) * 3 + sizeof(uint64_t) * 2 ||
        bytes.substr(0, Magic.size()) != Magic)
        throw std::invalid_argument("texture artifact has an invalid header");
    const size_t checksumOffset = bytes.size() - sizeof(uint64_t);
    Reader checksumReader(bytes.substr(checksumOffset));
    if (checksumReader.ReadU64() != Fnv1a64(bytes.substr(0, checksumOffset)))
        throw std::invalid_argument("texture artifact checksum mismatch");

    Reader reader(bytes.substr(Magic.size(), checksumOffset - Magic.size()));
    if (reader.ReadU32() != EndianMarker)
        throw std::invalid_argument("texture artifact has an invalid endian marker");
    if (reader.ReadString() != expectedSourceContentHash)
        throw std::invalid_argument("texture artifact does not match the imported source content");

    auto texture = std::make_shared<TextureCpuData>();
    texture->dimension = static_cast<TextureDimension>(reader.ReadU32());
    texture->semantic = static_cast<TextureSemantic>(reader.ReadU32());
    texture->format = static_cast<TextureFormat>(reader.ReadU32());
    for (float &value : texture->bakeBasis)
        value = reader.ReadFloat();
    for (float &value : texture->valueMin)
        value = reader.ReadFloat();
    for (float &value : texture->valueMax)
        value = reader.ReadFloat();
    if (!IsValidFormat(texture->format))
        throw std::invalid_argument("texture artifact has an unsupported concrete format");
    const uint32_t mipCount = reader.ReadU32();
    if (mipCount == 0 || mipCount > MaximumMipLevels)
        throw std::invalid_argument("texture artifact has an invalid mip count");
    texture->mipLevels.reserve(mipCount);
    uint64_t offset = 0;
    for (uint32_t index = 0; index < mipCount; ++index) {
        TextureMipLevel mip;
        mip.width = reader.ReadU32();
        mip.height = reader.ReadU32();
        mip.depth = reader.ReadU32();
        mip.byteOffset = offset;
        mip.byteSize = reader.ReadU64();
        mip.rowPitch = reader.ReadU64();
        mip.slicePitch = reader.ReadU64();
        if (mip.byteSize > MaximumPayloadBytes - offset)
            throw std::invalid_argument("texture artifact mip payload exceeds the format limit");
        offset += mip.byteSize;
        texture->mipLevels.push_back(mip);
    }
    const uint64_t payloadSize = reader.ReadU64();
    if (payloadSize != offset || payloadSize > MaximumPayloadBytes)
        throw std::invalid_argument("texture artifact has an invalid payload size");
    const auto payload = reader.ReadBytes(payloadSize);
    texture->bytes.assign(payload.begin(), payload.end());
    if (!reader.AtEnd())
        throw std::invalid_argument("texture artifact contains trailing data");
    ValidateTexture(*texture);
    return texture;
}

bool TextureArtifact::HasCurrentHeader(std::string_view bytes) noexcept
{
    return bytes.size() >= Magic.size() + sizeof(uint32_t) && bytes.substr(0, Magic.size()) == Magic &&
           static_cast<unsigned char>(bytes[Magic.size()]) == 0x04 &&
           static_cast<unsigned char>(bytes[Magic.size() + 1]) == 0x03 &&
           static_cast<unsigned char>(bytes[Magic.size() + 2]) == 0x02 &&
           static_cast<unsigned char>(bytes[Magic.size() + 3]) == 0x01;
}

} // namespace infernux
