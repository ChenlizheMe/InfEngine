#version 450

struct ParticleInstance
{
    vec4 positionSize;
    vec4 color;
    vec4 rotationCustom;
};

layout(set = 0, binding = 0, std430) readonly buffer Instances { ParticleInstance instances[]; };

layout(push_constant) uniform ViewConstants
{
    mat4 viewProjection;
    vec4 cameraRight;
    vec4 cameraUp;
    vec4 materialTint;
} view;

layout(location = 0) out vec4 outColor;
layout(location = 1) out vec2 outUv;

const vec2 corners[6] = vec2[](
    vec2(-1.0, -1.0), vec2(-1.0, 1.0), vec2(1.0, 1.0),
    vec2(-1.0, -1.0), vec2(1.0, 1.0), vec2(1.0, -1.0)
);
const vec2 uvs[6] = vec2[](
    vec2(0.0, 1.0), vec2(0.0, 0.0), vec2(1.0, 0.0),
    vec2(0.0, 1.0), vec2(1.0, 0.0), vec2(1.0, 1.0)
);

void main()
{
    ParticleInstance instance = instances[gl_InstanceIndex];
    vec2 corner = corners[gl_VertexIndex % 6];
    vec3 worldPosition = instance.positionSize.xyz +
        (view.cameraRight.xyz * corner.x + view.cameraUp.xyz * corner.y) * instance.positionSize.w;
    gl_Position = view.viewProjection * vec4(worldPosition, 1.0);
    outColor = instance.color;
    outUv = uvs[gl_VertexIndex % 6];
}
