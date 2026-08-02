#version 450

ShaderInfo {
    Name "Skybox Procedural"
    Cull Back
    DepthWrite Off
    DepthTest LessEqual
    Capabilities [Standalone]
    Outputs {
        Float3 fragWorldDir
    }
}

void main() {
    // Strip translation from view matrix (skybox centered on camera)
    mat4 viewNoTranslation = mat4(mat3(ubo.view));

    // inPosition is a unit cube vertex — its direction IS the world-space
    // direction we want for sky gradient / sun disc evaluation.
    fragWorldDir = inPosition;

    // Detect orthographic projection: proj[2][3] is -1 for perspective, 0 for ortho
    bool isOrtho = (abs(ubo.proj[2][3]) < 0.5);

    vec3 pos = inPosition;
    if (isOrtho) {
        // In ortho mode the unit cube may be smaller than the viewport.
        // Scale it so it always fills the screen.  proj[0][0] = 2/width,
        // proj[1][1] = 2/height (Vulkan Y-flipped).
        float halfW = 1.0 / abs(ubo.proj[0][0]);
        float halfH = 1.0 / abs(ubo.proj[1][1]);
        float s     = max(halfW, halfH) * 1.8;   // margin for rotation
        pos *= s;
    }

    // Transform the skybox cube
    vec4 clipPos = ubo.proj * viewNoTranslation * vec4(pos, 1.0);

    // Set z = w so depth is always at the far plane (1.0 after perspective divide)
    // Combined with depth test <= and no depth write, skybox renders behind everything
    gl_Position = clipPos.xyww;
}
