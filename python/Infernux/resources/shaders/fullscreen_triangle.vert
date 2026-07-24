#version 450
@shader_id: Fullscreen Triangle
@hidden

// Shared fullscreen triangle vertex shader for all FullScreenEffect passes.
// Generates a single triangle covering the entire screen.
// No vertex input required — vertices are procedurally generated.

layout(location = 0) out vec2 outUV;

void main() {
    // Generate full-screen triangle from gl_VertexIndex (3 vertices):
    //   0: (-1, -1)  UV (0, 0)
    //   1: (-1,  3)  UV (0, 2)
    //   2: ( 3, -1)  UV (2, 0)
    // Covers the entire (-1,-1) to (1,1) clip space.
    float x = -1.0 + float((gl_VertexIndex & 1) << 2);
    float y = -1.0 + float((gl_VertexIndex & 2) << 2);
    outUV = vec2((x + 1.0) * 0.5, (y + 1.0) * 0.5);
    gl_Position = vec4(x, y, 0.0, 1.0);
}
