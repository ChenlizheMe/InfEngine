/**
 * @file BindingPhysics.cpp
 * @brief Python bindings for physics collider components, PhysicsWorld, and raycast API.
 *
 * Registers BoxCollider, SphereCollider, CapsuleCollider, CylinderCollider,
 * RaycastHit, and
 * a "Physics" static
 * class with Raycast/RaycastAll methods.
 */

// Jolt types are no longer exposed in collider headers — no Jolt include needed here
// Except for gravity API which accesses PhysicsSystem directly.
#include <Jolt/Jolt.h>
#include <Jolt/Physics/PhysicsSystem.h>

#include "function/scene/BoxCollider.h"
#include "function/scene/CapsuleCollider.h"
#include "function/scene/Collider.h"
#include "function/scene/Component.h"
#include "function/scene/CylinderCollider.h"
#include "function/scene/GameObject.h"
#include "function/scene/MeshCollider.h"
#include "function/scene/Rigidbody.h"
#include "function/scene/SceneManager.h"
#include "function/scene/SphereCollider.h"
#include "function/scene/TagLayerManager.h"
#include "function/scene/physics/PhysicsContactListener.h"
#include "function/scene/physics/PhysicsWorld.h"
#include <core/config/EngineConfig.h>
#include <glm/glm.hpp>
#include <glm/gtc/quaternion.hpp>

#include <cmath>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <stdexcept>

namespace py = pybind11;

namespace infernux
{

// Forward-declare the registry (defined in BindingScene.cpp)
class ComponentBindingRegistry;

namespace
{
void RequireFinite(const glm::vec3 &value, const char *name)
{
    if (!std::isfinite(value.x) || !std::isfinite(value.y) || !std::isfinite(value.z))
        throw std::invalid_argument(std::string(name) + " must contain finite values");
}

void RequireDirectionAndDistance(const glm::vec3 &direction, float maxDistance)
{
    RequireFinite(direction, "direction");
    if (glm::dot(direction, direction) <= 1e-12f)
        throw std::invalid_argument("direction must be non-zero");
    if (!std::isfinite(maxDistance) || maxDistance <= 0.0f)
        throw std::invalid_argument("max_distance must be finite and greater than zero");
}

void RequirePositive(float value, const char *name)
{
    if (!std::isfinite(value) || value <= 0.0f)
        throw std::invalid_argument(std::string(name) + " must be finite and greater than zero");
}

void RequirePositiveExtents(const glm::vec3 &value)
{
    RequireFinite(value, "half_extents");
    if (value.x <= 0.0f || value.y <= 0.0f || value.z <= 0.0f)
        throw std::invalid_argument("half_extents must be greater than zero on every axis");
}

void RequireOrientation(const glm::quat &orientation)
{
    if (!std::isfinite(orientation.w) || !std::isfinite(orientation.x) || !std::isfinite(orientation.y) ||
        !std::isfinite(orientation.z) || glm::dot(orientation, orientation) <= 1e-12f)
        throw std::invalid_argument("orientation must be a finite, non-zero quaternion");
}

py::list BorrowedColliderList(const std::vector<Collider *> &colliders)
{
    py::list result;
    for (Collider *collider : colliders) {
        if (collider)
            result.append(py::cast(collider, py::return_value_policy::reference));
    }
    return result;
}
} // namespace

void RegisterPhysicsBindings(py::module_ &m)
{
    using namespace pybind11::literals;

    // ====================================================================
    // CollisionInfo struct (Unity: Collision)
    // ====================================================================
    py::class_<CollisionInfo>(m, "CollisionInfo")
        .def(py::init<>())
        .def_property_readonly(
            "collider", [](const CollisionInfo &c) { return c.collider; }, py::return_value_policy::reference,
            "The other Collider involved in the collision")
        .def_property_readonly(
            "game_object", [](const CollisionInfo &c) { return c.gameObject; }, py::return_value_policy::reference,
            "The other GameObject involved in the collision")
        .def_property_readonly(
            "contact_point", [](const CollisionInfo &c) { return c.contactPoint; }, "World-space contact point")
        .def_property_readonly(
            "contact_normal", [](const CollisionInfo &c) { return c.contactNormal; },
            "Contact normal (points from other towards this)")
        .def_property_readonly(
            "relative_velocity", [](const CollisionInfo &c) { return c.relativeVelocity; },
            "Relative velocity between the two bodies")
        .def("__repr__", [](const CollisionInfo &c) {
            std::string goName = c.gameObject ? c.gameObject->GetName() : "null";
            return "<CollisionInfo other='" + goName + "'>";
        });

    // ====================================================================
    // RaycastHit struct
    // ====================================================================
    py::class_<RaycastHit>(m, "RaycastHit")
        .def(py::init<>())
        .def_property_readonly(
            "point", [](const RaycastHit &h) { return h.point; }, "World-space hit point")
        .def_property_readonly(
            "normal", [](const RaycastHit &h) { return h.normal; }, "Surface normal at hit point")
        .def_property_readonly(
            "distance", [](const RaycastHit &h) { return h.distance; }, "Distance from ray origin to hit")
        .def_property_readonly(
            "game_object",
            [](const RaycastHit &h) -> GameObject * {
                if (h.gameObject)
                    return h.gameObject;
                if (h.collider)
                    return h.collider->GetGameObject();
                if (h.bodyId == 0xFFFFFFFF)
                    return nullptr;
                if (Collider *col = PhysicsWorld::Instance().FindColliderByBodyId(h.bodyId))
                    return col->GetGameObject();
                return nullptr;
            },
            py::return_value_policy::reference, "Hit GameObject")
        .def_property_readonly(
            "collider",
            [](const RaycastHit &h) -> Collider * {
                if (h.collider)
                    return h.collider;
                if (h.bodyId == 0xFFFFFFFF)
                    return nullptr;
                return PhysicsWorld::Instance().FindColliderByBodyId(h.bodyId);
            },
            py::return_value_policy::reference, "Hit Collider component")
        .def("__repr__", [](const RaycastHit &h) { return "<RaycastHit dist=" + std::to_string(h.distance) + ">"; });

    // ====================================================================
    py::enum_<PhysicsMaterialCombine>(m, "PhysicsMaterialCombine")
        .value("Average", PhysicsMaterialCombine::Average)
        .value("Minimum", PhysicsMaterialCombine::Minimum)
        .value("Multiply", PhysicsMaterialCombine::Multiply)
        .value("Maximum", PhysicsMaterialCombine::Maximum);

    // Collider base (abstract — not directly constructible)
    // ====================================================================
    py::class_<Collider, Component>(m, "Collider")
        .def_property("is_trigger", &Collider::IsTrigger, &Collider::SetIsTrigger, "Is this collider a trigger volume?")
        .def_property(
            "center", [](Collider *c) { return c->GetCenter(); },
            [](Collider *c, const glm::vec3 &v) { c->SetCenter(v); }, "Center offset in local space")
        .def_property("physic_material", &Collider::GetPhysicMaterial, &Collider::SetPhysicMaterial,
                      "Shared PhysicMaterial; None uses engine defaults")
        .def_property("physic_material_guid", &Collider::GetPhysicMaterialGuid, &Collider::SetPhysicMaterialGuid,
                      "GUID of the persistent PhysicMaterial asset")
        .def("serialize", &Collider::Serialize)
        .def("deserialize", &Collider::Deserialize, "json_str"_a);

    // ====================================================================
    // BoxCollider
    // ====================================================================
    py::class_<BoxCollider, Collider>(m, "BoxCollider")
        .def(py::init<>())
        .def_property(
            "size", [](BoxCollider *c) { return c->GetSize(); },
            [](BoxCollider *c, const glm::vec3 &v) { c->SetSize(v); }, "Size of the box collider (full extents)")
        .def("serialize", &BoxCollider::Serialize)
        .def("deserialize", &BoxCollider::Deserialize, "json_str"_a);

    // ====================================================================
    // SphereCollider
    // ====================================================================
    py::class_<SphereCollider, Collider>(m, "SphereCollider")
        .def(py::init<>())
        .def_property("radius", &SphereCollider::GetRadius, &SphereCollider::SetRadius, "Radius of the sphere collider")
        .def("serialize", &SphereCollider::Serialize)
        .def("deserialize", &SphereCollider::Deserialize, "json_str"_a);

    // ====================================================================
    // CapsuleCollider
    // ====================================================================
    py::class_<CapsuleCollider, Collider>(m, "CapsuleCollider")
        .def(py::init<>())
        .def_property("radius", &CapsuleCollider::GetRadius, &CapsuleCollider::SetRadius,
                      "Radius of the capsule collider")
        .def_property("height", &CapsuleCollider::GetHeight, &CapsuleCollider::SetHeight,
                      "Total height of the capsule (including caps)")
        .def_property("direction", &CapsuleCollider::GetDirection, &CapsuleCollider::SetDirection,
                      "Direction axis: 0=X, 1=Y, 2=Z")
        .def("serialize", &CapsuleCollider::Serialize)
        .def("deserialize", &CapsuleCollider::Deserialize, "json_str"_a);

    // ====================================================================
    // CylinderCollider
    // ====================================================================
    py::class_<CylinderCollider, Collider>(m, "CylinderCollider")
        .def(py::init<>())
        .def_property("radius", &CylinderCollider::GetRadius, &CylinderCollider::SetRadius,
                      "Radius of the cylinder collider")
        .def_property("height", &CylinderCollider::GetHeight, &CylinderCollider::SetHeight,
                      "Total height of the cylinder")
        .def_property("direction", &CylinderCollider::GetDirection, &CylinderCollider::SetDirection,
                      "Direction axis: 0=X, 1=Y, 2=Z")
        .def("serialize", &CylinderCollider::Serialize)
        .def("deserialize", &CylinderCollider::Deserialize, "json_str"_a);

    // ====================================================================
    // MeshCollider
    // ====================================================================
    py::class_<MeshCollider, Collider>(m, "MeshCollider")
        .def(py::init<>())
        .def_property("convex", &MeshCollider::IsConvex, &MeshCollider::SetConvex,
                      "Use convex hull collision. Dynamic rigidbodies set this property to true.")
        .def_property_readonly("shape_error", &MeshCollider::GetShapeError,
                               "Last mesh cooking error; empty after successful shape creation")
        .def_property_readonly("is_cooking", &MeshCollider::IsCooking,
                               "Whether immutable collision geometry is currently cooking on a worker")
        .def_static("clear_cooking_cache", &MeshCollider::ClearCookingCache,
                    "Clear cached CPU mesh-cooking payloads and reset hit/miss counters")
        .def_static(
            "get_cooking_cache_stats",
            []() {
                const auto [hits, misses] = MeshCollider::GetCookingCacheStats();
                py::dict stats;
                stats["hits"] = hits;
                stats["misses"] = misses;
                stats["pending"] = MeshCollider::GetPendingCookingCount();
                stats["async_submissions"] = MeshCollider::GetAsyncCookingSubmissionCount();
                return stats;
            },
            "Return CPU mesh-cooking cache and worker counters")
        .def(
            "get_convex_hull_positions",
            [](const MeshCollider &mc) -> py::list {
                py::list result;
                for (const auto &v : mc.GetConvexHullPositions()) {
                    result.append(py::make_tuple(v.x, v.y, v.z));
                }
                return result;
            },
            "Convex hull vertex positions in local space")
        .def(
            "get_convex_hull_edges",
            [](const MeshCollider &mc) -> py::list {
                py::list result;
                for (auto idx : mc.GetConvexHullEdges()) {
                    result.append(idx);
                }
                return result;
            },
            "Convex hull edge index pairs [a0,b0, a1,b1, ...]")
        .def("serialize", &MeshCollider::Serialize)
        .def("deserialize", &MeshCollider::Deserialize, "json_str"_a);

    // ====================================================================
    // ForceMode enum (Unity: ForceMode)
    // ====================================================================
    py::enum_<ForceMode>(m, "ForceMode")
        .value("Force", ForceMode::Force, "Continuous force (mass-dependent)")
        .value("Acceleration", ForceMode::Acceleration, "Continuous acceleration (mass-independent)")
        .value("Impulse", ForceMode::Impulse, "Instant force impulse (mass-dependent)")
        .value("VelocityChange", ForceMode::VelocityChange, "Instant velocity change (mass-independent)")
        .export_values();

    // ====================================================================
    // RigidbodyConstraints enum (Unity: RigidbodyConstraints)
    // ====================================================================
    py::enum_<RigidbodyConstraints>(m, "RigidbodyConstraints")
        .value("None", RigidbodyConstraints::None, "No constraints")
        .value("FreezePositionX", RigidbodyConstraints::FreezePositionX)
        .value("FreezePositionY", RigidbodyConstraints::FreezePositionY)
        .value("FreezePositionZ", RigidbodyConstraints::FreezePositionZ)
        .value("FreezeRotationX", RigidbodyConstraints::FreezeRotationX)
        .value("FreezeRotationY", RigidbodyConstraints::FreezeRotationY)
        .value("FreezeRotationZ", RigidbodyConstraints::FreezeRotationZ)
        .value("FreezePosition", RigidbodyConstraints::FreezePosition, "Freeze all position axes")
        .value("FreezeRotation", RigidbodyConstraints::FreezeRotation, "Freeze all rotation axes")
        .value("FreezeAll", RigidbodyConstraints::FreezeAll, "Freeze all position and rotation axes")
        .export_values();

    // ====================================================================
    // CollisionDetectionMode enum (Unity: CollisionDetectionMode)
    // ====================================================================
    py::enum_<CollisionDetectionMode>(m, "CollisionDetectionMode")
        .value("Discrete", CollisionDetectionMode::Discrete)
        .value("Continuous", CollisionDetectionMode::Continuous)
        .value("ContinuousDynamic", CollisionDetectionMode::ContinuousDynamic)
        .export_values();

    py::enum_<RigidbodyInterpolation>(m, "RigidbodyInterpolation")
        .value("None", RigidbodyInterpolation::None)
        .value("Interpolate", RigidbodyInterpolation::Interpolate)
        .export_values();

    // ====================================================================
    // Rigidbody component (Unity: Rigidbody)
    // ====================================================================
    py::class_<Rigidbody, Component>(m, "Rigidbody")
        .def(py::init<>())
        // ---- Serialized properties ----
        .def_property("mass", &Rigidbody::GetMass, &Rigidbody::SetMass, "Mass in kilograms (default 1)")
        .def_property("drag", &Rigidbody::GetDrag, &Rigidbody::SetDrag, "Linear drag (default 0)")
        .def_property("angular_drag", &Rigidbody::GetAngularDrag, &Rigidbody::SetAngularDrag,
                      "Angular drag (default 0.05)")
        .def_property("use_gravity", &Rigidbody::GetUseGravity, &Rigidbody::SetUseGravity,
                      "Use gravity? (default true)")
        .def_property("is_kinematic", &Rigidbody::IsKinematic, &Rigidbody::SetIsKinematic,
                      "Is kinematic? (default false)")
        .def_property("constraints", &Rigidbody::GetConstraints, &Rigidbody::SetConstraints,
                      "Constraints bitmask (RigidbodyConstraints)")
        .def_property("freeze_rotation", &Rigidbody::GetFreezeRotation, &Rigidbody::SetFreezeRotation,
                      "Shortcut to freeze all rotation axes")
        .def_property(
            "freeze_position_x", [](Rigidbody *rb) { return (static_cast<int>(rb->GetConstraints()) & 2) != 0; },
            [](Rigidbody *rb, bool v) {
                int c = static_cast<int>(rb->GetConstraints());
                rb->SetConstraints(v ? (c | 2) : (c & ~2));
            },
            "Freeze position X axis")
        .def_property(
            "freeze_position_y", [](Rigidbody *rb) { return (static_cast<int>(rb->GetConstraints()) & 4) != 0; },
            [](Rigidbody *rb, bool v) {
                int c = static_cast<int>(rb->GetConstraints());
                rb->SetConstraints(v ? (c | 4) : (c & ~4));
            },
            "Freeze position Y axis")
        .def_property(
            "freeze_position_z", [](Rigidbody *rb) { return (static_cast<int>(rb->GetConstraints()) & 8) != 0; },
            [](Rigidbody *rb, bool v) {
                int c = static_cast<int>(rb->GetConstraints());
                rb->SetConstraints(v ? (c | 8) : (c & ~8));
            },
            "Freeze position Z axis")
        .def_property(
            "freeze_rotation_x", [](Rigidbody *rb) { return (static_cast<int>(rb->GetConstraints()) & 16) != 0; },
            [](Rigidbody *rb, bool v) {
                int c = static_cast<int>(rb->GetConstraints());
                rb->SetConstraints(v ? (c | 16) : (c & ~16));
            },
            "Freeze rotation X axis")
        .def_property(
            "freeze_rotation_y", [](Rigidbody *rb) { return (static_cast<int>(rb->GetConstraints()) & 32) != 0; },
            [](Rigidbody *rb, bool v) {
                int c = static_cast<int>(rb->GetConstraints());
                rb->SetConstraints(v ? (c | 32) : (c & ~32));
            },
            "Freeze rotation Y axis")
        .def_property(
            "freeze_rotation_z", [](Rigidbody *rb) { return (static_cast<int>(rb->GetConstraints()) & 64) != 0; },
            [](Rigidbody *rb, bool v) {
                int c = static_cast<int>(rb->GetConstraints());
                rb->SetConstraints(v ? (c | 64) : (c & ~64));
            },
            "Freeze rotation Z axis")
        .def_property("collision_detection_mode", &Rigidbody::GetCollisionDetectionMode,
                      &Rigidbody::SetCollisionDetectionMode,
                      "Collision detection mode. Dynamic Continuous uses Jolt LinearCast sweep CCD.")
        .def_property("interpolation", &Rigidbody::GetInterpolation, &Rigidbody::SetInterpolation,
                      "Presentation interpolation mode (0=None, 1=Interpolate)")
        .def_property("max_angular_velocity", &Rigidbody::GetMaxAngularVelocity, &Rigidbody::SetMaxAngularVelocity,
                      "Maximum angular velocity in rad/s (default 7)")
        .def_property("max_linear_velocity", &Rigidbody::GetMaxLinearVelocity, &Rigidbody::SetMaxLinearVelocity,
                      "Maximum linear velocity in m/s")
        // ---- Velocity ----
        .def_property(
            "velocity", [](Rigidbody *rb) { return rb->GetVelocity(); },
            [](Rigidbody *rb, const glm::vec3 &v) { rb->SetVelocity(v); }, "Linear velocity in world space")
        .def_property(
            "angular_velocity", [](Rigidbody *rb) { return rb->GetAngularVelocity(); },
            [](Rigidbody *rb, const glm::vec3 &v) { rb->SetAngularVelocity(v); }, "Angular velocity in world space")
        // ---- Read-only world info ----
        .def_property_readonly(
            "world_center_of_mass", [](Rigidbody *rb) { return rb->GetWorldCenterOfMass(); },
            "World-space center of mass (read-only)")
        .def_property("position", &Rigidbody::GetPosition, &Rigidbody::SetPosition,
                      "World-space position. Assignment teleports while preserving velocity.")
        .def_property("rotation", &Rigidbody::GetRotation, &Rigidbody::SetRotation,
                      "World-space rotation. Assignment teleports while preserving velocity.")
        // ---- Force / Torque ----
        .def(
            "add_force", [](Rigidbody *rb, const glm::vec3 &f, ForceMode mode) { rb->AddForce(f, mode); }, "force"_a,
            "mode"_a = ForceMode::Force, "Add a force to the rigidbody")
        .def(
            "add_torque", [](Rigidbody *rb, const glm::vec3 &t, ForceMode mode) { rb->AddTorque(t, mode); }, "torque"_a,
            "mode"_a = ForceMode::Force, "Add a torque to the rigidbody")
        .def(
            "add_force_at_position",
            [](Rigidbody *rb, const glm::vec3 &f, const glm::vec3 &p, ForceMode mode) {
                rb->AddForceAtPosition(f, p, mode);
            },
            "force"_a, "position"_a, "mode"_a = ForceMode::Force, "Add a force at a world-space position")
        // ---- Kinematic movement ----
        .def(
            "move_position", [](Rigidbody *rb, const glm::vec3 &p) { rb->MovePosition(p); }, "position"_a,
            "Move kinematic body to target position")
        .def(
            "move_rotation", [](Rigidbody *rb, const glm::quat &q) { rb->MoveRotation(q); }, "rotation"_a,
            "Rotate kinematic body to target rotation")
        // ---- Sleep ----
        .def("is_sleeping", &Rigidbody::IsSleeping, "Is the rigidbody sleeping?")
        .def("wake_up", &Rigidbody::WakeUp, "Wake the rigidbody up")
        .def("sleep", &Rigidbody::Sleep, "Put the rigidbody to sleep")
        .def("serialize", &Rigidbody::Serialize)
        .def("deserialize", &Rigidbody::Deserialize, "json_str"_a);

    // ====================================================================
    // Physics static class (Unity: Physics.Raycast)
    // ====================================================================
    py::class_<PhysicsWorld, std::unique_ptr<PhysicsWorld, py::nodelete>>(m, "Physics")
        .def_property_readonly_static("body_count", [](py::object) { return PhysicsWorld::Instance().GetBodyCount(); })
        .def_static(
            "raycast",
            [](const glm::vec3 &origin, const glm::vec3 &direction, float maxDistance, uint32_t layerMask,
               bool queryTriggers) -> py::object {
                RequireFinite(origin, "origin");
                RequireDirectionAndDistance(direction, maxDistance);
                RaycastHit hit;
                if (PhysicsWorld::Instance().Raycast(origin, direction, maxDistance, hit, layerMask, queryTriggers)) {
                    return py::cast(hit);
                }
                return py::none();
            },
            "origin"_a, "direction"_a, "max_distance"_a = 1000.0f,
            "layer_mask"_a = EngineConfig::Get().defaultQueryLayerMask, "query_triggers"_a = true,
            "Cast a ray. Returns RaycastHit or None.")
        .def_static(
            "raycast_all",
            [](const glm::vec3 &origin, const glm::vec3 &direction, float maxDistance, uint32_t layerMask,
               bool queryTriggers) {
                RequireFinite(origin, "origin");
                RequireDirectionAndDistance(direction, maxDistance);
                return PhysicsWorld::Instance().RaycastAll(origin, direction, maxDistance, layerMask, queryTriggers);
            },
            "origin"_a, "direction"_a, "max_distance"_a = 1000.0f,
            "layer_mask"_a = EngineConfig::Get().defaultQueryLayerMask, "query_triggers"_a = true,
            "Cast a ray and return all hits.")
        // ---- Overlap queries ----
        .def_static(
            "overlap_sphere",
            [](const glm::vec3 &center, float radius, uint32_t layerMask, bool queryTriggers) {
                RequireFinite(center, "center");
                RequirePositive(radius, "radius");
                return BorrowedColliderList(
                    PhysicsWorld::Instance().OverlapSphere(center, radius, layerMask, queryTriggers));
            },
            "center"_a, "radius"_a, "layer_mask"_a = EngineConfig::Get().defaultQueryLayerMask,
            "query_triggers"_a = true, "Find all colliders within a sphere. Returns list of Collider.")
        .def_static(
            "overlap_box",
            [](const glm::vec3 &center, const glm::vec3 &halfExtents, const glm::quat &orientation, uint32_t layerMask,
               bool queryTriggers) {
                RequireFinite(center, "center");
                RequirePositiveExtents(halfExtents);
                RequireOrientation(orientation);
                return BorrowedColliderList(
                    PhysicsWorld::Instance().OverlapBox(center, halfExtents, orientation, layerMask, queryTriggers));
            },
            "center"_a, "half_extents"_a, "orientation"_a = glm::quat(1.0f, 0.0f, 0.0f, 0.0f),
            "layer_mask"_a = EngineConfig::Get().defaultQueryLayerMask, "query_triggers"_a = true,
            "Find all colliders within an oriented box. Returns list of Collider.")
        .def_static(
            "overlap_capsule",
            [](const glm::vec3 &point0, const glm::vec3 &point1, float radius, uint32_t layerMask, bool queryTriggers) {
                RequireFinite(point0, "point0");
                RequireFinite(point1, "point1");
                RequirePositive(radius, "radius");
                return BorrowedColliderList(
                    PhysicsWorld::Instance().OverlapCapsule(point0, point1, radius, layerMask, queryTriggers));
            },
            "point0"_a, "point1"_a, "radius"_a, "layer_mask"_a = EngineConfig::Get().defaultQueryLayerMask,
            "query_triggers"_a = true, "Find all colliders within a capsule. Returns list of Collider.")
        // ---- Shape casts ----
        .def_static(
            "sphere_cast",
            [](const glm::vec3 &origin, float radius, const glm::vec3 &direction, float maxDistance, uint32_t layerMask,
               bool queryTriggers) -> py::object {
                RequireFinite(origin, "origin");
                RequirePositive(radius, "radius");
                RequireDirectionAndDistance(direction, maxDistance);
                RaycastHit hit;
                if (PhysicsWorld::Instance().SphereCast(origin, radius, direction, maxDistance, hit, layerMask,
                                                        queryTriggers))
                    return py::cast(hit);
                return py::none();
            },
            "origin"_a, "radius"_a, "direction"_a, "max_distance"_a = 1000.0f,
            "layer_mask"_a = EngineConfig::Get().defaultQueryLayerMask, "query_triggers"_a = true,
            "Cast a sphere and return closest RaycastHit or None.")
        .def_static(
            "box_cast",
            [](const glm::vec3 &center, const glm::vec3 &halfExtents, const glm::vec3 &direction,
               const glm::quat &orientation, float maxDistance, uint32_t layerMask, bool queryTriggers) -> py::object {
                RequireFinite(center, "center");
                RequirePositiveExtents(halfExtents);
                RequireDirectionAndDistance(direction, maxDistance);
                RequireOrientation(orientation);
                RaycastHit hit;
                if (PhysicsWorld::Instance().BoxCast(center, halfExtents, direction, orientation, maxDistance, hit,
                                                     layerMask, queryTriggers))
                    return py::cast(hit);
                return py::none();
            },
            "center"_a, "half_extents"_a, "direction"_a, "orientation"_a = glm::quat(1.0f, 0.0f, 0.0f, 0.0f),
            "max_distance"_a = 1000.0f, "layer_mask"_a = EngineConfig::Get().defaultQueryLayerMask,
            "query_triggers"_a = true, "Cast a box and return closest RaycastHit or None.")
        .def_static(
            "capsule_cast",
            [](const glm::vec3 &point0, const glm::vec3 &point1, float radius, const glm::vec3 &direction,
               float maxDistance, uint32_t layerMask, bool queryTriggers) -> py::object {
                RequireFinite(point0, "point0");
                RequireFinite(point1, "point1");
                RequirePositive(radius, "radius");
                RequireDirectionAndDistance(direction, maxDistance);
                RaycastHit hit;
                if (PhysicsWorld::Instance().CapsuleCast(point0, point1, radius, direction, maxDistance, hit, layerMask,
                                                         queryTriggers))
                    return py::cast(hit);
                return py::none();
            },
            "point0"_a, "point1"_a, "radius"_a, "direction"_a, "max_distance"_a = 1000.0f,
            "layer_mask"_a = EngineConfig::Get().defaultQueryLayerMask, "query_triggers"_a = true,
            "Cast a capsule and return closest RaycastHit or None.")
        // ---- Gravity ----
        .def_static(
            "get_gravity",
            []() -> glm::vec3 {
                auto *sys = PhysicsWorld::Instance().GetJoltSystem();
                if (!sys)
                    return EngineConfig::Get().physicsGravity;
                JPH::Vec3 g = sys->GetGravity();
                return glm::vec3(g.GetX(), g.GetY(), g.GetZ());
            },
            "Get the global gravity vector.")
        .def_static(
            "set_gravity",
            [](const glm::vec3 &g) {
                if (!std::isfinite(g.x) || !std::isfinite(g.y) || !std::isfinite(g.z))
                    throw std::invalid_argument("gravity components must be finite");
                EngineConfig::Get().physicsGravity = g;
                auto *sys = PhysicsWorld::Instance().GetJoltSystem();
                if (sys)
                    sys->SetGravity(JPH::Vec3(g.x, g.y, g.z));
            },
            "gravity"_a, "Set the global gravity vector.")
        // ---- Ignore layer collision ----
        .def_static(
            "ignore_layer_collision",
            [](int layer1, int layer2, bool ignore) {
                TagLayerManager::Instance().SetLayersCollide(layer1, layer2, !ignore);
            },
            "layer1"_a, "layer2"_a, "ignore"_a = true, "Set whether two layers should ignore collisions.")
        .def_static(
            "get_ignore_layer_collision",
            [](int layer1, int layer2) -> bool {
                return !TagLayerManager::Instance().GetLayersCollide(layer1, layer2);
            },
            "layer1"_a, "layer2"_a, "Check if two layers ignore collisions.")
        // ---- Transform sync (Unity: Physics.SyncTransforms) ----
        .def_static(
            "sync_transforms", []() { SceneManager::Instance().SyncTransforms(); },
            "Apply all pending Transform changes to the physics engine.\n"
            "Call before same-frame physics queries (raycast, overlap) when you have\n"
            "moved objects in Update and need up-to-date collision geometry.\n"
            "Unity equivalent: Physics.SyncTransforms()")
        .def_static(
            "get_actor_count", []() { return PhysicsECSStore::Instance().GetAliveActorCount(); },
            "Get the number of live PhysicsActor slots.");

    // ====================================================================
    // Register component type casters in ComponentBindingRegistry
    // ====================================================================
    // Access the singleton (defined in BindingScene.cpp, same TU linkage via static)
    // We use an extern-style approach: call the lambdas that do dynamic_cast
    // The registry is populated after RegisterSceneBindings runs.
    // To avoid header coupling, we register via a post-init hook.
}

} // namespace infernux
