#version 450

ShaderInfo {
    Name "Tonemapping"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D _SourceTex
    }
    PushConstants pc {
        Float mode
        Float exposure
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

// Tonemapping post-process pass.
//
// Compresses linear HDR scene color into linear LDR [0,1]. The output stays
// in *linear* space — the built-in display-encode pass at the end of the
// pipeline performs the single linear → sRGB conversion. Doing gamma here as
// well would double-encode and wash the image out to gray.
//
// Modes:
//   0 — None (clamp only, no tone mapping)
//   1 — Reinhard
//   2 — ACES Filmic (default, matches Unity/Unreal look)
//
// Push constants:
//   [0] mode      — tone mapping operator (0/1/2)
//   [1] exposure  — pre-tonemap exposure multiplier

// ---- ACES Filmic (Stephen Hill's fit) ----
vec3 ACESFilm(vec3 x) {
    // sRGB → ACEScg input transform (simplified)
    const mat3 inputMat = mat3(
        0.59719, 0.07600, 0.02840,
        0.35458, 0.90834, 0.13383,
        0.04823, 0.01566, 0.83777
    );
    // ACEScg → sRGB output transform (simplified)
    const mat3 outputMat = mat3(
         1.60475, -0.10208, -0.00327,
        -0.53108,  1.10813, -0.07276,
        -0.07367, -0.00605,  1.07602
    );

    vec3 v = inputMat * x;

    // RRT + ODT fit
    vec3 a = v * (v + 0.0245786) - 0.000090537;
    vec3 b = v * (0.983729 * v + 0.4329510) + 0.238081;
    v = a / max(b, vec3(1e-6));

    return clamp(outputMat * v, 0.0, 1.0);
}

// ---- Reinhard ----
vec3 Reinhard(vec3 x) {
    return x / (x + vec3(1.0));
}

void main() {
    vec4 source = texture(_SourceTex, inUV);
    vec3 hdr = source.rgb;

    // Apply exposure
    hdr *= pc.exposure;

    // Tone mapping
    vec3 ldr;
    int m = int(pc.mode + 0.5);
    if (m == 1) {
        ldr = Reinhard(hdr);
    } else if (m == 2) {
        ldr = ACESFilm(hdr);
    } else {
        ldr = clamp(hdr, 0.0, 1.0);
    }

    // Output remains linear; display encode happens in the built-in
    // _DisplayEncode pass at the end of the pipeline.
    outColor = vec4(ldr, source.a);
}
