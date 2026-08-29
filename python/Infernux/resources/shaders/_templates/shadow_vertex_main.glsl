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
        mat4 lightViewWorld = inverse(shadowUBO.view);
        vec3 facingCandidate = inBoneIndices.z == 0u
            ? -lightViewWorld[2].xyz
            : normalMatrix * v.normal;
        vec3 facing = dot(facingCandidate, facingCandidate) > 1.0e-10
            ? normalize(facingCandidate)
            : vec3(0.0, 0.0, 1.0);
        vec3 viewRight = normalize(lightViewWorld[0].xyz);
        vec3 viewUp = normalize(lightViewWorld[1].xyz);
        vec3 fallbackSide = viewRight - tangentWorld * dot(viewRight, tangentWorld);
        if (dot(fallbackSide, fallbackSide) < 1.0e-8)
            fallbackSide = viewUp - tangentWorld * dot(viewUp, tangentWorld);
        if (dot(fallbackSide, fallbackSide) < 1.0e-10)
            fallbackSide = abs(tangentWorld.x) < 0.9
                ? cross(tangentWorld, vec3(1.0, 0.0, 0.0))
                : cross(tangentWorld, vec3(0.0, 1.0, 0.0));
        fallbackSide = normalize(fallbackSide);

        vec3 geometricSide = cross(facing, tangentWorld);
        float geometricLength = length(geometricSide);
        vec3 side = fallbackSide;
        if (geometricLength > 1.0e-6) {
            geometricSide /= geometricLength;
            if (dot(geometricSide, fallbackSide) < 0.0)
                geometricSide = -geometricSide;
            float geometricWeight = smoothstep(0.025, 0.20, geometricLength);
            side = normalize(mix(fallbackSide, geometricSide, geometricWeight));
        }
        worldPos = vec4(centerWorld.xyz + side * inBoneWeights.x, 1.0);
        if (inBoneWeights.z > 0.0) {
            vec3 towardLightCandidate = shadowUBO.light_vector.w < 0.5
                ? shadowUBO.light_vector.xyz
                : shadowUBO.light_vector.xyz - worldPos.xyz;
            if (dot(towardLightCandidate, towardLightCandidate) > 1.0e-10) {
                // Unity defines LineRenderer.shadowBias as a proportion of
                // line width. Move only line casters away from the light;
                // ordinary mesh shadows retain the shared raster bias path.
                worldPos.xyz -= normalize(towardLightCandidate) *
                    (2.0 * abs(inBoneWeights.x) * inBoneWeights.z);
            }
        }
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
