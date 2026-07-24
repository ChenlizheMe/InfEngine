@shader_id: Lib Shapes

// ============================================================================
// lib/shapes.glsl — Procedural shape functions (SDF-based)
//
// Full-coverage shape toolkit matching Unity ShaderGraph Procedural category.
// Provides: checkerboard, circle, rounded rect, ring, polygon, star, grid,
// ellipse, line segment, cross, pie, hexagon, heart, and more.
// All functions operate on 2D UV coordinates.
// Usage: @import: Lib Shapes
// ============================================================================

// ============================================================================
// Pattern generators  (Unity: Checkerboard)
// ============================================================================

// Checkerboard  (Unity: Checkerboard)
float checkerboard(vec2 uv, float scale) {
    vec2 c = floor(uv * scale);
    return mod(c.x + c.y, 2.0);
}

// Checkerboard — two-color variant  (Unity: Checkerboard)
vec3 checkerboard(vec2 uv, vec3 colorA, vec3 colorB, float scale) {
    float t = checkerboard(uv, scale);
    return mix(colorA, colorB, t);
}

// Grid lines pattern
float gridLines(vec2 uv, float scale, float lineWidth) {
    vec2 grid = abs(fract(uv * scale - 0.5) - 0.5);
    vec2 line = smoothstep(vec2(0.0), vec2(lineWidth), grid);
    return 1.0 - min(line.x, line.y);
}

// ============================================================================
// Basic SDFs  (Unity: Ellipse, Rectangle, Rounded Rectangle)
// ============================================================================

// Circle SDF  (Unity: Ellipse with equal radii)
float circleSDF(vec2 uv, vec2 center, float radius) {
    float dist = length(uv - center);
    return 1.0 - smoothstep(radius - fwidth(dist), radius + fwidth(dist), dist);
}

// Ellipse SDF  (Unity: Ellipse)
float ellipseSDF(vec2 uv, vec2 center, vec2 radii) {
    vec2 p = (uv - center) / radii;
    float dist = length(p);
    float fw = fwidth(dist);
    return 1.0 - smoothstep(1.0 - fw, 1.0 + fw, dist);
}

// Rounded rectangle SDF  (Unity: Rounded Rectangle)
float roundedRectSDF(vec2 uv, vec2 center, vec2 halfSize, float radius) {
    vec2 d = abs(uv - center) - halfSize + radius;
    float dist = length(max(d, 0.0)) - radius;
    return 1.0 - smoothstep(-fwidth(dist), fwidth(dist), dist);
}

// Box SDF (sharp corners)  (Unity: Rectangle)
float boxSDF(vec2 uv, vec2 center, vec2 halfSize) {
    vec2 d = abs(uv - center) - halfSize;
    float dist = length(max(d, 0.0)) + min(max(d.x, d.y), 0.0);
    return 1.0 - smoothstep(-fwidth(dist), fwidth(dist), dist);
}

// ============================================================================
// Ring & Annular  (Unity: custom, common in effects)
// ============================================================================

// Ring shape — annular region between inner and outer radius
float ringShape(vec2 uv, vec2 center, float innerRadius, float outerRadius) {
    float dist = length(uv - center);
    float outer = 1.0 - smoothstep(outerRadius - fwidth(dist), outerRadius + fwidth(dist), dist);
    float inner = 1.0 - smoothstep(innerRadius - fwidth(dist), innerRadius + fwidth(dist), dist);
    return outer - inner;
}

// ============================================================================
// Polygon & Star  (Unity: Polygon)
// ============================================================================

// Regular polygon (N-sided) SDF  (Unity: Polygon)
float polygonSDF(vec2 uv, vec2 center, float radius, float sides) {
    vec2 p = uv - center;
    float angle = atan(p.y, p.x);
    float slice = 6.28318530718 / sides;
    float dist = cos(floor(0.5 + angle / slice) * slice - angle) * length(p);
    return 1.0 - smoothstep(radius - fwidth(dist), radius + fwidth(dist), dist);
}

// Star shape (N-pointed)
float starSDF(vec2 uv, vec2 center, float innerRadius, float outerRadius, float points) {
    vec2 p = uv - center;
    float angle = atan(p.y, p.x);
    float slice = 6.28318530718 / points;
    float halfSlice = slice * 0.5;
    float a = mod(angle, slice) - halfSlice;
    float r = mix(innerRadius, outerRadius, step(0.0, cos(a * points)));
    float dist = length(p);
    return 1.0 - smoothstep(r - fwidth(dist), r + fwidth(dist), dist);
}

// ============================================================================
// Additional shapes  (Unity ShaderGraph extension / common procedural)
// ============================================================================

// Line segment SDF (anti-aliased)
float lineSDF(vec2 uv, vec2 a, vec2 b, float thickness) {
    vec2 pa = uv - a;
    vec2 ba = b - a;
    float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
    float dist = length(pa - ba * h);
    return 1.0 - smoothstep(thickness - fwidth(dist), thickness + fwidth(dist), dist);
}

// Cross / plus shape
float crossSDF(vec2 uv, vec2 center, vec2 size, float thickness) {
    vec2 d = abs(uv - center);
    float h = 1.0 - smoothstep(thickness - fwidth(d.x), thickness + fwidth(d.x), d.x)
            * (1.0 - step(size.y, d.y));
    float v = 1.0 - smoothstep(thickness - fwidth(d.y), thickness + fwidth(d.y), d.y)
            * (1.0 - step(size.x, d.x));
    return max(h, v);
}

// Pie / sector (angular slice)
float pieSDF(vec2 uv, vec2 center, float radius, float startAngle, float endAngle) {
    vec2 d = uv - center;
    float dist = length(d);
    float angle = atan(d.y, d.x);
    float inside = step(startAngle, angle) * step(angle, endAngle);
    float circle = 1.0 - smoothstep(radius - fwidth(dist), radius + fwidth(dist), dist);
    return circle * inside;
}

// Hexagon SDF
float hexagonSDF(vec2 uv, vec2 center, float radius) {
    vec2 p = abs(uv - center);
    float dist = max(dot(p, vec2(0.866025, 0.5)), p.y);
    return 1.0 - smoothstep(radius - fwidth(dist), radius + fwidth(dist), dist);
}

// Triangle SDF (equilateral, pointing up)
float triangleSDF(vec2 uv, vec2 center, float radius) {
    vec2 p = uv - center;
    float q = max(abs(p.x) * 0.866025 + p.y * 0.5, -p.y);
    float dist = q - radius * 0.5;
    return 1.0 - smoothstep(-fwidth(dist), fwidth(dist), dist);
}

// Heart shape SDF (approximate)
float heartSDF(vec2 uv, vec2 center, float size) {
    vec2 p = (uv - center) / size;
    p.y -= 0.3;
    float a = atan(p.x, p.y) / 3.14159265359;
    float r = length(p);
    float h = abs(a);
    float d = (13.0 * h - 22.0 * h * h + 10.0 * h * h * h) / (6.0 - 5.0 * h);
    float dist = r - d;
    return 1.0 - smoothstep(-fwidth(dist) * 2.0, fwidth(dist) * 2.0, dist);
}

// ============================================================================
// Raw distance functions (unsigned, no anti-alias — for combining SDFs)
// ============================================================================

// Raw circle distance
float circleDistRaw(vec2 uv, vec2 center, float radius) {
    return length(uv - center) - radius;
}

// Raw box distance
float boxDistRaw(vec2 uv, vec2 center, vec2 halfSize) {
    vec2 d = abs(uv - center) - halfSize;
    return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0);
}

// Raw rounded rect distance
float roundedRectDistRaw(vec2 uv, vec2 center, vec2 halfSize, float radius) {
    vec2 d = abs(uv - center) - halfSize + radius;
    return length(max(d, 0.0)) - radius;
}

// ============================================================================
// SDF Operations  (Unity ShaderGraph: Boolean operations on shapes)
// ============================================================================

// Union (OR) of two SDF distances
float sdfUnion(float d1, float d2) {
    return min(d1, d2);
}

// Intersection (AND) of two SDF distances
float sdfIntersection(float d1, float d2) {
    return max(d1, d2);
}

// Subtraction (d1 minus d2)
float sdfSubtraction(float d1, float d2) {
    return max(d1, -d2);
}

// Smooth union
float sdfSmoothUnion(float d1, float d2, float k) {
    float h = clamp(0.5 + 0.5 * (d2 - d1) / k, 0.0, 1.0);
    return mix(d2, d1, h) - k * h * (1.0 - h);
}

// Convert raw SDF distance to anti-aliased mask
float sdfToMask(float dist) {
    return 1.0 - smoothstep(-fwidth(dist), fwidth(dist), dist);
}
