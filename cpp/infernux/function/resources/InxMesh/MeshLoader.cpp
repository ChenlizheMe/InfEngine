/**
 * @file MeshLoader.cpp
 * @brief Assimp-based mesh loading for Infernux's AssetRegistry.
 *
 * Converts Assimp's aiScene into the engine's InxMesh representation.
 * All aiMesh nodes are collected via a recursive scene-graph traversal
 * and merged into a single vertex/index buffer with per-submesh offsets.
 */

#include "MeshLoader.h"
#include "InxMesh.h"
#include "MeshArtifact.h"

#include <core/config/MathConstants.h>
#include <core/log/InxLog.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/InxResource/InxResourceMeta.h>
#include <function/resources/InxSkinnedMesh/InxSkinnedMesh.h>
#include <function/resources/InxSkinnedMesh/SkinnedMeshArtifact.h>
#include <function/resources/InxSkinnedMesh/SkinnedModelImporter.h>

#include <assimp/Importer.hpp>
#include <assimp/postprocess.h>
#include <assimp/scene.h>

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <platform/filesystem/InxPath.h>
#include <unordered_map>
#include <unordered_set>

namespace infernux
{

namespace
{
std::string ReadArtifactBytes(const std::string &path, std::string_view label)
{
    std::ifstream file(ToFsPath(path), std::ios::binary | std::ios::ate);
    if (!file.is_open())
        throw std::runtime_error("failed to open " + std::string(label));
    const auto size = file.tellg();
    if (size <= 0)
        throw std::runtime_error(std::string(label) + " is empty");
    std::string bytes(static_cast<size_t>(size), '\0');
    file.seekg(0);
    if (!file.read(bytes.data(), size))
        throw std::runtime_error("failed to read " + std::string(label));
    return bytes;
}
} // namespace

// ============================================================================
// Import-setting helpers
// ============================================================================

struct MeshImportSettings
{
    float scaleFactor = 1.0f;
    bool generateNormals = true;
    bool generateTangents = true;
    bool flipUVs = true;
    bool swapUVChannels = false;
    bool optimizeMesh = true;
};

static MeshImportSettings ReadImportSettings(const InxResourceMeta &meta)
{
    MeshImportSettings settings;
    if (meta.HasKey("scale_factor"))
        settings.scaleFactor = meta.GetDataAs<float>("scale_factor");
    if (meta.HasKey("generate_normals"))
        settings.generateNormals = meta.GetDataAs<bool>("generate_normals");
    if (meta.HasKey("generate_tangents"))
        settings.generateTangents = meta.GetDataAs<bool>("generate_tangents");
    if (meta.HasKey("flip_uvs"))
        settings.flipUVs = meta.GetDataAs<bool>("flip_uvs");
    if (meta.HasKey("swap_uv_channels"))
        settings.swapUVChannels = meta.GetDataAs<bool>("swap_uv_channels");
    if (meta.HasKey("optimize_mesh"))
        settings.optimizeMesh = meta.GetDataAs<bool>("optimize_mesh");

    return settings;
}

static unsigned int BuildAssimpFlags(const MeshImportSettings &settings)
{
    unsigned int flags = aiProcess_Triangulate;

    if (settings.generateNormals)
        flags |= aiProcess_GenSmoothNormals;
    if (settings.generateTangents)
        flags |= aiProcess_CalcTangentSpace;
    if (settings.flipUVs)
        flags |= aiProcess_FlipUVs;
    // NOTE: aiProcess_OptimizeMeshes and aiProcess_OptimizeGraph are intentionally
    // omitted — they merge meshes across different Assimp nodes, destroying the
    // per-object hierarchy needed for correct scene object splitting.
    (void)settings.optimizeMesh;

    // Always apply these for correctness:
    flags |= aiProcess_JoinIdenticalVertices; // Weld duplicate vertices
    flags |= aiProcess_SortByPType;           // Separate points/lines from triangles
    flags |= aiProcess_ValidateDataStructure; // Sanity check
    flags |= aiProcess_ImproveCacheLocality;  // GPU vertex-cache friendly ordering

    return flags;
}

// ============================================================================
// Scene traversal — collect all meshes from the node hierarchy
// ============================================================================

/**
 * @brief Recursively traverse the Assimp node tree and collect mesh indices
 *        along with their accumulated world transform.
 *
 * Each node in an Assimp scene has a local transform and references zero or
 * more meshes by index.  We walk the tree depth-first, accumulating the
 * transform chain, so that every mesh's geometry is placed correctly in
 * model space.
 */
struct CollectedMesh
{
    uint32_t meshIndex;       ///< Index into aiScene::mMeshes
    glm::mat4 worldTransform; ///< Accumulated node transform
    uint32_t nodeGroup;       ///< Source node group (for per-object splitting)
};

static glm::mat4 AiToGlm(const aiMatrix4x4 &m)
{
    // Assimp stores column-major internally, but the aiMatrix4x4 API
    // exposes row-major accessors.  GLM is column-major.
    return glm::mat4(m.a1, m.b1, m.c1, m.d1, // col 0
                     m.a2, m.b2, m.c2, m.d2, // col 1
                     m.a3, m.b3, m.c3, m.d3, // col 2
                     m.a4, m.b4, m.c4, m.d4  // col 3
    );
}

static void CollectMeshes(const aiNode *node, const glm::mat4 &parentTransform, std::vector<CollectedMesh> &outMeshes,
                          std::vector<std::string> &outNodeNames)
{
    glm::mat4 nodeTransform = parentTransform * AiToGlm(node->mTransformation);

    if (node->mNumMeshes > 0) {
        uint32_t group = static_cast<uint32_t>(outNodeNames.size());
        outNodeNames.push_back(node->mName.C_Str());
        for (unsigned int i = 0; i < node->mNumMeshes; ++i) {
            outMeshes.push_back({node->mMeshes[i], nodeTransform, group});
        }
    }

    for (unsigned int i = 0; i < node->mNumChildren; ++i) {
        CollectMeshes(node->mChildren[i], nodeTransform, outMeshes, outNodeNames);
    }
}

// ============================================================================
// Core conversion: aiScene → InxMesh
// ============================================================================

static std::shared_ptr<InxMesh> ConvertScene(const aiScene *scene, const MeshImportSettings &settings,
                                             const std::string &name)
{
    auto mesh = std::make_shared<InxMesh>(name);

    // Collect all mesh instances with their transforms and node grouping
    std::vector<CollectedMesh> collectedMeshes;
    std::vector<std::string> nodeNames;
    collectedMeshes.reserve(scene->mNumMeshes);
    CollectMeshes(scene->mRootNode, glm::mat4(1.0f), collectedMeshes, nodeNames);

    if (collectedMeshes.empty()) {
        INXLOG_WARN("MeshLoader: scene '", name, "' contains no meshes");
        return mesh;
    }

    // Pre-calculate total counts for a single allocation
    uint32_t totalVertices = 0;
    uint32_t totalIndices = 0;
    for (const auto &cm : collectedMeshes) {
        const aiMesh *aiM = scene->mMeshes[cm.meshIndex];
        totalVertices += aiM->mNumVertices;
        for (unsigned int f = 0; f < aiM->mNumFaces; ++f)
            totalIndices += aiM->mFaces[f].mNumIndices;
    }

    std::vector<Vertex> vertices;
    std::vector<uint32_t> indices;
    std::vector<SubMesh> subMeshes;
    vertices.reserve(totalVertices);
    indices.reserve(totalIndices);
    subMeshes.reserve(collectedMeshes.size());

    // Deduplicate material slot assignments:
    // Assimp's material indices are per-aiScene.  Multiple aiMeshes can
    // share the same material index → same slot.
    std::unordered_map<unsigned int, uint32_t> aiMatToSlot;
    std::vector<std::string> materialSlotNames;
    std::vector<MaterialSlotData> materialSlotDataVec;

    uint32_t currentVertexOffset = 0;
    uint32_t currentIndexOffset = 0;

    const float scale = settings.scaleFactor;
    const bool applyScale = std::abs(scale - 1.0f) > kEpsilon;

    for (const auto &cm : collectedMeshes) {
        const aiMesh *aiM = scene->mMeshes[cm.meshIndex];

        // Skip non-triangle primitives (points, lines)
        if (!(aiM->mPrimitiveTypes & aiPrimitiveType_TRIANGLE))
            continue;

        const bool hasNormals = aiM->HasNormals();
        const bool hasTangents = aiM->HasTangentsAndBitangents();
        const bool hasUV0 = aiM->HasTextureCoords(0);
        const bool hasUV1 = aiM->HasTextureCoords(1);
        const bool swapUVs = settings.swapUVChannels && hasUV1;
        const bool hasUVs = swapUVs ? hasUV1 : hasUV0;
        const bool hasColors = aiM->HasVertexColors(0);

        // Compute normal matrix from the world transform (no scale skew for normals)
        const glm::mat4 &xform = cm.worldTransform;
        const glm::mat3 normalMatrix = glm::transpose(glm::inverse(glm::mat3(xform)));

        // ── Vertices ────────────────────────────────────────────────
        for (unsigned int v = 0; v < aiM->mNumVertices; ++v) {
            Vertex vert{};

            // Position: apply node transform, then uniform scale
            glm::vec3 pos(aiM->mVertices[v].x, aiM->mVertices[v].y, aiM->mVertices[v].z);
            glm::vec4 worldPos = xform * glm::vec4(pos, 1.0f);
            vert.pos = glm::vec3(worldPos);
            if (applyScale)
                vert.pos *= scale;

            // Normal
            if (hasNormals) {
                glm::vec3 n(aiM->mNormals[v].x, aiM->mNormals[v].y, aiM->mNormals[v].z);
                vert.normal = glm::normalize(normalMatrix * n);
            }

            // Tangent + bitangent handedness
            if (hasTangents) {
                glm::vec3 t(aiM->mTangents[v].x, aiM->mTangents[v].y, aiM->mTangents[v].z);
                glm::vec3 worldT = glm::normalize(glm::mat3(xform) * t);

                glm::vec3 b(aiM->mBitangents[v].x, aiM->mBitangents[v].y, aiM->mBitangents[v].z);
                glm::vec3 worldB = glm::normalize(glm::mat3(xform) * b);

                // Compute handedness: sign of dot(cross(N,T), B)
                float handedness = (glm::dot(glm::cross(vert.normal, worldT), worldB) < 0.0f) ? -1.0f : 1.0f;
                vert.tangent = glm::vec4(worldT, handedness);
            } else {
                vert.tangent = glm::vec4(1.0f, 0.0f, 0.0f, 1.0f);
            }

            // UV (channel 0 only for now)
            if (hasUVs) {
                const unsigned int uvChannel = swapUVs ? 1u : 0u;
                vert.texCoord = glm::vec2(aiM->mTextureCoords[uvChannel][v].x, aiM->mTextureCoords[uvChannel][v].y);
            } else {
                // Auto-generate UV via triplanar-dominant-axis projection
                // for meshes that have no texture coordinates at all.
                const glm::vec3 &p = vert.pos;
                const glm::vec3 n = hasNormals ? vert.normal : glm::vec3(0.0f, 1.0f, 0.0f);
                const glm::vec3 absN = glm::abs(n);
                if (absN.x >= absN.y && absN.x >= absN.z)
                    vert.texCoord = glm::vec2(p.z, p.y); // project along X
                else if (absN.y >= absN.x && absN.y >= absN.z)
                    vert.texCoord = glm::vec2(p.x, p.z); // project along Y
                else
                    vert.texCoord = glm::vec2(p.x, p.y); // project along Z
            }

            // Vertex colour
            if (hasColors) {
                vert.color = glm::vec3(aiM->mColors[0][v].r, aiM->mColors[0][v].g, aiM->mColors[0][v].b);
            } else {
                vert.color = glm::vec3(1.0f);
            }

            vertices.push_back(vert);
        }

        // ── Indices ─────────────────────────────────────────────────
        uint32_t submeshIndexStart = currentIndexOffset;
        for (unsigned int f = 0; f < aiM->mNumFaces; ++f) {
            const aiFace &face = aiM->mFaces[f];
            for (unsigned int idx = 0; idx < face.mNumIndices; ++idx) {
                indices.push_back(face.mIndices[idx] + currentVertexOffset);
            }
            currentIndexOffset += face.mNumIndices;
        }

        // ── Material slot mapping ───────────────────────────────────
        uint32_t slot = 0;
        auto it = aiMatToSlot.find(aiM->mMaterialIndex);
        if (it != aiMatToSlot.end()) {
            slot = it->second;
        } else {
            slot = static_cast<uint32_t>(materialSlotNames.size());
            aiMatToSlot[aiM->mMaterialIndex] = slot;

            // Extract material name from Assimp
            std::string matName;
            MaterialSlotData slotData;
            if (aiM->mMaterialIndex < scene->mNumMaterials) {
                const aiMaterial *aiMat = scene->mMaterials[aiM->mMaterialIndex];
                aiString aiName;
                aiMat->Get(AI_MATKEY_NAME, aiName);
                matName = aiName.C_Str();

                // Diffuse / base colour
                aiColor4D diffuse;
                if (aiMat->Get(AI_MATKEY_COLOR_DIFFUSE, diffuse) == AI_SUCCESS) {
                    slotData.baseColor = glm::vec4(diffuse.r, diffuse.g, diffuse.b, diffuse.a);
                }
                // Try PBR base color as override (glTF workflow)
                aiColor4D pbrBase;
                if (aiMat->Get(AI_MATKEY_BASE_COLOR, pbrBase) == AI_SUCCESS) {
                    slotData.baseColor = glm::vec4(pbrBase.r, pbrBase.g, pbrBase.b, pbrBase.a);
                }
                // Emission colour
                aiColor4D emission;
                if (aiMat->Get(AI_MATKEY_COLOR_EMISSIVE, emission) == AI_SUCCESS) {
                    slotData.emissionColor = glm::vec4(emission.r, emission.g, emission.b, emission.a);
                }
                // Metallic factor
                float metallic = 0.0f;
                if (aiMat->Get(AI_MATKEY_METALLIC_FACTOR, metallic) == AI_SUCCESS) {
                    slotData.metallic = metallic;
                }
                // Roughness → smoothness
                float roughness = 0.5f;
                if (aiMat->Get(AI_MATKEY_ROUGHNESS_FACTOR, roughness) == AI_SUCCESS) {
                    slotData.smoothness = 1.0f - roughness;
                }
                // Opacity
                float opacity = 1.0f;
                if (aiMat->Get(AI_MATKEY_OPACITY, opacity) == AI_SUCCESS) {
                    slotData.opacity = opacity;
                    slotData.baseColor.a *= opacity;
                }
            }
            if (matName.empty())
                matName = "Material_" + std::to_string(slot);
            materialSlotNames.push_back(matName);
            materialSlotDataVec.push_back(slotData);
        }

        // ── SubMesh ─────────────────────────────────────────────────
        SubMesh sub;
        sub.indexStart = submeshIndexStart;
        sub.indexCount = currentIndexOffset - submeshIndexStart;
        sub.vertexStart = currentVertexOffset;
        sub.vertexCount = aiM->mNumVertices;
        sub.materialSlot = slot;
        sub.nodeGroup = cm.nodeGroup;
        sub.name = aiM->mName.C_Str();

        // Compute per-submesh AABB from the vertices we just added
        if (sub.vertexCount > 0) {
            constexpr float INF = std::numeric_limits<float>::max();
            sub.boundsMin = glm::vec3(INF);
            sub.boundsMax = glm::vec3(-INF);
            for (uint32_t vi = sub.vertexStart; vi < sub.vertexStart + sub.vertexCount; ++vi) {
                sub.boundsMin = glm::min(sub.boundsMin, vertices[vi].pos);
                sub.boundsMax = glm::max(sub.boundsMax, vertices[vi].pos);
            }
        }

        subMeshes.push_back(std::move(sub));
        currentVertexOffset += aiM->mNumVertices;
    }

    mesh->SetData(std::move(vertices), std::move(indices), std::move(subMeshes));
    mesh->SetMaterialSlotNames(std::move(materialSlotNames));
    mesh->SetMaterialSlotData(std::move(materialSlotDataVec));
    mesh->SetNodeNames(std::move(nodeNames));

    return mesh;
}

// ============================================================================
// IAssetLoader interface
// ============================================================================

MeshSourceImportResult MeshLoader::ImportSourceDetailed(const std::string &filePath, const std::string &guid,
                                                        const InxResourceMeta &metadata)
{
    auto fsPath = ToFsPath(filePath);
    if (!std::filesystem::is_regular_file(fsPath))
        throw std::runtime_error("MeshLoader source file not found: " + filePath);

    // Read file into memory to avoid Assimp's narrow-string path issues on Windows
    std::ifstream file(fsPath, std::ios::binary | std::ios::ate);
    if (!file.is_open())
        throw std::runtime_error("MeshLoader cannot open source file: " + filePath);
    auto fileSize = file.tellg();
    if (fileSize <= 0)
        throw std::runtime_error("MeshLoader source file is empty or unreadable: " + filePath);
    std::vector<char> fileData(static_cast<size_t>(fileSize));
    file.seekg(0);
    if (!file.read(fileData.data(), fileSize))
        throw std::runtime_error("MeshLoader failed to read source file: " + filePath);

    MeshImportSettings settings = ReadImportSettings(metadata);
    unsigned int flags = BuildAssimpFlags(settings);

    // Derive extension hint for Assimp (e.g. "fbx")
    std::string ext = FromFsPath(fsPath.extension());
    if (!ext.empty() && ext[0] == '.')
        ext = ext.substr(1);
    std::transform(ext.begin(), ext.end(), ext.begin(),
                   [](unsigned char character) { return static_cast<char>(std::tolower(character)); });

    Assimp::Importer importer;
    const aiScene *scene = importer.ReadFileFromMemory(fileData.data(), fileData.size(), flags, ext.c_str());

    const bool animationOnlyScene = scene && scene->mRootNode && scene->mNumMeshes == 0 && scene->mNumAnimations > 0;
    if (!scene || !scene->mRootNode || ((scene->mFlags & AI_SCENE_FLAGS_INCOMPLETE) && !animationOnlyScene))
        throw std::runtime_error("MeshLoader Assimp import failed for '" + filePath +
                                 "': " + importer.GetErrorString());

    std::string name = FromFsPath(fsPath.stem());
    auto mesh = ConvertScene(scene, settings, name);
    mesh->SetGuid(guid);
    mesh->SetFilePath(filePath);

    MeshSourceImportResult result;
    result.mesh = std::move(mesh);
    result.meshCount = scene->mNumMeshes;
    for (unsigned int meshIndex = 0; meshIndex < scene->mNumMeshes; ++meshIndex) {
        const aiMesh *sourceMesh = scene->mMeshes[meshIndex];
        if (!sourceMesh)
            throw std::runtime_error("MeshLoader Assimp scene contains a null mesh");
        if (sourceMesh->mPrimitiveTypes & aiPrimitiveType_TRIANGLE) {
            result.vertexCount += sourceMesh->mNumVertices;
            for (unsigned int faceIndex = 0; faceIndex < sourceMesh->mNumFaces; ++faceIndex)
                result.indexCount += sourceMesh->mFaces[faceIndex].mNumIndices;
        }
    }

    result.materialSlots.reserve(scene->mNumMaterials);
    for (unsigned int materialIndex = 0; materialIndex < scene->mNumMaterials; ++materialIndex) {
        if (!scene->mMaterials[materialIndex])
            throw std::runtime_error("MeshLoader Assimp scene contains a null material");
        aiString sourceName;
        scene->mMaterials[materialIndex]->Get(AI_MATKEY_NAME, sourceName);
        std::string materialName = sourceName.C_Str();
        if (materialName.empty())
            materialName = "Material_" + std::to_string(materialIndex);
        result.materialSlots.push_back(std::move(materialName));
    }

    std::unordered_set<std::string> seenBones;
    for (unsigned int meshIndex = 0; meshIndex < scene->mNumMeshes; ++meshIndex) {
        const aiMesh *sourceMesh = scene->mMeshes[meshIndex];
        for (unsigned int boneIndex = 0; boneIndex < sourceMesh->mNumBones; ++boneIndex) {
            const aiBone *bone = sourceMesh->mBones[boneIndex];
            if (!bone)
                throw std::runtime_error("MeshLoader Assimp scene contains a null bone");
            std::string boneName = bone->mName.C_Str();
            if (!boneName.empty() && seenBones.insert(boneName).second)
                result.boneNames.push_back(std::move(boneName));
        }
    }

    result.animationNames.reserve(scene->mNumAnimations);
    for (unsigned int animationIndex = 0; animationIndex < scene->mNumAnimations; ++animationIndex) {
        const aiAnimation *animation = scene->mAnimations[animationIndex];
        if (!animation)
            throw std::runtime_error("MeshLoader Assimp scene contains a null animation");
        std::string animationName = animation->mName.C_Str();
        if (animationName.empty())
            animationName = "Anim_" + std::to_string(animationIndex);
        result.animationNames.push_back(std::move(animationName));
    }
    // Assimp marks valid animation-only FBX files as incomplete because they
    // contain no renderable meshes.  Preserve their take metadata in the
    // model artifact, but do not manufacture an invalid skinned companion.
    if (scene->mNumMeshes > 0 && SkinnedModelImporter::HasSkinningData(*scene))
        result.skinnedMesh = SkinnedModelImporter::ConvertScene(*scene, guid, filePath, settings.scaleFactor);
    return result;
}

std::shared_ptr<InxMesh> MeshLoader::ImportSource(const std::string &filePath, const std::string &guid,
                                                  const InxResourceMeta &metadata)
{
    auto imported = ImportSourceDetailed(filePath, guid, metadata);
    imported.mesh->SetSkinnedData(std::move(imported.skinnedMesh));
    return imported.mesh;
}

RuntimeAssetPayload MeshLoader::Load(const std::string &filePath, const std::string &guid, AssetDatabase *adb)
{
    if (!adb)
        throw std::invalid_argument("MeshLoader requires an AssetDatabase");
    auto metadata = adb->GetMetaByGuid(guid);
    if (!metadata)
        throw std::invalid_argument("MeshLoader could not resolve metadata for GUID: " + guid);
    if (!metadata->HasKey("content_hash"))
        throw std::invalid_argument("MeshLoader metadata has no source content hash");

    const std::string sourceHash = metadata->GetDataAs<std::string>("content_hash");
    const std::string artifactPath = adb->GetRuntimeArtifactPath(guid, ResourceType::Mesh);
    const std::string skinnedArtifactPath = adb->GetSkinnedMeshArtifactPath(guid);
    if (!artifactPath.empty() && std::filesystem::is_regular_file(ToFsPath(artifactPath))) {
        try {
            auto mesh = MeshArtifact::Deserialize(ReadArtifactBytes(artifactPath, "Mesh artifact"), sourceHash);
            auto skinned = SkinnedMeshArtifact::Deserialize(
                ReadArtifactBytes(skinnedArtifactPath, "skinned Mesh companion artifact"), sourceHash);
            if (skinned) {
                skinned->guid = guid;
                skinned->sourcePath = filePath;
            }
            mesh->SetSkinnedData(std::move(skinned));
            mesh->SetGuid(guid);
            mesh->SetFilePath(filePath);
            return mesh;
        } catch (const std::exception &exception) {
            INXLOG_WARN("MeshLoader: rejected derived artifact for '", filePath, "': ", exception.what(),
                        "; falling back to source import");
        }
    }

    return ImportSource(filePath, guid, *metadata);
}

bool MeshLoader::Reload(const RuntimeAssetPayload &existing, const std::string &filePath, const std::string &guid,
                        AssetDatabase *adb)
{
    INXLOG_INFO("MeshLoader::Reload: '", filePath, "'");

    auto freshData = Load(filePath, guid, adb);
    if (!freshData)
        return false;

    auto loaded = freshData.Get<InxMesh>();

    // Replace contents of the existing instance in-place to preserve pointer identity.
    auto target = existing.Get<InxMesh>();
    if (!target)
        return false;

    target->SetName(loaded->GetName());
    target->SetFilePath(loaded->GetFilePath());
    target->SetData(std::vector<Vertex>(loaded->GetVertices()), std::vector<uint32_t>(loaded->GetIndices()),
                    std::vector<SubMesh>(loaded->GetSubMeshes()));
    target->SetMaterialSlotNames(std::vector<std::string>(loaded->GetMaterialSlotNames()));
    target->SetMaterialSlotData(std::vector<MaterialSlotData>(loaded->GetMaterialSlotData()));
    target->SetNodeNames(std::vector<std::string>(loaded->GetNodeNames()));
    target->SetSkinnedData(loaded->GetSkinnedData());

    INXLOG_INFO("MeshLoader::Reload: updated '", target->GetName(), "' in-place");
    return true;
}

size_t MeshLoader::EstimateRuntimeBytes(const RuntimeAssetPayload &payload) const
{
    const auto mesh = payload.Get<InxMesh>();
    if (!mesh)
        throw std::invalid_argument("MeshLoader cannot estimate an empty runtime payload");
    return mesh->GetRuntimeMemoryBytes();
}

std::set<std::string> MeshLoader::ScanDependencies(const std::string & /*filePath*/, AssetDatabase * /*adb*/)
{
    // Mesh assets don't reference other assets directly.
    // Material bindings are on MeshRenderer, not on the mesh itself.
    return {};
}

// =============================================================================
// CreateMeta — mesh/binary asset .meta creation
// =============================================================================

void MeshLoader::CreateMeta(const char *content, size_t contentSize, const std::string &filePath,
                            InxResourceMeta &metaData) const
{
    metaData.Init(content, contentSize, filePath, ResourceType::Mesh);

    std::filesystem::path path = ToFsPath(filePath);
    std::string extension = FromFsPath(path.extension());

    metaData.AddMetadata("file_type", std::string("mesh"));
    metaData.AddMetadata("file_extension", extension);
    metaData.AddMetadata("is_readable", false);

    metaData.AddMetadata("file_size", contentSize);
}

} // namespace infernux
