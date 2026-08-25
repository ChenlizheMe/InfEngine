#pragma once

#include <function/resources/InxMesh/InxMesh.h>

#include <glm/glm.hpp>
#include <glm/gtc/quaternion.hpp>

#include <array>
#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace infernux
{

constexpr uint32_t kMaxSkinInfluences = 4;

struct SkinInfluence
{
    std::array<uint32_t, kMaxSkinInfluences> boneIndex{0, 0, 0, 0};
    std::array<float, kMaxSkinInfluences> weight{0.0f, 0.0f, 0.0f, 0.0f};
};

struct SkinnedRuntimeBone
{
    std::string name;
    int nodeIndex = -1;
    glm::mat4 inverseBind{1.0f};
};

struct SkinnedRuntimeNode
{
    std::string name;
    int parent = -1;
    glm::mat4 bindLocal{1.0f};
    glm::mat4 bindGlobal{1.0f};
};

struct SkinnedRuntimeTrack
{
    /// Source-skeleton node targeted by this channel.  FBX channel names are
    /// resolved once during import and never participate in retargeting.
    int nodeIndex = -1;
    std::vector<std::pair<double, glm::vec3>> positions;
    std::vector<std::pair<double, glm::quat>> rotations;
    std::vector<std::pair<double, glm::vec3>> scales;
};

struct SkinnedRuntimeAnimation
{
    std::string name;
    double durationTicks = 0.0;
    double ticksPerSecond = 25.0;
    std::vector<SkinnedRuntimeTrack> tracks;
    /// Indexed by source-skeleton node; -1 means that node has no channel.
    std::vector<int> trackByNodeIndex;

    [[nodiscard]] float DurationSeconds() const;
};

struct SkeletonRetargetMap
{
    /// Target node index -> source node index; -1 leaves the target at bind pose.
    std::vector<int> targetToSourceNode;
    size_t mappedNodes = 0;
    size_t mappedAnimatedNodes = 0;
    size_t missingTargetDeformJoints = 0;
    size_t topologyDifferences = 0;
    bool identicalTopology = false;
};

/**
 * Runtime skeleton shared by a renderable skin and independently imported
 * animation sources.  Geometry,
 * inverse-bind data and animation tracks no
 * longer define asset identity together. Animation tracks are mapped to
 * this
 * target skeleton independently, with partial humanoid matches supported.
 */
class Skeleton
{
  public:
    std::vector<SkinnedRuntimeNode> nodes;
    std::unordered_map<std::string, int> nodeByName;
    std::vector<SkinnedRuntimeBone> bones;
    std::unordered_map<std::string, uint32_t> boneByName;

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] SkeletonRetargetMap BuildRetargetMap(const Skeleton &source,
                                                       const SkinnedRuntimeAnimation &animation) const;
    [[nodiscard]] bool IsAnimationCompatible(const Skeleton &source, const SkinnedRuntimeAnimation &animation,
                                             std::string *reason = nullptr) const;
    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept;
};

struct SkinnedNodePose
{
    glm::vec3 translation{0.0f};
    glm::quat rotation{1.0f, 0.0f, 0.0f, 0.0f};
    glm::vec3 scale{1.0f};
};

struct SkinnedSampleRequest
{
    std::string takeName; ///< Empty = bind pose (no animation applied)
    float timeSeconds = 0.0f;
    bool loop = true; ///< Loop wraps time (fmod); non-loop clamps so the end pose holds
    std::string blendTakeName;
    float blendTimeSeconds = 0.0f;
    float blendWeight = 0.0f;
};

/// One weighted contribution to a multi-layer pose blend (AnimationTree output).
/// Non-additive layers are combined as a coverage-normalized weighted average
/// toward bind pose; additive layers add their (sample − bind) delta on top.
/// An empty boneMask affects all nodes; otherwise only nodes whose name matches.
struct PoseStackLayer
{
    std::string takeName;
    /// GUID of the model that owns this take. Empty means the render model.
    std::string sourceModelGuid;
    float timeSeconds = 0.0f;
    float weight = 1.0f;
    bool additive = false;
    bool loop = true;
    std::vector<std::string> boneMask;
};

class InxSkinnedMesh
{
  public:
    std::string sourcePath;
    std::string guid;
    float scaleFactor = 0.01f;

    std::vector<Vertex> baseVertices;
    std::vector<SkinInfluence> influences;
    std::vector<uint32_t> indices;
    std::vector<SubMesh> subMeshes;
    Skeleton skeleton;
    std::vector<SkinnedRuntimeAnimation> animations;

    [[nodiscard]] bool IsValid() const
    {
        return !baseVertices.empty() && !indices.empty();
    }
    [[nodiscard]] bool IsAnimationSource() const
    {
        return skeleton.IsValid() && !animations.empty();
    }
    [[nodiscard]] bool IsAssetPayloadValid() const
    {
        return IsValid() || IsAnimationSource();
    }

    [[nodiscard]] const SkinnedRuntimeAnimation *FindAnimation(const std::string &takeName) const;
    [[nodiscard]] float GetAnimationDurationSeconds(const std::string &takeName) const;
    [[nodiscard]] std::vector<glm::mat4>
    BuildGpuBonePalette(const SkinnedSampleRequest &request, const InxSkinnedMesh *animationSource = nullptr,
                        const InxSkinnedMesh *blendAnimationSource = nullptr) const;
    [[nodiscard]] std::shared_ptr<const std::vector<glm::mat4>>
    GetOrBuildGpuBonePalette(const SkinnedSampleRequest &request, const InxSkinnedMesh *animationSource = nullptr,
                             const InxSkinnedMesh *blendAnimationSource = nullptr) const;
    [[nodiscard]] std::vector<Vertex> SampleVertices(const SkinnedSampleRequest &request) const;

    /// Compute a conservative local-space AABB for a GPU-skinned pose.  The
    /// palette already contains the model import scale, so the result is in
    /// the same space as the rendered vertices rather than the unscaled FBX
    /// bind-pose stream.
    [[nodiscard]] bool ComputeSkinnedBounds(const std::vector<glm::mat4> &palette, glm::vec3 &outMin,
                                            glm::vec3 &outMax) const;

    /// Build bone matrices from a multi-layer pose stack (N-way weighted +
    /// additive blending with optional per-layer bone masks). Used by the
    /// Python AnimationTree runtime. Not cached (the stack is dynamic).
    [[nodiscard]] std::vector<glm::mat4> BuildBoneMatricesFromPoseStack(
        const std::vector<PoseStackLayer> &layers,
        const std::vector<std::shared_ptr<const InxSkinnedMesh>> &animationSources = {}) const;
    [[nodiscard]] std::vector<glm::mat4> BuildGpuBonePaletteFromPoseStack(
        const std::vector<PoseStackLayer> &layers,
        const std::vector<std::shared_ptr<const InxSkinnedMesh>> &animationSources = {}) const;

    void NormalizeInfluences();
    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept;

  private:
    struct PaletteCacheKey
    {
        std::string takeName;
        int64_t timeMicros = 0;
        bool loop = true;
        std::string blendTakeName;
        std::string animationSourceKey;
        std::string blendAnimationSourceKey;
        int64_t blendTimeMicros = 0;
        int32_t blendWeightMicros = 0;

        bool operator==(const PaletteCacheKey &rhs) const
        {
            return takeName == rhs.takeName && timeMicros == rhs.timeMicros && loop == rhs.loop &&
                   blendTakeName == rhs.blendTakeName && animationSourceKey == rhs.animationSourceKey &&
                   blendAnimationSourceKey == rhs.blendAnimationSourceKey && blendTimeMicros == rhs.blendTimeMicros &&
                   blendWeightMicros == rhs.blendWeightMicros;
        }
    };

    struct PaletteCacheKeyHash
    {
        size_t operator()(const PaletteCacheKey &key) const;
    };

    [[nodiscard]] SkinnedNodePose SampleRetargetedNodePose(const SkinnedRuntimeAnimation *anim,
                                                           const Skeleton &sourceSkeleton, size_t targetNodeIndex,
                                                           int sourceNodeIndex, double tTicks) const;
    [[nodiscard]] std::vector<SkinnedNodePose> BuildRetargetedLocalPoses(const Skeleton &sourceSkeleton,
                                                                         const SkinnedRuntimeAnimation *animation,
                                                                         const SkeletonRetargetMap &mapping,
                                                                         double timeTicks) const;
    [[nodiscard]] std::vector<glm::mat4> BuildBoneMatrices(const SkinnedSampleRequest &request,
                                                           const InxSkinnedMesh *animationSource,
                                                           const InxSkinnedMesh *blendAnimationSource) const;
    [[nodiscard]] static PaletteCacheKey MakePaletteCacheKey(const SkinnedSampleRequest &request,
                                                             const InxSkinnedMesh *animationSource,
                                                             const InxSkinnedMesh *blendAnimationSource);

    mutable std::unordered_map<PaletteCacheKey, std::shared_ptr<const std::vector<glm::mat4>>, PaletteCacheKeyHash>
        m_gpuPaletteCache;
    mutable std::vector<PaletteCacheKey> m_gpuPaletteCacheOrder;
};

} // namespace infernux
