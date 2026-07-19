#include <function/resources/InxPointCache/PointCacheArtifact.h>

#include <cassert>
#include <cstdint>
#include <cstring>
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

void WriteU32(std::vector<uint8_t> &bytes, size_t offset, uint32_t value)
{
    for (unsigned shift = 0; shift < 32; shift += 8)
        bytes[offset++] = static_cast<uint8_t>((value >> shift) & 0xffU);
}

void WriteFloat(std::vector<uint8_t> &bytes, size_t offset, float value)
{
    uint32_t bits = 0;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&bits, &value, sizeof(bits));
    WriteU32(bytes, offset, bits);
}
} // namespace

int main()
{
    infernux::PointCacheCpuData source;
    source.stableId = "morph-cache";
    source.name = "Morph Cache";
    source.bakeBasis = "right_handed_y_up";
    source.pointCount = 2;
    source.channels = {
        {"position", infernux::PointCacheChannelType::Float3, infernux::PointCacheChannelSemantic::Position, 0, 12},
        {"stable_id", infernux::PointCacheChannelType::UInt, infernux::PointCacheChannelSemantic::Id, 32, 4},
    };
    source.bytes.resize(40, 0);
    const float positions[6] = {1.0F, 2.0F, 3.0F, -4.0F, 5.5F, 6.0F};
    for (size_t index = 0; index < 6; ++index)
        WriteFloat(source.bytes, index * sizeof(float), positions[index]);
    WriteU32(source.bytes, 32, 7);
    WriteU32(source.bytes, 36, 42);

    constexpr const char *SourceHash = "0123456789abcdef";
    const std::string artifact = infernux::PointCacheArtifact::Serialize(source, SourceHash);
    const auto restored = infernux::PointCacheArtifact::Deserialize(artifact, SourceHash);
    assert(restored.stableId == source.stableId);
    assert(restored.bakeBasis == source.bakeBasis);
    assert(restored.pointCount == 2);
    assert(restored.channels.size() == 2);
    assert(restored.FindChannel("position") != nullptr);
    assert(restored.FindChannel("stable_id")->byteOffset == 32);
    assert(restored.bytes == source.bytes);
    assert(restored.idLookupMode == infernux::PointCacheIdLookupMode::Hash);
    assert(restored.FindPointIndex(7) == 0);
    assert(restored.FindPointIndex(42) == 1);
    assert(restored.FindPointIndex(999) == UINT32_MAX);

    auto identityIds = source;
    WriteU32(identityIds.bytes, 32, 0);
    WriteU32(identityIds.bytes, 36, 1);
    const auto identity = infernux::PointCacheArtifact::Deserialize(
        infernux::PointCacheArtifact::Serialize(identityIds, SourceHash), SourceHash);
    assert(identity.idLookupMode == infernux::PointCacheIdLookupMode::Identity);
    assert(identity.idLookup.empty());
    assert(identity.FindPointIndex(0) == 0);
    assert(identity.FindPointIndex(1) == 1);
    assert(identity.FindPointIndex(2) == UINT32_MAX);

    RequireInvalid([&] { (void)infernux::PointCacheArtifact::Deserialize(artifact, "different-source"); });
    std::string corrupted = artifact;
    corrupted[corrupted.size() / 2] ^= 0x5a;
    RequireInvalid([&] { (void)infernux::PointCacheArtifact::Deserialize(corrupted, SourceHash); });

    auto duplicateIds = source;
    WriteU32(duplicateIds.bytes, 36, 7);
    RequireInvalid([&] { (void)infernux::PointCacheArtifact::Serialize(duplicateIds, SourceHash); });

    auto invalidPosition = source;
    invalidPosition.channels[0].type = infernux::PointCacheChannelType::Float4;
    invalidPosition.channels[0].elementStride = 16;
    RequireInvalid([&] { (void)infernux::PointCacheArtifact::Serialize(invalidPosition, SourceHash); });

    std::cout << "Point cache artifact tests passed\n";
    return 0;
}
