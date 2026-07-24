#version 450
@shader_id: Deferred Lighting
@hidden

// Deferred lighting pass (pre-lit hybrid deferred).
// The GBuffer evaluate already performs full PBR lighting and stores the
// HDR lit color (including emission) in slot 0.  This pass simply passes
// it through, keeping the other GBuffer inputs available for future
// post-process effects (e.g. SSAO, SSR, bloom from emission).
//
// Binding layout (matches GBuffer MRT + depth + shadow map):
//   binding 0 — gAlbedo     (RGBA16_SFLOAT: pre-lit HDR color)
//   binding 1 — gNormal     (RGBA16_SFLOAT: encoded world normal.xyz)
//   binding 2 — gMaterial   (RGBA8_UNORM: metallic, occlusion, specularHighlights, 1.0)
//   binding 3 — gEmission   (RGBA16_SFLOAT: emission.rgb)
//   binding 4 — sceneDepth  (D32_SFLOAT)
//   binding 5 — shadowMap   (D32_SFLOAT)

layout(set = 0, binding = 0) uniform sampler2D _GAlbedo;
layout(set = 0, binding = 1) uniform sampler2D _GNormal;
layout(set = 0, binding = 2) uniform sampler2D _GMaterial;
layout(set = 0, binding = 3) uniform sampler2D _GEmission;
layout(set = 0, binding = 4) uniform sampler2D _SceneDepth;
layout(set = 0, binding = 5) uniform sampler2D _ShadowMap;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    outColor = texture(_GAlbedo, inUV);
}
