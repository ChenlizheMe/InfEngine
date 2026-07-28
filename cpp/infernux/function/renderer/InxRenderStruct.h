#pragma once
#define GLM_FORCE_RADIANS
#ifndef GLM_FORCE_DEPTH_ZERO_TO_ONE
#define GLM_FORCE_DEPTH_ZERO_TO_ONE
#endif

#include <array>
#include <memory>
#include <string>
#include <vector>

#include <function/renderer/Frustum.h>
#include <function/renderer/RenderIdentity.h>
#include <glm/glm.hpp>
#include <glm/gtc/matrix_transform.hpp>

#include <chrono>
#include <vulkan/vulkan.h>

namespace infernux
{
#ifdef DrawCall
#undef DrawCall
#endif

class InxMaterial;

/**
 * @brief Vertex structure for mesh rendering.
 *
 * Contains all vertex attributes needed for PBR rendering:
 * - Position: 3D world-space position
 * - Normal: Surface normal for lighting calculations
 * - Tangent: Tangent vector for normal mapping (w = handedness)
 * - Color: Vertex color (can be used for tinting or debugging)
 * - TexCoord: Primary UV coordinates
 */
struct Vertex
{
    glm::vec3 pos;                      ///< Position in local space
    glm::vec3 normal;                   ///< Normal vector (normalized)
    glm::vec4 tangent;                  ///< Tangent vector (xyz) + handedness (w = ±1)
    glm::vec3 color{1.0f, 1.0f, 1.0f};  ///< Vertex color (default white)
    glm::vec2 texCoord;                 ///< Primary UV coordinates
    glm::uvec4 boneIndices{0, 0, 0, 0}; ///< GPU skinning bone indices
    glm::vec4 boneWeights{0.0f};        ///< GPU skinning weights

    static VkVertexInputBindingDescription getBindingDescription()
    {
        VkVertexInputBindingDescription bindingDescription{};
        bindingDescription.binding = 0;
        bindingDescription.stride = sizeof(Vertex);
        bindingDescription.inputRate = VK_VERTEX_INPUT_RATE_VERTEX;

        return bindingDescription;
    }

    static std::array<VkVertexInputAttributeDescription, 7> getAttributeDescriptions()
    {
        std::array<VkVertexInputAttributeDescription, 7> attributeDescriptions{};

        // Location 0: Position
        attributeDescriptions[0].binding = 0;
        attributeDescriptions[0].location = 0;
        attributeDescriptions[0].format = VK_FORMAT_R32G32B32_SFLOAT;
        attributeDescriptions[0].offset = offsetof(Vertex, pos);

        // Location 1: Normal
        attributeDescriptions[1].binding = 0;
        attributeDescriptions[1].location = 1;
        attributeDescriptions[1].format = VK_FORMAT_R32G32B32_SFLOAT;
        attributeDescriptions[1].offset = offsetof(Vertex, normal);

        // Location 2: Tangent (vec4 for handedness)
        attributeDescriptions[2].binding = 0;
        attributeDescriptions[2].location = 2;
        attributeDescriptions[2].format = VK_FORMAT_R32G32B32A32_SFLOAT;
        attributeDescriptions[2].offset = offsetof(Vertex, tangent);

        // Location 3: Color
        attributeDescriptions[3].binding = 0;
        attributeDescriptions[3].location = 3;
        attributeDescriptions[3].format = VK_FORMAT_R32G32B32_SFLOAT;
        attributeDescriptions[3].offset = offsetof(Vertex, color);

        // Location 4: TexCoord
        attributeDescriptions[4].binding = 0;
        attributeDescriptions[4].location = 4;
        attributeDescriptions[4].format = VK_FORMAT_R32G32_SFLOAT;
        attributeDescriptions[4].offset = offsetof(Vertex, texCoord);

        // Location 5: Skin bone indices
        attributeDescriptions[5].binding = 0;
        attributeDescriptions[5].location = 5;
        attributeDescriptions[5].format = VK_FORMAT_R32G32B32A32_UINT;
        attributeDescriptions[5].offset = offsetof(Vertex, boneIndices);

        // Location 6: Skin bone weights
        attributeDescriptions[6].binding = 0;
        attributeDescriptions[6].location = 6;
        attributeDescriptions[6].format = VK_FORMAT_R32G32B32A32_SFLOAT;
        attributeDescriptions[6].offset = offsetof(Vertex, boneWeights);

        return attributeDescriptions;
    }

    /// @brief Create a vertex with position, normal, and UV (common case)
    static Vertex Create(const glm::vec3 &position, const glm::vec3 &norm, const glm::vec2 &uv,
                         const glm::vec3 &col = glm::vec3(1.0f))
    {
        Vertex v;
        v.pos = position;
        v.normal = norm;
        v.tangent = glm::vec4(1.0f, 0.0f, 0.0f, 1.0f); // Default tangent
        v.color = col;
        v.texCoord = uv;
        return v;
    }

    /// @brief Create a vertex with all attributes
    static Vertex CreateFull(const glm::vec3 &position, const glm::vec3 &norm, const glm::vec4 &tan,
                             const glm::vec3 &col, const glm::vec2 &uv)
    {
        Vertex v;
        v.pos = position;
        v.normal = norm;
        v.tangent = tan;
        v.color = col;
        v.texCoord = uv;
        return v;
    }
};

struct GPUSkinInstanceData
{
    uint32_t boneOffset = 0;
    uint32_t boneCount = 0;
    uint32_t flags = 0;
    uint32_t _pad = 0;
};

static constexpr uint32_t kGPUSkinFlagEnabled = 1u;

struct UniformBufferObject
{
    alignas(16) glm::mat4 model;
    alignas(16) glm::mat4 view;
    alignas(16) glm::mat4 proj;
    alignas(16) glm::mat4 previousViewProj;
};

/**
 * @brief DrawCall - Unity-style draw call information
 *
 * Represents a single draw call with its own material, transform,
 * and per-object mesh buffer references.
 *
 * Each DrawCall keeps its material alive while carrying non-owning pointers
 * to the object's vertex/index data. The
 * renderer creates persistent
 * per-object GPU buffers, eliminating the per-frame combined-buffer copy.
 */
struct DrawCall
{
    uint32_t indexStart = 0;               // Offset into index buffer
    uint32_t indexCount = 0;               // Number of indices to draw
    int32_t vertexStart = 0;               // Base vertex offset (for submesh rendering)
    glm::mat4 worldMatrix{1.0f};           // Object's world transform matrix
    std::shared_ptr<InxMaterial> material; // Owns the material for the lifetime of cached/render-thread draw calls
    uint64_t objectId = 0;                 // Compatibility key for buffer lookup and picking
    uint64_t pickingObjectId = 0;          // Optional scene owner exposed by picking passes
    uint32_t layerMask = 1u;               // Owning GameObject layer bit for per-light culling masks
    RenderDrawIdentity identity;           // Stable source identity across scene/component lifetimes
    bool frustumVisible = true;            // Whether object passed main-camera frustum culling
    bool castsShadows = true;              // Whether the source renderer participates in shadow passes
    AABB worldBounds;                      // World-space bounding box for shadow cascade culling

    // Per-object mesh data pointers
    // Non-owning references to MeshRenderer's persistent vertex/index data.
    // Used by the renderer to create/update per-object GPU buffers.
    const std::vector<Vertex> *meshVertices = nullptr;
    const std::vector<uint32_t> *meshIndices = nullptr;
    // Keeps the storage behind meshVertices/meshIndices alive for immutable
    // RenderWorld snapshots. Asset meshes retain their asset generation;
    // inline meshes retain an extraction-owned snapshot.
    std::shared_ptr<const void> meshDataOwner;
    std::string meshAssetGuid;
    uint64_t meshRuntimeVersion = 0;

    // Optional GPU skinning palette. When present, vertex data is the bind-pose
    // skinned mesh stream and the vertex shader applies these matrices.
    std::shared_ptr<const std::vector<glm::mat4>> skinBoneMatricesOwner;
    const std::vector<glm::mat4> *skinBoneMatrices = nullptr;

    // When true, forces GPU buffer re-upload even if vertex/index count hasn't
    // changed (e.g. vertex colour change for gizmo highlight).
    bool forceBufferUpdate = false;

    // Transparent particles preserve their sorted instance order while sharing
    // one mesh/material draw. Ordinary transparent draw calls remain unbatched.
    bool allowTransparentInstancing = false;
};

/**
 * @brief Result of building draw calls from renderables.
 *
 * Contains the combined vertex/index buffer and per-object draw call info
 * ready to upload to the GPU.
 */
struct DrawCallResult
{
    std::vector<DrawCall> drawCalls;
    std::vector<Vertex> combinedVertices;
    std::vector<uint32_t> combinedIndices;

    /// @brief Append another DrawCallResult (e.g. gizmo data) to this one
    void Append(const DrawCallResult &other)
    {
        if (other.drawCalls.empty())
            return;

        uint32_t indexOffset = static_cast<uint32_t>(combinedIndices.size());
        uint32_t vertexOffset = static_cast<uint32_t>(combinedVertices.size());

        combinedVertices.reserve(combinedVertices.size() + other.combinedVertices.size());
        combinedVertices.insert(combinedVertices.end(), other.combinedVertices.begin(), other.combinedVertices.end());

        combinedIndices.reserve(combinedIndices.size() + other.combinedIndices.size());
        for (uint32_t idx : other.combinedIndices) {
            combinedIndices.push_back(vertexOffset + idx);
        }

        drawCalls.reserve(drawCalls.size() + other.drawCalls.size());
        for (const auto &dc : other.drawCalls) {
            DrawCall newDc = dc;
            newDc.indexStart = indexOffset + dc.indexStart;
            drawCalls.push_back(newDc);
        }
    }
};
} // namespace infernux
