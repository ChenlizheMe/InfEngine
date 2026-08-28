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
    // Preserve the authored local position so Motion can evaluate the same
    // vertex against the previous skeletal pose after current skinning.
    vec3 inxUnskinnedPosition = v.position;
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
        vec3 facing;
        if (inBoneIndices.z == 0u) {
            vec3 toCamera = inverse(ubo.view)[3].xyz - centerWorld.xyz;
            facing = dot(toCamera, toCamera) > 1.0e-10 ? normalize(toCamera) : vec3(0.0, 0.0, 1.0);
        } else {
            vec3 transformedFacing = normalMatrix * v.normal;
            facing = dot(transformedFacing, transformedFacing) > 1.0e-10
                ? normalize(transformedFacing)
                : vec3(0.0, 0.0, 1.0);
        }
        mat4 cameraWorld = inverse(ubo.view);
        vec3 cameraRight = normalize(cameraWorld[0].xyz);
        vec3 side = cross(facing, tangentWorld);
        // Near a camera-facing tangent, normalizing the tiny cross product
        // amplifies frame noise and flips adjacent ribbon vertices. Project
        // camera-right off the tangent in that singular region instead.
        if (dot(side, side) < 1.0e-6)
            side = cameraRight - tangentWorld * dot(cameraRight, tangentWorld);
        if (dot(side, side) < 1.0e-10)
            side = cross(cameraWorld[1].xyz, tangentWorld);
        if (dot(side, side) < 1.0e-10)
            side = vec3(1.0, 0.0, 0.0);
        side = normalize(side);
        if (dot(side, cameraRight) < 0.0)
            side = -side;
        worldPos = vec4(centerWorld.xyz + side * inBoneWeights.x, 1.0);
        if (inBoneIndices.y != 0u) {
            worldNormal = facing;
            worldTangent = vec4(tangentWorld, 1.0);
        } else {
            vec3 fallbackNormal = normalMatrix * v.normal;
            worldNormal = dot(fallbackNormal, fallbackNormal) > 1.0e-10
                ? normalize(fallbackNormal)
                : vec3(0.0, 0.0, 1.0);
            worldTangent = vec4(tangentWorld, 1.0);
        }
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
    v_LineColor = inxLineVertex ? vec4(v.color, inBoneWeights.y) : vec4(1.0);
    gl_Position = ubo.proj * ubo.view * worldPos;
${PASS_VERTEX_OUTPUT}
}
