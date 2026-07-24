@shader_id: Lib Object Utils

@import: Lib Surface Utils

// ============================================================================
// lib/object_utils.glsl — Per-object fragment data toolkit
//
// Provides access to interpolated vertex data and per-object fragment helpers.
// Requires: fragment varyings (v_WorldPos, v_Normal, v_Tangent, v_Color,
//           v_TexCoord, v_ViewDepth) + InfGlobals UBO.
//
// Usage: @import: Lib Object Utils
//
// Includes (via lib/surface_utils → lib/normal_utils, lib/camera, lib/common):
//
//   Fragment inputs:
//     getWorldPosition()         — world-space fragment position
//     getWorldNormal()           — normalized interpolated normal
//     getWorldTangent()          — world-space tangent (w = bitangent sign)
//     getVertexColor()           — vertex color (linear)
//     getUV()                    — primary UV coordinates
//     getViewDepth()             — linear eye-space depth
//
//   View & camera:
//     getViewDir()               — view direction (frag → camera)
//     getCameraDistance()         — distance from fragment to camera
//     getFresnel()               — Schlick fresnel (F0 = 0.04)
//     getFresnelF0(f0)           — Schlick fresnel with custom F0
//
//   Normal mapping:
//     sampleNormal(map, uv, scale)  — sample normal map → world-space
//     sampleNormal(map, scale)      — same, using primary UV
//     sampleNormalFromHeight(...)    — height-map → world-space normal
//
//   From lib/camera:
//     getScreenUV(), getPixelCoord(), getCameraPosition(),
//     getViewDirection(worldPos), getCameraNear(), getCameraFar(),
//     linearEyeDepth(rawDepth), linear01Depth(rawDepth),
//     getTime(), getDeltaTime(), getSmoothDeltaTime()
//
//   From lib/common:
//     remap, inverseLerp, remapClamped, sq, luminance, safeNormalize
//
//   From lib/normal_utils:
//     constructTBN, normalFromTangentSpace, getNormalFromMap,
//     normalFromHeight, normalFromHeightWS
//
// Designed for surface() shaders. Auto-injected by the engine when a
// surface() function is detected (replaces the former lib/surface_utils).
// ============================================================================
