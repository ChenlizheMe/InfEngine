#version 450

@shader_id: Standard

// Default mesh vertex shader.
//
// Leaving this file without a user-defined vertex() function makes the engine
// use its built-in standard transform path: object -> world -> view -> clip.
//
// Optional wave deformation example:
// Uncomment the function below to animate vertices before the engine applies
// the per-instance model transform. VertexInput and _Globals are injected
// automatically for standard mesh vertex shaders.
//
// void vertex(inout VertexInput v) {
//     float wave = sin(v.position.x * 4.0 + _Globals._Time.x * 2.0) * 0.1;
//     v.position.y += wave;
// }