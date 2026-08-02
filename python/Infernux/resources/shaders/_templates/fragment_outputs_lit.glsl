// ============================================================================
// fragment_outputs_lit.glsl — Lit fragment shader output declarations
//
// Forward rendering: single color output + GetMainLight convenience macro.
// Deferred rendering uses the separate GBuffer compile target and its
// canonical five-target output contract.
// ============================================================================

layout(location = 0) out vec4 outColor;

// Convenience macro — wraps getMainLight() with auto-injected varyings
#define GetMainLight() getMainLight(v_WorldPos, (gl_FrontFacing ? normalize(v_Normal) : -normalize(v_Normal)), v_ViewDepth)
