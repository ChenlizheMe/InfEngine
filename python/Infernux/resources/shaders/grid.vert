#version 450

ShaderInfo {
    Name "Grid"
    Hidden On
    Cull None
    Capabilities [Standalone, NoMotionVectors]
    Outputs {
        Float3 nearPoint
        Float3 farPoint
    }
}

// Kept for compatibility with the existing draw path, which pushes model and
// normal matrices for mesh-style draw calls.

vec3 unprojectPoint(vec2 ndc, float z) {
    vec4 unprojected = inverse(ubo.view) * inverse(ubo.proj) * vec4(ndc, z, 1.0);
    return unprojected.xyz / unprojected.w;
}

void main() {
    vec2 ndc = inPosition.xy;

    nearPoint = unprojectPoint(ndc, 0.0);
    farPoint = unprojectPoint(ndc, 1.0);

    gl_Position = vec4(ndc, 0.0, 1.0);
}
