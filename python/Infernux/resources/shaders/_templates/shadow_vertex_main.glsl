// ============================================================================
// shadow_vertex_main.glsl — Shadow pass vertex main() template
//
// Transforms vertices into light clip-space using the shadow UBO.
// Outputs varyings needed for alpha-clip shadow support (texCoord, normal).
// ============================================================================

void main() {
    VertexInput v;
    v.position = inPosition;
    v.normal   = inNormal;
    v.tangent  = inTangent;
    v.color    = inColor;
    v.texCoord = inTexCoord;
${VERTEX_CALL}
    SkinInstanceData skin = skinInstances[gl_InstanceIndex];
    mat4 instModel = instanceModels[gl_InstanceIndex];
    bool inxLineVertex = inBoneIndices.w == 0x4C494E45u;
    vec4 worldPos;
    vec3 worldNormal;
    vec4 worldTangent;
    if (inxLineVertex) {
        vec4 centerWorld = instModel * vec4(v.position, 1.0);
        mat3 normalMatrix = transpose(inverse(mat3(instModel)));
        vec3 transformedTangent = mat3(instModel) * v.tangent.xyz;
        vec3 tangentWorld = dot(transformedTangent, transformedTangent) > 1.0e-10
            ? normalize(transformedTangent)
            : vec3(1.0, 0.0, 0.0);
        vec3 facingCandidate = inBoneIndices.z == 0u
            ? inverse(shadowUBO.view)[3].xyz - centerWorld.xyz
            : normalMatrix * v.normal;
        vec3 facing = dot(facingCandidate, facingCandidate) > 1.0e-10
            ? normalize(facingCandidate)
            : vec3(0.0, 0.0, 1.0);
        vec3 side = cross(facing, tangentWorld);
        if (dot(side, side) < 1.0e-10)
            side = cross(inverse(shadowUBO.view)[1].xyz, tangentWorld);
        if (dot(side, side) < 1.0e-10)
            side = vec3(1.0, 0.0, 0.0);
        worldPos = vec4(centerWorld.xyz + normalize(side) * inBoneWeights.x, 1.0);
        worldNormal = facing;
        worldTangent = vec4(tangentWorld, 1.0);
    } else {
        if ((skin.flags & 1u) != 0u && skin.boneCount > 0u) {
            mat4 skinMat =
                inBoneWeights.x * skinBones[skin.boneOffset + min(inBoneIndices.x, skin.boneCount - 1u)] +
                inBoneWeights.y * skinBones[skin.boneOffset + min(inBoneIndices.y, skin.boneCount - 1u)] +
                inBoneWeights.z * skinBones[skin.boneOffset + min(inBoneIndices.z, skin.boneCount - 1u)] +
                inBoneWeights.w * skinBones[skin.boneOffset + min(inBoneIndices.w, skin.boneCount - 1u)];
            v.position = (skinMat * vec4(v.position, 1.0)).xyz;
            mat3 skinNormalMat = mat3(skinMat);
            v.normal = normalize(skinNormalMat * v.normal);
            v.tangent = vec4(normalize(skinNormalMat * v.tangent.xyz), v.tangent.w);
        }
        worldPos = instModel * vec4(v.position, 1.0);
        mat3 normalMatrix = transpose(inverse(mat3(instModel)));
        worldNormal = normalize(normalMatrix * v.normal);
        worldTangent = vec4(normalize(normalMatrix * v.tangent.xyz), v.tangent.w);
    }

    // The caster pass stays purely geometric: shadow acne is handled by the
    // slope-scaled raster depth bias of the shadow pipeline plus the unified
    // receiver-side bias in lighting.glsl. World-space caster offsets warped
    // shadow shapes and detached contact shadows for local lights.
    v_WorldPos  = worldPos.xyz;
    v_Normal    = worldNormal;
    v_Tangent   = worldTangent;
    v_Color     = v.color;
    v_TexCoord  = v.texCoord;
    v_ViewDepth = 0.0;
    v_LineColor = inxLineVertex ? vec4(v.color, inBoneWeights.y) : vec4(1.0);
    gl_Position = shadowUBO.proj * shadowUBO.view * worldPos;
    if (shadowUBO.light_vector.w < 0.5) {
        // Directional shadow pancaking preserves casters that cross the light
        // near plane while maximizing the usable depth range of each cascade.
        gl_Position.z = max(gl_Position.z, 0.0);
    }
}
