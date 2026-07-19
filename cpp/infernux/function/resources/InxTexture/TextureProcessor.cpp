#include "TextureProcessor.h"

#define STB_DXT_IMPLEMENTATION
#include <stb_dxt.h>

#include <glm/gtc/packing.hpp>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <utility>
#include <vector>

namespace infernux
{
namespace
{
constexpr uint64_t MaximumProcessedBytes = 1ULL << 30;

bool IsRgba8(TextureFormat format)
{
    return format == TextureFormat::Rgba8UNorm || format == TextureFormat::Rgba8Srgb;
}

float SrgbToLinear(float value)
{
    return value <= 0.04045f ? value / 12.92f : std::pow((value + 0.055f) / 1.055f, 2.4f);
}

float LinearToSrgb(float value)
{
    value = std::clamp(value, 0.0f, 1.0f);
    return value <= 0.0031308f ? value * 12.92f : 1.055f * std::pow(value, 1.0f / 2.4f) - 0.055f;
}

uint8_t QuantizeUNorm(float value)
{
    return static_cast<uint8_t>(std::lround(std::clamp(value, 0.0f, 1.0f) * 255.0f));
}

uint64_t CheckedTexelCount(uint32_t width, uint32_t height, uint32_t depth)
{
    const uint64_t plane = static_cast<uint64_t>(width) * height;
    if (width == 0 || height == 0 || depth == 0 || plane > MaximumProcessedBytes ||
        plane > MaximumProcessedBytes / depth)
        throw std::overflow_error("texture processing dimensions exceed the payload limit");
    return plane * depth;
}

TextureMipLevel MakeUncompressedMip(uint32_t width, uint32_t height, uint32_t depth, uint64_t offset,
                                    TextureFormat format)
{
    const uint32_t bytesPerTexel = TextureFormatBytesPerTexel(format);
    if (bytesPerTexel == 0)
        throw std::invalid_argument("mip generation requires an uncompressed texture format");
    TextureMipLevel mip;
    mip.width = width;
    mip.height = height;
    mip.depth = depth;
    mip.byteOffset = offset;
    mip.rowPitch = static_cast<uint64_t>(width) * bytesPerTexel;
    mip.slicePitch = mip.rowPitch * height;
    mip.byteSize = mip.slicePitch * depth;
    if (mip.byteSize > MaximumProcessedBytes - offset)
        throw std::overflow_error("texture mip chain exceeds the payload limit");
    return mip;
}

std::array<float, 4> ReadTexel(const TextureCpuData &texture, const TextureMipLevel &mip, uint32_t x, uint32_t y,
                               uint32_t z)
{
    const uint64_t texel = static_cast<uint64_t>(z) * mip.slicePitch + static_cast<uint64_t>(y) * mip.rowPitch;
    if (IsRgba8(texture.format)) {
        const uint8_t *value = texture.bytes.data() + mip.byteOffset + texel + static_cast<uint64_t>(x) * 4U;
        std::array<float, 4> result{};
        for (size_t channel = 0; channel < result.size(); ++channel)
            result[channel] = static_cast<float>(value[channel]) / 255.0f;
        if (TextureFormatIsSrgb(texture.format) &&
            (texture.semantic == TextureSemantic::Color || texture.semantic == TextureSemantic::UserInterface ||
             texture.semantic == TextureSemantic::Sprite)) {
            result[0] = SrgbToLinear(result[0]);
            result[1] = SrgbToLinear(result[1]);
            result[2] = SrgbToLinear(result[2]);
        }
        return result;
    }
    if (texture.format != TextureFormat::Rgba32Float)
        throw std::invalid_argument("mip generation cannot read a block-compressed source");
    std::array<float, 4> result{};
    const uint64_t offset = mip.byteOffset + texel + static_cast<uint64_t>(x) * sizeof(float) * 4U;
    std::memcpy(result.data(), texture.bytes.data() + offset, sizeof(float) * result.size());
    return result;
}

void WriteTexel(TextureCpuData &texture, const TextureMipLevel &mip, uint32_t x, uint32_t y, uint32_t z,
                std::array<float, 4> value)
{
    const uint64_t texel = static_cast<uint64_t>(z) * mip.slicePitch + static_cast<uint64_t>(y) * mip.rowPitch;
    if (IsRgba8(texture.format)) {
        if (TextureFormatIsSrgb(texture.format) &&
            (texture.semantic == TextureSemantic::Color || texture.semantic == TextureSemantic::UserInterface ||
             texture.semantic == TextureSemantic::Sprite)) {
            value[0] = LinearToSrgb(value[0]);
            value[1] = LinearToSrgb(value[1]);
            value[2] = LinearToSrgb(value[2]);
        }
        uint8_t *destination = texture.bytes.data() + mip.byteOffset + texel + static_cast<uint64_t>(x) * 4U;
        for (size_t channel = 0; channel < value.size(); ++channel)
            destination[channel] = QuantizeUNorm(value[channel]);
        return;
    }
    const uint64_t offset = mip.byteOffset + texel + static_cast<uint64_t>(x) * sizeof(float) * 4U;
    std::memcpy(texture.bytes.data() + offset, value.data(), sizeof(float) * value.size());
}

void NormalizeNormal(std::array<float, 4> &value)
{
    float x = value[0] * 2.0f - 1.0f;
    float y = value[1] * 2.0f - 1.0f;
    float z = value[2] * 2.0f - 1.0f;
    const float lengthSquared = x * x + y * y + z * z;
    if (lengthSquared > 1.0e-20f) {
        const float inverseLength = 1.0f / std::sqrt(lengthSquared);
        x *= inverseLength;
        y *= inverseLength;
        z *= inverseLength;
    } else {
        x = 0.0f;
        y = 0.0f;
        z = 1.0f;
    }
    value[0] = x * 0.5f + 0.5f;
    value[1] = y * 0.5f + 0.5f;
    value[2] = z * 0.5f + 0.5f;
}

void GenerateMipChain(TextureCpuData &texture)
{
    if (texture.mipLevels.size() != 1 || TextureFormatIsBlockCompressed(texture.format))
        throw std::invalid_argument("mip generation requires one uncompressed base level");
    while (texture.mipLevels.back().width > 1 || texture.mipLevels.back().height > 1 ||
           texture.mipLevels.back().depth > 1) {
        const TextureMipLevel previous = texture.mipLevels.back();
        const uint32_t width = (std::max)(1U, previous.width / 2U);
        const uint32_t height = (std::max)(1U, previous.height / 2U);
        const uint32_t depth =
            texture.dimension == TextureDimension::Texture3D ? (std::max)(1U, previous.depth / 2U) : 1U;
        const TextureMipLevel current = MakeUncompressedMip(width, height, depth, texture.bytes.size(), texture.format);
        texture.bytes.resize(static_cast<size_t>(current.byteOffset + current.byteSize));
        texture.mipLevels.push_back(current);

        for (uint32_t z = 0; z < depth; ++z) {
            for (uint32_t y = 0; y < height; ++y) {
                for (uint32_t x = 0; x < width; ++x) {
                    std::array<float, 4> value{};
                    uint32_t sampleCount = 0;
                    const auto sourceRange = [](uint32_t outputIndex, uint32_t outputSize, uint32_t inputSize) {
                        const uint32_t begin =
                            static_cast<uint32_t>(static_cast<uint64_t>(outputIndex) * inputSize / outputSize);
                        const uint32_t end = static_cast<uint32_t>(
                            (static_cast<uint64_t>(outputIndex + 1U) * inputSize + outputSize - 1U) / outputSize);
                        return std::pair{begin, (std::min)(inputSize, end)};
                    };
                    const auto [xBegin, xEnd] = sourceRange(x, width, previous.width);
                    const auto [yBegin, yEnd] = sourceRange(y, height, previous.height);
                    const auto [zBegin, zEnd] = sourceRange(z, depth, previous.depth);
                    for (uint32_t sz = zBegin; sz < zEnd; ++sz) {
                        for (uint32_t sy = yBegin; sy < yEnd; ++sy) {
                            for (uint32_t sx = xBegin; sx < xEnd; ++sx) {
                                const auto sample = ReadTexel(texture, previous, sx, sy, sz);
                                for (size_t channel = 0; channel < value.size(); ++channel)
                                    value[channel] += sample[channel];
                                ++sampleCount;
                            }
                        }
                    }
                    for (float &channel : value)
                        channel /= static_cast<float>(sampleCount);
                    if (texture.semantic == TextureSemantic::Normal)
                        NormalizeNormal(value);
                    WriteTexel(texture, current, x, y, z, value);
                }
            }
        }
    }
}

void ComputeValueRange(TextureCpuData &texture)
{
    const TextureMipLevel &base = texture.mipLevels.front();
    texture.valueMin.fill(std::numeric_limits<float>::infinity());
    texture.valueMax.fill(-std::numeric_limits<float>::infinity());
    for (uint32_t z = 0; z < base.depth; ++z) {
        for (uint32_t y = 0; y < base.height; ++y) {
            for (uint32_t x = 0; x < base.width; ++x) {
                auto value = ReadTexel(texture, base, x, y, z);
                if (TextureFormatIsSrgb(texture.format) &&
                    (texture.semantic == TextureSemantic::Color || texture.semantic == TextureSemantic::UserInterface ||
                     texture.semantic == TextureSemantic::Sprite)) {
                    value[0] = LinearToSrgb(value[0]);
                    value[1] = LinearToSrgb(value[1]);
                    value[2] = LinearToSrgb(value[2]);
                }
                for (size_t channel = 0; channel < value.size(); ++channel) {
                    texture.valueMin[channel] = (std::min)(texture.valueMin[channel], value[channel]);
                    texture.valueMax[channel] = (std::max)(texture.valueMax[channel], value[channel]);
                }
            }
        }
    }
}

bool HasNonOpaqueAlpha(const TextureCpuData &texture)
{
    if (!IsRgba8(texture.format))
        return false;
    const TextureMipLevel &base = texture.mipLevels.front();
    for (uint32_t z = 0; z < base.depth; ++z) {
        for (uint32_t y = 0; y < base.height; ++y) {
            const uint8_t *row = texture.bytes.data() + base.byteOffset + static_cast<uint64_t>(z) * base.slicePitch +
                                 static_cast<uint64_t>(y) * base.rowPitch;
            for (uint32_t x = 0; x < base.width; ++x) {
                if (row[x * 4U + 3U] != 255U)
                    return true;
            }
        }
    }
    return false;
}

TextureCompression ResolveCompression(const TextureCpuData &texture, TextureCompression requested)
{
    if (requested != TextureCompression::Automatic)
        return requested;
    if (!IsRgba8(texture.format) || texture.dimension != TextureDimension::Texture2D)
        return TextureCompression::None;
    switch (texture.semantic) {
    case TextureSemantic::Color:
        return HasNonOpaqueAlpha(texture) ? TextureCompression::BC3 : TextureCompression::BC1;
    case TextureSemantic::Normal:
        return TextureCompression::BC5;
    case TextureSemantic::Data:
    case TextureSemantic::UserInterface:
    case TextureSemantic::Sprite:
    case TextureSemantic::VectorField:
    case TextureSemantic::SignedDistanceField:
        return TextureCompression::None;
    }
    return TextureCompression::None;
}

TextureFormat CompressedFormat(TextureFormat source, TextureCompression compression)
{
    const bool srgb = TextureFormatIsSrgb(source);
    switch (compression) {
    case TextureCompression::BC1:
        return srgb ? TextureFormat::BC1RgbaSrgb : TextureFormat::BC1RgbaUNorm;
    case TextureCompression::BC3:
        return srgb ? TextureFormat::BC3Srgb : TextureFormat::BC3UNorm;
    case TextureCompression::BC4:
        return TextureFormat::BC4UNorm;
    case TextureCompression::BC5:
        return TextureFormat::BC5UNorm;
    case TextureCompression::None:
    case TextureCompression::Automatic:
        return source;
    }
    return source;
}

TextureMipLevel MakeCompressedMip(uint32_t width, uint32_t height, uint32_t depth, uint64_t offset,
                                  TextureFormat format)
{
    TextureMipLevel mip;
    mip.width = width;
    mip.height = height;
    mip.depth = depth;
    mip.byteOffset = offset;
    mip.rowPitch = static_cast<uint64_t>((std::max)(1U, (width + 3U) / 4U)) * TextureFormatBlockBytes(format);
    mip.slicePitch = mip.rowPitch * (std::max)(1U, (height + 3U) / 4U);
    mip.byteSize = mip.slicePitch * depth;
    return mip;
}

TextureCpuData CompressTexture(const TextureCpuData &source, TextureCompression compression,
                               TextureCompressionQuality quality)
{
    if (!IsRgba8(source.format) || source.dimension != TextureDimension::Texture2D)
        throw std::invalid_argument("BC1/3/4/5 compression requires an RGBA8 Texture2D source");
    if (compression == TextureCompression::BC1 && HasNonOpaqueAlpha(source))
        throw std::invalid_argument("BC1 compression cannot preserve non-opaque alpha; use BC3 or automatic");

    TextureCpuData result;
    result.dimension = source.dimension;
    result.semantic = source.semantic;
    result.format = CompressedFormat(source.format, compression);
    result.bakeBasis = source.bakeBasis;
    result.valueMin = source.valueMin;
    result.valueMax = source.valueMax;
    const int dxtMode = quality == TextureCompressionQuality::Fast ? STB_DXT_NORMAL : STB_DXT_HIGHQUAL;

    for (const TextureMipLevel &sourceMip : source.mipLevels) {
        const TextureMipLevel destinationMip =
            MakeCompressedMip(sourceMip.width, sourceMip.height, sourceMip.depth, result.bytes.size(), result.format);
        result.bytes.resize(static_cast<size_t>(destinationMip.byteOffset + destinationMip.byteSize));
        result.mipLevels.push_back(destinationMip);
        const uint32_t blocksX = (std::max)(1U, (sourceMip.width + 3U) / 4U);
        const uint32_t blocksY = (std::max)(1U, (sourceMip.height + 3U) / 4U);
        for (uint32_t blockY = 0; blockY < blocksY; ++blockY) {
            for (uint32_t blockX = 0; blockX < blocksX; ++blockX) {
                std::array<uint8_t, 64> rgba{};
                std::array<uint8_t, 16> red{};
                std::array<uint8_t, 32> redGreen{};
                for (uint32_t y = 0; y < 4; ++y) {
                    for (uint32_t x = 0; x < 4; ++x) {
                        const uint32_t sx = (std::min)(sourceMip.width - 1U, blockX * 4U + x);
                        const uint32_t sy = (std::min)(sourceMip.height - 1U, blockY * 4U + y);
                        const uint8_t *sourcePixel = source.bytes.data() + sourceMip.byteOffset +
                                                     static_cast<uint64_t>(sy) * sourceMip.rowPitch + sx * 4U;
                        const size_t pixel = static_cast<size_t>(y * 4U + x);
                        std::memcpy(rgba.data() + pixel * 4U, sourcePixel, 4U);
                        red[pixel] = sourcePixel[0];
                        redGreen[pixel * 2U] = sourcePixel[0];
                        redGreen[pixel * 2U + 1U] = sourcePixel[1];
                    }
                }
                uint8_t *destination = result.bytes.data() + destinationMip.byteOffset +
                                       static_cast<uint64_t>(blockY) * destinationMip.rowPitch +
                                       static_cast<uint64_t>(blockX) * TextureFormatBlockBytes(result.format);
                switch (compression) {
                case TextureCompression::BC1:
                    stb_compress_dxt_block(destination, rgba.data(), 0, dxtMode);
                    break;
                case TextureCompression::BC3:
                    stb_compress_dxt_block(destination, rgba.data(), 1, dxtMode);
                    break;
                case TextureCompression::BC4:
                    stb_compress_bc4_block(destination, red.data());
                    break;
                case TextureCompression::BC5:
                    stb_compress_bc5_block(destination, redGreen.data());
                    break;
                case TextureCompression::None:
                case TextureCompression::Automatic:
                    throw std::logic_error("resolved texture compression mode is not concrete");
                }
            }
        }
    }
    return result;
}

TextureFormat ResolveTargetFormat(const TextureCpuData &source, TextureTargetFormat target)
{
    switch (target) {
    case TextureTargetFormat::Automatic:
        return source.format;
    case TextureTargetFormat::Rgba8:
        return TextureFormatIsSrgb(source.format) ? TextureFormat::Rgba8Srgb : TextureFormat::Rgba8UNorm;
    case TextureTargetFormat::Rgba4UNorm:
        return TextureFormat::Rgba4UNormPack16;
    case TextureTargetFormat::Rgba16UNorm:
        return TextureFormat::Rgba16UNorm;
    case TextureTargetFormat::Rgba16Float:
        return TextureFormat::Rgba16Float;
    case TextureTargetFormat::Rgba32Float:
        return TextureFormat::Rgba32Float;
    }
    throw std::invalid_argument("texture target format is invalid");
}

TextureCpuData ConvertTexture(const TextureCpuData &source, TextureTargetFormat target)
{
    if (target == TextureTargetFormat::Automatic)
        return source;

    TextureCpuData result;
    result.dimension = source.dimension;
    result.semantic = source.semantic;
    result.format = ResolveTargetFormat(source, target);
    result.bakeBasis = source.bakeBasis;
    result.valueMin = source.valueMin;
    result.valueMax = source.valueMax;

    for (const TextureMipLevel &sourceMip : source.mipLevels) {
        const TextureMipLevel destinationMip =
            MakeUncompressedMip(sourceMip.width, sourceMip.height, sourceMip.depth, result.bytes.size(), result.format);
        result.bytes.resize(static_cast<size_t>(destinationMip.byteOffset + destinationMip.byteSize));
        result.mipLevels.push_back(destinationMip);

        for (uint32_t z = 0; z < sourceMip.depth; ++z) {
            for (uint32_t y = 0; y < sourceMip.height; ++y) {
                for (uint32_t x = 0; x < sourceMip.width; ++x) {
                    const auto value = ReadTexel(source, sourceMip, x, y, z);
                    uint8_t *destination = result.bytes.data() + destinationMip.byteOffset +
                                           static_cast<uint64_t>(z) * destinationMip.slicePitch +
                                           static_cast<uint64_t>(y) * destinationMip.rowPitch +
                                           static_cast<uint64_t>(x) * TextureFormatBytesPerTexel(result.format);
                    switch (result.format) {
                    case TextureFormat::Rgba8UNorm:
                    case TextureFormat::Rgba8Srgb: {
                        std::array<float, 4> encoded = value;
                        if (TextureFormatIsSrgb(result.format)) {
                            encoded[0] = LinearToSrgb(encoded[0]);
                            encoded[1] = LinearToSrgb(encoded[1]);
                            encoded[2] = LinearToSrgb(encoded[2]);
                        }
                        for (size_t channel = 0; channel < encoded.size(); ++channel)
                            destination[channel] = QuantizeUNorm(encoded[channel]);
                        break;
                    }
                    case TextureFormat::Rgba4UNormPack16: {
                        const uint16_t packed =
                            static_cast<uint16_t>(std::lround(std::clamp(value[0], 0.0f, 1.0f) * 15.0f)) << 12U |
                            static_cast<uint16_t>(std::lround(std::clamp(value[1], 0.0f, 1.0f) * 15.0f)) << 8U |
                            static_cast<uint16_t>(std::lround(std::clamp(value[2], 0.0f, 1.0f) * 15.0f)) << 4U |
                            static_cast<uint16_t>(std::lround(std::clamp(value[3], 0.0f, 1.0f) * 15.0f));
                        std::memcpy(destination, &packed, sizeof(packed));
                        break;
                    }
                    case TextureFormat::Rgba16UNorm: {
                        std::array<uint16_t, 4> encoded{};
                        for (size_t channel = 0; channel < encoded.size(); ++channel)
                            encoded[channel] =
                                static_cast<uint16_t>(std::lround(std::clamp(value[channel], 0.0f, 1.0f) * 65535.0f));
                        std::memcpy(destination, encoded.data(), sizeof(encoded));
                        break;
                    }
                    case TextureFormat::Rgba16Float: {
                        std::array<uint16_t, 4> encoded{};
                        for (size_t channel = 0; channel < encoded.size(); ++channel)
                            encoded[channel] = glm::packHalf1x16(value[channel]);
                        std::memcpy(destination, encoded.data(), sizeof(encoded));
                        break;
                    }
                    case TextureFormat::Rgba32Float:
                        std::memcpy(destination, value.data(), sizeof(value));
                        break;
                    default:
                        throw std::logic_error("texture target conversion produced a compressed format");
                    }
                }
            }
        }
    }
    return result;
}
} // namespace

std::shared_ptr<const TextureCpuData> TextureProcessor::Process(TextureCpuData source,
                                                                const TextureProcessOptions &options)
{
    if (source.mipLevels.size() != 1 || source.bytes.empty() || TextureFormatIsBlockCompressed(source.format))
        throw std::invalid_argument("TextureProcessor requires one uncompressed base mip");
    (void)CheckedTexelCount(source.mipLevels.front().width, source.mipLevels.front().height,
                            source.mipLevels.front().depth);
    ComputeValueRange(source);
    if (options.generateMipmaps)
        GenerateMipChain(source);
    if (options.targetFormat != TextureTargetFormat::Automatic && options.compression != TextureCompression::None)
        throw std::invalid_argument("explicit texture format requires texture compression to be disabled");
    const TextureCompression compression = ResolveCompression(source, options.compression);
    if (compression != TextureCompression::None)
        source = CompressTexture(source, compression, options.quality);
    else if (options.targetFormat != TextureTargetFormat::Automatic)
        source = ConvertTexture(source, options.targetFormat);
    return std::make_shared<const TextureCpuData>(std::move(source));
}

} // namespace infernux
