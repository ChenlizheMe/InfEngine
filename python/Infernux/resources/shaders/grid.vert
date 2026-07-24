#version 450

@shader_id: Grid
@cull: none
@hidden
@capability: NoMotionVectors

layout(std140, binding = 0) uniform UniformBufferObject {
    mat4 model;
    mat4 view;
    mat4 proj;
} ubo;

// Kept for compatibility with the existing draw path, which pushes model and
// normal matrices for mesh-style draw calls.
layout(push_constant) uniform PushConstants {
    mat4 model;
    mat4 normalMat;
} pc;

layout(location = 0) in vec3 inPosition;
layout(location = 1) in vec3 inNormal;
layout(location = 2) in vec4 inTangent;
layout(location = 3) in vec3 inColor;
layout(location = 4) in vec2 inTexCoord;

layout(location = 0) out vec3 nearPoint;
layout(location = 1) out vec3 farPoint;

vec3 unprojectPoint(vec2 ndc, float z) {
    vec4 unprojected = inverse(ubo.view) * inverse(ubo.proj) * vec4(ndc, z, 1.0);
    return unprojected.xyz / unprojected.w;
}

void main() {
    vec2 ndc = inPosition.xy;

    nearPoint = unprojectPoint(ndc, 0.0);
    farPoint = unprojectPoint(ndc, 1.0);

    gl_Position = vec4(ndc, 0.0, 1.0);
}
