// ============================================================================
// vertex_main.glsl — Default vertex main() template
//
// The engine injects an optional vertex(v) call at the marked insertion point
// when the shader defines void vertex(inout VertexInput v).
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
    mat4 instModel    = instanceModels[gl_InstanceIndex];
    vec4 worldPos     = instModel * vec4(v.position, 1.0);
    mat3 normalMatrix = transpose(inverse(mat3(instModel)));
    vec3 worldNormal  = normalize(normalMatrix * v.normal);
    vec4 worldTangent = vec4(normalize(normalMatrix * v.tangent.xyz), v.tangent.w);

    v_WorldPos  = worldPos.xyz;
    v_Normal    = worldNormal;
    v_Tangent   = worldTangent;
    v_Color     = v.color;
    v_TexCoord  = v.texCoord;
    // GLM_FORCE_LEFT_HANDED view space looks down +Z, so (view * pos).z is
    // already the positive eye depth. Take abs() so CSM cascade selection and
    // depth helpers always receive a positive linear depth regardless of the
    // view-matrix handedness.
    v_ViewDepth = abs((ubo.view * worldPos).z);
    gl_Position = ubo.proj * ubo.view * worldPos;
${PASS_VERTEX_OUTPUT}
}
