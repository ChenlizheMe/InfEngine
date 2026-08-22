#include "InxSkinnedMesh.h"

#include <core/config/MathConstants.h>
#include <core/log/InxLog.h>

#define GLM_ENABLE_EXPERIMENTAL
#include <glm/gtc/matrix_transform.hpp>
#include <glm/gtx/norm.hpp>
#include <glm/gtx/quaternion.hpp>

#include <algorithm>
#include <cmath>
#include <functional>

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

size_t InxSkinnedMesh::GetRuntimeMemoryBytes() const noexcept
{
    size_t bytes = sizeof(*this) + sourcePath.capacity() + guid.capacity();
    bytes += baseVertices.capacity() * sizeof(Vertex);
    bytes += influences.capacity() * sizeof(SkinInfluence);
    bytes += indices.capacity() * sizeof(uint32_t);
    bytes += subMeshes.capacity() * sizeof(SubMesh);
    for (const auto &subMesh : subMeshes)
        bytes += subMesh.name.capacity();
    bytes += nodes.capacity() * sizeof(SkinnedRuntimeNode);
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
    bytes += animations.capacity() * sizeof(SkinnedRuntimeAnimation);
    for (const auto &animation : animations) {
        bytes += animation.name.capacity();
        bytes += animation.tracks.capacity() * sizeof(SkinnedRuntimeTrack);
        for (const auto &track : animation.tracks) {
            bytes += track.nodeName.capacity();
            bytes += track.positions.capacity() * sizeof(decltype(track.positions)::value_type);
            bytes += track.rotations.capacity() * sizeof(decltype(track.rotations)::value_type);
            bytes += track.scales.capacity() * sizeof(decltype(track.scales)::value_type);
        }
        bytes += animation.trackByNode.bucket_count() * sizeof(void *);
        for (const auto &[name, index] : animation.trackByNode) {
            (void)index;
            bytes += sizeof(std::pair<const std::string, size_t>) + name.capacity();
        }
    }
    bytes += m_gpuPaletteCache.bucket_count() * sizeof(void *);
    for (const auto &[key, palette] : m_gpuPaletteCache) {
        bytes +=
            sizeof(decltype(m_gpuPaletteCache)::value_type) + key.takeName.capacity() + key.blendTakeName.capacity();
        if (palette)
            bytes += sizeof(*palette) + palette->capacity() * sizeof(glm::mat4);
    }
    bytes += m_gpuPaletteCacheOrder.capacity() * sizeof(PaletteCacheKey);
    for (const auto &key : m_gpuPaletteCacheOrder)
        bytes += key.takeName.capacity() + key.blendTakeName.capacity();
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
    HashCombine(seed, std::hash<int64_t>{}(key.blendTimeMicros));
    HashCombine(seed, std::hash<int32_t>{}(key.blendWeightMicros));
    return seed;
}

InxSkinnedMesh::PaletteCacheKey InxSkinnedMesh::MakePaletteCacheKey(const SkinnedSampleRequest &request)
{
    PaletteCacheKey key;
    key.takeName = request.takeName;
    key.timeMicros = QuantizeSeconds(request.timeSeconds);
    key.loop = request.loop;
    key.blendTakeName = request.blendTakeName;
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

SkinnedNodePose InxSkinnedMesh::SampleNodePose(const SkinnedRuntimeAnimation *anim, const SkinnedRuntimeNode &node,
                                               double tTicks) const
{
    SkinnedNodePose pose = BindNodePose(node);
    if (!anim)
        return pose;

    auto trIt = anim->trackByNode.find(node.name);
    if (trIt == anim->trackByNode.end())
        return pose;

    const SkinnedRuntimeTrack &track = anim->tracks[trIt->second];
    pose.translation = SampleVec3(track.positions, tTicks, pose.translation);
    pose.rotation = SampleQuat(track.rotations, tTicks, pose.rotation);
    pose.scale = SampleVec3(track.scales, tTicks, pose.scale);
    return pose;
}

std::vector<glm::mat4> InxSkinnedMesh::BuildBoneMatrices(const SkinnedSampleRequest &request) const
{
    const SkinnedRuntimeAnimation *anim = FindAnimation(request.takeName);
    const SkinnedRuntimeAnimation *blendAnim =
        (request.blendWeight > 0.0f && !request.blendTakeName.empty()) ? FindAnimation(request.blendTakeName) : nullptr;
    const double tTicks = ToAnimationTicks(anim, request.timeSeconds, request.loop);
    // Blend source always loops: it represents the outgoing state mid-fade.
    const double blendTicks = ToAnimationTicks(blendAnim, request.blendTimeSeconds, true);
    // Same-take cross-fades at different times are valid (e.g. restarting a
    // clip with a fade) — only a missing blend animation disables blending.
    const float w = blendAnim ? glm::clamp(request.blendWeight, 0.0f, 1.0f) : 0.0f;

    std::vector<glm::mat4> globals(nodes.size(), glm::mat4(1.0f));
    for (size_t ni = 0; ni < nodes.size(); ++ni) {
        const SkinnedRuntimeNode &node = nodes[ni];
        SkinnedNodePose pose = SampleNodePose(anim, node, tTicks);
        if (w > 0.0f) {
            SkinnedNodePose target = SampleNodePose(blendAnim, node, blendTicks);
            pose = BlendNodePose(pose, target, w);
        }

        glm::mat4 local = MakeTRS(pose.translation, pose.rotation, pose.scale);
        globals[ni] = (node.parent >= 0) ? globals[static_cast<size_t>(node.parent)] * local : local;
    }

    std::vector<glm::mat4> boneMatrices(bones.size(), glm::mat4(1.0f));
    for (size_t bi = 0; bi < bones.size(); ++bi) {
        const SkinnedRuntimeBone &bone = bones[bi];
        if (bone.nodeIndex >= 0 && static_cast<size_t>(bone.nodeIndex) < globals.size())
            boneMatrices[bi] = globals[static_cast<size_t>(bone.nodeIndex)] * bone.inverseBind;
    }
    return boneMatrices;
}

std::vector<glm::mat4> InxSkinnedMesh::BuildBoneMatricesFromPoseStack(const std::vector<PoseStackLayer> &layers) const
{
    const size_t N = nodes.size();

    // Bind pose per node (the base / fallback for uncovered nodes).
    std::vector<SkinnedNodePose> bind(N);
    for (size_t ni = 0; ni < N; ++ni)
        bind[ni] = BindNodePose(nodes[ni]);

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

    for (const PoseStackLayer &layer : layers) {
        const float w = glm::clamp(layer.weight, 0.0f, 1.0f);
        if (w <= kEpsilon)
            continue;
        const SkinnedRuntimeAnimation *anim = FindAnimation(layer.takeName);
        const double tTicks = ToAnimationTicks(anim, layer.timeSeconds, layer.loop);

        // Resolve the optional bone mask to a node-name set (empty = all nodes).
        const bool masked = !layer.boneMask.empty();
        std::unordered_map<std::string, char> maskSet;
        if (masked)
            for (const std::string &b : layer.boneMask)
                maskSet.emplace(b, 1);

        for (size_t ni = 0; ni < N; ++ni) {
            if (masked && maskSet.find(nodes[ni].name) == maskSet.end())
                continue;
            const SkinnedNodePose pose = SampleNodePose(anim, nodes[ni], tTicks);
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
        globals[ni] = (nodes[ni].parent >= 0) ? globals[static_cast<size_t>(nodes[ni].parent)] * local : local;
    }

    std::vector<glm::mat4> boneMatrices(bones.size(), glm::mat4(1.0f));
    for (size_t bi = 0; bi < bones.size(); ++bi) {
        const SkinnedRuntimeBone &bone = bones[bi];
        if (bone.nodeIndex >= 0 && static_cast<size_t>(bone.nodeIndex) < globals.size())
            boneMatrices[bi] = globals[static_cast<size_t>(bone.nodeIndex)] * bone.inverseBind;
    }
    return boneMatrices;
}

std::vector<glm::mat4> InxSkinnedMesh::BuildGpuBonePalette(const SkinnedSampleRequest &request) const
{
    std::vector<glm::mat4> palette = BuildBoneMatrices(request);
    const glm::mat4 scale = glm::scale(glm::mat4(1.0f), glm::vec3(scaleFactor));
    for (glm::mat4 &m : palette)
        m = scale * m;
    return palette;
}

std::vector<glm::mat4> InxSkinnedMesh::BuildGpuBonePaletteFromPoseStack(const std::vector<PoseStackLayer> &layers) const
{
    std::vector<glm::mat4> palette = BuildBoneMatricesFromPoseStack(layers);
    const glm::mat4 scale = glm::scale(glm::mat4(1.0f), glm::vec3(scaleFactor));
    for (glm::mat4 &m : palette)
        m = scale * m;
    return palette;
}

std::shared_ptr<const std::vector<glm::mat4>>
InxSkinnedMesh::GetOrBuildGpuBonePalette(const SkinnedSampleRequest &request) const
{
    PaletteCacheKey key = MakePaletteCacheKey(request);
    auto it = m_gpuPaletteCache.find(key);
    if (it != m_gpuPaletteCache.end())
        return it->second;

    auto palette = std::make_shared<const std::vector<glm::mat4>>(BuildGpuBonePalette(request));
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

    if (bones.empty()) {
        for (auto &v : outVertices)
            v.pos *= scaleFactor;
        return outVertices;
    }

    const std::vector<glm::mat4> boneMatrices = BuildBoneMatrices(request);

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

} // namespace infernux
