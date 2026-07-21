#version 450
@shader_id: route_additive_composite
@hidden

layout(set = 0, binding = 0) uniform sampler2D _BaseTex;
layout(set = 0, binding = 1) uniform sampler2D _AdditiveTex;

layout(location = 0) in vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec4 base = texture(_BaseTex, inUV);
    vec3 additive = texture(_AdditiveTex, inUV).rgb;
    outColor = vec4(base.rgb + additive, base.a);
}
