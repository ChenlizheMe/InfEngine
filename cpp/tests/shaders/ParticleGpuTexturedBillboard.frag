#version 450

layout(location = 0) in vec4 inColor;
layout(location = 1) in vec2 inUv;
layout(location = 0) out vec4 outColor;

layout(set = 2, binding = 2) uniform sampler2D texSampler;

layout(push_constant) uniform ViewConstants
{
    mat4 viewProjection;
    mat4 previousViewProjection;
    vec4 cameraRight;
    vec4 cameraUp;
    vec4 materialTint;
    vec4 depthReconstruct;
    vec4 lightingControl;
    vec4 renderingControl;
    vec4 alignmentReference;
} view;

void main()
{
    outColor = texture(texSampler, inUv) * inColor * view.materialTint;
}
