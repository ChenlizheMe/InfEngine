#pragma once

#include <function/renderer/rhi/RhiCommand.h>
#include <function/renderer/rhi/RhiDevice.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux::particle
{

enum class GpuParticleColliderType : uint32_t
{
    Box = 0,
    Sphere = 1,
    Capsule = 2,
    Mesh = 3,
    Terrain = 4,
};

enum GpuParticleColliderFlags : uint32_t
{
    GpuParticleColliderTrigger = 1u << 0,
    GpuParticleColliderDynamic = 1u << 1,
    GpuParticleColliderConvex = 1u << 2,
};

/// Row-major affine 3x4 transform stored as three std430 vec4 values. Collider
/// transforms never contain projection, so retaining a constant fourth row
/// would waste GPU bandwidth for every particle system.
struct alignas(16) GpuParticleAffineTransform
{
    std::array<float, 4> row0{};
    std::array<float, 4> row1{};
    std::array<float, 4> row2{};
};

/// Stable std430 record shared by every GPU emitter. Affine transforms map
/// between collider-local and world space; shape interpretation is selected by
/// metadata.x. The ABI deliberately contains no scene/component pointers.
struct alignas(16) GpuParticleColliderRecord
{
    GpuParticleAffineTransform colliderToWorld{};
    GpuParticleAffineTransform worldToCollider{};
    GpuParticleAffineTransform previousWorldToCollider{};
    std::array<float, 4> shape{};
    /// Mesh payload: vertex offset, reordered index offset, BVH root, triangle count.
    std::array<uint32_t, 4> geometry{};
    std::array<float, 4> linearVelocity{};
    std::array<float, 4> material{};
    std::array<float, 4> worldAabbMin{};
    std::array<float, 4> worldAabbMax{};
    std::array<float, 4> previousWorldAabbMin{};
    std::array<float, 4> previousWorldAabbMax{};
    std::array<uint32_t, 4> metadata{};
    std::array<uint32_t, 4> identity{};
};

struct alignas(16) GpuParticleCollisionSceneHeader
{
    uint32_t colliderCount = 0;
    uint32_t staticColliderCount = 0;
    uint32_t revisionLow = 0;
    uint32_t revisionHigh = 0;
    std::array<float, 4> gridMinInvCellSize{};
    std::array<uint32_t, 4> gridDimensions{};
    /// vertex count, triangle-index count, BVH node count, topology revision.
    std::array<uint32_t, 4> topology{};
};

struct alignas(16) GpuParticleCollisionBvhNode
{
    std::array<float, 4> boundsMin{};
    std::array<float, 4> boundsMax{};
    /// Internal: left/right node, count=0. Leaf: first index/count, left/right unused.
    std::array<uint32_t, 4> metadata{};
};

struct GpuParticleCollisionMeshGeometry
{
    uint64_t identity = 0;
    std::vector<std::array<float, 4>> positions;
    std::vector<uint32_t> indices;
};

struct GpuParticleCollisionSceneSnapshot
{
    uint64_t revision = 0;
    uint64_t topologyRevision = 0;
    bool replaceMeshTopology = false;
    std::vector<GpuParticleColliderRecord> staticColliders;
    std::vector<GpuParticleColliderRecord> dynamicColliders;
    std::vector<GpuParticleCollisionMeshGeometry> meshGeometries;
};

/// Owns one device-local collider table shared by all resident GPU particle
/// emitters. CPU publication writes staging memory only when the scene revision
/// changes; RecordPendingUpload performs the frame-boundary RHI transfer.
class ParticleGpuCollisionScene
{
  public:
    static constexpr uint32_t DefaultCapacity = 4096;
    static constexpr uint32_t GridAxisCells = 8;
    static constexpr uint32_t MaxGridCellCount = GridAxisCells * GridAxisCells * GridAxisCells;
    static constexpr uint32_t MaxGridReferenceCount = DefaultCapacity * MaxGridCellCount;
    static constexpr uint32_t DefaultMeshVertexCapacity = 131072;
    static constexpr uint32_t DefaultMeshIndexCapacity = 393216;
    static constexpr uint32_t DefaultMeshBvhNodeCapacity = 262144;

    ParticleGpuCollisionScene() = default;
    ~ParticleGpuCollisionScene();

    ParticleGpuCollisionScene(const ParticleGpuCollisionScene &) = delete;
    ParticleGpuCollisionScene &operator=(const ParticleGpuCollisionScene &) = delete;

    [[nodiscard]] bool Create(rhi::Device &device, uint32_t capacity = DefaultCapacity, uint32_t uploadPageCount = 2,
                              uint32_t meshVertexCapacity = DefaultMeshVertexCapacity,
                              uint32_t meshIndexCapacity = DefaultMeshIndexCapacity,
                              uint32_t meshBvhNodeCapacity = DefaultMeshBvhNodeCapacity);
    void Destroy() noexcept;

    [[nodiscard]] bool Publish(const GpuParticleCollisionSceneSnapshot &snapshot, std::string *error = nullptr);
    [[nodiscard]] bool RecordPendingUpload(const rhi::TransferCommandEncoder &encoder);

    [[nodiscard]] bool IsValid() const noexcept;
    [[nodiscard]] bool HasPendingUpload() const noexcept
    {
        return m_uploadPending;
    }
    [[nodiscard]] uint64_t PublishedRevision() const noexcept
    {
        return m_publishedRevision;
    }
    [[nodiscard]] uint32_t PublishedColliderCount() const noexcept
    {
        return m_publishedColliderCount;
    }
    [[nodiscard]] uint32_t PublishedStaticColliderCount() const noexcept
    {
        return m_publishedStaticColliderCount;
    }
    [[nodiscard]] uint32_t PublishedDynamicColliderCount() const noexcept
    {
        return m_publishedColliderCount - m_publishedStaticColliderCount;
    }
    [[nodiscard]] uint32_t Capacity() const noexcept
    {
        return m_capacity;
    }
    [[nodiscard]] rhi::BufferHandle HeaderBuffer() const noexcept
    {
        return m_headerBuffer;
    }
    [[nodiscard]] rhi::BufferHandle ColliderBuffer() const noexcept
    {
        return m_colliderBuffer;
    }
    [[nodiscard]] rhi::BufferHandle GridOffsetBuffer() const noexcept
    {
        return m_gridOffsetBuffer;
    }
    [[nodiscard]] rhi::BufferHandle GridColliderIndexBuffer() const noexcept
    {
        return m_gridColliderIndexBuffer;
    }
    [[nodiscard]] rhi::BufferHandle MeshVertexBuffer() const noexcept
    {
        return m_meshVertexBuffer;
    }
    [[nodiscard]] rhi::BufferHandle MeshIndexBuffer() const noexcept
    {
        return m_meshIndexBuffer;
    }
    [[nodiscard]] rhi::BufferHandle MeshBvhBuffer() const noexcept
    {
        return m_meshBvhBuffer;
    }
    [[nodiscard]] uint32_t PublishedGridReferenceCount() const noexcept
    {
        return m_publishedGridReferenceCount;
    }
    [[nodiscard]] uint64_t PublishedTopologyRevision() const noexcept
    {
        return m_publishedTopologyRevision;
    }
    [[nodiscard]] uint32_t PublishedMeshVertexCount() const noexcept
    {
        return m_publishedMeshVertexCount;
    }
    [[nodiscard]] uint32_t PublishedMeshIndexCount() const noexcept
    {
        return m_publishedMeshIndexCount;
    }
    [[nodiscard]] uint32_t PublishedMeshBvhNodeCount() const noexcept
    {
        return m_publishedMeshBvhNodeCount;
    }

  private:
    struct UploadPage
    {
        rhi::BufferHandle header;
        rhi::BufferHandle colliders;
        rhi::BufferHandle gridOffsets;
        rhi::BufferHandle gridColliderIndices;
        rhi::BufferHandle meshVertices;
        rhi::BufferHandle meshIndices;
        rhi::BufferHandle meshBvhNodes;

        [[nodiscard]] bool IsValid() const noexcept
        {
            return header.IsValid() && colliders.IsValid() && gridOffsets.IsValid() && gridColliderIndices.IsValid() &&
                   meshVertices.IsValid() && meshIndices.IsValid() && meshBvhNodes.IsValid();
        }
    };

    rhi::Device *m_device = nullptr;
    rhi::BufferHandle m_headerBuffer;
    rhi::BufferHandle m_colliderBuffer;
    rhi::BufferHandle m_gridOffsetBuffer;
    rhi::BufferHandle m_gridColliderIndexBuffer;
    rhi::BufferHandle m_meshVertexBuffer;
    rhi::BufferHandle m_meshIndexBuffer;
    rhi::BufferHandle m_meshBvhBuffer;
    std::vector<UploadPage> m_uploadPages;
    uint32_t m_capacity = 0;
    uint32_t m_meshVertexCapacity = 0;
    uint32_t m_meshIndexCapacity = 0;
    uint32_t m_meshBvhNodeCapacity = 0;
    uint32_t m_pendingUploadPage = 0;
    uint32_t m_nextUploadPage = 0;
    uint32_t m_pendingColliderCount = 0;
    uint32_t m_pendingStaticColliderCount = 0;
    uint32_t m_publishedColliderCount = 0;
    uint32_t m_publishedStaticColliderCount = 0;
    uint32_t m_pendingGridOffsetCount = 0;
    uint32_t m_pendingGridReferenceCount = 0;
    uint32_t m_publishedGridReferenceCount = 0;
    uint32_t m_publishedMeshVertexCount = 0;
    uint32_t m_publishedMeshIndexCount = 0;
    uint32_t m_publishedMeshBvhNodeCount = 0;
    uint64_t m_pendingCopyOffset = 0;
    uint64_t m_pendingCopyBytes = 0;
    uint64_t m_pendingRevision = 0;
    uint64_t m_publishedRevision = 0;
    uint64_t m_pendingTopologyRevision = 0;
    uint64_t m_publishedTopologyRevision = 0;
    std::vector<GpuParticleColliderRecord> m_stagedStaticColliders;
    std::vector<GpuParticleColliderRecord> m_stagedDynamicColliders;
    std::vector<uint32_t> m_stagedGridOffsets;
    std::vector<uint32_t> m_stagedGridColliderIndices;
    std::vector<std::array<float, 4>> m_stagedMeshVertices;
    std::vector<uint32_t> m_stagedMeshIndices;
    std::vector<GpuParticleCollisionBvhNode> m_stagedMeshBvhNodes;
    std::unordered_map<uint64_t, std::array<uint32_t, 4>> m_stagedGeometryByIdentity;
    uint32_t m_pendingMeshVertexCount = 0;
    uint32_t m_pendingMeshIndexCount = 0;
    uint32_t m_pendingMeshBvhNodeCount = 0;
    bool m_pendingTopologyUpload = false;
    bool m_uploadPending = false;
};

static_assert(sizeof(GpuParticleAffineTransform) == 48);
static_assert(offsetof(GpuParticleAffineTransform, row0) == 0);
static_assert(offsetof(GpuParticleAffineTransform, row1) == 16);
static_assert(offsetof(GpuParticleAffineTransform, row2) == 32);
static_assert(sizeof(GpuParticleColliderRecord) == 304);
static_assert(offsetof(GpuParticleColliderRecord, colliderToWorld) == 0);
static_assert(offsetof(GpuParticleColliderRecord, worldToCollider) == 48);
static_assert(offsetof(GpuParticleColliderRecord, previousWorldToCollider) == 96);
static_assert(offsetof(GpuParticleColliderRecord, shape) == 144);
static_assert(offsetof(GpuParticleColliderRecord, geometry) == 160);
static_assert(offsetof(GpuParticleColliderRecord, linearVelocity) == 176);
static_assert(offsetof(GpuParticleColliderRecord, material) == 192);
static_assert(offsetof(GpuParticleColliderRecord, worldAabbMin) == 208);
static_assert(offsetof(GpuParticleColliderRecord, worldAabbMax) == 224);
static_assert(offsetof(GpuParticleColliderRecord, previousWorldAabbMin) == 240);
static_assert(offsetof(GpuParticleColliderRecord, previousWorldAabbMax) == 256);
static_assert(offsetof(GpuParticleColliderRecord, metadata) == 272);
static_assert(offsetof(GpuParticleColliderRecord, identity) == 288);
static_assert(sizeof(GpuParticleCollisionSceneHeader) == 64);
static_assert(sizeof(GpuParticleCollisionBvhNode) == 48);

} // namespace infernux::particle
