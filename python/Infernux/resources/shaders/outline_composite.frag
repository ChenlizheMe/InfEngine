#version 450
@shader_id: Outline Composite
@hidden

// Screen-space edge detection + compositing for Blender/Unity style outline.
//
// Algorithm:
//   1. If this pixel is inside the mask (selected object), skip (transparent output).
//   2. Otherwise, sample neighbours in a circular pattern within outlineWidth pixels.
//   3. If any neighbour is inside the mask, this pixel is on the outline edge.
//   4. Output the outline color with alpha = 1.0 for edge pixels.
//
// The caller composites this onto the scene color via alpha blending
// (srcAlpha, oneMinusSrcAlpha).

layout(set = 0, binding = 0) uniform sampler2D maskTex;

layout(push_constant) uniform PushConstants {
    vec4  outlineColor;   // rgba outline color
    vec2  texelSize;      // 1.0 / vec2(screenWidth, screenHeight)
    float outlineWidth;   // outline width in pixels
    float _padding;
} pc;

layout(location = 0) in  vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    float center = texture(maskTex, inUV).r;

    // Inside the mask — the object itself covers this pixel, no outline here
    if (center > 0.5) {
        outColor = vec4(0.0);
        return;
    }

    // Outside the mask — find the minimum distance to a mask pixel,
    // then apply smooth falloff for anti-aliased edges (Unity-style).
    float minDistSq = 999.0;
    int iWidth = clamp(int(pc.outlineWidth + 0.5), 1, 10);
    float radiusSq = float(iWidth * iWidth);

    // Fixed maximum loop bounds (GPU-friendly), dynamic skip via iWidth
    for (int y = -10; y <= 10; y++) {
        if (abs(y) > iWidth) continue;
        for (int x = -10; x <= 10; x++) {
            if (abs(x) > iWidth) continue;

            float distSq = float(x * x + y * y);
            // Circular kernel — skip corners
            if (distSq > radiusSq) continue;

            vec2 offset = vec2(float(x), float(y)) * pc.texelSize;
            float maskVal = texture(maskTex, inUV + offset).r;

            // Track nearest mask pixel distance
            if (maskVal > 0.5) {
                minDistSq = min(minDistSq, distSq);
            }
        }
    }

    // No mask pixel found within radius — fully transparent
    if (minDistSq > radiusSq) {
        outColor = vec4(0.0);
        return;
    }

    // Smooth anti-aliased falloff:
    //   - Inner edge (dist ≈ 0): full opacity
    //   - Outer edge (dist ≈ outlineWidth): fade to 0
    //   - 1-pixel smoothstep transition at the outer boundary
    float dist = sqrt(minDistSq);
    float alpha = 1.0 - smoothstep(pc.outlineWidth - 1.0, pc.outlineWidth, dist);

    outColor = vec4(pc.outlineColor.rgb, alpha * pc.outlineColor.a);
}
