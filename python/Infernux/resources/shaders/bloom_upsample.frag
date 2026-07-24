#version 450
@shader_id: Bloom Upsample
@hidden

// Bloom upsample pass — 9-tap tent filter
// Aligned with Unity URP's _BloomMipUp kernel.
//
// Uses a 3x3 tent (bilinear) filter for smooth upsampling.
// The scatter parameter controls how much of the lower-mip bloom
// bleeds into the higher-resolution mip (Unity's "Scatter" / diffusion).
//
// Texel size is computed from the source texture dimensions via textureSize().
// Push constants layout:
//   [0] scatter  — blend factor (0-1): how much lower-mip contributes

layout(set = 0, binding = 0) uniform sampler2D _SourceTex;  // lower-res mip (bloom)
layout(set = 0, binding = 1) uniform sampler2D _DestTex;    // higher-res mip (accumulator)

layout(push_constant) uniform PushConstants {
    float scatter;
} pc;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec2 texelSize = 1.0 / vec2(textureSize(_SourceTex, 0));

    // 9-tap tent filter (3x3 bilinear)
    //   1 2 1
    //   2 4 2  / 16
    //   1 2 1
    vec3 s;
    s  = texture(_SourceTex, inUV + vec2(-texelSize.x, -texelSize.y)).rgb;  // 1
    s += texture(_SourceTex, inUV + vec2( 0.0,         -texelSize.y)).rgb * 2.0; // 2
    s += texture(_SourceTex, inUV + vec2( texelSize.x, -texelSize.y)).rgb;  // 1
    s += texture(_SourceTex, inUV + vec2(-texelSize.x,  0.0        )).rgb * 2.0; // 2
    s += texture(_SourceTex, inUV).rgb * 4.0;                               // 4
    s += texture(_SourceTex, inUV + vec2( texelSize.x,  0.0        )).rgb * 2.0; // 2
    s += texture(_SourceTex, inUV + vec2(-texelSize.x,  texelSize.y)).rgb;  // 1
    s += texture(_SourceTex, inUV + vec2( 0.0,          texelSize.y)).rgb * 2.0; // 2
    s += texture(_SourceTex, inUV + vec2( texelSize.x,  texelSize.y)).rgb;  // 1
    s /= 16.0;

    // Blend with the higher-res accumulator using scatter factor
    vec3 highRes = texture(_DestTex, inUV).rgb;
    vec3 result = mix(highRes, s, pc.scatter);

    outColor = vec4(result, 1.0);
}
