#version 450

@shader_id: Skybox Procedural
@cull: back
@depth_write: false
@depth_test: less_equal

layout(std140, binding = 0) uniform UniformBufferObject {
    mat4 model;
    mat4 view;
    mat4 proj;
} ubo;

layout(push_constant) uniform PushConstants {
    mat4 model;
    mat4 normalMat;
} pc;

layout(location = 0) in vec3 inPosition;
layout(location = 1) in vec3 inNormal;
layout(location = 2) in vec4 inTangent;
layout(location = 3) in vec3 inColor;
layout(location = 4) in vec2 inTexCoord;

layout(location = 0) out vec3 fragWorldDir;

void main() {
    // Strip translation from view matrix (skybox centered on camera)
    mat4 viewNoTranslation = mat4(mat3(ubo.view));

    // inPosition is a unit cube vertex — its direction IS the world-space
    // direction we want for sky gradient / sun disc evaluation.
    fragWorldDir = inPosition;

    // Detect orthographic projection: proj[2][3] is -1 for perspective, 0 for ortho
    bool isOrtho = (abs(ubo.proj[2][3]) < 0.5);

    vec3 pos = inPosition;
    if (isOrtho) {
        // In ortho mode the unit cube may be smaller than the viewport.
        // Scale it so it always fills the screen.  proj[0][0] = 2/width,
        // proj[1][1] = 2/height (Vulkan Y-flipped).
        float halfW = 1.0 / abs(ubo.proj[0][0]);
        float halfH = 1.0 / abs(ubo.proj[1][1]);
        float s     = max(halfW, halfH) * 1.8;   // margin for rotation
        pos *= s;
    }

    // Transform the skybox cube
    vec4 clipPos = ubo.proj * viewNoTranslation * vec4(pos, 1.0);

    // Set z = w so depth is always at the far plane (1.0 after perspective divide)
    // Combined with depth test <= and no depth write, skybox renders behind everything
    gl_Position = clipPos.xyww;
}
