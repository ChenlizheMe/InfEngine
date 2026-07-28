#version 450

ShaderInfo {
    Name "Bloom Composite"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D _BloomTex
        Texture2D _SceneColor
    }
    PushConstants pc {
        Float intensity
        Float tintR
        Float tintG
        Float tintB
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

// Bloom composite pass — additive blend bloom texture onto scene color.
// Aligned with Unity URP's Bloom compositing.
//
// Push constants layout:
//   [0] intensity   — bloom intensity multiplier
//   [1] tintR       — bloom tint color R
//   [2] tintG       — bloom tint color G
//   [3] tintB       — bloom tint color B

void main() {
    vec3 bloom = texture(_BloomTex, inUV).rgb;
    vec4 sceneSample = texture(_SceneColor, inUV);
    vec3 scene = sceneSample.rgb;

    // Apply tint and intensity
    vec3 tint = vec3(pc.tintR, pc.tintG, pc.tintB);
    bloom *= tint * pc.intensity;

    // Additive blend
    vec3 result = scene + bloom;

    outColor = vec4(result, sceneSample.a);
}
