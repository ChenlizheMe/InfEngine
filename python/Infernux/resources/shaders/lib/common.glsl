@shader_id: Lib Common

// ============================================================================
// lib/common.glsl — Common utility functions
//
// Full-coverage math, logic, range, and wave helpers matching Unity
// ShaderGraph node library. Context-free — no UBO or varying dependencies.
// Usage: @import: Lib Common
// ============================================================================

// ============================================================================
// Constants
// ============================================================================

const float PI       = 3.14159265359;
const float INV_PI   = 0.31830988618;
const float HALF_PI  = 1.57079632679;
const float TWO_PI   = 6.28318530718;
const float TAU      = 6.28318530718;
const float EPSILON  = 0.0001;
const float FLT_MIN  = 1.175494e-38;
const float FLT_MAX  = 3.402823e+38;
const float DEG2RAD  = 0.01745329252;
const float RAD2DEG  = 57.2957795131;
const float E        = 2.71828182846;
const float SQRT2    = 1.41421356237;
const float PHI      = 1.61803398875;  // golden ratio

// ============================================================================
// Basic Math  (Unity: Math category)
// ============================================================================

// Saturate — clamp [0, 1]  (Unity: Saturate)
float saturate(float x) { return clamp(x, 0.0, 1.0); }
vec2  saturate(vec2  x) { return clamp(x, vec2(0.0), vec2(1.0)); }
vec3  saturate(vec3  x) { return clamp(x, vec3(0.0), vec3(1.0)); }
vec4  saturate(vec4  x) { return clamp(x, vec4(0.0), vec4(1.0)); }

// Fraction — fract alias  (Unity: Fraction)
float fraction(float x) { return fract(x); }
vec2  fraction(vec2  x) { return fract(x); }
vec3  fraction(vec3  x) { return fract(x); }
vec4  fraction(vec4  x) { return fract(x); }

// Reciprocal — 1/x  (Unity: Reciprocal)
float reciprocal(float x) { return 1.0 / x; }
vec2  reciprocal(vec2  x) { return vec2(1.0) / x; }
vec3  reciprocal(vec3  x) { return vec3(1.0) / x; }
vec4  reciprocal(vec4  x) { return vec4(1.0) / x; }

// Negate  (Unity: Negate)
float negate(float x) { return -x; }
vec2  negate(vec2  x) { return -x; }
vec3  negate(vec3  x) { return -x; }
vec4  negate(vec4  x) { return -x; }

// One Minus  (Unity: One Minus)
float oneMinus(float x) { return 1.0 - x; }
vec2  oneMinus(vec2  x) { return vec2(1.0) - x; }
vec3  oneMinus(vec3  x) { return vec3(1.0) - x; }
vec4  oneMinus(vec4  x) { return vec4(1.0) - x; }

// Square  (Unity: Power → 2)
float sq(float x) { return x * x; }
vec2  sq(vec2  x) { return x * x; }
vec3  sq(vec3  x) { return x * x; }
vec4  sq(vec4  x) { return x * x; }

// Exponential — e^x  (Unity: Exponential)
float exponential(float x) { return exp(x); }
vec2  exponential(vec2  x) { return exp(x); }
vec3  exponential(vec3  x) { return exp(x); }

// Square Root  (Unity: Square Root)
float squareRoot(float x) { return sqrt(x); }
vec2  squareRoot(vec2  x) { return sqrt(x); }
vec3  squareRoot(vec3  x) { return sqrt(x); }

// Truncate — integer part toward zero  (Unity: Truncate)
float truncate(float x) { return trunc(x); }
vec2  truncate(vec2  x) { return trunc(x); }
vec3  truncate(vec3  x) { return trunc(x); }

// Posterize — quantize to N steps  (Unity: Posterize)
float posterize(float value, float steps) {
    return floor(value * steps) / steps;
}
vec3 posterize(vec3 value, float steps) {
    return floor(value * steps) / steps;
}

// Modulo — GLSL mod alias  (Unity: Modulo)
float modulo(float x, float y) { return mod(x, y); }
vec2  modulo(vec2  x, vec2  y) { return mod(x, y); }
vec3  modulo(vec3  x, vec3  y) { return mod(x, y); }
vec2  modulo(vec2  x, float y) { return mod(x, vec2(y)); }

// ============================================================================
// Range & Interpolation  (Unity: Math > Range category)
// ============================================================================

// Remap  (Unity: Remap)
float remap(float value, float fromLow, float fromHigh, float toLow, float toHigh) {
    float t = (value - fromLow) / (fromHigh - fromLow);
    return mix(toLow, toHigh, t);
}

// Inverse Lerp  (Unity: Inverse Lerp)
float inverseLerp(float a, float b, float value) {
    return (value - a) / (b - a);
}

// Remap with output clamped to [toLow, toHigh]
float remapClamped(float value, float fromLow, float fromHigh, float toLow, float toHigh) {
    float t = clamp((value - fromLow) / (fromHigh - fromLow), 0.0, 1.0);
    return mix(toLow, toHigh, t);
}

// Smoothstep — hermite interpolation  (Unity: Smoothstep)
float smoothstepRange(float edge0, float edge1, float x) {
    return smoothstep(edge0, edge1, x);
}

// ============================================================================
// Comparison & Logic  (Unity: Math > Comparison)
// ============================================================================

// Comparison returning 0.0 or 1.0  (Unity: Comparison)
float isEqual(float a, float b)        { return step(1.0 - EPSILON, 1.0 - abs(a - b)); }
float isNotEqual(float a, float b)     { return 1.0 - isEqual(a, b); }
float isGreater(float a, float b)      { return step(b + EPSILON, a); }
float isGreaterOrEqual(float a, float b) { return step(b, a); }
float isLess(float a, float b)         { return step(a + EPSILON, b); }
float isLessOrEqual(float a, float b)  { return step(a, b); }

// Branch — branchless ternary  (Unity: Branch)
float branch(float predicate, float trueVal, float falseVal) {
    return mix(falseVal, trueVal, predicate);
}
vec3 branch(float predicate, vec3 trueVal, vec3 falseVal) {
    return mix(falseVal, trueVal, predicate);
}
vec4 branch(float predicate, vec4 trueVal, vec4 falseVal) {
    return mix(falseVal, trueVal, predicate);
}

// Is NaN / Is Infinite  (Unity: Is NaN, Is Infinite)
float isNaN(float x)      { return (x != x) ? 1.0 : 0.0; }
float isInfinite(float x) { return (abs(x) > 3.4e38) ? 1.0 : 0.0; }

// ============================================================================
// Vector Operations  (Unity: Math > Vector category)
// ============================================================================

// Luminance (Rec. 709)  (Unity: not a node, but commonly needed)
float luminance(vec3 color) {
    return dot(color, vec3(0.2126, 0.7152, 0.0722));
}

// Safe normalize — returns fallback when length is near zero
vec3 safeNormalize(vec3 v, vec3 fallback) {
    float len = length(v);
    return (len > 1e-6) ? v / len : fallback;
}
vec3 safeNormalize(vec3 v) {
    return safeNormalize(v, vec3(0.0, 0.0, 1.0));
}

// Projection — project A onto B  (Unity: Projection)
vec3 projection(vec3 a, vec3 b) {
    return b * (dot(a, b) / dot(b, b));
}
vec2 projection(vec2 a, vec2 b) {
    return b * (dot(a, b) / dot(b, b));
}

// Rejection — component of A perpendicular to B  (Unity: Rejection)
vec3 rejection(vec3 a, vec3 b) {
    return a - projection(a, b);
}
vec2 rejection(vec2 a, vec2 b) {
    return a - projection(a, b);
}

// Reflect — reflect incident vector around normal  (matches GLSL reflect)
// GLSL built-in: reflect(I, N)

// Rotate About Axis — rotate a vector around an arbitrary axis  (Unity: Rotate About Axis)
vec3 rotateAboutAxis(vec3 v, vec3 axis, float angle) {
    axis = normalize(axis);
    float s = sin(angle);
    float c = cos(angle);
    float oc = 1.0 - c;
    mat3 m = mat3(
        oc * axis.x * axis.x + c,          oc * axis.x * axis.y - axis.z * s,  oc * axis.z * axis.x + axis.y * s,
        oc * axis.x * axis.y + axis.z * s,  oc * axis.y * axis.y + c,          oc * axis.y * axis.z - axis.x * s,
        oc * axis.z * axis.x - axis.y * s,  oc * axis.y * axis.z + axis.x * s,  oc * axis.z * axis.z + c
    );
    return m * v;
}

// Sphere Mask — 1.0 inside the sphere, fading to 0.0 at the surface  (Unity: Sphere Mask)
float sphereMask(vec3 coords, vec3 center, float radius, float hardness) {
    float dist = length(coords - center);
    return 1.0 - saturate((dist - radius) / (1.0 - hardness + EPSILON));
}
float sphereMask(vec2 coords, vec2 center, float radius, float hardness) {
    float dist = length(coords - center);
    return 1.0 - saturate((dist - radius) / (1.0 - hardness + EPSILON));
}

// Distance — Euclidean  (Unity: Distance)
// GLSL built-in: distance(a, b), length(v)

// Transform Direction — normalize after matrix multiply  (no matrix here, helper)
vec3 transformDirection(mat4 m, vec3 dir) {
    return normalize((m * vec4(dir, 0.0)).xyz);
}

// ============================================================================
// Derivative / DDX / DDY  (Unity: DDX, DDY, DDXY)
// ============================================================================

// Partial derivative in screen-space X  (Unity: DDX)
float ddx(float v) { return dFdx(v); }
vec2  ddx(vec2  v) { return dFdx(v); }
vec3  ddx(vec3  v) { return dFdx(v); }

// Partial derivative in screen-space Y  (Unity: DDY)
float ddy(float v) { return dFdy(v); }
vec2  ddy(vec2  v) { return dFdy(v); }
vec3  ddy(vec3  v) { return dFdy(v); }

// Combined magnitude of DDX+DDY (for anti-aliasing, texel density)  (Unity: DDXY)
float ddxy(float v) { return abs(dFdx(v)) + abs(dFdy(v)); }
vec2  ddxy(vec2  v) { return abs(dFdx(v)) + abs(dFdy(v)); }

// ============================================================================
// Wave / Signal Generators  (Unity: Math > Wave category)
// ============================================================================

// Sawtooth Wave [0, 1]  (Unity: Sawtooth Wave)
float sawtoothWave(float x) { return fract(x); }

// Triangle Wave [0, 1]  (Unity: Triangle Wave)
float triangleWave(float x) { return abs(2.0 * fract(x) - 1.0); }

// Square Wave — 0.0 or 1.0  (Unity: Square Wave)
float squareWave(float x) { return step(0.5, fract(x)); }

// Noise Sine Wave — sine wave with random jitter  (Unity: Noise Sine Wave)
float noiseSineWave(float x, vec2 minMax) {
    float s = sin(x);
    float n = fract(sin(x * 12.9898 + 78.233) * 43758.5453);
    float noiseAmount = mix(minMax.x, minMax.y, n);
    return s + s * noiseAmount;
}

// ============================================================================
// Random  (Unity: Math > Random Range)
// ============================================================================

// Random float in [0, 1] from a seed  (Unity: Random Range seed aspect)
float randomFloat(vec2 seed) {
    return fract(sin(dot(seed, vec2(12.9898, 78.233))) * 43758.5453);
}

// Random float in [min, max] from a seed  (Unity: Random Range)
float randomRange(vec2 seed, float minVal, float maxVal) {
    return mix(minVal, maxVal, randomFloat(seed));
}

// ============================================================================
// Miscellaneous
// ============================================================================

// Degrees ↔ Radians  (Unity: Degrees To Radians, Radians To Degrees)
float degreesToRadians(float deg) { return deg * DEG2RAD; }
float radiansToDegrees(float rad) { return rad * RAD2DEG; }

// Absolute  (Unity: Absolute)
// GLSL built-in: abs(x)

// Ceiling / Floor  (Unity: Ceiling, Floor)
// GLSL built-in: ceil(x), floor(x)

// Sign  (Unity: Sign) — returns -1, 0, or 1
// GLSL built-in: sign(x)

// Min / Max / Clamp  (Unity: Minimum, Maximum, Clamp)
// GLSL built-in: min, max, clamp

// Lerp  (Unity: Lerp)
// GLSL built-in: mix(a, b, t)

// Smoothstep  (Unity: Smoothstep)
// GLSL built-in: smoothstep(edge0, edge1, x)

// Step  (Unity: Step)
// GLSL built-in: step(edge, x)

// Power  (Unity: Power)
// GLSL built-in: pow(base, exp)

// Log (natural) / Log2 / Log10  (Unity: Log)
// GLSL built-in: log(x), log2(x)
float log10(float x) { return log(x) * 0.43429448190; }
