// Engine-owned object layer forwarded from InstanceAuxData. This is shared by
// Forward, Forward+, and GBuffer so Light.culling_mask has identical semantics.
layout(location = 15) flat in uint _inx_ObjectLayerMask;
