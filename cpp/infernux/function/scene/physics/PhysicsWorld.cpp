/**
 * @file PhysicsWorld.cpp
 * @brief Jolt Physics integration — singleton world, body management, raycasts.
 */

// Jolt requires specific defines before including its headers
#include <Jolt/Jolt.h>

// Jolt includes (order matters)
#include <Jolt/Core/Factory.h>
#include <Jolt/Core/JobSystemThreadPool.h>
#include <Jolt/Core/TempAllocator.h>
#include <Jolt/Physics/Body/BodyActivationListener.h>
#include <Jolt/Physics/Body/BodyCreationSettings.h>
#include <Jolt/Physics/Body/BodyInterface.h>
#include <Jolt/Physics/Collision/BroadPhase/BroadPhaseLayer.h>
#include <Jolt/Physics/Collision/CastResult.h>
#include <Jolt/Physics/Collision/CollideShape.h>
#include <Jolt/Physics/Collision/CollisionCollectorImpl.h>
#include <Jolt/Physics/Collision/ObjectLayer.h>
#include <Jolt/Physics/Collision/RayCast.h>
#include <Jolt/Physics/Collision/Shape/BoxShape.h>
#include <Jolt/Physics/Collision/Shape/CapsuleShape.h>
#include <Jolt/Physics/Collision/Shape/CompoundShape.h>
#include <Jolt/Physics/Collision/Shape/SphereShape.h>
#include <Jolt/Physics/Collision/Shape/StaticCompoundShape.h>
#include <Jolt/Physics/Collision/ShapeCast.h>
#include <Jolt/Physics/PhysicsSettings.h>
#include <Jolt/Physics/PhysicsSystem.h>
#include <Jolt/RegisterTypes.h>

#include "PhysicsContactListener.h"
#include "PhysicsLayers.h"
#include "PhysicsWorld.h"

#include "../Collider.h"
#include "../Component.h"
#include "../GameObject.h"
#include "../Rigidbody.h"
#include "../Scene.h"
#include "../SceneManager.h"
#include "../Transform.h"
#include <core/config/EngineConfig.h>
#include <core/config/MathConstants.h>
#include <core/log/InxLog.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdarg>
#include <thread>
#include <unordered_set>

namespace infernux
{

namespace
{

constexpr float kMinQueryDirectionLengthSq = 1e-12f;

static bool IsFinite(const glm::vec3 &value)
{
    return std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z);
}

static bool IsFinite(const glm::quat &value)
{
    return std::isfinite(value.w) && std::isfinite(value.x) && std::isfinite(value.y) && std::isfinite(value.z) &&
           glm::dot(value, value) > kMinQueryDirectionLengthSq;
}

static bool HasPositiveFiniteExtents(const glm::vec3 &value)
{
    return IsFinite(value) && value.x > 0.0f && value.y > 0.0f && value.z > 0.0f;
}

static JPH::Quat ToJoltQuat(const glm::quat &rotation)
{
    const glm::quat normalized = glm::normalize(rotation);
    return JPH::Quat(normalized.x, normalized.y, normalized.z, normalized.w);
}

static glm::quat CapsuleOrientation(const glm::vec3 &point0, const glm::vec3 &point1)
{
    const glm::vec3 axis = glm::normalize(point1 - point0);
    constexpr glm::vec3 up(0.0f, 1.0f, 0.0f);
    const float cosine = glm::dot(up, axis);
    if (cosine > 1.0f - 1e-6f)
        return glm::quat(1.0f, 0.0f, 0.0f, 0.0f);
    if (cosine < -1.0f + 1e-6f)
        return glm::quat(0.0f, 1.0f, 0.0f, 0.0f);
    const glm::vec3 rotationAxis = glm::cross(up, axis);
    return glm::normalize(glm::quat(1.0f + cosine, rotationAxis.x, rotationAxis.y, rotationAxis.z));
}

static bool NormalizeQueryDirection(const glm::vec3 &direction, float maxDistance, glm::vec3 &outDirection)
{
    if (!IsFinite(direction) || !std::isfinite(maxDistance) || maxDistance <= 0.0f) {
        return false;
    }

    const float lengthSq = glm::dot(direction, direction);
    if (lengthSq <= kMinQueryDirectionLengthSq) {
        return false;
    }

    outDirection = direction / std::sqrt(lengthSq);
    return true;
}

static int MapMotionQualityMode(int quality)
{
    switch (quality) {
    case 1:
    case 2:
        return 1;
    case 0:
    case 3:
    default:
        return 0;
    }
}

static glm::mat3 InvertAllowedAngularSubspace(const glm::mat3 &inverseTensor, uint8_t allowedDofs)
{
    std::array<int, 3> axes{};
    int axisCount = 0;
    for (int axis = 0; axis < 3; ++axis) {
        if ((allowedDofs & (1u << (axis + 3))) != 0)
            axes[axisCount++] = axis;
    }

    glm::mat3 result(0.0f);
    if (axisCount == 0)
        return result;

    float augmented[3][6]{};
    for (int row = 0; row < axisCount; ++row) {
        for (int column = 0; column < axisCount; ++column)
            augmented[row][column] = inverseTensor[axes[column]][axes[row]];
        augmented[row][axisCount + row] = 1.0f;
    }

    for (int pivotColumn = 0; pivotColumn < axisCount; ++pivotColumn) {
        int pivotRow = pivotColumn;
        for (int row = pivotColumn + 1; row < axisCount; ++row) {
            if (std::abs(augmented[row][pivotColumn]) > std::abs(augmented[pivotRow][pivotColumn]))
                pivotRow = row;
        }
        if (std::abs(augmented[pivotRow][pivotColumn]) < 1e-8f)
            throw std::logic_error("dynamic body has singular inertia on an allowed rotation axis");

        if (pivotRow != pivotColumn) {
            for (int column = 0; column < axisCount * 2; ++column)
                std::swap(augmented[pivotRow][column], augmented[pivotColumn][column]);
        }

        const float inversePivot = 1.0f / augmented[pivotColumn][pivotColumn];
        for (int column = 0; column < axisCount * 2; ++column)
            augmented[pivotColumn][column] *= inversePivot;

        for (int row = 0; row < axisCount; ++row) {
            if (row == pivotColumn)
                continue;
            const float factor = augmented[row][pivotColumn];
            for (int column = 0; column < axisCount * 2; ++column)
                augmented[row][column] -= factor * augmented[pivotColumn][column];
        }
    }

    for (int row = 0; row < axisCount; ++row) {
        for (int column = 0; column < axisCount; ++column)
            result[axes[column]][axes[row]] = augmented[row][axisCount + column];
    }
    return result;
}

class LayerMaskObjectFilter final : public JPH::ObjectLayerFilter
{
  public:
    explicit LayerMaskObjectFilter(uint32_t layerMask) : m_layerMask(layerMask)
    {
    }

    bool ShouldCollide(JPH::ObjectLayer inLayer) const override
    {
        const int gameLayer = PhysicsObjectLayers::DecodeGameLayer(inLayer);
        return (m_layerMask & (1u << static_cast<uint32_t>(gameLayer))) != 0;
    }

  private:
    uint32_t m_layerMask;
};

static JPH::RefConst<JPH::Shape> BuildShapeForColliderSet(GameObject *go, const Collider *exclude,
                                                          size_t *outShapeCount = nullptr)
{
    if (!go) {
        return nullptr;
    }

    std::vector<std::pair<Collider *, JPH::RefConst<JPH::Shape>>> childShapes;
    auto colliders = go->GetComponents<Collider>();
    childShapes.reserve(colliders.size());

    for (auto *col : colliders) {
        if (!col || col == exclude || !col->IsEnabled()) {
            continue;
        }

        JPH::RefConst<JPH::Shape> child(static_cast<const JPH::Shape *>(col->CreateJoltShapeRaw()));
        if (child) {
            childShapes.emplace_back(col, child);
        }
    }

    if (childShapes.empty()) {
        if (outShapeCount)
            *outShapeCount = 0;
        return nullptr;
    }

    if (outShapeCount)
        *outShapeCount = childShapes.size();

    if (childShapes.size() == 1) {
        return childShapes.front().second;
    }

    JPH::StaticCompoundShapeSettings compoundSettings;
    for (size_t i = 0; i < childShapes.size(); ++i) {
        auto *col = childShapes[i].first;
        uint32_t userData = col ? static_cast<uint32_t>(col->GetComponentID()) : 0;
        compoundSettings.AddShape(JPH::Vec3::sZero(), JPH::Quat::sIdentity(), childShapes[i].second, userData);
    }

    auto result = compoundSettings.Create();
    if (result.HasError()) {
        return nullptr;
    }
    return result.Get();
}

} // namespace

// ============================================================================
// Jolt trace / assert callbacks (required by Jolt)
// ============================================================================

static void JoltTraceImpl(const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    char buffer[1024];
    vsnprintf(buffer, sizeof(buffer), fmt, args);
    va_end(args);
    INXLOG_DEBUG("[Jolt] ", buffer);
}

#ifdef JPH_ENABLE_ASSERTS
static bool JoltAssertFailed(const char *expression, const char *message, const char *file, unsigned int line)
{
    INXLOG_ERROR("[Jolt Assert] ", file, ":", line, " – ", expression, " – ", message ? message : "");
    // Returning true triggers __debugbreak() inside Jolt's assert macro.
    // In debug builds (debugger attached), this is desirable so we can inspect the state.
    // In release / no-debugger builds, return false to log-and-continue.
#ifdef NDEBUG
    return false;
#else
    return true;
#endif
}
#endif

// ============================================================================
// Layer helpers (must outlive PhysicsSystem)
// ============================================================================

struct PhysicsWorld::LayerInterfaces
{
    BPLayerInterface bpInterface;
    ObjectVsBPLayerFilter objVsBpFilter;
    ObjectLayerPairFilter objPairFilter;
};

// ============================================================================
// Singleton
// ============================================================================

PhysicsWorld &PhysicsWorld::Instance()
{
    // Intentionally leaked: Shutdown() is called explicitly from
    // Infernux::Cleanup(); a static destructor would re-run it after
    // dependent singletons may already be gone.
    static PhysicsWorld *instance = new PhysicsWorld();
    return *instance;
}

PhysicsWorld::~PhysicsWorld()
{
    Shutdown();
}

// ============================================================================
// Init / Shutdown
// ============================================================================

void PhysicsWorld::Initialize()
{
    if (m_initialized)
        return;

    INXLOG_INFO("PhysicsWorld: Initializing Jolt Physics…");

    // Register Jolt allocation hooks (use default malloc)
    JPH::RegisterDefaultAllocator();

    // Install trace / assert callbacks
    JPH::Trace = JoltTraceImpl;
#ifdef JPH_ENABLE_ASSERTS
    JPH::AssertFailed = JoltAssertFailed;
#endif

    // Create factory & register types
    JPH::Factory::sInstance = new JPH::Factory();
    JPH::RegisterTypes();

    // -------------------------------------------------------------------------
    // TempAllocatorImpl — shared stack pool for ALL Jolt worker threads.
    //
    // IMPORTANT: this is NOT a per-thread allocator.  All threads push/pop
    // from the same pool concurrently during broadphase + narrowphase +
    // constraint solver.  Running out of space calls std::abort() immediately,
    // so the pool must be sized for PEAK simultaneous usage across all threads.
    //
    // Sizing guide (bodies in scene → recommended pool):
    //   < 256  bodies : 32 MB  is comfortable
    //   < 1024 bodies : 64 MB  recommended
    //   < 4096 bodies : 128 MB recommended
    //   ≥ 4096 bodies : 256 MB or more
    //
    // Increasing this value has negligible real memory cost because the OS
    // only commits pages that are actually touched (virtual memory).
    // -------------------------------------------------------------------------
    auto &cfg = EngineConfig::Get();

    int hwThreads = static_cast<int>(std::thread::hardware_concurrency());
    int numThreads = cfg.physicsMaxWorkerThreads > 0 ? cfg.physicsMaxWorkerThreads : std::clamp(hwThreads - 1, 1, 8);

    m_tempAllocator = std::make_unique<JPH::TempAllocatorImpl>(cfg.physicsTempAllocatorSize);

    m_jobSystem = std::make_unique<JPH::JobSystemThreadPool>(cfg.physicsMaxJobs, cfg.physicsMaxBarriers, numThreads);

    // Layer interfaces
    m_layers = std::make_unique<LayerInterfaces>();

    // Physics system
    m_physicsSystem = std::make_unique<JPH::PhysicsSystem>();
    m_physicsSystem->Init(cfg.physicsMaxBodies, 0, cfg.physicsMaxBodyPairs, cfg.physicsMaxContactConstraints,
                          m_layers->bpInterface, m_layers->objVsBpFilter, m_layers->objPairFilter);

    // Tune physics settings for thin-body stability and precision.
    //  - Penetration slop: 2 mm (default 20 mm). Min BoxShape thickness is
    //    2 × kMinHalfExtent = 0.02 m; 2 mm slop keeps sinking at 10 %.
    //  - Speculative contact: 10 mm (default 20 mm).  Tighter for thin shells.
    //  - Position solver: 3 iterations (default 2). Better stacking stability.
    //  - LinearCast max penetration: 10 % of inner radius (default 25 %).
    //  - Baumgarte: 0.15 (default 0.2). Softer correction prevents violent
    //    pop-out when thin bodies briefly penetrate a surface.
    //  - Max penetration distance: 50 mm (default 200 mm). Limits per-step
    //    correction so thin-body overlaps resolve gradually.
    //  - LinearCast threshold: 50 % (default 75 %). Triggers CCD earlier,
    //    critical for thin bodies whose inner radius is very small.
    JPH::PhysicsSettings settings = m_physicsSystem->GetPhysicsSettings();
    settings.mPenetrationSlop = cfg.physicsPenetrationSlop;
    settings.mSpeculativeContactDistance = cfg.physicsSpeculativeContactDistance;
    settings.mNumVelocitySteps = cfg.physicsVelocitySteps;
    settings.mNumPositionSteps = cfg.physicsPositionSteps;
    settings.mLinearCastMaxPenetration = cfg.physicsLinearCastMaxPenetration;
    settings.mBaumgarte = cfg.physicsBaumgarte;
    settings.mMaxPenetrationDistance = cfg.physicsMaxPenetrationDistance;
    settings.mLinearCastThreshold = cfg.physicsLinearCastThreshold;
    settings.mMinVelocityForRestitution = cfg.physicsMinVelocityForRestitution;
    settings.mTimeBeforeSleep = cfg.physicsTimeBeforeSleep;
    settings.mPointVelocitySleepThreshold = cfg.physicsPointVelocitySleepThreshold;
    m_physicsSystem->SetPhysicsSettings(settings);

    // Gravity
    m_physicsSystem->SetGravity(JPH::Vec3(cfg.physicsGravity.x, cfg.physicsGravity.y, cfg.physicsGravity.z));

    // Install contact listener for collision/trigger callbacks
    m_contactListener = std::make_unique<InxContactListener>();
    m_physicsSystem->SetContactListener(m_contactListener.get());

    // Warm up the broadphase / job-system worker threads before the first real
    // Step().  On a cold start with many bodies, the very first Step() generates
    // a burst of jobs that can transiently exceed queue limits; running this
    // no-op pass lets the thread pool spin up and avoids the burst.
    m_physicsSystem->OptimizeBroadPhase();

    m_initialized = true;
    INXLOG_INFO("PhysicsWorld: Jolt Physics initialized.");
}

void PhysicsWorld::Shutdown()
{
    if (!m_initialized)
        return;

    INXLOG_INFO("PhysicsWorld: Shutting down...");

    // Step 1: drop contact pair tracking before any body dies, so callbacks
    // racing the teardown can't dereference freed Collider* pointers.
    if (m_contactListener)
        m_contactListener->ClearAll();

    // Step 1b: destroy any bodies that still exist. Scenes should already be
    // gone (SceneManager::Shutdown runs first in Infernux::Cleanup), so a
    // non-empty map here indicates a teardown-ordering bug — sweep it anyway
    // so Jolt's PhysicsSystem is destroyed with zero live bodies.
    if (m_physicsSystem && !m_bodyToCollider.empty()) {
        INXLOG_WARN("PhysicsWorld: ", m_bodyToCollider.size(),
                    " bodies still alive at shutdown — destroying them (check teardown order).");
        JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
        for (const auto &[id, collider] : m_bodyToCollider) {
            (void)collider;
            JPH::BodyID bodyId(id);
            if (bi.IsAdded(bodyId))
                bi.RemoveBody(bodyId);
            bi.DestroyBody(bodyId);
        }
    }
    m_bodyToCollider.clear();
    m_poseReadbackBodyIds.clear();
    m_continuousBodyIds.clear();
    m_kinematicMoveStates.clear();
    m_lastDynamicCCDSplitCount = 0;

    // Step 2: tear down subsystems in dependency order (newest first).
    m_contactListener.reset();
    m_physicsSystem.reset();
    m_jobSystem.reset();
    m_tempAllocator.reset();
    m_layers.reset();

    // Step 3: drop Jolt's process-global state. Safe because every owned
    // unique_ptr above has been released, so no Jolt object outlives the
    // factory.
    JPH::UnregisterTypes();
    delete JPH::Factory::sInstance;
    JPH::Factory::sInstance = nullptr;

    m_initialized = false;
}

// ============================================================================
// Step
// ============================================================================

float PhysicsWorld::FindEarliestDynamicCCDFraction(float deltaTime) const
{
    if (m_continuousBodyIds.empty())
        return 1.0f;

    struct BodySnapshot
    {
        JPH::BodyID id;
        JPH::ObjectLayer layer;
        JPH::CollisionGroup collisionGroup;
        JPH::RefConst<JPH::Shape> shape;
        JPH::RMat44 centerOfMassTransform;
        JPH::TransformedShape transformedShape;
        JPH::AABox sweptBounds;
        JPH::Vec3 deltaPosition;
        bool continuousDynamic = false;
    };

    JPH::BodyIDVector activeBodyIds;
    m_physicsSystem->GetActiveBodies(JPH::EBodyType::RigidBody, activeBodyIds);

    std::vector<BodySnapshot> bodies;
    bodies.reserve(activeBodyIds.size());
    for (const JPH::BodyID id : activeBodyIds) {
        JPH::BodyLockRead lock(m_physicsSystem->GetBodyLockInterface(), id);
        if (!lock.Succeeded())
            continue;

        const JPH::Body &body = lock.GetBody();
        if (body.IsStatic() || body.IsSensor() || body.GetMotionPropertiesUnchecked() == nullptr)
            continue;

        BodySnapshot snapshot;
        snapshot.id = id;
        snapshot.layer = body.GetObjectLayer();
        snapshot.collisionGroup = body.GetCollisionGroup();
        snapshot.shape = body.GetShape();
        snapshot.centerOfMassTransform = body.GetCenterOfMassTransform();
        snapshot.transformedShape = body.GetTransformedShape();
        snapshot.deltaPosition = deltaTime * body.GetLinearVelocity();
        snapshot.sweptBounds = body.GetWorldSpaceBounds();
        JPH::AABox endBounds = snapshot.sweptBounds;
        endBounds.Translate(snapshot.deltaPosition);
        snapshot.sweptBounds.Encapsulate(endBounds);
        snapshot.continuousDynamic =
            body.IsDynamic() && body.GetMotionProperties()->GetMotionQuality() == JPH::EMotionQuality::LinearCast;
        bodies.push_back(std::move(snapshot));
    }

    JPH::ShapeCastSettings castSettings;
    castSettings.mUseShrunkenShapeAndConvexRadius = true;
    castSettings.mBackFaceModeTriangles = JPH::EBackFaceMode::IgnoreBackFaces;
    castSettings.mBackFaceModeConvex = JPH::EBackFaceMode::IgnoreBackFaces;

    float earliestFraction = 1.0f;
    for (size_t sourceIndex = 0; sourceIndex < bodies.size(); ++sourceIndex) {
        const BodySnapshot &source = bodies[sourceIndex];
        if (!source.continuousDynamic)
            continue;

        for (size_t targetIndex = 0; targetIndex < bodies.size(); ++targetIndex) {
            if (sourceIndex == targetIndex)
                continue;

            const BodySnapshot &target = bodies[targetIndex];
            if (target.continuousDynamic && source.id > target.id)
                continue;
            if (!source.sweptBounds.Overlaps(target.sweptBounds))
                continue;
            if (!m_layers->objPairFilter.ShouldCollide(source.layer, target.layer) ||
                !source.collisionGroup.CanCollide(target.collisionGroup))
                continue;

            const JPH::Vec3 relativeMotion = source.deltaPosition - target.deltaPosition;
            if (relativeMotion.LengthSq() <= 1e-12f)
                continue;

            JPH::RShapeCast cast(source.shape, JPH::Vec3::sOne(), source.centerOfMassTransform, relativeMotion);
            JPH::ClosestHitCollisionCollector<JPH::CastShapeCollector> collector;
            JPH::ShapeFilter shapeFilter;
            shapeFilter.mBodyID2 = target.id;
            target.transformedShape.CastShape(cast, castSettings, source.centerOfMassTransform.GetTranslation(),
                                              collector, shapeFilter);
            if (!collector.HadHit())
                continue;

            const float fraction = collector.mHit.mFraction;
            if (fraction > 1e-4f && fraction < earliestFraction)
                earliestFraction = fraction;
        }
    }

    return earliestFraction;
}

void PhysicsWorld::SettleKinematicMoves()
{
    if (m_kinematicMoveStates.empty())
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    for (auto it = m_kinematicMoveStates.begin(); it != m_kinematicMoveStates.end();) {
        KinematicMoveState &state = it->second;
        if (state.movedThisStep) {
            // A target arrived since the previous step — let this step
            // integrate it, then re-evaluate.
            state.movedThisStep = false;
            ++it;
            continue;
        }

        const JPH::BodyID id(it->first);

        // The body arrived at its last target and no new target came in.
        // Without this, the MoveKinematic velocity persists and the body
        // glides away from its Transform forever.
        if (state.idleSteps == 0)
            bi.SetLinearAndAngularVelocity(id, JPH::Vec3::sZero(), JPH::Vec3::sZero());
        ++state.idleSteps;

        if (!state.restoreStatic) {
            // Genuine kinematic Rigidbody — stopping it is all we owe it.
            it = m_kinematicMoveStates.erase(it);
            continue;
        }

        // Temporarily-kinematic static: keep it kinematic for a few idle
        // steps, since a gizmo drag does not deliver a new pose on every
        // fixed step, then restore. SetMotionType(Static) deactivates the
        // body and zeroes its velocity inside Jolt.
        constexpr int kRestoreAfterIdleSteps = 3;
        if (state.idleSteps >= kRestoreAfterIdleSteps) {
            bool restore = true;
            if (auto found = m_bodyToCollider.find(it->first); found != m_bodyToCollider.end() && found->second) {
                const Rigidbody *rb = found->second->GetCachedRigidbody();
                restore = (rb == nullptr || !rb->IsEnabled());
            }
            if (restore)
                bi.SetMotionType(id, JPH::EMotionType::Static, JPH::EActivation::DontActivate);
            it = m_kinematicMoveStates.erase(it);
            continue;
        }
        ++it;
    }
}

void PhysicsWorld::Step(float deltaTime)
{
    m_poseReadbackBodyIds.clear();
    m_lastDynamicCCDSplitCount = 0;
    if (!m_initialized)
        return;

    SettleKinematicMoves();

    JPH::BodyIDVector activeBefore;
    m_physicsSystem->GetActiveBodies(JPH::EBodyType::RigidBody, activeBefore);

    if (m_contactListener)
        m_contactListener->PreStep();

    // Jolt's LinearCast narrow phase handles relative target motion, but its
    // broad-phase cast only spans the source body's own displacement. Two fast
    // dynamic bodies can therefore meet halfway without either source cast
    // reaching the other's starting AABB. Split at the earliest relative TOI
    // so the following Jolt update starts with the pair touching and lets the
    // regular contact solver own the response.
    constexpr int kMaxDynamicCCDSplits = 8;
    constexpr float kTOIPadding = 1e-4f;
    constexpr float kMinStepDuration = 1e-6f;
    float remainingTime = deltaTime;
    int dynamicCCDSplits = 0;
    while (remainingTime > kMinStepDuration) {
        const float hitFraction = FindEarliestDynamicCCDFraction(remainingTime);
        if (hitFraction >= 1.0f || dynamicCCDSplits >= kMaxDynamicCCDSplits) {
            m_physicsSystem->Update(remainingTime, EngineConfig::Get().physicsCollisionSteps, m_tempAllocator.get(),
                                    m_jobSystem.get());
            remainingTime = 0.0f;
            break;
        }

        const float segmentFraction = std::clamp(hitFraction + kTOIPadding, kTOIPadding, 0.999f);
        const float segmentTime = remainingTime * segmentFraction;
        m_physicsSystem->Update(segmentTime, 1, m_tempAllocator.get(), m_jobSystem.get());
        remainingTime -= segmentTime;
        ++dynamicCCDSplits;
    }
    m_lastDynamicCCDSplitCount = static_cast<size_t>(dynamicCCDSplits);

    JPH::BodyIDVector activeAfter;
    m_physicsSystem->GetActiveBodies(JPH::EBodyType::RigidBody, activeAfter);
    m_poseReadbackBodyIds.reserve(activeBefore.size() + activeAfter.size());
    for (const JPH::BodyID id : activeBefore)
        m_poseReadbackBodyIds.push_back(id.GetIndexAndSequenceNumber());
    for (const JPH::BodyID id : activeAfter)
        m_poseReadbackBodyIds.push_back(id.GetIndexAndSequenceNumber());
    std::sort(m_poseReadbackBodyIds.begin(), m_poseReadbackBodyIds.end());
    m_poseReadbackBodyIds.erase(std::unique(m_poseReadbackBodyIds.begin(), m_poseReadbackBodyIds.end()),
                                m_poseReadbackBodyIds.end());

    // Resolve raw contact events through pair tracking — suppresses spurious
    // Enter/Exit caused by Jolt's body sleep/wake cycles.
    if (m_contactListener) {
        JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
        m_contactListener->ResolveEvents(bi);
    }
}

// ============================================================================
// Contact event dispatch (Unity-style collision/trigger callbacks)
// ============================================================================

size_t PhysicsWorld::DispatchContactEvents()
{
    if (!m_contactListener)
        return 0;

    const auto &events = m_contactListener->GetEvents();
    if (events.empty())
        return 0;
    const size_t eventCount = events.size();

    std::vector<Component *> receiversA;
    std::vector<Component *> receiversB;
    receiversA.reserve(8);
    receiversB.reserve(8);

    for (const auto &evt : events) {
        Collider *colA = ResolveColliderForSubShape(evt.bodyIdA, evt.subShapeIdA);
        Collider *colB = ResolveColliderForSubShape(evt.bodyIdB, evt.subShapeIdB);
        if (!colA || !colB)
            continue;

        GameObject *goA = colA->GetGameObject();
        GameObject *goB = colB->GetGameObject();
        if (!goA || !goB)
            continue;

        ContactEventType type = evt.type;
        const bool triggerPair = colA->IsTrigger() || colB->IsTrigger();
        if (triggerPair) {
            if (type == ContactEventType::CollisionEnter)
                type = ContactEventType::TriggerEnter;
            else if (type == ContactEventType::CollisionStay)
                type = ContactEventType::TriggerStay;
            else if (type == ContactEventType::CollisionExit)
                type = ContactEventType::TriggerExit;
        } else {
            if (type == ContactEventType::TriggerEnter)
                type = ContactEventType::CollisionEnter;
            else if (type == ContactEventType::TriggerStay)
                type = ContactEventType::CollisionStay;
            else if (type == ContactEventType::TriggerExit)
                type = ContactEventType::CollisionExit;
        }

        // Layer filtering for trigger events — sensors bypass Jolt's object layer
        // pair filter, so enforce Infernux's layer collision matrix here.
        bool isTrigger = (type == ContactEventType::TriggerEnter || type == ContactEventType::TriggerStay ||
                          type == ContactEventType::TriggerExit);
        if (isTrigger) {
            int layerA = goA->GetLayer();
            int layerB = goB->GetLayer();
            if (!TagLayerManager::Instance().GetLayersCollide(layerA, layerB))
                continue;
        }

        receiversA.clear();
        receiversB.clear();

        for (const auto &comp : goA->GetAllComponents()) {
            if (!comp || !comp->IsEnabled() || !comp->WantsPhysicsCallbacks())
                continue;
            receiversA.push_back(comp.get());
        }

        for (const auto &comp : goB->GetAllComponents()) {
            if (!comp || !comp->IsEnabled() || !comp->WantsPhysicsCallbacks())
                continue;
            receiversB.push_back(comp.get());
        }

        if (receiversA.empty() && receiversB.empty())
            continue;

        // Build CollisionInfo for each side
        CollisionInfo infoForA;
        infoForA.collider = colB;
        infoForA.gameObject = goB;
        infoForA.contactPoint = evt.contactPoint;
        infoForA.contactNormal = evt.contactNormal;
        infoForA.relativeVelocity = evt.relativeVelocity;

        CollisionInfo infoForB;
        infoForB.collider = colA;
        infoForB.gameObject = goA;
        infoForB.contactPoint = evt.contactPoint;
        infoForB.contactNormal = -evt.contactNormal; // flip for B
        infoForB.relativeVelocity = -evt.relativeVelocity;

        // Dispatch to all components on both GameObjects
        auto dispatchToReceivers = [&](const std::vector<Component *> &receivers, const CollisionInfo &info,
                                       ContactEventType t) {
            for (Component *comp : receivers) {
                switch (t) {
                case ContactEventType::CollisionEnter:
                    comp->OnCollisionEnter(info);
                    break;
                case ContactEventType::CollisionStay:
                    comp->OnCollisionStay(info);
                    break;
                case ContactEventType::CollisionExit:
                    comp->OnCollisionExit(info);
                    break;
                case ContactEventType::TriggerEnter:
                    comp->OnTriggerEnter(info.collider);
                    break;
                case ContactEventType::TriggerStay:
                    comp->OnTriggerStay(info.collider);
                    break;
                case ContactEventType::TriggerExit:
                    comp->OnTriggerExit(info.collider);
                    break;
                }
            }
        };

        dispatchToReceivers(receiversA, infoForA, type);
        // Guard: a callback on side A may have destroyed body B's physics body (and vice-versa).
        // Re-validate both sides before dispatching to B's receivers.
        if (FindColliderByBodyId(evt.bodyIdA) && FindColliderByBodyId(evt.bodyIdB))
            dispatchToReceivers(receiversB, infoForB, type);
    }
    return eventCount;
}

// ============================================================================
// Body management
// ============================================================================

uint32_t PhysicsWorld::CreateBody(Collider *collider, bool isStatic, bool isTrigger)
{
    if (!m_initialized || !collider)
        return 0xFFFFFFFF;

    auto *go = collider->GetGameObject();
    if (!go)
        return 0xFFFFFFFF;

    size_t shapeCount = 0;
    auto shape = BuildShapeForColliderSet(go, nullptr, &shapeCount);
    if (!shape)
        return 0xFFFFFFFF;

    Transform *tf = go->GetTransform();
    glm::quat rot = tf->GetWorldRotation();
    glm::vec3 pos = tf->GetPosition();

    JPH::EMotionType motionType = isStatic ? JPH::EMotionType::Static : JPH::EMotionType::Dynamic;
    JPH::ObjectLayer objLayer = PhysicsObjectLayers::Encode(go->GetLayer(), !isStatic);

    JPH::BodyCreationSettings settings(shape, JPH::RVec3(pos.x, pos.y, pos.z), JPH::Quat(rot.x, rot.y, rot.z, rot.w),
                                       motionType, objLayer);

    // Allow static bodies to later be switched to dynamic/kinematic
    // (e.g. when a Rigidbody component is added). Without this flag,
    // Jolt does not create MotionProperties for static bodies and
    // SetMotionType() will crash.
    settings.mAllowDynamicOrKinematic = true;
    settings.mIsSensor = isTrigger;
    settings.mUserData = reinterpret_cast<uint64_t>(collider);
    settings.mUseManifoldReduction = shapeCount <= 1;

    // Body-level fallback. Contact callbacks replace these values using the
    // actual compound subshapes and their material combine modes.
    settings.mFriction = collider->GetFriction();
    settings.mRestitution = collider->GetBounciness();

    JPH::BodyInterface &bodyInterface = m_physicsSystem->GetBodyInterface();
    JPH::Body *body = bodyInterface.CreateBody(settings);
    if (!body) {
        INXLOG_ERROR("PhysicsWorld: Failed to create body.");
        return 0xFFFFFFFF;
    }

    JPH::BodyID bodyId = body->GetID();
    // NOTE: Body is created but NOT added to broadphase here.
    // Collider::OnEnable() calls AddBodyToBroadphase() to add it.

    uint32_t id = bodyId.GetIndexAndSequenceNumber();
    m_bodyToCollider[id] = collider;
    return id;
}

void PhysicsWorld::DestroyBody(Collider *collider)
{
    if (!m_initialized || !collider)
        return;

    uint32_t id = collider->GetBodyId();
    if (id == 0xFFFFFFFF)
        return;

    // Contract: caller (Collider::UnregisterBody) must have already removed
    // this body from the broadphase. See PhysicsWorld.h::DestroyBody for the
    // full ordering invariant.
    JPH::BodyInterface &bodyInterface = m_physicsSystem->GetBodyInterface();
    bodyInterface.DestroyBody(JPH::BodyID(id));

    m_bodyToCollider.erase(id);
    m_continuousBodyIds.erase(id);
    m_kinematicMoveStates.erase(id);
}

void PhysicsWorld::SetBodyPosition(uint32_t bodyId, const glm::vec3 &pos, const glm::quat &rot)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    // Transform sync runs serially on the main thread before PhysicsSystem::Update.
    // Avoid taking Jolt's striped body lock once per moved static/kinematic body.
    JPH::BodyInterface &bodyInterface = m_physicsSystem->GetBodyInterfaceNoLock();
    bodyInterface.SetPositionAndRotation(JPH::BodyID(bodyId), JPH::RVec3(pos.x, pos.y, pos.z),
                                         JPH::Quat(rot.x, rot.y, rot.z, rot.w), JPH::EActivation::DontActivate);
}

void PhysicsWorld::SetBodyPositionsBatch(const std::vector<PhysicsBodyPoseUpdate> &updates)
{
    if (!m_initialized || updates.empty())
        return;

    static thread_local JPH::BodyIDVector bodyIds;
    static thread_local std::vector<JPH::RVec3> positions;
    static thread_local std::vector<JPH::Quat> rotations;
    bodyIds.clear();
    positions.clear();
    rotations.clear();
    bodyIds.reserve(updates.size());
    positions.reserve(updates.size());
    rotations.reserve(updates.size());
    for (const auto &update : updates) {
        if (update.bodyId == 0xFFFFFFFF)
            continue;
        bodyIds.emplace_back(update.bodyId);
        positions.emplace_back(update.position.x, update.position.y, update.position.z);
        rotations.emplace_back(update.rotation.x, update.rotation.y, update.rotation.z, update.rotation.w);
    }
    if (bodyIds.empty())
        return;

    JPH::BodyInterface &bodyInterface = m_physicsSystem->GetBodyInterfaceNoLock();
    bodyInterface.SetPositionAndRotationBatch(bodyIds.data(), positions.data(), rotations.data(),
                                              static_cast<int>(bodyIds.size()));
}

void PhysicsWorld::UpdateBodyShape(Collider *collider, const Collider *exclude)
{
    if (!m_initialized || !collider)
        return;

    uint32_t id = collider->GetBodyId();
    if (id == 0xFFFFFFFF)
        return;

    size_t shapeCount = 0;
    auto newShape = BuildShapeForColliderSet(collider->GetGameObject(), exclude, &shapeCount);
    if (!newShape)
        return;

    JPH::BodyInterface &bodyInterface = m_physicsSystem->GetBodyInterface();
    bodyInterface.SetUseManifoldReduction(JPH::BodyID(id), shapeCount <= 1);
    bodyInterface.SetShape(JPH::BodyID(id), newShape, true, JPH::EActivation::Activate);
}

void PhysicsWorld::SetBodyIsSensor(uint32_t bodyId, bool isSensor)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyLockWrite lock(m_physicsSystem->GetBodyLockInterface(), JPH::BodyID(bodyId));
    if (lock.Succeeded()) {
        lock.GetBody().SetIsSensor(isSensor);
    }
}

void PhysicsWorld::InvalidateContactPairsForBody(uint32_t bodyId)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;
    if (m_contactListener)
        m_contactListener->InvalidatePairsForBody(bodyId);
}

void PhysicsWorld::AddBodyToBroadphase(uint32_t bodyId, bool isStatic)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bodyInterface = m_physicsSystem->GetBodyInterface();
    bodyInterface.AddBody(JPH::BodyID(bodyId), isStatic ? JPH::EActivation::DontActivate : JPH::EActivation::Activate);
}

void PhysicsWorld::AddBodiesBatch(const std::vector<std::pair<uint32_t, bool>> &bodies)
{
    if (!m_initialized || bodies.empty())
        return;

    // Separate static and dynamic bodies since they need different activation modes.
    std::vector<JPH::BodyID> staticIds;
    std::vector<JPH::BodyID> dynamicIds;
    staticIds.reserve(bodies.size());
    dynamicIds.reserve(bodies.size() / 4); // most spawned bodies are static

    for (auto &[id, isStatic] : bodies) {
        if (id == 0xFFFFFFFF)
            continue;
        if (isStatic)
            staticIds.push_back(JPH::BodyID(id));
        else
            dynamicIds.push_back(JPH::BodyID(id));
    }

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();

    if (!staticIds.empty()) {
        JPH::BodyInterface::AddState state = bi.AddBodiesPrepare(staticIds.data(), static_cast<int>(staticIds.size()));
        bi.AddBodiesFinalize(staticIds.data(), static_cast<int>(staticIds.size()), state,
                             JPH::EActivation::DontActivate);
    }
    if (!dynamicIds.empty()) {
        JPH::BodyInterface::AddState state =
            bi.AddBodiesPrepare(dynamicIds.data(), static_cast<int>(dynamicIds.size()));
        bi.AddBodiesFinalize(dynamicIds.data(), static_cast<int>(dynamicIds.size()), state, JPH::EActivation::Activate);
    }
}

void PhysicsWorld::RemoveBodyFromBroadphase(uint32_t bodyId)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bodyInterface = m_physicsSystem->GetBodyInterface();
    bodyInterface.RemoveBody(JPH::BodyID(bodyId));
}

// ============================================================================
// Body dynamics (used by Rigidbody component)
// ============================================================================

void PhysicsWorld::SetBodyMotionType(uint32_t bodyId, int motionType)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::EMotionType mt;
    JPH::ObjectLayer layer;
    switch (motionType) {
    case 0:
        mt = JPH::EMotionType::Static;
        layer = PhysicsObjectLayers::Encode(0, false);
        break;
    case 1:
        mt = JPH::EMotionType::Kinematic;
        layer = PhysicsObjectLayers::Encode(0, true);
        break;
    case 2:
    default:
        mt = JPH::EMotionType::Dynamic;
        layer = PhysicsObjectLayers::Encode(0, true);
        break;
    }

    if (auto it = m_bodyToCollider.find(bodyId);
        it != m_bodyToCollider.end() && it->second && it->second->GetGameObject()) {
        layer = PhysicsObjectLayers::Encode(it->second->GetGameObject()->GetLayer(), motionType != 0);
    }

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.SetMotionType(JPH::BodyID(bodyId), mt, JPH::EActivation::Activate);
    bi.SetObjectLayer(JPH::BodyID(bodyId), layer);
    if (mt != JPH::EMotionType::Dynamic)
        m_continuousBodyIds.erase(bodyId);
    // An explicit motion-type change supersedes any pending drag tracking —
    // never restore this body to Static behind the caller's back.
    m_kinematicMoveStates.erase(bodyId);
}

void PhysicsWorld::SetBodyGameLayer(uint32_t bodyId, int gameLayer)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    const JPH::EMotionType motionType = bi.GetMotionType(JPH::BodyID(bodyId));
    const bool moving = motionType != JPH::EMotionType::Static;
    bi.SetObjectLayer(JPH::BodyID(bodyId), PhysicsObjectLayers::Encode(gameLayer, moving));
}

void PhysicsWorld::SetBodyMassProperties(uint32_t bodyId, float mass)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyLockWrite lock(m_physicsSystem->GetBodyLockInterface(), JPH::BodyID(bodyId));
    if (lock.Succeeded()) {
        JPH::Body &body = lock.GetBody();
        if (body.IsDynamic()) {
            JPH::MotionProperties *mp = body.GetMotionProperties();
            if (mp->GetInverseMass() > 0.0f) {
                // Scale mass and inertia proportionally
                mp->ScaleToMass(mass > 0.001f ? mass : 0.001f);
            } else {
                // Body was just switched from static — compute mass from shape
                JPH::MassProperties massProp = body.GetShape()->GetMassProperties();
                massProp.ScaleToMass(mass > 0.001f ? mass : 0.001f);
                mp->SetMassProperties(JPH::EAllowedDOFs::All, massProp);
            }
        }
    }
}

void PhysicsWorld::SetBodyDamping(uint32_t bodyId, float linearDamping, float angularDamping)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyLockWrite lock(m_physicsSystem->GetBodyLockInterface(), JPH::BodyID(bodyId));
    if (lock.Succeeded()) {
        JPH::Body &body = lock.GetBody();
        if (!body.IsStatic()) {
            JPH::MotionProperties *mp = body.GetMotionProperties();
            mp->SetLinearDamping(linearDamping);
            mp->SetAngularDamping(angularDamping);
        }
    }
}

void PhysicsWorld::SetBodyGravityFactor(uint32_t bodyId, float factor)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyLockWrite lock(m_physicsSystem->GetBodyLockInterface(), JPH::BodyID(bodyId));
    if (lock.Succeeded()) {
        JPH::Body &body = lock.GetBody();
        if (!body.IsStatic()) {
            body.GetMotionProperties()->SetGravityFactor(factor);
        }
    }
}

void PhysicsWorld::SetBodyFriction(uint32_t bodyId, float friction)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.SetFriction(JPH::BodyID(bodyId), friction);
}

void PhysicsWorld::SetBodyRestitution(uint32_t bodyId, float restitution)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.SetRestitution(JPH::BodyID(bodyId), restitution);
}

glm::vec3 PhysicsWorld::GetBodyLinearVelocity(uint32_t bodyId) const
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return glm::vec3(0.0f);

    const JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterfaceNoLock();
    JPH::Vec3 v = bi.GetLinearVelocity(JPH::BodyID(bodyId));
    return glm::vec3(v.GetX(), v.GetY(), v.GetZ());
}

void PhysicsWorld::SetBodyLinearVelocity(uint32_t bodyId, const glm::vec3 &vel)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.SetLinearVelocity(JPH::BodyID(bodyId), JPH::Vec3(vel.x, vel.y, vel.z));

    // An explicit velocity supersedes a pending MoveKinematic arrival stop
    // (e.g. constant-velocity kinematic platforms). Drag-converted statics
    // stay tracked so they are still restored to Static.
    if (auto it = m_kinematicMoveStates.find(bodyId);
        it != m_kinematicMoveStates.end() && !it->second.restoreStatic)
        m_kinematicMoveStates.erase(it);
}

glm::vec3 PhysicsWorld::GetBodyAngularVelocity(uint32_t bodyId) const
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return glm::vec3(0.0f);

    const JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterfaceNoLock();
    JPH::Vec3 v = bi.GetAngularVelocity(JPH::BodyID(bodyId));
    return glm::vec3(v.GetX(), v.GetY(), v.GetZ());
}

void PhysicsWorld::SetBodyAngularVelocity(uint32_t bodyId, const glm::vec3 &vel)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.SetAngularVelocity(JPH::BodyID(bodyId), JPH::Vec3(vel.x, vel.y, vel.z));

    if (auto it = m_kinematicMoveStates.find(bodyId);
        it != m_kinematicMoveStates.end() && !it->second.restoreStatic)
        m_kinematicMoveStates.erase(it);
}

void PhysicsWorld::AddBodyForce(uint32_t bodyId, const glm::vec3 &force)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.AddForce(JPH::BodyID(bodyId), JPH::Vec3(force.x, force.y, force.z));
}

void PhysicsWorld::AddBodyImpulse(uint32_t bodyId, const glm::vec3 &impulse)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.AddImpulse(JPH::BodyID(bodyId), JPH::Vec3(impulse.x, impulse.y, impulse.z));
}

void PhysicsWorld::AddBodyTorque(uint32_t bodyId, const glm::vec3 &torque)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.AddTorque(JPH::BodyID(bodyId), JPH::Vec3(torque.x, torque.y, torque.z));
}

void PhysicsWorld::AddBodyAngularImpulse(uint32_t bodyId, const glm::vec3 &impulse)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.AddAngularImpulse(JPH::BodyID(bodyId), JPH::Vec3(impulse.x, impulse.y, impulse.z));
}

// ---- Forces at position ----

void PhysicsWorld::AddBodyForceAtPosition(uint32_t bodyId, const glm::vec3 &force, const glm::vec3 &point)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.AddForce(JPH::BodyID(bodyId), JPH::Vec3(force.x, force.y, force.z), JPH::RVec3(point.x, point.y, point.z));
}

void PhysicsWorld::AddBodyImpulseAtPosition(uint32_t bodyId, const glm::vec3 &impulse, const glm::vec3 &point)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.AddImpulse(JPH::BodyID(bodyId), JPH::Vec3(impulse.x, impulse.y, impulse.z),
                  JPH::RVec3(point.x, point.y, point.z));
}

// ---- Constraints / Motion quality ----

void PhysicsWorld::SetBodyAllowedDOFs(uint32_t bodyId, int allowedDOFs, float mass)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyLockWrite lock(m_physicsSystem->GetBodyLockInterface(), JPH::BodyID(bodyId));
    if (lock.Succeeded()) {
        JPH::Body &body = lock.GetBody();
        if (body.IsDynamic()) {
            JPH::MassProperties massProps = body.GetShape()->GetMassProperties();
            massProps.ScaleToMass(mass > 0.001f ? mass : 0.001f);

            // Thin-body inertia stabilization: ensure no principal axis has
            // less than 10 % of the maximum moment of inertia.  This prevents
            // extreme angular accelerations for flat / thin shapes (e.g.
            // sprites, panels) where one dimension is much smaller than the
            // others.
            JPH::Vec3 diag = massProps.mInertia.GetDiagonal3();
            float maxI = std::max({diag.GetX(), diag.GetY(), diag.GetZ()});
            if (maxI > 1e-10f) {
                float minI = maxI * 0.1f;
                massProps.mInertia.SetDiagonal3(
                    JPH::Vec3(std::max(diag.GetX(), minI), std::max(diag.GetY(), minI), std::max(diag.GetZ(), minI)));
            }

            body.GetMotionProperties()->SetMassProperties(static_cast<JPH::EAllowedDOFs>(allowedDOFs), massProps);
        }
    }
}

void PhysicsWorld::SetBodyMotionQuality(uint32_t bodyId, int quality)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::EMotionQuality mq =
        (MapMotionQualityMode(quality) == 1) ? JPH::EMotionQuality::LinearCast : JPH::EMotionQuality::Discrete;
    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.SetMotionQuality(JPH::BodyID(bodyId), mq);
    if (mq == JPH::EMotionQuality::LinearCast)
        m_continuousBodyIds.insert(bodyId);
    else
        m_continuousBodyIds.erase(bodyId);
}

void PhysicsWorld::SetBodyMaxAngularVelocity(uint32_t bodyId, float maxVel)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyLockWrite lock(m_physicsSystem->GetBodyLockInterface(), JPH::BodyID(bodyId));
    if (lock.Succeeded()) {
        JPH::Body &body = lock.GetBody();
        if (!body.IsStatic()) {
            body.GetMotionProperties()->SetMaxAngularVelocity(maxVel);
        }
    }
}

void PhysicsWorld::SetBodyMaxLinearVelocity(uint32_t bodyId, float maxVel)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyLockWrite lock(m_physicsSystem->GetBodyLockInterface(), JPH::BodyID(bodyId));
    if (lock.Succeeded()) {
        JPH::Body &body = lock.GetBody();
        if (!body.IsStatic()) {
            body.GetMotionProperties()->SetMaxLinearVelocity(maxVel);
        }
    }
}

// ---- Kinematic move ----

void PhysicsWorld::MoveBodyKinematic(uint32_t bodyId, const glm::vec3 &targetPos, const glm::quat &targetRot,
                                     float deltaTime)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.MoveKinematic(JPH::BodyID(bodyId), JPH::RVec3(targetPos.x, targetPos.y, targetPos.z),
                     JPH::Quat(targetRot.x, targetRot.y, targetRot.z, targetRot.w), deltaTime);

    // Track the move so SettleKinematicMoves() can zero the velocity once the
    // body has arrived — MoveKinematic velocity persists in Jolt otherwise.
    auto &state = m_kinematicMoveStates[bodyId];
    state.movedThisStep = true;
    state.idleSteps = 0;
}

void PhysicsWorld::MoveStaticBodyWithVelocity(uint32_t bodyId, const glm::vec3 &targetPos, const glm::quat &targetRot,
                                              float deltaTime)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF || deltaTime <= 0.0f)
        return;

    const JPH::BodyID id(bodyId);
    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();

    const JPH::EMotionType motionType = bi.GetMotionType(id);
    if (motionType == JPH::EMotionType::Dynamic) {
        // A Rigidbody took ownership of this body mid-drag — plain teleport.
        SetBodyPosition(bodyId, targetPos, targetRot);
        return;
    }

    if (motionType == JPH::EMotionType::Static) {
        // The object layer is intentionally left unchanged (still non-moving):
        // statics never collide with each other, and dynamic bodies pair with
        // the non-moving layer, which is all a drag push needs.
        bi.SetMotionType(id, JPH::EMotionType::Kinematic, JPH::EActivation::Activate);
    }

    // Cap the contact velocity the drag can impart (matches PhysX's default
    // maxDepenetrationVelocity). Any excess displacement — e.g. the first sync
    // after un-pausing, or a scripted long-range teleport — is applied as a
    // teleport so the body never lags behind its Transform.
    constexpr float kMaxDragSpeed = 10.0f;
    const JPH::RVec3 target(targetPos.x, targetPos.y, targetPos.z);
    const JPH::Quat targetQuat = ToJoltQuat(targetRot);
    const JPH::Vec3 delta(target - bi.GetPosition(id));
    const float maxStepDistance = kMaxDragSpeed * deltaTime;
    const float distance = delta.Length();
    if (distance > maxStepDistance) {
        const JPH::RVec3 nearTarget = target - JPH::RVec3(delta * (maxStepDistance / distance));
        bi.SetPositionAndRotation(id, nearTarget, targetQuat, JPH::EActivation::Activate);
    }
    bi.MoveKinematic(id, target, targetQuat, deltaTime);

    auto &state = m_kinematicMoveStates[bodyId];
    state.movedThisStep = true;
    state.idleSteps = 0;
    state.restoreStatic = true;
}

bool PhysicsWorld::IsBodySleeping(uint32_t bodyId) const
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return true;

    const JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterfaceNoLock();
    return !bi.IsActive(JPH::BodyID(bodyId));
}

bool PhysicsWorld::IsBodySensor(uint32_t bodyId) const
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return false;

    JPH::BodyLockRead lock(m_physicsSystem->GetBodyLockInterface(), JPH::BodyID(bodyId));
    return lock.Succeeded() ? lock.GetBody().IsSensor() : false;
}

void PhysicsWorld::ActivateBody(uint32_t bodyId)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.ActivateBody(JPH::BodyID(bodyId));
}

void PhysicsWorld::ActivateBodiesInAABB(const glm::vec3 &min, const glm::vec3 &max)
{
    if (!m_initialized)
        return;

    JPH::AABox box(JPH::Vec3(min.x, min.y, min.z), JPH::Vec3(max.x, max.y, max.z));
    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.ActivateBodiesInAABox(box, JPH::BroadPhaseLayerFilter(), JPH::ObjectLayerFilter());
}

void PhysicsWorld::WakeBodiesTouchingStatic(uint32_t bodyId)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    // Read the body's world-space AABB, then RELEASE the lock before
    // calling ActivateBodiesInAABox (which takes its own internal locks).
    // Holding BodyLockRead while calling the locking BodyInterface causes
    // a deadlock on Jolt's striped mutex.
    JPH::AABox bounds;
    {
        JPH::BodyLockRead lock(m_physicsSystem->GetBodyLockInterface(), JPH::BodyID(bodyId));
        if (!lock.Succeeded())
            return;

        const JPH::Body &body = lock.GetBody();
        bounds = body.GetWorldSpaceBounds();
    } // lock released

    // Expand by a small margin so bodies resting exactly on the surface are caught
    bounds.ExpandBy(JPH::Vec3::sReplicate(0.1f));

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.ActivateBodiesInAABox(bounds, JPH::BroadPhaseLayerFilter(), JPH::ObjectLayerFilter());
}

void PhysicsWorld::DeactivateBody(uint32_t bodyId)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return;

    JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterface();
    bi.DeactivateBody(JPH::BodyID(bodyId));
}

glm::vec3 PhysicsWorld::GetBodyPosition(uint32_t bodyId) const
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return glm::vec3(0.0f);

    // NoLock: safe because this is called from main thread AFTER Step().
    const JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterfaceNoLock();
    JPH::RVec3 p = bi.GetPosition(JPH::BodyID(bodyId));
    return glm::vec3(static_cast<float>(p.GetX()), static_cast<float>(p.GetY()), static_cast<float>(p.GetZ()));
}

glm::quat PhysicsWorld::GetBodyRotation(uint32_t bodyId) const
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return glm::quat(1, 0, 0, 0);

    const JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterfaceNoLock();
    JPH::Quat q = bi.GetRotation(JPH::BodyID(bodyId));
    return glm::normalize(glm::quat(q.GetW(), q.GetX(), q.GetY(), q.GetZ()));
}

glm::vec3 PhysicsWorld::GetBodyCenterOfMassPosition(uint32_t bodyId) const
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return glm::vec3(0.0f);

    const JPH::BodyInterface &bi = m_physicsSystem->GetBodyInterfaceNoLock();
    JPH::RVec3 p = bi.GetCenterOfMassPosition(JPH::BodyID(bodyId));
    return glm::vec3(static_cast<float>(p.GetX()), static_cast<float>(p.GetY()), static_cast<float>(p.GetZ()));
}

glm::mat3 PhysicsWorld::GetBodyWorldSpaceInertiaTensor(uint32_t bodyId) const
{
    if (!m_initialized || bodyId == 0xFFFFFFFF)
        return glm::mat3(0.0f);

    JPH::BodyLockRead lock(m_physicsSystem->GetBodyLockInterface(), JPH::BodyID(bodyId));
    if (!lock.Succeeded())
        return glm::mat3(0.0f);

    const JPH::Body &body = lock.GetBody();
    if (!body.IsDynamic())
        return glm::mat3(0.0f);

    const JPH::Mat44 invInertia = body.GetInverseInertia();
    const JPH::Vec3 c0 = invInertia.GetColumn3(0);
    const JPH::Vec3 c1 = invInertia.GetColumn3(1);
    const JPH::Vec3 c2 = invInertia.GetColumn3(2);

    const glm::mat3 invTensor(glm::vec3(c0.GetX(), c0.GetY(), c0.GetZ()), glm::vec3(c1.GetX(), c1.GetY(), c1.GetZ()),
                              glm::vec3(c2.GetX(), c2.GetY(), c2.GetZ()));

    const auto allowedDofs = static_cast<uint8_t>(body.GetMotionProperties()->GetAllowedDOFs());
    return InvertAllowedAngularSubspace(invTensor, allowedDofs);
}

// ============================================================================
// Raycast
// ============================================================================

bool PhysicsWorld::Raycast(const glm::vec3 &origin, const glm::vec3 &direction, float maxDistance, RaycastHit &outHit,
                           uint32_t layerMask, bool queryTriggers) const
{
    if (!m_initialized || layerMask == 0)
        return false;

    auto hits = RaycastAll(origin, direction, maxDistance, layerMask, queryTriggers);
    if (hits.empty()) {
        return false;
    }

    outHit = hits.front();
    return true;
}

std::vector<RaycastHit> PhysicsWorld::RaycastAll(const glm::vec3 &origin, const glm::vec3 &direction, float maxDistance,
                                                 uint32_t layerMask, bool queryTriggers) const
{
    SceneManager::Instance().EnsurePhysicsQueriesCurrent();
    std::vector<RaycastHit> hits;
    if (!m_initialized || layerMask == 0 || !IsFinite(origin))
        return hits;

    glm::vec3 dir(0.0f);
    if (!NormalizeQueryDirection(direction, maxDistance, dir)) {
        return hits;
    }

    JPH::RRayCast ray(JPH::RVec3(origin.x, origin.y, origin.z),
                      JPH::Vec3(dir.x * maxDistance, dir.y * maxDistance, dir.z * maxDistance));

    const JPH::NarrowPhaseQuery &npQuery = m_physicsSystem->GetNarrowPhaseQuery();
    JPH::AllHitCollisionCollector<JPH::CastRayCollector> collector;
    LayerMaskObjectFilter objectFilter(layerMask);
    npQuery.CastRay(ray, JPH::RayCastSettings(), collector, JPH::BroadPhaseLayerFilter(), objectFilter);

    if (!collector.HadHit()) {
        return hits;
    }

    collector.Sort();
    hits.reserve(static_cast<size_t>(collector.mHits.size()));

    for (const JPH::RayCastResult &result : collector.mHits) {
        const uint32_t bodyId = result.mBodyID.GetIndexAndSequenceNumber();

        RaycastHit hit;
        hit.distance = result.mFraction * maxDistance;
        hit.point = origin + dir * hit.distance;
        hit.bodyId = bodyId;

        hit.collider = ResolveColliderForSubShape(hit.bodyId, result.mSubShapeID2.GetValue());
        if (!queryTriggers && (IsBodySensor(bodyId) || (hit.collider && hit.collider->IsTrigger())))
            continue;
        if (hit.collider && hit.collider->GetGameObject()) {
            hit.gameObject = hit.collider->GetGameObject();
        }

        JPH::BodyLockRead lock(m_physicsSystem->GetBodyLockInterface(), result.mBodyID);
        if (lock.Succeeded()) {
            JPH::Vec3 normal = lock.GetBody().GetWorldSpaceSurfaceNormal(
                result.mSubShapeID2, JPH::RVec3(hit.point.x, hit.point.y, hit.point.z));
            hit.normal = glm::vec3(normal.GetX(), normal.GetY(), normal.GetZ());
        }

        hits.push_back(hit);
    }

    return hits;
}

// ============================================================================
// Shared overlap / shape-cast implementations
// ============================================================================

std::vector<Collider *> PhysicsWorld::OverlapShapeImpl(const JPH::Shape &shape, const glm::vec3 &center,
                                                       const glm::quat &orientation, uint32_t layerMask,
                                                       bool queryTriggers) const
{
    SceneManager::Instance().EnsurePhysicsQueriesCurrent();
    std::vector<Collider *> results;
    JPH::CollideShapeSettings settings;
    JPH::RMat44 transform =
        JPH::RMat44::sRotationTranslation(ToJoltQuat(orientation), JPH::RVec3(center.x, center.y, center.z));

    const JPH::NarrowPhaseQuery &npQuery = m_physicsSystem->GetNarrowPhaseQuery();
    JPH::AllHitCollisionCollector<JPH::CollideShapeCollector> collector;
    LayerMaskObjectFilter objectFilter(layerMask);
    npQuery.CollideShape(&shape, JPH::Vec3::sReplicate(1.0f), transform, settings,
                         JPH::RVec3(center.x, center.y, center.z), collector, JPH::BroadPhaseLayerFilter(),
                         objectFilter);

    if (!collector.HadHit())
        return results;

    std::unordered_set<Collider *> seen;
    for (const auto &hit : collector.mHits) {
        uint32_t bodyId = hit.mBodyID2.GetIndexAndSequenceNumber();
        Collider *col = ResolveColliderForSubShape(bodyId, hit.mSubShapeID2.GetValue());
        if (!queryTriggers && (IsBodySensor(bodyId) || (col && col->IsTrigger())))
            continue;
        if (col && seen.insert(col).second) {
            results.push_back(col);
        }
    }
    return results;
}

bool PhysicsWorld::ShapeCastImpl(const JPH::Shape &shape, const glm::vec3 &origin, const glm::quat &orientation,
                                 const glm::vec3 &direction, float maxDistance, RaycastHit &outHit, uint32_t layerMask,
                                 bool queryTriggers) const
{
    SceneManager::Instance().EnsurePhysicsQueriesCurrent();
    glm::vec3 dir(0.0f);
    if (!NormalizeQueryDirection(direction, maxDistance, dir))
        return false;

    JPH::RShapeCast shapeCast = JPH::RShapeCast::sFromWorldTransform(
        &shape, JPH::Vec3::sReplicate(1.0f),
        JPH::RMat44::sRotationTranslation(ToJoltQuat(orientation), JPH::RVec3(origin.x, origin.y, origin.z)),
        JPH::Vec3(dir.x * maxDistance, dir.y * maxDistance, dir.z * maxDistance));

    JPH::ShapeCastSettings castSettings;
    JPH::AllHitCollisionCollector<JPH::CastShapeCollector> collector;
    LayerMaskObjectFilter objectFilter(layerMask);

    const JPH::NarrowPhaseQuery &npQuery = m_physicsSystem->GetNarrowPhaseQuery();
    npQuery.CastShape(shapeCast, castSettings, JPH::RVec3(origin.x, origin.y, origin.z), collector,
                      JPH::BroadPhaseLayerFilter(), objectFilter);

    if (!collector.HadHit())
        return false;

    collector.Sort();
    const JPH::ShapeCastResult *selected = nullptr;
    Collider *selectedCollider = nullptr;
    for (const auto &candidate : collector.mHits) {
        uint32_t candidateBodyId = candidate.mBodyID2.GetIndexAndSequenceNumber();
        Collider *candidateCollider = ResolveColliderForSubShape(candidateBodyId, candidate.mSubShapeID2.GetValue());
        if (!queryTriggers && (IsBodySensor(candidateBodyId) || (candidateCollider && candidateCollider->IsTrigger())))
            continue;
        selected = &candidate;
        selectedCollider = candidateCollider;
        break;
    }
    if (!selected)
        return false;

    const auto &result = *selected;
    uint32_t bodyId = result.mBodyID2.GetIndexAndSequenceNumber();

    outHit.distance = result.mFraction * maxDistance;
    outHit.point = origin + dir * outHit.distance;
    outHit.bodyId = bodyId;
    outHit.normal =
        glm::vec3(-result.mPenetrationAxis.GetX(), -result.mPenetrationAxis.GetY(), -result.mPenetrationAxis.GetZ());
    float nLen = glm::length(outHit.normal);
    if (nLen > kEpsilon)
        outHit.normal /= nLen;

    outHit.collider = selectedCollider;
    if (outHit.collider && outHit.collider->GetGameObject())
        outHit.gameObject = outHit.collider->GetGameObject();

    return true;
}

// ============================================================================
// Overlap queries (Unity: Physics.OverlapSphere / OverlapBox)
// ============================================================================

std::vector<Collider *> PhysicsWorld::OverlapSphere(const glm::vec3 &center, float radius, uint32_t layerMask,
                                                    bool queryTriggers) const
{
    if (!m_initialized || layerMask == 0 || !IsFinite(center) || !std::isfinite(radius) || radius <= 0.0f)
        return {};
    JPH::SphereShape sphere(radius);
    return OverlapShapeImpl(sphere, center, glm::quat(1.0f, 0.0f, 0.0f, 0.0f), layerMask, queryTriggers);
}

std::vector<Collider *> PhysicsWorld::OverlapBox(const glm::vec3 &center, const glm::vec3 &halfExtents,
                                                 const glm::quat &orientation, uint32_t layerMask,
                                                 bool queryTriggers) const
{
    if (!m_initialized || layerMask == 0 || !IsFinite(center) || !HasPositiveFiniteExtents(halfExtents) ||
        !IsFinite(orientation))
        return {};
    JPH::BoxShape box(JPH::Vec3(halfExtents.x, halfExtents.y, halfExtents.z));
    return OverlapShapeImpl(box, center, orientation, layerMask, queryTriggers);
}

std::vector<Collider *> PhysicsWorld::OverlapCapsule(const glm::vec3 &point0, const glm::vec3 &point1, float radius,
                                                     uint32_t layerMask, bool queryTriggers) const
{
    if (!m_initialized || layerMask == 0 || !IsFinite(point0) || !IsFinite(point1) || !std::isfinite(radius) ||
        radius <= 0.0f)
        return {};
    const float segmentLength = glm::length(point1 - point0);
    const glm::vec3 center = (point0 + point1) * 0.5f;
    if (segmentLength <= 1e-6f) {
        JPH::SphereShape sphere(radius);
        return OverlapShapeImpl(sphere, center, glm::quat(1.0f, 0.0f, 0.0f, 0.0f), layerMask, queryTriggers);
    }
    JPH::CapsuleShape capsule(segmentLength * 0.5f, radius);
    return OverlapShapeImpl(capsule, center, CapsuleOrientation(point0, point1), layerMask, queryTriggers);
}

// ============================================================================
// Shape cast queries (Unity: Physics.SphereCast / BoxCast)
// ============================================================================

bool PhysicsWorld::SphereCast(const glm::vec3 &origin, float radius, const glm::vec3 &direction, float maxDistance,
                              RaycastHit &outHit, uint32_t layerMask, bool queryTriggers) const
{
    if (!m_initialized || layerMask == 0 || !IsFinite(origin) || !std::isfinite(radius) || radius <= 0.0f)
        return false;
    JPH::SphereShape sphere(radius);
    return ShapeCastImpl(sphere, origin, glm::quat(1.0f, 0.0f, 0.0f, 0.0f), direction, maxDistance, outHit, layerMask,
                         queryTriggers);
}

bool PhysicsWorld::BoxCast(const glm::vec3 &center, const glm::vec3 &halfExtents, const glm::vec3 &direction,
                           const glm::quat &orientation, float maxDistance, RaycastHit &outHit, uint32_t layerMask,
                           bool queryTriggers) const
{
    if (!m_initialized || layerMask == 0 || !IsFinite(center) || !HasPositiveFiniteExtents(halfExtents) ||
        !IsFinite(orientation))
        return false;

    JPH::BoxShape box(JPH::Vec3(halfExtents.x, halfExtents.y, halfExtents.z));
    return ShapeCastImpl(box, center, orientation, direction, maxDistance, outHit, layerMask, queryTriggers);
}

bool PhysicsWorld::CapsuleCast(const glm::vec3 &point0, const glm::vec3 &point1, float radius,
                               const glm::vec3 &direction, float maxDistance, RaycastHit &outHit, uint32_t layerMask,
                               bool queryTriggers) const
{
    if (!m_initialized || layerMask == 0 || !IsFinite(point0) || !IsFinite(point1) || !std::isfinite(radius) ||
        radius <= 0.0f)
        return false;
    const float segmentLength = glm::length(point1 - point0);
    const glm::vec3 center = (point0 + point1) * 0.5f;
    if (segmentLength <= 1e-6f) {
        JPH::SphereShape sphere(radius);
        return ShapeCastImpl(sphere, center, glm::quat(1.0f, 0.0f, 0.0f, 0.0f), direction, maxDistance, outHit,
                             layerMask, queryTriggers);
    }
    JPH::CapsuleShape capsule(segmentLength * 0.5f, radius);
    return ShapeCastImpl(capsule, center, CapsuleOrientation(point0, point1), direction, maxDistance, outHit, layerMask,
                         queryTriggers);
}

// ============================================================================
// Lookup
// ============================================================================

Collider *PhysicsWorld::FindColliderByBodyId(uint32_t bodyId) const
{
    auto it = m_bodyToCollider.find(bodyId);
    return (it != m_bodyToCollider.end()) ? it->second : nullptr;
}

Collider *PhysicsWorld::ResolveColliderForSubShape(uint32_t bodyId, uint32_t subShapeIdValue) const
{
    Collider *fallback = FindColliderByBodyId(bodyId);
    if (!fallback) {
        return nullptr;
    }

    auto *go = fallback->GetGameObject();
    if (!go) {
        return fallback;
    }

    JPH::BodyID joltBodyId(bodyId);
    JPH::BodyLockRead lock(m_physicsSystem->GetBodyLockInterface(), joltBodyId);
    if (!lock.Succeeded()) {
        return fallback;
    }

    const JPH::Shape *shape = lock.GetBody().GetShape();
    if (!shape || shape->GetType() != JPH::EShapeType::Compound) {
        return fallback;
    }

    const auto *compound = static_cast<const JPH::CompoundShape *>(shape);
    JPH::SubShapeID subShapeId;
    subShapeId.SetValue(subShapeIdValue);
    if (!compound->IsSubShapeIDValid(subShapeId)) {
        return fallback;
    }

    JPH::SubShapeID remainder;
    uint32_t subShapeIndex = compound->GetSubShapeIndexFromID(subShapeId, remainder);
    uint32_t componentId = compound->GetCompoundUserData(subShapeIndex);
    if (componentId == 0) {
        return fallback;
    }

    auto colliders = go->GetComponents<Collider>();
    for (auto *col : colliders) {
        if (col && static_cast<uint32_t>(col->GetComponentID()) == componentId) {
            return col;
        }
    }

    return fallback;
}

void PhysicsWorld::RebindBodyCollider(uint32_t bodyId, Collider *collider)
{
    if (!m_initialized || bodyId == 0xFFFFFFFF || !collider) {
        return;
    }
    m_bodyToCollider[bodyId] = collider;
    m_physicsSystem->GetBodyInterface().SetUserData(JPH::BodyID(bodyId), reinterpret_cast<uint64_t>(collider));
}

void PhysicsWorld::EnsureSceneBodiesRegistered(Scene *scene)
{
    if (!m_initialized || !scene)
        return;

    bool anyRegistered = false;
    auto &store = PhysicsECSStore::Instance();

    // Flush deferred body creation queue first (from Collider::Awake).
    auto pendingBodies = store.ConsumePendingBodyCreations();
    for (auto handle : pendingBodies) {
        if (!store.IsValid(handle))
            continue;
        auto &data = store.GetCollider(handle);
        auto *col = data.owner;
        if (!col || !col->IsEnabled() || col->GetBodyId() != 0xFFFFFFFF)
            continue;
        col->RegisterBody();
        if (col->GetBodyId() != 0xFFFFFFFF) {
            col->AddToBroadphase();
            anyRegistered = true;
        }
    }

    // Walk all alive colliders — register any that still lack a body
    // (shouldn't normally happen after the pending queue flush, but
    // guards against edge cases).
    auto handles = store.GetAliveColliderHandles();
    for (auto handle : handles) {
        auto &data = store.GetCollider(handle);
        auto *col = data.owner;
        if (!col || !col->IsEnabled())
            continue;

        if (col->GetBodyId() == 0xFFFFFFFF) {
            col->RegisterBody();
            col->AddToBroadphase();
            anyRegistered = true;
        }

        col->SyncTransformToPhysics();
    }

    // Flush deferred broadphase additions, then rebuild the BVH tree
    // so raycasts can find newly added static bodies.
    auto pending = store.ConsumePendingBroadphaseAdds();
    for (auto &[bodyId, isStatic] : pending) {
        AddBodyToBroadphase(bodyId, isStatic);
        anyRegistered = true;
    }

    if (anyRegistered) {
        m_physicsSystem->OptimizeBroadPhase();
    }
}

void PhysicsWorld::OptimizeBroadPhase()
{
    if (m_initialized && m_physicsSystem) {
        m_physicsSystem->OptimizeBroadPhase();
    }
}

} // namespace infernux
