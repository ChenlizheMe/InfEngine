#include "SkinnedModelImporter.h"

#include "InxSkinnedMesh.h"

#include <assimp/Importer.hpp>
#include <assimp/postprocess.h>
#include <assimp/scene.h>
#include <platform/filesystem/InxPath.h>

#include <glm/gtc/matrix_inverse.hpp>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace infernux
{
namespace
{
glm::mat4 AiToGlm(const aiMatrix4x4 &matrix)
{
    return glm::mat4(matrix.a1, matrix.b1, matrix.c1, matrix.d1, matrix.a2, matrix.b2, matrix.c2, matrix.d2, matrix.a3,
                     matrix.b3, matrix.c3, matrix.d3, matrix.a4, matrix.b4, matrix.c4, matrix.d4);
}

glm::vec3 AiToGlm(const aiVector3D &vector)
{
    return {vector.x, vector.y, vector.z};
}

glm::quat AiToGlm(const aiQuaternion &quaternion)
{
    return {quaternion.w, quaternion.x, quaternion.y, quaternion.z};
}

bool IsFinite(const glm::vec2 &value)
{
    return std::isfinite(value.x) && std::isfinite(value.y);
}

bool IsFinite(const glm::vec3 &value)
{
    return std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z);
}

bool IsFinite(const glm::quat &value)
{
    return std::isfinite(value.w) && std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z);
}

bool IsFinite(const glm::mat4 &value)
{
    for (glm::length_t column = 0; column < value.length(); ++column) {
        for (glm::length_t row = 0; row < value[column].length(); ++row) {
            if (!std::isfinite(value[column][row]))
                return false;
        }
    }
    return true;
}

glm::vec3 NormalizeOr(const glm::vec3 &value, const glm::vec3 &fallback)
{
    if (!IsFinite(value))
        return fallback;
    const float lengthSquared = glm::dot(value, value);
    if (!std::isfinite(lengthSquared) || lengthSquared <= 1e-12f)
        return fallback;
    return value * glm::inversesqrt(lengthSquared);
}

glm::quat NormalizeOrIdentity(const glm::quat &value)
{
    if (!IsFinite(value))
        return glm::quat(1.0f, 0.0f, 0.0f, 0.0f);
    const float lengthSquared = glm::dot(value, value);
    if (!std::isfinite(lengthSquared) || lengthSquared <= 1e-12f)
        return glm::quat(1.0f, 0.0f, 0.0f, 0.0f);
    return value * glm::inversesqrt(lengthSquared);
}

unsigned int BuildAssimpFlags()
{
    return aiProcess_Triangulate | aiProcess_GenSmoothNormals | aiProcess_CalcTangentSpace | aiProcess_FlipUVs |
           aiProcess_JoinIdenticalVertices | aiProcess_SortByPType | aiProcess_ValidateDataStructure |
           aiProcess_ImproveCacheLocality;
}

void CollectNodes(const aiNode &node, int parent, const glm::mat4 &parentGlobal, InxSkinnedMesh &output)
{
    const int index = static_cast<int>(output.skeleton.nodes.size());
    SkinnedRuntimeNode runtimeNode;
    runtimeNode.name = node.mName.C_Str();
    runtimeNode.parent = parent;
    runtimeNode.bindLocal = AiToGlm(node.mTransformation);
    runtimeNode.bindGlobal = parentGlobal * runtimeNode.bindLocal;
    if (!IsFinite(runtimeNode.bindLocal) || !IsFinite(runtimeNode.bindGlobal))
        throw std::runtime_error("Skinned model contains a non-finite node transform: " + runtimeNode.name);
    if (runtimeNode.name.empty() || !output.skeleton.nodeByName.emplace(runtimeNode.name, index).second)
        throw std::runtime_error("Skinned model contains an empty or duplicate node name");
    output.skeleton.nodes.push_back(runtimeNode);

    for (unsigned int childIndex = 0; childIndex < node.mNumChildren; ++childIndex) {
        if (!node.mChildren[childIndex])
            throw std::runtime_error("Skinned model scene contains a null node child");
        CollectNodes(*node.mChildren[childIndex], index, runtimeNode.bindGlobal, output);
    }
}

void CollectMeshNodes(const aiNode &node, const InxSkinnedMesh &model, std::vector<std::pair<uint32_t, int>> &output)
{
    int nodeIndex = -1;
    const auto found = model.skeleton.nodeByName.find(node.mName.C_Str());
    if (found != model.skeleton.nodeByName.end())
        nodeIndex = found->second;
    for (unsigned int meshIndex = 0; meshIndex < node.mNumMeshes; ++meshIndex)
        output.push_back({node.mMeshes[meshIndex], nodeIndex});
    for (unsigned int childIndex = 0; childIndex < node.mNumChildren; ++childIndex)
        CollectMeshNodes(*node.mChildren[childIndex], model, output);
}

void AddInfluence(SkinInfluence &influence, uint32_t boneIndex, float weight)
{
    if (!std::isfinite(weight) || weight <= 0.0f)
        return;
    for (uint32_t index = 0; index < kMaxSkinInfluences; ++index) {
        if (influence.weight[index] <= 0.0f) {
            influence.boneIndex[index] = boneIndex;
            influence.weight[index] = weight;
            return;
        }
    }
    uint32_t lightest = 0;
    for (uint32_t index = 1; index < kMaxSkinInfluences; ++index) {
        if (influence.weight[index] < influence.weight[lightest])
            lightest = index;
    }
    if (weight > influence.weight[lightest]) {
        influence.boneIndex[lightest] = boneIndex;
        influence.weight[lightest] = weight;
    }
}

bool MatricesNearlyEqual(const glm::mat4 &lhs, const glm::mat4 &rhs, float epsilon = 1e-3f)
{
    for (glm::length_t column = 0; column < 4; ++column) {
        for (glm::length_t row = 0; row < 4; ++row) {
            if (std::abs(lhs[column][row] - rhs[column][row]) > epsilon)
                return false;
        }
    }
    return true;
}

uint32_t GetOrCreateBone(InxSkinnedMesh &model, const aiBone &source, const glm::mat4 &inverseBind, uint32_t meshIndex)
{
    const std::string sourceName = source.mName.C_Str();
    if (sourceName.empty())
        throw std::runtime_error("Skinned model contains a bone without a name");
    if (!IsFinite(inverseBind))
        throw std::runtime_error("Skinned model contains a non-finite inverse bind transform for bone: " + sourceName);

    const auto node = model.skeleton.nodeByName.find(sourceName);
    const int nodeIndex = node != model.skeleton.nodeByName.end() ? node->second : -1;
    const std::string meshAliasPrefix = sourceName + "__mesh_";
    for (uint32_t index = 0; index < model.skeleton.bones.size(); ++index) {
        const auto &existing = model.skeleton.bones[index];
        const bool sameSourceBone =
            existing.name == sourceName || existing.name.compare(0, meshAliasPrefix.size(), meshAliasPrefix) == 0;
        if (sameSourceBone && existing.nodeIndex == nodeIndex && MatricesNearlyEqual(existing.inverseBind, inverseBind))
            return index;
    }

    std::string runtimeName = sourceName;
    if (model.skeleton.boneByName.find(runtimeName) != model.skeleton.boneByName.end()) {
        runtimeName += "__mesh_" + std::to_string(meshIndex);
        for (uint32_t suffix = 1; model.skeleton.boneByName.find(runtimeName) != model.skeleton.boneByName.end();
             ++suffix)
            runtimeName = sourceName + "__mesh_" + std::to_string(meshIndex) + "_" + std::to_string(suffix);
    }

    SkinnedRuntimeBone bone;
    bone.name = runtimeName;
    bone.nodeIndex = nodeIndex;
    bone.inverseBind = inverseBind;
    const uint32_t index = static_cast<uint32_t>(model.skeleton.bones.size());
    model.skeleton.boneByName.emplace(runtimeName, index);
    model.skeleton.bones.push_back(std::move(bone));
    return index;
}

uint32_t GetOrCreateMeshNodeFallbackBone(InxSkinnedMesh &model, int nodeIndex, const glm::mat4 &inverseBind)
{
    const std::string name = "__mesh_node_fallback_" + std::to_string(nodeIndex);
    const auto found = model.skeleton.boneByName.find(name);
    if (found != model.skeleton.boneByName.end())
        return found->second;
    SkinnedRuntimeBone bone;
    bone.name = name;
    bone.nodeIndex = nodeIndex;
    bone.inverseBind = inverseBind;
    const uint32_t index = static_cast<uint32_t>(model.skeleton.bones.size());
    model.skeleton.boneByName.emplace(name, index);
    model.skeleton.bones.push_back(std::move(bone));
    return index;
}

bool HasInfluence(const SkinInfluence &influence)
{
    for (const float weight : influence.weight) {
        if (weight > 1e-6f)
            return true;
    }
    return false;
}
} // namespace

bool SkinnedModelImporter::HasSkinningData(const aiScene &scene) noexcept
{
    if (scene.mNumAnimations > 0)
        return true;
    for (unsigned int index = 0; index < scene.mNumMeshes; ++index) {
        if (scene.mMeshes[index] && scene.mMeshes[index]->mNumBones > 0)
            return true;
    }
    return false;
}

std::shared_ptr<InxSkinnedMesh> SkinnedModelImporter::ConvertScene(const aiScene &scene, const std::string &sourceGuid,
                                                                   const std::string &sourcePath, float scaleFactor)
{
    if (!scene.mRootNode)
        throw std::invalid_argument("Skinned model scene has no root node");
    if (!std::isfinite(scaleFactor) || scaleFactor <= 0.0f)
        throw std::invalid_argument("Skinned model scale factor must be finite and positive");

    auto model = std::make_shared<InxSkinnedMesh>();
    model->sourcePath = sourcePath;
    model->guid = sourceGuid;
    model->scaleFactor = scaleFactor;
    CollectNodes(*scene.mRootNode, -1, glm::mat4(1.0f), *model);

    std::vector<std::pair<uint32_t, int>> meshNodes;
    CollectMeshNodes(*scene.mRootNode, *model, meshNodes);
    std::unordered_map<unsigned int, uint32_t> materialSlots;
    std::unordered_map<int, uint32_t> nodeGroups;
    uint32_t vertexOffset = 0;
    uint32_t indexOffset = 0;
    for (const auto &[meshIndex, nodeIndex] : meshNodes) {
        if (meshIndex >= scene.mNumMeshes || !scene.mMeshes[meshIndex])
            throw std::runtime_error("Skinned model node references an invalid mesh");
        const aiMesh &sourceMesh = *scene.mMeshes[meshIndex];
        if (!(sourceMesh.mPrimitiveTypes & aiPrimitiveType_TRIANGLE))
            continue;

        const uint32_t vertexStart = vertexOffset;
        const uint32_t indexStart = indexOffset;
        const glm::mat4 meshToModel =
            nodeIndex >= 0 ? model->skeleton.nodes[static_cast<size_t>(nodeIndex)].bindGlobal : glm::mat4(1.0f);
        if (!IsFinite(meshToModel) || std::abs(glm::determinant(glm::mat3(meshToModel))) <= 1e-12f)
            throw std::runtime_error("Skinned model mesh node has a non-invertible bind transform: " +
                                     std::string(sourceMesh.mName.C_Str()));
        const glm::mat4 modelToMesh = glm::inverse(meshToModel);
        const glm::mat3 normalToModel = glm::inverseTranspose(glm::mat3(meshToModel));
        const bool hasNormals = sourceMesh.HasNormals();
        const bool hasTangents = sourceMesh.HasTangentsAndBitangents();
        const bool hasUvs = sourceMesh.HasTextureCoords(0);
        const bool hasColors = sourceMesh.HasVertexColors(0);
        for (unsigned int vertexIndex = 0; vertexIndex < sourceMesh.mNumVertices; ++vertexIndex) {
            Vertex vertex{};
            vertex.pos = glm::vec3(meshToModel * glm::vec4(AiToGlm(sourceMesh.mVertices[vertexIndex]), 1.0f));
            if (!IsFinite(vertex.pos))
                throw std::runtime_error("Skinned model contains a non-finite vertex position");
            vertex.normal = hasNormals ? NormalizeOr(normalToModel * AiToGlm(sourceMesh.mNormals[vertexIndex]),
                                                     glm::vec3(0.0f, 1.0f, 0.0f))
                                       : glm::vec3(0.0f, 1.0f, 0.0f);
            vertex.tangent = hasTangents
                                 ? glm::vec4(NormalizeOr(normalToModel * AiToGlm(sourceMesh.mTangents[vertexIndex]),
                                                         glm::vec3(1.0f, 0.0f, 0.0f)),
                                             1.0f)
                                 : glm::vec4(1.0f, 0.0f, 0.0f, 1.0f);
            if (hasUvs) {
                vertex.texCoord = {sourceMesh.mTextureCoords[0][vertexIndex].x,
                                   sourceMesh.mTextureCoords[0][vertexIndex].y};
                if (!IsFinite(vertex.texCoord))
                    vertex.texCoord = glm::vec2(0.0f);
            }
            vertex.color = hasColors
                               ? glm::vec3(sourceMesh.mColors[0][vertexIndex].r, sourceMesh.mColors[0][vertexIndex].g,
                                           sourceMesh.mColors[0][vertexIndex].b)
                               : glm::vec3(1.0f);
            if (!IsFinite(vertex.color))
                vertex.color = glm::vec3(1.0f);
            model->baseVertices.push_back(vertex);
            model->influences.push_back({});
        }

        for (unsigned int boneIndex = 0; boneIndex < sourceMesh.mNumBones; ++boneIndex) {
            if (!sourceMesh.mBones[boneIndex])
                throw std::runtime_error("Skinned model mesh contains a null bone");
            const aiBone &sourceBone = *sourceMesh.mBones[boneIndex];
            const glm::mat4 inverseBind = AiToGlm(sourceBone.mOffsetMatrix) * modelToMesh;
            const uint32_t runtimeBone = GetOrCreateBone(*model, sourceBone, inverseBind, meshIndex);
            for (unsigned int weightIndex = 0; weightIndex < sourceBone.mNumWeights; ++weightIndex) {
                const aiVertexWeight &weight = sourceBone.mWeights[weightIndex];
                if (weight.mVertexId >= sourceMesh.mNumVertices)
                    throw std::runtime_error("Skinned model bone weight references an invalid vertex");
                AddInfluence(model->influences[vertexStart + weight.mVertexId], runtimeBone, weight.mWeight);
            }
        }

        if (nodeIndex >= 0) {
            uint32_t fallbackBone = 0;
            bool createdFallback = false;
            for (unsigned int vertexIndex = 0; vertexIndex < sourceMesh.mNumVertices; ++vertexIndex) {
                SkinInfluence &influence = model->influences[vertexStart + vertexIndex];
                if (!HasInfluence(influence)) {
                    if (!createdFallback) {
                        fallbackBone = GetOrCreateMeshNodeFallbackBone(*model, nodeIndex, modelToMesh);
                        createdFallback = true;
                    }
                    AddInfluence(influence, fallbackBone, 1.0f);
                }
            }
        }

        for (unsigned int faceIndex = 0; faceIndex < sourceMesh.mNumFaces; ++faceIndex) {
            const aiFace &face = sourceMesh.mFaces[faceIndex];
            if (face.mNumIndices != 3)
                throw std::runtime_error("Skinned model contains a non-triangle face after triangulation");
            for (unsigned int component = 0; component < face.mNumIndices; ++component) {
                if (face.mIndices[component] >= sourceMesh.mNumVertices)
                    throw std::runtime_error("Skinned model face references an invalid vertex");
                model->indices.push_back(face.mIndices[component] + vertexStart);
                ++indexOffset;
            }
        }

        const auto [slot, inserted] =
            materialSlots.try_emplace(sourceMesh.mMaterialIndex, static_cast<uint32_t>(materialSlots.size()));
        (void)inserted;
        SubMesh subMesh;
        subMesh.indexStart = indexStart;
        subMesh.indexCount = indexOffset - indexStart;
        subMesh.vertexStart = vertexStart;
        subMesh.vertexCount = sourceMesh.mNumVertices;
        subMesh.materialSlot = slot->second;
        // InxMesh assigns compact node groups only to nodes that own meshes.
        // Keep that exact contract here: the full skeleton node index is not a
        // renderer group and may include hundreds of intervening bone nodes.
        const auto [nodeGroup, groupInserted] =
            nodeGroups.try_emplace(nodeIndex, static_cast<uint32_t>(nodeGroups.size()));
        (void)groupInserted;
        subMesh.nodeGroup = nodeGroup->second;
        subMesh.name = sourceMesh.mName.C_Str();
        if (subMesh.vertexCount > 0) {
            subMesh.boundsMin = glm::vec3(std::numeric_limits<float>::max());
            subMesh.boundsMax = glm::vec3(std::numeric_limits<float>::lowest());
            for (uint32_t index = subMesh.vertexStart; index < subMesh.vertexStart + subMesh.vertexCount; ++index) {
                subMesh.boundsMin = glm::min(subMesh.boundsMin, model->baseVertices[index].pos);
                subMesh.boundsMax = glm::max(subMesh.boundsMax, model->baseVertices[index].pos);
            }
        }
        model->subMeshes.push_back(std::move(subMesh));
        vertexOffset += sourceMesh.mNumVertices;
    }
    model->NormalizeInfluences();

    std::unordered_set<std::string> animationNames;
    for (unsigned int animationIndex = 0; animationIndex < scene.mNumAnimations; ++animationIndex) {
        if (!scene.mAnimations[animationIndex])
            throw std::runtime_error("Skinned model scene contains a null animation");
        const aiAnimation &sourceAnimation = *scene.mAnimations[animationIndex];
        SkinnedRuntimeAnimation animation;
        animation.name = sourceAnimation.mName.C_Str();
        if (animation.name.empty())
            animation.name = "Anim_" + std::to_string(animationIndex);
        if (!animationNames.insert(animation.name).second)
            throw std::runtime_error("Skinned model contains duplicate animation names: " + animation.name);
        animation.durationTicks = std::isfinite(sourceAnimation.mDuration) && sourceAnimation.mDuration >= 0.0
                                      ? sourceAnimation.mDuration
                                      : 0.0;
        animation.ticksPerSecond =
            std::isfinite(sourceAnimation.mTicksPerSecond) && sourceAnimation.mTicksPerSecond > 0.0
                ? sourceAnimation.mTicksPerSecond
                : 25.0;
        animation.tracks.reserve(sourceAnimation.mNumChannels);
        animation.trackByNodeIndex.assign(model->skeleton.nodes.size(), -1);
        for (unsigned int channelIndex = 0; channelIndex < sourceAnimation.mNumChannels; ++channelIndex) {
            if (!sourceAnimation.mChannels[channelIndex])
                throw std::runtime_error("Skinned model animation contains a null channel");
            const aiNodeAnim &channel = *sourceAnimation.mChannels[channelIndex];
            SkinnedRuntimeTrack track;
            const std::string channelNodeName = channel.mNodeName.C_Str();
            const auto sourceNode = model->skeleton.nodeByName.find(channelNodeName);
            if (channelNodeName.empty() || sourceNode == model->skeleton.nodeByName.end())
                throw std::runtime_error("Skinned model animation targets a node outside its source skeleton");
            track.nodeIndex = sourceNode->second;
            if (animation.trackByNodeIndex[static_cast<size_t>(track.nodeIndex)] >= 0)
                throw std::runtime_error("Skinned model animation contains an invalid duplicate node track");
            track.positions.reserve(channel.mNumPositionKeys);
            track.rotations.reserve(channel.mNumRotationKeys);
            track.scales.reserve(channel.mNumScalingKeys);
            for (unsigned int key = 0; key < channel.mNumPositionKeys; ++key) {
                const auto value = AiToGlm(channel.mPositionKeys[key].mValue);
                if (std::isfinite(channel.mPositionKeys[key].mTime) && IsFinite(value))
                    track.positions.push_back({channel.mPositionKeys[key].mTime, value});
            }
            for (unsigned int key = 0; key < channel.mNumRotationKeys; ++key) {
                const auto value = AiToGlm(channel.mRotationKeys[key].mValue);
                if (std::isfinite(channel.mRotationKeys[key].mTime) && IsFinite(value))
                    track.rotations.push_back({channel.mRotationKeys[key].mTime, NormalizeOrIdentity(value)});
            }
            for (unsigned int key = 0; key < channel.mNumScalingKeys; ++key) {
                const auto value = AiToGlm(channel.mScalingKeys[key].mValue);
                if (std::isfinite(channel.mScalingKeys[key].mTime) && IsFinite(value))
                    track.scales.push_back({channel.mScalingKeys[key].mTime, value});
            }
            animation.trackByNodeIndex[static_cast<size_t>(track.nodeIndex)] =
                static_cast<int>(animation.tracks.size());
            animation.tracks.push_back(std::move(track));
        }
        model->animations.push_back(std::move(animation));
    }
    if (!model->IsAssetPayloadValid())
        throw std::runtime_error("Skinned model conversion produced neither renderable geometry nor animation data");
    return model;
}

std::shared_ptr<InxSkinnedMesh> SkinnedModelImporter::ImportSource(const std::string &sourceGuid,
                                                                   const std::string &sourcePath, float scaleFactor)
{
    const auto filePath = ToFsPath(sourcePath);
    if (!std::filesystem::is_regular_file(filePath))
        throw std::runtime_error("Skinned model source file not found: " + sourcePath);
    std::ifstream file(filePath, std::ios::binary | std::ios::ate);
    if (!file.is_open())
        throw std::runtime_error("Skinned model source file cannot be opened: " + sourcePath);
    const auto size = file.tellg();
    if (size <= 0)
        throw std::runtime_error("Skinned model source file is empty: " + sourcePath);
    std::vector<char> bytes(static_cast<size_t>(size));
    file.seekg(0);
    if (!file.read(bytes.data(), size))
        throw std::runtime_error("Skinned model source file cannot be read: " + sourcePath);

    std::string extension = FromFsPath(filePath.extension());
    if (!extension.empty() && extension.front() == '.')
        extension.erase(extension.begin());
    std::transform(extension.begin(), extension.end(), extension.begin(),
                   [](unsigned char character) { return static_cast<char>(std::tolower(character)); });
    Assimp::Importer importer;
    const aiScene *scene =
        importer.ReadFileFromMemory(bytes.data(), bytes.size(), BuildAssimpFlags(), extension.c_str());
    if (!scene || (scene->mFlags & AI_SCENE_FLAGS_INCOMPLETE) || !scene->mRootNode)
        throw std::runtime_error("Skinned model Assimp import failed for '" + sourcePath +
                                 "': " + importer.GetErrorString());
    return ConvertScene(*scene, sourceGuid, sourcePath, scaleFactor);
}

} // namespace infernux
