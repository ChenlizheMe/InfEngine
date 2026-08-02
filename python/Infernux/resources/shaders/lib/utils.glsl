ShaderInfo {
    Name "Lib Utils"
    Imports ["Lib Common", "Lib Color", "Lib Noise", "Lib Shapes", "Lib UV", "Lib Texture Utils", "Lib Lighting Utils", "Lib Vertex Utils"]
}

// ============================================================================
// lib/utils.glsl — General-purpose shader utility toolkit
//
// Aggregates all context-free utility libraries into a single import.
// No UBO or varying dependencies — works in ANY shader type
// (surface, fullscreen, post-processing, compute, etc.)
//
// Usage: ShaderInfo Imports: Lib Utils
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
// For individual imports, use the specific library (e.g. ShaderInfo Imports: Lib Noise).
// ============================================================================

// All constants (PI, EPSILON, etc.) and utility functions (saturate, etc.)
// are provided by lib/common — no duplicates needed here.
