#version 450
@shader_id: Route Additive Delta
@hidden

layout(set = 0, binding = 0) uniform sampler2D _OriginalTex;
layout(set = 0, binding = 1) uniform sampler2D _ProcessedTex;

layout(location = 0) in vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec3 original = texture(_OriginalTex, inUV).rgb;
    vec3 processed = texture(_ProcessedTex, inUV).rgb;
    outColor = vec4(max(processed - original, vec3(0.0)), 0.0);
}
