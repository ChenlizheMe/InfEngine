struct ParticleInstance {
    vec4 position_size;
    vec4 color;
    vec4 rotation_custom;
    vec4 scale_custom;
    uvec4 ribbon_data;
    vec4 custom_data;
    vec4 previous_position_history;
};

layout(set = 0, binding = 0, std430) readonly buffer ParticleInstances {
    ParticleInstance instances[];
};

layout(set = 0, binding = 1, std430) readonly buffer ParticleDrawIndices {
    uint draw_indices[];
};

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

layout(location = 0) out vec3 v_WorldPos;
layout(location = 1) out vec3 v_Normal;
layout(location = 2) out vec4 v_Tangent;
layout(location = 3) out vec3 v_Color;
layout(location = 4) out vec2 v_TexCoord;
layout(location = 5) out float v_ViewDepth;
layout(location = 9) out vec2 v_ParticleLocalTexCoord;
layout(location = 10) out vec2 v_ParticleFlipbookNextTexCoord;
layout(location = 11) out float v_ParticleFlipbookBlend;
layout(location = 12) out float v_ParticleNormalizedAge;
layout(location = 13) flat out uint v_ParticleId;
layout(location = 14) out float v_ParticleAlpha;
layout(location = 15) flat out uint _inx_ObjectLayerMask;

struct VertexInput {
    vec3 position;
    vec3 normal;
    vec4 tangent;
    vec3 color;
    vec2 texCoord;
};

vec3 inxSafeBillboardAxis(vec3 value, vec3 fallbackValue) {
    float lengthSquared = dot(value, value);
    return lengthSquared > 1.0e-10 ? value * inversesqrt(lengthSquared) : fallbackValue;
}

void inxParticleBillboardBasis(ParticleInstance instance, out vec3 rightAxis, out vec3 upAxis) {
    vec3 cameraRight = inxSafeBillboardAxis(particleView.camera_right.xyz, vec3(1.0, 0.0, 0.0));
    vec3 cameraUp = inxSafeBillboardAxis(particleView.camera_up.xyz, vec3(0.0, 1.0, 0.0));
    vec3 cameraNormal = inxSafeBillboardAxis(cross(cameraRight, cameraUp), vec3(0.0, 0.0, 1.0));
    int alignment = int(round(particleView.alignment_reference.w));
    if (alignment == 0) {
        rightAxis = cameraRight;
        upAxis = cameraUp;
        return;
    }
    if (alignment == 1) {
        vec3 toCamera = inxSafeBillboardAxis(
            particleView.alignment_reference.xyz - instance.position_size.xyz,
            cameraNormal);
        rightAxis = inxSafeBillboardAxis(cross(cameraUp, toCamera), cameraRight);
        upAxis = inxSafeBillboardAxis(cross(toCamera, rightAxis), cameraUp);
        return;
    }
    vec3 requestedUp = alignment == 2 ? particleView.alignment_reference.xyz : instance.custom_data.yzw;
    upAxis = inxSafeBillboardAxis(requestedUp, cameraUp);
    vec3 projectedRight = cross(upAxis, cameraNormal);
    if (dot(projectedRight, projectedRight) <= 1.0e-10)
        projectedRight = cameraRight - upAxis * dot(cameraRight, upAxis);
    rightAxis = inxSafeBillboardAxis(projectedRight, cameraRight);
    upAxis = inxSafeBillboardAxis(cross(cameraNormal, rightAxis), upAxis);
}
