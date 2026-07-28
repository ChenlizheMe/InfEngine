#version 450

ShaderInfo {
    Name "Deferred Lighting"
    Hidden On
    Capabilities [Fullscreen]
    Resources {
        Texture2D _GAlbedo
        Texture2D _GNormal
        Texture2D _GMaterial
        Texture2D _GEmission
        Texture2D _SceneDepth
        Texture2D _ShadowMap
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

// Deferred lighting pass (pre-lit hybrid deferred).
// The GBuffer evaluate already performs full PBR lighting and stores the
// HDR lit color (including emission) in slot 0.  This pass simply passes
// it through, keeping the other GBuffer inputs available for future
// post-process effects (e.g. SSAO, SSR, bloom from emission).
//
// Resource order (matches GBuffer MRT + depth + shadow map):
//   binding 0 — gAlbedo     (RGBA16_SFLOAT: pre-lit HDR color)
//   binding 1 — gNormal     (RGBA16_SFLOAT: encoded world normal.xyz)
//   binding 2 — gMaterial   (RGBA8_UNORM: metallic, occlusion, specularHighlights, 1.0)
//   binding 3 — gEmission   (RGBA16_SFLOAT: emission.rgb)
//   binding 4 — sceneDepth  (D32_SFLOAT)
//   binding 5 — shadowMap   (D32_SFLOAT)

void main() {
    outColor = texture(_GAlbedo, inUV);
}
