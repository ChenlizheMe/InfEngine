#include <function/resources/InxTexture/TextureArtifact.h>

#include <cassert>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>

namespace
{
template <typename Callback> void RequireInvalid(Callback callback)
{
    bool rejected = false;
    try {
        callback();
    } catch (const std::invalid_argument &) {
        rejected = true;
    }
    assert(rejected);
}

uint64_t Fnv1a64(const std::string &bytes)
{
    uint64_t hash = 14695981039346656037ULL;
    for (const unsigned char byte : bytes) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }
    return hash;
}

void AppendU64(std::string &bytes, uint64_t value)
{
    for (unsigned shift = 0; shift < 64; shift += 8)
        bytes.push_back(static_cast<char>((value >> shift) & 0xffU));
}

void AppendU32(std::string &bytes, uint32_t value)
{
    for (unsigned shift = 0; shift < 32; shift += 8)
        bytes.push_back(static_cast<char>((value >> shift) & 0xffU));
}

std::string MakeVersion1Artifact(const infernux::TextureCpuData &source, const std::string &sourceHash)
{
    std::string bytes = "INXTEX";
    AppendU32(bytes, 1);
    AppendU32(bytes, 0x01020304U);
    AppendU32(bytes, static_cast<uint32_t>(sourceHash.size()));
    bytes.append(sourceHash);
    AppendU32(bytes, source.format == infernux::TextureFormat::Rgba32Float ? 2U : 1U);
    AppendU32(bytes, static_cast<uint32_t>(source.mipLevels.size()));
    for (const auto &mip : source.mipLevels) {
        AppendU32(bytes, mip.width);
        AppendU32(bytes, mip.height);
        AppendU64(bytes, mip.byteSize);
    }
    AppendU64(bytes, source.bytes.size());
    bytes.append(reinterpret_cast<const char *>(source.bytes.data()), source.bytes.size());
    AppendU64(bytes, Fnv1a64(bytes));
    return bytes;
}
} // namespace

int main()
{
    infernux::TextureCpuData source;
    source.dimension = infernux::TextureDimension::Texture2D;
    source.semantic = infernux::TextureSemantic::Color;
    source.format = infernux::TextureFormat::Rgba8Srgb;
    source.mipLevels = {
        {4, 2, 1, 0, 32, 16, 32},
        {2, 1, 1, 32, 8, 8, 8},
        {1, 1, 1, 40, 4, 4, 4},
    };
    source.bytes.resize(44);
    for (size_t index = 0; index < source.bytes.size(); ++index)
        source.bytes[index] = static_cast<uint8_t>((index * 17U) & 0xffU);

    constexpr const char *SourceHash = "0123456789abcdef";
    const std::string bytes = infernux::TextureArtifact::Serialize(source, SourceHash);
    const auto restored = infernux::TextureArtifact::Deserialize(bytes, SourceHash);
    assert(restored->format == infernux::TextureFormat::Rgba8Srgb);
    assert(restored->dimension == infernux::TextureDimension::Texture2D);
    assert(restored->semantic == infernux::TextureSemantic::Color);
    assert(restored->mipLevels.size() == 3);
    assert(restored->mipLevels[0].width == 4);
    assert(restored->mipLevels[1].byteOffset == 32);
    assert(restored->mipLevels[2].byteSize == 4);
    assert(restored->bytes == source.bytes);

    RequireInvalid(
        [&] { (void)infernux::TextureArtifact::Deserialize(MakeVersion1Artifact(source, SourceHash), SourceHash); });

    infernux::TextureCpuData volume;
    volume.dimension = infernux::TextureDimension::Texture3D;
    volume.semantic = infernux::TextureSemantic::VectorField;
    volume.format = infernux::TextureFormat::BC5UNorm;
    volume.valueMin = {-1.0f, -1.0f, -1.0f, 0.0f};
    volume.valueMax = {1.0f, 1.0f, 1.0f, 1.0f};
    volume.mipLevels = {
        {4, 4, 4, 0, 64, 16, 16},
        {2, 2, 2, 64, 32, 16, 16},
        {1, 1, 1, 96, 16, 16, 16},
    };
    volume.bytes.resize(112, 0x5a);
    const std::string volumeBytes = infernux::TextureArtifact::Serialize(volume, SourceHash);
    const auto restoredVolume = infernux::TextureArtifact::Deserialize(volumeBytes, SourceHash);
    assert(restoredVolume->dimension == infernux::TextureDimension::Texture3D);
    assert(restoredVolume->semantic == infernux::TextureSemantic::VectorField);
    assert(restoredVolume->format == infernux::TextureFormat::BC5UNorm);
    assert(restoredVolume->mipLevels[1].depth == 2);
    assert(restoredVolume->valueMin[0] == -1.0f);

    RequireInvalid([&] { (void)infernux::TextureArtifact::Deserialize(bytes, "different-source"); });

    std::string corrupted = bytes;
    corrupted[corrupted.size() / 2] ^= 0x5a;
    RequireInvalid([&] { (void)infernux::TextureArtifact::Deserialize(corrupted, SourceHash); });
    RequireInvalid(
        [&] { (void)infernux::TextureArtifact::Deserialize(bytes.substr(0, bytes.size() - 1), SourceHash); });

    std::string trailing = bytes.substr(0, bytes.size() - sizeof(uint64_t));
    trailing.push_back('\0');
    AppendU64(trailing, Fnv1a64(trailing));
    RequireInvalid([&] { (void)infernux::TextureArtifact::Deserialize(trailing, SourceHash); });

    auto invalidChain = source;
    invalidChain.mipLevels[1].width = 3;
    RequireInvalid([&] { (void)infernux::TextureArtifact::Serialize(invalidChain, SourceHash); });
    RequireInvalid([&] { (void)infernux::TextureArtifact::Serialize(source, {}); });

    std::cout << "Texture artifact tests passed\n";
    return 0;
}
