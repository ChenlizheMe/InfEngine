#version 450

ShaderInfo {
    Name "Deferred Lighting"
    Hidden On
    Capabilities [Fullscreen, DeferredLighting, CameraMatrices]
    Imports ["Lighting", "PBR"]
    Resources {
        Texture2D _GAlbedo
        Texture2D _GNormal
        Texture2D _GMaterial
        Texture2D _GEmission
        Texture2DUInt _GObject
        Texture2D _SceneDepth
    }
    Inputs {
        Float2 inUV
    }
    Outputs {
        Float4 outColor
    }
}

// Canonical deferred lighting pass. Geometry records only surface state;
// lighting is evaluated here from the camera-local Forward+ light grid.
//
// Resource order (matches GBuffer MRT + depth + shadow map):
//   binding 0 — gAlbedo     (RGBA16_SFLOAT: base color + alpha)
//   binding 1 — gNormal     (RGBA16_SFLOAT: encoded world normal.xyz)
//   binding 2 — gMaterial   (RGBA8_UNORM: metallic, occlusion, specularHighlights, shadingParam0)
//   binding 3 — gEmission   (RGBA16_SFLOAT: emission.rgb, shadingParam1)
//   binding 4 — gObject     (RG32_UINT: layer mask, shading model)
//   binding 5 — sceneDepth  (D32_SFLOAT)

// Implemented by the compiler-generated registry appended to this shader.
// Every Deferred-capable .shadingmodel contributes its single shading()
// function under a stable model ID.
SurfaceData _inx_DeferredSurfaceData;
void inxDispatchShading(uint, out vec4);

vec3 reconstructWorldPosition(vec2 uv, float depth) {
    vec4 clip = vec4(uv * 2.0 - 1.0, depth, 1.0);
    vec4 world = ubo.inverseViewProj * clip;
    return world.xyz / max(abs(world.w), 1e-7);
}

void main() {
    float depth = texture(_SceneDepth, inUV).r;
    if (depth >= 0.999999) {
        outColor = vec4(0.0);
        return;
    }

    vec4 base = texture(_GAlbedo, inUV);
    vec4 normalData = texture(_GNormal, inUV);
    vec4 materialData = texture(_GMaterial, inUV);
    vec4 emissionData = texture(_GEmission, inUV);
    uvec2 objectData = texture(_GObject, inUV).rg;
    _inx_ObjectLayerMask = objectData.x;

    vec3 worldPos = reconstructWorldPosition(inUV, depth);
    vec3 normalWS = normalData.xyz * 2.0 - 1.0;
    SurfaceData surfaceData = InitSurfaceData();
    surfaceData.albedo = base.rgb;
    surfaceData.normalWS = normalize(normalWS);
    surfaceData.metallic = materialData.r;
    surfaceData.smoothness = normalData.a;
    surfaceData.occlusion = materialData.g;
    surfaceData.emission = emissionData.rgb;
    surfaceData.alpha = base.a;
    surfaceData.specularHighlights = materialData.b;
    surfaceData.shadingParam0 = materialData.a;
    surfaceData.shadingParam1 = emissionData.a;

    ShadingContext ctx = InitShadingContext();
    ctx.positionWS = worldPos;
    ctx.geometricNormalWS = surfaceData.normalWS;
    vec3 tangentReference = abs(surfaceData.normalWS.y) < 0.999
        ? vec3(0.0, 1.0, 0.0)
        : vec3(1.0, 0.0, 0.0);
    ctx.tangentWS = vec4(normalize(cross(tangentReference, surfaceData.normalWS)), 1.0);
    ctx.cameraPositionWS = lighting.cameraPos.xyz;
    ctx.viewDepth = abs((ubo.view * vec4(worldPos, 1.0)).z);
    ctx.frontFacing = true;
    _inx_ShadingContext = ctx;
    _inx_DeferredSurfaceData = surfaceData;
    inxDispatchShading(objectData.y, outColor);
}
