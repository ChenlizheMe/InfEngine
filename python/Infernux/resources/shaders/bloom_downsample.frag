#version 450

ShaderInfo {
    Name "Bloom Downsample"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D _SourceTex
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

// Bloom downsample pass — 13-tap downsample filter
// Aligned with Unity URP's _BloomMipDown kernel.
//
// Uses a 13-tap pattern (Jimenez 2014, "Next Generation Post Processing in
// Call of Duty: Advanced Warfare") to avoid aliasing when downsampling by 2x.
//
// Texel size is computed from the source texture dimensions via textureSize().
// No push constants required.

void main() {
    vec2 texelSize = 1.0 / vec2(textureSize(_SourceTex, 0));

    // 13-tap downsample (Jimenez 2014)
    //
    //   a - b - c
    //   - j - k -
    //   d - e - f
    //   - l - m -
    //   g - h - i
    //
    // Each letter represents a bilinear tap. Weighted sum:
    //   e * 0.125
    //   (a+c+g+i) * 0.03125
    //   (b+d+f+h) * 0.0625
    //   (j+k+l+m) * 0.125

    vec3 a = texture(_SourceTex, inUV + texelSize * vec2(-1.0, -1.0)).rgb;
    vec3 b = texture(_SourceTex, inUV + texelSize * vec2( 0.0, -1.0)).rgb;
    vec3 c = texture(_SourceTex, inUV + texelSize * vec2( 1.0, -1.0)).rgb;
    vec3 d = texture(_SourceTex, inUV + texelSize * vec2(-1.0,  0.0)).rgb;
    vec3 e = texture(_SourceTex, inUV).rgb;
    vec3 f = texture(_SourceTex, inUV + texelSize * vec2( 1.0,  0.0)).rgb;
    vec3 g = texture(_SourceTex, inUV + texelSize * vec2(-1.0,  1.0)).rgb;
    vec3 h = texture(_SourceTex, inUV + texelSize * vec2( 0.0,  1.0)).rgb;
    vec3 i = texture(_SourceTex, inUV + texelSize * vec2( 1.0,  1.0)).rgb;

    // In-between taps (half-texel offsets)
    vec3 j = texture(_SourceTex, inUV + texelSize * vec2(-0.5, -0.5)).rgb;
    vec3 k = texture(_SourceTex, inUV + texelSize * vec2( 0.5, -0.5)).rgb;
    vec3 l = texture(_SourceTex, inUV + texelSize * vec2(-0.5,  0.5)).rgb;
    vec3 m = texture(_SourceTex, inUV + texelSize * vec2( 0.5,  0.5)).rgb;

    vec3 result = e * 0.125;
    result += (a + c + g + i) * 0.03125;
    result += (b + d + f + h) * 0.0625;
    result += (j + k + l + m) * 0.125;

    outColor = vec4(result, 1.0);
}
