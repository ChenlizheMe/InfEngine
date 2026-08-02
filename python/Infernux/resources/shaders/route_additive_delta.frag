#version 450

ShaderInfo {
    Name "Route Additive Delta"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D _OriginalTex
        Texture2D _ProcessedTex
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

void main() {
    vec3 original = texture(_OriginalTex, inUV).rgb;
    vec3 processed = texture(_ProcessedTex, inUV).rgb;
    outColor = vec4(max(processed - original, vec3(0.0)), 0.0);
}
