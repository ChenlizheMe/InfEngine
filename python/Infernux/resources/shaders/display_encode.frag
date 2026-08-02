#version 450

ShaderInfo {
    Name "Display Encode"
    Hidden On
    Imports ["Lib Color"]
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

// Built-in display encode — the final image pass of every RenderStack
// pipeline (inserted automatically by the render graph).
//
// The swapchain and the editor viewport are UNORM surfaces without hardware
// sRGB encoding, so the graph explicitly encodes linear scene color to sRGB
// here. Scene rendering, post-process effects and tone mapping all operate
// in linear space; this pass is the single place where gamma is applied.

void main() {
    vec4 source = texture(_SourceTex, inUV);
    // Values above 1.0 (no tone mapping mounted) are clipped, matching what
    // a UNORM target would have done implicitly.
    vec3 ldr = clamp(source.rgb, 0.0, 1.0);
    outColor = vec4(linearToSRGB(ldr), source.a);
}
