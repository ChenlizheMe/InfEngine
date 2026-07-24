@shader_id: Lib Vertex Utils

// ============================================================================
// lib/vertex_utils.glsl — Vertex manipulation utilities
//
// Full-coverage vertex toolkit matching Unity ShaderGraph vertex-stage nodes.
// Provides: billboard, wind, displacement, wave, morph, and animation helpers.
// Usage: @import: Lib Vertex Utils
// ============================================================================

// ============================================================================
// Billboard  (Unity: custom billboard via vertex position override)
// ============================================================================

// Billboard — orient quad to face camera
vec3 billboardPosition(vec3 center, vec2 offset, vec3 cameraRight, vec3 cameraUp) {
    return center + cameraRight * offset.x + cameraUp * offset.y;
}

// Axis-locked billboard (rotates around Y axis only)
vec3 billboardAxisY(vec3 center, vec2 offset, vec3 cameraRight) {
    vec3 right = normalize(vec3(cameraRight.x, 0.0, cameraRight.z));
    vec3 up = vec3(0.0, 1.0, 0.0);
    return center + right * offset.x + up * offset.y;
}

// ============================================================================
// Displacement  (Unity: Position node manipulation)
// ============================================================================

// Vertex displacement along normal
vec3 displaceAlongNormal(vec3 position, vec3 normal, float amount) {
    return position + normal * amount;
}

// Displacement along arbitrary direction
vec3 displaceAlongDirection(vec3 position, vec3 direction, float amount) {
    return position + normalize(direction) * amount;
}

// Texture-based displacement along normal (height map)
vec3 heightDisplacement(vec3 position, vec3 normal, float heightSample, float scale, float offset) {
    return position + normal * (heightSample * scale + offset);
}

// ============================================================================
// Wind / Vegetation  (Unity: custom wind via vertex animation)
// ============================================================================

// Simple sine-based wind sway
vec3 windSway(vec3 position, float time, float frequency, float amplitude, float phase) {
    float sway = sin(position.x * frequency + time + phase) * amplitude;
    sway *= position.y;
    return vec3(sway, 0.0, sway * 0.5);
}

// Procedural wind (two-tier: trunk + leaf)
vec3 proceduralWind(vec3 position, vec3 objectCenter, float time,
                     vec2 windDir, float trunkFreq, float trunkAmp,
                     float leafFreq, float leafAmp) {
    float height = max(position.y - objectCenter.y, 0.0);
    float heightFactor = height * height;

    // Trunk sway
    float trunkPhase = dot(objectCenter.xz, windDir) * 0.1;
    float trunk = sin(time * trunkFreq + trunkPhase) * trunkAmp * heightFactor;

    // Leaf flutter
    float leafPhase = dot(position.xz, vec2(1.7, 2.3));
    float leaf = sin(time * leafFreq + leafPhase) * leafAmp * height;

    return vec3(windDir.x * trunk + leaf, 0.0, windDir.y * trunk + leaf * 0.5);
}

// ============================================================================
// Wave Deformation  (Unity: custom wave vertex animation)
// ============================================================================

// Sine wave displacement (for water-like effects)
vec3 sineWaveDisplacement(vec3 position, float time, vec2 direction, float frequency, float amplitude) {
    float phase = dot(position.xz, direction) * frequency + time;
    float height = sin(phase) * amplitude;
    return vec3(0.0, height, 0.0);
}

// Multi-direction Gerstner wave (single wave, for water)
vec3 gerstnerWave(vec3 position, float time, vec2 direction, float steepness,
                   float wavelength, out vec3 tangent, out vec3 binormal) {
    float k = 6.28318530718 / wavelength;
    float c = sqrt(9.8 / k);
    vec2 d = normalize(direction);
    float f = k * (dot(d, position.xz) - c * time);
    float a = steepness / k;

    tangent = vec3(
        1.0 - d.x * d.x * steepness * sin(f),
        d.x * steepness * cos(f),
        -d.x * d.y * steepness * sin(f)
    );
    binormal = vec3(
        -d.x * d.y * steepness * sin(f),
        d.y * steepness * cos(f),
        1.0 - d.y * d.y * steepness * sin(f)
    );

    return vec3(d.x * a * cos(f), a * sin(f), d.y * a * cos(f));
}

// ============================================================================
// Scale / Transform  (Unity: vertex transforms)
// ============================================================================

// Object-space scaling (for pulsing / breathing effects)
vec3 pulseScale(vec3 position, float time, float speed, float minScale, float maxScale) {
    float s = mix(minScale, maxScale, sin(time * speed) * 0.5 + 0.5);
    return position * s;
}

// Non-uniform scale
vec3 scalePosition(vec3 position, vec3 scaleXYZ) {
    return position * scaleXYZ;
}

// Rotate position around arbitrary axis (Rodrigues' rotation formula)
vec3 rotateAroundAxis(vec3 position, vec3 axis, float angle) {
    vec3 k = normalize(axis);
    float c = cos(angle);
    float s = sin(angle);
    return position * c + cross(k, position) * s + k * dot(k, position) * (1.0 - c);
}

// ============================================================================
// Morph / Blend Shape
// ============================================================================

// Linear blend of two vertex positions
vec3 morphPosition(vec3 base, vec3 target, float blend) {
    return mix(base, target, blend);
}

// Linear blend of two normals (re-normalized)
vec3 morphNormal(vec3 base, vec3 target, float blend) {
    return normalize(mix(base, target, blend));
}

// ============================================================================
// Vertex Color Masking
// ============================================================================

// Use vertex color channel as mask for vertex displacement
vec3 maskedDisplacement(vec3 position, vec3 normal, float displacement, float mask) {
    return position + normal * displacement * mask;
}

// ============================================================================
// Skinning / Skeleton helpers  (for custom skinning passes)
// ============================================================================

// Apply a single bone transform (4x4 matrix + weight)
vec3 applySingleBone(vec3 position, mat4 boneMatrix, float weight) {
    return (boneMatrix * vec4(position, 1.0)).xyz * weight;
}

// Squash and stretch along local Y axis (for cartoon animation)
vec3 squashStretch(vec3 position, float factor) {
    float invSqrt = 1.0 / sqrt(max(factor, 0.01));
    return vec3(position.x * invSqrt, position.y * factor, position.z * invSqrt);
}
