struct InstanceAuxData {
    mat4 previousModel;
    uvec2 objectId;
    uint flags;
    uint layerMask;
};

layout(std430, set = 2, binding = 4) readonly buffer InstanceAuxBuffer {
    InstanceAuxData instanceAuxData[];
};

// Location 15 is reserved for engine-owned pass data.
layout(location = 15) out vec2 _inx_MotionVector;
