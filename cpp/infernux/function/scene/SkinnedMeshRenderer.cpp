#include "SkinnedMeshRenderer.h"
#include "ComponentFactory.h"
#include "SceneManager.h"

#include <core/config/MathConstants.h>
#include <core/log/InxLog.h>
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

void TransformBounds(const glm::mat4 &matrix, const glm::vec3 &localMin, const glm::vec3 &localMax, glm::vec3 &outMin,
                     glm::vec3 &outMax)
{
    const glm::vec3 center = (localMin + localMax) * 0.5f;
    const glm::vec3 extent = (localMax - localMin) * 0.5f;
    glm::vec3 worldCenter(0.0f);
    glm::vec3 worldExtent(0.0f);
    for (int axis = 0; axis < 3; ++axis) {
        worldCenter[axis] = matrix[3][axis];
        for (int component = 0; component < 3; ++component) {
            const float value = matrix[component][axis];
            worldCenter[axis] += value * center[component];
            worldExtent[axis] += std::abs(value) * extent[component];
        }
    }
    outMin = worldCenter - worldExtent;
    outMax = worldCenter + worldExtent;
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

void SkinnedMeshRenderer::ComputeWorldBounds(const glm::mat4 &worldMatrix, glm::vec3 &outMin, glm::vec3 &outMax) const
{
    const auto model = GetOrLoadRuntimeModel();
    const auto palette = m_skinPoseHistory.Current();
    glm::vec3 localMin;
    glm::vec3 localMax;
    if (model && palette && model->ComputeSkinnedBounds(*palette, localMin, localMax)) {
        TransformBounds(worldMatrix, localMin, localMax, outMin, outMax);
        return;
    }
    MeshRenderer::ComputeWorldBounds(worldMatrix, outMin, outMax);
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
    if (!mesh->HasSkinnedData() || !mesh->GetSkinnedData()->IsValid())
        throw std::invalid_argument("SkinnedMeshRenderer requires a renderable Mesh asset with skin data: " + guid);
    ClearRuntimeSkinnedMesh();
    SetMeshAsset(guid, mesh);
    m_animationSourceModel.reset();
    m_blendAnimationSourceModel.reset();
    RefreshRuntimeSkinnedMesh();
}

std::shared_ptr<const InxSkinnedMesh>
SkinnedMeshRenderer::LoadCompatibleAnimationSource(const std::string &guid, const std::string &takeName) const
{
    auto target = GetOrLoadRuntimeModel();
    if (!target || !target->IsValid())
        throw std::invalid_argument("SkinnedMeshRenderer has no renderable target skeleton");
    if (guid.empty() || guid == GetSourceModelGuid())
        return target;

    auto mesh = AssetRegistry::Instance().LoadAsset<InxMesh>(guid, ResourceType::Mesh);
    const auto source = mesh ? mesh->GetSkinnedData() : nullptr;
    if (!source || !source->IsAnimationSource())
        throw std::invalid_argument("animation source has no imported skeleton animation data: " + guid);

    const SkinnedRuntimeAnimation *animation = nullptr;
    if (!takeName.empty()) {
        for (const auto &candidate : source->animations) {
            if (candidate.name == takeName) {
                animation = &candidate;
                break;
            }
        }
        if (!animation)
            throw std::invalid_argument("animation take '" + takeName + "' was not found in source: " + guid);
    } else if (!source->animations.empty()) {
        animation = &source->animations.front();
    }
    if (!animation)
        throw std::invalid_argument("animation source has no takes: " + guid);

    std::string reason;
    if (!target->skeleton.IsAnimationCompatible(source->skeleton, *animation, &reason))
        throw std::invalid_argument("animation source skeleton is incompatible with render model: " + reason);
    if (!reason.empty()) {
        const std::string warningKey = GetSourceModelGuid() + "\n" + guid + "\n" + reason;
        if (m_reportedRetargetWarnings.insert(warningKey).second)
            INXLOG_WARN("SkinnedMeshRenderer: ", reason, " (animation source ", guid, ", target ", GetSourceModelGuid(),
                        ")");
    }
    return source;
}

void SkinnedMeshRenderer::SetAnimationSourceGuid(const std::string &guid)
{
    const std::string canonical = guid == GetSourceModelGuid() ? std::string() : guid;
    if (canonical == m_animationSourceGuid && (canonical.empty() || m_animationSourceModel))
        return;
    auto source = LoadCompatibleAnimationSource(canonical, m_activeTakeName);
    m_animationSourceGuid = canonical;
    m_animationSourceModel = canonical.empty() ? nullptr : std::move(source);
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
                                              float blendWeight, bool loop, const std::string &animationSourceGuid,
                                              const std::string &blendAnimationSourceGuid)
{
    const std::string activeGuid = animationSourceGuid == GetSourceModelGuid() ? std::string() : animationSourceGuid;
    const std::string blendGuid =
        blendAnimationSourceGuid == GetSourceModelGuid() ? std::string() : blendAnimationSourceGuid;
    auto activeSource = activeGuid.empty() ? GetOrLoadRuntimeModel()
                                           : (activeGuid == m_animationSourceGuid && m_animationSourceModel
                                                  ? m_animationSourceModel
                                                  : LoadCompatibleAnimationSource(activeGuid, takeName));
    auto blendSource =
        blendTakeName.empty()
            ? nullptr
            : (blendGuid.empty() ? GetOrLoadRuntimeModel()
                                 : (blendGuid == m_blendAnimationSourceGuid && m_blendAnimationSourceModel
                                        ? m_blendAnimationSourceModel
                                        : LoadCompatibleAnimationSource(blendGuid, blendTakeName)));
    // Switching back to the single-clip / crossfade path clears any active
    // pose stack so a stale AnimationTree/blend pose doesn't keep overriding.
    if (m_usePoseStack) {
        m_usePoseStack = false;
        m_poseStack.clear();
        m_poseStackAnimationSources.clear();
    }
    m_activeTakeName = takeName;
    m_animationSourceGuid = activeGuid;
    m_animationSourceModel = activeGuid.empty() ? nullptr : std::move(activeSource);
    m_runtimeAnimationTime = timeSeconds;
    m_runtimeAnimationNormalized = normalizedTime;
    m_blendTakeName = blendTakeName;
    m_blendAnimationSourceGuid = blendGuid;
    m_blendAnimationSourceModel = blendGuid.empty() ? nullptr : std::move(blendSource);
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
    m_blendAnimationSourceGuid.clear();
    m_blendAnimationSourceModel.reset();
    m_blendAnimationTime = 0.0f;
    m_blendWeight = 0.0f;
    RefreshRuntimeSkinnedMesh();
}

void SkinnedMeshRenderer::SubmitPoseStack(const std::vector<PoseStackLayer> &layers)
{
    std::vector<std::shared_ptr<const InxSkinnedMesh>> sources;
    sources.reserve(layers.size());
    for (size_t index = 0; index < layers.size(); ++index) {
        const auto &layer = layers[index];
        if (index < m_poseStack.size() && index < m_poseStackAnimationSources.size() &&
            layer.sourceModelGuid == m_poseStack[index].sourceModelGuid && m_poseStackAnimationSources[index]) {
            sources.push_back(m_poseStackAnimationSources[index]);
        } else {
            sources.push_back(LoadCompatibleAnimationSource(layer.sourceModelGuid, layer.takeName));
        }
    }
    m_poseStack = layers;
    m_poseStackAnimationSources = std::move(sources);
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
    m_poseStackAnimationSources.clear();
    RefreshRuntimeSkinnedMesh();
}

float SkinnedMeshRenderer::GetAnimationDurationSeconds(const std::string &takeName,
                                                       const std::string &animationSourceGuid) const
{
    auto model = LoadCompatibleAnimationSource(animationSourceGuid, takeName);
    return model ? model->GetAnimationDurationSeconds(takeName) : 0.0f;
}

void SkinnedMeshRenderer::ReloadSourceModel()
{
    m_runtimeModel.reset();
    m_animationSourceModel.reset();
    m_blendAnimationSourceModel.reset();
    m_poseStackAnimationSources.clear();
    m_skinPoseHistory.Reset();
    RefreshRuntimeSkinnedMesh();
}

bool SkinnedMeshRenderer::ReferencesModelGuid(const std::string &guid) const
{
    if (guid.empty())
        return false;
    if (GetSourceModelGuid() == guid || m_animationSourceGuid == guid || m_blendAnimationSourceGuid == guid)
        return true;
    return std::any_of(m_poseStack.begin(), m_poseStack.end(),
                       [&guid](const PoseStackLayer &layer) { return layer.sourceModelGuid == guid; });
}

void SkinnedMeshRenderer::ClearRuntimeSkinnedMesh()
{
    const auto pose = m_skinPoseHistory.Acquire();
    if (!m_runtimeModel && !m_animationSourceModel && !m_blendAnimationSourceModel &&
        m_poseStackAnimationSources.empty() && (!pose || (!pose->current && !pose->previous)))
        return;
    m_runtimeModel.reset();
    m_animationSourceModel.reset();
    m_blendAnimationSourceModel.reset();
    m_poseStackAnimationSources.clear();
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
    if (!m_animationSourceGuid.empty() && !m_animationSourceModel)
        m_animationSourceModel = LoadCompatibleAnimationSource(m_animationSourceGuid, m_activeTakeName);
    if (!m_blendAnimationSourceGuid.empty() && !m_blendAnimationSourceModel && !m_blendTakeName.empty())
        m_blendAnimationSourceModel = LoadCompatibleAnimationSource(m_blendAnimationSourceGuid, m_blendTakeName);
    if (m_usePoseStack && m_poseStackAnimationSources.size() != m_poseStack.size()) {
        m_poseStackAnimationSources.clear();
        m_poseStackAnimationSources.reserve(m_poseStack.size());
        for (const auto &layer : m_poseStack)
            m_poseStackAnimationSources.push_back(LoadCompatibleAnimationSource(layer.sourceModelGuid, layer.takeName));
    }
    if (modelChanged)
        MarkMeshBufferDirty();
    m_animationTakeNames.clear();
    const auto animationSource = m_animationSourceModel ? m_animationSourceModel : model;
    m_animationTakeNames.reserve(animationSource->animations.size());
    for (const auto &animation : animationSource->animations)
        m_animationTakeNames.push_back(animation.name);

    std::shared_ptr<const std::vector<glm::mat4>> nextPalette;
    if (m_usePoseStack) {
        // AnimationTree path: N-way weighted + additive + masked blend.
        // Not cached (the stack changes most frames); built fresh each refresh.
        nextPalette = std::make_shared<const std::vector<glm::mat4>>(
            model->BuildGpuBonePaletteFromPoseStack(m_poseStack, m_poseStackAnimationSources));
    } else {
        SkinnedSampleRequest request;
        request.takeName = m_activeTakeName;
        request.timeSeconds = m_runtimeAnimationTime;
        request.loop = m_runtimeAnimationLoop;
        request.blendTakeName = m_blendTakeName;
        request.blendTimeSeconds = m_blendAnimationTime;
        request.blendWeight = m_blendWeight;
        nextPalette =
            model->GetOrBuildGpuBonePalette(request, animationSource.get(), m_blendAnimationSourceModel.get());
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
