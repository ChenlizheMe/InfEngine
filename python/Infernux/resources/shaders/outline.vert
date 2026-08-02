#version 450

ShaderInfo {
    Name "Outline"
    Hidden On
    Capabilities [Standalone]
    Properties {
        Float _OutlineWidth = 0.03
    }
}

void main() {
    // Extrude
    vec3 pos = inPosition + normalize(inNormal) * material._OutlineWidth;
    gl_Position = ubo.proj * ubo.view * pc.model * vec4(pos, 1.0);
}
