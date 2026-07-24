#version 450
@shader_id: Display Encode
@hidden

@import: Lib Color

// Built-in display encode — the final image pass of every RenderStack
// pipeline (inserted automatically by the render graph).
//
// The swapchain and the editor viewport are UNORM surfaces without hardware
// sRGB encoding, so the graph explicitly encodes linear scene color to sRGB
// here. Scene rendering, post-process effects and tone mapping all operate
// in linear space; this pass is the single place where gamma is applied.

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec4 source = texture(_SourceTex, inUV);
    // Values above 1.0 (no tone mapping mounted) are clipped, matching what
    // a UNORM target would have done implicitly.
    vec3 ldr = clamp(source.rgb, 0.0, 1.0);
    outColor = vec4(linearToSRGB(ldr), source.a);
}
