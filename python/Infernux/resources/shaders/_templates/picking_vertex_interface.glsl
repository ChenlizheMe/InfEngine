struct InstanceAuxData {
    mat4 previousModel;
    uvec2 objectId;
    uint flags;
    uint layerMask;
};

layout(std430, set = 2, binding = 4) readonly buffer InstanceAuxBuffer {
    InstanceAuxData instanceAuxData[];
};

// The linker reserves location 15 for engine-owned pass data.
layout(location = 15) flat out uvec2 _inx_ObjectId;
