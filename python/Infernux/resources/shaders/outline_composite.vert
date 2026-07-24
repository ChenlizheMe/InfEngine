#version 450
@shader_id: Outline Composite
@hidden

layout(location = 0) out vec2 outUV;

void main() {
    // Generate full-screen triangle
    // 0: (-1, -1), 1: (-1, 3), 2: (3, -1) -> covers (-1,-1) to (1,1)
    float x = -1.0 + float((gl_VertexIndex & 1) << 2);
    float y = -1.0 + float((gl_VertexIndex & 2) << 2);
    outUV = vec2((x + 1.0) * 0.5, (y + 1.0) * 0.5);
    gl_Position = vec4(x, y, 0.0, 1.0);
}
