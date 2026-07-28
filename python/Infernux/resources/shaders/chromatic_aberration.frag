#version 450

ShaderInfo {
    Name "Chromatic Aberration"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D _SourceTex
    }
    PushConstants pc {
        Float intensity
        Float _pad0
        Float _pad1
        Float _pad2
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

// Chromatic Aberration post-process — RGB channel offset from center.
// Matches Unity URP Chromatic Aberration.
//
// Push constants:
//   [0] intensity — channel separation strength (0 = off, 1 = max)

void main() {
    vec2 center = inUV - 0.5;
    float dist = length(center);

    // Offset increases with distance from center (radial CA)
    vec2 offset = center * dist * pc.intensity * 0.02;

    float r = texture(_SourceTex, inUV - offset).r;
    vec4 centerSample = texture(_SourceTex, inUV);
    float g = centerSample.g;
    float b = texture(_SourceTex, inUV + offset).b;

    outColor = vec4(r, g, b, centerSample.a);
}
