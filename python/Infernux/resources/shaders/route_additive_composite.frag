#version 450

ShaderInfo {
    Name "Route Additive Composite"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D _BaseTex
        Texture2D _AdditiveTex
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

void main() {
    vec4 base = texture(_BaseTex, inUV);
    vec3 additive = texture(_AdditiveTex, inUV).rgb;
    outColor = vec4(base.rgb + additive, base.a);
}
