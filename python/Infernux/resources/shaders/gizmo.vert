#version 450

ShaderInfo {
    Name "Gizmo"
    Hidden On
    Capabilities [Standalone, NoMotionVectors]
    Outputs {
        Float3 fragColor
    }
}

// UBO: model, view, projection matrices

void main() {
    gl_Position = ubo.proj * ubo.view * pc.model * vec4(inPosition, 1.0);
    fragColor = inColor;
}
