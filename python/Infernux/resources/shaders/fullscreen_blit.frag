#version 450
@shader_id: Fullscreen Blit
@hidden

// Simple pass-through blit shader for fullscreen copy operations.
// Samples the source texture and outputs it unchanged.

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    outColor = texture(_SourceTex, inUV);
}
