#pragma once

/**
 * @file PhysicsContactListener.h
 * @brief Jolt ContactListener implementation that collects collision/trigger
 *        events for dispatch to Unity-style component callbacks.
 *
 * Events are buffered during PhysicsWorld::Step() and flushed by
 * PhysicsWorld::DispatchContactEvents() immediately after.
 */

#include <Jolt/Jolt.h>
#include <Jolt/Physics/Body/Body.h>
#include <Jolt/Physics/Collision/ContactListener.h>

#include <glm/glm.hpp>

#include <array>
#include <cstdint>
#include <mutex>
#include <unordered_map>
#include <vector>

// Forward-declare Jolt BodyInterface for ResolveEvents parameter
namespace JPH
{
class BodyInterface;
}

namespace infernux
{

class Collider;
class GameObject;

// ============================================================================
// Collision — Unity-style collision data passed to callbacks
// ============================================================================

/**
 * @brief Data about a collision event (Unity: Collision).
 *
 * Passed to OnCollisionEnter / OnCollisionStay / OnCollisionExit.
 * For trigger events, only collider/gameObject are meaningful.
 */
struct CollisionInfo
{
    Collider *collider = nullptr;     ///< The other collider involved
    GameObject *gameObject = nullptr; ///< The other GameObject
    glm::vec3 contactPoint{0.0f};     ///< World-space contact point (first contact)
    glm::vec3 contactNormal{0.0f};    ///< Contact normal (points from other → this)
    glm::vec3 relativeVelocity{0.0f}; ///< Relative velocity between the bodies
};

// ============================================================================
// Internal event struct — used between listener and dispatch
// ============================================================================

enum class ContactEventType : uint8_t
{
    CollisionEnter,
    CollisionStay,
    CollisionExit,
    TriggerEnter,
    TriggerStay,
    TriggerExit,
};

enum ContactEventInterest : uint8_t
{
    CollisionEnterInterest = 1u << 0u,
    CollisionStayInterest = 1u << 1u,
    CollisionExitInterest = 1u << 2u,
    TriggerEnterInterest = 1u << 3u,
    TriggerStayInterest = 1u << 4u,
    TriggerExitInterest = 1u << 5u,
};

[[nodiscard]] constexpr uint8_t ContactEventInterestBit(ContactEventType type)
{
    switch (type) {
    case ContactEventType::CollisionEnter:
        return CollisionEnterInterest;
    case ContactEventType::CollisionStay:
        return CollisionStayInterest;
    case ContactEventType::CollisionExit:
        return CollisionExitInterest;
    case ContactEventType::TriggerEnter:
        return TriggerEnterInterest;
    case ContactEventType::TriggerStay:
        return TriggerStayInterest;
    case ContactEventType::TriggerExit:
        return TriggerExitInterest;
    }
    return 0;
}

struct ContactEvent
{
    ContactEventType type;
    uint32_t bodyIdA; ///< Jolt body index+sequence for body A
    uint32_t bodyIdB; ///< Jolt body index+sequence for body B
    uint32_t subShapeIdA = 0;
    uint32_t subShapeIdB = 0;
    glm::vec3 contactPoint{0.0f};
    glm::vec3 contactNormal{0.0f};
    glm::vec3 relativeVelocity{0.0f};
};

// ============================================================================
// InxContactListener — Jolt callback implementation
// ============================================================================

class InxContactListener : public JPH::ContactListener
{
  public:
    /// Publish only event classes requested by live components. Contact
    /// material resolution remains active regardless of this mask.
    void SetEventInterestMask(uint8_t mask);

    /// Reset per-step buffers. Call before PhysicsWorld::Step().
    void PreStep();

    /// Process raw contact events through pair tracking to produce final
    /// events that suppress sleep-related spurious Enter/Exit.
    /// Call after Step(), before DispatchContactEvents().
    void ResolveEvents(JPH::BodyInterface &bodyInterface);

    /// Full reset — clears all cached pair state (call on scene change / shutdown).
    void ClearAll();

    /// Remove all tracked contact pairs that involve @p bodyId.
    /// Call when a body's sensor flag changes at runtime so that the next
    /// step produces fresh Enter events instead of suppressing them as
    /// wake-from-sleep duplicates.
    void InvalidatePairsForBody(uint32_t bodyId);

    /// Get resolved events for dispatch.
    const std::vector<ContactEvent> &GetEvents() const
    {
        return m_events;
    }

    // -- JPH::ContactListener overrides --

    void OnContactAdded(const JPH::Body &inBody1, const JPH::Body &inBody2, const JPH::ContactManifold &inManifold,
                        JPH::ContactSettings &ioSettings) override;

    void OnContactPersisted(const JPH::Body &inBody1, const JPH::Body &inBody2, const JPH::ContactManifold &inManifold,
                            JPH::ContactSettings &ioSettings) override;

    void OnContactRemoved(const JPH::SubShapeIDPair &inSubShapePair) override;

  private:
    static constexpr size_t kEventShardCount = 16;

    void PushEvent(ContactEventType type, const JPH::Body &bodyA, const JPH::Body &bodyB,
                   const JPH::ContactManifold *manifold);
    void PushRawEvent(ContactEvent event);
    void MergeRawEvents();
    [[nodiscard]] bool Wants(ContactEventType type) const;

    struct ContactPairKey
    {
        uint32_t bodyA;
        uint32_t subShapeA;
        uint32_t bodyB;
        uint32_t subShapeB;

        bool operator==(const ContactPairKey &other) const
        {
            return bodyA == other.bodyA && subShapeA == other.subShapeA && bodyB == other.bodyB &&
                   subShapeB == other.subShapeB;
        }
    };

    struct ContactPairKeyHash
    {
        size_t operator()(const ContactPairKey &key) const;
    };

    static ContactPairKey MakePairKey(const ContactEvent &event);

    struct alignas(64) EventShard
    {
        std::mutex mutex;
        std::vector<ContactEvent> events;
    };

    std::array<EventShard, kEventShardCount> m_eventShards;
    std::vector<ContactEvent> m_rawEvents; ///< Main-thread aggregate built after Step
    std::vector<ContactEvent> m_events;    ///< Resolved events for dispatch

    /// Persistent pair tracking across physics steps.
    struct PairState
    {
        bool touchedThisStep = false;
        bool sleeping = false; ///< Exit was suppressed because a body went to sleep
        ContactEvent lastEvent{};
    };
    std::unordered_map<ContactPairKey, PairState, ContactPairKeyHash> m_contactPairs;
    uint8_t m_eventInterestMask = 0;

    // m_contactPairs and m_events are main-thread only. Jolt worker callbacks
    // write exclusively to m_eventShards.
};

} // namespace infernux
