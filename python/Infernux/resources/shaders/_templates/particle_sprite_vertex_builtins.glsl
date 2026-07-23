struct ParticleInstance {
    vec4 position_size;
    vec4 color;
    vec4 rotation_custom;
};

layout(set = 0, binding = 0, std430) readonly buffer ParticleInstances {
    ParticleInstance instances[];
};

layout(set = 0, binding = 1, std430) readonly buffer ParticleDrawIndices {
    uint draw_indices[];
};

layout(push_constant) uniform ParticleViewConstants {
    mat4 view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
} particleView;

layout(location = 0) out vec3 v_WorldPos;
layout(location = 1) out vec3 v_Normal;
layout(location = 2) out vec4 v_Tangent;
layout(location = 3) out vec3 v_Color;
layout(location = 4) out vec2 v_TexCoord;
layout(location = 5) out float v_ViewDepth;
layout(location = 14) out float v_ParticleAlpha;
layout(location = 15) flat out uint _inx_ObjectLayerMask;

struct VertexInput {
    vec3 position;
    vec3 normal;
    vec4 tangent;
    vec3 color;
    vec2 texCoord;
};
