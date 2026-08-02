#version 450

ShaderInfo {
    Name "Skybox Procedural"
    Queue 32767
    Cull Back
    DepthWrite Off
    DepthTest LessEqual
    PassTag Skybox
    CastShadows Off
    Imports ["Math"]
    Capabilities [Standalone]
    Properties {
        Color skyTopColor = [0.431, 0.494, 0.612, 1.0]
        Color skyHorizonColor = [0.651, 0.725, 0.816, 1.0]
        Color groundColor = [0.345, 0.345, 0.345, 1.0]
        Float exposure = 1.0
    }
    Inputs {
        Float3 fragWorldDir
    }
    Outputs {
        Float4 outColor
    }
}

// Input from vertex shader

// Output

// ============================================================================
// Procedural Sky
// ============================================================================

void main() {
    vec3 dir = normalize(fragWorldDir);

    // ---- Sky gradient ----
    // Y component: +1 = zenith, 0 = horizon, -1 = nadir
    float y = dir.y;

    vec3 skyColor = skyGradient(y,
        material.skyTopColor.rgb,
        material.skyHorizonColor.rgb,
        material.groundColor.rgb);

    // ---- Horizon haze ----
    // Add subtle brightness boost near the horizon
    float horizonGlow = 1.0 - abs(y);
    horizonGlow = pow(horizonGlow, 8.0) * 0.15;
    skyColor += vec3(horizonGlow);

    // ---- Final composition ----
    vec3 color = skyColor;

    // Exposure
    color *= material.exposure;

    // Output linear HDR — tonemapping and gamma correction are handled
    // by the post-process stack (consistent with lit.frag).
    // Applying them here would mix sRGB skybox samples with linear HDR
    // object samples during MSAA resolve, visibly degrading anti-aliasing.

    outColor = vec4(color, 1.0);
}
