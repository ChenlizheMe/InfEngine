#include <function/resources/InxSkinnedMesh/InxSkinnedMesh.h>
#include <function/resources/InxSkinnedMesh/SkinnedMeshArtifact.h>
#include <function/scene/SkinPoseHistory.h>

#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <stdexcept>

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

uint64_t Fnv1a64(std::string_view bytes)
{
    uint64_t hash = 14695981039346656037ULL;
    for (const unsigned char byte : bytes) {
        hash ^= byte;
        hash *= 1099511628211ULL;
    }
    return hash;
}

void RewriteChecksum(std::string &bytes)
{
    assert(bytes.size() >= sizeof(uint64_t));
    const size_t offset = bytes.size() - sizeof(uint64_t);
    const uint64_t checksum = Fnv1a64(std::string_view(bytes).substr(0, offset));
    for (unsigned int shift = 0; shift < 64; shift += 8)
        bytes[offset + shift / 8] = static_cast<char>((checksum >> shift) & 0xffU);
}

bool NearlyEqual(float left, float right)
{
    return std::abs(left - right) < 1.0e-6f;
}
} // namespace

int main()
{
    infernux::InxSkinnedMesh source;
    source.scaleFactor = 0.01f;

    infernux::Vertex vertex;
    vertex.pos = {1.0f, 2.0f, 3.0f};
    vertex.normal = {0.0f, 1.0f, 0.0f};
    vertex.tangent = {1.0f, 0.0f, 0.0f, 1.0f};
    vertex.color = {0.2f, 0.4f, 0.6f};
    vertex.texCoord = {0.25f, 0.75f};
    source.baseVertices.push_back(vertex);

    infernux::SkinInfluence influence;
    influence.boneIndex[0] = 0;
    influence.weight[0] = 1.0f;
    source.influences.push_back(influence);
    source.indices = {0, 0, 0};

    infernux::SubMesh subMesh;
    subMesh.indexCount = 3;
    subMesh.vertexCount = 1;
    subMesh.boundsMin = vertex.pos;
    subMesh.boundsMax = vertex.pos;
    subMesh.name = "body";
    source.subMeshes.push_back(subMesh);

    infernux::SkinnedRuntimeNode node;
    node.name = "Root";
    source.nodeByName.emplace(node.name, 0);
    source.nodes.push_back(node);

    infernux::SkinnedRuntimeBone bone;
    bone.name = "Root";
    bone.nodeIndex = 0;
    source.boneByName.emplace(bone.name, 0);
    source.bones.push_back(bone);

    infernux::SkinnedRuntimeTrack track;
    track.nodeName = "Root";
    track.positions = {{0.0, {0.0f, 0.0f, 0.0f}}, {10.0, {2.0f, 0.0f, 0.0f}}};
    track.rotations = {{0.0, glm::quat(1.0f, 0.0f, 0.0f, 0.0f)}, {10.0, glm::quat(1.0f, 0.0f, 0.0f, 0.0f)}};
    track.scales = {{0.0, {1.0f, 1.0f, 1.0f}}, {10.0, {1.0f, 1.0f, 1.0f}}};
    infernux::SkinnedRuntimeAnimation animation;
    animation.name = "Move";
    animation.durationTicks = 10.0;
    animation.ticksPerSecond = 20.0;
    animation.trackByNode.emplace(track.nodeName, 0);
    animation.tracks.push_back(track);
    source.animations.push_back(animation);
    source.NormalizeInfluences();

    constexpr std::string_view SourceHash = "0123456789abcdef";
    const std::string bytes = infernux::SkinnedMeshArtifact::Serialize(source, SourceHash);
    auto restored = infernux::SkinnedMeshArtifact::Deserialize(bytes, SourceHash);
    assert(restored);
    assert(restored->baseVertices.size() == 1);
    assert(restored->indices == std::vector<uint32_t>({0, 0, 0}));
    assert(restored->nodes.size() == 1 && restored->nodeByName.at("Root") == 0);
    assert(restored->bones.size() == 1 && restored->boneByName.at("Root") == 0);
    assert(restored->animations.size() == 1);
    assert(restored->animations.front().trackByNode.at("Root") == 0);
    assert(NearlyEqual(restored->animations.front().DurationSeconds(), 0.5f));
    assert(restored->BuildGpuBonePalette({"Move", 0.25f}).size() == 1);
    assert(restored->GetRuntimeMemoryBytes() > sizeof(infernux::InxSkinnedMesh));

    infernux::SkinPoseHistory poseHistory;
    auto firstPose = restored->GetOrBuildGpuBonePalette({"Move", 0.0f});
    poseHistory.Publish(firstPose, true);
    const auto firstSnapshot = poseHistory.Acquire();
    assert(firstSnapshot && firstSnapshot->IsValid());
    assert(firstSnapshot->current == firstPose);
    assert(firstSnapshot->previous == firstPose);

    auto secondPose = restored->GetOrBuildGpuBonePalette({"Move", 0.25f});
    poseHistory.Publish(secondPose, false);
    const auto secondSnapshot = poseHistory.Acquire();
    assert(secondSnapshot && secondSnapshot->IsValid());
    assert(secondSnapshot->current == secondPose);
    assert(secondSnapshot->previous == firstPose);
    assert(secondSnapshot->revision > firstSnapshot->revision);

    auto differentSkeleton = std::make_shared<const infernux::SkinPoseHistory::Palette>(2, glm::mat4(1.0f));
    poseHistory.Publish(differentSkeleton, false);
    const auto resizedSnapshot = poseHistory.Acquire();
    assert(resizedSnapshot && resizedSnapshot->IsValid());
    assert(resizedSnapshot->current == differentSkeleton);
    assert(resizedSnapshot->previous == differentSkeleton);
    assert(resizedSnapshot->revision > secondSnapshot->revision);

    poseHistory.Reset();
    const auto resetSnapshot = poseHistory.Acquire();
    assert(resetSnapshot && !resetSnapshot->IsValid());
    assert(!resetSnapshot->current);
    assert(!resetSnapshot->previous);
    assert(resetSnapshot->revision > resizedSnapshot->revision);

    const std::string empty = infernux::SkinnedMeshArtifact::SerializeEmpty(SourceHash);
    assert(!infernux::SkinnedMeshArtifact::Deserialize(empty, SourceHash));

    RequireInvalid([&] { (void)infernux::SkinnedMeshArtifact::Deserialize(bytes, "different-source"); });
    std::string corrupted = bytes;
    corrupted[corrupted.size() / 2] ^= 0x5a;
    RequireInvalid([&] { (void)infernux::SkinnedMeshArtifact::Deserialize(corrupted, SourceHash); });
    RequireInvalid(
        [&] { (void)infernux::SkinnedMeshArtifact::Deserialize(bytes.substr(0, bytes.size() - 1), SourceHash); });

    std::string trailing = bytes;
    trailing.insert(trailing.end() - static_cast<std::ptrdiff_t>(sizeof(uint64_t)), 'x');
    RewriteChecksum(trailing);
    RequireInvalid([&] { (void)infernux::SkinnedMeshArtifact::Deserialize(trailing, SourceHash); });

    source.influences.front().weight[0] = 0.5f;
    RequireInvalid([&] { (void)infernux::SkinnedMeshArtifact::Serialize(source, SourceHash); });

    std::cout << "Skinned Mesh artifact tests passed\n";
    return 0;
}
