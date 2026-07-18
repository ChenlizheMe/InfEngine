#version 450

layout(location = 0) in vec4 inColor;
layout(location = 1) in vec2 inUv;
layout(location = 0) out vec4 outColor;

layout(set = 0, binding = 1) uniform sampler2D texSampler;

layout(push_constant) uniform ViewConstants
{
    mat4 viewProjection;
    vec4 cameraRight;
    vec4 cameraUp;
    vec4 materialTint;
} view;

void main()
{
    outColor = texture(texSampler, inUv) * inColor * view.materialTint;
}
