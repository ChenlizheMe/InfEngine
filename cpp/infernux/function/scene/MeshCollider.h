/**
 * @file MeshCollider.h
 * @brief Triangle mesh / convex hull collider (Unity: MeshCollider).
 */

#pragma once

#include "Collider.h"

#include <cstdint>
#include <utility>

namespace infernux
{

class MeshCollider : public Collider
{
  public:
    MeshCollider() = default;
    ~MeshCollider() override = default;

    [[nodiscard]] const char *GetTypeName() const override
    {
        return "MeshCollider";
    }

    void Awake() override;

    [[nodiscard]] bool IsConvex() const
    {
        return m_convex;
    }
    void SetConvex(bool convex);

    [[nodiscard]] const std::string &GetShapeError() const
    {
        return m_shapeError;
    }

    [[nodiscard]] bool IsCooking() const
    {
        return m_cookingPending;
    }

    [[nodiscard]] void *CreateJoltShapeRaw() const override;

    static void ClearCookingCache();
    [[nodiscard]] static std::pair<size_t, size_t> GetCookingCacheStats();
    [[nodiscard]] static size_t GetPendingCookingCount();
    [[nodiscard]] static size_t GetAsyncCookingSubmissionCount();

    /// Commit completed worker payloads at a main-thread physics safe point.
    /// An explicit physics sync passes waitForAll=true and acts as a barrier.
    static void FlushCompletedCooking(bool waitForAll = false);

    /// Called when the sibling MeshRenderer changes collision geometry.
    void OnMeshGeometryChanged();

    [[nodiscard]] nlohmann::json SerializeDocument() const override;
    static void ValidateSerializedDocument(const nlohmann::json &document);
    bool DeserializeDocument(const nlohmann::json &document) override;
    [[nodiscard]] std::unique_ptr<Component> Clone() const override;

    void AutoFitToMesh() override;

    /// Convex hull positions (local space) cached after shape creation.
    const std::vector<glm::vec3> &GetConvexHullPositions() const
    {
        return m_convexHullPositions;
    }
    /// Convex hull edge pairs [a0,b0, a1,b1, …] cached after shape creation.
    const std::vector<uint32_t> &GetConvexHullEdges() const
    {
        return m_convexHullEdges;
    }

    /// Current cooked collision triangles in unscaled GameObject-local space.
    /// The topology is replaced atomically only after cooking succeeds, so GPU
    /// consumers can retain the previous revision while a replacement cooks.
    const std::vector<glm::vec3> &GetCollisionPositions() const
    {
        return m_collisionPositions;
    }
    const std::vector<uint32_t> &GetCollisionIndices() const
    {
        return m_collisionIndices;
    }
    [[nodiscard]] uint64_t GetCollisionGeometryRevision() const
    {
        return m_collisionGeometryRevision;
    }

  private:
    bool CollectMeshGeometry(std::vector<glm::vec3> &outVertices, std::vector<uint32_t> &outIndices) const;
    void CompleteCooking(uint64_t hashA, uint64_t hashB, size_t vertexCount, size_t indexCount, bool convex,
                         uint64_t revision, const std::string &error);
    void InvalidatePendingCooking() const;

    bool m_convex = false;
    mutable std::vector<glm::vec3> m_convexHullPositions;
    mutable std::vector<uint32_t> m_convexHullEdges;
    mutable std::vector<glm::vec3> m_collisionPositions;
    mutable std::vector<uint32_t> m_collisionIndices;
    mutable uint64_t m_collisionGeometryRevision = 0;
    mutable std::string m_shapeError;
    mutable uint64_t m_cookingRevision = 0;
    mutable uint64_t m_pendingHashA = 0;
    mutable uint64_t m_pendingHashB = 0;
    mutable size_t m_pendingVertexCount = 0;
    mutable size_t m_pendingIndexCount = 0;
    mutable bool m_pendingConvex = false;
    mutable bool m_cookingPending = false;
};

} // namespace infernux
