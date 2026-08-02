#version 450

ShaderInfo {
    Name "Flat White"
    Hidden On
    Capabilities [Standalone]
}

void main() {
    gl_Position = ubo.proj * ubo.view * pc.model * vec4(inPosition, 1.0);
}
