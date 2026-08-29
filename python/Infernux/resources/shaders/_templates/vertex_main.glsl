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
        mat4 cameraWorld = inverse(ubo.view);
        vec3 facing;
        if (inBoneIndices.z == 0u) {
            // View alignment is a camera-plane billboard. Using a separate
            // point-to-camera vector at every sample makes long trails twist
            // under perspective and cross the near-parallel fallback at
            // different frames. One view-plane normal keeps the whole strip
            // coherent while the camera and authored points move.
            vec3 viewFacing = -cameraWorld[2].xyz;
            facing = dot(viewFacing, viewFacing) > 1.0e-10
                ? normalize(viewFacing)
                : vec3(0.0, 0.0, -1.0);
        } else {
            vec3 transformedFacing = normalMatrix * v.normal;
            facing = dot(transformedFacing, transformedFacing) > 1.0e-10
                ? normalize(transformedFacing)
                : vec3(0.0, 0.0, 1.0);
        }
        vec3 cameraRight = normalize(cameraWorld[0].xyz);
        vec3 cameraUp = normalize(cameraWorld[1].xyz);
        vec3 fallbackSide = cameraRight - tangentWorld * dot(cameraRight, tangentWorld);
        if (dot(fallbackSide, fallbackSide) < 1.0e-8)
            fallbackSide = cameraUp - tangentWorld * dot(cameraUp, tangentWorld);
        if (dot(fallbackSide, fallbackSide) < 1.0e-10)
            fallbackSide = abs(tangentWorld.x) < 0.9
                ? cross(tangentWorld, vec3(1.0, 0.0, 0.0))
                : cross(tangentWorld, vec3(0.0, 1.0, 0.0));
        fallbackSide = normalize(fallbackSide);

        // The width direction is cross(facing, tangent): continuous in the
        // tangent, and the CPU already keeps tangent signs hemisphere-
        // continuous along the strip, so no per-vertex sign correction is
        // applied here. Snapping the sign to a camera axis placed every
        // screen-horizontal segment exactly on the flip boundary
        // (dot(cross(f,t), fallback) == -t.y in view space), so float noise
        // flipped individual vertices a full width each frame. Only a segment
        // pointing almost straight at the camera is truly ill-conditioned;
        // there we blend toward the camera-plane fallback and align the
        // fallback's hemisphere to the geometric side, never the reverse.
        vec3 geometricSide = cross(facing, tangentWorld);
        float geometricLength = length(geometricSide);
        vec3 side;
        if (geometricLength > 0.20) {
            side = geometricSide / geometricLength;
        } else if (geometricLength > 1.0e-6) {
            geometricSide /= geometricLength;
            if (dot(fallbackSide, geometricSide) < 0.0)
                fallbackSide = -fallbackSide;
            float geometricWeight = smoothstep(0.025, 0.20, geometricLength);
            side = normalize(mix(fallbackSide, geometricSide, geometricWeight));
        } else {
            side = fallbackSide;
        }
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
