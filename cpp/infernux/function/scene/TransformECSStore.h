#pragma once

#include <cstdint>
#include <functional>
#include <glm/glm.hpp>
#include <glm/gtc/quaternion.hpp>
#include <vector>

namespace infernux
{

class Scene;
class Transform;
class GameObject;

/// Transient value bundle — used for bulk copy (CloneDataTo) and
/// backwards-compatible read helpers, but **not** stored in the pool.
struct TransformECSData
{
    glm::vec3 localPosition{0.0f, 0.0f, 0.0f};
    glm::vec3 localEulerAngles{0.0f, 0.0f, 0.0f};
    glm::quat localRotation{1.0f, 0.0f, 0.0f, 0.0f};
    glm::vec3 cachedWorldEulerAngles{0.0f, 0.0f, 0.0f};
    bool hasCachedWorldEulerAngles = false;
    bool worldEulerExact = false;
    glm::vec3 localScale{1.0f, 1.0f, 1.0f};
    bool dirty = true;
    glm::mat4 cachedWorldMatrix{1.0f};
    bool worldMatrixDirty = true;
    Transform *owner = nullptr;
};

/// Structure-of-Arrays transform storage.
///
/// All transform data is stored in parallel arrays indexed by a generational
/// handle.  The layout is **SoA** (Structure-of-Arrays) rather than AoS so
/// that batch operations (future numpy batch API, SIMD sweeps) can iterate
/// a single field array with optimal cache locality.
///
/// The public ``Get()`` / ``Set()`` helpers use the slot index directly after
/// a generation check, so random-access cost is identical to the old pool.
class TransformECSStore
{
  public:
    using InvalidationObserver = std::function<void(Transform *)>;
    // Keep the same Handle type for binary compatibility.
    struct Handle
    {
        uint32_t index = UINT32_MAX;
        uint32_t generation = 0;

        [[nodiscard]] bool IsValid() const
        {
            return index != UINT32_MAX;
        }

        bool operator==(const Handle &rhs) const
        {
            return index == rhs.index && generation == rhs.generation;
        }

        bool operator!=(const Handle &rhs) const
        {
            return !(*this == rhs);
        }
    };

    static TransformECSStore &Instance();

    Handle Allocate(Transform *owner);
    void Release(Handle handle);
    [[nodiscard]] bool IsValid(Handle handle) const;

    void RebindOwner(Handle handle, Transform *owner);

    /// Pre-allocate capacity for all SoA arrays to avoid incremental growth.
    void Reserve(size_t capacity);

    void InvalidateSubtree(Transform *root, bool clearWorldEulerExact = false,
                           const Transform *skipObserverFor = nullptr) const;

    void SetInvalidationObserver(InvalidationObserver observer)
    {
        m_invalidationObserver = std::move(observer);
    }

    void SyncSceneWorldMatrices(Scene *scene);

    // ── per-field SoA accessors (inlined for hot-path performance) ───

    // — local position —
    [[nodiscard]] const glm::vec3 &GetLocalPosition(Handle h) const
    {
        return m_localPositions[h.index];
    }
    void SetLocalPosition(Handle h, const glm::vec3 &v)
    {
        m_localPositions[h.index] = v;
    }

    // — local euler angles —
    [[nodiscard]] const glm::vec3 &GetLocalEulerAngles(Handle h) const
    {
        return m_localEulerAngles[h.index];
    }
    void SetLocalEulerAngles(Handle h, const glm::vec3 &v)
    {
        m_localEulerAngles[h.index] = v;
    }

    // — local rotation (quaternion) —
    [[nodiscard]] const glm::quat &GetLocalRotation(Handle h) const
    {
        return m_localRotations[h.index];
    }
    void SetLocalRotation(Handle h, const glm::quat &q)
    {
        m_localRotations[h.index] = q;
    }

    // — cached world euler angles —
    [[nodiscard]] const glm::vec3 &GetCachedWorldEulerAngles(Handle h) const
    {
        return m_cachedWorldEulerAngles[h.index];
    }
    void SetCachedWorldEulerAngles(Handle h, const glm::vec3 &v)
    {
        m_cachedWorldEulerAngles[h.index] = v;
    }

    // — has cached world euler angles —
    [[nodiscard]] bool GetHasCachedWorldEulerAngles(Handle h) const
    {
        return m_hasCachedWorldEulerAngles[h.index];
    }
    void SetHasCachedWorldEulerAngles(Handle h, bool v)
    {
        m_hasCachedWorldEulerAngles[h.index] = v;
    }

    // — world euler exact —
    [[nodiscard]] bool GetWorldEulerExact(Handle h) const
    {
        return m_worldEulerExact[h.index];
    }
    void SetWorldEulerExact(Handle h, bool v)
    {
        m_worldEulerExact[h.index] = v;
    }

    // — local scale —
    [[nodiscard]] const glm::vec3 &GetLocalScale(Handle h) const
    {
        return m_localScales[h.index];
    }
    void SetLocalScale(Handle h, const glm::vec3 &v)
    {
        m_localScales[h.index] = v;
    }

    // — dirty —
    [[nodiscard]] bool GetDirty(Handle h) const
    {
        return m_dirty[h.index];
    }
    void SetDirty(Handle h, bool v)
    {
        m_dirty[h.index] = v;
    }

    // — cached world matrix —
    [[nodiscard]] const glm::mat4 &GetCachedWorldMatrix(Handle h) const
    {
        return m_cachedWorldMatrices[h.index];
    }
    void SetCachedWorldMatrix(Handle h, const glm::mat4 &m)
    {
        m_cachedWorldMatrices[h.index] = m;
    }

    // — world matrix dirty —
    [[nodiscard]] bool GetWorldMatrixDirty(Handle h) const
    {
        return m_worldMatrixDirty[h.index];
    }
    void SetWorldMatrixDirty(Handle h, bool v)
    {
        if (v) {
            MarkWorldMatrixDirty(h.index);
        } else {
            m_worldMatrixDirty[h.index] = 0;
        }
    }

    /// True if any transform has been dirtied since the last SyncSceneWorldMatrices.
    [[nodiscard]] bool IsAnyWorldMatrixDirty() const
    {
        return m_anyWorldMatrixDirty;
    }

    /// Monotonically increasing counter bumped whenever any transform is invalidated.
    /// Physics can compare against a cached serial to skip sync when nothing moved.
    [[nodiscard]] uint64_t GetGlobalTransformSerial() const
    {
        return m_globalTransformSerial;
    }
    void BumpGlobalTransformSerial()
    {
        ++m_globalTransformSerial;
    }

    // — owner pointer —
    [[nodiscard]] Transform *GetOwner(Handle h) const
    {
        return m_owners[h.index];
    }
    void SetOwner(Handle h, Transform *t)
    {
        m_owners[h.index] = t;
    }

    // ── bulk snapshot (for CloneDataTo / serialization convenience) ───

    [[nodiscard]] TransformECSData GetSnapshot(Handle h) const;
    void SetSnapshot(Handle h, const TransformECSData &data);

    // ── SoA raw pointers (for future batch / numpy API) ──────────────

    [[nodiscard]] const float *LocalPositionData() const
    {
        return &m_localPositions[0].x;
    }
    [[nodiscard]] float *LocalPositionData()
    {
        return &m_localPositions[0].x;
    }

    [[nodiscard]] const float *LocalScaleData() const
    {
        return &m_localScales[0].x;
    }
    [[nodiscard]] float *LocalScaleData()
    {
        return &m_localScales[0].x;
    }

    [[nodiscard]] const float *LocalRotationData() const
    {
        return &m_localRotations[0].x;
    }
    [[nodiscard]] float *LocalRotationData()
    {
        return &m_localRotations[0].x;
    }

    [[nodiscard]] size_t Capacity() const
    {
        return m_generations.size();
    }
    [[nodiscard]] size_t AliveCount() const
    {
        return m_aliveCount;
    }
    [[nodiscard]] uint64_t GetStructuralVersion() const
    {
        return m_structuralVersion;
    }
    [[nodiscard]] bool IsAlive(uint32_t index) const
    {
        return index < m_alive.size() && m_alive[index];
    }

    // ── batch gather/scatter (for batch_read / batch_write Python API) ──

    /// Gather local positions for a list of Transform pointers into a flat float array.
    /// @param transforms  Array of N Transform* pointers (must all be non-null & alive).
    /// @param out         Pre-allocated float buffer of size N*3.
    /// @param count       Number of transforms.
    void GatherLocalPositions(Transform *const *transforms, float *out, size_t count) const;
    void ScatterLocalPositions(Transform *const *transforms, const float *in, size_t count);

    void GatherLocalScales(Transform *const *transforms, float *out, size_t count) const;
    void ScatterLocalScales(Transform *const *transforms, const float *in, size_t count);

    void GatherLocalRotations(Transform *const *transforms, float *out, size_t count) const;
    void ScatterLocalRotations(Transform *const *transforms, const float *in, size_t count);

    void GatherLocalEulerAngles(Transform *const *transforms, float *out, size_t count) const;
    void ScatterLocalEulerAngles(Transform *const *transforms, const float *in, size_t count);

    /// Gather world positions — computes via GetWorldPosition() on each Transform.
    void GatherWorldPositions(Transform *const *transforms, float *out, size_t count) const;
    void ScatterWorldPositions(Transform *const *transforms, const float *in, size_t count);

    void GatherWorldEulerAngles(Transform *const *transforms, float *out, size_t count) const;
    void ScatterWorldEulerAngles(Transform *const *transforms, const float *in, size_t count);

    void GatherWorldRotations(Transform *const *transforms, float *out, size_t count) const;
    void ScatterWorldRotations(Transform *const *transforms, const float *in, size_t count);

    // ── Frame Cache (per-frame snapshot for O(1) property access) ────
    //
    // BeginFrameCache(): called once per frame before gameplay ticks.
    //   Snapshots all alive transforms' world position/rotation from
    //   the already-synced world matrices.  During the cache phase,
    //   Transform::GetWorldPosition() can read from cache[slot] (O(1))
    //   instead of walking the parent chain (O(depth)).
    //
    // EndFrameCache(): called after LateUpdate.
    //   Flushes dirty cache entries back to live SoA and invalidates
    //   affected subtrees.
    //
    // The cache also covers local properties for write-tracking consistency.

    void BeginFrameCache(Scene *scene);
    /// Commit cached writes. Returns true when at least one physics-authored
    /// pose was published; callers use this to invalidate render-only caches
    /// once per frame without feeding those poses back into physics.
    [[nodiscard]] bool EndFrameCache();

    [[nodiscard]] bool IsFrameCacheActive() const
    {
        return m_frameCacheActive;
    }
    [[nodiscard]] bool IsFrameCacheActiveFor(Handle h) const;

    // Cached world-space read (O(1) array index).  Caller must check
    // IsFrameCacheActiveFor() before calling.
    [[nodiscard]] glm::vec3 GetCachedWorldPosition(uint32_t slotIndex)
    {
        EnsureFrameCacheSlot(slotIndex);
        return m_fcWorldPositions[slotIndex];
    }
    [[nodiscard]] glm::quat GetCachedWorldRotation(uint32_t slotIndex)
    {
        EnsureFrameCacheSlot(slotIndex);
        return m_fcWorldRotations[slotIndex];
    }

    // Cached world-space write — marks slot dirty, defers flush to EndFrameCache.
    void SetCachedWorldPosition(uint32_t slotIndex, const glm::vec3 &v);
    void SetCachedWorldRotation(uint32_t slotIndex, const glm::quat &q);
    void SetCachedWorldPoseFromPhysics(uint32_t slotIndex, const glm::vec3 &position, const glm::quat &rotation,
                                       bool applyRotation);

    // Cached local-space read (alias of live SoA, already O(1), but
    // included for API symmetry).  Local getters don't need a separate
    // cache array since SoA local data is already O(1).

    // Cached local-space write — marks slot dirty.
    void SetCachedLocalPosition(uint32_t slotIndex, const glm::vec3 &v);
    void SetCachedLocalScale(uint32_t slotIndex, const glm::vec3 &v);
    void SetCachedLocalRotation(uint32_t slotIndex, const glm::quat &q);
    void SetCachedLocalEulerAngles(uint32_t slotIndex, const glm::vec3 &v);

  private:
    TransformECSStore() = default;

    [[nodiscard]] bool IsSlotInScene(size_t index, const Scene *scene) const;
    void SyncObjectWorldMatrices(GameObject *obj);
    void EnsureFrameCacheSlot(uint32_t slotIndex);
    void MarkFrameCacheDirty(uint32_t slotIndex, uint8_t bits);
    void MarkWorldMatrixDirty(uint32_t slotIndex);

    // ── SoA arrays (all the same length == Capacity()) ───────────────
    std::vector<glm::vec3> m_localPositions;
    std::vector<glm::vec3> m_localEulerAngles;
    std::vector<glm::quat> m_localRotations;
    std::vector<glm::vec3> m_cachedWorldEulerAngles;
    std::vector<uint8_t> m_hasCachedWorldEulerAngles; // avoid std::vector<bool>
    std::vector<uint8_t> m_worldEulerExact;
    std::vector<glm::vec3> m_localScales;
    std::vector<uint8_t> m_dirty;
    std::vector<glm::mat4> m_cachedWorldMatrices;
    std::vector<uint8_t> m_worldMatrixDirty;
    std::vector<uint8_t> m_worldMatrixDirtyListed;
    std::vector<uint32_t> m_worldMatrixDirtyIndices;
    std::vector<Transform *> m_owners;

    // ── Global dirty flag for fast SyncSceneWorldMatrices skip ───────
    bool m_anyWorldMatrixDirty = false;
    // ── Global transform change serial (for physics dirty skip) ──────
    uint64_t m_globalTransformSerial = 0;

    // ── slot metadata (free-list + generation) ───────────────────────
    std::vector<uint32_t> m_generations;
    std::vector<uint8_t> m_alive;
    std::vector<uint32_t> m_nextFree;
    uint32_t m_freeListHead = UINT32_MAX;
    size_t m_aliveCount = 0;
    uint64_t m_structuralVersion = 0;

    // ── Frame Cache arrays (same length as Capacity()) ───────────────
    std::vector<glm::vec3> m_fcWorldPositions;
    std::vector<glm::quat> m_fcWorldRotations;
    std::vector<uint8_t> m_fcRotationValid;
    // Dirty flags: bit 0 = world position dirty, bit 1 = world rotation dirty,
    // bit 2 = local position dirty, bit 3 = local scale dirty,
    // bit 4 = local rotation dirty, bit 5 = local euler dirty,
    // bit 6 = at least one authored write requires observer publication.
    std::vector<uint8_t> m_fcDirty;
    std::vector<uint64_t> m_fcStamp;
    std::vector<uint32_t> m_fcDirtyIndices;
    uint64_t m_frameCacheSerial = 0;
    bool m_frameCacheActive = false;
    Scene *m_fcScene = nullptr; // scene pointer for EndFrameCache sync
    bool m_fcPublishedPhysicsPose = false;
    InvalidationObserver m_invalidationObserver;
};

} // namespace infernux
