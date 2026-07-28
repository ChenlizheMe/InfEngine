// Engine-owned geometry inputs for a vertex stage with an explicit main().
// User-defined outputs are emitted from ShaderInfo after this block.

layout(std140, set = 1, binding = 5) uniform UniformBufferObject {
    mat4 model;
    mat4 view;
    mat4 proj;
    mat4 previousViewProj;
} ubo;

layout(push_constant) uniform PushConstants {
    mat4 model;
    mat4 normalMat;
} pc;

layout(std430, set = 2, binding = 1) readonly buffer InstanceBuffer {
    mat4 instanceModels[];
};

struct SkinInstanceData {
    uint boneOffset;
    uint boneCount;
    uint flags;
    uint _pad;
};

layout(std430, set = 2, binding = 2) readonly buffer SkinInstanceBuffer {
    SkinInstanceData skinInstances[];
};

layout(std430, set = 2, binding = 3) readonly buffer SkinBonePaletteBuffer {
    mat4 skinBones[];
};

layout(location = 0) in vec3 inPosition;
layout(location = 1) in vec3 inNormal;
layout(location = 2) in vec4 inTangent;
layout(location = 3) in vec3 inColor;
layout(location = 4) in vec2 inTexCoord;
layout(location = 5) in uvec4 inBoneIndices;
layout(location = 6) in vec4 inBoneWeights;

struct VertexInput {
    vec3 position;
    vec3 normal;
    vec4 tangent;
    vec3 color;
    vec2 texCoord;
};

struct InstanceAuxData {
    mat4 previousModel;
    uvec2 objectId;
    uint flags;
    uint layerMask;
};

layout(std430, set = 2, binding = 4) readonly buffer InstanceAuxBuffer {
    InstanceAuxData instanceAuxData[];
};
