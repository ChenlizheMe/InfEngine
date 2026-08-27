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
    source.skeleton.nodeByName.emplace(node.name, 0);
    source.skeleton.nodes.push_back(node);

    infernux::SkinnedRuntimeBone bone;
    bone.name = "Root";
    bone.nodeIndex = 0;
    source.skeleton.boneByName.emplace(bone.name, 0);
    source.skeleton.bones.push_back(bone);

    infernux::SkinnedRuntimeTrack track;
    track.nodeIndex = 0;
    track.positions = {{0.0, {0.0f, 0.0f, 0.0f}}, {10.0, {2.0f, 0.0f, 0.0f}}};
    track.rotations = {{0.0, glm::quat(1.0f, 0.0f, 0.0f, 0.0f)}, {10.0, glm::quat(1.0f, 0.0f, 0.0f, 0.0f)}};
    track.scales = {{0.0, {1.0f, 1.0f, 1.0f}}, {10.0, {1.0f, 1.0f, 1.0f}}};
    infernux::SkinnedRuntimeAnimation animation;
    animation.name = "Move";
    animation.durationTicks = 10.0;
    animation.ticksPerSecond = 20.0;
    animation.trackByNodeIndex = {0};
    animation.tracks.push_back(track);
    source.animations.push_back(animation);
    source.NormalizeInfluences();

    constexpr std::string_view SourceHash = "0123456789abcdef";
    const std::string bytes = infernux::SkinnedMeshArtifact::Serialize(source, SourceHash);
    auto restored = infernux::SkinnedMeshArtifact::Deserialize(bytes, SourceHash);
    assert(restored);
    assert(restored->baseVertices.size() == 1);
    assert(restored->indices == std::vector<uint32_t>({0, 0, 0}));
    assert(restored->skeleton.nodes.size() == 1 && restored->skeleton.nodeByName.at("Root") == 0);
    assert(restored->skeleton.bones.size() == 1 && restored->skeleton.boneByName.at("Root") == 0);
    assert(restored->animations.size() == 1);
    assert(restored->animations.front().trackByNodeIndex.at(0) == 0);
    assert(NearlyEqual(restored->animations.front().DurationSeconds(), 0.5f));
    assert(restored->BuildGpuBonePalette({"Move", 0.25f}).size() == 1);
    assert(restored->GetRuntimeMemoryBytes() > sizeof(infernux::InxSkinnedMesh));

    // A separate animation-only model with the same skeleton must drive the
    // render model. This is the standard Mixamo "With Skin" model + "Without
    // Skin" animation workflow.
    infernux::InxSkinnedMesh animationOnly;
    animationOnly.guid = "animation-source";
    animationOnly.skeleton = source.skeleton;
    animationOnly.skeleton.bones.clear();
    animationOnly.skeleton.boneByName.clear();
    infernux::SkinnedRuntimeAnimation externalAnimation = animation;
    externalAnimation.name = "ExternalMove";
    externalAnimation.tracks.front().positions.back().second = {4.0f, 0.0f, 0.0f};
    animationOnly.animations.push_back(externalAnimation);
    assert(!animationOnly.IsValid());
    assert(animationOnly.IsAnimationSource());
    std::string compatibilityReason;
    assert(source.skeleton.IsAnimationCompatible(animationOnly.skeleton, externalAnimation, &compatibilityReason));
    assert(compatibilityReason.empty());
    const auto externalPalette = restored->BuildGpuBonePalette({"ExternalMove", 0.25f}, &animationOnly);
    assert(externalPalette.size() == 1);
    assert(NearlyEqual(externalPalette.front()[3].x, 0.02f));
    glm::vec3 animatedBoundsMin;
    glm::vec3 animatedBoundsMax;
    assert(restored->ComputeSkinnedBounds(externalPalette, animatedBoundsMin, animatedBoundsMax));
    assert(NearlyEqual(animatedBoundsMin.x, 0.03f) && NearlyEqual(animatedBoundsMax.x, 0.03f));
    assert(NearlyEqual(animatedBoundsMin.y, 0.02f) && NearlyEqual(animatedBoundsMax.y, 0.02f));
    assert(NearlyEqual(animatedBoundsMin.z, 0.03f) && NearlyEqual(animatedBoundsMax.z, 0.03f));

    const std::string animationBytes = infernux::SkinnedMeshArtifact::Serialize(animationOnly, SourceHash);
    const auto restoredAnimation = infernux::SkinnedMeshArtifact::Deserialize(animationBytes, SourceHash);
    assert(restoredAnimation && restoredAnimation->IsAnimationSource() && !restoredAnimation->IsValid());
    assert(restoredAnimation->animations.front().name == "ExternalMove");

    // An entirely renamed rig is not guessed from geometry. Symmetric limbs
    // and fingers require an explicit Avatar/Rig-style mapping.
    infernux::Skeleton renamedSkeleton = animationOnly.skeleton;
    renamedSkeleton.nodeByName.clear();
    renamedSkeleton.nodes.front().name = "CompletelyDifferentJointName";
    renamedSkeleton.nodeByName.emplace(renamedSkeleton.nodes.front().name, 0);
    infernux::SkinnedRuntimeAnimation renamedAnimation = externalAnimation;
    assert(!source.skeleton.IsAnimationCompatible(renamedSkeleton, renamedAnimation, &compatibilityReason));
    assert(compatibilityReason.find("exactly named target joint") != std::string::npos);

    // Exact joint identity wins over geometric guessing. Symmetric branches
    // cannot be safely disambiguated by bind-pose position when exporters use
    // different coordinate systems or proportions.
    infernux::Skeleton targetFork;
    infernux::Skeleton sourceFork;
    for (int index = 0; index < 3; ++index) {
        infernux::SkinnedRuntimeNode targetNode;
        infernux::SkinnedRuntimeNode sourceNode;
        targetNode.parent = sourceNode.parent = index == 0 ? -1 : 0;
        targetNode.name = index == 0 ? "TargetRoot" : (index == 1 ? "Left" : "Right");
        sourceNode.name = index == 0 ? "SourceRoot" : (index == 1 ? "Right" : "Left");
        if (index > 0) {
            const float x = index == 1 ? -1.0f : 1.0f;
            targetNode.bindLocal[3] = sourceNode.bindLocal[3] = glm::vec4(x, 1.0f, 0.0f, 1.0f);
            targetNode.bindGlobal = targetNode.bindLocal;
            sourceNode.bindGlobal = sourceNode.bindLocal;
        }
        targetFork.nodeByName.emplace(targetNode.name, index);
        sourceFork.nodeByName.emplace(sourceNode.name, index);
        targetFork.nodes.push_back(targetNode);
        sourceFork.nodes.push_back(sourceNode);
    }
    infernux::SkinnedRuntimeAnimation forkAnimation;
    forkAnimation.name = "Fork";
    forkAnimation.durationTicks = 1.0;
    forkAnimation.ticksPerSecond = 1.0;
    infernux::SkinnedRuntimeTrack forkTrack;
    forkTrack.nodeIndex = 1;
    forkTrack.rotations = {{0.0, glm::quat(1.0f, 0.0f, 0.0f, 0.0f)}};
    forkAnimation.tracks.push_back(forkTrack);
    forkAnimation.trackByNodeIndex = {-1, 0, -1};
    const infernux::SkeletonRetargetMap forkMap = targetFork.BuildRetargetMap(sourceFork, forkAnimation);
    assert(forkMap.targetToSourceNode == std::vector<int>({-1, 2, 1}));
    assert(forkMap.exactNameMatches == 2);
    assert(forkMap.mappedAnimatedNodes == 1);
    assert(!forkMap.identicalTopology);
    assert(!targetFork.IsAnimationCompatible(sourceFork, forkAnimation, &compatibilityReason));
    assert(compatibilityReason.find("same mapped hierarchy") != std::string::npos);

    // Assimp expands FBX pivots into helper nodes in animation files. The
    // helper's channel must drive an exactly named deform joint even though
    // the render-model hierarchy does not contain that helper node.
    infernux::InxSkinnedMesh pivotTarget;
    pivotTarget.scaleFactor = 1.0f;
    infernux::SkinnedRuntimeNode targetRig;
    targetRig.name = "Rig";
    pivotTarget.skeleton.nodeByName.emplace(targetRig.name, 0);
    pivotTarget.skeleton.nodes.push_back(targetRig);
    infernux::SkinnedRuntimeNode targetJoint;
    targetJoint.name = "Joint";
    targetJoint.parent = 0;
    targetJoint.bindLocal[3] = glm::vec4(0.0f, 1.0f, 0.0f, 1.0f);
    targetJoint.bindGlobal = targetJoint.bindLocal;
    pivotTarget.skeleton.nodeByName.emplace(targetJoint.name, 1);
    pivotTarget.skeleton.nodes.push_back(targetJoint);
    infernux::SkinnedRuntimeBone targetBone;
    targetBone.name = targetJoint.name;
    targetBone.nodeIndex = 1;
    targetBone.inverseBind = glm::inverse(targetJoint.bindGlobal);
    pivotTarget.skeleton.boneByName.emplace(targetBone.name, 0);
    pivotTarget.skeleton.bones.push_back(targetBone);

    infernux::InxSkinnedMesh pivotSource;
    pivotSource.scaleFactor = 1.0f;
    pivotSource.skeleton.nodeByName.emplace(targetRig.name, 0);
    pivotSource.skeleton.nodes.push_back(targetRig);
    infernux::SkinnedRuntimeNode pivotHelper;
    pivotHelper.name = "Joint_$AssimpFbx$_Rotation";
    pivotHelper.parent = 0;
    pivotSource.skeleton.nodeByName.emplace(pivotHelper.name, 1);
    pivotSource.skeleton.nodes.push_back(pivotHelper);
    infernux::SkinnedRuntimeNode sourceJoint = targetJoint;
    sourceJoint.parent = 1;
    pivotSource.skeleton.nodeByName.emplace(sourceJoint.name, 2);
    pivotSource.skeleton.nodes.push_back(sourceJoint);
    infernux::SkinnedRuntimeAnimation pivotAnimation;
    pivotAnimation.name = "PivotRotation";
    pivotAnimation.durationTicks = 1.0;
    pivotAnimation.ticksPerSecond = 1.0;
    infernux::SkinnedRuntimeTrack pivotTrack;
    pivotTrack.nodeIndex = 1;
    constexpr float kSqrtHalf = 0.70710678118f;
    pivotTrack.rotations = {
        {0.0, glm::quat(1.0f, 0.0f, 0.0f, 0.0f)},
        {1.0, glm::quat(kSqrtHalf, 0.0f, 0.0f, kSqrtHalf)},
    };
    pivotAnimation.trackByNodeIndex = {-1, 0, -1};
    pivotAnimation.tracks.push_back(pivotTrack);
    pivotSource.animations.push_back(pivotAnimation);

    const auto pivotMap = pivotTarget.skeleton.BuildRetargetMap(pivotSource.skeleton, pivotAnimation);
    assert(pivotMap.targetToSourceNode == std::vector<int>({-1, 2}));
    assert(pivotMap.exactNameMatches == 1);
    assert(pivotMap.mappedAnimationTracks == 1);
    assert(pivotMap.missingTargetDeformJoints == 0);
    assert(pivotMap.topologyDifferences == 0);
    assert(pivotTarget.skeleton.IsAnimationCompatible(pivotSource.skeleton, pivotAnimation, &compatibilityReason));
    assert(compatibilityReason.empty());
    const auto pivotPalette = pivotTarget.BuildGpuBonePalette({"PivotRotation", 1.0f, false}, &pivotSource);
    assert(pivotPalette.size() == 1);
    assert(std::abs(pivotPalette.front()[0][0]) < 1.0e-5f);
    assert(std::abs(std::abs(pivotPalette.front()[0][1]) - 1.0f) < 1.0e-5f);

    infernux::Skeleton relatedTarget = source.skeleton;
    infernux::SkinnedRuntimeNode extraNode;
    extraNode.name = "AccessoryJoint";
    extraNode.parent = 0;
    relatedTarget.nodeByName.emplace(extraNode.name, 1);
    relatedTarget.nodes.push_back(extraNode);
    infernux::SkinnedRuntimeBone extraBone;
    extraBone.name = extraNode.name;
    extraBone.nodeIndex = 1;
    relatedTarget.boneByName.emplace(extraBone.name, 1);
    relatedTarget.bones.push_back(extraBone);
    assert(!relatedTarget.IsAnimationCompatible(animationOnly.skeleton, externalAnimation, &compatibilityReason));
    assert(compatibilityReason.find("exact joint identities") != std::string::npos);

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
