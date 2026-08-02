#version 450

ShaderInfo {
    Name "Gizmo"
    Hidden On
    CastShadows Off
    Imports ["Lib Color"]
    Capabilities [Standalone, ForwardOnly, NoDepthPass, NoPicking, NoMotionVectors]
    Inputs {
        Float3 fragColor
    }
    Outputs {
        Float4 outColor
    }
}

void main() {
    // Gizmo vertex colors are authored in sRGB (editor constants); the scene
    // buffer is linear and gets sRGB-encoded by the display encode pass.
    outColor = vec4(sRGBToLinear(fragColor), 1.0);
}
