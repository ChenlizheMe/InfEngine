#include "TransformECSStore.h"
#include "GameObject.h"
#include "Scene.h"
#include "Transform.h"
#include <cmath>
#include <cstring>
#include <glm/gtc/constants.hpp>
#include <glm/gtc/matrix_transform.hpp>
#include <glm/gtc/quaternion.hpp>

namespace infernux
{

namespace
{
glm::quat EulerYXZToQuat(const glm::vec3 &eulerDeg)
{
    glm::vec3 r = glm::radians(eulerDeg);
    float cx = std::cos(r.x * 0.5f), sx = std::sin(r.x * 0.5f);
    float cy = std::cos(r.y * 0.5f), sy = std::sin(r.y * 0.5f);
    float cz = std::cos(r.z * 0.5f), sz = std::sin(r.z * 0.5f);

    glm::quat q;
    q.w = cy * cx * cz + sy * sx * sz;
    q.x = cy * sx * cz + sy * cx * sz;
    q.y = sy * cx * cz - cy * sx * sz;
    q.z = cy * cx * sz - sy * sx * cz;
    return q;
}

glm::vec3 QuatToEulerYXZ(const glm::quat &rotation)
{
    glm::quat q = glm::normalize(rotation);
    float sinX = 2.0f * (q.w * q.x - q.y * q.z);

    float x, y, z;
    if (std::abs(sinX) < 0.9999f) {
        x = std::asin(sinX);
        y = std::atan2(2.0f * (q.x * q.z + q.w * q.y), 1.0f - 2.0f * (q.x * q.x + q.y * q.y));
        z = std::atan2(2.0f * (q.x * q.y + q.w * q.z), 1.0f - 2.0f * (q.x * q.x + q.z * q.z));
    } else {
        x = std::copysign(glm::half_pi<float>(), sinX);
        y = std::atan2(-(2.0f * (q.x * q.z - q.w * q.y)), 1.0f - 2.0f * (q.y * q.y + q.z * q.z));
        z = 0.0f;
    }
    return glm::degrees(glm::vec3(x, y, z));
}
} // namespace

TransformECSStore &TransformECSStore::Instance()
{
    // Intentionally leaked (see SceneManager::Instance) — Transform
    // destructors run during explicit Cleanup, never after static teardown.
    static TransformECSStore *instance = new TransformECSStore();
    return *instance;
}

TransformECSStore::Handle TransformECSStore::Allocate(Transform *owner)
{
    uint32_t index;

    if (m_freeListHead != UINT32_MAX) {
        index = m_freeListHead;
        m_freeListHead = m_nextFree[index];
        m_alive[index] = 1;
    } else {
        index = static_cast<uint32_t>(m_generations.size());
        m_localPositions.emplace_back(0.0f, 0.0f, 0.0f);
        m_localEulerAngles.emplace_back(0.0f, 0.0f, 0.0f);
        m_localRotations.emplace_back(1.0f, 0.0f, 0.0f, 0.0f);
        m_cachedWorldEulerAngles.emplace_back(0.0f, 0.0f, 0.0f);
        m_hasCachedWorldEulerAngles.push_back(0);
        m_worldEulerExact.push_back(0);
        m_localScales.emplace_back(1.0f, 1.0f, 1.0f);
        m_dirty.push_back(1);
        m_cachedWorldMatrices.emplace_back(1.0f);
        m_worldMatrixDirty.push_back(0);
        m_worldMatrixDirtyListed.push_back(0);
        m_owners.push_back(nullptr);
        m_generations.push_back(1);
        m_alive.push_back(1);
        m_nextFree.push_back(UINT32_MAX);

        // Keep frame cache arrays in sync with capacity.
        m_fcWorldPositions.emplace_back(0.0f, 0.0f, 0.0f);
        m_fcWorldRotations.emplace_back(1.0f, 0.0f, 0.0f, 0.0f);
        m_fcRotationValid.push_back(0);
        m_fcDirty.push_back(0);
        m_fcStamp.push_back(0);
    }

    // Reset fields to defaults for recycled slots.
    m_localPositions[index] = glm::vec3(0.0f);
    m_localEulerAngles[index] = glm::vec3(0.0f);
    m_localRotations[index] = glm::quat(1.0f, 0.0f, 0.0f, 0.0f);
    m_cachedWorldEulerAngles[index] = glm::vec3(0.0f);
    m_hasCachedWorldEulerAngles[index] = 0;
    m_worldEulerExact[index] = 0;
    m_localScales[index] = glm::vec3(1.0f);
    m_dirty[index] = 1;
    m_cachedWorldMatrices[index] = glm::mat4(1.0f);
    MarkWorldMatrixDirty(index);
    m_fcRotationValid[index] = 0;
    m_fcDirty[index] = 0;
    m_fcStamp[index] = 0;
    m_owners[index] = owner;

    ++m_aliveCount;
    ++m_structuralVersion;
    return Handle{index, m_generations[index]};
}

void TransformECSStore::Release(Handle handle)
{
    if (!IsValid(handle)) {
        return;
    }
    uint32_t idx = handle.index;
    m_owners[idx] = nullptr;
    m_alive[idx] = 0;
    m_fcRotationValid[idx] = 0;
    ++m_generations[idx];
    m_nextFree[idx] = m_freeListHead;
    m_freeListHead = idx;
    --m_aliveCount;
    ++m_structuralVersion;
}

bool TransformECSStore::IsValid(Handle handle) const
{
    if (!handle.IsValid() || handle.index >= m_generations.size()) {
        return false;
    }
    return m_alive[handle.index] && m_generations[handle.index] == handle.generation;
}

void TransformECSStore::RebindOwner(Handle handle, Transform *owner)
{
    if (!IsValid(handle)) {
        return;
    }
    if (m_owners[handle.index] != owner) {
        m_owners[handle.index] = owner;
        ++m_structuralVersion;
    }
}

void TransformECSStore::Reserve(size_t capacity)
{
    m_localPositions.reserve(capacity);
    m_localEulerAngles.reserve(capacity);
    m_localRotations.reserve(capacity);
    m_cachedWorldEulerAngles.reserve(capacity);
    m_hasCachedWorldEulerAngles.reserve(capacity);
    m_worldEulerExact.reserve(capacity);
    m_localScales.reserve(capacity);
    m_dirty.reserve(capacity);
    m_cachedWorldMatrices.reserve(capacity);
    m_worldMatrixDirty.reserve(capacity);
    m_worldMatrixDirtyListed.reserve(capacity);
    m_worldMatrixDirtyIndices.reserve(capacity);
    m_owners.reserve(capacity);
    m_generations.reserve(capacity);
    m_alive.reserve(capacity);
    m_nextFree.reserve(capacity);
    m_fcWorldPositions.reserve(capacity);
    m_fcWorldRotations.reserve(capacity);
    m_fcRotationValid.reserve(capacity);
    m_fcDirty.reserve(capacity);
    m_fcStamp.reserve(capacity);
}

TransformECSData TransformECSStore::GetSnapshot(Handle h) const
{
    uint32_t i = h.index;
    TransformECSData d;
    d.localPosition = m_localPositions[i];
    d.localEulerAngles = m_localEulerAngles[i];
    d.localRotation = m_localRotations[i];
    d.cachedWorldEulerAngles = m_cachedWorldEulerAngles[i];
    d.hasCachedWorldEulerAngles = m_hasCachedWorldEulerAngles[i] != 0;
    d.worldEulerExact = m_worldEulerExact[i] != 0;
    d.localScale = m_localScales[i];
    d.dirty = m_dirty[i] != 0;
    d.cachedWorldMatrix = m_cachedWorldMatrices[i];
    d.worldMatrixDirty = m_worldMatrixDirty[i] != 0;
    d.owner = m_owners[i];
    return d;
}

void TransformECSStore::SetSnapshot(Handle h, const TransformECSData &d)
{
    uint32_t i = h.index;
    m_localPositions[i] = d.localPosition;
    m_localEulerAngles[i] = d.localEulerAngles;
    m_localRotations[i] = d.localRotation;
    m_cachedWorldEulerAngles[i] = d.cachedWorldEulerAngles;
    m_hasCachedWorldEulerAngles[i] = d.hasCachedWorldEulerAngles ? 1 : 0;
    m_worldEulerExact[i] = d.worldEulerExact ? 1 : 0;
    m_localScales[i] = d.localScale;
    m_dirty[i] = d.dirty ? 1 : 0;
    m_cachedWorldMatrices[i] = d.cachedWorldMatrix;
    m_worldMatrixDirty[i] = 0;
    m_fcRotationValid[i] = 0;
    if (d.worldMatrixDirty)
        MarkWorldMatrixDirty(i);
    m_owners[i] = d.owner;
}

void TransformECSStore::InvalidateSubtree(Transform *root, bool clearWorldEulerExact,
                                          const Transform *skipObserverFor) const
{
    if (!root) {
        return;
    }

    auto handle = root->GetECSHandle();
    if (!IsValid(handle)) {
        return;
    }

    uint32_t idx = handle.index;
    // const_cast: cache invalidation is logically const — it only marks
    // cached data as stale.
    auto &self = const_cast<TransformECSStore &>(*this);
    if (!self.m_worldMatrixDirty[idx]) {
        self.MarkWorldMatrixDirty(idx);
        ++self.m_globalTransformSerial;
    }
    if (clearWorldEulerExact) {
        self.m_worldEulerExact[idx] = 0;
        self.m_fcRotationValid[idx] = 0;
    }

    if (m_invalidationObserver && root != skipObserverFor) {
        m_invalidationObserver(root);
    }

    GameObject *go = root->GetGameObject();
    if (!go) {
        return;
    }

    for (size_t i = 0; i < go->GetChildCount(); ++i) {
        GameObject *child = go->GetChild(i);
        if (child) {
            InvalidateSubtree(child->GetTransform(), clearWorldEulerExact, skipObserverFor);
        }
    }
}

void TransformECSStore::SyncSceneWorldMatrices(Scene *scene)
{
    if (!scene) {
        return;
    }

    // Skip redundant syncs during frame cache phase — all world-space
    // reads/writes go through the cache arrays, so recomputing
    // m_cachedWorldMatrices from live SoA is wasted work.
    if (m_frameCacheActive) {
        return;
    }

    // Fast skip when no transform was dirtied since the last sync.
    if (!m_anyWorldMatrixDirty) {
        return;
    }

    size_t retained = 0;
    for (uint32_t index : m_worldMatrixDirtyIndices) {
        if (index >= m_alive.size() || !m_alive[index] || !m_worldMatrixDirty[index]) {
            if (index < m_worldMatrixDirtyListed.size())
                m_worldMatrixDirtyListed[index] = 0;
            continue;
        }

        Transform *owner = m_owners[index];
        GameObject *gameObject = owner ? owner->GetGameObject() : nullptr;
        if (!gameObject || gameObject->GetScene() != scene) {
            m_worldMatrixDirtyIndices[retained++] = index;
            continue;
        }

        // Parents are resolved recursively by GetWorldMatrix. Descendants
        // invalidated by a parent change have their own sparse entries, so no
        // root-list traversal is needed for an otherwise static large scene.
        (void)owner->GetWorldMatrix();
        m_worldMatrixDirtyListed[index] = 0;
    }
    m_worldMatrixDirtyIndices.resize(retained);
    m_anyWorldMatrixDirty = !m_worldMatrixDirtyIndices.empty();
}

bool TransformECSStore::IsFrameCacheActiveFor(Handle h) const
{
    if (!m_frameCacheActive || !IsValid(h)) {
        return false;
    }

    return IsSlotInScene(h.index, m_fcScene);
}

bool TransformECSStore::IsSlotInScene(size_t index, const Scene *scene) const
{
    if (!scene || index >= m_owners.size() || !m_alive[index]) {
        return false;
    }

    Transform *owner = m_owners[index];
    GameObject *go = owner ? owner->GetGameObject() : nullptr;
    return go && go->GetScene() == scene;
}

void TransformECSStore::SyncObjectWorldMatrices(GameObject *obj)
{
    if (!obj) {
        return;
    }

    Transform *t = obj->GetTransform();
    if (t) {
        auto handle = t->GetECSHandle();
        if (IsValid(handle)) {
            uint32_t idx = handle.index;
            if (m_worldMatrixDirty[idx]) {
                glm::mat4 local = glm::translate(glm::mat4(1.0f), m_localPositions[idx]) *
                                  glm::mat4_cast(m_localRotations[idx]) *
                                  glm::scale(glm::mat4(1.0f), m_localScales[idx]);

                GameObject *parent = obj->GetParent();
                if (!parent) {
                    m_cachedWorldMatrices[idx] = local;
                } else {
                    Transform *pt = parent->GetTransform();
                    auto parentHandle = pt->GetECSHandle();
                    if (IsValid(parentHandle)) {
                        m_cachedWorldMatrices[idx] = m_cachedWorldMatrices[parentHandle.index] * local;
                    } else {
                        m_cachedWorldMatrices[idx] = local;
                    }
                }
                m_worldMatrixDirty[idx] = 0;
            }
        }
    }

    for (size_t i = 0; i < obj->GetChildCount(); ++i) {
        SyncObjectWorldMatrices(obj->GetChild(i));
    }
}

// ── batch gather/scatter ─────────────────────────────────────────────

void TransformECSStore::GatherLocalPositions(Transform *const *transforms, float *out, size_t count) const
{
    for (size_t i = 0; i < count; ++i) {
        uint32_t idx = transforms[i]->GetECSHandle().index;
        const auto &v = m_localPositions[idx];
        out[i * 3 + 0] = v.x;
        out[i * 3 + 1] = v.y;
        out[i * 3 + 2] = v.z;
    }
}

void TransformECSStore::ScatterLocalPositions(Transform *const *transforms, const float *in, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        auto h = transforms[i]->GetECSHandle();
        uint32_t idx = h.index;
        m_localPositions[idx] = glm::vec3(in[i * 3], in[i * 3 + 1], in[i * 3 + 2]);
        m_dirty[idx] = 1;
        MarkWorldMatrixDirty(idx);
    }
    m_anyWorldMatrixDirty = true;
    for (size_t i = 0; i < count; ++i) {
        InvalidateSubtree(transforms[i], false);
    }
}

void TransformECSStore::GatherLocalScales(Transform *const *transforms, float *out, size_t count) const
{
    for (size_t i = 0; i < count; ++i) {
        uint32_t idx = transforms[i]->GetECSHandle().index;
        const auto &v = m_localScales[idx];
        out[i * 3 + 0] = v.x;
        out[i * 3 + 1] = v.y;
        out[i * 3 + 2] = v.z;
    }
}

void TransformECSStore::ScatterLocalScales(Transform *const *transforms, const float *in, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        auto h = transforms[i]->GetECSHandle();
        uint32_t idx = h.index;
        m_localScales[idx] = glm::vec3(in[i * 3], in[i * 3 + 1], in[i * 3 + 2]);
        m_dirty[idx] = 1;
        MarkWorldMatrixDirty(idx);
    }
    m_anyWorldMatrixDirty = true;
    for (size_t i = 0; i < count; ++i) {
        InvalidateSubtree(transforms[i], false);
    }
}

void TransformECSStore::GatherLocalRotations(Transform *const *transforms, float *out, size_t count) const
{
    for (size_t i = 0; i < count; ++i) {
        uint32_t idx = transforms[i]->GetECSHandle().index;
        const auto &q = m_localRotations[idx];
        out[i * 4 + 0] = q.x;
        out[i * 4 + 1] = q.y;
        out[i * 4 + 2] = q.z;
        out[i * 4 + 3] = q.w;
    }
}

void TransformECSStore::ScatterLocalRotations(Transform *const *transforms, const float *in, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        auto h = transforms[i]->GetECSHandle();
        uint32_t idx = h.index;
        glm::quat q(in[i * 4 + 3], in[i * 4], in[i * 4 + 1], in[i * 4 + 2]); // glm: (w,x,y,z)
        m_localRotations[idx] = q;
        m_localEulerAngles[idx] = QuatToEulerYXZ(q);
        m_hasCachedWorldEulerAngles[idx] = 0;
        m_fcRotationValid[idx] = 0;
        m_dirty[idx] = 1;
        MarkWorldMatrixDirty(idx);
    }
    m_anyWorldMatrixDirty = true;
    for (size_t i = 0; i < count; ++i) {
        InvalidateSubtree(transforms[i], true);
    }
}

void TransformECSStore::GatherLocalEulerAngles(Transform *const *transforms, float *out, size_t count) const
{
    for (size_t i = 0; i < count; ++i) {
        uint32_t idx = transforms[i]->GetECSHandle().index;
        const auto &v = m_localEulerAngles[idx];
        out[i * 3 + 0] = v.x;
        out[i * 3 + 1] = v.y;
        out[i * 3 + 2] = v.z;
    }
}

void TransformECSStore::ScatterLocalEulerAngles(Transform *const *transforms, const float *in, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        auto h = transforms[i]->GetECSHandle();
        uint32_t idx = h.index;
        glm::vec3 euler(in[i * 3], in[i * 3 + 1], in[i * 3 + 2]);
        m_localEulerAngles[idx] = euler;
        m_localRotations[idx] = EulerYXZToQuat(euler);
        m_hasCachedWorldEulerAngles[idx] = 0;
        m_fcRotationValid[idx] = 0;
        m_dirty[idx] = 1;
        MarkWorldMatrixDirty(idx);
    }
    m_anyWorldMatrixDirty = true;
    for (size_t i = 0; i < count; ++i) {
        InvalidateSubtree(transforms[i], true);
    }
}

void TransformECSStore::GatherWorldPositions(Transform *const *transforms, float *out, size_t count) const
{
    for (size_t i = 0; i < count; ++i) {
        glm::vec3 wp = transforms[i]->GetWorldPosition();
        out[i * 3 + 0] = wp.x;
        out[i * 3 + 1] = wp.y;
        out[i * 3 + 2] = wp.z;
    }
}

void TransformECSStore::ScatterWorldPositions(Transform *const *transforms, const float *in, size_t count)
{
    bool requiresMatrixSync = false;
    for (size_t i = 0; i < count; ++i) {
        Transform *t = transforms[i];
        Transform *parent = t->GetParent();
        glm::vec3 wp(in[i * 3], in[i * 3 + 1], in[i * 3 + 2]);
        uint32_t idx = t->GetECSHandle().index;

        GameObject *gameObject = t->GetGameObject();
        const bool hasChildren = gameObject && gameObject->GetChildCount() > 0;
        if (!parent && !hasChildren) {
            m_localPositions[idx] = wp;
            m_cachedWorldMatrices[idx][3] = glm::vec4(wp, 1.0f);
            m_worldMatrixDirty[idx] = 0;
            if (m_frameCacheActive)
                m_fcWorldPositions[idx] = wp;
            if (m_invalidationObserver)
                m_invalidationObserver(t);
        } else {
            if (!parent) {
                m_localPositions[idx] = wp;
            } else {
                glm::mat4 invParent = glm::inverse(parent->GetWorldMatrix());
                m_localPositions[idx] = glm::vec3(invParent * glm::vec4(wp, 1.0f));
            }
            MarkWorldMatrixDirty(idx);
            requiresMatrixSync = true;
            InvalidateSubtree(t, false);
        }
        m_dirty[idx] = 1;
    }
    if (requiresMatrixSync)
        m_anyWorldMatrixDirty = true;
    if (count > 0)
        ++m_globalTransformSerial;
}

void TransformECSStore::GatherWorldEulerAngles(Transform *const *transforms, float *out, size_t count) const
{
    for (size_t i = 0; i < count; ++i) {
        glm::vec3 we = transforms[i]->GetWorldEulerAngles();
        out[i * 3 + 0] = we.x;
        out[i * 3 + 1] = we.y;
        out[i * 3 + 2] = we.z;
    }
}

void TransformECSStore::ScatterWorldEulerAngles(Transform *const *transforms, const float *in, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        transforms[i]->SetWorldEulerAngles(glm::vec3(in[i * 3], in[i * 3 + 1], in[i * 3 + 2]));
    }
}

void TransformECSStore::GatherWorldRotations(Transform *const *transforms, float *out, size_t count) const
{
    for (size_t i = 0; i < count; ++i) {
        glm::quat wr = transforms[i]->GetWorldRotation();
        out[i * 4 + 0] = wr.x;
        out[i * 4 + 1] = wr.y;
        out[i * 4 + 2] = wr.z;
        out[i * 4 + 3] = wr.w;
    }
}

void TransformECSStore::ScatterWorldRotations(Transform *const *transforms, const float *in, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        transforms[i]->SetWorldRotation(glm::quat(in[i * 4 + 3], in[i * 4], in[i * 4 + 1], in[i * 4 + 2]));
    }
}

// ── Frame Cache ──────────────────────────────────────────────────────

void TransformECSStore::BeginFrameCache(Scene *scene)
{
    if (!scene) {
        return;
    }

    // Ensure world matrices are up-to-date before snapshotting.
    SyncSceneWorldMatrices(scene);

    const size_t cap = m_generations.size();

    // Resize cache arrays to match current capacity.
    if (m_fcWorldPositions.size() < cap) {
        m_fcWorldPositions.resize(cap);
        m_fcWorldRotations.resize(cap);
        m_fcRotationValid.resize(cap, 0);
        m_fcDirty.resize(cap, 0);
        m_fcStamp.resize(cap, 0);
    }

    ++m_frameCacheSerial;
    if (m_frameCacheSerial == 0) {
        std::fill(m_fcStamp.begin(), m_fcStamp.end(), 0);
        m_frameCacheSerial = 1;
    }
    m_fcDirtyIndices.clear();
    m_fcPublishedPhysicsPose = false;

    m_frameCacheActive = true;
    m_fcScene = scene;
}

bool TransformECSStore::EndFrameCache()
{
    if (!m_frameCacheActive) {
        return false;
    }

    m_frameCacheActive = false;
    bool requiresFullSync = false;

    for (uint32_t i : m_fcDirtyIndices) {
        const uint8_t d = m_fcDirty[i];
        m_fcDirty[i] = 0;
        if (d == 0 || !IsSlotInScene(i, m_fcScene)) {
            continue;
        }

        Transform *owner = m_owners[i];
        if (!owner) {
            continue;
        }

        Transform *parent = owner->GetParent();
        GameObject *go = owner->GetGameObject();
        const bool hasChildren = (go && go->GetChildCount() > 0);

        const bool notifyOwner = (d & 0x40) != 0;

        // World position dirty → compute local position from inverse parent.
        if (d & 0x01) {
            if (!parent) {
                m_localPositions[i] = m_fcWorldPositions[i];
            } else {
                glm::mat4 invParent = glm::inverse(parent->GetWorldMatrix());
                m_localPositions[i] = glm::vec3(invParent * glm::vec4(m_fcWorldPositions[i], 1.0f));
            }
            m_dirty[i] = 1;
            MarkWorldMatrixDirty(i);
        }

        // World rotation dirty → compute local rotation from inverse parent.
        if (d & 0x02) {
            if (!parent) {
                m_localRotations[i] = m_fcWorldRotations[i];
            } else {
                m_localRotations[i] = glm::inverse(parent->GetWorldRotation()) * m_fcWorldRotations[i];
            }
            m_localEulerAngles[i] = QuatToEulerYXZ(m_localRotations[i]);
            m_hasCachedWorldEulerAngles[i] = 0;
            m_fcRotationValid[i] = 1;
            m_dirty[i] = 1;
            MarkWorldMatrixDirty(i);
        }

        // Local property dirty bits (2-5) already wrote to live SoA in SetCachedLocal*.
        // Just ensure subtree invalidation.
        if (d & 0x3C) { // bits 2-5
            m_dirty[i] = 1;
            MarkWorldMatrixDirty(i);
        }

        const bool positionOnlyDirty = ((d & ~static_cast<uint8_t>(0x45)) == 0);
        if (positionOnlyDirty && !parent && !hasChildren) {
            m_cachedWorldMatrices[i][3] = glm::vec4(m_localPositions[i], 1.0f);
            m_worldMatrixDirty[i] = 0;
            if (notifyOwner && m_invalidationObserver)
                m_invalidationObserver(owner);
            continue;
        }

        requiresFullSync = true;

        // Invalidate subtrees for any dirty slot.
        if (hasChildren) {
            InvalidateSubtree(owner, (d & 0x02) != 0, notifyOwner ? nullptr : owner);
        } else if (notifyOwner && m_invalidationObserver) {
            m_invalidationObserver(owner);
        }
    }

    m_fcDirtyIndices.clear();

    // Pre-sync world matrices now so CollectRenderables hits clean
    // caches (avoids 14,400 lazy recomputes with poor cache locality).
    if (m_fcScene && requiresFullSync) {
        m_anyWorldMatrixDirty = true; // dirty from the loop above
        SyncSceneWorldMatrices(m_fcScene);
    }
    m_fcScene = nullptr;
    const bool publishedPhysicsPose = m_fcPublishedPhysicsPose;
    m_fcPublishedPhysicsPose = false;
    return publishedPhysicsPose;
}

void TransformECSStore::EnsureFrameCacheSlot(uint32_t slotIndex)
{
    if (slotIndex >= m_fcStamp.size() || m_fcStamp[slotIndex] == m_frameCacheSerial)
        return;

    const glm::mat4 &world = m_cachedWorldMatrices[slotIndex];
    m_fcWorldPositions[slotIndex] = glm::vec3(world[3]);
    if (!m_fcRotationValid[slotIndex]) {
        m_fcWorldRotations[slotIndex] = glm::quat_cast(glm::mat3(world));
        m_fcRotationValid[slotIndex] = 1;
    }
    m_fcStamp[slotIndex] = m_frameCacheSerial;
}

void TransformECSStore::MarkFrameCacheDirty(uint32_t slotIndex, uint8_t bits)
{
    if (m_fcDirty[slotIndex] == 0)
        m_fcDirtyIndices.push_back(slotIndex);
    m_fcDirty[slotIndex] |= bits;
}

void TransformECSStore::MarkWorldMatrixDirty(uint32_t slotIndex)
{
    if (slotIndex >= m_worldMatrixDirty.size())
        return;
    m_worldMatrixDirty[slotIndex] = 1;
    if (!m_worldMatrixDirtyListed[slotIndex]) {
        m_worldMatrixDirtyListed[slotIndex] = 1;
        m_worldMatrixDirtyIndices.push_back(slotIndex);
    }
    m_anyWorldMatrixDirty = true;
}

void TransformECSStore::SetCachedWorldPosition(uint32_t slotIndex, const glm::vec3 &v)
{
    EnsureFrameCacheSlot(slotIndex);
    m_fcWorldPositions[slotIndex] = v;
    MarkFrameCacheDirty(slotIndex, 0x41);
}

void TransformECSStore::SetCachedWorldRotation(uint32_t slotIndex, const glm::quat &q)
{
    EnsureFrameCacheSlot(slotIndex);
    m_fcWorldRotations[slotIndex] = q;
    m_fcRotationValid[slotIndex] = 1;
    MarkFrameCacheDirty(slotIndex, 0x42);
}

void TransformECSStore::SetCachedWorldPoseFromPhysics(uint32_t slotIndex, const glm::vec3 &position,
                                                      const glm::quat &rotation, bool applyRotation)
{
    EnsureFrameCacheSlot(slotIndex);
    m_fcPublishedPhysicsPose = true;
    m_fcWorldPositions[slotIndex] = position;
    MarkFrameCacheDirty(slotIndex, 0x01);
    if (applyRotation) {
        m_fcWorldRotations[slotIndex] = rotation;
        m_fcRotationValid[slotIndex] = 1;
        MarkFrameCacheDirty(slotIndex, 0x02);
    }
}

void TransformECSStore::SetCachedLocalPosition(uint32_t slotIndex, const glm::vec3 &v)
{
    m_localPositions[slotIndex] = v;
    MarkFrameCacheDirty(slotIndex, 0x44);
    MarkWorldMatrixDirty(slotIndex);
}

void TransformECSStore::SetCachedLocalScale(uint32_t slotIndex, const glm::vec3 &v)
{
    m_localScales[slotIndex] = v;
    MarkFrameCacheDirty(slotIndex, 0x48);
    MarkWorldMatrixDirty(slotIndex);
}

void TransformECSStore::SetCachedLocalRotation(uint32_t slotIndex, const glm::quat &q)
{
    m_localRotations[slotIndex] = q;
    m_localEulerAngles[slotIndex] = QuatToEulerYXZ(q);
    m_hasCachedWorldEulerAngles[slotIndex] = 0;
    m_fcRotationValid[slotIndex] = 0;
    MarkFrameCacheDirty(slotIndex, 0x50);
    MarkWorldMatrixDirty(slotIndex);
}

void TransformECSStore::SetCachedLocalEulerAngles(uint32_t slotIndex, const glm::vec3 &v)
{
    m_localEulerAngles[slotIndex] = v;
    m_localRotations[slotIndex] = EulerYXZToQuat(v);
    m_hasCachedWorldEulerAngles[slotIndex] = 0;
    m_fcRotationValid[slotIndex] = 0;
    MarkFrameCacheDirty(slotIndex, 0x60);
    MarkWorldMatrixDirty(slotIndex);
}

} // namespace infernux
