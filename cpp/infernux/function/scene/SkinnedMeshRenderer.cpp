#include "SkinnedMeshRenderer.h"
#include "ComponentFactory.h"
#include "SceneManager.h"

#include <core/config/MathConstants.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMesh/InxMesh.h>

#include <algorithm>
#include <cmath>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace infernux
{

INFERNUX_REGISTER_VALIDATED_COMPONENT("SkinnedMeshRenderer", SkinnedMeshRenderer)

namespace
{
const std::vector<glm::mat4> &EmptySkinPalette()
{
    static const std::vector<glm::mat4> empty;
    return empty;
}
const std::vector<Vertex> &EmptyVertices()
{
    static const std::vector<Vertex> empty;
    return empty;
}
const std::vector<uint32_t> &EmptyIndices()
{
    static const std::vector<uint32_t> empty;
    return empty;
}
const std::vector<SubMesh> &EmptySubMeshes()
{
    static const std::vector<SubMesh> empty;
    return empty;
}
} // namespace

bool SkinnedMeshRenderer::HasRuntimeSkinnedMesh() const
{
    const auto pose = m_skinPoseHistory.Acquire();
    return m_runtimeModel && m_runtimeModel->IsValid() && pose && pose->IsValid();
}

const std::vector<Vertex> &SkinnedMeshRenderer::GetRuntimeSkinnedVertices() const
{
    return m_runtimeModel ? m_runtimeModel->baseVertices : EmptyVertices();
}

const std::vector<uint32_t> &SkinnedMeshRenderer::GetRuntimeSkinnedIndices() const
{
    return m_runtimeModel ? m_runtimeModel->indices : EmptyIndices();
}

const std::vector<SubMesh> &SkinnedMeshRenderer::GetRuntimeSkinnedSubMeshes() const
{
    return m_runtimeModel ? m_runtimeModel->subMeshes : EmptySubMeshes();
}

const std::vector<glm::mat4> &SkinnedMeshRenderer::GetRuntimeSkinBoneMatrices() const
{
    const auto palette = m_skinPoseHistory.Current();
    return palette ? *palette : EmptySkinPalette();
}

void SkinnedMeshRenderer::SetSourceModelGuid(const std::string &guid)
{
    if (GetMeshAssetGuid() == guid && m_runtimeModel)
        return;
    if (guid.empty()) {
        ClearRuntimeSkinnedMesh();
        ClearMeshAsset();
        return;
    }
    auto mesh = AssetRegistry::Instance().LoadAsset<InxMesh>(guid, ResourceType::Mesh);
    if (!mesh)
        throw std::invalid_argument("SkinnedMeshRenderer could not load Mesh asset GUID: " + guid);
    if (!mesh->HasSkinnedData())
        throw std::invalid_argument("SkinnedMeshRenderer requires a Mesh asset with skin or animation data: " + guid);
    ClearRuntimeSkinnedMesh();
    SetMeshAsset(guid, mesh);
    RefreshRuntimeSkinnedMesh();
}

void SkinnedMeshRenderer::SetActiveTakeName(const std::string &name)
{
    if (m_activeTakeName == name)
        return;
    m_activeTakeName = name;
    RefreshRuntimeSkinnedMesh();
}

void SkinnedMeshRenderer::SetRuntimeAnimationTime(float t)
{
    if (std::abs(m_runtimeAnimationTime - t) <= kEpsilon)
        return;
    m_runtimeAnimationTime = t;
    RefreshRuntimeSkinnedMesh();
}

void SkinnedMeshRenderer::SetRuntimeAnimationNormalizedTime(float n)
{
    m_runtimeAnimationNormalized = n;
}

void SkinnedMeshRenderer::SubmitAnimationPose(const std::string &takeName, float timeSeconds, float normalizedTime,
                                              const std::string &blendTakeName, float blendTimeSeconds,
                                              float blendWeight, bool loop)
{
    // Switching back to the single-clip / crossfade path clears any active
    // pose stack so a stale AnimationTree/blend pose doesn't keep overriding.
    if (m_usePoseStack) {
        m_usePoseStack = false;
        m_poseStack.clear();
    }
    m_activeTakeName = takeName;
    m_runtimeAnimationTime = timeSeconds;
    m_runtimeAnimationNormalized = normalizedTime;
    m_blendTakeName = blendTakeName;
    m_blendAnimationTime = blendTimeSeconds;
    m_blendWeight = std::clamp(blendWeight, 0.0f, 1.0f);
    m_runtimeAnimationLoop = loop;
    RefreshRuntimeSkinnedMesh();
}

void SkinnedMeshRenderer::SetBlendTakeName(const std::string &name)
{
    if (m_blendTakeName == name)
        return;
    m_blendTakeName = name;
    RefreshRuntimeSkinnedMesh();
}

void SkinnedMeshRenderer::SetBlendAnimationTime(float t)
{
    m_blendAnimationTime = t;
    RefreshRuntimeSkinnedMesh();
}

void SkinnedMeshRenderer::SetBlendWeight(float w)
{
    const float clamped = std::clamp(w, 0.0f, 1.0f);
    if (std::abs(m_blendWeight - clamped) <= kEpsilon)
        return;
    m_blendWeight = clamped;
    RefreshRuntimeSkinnedMesh();
}

void SkinnedMeshRenderer::ClearAnimationBlend()
{
    if (m_blendTakeName.empty() && m_blendAnimationTime == 0.0f && m_blendWeight == 0.0f)
        return;
    m_blendTakeName.clear();
    m_blendAnimationTime = 0.0f;
    m_blendWeight = 0.0f;
    RefreshRuntimeSkinnedMesh();
}

void SkinnedMeshRenderer::SubmitPoseStack(const std::vector<PoseStackLayer> &layers)
{
    m_poseStack = layers;
    m_usePoseStack = true;
    // Keep a representative active take name so inspector/UI reflect playback.
    if (!layers.empty()) {
        const PoseStackLayer *dominant = &layers.front();
        for (const auto &ly : layers)
            if (!ly.additive && ly.weight > dominant->weight)
                dominant = &ly;
        m_activeTakeName = dominant->takeName;
        m_runtimeAnimationTime = dominant->timeSeconds;
    }
    RefreshRuntimeSkinnedMesh();
}

void SkinnedMeshRenderer::ClearPoseStack()
{
    if (!m_usePoseStack)
        return;
    m_usePoseStack = false;
    m_poseStack.clear();
    RefreshRuntimeSkinnedMesh();
}

float SkinnedMeshRenderer::GetAnimationDurationSeconds(const std::string &takeName) const
{
    auto model = GetOrLoadRuntimeModel();
    return model ? model->GetAnimationDurationSeconds(takeName) : 0.0f;
}

void SkinnedMeshRenderer::ReloadSourceModel()
{
    m_runtimeModel.reset();
    m_skinPoseHistory.Reset();
    RefreshRuntimeSkinnedMesh();
}

void SkinnedMeshRenderer::ClearRuntimeSkinnedMesh()
{
    const auto pose = m_skinPoseHistory.Acquire();
    if (!m_runtimeModel && (!pose || (!pose->current && !pose->previous)))
        return;
    m_runtimeModel.reset();
    m_skinPoseHistory.Reset();
    MarkMeshBufferDirty();
}

std::shared_ptr<const InxSkinnedMesh> SkinnedMeshRenderer::GetOrLoadRuntimeModel() const
{
    if (m_runtimeModel && m_runtimeModel->IsValid())
        return m_runtimeModel;
    const auto mesh = GetMeshAssetRef().Get();
    if (!mesh)
        return nullptr;
    m_runtimeModel = mesh->GetSkinnedData();
    return m_runtimeModel;
}

void SkinnedMeshRenderer::RefreshRuntimeSkinnedMesh()
{
    if (!HasMeshAsset()) {
        ClearRuntimeSkinnedMesh();
        return;
    }
    // NOTE: an empty m_activeTakeName is a valid state — it renders the bind
    // pose (FindAnimation("") → nullptr → SampleNodePose falls back to the
    // node's bind-local TRS). The old behavior of clearing the mesh made
    // characters invisible before the Animator's first play() and after
    // stop(), which was a P0 correctness bug.

    const InxSkinnedMesh *previousModel = m_runtimeModel.get();
    auto model = GetOrLoadRuntimeModel();
    if (!model || !model->IsValid()) {
        ClearRuntimeSkinnedMesh();
        return;
    }

    const bool modelChanged = previousModel != model.get();
    m_runtimeModel = model;
    if (modelChanged)
        MarkMeshBufferDirty();
    m_animationTakeNames.clear();
    m_animationTakeNames.reserve(model->animations.size());
    for (const auto &animation : model->animations)
        m_animationTakeNames.push_back(animation.name);

    std::shared_ptr<const std::vector<glm::mat4>> nextPalette;
    if (m_usePoseStack) {
        // AnimationTree path: N-way weighted + additive + masked blend.
        // Not cached (the stack changes most frames); built fresh each refresh.
        nextPalette =
            std::make_shared<const std::vector<glm::mat4>>(model->BuildGpuBonePaletteFromPoseStack(m_poseStack));
    } else {
        SkinnedSampleRequest request;
        request.takeName = m_activeTakeName;
        request.timeSeconds = m_runtimeAnimationTime;
        request.loop = m_runtimeAnimationLoop;
        request.blendTakeName = m_blendTakeName;
        request.blendTimeSeconds = m_blendAnimationTime;
        request.blendWeight = m_blendWeight;
        nextPalette = model->GetOrBuildGpuBonePalette(request);
    }
    m_skinPoseHistory.Publish(std::move(nextPalette), modelChanged);
    SceneManager::Instance().NotifyMeshRendererContentChanged(this);
    if (modelChanged)
        SceneManager::Instance().NotifyMeshRendererChanged(this);
}

nlohmann::json SkinnedMeshRenderer::SerializeDocument() const
{
    json j = MeshRenderer::SerializeDocument();
    if (!m_activeTakeName.empty())
        j["activeTakeName"] = m_activeTakeName;
    return j;
}

void SkinnedMeshRenderer::ValidateSerializedDocument(const nlohmann::json &document)
{
    ValidateSerializedDocumentForType(document, "SkinnedMeshRenderer");
}

bool SkinnedMeshRenderer::DeserializeDocument(const nlohmann::json &j)
{
    if (!MeshRenderer::DeserializeDocument(j))
        return false;

    try {
        m_activeTakeName = j.value("activeTakeName", std::string());
        RefreshRuntimeSkinnedMesh();
        return true;
    } catch (...) {
        return false;
    }
}

std::unique_ptr<Component> SkinnedMeshRenderer::Clone() const
{
    auto clone = std::make_unique<SkinnedMeshRenderer>();
    const uint64_t newId = clone->GetComponentID();
    clone->DeserializeDocument(SerializeDocument());
    clone->SetComponentID(newId);
    return clone;
}

} // namespace infernux
