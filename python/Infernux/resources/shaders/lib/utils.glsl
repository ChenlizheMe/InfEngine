@shader_id: Lib Utils

@import: Lib Common
@import: Lib Color
@import: Lib Noise
@import: Lib Shapes
@import: Lib UV
@import: Lib Texture Utils
@import: Lib Lighting Utils
@import: Lib Vertex Utils

// ============================================================================
// lib/utils.glsl — General-purpose shader utility toolkit
//
// Aggregates all context-free utility libraries into a single import.
// No UBO or varying dependencies — works in ANY shader type
// (surface, fullscreen, post-processing, compute, etc.)
//
// Usage: @import: Lib Utils
//
// Includes:
//   lib/common          — constants, remap, saturate, comparison, wave, etc.
//   lib/color           — sRGB, HSV, HSL, brightness, contrast, blend modes
//   lib/noise           — hash, value/gradient/simplex noise, fbm, voronoi
//   lib/shapes          — SDF: circle, ellipse, rect, ring, polygon, star, etc.
//   lib/uv              — tiling, rotation, flipbook, parallax, triplanar, etc.
//   lib/texture_utils   — normal blending, detail blend, height blend, LOD, etc.
//   lib/lighting_utils  — fresnel, GGX, Cook-Torrance, rim, attenuation, SSS
//   lib/vertex_utils    — billboard, wind, displacement, Gerstner wave, morph
//
// For individual imports, use the specific library (e.g. @import: Lib Noise).
// ============================================================================

// All constants (PI, EPSILON, etc.) and utility functions (saturate, etc.)
// are provided by lib/common — no duplicates needed here.
