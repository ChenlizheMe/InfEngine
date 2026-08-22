#include "ParticleGpuCollisionScene.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <numeric>
#include <unordered_map>
#include <unordered_set>

namespace infernux::particle
{

namespace
{

void SetError(std::string *error, const char *message)
{
    if (error)
        *error = message;
}

bool EqualRecords(const std::vector<GpuParticleColliderRecord> &left,
                  const std::vector<GpuParticleColliderRecord> &right)
{
    return left.size() == right.size() &&
           (left.empty() || std::memcmp(left.data(), right.data(), left.size() * sizeof(left.front())) == 0);
}

uint64_t RecordIdentity(const GpuParticleColliderRecord &record)
{
    return static_cast<uint64_t>(record.identity[0]) | (static_cast<uint64_t>(record.identity[1]) << 32u);
}

bool RecordIdentityLess(const GpuParticleColliderRecord &left, const GpuParticleColliderRecord &right)
{
    return left.identity < right.identity;
}

bool ValidateRecordIdentities(const std::vector<GpuParticleColliderRecord> &staticRecords,
                              const std::vector<GpuParticleColliderRecord> &dynamicRecords)
{
    std::unordered_set<uint64_t> identities;
    identities.reserve(staticRecords.size() + dynamicRecords.size());
    const auto append = [&identities](const auto &records) {
        for (const auto &record : records) {
            const uint64_t identity = RecordIdentity(record);
            if (identity == 0 || !identities.insert(identity).second)
                return false;
        }
        return true;
    };
    return append(staticRecords) && append(dynamicRecords);
}

struct BuiltMeshTopology
{
    std::vector<std::array<float, 4>> vertices;
    std::vector<uint32_t> indices;
    std::vector<GpuParticleCollisionBvhNode> nodes;
    std::unordered_map<uint64_t, std::array<uint32_t, 4>> geometryByIdentity;
};

struct BuildTriangle
{
    uint32_t sourceTriangle = 0;
    std::array<float, 3> lower{};
    std::array<float, 3> upper{};
    std::array<float, 3> centroid{};
};

uint32_t BuildMeshBvhRecursive(const GpuParticleCollisionMeshGeometry &geometry, std::vector<BuildTriangle> &triangles,
                               uint32_t first, uint32_t count, uint32_t vertexOffset,
                               std::vector<uint32_t> &orderedIndices, std::vector<GpuParticleCollisionBvhNode> &nodes)
{
    const uint32_t nodeIndex = static_cast<uint32_t>(nodes.size());
    nodes.emplace_back();
    auto &node = nodes[nodeIndex];
    std::array<float, 3> lower = {std::numeric_limits<float>::max(), std::numeric_limits<float>::max(),
                                  std::numeric_limits<float>::max()};
    std::array<float, 3> upper = {std::numeric_limits<float>::lowest(), std::numeric_limits<float>::lowest(),
                                  std::numeric_limits<float>::lowest()};
    std::array<float, 3> centroidLower = lower;
    std::array<float, 3> centroidUpper = upper;
    for (uint32_t index = first; index < first + count; ++index) {
        for (size_t axis = 0; axis < 3; ++axis) {
            lower[axis] = std::min(lower[axis], triangles[index].lower[axis]);
            upper[axis] = std::max(upper[axis], triangles[index].upper[axis]);
            centroidLower[axis] = std::min(centroidLower[axis], triangles[index].centroid[axis]);
            centroidUpper[axis] = std::max(centroidUpper[axis], triangles[index].centroid[axis]);
        }
    }
    node.boundsMin = {lower[0], lower[1], lower[2], 0.0f};
    node.boundsMax = {upper[0], upper[1], upper[2], 0.0f};

    constexpr uint32_t LeafTriangleCount = 4;
    if (count <= LeafTriangleCount) {
        const uint32_t leafIndexOffset = static_cast<uint32_t>(orderedIndices.size());
        for (uint32_t index = first; index < first + count; ++index) {
            const uint32_t source = triangles[index].sourceTriangle * 3u;
            orderedIndices.push_back(vertexOffset + geometry.indices[source]);
            orderedIndices.push_back(vertexOffset + geometry.indices[source + 1u]);
            orderedIndices.push_back(vertexOffset + geometry.indices[source + 2u]);
        }
        node.metadata = {0u, 0u, leafIndexOffset, count};
        return nodeIndex;
    }

    size_t splitAxis = 0;
    if (centroidUpper[1] - centroidLower[1] > centroidUpper[splitAxis] - centroidLower[splitAxis])
        splitAxis = 1;
    if (centroidUpper[2] - centroidLower[2] > centroidUpper[splitAxis] - centroidLower[splitAxis])
        splitAxis = 2;
    const uint32_t middle = first + count / 2u;
    std::nth_element(triangles.begin() + first, triangles.begin() + middle, triangles.begin() + first + count,
                     [splitAxis](const BuildTriangle &left, const BuildTriangle &right) {
                         if (left.centroid[splitAxis] != right.centroid[splitAxis])
                             return left.centroid[splitAxis] < right.centroid[splitAxis];
                         return left.sourceTriangle < right.sourceTriangle;
                     });
    const uint32_t leftNode =
        BuildMeshBvhRecursive(geometry, triangles, first, middle - first, vertexOffset, orderedIndices, nodes);
    const uint32_t rightNode =
        BuildMeshBvhRecursive(geometry, triangles, middle, first + count - middle, vertexOffset, orderedIndices, nodes);
    nodes[nodeIndex].metadata = {leftNode, rightNode, 0u, 0u};
    return nodeIndex;
}

bool BuildMeshTopology(const std::vector<GpuParticleCollisionMeshGeometry> &geometries, BuiltMeshTopology &output,
                       std::string *error)
{
    for (const auto &geometry : geometries) {
        if (geometry.identity == 0 || geometry.positions.empty() || geometry.indices.empty() ||
            geometry.indices.size() % 3u != 0u) {
            SetError(error, "GPU particle mesh collision geometry is incomplete");
            return false;
        }
        if (output.geometryByIdentity.find(geometry.identity) != output.geometryByIdentity.end()) {
            SetError(error, "GPU particle mesh collision geometry identity is duplicated");
            return false;
        }
        const uint32_t vertexOffset = static_cast<uint32_t>(output.vertices.size());
        const uint32_t indexOffset = static_cast<uint32_t>(output.indices.size());
        output.vertices.reserve(output.vertices.size() + geometry.positions.size());
        for (const auto &position : geometry.positions) {
            if (!std::isfinite(position[0]) || !std::isfinite(position[1]) || !std::isfinite(position[2])) {
                SetError(error, "GPU particle mesh collision geometry contains a non-finite vertex");
                return false;
            }
            output.vertices.push_back(position);
        }
        const uint32_t triangleCount = static_cast<uint32_t>(geometry.indices.size() / 3u);
        std::vector<BuildTriangle> triangles(triangleCount);
        for (uint32_t triangle = 0; triangle < triangleCount; ++triangle) {
            auto &build = triangles[triangle];
            build.sourceTriangle = triangle;
            build.lower = {std::numeric_limits<float>::max(), std::numeric_limits<float>::max(),
                           std::numeric_limits<float>::max()};
            build.upper = {std::numeric_limits<float>::lowest(), std::numeric_limits<float>::lowest(),
                           std::numeric_limits<float>::lowest()};
            for (uint32_t corner = 0; corner < 3; ++corner) {
                const uint32_t sourceIndex = geometry.indices[triangle * 3u + corner];
                if (sourceIndex >= geometry.positions.size()) {
                    SetError(error, "GPU particle mesh collision index references a missing vertex");
                    return false;
                }
                const auto &position = geometry.positions[sourceIndex];
                for (size_t axis = 0; axis < 3; ++axis) {
                    build.lower[axis] = std::min(build.lower[axis], position[axis]);
                    build.upper[axis] = std::max(build.upper[axis], position[axis]);
                    build.centroid[axis] += position[axis] / 3.0f;
                }
            }
        }
        const uint32_t root =
            BuildMeshBvhRecursive(geometry, triangles, 0u, triangleCount, vertexOffset, output.indices, output.nodes);
        output.geometryByIdentity.emplace(geometry.identity,
                                          std::array<uint32_t, 4>{vertexOffset, indexOffset, root, triangleCount});
    }
    return true;
}

bool PatchMeshGeometry(std::vector<GpuParticleColliderRecord> &records,
                       const std::unordered_map<uint64_t, std::array<uint32_t, 4>> &geometryByIdentity,
                       std::string *error)
{
    for (auto &record : records) {
        if (record.metadata[0] != static_cast<uint32_t>(GpuParticleColliderType::Mesh))
            continue;
        const auto geometry = geometryByIdentity.find(RecordIdentity(record));
        if (geometry == geometryByIdentity.end()) {
            SetError(error, "GPU particle MeshCollider record has no cooked topology payload");
            return false;
        }
        record.geometry = geometry->second;
    }
    return true;
}

GpuParticleCollisionSceneHeader BuildHeader(const GpuParticleCollisionSceneSnapshot &snapshot, uint32_t meshVertexCount,
                                            uint32_t meshIndexCount, uint32_t meshBvhNodeCount)
{
    GpuParticleCollisionSceneHeader header;
    header.colliderCount = static_cast<uint32_t>(snapshot.staticColliders.size() + snapshot.dynamicColliders.size());
    header.staticColliderCount = static_cast<uint32_t>(snapshot.staticColliders.size());
    header.revisionLow = static_cast<uint32_t>(snapshot.revision);
    header.revisionHigh = static_cast<uint32_t>(snapshot.revision >> 32u);
    header.topology = {meshVertexCount, meshIndexCount, meshBvhNodeCount,
                       static_cast<uint32_t>(snapshot.topologyRevision)};
    if (header.colliderCount == 0) {
        header.gridMinInvCellSize = {0.0f, 0.0f, 0.0f, 1.0f};
        header.gridDimensions = {1u, 1u, 1u, 1u};
        return header;
    }

    std::array<float, 3> lower = {std::numeric_limits<float>::max(), std::numeric_limits<float>::max(),
                                  std::numeric_limits<float>::max()};
    std::array<float, 3> upper = {std::numeric_limits<float>::lowest(), std::numeric_limits<float>::lowest(),
                                  std::numeric_limits<float>::lowest()};
    const auto include = [&](const auto &records) {
        for (const auto &record : records) {
            for (size_t axis = 0; axis < 3; ++axis) {
                lower[axis] =
                    std::min(lower[axis], std::min(record.worldAabbMin[axis], record.previousWorldAabbMin[axis]));
                upper[axis] =
                    std::max(upper[axis], std::max(record.worldAabbMax[axis], record.previousWorldAabbMax[axis]));
            }
        }
    };
    include(snapshot.staticColliders);
    include(snapshot.dynamicColliders);
    const float maxExtent = std::max({upper[0] - lower[0], upper[1] - lower[1], upper[2] - lower[2], 1.0e-3f});
    const float cellSize = maxExtent / static_cast<float>(ParticleGpuCollisionScene::GridAxisCells);
    std::array<uint32_t, 3> dimensions{};
    for (size_t axis = 0; axis < 3; ++axis) {
        lower[axis] -= cellSize * 0.001f;
        const float extent = std::max(upper[axis] - lower[axis], 0.0f);
        dimensions[axis] = std::clamp(static_cast<uint32_t>(std::ceil(extent / cellSize)), 1u,
                                      ParticleGpuCollisionScene::GridAxisCells);
    }
    header.gridMinInvCellSize = {lower[0], lower[1], lower[2], 1.0f / cellSize};
    header.gridDimensions = {dimensions[0], dimensions[1], dimensions[2],
                             dimensions[0] * dimensions[1] * dimensions[2]};
    return header;
}

bool BuildGrid(const GpuParticleCollisionSceneHeader &header, const GpuParticleCollisionSceneSnapshot &snapshot,
               std::vector<uint32_t> &offsets, std::vector<uint32_t> &colliderIndices)
{
    const uint32_t cellCount = header.gridDimensions[3];
    if (cellCount == 0 || cellCount > ParticleGpuCollisionScene::MaxGridCellCount)
        return false;

    offsets.assign(static_cast<size_t>(cellCount) + 1u, 0u);
    colliderIndices.clear();
    const float invCellSize = header.gridMinInvCellSize[3];
    const std::array<uint32_t, 3> dimensions = {header.gridDimensions[0], header.gridDimensions[1],
                                                header.gridDimensions[2]};
    if (!(invCellSize > 0.0f) || dimensions[0] == 0 || dimensions[1] == 0 || dimensions[2] == 0)
        return false;

    struct CellRange
    {
        std::array<uint32_t, 3> lower{};
        std::array<uint32_t, 3> upper{};
    };
    std::vector<CellRange> ranges;
    ranges.reserve(snapshot.staticColliders.size() + snapshot.dynamicColliders.size());
    const auto appendRanges = [&](const auto &records) {
        for (const auto &record : records) {
            CellRange range;
            for (size_t axis = 0; axis < 3; ++axis) {
                const float sweptLower = std::min(record.worldAabbMin[axis], record.previousWorldAabbMin[axis]);
                const float sweptUpper = std::max(record.worldAabbMax[axis], record.previousWorldAabbMax[axis]);
                const float lower = (sweptLower - header.gridMinInvCellSize[axis]) * invCellSize;
                const float upper = (sweptUpper - header.gridMinInvCellSize[axis]) * invCellSize;
                if (!std::isfinite(lower) || !std::isfinite(upper))
                    return false;
                const int32_t maxCell = static_cast<int32_t>(dimensions[axis] - 1u);
                range.lower[axis] =
                    static_cast<uint32_t>(std::clamp(static_cast<int32_t>(std::floor(lower)), 0, maxCell));
                range.upper[axis] =
                    static_cast<uint32_t>(std::clamp(static_cast<int32_t>(std::floor(upper)), 0, maxCell));
            }
            ranges.push_back(range);
        }
        return true;
    };
    if (!appendRanges(snapshot.staticColliders) || !appendRanges(snapshot.dynamicColliders))
        return false;

    const auto flatten = [&](uint32_t x, uint32_t y, uint32_t z) {
        return x + dimensions[0] * (y + dimensions[1] * z);
    };
    for (const auto &range : ranges) {
        for (uint32_t z = range.lower[2]; z <= range.upper[2]; ++z)
            for (uint32_t y = range.lower[1]; y <= range.upper[1]; ++y)
                for (uint32_t x = range.lower[0]; x <= range.upper[0]; ++x)
                    ++offsets[flatten(x, y, z) + 1u];
    }
    for (uint32_t cell = 0; cell < cellCount; ++cell)
        offsets[cell + 1u] += offsets[cell];
    if (offsets.back() > ParticleGpuCollisionScene::MaxGridReferenceCount)
        return false;

    colliderIndices.resize(offsets.back());
    std::vector<uint32_t> cursors(offsets.begin(), offsets.end() - 1);
    for (uint32_t colliderIndex = 0; colliderIndex < ranges.size(); ++colliderIndex) {
        const auto &range = ranges[colliderIndex];
        for (uint32_t z = range.lower[2]; z <= range.upper[2]; ++z) {
            for (uint32_t y = range.lower[1]; y <= range.upper[1]; ++y) {
                for (uint32_t x = range.lower[0]; x <= range.upper[0]; ++x) {
                    const uint32_t cell = flatten(x, y, z);
                    colliderIndices[cursors[cell]++] = colliderIndex;
                }
            }
        }
    }
    return true;
}

} // namespace

ParticleGpuCollisionScene::~ParticleGpuCollisionScene()
{
    Destroy();
}

bool ParticleGpuCollisionScene::Create(rhi::Device &device, uint32_t capacity, uint32_t uploadPageCount,
                                       uint32_t meshVertexCapacity, uint32_t meshIndexCapacity,
                                       uint32_t meshBvhNodeCapacity)
{
    Destroy();
    if (capacity == 0 || uploadPageCount == 0 || meshVertexCapacity == 0 || meshIndexCapacity == 0 ||
        meshBvhNodeCapacity == 0 ||
        static_cast<uint64_t>(capacity) > std::numeric_limits<uint64_t>::max() / sizeof(GpuParticleColliderRecord) ||
        static_cast<uint64_t>(meshVertexCapacity) >
            std::numeric_limits<uint64_t>::max() / sizeof(std::array<float, 4>) ||
        static_cast<uint64_t>(meshIndexCapacity) > std::numeric_limits<uint64_t>::max() / sizeof(uint32_t) ||
        static_cast<uint64_t>(meshBvhNodeCapacity) >
            std::numeric_limits<uint64_t>::max() / sizeof(GpuParticleCollisionBvhNode))
        return false;

    m_device = &device;
    m_capacity = capacity;
    m_meshVertexCapacity = meshVertexCapacity;
    m_meshIndexCapacity = meshIndexCapacity;
    m_meshBvhNodeCapacity = meshBvhNodeCapacity;
    const uint64_t colliderBytes = static_cast<uint64_t>(capacity) * sizeof(GpuParticleColliderRecord);
    const uint64_t gridOffsetBytes = static_cast<uint64_t>(MaxGridCellCount + 1u) * sizeof(uint32_t);
    const uint64_t gridIndexBytes = static_cast<uint64_t>(capacity) * MaxGridCellCount * sizeof(uint32_t);
    const uint64_t meshVertexBytes = static_cast<uint64_t>(meshVertexCapacity) * sizeof(std::array<float, 4>);
    const uint64_t meshIndexBytes = static_cast<uint64_t>(meshIndexCapacity) * sizeof(uint32_t);
    const uint64_t meshBvhBytes = static_cast<uint64_t>(meshBvhNodeCapacity) * sizeof(GpuParticleCollisionBvhNode);
    // Create() publishes an empty table, so the first GpuParticle/PrimeSimulation
    // records these copies on the independent Compute family. Graphics-only
    // exclusive buffers hang that queue the same way resident particle state did.
    const auto sharedAccess =
        rhi::QueueAccessFlags::Graphics | rhi::QueueAccessFlags::Compute | rhi::QueueAccessFlags::Transfer;
    const auto createShared = [&](uint64_t bytes, rhi::BufferUsageFlags usage,
                                  rhi::BufferMemory memory = rhi::BufferMemory::DeviceLocal) {
        rhi::BufferDesc desc;
        desc.byteSize = bytes;
        desc.usage = usage;
        desc.memory = memory;
        desc.queueAccess = sharedAccess;
        return device.CreateBuffer(desc);
    };
    const auto destUsage = rhi::BufferUsageFlags::Storage | rhi::BufferUsageFlags::TransferDestination;
    m_headerBuffer = createShared(sizeof(GpuParticleCollisionSceneHeader), destUsage);
    m_colliderBuffer = createShared(colliderBytes, destUsage);
    m_gridOffsetBuffer = createShared(gridOffsetBytes, destUsage);
    m_gridColliderIndexBuffer = createShared(gridIndexBytes, destUsage);
    m_meshVertexBuffer = createShared(meshVertexBytes, destUsage);
    m_meshIndexBuffer = createShared(meshIndexBytes, destUsage);
    m_meshBvhBuffer = createShared(meshBvhBytes, destUsage);
    m_uploadPages.resize(uploadPageCount);
    for (auto &page : m_uploadPages) {
        page.header = createShared(sizeof(GpuParticleCollisionSceneHeader), rhi::BufferUsageFlags::TransferSource,
                                   rhi::BufferMemory::Upload);
        page.colliders = createShared(colliderBytes, rhi::BufferUsageFlags::TransferSource, rhi::BufferMemory::Upload);
        page.gridOffsets =
            createShared(gridOffsetBytes, rhi::BufferUsageFlags::TransferSource, rhi::BufferMemory::Upload);
        page.gridColliderIndices =
            createShared(gridIndexBytes, rhi::BufferUsageFlags::TransferSource, rhi::BufferMemory::Upload);
        page.meshVertices =
            createShared(meshVertexBytes, rhi::BufferUsageFlags::TransferSource, rhi::BufferMemory::Upload);
        page.meshIndices =
            createShared(meshIndexBytes, rhi::BufferUsageFlags::TransferSource, rhi::BufferMemory::Upload);
        page.meshBvhNodes =
            createShared(meshBvhBytes, rhi::BufferUsageFlags::TransferSource, rhi::BufferMemory::Upload);
    }
    if (!IsValid()) {
        Destroy();
        return false;
    }

    GpuParticleCollisionSceneSnapshot empty;
    empty.revision = 1;
    empty.topologyRevision = 1;
    empty.replaceMeshTopology = true;
    return Publish(empty);
}

void ParticleGpuCollisionScene::Destroy() noexcept
{
    if (m_device) {
        m_device->Release(m_headerBuffer);
        m_device->Release(m_colliderBuffer);
        m_device->Release(m_gridOffsetBuffer);
        m_device->Release(m_gridColliderIndexBuffer);
        m_device->Release(m_meshVertexBuffer);
        m_device->Release(m_meshIndexBuffer);
        m_device->Release(m_meshBvhBuffer);
        for (auto &page : m_uploadPages) {
            m_device->Release(page.header);
            m_device->Release(page.colliders);
            m_device->Release(page.gridOffsets);
            m_device->Release(page.gridColliderIndices);
            m_device->Release(page.meshVertices);
            m_device->Release(page.meshIndices);
            m_device->Release(page.meshBvhNodes);
        }
    }
    m_device = nullptr;
    m_headerBuffer = {};
    m_colliderBuffer = {};
    m_gridOffsetBuffer = {};
    m_gridColliderIndexBuffer = {};
    m_meshVertexBuffer = {};
    m_meshIndexBuffer = {};
    m_meshBvhBuffer = {};
    m_uploadPages.clear();
    m_capacity = 0;
    m_meshVertexCapacity = 0;
    m_meshIndexCapacity = 0;
    m_meshBvhNodeCapacity = 0;
    m_pendingUploadPage = 0;
    m_nextUploadPage = 0;
    m_pendingColliderCount = 0;
    m_pendingStaticColliderCount = 0;
    m_publishedColliderCount = 0;
    m_publishedStaticColliderCount = 0;
    m_pendingGridOffsetCount = 0;
    m_pendingGridReferenceCount = 0;
    m_publishedGridReferenceCount = 0;
    m_publishedMeshVertexCount = 0;
    m_publishedMeshIndexCount = 0;
    m_publishedMeshBvhNodeCount = 0;
    m_pendingCopyOffset = 0;
    m_pendingCopyBytes = 0;
    m_pendingRevision = 0;
    m_publishedRevision = 0;
    m_pendingTopologyRevision = 0;
    m_publishedTopologyRevision = 0;
    m_stagedStaticColliders.clear();
    m_stagedDynamicColliders.clear();
    m_stagedGridOffsets.clear();
    m_stagedGridColliderIndices.clear();
    m_stagedMeshVertices.clear();
    m_stagedMeshIndices.clear();
    m_stagedMeshBvhNodes.clear();
    m_stagedGeometryByIdentity.clear();
    m_pendingMeshVertexCount = 0;
    m_pendingMeshIndexCount = 0;
    m_pendingMeshBvhNodeCount = 0;
    m_pendingTopologyUpload = false;
    m_uploadPending = false;
}

bool ParticleGpuCollisionScene::Publish(const GpuParticleCollisionSceneSnapshot &snapshot, std::string *error)
{
    if (error)
        error->clear();
    if (!IsValid() || snapshot.revision == 0 || snapshot.topologyRevision == 0) {
        SetError(error, "GPU particle collision snapshot is invalid");
        return false;
    }

    const uint64_t currentTopologyRevision =
        m_pendingTopologyRevision != 0 ? m_pendingTopologyRevision : m_publishedTopologyRevision;
    BuiltMeshTopology replacementTopology;
    const auto *geometryByIdentity = &m_stagedGeometryByIdentity;
    uint32_t meshVertexCount = static_cast<uint32_t>(m_stagedMeshVertices.size());
    uint32_t meshIndexCount = static_cast<uint32_t>(m_stagedMeshIndices.size());
    uint32_t meshBvhNodeCount = static_cast<uint32_t>(m_stagedMeshBvhNodes.size());
    if (snapshot.replaceMeshTopology) {
        if (snapshot.topologyRevision <= currentTopologyRevision) {
            SetError(error, "GPU particle collision topology revision did not advance");
            return false;
        }
        if (!BuildMeshTopology(snapshot.meshGeometries, replacementTopology, error))
            return false;
        if (replacementTopology.vertices.size() > m_meshVertexCapacity ||
            replacementTopology.indices.size() > m_meshIndexCapacity ||
            replacementTopology.nodes.size() > m_meshBvhNodeCapacity) {
            SetError(error, "GPU particle collision mesh topology exceeds its fixed capacity");
            return false;
        }
        geometryByIdentity = &replacementTopology.geometryByIdentity;
        meshVertexCount = static_cast<uint32_t>(replacementTopology.vertices.size());
        meshIndexCount = static_cast<uint32_t>(replacementTopology.indices.size());
        meshBvhNodeCount = static_cast<uint32_t>(replacementTopology.nodes.size());
    } else if (!snapshot.meshGeometries.empty() || snapshot.topologyRevision != currentTopologyRevision) {
        SetError(error, "GPU particle collision topology changed without an explicit replacement");
        return false;
    }

    GpuParticleCollisionSceneSnapshot effectiveSnapshot;
    effectiveSnapshot.revision = snapshot.revision;
    effectiveSnapshot.topologyRevision = snapshot.topologyRevision;
    effectiveSnapshot.staticColliders = snapshot.staticColliders;
    effectiveSnapshot.dynamicColliders = snapshot.dynamicColliders;
    std::sort(effectiveSnapshot.staticColliders.begin(), effectiveSnapshot.staticColliders.end(), RecordIdentityLess);
    std::sort(effectiveSnapshot.dynamicColliders.begin(), effectiveSnapshot.dynamicColliders.end(), RecordIdentityLess);
    if (!ValidateRecordIdentities(effectiveSnapshot.staticColliders, effectiveSnapshot.dynamicColliders)) {
        SetError(error, "GPU particle collision snapshot contains a zero or duplicate collider identity");
        return false;
    }
    if (!PatchMeshGeometry(effectiveSnapshot.staticColliders, *geometryByIdentity, error) ||
        !PatchMeshGeometry(effectiveSnapshot.dynamicColliders, *geometryByIdentity, error))
        return false;

    const uint64_t currentRevision = m_uploadPending ? m_pendingRevision : m_publishedRevision;
    if (snapshot.revision < currentRevision) {
        SetError(error, "GPU particle collision snapshot revision moved backwards");
        return false;
    }
    if (snapshot.revision == currentRevision) {
        if (!snapshot.replaceMeshTopology && EqualRecords(effectiveSnapshot.staticColliders, m_stagedStaticColliders) &&
            EqualRecords(effectiveSnapshot.dynamicColliders, m_stagedDynamicColliders))
            return true;
        SetError(error, "GPU particle collision snapshot changed without a new revision");
        return false;
    }
    const size_t colliderCount = effectiveSnapshot.staticColliders.size() + effectiveSnapshot.dynamicColliders.size();
    if (colliderCount > m_capacity) {
        SetError(error, "GPU particle collision scene exceeds its collider capacity");
        return false;
    }

    const GpuParticleCollisionSceneHeader header =
        BuildHeader(effectiveSnapshot, meshVertexCount, meshIndexCount, meshBvhNodeCount);
    std::vector<uint32_t> gridOffsets;
    std::vector<uint32_t> gridColliderIndices;
    if (!BuildGrid(header, effectiveSnapshot, gridOffsets, gridColliderIndices)) {
        SetError(error, "GPU particle collision broadphase grid build failed");
        return false;
    }
    if (gridColliderIndices.size() > static_cast<size_t>(m_capacity) * MaxGridCellCount) {
        SetError(error, "GPU particle collision broadphase grid exceeds its reference capacity");
        return false;
    }
    const uint32_t uploadPage = m_uploadPending ? m_pendingUploadPage : m_nextUploadPage;
    auto &page = m_uploadPages[uploadPage];
    if (!m_device->WriteBuffer(page.header, 0, &header, sizeof(header))) {
        SetError(error, "GPU particle collision header staging upload failed");
        return false;
    }
    if (!m_device->WriteBuffer(page.gridOffsets, 0, gridOffsets.data(), gridOffsets.size() * sizeof(uint32_t))) {
        SetError(error, "GPU particle collision grid offset staging upload failed");
        return false;
    }
    if (!gridColliderIndices.empty() && !m_device->WriteBuffer(page.gridColliderIndices, 0, gridColliderIndices.data(),
                                                               gridColliderIndices.size() * sizeof(uint32_t))) {
        SetError(error, "GPU particle collision grid index staging upload failed");
        return false;
    }
    if (snapshot.replaceMeshTopology) {
        if (!replacementTopology.vertices.empty() &&
            !m_device->WriteBuffer(page.meshVertices, 0, replacementTopology.vertices.data(),
                                   replacementTopology.vertices.size() * sizeof(replacementTopology.vertices[0]))) {
            SetError(error, "GPU particle mesh vertex staging upload failed");
            return false;
        }
        if (!replacementTopology.indices.empty() &&
            !m_device->WriteBuffer(page.meshIndices, 0, replacementTopology.indices.data(),
                                   replacementTopology.indices.size() * sizeof(uint32_t))) {
            SetError(error, "GPU particle mesh index staging upload failed");
            return false;
        }
        if (!replacementTopology.nodes.empty() &&
            !m_device->WriteBuffer(page.meshBvhNodes, 0, replacementTopology.nodes.data(),
                                   replacementTopology.nodes.size() * sizeof(GpuParticleCollisionBvhNode))) {
            SetError(error, "GPU particle mesh BVH staging upload failed");
            return false;
        }
    }

    const bool staticChanged = !EqualRecords(effectiveSnapshot.staticColliders, m_stagedStaticColliders);
    const bool dynamicChanged = !EqualRecords(effectiveSnapshot.dynamicColliders, m_stagedDynamicColliders);
    const uint64_t staticBytes = effectiveSnapshot.staticColliders.size() * sizeof(GpuParticleColliderRecord);
    const uint64_t dynamicBytes = effectiveSnapshot.dynamicColliders.size() * sizeof(GpuParticleColliderRecord);
    if (staticChanged) {
        if (staticBytes > 0 &&
            !m_device->WriteBuffer(page.colliders, 0, effectiveSnapshot.staticColliders.data(), staticBytes)) {
            SetError(error, "GPU particle static collider staging upload failed");
            return false;
        }
        if (dynamicBytes > 0 && !m_device->WriteBuffer(page.colliders, staticBytes,
                                                       effectiveSnapshot.dynamicColliders.data(), dynamicBytes)) {
            SetError(error, "GPU particle dynamic collider staging upload failed");
            return false;
        }
        m_pendingCopyOffset = 0;
        m_pendingCopyBytes = staticBytes + dynamicBytes;
    } else if (dynamicChanged) {
        if (dynamicBytes > 0 && !m_device->WriteBuffer(page.colliders, staticBytes,
                                                       effectiveSnapshot.dynamicColliders.data(), dynamicBytes)) {
            SetError(error, "GPU particle dynamic collider staging upload failed");
            return false;
        }
        m_pendingCopyOffset = staticBytes;
        m_pendingCopyBytes = dynamicBytes;
    } else {
        m_pendingCopyOffset = 0;
        m_pendingCopyBytes = 0;
    }

    m_stagedStaticColliders = std::move(effectiveSnapshot.staticColliders);
    m_stagedDynamicColliders = std::move(effectiveSnapshot.dynamicColliders);
    m_stagedGridOffsets = std::move(gridOffsets);
    m_stagedGridColliderIndices = std::move(gridColliderIndices);
    if (snapshot.replaceMeshTopology) {
        m_stagedMeshVertices = std::move(replacementTopology.vertices);
        m_stagedMeshIndices = std::move(replacementTopology.indices);
        m_stagedMeshBvhNodes = std::move(replacementTopology.nodes);
        m_stagedGeometryByIdentity = std::move(replacementTopology.geometryByIdentity);
        m_pendingMeshVertexCount = static_cast<uint32_t>(m_stagedMeshVertices.size());
        m_pendingMeshIndexCount = static_cast<uint32_t>(m_stagedMeshIndices.size());
        m_pendingMeshBvhNodeCount = static_cast<uint32_t>(m_stagedMeshBvhNodes.size());
        m_pendingTopologyUpload = true;
    }
    m_pendingColliderCount = static_cast<uint32_t>(colliderCount);
    m_pendingStaticColliderCount = static_cast<uint32_t>(snapshot.staticColliders.size());
    m_pendingGridOffsetCount = static_cast<uint32_t>(m_stagedGridOffsets.size());
    m_pendingGridReferenceCount = static_cast<uint32_t>(m_stagedGridColliderIndices.size());
    m_pendingRevision = snapshot.revision;
    m_pendingTopologyRevision = snapshot.topologyRevision;
    m_pendingUploadPage = uploadPage;
    m_uploadPending = true;
    return true;
}

bool ParticleGpuCollisionScene::RecordPendingUpload(const rhi::TransferCommandEncoder &encoder)
{
    if (!m_uploadPending)
        return true;
    if (!IsValid() || !encoder.IsValid())
        return false;

    const auto &page = m_uploadPages[m_pendingUploadPage];
    encoder.CopyBuffer(page.header, m_headerBuffer, {0, 0, sizeof(GpuParticleCollisionSceneHeader)});
    encoder.CopyBuffer(page.gridOffsets, m_gridOffsetBuffer,
                       {0, 0, static_cast<uint64_t>(m_pendingGridOffsetCount) * sizeof(uint32_t)});
    if (m_pendingGridReferenceCount > 0) {
        encoder.CopyBuffer(page.gridColliderIndices, m_gridColliderIndexBuffer,
                           {0, 0, static_cast<uint64_t>(m_pendingGridReferenceCount) * sizeof(uint32_t)});
    }
    if (m_pendingCopyBytes > 0) {
        encoder.CopyBuffer(page.colliders, m_colliderBuffer,
                           {m_pendingCopyOffset, m_pendingCopyOffset, m_pendingCopyBytes});
    }
    if (m_pendingTopologyUpload) {
        if (m_pendingMeshVertexCount > 0) {
            encoder.CopyBuffer(page.meshVertices, m_meshVertexBuffer,
                               {0, 0, static_cast<uint64_t>(m_pendingMeshVertexCount) * sizeof(std::array<float, 4>)});
        }
        if (m_pendingMeshIndexCount > 0) {
            encoder.CopyBuffer(page.meshIndices, m_meshIndexBuffer,
                               {0, 0, static_cast<uint64_t>(m_pendingMeshIndexCount) * sizeof(uint32_t)});
        }
        if (m_pendingMeshBvhNodeCount > 0) {
            encoder.CopyBuffer(
                page.meshBvhNodes, m_meshBvhBuffer,
                {0, 0, static_cast<uint64_t>(m_pendingMeshBvhNodeCount) * sizeof(GpuParticleCollisionBvhNode)});
        }
        m_publishedMeshVertexCount = m_pendingMeshVertexCount;
        m_publishedMeshIndexCount = m_pendingMeshIndexCount;
        m_publishedMeshBvhNodeCount = m_pendingMeshBvhNodeCount;
    }
    m_publishedColliderCount = m_pendingColliderCount;
    m_publishedStaticColliderCount = m_pendingStaticColliderCount;
    m_publishedGridReferenceCount = m_pendingGridReferenceCount;
    m_publishedRevision = m_pendingRevision;
    m_publishedTopologyRevision = m_pendingTopologyRevision;
    m_pendingRevision = 0;
    m_pendingTopologyRevision = 0;
    m_nextUploadPage = (m_pendingUploadPage + 1u) % static_cast<uint32_t>(m_uploadPages.size());
    m_pendingCopyOffset = 0;
    m_pendingCopyBytes = 0;
    m_pendingGridOffsetCount = 0;
    m_pendingGridReferenceCount = 0;
    m_pendingTopologyUpload = false;
    m_uploadPending = false;
    return true;
}

bool ParticleGpuCollisionScene::IsValid() const noexcept
{
    return m_device && m_capacity > 0 && m_headerBuffer.IsValid() && m_colliderBuffer.IsValid() &&
           m_gridOffsetBuffer.IsValid() && m_gridColliderIndexBuffer.IsValid() && m_meshVertexBuffer.IsValid() &&
           m_meshIndexBuffer.IsValid() && m_meshBvhBuffer.IsValid() && !m_uploadPages.empty() &&
           std::all_of(m_uploadPages.begin(), m_uploadPages.end(),
                       [](const UploadPage &page) { return page.IsValid(); });
}

} // namespace infernux::particle
