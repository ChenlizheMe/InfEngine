// ============================================================================
// shadow_vertex_builtins.glsl — Shadow pass vertex shader declarations
//
// Uses the shadow UBO (set 0 binding 0) with light view/proj instead of
// the camera UBO + InfGlobals.  Push constants and vertex attributes
// are identical to the forward pass so vertex() functions work unchanged.
// ============================================================================

layout(std140, binding = 0) uniform ShadowUBO {
    mat4 _unused_model;
    mat4 view;
    mat4 proj;
    vec4 light_vector; // xyz = toward-light direction or local-light position, w = position flag
    vec4 bias;         // xy = depth/normal bias in texels, z = world texel size, w = far plane
} shadowUBO;

layout(push_constant) uniform PushConstants {
    mat4 model;
    mat4 normalMat;
} pc;

// Instance buffer — per-object model matrices for GPU instancing (set 1, binding 1)
layout(std430, set = 1, binding = 1) readonly buffer InstanceBuffer {
    mat4 instanceModels[];
};

struct SkinInstanceData {
    uint boneOffset;
    uint boneCount;
    uint flags;
    uint previousBoneOffset;
};

layout(std430, set = 1, binding = 2) readonly buffer SkinInstanceBuffer {
    SkinInstanceData skinInstances[];
};

layout(std430, set = 1, binding = 3) readonly buffer SkinBonePaletteBuffer {
    mat4 skinBones[];
};

// Vertex attributes (same layout as forward pass)
layout(location = 0) in vec3 inPosition;
layout(location = 1) in vec3 inNormal;
layout(location = 2) in vec4 inTangent;
layout(location = 3) in vec3 inColor;
layout(location = 4) in vec2 inTexCoord;
layout(location = 5) in uvec4 inBoneIndices;
layout(location = 6) in vec4 inBoneWeights;

// Unified varyings — must match fragment_varyings.glsl for interface compatibility
layout(location = 0) out vec3 v_WorldPos;
layout(location = 1) out vec3 v_Normal;
layout(location = 2) out vec4 v_Tangent;
layout(location = 3) out vec3 v_Color;
layout(location = 4) out vec2 v_TexCoord;
layout(location = 5) out float v_ViewDepth;
layout(location = 6) out vec4 v_LineColor;

// Vertex input structure (same as forward — user vertex() functions work unchanged)
struct VertexInput {
    vec3 position;
    vec3 normal;
    vec4 tangent;
    vec3 color;
    vec2 texCoord;
};
