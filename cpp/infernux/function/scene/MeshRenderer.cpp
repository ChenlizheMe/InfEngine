#include "MeshRenderer.h"
#include "ComponentDocumentValidation.h"
#include "ComponentFactory.h"
#include "GameObject.h"
#include "MeshCollider.h"
#include "SceneManager.h"
#include <algorithm>
#include <core/log/InxLog.h>
#include <cstring>
#include <function/resources/AssetDependencyGraph.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMaterial/MaterialDocumentValidation.h>
#include <function/scene/PrimitiveMeshes.h>
#include <limits>
#include <nlohmann/json.hpp>
#include <set>

using json = nlohmann::json;

namespace infernux
{

namespace
{

bool MeshDataEquals(const std::shared_ptr<InxMesh> &mesh, const std::vector<Vertex> &vertices,
                    const std::vector<uint32_t> &indices)
{
    if (!mesh)
        return false;
    if (mesh->GetVertices().size() != vertices.size() || mesh->GetIndices().size() != indices.size())
        return false;

    const bool sameVertices = vertices.empty() || std::memcmp(mesh->GetVertices().data(), vertices.data(),
                                                              vertices.size() * sizeof(Vertex)) == 0;
    const bool sameIndices = indices.empty() || std::memcmp(mesh->GetIndices().data(), indices.data(),
                                                            indices.size() * sizeof(uint32_t)) == 0;
    return sameVertices && sameIndices;
}

std::string FindMatchingMeshAssetGuid(const std::vector<Vertex> &vertices, const std::vector<uint32_t> &indices,
                                      const std::string &preferredName)
{
    if (vertices.empty() || indices.empty())
        return {};

    auto &registry = AssetRegistry::Instance();
    auto *assetDb = registry.GetAssetDatabase();
    if (!assetDb)
        return {};

    auto tryFind = [&](bool requirePreferredName) -> std::string {
        for (const auto &guid : assetDb->GetAllGuids()) {
            const auto meta = assetDb->GetMetaByGuid(guid);
            if (!meta || meta->GetResourceType() != ResourceType::Mesh)
                continue;

            if (requirePreferredName) {
                if (preferredName.empty() || meta->GetResourceName() != preferredName)
                    continue;
            }

            auto mesh = registry.GetAsset<InxMesh>(guid);
            if (!mesh)
                mesh = registry.LoadAsset<InxMesh>(guid, ResourceType::Mesh);
            if (!MeshDataEquals(mesh, vertices, indices))
                continue;
            return guid;
        }
        return {};
    };

    if (!preferredName.empty()) {
        if (auto guid = tryFind(true); !guid.empty())
            return guid;
    }

    return tryFind(false);
}

bool GetBuiltinPrimitiveMeshData(const std::string &name, const std::vector<Vertex> *&vertices,
                                 const std::vector<uint32_t> *&indices)
{
    vertices = nullptr;
    indices = nullptr;

    if (name == "Cube") {
        vertices = &PrimitiveMeshes::GetCubeVertices();
        indices = &PrimitiveMeshes::GetCubeIndices();
    } else if (name == "Sphere") {
        vertices = &PrimitiveMeshes::GetSphereVertices();
        indices = &PrimitiveMeshes::GetSphereIndices();
    } else if (name == "Capsule") {
        vertices = &PrimitiveMeshes::GetCapsuleVertices();
        indices = &PrimitiveMeshes::GetCapsuleIndices();
    } else if (name == "Cylinder") {
        vertices = &PrimitiveMeshes::GetCylinderVertices();
        indices = &PrimitiveMeshes::GetCylinderIndices();
    } else if (name == "Plane") {
        vertices = &PrimitiveMeshes::GetPlaneVertices();
        indices = &PrimitiveMeshes::GetPlaneIndices();
    } else if (name == "Quad") {
        vertices = &PrimitiveMeshes::GetQuadVertices();
        indices = &PrimitiveMeshes::GetQuadIndices();
    }

    return vertices != nullptr && indices != nullptr;
}

bool MatchesBuiltinPrimitiveMesh(const std::string &name, const std::vector<Vertex> &vertices,
                                 const std::vector<uint32_t> &indices)
{
    const std::vector<Vertex> *builtinVertices = nullptr;
    const std::vector<uint32_t> *builtinIndices = nullptr;
    if (!GetBuiltinPrimitiveMeshData(name, builtinVertices, builtinIndices))
        return false;

    if (builtinVertices->size() != vertices.size() || builtinIndices->size() != indices.size())
        return false;

    const bool sameVertices = builtinVertices->empty() || std::memcmp(builtinVertices->data(), vertices.data(),
                                                                      vertices.size() * sizeof(Vertex)) == 0;
    const bool sameIndices = builtinIndices->empty() || std::memcmp(builtinIndices->data(), indices.data(),
                                                                    indices.size() * sizeof(uint32_t)) == 0;
    return sameVertices && sameIndices;
}

void RestoreBuiltinPrimitiveMesh(const std::string &name, std::vector<Vertex> &vertices, std::vector<uint32_t> &indices)
{
    const std::vector<Vertex> *builtinVertices = nullptr;
    const std::vector<uint32_t> *builtinIndices = nullptr;
    if (!GetBuiltinPrimitiveMeshData(name, builtinVertices, builtinIndices))
        return;

    vertices.assign(builtinVertices->begin(), builtinVertices->end());
    indices.assign(builtinIndices->begin(), builtinIndices->end());
}

void NotifyRenderableStateChanged(MeshRenderer *renderer)
{
    if (renderer)
        SceneManager::Instance().NotifyMeshRendererChanged(renderer);
}

void NotifyCollisionGeometryChanged(MeshRenderer *renderer)
{
    NotifyRenderableStateChanged(renderer);
    auto *gameObject = renderer ? renderer->GetGameObject() : nullptr;
    if (!gameObject)
        return;
    for (auto *collider : gameObject->GetComponents<MeshCollider>()) {
        if (collider)
            collider->OnMeshGeometryChanged();
    }
}

} // namespace

INFERNUX_REGISTER_VALIDATED_COMPONENT("MeshRenderer", MeshRenderer)

MeshRenderer::~MeshRenderer()
{
    // Remove all dependency edges from this instance in the unified graph
    AssetDependencyGraph::Instance().ClearRuntimeDependenciesOf(GetInstanceGuid());
    // Safety net: ensure we're removed from the registry even if
    // OnDisable wasn't called (e.g. direct destruction during scene teardown).
    SceneManager::Instance().UnregisterMeshRenderer(this);
}

void MeshRenderer::OnEnable()
{
    // Only runtime-resident scenes contribute to the global renderer list.
    // Utility scenes stay isolated, while DontDestroyOnLoad renderers can be
    // disabled and re-enabled without disappearing from rendering.
    if (auto *go = GetGameObject())
        if (!SceneManager::Instance().IsRuntimeScene(go->GetScene()))
            return;
    SceneManager::Instance().RegisterMeshRenderer(this);
}

void MeshRenderer::OnDisable()
{
    SceneManager::Instance().UnregisterMeshRenderer(this);
}

void MeshRenderer::SetMesh(std::vector<Vertex> vertices, std::vector<uint32_t> indices)
{
    if (m_meshAsset.HasGuid())
        AssetDependencyGraph::Instance().RemoveRuntimeDependency(GetInstanceGuid(), m_meshAsset.GetGuid());

    m_sharedVertices = nullptr;
    m_sharedIndices = nullptr;
    m_inlineVertices = std::move(vertices);
    m_inlineIndices = std::move(indices);
    m_useInlineMesh = true;
    m_meshAsset.Clear();
    m_meshBufferDirty = true;
    ComputeLocalBoundsFromInlineVertices();
    NotifyCollisionGeometryChanged(this);
}

void MeshRenderer::SetProceduralMesh(std::vector<Vertex> vertices, std::vector<uint32_t> indices)
{
    const bool wasDrawable = m_useInlineMesh && !GetInlineVertices().empty() && !GetInlineIndices().empty();
    if (m_meshAsset.HasGuid())
        AssetDependencyGraph::Instance().RemoveRuntimeDependency(GetInstanceGuid(), m_meshAsset.GetGuid());

    m_sharedVertices = nullptr;
    m_sharedIndices = nullptr;
    m_inlineVertices = std::move(vertices);
    m_inlineIndices = std::move(indices);
    m_useInlineMesh = true;
    m_meshAsset.Clear();
    m_meshBufferDirty = true;
    ComputeLocalBoundsFromInlineVertices();
    const bool isDrawable = !m_inlineVertices.empty() && !m_inlineIndices.empty();
    if (wasDrawable != isDrawable)
        SceneManager::Instance().NotifyMeshRendererChanged(this);
    else
        SceneManager::Instance().NotifyMeshRendererContentChanged(this);
}

void MeshRenderer::SetSharedPrimitiveMesh(const std::vector<Vertex> &vertices, const std::vector<uint32_t> &indices,
                                          const std::string &primitiveName)
{
    if (m_meshAsset.HasGuid())
        AssetDependencyGraph::Instance().RemoveRuntimeDependency(GetInstanceGuid(), m_meshAsset.GetGuid());

    m_inlineVertices.clear();
    m_inlineIndices.clear();
    m_sharedVertices = &vertices;
    m_sharedIndices = &indices;
    m_useInlineMesh = true;
    m_inlineMeshName = primitiveName;
    m_meshAsset.Clear();
    m_meshBufferDirty = true;

    // Cache bounds per primitive type (keyed by static vertex data address).
    // Avoids iterating all vertices for every identical primitive.
    static std::unordered_map<const void *, std::pair<glm::vec3, glm::vec3>> s_boundsCache;
    auto it = s_boundsCache.find(&vertices);
    if (it != s_boundsCache.end()) {
        m_localBoundsMin = it->second.first;
        m_localBoundsMax = it->second.second;
    } else {
        ComputeLocalBoundsFromInlineVertices();
        s_boundsCache[&vertices] = {m_localBoundsMin, m_localBoundsMax};
    }

    NotifyCollisionGeometryChanged(this);
}

void MeshRenderer::SetMeshAsset(const std::string &guid, std::shared_ptr<InxMesh> mesh)
{
    auto &graph = AssetDependencyGraph::Instance();
    if (m_meshAsset.HasGuid() && m_meshAsset.GetGuid() != guid)
        graph.RemoveRuntimeDependency(GetInstanceGuid(), m_meshAsset.GetGuid());

    const uint64_t runtimeVersion = guid.empty() ? 0 : AssetRegistry::Instance().GetAssetVersion(guid);
    m_meshAsset = AssetRef<InxMesh>(guid, std::move(mesh), runtimeVersion);
    m_useInlineMesh = false;
    m_meshBufferDirty = true;
    m_inlineVertices.clear();
    m_inlineIndices.clear();
    m_sharedVertices = nullptr;
    m_sharedIndices = nullptr;

    if (!guid.empty())
        graph.AddRuntimeDependency(GetInstanceGuid(), guid);

    auto m = m_meshAsset.Get();
    if (m) {
        if (m_nodeGroup >= 0)
            UpdateBoundsForNodeGroup(m);
        else
            SetLocalBounds(m->GetBoundsMin(), m->GetBoundsMax());
    }

    SyncMaterialSlotsToMesh();
    NotifyCollisionGeometryChanged(this);
}

void MeshRenderer::SetMeshAssetGuid(const std::string &guid)
{
    auto &graph = AssetDependencyGraph::Instance();
    if (m_meshAsset.HasGuid() && m_meshAsset.GetGuid() != guid)
        graph.RemoveRuntimeDependency(GetInstanceGuid(), m_meshAsset.GetGuid());

    m_meshAsset.SetGuid(guid);
    m_useInlineMesh = false;
    m_meshBufferDirty = true;
    m_inlineVertices.clear();
    m_inlineIndices.clear();
    m_sharedVertices = nullptr;
    m_sharedIndices = nullptr;

    if (!guid.empty())
        graph.AddRuntimeDependency(GetInstanceGuid(), guid);

    NotifyCollisionGeometryChanged(this);
}

void MeshRenderer::ClearMeshAsset()
{
    if (m_meshAsset.HasGuid())
        AssetDependencyGraph::Instance().RemoveRuntimeDependency(GetInstanceGuid(), m_meshAsset.GetGuid());

    m_meshAsset.Clear();
    m_meshBufferDirty = true;
    m_useInlineMesh = false;
    m_inlineVertices.clear();
    m_inlineIndices.clear();
    m_sharedVertices = nullptr;
    m_sharedIndices = nullptr;
    m_localBoundsMin = glm::vec3(-0.5f);
    m_localBoundsMax = glm::vec3(0.5f);
    NotifyCollisionGeometryChanged(this);
}

void MeshRenderer::OnMeshAssetEvent(AssetEvent event)
{
    if (!m_meshAsset.HasGuid())
        return;

    if (event == AssetEvent::Deleted) {
        m_meshAsset.Invalidate();
        m_meshBufferDirty = true;
        m_useInlineMesh = false;
        m_inlineVertices.clear();
        m_inlineIndices.clear();
        m_sharedVertices = nullptr;
        m_sharedIndices = nullptr;
        m_localBoundsMin = glm::vec3(-0.5f);
        m_localBoundsMax = glm::vec3(0.5f);
        NotifyCollisionGeometryChanged(this);
        return;
    }

    AssetRegistry::Instance().Resolve(m_meshAsset, ResourceType::Mesh);
    auto mesh = m_meshAsset.Get();
    if (!mesh)
        return;

    if (m_nodeGroup >= 0)
        UpdateBoundsForNodeGroup(mesh);
    else
        SetLocalBounds(mesh->GetBoundsMin(), mesh->GetBoundsMax());
    SyncMaterialSlotsToMesh();
    MarkMeshBufferDirty();
    NotifyCollisionGeometryChanged(this);
}

bool MeshRenderer::ConsumeMeshBufferDirty()
{
    bool d = m_meshBufferDirty;
    m_meshBufferDirty = false;
    return d;
}

void MeshRenderer::SetMaterial(uint32_t slot, std::shared_ptr<InxMaterial> material)
{
    if (slot >= m_materials.size())
        m_materials.resize(slot + 1);

    auto &ref = m_materials[slot];
    auto oldMat = ref.Get();
    const std::string oldGuid = ref.GetGuid();
    const std::string guid = material ? material->GetGuid() : "";
    if (oldMat == material && oldGuid == guid)
        return;

    auto &graph = AssetDependencyGraph::Instance();
    if (!oldGuid.empty())
        graph.RemoveRuntimeDependency(GetInstanceGuid(), oldGuid);

    const uint64_t runtimeVersion = guid.empty() ? 0 : AssetRegistry::Instance().GetAssetVersion(guid);
    ref = AssetRef<InxMaterial>(guid, std::move(material), runtimeVersion);

    if (!guid.empty())
        graph.AddRuntimeDependency(GetInstanceGuid(), guid);

    NotifyRenderableStateChanged(this);
}

void MeshRenderer::SetMaterial(uint32_t slot, const std::string &guid)
{
    if (slot >= m_materials.size())
        m_materials.resize(slot + 1);

    auto &ref = m_materials[slot];
    const std::string oldGuid = ref.GetGuid();
    auto &graph = AssetDependencyGraph::Instance();
    if (!oldGuid.empty() && oldGuid != guid)
        graph.RemoveRuntimeDependency(GetInstanceGuid(), oldGuid);

    ref.SetGuid(guid);
    AssetRegistry::Instance().Resolve(ref, ResourceType::Material);

    if (!guid.empty())
        graph.AddRuntimeDependency(GetInstanceGuid(), guid);

    NotifyRenderableStateChanged(this);
}

void MeshRenderer::SetMaterials(const std::vector<std::string> &guids)
{
    // Clear old dependency edges
    auto &graph = AssetDependencyGraph::Instance();
    for (auto &ref : m_materials) {
        if (ref.HasGuid())
            graph.RemoveRuntimeDependency(GetInstanceGuid(), ref.GetGuid());
    }

    m_materials.resize(guids.size());
    for (uint32_t i = 0; i < guids.size(); ++i) {
        m_materials[i].SetGuid(guids[i]);
        AssetRegistry::Instance().Resolve(m_materials[i], ResourceType::Material);

        if (!guids[i].empty())
            graph.AddRuntimeDependency(GetInstanceGuid(), guids[i]);
    }

    NotifyRenderableStateChanged(this);
}

void MeshRenderer::SetMaterialSlotCount(uint32_t count)
{
    if (count == m_materials.size())
        return;

    // Remove dependency edges for slots being removed
    auto &graph = AssetDependencyGraph::Instance();
    for (uint32_t i = count; i < m_materials.size(); ++i) {
        if (m_materials[i].HasGuid())
            graph.RemoveRuntimeDependency(GetInstanceGuid(), m_materials[i].GetGuid());
    }
    m_materials.resize(count);
    NotifyRenderableStateChanged(this);
}

std::shared_ptr<InxMaterial> MeshRenderer::GetMaterial(uint32_t slot) const
{
    if (slot >= m_materials.size())
        return nullptr;
    return m_materials[slot].Get();
}

std::shared_ptr<InxMaterial> MeshRenderer::GetEffectiveMaterial(uint32_t slot) const
{
    auto mat = GetMaterial(slot);
    if (mat) {
        if (!mat->IsDeleted())
            return mat;
        auto &registry = AssetRegistry::Instance();
        auto err = registry.GetBuiltinMaterial("ErrorMaterial");
        return err ? err : registry.GetBuiltinMaterial("DefaultLit");
    }
    if (slot < m_materials.size() && m_materials[slot].HasGuid()) {
        auto &registry = AssetRegistry::Instance();
        auto err = registry.GetBuiltinMaterial("ErrorMaterial");
        return err ? err : registry.GetBuiltinMaterial("DefaultLit");
    }
    return AssetRegistry::Instance().GetBuiltinMaterial("DefaultLit");
}

void MeshRenderer::OnMaterialAssetEvent(const std::string &guid, AssetEvent event)
{
    if (guid.empty() || (event != AssetEvent::Deleted && event != AssetEvent::Modified))
        return;
    bool changed = false;
    for (auto &reference : m_materials) {
        if (reference.GetGuid() != guid)
            continue;
        if (event == AssetEvent::Deleted) {
            reference.Invalidate();
        } else {
            reference.MarkStale();
            AssetRegistry::Instance().Resolve(reference, ResourceType::Material);
        }
        changed = true;
    }
    if (changed)
        NotifyRenderableStateChanged(this);
}

std::string MeshRenderer::GetMaterialGuid(uint32_t slot) const
{
    if (slot >= m_materials.size())
        return "";
    return m_materials[slot].GetGuid();
}

std::vector<std::string> MeshRenderer::GetMaterialGuids() const
{
    std::vector<std::string> guids;
    guids.reserve(m_materials.size());
    for (const auto &ref : m_materials)
        guids.push_back(ref.GetGuid());
    return guids;
}

void MeshRenderer::SyncMaterialSlotsToMesh()
{
    if (!HasMeshAsset())
        return;
    auto mesh = m_meshAsset.Get();
    if (!mesh)
        return;

    // Single-submesh mode: only 1 material slot needed
    if (m_submeshIndex >= 0) {
        if (m_materials.size() < 1)
            m_materials.resize(1);
        return;
    }

    // Node-group mode: count unique material slots within this node group
    if (m_nodeGroup >= 0) {
        std::set<uint32_t> uniqueSlots;
        for (const auto &sub : mesh->GetSubMeshes()) {
            if (static_cast<int32_t>(sub.nodeGroup) == m_nodeGroup)
                uniqueSlots.insert(sub.materialSlot);
        }
        uint32_t needed = std::max(static_cast<uint32_t>(uniqueSlots.size()), 1u);
        if (m_materials.size() != needed)
            SetMaterialSlotCount(needed);
        return;
    }

    uint32_t needed = std::max(mesh->GetMaterialSlotCount(), 1u);
    if (m_materials.size() != needed)
        SetMaterialSlotCount(needed);
}

void MeshRenderer::ComputeLocalBoundsFromInlineVertices()
{
    const auto &verts = GetInlineVertices();
    if (verts.empty()) {
        m_localBoundsMin = glm::vec3(-0.5f);
        m_localBoundsMax = glm::vec3(0.5f);
        return;
    }

    glm::vec3 bmin(std::numeric_limits<float>::max());
    glm::vec3 bmax(std::numeric_limits<float>::lowest());
    for (const auto &v : verts) {
        bmin = glm::min(bmin, v.pos);
        bmax = glm::max(bmax, v.pos);
    }
    m_localBoundsMin = bmin;
    m_localBoundsMax = bmax;
}

void MeshRenderer::SetNodeGroup(int32_t group)
{
    m_nodeGroup = group;
    if (HasMeshAsset()) {
        auto mesh = m_meshAsset.Get();
        if (mesh) {
            if (m_nodeGroup >= 0)
                UpdateBoundsForNodeGroup(mesh);
            else
                SetLocalBounds(mesh->GetBoundsMin(), mesh->GetBoundsMax());
            SyncMaterialSlotsToMesh();
        }
    }
    NotifyRenderableStateChanged(this);
}

void MeshRenderer::UpdateBoundsForNodeGroup(const std::shared_ptr<InxMesh> &mesh)
{
    constexpr float INF = std::numeric_limits<float>::max();
    glm::vec3 bmin(INF);
    glm::vec3 bmax(-INF);
    bool found = false;
    for (const auto &sub : mesh->GetSubMeshes()) {
        if (static_cast<int32_t>(sub.nodeGroup) == m_nodeGroup) {
            bmin = glm::min(bmin, sub.boundsMin);
            bmax = glm::max(bmax, sub.boundsMax);
            found = true;
        }
    }
    if (found)
        SetLocalBounds(bmin, bmax);
}

void MeshRenderer::GetWorldBounds(glm::vec3 &outMin, glm::vec3 &outMax) const
{
    if (!m_gameObject) {
        outMin = m_localBoundsMin;
        outMax = m_localBoundsMax;
        return;
    }

    const Transform *transform = m_gameObject->GetTransform();
    const glm::mat4 &worldMatrix = transform->GetWorldMatrix();
    ComputeWorldBounds(worldMatrix, outMin, outMax);
}

void MeshRenderer::ComputeWorldBounds(const glm::mat4 &worldMatrix, glm::vec3 &outMin, glm::vec3 &outMax) const
{
    // Arvo's AABB transform method: O(9) multiply-adds instead of
    // 8 × mat4×vec4 (O(32) multiplies).  For each output axis, we
    // compute the transformed center ± transformed half-extent.
    const glm::vec3 center = (m_localBoundsMin + m_localBoundsMax) * 0.5f;
    const glm::vec3 extent = (m_localBoundsMax - m_localBoundsMin) * 0.5f;

    glm::vec3 newCenter, newExtent;
    for (int i = 0; i < 3; ++i) {
        newCenter[i] = worldMatrix[3][i]; // translation
        newExtent[i] = 0.0f;
        for (int j = 0; j < 3; ++j) {
            float e = worldMatrix[j][i]; // column-major: M[col][row]
            newCenter[i] += e * center[j];
            newExtent[i] += std::abs(e) * extent[j];
        }
    }

    outMin = newCenter - newExtent;
    outMax = newCenter + newExtent;
}

nlohmann::json MeshRenderer::SerializeDocument() const
{
    json j = Component::SerializeDocument();

    // Mesh reference
    j["meshId"] = m_mesh.meshId;

    const bool persistInlineMesh = ShouldSerializeInlineMeshData();
    const bool builtinPrimitive =
        persistInlineMesh && m_useInlineMesh && !m_inlineMeshName.empty() &&
        MatchesBuiltinPrimitiveMesh(m_inlineMeshName, GetInlineVertices(), GetInlineIndices());
    const std::string matchedInlineMeshGuid =
        (persistInlineMesh && !HasMeshAsset() && m_useInlineMesh && !builtinPrimitive)
            ? FindMatchingMeshAssetGuid(GetInlineVertices(), GetInlineIndices(), m_inlineMeshName)
            : std::string();
    const std::string serializedMeshGuid = HasMeshAsset() ? m_meshAsset.GetGuid() : matchedInlineMeshGuid;

    // Mesh asset GUID (for model-file meshes managed by AssetRegistry)
    if (!serializedMeshGuid.empty()) {
        j["meshAssetGuid"] = serializedMeshGuid;
    }

    // Materials:
    // GUID-backed slots remain compact strings. Runtime material snapshots are
    // embedded as typed documents instead of nested JSON text.
    json materialsJson = json::array();
    for (const auto &ref : m_materials) {
        const auto &guid = ref.GetGuid();
        if (!guid.empty()) {
            materialsJson.push_back(guid);
            continue;
        }

        auto mat = ref.Get();
        if (!mat) {
            materialsJson.push_back(nullptr);
            continue;
        }

        json slotJson = json::object();
        slotJson["material"] = mat->SerializeDocument();

        materialsJson.push_back(slotJson.empty() ? json(nullptr) : slotJson);
    }
    j["materials"] = materialsJson;

    // Rendering flags
    j["castShadows"] = m_castShadows;
    j["receivesShadows"] = m_receiveShadows;

    // Submesh filter
    if (m_submeshIndex >= 0) {
        j["submeshIndex"] = m_submeshIndex;
    }

    // Node group filter
    if (m_nodeGroup >= 0) {
        j["nodeGroup"] = m_nodeGroup;
    }

    // Mesh pivot offset (for submesh centering)
    if (m_meshPivotOffset != glm::vec3(0.0f)) {
        j["meshPivotOffset"] = {m_meshPivotOffset.x, m_meshPivotOffset.y, m_meshPivotOffset.z};
    }

    // Bounds
    j["boundsMin"] = {m_localBoundsMin.x, m_localBoundsMin.y, m_localBoundsMin.z};
    j["boundsMax"] = {m_localBoundsMax.x, m_localBoundsMax.y, m_localBoundsMax.z};

    // Inline mesh data (for primitives and procedural geometry)
    const bool serializeInlineMesh = persistInlineMesh && m_useInlineMesh && serializedMeshGuid.empty();
    j["useInlineMesh"] = serializeInlineMesh;
    if (persistInlineMesh && !m_inlineMeshName.empty()) {
        j["inlineMeshName"] = m_inlineMeshName;
    }
    if (serializeInlineMesh) {
        if (builtinPrimitive) {
            j["inlineMeshBuiltin"] = true;
        } else {
            json verticesJson = json::array();
            for (const auto &v : GetInlineVertices()) {
                json vj;
                vj["pos"] = {v.pos.x, v.pos.y, v.pos.z};
                vj["normal"] = {v.normal.x, v.normal.y, v.normal.z};
                vj["tangent"] = {v.tangent.x, v.tangent.y, v.tangent.z, v.tangent.w};
                vj["color"] = {v.color.x, v.color.y, v.color.z};
                vj["texCoord"] = {v.texCoord.x, v.texCoord.y};
                verticesJson.push_back(vj);
            }
            j["inlineVertices"] = verticesJson;

            json indicesJson = json::array();
            for (uint32_t idx : GetInlineIndices()) {
                indicesJson.push_back(idx);
            }
            j["inlineIndices"] = indicesJson;
        }
    }

    return j;
}

void MeshRenderer::ValidateSerializedDocument(const nlohmann::json &document)
{
    ValidateSerializedDocumentForType(document, "MeshRenderer");
}

void MeshRenderer::ValidateSerializedDocumentForType(const nlohmann::json &j, std::string_view expectedType)
{
    using namespace component_document_validation;
    std::vector<std::string_view> required = {"meshId",    "materials", "castShadows",  "receivesShadows",
                                              "boundsMin", "boundsMax", "useInlineMesh"};
    std::vector<std::string_view> optional = {"meshAssetGuid",   "submeshIndex",   "nodeGroup",
                                              "meshPivotOffset", "inlineMeshName", "inlineMeshBuiltin",
                                              "inlineVertices",  "inlineIndices"};
    if (expectedType == "SpriteRenderer") {
        required.insert(required.end(), {"frameId", "spriteColor", "flipX", "flipY"});
        optional.push_back("spriteGuid");
    } else if (expectedType == "LineRenderer") {
        required.insert(required.end(),
                        {"positions", "startWidth", "endWidth", "widthMultiplier", "startColor", "endColor", "loop",
                         "useWorldSpace", "alignment", "textureMode", "textureScale"});
    } else if (expectedType == "SkinnedMeshRenderer") {
        optional.push_back("activeTakeName");
    } else if (expectedType != "MeshRenderer") {
        throw std::invalid_argument("unsupported MeshRenderer document type: " + std::string(expectedType));
    }
    ValidateComponentDocumentFields(j, expectedType, required, optional);

    RequireUnsignedInteger(j, "meshId", expectedType);
    const auto &materials = j["materials"];
    if (!materials.is_array())
        throw std::invalid_argument(std::string(expectedType) + ".materials must be an array");
    for (size_t index = 0; index < materials.size(); ++index) {
        const auto &slot = materials[index];
        if (slot.is_null())
            continue;
        if (slot.is_string()) {
            if (slot.get_ref<const std::string &>().empty())
                throw std::invalid_argument(std::string(expectedType) + ".materials[" + std::to_string(index) +
                                            "] must use null instead of an empty GUID");
            continue;
        }
        if (!slot.is_object())
            throw std::invalid_argument(std::string(expectedType) + ".materials[" + std::to_string(index) +
                                        "] must be null, a GUID string, or an object");
        if (slot.size() != 1 || !slot.contains("material") || !slot["material"].is_object())
            throw std::invalid_argument(std::string(expectedType) + ".materials[" + std::to_string(index) +
                                        "] must contain only a material object");
        material_document_validation::ValidateMaterialDocument(
            slot["material"], std::string(expectedType) + ".materials[" + std::to_string(index) + "].material");
    }

    RequireBoolean(j, "castShadows", expectedType);
    RequireBoolean(j, "receivesShadows", expectedType);
    RequireFiniteVector(j, "boundsMin", 3, expectedType);
    RequireFiniteVector(j, "boundsMax", 3, expectedType);
    RequireBoolean(j, "useInlineMesh", expectedType);
    for (size_t axis = 0; axis < 3; ++axis) {
        if (j["boundsMin"][axis].get<float>() > j["boundsMax"][axis].get<float>())
            throw std::invalid_argument(std::string(expectedType) + " bounds are inverted");
    }

    if (j.contains("meshAssetGuid") && RequireString(j, "meshAssetGuid", expectedType).empty())
        throw std::invalid_argument(std::string(expectedType) + ".meshAssetGuid must not be empty");
    if (j.contains("submeshIndex") && RequireInteger(j, "submeshIndex", expectedType) < 0)
        throw std::invalid_argument(std::string(expectedType) + ".submeshIndex must be non-negative");
    if (j.contains("nodeGroup") && RequireInteger(j, "nodeGroup", expectedType) < 0)
        throw std::invalid_argument(std::string(expectedType) + ".nodeGroup must be non-negative");
    if (j.contains("meshPivotOffset"))
        RequireFiniteVector(j, "meshPivotOffset", 3, expectedType);
    if (j.contains("inlineMeshName"))
        RequireString(j, "inlineMeshName", expectedType);
    if (j.contains("inlineMeshBuiltin"))
        RequireBoolean(j, "inlineMeshBuiltin", expectedType);

    const bool useInlineMesh = j["useInlineMesh"].get<bool>();
    const bool inlineBuiltin = j.value("inlineMeshBuiltin", false);
    if (useInlineMesh && j.contains("meshAssetGuid"))
        throw std::invalid_argument(std::string(expectedType) + " cannot combine inline mesh and meshAssetGuid");
    if (!useInlineMesh &&
        (j.contains("inlineMeshBuiltin") || j.contains("inlineVertices") || j.contains("inlineIndices")))
        throw std::invalid_argument(std::string(expectedType) + " has inline data while useInlineMesh is false");
    if (useInlineMesh && inlineBuiltin && (j.contains("inlineVertices") || j.contains("inlineIndices")))
        throw std::invalid_argument(std::string(expectedType) + " builtin inline mesh cannot contain raw data");
    if (useInlineMesh && !inlineBuiltin && (!j.contains("inlineVertices") || !j.contains("inlineIndices")))
        throw std::invalid_argument(std::string(expectedType) + " raw inline mesh requires vertices and indices");

    if (j.contains("inlineVertices")) {
        const auto &vertices = j["inlineVertices"];
        if (!vertices.is_array())
            throw std::invalid_argument(std::string(expectedType) + ".inlineVertices must be an array");
        for (size_t index = 0; index < vertices.size(); ++index) {
            const auto &vertex = vertices[index];
            if (!vertex.is_object() || vertex.size() != 5 || !vertex.contains("pos") || !vertex.contains("normal") ||
                !vertex.contains("tangent") || !vertex.contains("color") || !vertex.contains("texCoord"))
                throw std::invalid_argument(std::string(expectedType) + ".inlineVertices[" + std::to_string(index) +
                                            "] has invalid fields");
            const auto validateVector = [&](const char *field, size_t size) {
                const auto &value = vertex[field];
                if (!value.is_array() || value.size() != size)
                    throw std::invalid_argument(std::string(expectedType) + ".inlineVertices[" + std::to_string(index) +
                                                "]." + field + " has invalid length");
                for (const auto &item : value) {
                    if (!item.is_number())
                        throw std::invalid_argument("inline vertex attribute must contain numbers");
                    const double number = item.get<double>();
                    if (!std::isfinite(number) || std::abs(number) > std::numeric_limits<float>::max())
                        throw std::invalid_argument("inline vertex attribute must contain finite floats");
                }
            };
            validateVector("pos", 3);
            validateVector("normal", 3);
            validateVector("tangent", 4);
            validateVector("color", 3);
            validateVector("texCoord", 2);
        }
    }
    if (j.contains("inlineIndices")) {
        const auto &indices = j["inlineIndices"];
        if (!indices.is_array())
            throw std::invalid_argument(std::string(expectedType) + ".inlineIndices must be an array");
        for (const auto &index : indices) {
            if (!index.is_number_unsigned() || index.get<uint64_t>() > std::numeric_limits<uint32_t>::max())
                throw std::invalid_argument(std::string(expectedType) + ".inlineIndices contains an invalid index");
        }
    }

    if (expectedType == "SkinnedMeshRenderer" && j.contains("activeTakeName"))
        RequireString(j, "activeTakeName", expectedType);
    if (expectedType == "SpriteRenderer") {
        const auto &frameId = RequireString(j, "frameId", expectedType);
        if (!frameId.empty()) {
            const bool valid =
                frameId.size() == 32 && std::all_of(frameId.begin(), frameId.end(), [](unsigned char ch) {
                    return (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f');
                });
            if (!valid)
                throw std::invalid_argument(
                    "SpriteRenderer.frameId must be empty or a 32-character lowercase UUID hex string");
        }
        RequireFiniteVector(j, "spriteColor", 4, expectedType);
        RequireBoolean(j, "flipX", expectedType);
        RequireBoolean(j, "flipY", expectedType);
        if (j.contains("spriteGuid")) {
            if (RequireString(j, "spriteGuid", expectedType).empty())
                throw std::invalid_argument("SpriteRenderer.spriteGuid must not be empty");
            if (frameId.empty())
                throw std::invalid_argument(
                    "SpriteRenderer.frameId must identify a persisted SpriteFrame when spriteGuid is set");
        }
    }
    if (expectedType == "LineRenderer") {
        const auto &positions = j["positions"];
        if (!positions.is_array())
            throw std::invalid_argument("LineRenderer.positions must be an array");
        for (size_t index = 0; index < positions.size(); ++index) {
            if (!positions[index].is_array() || positions[index].size() != 3)
                throw std::invalid_argument("LineRenderer.positions entries must contain three numbers");
            for (const auto &coordinate : positions[index]) {
                if (!coordinate.is_number() || !std::isfinite(coordinate.get<double>()))
                    throw std::invalid_argument("LineRenderer.positions must contain finite numbers");
            }
        }
        const auto requireFiniteNonNegative = [&](const char *field) {
            const double value = j[field].get<double>();
            if (!j[field].is_number() || !std::isfinite(value) || value < 0.0)
                throw std::invalid_argument(std::string("LineRenderer.") + field + " must be finite and non-negative");
        };
        requireFiniteNonNegative("startWidth");
        requireFiniteNonNegative("endWidth");
        requireFiniteNonNegative("widthMultiplier");
        requireFiniteNonNegative("textureScale");
        RequireFiniteVector(j, "startColor", 4, expectedType);
        RequireFiniteVector(j, "endColor", 4, expectedType);
        RequireBoolean(j, "loop", expectedType);
        RequireBoolean(j, "useWorldSpace", expectedType);
        const auto requireEnum = [&](const char *field, int maximum) {
            const int64_t value = RequireInteger(j, field, expectedType);
            if (value < 0 || value > maximum)
                throw std::invalid_argument(std::string("LineRenderer.") + field + " is outside its enum range");
        };
        requireEnum("alignment", 1);
        requireEnum("textureMode", 1);
    }
}

bool MeshRenderer::DeserializeDocument(const nlohmann::json &j)
{
    try {
        ValidateSerializedDocumentForType(j, GetTypeName());

        auto &registry = AssetRegistry::Instance();
        const std::string meshGuid = j.value("meshAssetGuid", std::string());
        std::shared_ptr<InxMesh> stagedMesh;
        if (!meshGuid.empty()) {
            stagedMesh = registry.LoadAsset<InxMesh>(meshGuid, ResourceType::Mesh);
        }
        const bool meshAssetResolved = static_cast<bool>(stagedMesh);

        const auto &materialsDocument = j["materials"];
        std::vector<AssetRef<InxMaterial>> stagedMaterials(materialsDocument.size());
        for (size_t index = 0; index < materialsDocument.size(); ++index) {
            const auto &slotDocument = materialsDocument[index];
            if (slotDocument.is_null())
                continue;
            if (slotDocument.is_string()) {
                const std::string guid = slotDocument.get<std::string>();
                auto material = registry.LoadAsset<InxMaterial>(guid, ResourceType::Material);
                if (material)
                    stagedMaterials[index] =
                        AssetRef<InxMaterial>(guid, std::move(material), registry.GetAssetVersion(guid));
                else
                    stagedMaterials[index] = AssetRef<InxMaterial>(guid);
                continue;
            }

            auto material = std::make_shared<InxMaterial>();
            if (!material->DeserializeDocument(slotDocument["material"]))
                throw std::invalid_argument("invalid embedded material document");
            stagedMaterials[index] = AssetRef<InxMaterial>(std::string(), std::move(material), 0);
        }

        if (!Component::DeserializeDocument(j))
            return false;

        // Mesh reference
        m_mesh.meshId = j["meshId"].get<uint64_t>();

        if (j.contains("nodeGroup"))
            m_nodeGroup = j["nodeGroup"].get<int32_t>();
        else
            m_nodeGroup = -1;

        // Mesh asset GUID (model-file meshes managed by AssetRegistry)
        if (stagedMesh)
            SetMeshAsset(meshGuid, std::move(stagedMesh));
        else if (!meshGuid.empty())
            SetMeshAssetGuid(meshGuid);
        else
            ClearMeshAsset();

        // ================================================================
        // Materials
        // v5: GUID strings, null slots, or typed runtime material documents.
        // ================================================================
        auto &graph = AssetDependencyGraph::Instance();
        for (auto &ref : m_materials) {
            if (ref.HasGuid())
                graph.RemoveRuntimeDependency(GetInstanceGuid(), ref.GetGuid());
        }
        m_materials = std::move(stagedMaterials);
        for (const auto &reference : m_materials) {
            if (reference.HasGuid())
                graph.AddRuntimeDependency(GetInstanceGuid(), reference.GetGuid());
        }

        // Sync slot count to mesh submesh count
        SyncMaterialSlotsToMesh();

        // Rendering flags
        if (j.contains("castShadows")) {
            m_castShadows = j["castShadows"].get<bool>();
        }
        if (j.contains("receivesShadows")) {
            m_receiveShadows = j["receivesShadows"].get<bool>();
        }
        if (j.contains("submeshIndex")) {
            m_submeshIndex = j["submeshIndex"].get<int32_t>();
        }
        if (j.contains("meshPivotOffset") && j["meshPivotOffset"].is_array() && j["meshPivotOffset"].size() == 3) {
            m_meshPivotOffset.x = j["meshPivotOffset"][0].get<float>();
            m_meshPivotOffset.y = j["meshPivotOffset"][1].get<float>();
            m_meshPivotOffset.z = j["meshPivotOffset"][2].get<float>();
        }

        // Persisted bounds are only a fallback for an unavailable external
        // asset. Once the mesh is resident, its imported geometry is the
        // authoritative source. Restoring an old cache here used to overwrite
        // SetMeshAsset()'s exact bounds and could make a model selectable from
        // almost anywhere in the Scene view.
        if (!meshAssetResolved) {
            if (j.contains("boundsMin") && j["boundsMin"].is_array() && j["boundsMin"].size() == 3) {
                m_localBoundsMin.x = j["boundsMin"][0].get<float>();
                m_localBoundsMin.y = j["boundsMin"][1].get<float>();
                m_localBoundsMin.z = j["boundsMin"][2].get<float>();
            }
            if (j.contains("boundsMax") && j["boundsMax"].is_array() && j["boundsMax"].size() == 3) {
                m_localBoundsMax.x = j["boundsMax"][0].get<float>();
                m_localBoundsMax.y = j["boundsMax"][1].get<float>();
                m_localBoundsMax.z = j["boundsMax"][2].get<float>();
            }
        }

        // Inline mesh data (for primitives like cubes)
        m_useInlineMesh = j.value("useInlineMesh", false);
        m_inlineMeshName = j.value("inlineMeshName", std::string());
        m_inlineVertices.clear();
        m_inlineIndices.clear();
        m_sharedVertices = nullptr;
        m_sharedIndices = nullptr;

        if (m_useInlineMesh) {
            const bool isBuiltinPrimitive = j.value("inlineMeshBuiltin", false);
            if (isBuiltinPrimitive) {
                RestoreBuiltinPrimitiveMesh(m_inlineMeshName, m_inlineVertices, m_inlineIndices);
            } else if (j.contains("inlineVertices") && j["inlineVertices"].is_array()) {
                for (const auto &vj : j["inlineVertices"]) {
                    Vertex v;
                    if (vj.contains("pos") && vj["pos"].is_array() && vj["pos"].size() == 3) {
                        v.pos.x = vj["pos"][0].get<float>();
                        v.pos.y = vj["pos"][1].get<float>();
                        v.pos.z = vj["pos"][2].get<float>();
                    }
                    if (vj.contains("normal") && vj["normal"].is_array() && vj["normal"].size() == 3) {
                        v.normal.x = vj["normal"][0].get<float>();
                        v.normal.y = vj["normal"][1].get<float>();
                        v.normal.z = vj["normal"][2].get<float>();
                    }
                    if (vj.contains("tangent") && vj["tangent"].is_array() && vj["tangent"].size() == 4) {
                        v.tangent.x = vj["tangent"][0].get<float>();
                        v.tangent.y = vj["tangent"][1].get<float>();
                        v.tangent.z = vj["tangent"][2].get<float>();
                        v.tangent.w = vj["tangent"][3].get<float>();
                    }
                    if (vj.contains("color") && vj["color"].is_array() && vj["color"].size() == 3) {
                        v.color.x = vj["color"][0].get<float>();
                        v.color.y = vj["color"][1].get<float>();
                        v.color.z = vj["color"][2].get<float>();
                    }
                    if (vj.contains("texCoord") && vj["texCoord"].is_array() && vj["texCoord"].size() == 2) {
                        v.texCoord.x = vj["texCoord"][0].get<float>();
                        v.texCoord.y = vj["texCoord"][1].get<float>();
                    }
                    m_inlineVertices.push_back(v);
                }
            }
            if (j.contains("inlineIndices") && j["inlineIndices"].is_array()) {
                for (const auto &idx : j["inlineIndices"]) {
                    m_inlineIndices.push_back(idx.get<uint32_t>());
                }
            }

            if (!m_inlineVertices.empty()) {
                ComputeLocalBoundsFromInlineVertices();
            }
        }

        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("MeshRenderer::Deserialize failed: ", e.what());
        return false;
    }
}

std::unique_ptr<Component> MeshRenderer::Clone() const
{
    auto clone = std::make_unique<MeshRenderer>();
    // Base fields
    clone->m_enabled = m_enabled;
    clone->m_executionOrder = m_executionOrder;
    // Mesh ref
    clone->m_mesh = m_mesh;
    clone->m_meshAsset = m_meshAsset;
    clone->m_meshBufferDirty = false;
    // Inline mesh
    clone->m_useInlineMesh = m_useInlineMesh;
    clone->m_inlineMeshName = m_inlineMeshName;
    clone->m_sharedVertices = m_sharedVertices;
    clone->m_sharedIndices = m_sharedIndices;
    if (!m_sharedVertices) {
        clone->m_inlineVertices = m_inlineVertices;
        clone->m_inlineIndices = m_inlineIndices;
    }
    // Materials
    clone->m_materials = m_materials;
    auto &graph = AssetDependencyGraph::Instance();
    if (clone->m_meshAsset.HasGuid())
        graph.AddRuntimeDependency(clone->GetInstanceGuid(), clone->m_meshAsset.GetGuid());
    for (const auto &reference : clone->m_materials) {
        if (reference.HasGuid())
            graph.AddRuntimeDependency(clone->GetInstanceGuid(), reference.GetGuid());
    }
    // Rendering flags
    clone->m_castShadows = m_castShadows;
    clone->m_receiveShadows = m_receiveShadows;
    // Submesh / node group
    clone->m_submeshIndex = m_submeshIndex;
    clone->m_nodeGroup = m_nodeGroup;
    clone->m_meshPivotOffset = m_meshPivotOffset;
    // Bounds
    clone->m_localBoundsMin = m_localBoundsMin;
    clone->m_localBoundsMax = m_localBoundsMax;
    return clone;
}

} // namespace infernux
