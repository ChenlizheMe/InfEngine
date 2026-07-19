#include "TextureDecoder.h"
#include "TextureProcessor.h"
#include "VectorFieldSource.h"

#include <function/resources/InxFileLoader/InxTextureLoader.hpp>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <platform/filesystem/InxPath.h>

#include <stb_image.h>
#include <stb_image_resize2.h>

#include <algorithm>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <vector>

namespace infernux
{
namespace
{
constexpr uint32_t MaximumDimension = 65'536;
constexpr uint64_t MaximumDecodedBytes = 1ULL << 30;

uint32_t ReadMaxSize(const InxResourceMeta &metadata)
{
    const int maxSize = metadata.HasKey("max_size") ? metadata.GetDataAs<int>("max_size") : 2048;
    if (maxSize <= 0 || maxSize > static_cast<int>(MaximumDimension))
        throw std::invalid_argument("texture max_size is outside the supported range");
    return static_cast<uint32_t>(maxSize);
}

bool ReadGenerateMipmaps(const InxResourceMeta &metadata)
{
    return metadata.HasKey("generate_mipmaps") ? metadata.GetDataAs<bool>("generate_mipmaps") : true;
}

TextureCompression ReadCompression(const InxResourceMeta &metadata)
{
    const std::string value =
        metadata.HasKey("texture_compression") ? metadata.GetDataAs<std::string>("texture_compression") : "auto";
    if (value == "none")
        return TextureCompression::None;
    if (value == "auto")
        return TextureCompression::Automatic;
    if (value == "bc1")
        return TextureCompression::BC1;
    if (value == "bc3")
        return TextureCompression::BC3;
    if (value == "bc4")
        return TextureCompression::BC4;
    if (value == "bc5")
        return TextureCompression::BC5;
    throw std::invalid_argument("texture_compression has an unsupported value: " + value);
}

TextureCompressionQuality ReadCompressionQuality(const InxResourceMeta &metadata)
{
    const std::string value = metadata.HasKey("texture_compression_quality")
                                  ? metadata.GetDataAs<std::string>("texture_compression_quality")
                                  : "normal";
    if (value == "fast")
        return TextureCompressionQuality::Fast;
    if (value == "normal")
        return TextureCompressionQuality::Normal;
    if (value == "high")
        return TextureCompressionQuality::High;
    throw std::invalid_argument("texture_compression_quality has an unsupported value: " + value);
}

TextureTargetFormat ReadTargetFormat(const InxResourceMeta &metadata)
{
    const std::string value =
        metadata.HasKey("texture_format") ? metadata.GetDataAs<std::string>("texture_format") : "auto";
    if (value == "auto")
        return TextureTargetFormat::Automatic;
    if (value == "rgba8")
        return TextureTargetFormat::Rgba8;
    if (value == "rgba4444")
        return TextureTargetFormat::Rgba4UNorm;
    if (value == "rgba16_unorm")
        return TextureTargetFormat::Rgba16UNorm;
    if (value == "rgba16_float")
        return TextureTargetFormat::Rgba16Float;
    if (value == "rgba32_float")
        return TextureTargetFormat::Rgba32Float;
    throw std::invalid_argument("texture_format has an unsupported value: " + value);
}

bool ReadSrgb(const InxResourceMeta &metadata)
{
    return metadata.HasKey("srgb") ? metadata.GetDataAs<bool>("srgb") : true;
}

TextureSemantic ReadSemantic(const InxResourceMeta &metadata)
{
    const std::string type =
        metadata.HasKey("texture_type") ? metadata.GetDataAs<std::string>("texture_type") : "default";
    if (type == "default" || type.empty())
        return TextureSemantic::Color;
    if (type == "normal_map")
        return TextureSemantic::Normal;
    if (type == "data")
        return TextureSemantic::Data;
    if (type == "ui")
        return TextureSemantic::UserInterface;
    if (type == "sprite")
        return TextureSemantic::Sprite;
    if (type == "vector_field")
        return TextureSemantic::VectorField;
    if (type == "sdf")
        return TextureSemantic::SignedDistanceField;
    throw std::invalid_argument("texture_type has an unsupported value: " + type);
}

std::vector<unsigned char> ReadSourceBytes(const std::string &path)
{
    std::ifstream file(ToFsPath(path), std::ios::binary | std::ios::ate);
    if (!file.is_open())
        throw std::runtime_error("failed to open texture source: " + path);
    const auto size = file.tellg();
    if (size <= 0 || static_cast<uint64_t>(size) > static_cast<uint64_t>(std::numeric_limits<int>::max()))
        throw std::runtime_error("texture source is empty or exceeds decoder limits: " + path);
    std::vector<unsigned char> bytes(static_cast<size_t>(size));
    file.seekg(0);
    file.read(reinterpret_cast<char *>(bytes.data()), size);
    if (!file)
        throw std::runtime_error("failed to read texture source: " + path);
    return bytes;
}

uint64_t LevelByteSize(uint32_t width, uint32_t height, TextureFormat format)
{
    const uint64_t bytesPerPixel = TextureFormatBytesPerTexel(format);
    if (bytesPerPixel == 0)
        throw std::invalid_argument("decoded texture requires an uncompressed working format");
    const uint64_t pixels = static_cast<uint64_t>(width) * height;
    if (pixels == 0 || pixels > MaximumDecodedBytes / bytesPerPixel)
        throw std::overflow_error("decoded texture exceeds the CPU artifact size limit");
    return pixels * bytesPerPixel;
}

void AppendLevel(TextureCpuData &texture, uint32_t width, uint32_t height, const void *pixels, uint64_t byteSize)
{
    if (!pixels || byteSize != LevelByteSize(width, height, texture.format) ||
        byteSize > MaximumDecodedBytes - texture.bytes.size())
        throw std::invalid_argument("decoded texture level has an invalid payload");
    TextureMipLevel level;
    level.width = width;
    level.height = height;
    level.depth = 1;
    level.byteOffset = texture.bytes.size();
    level.byteSize = byteSize;
    level.rowPitch = static_cast<uint64_t>(width) * TextureFormatBytesPerTexel(texture.format);
    level.slicePitch = level.rowPitch * height;
    const auto *begin = static_cast<const uint8_t *>(pixels);
    texture.bytes.insert(texture.bytes.end(), begin, begin + static_cast<size_t>(byteSize));
    texture.mipLevels.push_back(level);
}

} // namespace

std::shared_ptr<const TextureCpuData> TextureDecoder::Decode(const std::string &sourcePath,
                                                             const InxResourceMeta &metadata)
{
    const auto source = ReadSourceBytes(sourcePath);
    const uint32_t maxSize = ReadMaxSize(metadata);

    std::string extension = FromFsPath(ToFsPath(sourcePath).extension());
    std::transform(extension.begin(), extension.end(), extension.begin(), ::tolower);
    if (extension == ".inxvfield") {
        TextureCpuData volume =
            VectorFieldSource::Decode(std::string_view(reinterpret_cast<const char *>(source.data()), source.size()));
        const auto &base = volume.mipLevels.front();
        if (base.width > maxSize || base.height > maxSize || base.depth > maxSize)
            throw std::invalid_argument("vector field dimensions exceed texture max_size");
        return TextureProcessor::Process(
            std::move(volume), TextureProcessOptions{ReadGenerateMipmaps(metadata), ReadCompression(metadata),
                                                     ReadCompressionQuality(metadata), ReadTargetFormat(metadata)});
    }

    int sourceWidth = 0;
    int sourceHeight = 0;
    int sourceChannels = 0;

    auto texture = std::make_shared<TextureCpuData>();
    texture->dimension = TextureDimension::Texture2D;
    texture->semantic = ReadSemantic(metadata);
    if (texture->semantic == TextureSemantic::VectorField)
        throw std::invalid_argument("VectorField textures must use the .inxvfield source format");
    if (stbi_is_hdr_from_memory(source.data(), static_cast<int>(source.size())) != 0) {
        texture->format = TextureFormat::Rgba32Float;
        float *decoded = stbi_loadf_from_memory(source.data(), static_cast<int>(source.size()), &sourceWidth,
                                                &sourceHeight, &sourceChannels, STBI_rgb_alpha);
        if (!decoded)
            throw std::runtime_error("failed to decode HDR texture: " + sourcePath);
        auto release = std::unique_ptr<float, decltype(&stbi_image_free)>(decoded, &stbi_image_free);
        const float scale = static_cast<float>(maxSize) / static_cast<float>((std::max)(sourceWidth, sourceHeight));
        const uint32_t width = scale < 1.0f ? (std::max)(1U, static_cast<uint32_t>(sourceWidth * scale))
                                            : static_cast<uint32_t>(sourceWidth);
        const uint32_t height = scale < 1.0f ? (std::max)(1U, static_cast<uint32_t>(sourceHeight * scale))
                                             : static_cast<uint32_t>(sourceHeight);
        if (width != static_cast<uint32_t>(sourceWidth) || height != static_cast<uint32_t>(sourceHeight)) {
            std::vector<float> resized(static_cast<size_t>(width) * height * 4);
            if (!stbir_resize_float_linear(decoded, sourceWidth, sourceHeight, 0, resized.data(),
                                           static_cast<int>(width), static_cast<int>(height), 0, STBIR_RGBA))
                throw std::runtime_error("failed to resize HDR texture: " + sourcePath);
            AppendLevel(*texture, width, height, resized.data(), resized.size() * sizeof(float));
        } else {
            AppendLevel(*texture, width, height, decoded, static_cast<uint64_t>(width) * height * 4 * sizeof(float));
        }
    } else {
        texture->format = ReadSrgb(metadata) ? TextureFormat::Rgba8Srgb : TextureFormat::Rgba8UNorm;
        stbi_uc *decoded = stbi_load_from_memory(source.data(), static_cast<int>(source.size()), &sourceWidth,
                                                 &sourceHeight, &sourceChannels, STBI_rgb_alpha);
        std::vector<unsigned char> pnmPixels;
        if (!decoded) {
            InxTextureData pnm = InxTextureLoader::LoadFromMemory(source.data(), source.size(), sourcePath);
            if (!pnm.IsValid())
                throw std::runtime_error("failed to decode texture: " + sourcePath);
            sourceWidth = pnm.width;
            sourceHeight = pnm.height;
            pnmPixels = std::move(pnm.pixels);
        }
        auto release = std::unique_ptr<stbi_uc, decltype(&stbi_image_free)>(decoded, &stbi_image_free);
        const unsigned char *base = decoded ? decoded : pnmPixels.data();
        const float scale = static_cast<float>(maxSize) / static_cast<float>((std::max)(sourceWidth, sourceHeight));
        const uint32_t width = scale < 1.0f ? (std::max)(1U, static_cast<uint32_t>(sourceWidth * scale))
                                            : static_cast<uint32_t>(sourceWidth);
        const uint32_t height = scale < 1.0f ? (std::max)(1U, static_cast<uint32_t>(sourceHeight * scale))
                                             : static_cast<uint32_t>(sourceHeight);
        if (width != static_cast<uint32_t>(sourceWidth) || height != static_cast<uint32_t>(sourceHeight)) {
            std::vector<uint8_t> resized(static_cast<size_t>(width) * height * 4);
            const bool colorSrgb =
                TextureFormatIsSrgb(texture->format) &&
                (texture->semantic == TextureSemantic::Color || texture->semantic == TextureSemantic::UserInterface ||
                 texture->semantic == TextureSemantic::Sprite);
            const bool resizedOk = colorSrgb
                                       ? stbir_resize_uint8_srgb(base, sourceWidth, sourceHeight, 0, resized.data(),
                                                                 static_cast<int>(width), static_cast<int>(height), 0,
                                                                 STBIR_RGBA) != nullptr
                                       : stbir_resize_uint8_linear(base, sourceWidth, sourceHeight, 0, resized.data(),
                                                                   static_cast<int>(width), static_cast<int>(height), 0,
                                                                   STBIR_RGBA) != nullptr;
            if (!resizedOk)
                throw std::runtime_error("failed to resize texture: " + sourcePath);
            AppendLevel(*texture, width, height, resized.data(), resized.size());
        } else {
            AppendLevel(*texture, width, height, base, static_cast<uint64_t>(width) * height * 4);
        }
    }

    return TextureProcessor::Process(
        std::move(*texture), TextureProcessOptions{ReadGenerateMipmaps(metadata), ReadCompression(metadata),
                                                   ReadCompressionQuality(metadata), ReadTargetFormat(metadata)});
}

std::shared_ptr<const TextureCpuData> TextureDecoder::CreateRgba8(const uint8_t *pixels, size_t byteCount,
                                                                  uint32_t width, uint32_t height, bool generateMipmaps)
{
    if (!pixels)
        throw std::invalid_argument("RGBA8 texture payload has no pixels");
    const uint64_t expectedSize = LevelByteSize(width, height, TextureFormat::Rgba8UNorm);
    if (expectedSize != byteCount)
        throw std::invalid_argument("RGBA8 texture payload byte count does not match its dimensions");

    auto texture = std::make_shared<TextureCpuData>();
    texture->dimension = TextureDimension::Texture2D;
    texture->semantic = TextureSemantic::Data;
    texture->format = TextureFormat::Rgba8UNorm;
    AppendLevel(*texture, width, height, pixels, expectedSize);
    return TextureProcessor::Process(
        std::move(*texture),
        TextureProcessOptions{generateMipmaps, TextureCompression::None, TextureCompressionQuality::Normal});
}

} // namespace infernux
