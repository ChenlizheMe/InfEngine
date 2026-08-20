#version 450 core

layout(location = 0) out vec4 fColor;
layout(set = 0, binding = 0) uniform sampler2D sTexture;
layout(push_constant) uniform uPushConstant
{
    layout(offset = 16) int uLinearColor;
} pc;
layout(location = 0) in struct
{
    vec4 Color;
    vec2 UV;
} In;

vec3 linear_to_srgb(vec3 value)
{
    bvec3 low_range = lessThanEqual(value, vec3(0.0031308));
    vec3 low = value * 12.92;
    vec3 high = 1.055 * pow(max(value, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055;
    return mix(high, low, low_range);
}

void main()
{
    vec4 sampled = texture(sTexture, In.UV.st);
    if (pc.uLinearColor != 0)
        sampled.rgb = linear_to_srgb(sampled.rgb);
    fColor = In.Color * sampled;
}
