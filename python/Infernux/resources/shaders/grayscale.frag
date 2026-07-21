#version 450
@shader_id: grayscale
@hidden

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(push_constant) uniform GrayscaleParams {
    float intensity;
} pc;

layout(location = 0) in vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec4 source = texture(_SourceTex, inUV);
    float luminance = dot(source.rgb, vec3(0.2126, 0.7152, 0.0722));
    outColor = vec4(mix(source.rgb, vec3(luminance), pc.intensity), source.a);
}
