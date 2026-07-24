#version 450

@shader_id: Grid
@cull: none
@hidden
@property: fadeStart, Float, 15.0
@property: fadeEnd, Float, 80.0

layout(std140, binding = 0) uniform UniformBufferObject {
    mat4 model;
    mat4 view;
    mat4 proj;
} ubo;

layout(location = 0) in vec3 nearPoint;
layout(location = 1) in vec3 farPoint;
layout(location = 0) out vec4 outColor;

float computeDepth(vec3 pos) {
    vec4 clipSpacePos = ubo.proj * ubo.view * vec4(pos, 1.0);
    return clipSpacePos.z / clipSpacePos.w;
}

float rayPlaneIntersection(vec3 rayNear, vec3 rayFar) {
    vec3 rayDir = rayFar - rayNear;
    if (abs(rayDir.y) < 0.0001) {
        return -1.0;
    }
    return -rayNear.y / rayDir.y;
}

float pristineGridLine(vec2 uv) {
    vec2 dudv = max(fwidth(uv), vec2(1e-6));
    vec2 uvMod = fract(uv);
    vec2 uvDist = min(uvMod, 1.0 - uvMod);
    vec2 distInPixels = uvDist / dudv;
    vec2 lineAlpha = 1.0 - smoothstep(0.0, 1.0, distInPixels);

    float alpha = max(lineAlpha.x, lineAlpha.y);
    float density = max(dudv.x, dudv.y);
    float densityFade = 1.0 - smoothstep(0.5, 1.0, density);

    return alpha * densityFade;
}

float viewAngleFade(vec3 worldPos, vec3 cameraPos) {
    vec3 viewDir = normalize(worldPos - cameraPos);
    return smoothstep(0.0, 0.15, abs(viewDir.y));
}

void main() {
    float t = rayPlaneIntersection(nearPoint, farPoint);
    if (t < 0.0) {
        discard;
    }

    vec3 worldPos3D = nearPoint + t * (farPoint - nearPoint);
    float depth = computeDepth(worldPos3D);
    if (depth < 0.0 || depth > 1.0) {
        discard;
    }

    // Extract camera world position: pos = -(R^T * t) using view matrix orthogonality
    vec3 cameraPos = -(transpose(mat3(ubo.view)) * ubo.view[3].xyz);

    vec2 coord = worldPos3D.xz;

    // ---- Grid calculation: adapted from InfiniteGrid.glsl ----
    float minor = pristineGridLine(coord);
    float major = pristineGridLine(coord / 10.0);

    // Fade minor lines once the 1-unit grid becomes too dense on screen.
    vec2 minorDeriv = fwidth(coord);
    float lodFactor = smoothstep(0.3, 0.6, max(minorDeriv.x, minorDeriv.y));

    // Keep the original gray minor/major grid balance.
    float nearGridAlpha = minor * 0.25 + major * 0.4;
    float farGridAlpha = major * 0.4;
    float lineAlpha = mix(nearGridAlpha, farGridAlpha, lodFactor);

    // 0.8 sRGB expressed in linear space — the scene buffer is linear and is
    // sRGB-encoded by the display encode pass, keeping the familiar gray.
    vec3 lineColor = vec3(0.6038);

    // ---- Distance fade (XZ plane) ----
    float dist = length(coord - cameraPos.xz);
    float distFade = 1.0 - smoothstep(material.fadeStart, material.fadeEnd, dist);
    float angleFade = viewAngleFade(worldPos3D, cameraPos);

    float alpha = lineAlpha * distFade * angleFade;

    if (alpha < 0.005) {
        discard;
    }

    gl_FragDepth = depth;
    outColor = vec4(lineColor, alpha);
}
