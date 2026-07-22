// Canonical per-view Forward+ resources (set 1).
// TileHeaders[0] stores (tileCountX, tileCountY, tileSize, domainMask).

struct CanonicalLightData {
    vec4 positionRange;
    vec4 directionOuterCos;
    vec4 colorIntensity;
    vec4 shadowAndInnerCos;
    uvec4 metadata;
};

layout(std430, set = 1, binding = 1) readonly buffer CanonicalLightBuffer {
    uvec4 canonicalLightCountsAndGeneration;
    CanonicalLightData canonicalLights[];
};
layout(std430, set = 1, binding = 2) readonly buffer ForwardPlusTileHeaderBuffer {
    uvec4 forwardPlusTileHeaders[];
};
layout(std430, set = 1, binding = 3) readonly buffer ForwardPlusTileMaskBuffer {
    uint forwardPlusTileMasks[];
};

layout(location = 15) flat in uint _inx_ObjectLayerMask;

uvec4 inxForwardPlusTileHeader() {
    uvec4 grid = forwardPlusTileHeaders[0];
    uvec2 tileCount = max(grid.xy, uvec2(1u));
    uint tileSize = max(grid.z, 1u);
    uvec2 tile = min(uvec2(gl_FragCoord.xy) / tileSize, tileCount - uvec2(1u));
    return forwardPlusTileHeaders[1u + tile.y * tileCount.x + tile.x];
}
