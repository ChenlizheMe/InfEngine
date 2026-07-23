layout(location = 0) in vec3 v_WorldPos;
layout(location = 1) in vec3 v_Normal;
layout(location = 2) in vec4 v_Tangent;
layout(location = 3) in vec3 v_Color;
layout(location = 4) in vec2 v_TexCoord;
layout(location = 5) in float v_ViewDepth;
layout(location = 14) in float v_ParticleAlpha;
layout(location = 15) flat in uint _inx_ObjectLayerMask;
