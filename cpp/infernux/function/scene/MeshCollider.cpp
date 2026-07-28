/**
 * @file MeshCollider.cpp
 * @brief MeshCollider implementation — triangle mesh or convex hull shape creation.
 */

// Jolt/Jolt.h MUST be the very first Jolt include in this TU
#include <Jolt/Jolt.h>
#include <Jolt/Geometry/ConvexHullBuilder.h>
#include <Jolt/Physics/Collision/Shape/ConvexHullShape.h>
#include <Jolt/Physics/Collision/Shape/MeshShape.h>
#include <Jolt/Physics/Collision/Shape/RotatedTranslatedShape.h>

#include "ComponentDocumentValidation.h"
#include "MeshCollider.h"

#include "ComponentFactory.h"
#include "GameObject.h"
#include "MeshRenderer.h"
#include "Rigidbody.h"
#include "core/threading/JobSystem.h"
#include "physics/PhysicsECSStore.h"

#include <atomic>
#include <cfloat>
#include <cmath>
#include <core/log/InxLog.h>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <nlohmann/json.hpp>
#include <unordered_map>
#include <vector>

namespace infernux
{

namespace
{
struct CookedGeometry
{
    std::vector<glm::vec3> vertices;
    std::vector<uint32_t> indices;
    std::vector<glm::vec3> hullPositions;
    std::vector<uint32_t> hullEdges;
    std::vector<glm::vec3> collisionPositions;
    std::vector<uint32_t> collisionIndices;
};

struct CookingCacheKey
{
    uint64_t hashA = 0;
    uint64_t hashB = 0;
    size_t vertexCount = 0;
    size_t indexCount = 0;
    bool convex = false;

    bool operator==(const CookingCacheKey &other) const
    {
        return hashA == other.hashA && hashB == other.hashB && vertexCount == other.vertexCount &&
               indexCount == other.indexCount && convex == other.convex;
    }
};

struct CookingCacheKeyHash
{
    size_t operator()(const CookingCacheKey &key) const
    {
        return static_cast<size_t>(key.hashA ^
                                   (key.hashB + 0x9e3779b97f4a7c15ULL + (key.hashA << 6) + (key.hashA >> 2)));
    }
};

constexpr size_t kMaxCookingCacheEntries = 128;
std::mutex g_cookingCacheMutex;
std::unordered_map<CookingCacheKey, std::shared_ptr<const CookedGeometry>, CookingCacheKeyHash> g_cookingCache;
std::deque<CookingCacheKey> g_cookingCacheOrder;
std::atomic<size_t> g_cookingCacheHits{0};
std::atomic<size_t> g_cookingCacheMisses{0};
std::atomic<size_t> g_asyncCookingSubmissions{0};

struct CookingWaiter
{
    PhysicsECSStore::ColliderHandle handle;
    uint64_t revision = 0;
};

struct InflightCooking
{
    JobHandle job;
    std::vector<CookingWaiter> waiters;
};

struct CookingCompletion
{
    CookingCacheKey key;
    std::shared_ptr<const CookedGeometry> geometry;
    std::string error;
};

// In-flight ownership is main-thread only. Workers publish immutable payloads
// through the completion queue and never retain Collider/GameObject pointers.
std::unordered_map<CookingCacheKey, InflightCooking, CookingCacheKeyHash> g_inflightCooking;
std::mutex g_cookingCompletionMutex;
std::deque<CookingCompletion> g_cookingCompletions;

CookingCacheKey HashGeometry(const std::vector<glm::vec3> &vertices, const std::vector<uint32_t> &indices, bool convex)
{
    CookingCacheKey key{14695981039346656037ULL, 1099511628211ULL, vertices.size(), indices.size(), convex};
    const auto append = [&key](const void *data, size_t size) {
        const auto *bytes = static_cast<const unsigned char *>(data);
        for (size_t index = 0; index < size; ++index) {
            key.hashA ^= bytes[index];
            key.hashA *= 1099511628211ULL;
            key.hashB ^= static_cast<uint64_t>(bytes[index]) + 0x9e3779b97f4a7c15ULL;
            key.hashB *= 14029467366897019727ULL;
        }
    };
    append(&convex, sizeof(convex));
    for (const auto &vertex : vertices) {
        append(&vertex.x, sizeof(vertex.x));
        append(&vertex.y, sizeof(vertex.y));
        append(&vertex.z, sizeof(vertex.z));
    }
    if (!indices.empty())
        append(indices.data(), indices.size() * sizeof(uint32_t));
    return key;
}

std::shared_ptr<const CookedGeometry> FindCookedGeometry(const CookingCacheKey &key)
{
    std::lock_guard<std::mutex> lock(g_cookingCacheMutex);
    const auto it = g_cookingCache.find(key);
    if (it == g_cookingCache.end())
        return nullptr;
    ++g_cookingCacheHits;
    return it->second;
}

void StoreCookedGeometry(const CookingCacheKey &key, std::shared_ptr<const CookedGeometry> geometry)
{
    std::lock_guard<std::mutex> lock(g_cookingCacheMutex);
    if (g_cookingCache.find(key) != g_cookingCache.end())
        return;
    while (g_cookingCache.size() >= kMaxCookingCacheEntries) {
        g_cookingCache.erase(g_cookingCacheOrder.front());
        g_cookingCacheOrder.pop_front();
    }
    g_cookingCache.emplace(key, std::move(geometry));
    g_cookingCacheOrder.push_back(key);
}

JPH::Shape *CreateShapeFromCookedGeometry(const CookedGeometry &geometry, bool convex, std::string &error)
{
    const auto finish = [&error, convex](const JPH::ShapeSettings::ShapeResult &result) -> JPH::Shape * {
        if (result.HasError()) {
            error =
                std::string(convex ? "cached convex shape creation failed: " : "cached mesh shape creation failed: ") +
                result.GetError().c_str();
            return nullptr;
        }
        auto *shape = const_cast<JPH::Shape *>(result.Get().GetPtr());
        shape->AddRef();
        return shape;
    };
    if (convex) {
        JPH::Array<JPH::Vec3> points;
        points.reserve(static_cast<int>(geometry.vertices.size()));
        for (const auto &vertex : geometry.vertices)
            points.emplace_back(vertex.x, vertex.y, vertex.z);
        JPH::ConvexHullShapeSettings settings(points);
        const auto result = settings.Create();
        return finish(result);
    } else {
        JPH::MeshShapeSettings settings;
        settings.mTriangleVertices.reserve(static_cast<int>(geometry.vertices.size()));
        for (const auto &vertex : geometry.vertices)
            settings.mTriangleVertices.emplace_back(vertex.x, vertex.y, vertex.z);
        settings.mIndexedTriangles.reserve(static_cast<int>(geometry.indices.size() / 3));
        for (size_t index = 0; index + 2 < geometry.indices.size(); index += 3) {
            settings.mIndexedTriangles.emplace_back(geometry.indices[index], geometry.indices[index + 1],
                                                    geometry.indices[index + 2]);
        }
        settings.SetEmbedded();
        const auto result = settings.Create();
        return finish(result);
    }
}
} // namespace

void MeshCollider::ClearCookingCache()
{
    FlushCompletedCooking(true);
    std::lock_guard<std::mutex> lock(g_cookingCacheMutex);
    g_cookingCache.clear();
    g_cookingCacheOrder.clear();
    g_cookingCacheHits.store(0);
    g_cookingCacheMisses.store(0);
    g_asyncCookingSubmissions.store(0);
}

std::pair<size_t, size_t> MeshCollider::GetCookingCacheStats()
{
    return {g_cookingCacheHits.load(), g_cookingCacheMisses.load()};
}

size_t MeshCollider::GetPendingCookingCount()
{
    return g_inflightCooking.size();
}

size_t MeshCollider::GetAsyncCookingSubmissionCount()
{
    return g_asyncCookingSubmissions.load();
}

// ---------------------------------------------------------------------------
// Mesh cooking — mirrors Unity's MeshCollider cooking options:
//   WeldColocatedVertices + EnableMeshCleaning.
// Merges near-duplicate vertices and removes degenerate triangles so that
// Jolt's MeshShape receives clean, well-formed geometry.
// ---------------------------------------------------------------------------
static void CookMeshGeometry(std::vector<glm::vec3> &vertices, std::vector<uint32_t> &indices)
{
    if (vertices.empty() || indices.size() < 3)
        return;

    // --- Step 1: Weld colocated vertices ---
    // Quantize vertex positions into a grid; vertices that fall into the same
    // cell are treated as identical.  This eliminates duplicates created by
    // different normals / UVs in the rendering mesh.
    constexpr float kWeldCellSize = 1e-4f;
    const float invCell = 1.0f / kWeldCellSize;

    struct IVec3Hash
    {
        size_t operator()(const glm::ivec3 &v) const
        {
            size_t h = std::hash<int>()(v.x);
            h ^= std::hash<int>()(v.y) + 0x9e3779b9 + (h << 6) + (h >> 2);
            h ^= std::hash<int>()(v.z) + 0x9e3779b9 + (h << 6) + (h >> 2);
            return h;
        }
    };

    std::unordered_map<glm::ivec3, uint32_t, IVec3Hash> posMap;
    std::vector<glm::vec3> newVerts;
    std::vector<uint32_t> remap(vertices.size());
    newVerts.reserve(vertices.size());

    for (size_t i = 0; i < vertices.size(); ++i) {
        glm::ivec3 key(static_cast<int>(std::round(vertices[i].x * invCell)),
                       static_cast<int>(std::round(vertices[i].y * invCell)),
                       static_cast<int>(std::round(vertices[i].z * invCell)));
        auto it = posMap.find(key);
        if (it != posMap.end()) {
            remap[i] = it->second;
        } else {
            uint32_t idx = static_cast<uint32_t>(newVerts.size());
            posMap[key] = idx;
            newVerts.push_back(vertices[i]);
            remap[i] = idx;
        }
    }

    for (auto &idx : indices)
        idx = remap[idx];
    vertices = std::move(newVerts);

    // --- Step 2: Remove degenerate triangles ---
    // Discard triangles with collapsed indices or near-zero area.
    constexpr float kMinTriAreaSq = 1e-12f;

    std::vector<uint32_t> clean;
    clean.reserve(indices.size());

    for (size_t i = 0; i + 2 < indices.size(); i += 3) {
        uint32_t i0 = indices[i], i1 = indices[i + 1], i2 = indices[i + 2];
        if (i0 == i1 || i1 == i2 || i0 == i2)
            continue;
        glm::vec3 e1 = vertices[i1] - vertices[i0];
        glm::vec3 e2 = vertices[i2] - vertices[i0];
        float areaSq = glm::dot(glm::cross(e1, e2), glm::cross(e1, e2));
        if (areaSq < kMinTriAreaSq)
            continue;
        clean.push_back(i0);
        clean.push_back(i1);
        clean.push_back(i2);
    }

    indices = std::move(clean);
}

// ---------------------------------------------------------------------------
// Winding-order correction for Jolt MeshShape (CCW = outward-facing front).
//
// Uses the signed-volume heuristic (divergence theorem): for a *closed* mesh,
// positive signed volume ⇒ CCW-outward winding; negative ⇒ winding is
// inverted.  For near-planar / open meshes the signed volume is tiny, so we
// fall back to checking the scale determinant (negative ⇒ odd number of axis
// reflections which flips winding).
// ---------------------------------------------------------------------------
static void EnsureOutwardWinding(std::vector<glm::vec3> &vertices, std::vector<uint32_t> &indices,
                                 const glm::vec3 &worldScale)
{
    if (indices.size() < 3)
        return;

    // Compute signed volume × 6 in double precision (already-scaled vertices).
    double signedVol6 = 0.0;
    for (size_t i = 0; i + 2 < indices.size(); i += 3) {
        const glm::dvec3 a(vertices[indices[i]]);
        const glm::dvec3 b(vertices[indices[i + 1]]);
        const glm::dvec3 c(vertices[indices[i + 2]]);
        signedVol6 += glm::dot(a, glm::cross(b, c));
    }

    // Compare against AABB volume to decide whether the mesh has enough
    // "closed" volume for the heuristic to be reliable.
    glm::vec3 mn(FLT_MAX), mx(-FLT_MAX);
    for (const auto &v : vertices) {
        mn = glm::min(mn, v);
        mx = glm::max(mx, v);
    }
    double aabbVol =
        static_cast<double>(mx.x - mn.x) * static_cast<double>(mx.y - mn.y) * static_cast<double>(mx.z - mn.z);

    bool needFlip = false;
    if (std::abs(signedVol6) > aabbVol * 0.01) {
        // Mesh has enough closed volume — trust the signed-volume test.
        needFlip = (signedVol6 < 0.0);
    } else {
        // Flat / open mesh — fall back to scale-determinant check.
        needFlip = (worldScale.x * worldScale.y * worldScale.z < 0.0f);
    }

    if (needFlip) {
        for (size_t i = 0; i + 2 < indices.size(); i += 3) {
            std::swap(indices[i + 1], indices[i + 2]);
        }
        INXLOG_INFO("MeshCollider: flipped triangle winding (signed-vol=", signedVol6, ", aabb-vol=", aabbVol,
                    ", need-flip=true)");
    }
}

static std::shared_ptr<const CookedGeometry> CookGeometry(std::vector<glm::vec3> vertices,
                                                          std::vector<uint32_t> indices, bool convex,
                                                          const glm::vec3 &worldScale, std::string &error)
{
    auto cooked = std::make_shared<CookedGeometry>();
    if (convex) {
        constexpr int kMaxConvexVerts = 100;
        JPH::Array<JPH::Vec3> allPoints;
        allPoints.reserve(static_cast<int>(vertices.size()));
        for (const auto &vertex : vertices)
            allPoints.emplace_back(vertex.x, vertex.y, vertex.z);

        JPH::ConvexHullBuilder builder(allPoints);
        const char *buildError = nullptr;
        const auto buildResult = builder.Initialize(kMaxConvexVerts, 1.0e-3f, buildError);
        JPH::Array<JPH::Vec3> hullPoints;
        std::unordered_map<int, uint32_t> originalToCompact;

        if (buildResult == JPH::ConvexHullBuilder::EResult::Success ||
            buildResult == JPH::ConvexHullBuilder::EResult::MaxVerticesReached) {
            std::vector<bool> used(allPoints.size(), false);
            for (const auto *face : builder.GetFaces()) {
                const auto *edge = face->mFirstEdge;
                do {
                    used[static_cast<size_t>(edge->mStartIdx)] = true;
                    edge = edge->mNextEdge;
                } while (edge != face->mFirstEdge);
            }
            for (size_t index = 0; index < allPoints.size(); ++index) {
                if (!used[index])
                    continue;
                originalToCompact[static_cast<int>(index)] = static_cast<uint32_t>(hullPoints.size());
                hullPoints.push_back(allPoints[index]);
            }

            const float inverseX = worldScale.x != 0.0f ? 1.0f / worldScale.x : 1.0f;
            const float inverseY = worldScale.y != 0.0f ? 1.0f / worldScale.y : 1.0f;
            const float inverseZ = worldScale.z != 0.0f ? 1.0f / worldScale.z : 1.0f;
            cooked->hullPositions.reserve(hullPoints.size());
            for (const auto &point : hullPoints) {
                cooked->hullPositions.emplace_back(point.GetX() * inverseX, point.GetY() * inverseY,
                                                   point.GetZ() * inverseZ);
            }
            for (const auto *face : builder.GetFaces()) {
                const auto *edge = face->mFirstEdge;
                std::vector<uint32_t> faceIndices;
                do {
                    const uint32_t start = originalToCompact.at(edge->mStartIdx);
                    const uint32_t end = originalToCompact.at(edge->mNextEdge->mStartIdx);
                    cooked->hullEdges.push_back(start);
                    cooked->hullEdges.push_back(end);
                    faceIndices.push_back(start);
                    edge = edge->mNextEdge;
                } while (edge != face->mFirstEdge);
                for (size_t index = 1; index + 1 < faceIndices.size(); ++index) {
                    cooked->collisionIndices.push_back(faceIndices[0]);
                    cooked->collisionIndices.push_back(faceIndices[index]);
                    cooked->collisionIndices.push_back(faceIndices[index + 1]);
                }
            }
            cooked->collisionPositions = cooked->hullPositions;
        }

        if (hullPoints.empty())
            hullPoints = std::move(allPoints);
        cooked->vertices.reserve(hullPoints.size());
        for (const auto &point : hullPoints)
            cooked->vertices.emplace_back(point.GetX(), point.GetY(), point.GetZ());
        if (cooked->vertices.empty())
            error = buildError ? std::string("convex mesh cooking failed: ") + buildError
                               : "convex mesh cooking produced no vertices";
        return error.empty() ? cooked : nullptr;
    }

    CookMeshGeometry(vertices, indices);
    EnsureOutwardWinding(vertices, indices, worldScale);
    if (indices.size() < 3) {
        error = "mesh cooking removed every triangle";
        return nullptr;
    }
    const glm::vec3 inverseScale(worldScale.x != 0.0f ? 1.0f / worldScale.x : 1.0f,
                                 worldScale.y != 0.0f ? 1.0f / worldScale.y : 1.0f,
                                 worldScale.z != 0.0f ? 1.0f / worldScale.z : 1.0f);
    cooked->collisionPositions.reserve(vertices.size());
    for (const auto &vertex : vertices)
        cooked->collisionPositions.emplace_back(vertex.x * inverseScale.x, vertex.y * inverseScale.y,
                                                vertex.z * inverseScale.z);
    cooked->collisionIndices = indices;
    cooked->vertices = std::move(vertices);
    cooked->indices = std::move(indices);
    return cooked;
}

INFERNUX_REGISTER_VALIDATED_COMPONENT("MeshCollider", MeshCollider)

void MeshCollider::Awake()
{
    if (auto *go = GetGameObject()) {
        if (auto *rigidbody = go->GetComponent<Rigidbody>();
            rigidbody && rigidbody->IsEnabled() && !rigidbody->IsKinematic()) {
            m_convex = true;
        }
    }
    Collider::Awake();
}

void MeshCollider::SetConvex(bool convex)
{
    if (!convex) {
        if (auto *go = GetGameObject()) {
            if (auto *rigidbody = go->GetComponent<Rigidbody>();
                rigidbody && rigidbody->IsEnabled() && !rigidbody->IsKinematic()) {
                throw std::invalid_argument("dynamic Rigidbody requires MeshCollider.convex = true");
            }
        }
    }
    if (m_convex == convex) {
        return;
    }
    m_convex = convex;
    InvalidatePendingCooking();
    RebuildShape();
}

void MeshCollider::OnMeshGeometryChanged()
{
    InvalidatePendingCooking();
    RebuildShape();
}

void MeshCollider::InvalidatePendingCooking() const
{
    ++m_cookingRevision;
    m_cookingPending = false;
}

void MeshCollider::AutoFitToMesh()
{
    // MeshCollider uses the actual mesh vertices directly,
    // so center should remain at origin (no offset needed).
    DataMut().center = glm::vec3(0.0f);
}

bool MeshCollider::CollectMeshGeometry(std::vector<glm::vec3> &outVertices, std::vector<uint32_t> &outIndices) const
{
    outVertices.clear();
    outIndices.clear();

    auto *go = GetGameObject();
    if (!go) {
        return false;
    }

    auto *mr = go->GetComponent<MeshRenderer>();
    glm::vec3 scale(1.0f);
    if (auto *tf = go->GetTransform()) {
        scale = tf->GetWorldScale();
    }

    if (mr && mr->HasInlineMesh() && !mr->GetInlineVertices().empty() && mr->GetInlineIndices().size() >= 3) {
        outVertices.reserve(mr->GetInlineVertices().size());
        for (const auto &vertex : mr->GetInlineVertices()) {
            outVertices.emplace_back(vertex.pos.x * scale.x, vertex.pos.y * scale.y, vertex.pos.z * scale.z);
        }
        outIndices = mr->GetInlineIndices();
        return true;
    }

    // PATH 2: Asset-managed mesh (loaded from .fbx/.obj/.gltf etc.)
    if (mr && mr->HasMeshAsset()) {
        auto mesh = mr->GetMeshAssetRef().Get();
        if (mesh && !mesh->GetVertices().empty() && mesh->GetIndices().size() >= 3) {
            outVertices.reserve(mesh->GetVertices().size());
            for (const auto &vertex : mesh->GetVertices()) {
                outVertices.emplace_back(vertex.pos.x * scale.x, vertex.pos.y * scale.y, vertex.pos.z * scale.z);
            }
            outIndices = mesh->GetIndices();
            return true;
        }
    }

    return false;
}

void *MeshCollider::CreateJoltShapeRaw() const
{
    m_shapeError.clear();
    std::vector<glm::vec3> vertices;
    std::vector<uint32_t> indices;
    if (!CollectMeshGeometry(vertices, indices) || vertices.empty()) {
        InvalidatePendingCooking();
        m_shapeError = "MeshCollider requires a MeshRenderer with valid mesh geometry";
        return nullptr;
    }
    if (indices.size() < 3 || indices.size() % 3 != 0) {
        InvalidatePendingCooking();
        m_shapeError = "MeshCollider index data must contain complete triangles";
        return nullptr;
    }
    for (const auto &vertex : vertices) {
        if (!std::isfinite(vertex.x) || !std::isfinite(vertex.y) || !std::isfinite(vertex.z)) {
            InvalidatePendingCooking();
            m_shapeError = "MeshCollider vertices must contain only finite coordinates";
            return nullptr;
        }
    }
    for (const uint32_t index : indices) {
        if (index >= vertices.size()) {
            InvalidatePendingCooking();
            m_shapeError = "MeshCollider index data references a missing vertex";
            return nullptr;
        }
    }

    const bool useConvex = m_convex;
    if (auto *rb = GetCachedRigidbody(); rb && !rb->IsKinematic()) {
        if (!m_convex) {
            InvalidatePendingCooking();
            m_shapeError = "dynamic Rigidbody requires MeshCollider.convex = true";
            return nullptr;
        }
    }
    const CookingCacheKey cookingKey = HashGeometry(vertices, indices, useConvex);
    auto cookedGeometry = FindCookedGeometry(cookingKey);
    glm::vec3 worldScale(1.0f);
    if (auto *go = GetGameObject()) {
        if (auto *transform = go->GetTransform())
            worldScale = transform->GetWorldScale();
    }

    if (!cookedGeometry && !JobSystem::IsAvailable()) {
        ++g_cookingCacheMisses;
        cookedGeometry = CookGeometry(std::move(vertices), std::move(indices), useConvex, worldScale, m_shapeError);
        if (!cookedGeometry)
            return nullptr;
        StoreCookedGeometry(cookingKey, cookedGeometry);
    } else if (!cookedGeometry) {
        const bool sameRequest = m_cookingPending && m_pendingHashA == cookingKey.hashA &&
                                 m_pendingHashB == cookingKey.hashB && m_pendingVertexCount == cookingKey.vertexCount &&
                                 m_pendingIndexCount == cookingKey.indexCount && m_pendingConvex == cookingKey.convex;
        if (!sameRequest) {
            const uint64_t revision = ++m_cookingRevision;
            m_pendingHashA = cookingKey.hashA;
            m_pendingHashB = cookingKey.hashB;
            m_pendingVertexCount = cookingKey.vertexCount;
            m_pendingIndexCount = cookingKey.indexCount;
            m_pendingConvex = cookingKey.convex;
            m_cookingPending = true;

            CookingWaiter waiter{m_ecsHandle, revision};
            auto inflight = g_inflightCooking.find(cookingKey);
            if (inflight != g_inflightCooking.end()) {
                inflight->second.waiters.push_back(waiter);
            } else {
                ++g_cookingCacheMisses;
                ++g_asyncCookingSubmissions;
                InflightCooking request;
                request.waiters.push_back(waiter);
                request.job =
                    JobSystem::Get().Schedule([key = cookingKey, vertices = std::move(vertices),
                                               indices = std::move(indices), useConvex, worldScale]() mutable {
                        CookingCompletion completion;
                        completion.key = key;
                        try {
                            completion.geometry = CookGeometry(std::move(vertices), std::move(indices), useConvex,
                                                               worldScale, completion.error);
                        } catch (const std::exception &exception) {
                            completion.error = std::string("mesh cooking worker failed: ") + exception.what();
                        } catch (...) {
                            completion.error = "mesh cooking worker failed with an unknown exception";
                        }
                        std::lock_guard<std::mutex> lock(g_cookingCompletionMutex);
                        g_cookingCompletions.push_back(std::move(completion));
                    });
                g_inflightCooking.emplace(cookingKey, std::move(request));
            }
        }
        return nullptr;
    }

    if (m_cookingPending)
        InvalidatePendingCooking();
    JPH::Shape *shape = CreateShapeFromCookedGeometry(*cookedGeometry, useConvex, m_shapeError);
    if (!shape)
        return nullptr;
    m_convexHullPositions = cookedGeometry->hullPositions;
    m_convexHullEdges = cookedGeometry->hullEdges;
    if (m_collisionPositions != cookedGeometry->collisionPositions ||
        m_collisionIndices != cookedGeometry->collisionIndices) {
        m_collisionPositions = cookedGeometry->collisionPositions;
        m_collisionIndices = cookedGeometry->collisionIndices;
        ++m_collisionGeometryRevision;
        if (m_collisionGeometryRevision == 0)
            ++m_collisionGeometryRevision;
        PhysicsECSStore::Instance().NotifyCollisionSceneChanged();
    }

    glm::vec3 center = GetCenter();
    if (auto *go = GetGameObject()) {
        if (auto *tf = go->GetTransform()) {
            center *= tf->GetWorldScale();
        }
    }
    if (center != glm::vec3(0.0f)) {
        shape = new JPH::RotatedTranslatedShape(JPH::Vec3(center.x, center.y, center.z), JPH::Quat::sIdentity(), shape);
    }

    return shape;
}

void MeshCollider::CompleteCooking(uint64_t hashA, uint64_t hashB, size_t vertexCount, size_t indexCount, bool convex,
                                   uint64_t revision, const std::string &error)
{
    if (!m_cookingPending || m_cookingRevision != revision || m_pendingHashA != hashA || m_pendingHashB != hashB ||
        m_pendingVertexCount != vertexCount || m_pendingIndexCount != indexCount || m_pendingConvex != convex) {
        return;
    }

    m_cookingPending = false;
    if (!error.empty()) {
        m_shapeError = error;
        return;
    }

    m_shapeError.clear();
    if (GetBodyId() == 0xFFFFFFFF) {
        PhysicsECSStore::Instance().QueueBodyCreation(m_ecsHandle);
    } else {
        RebuildShape();
    }
}

void MeshCollider::FlushCompletedCooking(bool waitForAll)
{
    if (waitForAll && !g_inflightCooking.empty()) {
        if (!JobSystem::IsAvailable())
            throw std::logic_error("cannot wait for mesh cooking after JobSystem shutdown");
        std::vector<JobHandle> jobs;
        jobs.reserve(g_inflightCooking.size());
        for (const auto &entry : g_inflightCooking)
            jobs.push_back(entry.second.job);
        for (const auto &job : jobs)
            JobSystem::Get().Wait(job);
    }

    std::deque<CookingCompletion> completions;
    {
        std::lock_guard<std::mutex> lock(g_cookingCompletionMutex);
        completions.swap(g_cookingCompletions);
    }

    auto &store = PhysicsECSStore::Instance();
    for (auto &completion : completions) {
        auto inflight = g_inflightCooking.find(completion.key);
        if (inflight == g_inflightCooking.end())
            continue;
        auto waiters = std::move(inflight->second.waiters);
        g_inflightCooking.erase(inflight);
        if (completion.geometry)
            StoreCookedGeometry(completion.key, completion.geometry);

        for (const auto &waiter : waiters) {
            if (!store.IsValid(waiter.handle))
                continue;
            auto *collider = dynamic_cast<MeshCollider *>(store.GetCollider(waiter.handle).owner);
            if (!collider)
                continue;
            collider->CompleteCooking(completion.key.hashA, completion.key.hashB, completion.key.vertexCount,
                                      completion.key.indexCount, completion.key.convex, waiter.revision,
                                      completion.error);
        }
    }
}

nlohmann::json MeshCollider::SerializeDocument() const
{
    auto baseJson = Collider::SerializeDocument();
    baseJson["convex"] = m_convex;
    return baseJson;
}

void MeshCollider::ValidateSerializedDocument(const nlohmann::json &j)
{
    using namespace component_document_validation;
    ValidateComponentDocument(j, "MeshCollider", {"is_trigger", "center", "physic_material_guid", "convex"});
    RequireBoolean(j, "is_trigger", "MeshCollider");
    RequireFiniteVector(j, "center", 3, "MeshCollider");
    RequireString(j, "physic_material_guid", "MeshCollider");
    RequireBoolean(j, "convex", "MeshCollider");
}

bool MeshCollider::DeserializeDocument(const nlohmann::json &j)
{
    try {
        ValidateSerializedDocument(j);
        const bool stagedConvex = j["convex"].get<bool>();
        if (!Collider::DeserializeDocument(j))
            return false;
        m_convex = stagedConvex;
        RebuildShape();
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("MeshCollider::Deserialize failed: ", e.what());
        return false;
    }
}

std::unique_ptr<Component> MeshCollider::Clone() const
{
    auto clone = std::make_unique<MeshCollider>();
    CloneBaseColliderData(*clone);
    clone->m_convex = m_convex;
    clone->m_convexHullPositions = m_convexHullPositions;
    clone->m_convexHullEdges = m_convexHullEdges;
    clone->m_shapeError = m_shapeError;
    return clone;
}

} // namespace infernux
