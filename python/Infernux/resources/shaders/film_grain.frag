#version 450
@shader_id: Film Grain
@hidden

@import: Lib Utils

// Film Grain post-process — physically-inspired photographic grain.
//
// Real film grain differs from white noise in three ways, all modeled here:
//   1. Grains have a physical size: value noise sampled at a controllable
//      grain scale produces soft clumps instead of per-pixel static.
//   2. Grain is multiplicative: silver halide density modulates the exposed
//      image, so pure black stays clean and midtones carry the most grain
//      (URP uses the same input + input * grain formulation).
//   3. Grain advances in discrete film frames (24 fps steps), not every
//      rendered frame.
//
// Push constants:
//   [0] intensity — grain strength (0 = off, 1 = heavy)
//   [1] response  — luminance response (0 = uniform, 1 = shadows/mids only)
//   [2] size      — grain size in pixels (1 = fine, larger = coarser stock)
//   [3] colored   — 1.0 = per-channel (color negative), 0.0 = monochrome
//
// Time comes from the engine globals UBO (set 2), which the fullscreen
// pipeline always binds — push constants are only refreshed on parameter
// edits, so a push-constant clock would freeze the grain between rebuilds.

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(push_constant) uniform PushConstants {
    float intensity;
    float response;
    float size;
    float colored;
} pc;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

// Bilinearly-interpolated value noise: soft grain clumps with a physical
// size, unlike per-pixel hash which reads as digital static.
float inxFilmValueNoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash21(i);
    float b = hash21(i + vec2(1.0, 0.0));
    float c = hash21(i + vec2(0.0, 1.0));
    float d = hash21(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

// Two octaves approximate the density distribution of developed grain:
// large soft clumps modulated by finer structure. Output is roughly
// symmetric around 0 in [-1, 1].
float filmGrain(vec2 p, vec2 seed) {
    float coarse = inxFilmValueNoise(p + seed);
    float fine = inxFilmValueNoise(p * 2.7 + seed * 1.13 + vec2(41.7, 289.3));
    return (coarse * 0.65 + fine * 0.35) * 2.0 - 1.0;
}

void main() {
    vec4 color = texture(_SourceTex, inUV);

    vec2 texSize = vec2(textureSize(_SourceTex, 0));
    float grainSize = max(pc.size, 0.5);
    vec2 grainCoord = inUV * texSize / grainSize;

    // Advance the grain plate in discrete film frames (24 fps).
    float frame = floor(_Globals._Time.x * 24.0);
    vec2 seed = vec2(fract(frame * 0.1031) * 719.7, fract(frame * 0.11369) * 913.1);

    vec3 grain;
    if (pc.colored > 0.5) {
        // Independent grain per channel, like the three emulsion layers of
        // color negative stock.
        grain = vec3(
            filmGrain(grainCoord, seed),
            filmGrain(grainCoord + vec2(157.31, 63.09), seed),
            filmGrain(grainCoord + vec2(311.77, 201.53), seed));
    } else {
        grain = vec3(filmGrain(grainCoord, seed));
    }

    // Luminance response: film grain is most visible in the midtones and
    // fades in bright highlights (dense negative = grainy shadows on print,
    // but on positive display the perceptual result is mids/shadows).
    float luma = luminance(color.rgb);
    float response = mix(1.0, 1.0 - sqrt(clamp(luma, 0.0, 1.0)), pc.response);

    // Multiplicative application: black stays clean, exposure carries grain.
    color.rgb += color.rgb * grain * pc.intensity * response;
    color.rgb = max(color.rgb, vec3(0.0));

    outColor = color;
}
