#version 450

@shader_id: Skybox Procedural
@pass_tag: skybox
@cull: back
@depth_write: false
@depth_test: less_equal
@queue: 32767
@cast_shadows: off
@property: skyTopColor, Color, [0.431, 0.494, 0.612, 1.0]
@property: skyHorizonColor, Color, [0.651, 0.725, 0.816, 1.0]
@property: groundColor, Color, [0.345, 0.345, 0.345, 1.0]
@property: exposure, Float, 1.0

@import: Math

// Input from vertex shader
layout(location = 0) in vec3 fragWorldDir;

// Output
layout(location = 0) out vec4 outColor;

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
