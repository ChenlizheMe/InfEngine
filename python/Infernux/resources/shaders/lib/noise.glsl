ShaderInfo {
    Name "Lib Noise"
}

// ============================================================================
// lib/noise.glsl — Procedural noise functions
//
// Full-coverage noise library matching Unity ShaderGraph node library.
// Provides: hash, value noise (2D/3D), gradient/Perlin noise (2D/3D),
// simplex noise (2D/3D), Voronoi with cells (2D/3D), FBM, domain warp.
// Usage: ShaderInfo Imports: Lib Noise
// ============================================================================

// ============================================================================
// Hash Functions
// ============================================================================

float hash11(float p) {
    p = fract(p * 0.1031);
    p *= p + 33.33;
    p *= p + p;
    return fract(p);
}

float hash21(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

float hash31(vec3 p) {
    p = fract(p * 0.1031);
    p += dot(p, p.yzx + 33.33);
    return fract((p.x + p.y) * p.z);
}

vec2 hash22(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * vec3(0.1031, 0.1030, 0.0973));
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.xx + p3.yz) * p3.zy);
}

vec3 hash33(vec3 p3) {
    p3 = fract(p3 * vec3(0.1031, 0.1030, 0.0973));
    p3 += dot(p3, p3.yxz + 33.33);
    return fract((p3.xxy + p3.yxx) * p3.zyx);
}

vec2 hash32(vec3 p) {
    vec3 p3 = fract(p * vec3(0.1031, 0.1030, 0.0973));
    p3 += dot(p3, p3.yxz + 33.33);
    return fract((p3.xx + p3.yz) * p3.zy);
}

// ============================================================================
// Value Noise  (Unity: Simple Noise)
// ============================================================================

// 2D  (Unity: Simple Noise)
float valueNoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f); // Hermite smoothstep

    float a = hash21(i);
    float b = hash21(i + vec2(1.0, 0.0));
    float c = hash21(i + vec2(0.0, 1.0));
    float d = hash21(i + vec2(1.0, 1.0));

    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

// 3D
float valueNoise(vec3 p) {
    vec3 i = floor(p);
    vec3 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);

    float n000 = hash31(i);
    float n100 = hash31(i + vec3(1.0, 0.0, 0.0));
    float n010 = hash31(i + vec3(0.0, 1.0, 0.0));
    float n110 = hash31(i + vec3(1.0, 1.0, 0.0));
    float n001 = hash31(i + vec3(0.0, 0.0, 1.0));
    float n101 = hash31(i + vec3(1.0, 0.0, 1.0));
    float n011 = hash31(i + vec3(0.0, 1.0, 1.0));
    float n111 = hash31(i + vec3(1.0, 1.0, 1.0));

    float nx00 = mix(n000, n100, f.x);
    float nx10 = mix(n010, n110, f.x);
    float nx01 = mix(n001, n101, f.x);
    float nx11 = mix(n011, n111, f.x);
    float nxy0 = mix(nx00, nx10, f.y);
    float nxy1 = mix(nx01, nx11, f.y);
    return mix(nxy0, nxy1, f.z);
}

// ============================================================================
// Gradient / Perlin Noise  (Unity: Gradient Noise)
// ============================================================================

// 2D  (Unity: Gradient Noise)
vec2 gradientDir(vec2 p) {
    p = mod(p, 289.0);
    float x = mod((34.0 * p.x + 1.0) * p.x, 289.0);
    x = mod((34.0 * (x + p.y) + 1.0) * (x + p.y), 289.0);
    x = fract(x / 41.0) * 2.0 - 1.0;
    return normalize(vec2(x, abs(x) - 0.5));
}

float gradientNoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0); // Quintic Hermite

    return mix(
        mix(dot(gradientDir(i + vec2(0.0, 0.0)), f - vec2(0.0, 0.0)),
            dot(gradientDir(i + vec2(1.0, 0.0)), f - vec2(1.0, 0.0)), u.x),
        mix(dot(gradientDir(i + vec2(0.0, 1.0)), f - vec2(0.0, 1.0)),
            dot(gradientDir(i + vec2(1.0, 1.0)), f - vec2(1.0, 1.0)), u.x),
        u.y);
}

// 3D Gradient Noise
float gradientNoise(vec3 p) {
    vec3 i = floor(p);
    vec3 f = fract(p);
    vec3 u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);

    float n000 = dot(hash33(i) * 2.0 - 1.0, f);
    float n100 = dot(hash33(i + vec3(1, 0, 0)) * 2.0 - 1.0, f - vec3(1, 0, 0));
    float n010 = dot(hash33(i + vec3(0, 1, 0)) * 2.0 - 1.0, f - vec3(0, 1, 0));
    float n110 = dot(hash33(i + vec3(1, 1, 0)) * 2.0 - 1.0, f - vec3(1, 1, 0));
    float n001 = dot(hash33(i + vec3(0, 0, 1)) * 2.0 - 1.0, f - vec3(0, 0, 1));
    float n101 = dot(hash33(i + vec3(1, 0, 1)) * 2.0 - 1.0, f - vec3(1, 0, 1));
    float n011 = dot(hash33(i + vec3(0, 1, 1)) * 2.0 - 1.0, f - vec3(0, 1, 1));
    float n111 = dot(hash33(i + vec3(1, 1, 1)) * 2.0 - 1.0, f - vec3(1, 1, 1));

    float nx00 = mix(n000, n100, u.x);
    float nx10 = mix(n010, n110, u.x);
    float nx01 = mix(n001, n101, u.x);
    float nx11 = mix(n011, n111, u.x);
    float nxy0 = mix(nx00, nx10, u.y);
    float nxy1 = mix(nx01, nx11, u.y);
    return mix(nxy0, nxy1, u.z);
}

// ============================================================================
// Simplex Noise  (Unity: not directly, but commonly used)
// ============================================================================

// 2D Simplex
float simplexNoise(vec2 p) {
    const float K1 = 0.366025404; // (sqrt(3) - 1) / 2
    const float K2 = 0.211324865; // (3 - sqrt(3)) / 6

    vec2 i = floor(p + (p.x + p.y) * K1);
    vec2 a = p - i + (i.x + i.y) * K2;
    float m = step(a.y, a.x);
    vec2 o = vec2(m, 1.0 - m);
    vec2 b = a - o + K2;
    vec2 c = a - 1.0 + 2.0 * K2;

    vec3 h = max(0.5 - vec3(dot(a, a), dot(b, b), dot(c, c)), 0.0);
    vec3 n = h * h * h * h * vec3(
        dot(a, hash22(i) * 2.0 - 1.0),
        dot(b, hash22(i + o) * 2.0 - 1.0),
        dot(c, hash22(i + 1.0) * 2.0 - 1.0));

    return dot(n, vec3(70.0));
}

// 3D Simplex (Ashima/webgl-noise approximation)
float simplexNoise(vec3 v) {
    const vec2 C = vec2(1.0 / 6.0, 1.0 / 3.0);
    vec3 i = floor(v + dot(v, vec3(C.y)));
    vec3 x0 = v - i + dot(i, vec3(C.x));
    vec3 g = step(x0.yzx, x0.xyz);
    vec3 l = 1.0 - g;
    vec3 i1 = min(g, l.zxy);
    vec3 i2 = max(g, l.zxy);
    vec3 x1 = x0 - i1 + C.x;
    vec3 x2 = x0 - i2 + C.y;
    vec3 x3 = x0 - 0.5;
    vec4 w = max(0.6 - vec4(dot(x0,x0), dot(x1,x1), dot(x2,x2), dot(x3,x3)), 0.0);
    w = w * w * w * w;
    return dot(w, vec4(
        dot(x0, hash33(i) * 2.0 - 1.0),
        dot(x1, hash33(i + i1) * 2.0 - 1.0),
        dot(x2, hash33(i + i2) * 2.0 - 1.0),
        dot(x3, hash33(i + 1.0) * 2.0 - 1.0)
    )) * 52.0;
}

// ============================================================================
// Voronoi / Worley Noise  (Unity: Voronoi)
// ============================================================================

// 2D Voronoi — returns distance to nearest cell edge  (Unity: Voronoi)
float voronoiNoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    float minDist = 1.0;
    for (int y = -1; y <= 1; ++y) {
        for (int x = -1; x <= 1; ++x) {
            vec2 neighbor = vec2(float(x), float(y));
            vec2 point = hash22(i + neighbor);
            vec2 diff = neighbor + point - f;
            minDist = min(minDist, length(diff));
        }
    }
    return minDist;
}

// 2D Voronoi with cell ID output  (Unity: Voronoi — Cells output)
float voronoiNoise(vec2 p, out float cellID) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    float minDist = 1.0;
    vec2 closestCell = vec2(0.0);
    for (int y = -1; y <= 1; ++y) {
        for (int x = -1; x <= 1; ++x) {
            vec2 neighbor = vec2(float(x), float(y));
            vec2 cell = i + neighbor;
            vec2 point = hash22(cell);
            vec2 diff = neighbor + point - f;
            float d = length(diff);
            if (d < minDist) {
                minDist = d;
                closestCell = cell;
            }
        }
    }
    cellID = hash21(closestCell);
    return minDist;
}

// 3D Voronoi  (Unity: Voronoi 3D via Sub Graph)
float voronoiNoise(vec3 p) {
    vec3 i = floor(p);
    vec3 f = fract(p);
    float minDist = 1.0;
    for (int z = -1; z <= 1; ++z) {
        for (int y = -1; y <= 1; ++y) {
            for (int x = -1; x <= 1; ++x) {
                vec3 neighbor = vec3(float(x), float(y), float(z));
                vec3 point = hash33(i + neighbor);
                vec3 diff = neighbor + point - f;
                minDist = min(minDist, length(diff));
            }
        }
    }
    return minDist;
}

// 3D Voronoi with cell ID output
float voronoiNoise(vec3 p, out float cellID) {
    vec3 i = floor(p);
    vec3 f = fract(p);
    float minDist = 1.0;
    vec3 closestCell = vec3(0.0);
    for (int z = -1; z <= 1; ++z) {
        for (int y = -1; y <= 1; ++y) {
            for (int x = -1; x <= 1; ++x) {
                vec3 neighbor = vec3(float(x), float(y), float(z));
                vec3 cell = i + neighbor;
                vec3 point = hash33(cell);
                vec3 diff = neighbor + point - f;
                float d = length(diff);
                if (d < minDist) {
                    minDist = d;
                    closestCell = cell;
                }
            }
        }
    }
    cellID = hash31(closestCell);
    return minDist;
}

// ============================================================================
// Fractional Brownian Motion  (Unity: via stacked Simple/Gradient Noise)
// ============================================================================

// 2D FBM with gradient noise
float fbm(vec2 p, int octaves) {
    float value = 0.0;
    float amplitude = 0.5;
    float frequency = 1.0;
    for (int i = 0; i < octaves; ++i) {
        value += amplitude * gradientNoise(p * frequency);
        frequency *= 2.0;
        amplitude *= 0.5;
    }
    return value;
}

// 3D FBM with gradient noise
float fbm(vec3 p, int octaves) {
    float value = 0.0;
    float amplitude = 0.5;
    float frequency = 1.0;
    for (int i = 0; i < octaves; ++i) {
        value += amplitude * gradientNoise(p * frequency);
        frequency *= 2.0;
        amplitude *= 0.5;
    }
    return value;
}

// FBM with custom lacunarity and gain (persistence)
float fbm(vec2 p, int octaves, float lacunarity, float gain) {
    float value = 0.0;
    float amplitude = 0.5;
    float frequency = 1.0;
    for (int i = 0; i < octaves; ++i) {
        value += amplitude * gradientNoise(p * frequency);
        frequency *= lacunarity;
        amplitude *= gain;
    }
    return value;
}

// ============================================================================
// Domain Warp  (useful for organic distortion effects)
// ============================================================================

// Warp UV coordinates using noise offset
vec2 domainWarp(vec2 p, float strength, float frequency) {
    float nx = gradientNoise(p * frequency);
    float ny = gradientNoise(p * frequency + vec2(5.2, 1.3));
    return p + vec2(nx, ny) * strength;
}

vec3 domainWarp(vec3 p, float strength, float frequency) {
    float nx = gradientNoise(p * frequency);
    float ny = gradientNoise(p * frequency + vec3(5.2, 1.3, 9.7));
    float nz = gradientNoise(p * frequency + vec3(3.1, 7.4, 2.8));
    return p + vec3(nx, ny, nz) * strength;
}
