#version 450

ShaderInfo {
    Name "White Balance"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D _SourceTex
    }
    PushConstants pc {
        Float temperature
        Float tint
        Float _pad0
        Float _pad1
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

// White Balance post-process — adjusts color temperature and tint.
// Matches Unity URP White Balance.
//
// Push constants:
//   [0] temperature — color temperature shift (-100 to 100, 0 = neutral)
//   [1] tint        — green-magenta tint (-100 to 100, 0 = neutral)

// sRGB → Linear LMS (simplified Bradford chromatic adaptation)
const mat3 LIN_2_LMS = mat3(
    3.90405e-1, 7.08416e-2, 2.31082e-2,
    5.49941e-1, 9.63172e-1, 1.28021e-1,
    8.92632e-3, 1.35775e-3, 9.36245e-1
);

const mat3 LMS_2_LIN = mat3(
     2.85847e+0, -2.10182e-1, -4.18120e-2,
    -1.62879e+0,  1.15820e+0, -1.18169e-1,
    -2.48910e-2,  3.24281e-4,  1.06867e+0
);

// Convert temperature + tint to white point in LMS space
vec3 WhiteBalance(vec3 color, float temp, float tin) {
    // Temperature: warm (positive) → cool (negative)
    float t1 = temp / 60.0;
    float t2 = tin / 60.0;

    // Simple approximation of D-illuminant shift in LMS
    float x = 0.31271 - t1 * (t1 < 0.0 ? 0.1 : 0.05);
    float y = 0.32902 + t2 * 0.05;

    // Convert white point to LMS
    float Y = 1.0;
    float X = Y * x / y;
    float Z = Y * (1.0 - x - y) / y;

    vec3 w1 = LIN_2_LMS * vec3(0.949237, 1.03542, 1.08728); // D65 reference
    vec3 w2 = LIN_2_LMS * vec3(X, Y, Z);

    // Chromatic adaptation (von Kries)
    vec3 balance = w1 / max(w2, vec3(1e-5));

    vec3 lms = LIN_2_LMS * color;
    lms *= balance;
    return LMS_2_LIN * lms;
}

void main() {
    vec4 source = texture(_SourceTex, inUV);
    vec3 color = source.rgb;

    color = WhiteBalance(color, pc.temperature, pc.tint);
    color = max(color, vec3(0.0));

    outColor = vec4(color, source.a);
}
