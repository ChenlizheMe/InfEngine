ShaderInfo {
    Name "Lib Screen Utils"
    Imports ["Lib Camera", "Lib Depth"]
}

// ============================================================================
// lib/screen_utils.glsl — Screen-space & camera utility toolkit
//
// Aggregates screen-space and camera-related libraries.
// Requires: InfGlobals UBO (_Globals at set 2, binding 0).
//
// Usage: ShaderInfo Imports: Lib Screen Utils
//
// Includes:
//   lib/camera  — getScreenUV, getPixelCoord, getCameraPosition,
//                 getViewDirection, getCameraNear/Far,
//                 linearEyeDepth, linear01Depth,
//                 getTime, getDeltaTime, getSmoothDeltaTime
//   lib/depth   — depthFade, reconstructWorldPos,
//                 linearFog, exponentialFog, exponentialSquaredFog
//
// Note: These functions reference _Globals.* uniforms. In surface() shaders
//       the UBO is auto-injected. For post-processing / fullscreen shaders
//       with custom layout declarations, ensure the InfGlobals UBO is
//       declared and the descriptor set is bound by the pipeline.
// ============================================================================
