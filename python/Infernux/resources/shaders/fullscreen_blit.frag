#version 450

ShaderInfo {
    Name "Fullscreen Blit"
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

// Simple pass-through blit shader for fullscreen copy operations.
// Samples the source texture and outputs it unchanged.

void main() {
    outColor = texture(_SourceTex, inUV);
}
