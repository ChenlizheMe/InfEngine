#include <function/resources/InxTexture/TextureProcessor.h>

#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <vector>

namespace
{
infernux::TextureCpuData MakeRgba8(uint32_t width, uint32_t height, infernux::TextureSemantic semantic,
                                   infernux::TextureFormat format, std::vector<uint8_t> bytes)
{
    infernux::TextureCpuData texture;
    texture.dimension = infernux::TextureDimension::Texture2D;
    texture.semantic = semantic;
    texture.format = format;
    texture.mipLevels = {{width, height, 1, 0, bytes.size(), static_cast<uint64_t>(width) * 4U,
                          static_cast<uint64_t>(width) * height * 4U}};
    texture.bytes = std::move(bytes);
    return texture;
}

infernux::TextureProcessOptions NoCompression(bool mips)
{
    return {mips, infernux::TextureCompression::None, infernux::TextureCompressionQuality::Normal};
}
} // namespace

int main()
{
    {
        auto texture = MakeRgba8(2, 2, infernux::TextureSemantic::Color, infernux::TextureFormat::Rgba8Srgb,
                                 {0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255, 255, 255, 255, 255, 255});
        const auto processed = infernux::TextureProcessor::Process(std::move(texture), NoCompression(true));
        assert(processed->mipLevels.size() == 2);
        const auto &mip = processed->mipLevels[1];
        const uint8_t value = processed->bytes[mip.byteOffset];
        assert(value >= 187 && value <= 189); // 50% linear light encoded as sRGB, not byte-average 128.
    }

    {
        auto texture = MakeRgba8(3, 1, infernux::TextureSemantic::Data, infernux::TextureFormat::Rgba8UNorm,
                                 {0, 0, 0, 255, 0, 0, 0, 255, 255, 0, 0, 255});
        const auto processed = infernux::TextureProcessor::Process(std::move(texture), NoCompression(true));
        assert(processed->mipLevels.size() == 2);
        const auto &mip = processed->mipLevels[1];
        assert(processed->bytes[mip.byteOffset] == 85); // Odd dimensions must include the final source texel.
    }

    {
        const auto encodeNormal = [](float x, float y, float z) {
            return std::vector<uint8_t>{static_cast<uint8_t>(std::lround((x * 0.5f + 0.5f) * 255.0f)),
                                        static_cast<uint8_t>(std::lround((y * 0.5f + 0.5f) * 255.0f)),
                                        static_cast<uint8_t>(std::lround((z * 0.5f + 0.5f) * 255.0f)), 255};
        };
        std::vector<uint8_t> pixels;
        for (int index = 0; index < 2; ++index) {
            const auto normal = encodeNormal(1.0f, 0.0f, 0.0f);
            pixels.insert(pixels.end(), normal.begin(), normal.end());
        }
        for (int index = 0; index < 2; ++index) {
            const auto normal = encodeNormal(0.0f, 1.0f, 0.0f);
            pixels.insert(pixels.end(), normal.begin(), normal.end());
        }
        auto texture =
            MakeRgba8(2, 2, infernux::TextureSemantic::Normal, infernux::TextureFormat::Rgba8UNorm, std::move(pixels));
        const auto processed = infernux::TextureProcessor::Process(std::move(texture), NoCompression(true));
        const uint8_t *normal = processed->bytes.data() + processed->mipLevels[1].byteOffset;
        assert(normal[0] >= 217 && normal[0] <= 219);
        assert(normal[1] >= 217 && normal[1] <= 219);
        assert(normal[2] >= 127 && normal[2] <= 129);
    }

    {
        std::vector<uint8_t> opaque(4U * 4U * 4U, 255U);
        auto texture =
            MakeRgba8(4, 4, infernux::TextureSemantic::Color, infernux::TextureFormat::Rgba8Srgb, std::move(opaque));
        const auto processed =
            infernux::TextureProcessor::Process(std::move(texture), {false, infernux::TextureCompression::Automatic,
                                                                     infernux::TextureCompressionQuality::High});
        assert(processed->format == infernux::TextureFormat::BC1RgbaSrgb);
        assert(processed->bytes.size() == 8);
        assert(processed->mipLevels[0].rowPitch == 8);
    }

    {
        std::vector<uint8_t> alpha(4U * 4U * 4U, 255U);
        alpha[3] = 64U;
        auto texture =
            MakeRgba8(4, 4, infernux::TextureSemantic::Color, infernux::TextureFormat::Rgba8Srgb, std::move(alpha));
        const auto processed =
            infernux::TextureProcessor::Process(std::move(texture), {false, infernux::TextureCompression::Automatic,
                                                                     infernux::TextureCompressionQuality::Normal});
        assert(processed->format == infernux::TextureFormat::BC3Srgb);
        assert(processed->bytes.size() == 16);
    }

    {
        std::vector<uint8_t> normals(4U * 4U * 4U, 128U);
        for (size_t offset = 3; offset < normals.size(); offset += 4)
            normals[offset] = 255U;
        auto texture =
            MakeRgba8(4, 4, infernux::TextureSemantic::Normal, infernux::TextureFormat::Rgba8UNorm, std::move(normals));
        const auto processed =
            infernux::TextureProcessor::Process(std::move(texture), {false, infernux::TextureCompression::Automatic,
                                                                     infernux::TextureCompressionQuality::Normal});
        assert(processed->format == infernux::TextureFormat::BC5UNorm);
        assert(processed->bytes.size() == 16);
    }

    {
        std::vector<float> values(2U * 2U * 2U * 4U, 0.0f);
        for (size_t index = 0; index < values.size() / 4U; ++index) {
            values[index * 4U] = static_cast<float>(index);
            values[index * 4U + 3U] = 1.0f;
        }
        infernux::TextureCpuData volume;
        volume.dimension = infernux::TextureDimension::Texture3D;
        volume.semantic = infernux::TextureSemantic::VectorField;
        volume.format = infernux::TextureFormat::Rgba32Float;
        volume.bytes.resize(values.size() * sizeof(float));
        std::memcpy(volume.bytes.data(), values.data(), volume.bytes.size());
        volume.mipLevels = {{2, 2, 2, 0, volume.bytes.size(), 2U * 4U * sizeof(float), 2U * 2U * 4U * sizeof(float)}};
        const auto processed = infernux::TextureProcessor::Process(std::move(volume), NoCompression(true));
        assert(processed->mipLevels.size() == 2);
        assert(processed->mipLevels[1].depth == 1);
        float average = 0.0f;
        std::memcpy(&average, processed->bytes.data() + processed->mipLevels[1].byteOffset, sizeof(float));
        assert(std::abs(average - 3.5f) < 1.0e-6f);
        assert(processed->valueMin[0] == 0.0f);
        assert(processed->valueMax[0] == 7.0f);
    }

    std::cout << "Texture processor tests passed\n";
    return 0;
}
