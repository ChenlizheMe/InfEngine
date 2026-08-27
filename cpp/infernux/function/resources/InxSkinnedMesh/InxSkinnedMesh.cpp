#include "InxSkinnedMesh.h"

#include <core/config/MathConstants.h>
#include <core/log/InxLog.h>

#define GLM_ENABLE_EXPERIMENTAL
#include <glm/gtc/matrix_transform.hpp>
#include <glm/gtx/norm.hpp>
#include <glm/gtx/quaternion.hpp>

#include <algorithm>
#include <cmath>
#include <limits>

namespace infernux
{

namespace
{
static constexpr size_t kMaxGpuPaletteCacheEntries = 64;

static int64_t QuantizeSeconds(float seconds)
{
    return static_cast<int64_t>(std::llround(static_cast<double>(seconds) * 1000000.0));
}

static int32_t QuantizeUnitFloat(float value)
{
    return static_cast<int32_t>(std::llround(static_cast<double>(glm::clamp(value, 0.0f, 1.0f)) * 1000000.0));
}

static void HashCombine(size_t &seed, size_t value)
{
    seed ^= value + 0x9e3779b97f4a7c15ull + (seed << 6) + (seed >> 2);
}

static glm::mat4 MakeTRS(const glm::vec3 &t, const glm::quat &r, const glm::vec3 &s)
{
    return glm::translate(glm::mat4(1.0f), t) * glm::toMat4(r) * glm::scale(glm::mat4(1.0f), s);
}

template <typename T> static size_t FindKeySpan(const std::vector<std::pair<double, T>> &keys, double t)
{
    if (keys.size() < 2)
        return 0;

    const auto upper =
        std::upper_bound(keys.begin(), keys.end(), t, [](double time, const auto &key) { return time < key.first; });
    if (upper == keys.begin())
        return 0;
    if (upper == keys.end())
        return keys.size() - 2;
    return static_cast<size_t>(std::distance(keys.begin(), upper) - 1);
}

static glm::vec3 SampleVec3(const std::vector<std::pair<double, glm::vec3>> &keys, double t, const glm::vec3 &fallback)
{
    if (keys.empty())
        return fallback;
    if (keys.size() == 1)
        return keys[0].second;
    const size_t i = FindKeySpan(keys, t);
    const auto &a = keys[i];
    const auto &b = keys[i + 1];
    const double span = std::max(b.first - a.first, 1e-8);
    const float f = static_cast<float>((t - a.first) / span);
    return glm::mix(a.second, b.second, glm::clamp(f, 0.0f, 1.0f));
}

static glm::quat SampleQuat(const std::vector<std::pair<double, glm::quat>> &keys, double t, const glm::quat &fallback)
{
    if (keys.empty())
        return fallback;
    if (keys.size() == 1)
        return keys[0].second;
    const size_t i = FindKeySpan(keys, t);
    const auto &a = keys[i];
    const auto &b = keys[i + 1];
    const double span = std::max(b.first - a.first, 1e-8);
    const float f = static_cast<float>((t - a.first) / span);
    return glm::normalize(glm::slerp(a.second, b.second, glm::clamp(f, 0.0f, 1.0f)));
}

static void DecomposeTRS(const glm::mat4 &m, glm::vec3 &t, glm::quat &r, glm::vec3 &s)
{
    t = glm::vec3(m[3]);
    s = glm::vec3(glm::length(glm::vec3(m[0])), glm::length(glm::vec3(m[1])), glm::length(glm::vec3(m[2])));
    glm::mat3 rot(1.0f);
    if (s.x > kEpsilon)
        rot[0] = glm::vec3(m[0]) / s.x;
    if (s.y > kEpsilon)
        rot[1] = glm::vec3(m[1]) / s.y;
    if (s.z > kEpsilon)
        rot[2] = glm::vec3(m[2]) / s.z;
    r = glm::normalize(glm::quat_cast(rot));
}

static SkinnedNodePose BindNodePose(const SkinnedRuntimeNode &node)
{
    SkinnedNodePose pose;
    DecomposeTRS(node.bindLocal, pose.translation, pose.rotation, pose.scale);
    return pose;
}

static double ToAnimationTicks(const SkinnedRuntimeAnimation *anim, float seconds, bool loop)
{
    if (!anim)
        return 0.0;
    double tTicks = static_cast<double>(seconds) * anim->ticksPerSecond;
    if (anim->durationTicks > 0.0) {
        if (loop) {
            tTicks = std::fmod(tTicks, anim->durationTicks);
            if (tTicks < 0.0)
                tTicks += anim->durationTicks;
        } else {
            // Clamp so a finished non-looping clip holds its end pose instead
            // of wrapping back to frame 0 (fmod(duration, duration) == 0).
            tTicks = std::clamp(tTicks, 0.0, anim->durationTicks);
        }
    }
    return tTicks;
}

static SkinnedNodePose BlendNodePose(const SkinnedNodePose &a, const SkinnedNodePose &b, float weight)
{
    const float w = glm::clamp(weight, 0.0f, 1.0f);
    SkinnedNodePose pose;
    pose.translation = glm::mix(a.translation, b.translation, w);
    pose.rotation = glm::normalize(glm::slerp(a.rotation, b.rotation, w));
    pose.scale = glm::mix(a.scale, b.scale, w);
    return pose;
}

} // namespace

float SkinnedRuntimeAnimation::DurationSeconds() const
{
    if (durationTicks <= 0.0 || ticksPerSecond <= 0.0)
        return 0.0f;
    return static_cast<float>(durationTicks / ticksPerSecond);
}

bool Skeleton::IsValid() const noexcept
{
    if (nodes.empty() || nodeByName.size() != nodes.size())
        return false;
    for (size_t index = 0; index < nodes.size(); ++index) {
        const auto &node = nodes[index];
        const auto found = nodeByName.find(node.name);
        if (node.name.empty() || found == nodeByName.end() || found->second != static_cast<int>(index) ||
            node.parent < -1 || node.parent >= static_cast<int>(index))
            return false;
    }
    return true;
}

SkeletonRetargetMap Skeleton::BuildRetargetMap(const Skeleton &source, const SkinnedRuntimeAnimation &animation) const
{
    SkeletonRetargetMap mapping;
    mapping.targetToSourceNode.assign(nodes.size(), -1);
    if (!IsValid() || !source.IsValid() || animation.tracks.empty())
        return mapping;

    // Automatic compatibility is intentionally strict: authored joint names
    // establish identity. FBX importers may insert pivot/helper nodes in an
    // animation-only file, so hierarchy comparison skips unmatched containers,
    // but it never guesses a joint from geometry. Cross-rig animation needs an
    // explicit Avatar/Rig-style mapping rather than silently swapping symmetric
    // limbs or fingers.
    std::vector<bool> targetRelevant(nodes.size(), false);
    for (const auto &bone : bones) {
        if (bone.nodeIndex >= 0 && static_cast<size_t>(bone.nodeIndex) < targetRelevant.size())
            targetRelevant[static_cast<size_t>(bone.nodeIndex)] = true;
    }
    if (std::none_of(targetRelevant.begin(), targetRelevant.end(), [](bool value) { return value; })) {
        // Skeleton-only callers have no deformation stream to define the
        // target graph. Preserve the complete hierarchy in that case.
        std::fill(targetRelevant.begin(), targetRelevant.end(), true);
    }
    std::vector<bool> sourceUsed(source.nodes.size(), false);
    for (size_t targetIndex = 0; targetIndex < nodes.size(); ++targetIndex) {
        if (!targetRelevant[targetIndex])
            continue;
        const auto sourceNode = source.nodeByName.find(nodes[targetIndex].name);
        if (sourceNode == source.nodeByName.end() || sourceNode->second < 0 ||
            static_cast<size_t>(sourceNode->second) >= source.nodes.size() ||
            sourceUsed[static_cast<size_t>(sourceNode->second)])
            continue;
        mapping.targetToSourceNode[targetIndex] = sourceNode->second;
        sourceUsed[static_cast<size_t>(sourceNode->second)] = true;
    }

    std::vector<int> sourceToTarget(source.nodes.size(), -1);
    std::vector<bool> animatedSourceAncestors(source.nodes.size(), false);
    for (size_t targetIndex = 0; targetIndex < mapping.targetToSourceNode.size(); ++targetIndex) {
        const int sourceIndex = mapping.targetToSourceNode[targetIndex];
        if (sourceIndex < 0 || static_cast<size_t>(sourceIndex) >= source.nodes.size())
            continue;
        ++mapping.mappedNodes;
        ++mapping.exactNameMatches;
        sourceToTarget[static_cast<size_t>(sourceIndex)] = static_cast<int>(targetIndex);
        if (sourceIndex >= 0 && static_cast<size_t>(sourceIndex) < animation.trackByNodeIndex.size() &&
            animation.trackByNodeIndex[static_cast<size_t>(sourceIndex)] >= 0)
            ++mapping.mappedAnimatedNodes;
        for (int ancestor = sourceIndex; ancestor >= 0; ancestor = source.nodes[static_cast<size_t>(ancestor)].parent)
            animatedSourceAncestors[static_cast<size_t>(ancestor)] = true;
    }
    for (const auto &track : animation.tracks) {
        if (track.nodeIndex >= 0 && static_cast<size_t>(track.nodeIndex) < animatedSourceAncestors.size() &&
            animatedSourceAncestors[static_cast<size_t>(track.nodeIndex)])
            ++mapping.mappedAnimationTracks;
    }

    // Compare the mapped deformation hierarchy while deliberately skipping
    // importer-created helper nodes. This is the topology users authored.
    for (size_t targetIndex = 0; targetIndex < mapping.targetToSourceNode.size(); ++targetIndex) {
        const int sourceIndex = mapping.targetToSourceNode[targetIndex];
        if (sourceIndex < 0)
            continue;
        int targetAncestor = nodes[targetIndex].parent;
        while (targetAncestor >= 0 && mapping.targetToSourceNode[static_cast<size_t>(targetAncestor)] < 0)
            targetAncestor = nodes[static_cast<size_t>(targetAncestor)].parent;
        int sourceAncestor = source.nodes[static_cast<size_t>(sourceIndex)].parent;
        while (sourceAncestor >= 0 && sourceToTarget[static_cast<size_t>(sourceAncestor)] < 0)
            sourceAncestor = source.nodes[static_cast<size_t>(sourceAncestor)].parent;
        const int mappedSourceAncestor = sourceAncestor >= 0 ? sourceToTarget[static_cast<size_t>(sourceAncestor)] : -1;
        if (targetAncestor != mappedSourceAncestor)
            ++mapping.topologyDifferences;
    }
    for (const auto &bone : bones) {
        if (bone.nodeIndex < 0 || static_cast<size_t>(bone.nodeIndex) >= mapping.targetToSourceNode.size() ||
            mapping.targetToSourceNode[static_cast<size_t>(bone.nodeIndex)] < 0)
            ++mapping.missingTargetDeformJoints;
    }
    const size_t relevantTargetCount =
        static_cast<size_t>(std::count(targetRelevant.begin(), targetRelevant.end(), true));
    mapping.identicalTopology = mapping.mappedNodes == relevantTargetCount && mapping.topologyDifferences == 0;
    return mapping;
}

bool Skeleton::IsAnimationCompatible(const Skeleton &source, const SkinnedRuntimeAnimation &animation,
                                     std::string *reason) const
{
    const auto reject = [reason](std::string message) {
        if (reason)
            *reason = std::move(message);
        return false;
    };
    if (!IsValid())
        return reject("target skeleton hierarchy is invalid");
    if (!source.IsValid())
        return reject("animation source skeleton hierarchy is invalid");

    if (animation.tracks.empty())
        return reject("animation has no transform tracks");
    const SkeletonRetargetMap mapping = BuildRetargetMap(source, animation);
    if (mapping.mappedAnimationTracks == 0)
        return reject("animation has no tracks affecting an exactly named target joint");
    if (!mapping.identicalTopology) {
        return reject("automatic retarget requires exact joint identities and the same mapped hierarchy; mapped " +
                      std::to_string(mapping.exactNameMatches) + " joints, missing " +
                      std::to_string(mapping.missingTargetDeformJoints) + "/" + std::to_string(bones.size()) +
                      " target deform joints, and found " + std::to_string(mapping.topologyDifferences) +
                      " mapped hierarchy differences");
    }
    if (reason) {
        if (mapping.missingTargetDeformJoints > 0 || mapping.topologyDifferences > 0 ||
            mapping.mappedAnimationTracks < animation.tracks.size()) {
            *reason = "identity retarget maps " + std::to_string(mapping.exactNameMatches) + " joints and uses " +
                      std::to_string(mapping.mappedAnimationTracks) + "/" + std::to_string(animation.tracks.size()) +
                      " animation tracks; missing " + std::to_string(mapping.missingTargetDeformJoints) + "/" +
                      std::to_string(bones.size()) + " target deform joints; " +
                      std::to_string(mapping.topologyDifferences) + " mapped hierarchy differences";
        } else {
            reason->clear();
        }
    }
    return true;
}

size_t Skeleton::GetRuntimeMemoryBytes() const noexcept
{
    size_t bytes = sizeof(*this) + nodes.capacity() * sizeof(SkinnedRuntimeNode);
    for (const auto &node : nodes)
        bytes += node.name.capacity();
    bytes += nodeByName.bucket_count() * sizeof(void *);
    for (const auto &[name, index] : nodeByName) {
        (void)index;
        bytes += sizeof(std::pair<const std::string, int>) + name.capacity();
    }
    bytes += bones.capacity() * sizeof(SkinnedRuntimeBone);
    for (const auto &bone : bones)
        bytes += bone.name.capacity();
    bytes += boneByName.bucket_count() * sizeof(void *);
    for (const auto &[name, index] : boneByName) {
        (void)index;
        bytes += sizeof(std::pair<const std::string, uint32_t>) + name.capacity();
    }
    return bytes;
}

size_t InxSkinnedMesh::GetRuntimeMemoryBytes() const noexcept
{
    size_t bytes = sizeof(*this) + sourcePath.capacity() + guid.capacity();
    bytes += baseVertices.capacity() * sizeof(Vertex);
    bytes += influences.capacity() * sizeof(SkinInfluence);
    bytes += indices.capacity() * sizeof(uint32_t);
    bytes += subMeshes.capacity() * sizeof(SubMesh);
    for (const auto &subMesh : subMeshes)
        bytes += subMesh.name.capacity();
    bytes += skeleton.GetRuntimeMemoryBytes();
    bytes += animations.capacity() * sizeof(SkinnedRuntimeAnimation);
    for (const auto &animation : animations) {
        bytes += animation.name.capacity();
        bytes += animation.tracks.capacity() * sizeof(SkinnedRuntimeTrack);
        for (const auto &track : animation.tracks) {
            bytes += track.positions.capacity() * sizeof(decltype(track.positions)::value_type);
            bytes += track.rotations.capacity() * sizeof(decltype(track.rotations)::value_type);
            bytes += track.scales.capacity() * sizeof(decltype(track.scales)::value_type);
        }
        bytes += animation.trackByNodeIndex.capacity() * sizeof(int);
    }
    bytes += m_gpuPaletteCache.bucket_count() * sizeof(void *);
    for (const auto &[key, palette] : m_gpuPaletteCache) {
        bytes += sizeof(decltype(m_gpuPaletteCache)::value_type) + key.takeName.capacity() +
                 key.blendTakeName.capacity() + key.animationSourceKey.capacity() +
                 key.blendAnimationSourceKey.capacity();
        if (palette)
            bytes += sizeof(*palette) + palette->capacity() * sizeof(glm::mat4);
    }
    bytes += m_gpuPaletteCacheOrder.capacity() * sizeof(PaletteCacheKey);
    for (const auto &key : m_gpuPaletteCacheOrder)
        bytes += key.takeName.capacity() + key.blendTakeName.capacity() + key.animationSourceKey.capacity() +
                 key.blendAnimationSourceKey.capacity();
    return bytes;
}

const SkinnedRuntimeAnimation *InxSkinnedMesh::FindAnimation(const std::string &takeName) const
{
    if (animations.empty())
        return nullptr;

    // Empty take = explicit bind pose request (no animation sampled).
    if (takeName.empty())
        return nullptr;

    for (const auto &anim : animations)
        if (anim.name == takeName)
            return &anim;

    // Unknown name: warn loudly instead of silently playing the first take —
    // playing the wrong animation is much harder to debug than a bind pose.
    INXLOG_WARN("InxSkinnedMesh: animation take '", takeName, "' not found in '", sourcePath,
                "' — rendering bind pose. Available takes: ", animations.size());
    return nullptr;
}

float InxSkinnedMesh::GetAnimationDurationSeconds(const std::string &takeName) const
{
    const SkinnedRuntimeAnimation *anim = FindAnimation(takeName);
    return anim ? anim->DurationSeconds() : 0.0f;
}

size_t InxSkinnedMesh::PaletteCacheKeyHash::operator()(const PaletteCacheKey &key) const
{
    size_t seed = std::hash<std::string>{}(key.takeName);
    HashCombine(seed, std::hash<int64_t>{}(key.timeMicros));
    HashCombine(seed, std::hash<bool>{}(key.loop));
    HashCombine(seed, std::hash<std::string>{}(key.blendTakeName));
    HashCombine(seed, std::hash<std::string>{}(key.animationSourceKey));
    HashCombine(seed, std::hash<std::string>{}(key.blendAnimationSourceKey));
    HashCombine(seed, std::hash<int64_t>{}(key.blendTimeMicros));
    HashCombine(seed, std::hash<int32_t>{}(key.blendWeightMicros));
    return seed;
}

InxSkinnedMesh::PaletteCacheKey InxSkinnedMesh::MakePaletteCacheKey(const SkinnedSampleRequest &request,
                                                                    const InxSkinnedMesh *animationSource,
                                                                    const InxSkinnedMesh *blendAnimationSource)
{
    PaletteCacheKey key;
    key.takeName = request.takeName;
    key.timeMicros = QuantizeSeconds(request.timeSeconds);
    key.loop = request.loop;
    key.blendTakeName = request.blendTakeName;
    const auto sourceKey = [](const InxSkinnedMesh *source) -> std::string {
        if (!source)
            return {};
        const std::string &identity = !source->guid.empty() ? source->guid : source->sourcePath;
        return identity + "@" + std::to_string(reinterpret_cast<uintptr_t>(source));
    };
    key.animationSourceKey = sourceKey(animationSource);
    key.blendAnimationSourceKey = sourceKey(blendAnimationSource);
    key.blendTimeMicros = QuantizeSeconds(request.blendTimeSeconds);
    key.blendWeightMicros = QuantizeUnitFloat(request.blendWeight);
    return key;
}

void InxSkinnedMesh::NormalizeInfluences()
{
    for (size_t vi = 0; vi < influences.size(); ++vi) {
        auto &inf = influences[vi];
        float total = 0.0f;
        for (float w : inf.weight)
            total += w;
        if (total > kEpsilon) {
            for (float &w : inf.weight)
                w /= total;
        }

        if (vi < baseVertices.size()) {
            baseVertices[vi].boneIndices =
                glm::uvec4(inf.boneIndex[0], inf.boneIndex[1], inf.boneIndex[2], inf.boneIndex[3]);
            baseVertices[vi].boneWeights = glm::vec4(inf.weight[0], inf.weight[1], inf.weight[2], inf.weight[3]);
        }
    }
}

SkinnedNodePose InxSkinnedMesh::SampleRetargetedNodePose(const SkinnedRuntimeAnimation *anim,
                                                         const Skeleton &sourceSkeleton, size_t targetNodeIndex,
                                                         int sourceNodeIndex, double tTicks) const
{
    const SkinnedNodePose targetBind = BindNodePose(skeleton.nodes[targetNodeIndex]);
    if (!anim || sourceNodeIndex < 0 || static_cast<size_t>(sourceNodeIndex) >= sourceSkeleton.nodes.size() ||
        static_cast<size_t>(sourceNodeIndex) >= anim->trackByNodeIndex.size())
        return targetBind;
    const int trackIndex = anim->trackByNodeIndex[static_cast<size_t>(sourceNodeIndex)];
    if (trackIndex < 0 || static_cast<size_t>(trackIndex) >= anim->tracks.size())
        return targetBind;

    const SkinnedRuntimeNode &sourceNode = sourceSkeleton.nodes[static_cast<size_t>(sourceNodeIndex)];
    const SkinnedNodePose sourceBind = BindNodePose(sourceNode);
    const SkinnedRuntimeTrack &track = anim->tracks[static_cast<size_t>(trackIndex)];
    SkinnedNodePose sourcePose = sourceBind;
    sourcePose.translation = SampleVec3(track.positions, tTicks, sourcePose.translation);
    sourcePose.rotation = SampleQuat(track.rotations, tTicks, sourcePose.rotation);
    sourcePose.scale = SampleVec3(track.scales, tTicks, sourcePose.scale);

    float translationScale = 1.0f;
    const float sourceBoneLength = glm::length(sourceBind.translation);
    const float targetBoneLength = glm::length(targetBind.translation);
    if (sourceBoneLength > kEpsilon && targetBoneLength > kEpsilon)
        translationScale = targetBoneLength / sourceBoneLength;

    SkinnedNodePose targetPose;
    targetPose.translation =
        targetBind.translation + (sourcePose.translation - sourceBind.translation) * translationScale;
    const glm::quat localRotationDelta = glm::normalize(glm::inverse(sourceBind.rotation) * sourcePose.rotation);
    targetPose.rotation = glm::normalize(targetBind.rotation * localRotationDelta);
    for (glm::length_t component = 0; component < targetPose.scale.length(); ++component) {
        const float sourceBindScale = sourceBind.scale[component];
        const float scaleDelta =
            std::abs(sourceBindScale) > kEpsilon ? sourcePose.scale[component] / sourceBindScale : 1.0f;
        targetPose.scale[component] = targetBind.scale[component] * scaleDelta;
    }
    return targetPose;
}

std::vector<SkinnedNodePose> InxSkinnedMesh::BuildRetargetedLocalPoses(const Skeleton &sourceSkeleton,
                                                                       const SkinnedRuntimeAnimation *animation,
                                                                       const SkeletonRetargetMap &mapping,
                                                                       double timeTicks) const
{
    std::vector<SkinnedNodePose> targetPoses(skeleton.nodes.size());
    for (size_t index = 0; index < skeleton.nodes.size(); ++index)
        targetPoses[index] = BindNodePose(skeleton.nodes[index]);
    if (!animation || sourceSkeleton.nodes.empty())
        return targetPoses;

    std::vector<SkinnedNodePose> sourcePoses(sourceSkeleton.nodes.size());
    std::vector<glm::mat4> sourceGlobals(sourceSkeleton.nodes.size(), glm::mat4(1.0f));
    for (size_t index = 0; index < sourceSkeleton.nodes.size(); ++index) {
        sourcePoses[index] = BindNodePose(sourceSkeleton.nodes[index]);
        if (index < animation->trackByNodeIndex.size()) {
            const int trackIndex = animation->trackByNodeIndex[index];
            if (trackIndex >= 0 && static_cast<size_t>(trackIndex) < animation->tracks.size()) {
                const auto &track = animation->tracks[static_cast<size_t>(trackIndex)];
                sourcePoses[index].translation = SampleVec3(track.positions, timeTicks, sourcePoses[index].translation);
                sourcePoses[index].rotation = SampleQuat(track.rotations, timeTicks, sourcePoses[index].rotation);
                sourcePoses[index].scale = SampleVec3(track.scales, timeTicks, sourcePoses[index].scale);
            }
        }
        const glm::mat4 local =
            MakeTRS(sourcePoses[index].translation, sourcePoses[index].rotation, sourcePoses[index].scale);
        const int parent = sourceSkeleton.nodes[index].parent;
        sourceGlobals[index] = parent >= 0 ? sourceGlobals[static_cast<size_t>(parent)] * local : local;
    }

    auto skeletonExtent = [](const Skeleton &value) {
        glm::vec3 minimum(std::numeric_limits<float>::max());
        glm::vec3 maximum(std::numeric_limits<float>::lowest());
        for (const auto &node : value.nodes) {
            const glm::vec3 position(node.bindGlobal[3]);
            minimum = glm::min(minimum, position);
            maximum = glm::max(maximum, position);
        }
        const float extent = glm::length(maximum - minimum);
        return std::isfinite(extent) && extent > kEpsilon ? extent : 1.0f;
    };
    const float translationScale = skeletonExtent(skeleton) / skeletonExtent(sourceSkeleton);

    std::vector<glm::mat4> targetGlobals(skeleton.nodes.size(), glm::mat4(1.0f));
    for (size_t targetIndex = 0; targetIndex < skeleton.nodes.size(); ++targetIndex) {
        const auto &targetNode = skeleton.nodes[targetIndex];
        const int sourceIndex =
            targetIndex < mapping.targetToSourceNode.size() ? mapping.targetToSourceNode[targetIndex] : -1;
        if (sourceIndex >= 0 && static_cast<size_t>(sourceIndex) < sourceSkeleton.nodes.size()) {
            glm::vec3 sourceBindTranslation, sourceCurrentTranslation;
            glm::quat sourceBindRotation, sourceCurrentRotation;
            glm::vec3 sourceBindScale, sourceCurrentScale;
            DecomposeTRS(sourceSkeleton.nodes[static_cast<size_t>(sourceIndex)].bindGlobal, sourceBindTranslation,
                         sourceBindRotation, sourceBindScale);
            DecomposeTRS(sourceGlobals[static_cast<size_t>(sourceIndex)], sourceCurrentTranslation,
                         sourceCurrentRotation, sourceCurrentScale);

            // A joint's local axes are exporter-specific. Transfer its model-
            // space bind-to-animation delta, then solve the target local
            // rotation from the already-retargeted parent. This keeps the
            // result stable when the two rigs use different joint bases.
            glm::vec3 targetBindTranslation;
            glm::quat targetBindRotation;
            glm::vec3 targetBindScale;
            DecomposeTRS(targetNode.bindGlobal, targetBindTranslation, targetBindRotation, targetBindScale);
            const glm::quat globalDelta = glm::normalize(sourceCurrentRotation * glm::inverse(sourceBindRotation));
            const glm::quat desiredGlobalRotation = glm::normalize(globalDelta * targetBindRotation);
            if (targetNode.parent >= 0) {
                glm::vec3 parentTranslation, parentScale;
                glm::quat parentRotation;
                DecomposeTRS(targetGlobals[static_cast<size_t>(targetNode.parent)], parentTranslation, parentRotation,
                             parentScale);
                targetPoses[targetIndex].rotation =
                    glm::normalize(glm::inverse(parentRotation) * desiredGlobalRotation);
            } else {
                targetPoses[targetIndex].rotation = desiredGlobalRotation;
            }

            bool hasMappedAncestor = false;
            for (int ancestor = targetNode.parent; ancestor >= 0;
                 ancestor = skeleton.nodes[static_cast<size_t>(ancestor)].parent) {
                if (static_cast<size_t>(ancestor) < mapping.targetToSourceNode.size() &&
                    mapping.targetToSourceNode[static_cast<size_t>(ancestor)] >= 0) {
                    hasMappedAncestor = true;
                    break;
                }
            }
            if (!hasMappedAncestor) {
                const glm::vec3 desiredGlobalTranslation =
                    targetBindTranslation + (sourceCurrentTranslation - sourceBindTranslation) * translationScale;
                if (targetNode.parent >= 0) {
                    const glm::vec4 local = glm::inverse(targetGlobals[static_cast<size_t>(targetNode.parent)]) *
                                            glm::vec4(desiredGlobalTranslation, 1.0f);
                    targetPoses[targetIndex].translation = glm::vec3(local);
                } else {
                    targetPoses[targetIndex].translation = desiredGlobalTranslation;
                }
            }

            const SkinnedNodePose sourceBindLocal =
                BindNodePose(sourceSkeleton.nodes[static_cast<size_t>(sourceIndex)]);
            for (glm::length_t component = 0; component < targetPoses[targetIndex].scale.length(); ++component) {
                const float denominator = sourceBindLocal.scale[component];
                const float scaleDelta =
                    std::abs(denominator) > kEpsilon
                        ? sourcePoses[static_cast<size_t>(sourceIndex)].scale[component] / denominator
                        : 1.0f;
                targetPoses[targetIndex].scale[component] *= scaleDelta;
            }
        }

        const glm::mat4 local = MakeTRS(targetPoses[targetIndex].translation, targetPoses[targetIndex].rotation,
                                        targetPoses[targetIndex].scale);
        targetGlobals[targetIndex] =
            targetNode.parent >= 0 ? targetGlobals[static_cast<size_t>(targetNode.parent)] * local : local;
    }
    return targetPoses;
}

std::vector<glm::mat4> InxSkinnedMesh::BuildBoneMatrices(const SkinnedSampleRequest &request,
                                                         const InxSkinnedMesh *animationSource,
                                                         const InxSkinnedMesh *blendAnimationSource) const
{
    const InxSkinnedMesh *activeSource = animationSource ? animationSource : this;
    const InxSkinnedMesh *blendSource = blendAnimationSource ? blendAnimationSource : activeSource;
    const SkinnedRuntimeAnimation *anim = activeSource->FindAnimation(request.takeName);
    const SkinnedRuntimeAnimation *blendAnim = (request.blendWeight > 0.0f && !request.blendTakeName.empty())
                                                   ? blendSource->FindAnimation(request.blendTakeName)
                                                   : nullptr;
    const double tTicks = ToAnimationTicks(anim, request.timeSeconds, request.loop);
    // Blend source always loops: it represents the outgoing state mid-fade.
    const double blendTicks = ToAnimationTicks(blendAnim, request.blendTimeSeconds, true);
    // Same-take cross-fades at different times are valid (e.g. restarting a
    // clip with a fade) — only a missing blend animation disables blending.
    const float w = blendAnim ? glm::clamp(request.blendWeight, 0.0f, 1.0f) : 0.0f;
    const SkeletonRetargetMap activeMapping =
        anim ? skeleton.BuildRetargetMap(activeSource->skeleton, *anim) : SkeletonRetargetMap{};
    const SkeletonRetargetMap blendMapping =
        blendAnim ? skeleton.BuildRetargetMap(blendSource->skeleton, *blendAnim) : SkeletonRetargetMap{};
    const std::vector<SkinnedNodePose> activePoses =
        BuildRetargetedLocalPoses(activeSource->skeleton, anim, activeMapping, tTicks);
    const std::vector<SkinnedNodePose> blendPoses =
        BuildRetargetedLocalPoses(blendSource->skeleton, blendAnim, blendMapping, blendTicks);

    std::vector<glm::mat4> globals(skeleton.nodes.size(), glm::mat4(1.0f));
    for (size_t ni = 0; ni < skeleton.nodes.size(); ++ni) {
        const SkinnedRuntimeNode &node = skeleton.nodes[ni];
        SkinnedNodePose pose = activePoses[ni];
        if (w > 0.0f)
            pose = BlendNodePose(pose, blendPoses[ni], w);

        glm::mat4 local = MakeTRS(pose.translation, pose.rotation, pose.scale);
        globals[ni] = (node.parent >= 0) ? globals[static_cast<size_t>(node.parent)] * local : local;
    }

    std::vector<glm::mat4> boneMatrices(skeleton.bones.size(), glm::mat4(1.0f));
    for (size_t bi = 0; bi < skeleton.bones.size(); ++bi) {
        const SkinnedRuntimeBone &bone = skeleton.bones[bi];
        if (bone.nodeIndex >= 0 && static_cast<size_t>(bone.nodeIndex) < globals.size())
            boneMatrices[bi] = globals[static_cast<size_t>(bone.nodeIndex)] * bone.inverseBind;
    }
    return boneMatrices;
}

std::vector<glm::mat4> InxSkinnedMesh::BuildBoneMatricesFromPoseStack(
    const std::vector<PoseStackLayer> &layers,
    const std::vector<std::shared_ptr<const InxSkinnedMesh>> &animationSources) const
{
    const size_t N = skeleton.nodes.size();

    // Bind pose per node (the base / fallback for uncovered nodes).
    std::vector<SkinnedNodePose> bind(N);
    for (size_t ni = 0; ni < N; ++ni)
        bind[ni] = BindNodePose(skeleton.nodes[ni]);

    // Non-additive accumulation (coverage-normalized weighted average).
    std::vector<glm::vec3> posSum(N, glm::vec3(0.0f));
    std::vector<glm::vec3> scaleSum(N, glm::vec3(0.0f));
    std::vector<glm::quat> rotAccum(N, glm::quat(0.0f, 0.0f, 0.0f, 0.0f));
    std::vector<float> wSum(N, 0.0f);

    // Additive accumulation (delta from bind, applied on top).
    std::vector<glm::vec3> addPos(N, glm::vec3(0.0f));
    std::vector<glm::vec3> addScale(N, glm::vec3(0.0f));
    std::vector<glm::quat> addRot(N, glm::quat(1.0f, 0.0f, 0.0f, 0.0f));
    std::vector<bool> hasAdd(N, false);

    for (size_t layerIndex = 0; layerIndex < layers.size(); ++layerIndex) {
        const PoseStackLayer &layer = layers[layerIndex];
        const float w = glm::clamp(layer.weight, 0.0f, 1.0f);
        if (w <= kEpsilon)
            continue;
        const InxSkinnedMesh *source = layerIndex < animationSources.size() && animationSources[layerIndex]
                                           ? animationSources[layerIndex].get()
                                           : this;
        const SkinnedRuntimeAnimation *anim = source->FindAnimation(layer.takeName);
        const double tTicks = ToAnimationTicks(anim, layer.timeSeconds, layer.loop);
        const SkeletonRetargetMap retarget =
            anim ? skeleton.BuildRetargetMap(source->skeleton, *anim) : SkeletonRetargetMap{};
        const std::vector<SkinnedNodePose> retargetedPoses =
            BuildRetargetedLocalPoses(source->skeleton, anim, retarget, tTicks);

        // Resolve the optional bone mask to a node-name set (empty = all nodes).
        const bool masked = !layer.boneMask.empty();
        std::unordered_map<std::string, char> maskSet;
        if (masked)
            for (const std::string &b : layer.boneMask)
                maskSet.emplace(b, 1);

        for (size_t ni = 0; ni < N; ++ni) {
            if (masked && maskSet.find(skeleton.nodes[ni].name) == maskSet.end())
                continue;
            const SkinnedNodePose &pose = retargetedPoses[ni];
            if (layer.additive) {
                addPos[ni] += (pose.translation - bind[ni].translation) * w;
                addScale[ni] += (pose.scale - bind[ni].scale) * w;
                glm::quat delta = glm::normalize(pose.rotation * glm::inverse(bind[ni].rotation));
                glm::quat scaled = glm::slerp(glm::quat(1.0f, 0.0f, 0.0f, 0.0f), delta, w);
                addRot[ni] = glm::normalize(scaled * addRot[ni]);
                hasAdd[ni] = true;
            } else {
                posSum[ni] += pose.translation * w;
                scaleSum[ni] += pose.scale * w;
                glm::quat q = pose.rotation;
                if (wSum[ni] > 0.0f && glm::dot(rotAccum[ni], q) < 0.0f)
                    q = -q; // hemisphere-align for a stable nlerp accumulation
                rotAccum[ni] += q * w;
                wSum[ni] += w;
            }
        }
    }

    std::vector<glm::mat4> globals(N, glm::mat4(1.0f));
    for (size_t ni = 0; ni < N; ++ni) {
        SkinnedNodePose finalPose = bind[ni];
        if (wSum[ni] > kEpsilon) {
            const float coverage = glm::clamp(wSum[ni], 0.0f, 1.0f);
            SkinnedNodePose avg;
            avg.translation = posSum[ni] / wSum[ni];
            avg.scale = scaleSum[ni] / wSum[ni];
            avg.rotation = glm::normalize(rotAccum[ni]);
            // Blend bind → weighted-average by coverage (uncovered weight holds bind).
            finalPose.translation = glm::mix(bind[ni].translation, avg.translation, coverage);
            finalPose.scale = glm::mix(bind[ni].scale, avg.scale, coverage);
            finalPose.rotation = glm::normalize(glm::slerp(bind[ni].rotation, avg.rotation, coverage));
        }
        if (hasAdd[ni]) {
            finalPose.translation += addPos[ni];
            finalPose.scale += addScale[ni];
            finalPose.rotation = glm::normalize(addRot[ni] * finalPose.rotation);
        }

        glm::mat4 local = MakeTRS(finalPose.translation, finalPose.rotation, finalPose.scale);
        globals[ni] =
            (skeleton.nodes[ni].parent >= 0) ? globals[static_cast<size_t>(skeleton.nodes[ni].parent)] * local : local;
    }

    std::vector<glm::mat4> boneMatrices(skeleton.bones.size(), glm::mat4(1.0f));
    for (size_t bi = 0; bi < skeleton.bones.size(); ++bi) {
        const SkinnedRuntimeBone &bone = skeleton.bones[bi];
        if (bone.nodeIndex >= 0 && static_cast<size_t>(bone.nodeIndex) < globals.size())
            boneMatrices[bi] = globals[static_cast<size_t>(bone.nodeIndex)] * bone.inverseBind;
    }
    return boneMatrices;
}

std::vector<glm::mat4> InxSkinnedMesh::BuildGpuBonePalette(const SkinnedSampleRequest &request,
                                                           const InxSkinnedMesh *animationSource,
                                                           const InxSkinnedMesh *blendAnimationSource) const
{
    std::vector<glm::mat4> palette = BuildBoneMatrices(request, animationSource, blendAnimationSource);
    const glm::mat4 scale = glm::scale(glm::mat4(1.0f), glm::vec3(scaleFactor));
    for (glm::mat4 &m : palette)
        m = scale * m;
    return palette;
}

std::vector<glm::mat4> InxSkinnedMesh::BuildGpuBonePaletteFromPoseStack(
    const std::vector<PoseStackLayer> &layers,
    const std::vector<std::shared_ptr<const InxSkinnedMesh>> &animationSources) const
{
    std::vector<glm::mat4> palette = BuildBoneMatricesFromPoseStack(layers, animationSources);
    const glm::mat4 scale = glm::scale(glm::mat4(1.0f), glm::vec3(scaleFactor));
    for (glm::mat4 &m : palette)
        m = scale * m;
    return palette;
}

std::shared_ptr<const std::vector<glm::mat4>>
InxSkinnedMesh::GetOrBuildGpuBonePalette(const SkinnedSampleRequest &request, const InxSkinnedMesh *animationSource,
                                         const InxSkinnedMesh *blendAnimationSource) const
{
    PaletteCacheKey key = MakePaletteCacheKey(request, animationSource, blendAnimationSource);
    auto it = m_gpuPaletteCache.find(key);
    if (it != m_gpuPaletteCache.end())
        return it->second;

    auto palette = std::make_shared<const std::vector<glm::mat4>>(
        BuildGpuBonePalette(request, animationSource, blendAnimationSource));
    m_gpuPaletteCache.emplace(key, palette);
    m_gpuPaletteCacheOrder.push_back(key);

    while (m_gpuPaletteCacheOrder.size() > kMaxGpuPaletteCacheEntries) {
        m_gpuPaletteCache.erase(m_gpuPaletteCacheOrder.front());
        m_gpuPaletteCacheOrder.erase(m_gpuPaletteCacheOrder.begin());
    }

    return palette;
}

std::vector<Vertex> InxSkinnedMesh::SampleVertices(const SkinnedSampleRequest &request) const
{
    std::vector<Vertex> outVertices = baseVertices;
    if (outVertices.empty())
        return outVertices;

    if (skeleton.bones.empty()) {
        for (auto &v : outVertices)
            v.pos *= scaleFactor;
        return outVertices;
    }

    const std::vector<glm::mat4> boneMatrices = BuildBoneMatrices(request, nullptr, nullptr);

    for (size_t vi = 0; vi < outVertices.size() && vi < influences.size(); ++vi) {
        const Vertex &base = baseVertices[vi];
        const SkinInfluence &inf = influences[vi];

        glm::vec4 p(0.0f);
        glm::vec3 n(0.0f);
        glm::vec3 tangent(0.0f);
        float total = 0.0f;

        for (uint32_t i = 0; i < kMaxSkinInfluences; ++i) {
            const float w = inf.weight[i];
            const uint32_t bi = inf.boneIndex[i];
            if (w <= 0.0f || bi >= boneMatrices.size())
                continue;
            const glm::mat4 &m = boneMatrices[bi];
            p += w * (m * glm::vec4(base.pos, 1.0f));
            n += w * (glm::mat3(m) * base.normal);
            tangent += w * (glm::mat3(m) * glm::vec3(base.tangent));
            total += w;
        }

        if (total > kEpsilon) {
            outVertices[vi].pos = glm::vec3(p) * scaleFactor;
            if (glm::length2(n) > kEpsilon)
                outVertices[vi].normal = glm::normalize(n);
            if (glm::length2(tangent) > kEpsilon)
                outVertices[vi].tangent = glm::vec4(glm::normalize(tangent), base.tangent.w);
        } else {
            outVertices[vi].pos = base.pos * scaleFactor;
        }
    }
    return outVertices;
}

bool InxSkinnedMesh::ComputeSkinnedBounds(const std::vector<glm::mat4> &palette, glm::vec3 &outMin,
                                          glm::vec3 &outMax) const
{
    if (baseVertices.empty())
        return false;

    outMin = glm::vec3(std::numeric_limits<float>::max());
    outMax = glm::vec3(std::numeric_limits<float>::lowest());
    for (size_t vertexIndex = 0; vertexIndex < baseVertices.size(); ++vertexIndex) {
        const glm::vec3 basePosition = baseVertices[vertexIndex].pos;
        glm::vec4 skinnedPosition(0.0f);
        float totalWeight = 0.0f;
        if (vertexIndex < influences.size()) {
            const SkinInfluence &influence = influences[vertexIndex];
            for (uint32_t component = 0; component < kMaxSkinInfluences; ++component) {
                const float weight = influence.weight[component];
                const uint32_t boneIndex = influence.boneIndex[component];
                if (weight <= 0.0f || boneIndex >= palette.size())
                    continue;
                skinnedPosition += weight * (palette[boneIndex] * glm::vec4(basePosition, 1.0f));
                totalWeight += weight;
            }
        }
        const glm::vec3 position =
            totalWeight > kEpsilon ? glm::vec3(skinnedPosition) / totalWeight : basePosition * scaleFactor;
        if (!std::isfinite(position.x) || !std::isfinite(position.y) || !std::isfinite(position.z))
            continue;
        outMin = glm::min(outMin, position);
        outMax = glm::max(outMax, position);
    }
    return outMin.x <= outMax.x && outMin.y <= outMax.y && outMin.z <= outMax.z;
}

} // namespace infernux
