/**
 * @file Infernux.cpp
 * @brief Infernux — Core lifecycle, resources, renderer init, gizmos, material pipeline
 *
 * Editor camera control → InfernuxCamera.cpp
 * Scene picking / raycasting → ScenePicker.cpp
 */

#include "Infernux.h"
// Explicit includes for types now only forward-declared in InxRenderer.h
#include <algorithm>
#include <array>
#include <cctype>
#include <cerrno>
#include <charconv>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <function/audio/AudioClipLoader.h>
#include <function/audio/AudioEngine.h>
#include <function/audio/AudioSource.h>
#include <function/renderer/EditorGizmos.h>
#include <function/renderer/GizmosDrawCallBuffer.h>
#include <function/renderer/SceneRenderGraph.h>
#include <function/renderer/ScriptableRenderContext.h>
#include <function/renderer/gui/InxGUIContext.h>
#include <function/renderer/gui/InxResourcePreviewer.h>
#include <function/renderer/gui/InxScreenUIRenderer.h>
#include <function/renderer/vk/VkResourceManager.h>
#include <function/resources/AssetDependencyGraph.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxFileLoader/InxDefaultLoader.hpp>
#include <function/resources/InxFileLoader/InxPythonScriptLoader.hpp>
#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/InxFileLoader/InxTextureLoader.hpp>
#include <function/resources/InxMaterial/MaterialLoader.h>
#include <function/resources/InxMesh/InxMesh.h>
#include <function/resources/InxMesh/MeshLoader.h>
#include <function/resources/InxTexture/InxTexture.h>
#include <function/resources/InxTexture/TextureLoader.h>
#include <function/resources/PhysicMaterial/PhysicMaterialLoader.h>
#include <function/resources/ShaderAsset/ShaderAsset.h>
#include <function/resources/ShaderAsset/ShaderLoader.h>
#include <function/scene/Collider.h>
#include <function/scene/Component.h>
#include <function/scene/MeshRenderer.h>
#include <function/scene/PrimitiveMeshes.h>
#include <function/scene/physics/PhysicsWorld.h>
#include <glm/gtc/matrix_transform.hpp>
#include <glm/gtc/quaternion.hpp>
#include <imgui.h>
#include <imgui_internal.h>
#include <limits>
#include <nlohmann/json.hpp>
#include <platform/filesystem/DocumentStore.h>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <unordered_map>
#include <unordered_set>

#include <core/config/InxPlatform.h>
#include <core/threading/JobSystem.h>
#include <function/scene/TransformECSStore.h>
#ifdef INX_PLATFORM_WINDOWS
#include <ShlObj.h> // SHGetFolderPathW for Documents path
#endif

namespace infernux
{

namespace
{

using json = nlohmann::json;

struct AcceptanceClock
{
    bool enabled = false;
    float fixedDeltaSeconds = 0.0f;
    uint64_t pauseAfterFrame = 0;
};

AcceptanceClock ReadPlayerAcceptanceClock()
{
    const char *fixedDeltaText = std::getenv("_INFERNUX_PLAYER_FIXED_DELTA");
    const char *pauseFrameText = std::getenv("_INFERNUX_PLAYER_PAUSE_AFTER_FRAME");
    const bool hasFixedDelta = fixedDeltaText != nullptr && fixedDeltaText[0] != '\0';
    const bool hasPauseFrame = pauseFrameText != nullptr && pauseFrameText[0] != '\0';
    if (!hasFixedDelta && !hasPauseFrame)
        return {};
    if (std::getenv("_INFERNUX_PLAYER_DEBUG_BUILD") == nullptr ||
        std::string_view(std::getenv("_INFERNUX_PLAYER_DEBUG_BUILD")) != "1") {
        throw std::logic_error("Player acceptance clock is available only in a PlayerDebug build");
    }
    if (!hasFixedDelta || !hasPauseFrame) {
        throw std::invalid_argument("Player acceptance clock requires both _INFERNUX_PLAYER_FIXED_DELTA and "
                                    "_INFERNUX_PLAYER_PAUSE_AFTER_FRAME");
    }

    errno = 0;
    char *fixedDeltaEnd = nullptr;
    const float fixedDelta = std::strtof(fixedDeltaText, &fixedDeltaEnd);
    if (errno != 0 || fixedDeltaEnd == fixedDeltaText || *fixedDeltaEnd != '\0' || !std::isfinite(fixedDelta) ||
        fixedDelta <= 0.0f || fixedDelta > 0.25f) {
        throw std::invalid_argument("_INFERNUX_PLAYER_FIXED_DELTA must be finite and in (0, 0.25]");
    }

    uint64_t pauseAfterFrame = 0;
    const char *pauseFrameEnd = pauseFrameText + std::strlen(pauseFrameText);
    const auto [parsedEnd, parseError] = std::from_chars(pauseFrameText, pauseFrameEnd, pauseAfterFrame, 10);
    if (parseError != std::errc{} || parsedEnd != pauseFrameEnd || pauseAfterFrame == 0) {
        throw std::invalid_argument("_INFERNUX_PLAYER_PAUSE_AFTER_FRAME must be a positive integer");
    }
    return {true, fixedDelta, pauseAfterFrame};
}

bool IsDirectStructuredStage(const ShaderDescriptor &descriptor)
{
    return std::any_of(
        descriptor.capabilities.begin(), descriptor.capabilities.end(), [](const std::string &capability) {
            std::string normalized = capability;
            std::transform(normalized.begin(), normalized.end(), normalized.begin(),
                           [](unsigned char character) { return static_cast<char>(std::tolower(character)); });
            return normalized == "fullscreen" || normalized == "standalone";
        });
}

void RegisterPhysicMaterialAssetCallback()
{
    AssetDependencyGraph::Instance().RegisterCallback(
        ResourceType::PhysicMaterial,
        [](const std::string &dependentGuid, const std::string & /*materialGuid*/, AssetEvent event) {
            uint64_t componentId = 0;
            const char *begin = dependentGuid.data();
            const char *end = begin + dependentGuid.size();
            const auto [parsedEnd, error] = std::from_chars(begin, end, componentId);
            if (error != std::errc{} || parsedEnd != end)
                return;
            auto *collider = dynamic_cast<Collider *>(Component::FindByComponentId(componentId));
            if (!collider)
                return;
            collider->OnPhysicMaterialAssetEvent(event);
        });
}

bool ParseModelEmbeddedMaterialSlot(const std::string &path, std::string &outModel, int &outSlot)
{
    constexpr const char kTok[] = "::submat:";
    const size_t pos = path.find(kTok);
    if (pos == std::string::npos)
        return false;
    outModel = path.substr(0, pos);
    try {
        outSlot = std::stoi(path.substr(pos + sizeof(kTok) - 1));
    } catch (...) {
        return false;
    }
    return !outModel.empty() && outSlot >= 0;
}

struct PrefabPreviewAggregate
{
    std::vector<Vertex> vertices;
    std::vector<uint32_t> indices;
    std::vector<SubMesh> subMeshes;
    std::vector<std::shared_ptr<InxMaterial>> materials;
};

glm::quat PreviewEulerYXZToQuat(const glm::vec3 &eulerDeg)
{
    glm::vec3 r = glm::radians(eulerDeg);
    float cx = std::cos(r.x * 0.5f), sx = std::sin(r.x * 0.5f);
    float cy = std::cos(r.y * 0.5f), sy = std::sin(r.y * 0.5f);
    float cz = std::cos(r.z * 0.5f), sz = std::sin(r.z * 0.5f);

    glm::quat q;
    q.w = cy * cx * cz + sy * sx * sz;
    q.x = cy * sx * cz + sy * cx * sz;
    q.y = sy * cx * cz - cy * sx * sz;
    q.z = cy * cx * sz - sy * sx * cz;
    return q;
}

glm::vec3 ReadVec3(const json &j, const char *key, const glm::vec3 &fallback)
{
    auto it = j.find(key);
    if (it == j.end() || !it->is_array() || it->size() != 3)
        return fallback;
    return glm::vec3((*it)[0].get<float>(), (*it)[1].get<float>(), (*it)[2].get<float>());
}

glm::mat4 ReadNodeLocalMatrix(const json &node)
{
    auto it = node.find("transform");
    if (it == node.end() || !it->is_object())
        return glm::mat4(1.0f);

    const glm::vec3 position = ReadVec3(*it, "position", glm::vec3(0.0f));
    const glm::vec3 rotation = ReadVec3(*it, "rotation", glm::vec3(0.0f));
    const glm::vec3 scale = ReadVec3(*it, "scale", glm::vec3(1.0f));

    return glm::translate(glm::mat4(1.0f), position) * glm::mat4_cast(PreviewEulerYXZToQuat(rotation)) *
           glm::scale(glm::mat4(1.0f), scale);
}

bool GetPreviewPrimitiveMeshData(const std::string &name, const std::vector<Vertex> *&vertices,
                                 const std::vector<uint32_t> *&indices)
{
    vertices = nullptr;
    indices = nullptr;

    if (name == "Cube") {
        vertices = &PrimitiveMeshes::GetCubeVertices();
        indices = &PrimitiveMeshes::GetCubeIndices();
    } else if (name == "Quad") {
        vertices = &PrimitiveMeshes::GetQuadVertices();
        indices = &PrimitiveMeshes::GetQuadIndices();
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
    }

    return vertices != nullptr && indices != nullptr;
}

glm::vec3 NormalizeOrFallback(const glm::vec3 &value, const glm::vec3 &fallback)
{
    const float lenSq = glm::dot(value, value);
    if (lenSq > 1e-10f)
        return glm::normalize(value);

    const float fallbackLenSq = glm::dot(fallback, fallback);
    if (fallbackLenSq > 1e-10f)
        return glm::normalize(fallback);

    return glm::vec3(0.0f, 1.0f, 0.0f);
}

void ComputeBoundsFromVertices(const std::vector<Vertex> &vertices, glm::vec3 &outMin, glm::vec3 &outMax)
{
    constexpr float kInf = std::numeric_limits<float>::max();
    outMin = glm::vec3(kInf);
    outMax = glm::vec3(-kInf);
    for (const auto &v : vertices) {
        outMin = glm::min(outMin, v.pos);
        outMax = glm::max(outMax, v.pos);
    }
    if (vertices.empty()) {
        outMin = glm::vec3(0.0f);
        outMax = glm::vec3(0.0f);
    }
}

void ComputeBoundsFromIndexRange(const std::vector<Vertex> &vertices, const std::vector<uint32_t> &indices,
                                 uint32_t indexStart, uint32_t indexCount, glm::vec3 &outMin, glm::vec3 &outMax)
{
    constexpr float kInf = std::numeric_limits<float>::max();
    outMin = glm::vec3(kInf);
    outMax = glm::vec3(-kInf);

    for (uint32_t i = 0; i < indexCount; ++i) {
        const uint32_t index = indices[indexStart + i];
        if (index >= vertices.size())
            continue;
        outMin = glm::min(outMin, vertices[index].pos);
        outMax = glm::max(outMax, vertices[index].pos);
    }

    if (indexCount == 0 || outMin.x == kInf) {
        outMin = glm::vec3(0.0f);
        outMax = glm::vec3(0.0f);
    }
}

std::shared_ptr<InxMaterial> BuildPreviewMaterialFromSlotData(const MaterialSlotData *slotData,
                                                              const std::shared_ptr<InxMaterial> &defaultMat)
{
    if (!defaultMat)
        return nullptr;
    if (!slotData)
        return defaultMat;

    auto mat = defaultMat->Clone();
    if (!mat)
        return defaultMat;

    mat->SetColor("baseColor", slotData->baseColor);
    mat->SetColor("emissionColor", slotData->emissionColor);
    mat->SetFloat("metallic", slotData->metallic);
    mat->SetFloat("smoothness", slotData->smoothness);
    return mat;
}

std::shared_ptr<InxMaterial> ResolvePrefabPreviewMaterial(const json &componentJson, uint32_t materialSlot,
                                                          const std::shared_ptr<InxMesh> &assetMesh,
                                                          const std::shared_ptr<InxMaterial> &defaultMat,
                                                          const std::shared_ptr<InxMaterial> &errorMat)
{
    auto &registry = AssetRegistry::Instance();
    auto matsIt = componentJson.find("materials");
    if (matsIt != componentJson.end() && matsIt->is_array() && materialSlot < matsIt->size()) {
        const auto &slotJson = (*matsIt)[materialSlot];
        if (slotJson.is_string()) {
            const std::string guid = slotJson.get<std::string>();
            if (!guid.empty()) {
                auto mat = registry.GetAsset<InxMaterial>(guid);
                if (!mat)
                    mat = registry.LoadAsset<InxMaterial>(guid, ResourceType::Material);
                if (mat) {
                    if (!mat->IsDeleted())
                        return mat;
                    return errorMat ? errorMat : defaultMat;
                }
            }
        }
    }

    const MaterialSlotData *slotData = nullptr;
    if (assetMesh && materialSlot < assetMesh->GetMaterialSlotData().size())
        slotData = &assetMesh->GetMaterialSlotData()[materialSlot];
    return BuildPreviewMaterialFromSlotData(slotData, defaultMat);
}

bool AppendPrefabMeshComponent(const json &componentJson, const glm::mat4 &worldMatrix,
                               PrefabPreviewAggregate &aggregate, const std::shared_ptr<InxMaterial> &defaultMat,
                               const std::shared_ptr<InxMaterial> &errorMat)
{
    if (!componentJson.is_object())
        return false;
    const std::string compType = componentJson.value("type", std::string());
    if (compType != "MeshRenderer" && compType != "SkinnedMeshRenderer" && compType != "LineRenderer")
        return false;
    if (!componentJson.value("enabled", true))
        return false;

    std::shared_ptr<InxMesh> assetMesh;
    std::vector<Vertex> inlineVertices;
    std::vector<uint32_t> inlineIndices;
    std::vector<SubMesh> inlineSubMeshes;

    const std::vector<Vertex> *srcVertices = nullptr;
    const std::vector<uint32_t> *srcIndices = nullptr;
    const std::vector<SubMesh> *srcSubMeshes = nullptr;

    auto meshGuidIt = componentJson.find("meshAssetGuid");
    if (meshGuidIt != componentJson.end() && meshGuidIt->is_string()) {
        const std::string meshGuid = meshGuidIt->get<std::string>();
        if (!meshGuid.empty()) {
            auto &registry = AssetRegistry::Instance();
            assetMesh = registry.GetAsset<InxMesh>(meshGuid);
            if (!assetMesh)
                assetMesh = registry.LoadAsset<InxMesh>(meshGuid, ResourceType::Mesh);
            if (assetMesh) {
                srcVertices = &assetMesh->GetVertices();
                srcIndices = &assetMesh->GetIndices();
                srcSubMeshes = &assetMesh->GetSubMeshes();
            }
        }
    }

    if ((!srcVertices || !srcIndices || !srcSubMeshes) && componentJson.value("useInlineMesh", false)) {
        const std::string inlineName = componentJson.value("inlineMeshName", std::string());
        if (componentJson.value("inlineMeshBuiltin", false)) {
            const std::vector<Vertex> *builtinVertices = nullptr;
            const std::vector<uint32_t> *builtinIndices = nullptr;
            if (GetPreviewPrimitiveMeshData(inlineName, builtinVertices, builtinIndices)) {
                inlineVertices.assign(builtinVertices->begin(), builtinVertices->end());
                inlineIndices.assign(builtinIndices->begin(), builtinIndices->end());
            }
        } else {
            auto vertsIt = componentJson.find("inlineVertices");
            if (vertsIt != componentJson.end() && vertsIt->is_array()) {
                inlineVertices.reserve(vertsIt->size());
                for (const auto &vertexJson : *vertsIt) {
                    Vertex vertex{};
                    vertex.pos = ReadVec3(vertexJson, "pos", glm::vec3(0.0f));
                    vertex.normal = ReadVec3(vertexJson, "normal", glm::vec3(0.0f, 1.0f, 0.0f));
                    vertex.color = ReadVec3(vertexJson, "color", glm::vec3(1.0f));

                    auto tangentIt = vertexJson.find("tangent");
                    if (tangentIt != vertexJson.end() && tangentIt->is_array() && tangentIt->size() == 4) {
                        vertex.tangent = glm::vec4((*tangentIt)[0].get<float>(), (*tangentIt)[1].get<float>(),
                                                   (*tangentIt)[2].get<float>(), (*tangentIt)[3].get<float>());
                    } else {
                        vertex.tangent = glm::vec4(1.0f, 0.0f, 0.0f, 1.0f);
                    }

                    auto uvIt = vertexJson.find("texCoord");
                    if (uvIt != vertexJson.end() && uvIt->is_array() && uvIt->size() == 2) {
                        vertex.texCoord = glm::vec2((*uvIt)[0].get<float>(), (*uvIt)[1].get<float>());
                    }

                    inlineVertices.push_back(vertex);
                }
            }

            auto indicesIt = componentJson.find("inlineIndices");
            if (indicesIt != componentJson.end() && indicesIt->is_array()) {
                inlineIndices.reserve(indicesIt->size());
                for (const auto &indexJson : *indicesIt)
                    inlineIndices.push_back(indexJson.get<uint32_t>());
            }
        }

        if (!inlineVertices.empty() && !inlineIndices.empty()) {
            SubMesh inlineSubMesh;
            inlineSubMesh.indexStart = 0;
            inlineSubMesh.indexCount = static_cast<uint32_t>(inlineIndices.size());
            inlineSubMesh.vertexStart = 0;
            inlineSubMesh.vertexCount = static_cast<uint32_t>(inlineVertices.size());
            inlineSubMesh.materialSlot = 0;
            inlineSubMesh.nodeGroup = 0;
            inlineSubMesh.name = inlineName;
            ComputeBoundsFromVertices(inlineVertices, inlineSubMesh.boundsMin, inlineSubMesh.boundsMax);
            inlineSubMeshes.push_back(std::move(inlineSubMesh));

            srcVertices = &inlineVertices;
            srcIndices = &inlineIndices;
            srcSubMeshes = &inlineSubMeshes;
        }
    }

    if (!srcVertices || !srcIndices || !srcSubMeshes || srcVertices->empty() || srcIndices->empty())
        return false;

    const int32_t submeshFilter = componentJson.value("submeshIndex", -1);
    const int32_t nodeGroupFilter = componentJson.value("nodeGroup", -1);

    std::vector<const SubMesh *> selectedSubMeshes;
    selectedSubMeshes.reserve(srcSubMeshes->size());
    for (size_t subMeshIndex = 0; subMeshIndex < srcSubMeshes->size(); ++subMeshIndex) {
        const SubMesh &subMesh = (*srcSubMeshes)[subMeshIndex];
        if (subMesh.indexCount == 0)
            continue;
        if (submeshFilter >= 0 && static_cast<int32_t>(subMeshIndex) != submeshFilter)
            continue;
        if (nodeGroupFilter >= 0 && static_cast<int32_t>(subMesh.nodeGroup) != nodeGroupFilter)
            continue;
        selectedSubMeshes.push_back(&subMesh);
    }

    if (selectedSubMeshes.empty())
        return false;

    const glm::vec3 pivotOffset = ReadVec3(componentJson, "meshPivotOffset", glm::vec3(0.0f));
    const glm::mat3 world3x3(worldMatrix);
    glm::mat3 normalMatrix(1.0f);
    const float determinant = glm::determinant(world3x3);
    if (std::abs(determinant) > 1e-8f)
        normalMatrix = glm::transpose(glm::inverse(world3x3));
    const float tangentHandedness = determinant < 0.0f ? -1.0f : 1.0f;

    const uint32_t vertexBase = static_cast<uint32_t>(aggregate.vertices.size());
    aggregate.vertices.reserve(aggregate.vertices.size() + srcVertices->size());
    for (const auto &srcVertex : *srcVertices) {
        Vertex vertex = srcVertex;
        vertex.pos = glm::vec3(worldMatrix * glm::vec4(srcVertex.pos + pivotOffset, 1.0f));
        vertex.normal = NormalizeOrFallback(normalMatrix * srcVertex.normal, srcVertex.normal);
        vertex.tangent =
            glm::vec4(NormalizeOrFallback(normalMatrix * glm::vec3(srcVertex.tangent), glm::vec3(srcVertex.tangent)),
                      srcVertex.tangent.w * tangentHandedness);
        aggregate.vertices.push_back(vertex);
    }

    for (const SubMesh *subMesh : selectedSubMeshes) {
        SubMesh previewSubMesh;
        previewSubMesh.indexStart = static_cast<uint32_t>(aggregate.indices.size());
        previewSubMesh.vertexStart = vertexBase;
        previewSubMesh.vertexCount = static_cast<uint32_t>(srcVertices->size());
        previewSubMesh.materialSlot = static_cast<uint32_t>(aggregate.materials.size());
        previewSubMesh.nodeGroup = subMesh->nodeGroup;
        previewSubMesh.name = subMesh->name;

        const uint32_t indexEnd = subMesh->indexStart + subMesh->indexCount;
        aggregate.indices.reserve(aggregate.indices.size() + subMesh->indexCount);
        for (uint32_t index = subMesh->indexStart; index < indexEnd; ++index)
            aggregate.indices.push_back((*srcIndices)[index] + vertexBase);

        previewSubMesh.indexCount = static_cast<uint32_t>(aggregate.indices.size()) - previewSubMesh.indexStart;
        ComputeBoundsFromIndexRange(aggregate.vertices, aggregate.indices, previewSubMesh.indexStart,
                                    previewSubMesh.indexCount, previewSubMesh.boundsMin, previewSubMesh.boundsMax);

        aggregate.materials.push_back(
            ResolvePrefabPreviewMaterial(componentJson, subMesh->materialSlot, assetMesh, defaultMat, errorMat));
        aggregate.subMeshes.push_back(std::move(previewSubMesh));
    }

    return true;
}

void AppendPrefabNodePreview(const json &nodeJson, const glm::mat4 &parentWorld, bool parentActive,
                             PrefabPreviewAggregate &aggregate, const std::shared_ptr<InxMaterial> &defaultMat,
                             const std::shared_ptr<InxMaterial> &errorMat)
{
    if (!nodeJson.is_object())
        return;

    const bool isActive = parentActive && nodeJson.value("active", true);
    if (!isActive)
        return;

    const glm::mat4 worldMatrix = parentWorld * ReadNodeLocalMatrix(nodeJson);

    auto componentsIt = nodeJson.find("components");
    if (componentsIt != nodeJson.end() && componentsIt->is_array()) {
        for (const auto &componentJson : *componentsIt) {
            if (!componentJson.is_object())
                continue;

            auto dataIt = componentJson.find("data");
            if (dataIt == componentJson.end() || !dataIt->is_object())
                continue;

            json normalized = *dataIt;
            normalized["enabled"] = componentJson.value("enabled", true);
            std::string type = componentJson.value("type_id", std::string());
            const size_t typeSeparator = type.find_last_of(".:");
            if (typeSeparator != std::string::npos)
                type = type.substr(typeSeparator + 1);
            normalized["type"] = std::move(type);
            AppendPrefabMeshComponent(normalized, worldMatrix, aggregate, defaultMat, errorMat);
        }
    }

    auto childrenIt = nodeJson.find("children");
    if (childrenIt != nodeJson.end() && childrenIt->is_array()) {
        for (const auto &childJson : *childrenIt)
            AppendPrefabNodePreview(childJson, worldMatrix, isActive, aggregate, defaultMat, errorMat);
    }
}

bool BuildPrefabPreviewMesh(const std::string &prefabFilePath, std::shared_ptr<InxMesh> &outMesh,
                            std::vector<std::shared_ptr<InxMaterial>> &outMaterials)
{
    std::ifstream input(ToFsPath(prefabFilePath), std::ios::binary);
    if (!input.is_open())
        return false;

    json prefabJson = json::parse(input, nullptr, false);
    if (prefabJson.is_discarded())
        return false;

    auto rootIt = prefabJson.find("root_object");
    if (rootIt == prefabJson.end() || !rootIt->is_object())
        return false;

    auto &registry = AssetRegistry::Instance();
    auto defaultMat = registry.GetBuiltinMaterial("DefaultLit");
    auto errorMat = registry.GetBuiltinMaterial("ErrorMaterial");

    PrefabPreviewAggregate aggregate;
    AppendPrefabNodePreview(*rootIt, glm::mat4(1.0f), true, aggregate, defaultMat, errorMat);

    if (aggregate.vertices.empty() || aggregate.indices.empty() || aggregate.subMeshes.empty())
        return false;

    auto mesh = std::make_shared<InxMesh>(FromFsPath(ToFsPath(prefabFilePath).stem()));
    mesh->SetFilePath(prefabFilePath);
    mesh->SetData(std::move(aggregate.vertices), std::move(aggregate.indices), std::move(aggregate.subMeshes));

    outMesh = std::move(mesh);
    outMaterials = std::move(aggregate.materials);
    return true;
}

std::vector<std::shared_ptr<InxMaterial>> BuildDefaultPreviewMaterialsForMesh(const InxMesh &mesh)
{
    auto defaultMat = AssetRegistry::Instance().GetBuiltinMaterial("DefaultLit");
    std::vector<std::shared_ptr<InxMaterial>> materials;
    if (!defaultMat)
        return materials;

    uint32_t maxSlot = 0;
    for (const auto &subMesh : mesh.GetSubMeshes())
        maxSlot = std::max(maxSlot, subMesh.materialSlot + 1);

    const auto &slotData = mesh.GetMaterialSlotData();
    materials.reserve(maxSlot);
    for (uint32_t slot = 0; slot < maxSlot; ++slot) {
        const MaterialSlotData *data = slot < slotData.size() ? &slotData[slot] : nullptr;
        materials.push_back(BuildPreviewMaterialFromSlotData(data, defaultMat));
    }
    return materials;
}

bool IsPrefabPreviewPath(const std::string &filePath)
{
    std::string ext = FromFsPath(ToFsPath(filePath).extension());
    std::transform(ext.begin(), ext.end(), ext.begin(),
                   [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); });
    return ext == ".prefab";
}

} // namespace

struct LinkedShaderProgramLoadTicket::State
{
    struct Work
    {
        ShaderStagePair stages;
        std::string vertexPath;
        std::string fragmentPath;
        std::string vertexSource;
        std::string fragmentSource;
        uint64_t sourceStamp = 0;
        bool directStructuredStage = false;
        ShaderDescriptor fragmentDescriptor;
        LinkedShaderProgramArtifactCompilation compilation;
        std::string error;
    };

    std::vector<Work> work;
    JobHandle job;
    std::exception_ptr failure;
    std::thread::id ownerThread;
    std::thread::id producerThread;
    std::atomic<bool> cancelRequested{false};
    bool committed = false;
};

bool LinkedShaderProgramLoadTicket::IsComplete() const noexcept
{
    return !m_state || !m_state->job.IsValid() || m_state->job.IsComplete();
}

bool LinkedShaderProgramLoadTicket::IsCommitted() const noexcept
{
    return m_state && m_state->committed;
}

bool LinkedShaderProgramLoadTicket::WasProducedOnWorker() const noexcept
{
    return IsComplete() && m_state && m_state->producerThread != std::thread::id{} &&
           m_state->producerThread != m_state->ownerThread;
}

bool LinkedShaderProgramLoadTicket::Cancel() noexcept
{
    if (!m_state)
        return false;
    m_state->cancelRequested.store(true, std::memory_order_release);
    return !m_state->job.IsValid() || m_state->job.Cancel();
}

// ----------------------------------
// Helper method for validation
// ----------------------------------

bool Infernux::CheckEngineValid(const char *operation) const
{
    if (m_isCleanedUp) {
        INXLOG_ERROR("Cannot ", operation, ": Engine has been cleaned up.");
        return false;
    }
    if (m_isCleaningUp) {
        INXLOG_ERROR("Cannot ", operation, ": Engine is cleaning up.");
        return false;
    }
    return true;
}

// ----------------------------------
// Resources handling
// ----------------------------------

AssetDatabase *Infernux::GetAssetDatabase() const
{
    // After InitRenderer, ownership is transferred to AssetRegistry
    auto *adb = AssetRegistry::Instance().GetAssetDatabase();
    return adb ? adb : m_assetDatabase.get();
}

std::vector<AssetRuntimeRecord> Infernux::GetAssetRuntimeRecords() const
{
    const auto &registry = AssetRegistry::Instance();
    std::unordered_map<std::string, AssetRuntimeRecord> records;
    for (const auto &published : registry.GetAllPublishedAssetVersions()) {
        auto &record = records[published.guid];
        record.guid = published.guid;
        record.type = published.type;
        record.runtimeVersion = published.runtimeVersion;
    }
    for (const auto &cpu : registry.GetAllAssetResidency()) {
        auto &record = records[cpu.guid];
        record.guid = cpu.guid;
        record.type = cpu.type;
        record.runtimeTypeName = cpu.runtimeTypeName;
        record.runtimeVersion = cpu.runtimeVersion;
        record.cpuResident = true;
        record.cpuBytes = cpu.cpuBytes;
        record.explicitCpuPinCount = cpu.explicitPinCount;
        record.externalCpuReferenceCount = cpu.externalReferenceCount;
        record.cpuEvictable = cpu.evictable;
    }
    if (const auto *renderer = GetRenderer()) {
        for (const auto &gpu : renderer->GetAssetGpuResidency()) {
            auto &record = records[gpu.guid];
            record.guid = gpu.guid;
            if (record.runtimeVersion == 0) {
                record.runtimeVersion = gpu.runtimeVersion;
                record.type = gpu.domain == GpuAssetDomain::Mesh ? ResourceType::Mesh : ResourceType::Texture;
            }
            if (gpu.runtimeVersion != record.runtimeVersion) {
                record.staleGpuBytes += gpu.residentBytes;
                ++record.staleGpuAllocationCount;
                record.gpuVersionSynchronized = false;
                continue;
            }
            if (gpu.pending)
                record.gpuPendingBytes += gpu.residentBytes;
            else
                record.gpuResidentBytes += gpu.residentBytes;
            ++record.gpuAllocationCount;
            record.gpuPinned = record.gpuPinned || gpu.pinned;
        }
    }

    std::vector<AssetRuntimeRecord> result;
    result.reserve(records.size());
    for (auto &[guid, record] : records) {
        (void)guid;
        result.push_back(std::move(record));
    }
    std::sort(result.begin(), result.end(), [](const auto &left, const auto &right) { return left.guid < right.guid; });
    return result;
}

std::shared_ptr<LinkedShaderProgramLoadTicket>
Infernux::BeginPrepareLinkedShaderPrograms(const std::vector<std::string> &materialGuids)
{
    auto ticket = std::make_shared<LinkedShaderProgramLoadTicket>();
    ticket->m_state = std::make_shared<LinkedShaderProgramLoadTicket::State>();
    auto state = ticket->m_state;
    state->ownerThread = std::this_thread::get_id();

    if (!m_renderer)
        return ticket;
    auto *adb = GetAssetDatabase();
    if (!adb)
        return ticket;

    auto resolvePath = [&](const ShaderAssetReference &reference, const char *stage) {
        if (!reference.guid.empty())
            return adb->GetPathFromGuid(reference.guid);
        if (!reference.pathHint.empty())
            return std::string{};
        return adb->FindShaderPathById(reference.shaderId, stage);
    };
    auto readSource = [&](const std::string &path, std::string &source) {
        std::vector<char> bytes;
        if (!adb->ReadFile(path, bytes) || bytes.empty())
            return false;
        if (bytes.back() == '\0')
            bytes.pop_back();
        source.assign(bytes.begin(), bytes.end());
        return true;
    };

    std::unordered_set<ShaderStagePair, ShaderStagePairHash> scheduled;
    auto &registry = AssetRegistry::Instance();
    for (const auto &guid : materialGuids) {
        auto material = registry.GetAsset<InxMaterial>(guid);
        if (!material)
            continue;

        const ShaderStagePair stages{material->GetVertShaderName(), material->GetFragShaderName()};
        if (!stages.IsValid() || !scheduled.insert(stages).second)
            continue;
        const auto cached = m_linkedShaderProgramCache.find(stages);
        if (cached != m_linkedShaderProgramCache.end() && cached->second.sourceStamp != 0 &&
            cached->second.programKey.IsValid() && m_renderer->HasShaderProgramArtifact(cached->second.programKey)) {
            continue;
        }

        LinkedShaderProgramLoadTicket::State::Work work;
        work.stages = stages;
        work.vertexPath = resolvePath(material->GetVertShaderReference(), "vertex");
        work.fragmentPath = resolvePath(material->GetFragShaderReference(), "fragment");
        if (work.vertexPath.empty() || work.fragmentPath.empty() || !readSource(work.vertexPath, work.vertexSource) ||
            !readSource(work.fragmentPath, work.fragmentSource)) {
            continue;
        }
        work.sourceStamp =
            ComputeShaderProgramRevision(work.vertexSource, work.fragmentSource, ShaderCompileTarget::Forward, 0);
        if (cached != m_linkedShaderProgramCache.end() && cached->second.failedSourceStamp == work.sourceStamp)
            continue;
        state->work.push_back(std::move(work));
    }

    if (state->work.empty())
        return ticket;

    auto compile = [state]() {
        state->producerThread = std::this_thread::get_id();
        try {
            // One job compiles the scene's unique pairs serially. This keeps
            // compiler memory bounded on low-end machines while the shared
            // guard also protects editor hot reload and imported shaders.
            const InxShaderLoader::CompilationGuard compilationGuard;
            InxShaderLoader compiler(true, false, false, false, false, true, false, false, false, false);
            for (auto &work : state->work) {
                if (state->cancelRequested.load(std::memory_order_acquire))
                    return;
                const std::string vertexCompilePath =
                    InxShaderLoader::StageQualifiedVirtualPath(work.vertexPath, "vertex");
                const std::string fragmentCompilePath =
                    InxShaderLoader::StageQualifiedVirtualPath(work.fragmentPath, "fragment");
                const ShaderDescriptor vertexDescriptor =
                    compiler.ParseShaderSource(work.vertexSource, vertexCompilePath);
                work.fragmentDescriptor = compiler.ParseShaderSource(work.fragmentSource, fragmentCompilePath);
                if (IsDirectStructuredStage(vertexDescriptor) || IsDirectStructuredStage(work.fragmentDescriptor)) {
                    work.directStructuredStage = true;
                    continue;
                }
                if (vertexDescriptor.shaderId != work.stages.vertexShaderId ||
                    work.fragmentDescriptor.shaderId != work.stages.fragmentShaderId) {
                    work.error = "ShaderInfo Name must match the shader IDs referenced by the material";
                    continue;
                }
                work.compilation = compiler.CompileLinkedProgramArtifact(work.vertexSource, vertexCompilePath,
                                                                         work.fragmentSource, fragmentCompilePath);
                if (!work.compilation.IsValid()) {
                    std::ostringstream diagnostics;
                    for (const auto &error : work.compilation.errors) {
                        if (diagnostics.tellp() > 0)
                            diagnostics << '\n';
                        diagnostics << error;
                    }
                    work.error = diagnostics.str();
                    if (work.error.empty())
                        work.error = "Linked shader program compilation failed";
                }
            }
        } catch (...) {
            state->failure = std::current_exception();
        }
    };

    if (JobSystem::IsAvailable()) {
        state->job = JobSystem::Get().Schedule(std::move(compile), JobDomain::Asset, JobPriority::High);
    } else {
        compile();
    }
    return ticket;
}

bool Infernux::TryCommitLinkedShaderPrograms(const std::shared_ptr<LinkedShaderProgramLoadTicket> &ticket)
{
    if (!ticket || !ticket->m_state)
        return false;
    auto &state = *ticket->m_state;
    if (std::this_thread::get_id() != state.ownerThread)
        throw std::logic_error("linked shader program publication must run on the ticket owner thread");
    if (!ticket->IsComplete())
        return false;
    if (state.committed)
        return true;
    if (state.failure)
        std::rethrow_exception(state.failure);
    if (state.cancelRequested.load(std::memory_order_acquire))
        return false;

    for (auto &work : state.work) {
        if (work.directStructuredStage) {
            m_linkedShaderProgramCache.erase(work.stages);
            continue;
        }
        if (!work.error.empty()) {
            auto &entry = m_linkedShaderProgramCache[work.stages];
            entry.failedSourceStamp = work.sourceStamp;
            entry.lastError = work.error;
            INXLOG_ERROR("Linked shader prewarm rejected '", work.stages.ToString(), "': ", work.error);
            continue;
        }

        ShaderProgramArtifact artifact = work.compilation.CreateRuntimeArtifact();
        if (!artifact.IsValid() || artifact.key.stages != work.stages ||
            !m_renderer->PublishShaderProgramArtifact(artifact)) {
            auto &entry = m_linkedShaderProgramCache[work.stages];
            entry.failedSourceStamp = work.sourceStamp;
            entry.lastError = "Renderer rejected the prewarmed linked shader program artifact";
            INXLOG_ERROR("Linked shader prewarm publication failed for '", work.stages.ToString(), "'");
            continue;
        }

        m_linkedShaderProgramCache[work.stages] = LinkedShaderProgramCacheEntry{work.sourceStamp, artifact.key, 0, {}};
        const auto &fragment = work.fragmentDescriptor;
        m_renderer->StoreShaderRenderMeta(work.stages.fragmentShaderId, fragment.surfaceOptions.cullMode,
                                          fragment.depthWrite, fragment.depthTest, fragment.surfaceOptions.blendMode,
                                          fragment.renderQueue, fragment.passTag, fragment.stencil,
                                          fragment.surfaceOptions.alphaClip);
    }
    state.committed = true;
    return true;
}

// ----------------------------------
// Lifecycle
// ----------------------------------

Infernux::Infernux(std::string dllPath, RuntimeMode mode) : m_runtimeMode(mode), m_isCleanedUp(false)
{
    (void)dllPath;
    INXLOG_DEBUG("Create Infernux.");
    m_assetDatabase = std::make_unique<AssetDatabase>();

    if (m_runtimeMode == RuntimeMode::Graphical) {
        INXLOG_DEBUG("Create Infernux Renderer.");
        m_renderer = std::make_unique<InxRenderer>();
        m_renderer->SetShaderProgramArtifactResolver([this](const std::shared_ptr<InxMaterial> &material) {
            const LinkedShaderProgramPreparation prepared = EnsureLinkedShaderProgramArtifact(material);
            if (prepared.usesLinkedArtifact && !prepared.success) {
                static std::unordered_set<std::string> reportedFailures;
                const std::string materialKey = material ? material->GetMaterialKey() : std::string("<null>");
                const std::string stages = material
                                               ? material->GetVertShaderName() + "|" + material->GetFragShaderName()
                                               : std::string("<unknown>");
                const std::string failureKey = materialKey + "|" + stages + "|" + prepared.error;
                if (reportedFailures.insert(failureKey).second) {
                    INXLOG_ERROR("Material shader rebuild rejected for '", materialKey, "' (", stages,
                                 "): ", prepared.error,
                                 ". The previous valid GPU pipeline remains active; this failure will not be retried "
                                 "until the shader inputs change.");
                }
            }
        });
        m_renderer->SetShaderAssetResolver([this](const std::string &shaderId, const std::string &shaderType) {
            return EnsureShaderLoaded(shaderId, shaderType);
        });
    }
}

Infernux::~Infernux()
{
    Cleanup();
}

void Infernux::Run()
{
    if (!CheckEngineValid("run") || !m_isInitialized) {
        throw std::logic_error("Cannot run an uninitialized engine");
    }

    m_exitRequested.store(false, std::memory_order_release);
    INXLOG_DEBUG("Run Infernux.");
    const AcceptanceClock acceptanceClock = ReadPlayerAcceptanceClock();
    if (m_runtimeMode == RuntimeMode::Headless) {
        auto previous = std::chrono::steady_clock::now();
        while (!m_exitRequested.load(std::memory_order_acquire)) {
            const auto now = std::chrono::steady_clock::now();
            const float deltaTime = std::min(std::chrono::duration<float>(now - previous).count(), 1.0f / 3.0f);
            previous = now;
            Tick(deltaTime);

            std::unique_lock<std::mutex> lock(m_runMutex);
            m_runCv.wait_for(lock, std::chrono::milliseconds(1),
                             [this] { return m_exitRequested.load(std::memory_order_acquire); });
        }
        INXLOG_DEBUG("Headless loop ended.");
        return;
    }

    m_runLoopActive.store(true, std::memory_order_release);
    struct RunLoopGuard
    {
        std::atomic<bool> &flag;
        ~RunLoopGuard()
        {
            flag.store(false, std::memory_order_release);
        }
    } runLoopGuard{m_runLoopActive};

    bool acceptancePauseApplied = false;
    while (!m_exitRequested.load(std::memory_order_acquire) && m_renderer->GetUserEvent()) {
        if (acceptanceClock.enabled) {
            const auto &sceneManager = SceneManager::Instance();
            m_renderer->OverrideNextFrameDeltaTime(sceneManager.IsPaused() ? 0.0f : acceptanceClock.fixedDeltaSeconds);
        }
        m_renderer->DrawFrame();
        auto &sceneManager = SceneManager::Instance();
        if (acceptanceClock.enabled && !acceptancePauseApplied && sceneManager.IsPlaying() &&
            !sceneManager.IsPaused() && sceneManager.GetRuntimeFrameCount() >= acceptanceClock.pauseAfterFrame) {
            if (sceneManager.GetRuntimeFrameCount() != acceptanceClock.pauseAfterFrame) {
                throw std::logic_error("Player acceptance clock advanced past its requested capture frame");
            }
            sceneManager.Pause();
            acceptancePauseApplied = true;
            INXLOG_INFO("INFERNUX_PLAYER_ACCEPTANCE_PAUSED frame=", sceneManager.GetRuntimeFrameCount(),
                        " fixed_delta=", acceptanceClock.fixedDeltaSeconds);
        }

        // Periodically save layout when ImGui marks it dirty
        ImGuiIO &io = ImGui::GetIO();
        if (io.WantSaveIniSettings) {
            SaveImGuiLayout();
            io.WantSaveIniSettings = false;
        }
    }
    INXLOG_DEBUG("Main loop ended.");
    SaveImGuiLayout();
    // NOTE: Cleanup is no longer called here — Python controls the
    // shutdown order so it can stop background threads first.
    // ~Infernux() still calls Cleanup() as a safety net.
}

void Infernux::Tick(float deltaTime)
{
    if (!CheckEngineValid("tick") || !m_isInitialized) {
        throw std::logic_error("Cannot tick an uninitialized engine");
    }
    if (!std::isfinite(deltaTime) || deltaTime < 0.0f) {
        throw std::invalid_argument("delta_time must be finite and non-negative");
    }

    if (m_runtimeMode == RuntimeMode::Graphical) {
        // Headless-style manual stepping must stay usable with a window open:
        // one Tick = one fully simulated AND rendered frame with an exact
        // delta time. The only exclusion is the free-running Run() loop,
        // which would otherwise double-step the simulation.
        if (m_runLoopActive.load(std::memory_order_acquire)) {
            throw std::logic_error("tick cannot be called while run() is driving frames");
        }
        if (!m_renderer->GetUserEvent()) {
            Exit();
            return;
        }
        m_renderer->OverrideNextFrameDeltaTime(deltaTime);
        m_renderer->DrawFrame();
        return;
    }

    auto &sceneManager = SceneManager::Instance();
    if (m_preSceneUpdateCallback)
        m_preSceneUpdateCallback(deltaTime);
    const float sceneDeltaTime = sceneManager.ConsumeFrameDeltaTime(deltaTime);
    TransformECSStore::Instance().BeginFrameCache(sceneManager.GetActiveScene());
    sceneManager.Update(sceneDeltaTime);
    sceneManager.LateUpdate(sceneDeltaTime);
    // Mirror the graphical DrawFrame simulation segment exactly (see
    // InxRenderer::DrawFrame). Headless previously skipped the world-matrix
    // sync and the FinalTransformResolve/AnimationTimeline barriers, so
    // transform reads between frames and Python systems registered on those
    // barriers observed a different world than the graphical runtime.
    // Graphics-only barriers (RenderExtraction, RenderGraph,
    // SnapshotPublication) stay renderer-owned by design.
    if (TransformECSStore::Instance().EndFrameCache())
        sceneManager.PublishPhysicsTransformsToRenderer();
    if (Scene *activeScene = sceneManager.GetActiveScene())
        TransformECSStore::Instance().SyncSceneWorldMatrices(activeScene);
    sceneManager.PublishAuthoredTransformsToPhysics();
    sceneManager.EmitRuntimeFrameBarrier(SceneManager::RuntimeFrameBarrier::FinalTransformResolve);
    sceneManager.EmitRuntimeFrameBarrier(SceneManager::RuntimeFrameBarrier::AnimationTimeline);
    sceneManager.EndFrame();
}

void Infernux::SetPreSceneUpdateCallback(std::function<void(float)> callback)
{
    m_preSceneUpdateCallback = std::move(callback);
    if (!m_renderer)
        return;

    // Keep the Python-owning callable in one place. Copying it into the
    // renderer gives two independent py::function lifetimes and makes native
    // renderer teardown responsible for releasing a Python object. The
    // renderer only needs a native forwarding edge while the engine is live.
    if (m_preSceneUpdateCallback) {
        m_renderer->SetPreSceneUpdateCallback([this](float deltaTime) {
            if (m_preSceneUpdateCallback)
                m_preSceneUpdateCallback(deltaTime);
        });
    } else {
        m_renderer->SetPreSceneUpdateCallback(nullptr);
    }
}

void Infernux::Exit()
{
    INXLOG_DEBUG("Exit requested.");
    m_exitRequested.store(true, std::memory_order_release);
    m_runCv.notify_all();
}

void Infernux::Cleanup()
{
    if (m_isCleanedUp)
        return;

    m_isCleaningUp = true;
    m_preSceneUpdateCallback = nullptr;

    if (m_runtimeMode == RuntimeMode::Graphical) {
        if (m_isInitialized) {
            SaveImGuiLayout();
        }
    }
    DrainPreviewJobs();

    // Destroy all scenes/GameObjects FIRST: Collider destructors release
    // their Jolt bodies (needs a live PhysicsWorld) and AudioSource
    // destructors detach from the AudioEngine. Singletons are intentionally
    // leaked, so this explicit call is the only place scene teardown happens.
    SceneManager::Instance().Shutdown();

    if (auto *assetDatabase = AssetRegistry::Instance().GetAssetDatabase())
        assetDatabase->FlushDerivedIndex(/*waitForPendingScan=*/false);

    // Event callbacks may capture this engine lifetime. Drop them together
    // with all runtime dependency edges before another engine is initialized.
    AssetDependencyGraph::Instance().Clear();

    AudioEngine::Instance().Shutdown();
    PhysicsWorld::Instance().Shutdown();

    // CPU asset preparation holds loader pointers and must finish before the
    // renderer or headless path stops the engine-wide JobSystem.
    AssetRegistry::Instance().DrainPendingLoads();

    m_renderer.reset();

    if (m_runtimeMode == RuntimeMode::Headless) {
        JobSystem::Shutdown();
    }

    // AssetRegistry owns all loaded assets + builtins.
    AssetRegistry::Instance().Shutdown();

    // All document producers are now stopped. No accepted scene, material,
    // metadata, settings, or prefab write may outlive this engine lifetime.
    DocumentStore::Instance().Shutdown();

    m_assetDatabase.reset();
    m_extLoader.reset();

    m_isCleanedUp = true;
    m_isInitialized = false;
    m_isCleaningUp = false;
    INXLOG_DEBUG("Cleanup completed.");
#if INFERNUX_FILE_LOGGING
    INXLOG_FLUSH_FILE();
    INXLOG_SHUTDOWN();
#endif
}

void Infernux::DrainPreviewJobs()
{
    JobHandle dispatcher;
    {
        std::lock_guard<std::mutex> lock(m_previewJobMutex);
        m_acceptPreviewJobs = false;
        dispatcher = m_previewDispatcherJob;
    }

    if (dispatcher.IsValid() && JobSystem::IsAvailable())
        JobSystem::Get().WaitPassive(dispatcher);

    {
        std::lock_guard<std::mutex> lock(m_previewJobMutex);
        if (!m_previewJobs.empty() || m_previewDispatcherScheduled) {
            // This runs on the shutdown path: an incompletely drained preview
            // queue is a diagnostic, not a reason to abort Cleanup halfway
            // (throwing here used to leave the renderer and DocumentStore
            // alive until the exit watchdog hard-killed the process).
            INXLOG_ERROR("Preview dispatcher did not drain all accepted jobs (", m_previewJobs.size(),
                         " left); discarding them for shutdown");
            std::queue<std::function<void()>> emptyJobs;
            m_previewJobs.swap(emptyJobs);
            m_previewDispatcherScheduled = false;
        }
        m_previewDispatcherJob = {};
    }

    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    std::queue<TexturePreviewCompleted> emptyTextures;
    m_texturePreviewCompletedQueue.swap(emptyTextures);
    std::queue<MaterialPreviewRequest> emptyMaterials;
    m_previewRequestQueue.swap(emptyMaterials);
    std::queue<MeshPreviewRequest> emptyMeshes;
    m_meshPreviewRequestQueue.swap(emptyMeshes);
    m_materialPreviewStates.clear();
    m_texturePreviewStates.clear();
    m_meshPreviewStates.clear();
    m_hasPendingPreviewUploads.store(false, std::memory_order_release);
    m_hasPreviewPumpWork.store(false, std::memory_order_release);
}

void Infernux::EnqueuePreviewTask(std::function<void()> fn)
{
    if (!fn)
        throw std::invalid_argument("preview job requires a callable");
    if (!JobSystem::IsAvailable())
        throw std::logic_error("preview jobs require an initialized JobSystem");

    std::lock_guard<std::mutex> lock(m_previewJobMutex);
    if (!m_acceptPreviewJobs)
        throw std::runtime_error("preview jobs are no longer accepted during engine shutdown");

    m_previewJobs.push(std::move(fn));
    if (m_previewDispatcherScheduled)
        return;

    m_previewDispatcherScheduled = true;
    try {
        m_previewDispatcherJob = JobSystem::Get().Schedule([this]() {
            for (;;) {
                std::function<void()> task;
                {
                    std::lock_guard<std::mutex> lock(m_previewJobMutex);
                    if (m_previewJobs.empty()) {
                        m_previewDispatcherScheduled = false;
                        return;
                    }
                    task = std::move(m_previewJobs.front());
                    m_previewJobs.pop();
                }
                try {
                    task();
                } catch (const std::exception &error) {
                    INXLOG_ERROR("Preview worker job failed: ", error.what());
                } catch (...) {
                    INXLOG_ERROR("Preview worker job failed with an unknown exception");
                }
            }
        });
    } catch (...) {
        m_previewDispatcherScheduled = false;
        m_previewJobs.pop();
        throw;
    }
}

std::string Infernux::BuildPreviewTextureName(const std::string &resourceKey)
{
    const auto hv = std::hash<std::string>{}(resourceKey);
    return std::string("__cpp_preview_mat__") + std::to_string(static_cast<unsigned long long>(hv));
}

std::string Infernux::BuildTexturePreviewTextureName(const std::string &resourceKey)
{
    const auto hv = std::hash<std::string>{}(resourceKey);
    return std::string("__cpp_preview_tex__") + std::to_string(static_cast<unsigned long long>(hv));
}

std::string Infernux::BuildMeshPreviewTextureName(const std::string &resourceKey)
{
    const auto hv = std::hash<std::string>{}(resourceKey);
    return std::string("__cpp_preview_mesh__") + std::to_string(static_cast<unsigned long long>(hv));
}

// Canonicalize the path portion of a preview cache key so callers that spell the
// same file differently map to ONE cache entry. The C++ Project panel builds keys
// with absolute forward-slash paths (FromFsPath), while the Python inspector passes
// os.path.normpath() results (backslashes on Windows). Without this, "mat|D:/a/b.mat"
// and "mat|D:\a\b.mat" are distinct entries, so the inspector can never reuse the
// Project panel's rendered texture (the long-standing flaky-preview bug). Only the
// MAP KEY is normalized here — the real file path used for disk I/O is passed
// separately and left untouched. Non-ASCII (UTF-8) bytes are preserved.
/// Numeric ImGui handles are frame-scoped observations. Callers retain the stable
/// texture name and re-resolve the currently published descriptor before drawing.
static uint64_t LiveImGuiTextureId(const InxRenderer *renderer, const std::string &texName)
{
    if (!renderer || texName.empty())
        return 0;
    return renderer->GetImGuiTextureId(texName);
}

static std::string CanonicalizePreviewKey(const std::string &resourceKey)
{
    const size_t bar = resourceKey.find('|');
    const size_t start = (bar == std::string::npos) ? 0 : bar + 1;
    std::string out = resourceKey;
    for (size_t i = start; i < out.size(); ++i) {
        char &c = out[i];
        if (c == '\\') {
            c = '/';
        }
#ifdef _WIN32
        else if (c >= 'A' && c <= 'Z') {
            c = static_cast<char>(c - 'A' + 'a'); // Windows paths are case-insensitive
        }
#endif
    }
    return out;
}

static void DownsampleNearestRgba(const std::vector<unsigned char> &src, int srcW, int srcH, int maxPx,
                                  std::vector<unsigned char> &dst, int &dstW, int &dstH)
{
    if (srcW <= 0 || srcH <= 0 || src.empty()) {
        dst.clear();
        dstW = 0;
        dstH = 0;
        return;
    }

    if (maxPx <= 0 || (srcW <= maxPx && srcH <= maxPx)) {
        dst = src;
        dstW = srcW;
        dstH = srcH;
        return;
    }

    const float scale = static_cast<float>(maxPx) / static_cast<float>(std::max(srcW, srcH));
    dstW = std::max(1, static_cast<int>(srcW * scale));
    dstH = std::max(1, static_cast<int>(srcH * scale));
    dst.resize(static_cast<size_t>(dstW) * static_cast<size_t>(dstH) * 4u);

    const int rowStride = srcW * 4;
    for (int dy = 0; dy < dstH; ++dy) {
        const int sy = std::min(static_cast<int>((dy + 0.5f) * srcH / dstH), srcH - 1);
        const int rowOff = sy * rowStride;
        for (int dx = 0; dx < dstW; ++dx) {
            const int sx = std::min(static_cast<int>((dx + 0.5f) * srcW / dstW), srcW - 1);
            const int srcIdx = rowOff + sx * 4;
            const int dstIdx = (dy * dstW + dx) * 4;
            dst[dstIdx + 0] = src[srcIdx + 0];
            dst[dstIdx + 1] = src[srcIdx + 1];
            dst[dstIdx + 2] = src[srcIdx + 2];
            dst[dstIdx + 3] = src[srcIdx + 3];
        }
    }
}

static void ApplyLinearToSrgbPreviewInPlace(std::vector<unsigned char> &pixels)
{
    if (pixels.empty())
        return;

    uint8_t lut[256];
    for (int i = 0; i < 256; ++i) {
        const float linear = static_cast<float>(i) / 255.0f;
        const float encoded = linear <= 0.0031308f ? linear * 12.92f : 1.055f * std::pow(linear, 1.0f / 2.4f) - 0.055f;
        lut[i] = static_cast<uint8_t>(std::clamp(static_cast<int>(encoded * 255.0f + 0.5f), 0, 255));
    }

    for (size_t i = 0; i + 3 < pixels.size(); i += 4) {
        pixels[i + 0] = lut[pixels[i + 0]];
        pixels[i + 1] = lut[pixels[i + 1]];
        pixels[i + 2] = lut[pixels[i + 2]];
    }
}

static void ApplyTextureFormatPreviewInPlace(std::vector<unsigned char> &pixels, const std::string &format)
{
    if (format != "rgba4444")
        return;
    for (unsigned char &value : pixels) {
        const unsigned int nibble = (static_cast<unsigned int>(value) + 8U) / 17U;
        value = static_cast<unsigned char>((std::min)(15U, nibble) * 17U);
    }
}

static void ApplyNormalMapPreviewInPlace(std::vector<unsigned char> &pixels, int width, int height)
{
    if (pixels.empty() || width <= 0 || height <= 0)
        return;

    const std::vector<unsigned char> source = pixels;
    constexpr float kStrength = 2.0f;
    const auto sampleLuminance = [&source, width, height](int x, int y) {
        x = std::clamp(x, 0, width - 1);
        y = std::clamp(y, 0, height - 1);
        const size_t index = (static_cast<size_t>(y) * static_cast<size_t>(width) + static_cast<size_t>(x)) * 4u;
        return (static_cast<float>(source[index]) * 0.299f + static_cast<float>(source[index + 1]) * 0.587f +
                static_cast<float>(source[index + 2]) * 0.114f) /
               255.0f;
    };

    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            float nx = (sampleLuminance(x - 1, y) - sampleLuminance(x + 1, y)) * kStrength;
            float ny = (sampleLuminance(x, y - 1) - sampleLuminance(x, y + 1)) * kStrength;
            float nz = 1.0f;
            const float inverseLength = 1.0f / std::sqrt(nx * nx + ny * ny + nz * nz);
            nx *= inverseLength;
            ny *= inverseLength;
            nz *= inverseLength;

            const size_t index = (static_cast<size_t>(y) * static_cast<size_t>(width) + static_cast<size_t>(x)) * 4u;
            pixels[index] = static_cast<unsigned char>((nx * 0.5f + 0.5f) * 255.0f);
            pixels[index + 1] = static_cast<unsigned char>((ny * 0.5f + 0.5f) * 255.0f);
            pixels[index + 2] = static_cast<unsigned char>((nz * 0.5f + 0.5f) * 255.0f);
            pixels[index + 3] = 255;
        }
    }
}

struct PreviewPixelSummary
{
    uint64_t hash = UINT64_C(1469598103934665603);
    uint32_t nonTransparentPixelCount = 0;
    uint8_t minRgb = 0;
    uint8_t maxRgb = 0;
};

static PreviewPixelSummary SummarizePreviewPixels(const std::vector<unsigned char> &pixels)
{
    PreviewPixelSummary summary;
    if (pixels.empty()) {
        summary.hash = 0;
        return summary;
    }

    uint8_t minRgb = 255;
    uint8_t maxRgb = 0;
    for (size_t index = 0; index < pixels.size(); ++index) {
        const uint8_t value = pixels[index];
        summary.hash ^= value;
        summary.hash *= UINT64_C(1099511628211);
        const size_t channel = index & 3u;
        if (channel < 3u) {
            minRgb = (std::min)(minRgb, value);
            maxRgb = (std::max)(maxRgb, value);
        } else if (value != 0) {
            ++summary.nonTransparentPixelCount;
        }
    }
    summary.minRgb = minRgb;
    summary.maxRgb = maxRgb;
    return summary;
}

uint64_t Infernux::QueryOrScheduleMaterialPreview(const std::string &resourceKey, const std::string &matFilePath,
                                                  const std::string &materialJson, uint64_t fileMtimeHint,
                                                  bool authoring)
{
    if (resourceKey.empty())
        return 0;

    const std::string key = CanonicalizePreviewKey(resourceKey);

    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    auto &state = m_materialPreviewStates[key];
    if (state.textureName.empty())
        state.textureName = BuildPreviewTextureName(key);

    state.textureId = LiveImGuiTextureId(m_renderer.get(), state.textureName);
    // An open authored document owns this shared preview until its durable
    // write has completed. Passive ProjectPanel requests must not publish the
    // previous disk revision while that hand-off is still in progress.
    if (!authoring && state.authoring)
        return state.textureId;
    if (authoring)
        state.authoring = true;

    state.latestMatFilePath = matFilePath;
    state.latestMaterialJson = materialJson;

    // ── Detect content changes ──────────────────────────────────
    std::string renderJson; // JSON to use if we schedule a render

    if (!materialJson.empty()) {
        const uint64_t h = std::hash<std::string>{}(materialJson);
        if (h != state.lastJsonHash) {
            state.lastJsonHash = h;
            state.generation++;
            if (state.pendingUploadVersion != 0) {
                m_renderer->SupersedePendingImGuiTextureUploads(state.textureName);
                state.pendingUploadVersion = 0;
                state.pendingPreviewGeneration = 0;
                state.pendingSize = 0;
                state.inFlight = false;
            }
        }
        renderJson = materialJson; // prefer JSON for rendering
    }

    if (fileMtimeHint != 0 && fileMtimeHint != state.lastFileMtime) {
        state.lastFileMtime = fileMtimeHint;
        // Only bump generation from mtime if no JSON was provided in this call
        // (avoids double-bump when both are present).
        if (materialJson.empty()) {
            state.generation++;
            if (state.pendingUploadVersion != 0) {
                m_renderer->SupersedePendingImGuiTextureUploads(state.textureName);
                state.pendingUploadVersion = 0;
                state.pendingPreviewGeneration = 0;
                state.pendingSize = 0;
                state.inFlight = false;
            }
        }
    }

    // First-ever request from a passive caller (the inspector shares this "mat|"
    // key but passes neither JSON nor an mtime hint — the Project panel owns
    // mtime-based change detection so the two don't fight over the generation).
    // Force one render so the shared preview appears instead of staying blank.
    if (state.generation == 0)
        state.generation = 1;

    // ── Already up-to-date? ─────────────────────────────────────
    if (state.readyGeneration == state.generation && state.textureId != 0) {
        return state.textureId;
    }
    if (state.readyGeneration == state.generation && state.pendingUploadVersion == 0)
        state.readyGeneration = 0;

    // ── Schedule render if not already in flight ────────────────
    if (!state.inFlight && state.readyGeneration < state.generation) {
        state.inFlight = true;
        m_previewRequestQueue.push(MaterialPreviewRequest{key, matFilePath, state.generation, renderJson});
        m_hasPreviewPumpWork.store(true, std::memory_order_release);
        if (m_renderer)
            m_renderer->RequestFullSpeedFrame();
    }

    // Stale-return: keep showing old preview while new one renders (no flicker).
    return state.textureId;
}

int Infernux::PumpMaterialPreviewUploads(int uploadBudget, bool ignoreCooldown)
{
    if (!m_renderer || uploadBudget <= 0)
        return 0;

    constexpr int kMaterialPreviewSize = 200;
    // Shader programs, preview framebuffers and texture publications can all
    // become ready a few frames after ProjectPanel starts prewarming assets.
    // A CPU preview is useful for genuinely unsupported materials, but it must
    // not become the permanent thumbnail merely because the GPU path was still
    // starting up. Keep the retries bounded so malformed/custom materials
    // eventually retain the existing fallback behavior.
    constexpr uint16_t kMaxTransientGpuPreviewFailures = 60;
    int consumed = 0;
    struct CompletedMaterialRender
    {
        std::string resourceKey;
        uint64_t generation = 0;
        std::shared_ptr<vk::ImageReadbackTicket> ticket;
        std::shared_ptr<InxMaterial> material;
    };
    std::vector<CompletedMaterialRender> completedRenders;
    {
        std::lock_guard<std::mutex> lock(m_previewResultMutex);
        for (const auto &[resourceKey, state] : m_materialPreviewStates) {
            if (state.renderTicket && state.renderTicket->IsDone())
                completedRenders.push_back(
                    {resourceKey, state.renderGeneration, state.renderTicket, state.renderMaterial});
        }
    }

    AssetDatabase *assetDatabase = GetAssetDatabase();
    for (const auto &completed : completedRenders) {
        if (consumed >= uploadBudget)
            break;
        std::vector<unsigned char> pixels;
        if (!m_renderer->TryCompleteMaterialPreviewGPU(completed.ticket, kMaterialPreviewSize, pixels))
            MaterialPreviewer::RenderCpuPreview(completed.material, kMaterialPreviewSize, pixels, assetDatabase);
        const PreviewPixelSummary pixelSummary = SummarizePreviewPixels(pixels);

        std::string textureName;
        {
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            auto it = m_materialPreviewStates.find(completed.resourceKey);
            if (it == m_materialPreviewStates.end() || it->second.renderTicket != completed.ticket)
                continue;
            it->second.renderTicket.reset();
            it->second.renderMaterial.reset();
            it->second.renderGeneration = 0;
            if (pixels.empty() || it->second.generation != completed.generation) {
                it->second.inFlight = false;
                if (!pixels.empty() && it->second.readyGeneration < it->second.generation) {
                    it->second.inFlight = true;
                    m_previewRequestQueue.push(MaterialPreviewRequest{
                        completed.resourceKey,
                        it->second.latestMatFilePath,
                        it->second.generation,
                        it->second.latestMaterialJson,
                    });
                    m_hasPreviewPumpWork.store(true, std::memory_order_release);
                    m_renderer->RequestFullSpeedFrame();
                }
                continue;
            }
            if (it->second.textureName.empty())
                it->second.textureName = BuildPreviewTextureName(completed.resourceKey);
            textureName = it->second.textureName;
        }

        try {
            const uint64_t uploadVersion =
                m_renderer->SubmitTextureForImGui(textureName, pixels.data(), pixels.size(), kMaterialPreviewSize,
                                                  kMaterialPreviewSize, rhi::FilterMode::Linear);
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            auto it = m_materialPreviewStates.find(completed.resourceKey);
            if (it != m_materialPreviewStates.end()) {
                it->second.pendingUploadVersion = uploadVersion;
                it->second.pendingPreviewGeneration = completed.generation;
                it->second.pendingSize = kMaterialPreviewSize;
                it->second.pixelGeneration = completed.generation;
                it->second.pixelHash = pixelSummary.hash;
                it->second.nonTransparentPixelCount = pixelSummary.nonTransparentPixelCount;
                it->second.minRgb = pixelSummary.minRgb;
                it->second.maxRgb = pixelSummary.maxRgb;
                m_hasPendingPreviewUploads.store(true, std::memory_order_release);
                m_hasPreviewPumpWork.store(true, std::memory_order_release);
            }
            ++consumed;
        } catch (const std::exception &error) {
            INXLOG_ERROR("Failed to submit material preview texture: ", error.what());
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            auto it = m_materialPreviewStates.find(completed.resourceKey);
            if (it != m_materialPreviewStates.end())
                it->second.inFlight = false;
        }
    }

    bool renderBusy = false;
    size_t queueSize = 0;
    {
        std::lock_guard<std::mutex> lock(m_previewResultMutex);
        renderBusy = std::any_of(m_materialPreviewStates.begin(), m_materialPreviewStates.end(),
                                 [](const auto &entry) { return static_cast<bool>(entry.second.renderTicket); });
        queueSize = m_previewRequestQueue.size();
    }
    if (renderBusy)
        m_hasPreviewPumpWork.store(true, std::memory_order_release);
    if (renderBusy || queueSize == 0)
        return consumed;

    constexpr int kMaterialCooldownMs = 100;
    auto now = std::chrono::steady_clock::now();
    auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - m_lastMaterialRenderTime);
    if (!ignoreCooldown && queueSize < 2 && elapsed.count() < kMaterialCooldownMs) {
        m_hasPreviewPumpWork.store(true, std::memory_order_release);
        return consumed;
    }

    MaterialPreviewRequest request;
    {
        std::lock_guard<std::mutex> lock(m_previewResultMutex);
        if (m_previewRequestQueue.empty())
            return consumed;
        request = std::move(m_previewRequestQueue.front());
        m_previewRequestQueue.pop();
    }

    std::shared_ptr<InxMaterial> material;
    if (!request.materialJson.empty()) {
        material = MaterialPreviewer::BuildPreviewMaterialFromJson(request.materialJson, assetDatabase);
    } else {
        std::string embeddedModel;
        int embeddedSlot = -1;
        if (ParseModelEmbeddedMaterialSlot(request.matFilePath, embeddedModel, embeddedSlot))
            material =
                MaterialPreviewer::BuildEmbeddedPreviewMaterial(embeddedModel, static_cast<uint32_t>(embeddedSlot));
        else
            material = MaterialPreviewer::BuildPreviewMaterialFromFile(request.matFilePath, assetDatabase);
    }

    if (!material) {
        std::lock_guard<std::mutex> lock(m_previewResultMutex);
        auto it = m_materialPreviewStates.find(request.resourceKey);
        if (it != m_materialPreviewStates.end())
            it->second.inFlight = false;
        return consumed;
    }

    bool texturePending = false;
    auto ticket = m_renderer->BeginMaterialPreviewGPU(material, kMaterialPreviewSize, &texturePending);
    if (texturePending) {
        std::lock_guard<std::mutex> lock(m_previewResultMutex);
        auto it = m_materialPreviewStates.find(request.resourceKey);
        if (it != m_materialPreviewStates.end()) {
            const auto &state = it->second;
            m_previewRequestQueue.push(MaterialPreviewRequest{
                request.resourceKey,
                state.latestMatFilePath,
                state.generation,
                state.latestMaterialJson,
                request.transientGpuFailures,
            });
            m_hasPreviewPumpWork.store(true, std::memory_order_release);
            m_renderer->RequestFullSpeedFrame();
        }
        m_lastMaterialRenderTime = now;
        return consumed;
    }
    if (!ticket) {
        if (request.transientGpuFailures < kMaxTransientGpuPreviewFailures) {
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            auto it = m_materialPreviewStates.find(request.resourceKey);
            if (it != m_materialPreviewStates.end() && it->second.generation == request.generation) {
                ++request.transientGpuFailures;
                request.matFilePath = it->second.latestMatFilePath;
                request.materialJson = it->second.latestMaterialJson;
                m_previewRequestQueue.push(std::move(request));
                m_hasPreviewPumpWork.store(true, std::memory_order_release);
                m_renderer->RequestFullSpeedFrame();
                m_lastMaterialRenderTime = now;
                return consumed;
            }
        }
        std::vector<unsigned char> pixels;
        MaterialPreviewer::RenderCpuPreview(material, kMaterialPreviewSize, pixels, assetDatabase);
        if (pixels.empty()) {
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            auto it = m_materialPreviewStates.find(request.resourceKey);
            if (it != m_materialPreviewStates.end())
                it->second.inFlight = false;
            return consumed;
        }
        std::string textureName;
        {
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            auto it = m_materialPreviewStates.find(request.resourceKey);
            if (it == m_materialPreviewStates.end())
                return consumed;
            if (it->second.generation != request.generation) {
                it->second.inFlight = false;
                if (it->second.readyGeneration < it->second.generation) {
                    it->second.inFlight = true;
                    m_previewRequestQueue.push(MaterialPreviewRequest{
                        request.resourceKey,
                        it->second.latestMatFilePath,
                        it->second.generation,
                        it->second.latestMaterialJson,
                    });
                    m_hasPreviewPumpWork.store(true, std::memory_order_release);
                    m_renderer->RequestFullSpeedFrame();
                }
                return consumed;
            }
            if (it->second.textureName.empty())
                it->second.textureName = BuildPreviewTextureName(request.resourceKey);
            textureName = it->second.textureName;
        }
        try {
            const PreviewPixelSummary pixelSummary = SummarizePreviewPixels(pixels);
            const uint64_t uploadVersion =
                m_renderer->SubmitTextureForImGui(textureName, pixels.data(), pixels.size(), kMaterialPreviewSize,
                                                  kMaterialPreviewSize, rhi::FilterMode::Linear);
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            auto it = m_materialPreviewStates.find(request.resourceKey);
            if (it != m_materialPreviewStates.end()) {
                it->second.pendingUploadVersion = uploadVersion;
                it->second.pendingPreviewGeneration = request.generation;
                it->second.pendingSize = kMaterialPreviewSize;
                it->second.pixelGeneration = request.generation;
                it->second.pixelHash = pixelSummary.hash;
                it->second.nonTransparentPixelCount = pixelSummary.nonTransparentPixelCount;
                it->second.minRgb = pixelSummary.minRgb;
                it->second.maxRgb = pixelSummary.maxRgb;
                m_hasPendingPreviewUploads.store(true, std::memory_order_release);
                m_hasPreviewPumpWork.store(true, std::memory_order_release);
            }
            ++consumed;
        } catch (const std::exception &error) {
            INXLOG_ERROR("Failed to submit CPU material preview fallback: ", error.what());
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            auto it = m_materialPreviewStates.find(request.resourceKey);
            if (it != m_materialPreviewStates.end())
                it->second.inFlight = false;
        }
    } else {
        std::lock_guard<std::mutex> lock(m_previewResultMutex);
        auto it = m_materialPreviewStates.find(request.resourceKey);
        if (it != m_materialPreviewStates.end()) {
            it->second.renderTicket = std::move(ticket);
            it->second.renderMaterial = std::move(material);
            it->second.renderGeneration = request.generation;
            m_hasPreviewPumpWork.store(true, std::memory_order_release);
        }
    }
    m_lastMaterialRenderTime = now;

    return consumed;
}

void Infernux::CommitPublishedPreviewTextures()
{
    if (!m_hasPendingPreviewUploads.exchange(false, std::memory_order_acq_rel))
        return;

    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    auto commitMaterial = [this](MaterialPreviewState &state) {
        if (state.pendingUploadVersion != 0 &&
            m_renderer->GetFailedImGuiTextureVersion(state.textureName) >= state.pendingUploadVersion) {
            state.pendingUploadVersion = 0;
            state.pendingPreviewGeneration = 0;
            state.pendingSize = 0;
            state.inFlight = false;
            return false;
        }
        if (state.pendingUploadVersion == 0)
            return false;
        if (m_renderer->GetImGuiTextureVersion(state.textureName) < state.pendingUploadVersion)
            return true;
        state.textureId = m_renderer->GetImGuiTextureId(state.textureName);
        state.readyGeneration = state.pendingPreviewGeneration;
        state.readySize = state.pendingSize;
        state.pendingUploadVersion = 0;
        state.pendingPreviewGeneration = 0;
        state.pendingSize = 0;
        state.inFlight = false;
        return false;
    };
    auto commitTexture = [this](TexturePreviewState &state) {
        if (state.pendingUploadVersion != 0 &&
            m_renderer->GetFailedImGuiTextureVersion(state.textureName) >= state.pendingUploadVersion) {
            state.pendingUploadVersion = 0;
            state.pendingPreviewGeneration = 0;
            state.pendingWidth = 0;
            state.pendingHeight = 0;
            state.inFlight = false;
            return false;
        }
        if (state.pendingUploadVersion == 0)
            return false;
        if (m_renderer->GetImGuiTextureVersion(state.textureName) < state.pendingUploadVersion)
            return true;
        state.textureId = m_renderer->GetImGuiTextureId(state.textureName);
        state.readyGeneration = state.pendingPreviewGeneration;
        state.readyWidth = state.pendingWidth;
        state.readyHeight = state.pendingHeight;
        state.pendingUploadVersion = 0;
        state.pendingPreviewGeneration = 0;
        state.pendingWidth = 0;
        state.pendingHeight = 0;
        state.inFlight = false;
        return false;
    };
    auto commitMesh = [this](MeshPreviewState &state) {
        if (state.pendingUploadVersion != 0 &&
            m_renderer->GetFailedImGuiTextureVersion(state.textureName) >= state.pendingUploadVersion) {
            state.pendingUploadVersion = 0;
            state.pendingPreviewGeneration = 0;
            state.pendingSize = 0;
            state.inFlight = false;
            return false;
        }
        if (state.pendingUploadVersion == 0)
            return false;
        if (m_renderer->GetImGuiTextureVersion(state.textureName) < state.pendingUploadVersion)
            return true;
        state.textureId = m_renderer->GetImGuiTextureId(state.textureName);
        state.readyGeneration = state.pendingPreviewGeneration;
        state.readySize = state.pendingSize;
        state.pendingUploadVersion = 0;
        state.pendingPreviewGeneration = 0;
        state.pendingSize = 0;
        state.inFlight = false;
        return false;
    };
    bool hasUnpublishedUploads = false;
    for (auto &[key, state] : m_materialPreviewStates) {
        const uint64_t publishingGeneration = state.pendingPreviewGeneration;
        const bool stillPublishing = commitMaterial(state);
        hasUnpublishedUploads |= stillPublishing;
        if (!stillPublishing && publishingGeneration != 0 && publishingGeneration != state.generation &&
            !state.inFlight && state.readyGeneration < state.generation) {
            state.inFlight = true;
            m_previewRequestQueue.push(MaterialPreviewRequest{
                key,
                state.latestMatFilePath,
                state.generation,
                state.latestMaterialJson,
            });
            m_hasPreviewPumpWork.store(true, std::memory_order_release);
            m_renderer->RequestFullSpeedFrame();
        }
    }
    for (auto &[key, state] : m_texturePreviewStates) {
        (void)key;
        hasUnpublishedUploads |= commitTexture(state);
    }
    for (auto &[key, state] : m_meshPreviewStates) {
        (void)key;
        hasUnpublishedUploads |= commitMesh(state);
    }
    if (hasUnpublishedUploads)
        m_hasPendingPreviewUploads.store(true, std::memory_order_release);
    if (hasUnpublishedUploads)
        m_hasPreviewPumpWork.store(true, std::memory_order_release);
}

void Infernux::PumpPreviewTasks()
{
    if (!m_renderer)
        return;

    // Inspector and Project can both pump during one ImGui frame. Check the
    // frame before consuming the edge-triggered work flag; otherwise the
    // second caller clears work re-armed by the first caller and a completed
    // GPU readback can remain in-flight forever.
    const int currentFrame = ImGui::GetFrameCount();
    if (m_lastPumpFrame == currentFrame) {
        PumpTimelineCubePreviewIfDirty();
        return;
    }
    m_lastPumpFrame = currentFrame;

    if (!m_hasPreviewPumpWork.exchange(false, std::memory_order_acq_rel)) {
        PumpTimelineCubePreviewIfDirty();
        return;
    }

    CommitPublishedPreviewTextures();

    // Registered project textures never enter the CPU thumbnail pipeline.
    // A pump-disabled query records only its GUID; the first normal preview
    // pump resolves the immutable runtime GPU publication and exposes that
    // same view to ImGui.
    struct ImportedPreviewPublication
    {
        std::string key;
        std::string textureName;
        std::string guid;
    };
    std::vector<ImportedPreviewPublication> importedPublications;
    {
        std::lock_guard<std::mutex> lock(m_previewResultMutex);
        for (const auto &[key, state] : m_texturePreviewStates) {
            if (state.importedGpuPending && !state.importedTextureGuid.empty())
                importedPublications.push_back({key, state.textureName, state.importedTextureGuid});
        }
    }
    for (const auto &publication : importedPublications) {
        const uint64_t textureId = m_renderer->QueryImportedTextureForImGui(publication.textureName, publication.guid);
        std::lock_guard<std::mutex> lock(m_previewResultMutex);
        const auto stateIt = m_texturePreviewStates.find(publication.key);
        if (stateIt == m_texturePreviewStates.end() || stateIt->second.importedTextureGuid != publication.guid)
            continue;
        if (textureId == 0) {
            m_hasPreviewPumpWork.store(true, std::memory_order_release);
            m_renderer->RequestFullSpeedFrame();
            continue;
        }
        stateIt->second.textureId = textureId;
        stateIt->second.importedGpuPending = false;
    }

    constexpr int kMaxUploadsPerFrame = 3;
    int uploadBudget = kMaxUploadsPerFrame;

    uploadBudget -= PumpMaterialPreviewUploads(uploadBudget, false);

    // ── Process completed texture uploads ────────────────────────
    {
        std::queue<TexturePreviewCompleted> texLocal;
        {
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            texLocal.swap(m_texturePreviewCompletedQueue);
        }

        while (!texLocal.empty() && uploadBudget > 0) {
            TexturePreviewCompleted completed = std::move(texLocal.front());
            texLocal.pop();

            TexturePreviewState stateSnapshot;
            {
                std::lock_guard<std::mutex> lock(m_previewResultMutex);
                auto it = m_texturePreviewStates.find(completed.resourceKey);
                if (it == m_texturePreviewStates.end())
                    continue;

                if (it->second.generation != completed.generation) {
                    it->second.inFlight = false;
                    continue;
                }
                if (it->second.textureName.empty())
                    it->second.textureName = BuildTexturePreviewTextureName(completed.resourceKey);
                stateSnapshot = it->second;
            }

            if (!completed.success || completed.pixels.empty() || completed.width <= 0 || completed.height <= 0) {
                std::lock_guard<std::mutex> lock(m_previewResultMutex);
                auto it = m_texturePreviewStates.find(completed.resourceKey);
                if (it != m_texturePreviewStates.end())
                    it->second.inFlight = false;
                continue;
            }

            if (stateSnapshot.textureName.empty())
                continue;

            const PreviewPixelSummary pixelSummary = SummarizePreviewPixels(completed.pixels);
            uint64_t uploadVersion = 0;
            try {
                uploadVersion = m_renderer->SubmitTextureForImGui(
                    stateSnapshot.textureName, completed.pixels.data(), completed.pixels.size(), completed.width,
                    completed.height, completed.nearest ? rhi::FilterMode::Nearest : rhi::FilterMode::Linear);
            } catch (const std::exception &error) {
                INXLOG_ERROR("Failed to submit image preview texture: ", error.what());
                std::lock_guard<std::mutex> lock(m_previewResultMutex);
                auto it = m_texturePreviewStates.find(completed.resourceKey);
                if (it != m_texturePreviewStates.end())
                    it->second.inFlight = false;
                continue;
            }

            {
                std::lock_guard<std::mutex> lock(m_previewResultMutex);
                auto it = m_texturePreviewStates.find(completed.resourceKey);
                if (it == m_texturePreviewStates.end())
                    continue;

                it->second.pendingUploadVersion = uploadVersion;
                it->second.pendingPreviewGeneration = completed.generation;
                it->second.pendingWidth = completed.width;
                it->second.pendingHeight = completed.height;
                it->second.pixelGeneration = completed.generation;
                it->second.pixelHash = pixelSummary.hash;
                it->second.nonTransparentPixelCount = pixelSummary.nonTransparentPixelCount;
                it->second.minRgb = pixelSummary.minRgb;
                it->second.maxRgb = pixelSummary.maxRgb;
                m_hasPendingPreviewUploads.store(true, std::memory_order_release);
                m_hasPreviewPumpWork.store(true, std::memory_order_release);
            }
            --uploadBudget;
        }

        // Put unconsumed items back for next frame.
        if (!texLocal.empty()) {
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            m_hasPreviewPumpWork.store(true, std::memory_order_release);
            while (!texLocal.empty()) {
                m_texturePreviewCompletedQueue.push(std::move(texLocal.front()));
                texLocal.pop();
            }
        }
    }

    // ── Mesh preview render + readback + upload ─────────────────
    {
        constexpr int kMeshPreviewSize = 256;
        struct CompletedMeshRender
        {
            std::string resourceKey;
            uint64_t generation = 0;
            std::shared_ptr<vk::ImageReadbackTicket> ticket;
        };
        std::vector<CompletedMeshRender> completedRenders;
        {
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            for (const auto &[resourceKey, state] : m_meshPreviewStates) {
                if (state.renderTicket && state.renderTicket->IsDone())
                    completedRenders.push_back({resourceKey, state.renderGeneration, state.renderTicket});
            }
        }

        for (const auto &completed : completedRenders) {
            if (uploadBudget <= 0)
                break;
            std::vector<unsigned char> pixels;
            const bool rendered = m_renderer->TryCompleteMeshPreviewGPU(completed.ticket, kMeshPreviewSize, pixels);
            std::string textureName;
            {
                std::lock_guard<std::mutex> lock(m_previewResultMutex);
                auto it = m_meshPreviewStates.find(completed.resourceKey);
                if (it == m_meshPreviewStates.end() || it->second.renderTicket != completed.ticket)
                    continue;
                it->second.renderTicket.reset();
                it->second.renderGeneration = 0;
                if (!rendered || pixels.empty() || it->second.generation != completed.generation) {
                    it->second.inFlight = false;
                    continue;
                }
                if (it->second.textureName.empty())
                    it->second.textureName = BuildMeshPreviewTextureName(completed.resourceKey);
                textureName = it->second.textureName;
            }

            try {
                const PreviewPixelSummary pixelSummary = SummarizePreviewPixels(pixels);
                const uint64_t uploadVersion =
                    m_renderer->SubmitTextureForImGui(textureName, pixels.data(), pixels.size(), kMeshPreviewSize,
                                                      kMeshPreviewSize, rhi::FilterMode::Linear);
                {
                    std::lock_guard<std::mutex> lock(m_previewResultMutex);
                    auto it = m_meshPreviewStates.find(completed.resourceKey);
                    if (it != m_meshPreviewStates.end()) {
                        it->second.pendingUploadVersion = uploadVersion;
                        it->second.pendingPreviewGeneration = completed.generation;
                        it->second.pendingSize = kMeshPreviewSize;
                        it->second.pixelGeneration = completed.generation;
                        it->second.pixelHash = pixelSummary.hash;
                        it->second.nonTransparentPixelCount = pixelSummary.nonTransparentPixelCount;
                        it->second.minRgb = pixelSummary.minRgb;
                        it->second.maxRgb = pixelSummary.maxRgb;
                        m_hasPendingPreviewUploads.store(true, std::memory_order_release);
                        m_hasPreviewPumpWork.store(true, std::memory_order_release);
                    }
                }
                --uploadBudget;
            } catch (const std::exception &error) {
                INXLOG_ERROR("Failed to submit mesh preview texture: ", error.what());
                std::lock_guard<std::mutex> lock(m_previewResultMutex);
                auto it = m_meshPreviewStates.find(completed.resourceKey);
                if (it != m_meshPreviewStates.end())
                    it->second.inFlight = false;
            }
        }

        bool renderBusy = false;
        MeshPreviewRequest request;
        std::shared_ptr<AssetLoadTicket> completedLoad;
        std::string completedLoadKey;
        std::string completedLoadGuid;
        uint64_t completedLoadGeneration = 0;
        {
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            renderBusy = std::any_of(m_meshPreviewStates.begin(), m_meshPreviewStates.end(),
                                     [](const auto &entry) { return static_cast<bool>(entry.second.renderTicket); });
            if (renderBusy) {
                m_hasPreviewPumpWork.store(true, std::memory_order_release);
            } else {
                for (const auto &[key, state] : m_meshPreviewStates) {
                    if (state.loadTicket && state.loadTicket->IsComplete()) {
                        completedLoad = state.loadTicket;
                        completedLoadKey = key;
                        completedLoadGuid = state.loadGuid;
                        completedLoadGeneration = state.loadGeneration;
                        break;
                    }
                }
                if (!completedLoad && !m_meshPreviewRequestQueue.empty()) {
                    request = std::move(m_meshPreviewRequestQueue.front());
                    m_meshPreviewRequestQueue.pop();
                }
            }
        }

        auto markMeshPreviewFailed = [this](const std::string &key) {
            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            auto it = m_meshPreviewStates.find(key);
            if (it != m_meshPreviewStates.end()) {
                it->second.loadTicket.reset();
                it->second.loadGuid.clear();
                it->second.loadGeneration = 0;
                it->second.failedGeneration = it->second.generation;
                it->second.inFlight = false;
            }
        };

        // Complete an Assimp import on the owner thread only after the worker
        // has finished.  The old path used LoadAssetByPath here, which made
        // opening a directory of FBX files block the ImGui/render thread.
        if (!renderBusy && completedLoad) {
            std::shared_ptr<InxMesh> mesh;
            try {
                if (AssetRegistry::Instance().TryCommitAssetLoad(completedLoad, true))
                    mesh = AssetRegistry::Instance().GetAsset<InxMesh>(completedLoadGuid);
            } catch (const std::exception &error) {
                // A second consumer may have loaded/reimported this model while
                // the preview worker was decoding it. In that case the worker
                // ticket is stale by design, but the newer registry object is
                // exactly what the preview should render. Treating this as a
                // permanent preview failure made thumbnails depend on clicking
                // the model in the Inspector first.
                mesh = AssetRegistry::Instance().GetAsset<InxMesh>(completedLoadGuid);
                if (mesh) {
                    INXLOG_DEBUG("Mesh preview adopted newer registry asset '", completedLoadGuid,
                                 "' after its worker ticket became stale");
                } else {
                    INXLOG_ERROR("Mesh preview import failed for '", completedLoadGuid, "': ", error.what());
                }
            }

            {
                std::lock_guard<std::mutex> lock(m_previewResultMutex);
                auto it = m_meshPreviewStates.find(completedLoadKey);
                if (it != m_meshPreviewStates.end() && it->second.loadTicket == completedLoad)
                    it->second.loadTicket.reset();
            }

            if (!mesh) {
                markMeshPreviewFailed(completedLoadKey);
            } else {
                auto *database = AssetRegistry::Instance().GetAssetDatabase();
                request = {completedLoadKey, database ? database->GetPathFromGuid(completedLoadGuid) : std::string(),
                           completedLoadGeneration};
                request.meshFilePath = request.meshFilePath.empty() ? completedLoadGuid : request.meshFilePath;
            }
        }

        if (!renderBusy && !request.resourceKey.empty()) {
            std::shared_ptr<InxMesh> mesh;
            std::vector<std::shared_ptr<InxMaterial>> materials;
            if (IsPrefabPreviewPath(request.meshFilePath)) {
                if (!BuildPrefabPreviewMesh(request.meshFilePath, mesh, materials)) {
                    markMeshPreviewFailed(request.resourceKey);
                }
            } else if (!completedLoad) {
                auto *database = AssetRegistry::Instance().GetAssetDatabase();
                const std::string guid = database ? database->GetGuidFromPath(request.meshFilePath) : std::string();
                if (guid.empty()) {
                    markMeshPreviewFailed(request.resourceKey);
                } else {
                    try {
                        auto loadTicket = AssetRegistry::Instance().BeginLoadAsset(guid, ResourceType::Mesh);
                        if (!loadTicket->IsCommitted()) {
                            std::lock_guard<std::mutex> lock(m_previewResultMutex);
                            auto it = m_meshPreviewStates.find(request.resourceKey);
                            if (it != m_meshPreviewStates.end()) {
                                it->second.loadTicket = std::move(loadTicket);
                                it->second.loadGuid = guid;
                                it->second.loadGeneration = request.generation;
                            }
                            m_hasPreviewPumpWork.store(true, std::memory_order_release);
                            // The worker owns the expensive importer now.  Clear
                            // the local request so the render path below does not
                            // mistake "waiting" for a failed load.
                            request.resourceKey.clear();
                        }
                        mesh = AssetRegistry::Instance().GetAsset<InxMesh>(guid);
                    } catch (const std::exception &error) {
                        INXLOG_ERROR("Mesh preview load rejected for '", request.meshFilePath, "': ", error.what());
                    }
                }
            } else {
                auto *database = AssetRegistry::Instance().GetAssetDatabase();
                const std::string guid = database ? database->GetGuidFromPath(request.meshFilePath) : std::string();
                if (!guid.empty())
                    mesh = AssetRegistry::Instance().GetAsset<InxMesh>(guid);
            }

            if (mesh && !IsPrefabPreviewPath(request.meshFilePath))
                materials = BuildDefaultPreviewMaterialsForMesh(*mesh);

            if (!mesh) {
                markMeshPreviewFailed(request.resourceKey);
            } else {
                auto ticket = m_renderer->BeginMeshPreviewGPU(*mesh, materials, kMeshPreviewSize);
                if (!ticket) {
                    std::lock_guard<std::mutex> lock(m_previewResultMutex);
                    m_meshPreviewRequestQueue.push(std::move(request));
                    m_hasPreviewPumpWork.store(true, std::memory_order_release);
                } else {
                    std::lock_guard<std::mutex> lock(m_previewResultMutex);
                    auto it = m_meshPreviewStates.find(request.resourceKey);
                    if (it != m_meshPreviewStates.end()) {
                        it->second.renderTicket = std::move(ticket);
                        it->second.renderGeneration = request.generation;
                        m_hasPreviewPumpWork.store(true, std::memory_order_release);
                    }
                }
            }
        }
    }

    PumpTimelineCubePreviewIfDirty();
}

void Infernux::PollGpuCompletions()
{
    if (m_renderer)
        m_renderer->PollGpuCompletions();
}

uint64_t Infernux::GetMaterialPreviewTextureId(const std::string &resourceKey) const
{
    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    auto it = m_materialPreviewStates.find(CanonicalizePreviewKey(resourceKey));
    if (it == m_materialPreviewStates.end())
        return 0;
    return LiveImGuiTextureId(m_renderer.get(), it->second.textureName);
}

bool Infernux::IsMaterialPreviewReady(const std::string &resourceKey) const
{
    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    auto it = m_materialPreviewStates.find(CanonicalizePreviewKey(resourceKey));
    if (it == m_materialPreviewStates.end())
        return false;
    const auto &state = it->second;
    return state.generation != 0 && state.readyGeneration == state.generation &&
           LiveImGuiTextureId(m_renderer.get(), state.textureName) != 0;
}

std::vector<Infernux::PreviewTaskSnapshot> Infernux::GetPreviewTaskSnapshots() const
{
    std::unordered_map<uint64_t, uint32_t> imguiTextureUseCounts;
    if (const ImDrawData *drawData = ImGui::GetDrawData(); drawData && drawData->Valid) {
        for (int listIndex = 0; listIndex < drawData->CmdListsCount; ++listIndex) {
            const ImDrawList *drawList = drawData->CmdLists[listIndex];
            if (!drawList)
                continue;
            for (const ImDrawCmd &command : drawList->CmdBuffer) {
                const ImTextureID textureId = command.GetTexID();
                uint64_t value = 0;
                static_assert(sizeof(textureId) <= sizeof(value));
                std::memcpy(&value, &textureId, sizeof(textureId));
                if (value != 0)
                    ++imguiTextureUseCounts[value];
            }
        }
    }

    std::vector<PreviewTaskSnapshot> snapshots;
    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    snapshots.reserve(m_materialPreviewStates.size() + m_texturePreviewStates.size() + m_meshPreviewStates.size());

    auto finish = [this, &imguiTextureUseCounts](PreviewTaskSnapshot &snapshot) {
        snapshot.textureId = LiveImGuiTextureId(m_renderer.get(), snapshot.textureName);
        snapshot.imguiDrawCommandCount = imguiTextureUseCounts[snapshot.textureId];
        if (m_renderer) {
            snapshot.publishedUploadVersion = m_renderer->GetImGuiTextureVersion(snapshot.textureName);
            snapshot.failedUploadVersion = m_renderer->GetFailedImGuiTextureVersion(snapshot.textureName);
        }
    };
    const auto copyPixelSummary = [](PreviewTaskSnapshot &snapshot, const auto &state) {
        snapshot.pixelGeneration = state.pixelGeneration;
        snapshot.pixelHash = state.pixelHash;
        snapshot.nonTransparentPixelCount = state.nonTransparentPixelCount;
        snapshot.minRgb = state.minRgb;
        snapshot.maxRgb = state.maxRgb;
    };

    for (const auto &[key, state] : m_materialPreviewStates) {
        PreviewTaskSnapshot snapshot;
        snapshot.kind = "material";
        snapshot.resourceKey = key;
        snapshot.textureName = state.textureName;
        snapshot.generation = state.generation;
        snapshot.readyGeneration = state.readyGeneration;
        snapshot.pendingUploadVersion = state.pendingUploadVersion;
        snapshot.pendingPreviewGeneration = state.pendingPreviewGeneration;
        snapshot.inFlight = state.inFlight;
        snapshot.authoring = state.authoring;
        snapshot.hasRenderTicket = static_cast<bool>(state.renderTicket);
        snapshot.renderTicketDone = state.renderTicket && state.renderTicket->IsDone();
        snapshot.pendingWidth = state.pendingSize;
        snapshot.pendingHeight = state.pendingSize;
        snapshot.readyWidth = state.readySize;
        snapshot.readyHeight = state.readySize;
        copyPixelSummary(snapshot, state);
        finish(snapshot);
        snapshots.push_back(std::move(snapshot));
    }

    for (const auto &[key, state] : m_texturePreviewStates) {
        PreviewTaskSnapshot snapshot;
        snapshot.kind = "texture";
        snapshot.resourceKey = key;
        snapshot.textureName = state.textureName;
        snapshot.generation = state.generation;
        snapshot.readyGeneration = state.readyGeneration;
        snapshot.pendingUploadVersion = state.pendingUploadVersion;
        snapshot.pendingPreviewGeneration = state.pendingPreviewGeneration;
        snapshot.inFlight = state.inFlight;
        snapshot.authoring = state.authoring;
        snapshot.pendingWidth = state.pendingWidth;
        snapshot.pendingHeight = state.pendingHeight;
        snapshot.readyWidth = state.readyWidth;
        snapshot.readyHeight = state.readyHeight;
        copyPixelSummary(snapshot, state);
        finish(snapshot);
        snapshots.push_back(std::move(snapshot));
    }

    for (const auto &[key, state] : m_meshPreviewStates) {
        PreviewTaskSnapshot snapshot;
        snapshot.kind = "mesh";
        snapshot.resourceKey = key;
        snapshot.textureName = state.textureName;
        snapshot.generation = state.generation;
        snapshot.readyGeneration = state.readyGeneration;
        snapshot.pendingUploadVersion = state.pendingUploadVersion;
        snapshot.pendingPreviewGeneration = state.pendingPreviewGeneration;
        snapshot.inFlight = state.inFlight;
        snapshot.hasRenderTicket = static_cast<bool>(state.renderTicket);
        snapshot.renderTicketDone = state.renderTicket && state.renderTicket->IsDone();
        snapshot.pendingWidth = state.pendingSize;
        snapshot.pendingHeight = state.pendingSize;
        snapshot.readyWidth = state.readySize;
        snapshot.readyHeight = state.readySize;
        copyPixelSummary(snapshot, state);
        finish(snapshot);
        snapshots.push_back(std::move(snapshot));
    }

    return snapshots;
}

uint64_t Infernux::GetTexturePreviewTextureId(const std::string &resourceKey) const
{
    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    auto it = m_texturePreviewStates.find(CanonicalizePreviewKey(resourceKey));
    if (it == m_texturePreviewStates.end())
        return 0;
    return LiveImGuiTextureId(m_renderer.get(), it->second.textureName);
}

uint64_t Infernux::GetMeshPreviewTextureId(const std::string &resourceKey) const
{
    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    auto it = m_meshPreviewStates.find(CanonicalizePreviewKey(resourceKey));
    if (it == m_meshPreviewStates.end())
        return 0;
    return LiveImGuiTextureId(m_renderer.get(), it->second.textureName);
}

std::pair<int, int> Infernux::GetTexturePreviewSize(const std::string &resourceKey) const
{
    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    auto it = m_texturePreviewStates.find(CanonicalizePreviewKey(resourceKey));
    if (it == m_texturePreviewStates.end())
        return {0, 0};
    return {it->second.readyWidth, it->second.readyHeight};
}

void Infernux::InvalidateMaterialPreviewTask(const std::string &resourceKey)
{
    if (resourceKey.empty())
        return;

    // Bump generation so next query re-renders.  Keep old textureId for
    // stale-return anti-flicker.  Reset content hashes so both sources
    // (JSON and mtime) re-evaluate on next call.
    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    auto it = m_materialPreviewStates.find(CanonicalizePreviewKey(resourceKey));
    if (it != m_materialPreviewStates.end()) {
        it->second.generation++;
        it->second.lastJsonHash = 0;
        it->second.lastFileMtime = 0;
        if (it->second.pendingUploadVersion != 0 && m_renderer) {
            m_renderer->SupersedePendingImGuiTextureUploads(it->second.textureName);
            it->second.pendingUploadVersion = 0;
            it->second.pendingPreviewGeneration = 0;
            it->second.pendingSize = 0;
            it->second.inFlight = false;
        }
    }
}

void Infernux::InvalidateTexturePreviewTask(const std::string &resourceKey)
{
    if (resourceKey.empty())
        return;

    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    auto it = m_texturePreviewStates.find(CanonicalizePreviewKey(resourceKey));
    if (it == m_texturePreviewStates.end())
        return;

    // Keep the last imported preview visible while its replacement is prepared.
    it->second.generation++;
    it->second.lastContentStamp = 0;
    it->second.inFlight = false;
}

void Infernux::ReleasePreviewAuthoring(const std::string &resourceKey)
{
    if (resourceKey.empty())
        return;

    const std::string key = CanonicalizePreviewKey(resourceKey);
    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    if (auto material = m_materialPreviewStates.find(key); material != m_materialPreviewStates.end()) {
        material->second.authoring = false;
        material->second.lastFileMtime = 0;
        material->second.lastJsonHash = 0;
    }
    if (auto texture = m_texturePreviewStates.find(key); texture != m_texturePreviewStates.end()) {
        texture->second.authoring = false;
        texture->second.lastContentStamp = 0;
    }
}

std::tuple<uint64_t, int, int> Infernux::QueryOrScheduleTexturePreview(
    const std::string &resourceKey, const std::string &textureFilePath, uint64_t contentStampHint, bool nearest,
    bool srgb, int maxSize, const std::string &textureFormat, const std::string &textureType, bool authoring, bool pump)
{
    if (resourceKey.empty() || textureFilePath.empty())
        return {0, 0, 0};

    if (pump)
        PumpPreviewTasks();

    const std::string key = CanonicalizePreviewKey(resourceKey);

    // Every registered texture preview, including sprite slicing, presents the
    // final imported GPU publication. This keeps BC formats, sRGB conversion,
    // mip policy and runtime sampling identical across Project, Inspector,
    // sprite authoring and scene rendering.
    if (auto *database = GetAssetDatabase(); database && m_renderer) {
        const std::string guid = database->GetGuidFromPath(textureFilePath);
        if (!guid.empty()) {
            int importedWidth = 0;
            int importedHeight = 0;
            if (const auto meta = database->GetMetaByGuid(guid)) {
                if (meta->HasKey("artifact_width"))
                    importedWidth = meta->GetDataAs<int>("artifact_width");
                if (meta->HasKey("artifact_height"))
                    importedHeight = meta->GetDataAs<int>("artifact_height");
            }
            const std::string textureName = BuildTexturePreviewTextureName(key);
            if (!pump) {
                // A non-pumping query is a registration/probe operation. Never
                // allocate an ImGui descriptor before the first normal preview
                // pump; early-failure cleanup may otherwise run before the
                // Vulkan backend has entered its first frame.
                std::lock_guard<std::mutex> lock(m_previewResultMutex);
                auto &state = m_texturePreviewStates[key];
                state.textureName = textureName;
                state.importedTextureGuid = guid;
                state.importedGpuPending = true;
                state.readyWidth = importedWidth;
                state.readyHeight = importedHeight;
                state.authoring = state.authoring || authoring;
                m_hasPreviewPumpWork.store(true, std::memory_order_release);
                m_renderer->RequestFullSpeedFrame();
                return {0, 0, 0};
            }
            const uint64_t importedId = m_renderer->QueryImportedTextureForImGui(textureName, guid);
            if (importedId != 0) {
                std::lock_guard<std::mutex> lock(m_previewResultMutex);
                auto &state = m_texturePreviewStates[key];
                state.textureName = textureName;
                state.importedTextureGuid = guid;
                state.importedGpuPending = false;
                state.textureId = importedId;
                state.readyWidth = importedWidth;
                state.readyHeight = importedHeight;
                state.authoring = state.authoring || authoring;
            }
            return {importedId, importedWidth, importedHeight};
        }
    }

    bool shouldEnqueue = false;
    TexturePreviewRequest req;
    uint64_t texId = 0;
    int w = 0, h = 0;

    {
        std::lock_guard<std::mutex> lock(m_previewResultMutex);
        auto &state = m_texturePreviewStates[key];
        if (state.textureName.empty())
            state.textureName = BuildTexturePreviewTextureName(key);

        state.textureId = LiveImGuiTextureId(m_renderer.get(), state.textureName);
        if (!authoring && state.authoring)
            return {state.textureId, state.readyWidth, state.readyHeight};
        if (authoring)
            state.authoring = true;

        // ── Detect content changes ──────────────────────────────
        if (contentStampHint != 0 && contentStampHint != state.lastContentStamp) {
            state.lastContentStamp = contentStampHint;
            state.generation++;
        }

        const int sanitizedMaxSize = std::clamp(maxSize, 1, 65'536);
        // Also bump generation if a caller omitted these settings from its content stamp.
        if (state.generation != 0 &&
            (nearest != state.nearest || srgb != state.srgb || sanitizedMaxSize != state.maxSize ||
             textureFormat != state.textureFormat || textureType != state.textureType)) {
            state.generation++;
        }
        state.nearest = nearest;
        state.srgb = srgb;
        state.maxSize = sanitizedMaxSize;
        state.textureFormat = textureFormat;
        state.textureType = textureType;

        texId = state.textureId;
        w = state.readyWidth;
        h = state.readyHeight;

        // Already up-to-date?
        if (state.readyGeneration == state.generation && state.textureId != 0)
            return {texId, w, h};
        if (state.readyGeneration == state.generation && state.pendingUploadVersion == 0)
            state.readyGeneration = 0;

        // Schedule render if not already in flight.
        if (!state.inFlight && state.readyGeneration < state.generation) {
            state.inFlight = true;
            req = TexturePreviewRequest{key,  textureFilePath,  state.generation, nearest,
                                        srgb, sanitizedMaxSize, textureFormat,    textureType};
            shouldEnqueue = true;
        }
    }

    if (shouldEnqueue) {
        constexpr int kDefaultPreviewResolution = 200;
        // Inspector component icons (~16 logical px, often 2x framebuffer); keep GPU texture near
        // display density instead of a 256px atlas + heavy minification.
        constexpr int kComponentIconPreviewMaxDim = 64;
        EnqueuePreviewTask([this, req, kDefaultPreviewResolution, kComponentIconPreviewMaxDim]() {
            TexturePreviewCompleted completed;
            completed.resourceKey = req.resourceKey;
            completed.generation = req.generation;
            completed.nearest = req.nearest;
            try {
                auto texData = InxTextureLoader::LoadFromFile(req.textureFilePath);
                if (texData.IsValid()) {
                    std::vector<unsigned char> sampled;
                    int outW = 0;
                    int outH = 0;
                    const bool spriteEditPreview =
                        !req.resourceKey.empty() && req.resourceKey.compare(0, 11, "spriteedit|") == 0;
                    const int configuredMaxDim = std::clamp(req.maxSize, 1, kDefaultPreviewResolution);
                    const int maxDim =
                        spriteEditPreview
                            ? std::max(texData.width, texData.height)
                            : ((!req.resourceKey.empty() && req.resourceKey.compare(0, 9, "compicon|") == 0)
                                   ? (std::min)(kComponentIconPreviewMaxDim, configuredMaxDim)
                                   : configuredMaxDim);
                    DownsampleNearestRgba(texData.pixels, texData.width, texData.height, maxDim, sampled, outW, outH);
                    if (!sampled.empty() && outW > 0 && outH > 0) {
                        if (req.textureType == "normal_map")
                            ApplyNormalMapPreviewInPlace(sampled, outW, outH);
                        else if (!req.srgb)
                            ApplyLinearToSrgbPreviewInPlace(sampled);
                        ApplyTextureFormatPreviewInPlace(sampled, req.textureFormat);
                        completed.width = outW;
                        completed.height = outH;
                        completed.success = true;
                        completed.pixels = std::move(sampled);
                    }
                }
            } catch (const std::exception &error) {
                INXLOG_WARN("Texture preview decode failed for ", req.textureFilePath, ": ", error.what());
            }

            std::lock_guard<std::mutex> lock(m_previewResultMutex);
            m_texturePreviewCompletedQueue.push(std::move(completed));
            m_hasPreviewPumpWork.store(true, std::memory_order_release);
        });
    }

    // Stale-return: keep showing old preview while new one loads (no flicker).
    return {texId, w, h};
}

bool Infernux::ScheduleTexturePreviewFromMemory(const std::string &resourceKey, std::vector<unsigned char> imageData,
                                                uint64_t stamp, bool nearest)
{
    if (resourceKey.empty() || imageData.empty())
        return false;

    uint64_t gen = 0;
    {
        std::lock_guard<std::mutex> lock(m_previewResultMutex);
        auto &state = m_texturePreviewStates[resourceKey];
        if (state.textureName.empty())
            state.textureName = BuildTexturePreviewTextureName(resourceKey);

        // Use caller's stamp as content-change hint.
        if (stamp != 0 && stamp != state.lastContentStamp) {
            state.lastContentStamp = stamp;
            state.generation++;
        }

        state.textureId = LiveImGuiTextureId(m_renderer.get(), state.textureName);
        if (state.readyGeneration == state.generation && state.textureId != 0)
            return true;
        if (state.readyGeneration == state.generation && state.pendingUploadVersion == 0)
            state.readyGeneration = 0;

        if (state.inFlight)
            return true; // already in-flight

        state.inFlight = true;
        state.nearest = nearest;
        gen = state.generation;
    }

    auto dataCopy = std::make_shared<std::vector<unsigned char>>(std::move(imageData));
    const std::string keyCopy = resourceKey;
    const uint64_t genCopy = gen;
    const bool nearestCopy = nearest;

    EnqueuePreviewTask([this, keyCopy, dataCopy, genCopy, nearestCopy]() {
        TexturePreviewCompleted completed;
        completed.resourceKey = keyCopy;
        completed.generation = genCopy;
        completed.nearest = nearestCopy;

        try {
            auto texData = InxTextureLoader::LoadFromMemory(dataCopy->data(), dataCopy->size());
            if (texData.IsValid()) {
                completed.width = texData.width;
                completed.height = texData.height;
                completed.success = true;
                completed.pixels = std::move(texData.pixels);
            }
        } catch (const std::exception &error) {
            INXLOG_WARN("In-memory texture preview decode failed: ", error.what());
        }

        std::lock_guard<std::mutex> lock(m_previewResultMutex);
        m_texturePreviewCompletedQueue.push(std::move(completed));
        m_hasPreviewPumpWork.store(true, std::memory_order_release);
    });

    return true;
}

uint64_t Infernux::QueryOrScheduleMeshPreview(const std::string &resourceKey, const std::string &meshFilePath,
                                              uint64_t fileMtimeHint)
{
    if (resourceKey.empty() || meshFilePath.empty())
        return 0;

    const std::string key = CanonicalizePreviewKey(resourceKey);
    std::lock_guard<std::mutex> lock(m_previewResultMutex);
    auto &state = m_meshPreviewStates[key];
    if (state.textureName.empty())
        state.textureName = BuildMeshPreviewTextureName(key);
    state.meshFilePath = meshFilePath;

    // ── Detect content changes ──────────────────────────────────
    if (fileMtimeHint != 0 && fileMtimeHint != state.lastFileMtime) {
        state.lastFileMtime = fileMtimeHint;
        state.generation++;
    }

    // First request: generation was never bumped, force it so we render once.
    if (state.generation == 0)
        state.generation = 1;

    // ── Already up-to-date? ─────────────────────────────────────
    state.textureId = LiveImGuiTextureId(m_renderer.get(), state.textureName);
    if (state.readyGeneration == state.generation && state.textureId != 0)
        return state.textureId;
    if (state.failedGeneration == state.generation)
        return state.textureId;
    if (state.readyGeneration == state.generation && state.pendingUploadVersion == 0)
        state.readyGeneration = 0;

    // ── Schedule render if not already in flight ────────────────
    if (!state.inFlight && state.readyGeneration < state.generation) {
        state.inFlight = true;
        m_meshPreviewRequestQueue.push(MeshPreviewRequest{key, meshFilePath, state.generation});
        m_hasPreviewPumpWork.store(true, std::memory_order_release);
        if (m_renderer)
            m_renderer->RequestFullSpeedFrame();
    }

    // Stale-return: keep showing old preview while new one renders (no flicker).
    return state.textureId;
}

void Infernux::PumpTimelineCubePreviewIfDirty()
{
    if (!m_renderer || !m_timelineCubeDirty)
        return;
    if (m_pendingCubePreviewHash == m_lastCubePreviewHash) {
        m_timelineCubeDirty = false;
        return;
    }
    // Keep the latest request dirty while the GPU preview submission is still
    // in flight. The pending fields are overwritten by newer UI samples, so
    // completion naturally renders the newest state without building a queue.
    m_timelineCubeDirty = !ExecuteTimelineCubePreviewRender(
        m_pendingCubePx, m_pendingCubePy, m_pendingCubePz, m_pendingCubeRx, m_pendingCubeRy, m_pendingCubeRz,
        m_pendingCubeSx, m_pendingCubeSy, m_pendingCubeSz, m_pendingCubeCamYaw, m_pendingCubeCamPitch,
        m_pendingCubeCamDist, m_pendingCubeSize, m_pendingCubePreviewHash);
}

bool Infernux::ExecuteTimelineCubePreviewRender(float px, float py, float pz, float rx, float ry, float rz, float sx,
                                                float sy, float sz, float camYaw, float camPitch, float camDistance,
                                                int size, uint64_t hash)
{
    if (!m_renderer || size <= 0)
        return false;

    // ── Build the transformed cube + a fullscreen grid quad as one mesh ──
    auto makeVert = [](const glm::vec3 &p, const glm::vec3 &n) {
        Vertex v{};
        v.pos = p;
        v.normal = n;
        v.tangent = glm::vec4(1.0f, 0.0f, 0.0f, 1.0f);
        v.color = glm::vec3(1.0f);
        v.texCoord = glm::vec2(0.0f);
        return v;
    };

    // Persistent dedicated preview materials (built once → cached pipeline reused).
    auto &registry = AssetRegistry::Instance();
    if (!m_cubePreviewCubeMat) {
        auto defaultLit = registry.GetBuiltinMaterial("DefaultLit");
        if (!defaultLit)
            return false;
        m_cubePreviewCubeMat = defaultLit->Clone();
        if (!m_cubePreviewCubeMat)
            return false;
        m_cubePreviewCubeMat->SetColor("baseColor", glm::vec4(0.62f, 0.63f, 0.66f, 1.0f));
        m_cubePreviewCubeMat->SetFloat("metallic", 0.0f);
        m_cubePreviewCubeMat->SetFloat("smoothness", 0.35f);
    }
    if (!m_cubePreviewFloorMat) {
        auto floorSrc = registry.GetBuiltinMaterial("DefaultLit");
        if (floorSrc) {
            m_cubePreviewFloorMat = floorSrc->Clone();
            if (m_cubePreviewFloorMat)
                m_cubePreviewFloorMat->SetColor("baseColor", glm::vec4(0.20f, 0.21f, 0.23f, 1.0f));
        }
    }
    auto cubeMat = m_cubePreviewCubeMat;
    auto floorMat = m_cubePreviewFloorMat;

    std::vector<Vertex> verts;
    std::vector<uint32_t> indices;
    std::vector<SubMesh> subs;
    std::vector<std::shared_ptr<InxMaterial>> materials;

    // Cube (slot 0, drawn first / opaque): unit cube with the transform baked in.
    {
        const glm::quat q = glm::quat(glm::vec3(glm::radians(rx), glm::radians(ry), glm::radians(rz)));
        const glm::mat3 R = glm::mat3_cast(q);
        const glm::vec3 scl(sx, sy, sz);
        const glm::vec3 off(px, 0.5f * sy + py, pz);

        struct Face
        {
            glm::vec3 n;
            glm::vec3 p[4];
        };
        const Face faces[6] = {
            {{0, 0, 1}, {{-0.5f, -0.5f, 0.5f}, {0.5f, -0.5f, 0.5f}, {0.5f, 0.5f, 0.5f}, {-0.5f, 0.5f, 0.5f}}},
            {{0, 0, -1}, {{0.5f, -0.5f, -0.5f}, {-0.5f, -0.5f, -0.5f}, {-0.5f, 0.5f, -0.5f}, {0.5f, 0.5f, -0.5f}}},
            {{1, 0, 0}, {{0.5f, -0.5f, 0.5f}, {0.5f, -0.5f, -0.5f}, {0.5f, 0.5f, -0.5f}, {0.5f, 0.5f, 0.5f}}},
            {{-1, 0, 0}, {{-0.5f, -0.5f, -0.5f}, {-0.5f, -0.5f, 0.5f}, {-0.5f, 0.5f, 0.5f}, {-0.5f, 0.5f, -0.5f}}},
            {{0, 1, 0}, {{-0.5f, 0.5f, 0.5f}, {0.5f, 0.5f, 0.5f}, {0.5f, 0.5f, -0.5f}, {-0.5f, 0.5f, -0.5f}}},
            {{0, -1, 0}, {{-0.5f, -0.5f, -0.5f}, {0.5f, -0.5f, -0.5f}, {0.5f, -0.5f, 0.5f}, {-0.5f, -0.5f, 0.5f}}},
        };
        const uint32_t cubeIdxStart = static_cast<uint32_t>(indices.size());
        for (const auto &f : faces) {
            const glm::vec3 nW = glm::normalize(R * f.n);
            const uint32_t base = static_cast<uint32_t>(verts.size());
            for (int k = 0; k < 4; ++k)
                verts.push_back(makeVert(R * (f.p[k] * scl) + off, nW));
            const uint32_t fi[6] = {base, base + 1, base + 2, base, base + 2, base + 3};
            for (uint32_t i : fi)
                indices.push_back(i);
        }
        SubMesh sm;
        sm.indexStart = cubeIdxStart;
        sm.indexCount = static_cast<uint32_t>(indices.size()) - cubeIdxStart;
        sm.vertexStart = 0;
        sm.vertexCount = static_cast<uint32_t>(verts.size());
        sm.materialSlot = 0;
        subs.push_back(sm);
        materials.push_back(cubeMat);
    }

    // Simple floor plane (DefaultLit) — avoids the fullscreen GridMaterial pass
    // which is far too heavy for a per-frame interactive preview.
    if (floorMat) {
        if (!m_cubePreviewFloorBuilt) {
            const glm::vec3 n(0.0f, 1.0f, 0.0f);
            const float half = 12.0f;
            m_cubePreviewFloorVerts.clear();
            m_cubePreviewFloorIndices.clear();
            m_cubePreviewFloorVerts.push_back(makeVert({-half, 0.0f, -half}, n));
            m_cubePreviewFloorVerts.push_back(makeVert({half, 0.0f, -half}, n));
            m_cubePreviewFloorVerts.push_back(makeVert({half, 0.0f, half}, n));
            m_cubePreviewFloorVerts.push_back(makeVert({-half, 0.0f, half}, n));
            m_cubePreviewFloorIndices = {0, 1, 2, 0, 2, 3};
            m_cubePreviewFloorBuilt = true;
        }
        const uint32_t fBase = static_cast<uint32_t>(verts.size());
        const uint32_t fIdxStart = static_cast<uint32_t>(indices.size());
        verts.insert(verts.end(), m_cubePreviewFloorVerts.begin(), m_cubePreviewFloorVerts.end());
        for (uint32_t fi : m_cubePreviewFloorIndices)
            indices.push_back(fi + fBase);
        SubMesh sm;
        sm.indexStart = fIdxStart;
        sm.indexCount = static_cast<uint32_t>(m_cubePreviewFloorIndices.size());
        sm.vertexStart = fBase;
        sm.vertexCount = static_cast<uint32_t>(m_cubePreviewFloorVerts.size());
        sm.materialSlot = 1;
        subs.push_back(sm);
        materials.push_back(floorMat);
    }

    InxMesh mesh("__timeline_cube__");
    mesh.SetData(std::move(verts), std::move(indices), std::move(subs));

    const glm::vec3 target(0.0f, 0.35f, 0.0f);
    const float dist = std::max(1.5f, camDistance);
    const float cp = glm::cos(camPitch);
    const glm::vec3 dir(cp * glm::sin(camYaw), glm::sin(camPitch), cp * glm::cos(camYaw));
    const glm::vec3 camPos = target + dir * dist;
    glm::mat4 view = glm::lookAt(camPos, target, glm::vec3(0.0f, 1.0f, 0.0f));
    glm::mat4 proj = glm::perspective(glm::radians(35.0f), 1.0f, 0.05f, 100.0f);
    proj[1][1] *= -1.0f;

    std::vector<unsigned char> pixels;
    const uint64_t texId =
        m_renderer->RenderMeshPreviewGPUImGuiCamera(mesh, materials, size, view, proj, camPos, false);
    if (texId != 0) {
        m_cubePreviewTexId = texId;
        m_lastCubePreviewHash = hash;
    }
    return texId != 0;
}

uint64_t Infernux::RenderTimelineCubePreview(float px, float py, float pz, float rx, float ry, float rz, float sx,
                                             float sy, float sz, float camYaw, float camPitch, float camDistance,
                                             int size)
{
    if (!m_renderer || size <= 0)
        return m_cubePreviewTexId;

    auto qi = [](float f) -> long long { return static_cast<long long>(f * 1000.0f + (f >= 0.0f ? 0.5f : -0.5f)); };
    uint64_t hash = 1469598103934665603ull;
    auto mix = [&hash](long long v) { hash = (hash ^ static_cast<uint64_t>(v)) * 1099511628211ull; };
    mix(qi(px));
    mix(qi(py));
    mix(qi(pz));
    mix(qi(rx));
    mix(qi(ry));
    mix(qi(rz));
    mix(qi(sx));
    mix(qi(sy));
    mix(qi(sz));
    mix(qi(camYaw));
    mix(qi(camPitch));
    mix(qi(camDistance));
    mix(size);

    // The shared GPUMeshPreview recreates its display target on size/format
    // changes (e.g. another preview consumer requested a different size), which
    // invalidates every previously returned descriptor id. Never hand a stale
    // id back to the UI — it would be bound as a freed VkDescriptorSet.
    if (m_cubePreviewTexId != 0 && m_cubePreviewTexId != m_renderer->GetMeshPreviewDisplayTextureId()) {
        m_cubePreviewTexId = 0;
        m_lastCubePreviewHash = 0;
    }

    if (m_cubePreviewTexId != 0 && hash == m_lastCubePreviewHash)
        return m_cubePreviewTexId;

    m_pendingCubePx = px;
    m_pendingCubePy = py;
    m_pendingCubePz = pz;
    m_pendingCubeRx = rx;
    m_pendingCubeRy = ry;
    m_pendingCubeRz = rz;
    m_pendingCubeSx = sx;
    m_pendingCubeSy = sy;
    m_pendingCubeSz = sz;
    m_pendingCubeCamYaw = camYaw;
    m_pendingCubeCamPitch = camPitch;
    m_pendingCubeCamDist = camDistance;
    m_pendingCubeSize = size;
    m_pendingCubePreviewHash = hash;
    m_timelineCubeDirty = true;

    return m_cubePreviewTexId;
}

// ----------------------------------
// Renderer initialization
// ----------------------------------

void Infernux::InitRenderer(int width, int height, const std::string &projectPath,
                            const std::string &builtinResourcePath)
{
    if (m_runtimeMode != RuntimeMode::Graphical) {
        throw std::logic_error("init_renderer is unavailable in headless mode");
    }
    if (!CheckEngineValid("initialize renderer") || !m_renderer) {
        throw std::logic_error("Renderer is not available");
    }
    if (m_isInitialized) {
        throw std::logic_error("Engine is already initialized");
    }

    using StartupClock = std::chrono::steady_clock;
    m_startupPhaseTimingsMs.clear();
    const auto startupBegin = StartupClock::now();
    auto startupPhase = [this](const char *name, const StartupClock::time_point begin) {
        m_startupPhaseTimingsMs[name] = std::chrono::duration<double, std::milli>(StartupClock::now() - begin).count();
    };

    auto phaseBegin = StartupClock::now();
    m_renderer->Init(width, height, m_metadata);
    startupPhase("renderer_init", phaseBegin);
    if (!m_renderer->PumpStartupEvents())
        throw std::runtime_error("Startup cancelled");

    // Wire SceneManager to renderer so Play()/Stop() directly bypass idle
    // sleep without relying on the Python callback chain timing.
    {
        auto *renderer = m_renderer.get();
        SceneManager::Instance().SetPlayStateChangedCallback([renderer](bool playing) {
            if (renderer)
                renderer->SetPlayModeRendering(playing);
        });
    }
    // Debug / RelWithDebInfo: truncate on startup and write through.
    // Release: retain only the last 100 lines and dump them on exit.
#if INFERNUX_FILE_LOGGING
    {
        auto logsDir = ToFsPath(JoinPath({projectPath, "Logs"}));
        std::filesystem::create_directories(logsDir);
        auto logFile = logsDir / "engine.log";
#if INFERNUX_DEFERRED_FILE_LOGGING
        INXLOG_SET_DEFERRED_FILE(FromFsPath(logFile), 100);
#else
        INXLOG_SET_FILE(FromFsPath(logFile));
#endif
    }
#endif

    INXLOG_DEBUG("Load shaders.");
    std::string defaultShaderPath = JoinPath({builtinResourcePath, "shaders"});
    std::string assetsPath = JoinPath({projectPath, "Assets"});
    if (m_assetDatabase) {
        // Register the built-in shader search path for ShaderInfo imports.
        InxShaderLoader::AddShaderSearchPath(defaultShaderPath);

        phaseBegin = StartupClock::now();
        m_assetDatabase->Initialize(projectPath);

        // ── Transfer AssetDatabase ownership to AssetRegistry ──────
        auto &registry = AssetRegistry::Instance();
        registry.Initialize(std::move(m_assetDatabase));

        // Register loader plug-ins for all asset types
        registry.RegisterLoader(ResourceType::Material, std::make_unique<MaterialLoader>());
        registry.RegisterLoader(ResourceType::PhysicMaterial, std::make_unique<PhysicMaterialLoader>());
        registry.RegisterLoader(ResourceType::Texture, std::make_unique<TextureLoader>());
        registry.RegisterLoader(ResourceType::Mesh, std::make_unique<MeshLoader>());
        registry.RegisterLoader(ResourceType::Audio, std::make_unique<AudioClipLoader>());
        registry.RegisterLoader(ResourceType::Shader, std::make_unique<ShaderLoader>());
        registry.RegisterLoader(ResourceType::Script, std::make_unique<InxPythonScriptLoader>());
        registry.RegisterLoader(ResourceType::DefaultText, std::make_unique<InxDefaultTextLoader>());
        registry.RegisterLoader(ResourceType::RenderEffect,
                                std::make_unique<InxDefaultTextLoader>(ResourceType::RenderEffect));
        registry.RegisterLoader(ResourceType::ParticleGraph,
                                std::make_unique<InxDefaultTextLoader>(ResourceType::ParticleGraph));
        registry.RegisterLoader(ResourceType::DefaultBinary, std::make_unique<InxDefaultBinaryLoader>());

        // Populate AssetDatabase's meta-loader table from registered loaders
        registry.PopulateAssetDatabaseLoaders();

        // Register the builtin resource directory as an extra scan root
        // so that Library/Resources assets (materials, etc.) get GUIDs.
        if (!builtinResourcePath.empty())
            registry.GetAssetDatabase()->AddReadOnlyScanRoot(builtinResourcePath);
        // Package Runtime/Editor sources are first-class project assets. Their
        // paths may move, while serialized references and Player staging are
        // always resolved through the GUID catalog.
        registry.GetAssetDatabase()->AddScanRoot(JoinPath({projectPath, "Packages"}));

        const char *playerModeFlag = std::getenv("_INFERNUX_PLAYER_MODE");
        const bool playerMode = playerModeFlag != nullptr && playerModeFlag[0] == '1' && playerModeFlag[1] == '\0';
        const std::string runtimeAssetCatalog = JoinPath({projectPath, "Library", "RuntimeAssetRecords.json"});
        std::error_code runtimeCatalogError;
        const bool hasRuntimeCatalog =
            std::filesystem::is_regular_file(ToFsPath(runtimeAssetCatalog), runtimeCatalogError) &&
            !runtimeCatalogError;
        if (playerMode && hasRuntimeCatalog) {
            // A cooked Player already has an immutable catalog. Walking Assets
            // and the engine resource tree here re-imports every builtin
            // texture/icon on launch and is the dominant enter-game stall.
            // Builtin shaders still import on demand in LoadAndRegisterShaders.
            registry.GetAssetDatabase()->InstallRuntimeAssetCatalog(runtimeAssetCatalog, true);
        } else {
            const bool restoredCachedCatalog = registry.GetAssetDatabase()->RestoreCachedCatalog();
            if (restoredCachedCatalog) {
                // The cached catalog is sufficient for scene/resource lookup.
                // Validate source fingerprints and import changed files on the
                // JobSystem after the editor becomes interactive.
                registry.GetAssetDatabase()->BeginRefresh();
            } else {
                registry.GetAssetDatabase()->Refresh();
            }
            if (hasRuntimeCatalog)
                registry.GetAssetDatabase()->InstallRuntimeAssetCatalog(runtimeAssetCatalog);
        }
        startupPhase("asset_catalog", phaseBegin);
        if (!m_renderer->PumpStartupEvents())
            throw std::runtime_error("Startup cancelled");

        RegisterPhysicMaterialAssetCallback();

        // ── Load and register shaders via AssetRegistry ─────────────
        phaseBegin = StartupClock::now();
        LoadAndRegisterShaders(defaultShaderPath, false);
        if (!m_renderer->PumpStartupEvents())
            throw std::runtime_error("Startup cancelled");
        LoadAndRegisterShaders(assetsPath, true);

        // The material core immediately reflects DefaultLit during
        // PreparePipeline(). The tiny "default" bootstrap pair is only a
        // presentation fallback and cannot provide Standard|Lit's material
        // ABI, so publish the canonical pair before material initialization in
        // both Editor and Player.
        static constexpr std::array<std::pair<std::string_view, std::string_view>, 2> defaultMaterialShaderStages{{
            {"Standard", "vertex"},
            {"Lit", "fragment"},
        }};
        for (const auto &[shaderId, shaderType] : defaultMaterialShaderStages) {
            if (!EnsureShaderLoaded(std::string(shaderId), std::string(shaderType))) {
                INXLOG_ERROR("Default material shader is unavailable: '", shaderId, "' (", shaderType, ")");
            }
        }

        // The procedural sky is renderer-owned: no authored material GUID
        // creates a dependency edge that can prewarm it for a Player. Its two
        // Standalone main() stages are compiled independently, so publish both
        // before the material system creates the builtin sky material.
        static constexpr std::array<std::pair<std::string_view, std::string_view>, 2> proceduralSkyShaderStages{{
            {"Skybox Procedural", "vertex"},
            {"Skybox Procedural", "fragment"},
        }};
        for (const auto &[shaderId, shaderType] : proceduralSkyShaderStages) {
            if (!EnsureShaderLoaded(std::string(shaderId), std::string(shaderType))) {
                INXLOG_ERROR("Required procedural sky shader is unavailable: '", shaderId, "' (", shaderType, ")");
            }
        }
        if (!playerMode) {
            // Editor overlays are present from the first Scene frame. Keep the
            // rest of the shader catalog lazy, but publish this small mandatory
            // set before their built-in materials can create fallback pipelines.
            // A late publication cannot repair a cached fallback descriptor
            // generation without an explicit material invalidation.
            static constexpr std::array<std::pair<std::string_view, std::string_view>, 10> editorOverlayShaderStages{{
                {"Gizmo", "vertex"},
                {"Gizmo", "fragment"},
                {"Grid", "vertex"},
                {"Grid", "fragment"},
                {"Gizmo Icon", "vertex"},
                {"Gizmo Icon", "fragment"},
                {"Outline Mask", "vertex"},
                {"Outline Mask", "fragment"},
                {"Outline Composite", "vertex"},
                {"Outline Composite", "fragment"},
            }};
            for (const auto &[shaderId, shaderType] : editorOverlayShaderStages) {
                if (!EnsureShaderLoaded(std::string(shaderId), std::string(shaderType))) {
                    INXLOG_ERROR("Editor overlay shader is unavailable: '", shaderId, "' (", shaderType, ")");
                }
            }
        }
        startupPhase("startup_shaders", phaseBegin);
        if (!m_renderer->PumpStartupEvents())
            throw std::runtime_error("Startup cancelled");

        // ── Register unified asset event callbacks ──────────────────
        auto &graph = AssetDependencyGraph::Instance();

        auto resolveMaterial = [](const std::string &matGuid) -> std::shared_ptr<InxMaterial> {
            auto mat = AssetRegistry::Instance().GetAsset<InxMaterial>(matGuid);
            if (mat)
                return mat;
            auto *adb = AssetRegistry::Instance().GetAssetDatabase();
            if (adb) {
                std::string matPath = adb->GetPathFromGuid(matGuid);
                if (!matPath.empty() && adb->GetResourceTypeForPath(matPath) == ResourceType::Material)
                    mat = AssetRegistry::Instance().LoadAssetByPath<InxMaterial>(matPath, ResourceType::Material);
            }
            return mat;
        };

        graph.RegisterCallback(
            ResourceType::Texture,
            [this, resolveMaterial](const std::string &dependentGuid, const std::string &texGuid, AssetEvent event) {
                auto mat = resolveMaterial(dependentGuid);
                if (!mat)
                    return;

                if (event == AssetEvent::Deleted) {
                    for (const auto &[propName, prop] : mat->GetAllProperties()) {
                        if (prop.type != MaterialPropertyType::Texture2D)
                            continue;
                        const auto *val = std::get_if<std::string>(&prop.value);
                        if (!val || *val != texGuid)
                            continue;
                        INXLOG_INFO("AssetGraph: texture '", propName, "' is missing for material '", mat->GetName(),
                                    "'; preserving its GUID");
                    }
                }

                if (event == AssetEvent::Deleted || event == AssetEvent::Modified) {
                    mat->MarkPropertiesDirty();
                    if (auto *database = AssetRegistry::Instance().GetAssetDatabase()) {
                        const std::string materialPath = database->GetPathFromGuid(dependentGuid);
                        if (!materialPath.empty())
                            InvalidateMaterialPreviewTask(std::string("mat|") + materialPath);
                    }
                    INXLOG_INFO("AssetGraph: queued descriptor refresh for material '", mat->GetMaterialKey(),
                                "' (texture changed)");
                }
            });

        graph.RegisterCallback(
            ResourceType::Material, [](const std::string &dependentGuid, const std::string &matGuid, AssetEvent event) {
                if (event != AssetEvent::Deleted && event != AssetEvent::Modified)
                    return;
                uint64_t compId = 0;
                try {
                    compId = std::stoull(dependentGuid);
                } catch (...) {
                    return;
                }
                auto *comp = Component::FindByComponentId(compId);
                if (!comp)
                    return;
                auto *mr = dynamic_cast<MeshRenderer *>(comp);
                if (!mr)
                    return;
                mr->OnMaterialAssetEvent(matGuid, event);
                INXLOG_INFO("AssetGraph: refreshed MeshRenderer material reference without changing GUID");
            });

        graph.RegisterCallback(ResourceType::Mesh, [](const std::string &dependentGuid,
                                                      const std::string & /*meshGuid*/, AssetEvent event) {
            uint64_t compId = 0;
            try {
                compId = std::stoull(dependentGuid);
            } catch (...) {
                return;
            }
            auto *comp = Component::FindByComponentId(compId);
            if (!comp)
                return;
            auto *mr = dynamic_cast<MeshRenderer *>(comp);
            if (!mr)
                return;
            mr->OnMeshAssetEvent(event);
            INXLOG_INFO("AssetGraph: refreshed MeshRenderer mesh state");
        });

        graph.RegisterCallback(
            ResourceType::Audio, [](const std::string &dependentGuid, const std::string &audioGuid, AssetEvent event) {
                if (event != AssetEvent::Deleted && event != AssetEvent::Modified)
                    return;
                uint64_t componentId = 0;
                const char *begin = dependentGuid.data();
                const char *end = begin + dependentGuid.size();
                const auto [parsedEnd, error] = std::from_chars(begin, end, componentId);
                if (error != std::errc{} || parsedEnd != end)
                    return;
                auto *source = dynamic_cast<AudioSource *>(Component::FindByComponentId(componentId));
                if (!source)
                    return;
                source->OnAudioClipAssetEvent(audioGuid, event);
                INXLOG_INFO("AssetGraph: refreshed AudioSource clip reference without changing GUID");
            });

        graph.RegisterCallback(ResourceType::Shader, [this, resolveMaterial](const std::string &dependentGuid,
                                                                             const std::string & /*shaderGuid*/,
                                                                             AssetEvent event) {
            if (event != AssetEvent::Modified && event != AssetEvent::Deleted)
                return;
            auto mat = resolveMaterial(dependentGuid);
            if (!mat)
                return;
            mat->MarkPipelineDirty();
            INXLOG_INFO("AssetGraph: marked material '", mat->GetName(), "' pipeline dirty (shader changed)");
        });
    }

    INXLOG_DEBUG("Prepare pipeline.");
    phaseBegin = StartupClock::now();
    m_renderer->PreparePipeline();
    startupPhase("prepare_pipeline", phaseBegin);
    if (!m_renderer->PumpStartupEvents())
        throw std::runtime_error("Startup cancelled");

    // Set ImGui ini file path to user's Documents folder for per-project
    // layout persistence (keeps project directory clean / not in VCS).
    // We use std::filesystem::path throughout (wide-char on Windows) so
    // paths with non-ASCII characters (e.g. Chinese usernames) work.
    phaseBegin = StartupClock::now();
    {
        std::filesystem::path layoutDir;
#ifdef INX_PLATFORM_WINDOWS
        wchar_t docsPath[MAX_PATH] = {};
        if (SHGetFolderPathW(nullptr, CSIDL_PERSONAL, nullptr, SHGFP_TYPE_CURRENT, docsPath) == S_OK) {
            std::filesystem::path projFs = ToFsPath(projectPath);
            std::filesystem::path projectNameFs = projFs.filename();
            layoutDir = std::filesystem::path(docsPath) / L"Infernux" / projectNameFs;
        }
#else
        const char *home = std::getenv("HOME");
        if (home) {
            std::filesystem::path projFs = ToFsPath(projectPath);
            std::filesystem::path projectNameFs = projFs.filename();
            layoutDir = std::filesystem::path(home) / ".config" / "Infernux" / projectNameFs;
        }
#endif
        if (layoutDir.empty()) {
            layoutDir = ToFsPath(projectPath);
        }
        std::filesystem::create_directories(layoutDir);
        m_imguiIniPath = layoutDir / "imgui.ini";
        m_imguiLayoutMetadataPath = layoutDir / "imgui-layout.json";
    }
    // Disable ImGui auto-save (it uses fopen which can't handle Unicode
    // paths on Windows). We manually load/save with std::fstream instead.
    ImGuiIO &io = ImGui::GetIO();
    io.IniFilename = nullptr;
    LoadImGuiLayout();
    startupPhase("layout", phaseBegin);

    // Initialize physics world (Jolt)
    phaseBegin = StartupClock::now();
    PhysicsWorld::Instance().Initialize();
    startupPhase("physics", phaseBegin);

    // Initialize audio engine (SDL3 audio)
    phaseBegin = StartupClock::now();
    if (!AudioEngine::Instance().Initialize()) {
        INXLOG_WARN("Audio engine failed to initialize. Audio features will be unavailable.");
    }
    startupPhase("audio", phaseBegin);
    startupPhase("total", startupBegin);
    m_isInitialized = true;
}

void Infernux::InitHeadless(const std::string &projectPath, const std::string &builtinResourcePath)
{
    if (m_runtimeMode != RuntimeMode::Headless) {
        throw std::logic_error("init_headless requires RuntimeMode::Headless");
    }
    if (!CheckEngineValid("initialize headless runtime")) {
        throw std::logic_error("Engine is not available");
    }
    if (m_isInitialized) {
        throw std::logic_error("Engine is already initialized");
    }

#if INFERNUX_FILE_LOGGING
    {
        auto logsDir = ToFsPath(JoinPath({projectPath, "Logs"}));
        std::filesystem::create_directories(logsDir);
        auto logFile = logsDir / "engine.log";
#if INFERNUX_DEFERRED_FILE_LOGGING
        INXLOG_SET_DEFERRED_FILE(FromFsPath(logFile), 100);
#else
        INXLOG_SET_FILE(FromFsPath(logFile));
#endif
    }
#endif

    if (!JobSystem::IsAvailable()) {
        JobSystem::Initialize();
    }

    m_assetDatabase->Initialize(projectPath);
    auto &registry = AssetRegistry::Instance();
    registry.Initialize(std::move(m_assetDatabase));
    registry.RegisterLoader(ResourceType::Material, std::make_unique<MaterialLoader>());
    registry.RegisterLoader(ResourceType::PhysicMaterial, std::make_unique<PhysicMaterialLoader>());
    registry.RegisterLoader(ResourceType::Texture, std::make_unique<TextureLoader>());
    registry.RegisterLoader(ResourceType::Mesh, std::make_unique<MeshLoader>());
    registry.RegisterLoader(ResourceType::Audio, std::make_unique<AudioClipLoader>());
    registry.RegisterLoader(ResourceType::Shader, std::make_unique<ShaderLoader>());
    registry.RegisterLoader(ResourceType::Script, std::make_unique<InxPythonScriptLoader>());
    registry.RegisterLoader(ResourceType::DefaultText, std::make_unique<InxDefaultTextLoader>());
    registry.RegisterLoader(ResourceType::RenderEffect,
                            std::make_unique<InxDefaultTextLoader>(ResourceType::RenderEffect));
    registry.RegisterLoader(ResourceType::ParticleGraph,
                            std::make_unique<InxDefaultTextLoader>(ResourceType::ParticleGraph));
    registry.RegisterLoader(ResourceType::DefaultBinary, std::make_unique<InxDefaultBinaryLoader>());
    registry.PopulateAssetDatabaseLoaders();
    if (!builtinResourcePath.empty()) {
        InxShaderLoader::AddShaderSearchPath(JoinPath({builtinResourcePath, "shaders"}));
        registry.GetAssetDatabase()->AddReadOnlyScanRoot(builtinResourcePath);
    }
    registry.GetAssetDatabase()->AddScanRoot(JoinPath({projectPath, "Packages"}));

    // Asset catalog resolution must match the graphical runtime so a cooked
    // project simulates against the same GUID catalog in both modes. Headless
    // keeps the synchronous full Refresh in the non-player path because
    // deterministic batch runs must not race a background catalog validation.
    {
        const char *playerModeFlag = std::getenv("_INFERNUX_PLAYER_MODE");
        const bool playerMode = playerModeFlag != nullptr && playerModeFlag[0] == '1' && playerModeFlag[1] == '\0';
        const std::string runtimeAssetCatalog = JoinPath({projectPath, "Library", "RuntimeAssetRecords.json"});
        std::error_code runtimeCatalogError;
        const bool hasRuntimeCatalog =
            std::filesystem::is_regular_file(ToFsPath(runtimeAssetCatalog), runtimeCatalogError) &&
            !runtimeCatalogError;
        if (playerMode && hasRuntimeCatalog) {
            registry.GetAssetDatabase()->InstallRuntimeAssetCatalog(runtimeAssetCatalog, true);
        } else {
            registry.GetAssetDatabase()->Refresh();
            if (hasRuntimeCatalog)
                registry.GetAssetDatabase()->InstallRuntimeAssetCatalog(runtimeAssetCatalog);
        }
    }

    RegisterPhysicMaterialAssetCallback();

    PhysicsWorld::Instance().Initialize();
    if (SceneManager::Instance().GetActiveScene() == nullptr) {
        SceneManager::Instance().CreateScene("Headless Scene");
    }

    m_isInitialized = true;
    INXLOG_INFO("Headless runtime initialized without renderer, window, GUI, or audio device.");
}

// ----------------------------------
// Mesh geometry extraction helper (shared by SetSelectionOutline / SetSelectionOutlines)
// ----------------------------------

static bool ExtractMeshGeometry(MeshRenderer *renderer, std::vector<glm::vec3> &positions,
                                std::vector<glm::vec3> &normals, std::vector<uint32_t> &indices)
{
    positions.clear();
    normals.clear();
    indices.clear();

    if (renderer->HasInlineMesh()) {
        const auto &verts = renderer->GetInlineVertices();
        positions.reserve(verts.size());
        normals.reserve(verts.size());
        for (const auto &v : verts) {
            positions.push_back(v.pos);
            normals.push_back(v.normal);
        }
        indices = renderer->GetInlineIndices();
    } else if (renderer->HasMeshAsset()) {
        auto mesh = renderer->GetMeshAssetRef().Get();
        if (!mesh || mesh->GetVertices().empty() || mesh->GetIndices().empty())
            return false;

        const auto &meshVertices = mesh->GetVertices();
        const auto &meshIndices = mesh->GetIndices();
        int32_t nodeGroup = renderer->GetNodeGroup();

        if (nodeGroup >= 0) {
            std::unordered_map<uint32_t, uint32_t> vertexRemap;
            for (const auto &sub : mesh->GetSubMeshes()) {
                if (static_cast<int32_t>(sub.nodeGroup) != nodeGroup)
                    continue;
                for (uint32_t i = 0; i < sub.indexCount; ++i) {
                    uint32_t origIdx = meshIndices[sub.indexStart + i];
                    auto it = vertexRemap.find(origIdx);
                    if (it == vertexRemap.end()) {
                        uint32_t newIdx = static_cast<uint32_t>(positions.size());
                        vertexRemap[origIdx] = newIdx;
                        positions.push_back(meshVertices[origIdx].pos);
                        normals.push_back(meshVertices[origIdx].normal);
                        indices.push_back(newIdx);
                    } else {
                        indices.push_back(it->second);
                    }
                }
            }
        } else {
            positions.reserve(meshVertices.size());
            normals.reserve(meshVertices.size());
            for (const auto &v : meshVertices) {
                positions.push_back(v.pos);
                normals.push_back(v.normal);
            }
            indices = meshIndices;
        }
    } else {
        return false;
    }

    return !positions.empty() && !indices.empty();
}

static void CollectOutlineSubtreeIds(GameObject *obj, std::vector<uint64_t> &outIds, std::unordered_set<uint64_t> &seen)
{
    if (!obj || !obj->IsActiveInHierarchy())
        return;
    const uint64_t id = obj->GetID();
    if (id != 0 && seen.insert(id).second)
        outIds.push_back(id);
    for (size_t i = 0; i < obj->GetChildCount(); ++i)
        CollectOutlineSubtreeIds(obj->GetChild(i), outIds, seen);
}

static std::vector<uint64_t> ExpandOutlineIds(Scene *scene, const std::vector<uint64_t> &objectIds)
{
    std::vector<uint64_t> expanded;
    if (!scene)
        return expanded;

    std::unordered_set<uint64_t> seen;
    for (uint64_t objectId : objectIds) {
        GameObject *obj = scene->FindByID(objectId);
        CollectOutlineSubtreeIds(obj, expanded, seen);
    }
    return expanded;
}

// ----------------------------------
// Editor Gizmos
// ----------------------------------

void Infernux::SetSelectionOutline(uint64_t objectId)
{
    if (m_isCleanedUp || !m_renderer) {
        return;
    }
    SetSelectionOutlines(objectId == 0 ? std::vector<uint64_t>{} : std::vector<uint64_t>{objectId});
}

void Infernux::ClearSelectionOutline()
{
    if (m_isCleanedUp || !m_renderer) {
        return;
    }
    m_cachedOutlineIds.clear();
    m_selectedObjectId = 0;
    m_renderer->SetSelectedObjectId(0);
    m_renderer->GetEditorGizmos().ClearSelectionOutline();
}

void Infernux::SetSelectionOutlines(const std::vector<uint64_t> &objectIds)
{
    if (m_isCleanedUp || !m_renderer) {
        return;
    }

    auto &gizmos = m_renderer->GetEditorGizmos();

    if (objectIds.empty()) {
        m_cachedOutlineIds.clear();
        m_selectedObjectId = 0;
        m_renderer->SetSelectionState(0, {});
        gizmos.ClearSelectionOutline();
        return;
    }

    Scene *scene = SceneManager::Instance().GetActiveScene();
    if (!scene) {
        m_cachedOutlineIds.clear();
        m_selectedObjectId = 0;
        m_renderer->SetSelectionState(0, {});
        gizmos.ClearSelectionOutline();
        return;
    }

    std::vector<uint64_t> expandedIds = ExpandOutlineIds(scene, objectIds);
    const uint64_t primaryObjectId = objectIds.empty() ? 0 : objectIds.back();
    m_selectedObjectId = primaryObjectId;
    m_renderer->SetSelectionState(primaryObjectId, expandedIds);
    if (expandedIds == m_cachedOutlineIds) {
        return;
    }
    m_cachedOutlineIds = expandedIds;

    std::vector<glm::vec3> mergedPositions;
    std::vector<glm::vec3> mergedNormals;
    std::vector<uint32_t> mergedIndices;

    for (uint64_t objId : expandedIds) {
        GameObject *obj = scene->FindByID(objId);
        if (!obj || !obj->IsActiveInHierarchy())
            continue;

        MeshRenderer *renderer = obj->GetComponent<MeshRenderer>();
        if (!renderer || !renderer->IsEnabled())
            continue;

        std::vector<glm::vec3> positions;
        std::vector<glm::vec3> normals;
        std::vector<uint32_t> indices;

        if (!ExtractMeshGeometry(renderer, positions, normals, indices))
            continue;

        glm::mat4 worldMatrix = obj->GetTransform()->GetWorldMatrix();
        glm::mat3 normalMatrix = glm::transpose(glm::inverse(glm::mat3(worldMatrix)));

        uint32_t baseIndex = static_cast<uint32_t>(mergedPositions.size());
        for (size_t i = 0; i < positions.size(); ++i) {
            glm::vec4 wp = worldMatrix * glm::vec4(positions[i], 1.0f);
            mergedPositions.push_back(glm::vec3(wp));
            glm::vec3 wn = glm::normalize(normalMatrix * normals[i]);
            mergedNormals.push_back(wn);
        }
        for (uint32_t idx : indices) {
            mergedIndices.push_back(idx + baseIndex);
        }
    }

    if (mergedPositions.empty() || mergedIndices.empty()) {
        gizmos.ClearSelectionOutline();
        return;
    }

    gizmos.SetSelectionOutline(mergedPositions, mergedNormals, mergedIndices, glm::mat4(1.0f));
}

// ----------------------------------
// Material Pipeline
// ----------------------------------

void Infernux::RegisterShaderToRenderer(const ShaderAsset &asset)
{
    if (!m_renderer || !asset.HasVariant(ShaderCompileTarget::Forward))
        return;

    const char *stage = asset.shaderType == "vertex" ? "vertex" : "fragment";
    auto runtimeId = [&](ShaderCompileTarget target) {
        switch (target) {
        case ShaderCompileTarget::Forward:
            return asset.shaderId;
        case ShaderCompileTarget::GBuffer:
            return asset.shaderId + "/gbuffer";
        case ShaderCompileTarget::Shadow:
            return asset.shaderId + "/shadow";
        case ShaderCompileTarget::Depth:
            return asset.shaderId + "/depth";
        case ShaderCompileTarget::Picking:
            return asset.shaderId + "/picking";
        case ShaderCompileTarget::Motion:
            return asset.shaderId + "/motion";
        case ShaderCompileTarget::ForwardPlus:
            return asset.shaderId + "/forward_plus";
        case ShaderCompileTarget::Normal:
            return asset.shaderId + "/normal";
        case ShaderCompileTarget::BaseColor:
            return asset.shaderId + "/base_color";
        case ShaderCompileTarget::Count:
            break;
        }
        return std::string();
    };

    for (const auto &variant : asset.variants) {
        if (!variant.IsValid())
            continue;
        const std::string id = runtimeId(variant.target);
        if (id.empty())
            continue;
        m_renderer->LoadShader(id.c_str(), variant.spirv, stage);
        if (variant.target != ShaderCompileTarget::Forward)
            INXLOG_INFO("Registered ", ShaderCompileTargetName(variant.target), " ", stage, " variant '", id, "'");
    }

    // Render-state metadata (fragment shaders only)
    if (asset.shaderType == "fragment") {
        const auto &rm = asset.renderMeta;
        m_renderer->StoreShaderRenderMeta(asset.shaderId, rm.cullMode, rm.depthWrite, rm.depthTest, rm.blend, rm.queue,
                                          rm.passTag, rm.stencil, rm.alphaClip);
    }
}

void Infernux::LoadAndRegisterShaders(const std::string &dir, bool recursive)
{
    namespace fs = std::filesystem;
    auto &registry = AssetRegistry::Instance();
    auto *adb = registry.GetAssetDatabase();
    if (!adb || !m_renderer)
        return;

    fs::path dirPath = ToFsPath(dir);
    const bool directoryExists = fs::exists(dirPath);
    if (!directoryExists && !recursive)
        return;
    // Project shaders resolve lazily from AssetDatabase metadata in both the
    // Editor and cooked Player. Eagerly compiling every authored fullscreen
    // and material stage made startup proportional to the entire project,
    // although the active scene normally references only a small subset.
    // Material refresh and FullscreenRenderer share EnsureShaderLoaded(), so
    // this changes timing rather than shader availability or hot reload.
    if (recursive)
        return;

    // Bootstrap needs only a valid surface while the first scene's authored
    // material programs are being prepared. Running this fallback through the
    // ShaderInfo linker expands templates and shading models that cannot be
    // observed before scene publication. Compile a tiny ABI-compatible GLSL
    // pair instead. This is not a cooked project shader: authored GLSL remains
    // dynamic and replaces the fallback as soon as its material is resident.
    static constexpr std::string_view bootstrapVertex = R"glsl(#version 450
layout(std140, set = 1, binding = 5) uniform UniformBufferObject {
    mat4 model;
    mat4 view;
    mat4 proj;
    mat4 previousViewProj;
    mat4 inverseViewProj;
    vec4 projectionParams;
    vec4 zBufferParams;
} ubo;
layout(std430, set = 2, binding = 1) readonly buffer InstanceBuffer {
    mat4 instanceModels[];
};
layout(location = 0) in vec3 inPosition;
void main() {
    gl_Position = ubo.proj * ubo.view * instanceModels[gl_InstanceIndex] * vec4(inPosition, 1.0);
}
)glsl";
    static constexpr std::string_view bootstrapFragment = R"glsl(#version 450
layout(location = 0) out vec4 outColor;
void main() {
    outColor = vec4(1.0);
}
)glsl";
    InxShaderLoader bootstrapCompiler(false, true, false, true, false, false, false, false, false, false);
    auto bootstrapVertexSpirv =
        bootstrapCompiler.CompileVertexGlsl(std::string(bootstrapVertex), "<infernux-bootstrap.vert>");
    auto bootstrapFragmentSpirv =
        bootstrapCompiler.CompileFragmentGlsl(std::string(bootstrapFragment), "<infernux-bootstrap.frag>");
    if (!bootstrapVertexSpirv.empty() && !bootstrapFragmentSpirv.empty()) {
        m_renderer->LoadShader("default", bootstrapVertexSpirv, "vertex");
        m_renderer->LoadShader("default", bootstrapFragmentSpirv, "fragment");
        return;
    }
    INXLOG_WARN("Minimal default shader bootstrap failed; falling back to authored asset compilation");

    std::unordered_set<std::string> loadedShaderKeys;
    std::vector<char> defaultVertCode;
    std::vector<char> defaultFragCode;

    auto processPath = [&](const fs::path &file) {
        std::error_code fileError;
        if (!fs::is_regular_file(file, fileError) || fileError)
            return;

        std::string filePath = FromFsPath(file);

        // Refresh already registers project and built-in roots. Re-importing a
        // read-only built-in here would create derived sidecars beside package
        // resources and duplicate importer work.
        std::string guid = adb->GetGuidFromPath(filePath);
        if (guid.empty())
            guid = adb->ImportAsset(filePath).guid;
        if (guid.empty())
            return;

        // Imported metadata is authoritative in both Editor and Player; the
        // source extension remains a fallback for loose GLSL files.
        std::string shaderId;
        const auto meta = adb->GetMetaByGuid(guid);
        if (meta && meta->HasKey("shader_id")) {
            shaderId = meta->GetDataAs<std::string>("shader_id");
        }
        if (shaderId.empty()) {
            shaderId = FromFsPath(file.stem());
        }

        std::string shaderType;
        if (meta && meta->HasKey("type"))
            shaderType = meta->GetDataAs<std::string>("type");
        const std::string physicalExt = FromFsPath(file.extension());
        if (shaderType != "vertex" && shaderType != "fragment") {
            if (physicalExt == ".vert")
                shaderType = "vertex";
            else if (physicalExt == ".frag")
                shaderType = "fragment";
        }
        if (shaderType != "vertex" && shaderType != "fragment")
            return;

        if (!recursive) {
            const bool minimalBootstrapShader =
                (shaderId == "Standard" && shaderType == "vertex") || (shaderId == "Unlit" && shaderType == "fragment");
            if (!minimalBootstrapShader)
                return;
        }

        const std::string stageExt = shaderType == "vertex" ? ".vert" : ".frag";

        std::string shaderKey = shaderId + "_" + stageExt;

        // Skip duplicates
        if (loadedShaderKeys.count(shaderKey))
            return;

        // For recursive (asset) shaders, skip if already loaded in renderer
        if (recursive && m_renderer->HasShader(shaderId, shaderType)) {
            loadedShaderKeys.insert(shaderKey);
            return;
        }

        loadedShaderKeys.insert(shaderKey);

        bool directStructuredStage = false;
        if (meta && meta->HasKey("shader_capabilities")) {
            try {
                const auto capabilities = json::parse(meta->GetDataAs<std::string>("shader_capabilities"));
                directStructuredStage = capabilities.is_array() &&
                                        std::any_of(capabilities.begin(), capabilities.end(), [](const json &value) {
                                            return value.is_string() && (value.get<std::string>() == "Fullscreen" ||
                                                                         value.get<std::string>() == "Standalone");
                                        });
            } catch (const json::exception &) {
                directStructuredStage = false;
            }
        }

        const bool builtinFallbackStage =
            !recursive &&
            ((shaderId == "Standard" && stageExt == ".vert") || (shaderId == "Lit" && stageExt == ".frag") ||
             (shaderId == "Unlit" && stageExt == ".frag") || shaderId == "Error");

        // ShaderInfo material stages are programs and must be linked as a
        // vertex/fragment pair, regardless of whether they live in Assets or
        // the engine's read-only resources. Only explicitly direct stages and
        // the renderer's minimal fallback pair are compiled independently.
        if (meta && meta->HasKey("shader_schema_format") &&
            meta->GetDataAs<std::string>("shader_schema_format") == "ShaderInfo" && !directStructuredStage &&
            !builtinFallbackStage) {
            return;
        }

        // Load via AssetRegistry (compiles the shader)
        auto shaderAsset = registry.LoadAsset<ShaderAsset>(guid, ResourceType::Shader);
        if (!shaderAsset || !shaderAsset->HasVariant(ShaderCompileTarget::Forward))
            return;

        // Register all variants with the renderer
        RegisterShaderToRenderer(*shaderAsset);
        if (m_renderer && !m_renderer->PumpStartupEvents())
            throw std::runtime_error("Startup cancelled");

        INXLOG_DEBUG("Loaded shader '", shaderId, "' (", stageExt, ") from ", filePath);

        // Track built-in fallback shaders used for the renderer's default program.
        if (!recursive) {
            const auto *forward = shaderAsset->FindVariant(ShaderCompileTarget::Forward);
            if (shaderId == "Standard" && stageExt == ".vert")
                defaultVertCode = forward->spirv;
            else if (shaderId == "Unlit" && stageExt == ".frag")
                defaultFragCode = forward->spirv;
        }
    };

    if (recursive) {
        if (directoryExists) {
            for (const auto &entry : fs::recursive_directory_iterator(dirPath))
                processPath(entry.path());
        }

        // A Player does not retain project shaders under Assets. Its runtime
        // catalog maps packed GLSL to Library artifacts, so enumerate the
        // database rather than assuming authoring paths are physically
        // present. The key set above also prevents duplicate publication.
        for (const auto &assetPath : adb->GetAllAssetPaths()) {
            const auto metadata = adb->GetMetaByPath(assetPath);
            const ResourceType resourceType =
                metadata ? metadata->GetResourceType() : adb->GetResourceTypeForPath(assetPath);
            if (resourceType == ResourceType::Shader)
                processPath(ToFsPath(assetPath));
        }
    } else {
        for (const auto &entry : fs::directory_iterator(dirPath))
            processPath(entry.path());
    }

    // Register fallback shaders (non-recursive = builtin shaders only)
    if (!recursive) {
        if (!defaultVertCode.empty()) {
            m_renderer->LoadShader("default", defaultVertCode, "vertex");
            INXLOG_INFO("Registered 'standard' as default vertex shader");
        }
        if (!defaultFragCode.empty()) {
            m_renderer->LoadShader("default", defaultFragCode, "fragment");
            INXLOG_INFO("Registered 'unlit' as default fragment shader");
        }
    }
}

bool Infernux::EnsureShaderLoaded(const std::string &shaderId, const std::string &shaderType)
{
    if (m_renderer->HasShader(shaderId, shaderType)) {
        return true;
    }

    INXLOG_DEBUG("Infernux::EnsureShaderLoaded: shader '", shaderId, "' (", shaderType,
                 ") not loaded, trying to find and load it");

    auto *adb = GetAssetDatabase();
    if (!adb) {
        INXLOG_WARN("Infernux::EnsureShaderLoaded: no AssetDatabase available");
        return false;
    }
    std::string shaderPath = adb->FindShaderPathById(shaderId, shaderType);
    if (shaderPath.empty()) {
        INXLOG_WARN("Infernux::EnsureShaderLoaded: could not find shader file for '", shaderId, "' (", shaderType, ")");
        return false;
    }

    INXLOG_DEBUG("Infernux::EnsureShaderLoaded: found shader at '", shaderPath, "', loading...");

    // This path is runtime publication, not an editor hot-reload operation.
    // Loading through AssetRegistry keeps stage identity in imported metadata
    // for both loose Editor shaders and packed Player GLSL.
    auto &registry = AssetRegistry::Instance();
    const std::string guid = adb->GetGuidFromPath(shaderPath);
    if (guid.empty()) {
        INXLOG_ERROR("Infernux::EnsureShaderLoaded: shader is not imported: ", shaderPath);
        return false;
    }
    auto shaderAsset = registry.LoadAsset<ShaderAsset>(guid, ResourceType::Shader);
    if (!shaderAsset || shaderAsset->shaderId != shaderId || shaderAsset->shaderType != shaderType) {
        INXLOG_ERROR("Infernux::EnsureShaderLoaded: imported shader identity/stage mismatch for '", shaderPath,
                     "' (expected ", shaderId, "/", shaderType, ")");
        return false;
    }
    RegisterShaderToRenderer(*shaderAsset);
    return m_renderer->HasShader(shaderId, shaderType);
}

Infernux::LinkedShaderProgramPreparation
Infernux::EnsureLinkedShaderProgramArtifact(const std::shared_ptr<InxMaterial> &material)
{
    if (!material)
        return {};

    auto *adb = GetAssetDatabase();
    if (!adb)
        return {};

    auto resolvePath = [&](const ShaderAssetReference &reference, const char *stage) {
        if (!reference.guid.empty()) {
            // A missing GUID remains a missing reference. Never fall through
            // to path_hint or shader_id and silently bind a different asset.
            return adb->GetPathFromGuid(reference.guid);
        }

        // path_hint is display/diagnostic metadata only. A GUID-less, pathless
        // shader_id is the explicit symbolic built-in shader case; it is not
        // an AssetDatabase dependency and may use the program registry.
        if (!reference.pathHint.empty())
            return std::string{};
        return adb->FindShaderPathById(reference.shaderId, stage);
    };

    const ShaderStagePair stages{material->GetVertShaderName(), material->GetFragShaderName()};
    const std::string vertexPath = resolvePath(material->GetVertShaderReference(), "vertex");
    const std::string fragmentPath = resolvePath(material->GetFragShaderReference(), "fragment");
    return EnsureLinkedShaderProgramArtifact(stages, vertexPath, fragmentPath);
}

Infernux::LinkedShaderProgramPreparation Infernux::EnsureLinkedShaderProgramArtifact(const ShaderStagePair &stages)
{
    auto *adb = GetAssetDatabase();
    if (!adb)
        return {};

    return EnsureLinkedShaderProgramArtifact(stages, adb->FindShaderPathById(stages.vertexShaderId, "vertex"),
                                             adb->FindShaderPathById(stages.fragmentShaderId, "fragment"));
}

Infernux::LinkedShaderProgramPreparation Infernux::EnsureLinkedShaderProgramArtifact(const ShaderStagePair &stages,
                                                                                     const std::string &vertexPath,
                                                                                     const std::string &fragmentPath)
{
    LinkedShaderProgramPreparation result;
    if (!m_renderer || !stages.IsValid())
        return result;

    const auto cached = m_linkedShaderProgramCache.find(stages);
    if (cached != m_linkedShaderProgramCache.end()) {
        result.usesLinkedArtifact = true;
        if (cached->second.sourceStamp != 0 && cached->second.programKey.IsValid() &&
            m_renderer->HasShaderProgramArtifact(cached->second.programKey)) {
            return result;
        }
        if (cached->second.failedSourceStamp != 0) {
            result.success = false;
            result.error = cached->second.lastError;
            return result;
        }
    }

    if (vertexPath.empty() || fragmentPath.empty())
        return result;

    auto *adb = GetAssetDatabase();
    if (!adb)
        return result;

    auto readSource = [&](const std::string &path, std::string &source) {
        std::vector<char> bytes;
        if (!adb->ReadFile(path, bytes) || bytes.empty())
            return false;
        if (bytes.back() == '\0')
            bytes.pop_back();
        source.assign(bytes.begin(), bytes.end());
        return true;
    };

    std::string vertexSource;
    std::string fragmentSource;
    if (!readSource(vertexPath, vertexSource) || !readSource(fragmentPath, fragmentSource))
        return result;

    const uint64_t sourceStamp =
        ComputeShaderProgramRevision(vertexSource, fragmentSource, ShaderCompileTarget::Forward, 0);
    auto rememberFailure = [&](const std::string &error) {
        auto &entry = m_linkedShaderProgramCache[stages];
        entry.failedSourceStamp = sourceStamp;
        entry.lastError = error;
    };

    InxShaderLoader compiler(true, false, false, false, false, true, false, false, false, false);
    const std::string vertexCompilePath = InxShaderLoader::StageQualifiedVirtualPath(vertexPath, "vertex");
    const std::string fragmentCompilePath = InxShaderLoader::StageQualifiedVirtualPath(fragmentPath, "fragment");
    const ShaderDescriptor vertexDescriptor = compiler.ParseShaderSource(vertexSource, vertexCompilePath);
    const ShaderDescriptor fragmentDescriptor = compiler.ParseShaderSource(fragmentSource, fragmentCompilePath);

    if (IsDirectStructuredStage(vertexDescriptor) || IsDirectStructuredStage(fragmentDescriptor)) {
        // Explicit main() stages are independently compiled and registered.
        // They must never leave a failed material-artifact cache entry behind.
        m_linkedShaderProgramCache.erase(stages);
        return {};
    }

    result.usesLinkedArtifact = true;
    if (vertexDescriptor.shaderId != stages.vertexShaderId || fragmentDescriptor.shaderId != stages.fragmentShaderId) {
        result.success = false;
        result.error = "ShaderInfo Name must match the shader IDs referenced by the material";
        rememberFailure(result.error);
        return result;
    }

    auto compilation =
        compiler.CompileLinkedProgramArtifact(vertexSource, vertexCompilePath, fragmentSource, fragmentCompilePath);
    if (!compilation.IsValid()) {
        result.success = false;
        std::ostringstream diagnostics;
        for (const auto &error : compilation.errors) {
            if (diagnostics.tellp() > 0)
                diagnostics << '\n';
            diagnostics << error;
        }
        result.error = diagnostics.str();
        if (result.error.empty())
            result.error = "Linked shader program compilation failed";
        rememberFailure(result.error);
        return result;
    }

    ShaderProgramArtifact artifact = compilation.CreateRuntimeArtifact();
    if (!artifact.IsValid() || artifact.key.stages != stages) {
        result.success = false;
        result.error = "Linked shader compiler produced an invalid or mismatched program artifact";
        rememberFailure(result.error);
        return result;
    }

    if (!m_renderer->PublishShaderProgramArtifact(artifact)) {
        result.success = false;
        result.error = "Renderer rejected the linked shader program artifact; last-known-good remains active";
        rememberFailure(result.error);
        return result;
    }

    if (!compilation.pendingTargets.empty()) {
        std::ostringstream pending;
        for (const auto target : compilation.pendingTargets) {
            if (pending.tellp() > 0)
                pending << ", ";
            pending << ShaderCompileTargetName(target);
        }
        INXLOG_DEBUG("Linked shader program '", artifact.key.stages.ToString(),
                     "' has planned targets awaiting backend generation: ", pending.str());
    }

    m_linkedShaderProgramCache[stages] = LinkedShaderProgramCacheEntry{sourceStamp, artifact.key, 0, {}};
    m_renderer->StoreShaderRenderMeta(
        stages.fragmentShaderId, fragmentDescriptor.surfaceOptions.cullMode, fragmentDescriptor.depthWrite,
        fragmentDescriptor.depthTest, fragmentDescriptor.surfaceOptions.blendMode, fragmentDescriptor.renderQueue,
        fragmentDescriptor.passTag, fragmentDescriptor.stencil, fragmentDescriptor.surfaceOptions.alphaClip);
    return result;
}

bool Infernux::RefreshMaterialPipeline(std::shared_ptr<InxMaterial> material)
{
    INXLOG_DEBUG("Infernux::RefreshMaterialPipeline called");
    if (!CheckEngineValid("refresh material pipeline") || !m_renderer) {
        INXLOG_ERROR("Infernux::RefreshMaterialPipeline: engine or renderer invalid");
        return false;
    }

    auto *adb = GetAssetDatabase();
    if (!adb) {
        INXLOG_ERROR("Infernux::RefreshMaterialPipeline: no AssetDatabase available");
        return false;
    }

    if (!material) {
        INXLOG_ERROR("Infernux::RefreshMaterialPipeline: material is null");
        return false;
    }

    // Get shader names from material
    const std::string &vertName = material->GetVertShaderName();
    const std::string &fragName = material->GetFragShaderName();

    const LinkedShaderProgramPreparation linkedProgram = EnsureLinkedShaderProgramArtifact(material);
    if (linkedProgram.usesLinkedArtifact) {
        if (!linkedProgram.success)
            INXLOG_ERROR("Infernux::RefreshMaterialPipeline: ", linkedProgram.error);
        // Refresh still runs on failure so an already-published last-known-good
        // artifact can remain visible.
        return m_renderer->RefreshMaterialPipeline(material);
    }

    // Ensure shaders are loaded before refreshing pipeline
    if (!vertName.empty()) {
        EnsureShaderLoaded(vertName, "vertex");
    }
    if (!fragName.empty()) {
        EnsureShaderLoaded(fragName, "fragment");
    }

    INXLOG_DEBUG("Infernux::RefreshMaterialPipeline: calling renderer");
    return m_renderer->RefreshMaterialPipeline(material);
}

std::string Infernux::ReloadShaderRuntime(const std::string &shaderPath, const std::string &previousShaderId)
{
    // INXLOG_INFO("Infernux::ReloadShaderRuntime called: ", shaderPath);
    if (!CheckEngineValid("reload shader") || !m_renderer) {
        INXLOG_ERROR("Infernux::ReloadShaderRuntime: engine or renderer invalid");
        return "Engine or renderer invalid";
    }

    auto &registry = AssetRegistry::Instance();
    auto *adb = registry.GetAssetDatabase();
    if (!adb) {
        INXLOG_ERROR("Infernux::ReloadShaderRuntime: no AssetDatabase available");
        return "No AssetDatabase available";
    }

    std::filesystem::path path = ToFsPath(shaderPath);
    std::string ext = FromFsPath(path.extension());

    if (ext != ".vert" && ext != ".frag") {
        INXLOG_ERROR("Infernux::ReloadShaderRuntime: unsupported shader extension: ", ext);
        return "Unsupported shader extension: " + ext;
    }

    const std::string guid = adb->GetGuidFromPath(shaderPath);

    // Invalidate shader-id map cache for this directory so shading models
    // and imports added/modified since the last compile are discovered.
    InxShaderLoader::InvalidateDirectoryCache(FromFsPath(ToFsPath(shaderPath).parent_path()));
    InxShaderLoader::InvalidateTemplateCache();

    if (guid.empty()) {
        INXLOG_ERROR("Infernux::ReloadShaderRuntime: shader is not imported: ", shaderPath);
        return "Shader asset is not imported: " + shaderPath;
    }

    std::vector<char> sourceBytes;
    if (!adb->ReadFile(shaderPath, sourceBytes) || sourceBytes.empty())
        return "Failed to read shader source: " + shaderPath;
    if (sourceBytes.back() == '\0')
        sourceBytes.pop_back();
    const std::string source(sourceBytes.begin(), sourceBytes.end());

    InxShaderLoader sourceParser(true, false, false, false, false, true, false, false, false, false);
    const ShaderDescriptor changedDescriptor = sourceParser.ParseShaderSource(source, shaderPath);
    if (!changedDescriptor.errors.empty()) {
        std::ostringstream diagnostics;
        diagnostics << "ShaderInfo validation failed:";
        for (const auto &error : changedDescriptor.errors)
            diagnostics << '\n' << error;
        return diagnostics.str();
    }

    {
        const std::string changedShaderId = changedDescriptor.shaderId;
        if (changedShaderId.empty())
            return "ShaderInfo Name is required for runtime reload";
        if (!previousShaderId.empty() && changedShaderId != previousShaderId) {
            return "Changing ShaderInfo Name during hot reload requires an asset reimport before materials can be "
                   "migrated";
        }

        if (IsDirectStructuredStage(changedDescriptor)) {
            registry.InvalidateAsset(guid);
            auto shaderAsset = registry.LoadAsset<ShaderAsset>(guid, ResourceType::Shader);
            if (!shaderAsset || !shaderAsset->HasVariant(ShaderCompileTarget::Forward)) {
                const std::string compileError = InxShaderLoader::GetLastCompileError();
                return compileError.empty() ? "Standalone ShaderInfo stage compilation failed" : compileError;
            }
            m_renderer->InvalidateShaderCache(changedShaderId);
            RegisterShaderToRenderer(*shaderAsset);
            m_renderer->RefreshMaterialsUsingShader(changedShaderId);
            INXLOG_INFO("Infernux::ReloadShaderRuntime: reloaded standalone ShaderInfo stage '", changedShaderId, "'");
            return "";
        }

        std::vector<ShaderStagePair> affectedPairs;
        for (auto &[stages, cacheEntry] : m_linkedShaderProgramCache) {
            if (!stages.UsesShader(changedShaderId))
                continue;
            affectedPairs.push_back(stages);
            // Force regeneration even when only an imported library or a
            // compiler template changed and the two root source files did not.
            cacheEntry.sourceStamp = 0;
            cacheEntry.failedSourceStamp = 0;
            cacheEntry.lastError.clear();
        }

        registry.InvalidateAsset(guid);
        std::unordered_set<ShaderStagePair, ShaderStagePairHash> preparedPairs;
        std::string firstError;
        bool foundMaterial = false;
        for (const auto &stages : affectedPairs) {
            const LinkedShaderProgramPreparation prepared = EnsureLinkedShaderProgramArtifact(stages);
            preparedPairs.insert(stages);
            if (!prepared.success && firstError.empty())
                firstError = prepared.error;
        }
        for (auto &material : registry.GetAllMaterials()) {
            if (!material)
                continue;
            const ShaderStagePair stages{material->GetVertShaderName(), material->GetFragShaderName()};
            if (!stages.UsesShader(changedShaderId))
                continue;
            foundMaterial = true;
            if (preparedPairs.insert(stages).second) {
                const LinkedShaderProgramPreparation prepared = EnsureLinkedShaderProgramArtifact(material);
                if (!prepared.success && firstError.empty())
                    firstError = prepared.error;
            }
            // This refresh consumes either the newly published artifact or the
            // previous last-known-good artifact when compilation failed.
            m_renderer->RefreshMaterialPipeline(material);
        }

        if (!firstError.empty()) {
            INXLOG_ERROR("Infernux::ReloadShaderRuntime: linked program compile failed; keeping last-known-good: ",
                         firstError);
            return firstError;
        }
        // INXLOG_INFO("Infernux::ReloadShaderRuntime: published ShaderInfo program revisions for '", changedShaderId,
        //             "' (material pairs=", preparedPairs.size(), ", referenced=", foundMaterial ? "yes" : "no", ")");
        return "";
    }
}

void Infernux::ReloadTexture(const std::string &texturePath)
{
    if (!CheckEngineValid("reload texture") || !m_renderer) {
        INXLOG_ERROR("Infernux::ReloadTexture: engine or renderer invalid");
        return;
    }

    // Boundary adapter: this is the only place where the texture hot-reload
    // path crosses from the file-system domain (paths, from file watchers)
    // into the renderer domain (GUID-only). Resolve here or bail out.
    auto &registry = AssetRegistry::Instance();
    auto *adb = registry.GetAssetDatabase();
    std::string guid;
    if (adb) {
        guid = adb->GetGuidFromPath(texturePath);
        if (guid.empty()) {
            // Unregistered texture (e.g. just created) — register to mint a GUID.
            guid = adb->ImportAsset(texturePath).guid;
        }
    }

    if (guid.empty()) {
        INXLOG_WARN("Infernux::ReloadTexture: could not resolve GUID for '", texturePath,
                    "' — skipping GPU cache invalidation (GUID-only contract).");
        return;
    }

    // Retire the CPU publication without decoding the processed texture on the
    // render/UI owner thread. Material descriptors retain their last-known-good
    // GPU publication while ResolveTextureAsset asynchronously loads and
    // uploads the new revision.
    if (!guid.empty() && registry.IsLoaded(guid)) {
        registry.InvalidateAsset(guid);
    }

    // GUID-only invalidation (path fallback removed by design).
    m_renderer->InvalidateTextureCache(guid);

    // Fire graph notification so dependent materials get their pipelines
    // invalidated via the Texture Modified callback.
    {
        auto dependents = AssetDependencyGraph::Instance().GetDependents(guid);
        AssetDependencyGraph::Instance().NotifyEvent(guid, ResourceType::Texture, AssetEvent::Modified);
    }
}

void Infernux::ReloadMesh(const std::string &meshPath)
{
    // INXLOG_INFO("Infernux::ReloadMesh called: ", meshPath);

    if (!CheckEngineValid("reload mesh")) {
        INXLOG_ERROR("Infernux::ReloadMesh: engine invalid");
        return;
    }

    auto &registry = AssetRegistry::Instance();
    auto *adb = registry.GetAssetDatabase();
    std::string guid;
    if (adb)
        guid = adb->GetGuidFromPath(meshPath);

    if (guid.empty()) {
        INXLOG_WARN("Infernux::ReloadMesh: could not resolve GUID for '", meshPath, "'");
        return;
    }

    if (registry.IsLoaded(guid))
        registry.ReloadAsset(guid);

    SceneManager::Instance().MarkMeshRenderersDirtyForAsset(guid, meshPath);

    auto dependents = AssetDependencyGraph::Instance().GetDependents(guid);
    // INXLOG_INFO("Infernux::ReloadMesh: NotifyEvent guid=", guid, " dependents=", dependents.size());
    AssetDependencyGraph::Instance().NotifyEvent(guid, ResourceType::Mesh, AssetEvent::Modified);

    // INXLOG_INFO("Infernux::ReloadMesh: done for '", meshPath, "'");
}

void Infernux::ReloadAudio(const std::string &audioPath)
{
    INXLOG_INFO("Infernux::ReloadAudio called: ", audioPath);

    if (!CheckEngineValid("reload audio")) {
        INXLOG_ERROR("Infernux::ReloadAudio: engine invalid");
        return;
    }

    auto &registry = AssetRegistry::Instance();
    auto *adb = registry.GetAssetDatabase();
    std::string guid;
    if (adb)
        guid = adb->GetGuidFromPath(audioPath);

    if (guid.empty()) {
        INXLOG_WARN("Infernux::ReloadAudio: could not resolve GUID for '", audioPath, "'");
        return;
    }

    if (registry.IsLoaded(guid))
        registry.ReloadAsset(guid);

    AssetDependencyGraph::Instance().NotifyEvent(guid, ResourceType::Audio, AssetEvent::Modified);

    INXLOG_INFO("Infernux::ReloadAudio: done for '", audioPath, "'");
}

// ----------------------------------
// Debug
// ----------------------------------

void Infernux::SetLogLevel(LogLevel engineLevel)
{
    INXLOG_SET_LEVEL(engineLevel);
    m_logLevel = engineLevel;
}

// ----------------------------------
// ImGui layout save / load (Unicode-safe)
// ----------------------------------

void Infernux::ResetImGuiLayout()
{
    // Clear ImGui's in-memory ini state (windows, docking, tables)
    ImGui::ClearIniSettings();
    // Delete the persisted ini file so the reset survives a restart
    if (!m_imguiIniPath.empty() && std::filesystem::exists(m_imguiIniPath)) {
        std::filesystem::remove(m_imguiIniPath);
    }
    if (!m_imguiLayoutMetadataPath.empty() && std::filesystem::exists(m_imguiLayoutMetadataPath)) {
        std::filesystem::remove(m_imguiLayoutMetadataPath);
    }
}

void Infernux::SelectDockedWindow(const std::string &windowId, bool allowDuringModal)
{
    auto *renderer = GetRenderer();
    if (renderer == nullptr) {
        return;
    }
    renderer->QueueDockTabSelection(windowId.c_str(), allowDuringModal);
}

uint64_t Infernux::QueueSyntheticKeyInput(int scancode, bool pressed, bool repeat)
{
    auto *renderer = GetRenderer();
    return renderer ? renderer->QueueSyntheticKeyInput(scancode, pressed, repeat) : 0;
}

uint64_t Infernux::QueueSyntheticMouseButtonInput(int button, bool pressed, float x, float y)
{
    auto *renderer = GetRenderer();
    return renderer ? renderer->QueueSyntheticMouseButtonInput(button, pressed, x, y) : 0;
}

uint64_t Infernux::QueueSyntheticMouseMotionInput(float x, float y, float deltaX, float deltaY)
{
    auto *renderer = GetRenderer();
    return renderer ? renderer->QueueSyntheticMouseMotionInput(x, y, deltaX, deltaY) : 0;
}

uint64_t Infernux::QueueSyntheticMouseWheelInput(float horizontal, float vertical)
{
    auto *renderer = GetRenderer();
    return renderer ? renderer->QueueSyntheticMouseWheelInput(horizontal, vertical) : 0;
}

uint64_t Infernux::QueueSyntheticTextInput(const std::string &text)
{
    auto *renderer = GetRenderer();
    return renderer ? renderer->QueueSyntheticTextInput(text) : 0;
}

uint64_t Infernux::QueueSyntheticCloseRequest()
{
    auto *renderer = GetRenderer();
    return renderer ? renderer->QueueSyntheticCloseRequest() : 0;
}

uint64_t Infernux::GetLastProcessedSyntheticInputSequence() const
{
    auto *renderer = GetRenderer();
    return renderer ? renderer->GetLastProcessedSyntheticInputSequence() : 0;
}

size_t Infernux::GetPendingSyntheticInputCount() const
{
    auto *renderer = GetRenderer();
    return renderer ? renderer->GetPendingSyntheticInputCount() : 0;
}

void Infernux::LoadImGuiLayout()
{
    if (!std::filesystem::exists(m_imguiIniPath))
        return;
    if (!std::filesystem::exists(m_imguiLayoutMetadataPath)) {
        INXLOG_INFO("Ignoring unversioned ImGui layout; a current DPI-aware layout will be written on save");
        return;
    }

    std::ifstream metadataStream(m_imguiLayoutMetadataPath, std::ios::binary);
    if (!metadataStream.is_open())
        throw std::runtime_error("Cannot read ImGui layout metadata");

    json metadata;
    try {
        metadataStream >> metadata;
    } catch (const json::exception &error) {
        INXLOG_WARN("Ignoring invalid ImGui layout metadata: ", error.what());
        return;
    }
    if (!metadata.is_object() || metadata.value("schema", 0) != 1 || !metadata.contains("display_scale") ||
        !metadata["display_scale"].is_number()) {
        INXLOG_WARN("Ignoring ImGui layout with an unsupported metadata contract");
        return;
    }

    const float storedScale = metadata["display_scale"].get<float>();
    if (!std::isfinite(storedScale) || storedScale <= 0.0f) {
        INXLOG_WARN("Ignoring ImGui layout with an invalid display scale");
        return;
    }
    auto *renderer = GetRenderer();
    if (renderer == nullptr)
        throw std::logic_error("Cannot load ImGui layout without an initialized renderer");
    const float currentScale = renderer->GetDisplayScale();
    if (std::abs(storedScale - currentScale) >= 0.01f) {
        INXLOG_INFO("Ignoring ImGui layout authored at display scale ", storedScale, "; current display scale is ",
                    currentScale);
        return;
    }

    // std::ifstream(std::filesystem::path) uses wchar_t on Windows,
    // so paths with Chinese / non-ASCII characters are handled properly.
    std::ifstream ifs(m_imguiIniPath, std::ios::binary | std::ios::ate);
    if (!ifs.is_open())
        throw std::runtime_error("Cannot read persisted ImGui layout");
    auto size = ifs.tellg();
    if (size <= 0) {
        INXLOG_WARN("Ignoring empty ImGui layout");
        return;
    }
    ifs.seekg(0);
    std::string data(static_cast<size_t>(size), '\0');
    ifs.read(data.data(), size);
    ImGui::LoadIniSettingsFromMemory(data.c_str(), data.size());
}

void Infernux::SaveImGuiLayout()
{
    if (m_imguiIniPath.empty() || m_imguiLayoutMetadataPath.empty())
        throw std::logic_error("Cannot save ImGui layout before layout storage is initialized");
    size_t dataSize = 0;
    const char *data = ImGui::SaveIniSettingsToMemory(&dataSize);
    if (!data || dataSize == 0)
        return;
    auto *renderer = GetRenderer();
    if (renderer == nullptr)
        throw std::logic_error("Cannot save ImGui layout without an initialized renderer");
    const float displayScale = renderer->GetDisplayScale();
    std::filesystem::create_directories(m_imguiIniPath.parent_path());
    std::ofstream ofs(m_imguiIniPath, std::ios::binary);
    if (!ofs.is_open())
        throw std::runtime_error("Cannot write persisted ImGui layout");
    ofs.write(data, static_cast<std::streamsize>(dataSize));
    if (!ofs.good())
        throw std::runtime_error("Failed while writing persisted ImGui layout");

    std::ofstream metadataStream(m_imguiLayoutMetadataPath, std::ios::binary);
    if (!metadataStream.is_open())
        throw std::runtime_error("Cannot write ImGui layout metadata");
    metadataStream << json{{"schema", 1}, {"display_scale", displayScale}}.dump(2) << '\n';
    if (!metadataStream.good())
        throw std::runtime_error("Failed while writing ImGui layout metadata");
}

} // namespace infernux
