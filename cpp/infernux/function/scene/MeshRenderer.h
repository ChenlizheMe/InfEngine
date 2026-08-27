#pragma once

#include "Component.h"
#include "function/renderer/InxRenderStruct.h"
#include <cstdint>
#include <function/resources/AssetDependencyGraph.h>
#include <function/resources/AssetRef.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/resources/InxMesh/InxMesh.h>
#include <glm/glm.hpp>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace infernux
{

/**
 * @brief Reference to a mesh resource for rendering.
 *
 * This is a lightweight reference to mesh data stored elsewhere.
 * The actual vertex/index data is managed by the resource system.
 */
struct MeshRef
{
    uint64_t meshId = 0; // Resource ID for the mesh

    bool IsValid() const
    {
        return meshId != 0;
    }
};

/**
 * @brief MeshRenderer component for rendering 3D meshes.
 *
 * Attach to a GameObject to make it render a mesh.
 * The renderer uses the Transform of the GameObject for positioning.
 *
 * Material reference uses a single AssetRef<InxMaterial> identified by GUID.
 * Serialization writes only "materialGuid"; deserialization resolves via
 * AssetRegistry to obtain the live InxMaterial pointer.
 */
class MeshRenderer : public Component
{
  public:
    [[nodiscard]] static ComponentTypeConstraints GetTypeConstraints()
    {
        ComponentTypeConstraints constraints;
        constraints.allowMultiple = false;
        constraints.exclusiveGroups = {"renderer-owner"};
        return constraints;
    }

    MeshRenderer() = default;
    ~MeshRenderer() override;

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "MeshRenderer";
    }

    // ========================================================================
    // Lifecycle — register/unregister with SceneManager component registry
    // ========================================================================

    void OnEnable() override;
    void OnDisable() override;

    // ========================================================================
    // Mesh
    // ========================================================================

    [[nodiscard]] const MeshRef &GetMesh() const
    {
        return m_mesh;
    }
    void SetMesh(const MeshRef &mesh)
    {
        m_mesh = mesh;
    }
    void SetMesh(uint64_t meshId)
    {
        m_mesh.meshId = meshId;
        m_useInlineMesh = false;
    }

    /// @brief Set mesh from inline vertex/index data (for primitives)
    void SetMesh(std::vector<Vertex> vertices, std::vector<uint32_t> indices);

    /// @brief Set mesh from shared static primitive data (zero-copy).
    /// The referenced vectors must outlive this MeshRenderer.
    void SetSharedPrimitiveMesh(const std::vector<Vertex> &vertices, const std::vector<uint32_t> &indices,
                                const std::string &primitiveName);

    /// @brief Get/set the display name for inline (primitive) meshes.
    [[nodiscard]] const std::string &GetInlineMeshName() const
    {
        return m_inlineMeshName;
    }
    void SetInlineMeshName(const std::string &name)
    {
        m_inlineMeshName = name;
    }

    // ========================================================================
    // Mesh asset reference (for model-file meshes managed by AssetRegistry)
    // ========================================================================

    /// @brief Set mesh from asset GUID + pre-resolved pointer
    void SetMeshAsset(const std::string &guid, std::shared_ptr<InxMesh> mesh);

    /// @brief Set mesh from asset GUID only (resolution deferred)
    void SetMeshAssetGuid(const std::string &guid);

    /// @brief Clear the asset-managed mesh reference.
    void ClearMeshAsset();

    /// @brief Handle asset graph notifications for the referenced mesh asset.
    void OnMeshAssetEvent(AssetEvent event);

    /// @brief Invalidate or reconnect material slots without changing GUIDs.
    void OnMaterialAssetEvent(const std::string &guid, AssetEvent event);

    /// @brief Get the mesh asset reference
    [[nodiscard]] const AssetRef<InxMesh> &GetMeshAssetRef() const
    {
        return m_meshAsset;
    }

    /// @brief Get the mesh asset GUID (empty if using inline or no asset)
    [[nodiscard]] const std::string &GetMeshAssetGuid() const
    {
        return m_meshAsset.GetGuid();
    }

    /// @brief Check if this renderer uses an asset-managed mesh
    [[nodiscard]] bool HasMeshAsset() const
    {
        return m_meshAsset.HasGuid();
    }

    /// @brief Mark the mesh GPU buffer as needing re-upload (after asset reload)
    void MarkMeshBufferDirty()
    {
        m_meshBufferDirty = true;
    }

    /// @brief Check and consume the dirty flag
    [[nodiscard]] bool ConsumeMeshBufferDirty();

    /// @brief Check if this renderer uses inline mesh data
    [[nodiscard]] bool HasInlineMesh() const
    {
        return m_useInlineMesh;
    }

    /// Shared inline meshes are immutable engine-owned primitive streams.
    /// Their pointer identity is stable for the process lifetime, so render
    /// extraction can retain the stream directly instead of snapshotting the
    /// same Cube/Quad data once per GameObject.
    [[nodiscard]] bool HasSharedInlineMesh() const
    {
        return m_useInlineMesh && m_sharedVertices != nullptr && m_sharedIndices != nullptr;
    }

    /// Stable cache identity for an engine-owned inline primitive.
    [[nodiscard]] std::string GetSharedInlineMeshGuid() const
    {
        return HasSharedInlineMesh() && !m_inlineMeshName.empty() ? "builtin-mesh:" + m_inlineMeshName : "";
    }

    /// @brief Get inline vertex data
    [[nodiscard]] const std::vector<Vertex> &GetInlineVertices() const
    {
        return m_sharedVertices ? *m_sharedVertices : m_inlineVertices;
    }

    /// @brief Get inline index data
    [[nodiscard]] const std::vector<uint32_t> &GetInlineIndices() const
    {
        return m_sharedIndices ? *m_sharedIndices : m_inlineIndices;
    }

    // ========================================================================
    // Materials (multi-slot, submesh-indexed)
    // ========================================================================

    /// @brief Get the number of material slots.
    [[nodiscard]] uint32_t GetMaterialCount() const
    {
        return static_cast<uint32_t>(m_materials.size());
    }

    /// @brief Get material on a specific slot (nullptr if not assigned).
    [[nodiscard]] std::shared_ptr<InxMaterial> GetMaterial(uint32_t slot = 0) const;

    /// @brief Get the effective material for a slot (returns default if none).
    [[nodiscard]] virtual std::shared_ptr<InxMaterial> GetEffectiveMaterial(uint32_t slot = 0) const;

    /// @brief Get the GUID of the material at a specific slot.
    [[nodiscard]] std::string GetMaterialGuid(uint32_t slot = 0) const;

    /// @brief Get all material GUIDs.
    [[nodiscard]] std::vector<std::string> GetMaterialGuids() const;

    /// @brief Set a material on a specific slot by GUID (resolution deferred).
    void SetMaterial(uint32_t slot, const std::string &guid);

    /// @brief Set a material on a specific slot by pointer.
    void SetMaterial(uint32_t slot, std::shared_ptr<InxMaterial> material);

    /// @brief Bulk-set all materials from GUID list.
    void SetMaterials(const std::vector<std::string> &guids);

    /// @brief Resize the material slot array (new slots get empty refs).
    void SetMaterialSlotCount(uint32_t count);

    /// @brief Get all material AssetRefs.
    [[nodiscard]] const std::vector<AssetRef<InxMaterial>> &GetMaterialRefs() const
    {
        return m_materials;
    }

    /// @brief Synchronize material slot count to match mesh submesh count.
    void SyncMaterialSlotsToMesh();

    // ========================================================================
    // Rendering flags
    // ========================================================================

    // ========================================================================
    // Submesh filtering
    // ========================================================================

    /// @brief Get the submesh index filter (-1 = render all submeshes).
    [[nodiscard]] int32_t GetSubmeshIndex() const
    {
        return m_submeshIndex;
    }

    /// @brief Set which submesh to render (-1 = all, >= 0 = specific submesh).
    void SetSubmeshIndex(int32_t index)
    {
        m_submeshIndex = index;
    }

    /// @brief Get the mesh pivot offset (pre-transform to re-center submesh geometry).
    [[nodiscard]] const glm::vec3 &GetMeshPivotOffset() const
    {
        return m_meshPivotOffset;
    }

    /// @brief Set the mesh pivot offset (used to re-center submesh geometry around the transform).
    void SetMeshPivotOffset(const glm::vec3 &offset)
    {
        m_meshPivotOffset = offset;
    }

    /// @brief Get the node group filter (-1 = render all nodes, >= 0 = specific node group).
    [[nodiscard]] int32_t GetNodeGroup() const
    {
        return m_nodeGroup;
    }

    /// @brief Set which node group to render (-1 = all, >= 0 = specific node group).
    void SetNodeGroup(int32_t group);

    [[nodiscard]] bool CastsShadows() const
    {
        return m_castShadows;
    }
    void SetCastShadows(bool cast)
    {
        m_castShadows = cast;
    }

    [[nodiscard]] bool ReceivesShadows() const
    {
        return m_receiveShadows;
    }
    void SetReceivesShadows(bool receive)
    {
        m_receiveShadows = receive;
    }

    // ========================================================================
    // Bounds (for culling)
    // ========================================================================

    /// @brief Get local-space bounding box (from mesh)
    [[nodiscard]] const glm::vec3 &GetLocalBoundsMin() const
    {
        return m_localBoundsMin;
    }
    [[nodiscard]] const glm::vec3 &GetLocalBoundsMax() const
    {
        return m_localBoundsMax;
    }

    /// @brief Set local bounds (usually from mesh loading)
    void SetLocalBounds(const glm::vec3 &min, const glm::vec3 &max)
    {
        m_localBoundsMin = min;
        m_localBoundsMax = max;
    }

    /// @brief Get world-space bounding box (transformed by GameObject)
    [[nodiscard]] void GetWorldBounds(glm::vec3 &outMin, glm::vec3 &outMax) const;

    /// @brief Compute world bounds from a pre-computed world matrix (avoids double GetWorldMatrix)
    virtual void ComputeWorldBounds(const glm::mat4 &worldMatrix, glm::vec3 &outMin, glm::vec3 &outMax) const;

    /// @brief Recompute local bounds from inline vertex positions.
    void ComputeLocalBoundsFromInlineVertices();

    /// @brief Recompute local bounds for a specific node group.
    void UpdateBoundsForNodeGroup(const std::shared_ptr<InxMesh> &mesh);

    // ========================================================================
    // Serialization
    // ========================================================================

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    static void ValidateSerializedDocument(const nlohmann::json &document);
    bool DeserializeDocument(const nlohmann::json &document) override;
    [[nodiscard]] std::unique_ptr<Component> Clone() const override;

  protected:
    static void ValidateSerializedDocumentForType(const nlohmann::json &document, std::string_view expectedType);

  private:
    MeshRef m_mesh;

    // Material slots — one per submesh, GUID-based, resolved via AssetRegistry
    std::vector<AssetRef<InxMaterial>> m_materials;

    // Mesh asset reference (for model-file meshes managed by AssetRegistry)
    AssetRef<InxMesh> m_meshAsset;
    bool m_meshBufferDirty = false;

    // Inline mesh data (for primitives, not using resource system)
    std::vector<Vertex> m_inlineVertices;
    std::vector<uint32_t> m_inlineIndices;
    // Shared primitive mesh data (zero-copy pointer to static data)
    const std::vector<Vertex> *m_sharedVertices = nullptr;
    const std::vector<uint32_t> *m_sharedIndices = nullptr;
    bool m_useInlineMesh = false;
    std::string m_inlineMeshName; // display name for inline (primitive) meshes

    int32_t m_submeshIndex = -1;       // -1 = render all submeshes, >= 0 = single submesh
    int32_t m_nodeGroup = -1;          // -1 = render all node groups, >= 0 = specific node group
    glm::vec3 m_meshPivotOffset{0.0f}; // Pre-transform to re-center submesh geometry

    bool m_castShadows = true;
    bool m_receiveShadows = true;

    // Local-space bounding box
    glm::vec3 m_localBoundsMin{-0.5f};
    glm::vec3 m_localBoundsMax{0.5f};
};

} // namespace infernux
