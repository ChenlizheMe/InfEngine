// ============================================================================
// fragment_varyings.glsl — Unified fragment varying inputs
//
// All shading models receive the same set of varyings from the vertex shader.
// Unlit shaders may ignore unused varyings — the GPU optimizes them away.
// ============================================================================

layout(location = 0) in vec3 v_WorldPos;
layout(location = 1) in vec3 v_Normal;
layout(location = 2) in vec4 v_Tangent;
layout(location = 3) in vec3 v_Color;
layout(location = 4) in vec2 v_TexCoord;
layout(location = 5) in float v_ViewDepth;
layout(location = 6) in vec4 v_LineColor;
