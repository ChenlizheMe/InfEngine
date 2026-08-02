#pragma once

#include "MeshRenderer.h"
#include "SkinPoseHistory.h"

#include <function/resources/InxSkinnedMesh/InxSkinnedMesh.h>

#include <glm/glm.hpp>
#include <memory>
#include <string>
#include <vector>

namespace infernux
{

class InxSkinnedMesh;

/**
 * @brief Renderer component for animated skeletal model instances.
 *
 * Runtime skeleton/animation data is the immutable skinned payload of the
 * referenced InxMesh asset.
 * This
 * component owns playback-facing state and exposes bind-pose vertices plus a per-frame GPU bone palette to the
 * renderer.
 */
class SkinnedMeshRenderer : public MeshRenderer
{
  public:
    SkinnedMeshRenderer() = default;

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "SkinnedMeshRenderer";
    }

    void SetSourceModelGuid(const std::string &guid);
    [[nodiscard]] const std::string &GetSourceModelGuid() const
    {
        return GetMeshAssetGuid();
    }

    [[nodiscard]] const std::vector<std::string> &GetAnimationTakeNames() const
    {
        return m_animationTakeNames;
    }

    void SetActiveTakeName(const std::string &name);
    [[nodiscard]] const std::string &GetActiveTakeName() const
    {
        return m_activeTakeName;
    }

    /// Elapsed time in the current clip (seconds). Runtime-only, not serialized — fed by SkeletalAnimator.
    void SetRuntimeAnimationTime(float t);
    [[nodiscard]] float GetRuntimeAnimationTime() const
    {
        return m_runtimeAnimationTime;
    }

    /// Normalized clip progress [0,1] when a duration is known. Runtime-only.
    void SetRuntimeAnimationNormalizedTime(float n);
    [[nodiscard]] float GetRuntimeAnimationNormalizedTime() const
    {
        return m_runtimeAnimationNormalized;
    }

    /// Optional second clip for cross-fade / pose blending. Runtime-only.
    /// An empty takeName renders the bind pose (mesh stays visible).
    /// loop=false clamps sampling time so a finished clip holds its end pose.
    void SubmitAnimationPose(const std::string &takeName, float timeSeconds, float normalizedTime,
                             const std::string &blendTakeName, float blendTimeSeconds, float blendWeight,
                             bool loop = true);
    void SetBlendTakeName(const std::string &name);
    [[nodiscard]] const std::string &GetBlendTakeName() const
    {
        return m_blendTakeName;
    }
    void SetBlendAnimationTime(float t);
    [[nodiscard]] float GetBlendAnimationTime() const
    {
        return m_blendAnimationTime;
    }
    void SetBlendWeight(float w);
    [[nodiscard]] float GetBlendWeight() const
    {
        return m_blendWeight;
    }
    void ClearAnimationBlend();

    /// Submit a multi-layer pose stack (AnimationTree output: N-way weighted +
    /// additive blend with optional per-layer bone masks). This overrides the
    /// single-clip / crossfade path until ClearPoseStack() is called.
    void SubmitPoseStack(const std::vector<PoseStackLayer> &layers);
    void ClearPoseStack();
    [[nodiscard]] bool HasPoseStack() const
    {
        return m_usePoseStack;
    }

    [[nodiscard]] bool HasAnimationTakes() const
    {
        return !m_animationTakeNames.empty();
    }
    [[nodiscard]] float GetAnimationDurationSeconds(const std::string &takeName) const;

    void ReloadSourceModel();

    [[nodiscard]] bool HasRuntimeSkinnedMesh() const;
    [[nodiscard]] const std::vector<Vertex> &GetRuntimeSkinnedVertices() const;
    [[nodiscard]] const std::vector<uint32_t> &GetRuntimeSkinnedIndices() const;
    [[nodiscard]] const std::vector<SubMesh> &GetRuntimeSkinnedSubMeshes() const;
    [[nodiscard]] std::shared_ptr<const InxSkinnedMesh> GetRuntimeModelSnapshot() const
    {
        return m_runtimeModel;
    }
    [[nodiscard]] const std::vector<glm::mat4> &GetRuntimeSkinBoneMatrices() const;
    [[nodiscard]] std::shared_ptr<const std::vector<glm::mat4>> GetRuntimeSkinBonePalette() const
    {
        return m_skinPoseHistory.Current();
    }
    [[nodiscard]] std::shared_ptr<const std::vector<glm::mat4>> GetPreviousRuntimeSkinBonePalette() const
    {
        return m_skinPoseHistory.Previous();
    }
    [[nodiscard]] SkinPoseHistory::SnapshotPtr GetRuntimeSkinPoseSnapshot() const
    {
        return m_skinPoseHistory.Acquire();
    }

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    static void ValidateSerializedDocument(const nlohmann::json &document);
    bool DeserializeDocument(const nlohmann::json &document) override;
    [[nodiscard]] std::unique_ptr<Component> Clone() const override;

  private:
    void RefreshRuntimeSkinnedMesh();
    void ClearRuntimeSkinnedMesh();
    [[nodiscard]] std::shared_ptr<const InxSkinnedMesh> GetOrLoadRuntimeModel() const;

    std::vector<std::string> m_animationTakeNames;
    std::string m_activeTakeName;
    float m_runtimeAnimationTime = 0.0f;
    float m_runtimeAnimationNormalized = 0.0f;
    std::string m_blendTakeName;
    float m_blendAnimationTime = 0.0f;
    float m_blendWeight = 0.0f;
    bool m_runtimeAnimationLoop = true;
    std::vector<PoseStackLayer> m_poseStack;
    bool m_usePoseStack = false;
    SkinPoseHistory m_skinPoseHistory;
    mutable std::shared_ptr<const InxSkinnedMesh> m_runtimeModel;
};

} // namespace infernux
