#version 450
@shader_id: route_alpha_composite
@hidden

// Premultiplied-alpha composition for isolated queue, layer, and stage images.
// Rendering into a transparent route target through normal GPU blending
// produces premultiplied RGB. Keeping that representation across intermediate
// accumulators avoids dark fringes and double-multiplication.

layout(set = 0, binding = 0) uniform sampler2D _BaseTex;
layout(set = 0, binding = 1) uniform sampler2D _LayerTex;

layout(location = 0) in vec2 inUV;
layout(location = 0) out vec4 outColor;

void main() {
    vec4 base = texture(_BaseTex, inUV);
    vec4 layer = texture(_LayerTex, inUV);
    float inverseAlpha = 1.0 - layer.a;
    outColor = vec4(
        layer.rgb + base.rgb * inverseAlpha,
        layer.a + base.a * inverseAlpha
    );
}
