/**
 * @file BindingVector4f.cpp
 * @brief Python bindings for glm::vec4 as "vec4f" and glm::quat as "quatf".
 *
 * Operates directly on
 * glm::vec4 so that no manual conversion is needed anywhere in the binding
 * layer.  Also adds glm::quat bindings ("quatf") that were previously
 * missing — quaternions were only exposed as raw (x,y,z,w) tuples before.
 */

#include <cmath>
#include <functional>
#include <glm/glm.hpp>
#include <glm/gtc/quaternion.hpp>
#include <pybind11/pybind11.h>

namespace py = pybind11;

namespace infernux
{

// ── helper free-functions (mirror the old Vector4f static API) ──────────────
namespace vec4_util
{

inline float Dot(const glm::vec4 &a, const glm::vec4 &b)
{
    return glm::dot(a, b);
}
inline float Magnitude(const glm::vec4 &v)
{
    return glm::length(v);
}
inline float SqrMagnitude(const glm::vec4 &v)
{
    return glm::dot(v, v);
}
inline glm::vec4 Project(const glm::vec4 &a, const glm::vec4 &b)
{
    float denom = Dot(b, b);
    if (denom < 1e-6f)
        return glm::vec4(0.f);
    return b * (Dot(a, b) / denom);
}
inline float Distance(const glm::vec4 &a, const glm::vec4 &b)
{
    return Magnitude(a - b);
}
inline glm::vec4 Lerp(const glm::vec4 &a, const glm::vec4 &b, float t)
{
    t = std::clamp(t, 0.f, 1.f);
    return a + (b - a) * t;
}
inline glm::vec4 LerpUnclamped(const glm::vec4 &a, const glm::vec4 &b, float t)
{
    return a + (b - a) * t;
}
inline glm::vec4 Max(const glm::vec4 &a, const glm::vec4 &b)
{
    return glm::max(a, b);
}
inline glm::vec4 Min(const glm::vec4 &a, const glm::vec4 &b)
{
    return glm::min(a, b);
}
inline glm::vec4 MoveTowards(const glm::vec4 &current, const glm::vec4 &target, float maxDelta)
{
    glm::vec4 to = target - current;
    float dist = Magnitude(to);
    if (dist <= maxDelta || dist < 1e-6f)
        return target;
    return current + to * (maxDelta / dist);
}
inline glm::vec4 Normalize(const glm::vec4 &v)
{
    float mag = Magnitude(v);
    if (mag < 1e-6f)
        return glm::vec4(0.f);
    return v / mag;
}

inline float RequireFiniteFloat(float value)
{
    if (!std::isfinite(value))
        throw std::invalid_argument("Vector4 components must be finite");
    return value;
}

inline void RequireFiniteVec4(const glm::vec4 &value, const char *name)
{
    if (!std::isfinite(value.x) || !std::isfinite(value.y) || !std::isfinite(value.z) || !std::isfinite(value.w))
        throw std::invalid_argument(std::string(name) + " must contain finite values");
}

inline glm::vec4 SmoothDamp(glm::vec4 current, glm::vec4 target, glm::vec4 &currentVelocity, float smoothTime,
                            float maxSpeed, float deltaTime)
{
    RequireFiniteVec4(current, "current");
    RequireFiniteVec4(target, "target");
    RequireFiniteVec4(currentVelocity, "current_velocity");
    if (!std::isfinite(smoothTime) || smoothTime <= 0.0f)
        throw std::invalid_argument("smooth_time must be finite and positive");
    if (!std::isfinite(deltaTime) || deltaTime <= 0.0f)
        throw std::invalid_argument("delta_time must be finite and positive");
    if (std::isnan(maxSpeed) || maxSpeed < 0.0f)
        throw std::invalid_argument("max_speed must be non-negative");

    glm::vec4 originalTarget = target;
    glm::vec4 diff = current - target;
    float maxDist = maxSpeed * smoothTime;
    float dist = Magnitude(diff);
    if (std::isfinite(maxDist) && dist > maxDist && dist > 1e-6f)
        diff = diff * (maxDist / dist);
    glm::vec4 targetPos = current - diff;
    float omega = 2.f / smoothTime;
    float x = omega * deltaTime;
    float exp = 1.f / (1.f + x + 0.48f * x * x + 0.235f * x * x * x);
    glm::vec4 temp = (diff * omega) + currentVelocity;
    glm::vec4 change = temp * deltaTime;
    currentVelocity = (currentVelocity - temp * omega) * exp;
    glm::vec4 result = targetPos + (diff + change) * exp;

    glm::vec4 toOriginal = originalTarget - current;
    glm::vec4 toResult = result - originalTarget;
    if (Dot(toOriginal, toResult) > 0.0f) {
        result = originalTarget;
        currentVelocity = glm::vec4(0.0f);
    }

    if (!std::isfinite(result.x) || !std::isfinite(result.y) || !std::isfinite(result.z) || !std::isfinite(result.w)) {
        throw std::overflow_error("Vector4.smooth_damp produced a non-finite result");
    }
    return result;
}

} // namespace vec4_util

void RegisterVec4fBindings(py::module_ &m)
{
    // ====================================================================
    // vec4f — glm::vec4
    // ====================================================================
    {
        using Vec = glm::vec4;
        py::class_<Vec>(m, "vec4f")
            .def(py::init<>())
            .def(py::init([](float x, float y, float z, float w) {
                     return glm::vec4(vec4_util::RequireFiniteFloat(x), vec4_util::RequireFiniteFloat(y),
                                      vec4_util::RequireFiniteFloat(z), vec4_util::RequireFiniteFloat(w));
                 }),
                 "Construct vec4f", py::arg("x"), py::arg("y"), py::arg("z"), py::arg("w"))
            .def("__getitem__",
                 [](const Vec &v, int i) -> float {
                     if (i < 0 || i >= 4)
                         throw std::out_of_range("index out of range");
                     return v[i];
                 })
            .def("__setitem__",
                 [](Vec &v, int i, float value) {
                     if (i < 0 || i >= 4)
                         throw std::out_of_range("index out of range");
                     v[i] = vec4_util::RequireFiniteFloat(value);
                 })
            .def("__add__", [](const Vec &a, const Vec &b) { return Vec(a + b); })
            .def("__add__",
                 [](const Vec &v, py::object s) {
                     float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                     return Vec(v.x + f, v.y + f, v.z + f, v.w + f);
                 })
            .def("__radd__",
                 [](const Vec &v, py::object s) {
                     float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                     return Vec(f + v.x, f + v.y, f + v.z, f + v.w);
                 })
            .def("__sub__", [](const Vec &a, const Vec &b) { return Vec(a - b); })
            .def("__sub__",
                 [](const Vec &v, py::object s) {
                     float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                     return Vec(v.x - f, v.y - f, v.z - f, v.w - f);
                 })
            .def("__rsub__",
                 [](const Vec &v, py::object s) {
                     float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                     return Vec(f - v.x, f - v.y, f - v.z, f - v.w);
                 })
            .def("__mul__", [](const Vec &a, const Vec &b) { return Vec(a * b); })
            .def("__mul__",
                 [](const Vec &v, py::object s) {
                     float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                     return Vec(v * f);
                 })
            .def("__rmul__",
                 [](const Vec &v, py::object s) {
                     float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                     return Vec(v * f);
                 })
            .def("__truediv__", [](const Vec &a, const Vec &b) { return Vec(a / b); })
            .def("__truediv__",
                 [](const Vec &v, py::object s) {
                     float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                     return Vec(v / f);
                 })
            .def("__rtruediv__",
                 [](const Vec &v, py::object s) {
                     float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                     return Vec(f / v.x, f / v.y, f / v.z, f / v.w);
                 })
            .def("__iadd__",
                 [](Vec &v, const Vec &o) {
                     v += o;
                     return v;
                 })
            .def("__iadd__",
                 [](Vec &v, py::object s) {
                     float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                     v.x += f;
                     v.y += f;
                     v.z += f;
                     v.w += f;
                     return v;
                 })
            .def("__isub__",
                 [](Vec &v, const Vec &o) {
                     v -= o;
                     return v;
                 })
            .def("__isub__",
                 [](Vec &v, py::object s) {
                     float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                     v.x -= f;
                     v.y -= f;
                     v.z -= f;
                     v.w -= f;
                     return v;
                 })
            .def("__imul__",
                 [](Vec &v, py::object s) {
                     float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                     v *= f;
                     return v;
                 })
            .def("__itruediv__",
                 [](Vec &v, py::object s) {
                     float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                     v /= f;
                     return v;
                 })
            .def("__eq__",
                 [](const Vec &a, const Vec &b) {
                     return std::fabs(a.x - b.x) <= 1e-6f && std::fabs(a.y - b.y) <= 1e-6f &&
                            std::fabs(a.z - b.z) <= 1e-6f && std::fabs(a.w - b.w) <= 1e-6f;
                 })
            .def("__ne__",
                 [](const Vec &a, const Vec &b) {
                     return std::fabs(a.x - b.x) > 1e-6f || std::fabs(a.y - b.y) > 1e-6f ||
                            std::fabs(a.z - b.z) > 1e-6f || std::fabs(a.w - b.w) > 1e-6f;
                 })
            .def_property(
                "x", [](const Vec &v) { return v.x; },
                [](Vec &v, float val) { v.x = vec4_util::RequireFiniteFloat(val); })
            .def_property(
                "y", [](const Vec &v) { return v.y; },
                [](Vec &v, float val) { v.y = vec4_util::RequireFiniteFloat(val); })
            .def_property(
                "z", [](const Vec &v) { return v.z; },
                [](Vec &v, float val) { v.z = vec4_util::RequireFiniteFloat(val); })
            .def_property(
                "w", [](const Vec &v) { return v.w; },
                [](Vec &v, float val) { v.w = vec4_util::RequireFiniteFloat(val); })
            .def_property(
                "r", [](const Vec &v) { return v.x; },
                [](Vec &v, float val) { v.x = vec4_util::RequireFiniteFloat(val); })
            .def_property(
                "g", [](const Vec &v) { return v.y; },
                [](Vec &v, float val) { v.y = vec4_util::RequireFiniteFloat(val); })
            .def_property(
                "b", [](const Vec &v) { return v.z; },
                [](Vec &v, float val) { v.z = vec4_util::RequireFiniteFloat(val); })
            .def_property(
                "a", [](const Vec &v) { return v.w; },
                [](Vec &v, float val) { v.w = vec4_util::RequireFiniteFloat(val); })
            .def("__repr__",
                 [](const Vec &v) {
                     return "Vector4(" + std::to_string(v.x) + ", " + std::to_string(v.y) + ", " + std::to_string(v.z) +
                            ", " + std::to_string(v.w) + ")";
                 })
            .def("__str__",
                 [](const Vec &v) {
                     return "(" + std::to_string(v.x) + ", " + std::to_string(v.y) + ", " + std::to_string(v.z) + ", " +
                            std::to_string(v.w) + ")";
                 })
            .def("__neg__", [](const Vec &v) { return Vec(-v.x, -v.y, -v.z, -v.w); })
            .def("__len__", [](const Vec &) { return 4; })
            .def("__iter__", [](const Vec &v) { return py::make_tuple(v.x, v.y, v.z, v.w).attr("__iter__")(); })
            .def("__hash__",
                 [](const Vec &v) {
                     size_t h = std::hash<float>{}(v.x);
                     h ^= std::hash<float>{}(v.y) + 0x9e3779b9 + (h << 6) + (h >> 2);
                     h ^= std::hash<float>{}(v.z) + 0x9e3779b9 + (h << 6) + (h >> 2);
                     h ^= std::hash<float>{}(v.w) + 0x9e3779b9 + (h << 6) + (h >> 2);
                     return h;
                 })
            .def("__bool__", [](const Vec &v) { return vec4_util::SqrMagnitude(v) > 1e-12f; })
            .def("__abs__",
                 [](const Vec &v) { return Vec(std::abs(v.x), std::abs(v.y), std::abs(v.z), std::abs(v.w)); })
            .def("__copy__", [](const Vec &v) { return Vec(v); })
            .def("__deepcopy__", [](const Vec &v, py::dict) { return Vec(v); })
            // ---- Instance properties ----
            .def_property_readonly(
                "magnitude", [](const Vec &v) { return vec4_util::Magnitude(v); },
                "Length of this vector. Unity: vector.magnitude")
            .def_property_readonly(
                "normalized", [](const Vec &v) { return vec4_util::Normalize(v); },
                "Normalized copy of this vector. Unity: vector.normalized")
            .def_property_readonly(
                "sqr_magnitude", [](const Vec &v) { return vec4_util::SqrMagnitude(v); },
                "Squared length of this vector. Unity: vector.sqrMagnitude")
            // ---- Instance methods ----
            .def(
                "set",
                [](Vec &v, float x, float y, float z, float w) {
                    v.x = x;
                    v.y = y;
                    v.z = z;
                    v.w = w;
                },
                py::arg("x"), py::arg("y"), py::arg("z"), py::arg("w"), "Set x, y, z, w components")
            .def("to_tuple",
                 [](const Vec &v) -> py::tuple {
                     return py::make_tuple(py::float_(v.x), py::float_(v.y), py::float_(v.z), py::float_(v.w));
                 })
            .def("to_list",
                 [](const Vec &v) {
                     py::list l;
                     l.append(v.x);
                     l.append(v.y);
                     l.append(v.z);
                     l.append(v.w);
                     return l;
                 })
            // ---- Static constants ----
            .def_static("zero", []() { return Vec(0.f); })
            .def_static("one", []() { return Vec(1.f); })
            .def_static("negative_infinity", []() { return Vec(-std::numeric_limits<float>::infinity()); })
            .def_static("positive_infinity", []() { return Vec(std::numeric_limits<float>::infinity()); })
            // ---- Static math operations ----
            .def_static("normalize", &vec4_util::Normalize)
            .def_static("distance", &vec4_util::Distance)
            .def_static("project", &vec4_util::Project)
            .def_static("dot", &vec4_util::Dot)
            .def_static("lerp", &vec4_util::Lerp)
            .def_static("lerp_unclamped", &vec4_util::LerpUnclamped)
            .def_static("max", &vec4_util::Max)
            .def_static("min", &vec4_util::Min)
            .def_static("move_towards", &vec4_util::MoveTowards)
            .def_static(
                "scale", [](const Vec &a, const Vec &b) { return Vec(a * b); },
                "Multiplies two vectors component-wise. Unity: Vector4.Scale(a, b)")
            .def_static("smooth_damp", [](const Vec &current, const Vec &target, Vec currentVelocity, float smoothTime,
                                          float maxSpeed, float deltaTime) {
                Vec result = vec4_util::SmoothDamp(current, target, currentVelocity, smoothTime, maxSpeed, deltaTime);
                return py::make_tuple(result, currentVelocity);
            });
    }

    // ====================================================================
    // quatf — glm::quat  (NEW — previously only exposed as raw tuples)
    // ====================================================================
    {
        using Quat = glm::quat;
        py::class_<Quat>(m, "quatf")
            .def(py::init<>())
            .def(py::init([](float x, float y, float z, float w) {
                     return Quat(w, x, y, z); // glm ctor order: w, x, y, z
                 }),
                 py::arg("x"), py::arg("y"), py::arg("z"), py::arg("w"), "Construct from (x, y, z, w)")
            .def_property(
                "x", [](const Quat &q) { return q.x; }, [](Quat &q, float v) { q.x = v; })
            .def_property(
                "y", [](const Quat &q) { return q.y; }, [](Quat &q, float v) { q.y = v; })
            .def_property(
                "z", [](const Quat &q) { return q.z; }, [](Quat &q, float v) { q.z = v; })
            .def_property(
                "w", [](const Quat &q) { return q.w; }, [](Quat &q, float v) { q.w = v; })
            .def("__repr__",
                 [](const Quat &q) {
                     return "Quaternion(" + std::to_string(q.x) + ", " + std::to_string(q.y) + ", " +
                            std::to_string(q.z) + ", " + std::to_string(q.w) + ")";
                 })
            .def("__str__",
                 [](const Quat &q) {
                     return "(" + std::to_string(q.x) + ", " + std::to_string(q.y) + ", " + std::to_string(q.z) + ", " +
                            std::to_string(q.w) + ")";
                 })
            .def("__eq__", [](const Quat &a, const Quat &b) { return a == b; })
            .def("__ne__", [](const Quat &a, const Quat &b) { return a != b; })
            .def("__mul__", [](const Quat &a, const Quat &b) { return a * b; })
            .def("__getitem__",
                 [](const Quat &q, int i) -> float {
                     switch (i) {
                     case 0:
                         return q.x;
                     case 1:
                         return q.y;
                     case 2:
                         return q.z;
                     case 3:
                         return q.w;
                     default:
                         throw std::out_of_range("index out of range");
                     }
                 })
            .def("__len__", [](const Quat &) { return 4; })
            .def("__iter__", [](const Quat &q) { return py::make_tuple(q.x, q.y, q.z, q.w).attr("__iter__")(); })
            .def("__copy__", [](const Quat &q) { return Quat(q); })
            .def("__deepcopy__", [](const Quat &q, py::dict) { return Quat(q); })
            .def("to_tuple",
                 [](const Quat &q) -> py::tuple {
                     return py::make_tuple(py::float_(q.x), py::float_(q.y), py::float_(q.z), py::float_(q.w));
                 })
            // ---- Instance properties ----
            .def_property_readonly(
                "euler_angles",
                [](const Quat &q) {
                    auto toPublicAngleDegrees = [](float angle) {
                        float positive = std::fmod(angle, 360.0f);
                        if (positive < 0.0f) {
                            positive += 360.0f;
                        }

                        constexpr float kUnityEulerSnapEpsilon = 0.005729578f;
                        if (positive < kUnityEulerSnapEpsilon || positive > 360.0f - kUnityEulerSnapEpsilon) {
                            return 0.0f;
                        }

                        return positive;
                    };

                    // YXZ intrinsic extraction (Unity convention)
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
                    glm::vec3 euler = glm::degrees(glm::vec3(x, y, z));
                    return glm::vec3(toPublicAngleDegrees(euler.x), toPublicAngleDegrees(euler.y),
                                     toPublicAngleDegrees(euler.z));
                },
                "Convert to Euler angles (degrees, YXZ convention)")
            .def_property_readonly(
                "normalized", [](const Quat &q) { return glm::normalize(q); }, "Normalized copy of this quaternion")
            // ---- Static constructors ----
            .def_static("identity", []() { return Quat(1.f, 0.f, 0.f, 0.f); })
            .def_static(
                "euler",
                [](float x, float y, float z) {
                    // YXZ intrinsic: q = qY * qX * qZ (Unity convention)
                    glm::vec3 r = glm::radians(glm::vec3(x, y, z));
                    float cx = std::cos(r.x * 0.5f), sx = std::sin(r.x * 0.5f);
                    float cy = std::cos(r.y * 0.5f), sy = std::sin(r.y * 0.5f);
                    float cz = std::cos(r.z * 0.5f), sz = std::sin(r.z * 0.5f);
                    glm::quat q;
                    q.w = cy * cx * cz + sy * sx * sz;
                    q.x = cy * sx * cz + sy * cx * sz;
                    q.y = sy * cx * cz - cy * sx * sz;
                    q.z = cy * cx * sz - sy * sx * cz;
                    return q;
                },
                py::arg("x"), py::arg("y"), py::arg("z"),
                "Create quaternion from Euler angles (degrees, YXZ convention)")
            .def_static(
                "angle_axis",
                [](float angle, const glm::vec3 &axis) {
                    return glm::angleAxis(glm::radians(angle), glm::normalize(axis));
                },
                py::arg("angle"), py::arg("axis"), "Create quaternion from angle (degrees) and axis")
            .def_static(
                "look_rotation",
                [](const glm::vec3 &forward, const glm::vec3 &up) {
                    return glm::quatLookAt(glm::normalize(forward), up);
                },
                py::arg("forward"), py::arg("up") = glm::vec3(0.f, 1.f, 0.f),
                "Create rotation looking in forward direction")
            // ---- Static math ----
            .def_static("dot", [](const Quat &a, const Quat &b) { return glm::dot(a, b); })
            .def_static("angle",
                        [](const Quat &a, const Quat &b) { return glm::degrees(glm::angle(a * glm::inverse(b))); })
            .def_static("slerp",
                        [](const Quat &a, const Quat &b, float t) { return glm::slerp(a, b, std::clamp(t, 0.f, 1.f)); })
            .def_static("lerp", [](const Quat &a, const Quat &b,
                                   float t) { return glm::normalize(glm::lerp(a, b, std::clamp(t, 0.f, 1.f))); })
            .def_static("inverse", [](const Quat &q) { return glm::inverse(q); })
            .def_static(
                "rotate_towards",
                [](const Quat &from, const Quat &to, float maxDeg) {
                    float ang = glm::degrees(glm::angle(from * glm::inverse(to)));
                    if (ang < 1e-6f)
                        return to;
                    float t = std::min(1.f, maxDeg / ang);
                    return glm::slerp(from, to, t);
                },
                py::arg("from"), py::arg("to"), py::arg("max_degrees_delta"));
    }
}

} // namespace infernux
