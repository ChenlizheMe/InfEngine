// ============================================================================
// vertex_builtins.glsl — Auto-injected vertex shader declarations
//
// Provides: UBO, push constants, vertex attributes, unified varyings,
//           and VertexInput struct for void vertex(inout VertexInput v).
// ============================================================================

layout(std140, binding = 0) uniform UniformBufferObject {
    mat4 model;
    mat4 view;
    mat4 proj;
    mat4 previousViewProj;
} ubo;

layout(push_constant) uniform PushConstants {
    mat4 model;
    mat4 normalMat;
} pc;

// Instance buffer — per-object model matrices for GPU instancing (set 2, binding 1)
layout(std430, set = 2, binding = 1) readonly buffer InstanceBuffer {
    mat4 instanceModels[];
};

struct SkinInstanceData {
    uint boneOffset;
    uint boneCount;
    uint flags;
    uint _pad;
};

// Skin metadata and bone palettes share the engine globals set so every
// material can render skinned and static meshes through the same pipeline.
layout(std430, set = 2, binding = 2) readonly buffer SkinInstanceBuffer {
    SkinInstanceData skinInstances[];
};

layout(std430, set = 2, binding = 3) readonly buffer SkinBonePaletteBuffer {
    mat4 skinBones[];
};

// Vertex attributes
layout(location = 0) in vec3 inPosition;
layout(location = 1) in vec3 inNormal;
layout(location = 2) in vec4 inTangent;
layout(location = 3) in vec3 inColor;
layout(location = 4) in vec2 inTexCoord;
layout(location = 5) in uvec4 inBoneIndices;
layout(location = 6) in vec4 inBoneWeights;

// Unified varyings — all shading models use the same set
layout(location = 0) out vec3 v_WorldPos;
layout(location = 1) out vec3 v_Normal;
layout(location = 2) out vec4 v_Tangent;
layout(location = 3) out vec3 v_Color;
layout(location = 4) out vec2 v_TexCoord;
layout(location = 5) out float v_ViewDepth;

// Vertex input structure for user-defined void vertex(inout VertexInput v)
struct VertexInput {
    vec3 position;
    vec3 normal;
    vec4 tangent;
    vec3 color;
    vec2 texCoord;
};
