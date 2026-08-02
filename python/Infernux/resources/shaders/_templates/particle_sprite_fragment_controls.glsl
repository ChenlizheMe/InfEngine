layout(push_constant) uniform ParticleViewConstants {
    mat4 view_projection;
    mat4 previous_view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
    vec4 alignment_reference;
} particleView;

bool isParticleRibbonOutput() {
    return particleView.alignment_reference.w < -0.5;
}
