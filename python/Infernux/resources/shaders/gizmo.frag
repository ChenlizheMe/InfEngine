#version 450

@shader_id: Gizmo
@hidden

@import: Lib Color

layout(location = 0) in vec3 fragColor;
layout(location = 0) out vec4 outColor;

void main() {
    // Gizmo vertex colors are authored in sRGB (editor constants); the scene
    // buffer is linear and gets sRGB-encoded by the display encode pass.
    outColor = vec4(sRGBToLinear(fragColor), 1.0);
}
