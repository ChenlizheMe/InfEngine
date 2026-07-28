#version 450

ShaderInfo {
    Name "Outline Mask"
    Hidden On
    Capabilities [Standalone]
    Outputs {
        Float4 outColor
    }
}

void main() {
    outColor = vec4(1.0, 1.0, 1.0, 1.0);
}
