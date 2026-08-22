#version 450

ShaderInfo {
    Name "Display Encode"
    Hidden On
    Imports ["Lib Color"]
    Capabilities [Fullscreen]
    Resources {
        Texture2D _SourceTex
    }
    PushConstants pc {
        Float dithering
        Float stopNaNs
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

// Built-in display encode — the final image pass of every RenderStack
// pipeline (inserted automatically by the render graph).
//
// The swapchain and the editor viewport are UNORM surfaces without hardware
// sRGB encoding, so the graph explicitly encodes linear scene color to sRGB
// here. Scene rendering, post-process effects and tone mapping all operate
// in linear space; this pass is the single place where gamma is applied.

float interleavedGradientNoise(vec2 pixel) {
    return fract(52.9829189 * fract(dot(pixel, vec2(0.06711056, 0.00583715))));
}

float triangularDither(vec2 pixel, vec2 offset) {
    // Subtracting two decorrelated uniform samples gives a zero-mean TPDF.
    // Compared with a single grayscale sample this hides diagonal structure
    // and avoids biasing the output toward either neighboring 8-bit value.
    float a = interleavedGradientNoise(pixel + offset);
    float b = interleavedGradientNoise(pixel.yx + offset.yx + vec2(37.0, 17.0));
    return a - b;
}

void main() {
    vec4 source = texture(_SourceTex, inUV);
    if (pc.stopNaNs > 0.5 && (any(isnan(source)) || any(isinf(source)))) {
        source = vec4(0.0, 0.0, 0.0, 1.0);
    }
    // Values above 1.0 (no tone mapping mounted) are clipped, matching what
    // a UNORM target would have done implicitly.
    vec3 ldr = clamp(source.rgb, 0.0, 1.0);
    vec3 encoded = linearToSRGB(ldr);
    if (pc.dithering > 0.5) {
        // Dither in encoded space because the following UNORM conversion is
        // the actual 8-bit quantizer. Static per-channel offsets keep the
        // result shimmer-free while avoiding visible monochrome streaks.
        vec2 pixel = floor(gl_FragCoord.xy);
        vec3 noise = vec3(
            triangularDither(pixel, vec2(0.0, 0.0)),
            triangularDither(pixel, vec2(19.0, 47.0)),
            triangularDither(pixel, vec2(73.0, 11.0))
        );
        encoded = clamp(encoded + noise * (1.0 / 255.0), 0.0, 1.0);
    }
    outColor = vec4(encoded, source.a);
}
