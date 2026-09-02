#include "InxTextureLoader.hpp"

#include <algorithm>
#include <cctype>
#include <core/log/InxLog.h>
#include <cstdlib>
#include <filesystem>
#include <platform/filesystem/InxPath.h>
#include <stb_image.h>
#include <vector>

namespace infernux
{
namespace
{

void SkipPnmWhitespaceAndComments(const unsigned char *data, size_t dataSize, size_t &pos)
{
    while (pos < dataSize) {
        while (pos < dataSize && std::isspace(static_cast<unsigned char>(data[pos]))) {
            ++pos;
        }
        if (pos < dataSize && data[pos] == '#') {
            while (pos < dataSize && data[pos] != '\n' && data[pos] != '\r') {
                ++pos;
            }
            continue;
        }
        break;
    }
}

bool ReadPnmToken(const unsigned char *data, size_t dataSize, size_t &pos, std::string &out)
{
    SkipPnmWhitespaceAndComments(data, dataSize, pos);
    if (pos >= dataSize) {
        return false;
    }
    const size_t start = pos;
    while (pos < dataSize && !std::isspace(static_cast<unsigned char>(data[pos])) && data[pos] != '#') {
        ++pos;
    }
    out.assign(reinterpret_cast<const char *>(data + start), pos - start);
    return !out.empty();
}

bool ReadPnmInt(const unsigned char *data, size_t dataSize, size_t &pos, int &out)
{
    std::string token;
    if (!ReadPnmToken(data, dataSize, pos, token)) {
        return false;
    }
    char *end = nullptr;
    long value = std::strtol(token.c_str(), &end, 10);
    if (end == token.c_str() || *end != '\0' || value < 0 || value > 65535) {
        return false;
    }
    out = static_cast<int>(value);
    return true;
}

unsigned char ScalePnmSample(int value, int maxValue)
{
    if (maxValue <= 0) {
        return 0;
    }
    value = std::clamp(value, 0, maxValue);
    return static_cast<unsigned char>((value * 255 + maxValue / 2) / maxValue);
}

bool DecodePnmToRgba(const unsigned char *data, size_t dataSize, InxTextureData &result)
{
    if (!data || dataSize < 3 || data[0] != 'P') {
        return false;
    }

    const unsigned char magic = data[1];
    const bool asciiRgb = magic == '3';
    const bool asciiGray = magic == '2';
    const bool binaryRgb = magic == '6';
    const bool binaryGray = magic == '5';
    if (!asciiRgb && !asciiGray && !binaryRgb && !binaryGray) {
        return false;
    }

    size_t pos = 2;
    int width = 0;
    int height = 0;
    int maxValue = 0;
    if (!ReadPnmInt(data, dataSize, pos, width) || !ReadPnmInt(data, dataSize, pos, height) ||
        !ReadPnmInt(data, dataSize, pos, maxValue)) {
        return false;
    }
    if (width <= 0 || height <= 0 || maxValue <= 0 || maxValue > 65535) {
        return false;
    }

    const size_t pixelCount = static_cast<size_t>(width) * static_cast<size_t>(height);
    result.width = width;
    result.height = height;
    result.channels = 4;
    result.pixels.clear();
    result.pixels.resize(pixelCount * 4);

    if (asciiRgb || asciiGray) {
        for (size_t i = 0; i < pixelCount; ++i) {
            int r = 0;
            int g = 0;
            int b = 0;
            if (asciiRgb) {
                if (!ReadPnmInt(data, dataSize, pos, r) || !ReadPnmInt(data, dataSize, pos, g) ||
                    !ReadPnmInt(data, dataSize, pos, b)) {
                    return false;
                }
            } else {
                if (!ReadPnmInt(data, dataSize, pos, r)) {
                    return false;
                }
                g = r;
                b = r;
            }
            const size_t out = i * 4;
            result.pixels[out + 0] = ScalePnmSample(r, maxValue);
            result.pixels[out + 1] = ScalePnmSample(g, maxValue);
            result.pixels[out + 2] = ScalePnmSample(b, maxValue);
            result.pixels[out + 3] = 255;
        }
        return true;
    }

    // Binary P5/P6: one whitespace byte separates the header from pixel bytes.
    SkipPnmWhitespaceAndComments(data, dataSize, pos);
    const int srcChannels = binaryRgb ? 3 : 1;
    const int bytesPerSample = maxValue > 255 ? 2 : 1;
    const size_t required = pixelCount * static_cast<size_t>(srcChannels) * static_cast<size_t>(bytesPerSample);
    if (pos + required > dataSize) {
        return false;
    }

    for (size_t i = 0; i < pixelCount; ++i) {
        int samples[3] = {0, 0, 0};
        for (int c = 0; c < srcChannels; ++c) {
            if (bytesPerSample == 1) {
                samples[c] = data[pos++];
            } else {
                samples[c] = (static_cast<int>(data[pos]) << 8) | static_cast<int>(data[pos + 1]);
                pos += 2;
            }
        }
        if (srcChannels == 1) {
            samples[1] = samples[0];
            samples[2] = samples[0];
        }
        const size_t out = i * 4;
        result.pixels[out + 0] = ScalePnmSample(samples[0], maxValue);
        result.pixels[out + 1] = ScalePnmSample(samples[1], maxValue);
        result.pixels[out + 2] = ScalePnmSample(samples[2], maxValue);
        result.pixels[out + 3] = 255;
    }
    return true;
}

} // namespace

InxTextureData InxTextureLoader::LoadFromFile(const std::string &filePath, const std::string &name)
{
    InxTextureData result;
    result.sourcePath = filePath;
    result.name = name.empty() ? FromFsPath(ToFsPath(filePath).stem()) : name;

    int width, height, channels;
    // Read file bytes first to support Unicode paths on Windows
    std::vector<unsigned char> fileBytes;
    if (!ReadFileBytes(filePath, fileBytes) || fileBytes.empty()) {
        INXLOG_ERROR("Failed to read texture file: ", filePath);
        return result;
    }
    stbi_uc *pixels = stbi_load_from_memory(fileBytes.data(), static_cast<int>(fileBytes.size()), &width, &height,
                                            &channels, STBI_rgb_alpha);

    if (!pixels) {
        if (DecodePnmToRgba(fileBytes.data(), fileBytes.size(), result)) {
            return result;
        }
        INXLOG_ERROR("stbi_load failed for: ", filePath, " - ", stbi_failure_reason());
        return result;
    }

    result.width = width;
    result.height = height;
    result.channels = 4; // Always RGBA
    size_t dataSize = static_cast<size_t>(width) * height * 4;
    result.pixels.assign(pixels, pixels + dataSize);

    stbi_image_free(pixels);

    return result;
}

InxTextureData InxTextureLoader::LoadFromMemory(const unsigned char *data, size_t dataSize, const std::string &name)
{
    InxTextureData result;
    result.name = name;

    int width, height, channels;
    stbi_uc *pixels =
        stbi_load_from_memory(data, static_cast<int>(dataSize), &width, &height, &channels, STBI_rgb_alpha);

    if (!pixels) {
        if (DecodePnmToRgba(data, dataSize, result)) {
            return result;
        }
        INXLOG_ERROR("stbi_load_from_memory failed: ", stbi_failure_reason());
        return result;
    }

    result.width = width;
    result.height = height;
    result.channels = 4;
    size_t pixelDataSize = static_cast<size_t>(width) * height * 4;
    result.pixels.assign(pixels, pixels + pixelDataSize);

    stbi_image_free(pixels);

    return result;
}

InxTextureData InxTextureLoader::CreateSolidColor(int width, int height, unsigned char r, unsigned char g,
                                                  unsigned char b, unsigned char a, const std::string &name)
{
    InxTextureData result;
    result.name = name;
    result.width = width;
    result.height = height;
    result.channels = 4;
    result.pixels.resize(static_cast<size_t>(width) * height * 4);

    for (int i = 0; i < width * height; ++i) {
        result.pixels[i * 4 + 0] = r;
        result.pixels[i * 4 + 1] = g;
        result.pixels[i * 4 + 2] = b;
        result.pixels[i * 4 + 3] = a;
    }

    return result;
}

InxTextureData InxTextureLoader::CreateCheckerboard(int width, int height, int checkerSize, const std::string &name)
{
    InxTextureData result;
    result.name = name;
    result.width = width;
    result.height = height;
    result.channels = 4;
    result.pixels.resize(static_cast<size_t>(width) * height * 4);

    // Magenta and black checkerboard (classic "missing texture" pattern)
    const unsigned char color1[4] = {255, 0, 255, 255}; // Magenta
    const unsigned char color2[4] = {0, 0, 0, 255};     // Black

    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            int checkerX = x / checkerSize;
            int checkerY = y / checkerSize;
            const unsigned char *color = ((checkerX + checkerY) % 2 == 0) ? color1 : color2;

            int idx = (y * width + x) * 4;
            result.pixels[idx + 0] = color[0];
            result.pixels[idx + 1] = color[1];
            result.pixels[idx + 2] = color[2];
            result.pixels[idx + 3] = color[3];
        }
    }

    return result;
}

} // namespace infernux
