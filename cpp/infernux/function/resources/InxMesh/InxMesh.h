#pragma once

#include <function/renderer/InxRenderStruct.h>

#include <glm/glm.hpp>

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace infernux
{

class InxSkinnedMesh;

/**
 * @brief A contiguous range within a shared vertex/index buffer.
 *
 * Each SubMesh maps to exactly one material slot on the MeshRenderer.
 * Multiple SubMeshes share the parent InxMesh's vertex and index arrays
 * so a single GPU buffer upload serves the entire model.
 */
struct SubMesh
{
    uint32_t indexStart = 0;  ///< First index in the parent index array
    uint32_t indexCount = 0;  ///< Number of indices (must be a multiple of 3)
    uint32_t vertexStart = 0; ///< First vertex in the parent vertex array (base-vertex offset)
    uint32_t vertexCount = 0; ///< Number of vertices referenced by this submesh

    uint32_t materialSlot = 0; ///< Material slot index (0-based, maps to MeshRenderer.materials[])
    uint32_t nodeGroup = 0;    ///< Source node group (meshes under the same DCC node share the same group)

    glm::vec3 boundsMin{0.0f}; ///< Local-space AABB minimum
    glm::vec3 boundsMax{0.0f}; ///< Local-space AABB maximum

    std::string name; ///< Optional submesh name (from DCC tool, e.g. "Body", "Glass")
};

/**
 * @brief Per-slot material data extracted from a model file (FBX/OBJ/GLTF).
 *
 * Stores the material properties that Assimp reads from the source file
 * so that default materials can be created with correct colours rather
 * than falling back to plain white.
 */
struct MaterialSlotData
{
    glm::vec4 baseColor{1.0f, 1.0f, 1.0f, 1.0f};     ///< Diffuse / albedo colour (RGBA)
    glm::vec4 emissionColor{0.0f, 0.0f, 0.0f, 0.0f}; ///< Emission colour (RGBA)
    float metallic = 0.0f;
    float smoothness = 0.5f;
    float opacity = 1.0f;
};

/**
 * @brief Runtime mesh asset — the loaded, GPU-ready representation of a 3D model.
 *
 * InxMesh is the engine's canonical mesh data container, analogous to
 * Unity's Mesh or UE5's UStaticMesh.  It stores all geometry for a model
 * file (.fbx, .obj, .gltf, …) as a single pair of vertex/index arrays
 * partitioned into SubMeshes.
 *
 * Design decisions:
 *   - **Single buffer, multiple submeshes** — minimises GPU buffer count
 *     and allows one vkCmdBindVertexBuffers per model regardless of
 *     how many material slots it uses.
 *   - **Source file is the truth** — no intermediate .mesh format.
 *     The original .fbx/.obj/.gltf is re-parsed by Assimp at load time.
 *     A binary cache can be added later as an optimisation.
 *   - **Vertex layout matches the engine's `Vertex` struct** — Assimp
 *     data is converted once during loading; no runtime format conversion.
 *
 * Ownership: managed by AssetRegistry via shared_ptr<InxMesh>.
 * MeshRenderers hold AssetRef<InxMesh> resolved through the registry.
 */
class InxMesh
{
  public:
    InxMesh() = default;
    explicit InxMesh(const std::string &name) : m_name(name)
    {
    }

    // ── Identification ───────────────────────────────────────────────────

    [[nodiscard]] const std::string &GetName() const
    {
        return m_name;
    }
    void SetName(const std::string &name)
    {
        m_name = name;
    }

    [[nodiscard]] const std::string &GetGuid() const
    {
        return m_guid;
    }
    void SetGuid(const std::string &guid)
    {
        m_guid = guid;
    }

    [[nodiscard]] const std::string &GetFilePath() const
    {
        return m_filePath;
    }
    void SetFilePath(const std::string &path)
    {
        m_filePath = path;
    }

    // ── Geometry data ────────────────────────────────────────────────────

    [[nodiscard]] const std::vector<Vertex> &GetVertices() const
    {
        return m_vertices;
    }
    [[nodiscard]] const std::vector<uint32_t> &GetIndices() const
    {
        return m_indices;
    }

    [[nodiscard]] uint32_t GetVertexCount() const
    {
        return static_cast<uint32_t>(m_vertices.size());
    }
    [[nodiscard]] uint32_t GetIndexCount() const
    {
        return static_cast<uint32_t>(m_indices.size());
    }

    // ── SubMesh access ───────────────────────────────────────────────────

    [[nodiscard]] const std::vector<SubMesh> &GetSubMeshes() const
    {
        return m_subMeshes;
    }
    [[nodiscard]] uint32_t GetSubMeshCount() const
    {
        return static_cast<uint32_t>(m_subMeshes.size());
    }
    [[nodiscard]] const SubMesh &GetSubMesh(uint32_t index) const
    {
        return m_subMeshes.at(index);
    }

    // ── Bounds ───────────────────────────────────────────────────────────

    [[nodiscard]] const glm::vec3 &GetBoundsMin() const
    {
        return m_boundsMin;
    }
    [[nodiscard]] const glm::vec3 &GetBoundsMax() const
    {
        return m_boundsMax;
    }

    [[nodiscard]] uint64_t GetGeneration() const noexcept
    {
        return m_generation;
    }

    // ── Material slot names (extracted from model file) ──────────────────

    [[nodiscard]] const std::vector<std::string> &GetMaterialSlotNames() const
    {
        return m_materialSlotNames;
    }
    [[nodiscard]] uint32_t GetMaterialSlotCount() const
    {
        return static_cast<uint32_t>(m_materialSlotNames.size());
    }

    // ── Material slot data (extracted from model file) ───────────────

    [[nodiscard]] const std::vector<MaterialSlotData> &GetMaterialSlotData() const
    {
        return m_materialSlotData;
    }
    void SetMaterialSlotData(std::vector<MaterialSlotData> data)
    {
        m_materialSlotData = std::move(data);
    }

    // ── Node group metadata (for per-object hierarchy) ────────────────

    [[nodiscard]] const std::vector<std::string> &GetNodeNames() const
    {
        return m_nodeNames;
    }
    [[nodiscard]] uint32_t GetNodeGroupCount() const
    {
        return static_cast<uint32_t>(m_nodeNames.size());
    }
    void SetNodeNames(std::vector<std::string> names)
    {
        m_nodeNames = std::move(names);
    }

    // ── Builder API (called by MeshLoader during import) ─────────────────

    /**
     * @brief Set the mesh geometry and submesh layout.
     *
     * Takes ownership of the data via move.  Recomputes the overall AABB
     * from the vertex positions.
     */
    void SetData(std::vector<Vertex> vertices, std::vector<uint32_t> indices, std::vector<SubMesh> subMeshes);

    /**
     * @brief Set material slot names extracted from the model file.
     *
     * The i-th name corresponds to materialSlot i.  The MeshRenderer
     * inspector uses these names as labels.
     */
    void SetMaterialSlotNames(std::vector<std::string> names)
    {
        m_materialSlotNames = std::move(names);
    }

    void SetSkinnedData(std::shared_ptr<const InxSkinnedMesh> skinnedData);
    [[nodiscard]] const std::shared_ptr<const InxSkinnedMesh> &GetSkinnedData() const noexcept
    {
        return m_skinnedData;
    }
    [[nodiscard]] bool HasSkinnedData() const noexcept
    {
        return static_cast<bool>(m_skinnedData);
    }

    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept;

  private:
    std::string m_name;
    std::string m_guid;
    std::string m_filePath;

    std::vector<Vertex> m_vertices;
    std::vector<uint32_t> m_indices;
    std::vector<SubMesh> m_subMeshes;

    glm::vec3 m_boundsMin{0.0f};
    glm::vec3 m_boundsMax{0.0f};

    std::vector<std::string> m_materialSlotNames;
    std::vector<MaterialSlotData> m_materialSlotData;
    std::vector<std::string> m_nodeNames; ///< Node names indexed by nodeGroup
    std::shared_ptr<const InxSkinnedMesh> m_skinnedData;
    uint64_t m_generation = 0;

    /// Recompute m_boundsMin/Max from vertex positions.
    void RecalculateBounds();
};

} // namespace infernux
