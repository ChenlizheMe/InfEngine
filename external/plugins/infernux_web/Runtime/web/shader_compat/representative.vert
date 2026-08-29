#version 450

layout(push_constant) uniform DrawConstants
{
    mat4 modelViewProjection;
    vec4 tint;
} drawConstants;

layout(location = 0) in vec3 inPosition;
layout(location = 0) out vec2 outUv;

void main()
{
    gl_Position = drawConstants.modelViewProjection * vec4(inPosition, 1.0);
    outUv = inPosition.xy * 0.5 + vec2(0.5);
}
