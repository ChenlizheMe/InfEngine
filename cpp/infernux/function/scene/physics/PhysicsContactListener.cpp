#include "PhysicsContactListener.h"

#include "../Collider.h"
#include "../GameObject.h"

#include <Jolt/Physics/Body/Body.h>
#include <Jolt/Physics/Body/BodyInterface.h>
#include <Jolt/Physics/Collision/ContactListener.h>
#include <Jolt/Physics/Collision/Shape/CompoundShape.h>
#include <algorithm>
#include <glm/glm.hpp>
#include <thread>

namespace infernux
{

namespace
{
bool IsTriggerEvent(ContactEventType type)
{
    return type == ContactEventType::TriggerEnter || type == ContactEventType::TriggerStay ||
           type == ContactEventType::TriggerExit;
}

Collider *ResolveContactCollider(const JPH::Body &body, const JPH::SubShapeID &subShapeId)
{
    auto *fallback = reinterpret_cast<Collider *>(body.GetUserData());
    if (!fallback)
        return fallback;

    const JPH::Shape *shape = body.GetShape();
    if (!shape || shape->GetType() != JPH::EShapeType::Compound)
        return fallback;

    const auto *compound = static_cast<const JPH::CompoundShape *>(shape);
    if (!compound->IsSubShapeIDValid(subShapeId))
        return fallback;

    JPH::SubShapeID remainder;
    uint32_t index = compound->GetSubShapeIndexFromID(subShapeId, remainder);
    uint32_t componentId = compound->GetCompoundUserData(index);
    auto *gameObject = fallback->GetGameObject();
    if (!gameObject || componentId == 0)
        return fallback;

    for (auto *collider : gameObject->GetComponents<Collider>()) {
        if (collider && static_cast<uint32_t>(collider->GetComponentID()) == componentId)
            return collider;
    }
    return fallback;
}

bool IsTriggerContact(const JPH::Body &body1, const JPH::Body &body2, const JPH::ContactManifold &manifold)
{
    auto *collider1 = ResolveContactCollider(body1, manifold.mSubShapeID1);
    auto *collider2 = ResolveContactCollider(body2, manifold.mSubShapeID2);
    return body1.IsSensor() || body2.IsSensor() || (collider1 && collider1->IsTrigger()) ||
           (collider2 && collider2->IsTrigger());
}

float CombineMaterialValue(float value1, PhysicsMaterialCombine mode1, float value2, PhysicsMaterialCombine mode2)
{
    const auto mode = static_cast<PhysicsMaterialCombine>(std::max(static_cast<int>(mode1), static_cast<int>(mode2)));
    switch (mode) {
    case PhysicsMaterialCombine::Average:
        return 0.5f * (value1 + value2);
    case PhysicsMaterialCombine::Minimum:
        return std::min(value1, value2);
    case PhysicsMaterialCombine::Multiply:
        return value1 * value2;
    case PhysicsMaterialCombine::Maximum:
        return std::max(value1, value2);
    }
    return 0.0f;
}

void ApplyContactMaterial(const JPH::Body &body1, const JPH::Body &body2, const JPH::ContactManifold &manifold,
                          JPH::ContactSettings &settings)
{
    const Collider *collider1 = ResolveContactCollider(body1, manifold.mSubShapeID1);
    const Collider *collider2 = ResolveContactCollider(body2, manifold.mSubShapeID2);
    if (!collider1 || !collider2)
        return;

    settings.mCombinedFriction = CombineMaterialValue(collider1->GetFriction(), collider1->GetFrictionCombine(),
                                                      collider2->GetFriction(), collider2->GetFrictionCombine());
    settings.mCombinedRestitution = CombineMaterialValue(collider1->GetBounciness(), collider1->GetBounceCombine(),
                                                         collider2->GetBounciness(), collider2->GetBounceCombine());
}
} // namespace

size_t InxContactListener::ContactPairKeyHash::operator()(const ContactPairKey &key) const
{
    size_t hash = std::hash<uint32_t>{}(key.bodyA);
    auto combine = [&hash](uint32_t value) {
        hash ^= std::hash<uint32_t>{}(value) + 0x9e3779b9U + (hash << 6U) + (hash >> 2U);
    };
    combine(key.subShapeA);
    combine(key.bodyB);
    combine(key.subShapeB);
    return hash;
}

InxContactListener::ContactPairKey InxContactListener::MakePairKey(const ContactEvent &event)
{
    const bool swapSides =
        event.bodyIdA > event.bodyIdB || (event.bodyIdA == event.bodyIdB && event.subShapeIdA > event.subShapeIdB);
    if (swapSides)
        return {event.bodyIdB, event.subShapeIdB, event.bodyIdA, event.subShapeIdA};
    return {event.bodyIdA, event.subShapeIdA, event.bodyIdB, event.subShapeIdB};
}

void InxContactListener::SetEventInterestMask(uint8_t mask)
{
    constexpr uint8_t pairTrackingMask =
        CollisionEnterInterest | CollisionExitInterest | TriggerEnterInterest | TriggerExitInterest;
    if ((m_eventInterestMask & pairTrackingMask) != (mask & pairTrackingMask))
        m_contactPairs.clear();
    m_eventInterestMask = mask;
}

bool InxContactListener::Wants(ContactEventType type) const
{
    return (m_eventInterestMask & ContactEventInterestBit(type)) != 0;
}

void InxContactListener::PreStep()
{
    m_rawEvents.clear();
    m_events.clear();
    for (auto &shard : m_eventShards) {
        std::lock_guard<std::mutex> lock(shard.mutex);
        shard.events.clear();
    }
    for (auto &[key, state] : m_contactPairs)
        state.touchedThisStep = false;
}

void InxContactListener::ClearAll()
{
    m_rawEvents.clear();
    m_events.clear();
    for (auto &shard : m_eventShards) {
        std::lock_guard<std::mutex> lock(shard.mutex);
        shard.events.clear();
    }
    m_contactPairs.clear();
}

void InxContactListener::InvalidatePairsForBody(uint32_t bodyId)
{
    auto it = m_contactPairs.begin();
    while (it != m_contactPairs.end()) {
        if (it->first.bodyA == bodyId || it->first.bodyB == bodyId)
            it = m_contactPairs.erase(it);
        else
            ++it;
    }
}

void InxContactListener::PushEvent(ContactEventType type, const JPH::Body &bodyA, const JPH::Body &bodyB,
                                   const JPH::ContactManifold *manifold)
{
    ContactEvent evt;
    evt.type = type;
    evt.bodyIdA = bodyA.GetID().GetIndexAndSequenceNumber();
    evt.bodyIdB = bodyB.GetID().GetIndexAndSequenceNumber();

    if (manifold) {
        evt.subShapeIdA = manifold->mSubShapeID1.GetValue();
        evt.subShapeIdB = manifold->mSubShapeID2.GetValue();
        // Use first contact point if available
        if (manifold->mRelativeContactPointsOn1.size() > 0) {
            JPH::Vec3 wp = manifold->GetWorldSpaceContactPointOn1(0);
            evt.contactPoint = glm::vec3(wp.GetX(), wp.GetY(), wp.GetZ());
        }
        JPH::Vec3 n = manifold->mWorldSpaceNormal;
        evt.contactNormal = glm::vec3(n.GetX(), n.GetY(), n.GetZ());
    }

    // Relative velocity
    JPH::Vec3 velA = bodyA.GetLinearVelocity();
    JPH::Vec3 velB = bodyB.GetLinearVelocity();
    JPH::Vec3 relVel = velA - velB;
    evt.relativeVelocity = glm::vec3(relVel.GetX(), relVel.GetY(), relVel.GetZ());

    PushRawEvent(std::move(evt));
}

void InxContactListener::PushRawEvent(ContactEvent event)
{
    static thread_local const size_t shardIndex =
        std::hash<std::thread::id>{}(std::this_thread::get_id()) % kEventShardCount;
    auto &shard = m_eventShards[shardIndex];
    std::lock_guard<std::mutex> lock(shard.mutex);
    shard.events.push_back(std::move(event));
}

void InxContactListener::MergeRawEvents()
{
    size_t total = 0;
    for (auto &shard : m_eventShards) {
        std::lock_guard<std::mutex> lock(shard.mutex);
        total += shard.events.size();
    }
    m_rawEvents.clear();
    m_rawEvents.reserve(total);
    for (auto &shard : m_eventShards) {
        std::lock_guard<std::mutex> lock(shard.mutex);
        m_rawEvents.insert(m_rawEvents.end(), shard.events.begin(), shard.events.end());
    }
}

void InxContactListener::OnContactAdded(const JPH::Body &inBody1, const JPH::Body &inBody2,
                                        const JPH::ContactManifold &inManifold, JPH::ContactSettings &ioSettings)
{
    bool isSensor = IsTriggerContact(inBody1, inBody2, inManifold);
    ioSettings.mIsSensor = isSensor;
    ApplyContactMaterial(inBody1, inBody2, inManifold, ioSettings);
    ContactEventType type = isSensor ? ContactEventType::TriggerEnter : ContactEventType::CollisionEnter;
    const ContactEventType exitType = isSensor ? ContactEventType::TriggerExit : ContactEventType::CollisionExit;
    if (Wants(type) || Wants(exitType))
        PushEvent(type, inBody1, inBody2, &inManifold);
}

void InxContactListener::OnContactPersisted(const JPH::Body &inBody1, const JPH::Body &inBody2,
                                            const JPH::ContactManifold &inManifold, JPH::ContactSettings &ioSettings)
{
    bool isSensor = IsTriggerContact(inBody1, inBody2, inManifold);
    ioSettings.mIsSensor = isSensor;
    ApplyContactMaterial(inBody1, inBody2, inManifold, ioSettings);
    ContactEventType type = isSensor ? ContactEventType::TriggerStay : ContactEventType::CollisionStay;
    if (Wants(type))
        PushEvent(type, inBody1, inBody2, &inManifold);
}

void InxContactListener::OnContactRemoved(const JPH::SubShapeIDPair &inSubShapePair)
{
    constexpr uint8_t removalInterest =
        CollisionEnterInterest | CollisionExitInterest | TriggerEnterInterest | TriggerExitInterest;
    if ((m_eventInterestMask & removalInterest) == 0)
        return;

    // On removal we don't have full Body references — extract IDs from the pair.
    ContactEvent evt;
    // SubShapeIDPair stores BodyIDs for both bodies.
    evt.bodyIdA = inSubShapePair.GetBody1ID().GetIndexAndSequenceNumber();
    evt.bodyIdB = inSubShapePair.GetBody2ID().GetIndexAndSequenceNumber();
    evt.subShapeIdA = inSubShapePair.GetSubShapeID1().GetValue();
    evt.subShapeIdB = inSubShapePair.GetSubShapeID2().GetValue();
    // We cannot tell if it was a sensor pair from the SubShapeIDPair alone.
    // Store as CollisionExit; DispatchContactEvents will check isTrigger on
    // the Collider* to re-classify.
    evt.type = ContactEventType::CollisionExit;

    PushRawEvent(std::move(evt));
}

// ============================================================================
// ResolveEvents — post-step pair tracking (suppresses sleep-related spurious
// Enter/Exit to match Unity OnCollision semantics)
// ============================================================================

void InxContactListener::ResolveEvents(JPH::BodyInterface &bodyInterface)
{
    MergeRawEvents();
    for (const auto &raw : m_rawEvents) {
        ContactPairKey key = MakePairKey(raw);

        switch (raw.type) {
        case ContactEventType::CollisionEnter:
        case ContactEventType::TriggerEnter: {
            auto it = m_contactPairs.find(key);
            if (it != m_contactPairs.end() && it->second.sleeping) {
                // Body woke up — pair already tracked, suppress duplicate Enter.
                it->second.sleeping = false;
                it->second.touchedThisStep = true;
                it->second.lastEvent = raw;
            } else {
                // Genuine new contact.
                m_contactPairs[key] = {true, false, raw};
                if (Wants(raw.type))
                    m_events.push_back(raw);
            }
            break;
        }

        case ContactEventType::CollisionStay:
        case ContactEventType::TriggerStay: {
            auto it = m_contactPairs.find(key);
            if (it != m_contactPairs.end()) {
                it->second.touchedThisStep = true;
                it->second.lastEvent = raw;
            }
            if (Wants(raw.type))
                m_events.push_back(raw);
            break;
        }

        case ContactEventType::CollisionExit:
            // Note: OnContactRemoved always tags as CollisionExit because Body
            // refs aren't available. DispatchContactEvents re-classifies to
            // TriggerExit when either Collider is a trigger.
            {
                JPH::BodyID joltA(raw.bodyIdA);
                JPH::BodyID joltB(raw.bodyIdB);

                // A "sleeping dynamic" body is still in the broadphase but not
                // active, and is NOT a static body (statics are always inactive).
                bool aAdded = bodyInterface.IsAdded(joltA);
                bool bAdded = bodyInterface.IsAdded(joltB);

                bool aSleeping = aAdded && !bodyInterface.IsActive(joltA) &&
                                 bodyInterface.GetMotionType(joltA) != JPH::EMotionType::Static;
                bool bSleeping = bAdded && !bodyInterface.IsActive(joltB) &&
                                 bodyInterface.GetMotionType(joltB) != JPH::EMotionType::Static;

                if (aSleeping || bSleeping) {
                    // Sleep-related removal — suppress Exit, mark pair as sleeping.
                    auto it = m_contactPairs.find(key);
                    if (it != m_contactPairs.end())
                        it->second.sleeping = true;
                } else {
                    // Real separation or body removed from broadphase.
                    ContactEvent exitEvent = raw;
                    auto tracked = m_contactPairs.find(key);
                    if (tracked != m_contactPairs.end() && IsTriggerEvent(tracked->second.lastEvent.type))
                        exitEvent.type = ContactEventType::TriggerExit;
                    m_contactPairs.erase(key);
                    if (Wants(exitEvent.type))
                        m_events.push_back(exitEvent);
                }
                break;
            }

        default:
            m_events.push_back(raw);
            break;
        }
    }

    // ========================================================================
    // Sweep sleeping pairs: if the previously-sleeping body is now active but
    // we received no Added/Persisted this step, the bodies separated after
    // waking. Emit deferred Exit for these pairs.
    // ========================================================================
    std::vector<ContactPairKey> expiredPairs;
    for (auto &[key, state] : m_contactPairs) {
        if (!state.sleeping || state.touchedThisStep)
            continue;

        uint32_t idA = key.bodyA;
        uint32_t idB = key.bodyB;

        JPH::BodyID joltA(idA);
        JPH::BodyID joltB(idB);

        // Check if a previously-sleeping dynamic body woke up or was removed.
        bool aWokeUp = bodyInterface.IsAdded(joltA) && bodyInterface.IsActive(joltA);
        bool bWokeUp = bodyInterface.IsAdded(joltB) && bodyInterface.IsActive(joltB);
        bool aRemoved = !bodyInterface.IsAdded(joltA);
        bool bRemoved = !bodyInterface.IsAdded(joltB);

        if (aWokeUp || bWokeUp || aRemoved || bRemoved) {
            ContactEvent exitEvt = state.lastEvent;
            exitEvt.type =
                IsTriggerEvent(state.lastEvent.type) ? ContactEventType::TriggerExit : ContactEventType::CollisionExit;
            if (Wants(exitEvt.type))
                m_events.push_back(exitEvt);
            expiredPairs.push_back(key);
        }
    }

    for (const ContactPairKey &key : expiredPairs)
        m_contactPairs.erase(key);
}

} // namespace infernux
