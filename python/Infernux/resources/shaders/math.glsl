ShaderInfo {
    Name "Math"
    Imports ["Lib Common"]
}

// ============================================================================
// math.glsl — Shared math constants and utility functions
//
// Constants (PI, INV_PI, HALF_PI, TWO_PI, EPSILON, FLT_MIN) and saturate()
// are provided by lib/common. This file adds legacy aliases and sky helpers.
// ============================================================================

// Legacy alias — existing code uses saturateVec3(x) instead of saturate(vec3)
vec3 saturateVec3(vec3 x) {
    return clamp(x, vec3(0.0), vec3(1.0));
}

// Shared sky gradient — used by both skybox_procedural.frag and
// sampleAmbientProbe(). Adjust constants here once.
//
// Uses a single continuous blend:
//   ground ──smoothstep──▶ equator ──smoothstep──▶ sky
//   nadir (-1)           horizon (0)              zenith (+1)
//
// SKY_EDGE / GROUND_EDGE control how far the equator band extends on each
// side of the horizon. The band is asymmetric on purpose:
//   - below the horizon it dies out quickly (narrow + hard cut to ground)
//   - above the horizon it fades wider and softer into the sky
vec3 skyGradient(float y, vec3 sky, vec3 equator, vec3 ground) {
    const float SKY_EDGE    = 0.45;   // horizon -> sky reach (wide, soft)
    const float GROUND_EDGE = 0.10;   // horizon -> ground reach (narrow, hard)
    const float EQUATOR_STRENGTH = 0.35;  // how much equator tints the horizon (0=none, 1=full)

    // Base: direct sky↔ground blend through the horizon
    float t = smoothstep(-GROUND_EDGE, SKY_EDGE, y);
    vec3 base = mix(ground, sky, t);

    // Equator band, per-side falloff:
    //  - ground side: short reach, sharpened (squared) for a hard transition
    //  - sky side: longer reach, plain smoothstep for a soft fade
    float horizonMask;
    if (y < 0.0) {
        float s = 1.0 - smoothstep(0.0, GROUND_EDGE, -y);
        horizonMask = s * s;
    } else {
        horizonMask = 1.0 - smoothstep(0.0, SKY_EDGE, y);
    }
    return mix(base, equator, horizonMask * EQUATOR_STRENGTH);
}
