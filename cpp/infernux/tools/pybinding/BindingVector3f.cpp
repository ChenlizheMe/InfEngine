/**
 * @file BindingVector3f.cpp
 * @brief Python bindings for glm::vec3 as "Vector3".
 *
 * Operates directly on
 * glm::vec3 so that no manual conversion is needed anywhere in the binding
 * layer.
 */

#include <cmath>
#include <functional>
#include <glm/glm.hpp>
#include <glm/gtc/constants.hpp>
#include <pybind11/pybind11.h>

namespace py = pybind11;

namespace infernux
{

// ── helper free-functions (mirror the old Vector3f static API) ──────────────
namespace vec3_util
{

inline float Dot(const glm::vec3 &a, const glm::vec3 &b)
{
    return glm::dot(a, b);
}
inline glm::vec3 Cross(const glm::vec3 &a, const glm::vec3 &b)
{
    return glm::cross(a, b);
}
inline float Magnitude(const glm::vec3 &v)
{
    return glm::length(v);
}
inline float SqrMagnitude(const glm::vec3 &v)
{
    return glm::dot(v, v);
}
inline float Angle(const glm::vec3 &from, const glm::vec3 &to)
{
    float denom = Magnitude(from) * Magnitude(to);
    if (denom < 1e-6f)
        return 0.f;
    float val = Dot(from, to) / denom;
    return std::acos(std::clamp(val, -1.f, 1.f)) * (180.0f / glm::pi<float>());
}
inline glm::vec3 ClampMagnitude(const glm::vec3 &v, float maxLength)
{
    float mag = Magnitude(v);
    if (mag > maxLength && mag > 1e-6f)
        return v * (maxLength / mag);
    return v;
}
inline float Distance(const glm::vec3 &a, const glm::vec3 &b)
{
    return Magnitude(a - b);
}
inline glm::vec3 Lerp(const glm::vec3 &a, const glm::vec3 &b, float t)
{
    t = std::clamp(t, 0.f, 1.f);
    return a + (b - a) * t;
}
inline glm::vec3 LerpUnclamped(const glm::vec3 &a, const glm::vec3 &b, float t)
{
    return a + (b - a) * t;
}
inline glm::vec3 Max(const glm::vec3 &a, const glm::vec3 &b)
{
    return glm::max(a, b);
}
inline glm::vec3 Min(const glm::vec3 &a, const glm::vec3 &b)
{
    return glm::min(a, b);
}
inline glm::vec3 MoveTowards(const glm::vec3 &current, const glm::vec3 &target, float maxDelta)
{
    glm::vec3 to = target - current;
    float dist = Magnitude(to);
    if (dist <= maxDelta || dist < 1e-6f)
        return target;
    return current + to * (maxDelta / dist);
}
inline glm::vec3 Normalize(const glm::vec3 &v)
{
    float mag = Magnitude(v);
    if (mag < 1e-6f)
        return glm::vec3(0.f);
    return v / mag;
}
inline glm::vec3 Project(const glm::vec3 &v, const glm::vec3 &onNormal)
{
    float denom = Dot(onNormal, onNormal);
    if (denom < 1e-6f)
        return glm::vec3(0.f);
    return onNormal * (Dot(v, onNormal) / denom);
}
inline glm::vec3 ProjectOnPlane(const glm::vec3 &v, const glm::vec3 &planeNormal)
{
    return v - Project(v, planeNormal);
}
inline glm::vec3 Reflect(const glm::vec3 &inDir, const glm::vec3 &inNorm)
{
    return inDir - inNorm * (Dot(inDir, inNorm) * 2.f);
}
inline float SignedAngle(const glm::vec3 &from, const glm::vec3 &to)
{
    float ang = Angle(from, to);
    if (Cross(from, to).z < 0)
        ang = -ang;
    return ang;
}
inline glm::vec3 SlerpUnclamped(glm::vec3 a, glm::vec3 b, float t)
{
    a = Normalize(a);
    b = Normalize(b);
    float dotAB = std::clamp(Dot(a, b), -1.f, 1.f);
    float theta = std::acos(dotAB) * t;
    glm::vec3 rel = Normalize(b - a * dotAB);
    if (SqrMagnitude(rel) < 1e-12f)
        return LerpUnclamped(a, b, t);
    return a * std::cos(theta) + rel * std::sin(theta);
}
inline glm::vec3 Slerp(const glm::vec3 &a, const glm::vec3 &b, float t)
{
    return SlerpUnclamped(a, b, std::clamp(t, 0.f, 1.f));
}
inline void RotateTowards(glm::vec3 &current, const glm::vec3 &target, float maxRadiansDelta, float maxMagnitudeDelta)
{
    float magCur = Magnitude(current);
    float magTar = Magnitude(target);
    float newMag = std::clamp(magTar, magCur - maxMagnitudeDelta, magCur + maxMagnitudeDelta);
    float ang = Angle(current, target) * (glm::pi<float>() / 180.0f);
    if (ang < 1e-6f) {
        current = Normalize(target) * newMag;
        return;
    }
    float step = maxRadiansDelta / ang;
    step = std::min(step, 1.f);
    glm::vec3 s = SlerpUnclamped(Normalize(current), Normalize(target), step);
    current = s * newMag;
}
inline void OrthoNormalize(glm::vec3 &v1, glm::vec3 &v2, glm::vec3 &v3)
{
    v1 = Normalize(v1);
    v2 = ProjectOnPlane(v2, v1);
    v2 = Normalize(v2);
    v3 = ProjectOnPlane(v3, v1);
    v3 = ProjectOnPlane(v3, v2);
    v3 = Normalize(v3);
}
inline float RequireFiniteFloat(float value)
{
    if (!std::isfinite(value))
        throw std::invalid_argument("Vector3 components must be finite");
    return value;
}

inline void RequireFiniteVec3(const glm::vec3 &value, const char *name)
{
    if (!std::isfinite(value.x) || !std::isfinite(value.y) || !std::isfinite(value.z))
        throw std::invalid_argument(std::string(name) + " must contain finite values");
}

inline glm::vec3 SmoothDamp(glm::vec3 current, glm::vec3 target, glm::vec3 &currentVelocity, float smoothTime,
                            float maxSpeed, float deltaTime)
{
    RequireFiniteVec3(current, "current");
    RequireFiniteVec3(target, "target");
    RequireFiniteVec3(currentVelocity, "current_velocity");
    if (!std::isfinite(smoothTime) || smoothTime <= 0.0f)
        throw std::invalid_argument("smooth_time must be finite and positive");
    if (!std::isfinite(deltaTime) || deltaTime <= 0.0f)
        throw std::invalid_argument("delta_time must be finite and positive");
    if (std::isnan(maxSpeed) || maxSpeed < 0.0f)
        throw std::invalid_argument("max_speed must be non-negative");

    float omega = 2.f / smoothTime;
    float x = omega * deltaTime;
    float exp = 1.f / (1.f + x + 0.48f * x * x + 0.235f * x * x * x);

    glm::vec3 originalTarget = target;
    glm::vec3 diff = current - target;
    float maxDist = maxSpeed * smoothTime;
    float dist = Magnitude(diff);
    if (std::isfinite(maxDist) && dist > maxDist && dist > 1e-6f)
        diff = diff * (maxDist / dist);
    target = current - diff;

    glm::vec3 temp = (currentVelocity + diff * omega) * deltaTime;
    currentVelocity = (currentVelocity - omega * temp) * exp;
    glm::vec3 result = target + (diff + temp) * exp;

    // Prevent overshooting the original target
    glm::vec3 toOriginal = originalTarget - current;
    glm::vec3 toResult = result - originalTarget;
    if (Dot(toOriginal, toResult) > 0.0f) {
        result = originalTarget;
        currentVelocity = glm::vec3(0.0f);
    }

    if (!std::isfinite(result.x) || !std::isfinite(result.y) || !std::isfinite(result.z)) {
        throw std::overflow_error("Vector3.smooth_damp produced a non-finite result");
    }
    return result;
}

} // namespace vec3_util

void RegisterVector3Bindings(py::module_ &m)
{
    using Vec = glm::vec3;
    py::class_<Vec>(m, "Vector3")
        .def(py::init<>())
        .def(py::init([](float x, float y, float z) {
                 return glm::vec3(vec3_util::RequireFiniteFloat(x), vec3_util::RequireFiniteFloat(y),
                                  vec3_util::RequireFiniteFloat(z));
             }),
             "Construct vec3", py::arg("x"), py::arg("y"), py::arg("z"))
        .def("__getitem__",
             [](const Vec &v, int i) -> float {
                 if (i < 0 || i >= 3)
                     throw std::out_of_range("index out of range");
                 return v[i];
             })
        .def("__setitem__",
             [](Vec &v, int i, float value) {
                 if (i < 0 || i >= 3)
                     throw std::out_of_range("index out of range");
                 v[i] = vec3_util::RequireFiniteFloat(value);
             })
        .def("__add__", [](const Vec &a, const Vec &b) { return Vec(a + b); })
        .def("__add__",
             [](const Vec &v, py::object s) {
                 float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                 return Vec(v.x + f, v.y + f, v.z + f);
             })
        .def("__radd__",
             [](const Vec &v, py::object s) {
                 float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                 return Vec(f + v.x, f + v.y, f + v.z);
             })
        .def("__sub__", [](const Vec &a, const Vec &b) { return Vec(a - b); })
        .def("__sub__",
             [](const Vec &v, py::object s) {
                 float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                 return Vec(v.x - f, v.y - f, v.z - f);
             })
        .def("__rsub__",
             [](const Vec &v, py::object s) {
                 float f = py::isinstance<py::int_>(s) ? static_cast<float>(s.cast<int>()) : s.cast<float>();
                 return Vec(f - v.x, f - v.y, f - v.z);
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
                 return Vec(f / v.x, f / v.y, f / v.z);
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
                 return std::fabs(a.x - b.x) <= 1e-6f && std::fabs(a.y - b.y) <= 1e-6f && std::fabs(a.z - b.z) <= 1e-6f;
             })
        .def("__ne__",
             [](const Vec &a, const Vec &b) {
                 return std::fabs(a.x - b.x) > 1e-6f || std::fabs(a.y - b.y) > 1e-6f || std::fabs(a.z - b.z) > 1e-6f;
             })
        .def_property(
            "x", [](const Vec &v) { return v.x; }, [](Vec &v, float val) { v.x = vec3_util::RequireFiniteFloat(val); })
        .def_property(
            "y", [](const Vec &v) { return v.y; }, [](Vec &v, float val) { v.y = vec3_util::RequireFiniteFloat(val); })
        .def_property(
            "z", [](const Vec &v) { return v.z; }, [](Vec &v, float val) { v.z = vec3_util::RequireFiniteFloat(val); })
        .def_property(
            "r", [](const Vec &v) { return v.x; }, [](Vec &v, float val) { v.x = vec3_util::RequireFiniteFloat(val); })
        .def_property(
            "g", [](const Vec &v) { return v.y; }, [](Vec &v, float val) { v.y = vec3_util::RequireFiniteFloat(val); })
        .def_property(
            "b", [](const Vec &v) { return v.z; }, [](Vec &v, float val) { v.z = vec3_util::RequireFiniteFloat(val); })
        .def("__repr__",
             [](const Vec &v) {
                 return "Vector3(" + std::to_string(v.x) + ", " + std::to_string(v.y) + ", " + std::to_string(v.z) +
                        ")";
             })
        .def("__str__",
             [](const Vec &v) {
                 return "(" + std::to_string(v.x) + ", " + std::to_string(v.y) + ", " + std::to_string(v.z) + ")";
             })
        .def("__neg__", [](const Vec &v) { return Vec(-v.x, -v.y, -v.z); })
        .def("__len__", [](const Vec &) { return 3; })
        .def("__iter__", [](const Vec &v) { return py::make_tuple(v.x, v.y, v.z).attr("__iter__")(); })
        .def("__hash__",
             [](const Vec &v) {
                 size_t h = std::hash<float>{}(v.x);
                 h ^= std::hash<float>{}(v.y) + 0x9e3779b9 + (h << 6) + (h >> 2);
                 h ^= std::hash<float>{}(v.z) + 0x9e3779b9 + (h << 6) + (h >> 2);
                 return h;
             })
        .def("__bool__", [](const Vec &v) { return vec3_util::SqrMagnitude(v) > 1e-12f; })
        .def("__abs__", [](const Vec &v) { return Vec(std::abs(v.x), std::abs(v.y), std::abs(v.z)); })
        .def("__copy__", [](const Vec &v) { return Vec(v); })
        .def("__deepcopy__", [](const Vec &v, py::dict) { return Vec(v); })
        // ---- Instance properties ----
        .def_property_readonly(
            "magnitude", [](const Vec &v) { return vec3_util::Magnitude(v); },
            "Length of this vector. Unity: vector.magnitude")
        .def_property_readonly(
            "normalized", [](const Vec &v) { return vec3_util::Normalize(v); },
            "Normalized copy of this vector (unit length). Unity: vector.normalized")
        .def_property_readonly(
            "sqr_magnitude", [](const Vec &v) { return vec3_util::SqrMagnitude(v); },
            "Squared length of this vector. Unity: vector.sqrMagnitude")
        // ---- Instance methods ----
        .def(
            "set",
            [](Vec &v, float x, float y, float z) {
                v.x = x;
                v.y = y;
                v.z = z;
            },
            py::arg("x"), py::arg("y"), py::arg("z"), "Set x, y, z components. Unity: vector.Set(x, y, z)")
        .def(
            "to_tuple",
            [](const Vec &v) -> py::tuple { return py::make_tuple(py::float_(v.x), py::float_(v.y), py::float_(v.z)); },
            "Convert to Python tuple (x, y, z)")
        .def(
            "to_list",
            [](const Vec &v) {
                py::list l;
                l.append(v.x);
                l.append(v.y);
                l.append(v.z);
                return l;
            },
            "Convert to Python list [x, y, z]")
        // ---- Static direction constants ----
        .def_static("up", []() { return Vec(0.f, 1.f, 0.f); })
        .def_static("down", []() { return Vec(0.f, -1.f, 0.f); })
        .def_static("left", []() { return Vec(-1.f, 0.f, 0.f); })
        .def_static("right", []() { return Vec(1.f, 0.f, 0.f); })
        .def_static("forward", []() { return Vec(0.f, 0.f, 1.f); })
        .def_static("back", []() { return Vec(0.f, 0.f, -1.f); })
        .def_static("zero", []() { return Vec(0.f); })
        .def_static("one", []() { return Vec(1.f); })
        .def_static("negative_infinity", []() { return Vec(-std::numeric_limits<float>::infinity()); })
        .def_static("positive_infinity", []() { return Vec(std::numeric_limits<float>::infinity()); })
        // ---- Static math operations (Unity: Vector3.Dot, Vector3.Cross, etc.) ----
        .def_static("cross", &vec3_util::Cross)
        .def_static("normalize", &vec3_util::Normalize)
        .def_static("angle", &vec3_util::Angle)
        .def_static("clamp_magnitude", &vec3_util::ClampMagnitude)
        .def_static("distance", &vec3_util::Distance)
        .def_static("dot", &vec3_util::Dot)
        .def_static("lerp", &vec3_util::Lerp)
        .def_static("lerp_unclamped", &vec3_util::LerpUnclamped)
        .def_static("max", &vec3_util::Max)
        .def_static("min", &vec3_util::Min)
        .def_static("ortho_normalize",
                    [](Vec v1, Vec v2, Vec v3) {
                        vec3_util::OrthoNormalize(v1, v2, v3);
                        return py::make_tuple(v1, v2, v3);
                    })
        .def_static("project", &vec3_util::Project)
        .def_static("project_on_plane", &vec3_util::ProjectOnPlane)
        .def_static("move_towards", &vec3_util::MoveTowards)
        .def_static("reflect", &vec3_util::Reflect)
        .def_static("rotate_towards",
                    [](Vec current, const Vec &target, float maxRadiansDelta, float maxMagnitudeDelta) {
                        vec3_util::RotateTowards(current, target, maxRadiansDelta, maxMagnitudeDelta);
                        return current;
                    })
        .def_static("slerp", &vec3_util::Slerp)
        .def_static("slerp_unclamped", &vec3_util::SlerpUnclamped)
        .def_static(
            "scale", [](const Vec &a, const Vec &b) { return Vec(a * b); },
            "Multiplies two vectors component-wise. Unity: Vector3.Scale(a, b)")
        .def_static("signed_angle", &vec3_util::SignedAngle)
        .def_static("smooth_damp", [](const Vec &current, const Vec &target, Vec currentVelocity, float smoothTime,
                                      float maxSpeed, float deltaTime) {
            Vec result = vec3_util::SmoothDamp(current, target, currentVelocity, smoothTime, maxSpeed, deltaTime);
            return py::make_tuple(result, currentVelocity);
        });
}

} // namespace infernux
