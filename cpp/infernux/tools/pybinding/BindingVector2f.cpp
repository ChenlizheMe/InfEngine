/**
 * @file BindingVector2f.cpp
 * @brief Python bindings for glm::vec2 as "Vector2".
 *
 * Operates directly on
 * glm::vec2 so that no manual conversion is needed anywhere in the binding
 * layer.
 */

#include <cmath>
#include <functional>
#include <glm/glm.hpp>
#include <pybind11/pybind11.h>

namespace py = pybind11;

namespace infernux
{

// ── helper free-functions (mirror the old Vector2f static API) ──────────────
namespace vec2_util
{

inline float Dot(const glm::vec2 &a, const glm::vec2 &b)
{
    return glm::dot(a, b);
}
inline float Cross(const glm::vec2 &a, const glm::vec2 &b)
{
    return a.x * b.y - a.y * b.x;
}
inline float Magnitude(const glm::vec2 &v)
{
    return glm::length(v);
}
inline float SqrMagnitude(const glm::vec2 &v)
{
    return glm::dot(v, v);
}
inline float Angle(const glm::vec2 &from, const glm::vec2 &to)
{
    float denom = Magnitude(from) * Magnitude(to);
    if (denom < 1e-6f)
        return 0.f;
    float val = Dot(from, to) / denom;
    return std::acos(std::clamp(val, -1.f, 1.f)) * (180.0f / 3.14159265358979323846f);
}
inline glm::vec2 ClampMagnitude(const glm::vec2 &v, float maxLength)
{
    float mag = Magnitude(v);
    if (mag > maxLength && mag > 1e-6f)
        return v * (maxLength / mag);
    return v;
}
inline float Distance(const glm::vec2 &a, const glm::vec2 &b)
{
    return Magnitude(a - b);
}
inline glm::vec2 Lerp(const glm::vec2 &a, const glm::vec2 &b, float t)
{
    t = std::clamp(t, 0.f, 1.f);
    return a + (b - a) * t;
}
inline glm::vec2 LerpUnclamped(const glm::vec2 &a, const glm::vec2 &b, float t)
{
    return a + (b - a) * t;
}
inline glm::vec2 Max(const glm::vec2 &a, const glm::vec2 &b)
{
    return glm::max(a, b);
}
inline glm::vec2 Min(const glm::vec2 &a, const glm::vec2 &b)
{
    return glm::min(a, b);
}
inline glm::vec2 MoveTowards(const glm::vec2 &current, const glm::vec2 &target, float maxDelta)
{
    glm::vec2 to = target - current;
    float dist = Magnitude(to);
    if (dist <= maxDelta || dist < 1e-6f)
        return target;
    return current + to * (maxDelta / dist);
}
inline glm::vec2 Normalize(const glm::vec2 &v)
{
    float mag = Magnitude(v);
    if (mag < 1e-6f)
        return glm::vec2(0.f);
    return v / mag;
}
inline glm::vec2 Project(const glm::vec2 &v, const glm::vec2 &onNormal)
{
    float denom = Dot(onNormal, onNormal);
    if (denom < 1e-6f)
        return glm::vec2(0.f);
    return onNormal * (Dot(v, onNormal) / denom);
}
inline glm::vec2 ProjectOnPlane(const glm::vec2 &v, const glm::vec2 &planeNormal)
{
    return v - Project(v, planeNormal);
}
inline glm::vec2 Perpendicular(const glm::vec2 &v)
{
    return glm::vec2(-v.y, v.x);
}
inline glm::vec2 Reflect(const glm::vec2 &inDir, const glm::vec2 &inNorm)
{
    return inDir - inNorm * (Dot(inDir, inNorm) * 2.f);
}
inline float SignedAngle(const glm::vec2 &from, const glm::vec2 &to)
{
    float ang = Angle(from, to);
    if (Cross(from, to) < 0)
        ang = -ang;
    return ang;
}
inline glm::vec2 SlerpUnclamped(glm::vec2 a, glm::vec2 b, float t)
{
    a = Normalize(a);
    b = Normalize(b);
    float dotAB = std::clamp(Dot(a, b), -1.f, 1.f);
    float theta = std::acos(dotAB) * t;
    glm::vec2 rel = Normalize(b - a * dotAB);
    if (SqrMagnitude(rel) < 1e-12f)
        return LerpUnclamped(a, b, t);
    return a * std::cos(theta) + rel * std::sin(theta);
}
inline glm::vec2 Slerp(const glm::vec2 &a, const glm::vec2 &b, float t)
{
    return SlerpUnclamped(a, b, std::clamp(t, 0.f, 1.f));
}
inline float RequireFiniteFloat(float value)
{
    if (!std::isfinite(value))
        throw std::invalid_argument("Vector2 components must be finite");
    return value;
}

inline void RequireFiniteVec2(const glm::vec2 &value, const char *name)
{
    if (!std::isfinite(value.x) || !std::isfinite(value.y))
        throw std::invalid_argument(std::string(name) + " must contain finite values");
}

inline glm::vec2 SmoothDamp(glm::vec2 current, glm::vec2 target, glm::vec2 &currentVelocity, float smoothTime,
                            float maxSpeed, float deltaTime)
{
    RequireFiniteVec2(current, "current");
    RequireFiniteVec2(target, "target");
    RequireFiniteVec2(currentVelocity, "current_velocity");
    if (!std::isfinite(smoothTime) || smoothTime <= 0.0f)
        throw std::invalid_argument("smooth_time must be finite and positive");
    if (!std::isfinite(deltaTime) || deltaTime <= 0.0f)
        throw std::invalid_argument("delta_time must be finite and positive");
    if (std::isnan(maxSpeed) || maxSpeed < 0.0f)
        throw std::invalid_argument("max_speed must be non-negative");

    float omega = 2.f / smoothTime;
    float x = omega * deltaTime;
    float exp = 1.f / (1.f + x + 0.48f * x * x + 0.235f * x * x * x);

    glm::vec2 originalTarget = target;
    glm::vec2 diff = current - target;
    float maxDist = maxSpeed * smoothTime;
    float dist = Magnitude(diff);
    if (std::isfinite(maxDist) && dist > maxDist && dist > 1e-6f)
        diff = diff * (maxDist / dist);
    target = current - diff;

    glm::vec2 temp = (currentVelocity + diff * omega) * deltaTime;
    currentVelocity = (currentVelocity - omega * temp) * exp;
    glm::vec2 result = target + (diff + temp) * exp;

    // Prevent overshooting the original target
    glm::vec2 toOriginal = originalTarget - current;
    glm::vec2 toResult = result - originalTarget;
    if (Dot(toOriginal, toResult) > 0.0f) {
        result = originalTarget;
        currentVelocity = glm::vec2(0.0f);
    }

    if (!std::isfinite(result.x) || !std::isfinite(result.y)) {
        throw std::overflow_error("Vector2.smooth_damp produced a non-finite result");
    }
    return result;
}

} // namespace vec2_util

void RegisterVector2Bindings(py::module_ &m)
{
    using Vec = glm::vec2;
    py::class_<Vec>(m, "Vector2")
        .def(py::init<>())
        .def(py::init([](float x, float y) {
                 return glm::vec2(vec2_util::RequireFiniteFloat(x), vec2_util::RequireFiniteFloat(y));
             }),
             "Construct vec2", py::arg("x"), py::arg("y"))
        .def("__getitem__",
             [](const Vec &v, int i) -> float {
                 if (i < 0 || i >= 2)
                     throw std::out_of_range("index out of range");
                 return v[i];
             })
        .def("__setitem__",
             [](Vec &v, int i, float value) {
                 if (i < 0 || i >= 2)
                     throw std::out_of_range("index out of range");
                 v[i] = vec2_util::RequireFiniteFloat(value);
             })
        .def("__add__", [](const Vec &a, const Vec &b) { return Vec(a + b); })
        .def("__add__",
             [](const Vec &v, py::object s) {
                 float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                 return Vec(v.x + f, v.y + f);
             })
        .def("__radd__",
             [](const Vec &v, py::object s) {
                 float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                 return Vec(f + v.x, f + v.y);
             })
        .def("__sub__", [](const Vec &a, const Vec &b) { return Vec(a - b); })
        .def("__sub__",
             [](const Vec &v, py::object s) {
                 float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                 return Vec(v.x - f, v.y - f);
             })
        .def("__rsub__",
             [](const Vec &v, py::object s) {
                 float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                 return Vec(f - v.x, f - v.y);
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
                 return Vec(f / v.x, f / v.y);
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
             [](const Vec &a, const Vec &b) { return std::fabs(a.x - b.x) <= 1e-6f && std::fabs(a.y - b.y) <= 1e-6f; })
        .def("__ne__",
             [](const Vec &a, const Vec &b) { return std::fabs(a.x - b.x) > 1e-6f || std::fabs(a.y - b.y) > 1e-6f; })
        .def_property(
            "x", [](const Vec &v) { return v.x; }, [](Vec &v, float val) { v.x = vec2_util::RequireFiniteFloat(val); })
        .def_property(
            "y", [](const Vec &v) { return v.y; }, [](Vec &v, float val) { v.y = vec2_util::RequireFiniteFloat(val); })
        .def_property(
            "r", [](const Vec &v) { return v.x; }, [](Vec &v, float val) { v.x = vec2_util::RequireFiniteFloat(val); })
        .def_property(
            "g", [](const Vec &v) { return v.y; }, [](Vec &v, float val) { v.y = vec2_util::RequireFiniteFloat(val); })
        .def("__repr__",
             [](const Vec &v) { return "Vector2(" + std::to_string(v.x) + ", " + std::to_string(v.y) + ")"; })
        .def("__str__", [](const Vec &v) { return "(" + std::to_string(v.x) + ", " + std::to_string(v.y) + ")"; })
        .def("__neg__", [](const Vec &v) { return Vec(-v.x, -v.y); })
        .def("__len__", [](const Vec &) { return 2; })
        .def("__iter__", [](const Vec &v) { return py::make_tuple(v.x, v.y).attr("__iter__")(); })
        .def("__hash__",
             [](const Vec &v) {
                 size_t h = std::hash<float>{}(v.x);
                 h ^= std::hash<float>{}(v.y) + 0x9e3779b9 + (h << 6) + (h >> 2);
                 return h;
             })
        .def("__bool__", [](const Vec &v) { return vec2_util::SqrMagnitude(v) > 1e-12f; })
        .def("__abs__", [](const Vec &v) { return Vec(std::abs(v.x), std::abs(v.y)); })
        .def("__copy__", [](const Vec &v) { return Vec(v); })
        .def("__deepcopy__", [](const Vec &v, py::dict) { return Vec(v); })
        // ---- Instance properties ----
        .def_property_readonly(
            "magnitude", [](const Vec &v) { return vec2_util::Magnitude(v); },
            "Length of this vector. Unity: vector.magnitude")
        .def_property_readonly(
            "normalized", [](const Vec &v) { return vec2_util::Normalize(v); },
            "Normalized copy of this vector. Unity: vector.normalized")
        .def_property_readonly(
            "sqr_magnitude", [](const Vec &v) { return vec2_util::SqrMagnitude(v); },
            "Squared length of this vector. Unity: vector.sqrMagnitude")
        // ---- Instance methods ----
        .def(
            "set",
            [](Vec &v, float x, float y) {
                v.x = x;
                v.y = y;
            },
            py::arg("x"), py::arg("y"), "Set x, y components")
        .def("to_tuple", [](const Vec &v) -> py::tuple { return py::make_tuple(py::float_(v.x), py::float_(v.y)); })
        .def("to_list",
             [](const Vec &v) {
                 py::list l;
                 l.append(v.x);
                 l.append(v.y);
                 return l;
             })
        // ---- Static constants ----
        .def_static("up", []() { return Vec(0.f, 1.f); })
        .def_static("down", []() { return Vec(0.f, -1.f); })
        .def_static("left", []() { return Vec(-1.f, 0.f); })
        .def_static("right", []() { return Vec(1.f, 0.f); })
        .def_static("zero", []() { return Vec(0.f); })
        .def_static("one", []() { return Vec(1.f); })
        .def_static("negative_infinity", []() { return Vec(-std::numeric_limits<float>::infinity()); })
        .def_static("positive_infinity", []() { return Vec(std::numeric_limits<float>::infinity()); })
        // ---- Static math operations ----
        .def_static("cross", &vec2_util::Cross)
        .def_static("normalize", &vec2_util::Normalize)
        .def_static("angle", &vec2_util::Angle)
        .def_static("clamp_magnitude", &vec2_util::ClampMagnitude)
        .def_static("distance", &vec2_util::Distance)
        .def_static("dot", &vec2_util::Dot)
        .def_static("lerp", &vec2_util::Lerp)
        .def_static("lerp_unclamped", &vec2_util::LerpUnclamped)
        .def_static("max", &vec2_util::Max)
        .def_static("min", &vec2_util::Min)
        .def_static("move_towards", &vec2_util::MoveTowards)
        .def_static("perpendicular", &vec2_util::Perpendicular)
        .def_static("project", &vec2_util::Project)
        .def_static("reflect", &vec2_util::Reflect)
        .def_static(
            "scale", [](const Vec &a, const Vec &b) { return Vec(a * b); },
            "Multiplies two vectors component-wise. Unity: Vector2.Scale(a, b)")
        .def_static("signed_angle", &vec2_util::SignedAngle)
        .def_static("smooth_damp", [](const Vec &current, const Vec &target, Vec currentVelocity, float smoothTime,
                                      float maxSpeed, float deltaTime) {
            Vec result = vec2_util::SmoothDamp(current, target, currentVelocity, smoothTime, maxSpeed, deltaTime);
            return py::make_tuple(result, currentVelocity);
        });
}

} // namespace infernux
