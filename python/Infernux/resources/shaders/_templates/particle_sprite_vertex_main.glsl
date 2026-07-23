const vec2 _inxParticleCorners[6] = vec2[](
    vec2(-1.0, -1.0), vec2(-1.0, 1.0), vec2(1.0, 1.0),
    vec2(-1.0, -1.0), vec2(1.0, 1.0), vec2(1.0, -1.0)
);

const vec2 _inxParticleUvs[6] = vec2[](
    vec2(0.0, 1.0), vec2(0.0, 0.0), vec2(1.0, 0.0),
    vec2(0.0, 1.0), vec2(1.0, 0.0), vec2(1.0, 1.0)
);

void main() {
    ParticleInstance instance = instances[draw_indices[gl_InstanceIndex]];
    vec2 corner = _inxParticleCorners[gl_VertexIndex % 6];
    float cosine = cos(instance.rotation_custom.x);
    float sine = sin(instance.rotation_custom.x);
    corner = mat2(cosine, -sine, sine, cosine) * corner;

    VertexInput v;
    v.position = instance.position_size.xyz +
        (particleView.camera_right.xyz * corner.x * instance.scale_custom.x +
         particleView.camera_up.xyz * corner.y * instance.scale_custom.y) * instance.position_size.w;
    v.normal = normalize(cross(particleView.camera_right.xyz, particleView.camera_up.xyz));
    v.tangent = vec4(normalize(particleView.camera_right.xyz), 1.0);
    v.color = instance.color.rgb * particleView.material_tint.rgb;
    v.texCoord = _inxParticleUvs[gl_VertexIndex % 6];
${VERTEX_CALL}

    vec4 clipPosition = particleView.view_projection * vec4(v.position, 1.0);
    v_WorldPos = v.position;
    v_Normal = normalize(v.normal);
    v_Tangent = v.tangent;
    v_Color = v.color;
    v_TexCoord = v.texCoord;
    v_ViewDepth = clipPosition.w;
    v_ParticleAlpha = instance.color.a * particleView.material_tint.a;
    _inx_ObjectLayerMask = floatBitsToUint(particleView.lighting_control.w);
    gl_Position = clipPosition;
${PASS_VERTEX_OUTPUT}
}
