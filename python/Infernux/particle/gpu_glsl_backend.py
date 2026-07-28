"""AOT lowering from portable particle Kernel IR to Vulkan GLSL compute sources."""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
import hashlib
import math
import re
import struct
from typing import Any, Mapping
import zlib

from Infernux.graph.types import AssetReference, CoordinateSpace, TypeRef, ValueType
from Infernux.graph.ramp import Curve, Gradient

from .data_interface import SdfVolume, VectorField
from .hir import ParticleStage
from .kernel_ir import (
    KernelParameter,
    KernelCompileError,
    KernelInstruction,
    ParticleEmitterKernelIR,
    ParticleKernelFunction,
    ParticleKernelProgram,
)
from .kernel_semantics import KernelStage


class GpuParticleCompileError(ValueError):
    pass


_SPIRV_DESCRIPTOR_CACHE: dict[str, dict[str, Any]] = {}

_BILLBOARD_VERTEX_GLSL = """#version 450

struct ParticleInstance {
    vec4 position_size;
    vec4 color;
    vec4 rotation_custom;
    vec4 scale_custom;
    uvec4 ribbon_data;
    vec4 custom_data;
    vec4 previous_position_history;
};

layout(set = 0, binding = 0, std430) readonly buffer Instances {
    ParticleInstance instances[];
};
layout(set = 0, binding = 1, std430) readonly buffer RenderIndices {
    uint render_indices[];
};

layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    mat4 previous_view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
    vec4 alignment_reference;
} view;

layout(location = 0) out vec4 out_color;
layout(location = 1) out vec2 out_uv;
layout(location = 2) out vec3 out_world_position;
layout(location = 3) out vec3 out_world_normal;
layout(location = 4) out float out_view_depth;
#ifdef INX_PARTICLE_MOTION_PASS
layout(location = 15) out vec2 out_motion;
#endif

const vec2 corners[6] = vec2[](
    vec2(-1.0, -1.0), vec2(-1.0, 1.0), vec2(1.0, 1.0),
    vec2(-1.0, -1.0), vec2(1.0, 1.0), vec2(1.0, -1.0)
);

const vec2 uvs[6] = vec2[](
    vec2(0.0, 1.0), vec2(0.0, 0.0), vec2(1.0, 0.0),
    vec2(0.0, 1.0), vec2(1.0, 0.0), vec2(1.0, 1.0)
);

vec3 inx_safe_billboard_axis(vec3 value, vec3 fallback_value) {
    float length_squared = dot(value, value);
    return length_squared > 1.0e-10 ? value * inversesqrt(length_squared) : fallback_value;
}

void inx_billboard_basis(ParticleInstance instance, out vec3 right_axis, out vec3 up_axis) {
    vec3 camera_right = inx_safe_billboard_axis(view.camera_right.xyz, vec3(1.0, 0.0, 0.0));
    vec3 camera_up = inx_safe_billboard_axis(view.camera_up.xyz, vec3(0.0, 1.0, 0.0));
    vec3 camera_normal = inx_safe_billboard_axis(cross(camera_right, camera_up), vec3(0.0, 0.0, 1.0));
    int alignment = int(round(view.alignment_reference.w));
    if (alignment == 0) {
        right_axis = camera_right;
        up_axis = camera_up;
        return;
    }
    if (alignment == 1) {
        vec3 to_camera = inx_safe_billboard_axis(
            view.alignment_reference.xyz - instance.position_size.xyz,
            camera_normal);
        right_axis = inx_safe_billboard_axis(cross(camera_up, to_camera), camera_right);
        up_axis = inx_safe_billboard_axis(cross(to_camera, right_axis), camera_up);
        return;
    }
    vec3 requested_up = alignment == 2 ? view.alignment_reference.xyz : instance.custom_data.yzw;
    up_axis = inx_safe_billboard_axis(requested_up, camera_up);
    vec3 projected_right = cross(up_axis, camera_normal);
    if (dot(projected_right, projected_right) <= 1.0e-10)
        projected_right = camera_right - up_axis * dot(camera_right, up_axis);
    right_axis = inx_safe_billboard_axis(projected_right, camera_right);
    up_axis = inx_safe_billboard_axis(cross(camera_normal, right_axis), up_axis);
}

void main() {
    uint particle_index = view.lighting_control.y > 0.5
        ? render_indices[gl_InstanceIndex]
        : gl_InstanceIndex;
    ParticleInstance instance = instances[particle_index];
    vec2 corner = corners[gl_VertexIndex % 6];
    float cosine = cos(instance.rotation_custom.x);
    float sine = sin(instance.rotation_custom.x);
    corner = mat2(cosine, -sine, sine, cosine) * corner;
    vec3 billboard_right;
    vec3 billboard_up;
    inx_billboard_basis(instance, billboard_right, billboard_up);
    vec3 world_position = instance.position_size.xyz +
        (billboard_right * corner.x * instance.scale_custom.x +
         billboard_up * corner.y * instance.scale_custom.y) * instance.position_size.w;
    gl_Position = view.view_projection * vec4(world_position, 1.0);
    out_color = instance.color;
    vec2 flipbook_grid = max(view.rendering_control.zw, vec2(1.0));
    float flipbook_count = flipbook_grid.x * flipbook_grid.y;
    float flipbook_frame = mod(floor(max(instance.custom_data.x, 0.0)), flipbook_count);
    vec2 flipbook_cell = vec2(
        mod(flipbook_frame, flipbook_grid.x),
        floor(flipbook_frame / flipbook_grid.x));
    out_uv = (uvs[gl_VertexIndex % 6] + flipbook_cell) / flipbook_grid;
    out_world_position = world_position;
    out_world_normal = normalize(cross(billboard_right, billboard_up));
    out_view_depth = gl_Position.w;
#ifdef INX_PARTICLE_MOTION_PASS
    vec3 previous_world_position = instance.previous_position_history.xyz +
        (world_position - instance.position_size.xyz);
    vec4 previous_clip = view.previous_view_projection * vec4(previous_world_position, 1.0);
    vec2 current_ndc = gl_Position.xy / max(abs(gl_Position.w), 1e-6);
    vec2 previous_ndc = previous_clip.xy / max(abs(previous_clip.w), 1e-6);
    out_motion = (current_ndc - previous_ndc) * vec2(0.5, -0.5);
#endif
}
"""


def _motion_vertex_source(source: str) -> str:
    return source.replace("#version 450\n", "#version 450\n#define INX_PARTICLE_MOTION_PASS 1\n", 1)


_BILLBOARD_MOTION_FRAGMENT_GLSL = """#version 450
layout(location = 0) in vec4 in_color;
layout(location = 1) in vec2 in_uv;
layout(location = 15) in vec2 in_motion;
layout(location = 0) out vec2 out_motion;
layout(set = 0, binding = 2) uniform sampler2D texSampler;
void main() {
    if ((texture(texSampler, in_uv) * in_color).a <= 0.0001) discard;
    out_motion = in_motion;
}
"""

_BILLBOARD_FRAGMENT_GLSL = """#version 450

layout(location = 0) in vec4 in_color;
layout(location = 1) in vec2 in_uv;
layout(location = 0) out vec4 out_color;

layout(set = 0, binding = 2) uniform sampler2D texSampler;
layout(set = 0, binding = 15) uniform sampler2D _InxParticleSceneDepth;

layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    mat4 previous_view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
    vec4 alignment_reference;
} view;

float particle_eye_depth(float device_depth) {
    float numerator = view.depth_reconstruct.y - device_depth * view.depth_reconstruct.w;
    float denominator = device_depth * view.depth_reconstruct.z - view.depth_reconstruct.x;
    return max(0.0, -numerator / (abs(denominator) > 1e-7 ? denominator : 1e-7));
}

void main() {
    out_color = texture(texSampler, in_uv) * in_color * view.material_tint;
    if (view.camera_up.w > 0.5) {
        ivec2 depth_size = textureSize(_InxParticleSceneDepth, 0);
        ivec2 depth_coord = clamp(ivec2(gl_FragCoord.xy), ivec2(0), depth_size - ivec2(1));
        float scene_depth = particle_eye_depth(texelFetch(_InxParticleSceneDepth, depth_coord, 0).r);
        float particle_depth = particle_eye_depth(gl_FragCoord.z);
        out_color.a *= clamp((scene_depth - particle_depth) / max(view.camera_right.w, 1e-4), 0.0, 1.0);
    }
}
"""

_PARTICLE_FORWARD_PLUS_LIGHTING_GLSL = """
const int INX_MAX_DIRECTIONAL_LIGHTS = 4;
const int INX_MAX_POINT_LIGHTS = 64;
const int INX_MAX_SPOT_LIGHTS = 32;
const int INX_MAX_AREA_LIGHTS = 16;
const int INX_MAX_SHADOW_VIEWS = 64;

struct ParticleDirectionalLightData {
    vec4 direction;
    vec4 color;
    vec4 shadow_params;
    uvec4 metadata;
};
struct ParticlePointLightData {
    vec4 position;
    vec4 color;
    vec4 attenuation;
    vec4 shadow_params;
    uvec4 metadata;
};
struct ParticleSpotLightData {
    vec4 position;
    vec4 direction;
    vec4 color;
    vec4 spot_params;
    vec4 attenuation;
    vec4 shadow_params;
    uvec4 metadata;
};
struct ParticleAreaLightData {
    vec4 position_range;
    vec4 direction;
    vec4 right_width;
    vec4 up_height;
    vec4 color;
    vec4 shadow_params;
    uvec4 metadata;
};
struct ParticleShadowViewData {
    mat4 view_projection;
    vec4 atlas_scale_offset;
    vec4 depth_texel;
    vec4 split_data;
    uvec4 metadata;
};
struct CanonicalLightData {
    vec4 position_range;
    vec4 direction_spot;
    vec4 color_intensity;
    vec4 attenuation;
    vec4 area_right_width;
    vec4 area_up_height;
    uvec4 metadata;
    uvec4 identity_shadow;
};

layout(set = 1, binding = 0) uniform sampler2D particle_shadow_map;
layout(set = 1, binding = 1, std430) readonly buffer CanonicalLights {
    uvec4 counts_generation;
    CanonicalLightData lights[];
};
layout(set = 1, binding = 2, std430) readonly buffer ParticleTileHeaders {
    uvec4 tile_headers[];
};
layout(set = 1, binding = 3, std430) readonly buffer ParticleTileLightMasks {
    uint tile_light_masks[];
};
layout(std140, set = 1, binding = 4) uniform ParticleLighting {
    ivec4 light_counts;
    vec4 ambient_color;
    vec4 ambient_sky_color;
    vec4 ambient_equator_color;
    vec4 ambient_ground_color;
    vec4 camera_position;
    ParticleDirectionalLightData directional_lights[INX_MAX_DIRECTIONAL_LIGHTS];
    ParticlePointLightData point_lights[INX_MAX_POINT_LIGHTS];
    ParticleSpotLightData spot_lights[INX_MAX_SPOT_LIGHTS];
    ParticleAreaLightData area_lights[INX_MAX_AREA_LIGHTS];
    uvec4 shadow_view_header;
    ParticleShadowViewData shadow_views[INX_MAX_SHADOW_VIEWS];
} particle_lighting;

uint inx_particle_point_shadow_face(vec3 direction_from_light) {
    vec3 a = abs(direction_from_light);
    if (a.x >= a.y && a.x >= a.z) return direction_from_light.x >= 0.0 ? 0u : 1u;
    if (a.y >= a.z) return direction_from_light.y >= 0.0 ? 2u : 3u;
    return direction_from_light.z >= 0.0 ? 4u : 5u;
}

float inx_particle_shadow_bilinear_pcf(vec2 atlas_uv, float receiver_depth,
                                       vec2 receiver_plane_gradient,
                                       vec2 tile_min, vec2 tile_max, float atlas_size) {
    vec2 inverse_atlas = vec2(1.0 / atlas_size);
    vec2 pixel = atlas_uv * atlas_size - vec2(0.5);
    vec2 base = floor(pixel);
    vec2 fraction = fract(pixel);
    vec2 gather_uv = (base + vec2(1.0)) * inverse_atlas;
    gather_uv = clamp(gather_uv, tile_min + inverse_atlas * 0.5, tile_max - inverse_atlas * 0.5);
    vec4 depths = textureGather(particle_shadow_map, gather_uv, 0);
    vec4 receiver_depths;
    receiver_depths.w = receiver_depth + dot(receiver_plane_gradient,
        gather_uv + vec2(-0.5, -0.5) * inverse_atlas - atlas_uv);
    receiver_depths.z = receiver_depth + dot(receiver_plane_gradient,
        gather_uv + vec2( 0.5, -0.5) * inverse_atlas - atlas_uv);
    receiver_depths.x = receiver_depth + dot(receiver_plane_gradient,
        gather_uv + vec2(-0.5,  0.5) * inverse_atlas - atlas_uv);
    receiver_depths.y = receiver_depth + dot(receiver_plane_gradient,
        gather_uv + vec2( 0.5,  0.5) * inverse_atlas - atlas_uv);
    vec4 comparison = step(receiver_depths, depths);
    float lower = mix(comparison.w, comparison.z, fraction.x);
    float upper = mix(comparison.x, comparison.y, fraction.x);
    return mix(lower, upper, fraction.y);
}

vec2 inx_particle_shadow_receiver_plane_gradient(vec2 shadow_uv, float shadow_depth,
                                                  float atlas_size) {
    vec2 uv_dx = dFdx(shadow_uv);
    vec2 uv_dy = dFdy(shadow_uv);
    float depth_dx = dFdx(shadow_depth);
    float depth_dy = dFdy(shadow_depth);
    float determinant = uv_dx.x * uv_dy.y - uv_dx.y * uv_dy.x;
    if (abs(determinant) <= 1e-10) return vec2(0.0);
    vec2 gradient = vec2(
        uv_dy.y * depth_dx - uv_dx.y * depth_dy,
        -uv_dy.x * depth_dx + uv_dx.x * depth_dy) / determinant;
    float max_gradient = 0.002 * atlas_size;
    return clamp(gradient, vec2(-max_gradient), vec2(max_gradient));
}

mat2 inx_particle_shadow_kernel_rotation(vec2 atlas_uv, float atlas_size) {
    vec2 cell = floor(atlas_uv * atlas_size);
    float phase = fract(sin(dot(cell, vec2(12.9898, 78.233))) * 43758.5453) * 6.2831853;
    float sine = sin(phase);
    float cosine = cos(phase);
    return mat2(cosine, -sine, sine, cosine);
}

float inx_particle_sample_shadow_view_visibility(uint view_index, vec3 world_position, vec3 normal,
                                                  vec3 to_light, vec4 shadow_params,
                                                  bool soft_filter) {
    if (view.rendering_control.x < 0.5 || view_index >= particle_lighting.shadow_view_header.x) return 1.0;
    ParticleShadowViewData shadow_view = particle_lighting.shadow_views[view_index];
    vec3 receiver_position = world_position;
    if (shadow_view.metadata.x == 0u) {
        float normal_scale = 1.0 - clamp(dot(normalize(normal), normalize(to_light)), 0.0, 1.0);
        receiver_position += normalize(normal) *
            (shadow_params.z * shadow_view.depth_texel.z * normal_scale);
    }
    vec4 clip = shadow_view.view_projection * vec4(receiver_position, 1.0);
    if (abs(clip.w) < 0.000001) return 1.0;
    vec3 ndc = clip.xyz / clip.w;
    vec2 local_uv = ndc.xy * 0.5 + 0.5;
    if (any(lessThan(local_uv, vec2(0.0))) || any(greaterThan(local_uv, vec2(1.0))) ||
        ndc.z <= 0.0 || ndc.z >= 1.0) return 1.0;
    vec2 atlas_offset = shadow_view.atlas_scale_offset.zw;
    vec2 atlas_scale = shadow_view.atlas_scale_offset.xy;
    vec2 atlas_uv = atlas_offset + local_uv * atlas_scale;
    float atlas_size = max(float(particle_lighting.shadow_view_header.y), 1.0);
    vec2 texel = vec2(1.0 / atlas_size);
    vec2 tile_min = atlas_offset + texel * 0.5;
    vec2 tile_max = atlas_offset + atlas_scale - texel * 0.5;
    vec2 receiver_plane_gradient =
        inx_particle_shadow_receiver_plane_gradient(atlas_uv, ndc.z, atlas_size);
    if (!soft_filter) {
        return inx_particle_shadow_bilinear_pcf(
            atlas_uv, ndc.z, receiver_plane_gradient, tile_min, tile_max, atlas_size);
    }

    const vec2 filter_disk[16] = vec2[](
        vec2(-0.942, -0.399), vec2( 0.946, -0.769), vec2(-0.094, -0.929), vec2( 0.345,  0.294),
        vec2(-0.916,  0.458), vec2(-0.815, -0.879), vec2(-0.382,  0.277), vec2( 0.974,  0.756),
        vec2( 0.443, -0.975), vec2( 0.537, -0.474), vec2(-0.265, -0.418), vec2( 0.792,  0.191),
        vec2(-0.242,  0.997), vec2(-0.814,  0.914), vec2( 0.200,  0.786), vec2( 0.144, -0.141));
    // Keep particle and geometry shadow filtering bit-for-bit equivalent: a
    // stable per-texel rotation, fixed Poisson budget and no blocker search.
    mat2 kernel_rotation = inx_particle_shadow_kernel_rotation(atlas_uv, atlas_size);
    float radius = shadow_view.metadata.x == 0u
        ? clamp(1.5 + shadow_view.depth_texel.w * 1.75, 2.0, 16.0)
        : clamp(shadow_view.depth_texel.w, 0.75, 8.0);
    float visibility = 0.0;
    for (int index = 0; index < 16; ++index) {
        vec2 offset = kernel_rotation * filter_disk[index] * radius * texel;
        float tap_depth = ndc.z + dot(receiver_plane_gradient, offset);
        visibility += inx_particle_shadow_bilinear_pcf(
            atlas_uv + offset, tap_depth, receiver_plane_gradient, tile_min, tile_max, atlas_size);
    }
    return visibility * 0.0625;
}

float inx_particle_sample_shadow_view(uint view_index, vec3 world_position, vec3 normal,
                                      vec3 to_light, vec4 shadow_params) {
    if (view.rendering_control.x < 0.5 || view_index >= particle_lighting.shadow_view_header.x ||
        shadow_params.w < 0.5 || shadow_params.x <= 0.0) return 1.0;
    float visibility = inx_particle_sample_shadow_view_visibility(
        view_index, world_position, normal, to_light, shadow_params, shadow_params.w > 1.5);
    return mix(1.0, visibility, shadow_params.x);
}

float inx_particle_directional_shadow(CanonicalLightData light, vec3 world_position,
                                      vec3 normal, vec3 to_light, float view_depth) {
    uint first_view = light.identity_shadow.z;
    uint view_count = light.identity_shadow.w;
    uint available_views = particle_lighting.shadow_view_header.x;
    if (view_count == 0u || first_view >= available_views) return 1.0;
    view_count = min(view_count, available_views - first_view);
    uint selected = view_count - 1u;
    for (uint index = 0u; index < view_count; ++index) {
        if (view_depth < particle_lighting.shadow_views[first_view + index].split_data.y) {
            selected = index;
            break;
        }
    }
    vec4 shadow_params = vec4(light.attenuation.xyz, float(light.metadata.z));
    float shadow = inx_particle_sample_shadow_view(
        first_view + selected, world_position, normal, to_light, shadow_params);
    if (selected + 1u < view_count) {
        ParticleShadowViewData current = particle_lighting.shadow_views[first_view + selected];
        float overlap = max((current.split_data.y - current.split_data.x) * 0.1, 0.0001);
        float blend = smoothstep(0.0, overlap, current.split_data.y - view_depth);
        float next_shadow = inx_particle_sample_shadow_view(
            first_view + selected + 1u, world_position, normal, to_light, shadow_params);
        shadow = mix(next_shadow, shadow, blend);
    } else {
        ParticleShadowViewData last = particle_lighting.shadow_views[first_view + selected];
        float fade_width = max((last.split_data.y - last.split_data.x) * 0.1, 0.0001);
        float fade = smoothstep(last.split_data.y - fade_width, last.split_data.y, view_depth);
        shadow = mix(shadow, 1.0, fade);
    }
    return shadow;
}

float inx_particle_local_shadow(CanonicalLightData light, vec3 world_position,
                                vec3 normal, vec3 to_light) {
    uint first_view = light.identity_shadow.z;
    uint view_count = light.identity_shadow.w;
    uint available_views = particle_lighting.shadow_view_header.x;
    if (view_count == 0u || first_view >= available_views) return 1.0;
    view_count = min(view_count, available_views - first_view);
    vec4 shadow_params = vec4(light.attenuation.xyz, float(light.metadata.z));
    if (light.metadata.x == 2u || shadow_params.w < 1.5) {
        uint offset = light.metadata.x == 2u ? 0u :
            min(inx_particle_point_shadow_face(world_position - light.position_range.xyz), view_count - 1u);
        return inx_particle_sample_shadow_view(first_view + offset, world_position, normal, to_light,
                                               shadow_params);
    }

    vec3 radial = world_position - light.position_range.xyz;
    float radial_length = max(length(radial), 0.0001);
    vec3 radial_direction = radial / radial_length;
    vec3 helper = abs(radial_direction.y) < 0.99 ? vec3(0.0, 1.0, 0.0) : vec3(1.0, 0.0, 0.0);
    vec3 tangent = normalize(cross(helper, radial_direction));
    vec3 bitangent = cross(radial_direction, tangent);
    uint center_face = min(inx_particle_point_shadow_face(radial), view_count - 1u);
    ParticleShadowViewData center_view = particle_lighting.shadow_views[first_view + center_face];
    float inner_resolution = max(center_view.atlas_scale_offset.x *
        float(particle_lighting.shadow_view_header.y), 1.0);
    float radius_world = center_view.depth_texel.w * (2.0 * radial_length / inner_resolution);
    const vec2 disk[8] = vec2[](
        vec2(0.0, 0.0), vec2(0.7071, 0.0), vec2(-0.7071, 0.0),
        vec2(0.0, 0.7071), vec2(0.0, -0.7071), vec2(0.5, 0.5),
        vec2(-0.5, 0.5), vec2(0.5, -0.5));
    float visibility = 0.0;
    for (int sample_index = 0; sample_index < 8; ++sample_index) {
        vec3 sample_position = world_position +
            (tangent * disk[sample_index].x + bitangent * disk[sample_index].y) * radius_world;
        uint face = min(inx_particle_point_shadow_face(sample_position - light.position_range.xyz),
                        view_count - 1u);
        visibility += inx_particle_sample_shadow_view_visibility(
            first_view + face, sample_position, normal, to_light, shadow_params, false);
    }
    return mix(1.0, visibility * 0.125, shadow_params.x);
}

float inx_particle_distance_attenuation(float range, float distance_to_light) {
    float safe_range = max(range, 0.0001);
    float distance_squared = distance_to_light * distance_to_light;
    float ratio_squared = distance_squared / (safe_range * safe_range);
    float window = max(1.0 - ratio_squared * ratio_squared, 0.0);
    return (window * window) / (distance_squared + 1.0);
}

vec3 inx_particle_forward_plus(vec3 world_position, vec3 normal, float view_depth, vec3 albedo, bool two_sided) {
    vec3 result = albedo * particle_lighting.ambient_color.rgb * particle_lighting.ambient_color.w;
    uint object_layer_mask = floatBitsToUint(view.lighting_control.w);
    uint directional_count = counts_generation.x;
    for (uint index = 0u; index < directional_count; ++index) {
        CanonicalLightData light = lights[index];
        if ((light.metadata.w & 2u) == 0u) continue;
        if ((light.metadata.y & object_layer_mask) == 0u) continue;
        vec3 direction = normalize(-light.direction_spot.xyz);
        float ndotl = dot(normal, direction);
        ndotl = two_sided ? abs(ndotl) : max(ndotl, 0.0);
        float shadow = inx_particle_directional_shadow(light, world_position, normal, direction, view_depth);
        result += albedo * light.color_intensity.rgb * light.color_intensity.w * ndotl * shadow;
    }

    uvec4 grid = tile_headers[0];
    uvec2 tile_count = max(grid.xy, uvec2(1u));
    uint tile_size = max(grid.z, 1u);
    uvec2 tile = min(uvec2(gl_FragCoord.xy) / tile_size, tile_count - uvec2(1u));
    uvec4 header = tile_headers[1u + tile.y * tile_count.x + tile.x];
    for (uint word_entry = 0u; word_entry < header.y; ++word_entry) {
        uint light_mask = tile_light_masks[header.x + word_entry];
        while (light_mask != 0u) {
            uint bit = uint(findLSB(light_mask));
            uint local_index = word_entry * 32u + bit;
            light_mask &= light_mask - 1u;
            if (local_index >= header.z) continue;
            CanonicalLightData light = lights[directional_count + local_index];
            if ((light.metadata.w & 2u) == 0u) continue;
            if ((light.metadata.y & object_layer_mask) == 0u) continue;
            if (light.metadata.x == 3u) {
                vec3 from_light = world_position - light.position_range.xyz;
                float emitter_facing = dot(normalize(light.direction_spot.xyz), normalize(from_light));
                emitter_facing = light.direction_spot.w > 0.5 ? abs(emitter_facing) : max(emitter_facing, 0.0);
                if (emitter_facing <= 0.0001) continue;
                vec3 right = normalize(light.area_right_width.xyz) * light.area_right_width.w * 0.5;
                vec3 up = normalize(light.area_up_height.xyz) * light.area_up_height.w * 0.5;
                vec3 samples[4] = vec3[4](
                    light.position_range.xyz - right - up,
                    light.position_range.xyz + right - up,
                    light.position_range.xyz + right + up,
                    light.position_range.xyz - right + up);
                vec3 center_direction = normalize(light.position_range.xyz - world_position);
                float shadow = inx_particle_local_shadow(light, world_position, normal, center_direction);
                for (int sample_index = 0; sample_index < 4; ++sample_index) {
                    vec3 sample_vector = samples[sample_index] - world_position;
                    float sample_distance = length(sample_vector);
                    if (sample_distance <= 0.00001 || sample_distance >= light.position_range.w) continue;
                    vec3 sample_direction = sample_vector / sample_distance;
                    float ndotl = dot(normal, sample_direction);
                    ndotl = two_sided ? abs(ndotl) : max(ndotl, 0.0);
                    float falloff = inx_particle_distance_attenuation(light.position_range.w, sample_distance);
                    result += albedo * light.color_intensity.rgb * light.color_intensity.w *
                              falloff * emitter_facing * ndotl * shadow * 0.25;
                }
                continue;
            }
            vec3 light_vector = light.position_range.xyz - world_position;
            float distance_to_light = length(light_vector);
            float range = max(light.position_range.w, 0.0001);
            if (distance_to_light <= 0.00001 || distance_to_light >= range) continue;
            vec3 direction = light_vector / distance_to_light;
            float falloff = inx_particle_distance_attenuation(range, distance_to_light);
            if (light.metadata.x == 2u) {
                float cone = dot(direction, -normalize(light.direction_spot.xyz));
                falloff *= smoothstep(light.direction_spot.w, light.attenuation.w, cone);
            }
            float ndotl = dot(normal, direction);
            ndotl = two_sided ? abs(ndotl) : max(ndotl, 0.0);
            float shadow = inx_particle_local_shadow(light, world_position, normal, direction);
            result += albedo * light.color_intensity.rgb * light.color_intensity.w * falloff * ndotl * shadow;
        }
    }
    return result;
}
"""

_BILLBOARD_FORWARD_PLUS_FRAGMENT_GLSL = """#version 450

layout(location = 0) in vec4 in_color;
layout(location = 1) in vec2 in_uv;
layout(location = 2) in vec3 in_world_position;
layout(location = 3) in vec3 in_world_normal;
layout(location = 4) in float in_view_depth;
layout(location = 0) out vec4 out_color;

layout(set = 0, binding = 2) uniform sampler2D texSampler;
layout(set = 0, binding = 15) uniform sampler2D _InxParticleSceneDepth;

layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    mat4 previous_view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
    vec4 alignment_reference;
} view;
""" + _PARTICLE_FORWARD_PLUS_LIGHTING_GLSL + """
float particle_eye_depth(float device_depth) {
    float numerator = view.depth_reconstruct.y - device_depth * view.depth_reconstruct.w;
    float denominator = device_depth * view.depth_reconstruct.z - view.depth_reconstruct.x;
    return max(0.0, -numerator / (abs(denominator) > 1e-7 ? denominator : 1e-7));
}

void main() {
    vec4 base = texture(texSampler, in_uv) * in_color * view.material_tint;
    vec2 centered_uv = in_uv * 2.0 - 1.0;
    float edge_width = max(view.lighting_control.z, 0.0001);
    base.a *= 1.0 - smoothstep(1.0 - edge_width, 1.0, length(centered_uv));
    if (view.lighting_control.x > 0.5) {
        base.rgb = inx_particle_forward_plus(
            in_world_position, normalize(in_world_normal), in_view_depth, base.rgb, true
        );
    }
    if (view.camera_up.w > 0.5) {
        ivec2 depth_size = textureSize(_InxParticleSceneDepth, 0);
        ivec2 depth_coord = clamp(ivec2(gl_FragCoord.xy), ivec2(0), depth_size - ivec2(1));
        float scene_depth = particle_eye_depth(texelFetch(_InxParticleSceneDepth, depth_coord, 0).r);
        float particle_depth = particle_eye_depth(gl_FragCoord.z);
        base.a *= clamp((scene_depth - particle_depth) / max(view.camera_right.w, 1e-4), 0.0, 1.0);
    }
    out_color = base;
}
"""

_BILLBOARD_PICKING_FRAGMENT_GLSL = """#version 450

layout(location = 0) out uvec2 out_object_id;

layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    mat4 previous_view_projection;
    vec4 camera_right;
    vec4 camera_up;
    uvec4 object_id;
} view;

void main() {
    out_object_id = view.object_id.xy;
}
"""

_MESH_VERTEX_GLSL = """#version 450

struct ParticleInstance {
    vec4 position_size;
    vec4 color;
    vec4 rotation_custom;
    vec4 scale_custom;
    uvec4 ribbon_data;
    vec4 custom_data;
    vec4 previous_position_history;
};

struct ParticleMeshVertex {
    vec4 position;
    vec4 normal;
    vec4 tangent;
    vec4 color;
    vec4 uv;
};

layout(set = 0, binding = 0, std430) readonly buffer Instances {
    ParticleInstance instances[];
};
layout(set = 0, binding = 1, std430) readonly buffer RenderIndices {
    uint render_indices[];
};
layout(set = 0, binding = 2, std430) readonly buffer MeshVertices {
    ParticleMeshVertex mesh_vertices[];
};
layout(set = 0, binding = 3, std430) readonly buffer MeshIndices {
    uint mesh_indices[];
};

layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    mat4 previous_view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
    vec4 alignment_reference;
} view;

layout(location = 0) out vec4 out_color;
layout(location = 1) out vec3 out_normal;
layout(location = 2) out vec2 out_uv;
layout(location = 3) out vec3 out_world_position;
layout(location = 4) out float out_view_depth;
#ifdef INX_PARTICLE_MOTION_PASS
layout(location = 15) out vec2 out_motion;
#endif

void main() {
    uint particle_index = render_indices[gl_InstanceIndex];
    ParticleInstance instance = instances[particle_index];
    ParticleMeshVertex vertex = mesh_vertices[mesh_indices[gl_VertexIndex]];
    vec3 angles = instance.rotation_custom.yzw;
    vec3 cosine = cos(angles);
    vec3 sine = sin(angles);
    mat3 rotation_x = mat3(
        1.0, 0.0, 0.0,
        0.0, cosine.x, sine.x,
        0.0, -sine.x, cosine.x
    );
    mat3 rotation_y = mat3(
        cosine.y, 0.0, -sine.y,
        0.0, 1.0, 0.0,
        sine.y, 0.0, cosine.y
    );
    mat3 rotation_z = mat3(
        cosine.z, sine.z, 0.0,
        -sine.z, cosine.z, 0.0,
        0.0, 0.0, 1.0
    );
    mat3 rotation = rotation_z * rotation_y * rotation_x;
    vec3 particle_scale = instance.scale_custom.xyz * instance.position_size.w;
    vec3 world_position = instance.position_size.xyz +
        rotation * (vertex.position.xyz * particle_scale);
    vec3 inverse_scale = sign(particle_scale) / max(abs(particle_scale), vec3(1e-6));
    vec3 transformed_normal = rotation * (vertex.normal.xyz * inverse_scale);
    float normal_length_squared = dot(transformed_normal, transformed_normal);
    out_normal = normal_length_squared > 1e-12
        ? transformed_normal * inversesqrt(normal_length_squared)
        : vec3(0.0, 1.0, 0.0);
    vec4 unbiassed_clip = view.view_projection * vec4(world_position, 1.0);
    if (view.rendering_control.y > 0.5) {
        vec3 to_light = view.camera_right.w > 0.5
            ? normalize(view.camera_right.xyz - world_position)
            : normalize(view.camera_right.xyz);
        float perspective_scale = view.camera_right.w > 0.5
            ? clamp(abs(unbiassed_clip.w) / max(view.camera_up.w, 0.000001), 0.001, 1.0)
            : 1.0;
        float world_texel = max(view.camera_up.z * perspective_scale, 0.000001);
        float normal_scale = 1.0 - clamp(dot(out_normal, to_light), 0.0, 1.0);
        world_position -= to_light * (view.camera_up.x * world_texel);
        world_position -= out_normal * (view.camera_up.y * world_texel * normal_scale);
    }
    gl_Position = view.view_projection * vec4(world_position, 1.0);
    if (view.rendering_control.y > 0.5 && view.camera_right.w < 0.5) {
        gl_Position.z = max(gl_Position.z, 0.0);
    }
    out_color = instance.color * vertex.color;
    out_uv = vertex.uv.xy;
    out_world_position = world_position;
    out_view_depth = gl_Position.w;
#ifdef INX_PARTICLE_MOTION_PASS
    vec3 previous_world_position = instance.previous_position_history.xyz +
        (world_position - instance.position_size.xyz);
    vec4 previous_clip = view.previous_view_projection * vec4(previous_world_position, 1.0);
    vec2 current_ndc = gl_Position.xy / max(abs(gl_Position.w), 1e-6);
    vec2 previous_ndc = previous_clip.xy / max(abs(previous_clip.w), 1e-6);
    out_motion = (current_ndc - previous_ndc) * vec2(0.5, -0.5);
#endif
}
"""

_MESH_MOTION_FRAGMENT_GLSL = """#version 450
layout(location = 15) in vec2 in_motion;
layout(location = 0) out vec2 out_motion;
void main() { out_motion = in_motion; }
"""

_MESH_FRAGMENT_GLSL = """#version 450

layout(location = 0) in vec4 in_color;
layout(location = 1) in vec3 in_normal;
layout(location = 2) in vec2 in_uv;
layout(location = 0) out vec4 out_color;

layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    mat4 previous_view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
    vec4 alignment_reference;
} view;

void main() {
    out_color = in_color * view.material_tint;
}
"""

_MESH_FORWARD_PLUS_FRAGMENT_GLSL = """#version 450

layout(location = 0) in vec4 in_color;
layout(location = 1) in vec3 in_normal;
layout(location = 2) in vec2 in_uv;
layout(location = 3) in vec3 in_world_position;
layout(location = 4) in float in_view_depth;
layout(location = 0) out vec4 out_color;

layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    mat4 previous_view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
    vec4 alignment_reference;
} view;
""" + _PARTICLE_FORWARD_PLUS_LIGHTING_GLSL + """
void main() {
    vec4 base = in_color * view.material_tint;
    if (view.lighting_control.x > 0.5) {
        base.rgb = inx_particle_forward_plus(
            in_world_position, normalize(in_normal), in_view_depth, base.rgb, false
        );
    }
    out_color = base;
}
"""


@dataclass(frozen=True)
class GpuParticleContinuationSource:
    record_stride: int
    lane_count: int
    join_count: int
    prepare: str
    classify: str
    dispatch: str

    def __post_init__(self) -> None:
        if (
            type(self.record_stride) is not int
            or self.record_stride < 64
            or self.record_stride % 16
            or type(self.lane_count) is not int
            or self.lane_count <= 0
            or type(self.join_count) is not int
            or self.join_count < 0
            or not all(
                type(source) is str and source
                for source in (self.prepare, self.classify, self.dispatch)
            )
        ):
            raise GpuParticleCompileError("GPU continuation source is invalid")

    def stages(self) -> dict[str, str]:
        return {
            "prepare": self.prepare,
            "classify": self.classify,
            "dispatch": self.dispatch,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_stride": self.record_stride,
            "lane_count": self.lane_count,
            "join_count": self.join_count,
            "stages": self.stages(),
        }


@dataclass(frozen=True)
class GpuParticleEmitterSource:
    stable_id: str
    kernel_hash: str
    attribute_fields: tuple[tuple[str, str, str, int, int], ...]
    state_stride: int
    event_output_stages: tuple[str, ...]
    bootstrap: str
    init: str
    event_init: str
    update: str
    render_reset: str
    rendering: str
    continuation: GpuParticleContinuationSource | None
    data_interfaces: tuple[dict[str, Any], ...] = ()
    data_interface_layout: dict[str, Any] = field(default_factory=dict)

    def stages(self) -> dict[str, str]:
        return {
            "bootstrap": self.bootstrap,
            "init": self.init,
            "event_init": self.event_init,
            "update": self.update,
            "render_reset": self.render_reset,
            "rendering": self.rendering,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "kernel_hash": self.kernel_hash,
            "attribute_fields": [
                {
                    "stable_id": stable_id,
                    "field": field,
                    "glsl_type": glsl_type,
                    "offset": offset,
                    "byte_size": byte_size,
                }
                for stable_id, field, glsl_type, offset, byte_size in self.attribute_fields
            ],
            "state_stride": self.state_stride,
            "event_output_stages": list(self.event_output_stages),
            "data_interfaces": [dict(value) for value in self.data_interfaces],
            "data_interface_layout": dict(self.data_interface_layout),
            "continuation": (
                self.continuation.to_dict()
                if self.continuation is not None
                else None
            ),
            "stages": self.stages(),
        }


@dataclass(frozen=True)
class GpuParticleProgramSource:
    kernel_hash: str
    parameters: tuple[KernelParameter, ...]
    emitters: tuple[GpuParticleEmitterSource, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "$schema": "infernux.particle_gpu_glsl",
            "kernel_hash": self.kernel_hash,
            "parameters": [parameter.to_dict() for parameter in self.parameters],
            "emitters": [emitter.to_dict() for emitter in self.emitters],
        }


class GpuParticleGlslLowerer:
    """Generate portable GLSL 450 consumed by the Vulkan RHI backend."""

    def lower(self, program: ParticleKernelProgram) -> GpuParticleProgramSource:
        if not isinstance(program, ParticleKernelProgram):
            raise TypeError("GPU particle lowering requires ParticleKernelProgram")
        return GpuParticleProgramSource(
            program.kernel_hash,
            program.parameters,
            tuple(
                self._lower_emitter(
                    program.kernel_hash,
                    emitter,
                    program.events,
                    program.parameters,
                    emitter_index,
                )
                for emitter_index, emitter in enumerate(program.emitters)
            ),
        )

    def _lower_emitter(
        self,
        kernel_hash: str,
        emitter: ParticleEmitterKernelIR,
        events,
        parameters: tuple[KernelParameter, ...],
        emitter_index: int,
    ) -> GpuParticleEmitterSource:
        fields = _attribute_fields(emitter)
        attribute_layout, state_stride = _std430_attribute_layout(fields)
        data_interface_layout = _data_interface_layout(emitter, parameters)
        prelude = _shader_prelude(
            fields,
            emitter.random_seed,
            data_interface_layout,
            len(parameters),
        )
        parameter_slots = {
            parameter.stable_id: (parameter.slot, parameter.value_type)
            for parameter in parameters
        }
        continuation_lane_indices = {
            stable_id: index
            for index, stable_id in enumerate(
                sorted({item.lane_stable_id for item in emitter.suspensions})
            )
        }
        continuation_join_indices = {
            (flow.lifecycle_stage, join.source_node_uid): index
            for index, (flow, join) in enumerate(
                (flow, join)
                for flow in emitter.flows
                for join in flow.joins
            )
        }
        init_flows = tuple(
            flow for flow in emitter.flows if flow.kernel_stage is KernelStage.INIT
        )
        update_flows = tuple(
            flow for flow in emitter.flows if flow.kernel_stage is KernelStage.UPDATE
        )
        bootstrap = prelude + _bootstrap_main()
        init_body, _, init_events = _StageCompiler(
            emitter,
            fields,
            data_interface_layout,
            events,
            emitter_index,
            parameter_slots=parameter_slots,
            continuation_lane_indices=continuation_lane_indices,
            continuation_join_indices=continuation_join_indices,
        ).compile(emitter.init, init_flows)
        event_init_body, _, event_init_events = _StageCompiler(
            emitter,
            fields,
            data_interface_layout,
            events,
            emitter_index,
            event_input=True,
            parameter_slots=parameter_slots,
            continuation_lane_indices=continuation_lane_indices,
            continuation_join_indices=continuation_join_indices,
        ).compile(emitter.init, init_flows)
        update_body, _, update_events = _StageCompiler(
            emitter,
            fields,
            data_interface_layout,
            events,
            emitter_index,
            parameter_slots=parameter_slots,
            continuation_lane_indices=continuation_lane_indices,
            continuation_join_indices=continuation_join_indices,
        ).compile(emitter.update, update_flows)
        rendering_body, exports, rendering_events = _StageCompiler(
            emitter,
            fields,
            data_interface_layout,
            events,
            emitter_index,
            parameter_slots=parameter_slots,
        ).compile(emitter.rendering)
        required = {"builtin.position", "builtin.size", "builtin.color", "builtin.rotation"}
        if not required.issubset(exports):
            missing = ", ".join(sorted(required - set(exports)))
            raise GpuParticleCompileError(
                f"particle rendering stage does not export {missing}"
            )
        continuation = _continuation_source(
            emitter,
            fields,
            data_interface_layout,
            events,
            emitter_index,
            parameter_slots,
            continuation_lane_indices,
            continuation_join_indices,
        )
        continuation_bindings = (
            _continuation_bindings_glsl(5) if continuation is not None else ""
        )
        return GpuParticleEmitterSource(
            emitter.stable_id,
            kernel_hash,
            tuple(
                (stable_id, field, _glsl_type(value_type), offset, byte_size)
                for stable_id, value_type, field, offset, byte_size in attribute_layout
            ),
            state_stride,
            tuple(
                stage
                for stage, body in (
                    ("init", init_events),
                    ("update", update_events),
                    ("rendering", rendering_events),
                )
                if body
            ),
            bootstrap,
            prelude
            + continuation_bindings
            + (_event_output_bindings(4) if init_events else "")
            + _init_main(init_body, init_events, emitter, fields),
            prelude
            + continuation_bindings
            + _event_init_bindings()
            + (_event_output_bindings(4) if event_init_events else "")
            + _event_init_main(
                event_init_body, event_init_events, emitter, fields
            ),
            prelude
            + continuation_bindings
            + (_event_output_bindings(4) if update_events else "")
            + _update_main(update_body, update_events, emitter, fields),
            prelude + _render_reset_main(),
            prelude
            + (_event_output_bindings(4) if rendering_events else "")
            + _rendering_main(rendering_body, rendering_events, exports),
            continuation,
            tuple(interface.to_dict() for interface in emitter.data_interfaces),
            data_interface_layout,
        )


def _compile_cached_graphics(
    native,
    sources: Mapping[str, str],
    label: str,
) -> dict[str, dict[str, Any]]:
    keys = {
        stage: hashlib.sha256(
            (f"vulkan1.2-spirv1.5\0{stage}\0" + source).encode("utf-8")
        ).hexdigest()
        for stage, source in sources.items()
    }
    missing = {
        stage: sources[stage]
        for stage, key in keys.items()
        if key not in _SPIRV_DESCRIPTOR_CACHE
    }
    compiled = native._compile_graphics_glsl_batch(missing, label) if missing else {}
    if set(compiled) != set(missing):
        raise GpuParticleCompileError(
            f"engine graphics compiler returned incomplete stages for {label}"
        )
    result = {}
    for stage, key in keys.items():
        descriptor = _SPIRV_DESCRIPTOR_CACHE.get(key)
        if descriptor is None:
            binary = bytes(compiled[stage])
            if len(binary) < 20 or int.from_bytes(binary[:4], "little") != 0x07230203:
                raise GpuParticleCompileError(
                    f"engine graphics compiler returned invalid SPIR-V for {label}:{stage}"
                )
            descriptor = {
                "byte_size": len(binary),
                "sha256": hashlib.sha256(binary).hexdigest(),
                "zlib_base64": base64.b64encode(zlib.compress(binary, 9)).decode("ascii"),
            }
            _SPIRV_DESCRIPTOR_CACHE[key] = descriptor
        result[stage] = dict(descriptor)
    return result


def compile_gpu_particle_spirv(program: GpuParticleProgramSource) -> dict[str, Any]:
    """Compile and compress all generated stages using the engine glslang service."""
    from Infernux.lib import _Infernux as native

    emitters = []
    for emitter in program.emitters:
        sources = dict(emitter.stages())
        if emitter.continuation is not None:
            sources.update(
                {
                    f"continuation.{stage}": source
                    for stage, source in emitter.continuation.stages().items()
                }
            )
        source_keys = {
            stage: hashlib.sha256(
                ("vulkan1.2-spirv1.5\0" + source).encode("utf-8")
            ).hexdigest()
            for stage, source in sources.items()
        }
        missing = {
            stage: sources[stage]
            for stage, key in source_keys.items()
            if key not in _SPIRV_DESCRIPTOR_CACHE
        }
        compiled = (
            native._compile_compute_glsl_batch(
                missing, f"particle:{emitter.stable_id}"
            )
            if missing
            else {}
        )
        if set(compiled) != set(missing):
            raise GpuParticleCompileError("engine compute compiler returned incomplete stages")
        encoded_sources = {}
        for stage, key in sorted(source_keys.items()):
            descriptor = _SPIRV_DESCRIPTOR_CACHE.get(key)
            if descriptor is None:
                binary = bytes(compiled[stage])
                if len(binary) < 20 or int.from_bytes(binary[:4], "little") != 0x07230203:
                    raise GpuParticleCompileError(
                        f"engine compute compiler returned invalid SPIR-V for {stage}"
                    )
                descriptor = {
                    "byte_size": len(binary),
                    "sha256": hashlib.sha256(binary).hexdigest(),
                    "zlib_base64": base64.b64encode(zlib.compress(binary, 9)).decode("ascii"),
                }
                _SPIRV_DESCRIPTOR_CACHE[key] = descriptor
            encoded_sources[stage] = dict(descriptor)
        stages = {
            stage: descriptor
            for stage, descriptor in encoded_sources.items()
            if not stage.startswith("continuation.")
        }
        continuation = None
        if emitter.continuation is not None:
            continuation = {
                "record_stride": emitter.continuation.record_stride,
                "lane_count": emitter.continuation.lane_count,
                "join_count": emitter.continuation.join_count,
                "stages": {
                    stage: encoded_sources[f"continuation.{stage}"]
                    for stage in emitter.continuation.stages()
                },
            }
        emitters.append(
            {
                "stable_id": emitter.stable_id,
                "stages": stages,
                "continuation": continuation,
            }
        )
    graphics_sources = {
        "vertex": _BILLBOARD_VERTEX_GLSL,
        "fragment": _BILLBOARD_FRAGMENT_GLSL,
    }
    graphics_keys = {
        stage: hashlib.sha256(
            (f"vulkan1.2-spirv1.5\0{stage}\0" + source).encode("utf-8")
        ).hexdigest()
        for stage, source in graphics_sources.items()
    }
    missing_graphics = {
        stage: graphics_sources[stage]
        for stage, key in graphics_keys.items()
        if key not in _SPIRV_DESCRIPTOR_CACHE
    }
    compiled_graphics = (
        native._compile_graphics_glsl_batch(
            missing_graphics, "particle:builtin-billboard"
        )
        if missing_graphics
        else {}
    )
    if set(compiled_graphics) != set(missing_graphics):
        raise GpuParticleCompileError(
            "engine graphics compiler returned incomplete billboard stages"
        )
    billboard = {}
    for stage, key in sorted(graphics_keys.items()):
        descriptor = _SPIRV_DESCRIPTOR_CACHE.get(key)
        if descriptor is None:
            binary = bytes(compiled_graphics[stage])
            if (
                len(binary) < 20
                or int.from_bytes(binary[:4], "little") != 0x07230203
            ):
                raise GpuParticleCompileError(
                    f"engine graphics compiler returned invalid SPIR-V for {stage}"
                )
            descriptor = {
                "byte_size": len(binary),
                "sha256": hashlib.sha256(binary).hexdigest(),
                "zlib_base64": base64.b64encode(zlib.compress(binary, 9)).decode(
                    "ascii"
                ),
            }
            _SPIRV_DESCRIPTOR_CACHE[key] = descriptor
        billboard[stage] = dict(descriptor)

    picking_key = hashlib.sha256(
        ("vulkan1.2-spirv1.5\0fragment\0" + _BILLBOARD_PICKING_FRAGMENT_GLSL).encode("utf-8")
    ).hexdigest()
    picking_descriptor = _SPIRV_DESCRIPTOR_CACHE.get(picking_key)
    if picking_descriptor is None:
        compiled_picking = native._compile_graphics_glsl_batch(
            {"fragment": _BILLBOARD_PICKING_FRAGMENT_GLSL},
            "particle:builtin-billboard-picking",
        )
        binary = bytes(compiled_picking.get("fragment", b""))
        if len(binary) < 20 or int.from_bytes(binary[:4], "little") != 0x07230203:
            raise GpuParticleCompileError(
                "engine graphics compiler returned invalid particle picking SPIR-V"
            )
        picking_descriptor = {
            "byte_size": len(binary),
            "sha256": hashlib.sha256(binary).hexdigest(),
            "zlib_base64": base64.b64encode(zlib.compress(binary, 9)).decode("ascii"),
        }
        _SPIRV_DESCRIPTOR_CACHE[picking_key] = picking_descriptor
    billboard["picking_fragment"] = dict(picking_descriptor)

    billboard_forward_plus_key = hashlib.sha256(
        (
            "vulkan1.2-spirv1.5\0fragment\0"
            + _BILLBOARD_FORWARD_PLUS_FRAGMENT_GLSL
        ).encode("utf-8")
    ).hexdigest()
    billboard_forward_plus = _SPIRV_DESCRIPTOR_CACHE.get(
        billboard_forward_plus_key
    )
    if billboard_forward_plus is None:
        compiled = native._compile_graphics_glsl_batch(
            {"fragment": _BILLBOARD_FORWARD_PLUS_FRAGMENT_GLSL},
            "particle:builtin-billboard-forward-plus",
        )
        binary = bytes(compiled.get("fragment", b""))
        if len(binary) < 20 or int.from_bytes(binary[:4], "little") != 0x07230203:
            raise GpuParticleCompileError(
                "engine graphics compiler returned invalid particle Forward+ billboard SPIR-V"
            )
        billboard_forward_plus = {
            "byte_size": len(binary),
            "sha256": hashlib.sha256(binary).hexdigest(),
            "zlib_base64": base64.b64encode(zlib.compress(binary, 9)).decode("ascii"),
        }
        _SPIRV_DESCRIPTOR_CACHE[billboard_forward_plus_key] = billboard_forward_plus
    billboard["forward_plus_fragment"] = dict(billboard_forward_plus)
    billboard_motion = _compile_cached_graphics(
        native,
        {
            "vertex": _motion_vertex_source(_BILLBOARD_VERTEX_GLSL),
            "fragment": _BILLBOARD_MOTION_FRAGMENT_GLSL,
        },
        "particle:builtin-billboard-motion",
    )
    billboard["motion_vertex"] = billboard_motion["vertex"]
    billboard["motion_fragment"] = billboard_motion["fragment"]

    mesh_sources = {
        "vertex": _MESH_VERTEX_GLSL,
        "fragment": _MESH_FRAGMENT_GLSL,
    }
    mesh_keys = {
        stage: hashlib.sha256(
            (f"vulkan1.2-spirv1.5\0{stage}\0" + source).encode("utf-8")
        ).hexdigest()
        for stage, source in mesh_sources.items()
    }
    missing_mesh = {
        stage: mesh_sources[stage]
        for stage, key in mesh_keys.items()
        if key not in _SPIRV_DESCRIPTOR_CACHE
    }
    compiled_mesh = (
        native._compile_graphics_glsl_batch(missing_mesh, "particle:builtin-mesh")
        if missing_mesh
        else {}
    )
    if set(compiled_mesh) != set(missing_mesh):
        raise GpuParticleCompileError(
            "engine graphics compiler returned incomplete particle mesh stages"
        )
    mesh = {}
    for stage, key in sorted(mesh_keys.items()):
        descriptor = _SPIRV_DESCRIPTOR_CACHE.get(key)
        if descriptor is None:
            binary = bytes(compiled_mesh[stage])
            if len(binary) < 20 or int.from_bytes(binary[:4], "little") != 0x07230203:
                raise GpuParticleCompileError(
                    f"engine graphics compiler returned invalid mesh SPIR-V for {stage}"
                )
            descriptor = {
                "byte_size": len(binary),
                "sha256": hashlib.sha256(binary).hexdigest(),
                "zlib_base64": base64.b64encode(zlib.compress(binary, 9)).decode("ascii"),
            }
            _SPIRV_DESCRIPTOR_CACHE[key] = descriptor
        mesh[stage] = dict(descriptor)
    mesh["picking_fragment"] = dict(picking_descriptor)

    mesh_forward_plus_key = hashlib.sha256(
        ("vulkan1.2-spirv1.5\0fragment\0" + _MESH_FORWARD_PLUS_FRAGMENT_GLSL).encode(
            "utf-8"
        )
    ).hexdigest()
    mesh_forward_plus = _SPIRV_DESCRIPTOR_CACHE.get(mesh_forward_plus_key)
    if mesh_forward_plus is None:
        compiled = native._compile_graphics_glsl_batch(
            {"fragment": _MESH_FORWARD_PLUS_FRAGMENT_GLSL},
            "particle:builtin-mesh-forward-plus",
        )
        binary = bytes(compiled.get("fragment", b""))
        if len(binary) < 20 or int.from_bytes(binary[:4], "little") != 0x07230203:
            raise GpuParticleCompileError(
                "engine graphics compiler returned invalid particle Forward+ mesh SPIR-V"
            )
        mesh_forward_plus = {
            "byte_size": len(binary),
            "sha256": hashlib.sha256(binary).hexdigest(),
            "zlib_base64": base64.b64encode(zlib.compress(binary, 9)).decode("ascii"),
        }
        _SPIRV_DESCRIPTOR_CACHE[mesh_forward_plus_key] = mesh_forward_plus
    mesh["forward_plus_fragment"] = dict(mesh_forward_plus)
    mesh_motion = _compile_cached_graphics(
        native,
        {
            "vertex": _motion_vertex_source(_MESH_VERTEX_GLSL),
            "fragment": _MESH_MOTION_FRAGMENT_GLSL,
        },
        "particle:builtin-mesh-motion",
    )
    mesh["motion_vertex"] = mesh_motion["vertex"]
    mesh["motion_fragment"] = mesh_motion["fragment"]

    return {
        "$schema": "infernux.particle_gpu_spirv",
        "target": "vulkan1.2-spirv1.5",
        "kernel_hash": program.kernel_hash,
        "parameters": [parameter.to_dict() for parameter in program.parameters],
        "parameter_words": list(pack_gpu_particle_parameters(program.parameters)),
        "emitters": emitters,
        "billboard": billboard,
        "mesh": mesh,
    }


def validate_gpu_particle_spirv(
    value: Any, program: GpuParticleProgramSource
) -> dict[str, Any]:
    """Strictly validate a persisted GPU binary payload without recompiling it."""
    expected_top = {
        "$schema",
        "target",
        "kernel_hash",
        "parameters",
        "parameter_words",
        "emitters",
        "billboard",
        "mesh",
    }
    if type(value) is not dict or set(value) != expected_top:
        raise GpuParticleCompileError("particle GPU SPIR-V payload is invalid")
    if (
        value["$schema"] != "infernux.particle_gpu_spirv"
        or value["target"] != "vulkan1.2-spirv1.5"
        or value["kernel_hash"] != program.kernel_hash
        or value["parameters"] != [parameter.to_dict() for parameter in program.parameters]
        or value["parameter_words"] != list(pack_gpu_particle_parameters(program.parameters))
        or type(value["emitters"]) is not list
        or len(value["emitters"]) != len(program.emitters)
    ):
        raise GpuParticleCompileError("particle GPU SPIR-V header is incompatible")
    billboard = value["billboard"]
    if type(billboard) is not dict or set(billboard) != {
        "vertex", "fragment", "forward_plus_fragment", "picking_fragment",
        "motion_vertex", "motion_fragment",
    }:
        raise GpuParticleCompileError("particle GPU billboard binary is incomplete")
    mesh = value["mesh"]
    if type(mesh) is not dict or set(mesh) != {
        "vertex", "fragment", "forward_plus_fragment", "picking_fragment",
        "motion_vertex", "motion_fragment",
    }:
        raise GpuParticleCompileError("particle GPU mesh binary is incomplete")
    for encoded, source in zip(value["emitters"], program.emitters):
        if type(encoded) is not dict or set(encoded) != {
            "stable_id",
            "stages",
            "continuation",
        }:
            raise GpuParticleCompileError("particle GPU emitter binary entry is invalid")
        stages = encoded["stages"]
        if encoded["stable_id"] != source.stable_id or type(stages) is not dict:
            raise GpuParticleCompileError("particle GPU emitter binary identity is invalid")
        if set(stages) != set(source.stages()):
            raise GpuParticleCompileError("particle GPU emitter binary stages are incomplete")
        for stage, descriptor in stages.items():
            _validate_spirv_descriptor(descriptor, stage)
        continuation = encoded["continuation"]
        if source.continuation is None:
            if continuation is not None:
                raise GpuParticleCompileError(
                    "particle GPU emitter unexpectedly contains continuation binaries"
                )
        else:
            if type(continuation) is not dict or set(continuation) != {
                "record_stride",
                "lane_count",
                "join_count",
                "stages",
            }:
                raise GpuParticleCompileError(
                    "particle GPU continuation binary entry is invalid"
                )
            if (
                continuation["record_stride"] != source.continuation.record_stride
                or continuation["lane_count"] != source.continuation.lane_count
                or continuation["join_count"] != source.continuation.join_count
                or type(continuation["stages"]) is not dict
                or set(continuation["stages"])
                != set(source.continuation.stages())
            ):
                raise GpuParticleCompileError(
                    "particle GPU continuation binary metadata is incompatible"
                )
            for stage, descriptor in continuation["stages"].items():
                _validate_spirv_descriptor(descriptor, f"continuation.{stage}")
    for stage, descriptor in billboard.items():
        _validate_spirv_descriptor(descriptor, f"billboard.{stage}")
    for stage, descriptor in mesh.items():
        _validate_spirv_descriptor(descriptor, f"mesh.{stage}")
    return value


def _validate_spirv_descriptor(descriptor: Any, stage: str) -> None:
    if type(descriptor) is not dict or set(descriptor) != {
        "byte_size",
        "sha256",
        "zlib_base64",
    }:
        raise GpuParticleCompileError(
            f"particle GPU binary descriptor {stage!r} is invalid"
        )
    try:
        binary = zlib.decompress(
            base64.b64decode(descriptor["zlib_base64"], validate=True)
        )
    except (TypeError, ValueError, zlib.error) as exc:
        raise GpuParticleCompileError(
            f"particle GPU binary {stage!r} is corrupt"
        ) from exc
    if (
        type(descriptor["byte_size"]) is not int
        or descriptor["byte_size"] != len(binary)
        or type(descriptor["sha256"]) is not str
        or descriptor["sha256"] != hashlib.sha256(binary).hexdigest()
        or len(binary) < 20
        or int.from_bytes(binary[:4], "little") != 0x07230203
    ):
        raise GpuParticleCompileError(
            f"particle GPU binary {stage!r} failed integrity validation"
        )


def decode_gpu_particle_spirv(value: Any, emitter_index: int) -> dict[str, Any]:
    """Decode one validated emitter payload into native-ready SPIR-V bytes."""
    if type(emitter_index) is not int or emitter_index < 0:
        raise GpuParticleCompileError("particle GPU emitter index is invalid")
    emitters = value.get("emitters") if type(value) is dict else None
    parameters = value.get("parameters") if type(value) is dict else None
    parameter_words = value.get("parameter_words") if type(value) is dict else None
    billboard = value.get("billboard") if type(value) is dict else None
    mesh = value.get("mesh") if type(value) is dict else None
    if (
        type(emitters) is not list
        or emitter_index >= len(emitters)
        or type(emitters[emitter_index]) is not dict
        or type(emitters[emitter_index].get("stages")) is not dict
        or type(billboard) is not dict
        or type(mesh) is not dict
        or type(parameters) is not list
        or type(parameter_words) is not list
        or any(type(word) is not int or not 0 <= word <= 0xFFFFFFFF for word in parameter_words)
    ):
        raise GpuParticleCompileError("particle GPU emitter payload is invalid")

    def decode(descriptor: Any, stage: str) -> bytes:
        _validate_spirv_descriptor(descriptor, stage)
        return zlib.decompress(
            base64.b64decode(descriptor["zlib_base64"], validate=True)
        )

    emitter = emitters[emitter_index]
    continuation = emitter.get("continuation")
    if continuation is not None and (
        type(continuation) is not dict
        or set(continuation) != {
            "record_stride",
            "lane_count",
            "join_count",
            "stages",
        }
        or type(continuation["stages"]) is not dict
    ):
        raise GpuParticleCompileError("particle GPU continuation payload is invalid")
    return {
        "stable_id": emitter.get("stable_id", ""),
        "parameters": parameters,
        "parameter_words": parameter_words,
        "continuation": (
            {
                "record_stride": continuation["record_stride"],
                "lane_count": continuation["lane_count"],
                "join_count": continuation["join_count"],
                "stages": {
                    stage: decode(descriptor, f"continuation.{stage}")
                    for stage, descriptor in continuation["stages"].items()
                },
            }
            if continuation is not None
            else None
        ),
        "stages": {
            stage: decode(descriptor, stage)
            for stage, descriptor in emitter["stages"].items()
        },
        "billboard": {
            stage: decode(descriptor, f"billboard.{stage}")
            for stage, descriptor in billboard.items()
        },
        "mesh": {
            stage: decode(descriptor, f"mesh.{stage}")
            for stage, descriptor in mesh.items()
        },
    }


class _StageCompiler:
    def __init__(
        self,
        emitter: ParticleEmitterKernelIR,
        fields: tuple[tuple[str, TypeRef, str], ...],
        data_interface_layout: dict[str, Any],
        events,
        emitter_index: int,
        *,
        event_input: bool = False,
        parameter_slots: Mapping[str, tuple[int, TypeRef]] | None = None,
        continuation_lane_indices: Mapping[str, int] | None = None,
        continuation_join_indices: Mapping[tuple[ParticleStage, str], int]
        | None = None,
        existing_continuation_lane: int | None = None,
    ) -> None:
        self._emitter = emitter
        self._fields = {stable_id: (value_type, field) for stable_id, value_type, field in fields}
        self._values: dict[str, str] = {}
        self._exports: dict[str, str] = {}
        self._lines: list[str] = []
        self._event_lines: list[str] = []
        self._events = events
        self._emitter_index = int(emitter_index)
        self._event_input = bool(event_input)
        self._parameter_slots = dict(parameter_slots or {})
        self._continuation_lane_indices = dict(continuation_lane_indices or {})
        self._continuation_join_indices = dict(continuation_join_indices or {})
        self._existing_continuation_lane = existing_continuation_lane
        self._active_lane_var: str | None = None
        self._suspension_join_contexts: dict[str, tuple[int, int]] = {}
        self._inline_events = False
        self._volume_interfaces = {
            interface["stable_id"]: interface["interface_index"]
            for interface in data_interface_layout.get("volume_interfaces", ())
        }
        self._texture_parameters = {
            parameter["stable_id"]: parameter
            for parameter in data_interface_layout.get("texture2d_parameters", ())
        }

    def compile(
        self,
        function: ParticleKernelFunction,
        flows=(),
    ) -> tuple[str, dict[str, str], str]:
        if self._continuation_lane_indices and any(
            instruction.opcode in {"suspend_frames", "suspend_seconds"}
            for instruction in function.instructions
        ):
            self._compile_flow_aware(function, tuple(flows))
        else:
            for instruction in function.instructions:
                self._compile_instruction(instruction)
        return (
            "\n".join(f"    {line}" for line in self._lines),
            dict(self._exports),
            "\n".join(f"    {line}" for line in self._event_lines),
        )

    def _compile_flow_aware(self, function: ParticleKernelFunction, flows) -> None:
        if not flows:
            raise GpuParticleCompileError(
                "GPU Wait lowering requires lifecycle flow metadata"
            )
        self._inline_events = True
        entries = []
        lane_names: dict[tuple[int, int], str] = {}
        joins = {}
        runtime_joins = {}
        lane_arrivals: dict[tuple[int, int], list[tuple[int, int]]] = {}
        lane_waits: dict[tuple[int, int], set[int]] = {}

        def is_descendant(flow, lane_index: int, ancestor_index: int) -> bool:
            current = lane_index
            while current >= 0:
                if current == ancestor_index:
                    return True
                current = flow.lanes[current].parent_index
            return False

        for flow_index, flow in enumerate(flows):
            prefix = re.sub(r"[^a-zA-Z0-9_]", "_", flow.lifecycle_stage.value)
            for lane in flow.lanes:
                lane_names[(flow_index, lane.index)] = (
                    f"inx_lane_{prefix}_{flow_index}_{lane.index}_active"
                )
            joins.update(
                {
                    (flow_index, join.output_lane_index): join
                    for join in flow.joins
                }
            )
            schedule_order = {
                operation_index: order
                for order, operation_index in enumerate(flow.operation_schedule)
            }
            join_order = {
                join.source_node_uid: schedule_order[
                    next(
                        block.operation_index
                        for block in flow.blocks
                        if block.source_node_uid == join.source_node_uid
                    )
                ]
                for join in flow.joins
            }
            flow_suspensions = tuple(
                item
                for item in self._emitter.suspensions
                if item.lifecycle_stage is flow.lifecycle_stage
            )
            for suspension in flow_suspensions:
                lane_waits.setdefault((flow_index, suspension.lane_index), set()).add(
                    self._continuation_lane_indices[suspension.lane_stable_id]
                )
            for join in flow.joins:
                if len(join.input_lane_indices) > 32:
                    raise GpuParticleCompileError(
                        "GPU Join All supports at most 32 input lanes"
                    )
                crossed = any(
                    any(
                        is_descendant(flow, input_lane, suspension.lane_index)
                        for input_lane in join.input_lane_indices
                    )
                    for suspension in flow_suspensions
                )
                if not crossed:
                    continue
                try:
                    global_join_index = self._continuation_join_indices[
                        (flow.lifecycle_stage, join.source_node_uid)
                    ]
                except KeyError as exc:
                    raise GpuParticleCompileError(
                        "GPU continuation Join All index is missing"
                    ) from exc
                expected_mask = (1 << len(join.input_lane_indices)) - 1
                runtime_joins[(flow_index, join.output_lane_index)] = (
                    join,
                    global_join_index,
                    expected_mask,
                )
                for bit, input_lane in enumerate(join.input_lane_indices):
                    lane_arrivals.setdefault((flow_index, input_lane), []).append(
                        (global_join_index, 1 << bit)
                    )
            for suspension in flow_suspensions:
                candidates = []
                for join in flow.joins:
                    runtime = runtime_joins.get(
                        (flow_index, join.output_lane_index)
                    )
                    if runtime is None or not any(
                        is_descendant(flow, input_lane, suspension.lane_index)
                        for input_lane in join.input_lane_indices
                    ):
                        continue
                    candidates.append((join_order[join.source_node_uid], runtime))
                if candidates:
                    _order, (_join, global_join_index, expected_mask) = min(
                        candidates, key=lambda item: item[0]
                    )
                    self._suspension_join_contexts[suspension.lane_stable_id] = (
                        global_join_index,
                        expected_mask,
                    )
            entries.extend(
                (
                    block.instruction_begin,
                    flow_index,
                    schedule_order[block.operation_index],
                    block,
                )
                for block in flow.blocks
                if block.operation_index >= 0
            )
        entries.sort(key=lambda item: (item[0], item[1], item[2]))
        for flow_index, flow in enumerate(flows):
            for lane in flow.lanes:
                initial = "true" if lane.index == 0 else "false"
                guards = []
                for runtime_lane in sorted(
                    lane_waits.get((flow_index, lane.index), ())
                ):
                    guards.append(
                        f"!inx_continuation_lane_pending(particle_index, {runtime_lane}u)"
                    )
                for join_index, arrival_bit in lane_arrivals.get(
                    (flow_index, lane.index), ()
                ):
                    guards.append(
                        "!inx_continuation_join_has_arrived("
                        f"particle_index, state.spawn_generation, {join_index}u, "
                        f"{arrival_bit}u)"
                    )
                if guards:
                    initial = f"({initial}) && " + " && ".join(guards)
                self._lines.append(
                    f"bool {lane_names[(flow_index, lane.index)]} = {initial};"
                )

        initialized = {(index, 0) for index in range(len(flows))}
        cursor = 0
        for begin, flow_index, _order, block in entries:
            if begin < cursor:
                raise GpuParticleCompileError(
                    "GPU lifecycle instruction ranges overlap"
                )
            for instruction in function.instructions[cursor:begin]:
                self._active_lane_var = None
                self._compile_instruction(instruction)
            lane_key = (flow_index, block.lane_index)
            lane_var = lane_names[lane_key]
            if lane_key not in initialized:
                runtime_join = runtime_joins.get(lane_key)
                join = joins.get(lane_key)
                if runtime_join is not None:
                    join, join_index, expected_mask = runtime_join
                    arrivals = " | ".join(
                        f"({lane_names[(flow_index, value)]} ? {1 << bit}u : 0u)"
                        for bit, value in enumerate(join.input_lane_indices)
                    )
                    condition = (
                        "inx_continuation_join_arrive("
                        f"particle_index, state.spawn_generation, {join_index}u, 0u, "
                        f"{expected_mask}u, ({arrivals}))"
                    )
                elif join is not None:
                    condition = " && ".join(
                        lane_names[(flow_index, value)]
                        for value in join.input_lane_indices
                    )
                else:
                    lane = flows[flow_index].lanes[block.lane_index]
                    condition = lane_names[(flow_index, lane.parent_index)]
                    guards = []
                    for runtime_lane in sorted(lane_waits.get(lane_key, ())):
                        guards.append(
                            f"!inx_continuation_lane_pending(particle_index, {runtime_lane}u)"
                        )
                    for join_index, arrival_bit in lane_arrivals.get(lane_key, ()):
                        guards.append(
                            "!inx_continuation_join_has_arrived("
                            f"particle_index, state.spawn_generation, {join_index}u, "
                            f"{arrival_bit}u)"
                        )
                    if guards:
                        condition = f"({condition}) && " + " && ".join(guards)
                self._lines.append(f"{lane_var} = ({condition});")
                initialized.add(lane_key)
            if block.instruction_begin == block.instruction_end:
                cursor = begin
                continue
            self._lines.append(f"if ({lane_var}) {{")
            self._active_lane_var = lane_var
            for instruction in function.instructions[
                block.instruction_begin : block.instruction_end
            ]:
                self._compile_instruction(instruction)
            self._lines.append("}")
            cursor = block.instruction_end
        for instruction in function.instructions[cursor:]:
            self._active_lane_var = None
            self._compile_instruction(instruction)
        self._active_lane_var = None

    def compile_resume(
        self,
        function: ParticleKernelFunction,
        flow,
        suspension,
    ) -> tuple[str, str]:
        schedule_order = {
            operation_index: order
            for order, operation_index in enumerate(flow.operation_schedule)
        }
        try:
            resume_order = schedule_order[suspension.resume_operation_index]
        except KeyError as exc:
            raise GpuParticleCompileError(
                "GPU continuation resume block is absent from its lifecycle schedule"
            ) from exc

        def is_descendant(lane_index: int, ancestor_index: int) -> bool:
            current = lane_index
            while current >= 0:
                if current == ancestor_index:
                    return True
                current = flow.lanes[current].parent_index
            return False

        selected_lanes = {
            lane.index
            for lane in flow.lanes
            if is_descendant(lane.index, suspension.lane_index)
        }
        reached_joins = []
        changed = True
        while changed:
            changed = False
            for join in flow.joins:
                if join in reached_joins or not any(
                    lane in selected_lanes for lane in join.input_lane_indices
                ):
                    continue
                if len(join.input_lane_indices) > 32:
                    raise GpuParticleCompileError(
                        "GPU Join All supports at most 32 input lanes"
                    )
                reached_joins.append(join)
                before = len(selected_lanes)
                selected_lanes.update(
                    lane.index
                    for lane in flow.lanes
                    if is_descendant(lane.index, join.output_lane_index)
                )
                changed = changed or len(selected_lanes) != before
        selected_blocks = [
            block
            for block in flow.blocks
            if block.operation_index >= 0
            and schedule_order[block.operation_index] >= resume_order
            and block.lane_index in selected_lanes
        ]
        selected_blocks.sort(key=lambda block: schedule_order[block.operation_index])
        if not selected_blocks or selected_blocks[0].operation_index != suspension.resume_operation_index:
            raise GpuParticleCompileError(
                "GPU continuation resume schedule is not reachable from its lane"
            )

        selected_instruction_indices = {
            index
            for block in selected_blocks
            for index in range(block.instruction_begin, block.instruction_end)
        }
        producer_by_value = {
            instruction.result_id: index
            for index, instruction in enumerate(function.instructions)
            if instruction.result_id
        }
        prerequisite_indices: set[int] = set()
        pending_values = [
            operand.value_id
            for index in sorted(selected_instruction_indices)
            for operand in function.instructions[index].operands
            if operand.value_id
        ]
        while pending_values:
            value_id = pending_values.pop()
            producer_index = producer_by_value.get(value_id)
            if producer_index is None:
                raise GpuParticleCompileError(
                    f"GPU continuation references unknown SSA value {value_id!r}"
                )
            if (
                producer_index in selected_instruction_indices
                or producer_index in prerequisite_indices
            ):
                continue
            producer = function.instructions[producer_index]
            if producer.opcode == "event_payload":
                raise GpuParticleCompileError(
                    "GPU Event Payload values cannot remain live across Wait; "
                    "copy the value into a particle attribute first"
                )
            prerequisite_indices.add(producer_index)
            pending_values.extend(
                operand.value_id
                for operand in producer.operands
                if operand.value_id
            )

        # Resume shaders are separate entry points. Rebuild only the pure SSA
        # dependency slice required by post-Wait blocks; stage stores, kills,
        # events and suspension side effects are deliberately not replayed.
        for index in sorted(prerequisite_indices):
            self._active_lane_var = None
            self._compile_instruction(function.instructions[index])

        self._inline_events = True
        lane_names = {
            lane.index: f"inx_resume_lane_{lane.index}_active"
            for lane in flow.lanes
            if lane.index in selected_lanes
        }
        for lane_index, lane_var in lane_names.items():
            initial = "true" if lane_index == suspension.lane_index else "false"
            self._lines.append(f"bool {lane_var} = {initial};")
        initialized = {suspension.lane_index}
        joins_by_output = {join.output_lane_index: join for join in reached_joins}
        join_orders = {
            join.source_node_uid: schedule_order[
                next(
                    block.operation_index
                    for block in flow.blocks
                    if block.source_node_uid == join.source_node_uid
                )
            ]
            for join in reached_joins
        }
        for candidate_suspension in self._emitter.suspensions:
            if candidate_suspension.lifecycle_stage is not flow.lifecycle_stage:
                continue
            candidates = [
                join
                for join in reached_joins
                if any(
                    is_descendant(input_lane, candidate_suspension.lane_index)
                    for input_lane in join.input_lane_indices
                )
            ]
            if not candidates:
                continue
            nearest = min(candidates, key=lambda join: join_orders[join.source_node_uid])
            nearest_index = self._continuation_join_indices[
                (flow.lifecycle_stage, nearest.source_node_uid)
            ]
            self._suspension_join_contexts[candidate_suspension.lane_stable_id] = (
                nearest_index,
                (1 << len(nearest.input_lane_indices)) - 1,
            )
        for block in selected_blocks:
            lane = flow.lanes[block.lane_index]
            lane_var = lane_names[block.lane_index]
            if block.lane_index not in initialized:
                join = joins_by_output.get(block.lane_index)
                if join is not None:
                    join_index = self._continuation_join_indices[
                        (flow.lifecycle_stage, join.source_node_uid)
                    ]
                    expected_mask = (1 << len(join.input_lane_indices)) - 1
                    arrivals = " | ".join(
                        f"({lane_names[value]} ? {1 << bit}u : 0u)"
                        for bit, value in enumerate(join.input_lane_indices)
                        if value in lane_names
                    ) or "0u"
                    token = (
                        "(inx_continuation_record_join_index == "
                        f"{join_index}u ? inx_continuation_record_branch_token : 0u)"
                    )
                    self._lines.append(
                        f"{lane_var} = inx_continuation_join_arrive("
                        f"particle_index, state.spawn_generation, {join_index}u, "
                        f"{token}, {expected_mask}u, ({arrivals}));"
                    )
                elif lane.parent_index not in lane_names:
                    raise GpuParticleCompileError(
                        "GPU continuation descendant lane lost its parent"
                    )
                else:
                    self._lines.append(
                        f"{lane_var} = ({lane_names[lane.parent_index]});"
                    )
                initialized.add(block.lane_index)
            if block.instruction_begin == block.instruction_end:
                continue
            self._lines.append(f"if ({lane_var}) {{")
            self._active_lane_var = lane_var
            for instruction in function.instructions[
                block.instruction_begin : block.instruction_end
            ]:
                self._compile_instruction(instruction)
            self._lines.append("}")
        self._active_lane_var = None
        return (
            "\n".join(f"            {line}" for line in self._lines),
            "\n".join(f"            {line}" for line in self._event_lines),
        )

    def _compile_instruction(self, instruction: KernelInstruction) -> None:
        opcode = instruction.opcode
        immediate = instruction.immediate_dict()
        try:
            operands = [self._values[item.value_id] for item in instruction.operands]
        except KeyError as exc:
            raise GpuParticleCompileError(
                f"GPU instruction {opcode!r} references unavailable SSA value "
                f"{exc.args[0]!r}"
            ) from exc
        result = _value_name(instruction.result_id) if instruction.result_id else ""
        result_type = instruction.result_type
        source = instruction.source
        if source.node_uid or source.operation:
            label = source.node_uid or source.operation
            self._lines.append(f"// {label}")

        expression = ""
        if opcode == "constant":
            expression = _glsl_literal(immediate["value"], result_type)
        elif opcode == "load_attribute":
            value_type, field = self._field(immediate["attribute"])
            expression = f"state.{field}"
            if value_type.value_type is ValueType.BOOL:
                expression = f"({expression} != 0u)"
        elif opcode == "load_uniform":
            if immediate["name"] != "delta_time":
                raise GpuParticleCompileError(
                    f"GPU backend does not implement uniform {immediate['name']!r}"
                )
            expression = "pc.delta_time"
        elif opcode == "load_parameter":
            stable_id = str(immediate["parameter"])
            parameter = self._parameter_slots.get(stable_id)
            if parameter is None or parameter[1] != result_type:
                raise GpuParticleCompileError(
                    f"GPU backend cannot resolve parameter {stable_id!r}"
                )
            if result_type.value_type is ValueType.TEXTURE2D:
                resource = self._texture_parameters.get(stable_id)
                if resource is None:
                    raise GpuParticleCompileError(
                        f"GPU backend cannot resolve Texture2D parameter {stable_id!r}"
                    )
                self._values[instruction.result_id] = (
                    f"inx_parameter_texture_{int(resource['resource_index'])}"
                )
                return
            expression = _parameter_load_glsl(parameter[0], result_type)
        elif opcode == "event_payload":
            channel_index = int(immediate["channel_index"])
            if not 0 <= channel_index < len(self._events.routes):
                raise GpuParticleCompileError(
                    "GPU event payload references an unknown channel"
                )
            route = self._events.routes[channel_index]
            if route.target_emitter_index != self._emitter_index:
                raise GpuParticleCompileError(
                    "GPU event payload does not belong to this emitter"
                )
            default = _glsl_literal(immediate["default"], result_type)
            if self._event_input:
                word_offset = int(immediate["word_offset"])
                word_count = int(immediate["word_count"])
                words = tuple(
                    f"event_record_words[record_base + {4 + word_offset + index}u]"
                    for index in range(word_count)
                )
                decoded = _event_payload_glsl_expression(words, result_type)
                expression = (
                    f"(channel_index == {channel_index}u ? {decoded} : {default})"
                )
            else:
                expression = default
        elif opcode == "numeric_resize":
            expression = _numeric_resize_glsl(
                operands[0], instruction.operands[0].value_type, result_type
            )
        elif opcode in {"compose_vec2", "compose_vec3", "compose_vec4"}:
            expression = f"{_glsl_type(result_type)}({', '.join(operands)})"
        elif opcode == "split_component":
            expression = f"({operands[0]}).{'xyzw'[int(immediate['component'])]}"
        elif opcode == "add":
            expression = f"({operands[0]} + {operands[1]})"
        elif opcode == "subtract":
            expression = f"({operands[0]} - {operands[1]})"
        elif opcode == "multiply":
            expression = f"({operands[0]} * {operands[1]})"
        elif opcode == "divide":
            expression = f"({operands[0]} / {operands[1]})"
        elif opcode == "normalized_age":
            expression = (
                f"clamp({operands[0]} / max({operands[1]}, 0.000001), 0.0, 1.0)"
            )
        elif opcode == "lerp":
            expression = f"mix({operands[0]}, {operands[1]}, {operands[2]})"
        elif opcode == "less_than":
            expression = f"({operands[0]} < {operands[1]})"
        elif opcode == "less_equal":
            expression = f"({operands[0]} <= {operands[1]})"
        elif opcode == "greater_than":
            expression = f"({operands[0]} > {operands[1]})"
        elif opcode == "greater_equal":
            expression = f"({operands[0]} >= {operands[1]})"
        elif opcode == "equal":
            expression = f"({operands[0]} == {operands[1]})"
        elif opcode == "not_equal":
            expression = f"({operands[0]} != {operands[1]})"
        elif opcode == "logical_and":
            expression = f"({operands[0]} && {operands[1]})"
        elif opcode == "logical_or":
            expression = f"({operands[0]} || {operands[1]})"
        elif opcode == "logical_not":
            expression = f"!({operands[0]})"
        elif opcode == "begin_if":
            self._lines.append(f"if ({operands[0]}) {{")
            return
        elif opcode == "end_if":
            self._lines.append("}")
            return
        elif opcode == "normalize":
            expression = f"inx_safe_normalize({operands[0]})"
        elif opcode == "random_f32":
            expression = (
                f"inx_random_range({operands[0]}, {operands[1]}, {operands[2]}, "
                f"{int(immediate['random_slot'])}u, state.{self._field('builtin.id')[1]}, "
                "state.spawn_generation)"
            )
        elif opcode == "sample_curve":
            curve = Curve.from_dict(immediate["curve"])
            sample_time = f"{result}_time"
            self._lines.append(
                f"float {sample_time} = {_glsl_wrapped_curve_time(operands[0], curve)};"
            )
            expression = _glsl_curve_sample(sample_time, curve)
        elif opcode == "sample_gradient":
            gradient = Gradient.from_dict(immediate["gradient"])
            sample_time = f"{result}_time"
            self._lines.append(
                f"float {sample_time} = clamp({operands[0]}, "
                f"{_float_literal(gradient.keys[0].time)}, "
                f"{_float_literal(gradient.keys[-1].time)});"
            )
            expression = _glsl_gradient_sample(sample_time, gradient)
        elif opcode == "sample_texture2d":
            expression = f"texture({operands[0]}, {operands[1]})"
        elif opcode == "value_noise_3d":
            expression = f"inx_value_noise_3d({operands[0]}, {operands[1]}, {operands[2]})"
        elif opcode == "vector_noise_3d":
            expression = f"inx_vector_noise_3d({operands[0]}, {operands[1]}, {operands[2]})"
        elif opcode.startswith("sample_shape_"):
            mode = "position" if opcode.endswith("position") else "direction"
            slots = immediate["random_slots"]
            if immediate["shape"] == "mesh":
                if mode != "position":
                    raise GpuParticleCompileError(
                        "automatic Mesh emitter shape only samples particle position"
                    )
                expression = (
                    "inx_sample_mesh_shape_position(inx_shape_random("
                    f"uvec3({int(slots[0])}u, {int(slots[1])}u, {int(slots[2])}u), "
                    f"state.{self._field('builtin.id')[1]}, state.spawn_generation))"
                )
            else:
                expression = (
                    f"inx_sample_shape_{mode}({_shape_kind(immediate['shape'])}u, "
                    f"{_float_literal(immediate['radius'])}, "
                    f"{_float_literal(immediate['angle_degrees'])}, "
                    f"{_vector_literal(immediate['dimensions'], 3)}, "
                    f"uvec3({int(slots[0])}u, {int(slots[1])}u, {int(slots[2])}u), "
                    f"state.{self._field('builtin.id')[1]}, state.spawn_generation)"
                )
        elif opcode == "sample_vector_field":
            stable_id = immediate["interface"]
            try:
                sample_index = self._volume_interfaces[stable_id]
            except KeyError as exc:
                raise GpuParticleCompileError(
                    f"GPU vector field sample layout is missing {stable_id!r}"
                ) from exc
            expression = f"inx_sample_vector_field_{sample_index}({operands[0]})"
        elif opcode == "convert_space":
            expression = _space_conversion(operands[0], result_type, immediate)
        elif opcode == "store_attribute":
            value_type, field = self._field(immediate["attribute"])
            value = operands[0]
            if value_type.value_type is ValueType.BOOL:
                value = f"({value} ? 1u : 0u)"
            self._lines.append(f"state.{field} = {value};")
            return
        elif opcode == "kill_if":
            self._lines.append(f"particle_alive = particle_alive && !({operands[0]});")
            return
        elif opcode in {"suspend_frames", "suspend_seconds"}:
            if self._active_lane_var is None:
                raise GpuParticleCompileError(
                    "GPU Wait instruction is not owned by an execution lane"
                )
            lane_stable_id = str(immediate["lane_stable_id"])
            try:
                lane_index = self._continuation_lane_indices[lane_stable_id]
            except KeyError as exc:
                raise GpuParticleCompileError(
                    f"GPU continuation lane {lane_stable_id!r} is not registered"
                ) from exc
            existing_record = (
                "inx_continuation_record_index"
                if self._existing_continuation_lane == lane_index
                else "INX_INVALID_INDEX"
            )
            join_index, expected_mask = self._suspension_join_contexts.get(
                lane_stable_id,
                (0xFFFFFFFF, 0),
            )
            helper = (
                "inx_suspend_frames"
                if opcode == "suspend_frames"
                else "inx_suspend_seconds"
            )
            self._lines.append(
                f"if ({helper}(particle_index, state.spawn_generation, {lane_index}u, "
                f"{int(immediate['resume_program_counter'])}u, {operands[0]}, "
                f"{join_index}u, {expected_mask}u, {existing_record})) {{"
            )
            self._lines.append(f"    {self._active_lane_var} = false;")
            if self._existing_continuation_lane == lane_index:
                self._lines.append("    inx_continuation_resuspended = true;")
            self._lines.append("}")
            return
        elif opcode == "collide_scene":
            position_type, position_field = self._field(immediate["position_attribute"])
            velocity_type, velocity_field = self._field(immediate["velocity_attribute"])
            if (
                position_type.value_type is not ValueType.VEC3
                or velocity_type.value_type is not ValueType.VEC3
            ):
                raise GpuParticleCompileError(
                    "Scene Collision requires vec3 position and velocity attributes"
                )
            suffix = len(self._lines)
            position_name = f"inx_scene_position_{suffix}"
            velocity_name = f"inx_scene_velocity_{suffix}"
            normal_name = f"inx_scene_normal_{suffix}"
            hit_name = f"inx_scene_hit_{suffix}"
            self._lines.extend(
                (
                    f"vec3 {position_name} = {operands[0]};",
                    f"vec3 {velocity_name} = {operands[1]};",
                    f"vec3 {normal_name} = vec3(0.0);",
                    f"bool {hit_name} = inx_collide_scene("
                    f"{position_name}, {velocity_name}, {', '.join(operands[2:])}, "
                    f"{normal_name});",
                    f"state.{position_field} = {position_name};",
                    f"state.{velocity_field} = {velocity_name};",
                )
            )
            hit_attribute = immediate["hit_attribute"]
            if hit_attribute:
                hit_type, hit_field = self._field(hit_attribute)
                if hit_type.value_type is not ValueType.BOOL:
                    raise GpuParticleCompileError(
                        "Scene Collision hit output requires a bool attribute"
                    )
                self._lines.append(
                    f"state.{hit_field} = (state.{hit_field} != 0u || {hit_name}) ? 1u : 0u;"
                )
            normal_attribute = immediate["normal_attribute"]
            if normal_attribute:
                normal_type, normal_field = self._field(normal_attribute)
                if normal_type != TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION):
                    raise GpuParticleCompileError(
                        "Scene Collision normal output requires a simulation-space vec3 attribute"
                    )
                self._lines.append(
                    f"if ({hit_name}) state.{normal_field} = {normal_name};"
                )
            return
        elif opcode == "event_append":
            channel_index = int(immediate["channel_index"])
            payload_layout = immediate["payload_layout"]
            if channel_index < 0 or type(payload_layout) is not list or len(
                payload_layout
            ) != len(operands) - 1:
                raise GpuParticleCompileError("GPU event append metadata is invalid")
            event_lines = self._lines if self._inline_events else self._event_lines
            suffix = len(event_lines)
            id_field = self._field("builtin.id")[1]
            event_lines.extend(
                (
                    f"if (pc.reserved != 0u && particle_alive && ({operands[0]})) {{",
                    f"    const uint inx_event_channel_index_{suffix} = {channel_index}u;",
                    f"    ParticleEventOutputChannel inx_event_channel_{suffix} = "
                    f"event_output_channels[inx_event_channel_index_{suffix}];",
                    f"    uint inx_event_slot_{suffix} = atomicAdd("
                    f"event_output_counters[inx_event_channel_index_{suffix}].x, 1u);",
                    f"    if (inx_event_slot_{suffix} < inx_event_channel_{suffix}.capacity) {{",
                    f"        uint inx_event_base_{suffix} = inx_event_channel_{suffix}.record_base_words + "
                    f"inx_event_slot_{suffix} * inx_event_channel_{suffix}.record_stride_words;",
                    f"        event_output_record_words[inx_event_base_{suffix} + 0u] = "
                    f"inx_event_channel_{suffix}.event_type_index;",
                    f"        event_output_record_words[inx_event_base_{suffix} + 1u] = "
                    f"inx_event_channel_{suffix}.source_emitter_index;",
                    f"        event_output_record_words[inx_event_base_{suffix} + 2u] = state.{id_field};",
                    f"        event_output_record_words[inx_event_base_{suffix} + 3u] = state.spawn_generation;",
                )
            )
            for field, operand, kernel_operand in zip(
                payload_layout, operands[1:], instruction.operands[1:]
            ):
                field_type = TypeRef.from_dict(field["type"])
                if field_type != kernel_operand.value_type:
                    raise GpuParticleCompileError(
                        "GPU event append payload type does not match its operand"
                    )
                words = _event_payload_word_expressions(operand, field_type)
                if len(words) != int(field["word_count"]):
                    raise GpuParticleCompileError(
                        "GPU event append payload word layout is invalid"
                    )
                word_offset = int(field["word_offset"])
                event_lines.extend(
                    f"        event_output_record_words[inx_event_base_{suffix} + "
                    f"{4 + word_offset + index}u] = {word};"
                    for index, word in enumerate(words)
                )
            event_lines.extend(
                (
                    "    } else {",
                    f"        atomicAdd(event_output_counters[inx_event_channel_index_{suffix}].y, 1u);",
                    "    }",
                    "}",
                )
            )
            return
        elif opcode == "collide_plane_position":
            expression = f"inx_collide_plane_position({', '.join(operands)})"
        elif opcode == "collide_plane_velocity":
            expression = f"inx_collide_plane_velocity({', '.join(operands)})"
        elif opcode == "collide_sphere_position":
            expression = f"inx_collide_sphere_position({', '.join(operands)})"
        elif opcode == "collide_sphere_velocity":
            expression = f"inx_collide_sphere_velocity({', '.join(operands)})"
        elif opcode in {"collide_sdf_position", "collide_sdf_velocity"}:
            try:
                interface_index = self._volume_interfaces[immediate["interface"]]
            except KeyError as exc:
                raise GpuParticleCompileError(
                    f"GPU kernel references unknown SDF interface {immediate['interface']!r}"
                ) from exc
            inverted = "true" if immediate["inverted"] else "false"
            expression = (
                f"inx_{opcode}_{interface_index}({', '.join(operands)}, {inverted})"
            )
        elif opcode == "export_attribute":
            self._exports[immediate["attribute"]] = operands[0]
            return
        else:
            raise GpuParticleCompileError(
                f"GPU backend does not implement kernel opcode {opcode!r}"
            )

        if not instruction.result_id or result_type is None:
            raise KernelCompileError(f"kernel opcode {opcode!r} did not produce a value")
        self._lines.append(f"{_glsl_type(result_type)} {result} = {expression};")
        self._values[instruction.result_id] = result

    def _field(self, stable_id: str) -> tuple[TypeRef, str]:
        try:
            return self._fields[stable_id]
        except KeyError as exc:
            raise GpuParticleCompileError(
                f"GPU kernel references unknown attribute {stable_id!r}"
            ) from exc


def _glsl_curve_wrap(value: str, first: float, last: float, mode: str) -> str:
    span = last - first
    offset = f"({value} - {_float_literal(first)})"
    if mode == "repeat":
        return f"({_float_literal(first)} + mod({offset}, {_float_literal(span)}))"
    return (
        f"({_float_literal(first)} + {_float_literal(span)} - "
        f"abs(mod({offset}, {_float_literal(span * 2.0)}) - {_float_literal(span)}))"
    )


def _glsl_wrapped_curve_time(source: str, curve: Curve) -> str:
    first = curve.keys[0].time
    last = curve.keys[-1].time
    if first == last:
        return _float_literal(first)
    before_source = f"({source} < {_float_literal(first)})"
    after_source = f"({source} > {_float_literal(last)})"
    before = (
        _float_literal(first)
        if curve.pre_wrap == "clamp"
        else _glsl_curve_wrap(source, first, last, curve.pre_wrap)
    )
    after = (
        _float_literal(last)
        if curve.post_wrap == "clamp"
        else _glsl_curve_wrap(source, first, last, curve.post_wrap)
    )
    return f"({before_source} ? {before} : ({after_source} ? {after} : {source}))"


def _glsl_curve_segment(sample_time: str, left, right) -> str:
    duration = right.time - left.time
    u = f"(({sample_time} - {_float_literal(left.time)}) / {_float_literal(duration)})"
    u2 = f"({u} * {u})"
    u3 = f"({u2} * {u})"
    return (
        f"((2.0 * {u3} - 3.0 * {u2} + 1.0) * {_float_literal(left.value)} + "
        f"({u3} - 2.0 * {u2} + {u}) * {_float_literal(left.out_tangent * duration)} + "
        f"(-2.0 * {u3} + 3.0 * {u2}) * {_float_literal(right.value)} + "
        f"({u3} - {u2}) * {_float_literal(right.in_tangent * duration)})"
    )


def _glsl_curve_sample(sample_time: str, curve: Curve) -> str:
    if len(curve.keys) == 1:
        return _float_literal(curve.keys[0].value)
    expression = _float_literal(curve.keys[-1].value)
    for left, right in reversed(tuple(zip(curve.keys, curve.keys[1:]))):
        segment = _glsl_curve_segment(sample_time, left, right)
        expression = f"({sample_time} < {_float_literal(right.time)} ? {segment} : {expression})"
    return expression


def _glsl_gradient_sample(sample_time: str, gradient: Gradient) -> str:
    if len(gradient.keys) == 1:
        return _vector_literal(gradient.keys[0].color, 4)
    expression = _vector_literal(gradient.keys[-1].color, 4)
    for left, right in reversed(tuple(zip(gradient.keys, gradient.keys[1:]))):
        if gradient.mode == "fixed":
            segment = _vector_literal(left.color, 4)
        else:
            factor = (
                f"(({sample_time} - {_float_literal(left.time)}) / "
                f"{_float_literal(right.time - left.time)})"
            )
            segment = (
                f"mix({_vector_literal(left.color, 4)}, "
                f"{_vector_literal(right.color, 4)}, {factor})"
            )
        expression = f"({sample_time} < {_float_literal(right.time)} ? {segment} : {expression})"
    return expression


def _data_interface_layout(
    emitter: ParticleEmitterKernelIR,
    parameters: tuple[KernelParameter, ...],
) -> dict[str, Any]:
    layout = {
        "version": 1,
        "metadata_binding": 0,
    }
    layout.update(_volume_interface_layout(emitter))
    layout.update(_texture_parameter_layout(emitter, parameters, layout))
    layout["mesh_shape"] = _mesh_shape_layout(emitter, layout)
    return layout


def _texture_parameter_layout(
    emitter: ParticleEmitterKernelIR,
    parameters: tuple[KernelParameter, ...],
    layout: Mapping[str, Any],
) -> dict[str, Any]:
    by_id = {
        parameter.stable_id: parameter
        for parameter in parameters
        if parameter.value_type.value_type is ValueType.TEXTURE2D
    }
    sampled = set()
    for function in (emitter.init, emitter.update, emitter.rendering):
        for instruction in function.instructions:
            if (
                instruction.opcode == "load_parameter"
                and instruction.result_type is not None
                and instruction.result_type.value_type is ValueType.TEXTURE2D
            ):
                stable_id = str(instruction.immediate_dict()["parameter"])
                if stable_id not in by_id:
                    raise GpuParticleCompileError(
                        f"GPU kernel references unknown Texture2D parameter {stable_id!r}"
                    )
                sampled.add(stable_id)
    volume_count = len(layout.get("volume_interfaces", ()))
    if volume_count + len(sampled) > 15:
        raise GpuParticleCompileError(
            "GPU particle emitters support at most fifteen sampled Texture2D and volume resources"
        )
    return {
        "texture2d_parameters": [
            {
                "stable_id": stable_id,
                "name": by_id[stable_id].name,
                "parameter_slot": by_id[stable_id].slot,
                "resource_index": index,
                "texture_binding": volume_count + index + 1,
                "default": dict(by_id[stable_id].default),
            }
            for index, stable_id in enumerate(sorted(sampled))
        ]
    }


def _mesh_shape_instruction(emitter: ParticleEmitterKernelIR):
    return next(
        (
            instruction
            for instruction in emitter.init.instructions
            if instruction.opcode == "sample_shape_position"
            and instruction.immediate_dict()["shape"] == "mesh"
        ),
        None,
    )


def _mesh_shape_layout(
    emitter: ParticleEmitterKernelIR, layout: Mapping[str, Any]
) -> dict[str, Any] | None:
    instruction = _mesh_shape_instruction(emitter)
    if instruction is None:
        return None
    immediate = instruction.immediate_dict()
    return {
        "mesh": dict(immediate["mesh"]),
        "mode": immediate["mesh_mode"],
        "metadata_offset": 0,
        "vertex_binding": 14,
        "triangle_binding": 15,
    }


def _volume_interface_layout(emitter: ParticleEmitterKernelIR) -> dict[str, Any]:
    interfaces = {
        interface.stable_id: interface
        for interface in emitter.data_interfaces
        if isinstance(interface, VectorField)
    }
    sdf_interfaces = {
        interface.stable_id: interface
        for interface in emitter.data_interfaces
        if isinstance(interface, SdfVolume)
    }
    sampled: dict[str, str] = {}
    for function in (emitter.init, emitter.update, emitter.rendering):
        for instruction in function.instructions:
            if instruction.opcode != "sample_vector_field":
                if instruction.opcode not in {
                    "collide_sdf_position",
                    "collide_sdf_velocity",
                }:
                    continue
                stable_id = instruction.immediate_dict()["interface"]
                if stable_id not in sdf_interfaces:
                    raise GpuParticleCompileError(
                        f"GPU kernel references unknown SDF interface {stable_id!r}"
                    )
                sampled[stable_id] = "sdf"
                continue
            stable_id = instruction.immediate_dict()["interface"]
            if stable_id not in interfaces:
                raise GpuParticleCompileError(
                    f"GPU kernel references unknown vector field interface {stable_id!r}"
                )
            sampled[stable_id] = "vector_field"
    if len(sampled) > 15:
        raise GpuParticleCompileError(
            "GPU particle emitters currently support at most fifteen sampled volume interfaces"
        )
    return {
        "volume_metadata_binding": 0,
        "volume_stride_words": 32,
        "volume_interfaces": [
            ({
                "kind": sampled[stable_id],
                "stable_id": stable_id,
                "interface_index": index,
                "texture_binding": index + 1,
                "boundary": interfaces[stable_id].boundary.value,
                "filtering": interfaces[stable_id].filtering.value,
            } if sampled[stable_id] == "vector_field" else {
                "kind": "sdf",
                "stable_id": stable_id,
                "interface_index": index,
                "texture_binding": index + 1,
                "filtering": sdf_interfaces[stable_id].filtering.value,
            })
            for index, stable_id in enumerate(sorted(sampled))
        ],
    }


def _attribute_fields(
    emitter: ParticleEmitterKernelIR,
) -> tuple[tuple[str, TypeRef, str], ...]:
    used: set[str] = set()
    result = []
    for stable_id, value_type, _default in emitter.attributes:
        if value_type.value_type in {
            ValueType.STRING,
            ValueType.ASSET_REF,
            ValueType.TEXTURE2D,
        }:
            raise GpuParticleCompileError(
                f"attribute {stable_id!r} cannot be stored in a GPU particle buffer"
            )
        base = "a_" + re.sub(r"[^a-zA-Z0-9_]", "_", stable_id)
        field = base
        suffix = 2
        while field in used:
            field = f"{base}_{suffix}"
            suffix += 1
        used.add(field)
        result.append((stable_id, value_type, field))
    required = {"builtin.id", "builtin.position", "builtin.size", "builtin.color"}
    if not required.issubset(stable_id for stable_id, _type, _field in result):
        raise GpuParticleCompileError("GPU particles require the standard builtin attributes")
    return tuple(result)


_STD430_STORAGE_LAYOUT = {
    ValueType.BOOL: (4, 4),
    ValueType.I32: (4, 4),
    ValueType.U32: (4, 4),
    ValueType.F32: (4, 4),
    ValueType.VEC2: (8, 8),
    ValueType.VEC3: (16, 12),
    ValueType.VEC4: (16, 16),
    ValueType.COLOR: (16, 16),
    ValueType.MAT3: (16, 48),
    ValueType.MAT4: (16, 64),
}


def _std430_attribute_layout(
    fields: tuple[tuple[str, TypeRef, str], ...],
) -> tuple[tuple[tuple[str, TypeRef, str, int, int], ...], int]:
    offset = 8  # alive + spawn_generation
    struct_alignment = 4
    result = []
    for stable_id, value_type, field in fields:
        try:
            alignment, byte_size = _STD430_STORAGE_LAYOUT[value_type.value_type]
        except KeyError as exc:
            raise GpuParticleCompileError(
                f"attribute {stable_id!r} has no std430 storage layout"
            ) from exc
        offset = _align_up(offset, alignment)
        result.append((stable_id, value_type, field, offset, byte_size))
        offset += byte_size
        struct_alignment = max(struct_alignment, alignment)
    return tuple(result), _align_up(offset, struct_alignment)


def _std430_state_stride(fields: tuple[tuple[str, TypeRef, str], ...]) -> int:
    return _std430_attribute_layout(fields)[1]


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def build_gpu_particle_migration(
    previous_layout: dict[str, Any],
    next_layout: dict[str, Any],
    next_kernel: ParticleEmitterKernelIR,
) -> dict[str, Any]:
    """Build raw-word copy ranges and defaults for one layout migration."""
    if not isinstance(previous_layout, dict) or not isinstance(next_layout, dict):
        raise GpuParticleCompileError("GPU particle migration requires two layout documents")
    if previous_layout.get("stable_id") != next_layout.get("stable_id"):
        raise GpuParticleCompileError("GPU particle migration emitter identity changed")
    old_stride = previous_layout.get("state_stride")
    new_stride = next_layout.get("state_stride")
    if (
        type(old_stride) is not int
        or old_stride <= 0
        or old_stride % 4
        or type(new_stride) is not int
        or new_stride <= 0
        or new_stride % 4
    ):
        raise GpuParticleCompileError("GPU particle migration state strides are invalid")

    previous_fields = _decode_attribute_layout(previous_layout)
    next_fields = _decode_attribute_layout(next_layout)
    kernel_schema = {
        stable_id: (value_type, default)
        for stable_id, value_type, default in next_kernel.attributes
    }
    if set(next_fields) != set(kernel_schema):
        raise GpuParticleCompileError("GPU particle migration layout does not match Kernel IR")

    defaults = bytearray(new_stride)
    for stable_id, (value_type, default) in kernel_schema.items():
        _glsl_type_name, offset, _byte_size = next_fields[stable_id]
        _pack_std430_default(defaults, offset, value_type, default)

    copy_ranges = []
    for stable_id, (new_type, new_offset, new_size) in next_fields.items():
        previous = previous_fields.get(stable_id)
        if previous is None:
            continue
        old_type, old_offset, old_size = previous
        if old_type != new_type or old_size != new_size:
            raise GpuParticleCompileError(
                f"GPU particle attribute {stable_id!r} changed storage type"
            )
        copy_ranges.append(
            {
                "source_offset": old_offset,
                "destination_offset": new_offset,
                "byte_size": new_size,
            }
        )
    copy_ranges.sort(key=lambda item: item["destination_offset"])
    return {
        "source_stride": old_stride,
        "destination_stride": new_stride,
        "copy_ranges": copy_ranges,
        "default_state_words": list(struct.unpack(f"<{new_stride // 4}I", defaults)),
    }


def _decode_attribute_layout(
    layout: dict[str, Any],
) -> dict[str, tuple[str, int, int]]:
    fields = layout.get("attribute_fields")
    stride = layout.get("state_stride")
    if type(fields) is not list or type(stride) is not int:
        raise GpuParticleCompileError("GPU particle attribute layout is incomplete")
    result = {}
    expected_keys = {
        "stable_id",
        "field",
        "glsl_type",
        "offset",
        "byte_size",
    }
    for field in fields:
        if type(field) is not dict or set(field) != expected_keys:
            raise GpuParticleCompileError("GPU particle attribute layout entry is invalid")
        stable_id = field["stable_id"]
        glsl_type = field["glsl_type"]
        offset = field["offset"]
        byte_size = field["byte_size"]
        if (
            type(stable_id) is not str
            or not stable_id
            or stable_id in result
            or type(glsl_type) is not str
            or type(offset) is not int
            or offset < 8
            or offset % 4
            or type(byte_size) is not int
            or byte_size <= 0
            or byte_size % 4
            or offset + byte_size > stride
        ):
            raise GpuParticleCompileError("GPU particle attribute layout entry is invalid")
        result[stable_id] = (glsl_type, offset, byte_size)
    return result


def _pack_std430_default(
    destination: bytearray,
    offset: int,
    value_type: TypeRef,
    value: Any,
) -> None:
    kind = value_type.value_type
    if kind is ValueType.BOOL:
        struct.pack_into("<I", destination, offset, 1 if value else 0)
    elif kind is ValueType.I32:
        struct.pack_into("<i", destination, offset, int(value))
    elif kind is ValueType.U32:
        struct.pack_into("<I", destination, offset, int(value))
    elif kind is ValueType.F32:
        struct.pack_into("<f", destination, offset, float(value))
    elif kind in {ValueType.VEC2, ValueType.VEC3, ValueType.VEC4, ValueType.COLOR}:
        count = {
            ValueType.VEC2: 2,
            ValueType.VEC3: 3,
            ValueType.VEC4: 4,
            ValueType.COLOR: 4,
        }[kind]
        struct.pack_into(f"<{count}f", destination, offset, *(float(item) for item in value))
    elif kind is ValueType.MAT3:
        values = [float(item) for item in value]
        for column in range(3):
            struct.pack_into(
                "<3f",
                destination,
                offset + column * 16,
                *values[column * 3 : column * 3 + 3],
            )
    elif kind is ValueType.MAT4:
        struct.pack_into("<16f", destination, offset, *(float(item) for item in value))
    else:
        raise GpuParticleCompileError(
            f"GPU particle default for {kind.value!r} has no std430 encoding"
        )


def _mesh_shape_glsl(layout: dict[str, Any]) -> str:
    mesh_shape = layout.get("mesh_shape")
    if mesh_shape is None:
        return ""
    lines = [
        "layout(std430, set = 1, binding = 0) readonly buffer "
        "InxParticleDataMetadata { uint inx_particle_data_meta[]; };"
    ]
    if mesh_shape is not None:
        metadata = int(mesh_shape["metadata_offset"])
        vertex_binding = int(mesh_shape["vertex_binding"])
        triangle_binding = int(mesh_shape["triangle_binding"])
        lines.extend(
            (
                "struct InxMeshShapeVertex {",
                "    vec4 position; vec4 normal; vec4 tangent; vec4 color; vec4 uv;",
                "};",
                f"layout(std430, set = 1, binding = {vertex_binding}) readonly buffer InxMeshShapeVertices {{ InxMeshShapeVertex inx_mesh_shape_vertices[]; }};",
                f"layout(std430, set = 1, binding = {triangle_binding}) readonly buffer InxMeshShapeTriangles {{ uvec4 inx_mesh_shape_triangles[]; }};",
                "vec3 inx_sample_mesh_shape_position(vec3 random_value) {",
                f"    uint vertex_count = inx_particle_data_meta[{metadata}u];",
                f"    uint triangle_count = inx_particle_data_meta[{metadata + 1}u];",
            )
        )
        if mesh_shape["mode"] == "vertex":
            lines.extend(
                (
                    "    uint vertex_index = min(uint(random_value.x * float(vertex_count)), vertex_count - 1u);",
                    "    return inx_mesh_shape_vertices[vertex_index].position.xyz;",
                )
            )
        else:
            if mesh_shape["mode"] == "surface":
                lines.extend(
                    (
                        "    uint low = 0u;",
                        "    uint high = triangle_count;",
                        "    while (low < high) {",
                        "        uint middle = low + (high - low) / 2u;",
                        "        float cdf = uintBitsToFloat(inx_mesh_shape_triangles[middle].w);",
                        "        if (cdf < random_value.x) low = middle + 1u; else high = middle;",
                        "    }",
                        "    uint triangle_index = min(low, triangle_count - 1u);",
                    )
                )
            else:
                lines.append(
                    "    uint triangle_index = min(uint(random_value.x * float(triangle_count)), triangle_count - 1u);"
                )
            lines.extend(
                (
                    "    uvec3 triangle = inx_mesh_shape_triangles[triangle_index].xyz;",
                    "    float root = sqrt(random_value.y);",
                    "    vec3 barycentric = vec3(1.0 - root, root * (1.0 - random_value.z), root * random_value.z);",
                    "    return inx_mesh_shape_vertices[triangle.x].position.xyz * barycentric.x",
                    "         + inx_mesh_shape_vertices[triangle.y].position.xyz * barycentric.y",
                    "         + inx_mesh_shape_vertices[triangle.z].position.xyz * barycentric.z;",
                )
            )
        lines.append("}")
    return "\n".join(lines)


def _volume_interface_glsl(layout: dict[str, Any]) -> str:
    interfaces = layout.get("volume_interfaces", ())
    if not interfaces:
        return ""
    stride = int(layout["volume_stride_words"])
    lines = [
        "layout(std430, set = 2, binding = 0) readonly buffer InxVolumeMetadata { uint inx_volume_meta[]; };"
    ]
    for interface in interfaces:
        index = int(interface["interface_index"])
        base = index * stride
        lines.extend(
            (
                f"layout(set = 2, binding = {int(interface['texture_binding'])}) uniform sampler3D inx_volume_texture_{index};",
                f"mat4 inx_volume_simulation_to_field_{index}() {{",
                "    return mat4(",
                *(
                    "        vec4("
                    + ", ".join(
                        f"uintBitsToFloat(inx_volume_meta[{base + column * 4 + row}u])"
                        for row in range(4)
                    )
                    + (")," if column < 3 else ")")
                    for column in range(4)
                ),
                "    );",
                "}",
                f"mat3 inx_volume_direction_to_simulation_{index}() {{",
                "    return mat3(",
                *(
                    "        vec3("
                    + ", ".join(
                        f"uintBitsToFloat(inx_volume_meta[{base + 16 + column * 4 + row}u])"
                        for row in range(3)
                    )
                    + (")," if column < 2 else ")")
                    for column in range(3)
                ),
                "    );",
                "}",
            )
        )
        if interface["kind"] == "vector_field":
            lines.extend(
                (
                    f"vec3 inx_sample_vector_field_{index}(vec3 simulation_position) {{",
                    f"    vec3 uvw = (inx_volume_simulation_to_field_{index}() * vec4(simulation_position, 1.0)).xyz;",
                )
            )
            if interface["boundary"] == "zero":
                lines.append(
                    "    if (any(lessThan(uvw, vec3(0.0))) || any(greaterThan(uvw, vec3(1.0)))) return vec3(0.0);"
                )
            lines.extend(
                (
                    f"    vec3 value = texture(inx_volume_texture_{index}, uvw).xyz;",
                    f"    float scale = uintBitsToFloat(inx_volume_meta[{base + 28}u]);",
                    f"    return inx_volume_direction_to_simulation_{index}() * value * scale;",
                    "}",
                )
            )
            continue
        lines.extend(
            (
                f"bool inx_sample_sdf_{index}(vec3 simulation_position, bool inverted, out float distance_value, out vec3 normal) {{",
                f"    vec3 field_position = (inx_volume_simulation_to_field_{index}() * vec4(simulation_position, 1.0)).xyz;",
                "    vec3 uvw = field_position + vec3(0.5);",
                "    if (any(lessThan(uvw, vec3(0.0))) || any(greaterThan(uvw, vec3(1.0)))) return false;",
                f"    ivec3 dimensions = textureSize(inx_volume_texture_{index}, 0);",
                "    vec3 texel = vec3(1.0) / vec3(dimensions);",
                f"    float dx = texture(inx_volume_texture_{index}, clamp(uvw + vec3(texel.x, 0.0, 0.0), vec3(0.0), vec3(1.0))).r",
                f"             - texture(inx_volume_texture_{index}, clamp(uvw - vec3(texel.x, 0.0, 0.0), vec3(0.0), vec3(1.0))).r;",
                f"    float dy = texture(inx_volume_texture_{index}, clamp(uvw + vec3(0.0, texel.y, 0.0), vec3(0.0), vec3(1.0))).r",
                f"             - texture(inx_volume_texture_{index}, clamp(uvw - vec3(0.0, texel.y, 0.0), vec3(0.0), vec3(1.0))).r;",
                f"    float dz = texture(inx_volume_texture_{index}, clamp(uvw + vec3(0.0, 0.0, texel.z), vec3(0.0), vec3(1.0))).r",
                f"             - texture(inx_volume_texture_{index}, clamp(uvw - vec3(0.0, 0.0, texel.z), vec3(0.0), vec3(1.0))).r;",
                "    vec3 field_gradient = vec3(dx / (2.0 * texel.x), dy / (2.0 * texel.y), dz / (2.0 * texel.z));",
                f"    vec3 transformed = inx_volume_direction_to_simulation_{index}() * field_gradient;",
                "    float normal_length = length(transformed);",
                "    normal = normal_length > 1.0e-6 ? transformed / normal_length : vec3(0.0, 1.0, 0.0);",
                f"    distance_value = texture(inx_volume_texture_{index}, uvw).r * uintBitsToFloat(inx_volume_meta[{base + 28}u]);",
                "    if (inverted) { distance_value = -distance_value; normal = -normal; }",
                "    return true;",
                "}",
                f"vec3 inx_collide_sdf_position_{index}(vec3 position, vec3 velocity, float radius, float restitution, float friction, bool inverted) {{",
                "    float distance_value; vec3 normal;",
                f"    if (!inx_sample_sdf_{index}(position, inverted, distance_value, normal)) return position;",
                "    float penetration = max(radius, 0.0) - distance_value;",
                "    return penetration > 0.0 ? position + normal * penetration : position;",
                "}",
                f"vec3 inx_collide_sdf_velocity_{index}(vec3 position, vec3 velocity, float radius, float restitution, float friction, bool inverted) {{",
                "    float distance_value; vec3 normal;",
                f"    if (!inx_sample_sdf_{index}(position, inverted, distance_value, normal)) return velocity;",
                "    float normal_speed = dot(velocity, normal);",
                "    if (distance_value >= max(radius, 0.0) || normal_speed >= 0.0) return velocity;",
                "    vec3 tangent = velocity - normal * normal_speed;",
                "    return tangent * (1.0 - clamp(friction, 0.0, 1.0))",
                "         - normal * normal_speed * clamp(restitution, 0.0, 1.0);",
                "}",
            )
        )
    return "\n".join(lines)


def _texture_parameter_glsl(layout: dict[str, Any]) -> str:
    return "\n".join(
        f"layout(set = 2, binding = {int(parameter['texture_binding'])}) "
        f"uniform sampler2D inx_parameter_texture_{int(parameter['resource_index'])};"
        for parameter in layout.get("texture2d_parameters", ())
    )


def _continuation_bindings_glsl(set_index: int) -> str:
    set_index = int(set_index)
    return f"""
layout(std430, set = {set_index}, binding = 0) buffer ParticleContinuationRecords {{
    uint continuation_record_words[];
}};
layout(std430, set = {set_index}, binding = 1) buffer ParticleContinuationFreeList {{
    uint continuation_free_records[];
}};
layout(std430, set = {set_index}, binding = 2) buffer ParticleContinuationReadyQueue {{
    uint continuation_ready_records[];
}};
layout(std430, set = {set_index}, binding = 3) buffer ParticleContinuationActiveQueueA {{
    uint continuation_active_records_a[];
}};
layout(std430, set = {set_index}, binding = 4) buffer ParticleContinuationActiveQueueB {{
    uint continuation_active_records_b[];
}};
layout(std430, set = {set_index}, binding = 5) buffer ParticleContinuationCounters {{
    uint free_count;
    uint active_count_a;
    uint active_count_b;
    uint ready_count;
    uint dropped_capacity;
    uint stale_generation;
    uint resumed_count;
    uint completed_count;
    uint program_generation;
    uint reset_serial;
    uint current_simulation_step;
    uint elapsed_time_low;
    uint elapsed_time_high;
    uint record_stride_words;
    uint lane_count;
    uint join_count;
    uint continuation_capacity;
    uint particle_capacity;
    uint branch_token_counter;
    uint reserved;
}} continuation_counters;
layout(std430, set = {set_index}, binding = 6) buffer ParticleContinuationClassifyIndirect {{
    uint continuation_classify_x;
    uint continuation_classify_y;
    uint continuation_classify_z;
    uint continuation_classify_reserved;
}};
layout(std430, set = {set_index}, binding = 7) buffer ParticleContinuationDispatchIndirect {{
    uint continuation_dispatch_x;
    uint continuation_dispatch_y;
    uint continuation_dispatch_z;
    uint continuation_dispatch_reserved;
}};
layout(std430, set = {set_index}, binding = 8) buffer ParticleContinuationLaneSlots {{
    uint continuation_lane_slots[];
}};
layout(std430, set = {set_index}, binding = 9) buffer ParticleContinuationJoinStates {{
    uvec4 continuation_join_states[];
}};

const uint INX_CONTINUATION_FLAG_SECONDS = 1u;
const uint INX_CONTINUATION_INVALID_INDEX = 0xffffffffu;

uint inx_continuation_record_base(uint record_index) {{
    return record_index * continuation_counters.record_stride_words;
}}

uint inx_continuation_linear_index() {{
    return gl_GlobalInvocationID.x
         + gl_GlobalInvocationID.y * gl_NumWorkGroups.x * gl_WorkGroupSize.x;
}}

bool inx_continuation_lane_pending(uint particle_index, uint lane_index) {{
    if (particle_index >= continuation_counters.particle_capacity
        || lane_index >= continuation_counters.lane_count) return false;
    uint slot = particle_index * continuation_counters.lane_count + lane_index;
    return atomicAdd(continuation_lane_slots[slot], 0u) != 0u;
}}

uint inx_continuation_join_state_index(uint particle_index, uint join_index) {{
    return particle_index * continuation_counters.join_count + join_index;
}}

uint inx_continuation_join_begin(
    uint particle_index,
    uint particle_generation,
    uint join_index,
    uint expected_mask
) {{
    if (particle_index >= continuation_counters.particle_capacity
        || join_index >= continuation_counters.join_count
        || expected_mask == 0u) return 0u;
    uint state_index = inx_continuation_join_state_index(particle_index, join_index);
    uint token = atomicAdd(continuation_join_states[state_index].x, 0u);
    uint generation = atomicAdd(continuation_join_states[state_index].w, 0u);
    if (token != 0u && generation == particle_generation) return token;

    uint replacement = atomicAdd(continuation_counters.branch_token_counter, 1u);
    if (replacement == 0u) {{
        replacement = atomicAdd(continuation_counters.branch_token_counter, 1u);
    }}
    uint observed = atomicCompSwap(
        continuation_join_states[state_index].x, token, replacement
    );
    if (observed != token) return observed;
    atomicExchange(continuation_join_states[state_index].y, expected_mask);
    atomicExchange(continuation_join_states[state_index].z, 0u);
    atomicExchange(continuation_join_states[state_index].w, particle_generation);
    memoryBarrierBuffer();
    return replacement;
}}

bool inx_continuation_join_has_arrived(
    uint particle_index,
    uint particle_generation,
    uint join_index,
    uint arrival_bit
) {{
    if (particle_index >= continuation_counters.particle_capacity
        || join_index >= continuation_counters.join_count) return false;
    uint state_index = inx_continuation_join_state_index(particle_index, join_index);
    return atomicAdd(continuation_join_states[state_index].x, 0u) != 0u
        && atomicAdd(continuation_join_states[state_index].w, 0u) == particle_generation
        && (atomicAdd(continuation_join_states[state_index].z, 0u) & arrival_bit) != 0u;
}}

bool inx_continuation_join_arrive(
    uint particle_index,
    uint particle_generation,
    uint join_index,
    uint branch_token,
    uint expected_mask,
    uint arrival_mask
) {{
    if (arrival_mask == 0u) return false;
    uint token = branch_token;
    if (token == 0u) {{
        token = inx_continuation_join_begin(
            particle_index, particle_generation, join_index, expected_mask
        );
    }}
    if (token == 0u
        || particle_index >= continuation_counters.particle_capacity
        || join_index >= continuation_counters.join_count) return false;
    uint state_index = inx_continuation_join_state_index(particle_index, join_index);
    if (atomicAdd(continuation_join_states[state_index].x, 0u) != token
        || atomicAdd(continuation_join_states[state_index].w, 0u) != particle_generation) {{
        return false;
    }}
    uint expected = atomicAdd(continuation_join_states[state_index].y, 0u);
    uint accepted = arrival_mask & expected;
    uint previous = atomicOr(continuation_join_states[state_index].z, accepted);
    uint combined = previous | accepted;
    if ((combined & expected) != expected || (previous & expected) == expected) {{
        return false;
    }}
    if (atomicCompSwap(continuation_join_states[state_index].x, token, 0u) != token) {{
        return false;
    }}
    atomicExchange(continuation_join_states[state_index].y, 0u);
    atomicExchange(continuation_join_states[state_index].z, 0u);
    atomicExchange(continuation_join_states[state_index].w, 0u);
    return true;
}}

uint inx_continuation_pop_free() {{
    uint observed = atomicAdd(continuation_counters.free_count, 0u);
    while (observed > 0u) {{
        uint prior = atomicCompSwap(continuation_counters.free_count, observed, observed - 1u);
        if (prior == observed) return continuation_free_records[observed - 1u];
        observed = prior;
    }}
    return INX_CONTINUATION_INVALID_INDEX;
}}

void inx_continuation_push_free(uint record_index) {{
    uint destination = atomicAdd(continuation_counters.free_count, 1u);
    if (destination < continuation_counters.continuation_capacity) {{
        continuation_free_records[destination] = record_index;
    }} else {{
        atomicAdd(continuation_counters.free_count, 0xffffffffu);
        atomicAdd(continuation_counters.dropped_capacity, 1u);
    }}
}}

bool inx_continuation_append_active(uint record_index) {{
    bool write_a = (continuation_counters.current_simulation_step & 1u) != 0u;
    uint destination = write_a
        ? atomicAdd(continuation_counters.active_count_a, 1u)
        : atomicAdd(continuation_counters.active_count_b, 1u);
    if (destination >= continuation_counters.continuation_capacity) {{
        if (write_a) atomicAdd(continuation_counters.active_count_a, 0xffffffffu);
        else atomicAdd(continuation_counters.active_count_b, 0xffffffffu);
        return false;
    }}
    if (write_a) continuation_active_records_a[destination] = record_index;
    else continuation_active_records_b[destination] = record_index;
    return true;
}}

void inx_continuation_add_seconds(float seconds, out uint wake_low, out uint wake_high) {{
    float duration = max(seconds, 0.0);
    float high_chunks = floor(duration / 4.294967296);
    uint add_high = uint(min(high_chunks, 4294967040.0));
    float remainder = max(duration - high_chunks * 4.294967296, 0.0);
    uint add_low = uint(min(floor(remainder * 1000000000.0 + 0.5), 4294967040.0));
    wake_low = continuation_counters.elapsed_time_low + add_low;
    uint carry = wake_low < continuation_counters.elapsed_time_low ? 1u : 0u;
    wake_high = continuation_counters.elapsed_time_high + add_high + carry;
}}

bool inx_continuation_suspend(
    uint particle_index,
    uint particle_generation,
    uint lane_index,
    uint resume_program_counter,
    uint wake_frame,
    uint wake_time_low,
    uint wake_time_high,
    uint flags,
    uint join_index,
    uint join_expected_mask,
    uint existing_record
) {{
    if (particle_index >= continuation_counters.particle_capacity
        || lane_index >= continuation_counters.lane_count) {{
        atomicAdd(continuation_counters.dropped_capacity, 1u);
        return false;
    }}
    uint lane_slot = particle_index * continuation_counters.lane_count + lane_index;
    uint record_index = existing_record;
    bool reusing_record = record_index != INX_CONTINUATION_INVALID_INDEX;
    if (record_index == INX_CONTINUATION_INVALID_INDEX) {{
        if (atomicAdd(continuation_lane_slots[lane_slot], 0u) != 0u) return true;
        record_index = inx_continuation_pop_free();
        if (record_index == INX_CONTINUATION_INVALID_INDEX) {{
            atomicAdd(continuation_counters.dropped_capacity, 1u);
            return false;
        }}
        uint previous = atomicCompSwap(
            continuation_lane_slots[lane_slot], 0u, record_index + 1u
        );
        if (previous != 0u) {{
            inx_continuation_push_free(record_index);
            return true;
        }}
    }} else if (atomicAdd(continuation_lane_slots[lane_slot], 0u) != record_index + 1u) {{
        atomicAdd(continuation_counters.stale_generation, 1u);
        return false;
    }}
    uint branch_token = 0u;
    if (join_index != INX_CONTINUATION_INVALID_INDEX) {{
        branch_token = inx_continuation_join_begin(
            particle_index, particle_generation, join_index, join_expected_mask
        );
        if (branch_token == 0u) {{
            if (!reusing_record) {{
                atomicCompSwap(continuation_lane_slots[lane_slot], record_index + 1u, 0u);
                inx_continuation_push_free(record_index);
            }}
            atomicAdd(continuation_counters.dropped_capacity, 1u);
            return false;
        }}
    }}
    uint base = inx_continuation_record_base(record_index);
    continuation_record_words[base + 0u] = particle_index;
    continuation_record_words[base + 1u] = particle_generation;
    continuation_record_words[base + 2u] = continuation_counters.program_generation;
    continuation_record_words[base + 3u] = resume_program_counter;
    continuation_record_words[base + 4u] = wake_frame;
    continuation_record_words[base + 5u] = wake_time_low;
    continuation_record_words[base + 6u] = wake_time_high;
    continuation_record_words[base + 7u] = lane_index;
    continuation_record_words[base + 8u] = branch_token;
    continuation_record_words[base + 9u] = join_index;
    continuation_record_words[base + 10u] = 0u;
    continuation_record_words[base + 11u] = flags;
    continuation_record_words[base + 12u] = 0u;
    continuation_record_words[base + 13u] = 0u;
    continuation_record_words[base + 14u] = 0u;
    continuation_record_words[base + 15u] = 0u;
    memoryBarrierBuffer();
    if (!inx_continuation_append_active(record_index)) {{
        if (!reusing_record) {{
            atomicCompSwap(continuation_lane_slots[lane_slot], record_index + 1u, 0u);
            inx_continuation_push_free(record_index);
        }}
        atomicAdd(continuation_counters.dropped_capacity, 1u);
        return false;
    }}
    return true;
}}

bool inx_suspend_frames(
    uint particle_index,
    uint particle_generation,
    uint lane_index,
    uint resume_program_counter,
    int frames,
    uint join_index,
    uint join_expected_mask,
    uint existing_record
) {{
    return inx_continuation_suspend(
        particle_index,
        particle_generation,
        lane_index,
        resume_program_counter,
        continuation_counters.current_simulation_step + uint(max(frames, 0)),
        0u,
        0u,
        0u,
        join_index,
        join_expected_mask,
        existing_record
    );
}}

bool inx_suspend_seconds(
    uint particle_index,
    uint particle_generation,
    uint lane_index,
    uint resume_program_counter,
    float seconds,
    uint join_index,
    uint join_expected_mask,
    uint existing_record
) {{
    uint wake_low = 0u;
    uint wake_high = 0u;
    inx_continuation_add_seconds(seconds, wake_low, wake_high);
    return inx_continuation_suspend(
        particle_index,
        particle_generation,
        lane_index,
        resume_program_counter,
        0u,
        wake_low,
        wake_high,
        INX_CONTINUATION_FLAG_SECONDS,
        join_index,
        join_expected_mask,
        existing_record
    );
}}
"""


def _continuation_scheduler_constants_glsl() -> str:
    return """
layout(push_constant) uniform ParticleContinuationConstants {
    uint capacity;
    uint particle_capacity;
    uint lane_count;
    uint join_count;
    uint program_generation;
    uint simulation_step;
    uint reset_serial;
    uint reset_requested;
    uint elapsed_time_low;
    uint elapsed_time_high;
    uint record_stride_words;
    uint system_seed;
    float delta_time;
    uint event_output_enabled;
    uint reserved0;
    uint reserved1;
} continuation_pc;
"""


def _continuation_prepare_glsl() -> str:
    return (
        "#version 450\n\n"
        "layout(local_size_x = 256, local_size_y = 1, local_size_z = 1) in;\n"
        + _continuation_bindings_glsl(0)
        + _continuation_scheduler_constants_glsl()
        + """
void main() {
    uint index = inx_continuation_linear_index();
    if (continuation_pc.reset_requested != 0u) {
        if (index < continuation_pc.capacity) {
            continuation_free_records[index] = index;
        }
        uint lane_slot_count = continuation_pc.particle_capacity * continuation_pc.lane_count;
        if (index < lane_slot_count) continuation_lane_slots[index] = 0u;
        uint join_state_count = continuation_pc.particle_capacity * continuation_pc.join_count;
        if (index < join_state_count) continuation_join_states[index] = uvec4(0u);
    }
    if (index != 0u) return;

    uint active_count = 0u;
    if (continuation_pc.reset_requested != 0u) {
        continuation_counters.free_count = continuation_pc.capacity;
        continuation_counters.active_count_a = 0u;
        continuation_counters.active_count_b = 0u;
        continuation_counters.ready_count = 0u;
        continuation_counters.dropped_capacity = 0u;
        continuation_counters.stale_generation = 0u;
        continuation_counters.resumed_count = 0u;
        continuation_counters.completed_count = 0u;
        continuation_counters.branch_token_counter = 1u;
    } else {
        bool read_a = (continuation_pc.simulation_step & 1u) == 0u;
        active_count = read_a
            ? continuation_counters.active_count_a
            : continuation_counters.active_count_b;
        if (read_a) continuation_counters.active_count_b = 0u;
        else continuation_counters.active_count_a = 0u;
        continuation_counters.ready_count = 0u;
    }
    continuation_counters.program_generation = continuation_pc.program_generation;
    continuation_counters.reset_serial = continuation_pc.reset_serial;
    continuation_counters.current_simulation_step = continuation_pc.simulation_step;
    continuation_counters.elapsed_time_low = continuation_pc.elapsed_time_low;
    continuation_counters.elapsed_time_high = continuation_pc.elapsed_time_high;
    continuation_counters.record_stride_words = continuation_pc.record_stride_words;
    continuation_counters.lane_count = continuation_pc.lane_count;
    continuation_counters.join_count = continuation_pc.join_count;
    continuation_counters.continuation_capacity = continuation_pc.capacity;
    continuation_counters.particle_capacity = continuation_pc.particle_capacity;

    uint group_count = (active_count + 255u) / 256u;
    uint group_x = min(group_count, 65535u);
    uint group_y = group_count == 0u ? 1u : (group_count + group_x - 1u) / group_x;
    continuation_classify_x = group_x;
    continuation_classify_y = group_y;
    continuation_classify_z = 1u;
    continuation_classify_reserved = 0u;
    continuation_dispatch_x = group_x;
    continuation_dispatch_y = group_y;
    continuation_dispatch_z = 1u;
    continuation_dispatch_reserved = 0u;
}
"""
    )


def _continuation_classify_glsl() -> str:
    return (
        "#version 450\n\n"
        "layout(local_size_x = 256, local_size_y = 1, local_size_z = 1) in;\n"
        + _continuation_bindings_glsl(0)
        + _continuation_scheduler_constants_glsl()
        + """
bool inx_continuation_time_reached(uint wake_low, uint wake_high) {
    return continuation_pc.elapsed_time_high > wake_high
        || (continuation_pc.elapsed_time_high == wake_high
            && continuation_pc.elapsed_time_low >= wake_low);
}

void inx_continuation_discard(uint record_index, uint base) {
    uint particle_index = continuation_record_words[base + 0u];
    uint lane_index = continuation_record_words[base + 7u];
    if (particle_index < continuation_pc.particle_capacity
        && lane_index < continuation_pc.lane_count) {
        uint lane_slot = particle_index * continuation_pc.lane_count + lane_index;
        atomicCompSwap(continuation_lane_slots[lane_slot], record_index + 1u, 0u);
    }
    inx_continuation_push_free(record_index);
}

void main() {
    uint queue_index = inx_continuation_linear_index();
    bool read_a = (continuation_pc.simulation_step & 1u) == 0u;
    uint active_count = read_a
        ? continuation_counters.active_count_a
        : continuation_counters.active_count_b;
    if (queue_index >= active_count) return;
    uint record_index = read_a
        ? continuation_active_records_a[queue_index]
        : continuation_active_records_b[queue_index];
    if (record_index >= continuation_pc.capacity) {
        atomicAdd(continuation_counters.stale_generation, 1u);
        return;
    }
    uint base = inx_continuation_record_base(record_index);
    if (continuation_record_words[base + 2u] != continuation_pc.program_generation) {
        atomicAdd(continuation_counters.stale_generation, 1u);
        inx_continuation_discard(record_index, base);
        return;
    }
    uint flags = continuation_record_words[base + 11u];
    bool ready = (flags & INX_CONTINUATION_FLAG_SECONDS) != 0u
        ? inx_continuation_time_reached(
            continuation_record_words[base + 5u],
            continuation_record_words[base + 6u]
          )
        : int(continuation_pc.simulation_step - continuation_record_words[base + 4u]) >= 0;
    if (!ready) {
        if (!inx_continuation_append_active(record_index)) {
            atomicAdd(continuation_counters.dropped_capacity, 1u);
            inx_continuation_discard(record_index, base);
        }
        return;
    }
    uint destination = atomicAdd(continuation_counters.ready_count, 1u);
    if (destination < continuation_pc.capacity) {
        continuation_ready_records[destination] = record_index;
    } else {
        atomicAdd(continuation_counters.ready_count, 0xffffffffu);
        atomicAdd(continuation_counters.dropped_capacity, 1u);
        inx_continuation_discard(record_index, base);
    }
}
"""
    )


def _continuation_dispatch_glsl(
    emitter: ParticleEmitterKernelIR,
    fields: tuple[tuple[str, TypeRef, str], ...],
    data_interface_layout: dict[str, Any],
    events,
    emitter_index: int,
    parameter_slots: Mapping[str, tuple[int, TypeRef]],
    continuation_lane_indices: Mapping[str, int],
    continuation_join_indices: Mapping[tuple[ParticleStage, str], int],
) -> str:
    flow_by_stage = {flow.lifecycle_stage: flow for flow in emitter.flows}
    function_by_stage = {
        KernelStage.INIT: emitter.init,
        KernelStage.UPDATE: emitter.update,
        KernelStage.RENDERING: emitter.rendering,
    }
    cases = []
    for suspension in sorted(
        emitter.suspensions, key=lambda value: value.resume_program_counter
    ):
        flow = flow_by_stage[suspension.lifecycle_stage]
        function = function_by_stage[flow.kernel_stage]
        runtime_lane = continuation_lane_indices[suspension.lane_stable_id]
        if suspension.resume_instruction_index < 0:
            body, event_body = "", ""
        else:
            if any(
                instruction.opcode == "event_payload"
                for instruction in function.instructions[
                    suspension.resume_instruction_index :
                ]
            ):
                raise GpuParticleCompileError(
                    "GPU Event Payload values cannot remain live across Wait; "
                    "copy the value into a particle attribute first"
                )
            compiler = _StageCompiler(
                emitter,
                fields,
                data_interface_layout,
                events,
                emitter_index,
                parameter_slots=parameter_slots,
                continuation_lane_indices=continuation_lane_indices,
                continuation_join_indices=continuation_join_indices,
                existing_continuation_lane=runtime_lane,
            )
            body, event_body = compiler.compile_resume(function, flow, suspension)
        finite = _finite_state_check(function, fields)
        cases.append(
            f"""        case {suspension.resume_program_counter}u: {{
{body}
{event_body}
            particle_alive = particle_alive && ({finite});
            break;
        }}"""
        )
    event_output = any(
        instruction.opcode == "event_append"
        for function in (emitter.init, emitter.update, emitter.rendering)
        for instruction in function.instructions
    )
    prelude = _shader_prelude(
        fields,
        emitter.random_seed,
        data_interface_layout,
        len(parameter_slots),
        continuation_dispatch=True,
    )
    bindings = _continuation_bindings_glsl(5)
    event_bindings = _event_output_bindings(4) if event_output else ""
    switch_cases = "\n".join(cases)
    return (
        prelude
        + bindings
        + event_bindings
        + f"""
void inx_finish_continuation(uint record_index, uint particle_index, uint lane_index) {{
    if (particle_index < continuation_counters.particle_capacity
        && lane_index < continuation_counters.lane_count) {{
        uint lane_slot = particle_index * continuation_counters.lane_count + lane_index;
        atomicCompSwap(continuation_lane_slots[lane_slot], record_index + 1u, 0u);
    }}
    inx_continuation_push_free(record_index);
}}

void main() {{
    uint ready_index = inx_continuation_linear_index();
    if (ready_index >= continuation_counters.ready_count) return;
    uint inx_continuation_record_index = continuation_ready_records[ready_index];
    if (inx_continuation_record_index >= pc.continuation_capacity) return;
    uint record_base = inx_continuation_record_base(inx_continuation_record_index);
    uint particle_index = continuation_record_words[record_base + 0u];
    uint particle_generation = continuation_record_words[record_base + 1u];
    uint record_program_generation = continuation_record_words[record_base + 2u];
    uint resume_program_counter = continuation_record_words[record_base + 3u];
    uint lane_index = continuation_record_words[record_base + 7u];
    uint inx_continuation_record_branch_token = continuation_record_words[record_base + 8u];
    uint inx_continuation_record_join_index = continuation_record_words[record_base + 9u];
    if (record_program_generation != pc.continuation_program_generation
        || particle_index >= pc.capacity
        || lane_index >= pc.continuation_lane_count
        || states[particle_index].alive == 0u
        || states[particle_index].spawn_generation != particle_generation) {{
        atomicAdd(continuation_counters.stale_generation, 1u);
        inx_finish_continuation(
            inx_continuation_record_index, particle_index, lane_index
        );
        return;
    }}

    ParticleState state = states[particle_index];
    bool particle_alive = true;
    bool inx_continuation_resuspended = false;
    switch (resume_program_counter) {{
{switch_cases}
        default:
            atomicAdd(continuation_counters.stale_generation, 1u);
            break;
    }}
    state.alive = particle_alive ? 1u : 0u;
    states[particle_index] = state;
    if (!particle_alive) inx_push_free(particle_index);
    if (!inx_continuation_resuspended) {{
        inx_finish_continuation(
            inx_continuation_record_index, particle_index, lane_index
        );
        atomicAdd(continuation_counters.completed_count, 1u);
    }} else {{
        atomicAdd(continuation_counters.resumed_count, 1u);
    }}
}}
"""
    )


def _continuation_source(
    emitter: ParticleEmitterKernelIR,
    fields: tuple[tuple[str, TypeRef, str], ...],
    data_interface_layout: dict[str, Any],
    events,
    emitter_index: int,
    parameter_slots: Mapping[str, tuple[int, TypeRef]],
    continuation_lane_indices: Mapping[str, int],
    continuation_join_indices: Mapping[tuple[ParticleStage, str], int],
) -> GpuParticleContinuationSource | None:
    if not emitter.suspensions:
        return None
    if not continuation_lane_indices:
        raise GpuParticleCompileError(
            "GPU continuation has no statically bounded execution lanes"
        )
    join_count = sum(len(flow.joins) for flow in emitter.flows)
    return GpuParticleContinuationSource(
        64,
        len(continuation_lane_indices),
        join_count,
        _continuation_prepare_glsl(),
        _continuation_classify_glsl(),
        _continuation_dispatch_glsl(
            emitter,
            fields,
            data_interface_layout,
            events,
            emitter_index,
            parameter_slots,
            continuation_lane_indices,
            continuation_join_indices,
        ),
    )


def _shader_prelude(
    fields: tuple[tuple[str, TypeRef, str], ...],
    emitter_seed: int,
    data_interface_layout: dict[str, Any],
    parameter_count: int,
    *,
    continuation_dispatch: bool = False,
) -> str:
    state_fields = "\n".join(
        f"    {_storage_type(value_type)} {field};"
        for _stable_id, value_type, field in fields
    )
    data_interface_glsl = "\n".join(
        part
        for part in (
            _mesh_shape_glsl(data_interface_layout),
            _volume_interface_glsl(data_interface_layout),
            _texture_parameter_glsl(data_interface_layout),
        )
        if part
    )
    parameter_glsl = (
        "layout(std430, set = 0, binding = 7) readonly buffer "
        "ParticleParameters { uvec4 parameter_words[]; };"
        if parameter_count
        else ""
    )
    push_constants = (
        """layout(push_constant) uniform ParticlePushConstants {
    uint continuation_capacity;
    uint capacity;
    uint continuation_lane_count;
    uint continuation_join_count;
    uint continuation_program_generation;
    uint simulation_step;
    uint continuation_reset_serial;
    uint continuation_reset_requested;
    uint continuation_elapsed_time_low;
    uint continuation_elapsed_time_high;
    uint continuation_record_stride_words;
    uint system_seed;
    float delta_time;
    uint reserved;
    uint continuation_reserved0;
    uint continuation_reserved1;
} pc;"""
        if continuation_dispatch
        else """layout(push_constant) uniform ParticlePushConstants {
    uint capacity;
    uint invocation_count;
    uint spawn_base_id;
    uint spawn_generation;
    uint system_seed;
    uint simulation_step;
    float delta_time;
    uint reserved;
} pc;"""
    )
    return f"""#version 450

layout(local_size_x = 256, local_size_y = 1, local_size_z = 1) in;

struct ParticleState {{
    uint alive;
    uint spawn_generation;
{state_fields}
}};

struct ParticleRenderInstance {{
    vec4 position_size;
    vec4 color;
    vec4 rotation_custom;
    vec4 scale_custom;
    uvec4 ribbon_data;
    vec4 custom_data;
    vec4 previous_position_history;
}};

layout(std430, set = 0, binding = 0) buffer ParticleStates {{ ParticleState states[]; }};
layout(std430, set = 0, binding = 1) buffer ParticleFreeList {{ uint free_slots[]; }};
layout(std430, set = 0, binding = 2) buffer ParticleCounters {{
    uint free_count;
    uint visible_count;
    uint dropped_count;
    uint reserved_count;
}} counters;
layout(std430, set = 0, binding = 3) buffer ParticleInstances {{ ParticleRenderInstance instances[]; }};
layout(std430, set = 0, binding = 4) buffer ParticleIndirect {{
    uint vertex_count;
    uint instance_count;
    uint first_vertex;
    uint first_instance;
}} indirect_args;
layout(std140, set = 0, binding = 5) uniform ParticleTransforms {{
    mat4 emitter_to_world;
    mat4 world_to_emitter;
    mat4 simulation_to_world;
    mat4 world_to_simulation;
}} transforms;
layout(std430, set = 0, binding = 6) buffer ParticleRenderIndices {{ uint render_indices[]; }};
{parameter_glsl}
struct InxParticleAffine {{
    vec4 row0;
    vec4 row1;
    vec4 row2;
}};
struct InxParticleCollider {{
    InxParticleAffine collider_to_world;
    InxParticleAffine world_to_collider;
    InxParticleAffine previous_world_to_collider;
    vec4 shape;
    uvec4 geometry;
    vec4 linear_velocity;
    vec4 material;
    vec4 world_aabb_min;
    vec4 world_aabb_max;
    vec4 previous_world_aabb_min;
    vec4 previous_world_aabb_max;
    uvec4 metadata;
    uvec4 identity;
}};
struct InxParticleCollisionBvhNode {{
    vec4 bounds_min;
    vec4 bounds_max;
    uvec4 metadata;
}};
layout(std430, set = 0, binding = 8) readonly buffer ParticleCollisionSceneHeader {{
    uint collider_count;
    uint static_collider_count;
    uint collision_scene_revision_low;
    uint collision_scene_revision_high;
    vec4 grid_min_inv_cell_size;
    uvec4 grid_dimensions;
    uvec4 topology;
}} inx_collision_scene;
layout(std430, set = 0, binding = 9) readonly buffer ParticleCollisionSceneRecords {{
    InxParticleCollider particle_colliders[];
}};
layout(std430, set = 0, binding = 10) readonly buffer ParticleCollisionGridOffsets {{
    uint particle_collision_grid_offsets[];
}};
layout(std430, set = 0, binding = 11) readonly buffer ParticleCollisionGridColliderIndices {{
    uint particle_collision_grid_collider_indices[];
}};
layout(std430, set = 0, binding = 12) readonly buffer ParticleCollisionMeshVertices {{
    vec4 particle_collision_mesh_vertices[];
}};
layout(std430, set = 0, binding = 13) readonly buffer ParticleCollisionMeshIndices {{
    uint particle_collision_mesh_indices[];
}};
layout(std430, set = 0, binding = 14) readonly buffer ParticleCollisionMeshBvh {{
    InxParticleCollisionBvhNode particle_collision_mesh_bvh[];
}};
layout(std430, set = 0, binding = 15) readonly buffer ParticleSimulationControl {{
    uint any_view_visible;
    uint simulation_allowed;
    uint offscreen_policy;
    uint simulation_control_reserved;
}} simulation_control;
{data_interface_glsl}
{push_constants}

const uint INX_EMITTER_SEED = {emitter_seed}u;
const uint INX_INVALID_INDEX = 0xffffffffu;

uint inx_pop_free() {{
    uint observed = atomicAdd(counters.free_count, 0u);
    while (observed > 0u) {{
        uint prior = atomicCompSwap(counters.free_count, observed, observed - 1u);
        if (prior == observed) return free_slots[observed - 1u];
        observed = prior;
    }}
    return INX_INVALID_INDEX;
}}

void inx_push_free(uint particle_index) {{
    uint destination = atomicAdd(counters.free_count, 1u);
    if (destination < pc.capacity) free_slots[destination] = particle_index;
    else atomicAdd(counters.free_count, 0xffffffffu);
}}

uint inx_random_u32(uint node_seed, uint particle_id, uint generation, uint random_slot) {{
    uint value = 0x811c9dc5u;
    value = (value ^ pc.system_seed) * 0x01000193u; value ^= value >> 16;
    value = (value ^ INX_EMITTER_SEED) * 0x01000193u; value ^= value >> 16;
    value = (value ^ node_seed) * 0x01000193u; value ^= value >> 16;
    value = (value ^ particle_id) * 0x01000193u; value ^= value >> 16;
    value = (value ^ generation) * 0x01000193u; value ^= value >> 16;
    value = (value ^ pc.simulation_step) * 0x01000193u; value ^= value >> 16;
    value = (value ^ random_slot) * 0x01000193u; value ^= value >> 16;
    value ^= value >> 16; value *= 0x7feb352du; value ^= value >> 15;
    value *= 0x846ca68bu; value ^= value >> 16;
    return value;
}}

float inx_random01(uint node_seed, uint random_slot, uint particle_id, uint generation) {{
    return float(inx_random_u32(node_seed, particle_id, generation, random_slot) >> 8u) * (1.0 / 16777216.0);
}}

float inx_random_range(float low, float high, uint node_seed, uint random_slot, uint particle_id, uint generation) {{
    return low + inx_random01(node_seed, random_slot, particle_id, generation) * (high - low);
}}

uint inx_noise_hash(uvec3 cell, uint seed) {{
    uint value = cell.x * 0x8da6b343u;
    value ^= cell.y * 0xd8163841u;
    value ^= cell.z * 0xcb1ab31fu;
    value ^= seed;
    value ^= value >> 16u; value *= 0x7feb352du; value ^= value >> 15u;
    value *= 0x846ca68bu; value ^= value >> 16u;
    return value;
}}

float inx_noise_corner(ivec3 cell, uint seed) {{
    return float(inx_noise_hash(uvec3(cell), seed) >> 8u) * (1.0 / 16777216.0);
}}

float inx_value_noise_3d(vec3 position, float frequency, uint seed) {{
    vec3 scaled = position * frequency;
    ivec3 base = ivec3(floor(scaled));
    vec3 fraction = fract(scaled);
    vec3 smooth_value = fraction * fraction * (vec3(3.0) - vec3(2.0) * fraction);
    float z0y0 = mix(inx_noise_corner(base + ivec3(0, 0, 0), seed),
                      inx_noise_corner(base + ivec3(1, 0, 0), seed), smooth_value.x);
    float z0y1 = mix(inx_noise_corner(base + ivec3(0, 1, 0), seed),
                      inx_noise_corner(base + ivec3(1, 1, 0), seed), smooth_value.x);
    float z1y0 = mix(inx_noise_corner(base + ivec3(0, 0, 1), seed),
                      inx_noise_corner(base + ivec3(1, 0, 1), seed), smooth_value.x);
    float z1y1 = mix(inx_noise_corner(base + ivec3(0, 1, 1), seed),
                      inx_noise_corner(base + ivec3(1, 1, 1), seed), smooth_value.x);
    return mix(mix(z0y0, z0y1, smooth_value.y),
               mix(z1y0, z1y1, smooth_value.y), smooth_value.z);
}}

vec3 inx_vector_noise_3d(vec3 position, float frequency, uint seed) {{
    return vec3(
        inx_value_noise_3d(position, frequency, seed),
        inx_value_noise_3d(position, frequency, seed ^ 0x9e3779b9u),
        inx_value_noise_3d(position, frequency, seed ^ 0x85ebca6bu)
    ) * 2.0 - vec3(1.0);
}}

vec2 inx_safe_normalize(vec2 value) {{ float length_value = length(value); return length_value > 0.0 ? value / length_value : vec2(0.0); }}
vec3 inx_safe_normalize(vec3 value) {{ float length_value = length(value); return length_value > 0.0 ? value / length_value : vec3(0.0); }}
vec4 inx_safe_normalize(vec4 value) {{ float length_value = length(value); return length_value > 0.0 ? value / length_value : vec4(0.0); }}

vec3 inx_collide_plane_position(vec3 position, vec3 velocity, vec3 point, vec3 normal,
                                float radius, float restitution, float friction) {{
    vec3 n = inx_safe_normalize(normal);
    float penetration = max(radius, 0.0) - dot(position - point, n);
    return penetration > 0.0 ? position + n * penetration : position;
}}

vec3 inx_collide_plane_velocity(vec3 position, vec3 velocity, vec3 point, vec3 normal,
                                float radius, float restitution, float friction) {{
    vec3 n = inx_safe_normalize(normal);
    float normal_speed = dot(velocity, n);
    bool collision = dot(position - point, n) < max(radius, 0.0) && normal_speed < 0.0;
    if (!collision) return velocity;
    vec3 tangent_velocity = velocity - n * normal_speed;
    return tangent_velocity * (1.0 - clamp(friction, 0.0, 1.0))
         - n * normal_speed * clamp(restitution, 0.0, 1.0);
}}

vec3 inx_sphere_collision_normal(vec3 position, vec3 velocity, vec3 center) {{
    vec3 delta = position - center;
    float distance_value = length(delta);
    if (distance_value > 1.0e-6) return delta / distance_value;
    float speed = length(velocity);
    return speed > 1.0e-6 ? -velocity / speed : vec3(0.0, 1.0, 0.0);
}}

vec3 inx_collide_sphere_position(vec3 position, vec3 velocity, vec3 center,
                                 float sphere_radius, float particle_radius,
                                 float restitution, float friction) {{
    vec3 n = inx_sphere_collision_normal(position, velocity, center);
    float combined_radius = max(sphere_radius, 0.0) + max(particle_radius, 0.0);
    float penetration = combined_radius - length(position - center);
    return penetration > 0.0 ? position + n * penetration : position;
}}

vec3 inx_collide_sphere_velocity(vec3 position, vec3 velocity, vec3 center,
                                 float sphere_radius, float particle_radius,
                                 float restitution, float friction) {{
    vec3 n = inx_sphere_collision_normal(position, velocity, center);
    float normal_speed = dot(velocity, n);
    float combined_radius = max(sphere_radius, 0.0) + max(particle_radius, 0.0);
    bool collision = length(position - center) < combined_radius && normal_speed < 0.0;
    if (!collision) return velocity;
    vec3 tangent_velocity = velocity - n * normal_speed;
    return tangent_velocity * (1.0 - clamp(friction, 0.0, 1.0))
         - n * normal_speed * clamp(restitution, 0.0, 1.0);
}}

ivec3 inx_collision_grid_cell(vec3 world_position) {{
    vec3 relative = (world_position - inx_collision_scene.grid_min_inv_cell_size.xyz)
                  * inx_collision_scene.grid_min_inv_cell_size.w;
    return clamp(ivec3(floor(relative)), ivec3(0),
                 ivec3(inx_collision_scene.grid_dimensions.xyz) - ivec3(1));
}}

uint inx_collision_grid_index(ivec3 cell) {{
    uvec3 dimensions = inx_collision_scene.grid_dimensions.xyz;
    return uint(cell.x) + dimensions.x * (uint(cell.y) + dimensions.y * uint(cell.z));
}}

vec3 inx_particle_affine_point(InxParticleAffine transform_value, vec3 point_value) {{
    vec4 homogeneous = vec4(point_value, 1.0);
    return vec3(dot(transform_value.row0, homogeneous),
                dot(transform_value.row1, homogeneous),
                dot(transform_value.row2, homogeneous));
}}

mat3 inx_particle_affine_linear(InxParticleAffine transform_value) {{
    return mat3(vec3(transform_value.row0.x, transform_value.row1.x, transform_value.row2.x),
                vec3(transform_value.row0.y, transform_value.row1.y, transform_value.row2.y),
                vec3(transform_value.row0.z, transform_value.row1.z, transform_value.row2.z));
}}

float inx_collision_local_radius(InxParticleAffine world_to_collider, float world_radius) {{
    mat3 linear = inx_particle_affine_linear(world_to_collider);
    float inverse_scale = max(length(linear[0]), max(length(linear[1]), length(linear[2])));
    return max(world_radius, 0.0) * inverse_scale;
}}

vec3 inx_collision_world_normal(InxParticleCollider collider, vec3 local_normal) {{
    return inx_safe_normalize(
        transpose(inx_particle_affine_linear(collider.world_to_collider)) * local_normal);
}}

bool inx_sweep_sphere(vec3 start_position, vec3 end_position, float radius,
                      out float hit_time, out vec3 hit_normal) {{
    vec3 displacement = end_position - start_position;
    float radius_squared = radius * radius;
    float start_distance = dot(start_position, start_position) - radius_squared;
    // Initial overlap is handled by the shape's penetration solver. Treating
    // it as a time-zero sweep hit would choose an arbitrary inward normal.
    if (start_distance <= 0.0) return false;
    float displacement_squared = dot(displacement, displacement);
    if (displacement_squared <= 1.0e-12) return false;
    float projected = dot(start_position, displacement);
    if (projected >= 0.0) return false;
    float discriminant = projected * projected - displacement_squared * start_distance;
    if (discriminant < 0.0) return false;
    float candidate = (-projected - sqrt(discriminant)) / displacement_squared;
    if (candidate < 0.0 || candidate > 1.0) return false;
    vec3 point = start_position + displacement * candidate;
    hit_time = candidate;
    hit_normal = inx_safe_normalize(point);
    return true;
}}

bool inx_sweep_box(vec3 start_position, vec3 end_position, vec3 half_extent,
                   out float hit_time, out vec3 hit_normal) {{
    if (all(lessThanEqual(abs(start_position), half_extent))) return false;
    vec3 displacement = end_position - start_position;
    float entry_time = 0.0;
    float exit_time = 1.0;
    vec3 entry_normal = vec3(0.0);
    for (uint axis = 0u; axis < 3u; ++axis) {{
        if (abs(displacement[axis]) <= 1.0e-7) {{
            if (start_position[axis] < -half_extent[axis] ||
                start_position[axis] > half_extent[axis])
                return false;
            continue;
        }}
        float inverse_direction = 1.0 / displacement[axis];
        float near_time = (-half_extent[axis] - start_position[axis]) * inverse_direction;
        float far_time = (half_extent[axis] - start_position[axis]) * inverse_direction;
        float normal_sign = -1.0;
        if (near_time > far_time) {{
            float temporary = near_time;
            near_time = far_time;
            far_time = temporary;
            normal_sign = 1.0;
        }}
        if (near_time > entry_time) {{
            entry_time = near_time;
            entry_normal = vec3(0.0);
            entry_normal[axis] = normal_sign;
        }}
        exit_time = min(exit_time, far_time);
        if (entry_time > exit_time) return false;
    }}
    if (entry_time < 0.0 || entry_time > 1.0 || dot(entry_normal, entry_normal) == 0.0)
        return false;
    hit_time = entry_time;
    hit_normal = entry_normal;
    return true;
}}

bool inx_sweep_capsule(vec3 start_position, vec3 end_position, uint axis,
                       float half_segment, float radius,
                       out float hit_time, out vec3 hit_normal) {{
    vec3 segment_start = vec3(0.0);
    vec3 segment_end = vec3(0.0);
    segment_start[axis] = -half_segment;
    segment_end[axis] = half_segment;
    vec3 capsule_axis = segment_end - segment_start;
    float axis_length_squared = dot(capsule_axis, capsule_axis);
    if (axis_length_squared <= 1.0e-12)
        return inx_sweep_sphere(start_position, end_position, radius, hit_time, hit_normal);

    float start_axis_fraction = clamp(
        dot(start_position - segment_start, capsule_axis) / axis_length_squared, 0.0, 1.0);
    vec3 start_closest = segment_start + capsule_axis * start_axis_fraction;
    if (dot(start_position - start_closest, start_position - start_closest) <= radius * radius)
        return false;

    vec3 displacement = end_position - start_position;
    vec3 from_segment_start = start_position - segment_start;
    float axis_dot_displacement = dot(capsule_axis, displacement);
    float axis_dot_origin = dot(capsule_axis, from_segment_start);
    float displacement_dot_origin = dot(displacement, from_segment_start);
    float displacement_squared = dot(displacement, displacement);
    float origin_squared = dot(from_segment_start, from_segment_start);
    float cylinder_a = axis_length_squared * displacement_squared
                     - axis_dot_displacement * axis_dot_displacement;
    float cylinder_b = axis_length_squared * displacement_dot_origin
                     - axis_dot_origin * axis_dot_displacement;
    float cylinder_c = axis_length_squared * origin_squared
                     - axis_dot_origin * axis_dot_origin
                     - radius * radius * axis_length_squared;

    bool found = false;
    float earliest = 2.0;
    vec3 earliest_normal = vec3(0.0);
    float cylinder_discriminant = cylinder_b * cylinder_b - cylinder_a * cylinder_c;
    if (abs(cylinder_a) > 1.0e-12 && cylinder_discriminant >= 0.0) {{
        float candidate = (-cylinder_b - sqrt(cylinder_discriminant)) / cylinder_a;
        float axis_position = axis_dot_origin + candidate * axis_dot_displacement;
        if (candidate >= 0.0 && candidate <= 1.0 &&
            axis_position > 0.0 && axis_position < axis_length_squared) {{
            vec3 point = start_position + displacement * candidate;
            vec3 axis_point = segment_start + capsule_axis * (axis_position / axis_length_squared);
            earliest = candidate;
            earliest_normal = inx_safe_normalize(point - axis_point);
            found = true;
        }}
    }}

    float cap_time = 0.0;
    vec3 cap_normal = vec3(0.0);
    if (inx_sweep_sphere(start_position - segment_start, end_position - segment_start,
                         radius, cap_time, cap_normal) && cap_time < earliest) {{
        earliest = cap_time;
        earliest_normal = cap_normal;
        found = true;
    }}
    if (inx_sweep_sphere(start_position - segment_end, end_position - segment_end,
                         radius, cap_time, cap_normal) && cap_time < earliest) {{
        earliest = cap_time;
        earliest_normal = cap_normal;
        found = true;
    }}
    if (!found) return false;
    hit_time = earliest;
    hit_normal = earliest_normal;
    return true;
}}

vec3 inx_closest_point_triangle(vec3 point_value, vec3 a, vec3 b, vec3 c) {{
    vec3 ab = b - a;
    vec3 ac = c - a;
    vec3 ap = point_value - a;
    float d1 = dot(ab, ap);
    float d2 = dot(ac, ap);
    if (d1 <= 0.0 && d2 <= 0.0) return a;
    vec3 bp = point_value - b;
    float d3 = dot(ab, bp);
    float d4 = dot(ac, bp);
    if (d3 >= 0.0 && d4 <= d3) return b;
    float vc = d1 * d4 - d3 * d2;
    if (vc <= 0.0 && d1 >= 0.0 && d3 <= 0.0)
        return a + ab * (d1 / max(d1 - d3, 1.0e-12));
    vec3 cp = point_value - c;
    float d5 = dot(ab, cp);
    float d6 = dot(ac, cp);
    if (d6 >= 0.0 && d5 <= d6) return c;
    float vb = d5 * d2 - d1 * d6;
    if (vb <= 0.0 && d2 >= 0.0 && d6 <= 0.0)
        return a + ac * (d2 / max(d2 - d6, 1.0e-12));
    float va = d3 * d6 - d5 * d4;
    if (va <= 0.0 && d4 - d3 >= 0.0 && d5 - d6 >= 0.0)
        return b + (c - b) * ((d4 - d3) / max((d4 - d3) + (d5 - d6), 1.0e-12));
    float denominator = max(va + vb + vc, 1.0e-12);
    return a + ab * (vb / denominator) + ac * (vc / denominator);
}}

bool inx_point_in_triangle(vec3 point_value, vec3 a, vec3 b, vec3 c, vec3 normal_value) {{
    const float epsilon = -1.0e-5;
    return dot(cross(b - a, point_value - a), normal_value) >= epsilon &&
           dot(cross(c - b, point_value - b), normal_value) >= epsilon &&
           dot(cross(a - c, point_value - c), normal_value) >= epsilon;
}}

bool inx_sweep_segment_capsule(vec3 start_position, vec3 end_position, vec3 segment_start,
                               vec3 segment_end, float radius,
                               out float hit_time, out vec3 hit_normal) {{
    vec3 capsule_axis = segment_end - segment_start;
    float axis_length_squared = dot(capsule_axis, capsule_axis);
    if (axis_length_squared <= 1.0e-12)
        return inx_sweep_sphere(start_position - segment_start, end_position - segment_start,
                                radius, hit_time, hit_normal);
    float start_fraction = clamp(dot(start_position - segment_start, capsule_axis) /
                                 axis_length_squared, 0.0, 1.0);
    vec3 start_closest = segment_start + capsule_axis * start_fraction;
    if (dot(start_position - start_closest, start_position - start_closest) <= radius * radius)
        return false;

    vec3 displacement = end_position - start_position;
    vec3 origin = start_position - segment_start;
    float axis_dot_displacement = dot(capsule_axis, displacement);
    float axis_dot_origin = dot(capsule_axis, origin);
    float displacement_dot_origin = dot(displacement, origin);
    float displacement_squared = dot(displacement, displacement);
    float origin_squared = dot(origin, origin);
    float qa = axis_length_squared * displacement_squared - axis_dot_displacement * axis_dot_displacement;
    float qb = axis_length_squared * displacement_dot_origin - axis_dot_origin * axis_dot_displacement;
    float qc = axis_length_squared * origin_squared - axis_dot_origin * axis_dot_origin
             - radius * radius * axis_length_squared;
    bool found = false;
    float earliest = 2.0;
    vec3 earliest_normal = vec3(0.0);
    float discriminant = qb * qb - qa * qc;
    if (abs(qa) > 1.0e-12 && discriminant >= 0.0) {{
        float candidate = (-qb - sqrt(discriminant)) / qa;
        float axis_position = axis_dot_origin + candidate * axis_dot_displacement;
        if (candidate >= 0.0 && candidate <= 1.0 && axis_position > 0.0 &&
            axis_position < axis_length_squared) {{
            vec3 center = start_position + displacement * candidate;
            vec3 edge_point = segment_start + capsule_axis * (axis_position / axis_length_squared);
            earliest = candidate;
            earliest_normal = inx_safe_normalize(center - edge_point);
            found = true;
        }}
    }}
    float candidate_time = 0.0;
    vec3 candidate_normal = vec3(0.0);
    if (inx_sweep_sphere(start_position - segment_start, end_position - segment_start,
                         radius, candidate_time, candidate_normal) && candidate_time < earliest) {{
        earliest = candidate_time;
        earliest_normal = candidate_normal;
        found = true;
    }}
    if (inx_sweep_sphere(start_position - segment_end, end_position - segment_end,
                         radius, candidate_time, candidate_normal) && candidate_time < earliest) {{
        earliest = candidate_time;
        earliest_normal = candidate_normal;
        found = true;
    }}
    if (!found) return false;
    hit_time = earliest;
    hit_normal = earliest_normal;
    return true;
}}

bool inx_sweep_triangle(vec3 start_position, vec3 end_position, float radius,
                        vec3 a, vec3 b, vec3 c,
                        out float hit_time, out vec3 hit_normal) {{
    vec3 raw_normal = cross(b - a, c - a);
    float normal_length = length(raw_normal);
    if (normal_length <= 1.0e-8) return false;
    vec3 triangle_normal = raw_normal / normal_length;
    vec3 displacement = end_position - start_position;
    float start_distance = dot(start_position - a, triangle_normal);
    float distance_delta = dot(displacement, triangle_normal);
    bool found = false;
    float earliest = 2.0;
    vec3 earliest_normal = vec3(0.0);
    if (abs(distance_delta) > 1.0e-12) {{
        for (int side = -1; side <= 1; side += 2) {{
            float signed_radius = float(side) * radius;
            float candidate = (signed_radius - start_distance) / distance_delta;
            if (candidate < 0.0 || candidate > 1.0 || candidate >= earliest) continue;
            vec3 center = start_position + displacement * candidate;
            vec3 contact = center - triangle_normal * signed_radius;
            vec3 oriented_normal = triangle_normal * float(side);
            if (inx_point_in_triangle(contact, a, b, c, triangle_normal) &&
                dot(displacement, oriented_normal) < 0.0) {{
                earliest = candidate;
                earliest_normal = oriented_normal;
                found = true;
            }}
        }}
    }}
    float edge_time = 0.0;
    vec3 edge_normal = vec3(0.0);
    if (inx_sweep_segment_capsule(start_position, end_position, a, b, radius,
                                  edge_time, edge_normal) && edge_time < earliest) {{
        earliest = edge_time; earliest_normal = edge_normal; found = true;
    }}
    if (inx_sweep_segment_capsule(start_position, end_position, b, c, radius,
                                  edge_time, edge_normal) && edge_time < earliest) {{
        earliest = edge_time; earliest_normal = edge_normal; found = true;
    }}
    if (inx_sweep_segment_capsule(start_position, end_position, c, a, radius,
                                  edge_time, edge_normal) && edge_time < earliest) {{
        earliest = edge_time; earliest_normal = edge_normal; found = true;
    }}
    if (!found) return false;
    hit_time = earliest;
    hit_normal = earliest_normal;
    return true;
}}

bool inx_mesh_node_overlaps_sweep(InxParticleCollisionBvhNode node, vec3 start_position,
                                  vec3 end_position, float radius) {{
    vec3 swept_min = min(start_position, end_position) - vec3(radius);
    vec3 swept_max = max(start_position, end_position) + vec3(radius);
    return !any(lessThan(swept_max, node.bounds_min.xyz)) &&
           !any(greaterThan(swept_min, node.bounds_max.xyz));
}}

bool inx_collision_mesh(InxParticleCollider collider, vec3 previous_world_position,
                        vec3 world_position, float world_radius,
                        out vec3 corrected_world_position, out vec3 world_normal) {{
    if (collider.geometry.w == 0u || collider.geometry.z >= inx_collision_scene.topology.z)
        return false;
    vec3 local_position = inx_particle_affine_point(collider.world_to_collider, world_position);
    vec3 local_previous =
        inx_particle_affine_point(collider.previous_world_to_collider, previous_world_position);
    float local_radius = inx_collision_local_radius(collider.world_to_collider, world_radius);
    uint stack[64];
    uint stack_size = 1u;
    stack[0] = collider.geometry.z;
    bool found_sweep = false;
    float earliest = 2.0;
    vec3 earliest_normal = vec3(0.0);
    bool found_overlap = false;
    float closest_distance_squared = local_radius * local_radius;
    vec3 closest_normal = vec3(0.0);
    vec3 closest_surface = vec3(0.0);
    while (stack_size > 0u) {{
        uint node_index = stack[--stack_size];
        if (node_index >= inx_collision_scene.topology.z) continue;
        InxParticleCollisionBvhNode node = particle_collision_mesh_bvh[node_index];
        if (!inx_mesh_node_overlaps_sweep(node, local_previous, local_position, local_radius)) continue;
        if (node.metadata.w == 0u) {{
            if (stack_size + 2u > 64u) continue;
            stack[stack_size++] = node.metadata.x;
            stack[stack_size++] = node.metadata.y;
            continue;
        }}
        for (uint triangle = 0u; triangle < node.metadata.w; ++triangle) {{
            uint first_index = node.metadata.z + triangle * 3u;
            if (first_index + 2u >= inx_collision_scene.topology.y) continue;
            uint ia = particle_collision_mesh_indices[first_index];
            uint ib = particle_collision_mesh_indices[first_index + 1u];
            uint ic = particle_collision_mesh_indices[first_index + 2u];
            if (ia >= inx_collision_scene.topology.x || ib >= inx_collision_scene.topology.x ||
                ic >= inx_collision_scene.topology.x) continue;
            vec3 a = particle_collision_mesh_vertices[ia].xyz;
            vec3 b = particle_collision_mesh_vertices[ib].xyz;
            vec3 c = particle_collision_mesh_vertices[ic].xyz;
            float candidate_time = 0.0;
            vec3 candidate_normal = vec3(0.0);
            if (inx_sweep_triangle(local_previous, local_position, local_radius, a, b, c,
                                   candidate_time, candidate_normal) && candidate_time < earliest) {{
                earliest = candidate_time;
                earliest_normal = candidate_normal;
                found_sweep = true;
            }}
            vec3 surface = inx_closest_point_triangle(local_position, a, b, c);
            vec3 delta = local_position - surface;
            float distance_squared = dot(delta, delta);
            if (distance_squared < closest_distance_squared) {{
                closest_distance_squared = distance_squared;
                closest_surface = surface;
                closest_normal = distance_squared > 1.0e-12
                                     ? delta / sqrt(distance_squared)
                                     : inx_safe_normalize(cross(b - a, c - a));
                found_overlap = true;
            }}
        }}
    }}
    vec3 local_normal = vec3(0.0);
    if (found_sweep) {{
        local_position = mix(local_previous, local_position, earliest);
        local_normal = earliest_normal;
    }} else if (found_overlap) {{
        local_normal = dot(closest_normal, closest_normal) > 0.0
                         ? closest_normal : vec3(0.0, 1.0, 0.0);
        local_position = closest_surface + local_normal * local_radius;
    }} else {{
        return false;
    }}
    corrected_world_position = inx_particle_affine_point(collider.collider_to_world, local_position);
    world_normal = inx_collision_world_normal(collider, local_normal);
    return true;
}}

bool inx_collision_box(InxParticleCollider collider, vec3 previous_world_position,
                       vec3 world_position, float world_radius,
                       out vec3 corrected_world_position, out vec3 world_normal) {{
    vec3 local_position = inx_particle_affine_point(collider.world_to_collider, world_position);
    vec3 local_previous =
        inx_particle_affine_point(collider.previous_world_to_collider, previous_world_position);
    float local_radius = inx_collision_local_radius(collider.world_to_collider, world_radius);
    vec3 half_extent = max(collider.shape.xyz * 0.5, vec3(0.0)) + vec3(local_radius);
    vec3 local_normal = vec3(0.0);
    float hit_time = 0.0;
    bool start_inside = all(lessThanEqual(abs(local_previous), half_extent));
    if (!start_inside &&
        inx_sweep_box(local_previous, local_position, half_extent, hit_time, local_normal)) {{
        local_position = mix(local_previous, local_position, hit_time);
    }} else if (all(lessThanEqual(abs(local_position), half_extent))) {{
        vec3 face_distance = half_extent - abs(local_position);
        uint axis = face_distance.x <= face_distance.y && face_distance.x <= face_distance.z
                        ? 0u : (face_distance.y <= face_distance.z ? 1u : 2u);
        local_normal[axis] = local_position[axis] < 0.0 ? -1.0 : 1.0;
        local_position[axis] = local_normal[axis] * half_extent[axis];
    }} else {{
        return false;
    }}
    corrected_world_position = inx_particle_affine_point(collider.collider_to_world, local_position);
    world_normal = inx_collision_world_normal(collider, local_normal);
    return true;
}}

bool inx_collision_sphere(InxParticleCollider collider, vec3 previous_world_position,
                          vec3 world_position, float world_radius,
                          out vec3 corrected_world_position, out vec3 world_normal) {{
    vec3 local_position = inx_particle_affine_point(collider.world_to_collider, world_position);
    vec3 local_previous =
        inx_particle_affine_point(collider.previous_world_to_collider, previous_world_position);
    float combined_radius = max(collider.shape.x, 0.0)
                          + inx_collision_local_radius(collider.world_to_collider, world_radius);
    float distance_value = length(local_position);
    vec3 local_normal = vec3(0.0);
    float hit_time = 0.0;
    bool start_inside = dot(local_previous, local_previous) <= combined_radius * combined_radius;
    if (!start_inside &&
        inx_sweep_sphere(local_previous, local_position, combined_radius,
                         hit_time, local_normal)) {{
        local_position = mix(local_previous, local_position, hit_time);
    }} else if (distance_value < combined_radius) {{
        local_normal = distance_value > 1.0e-6
                           ? local_position / distance_value
                           : inx_safe_normalize(local_previous - local_position);
        if (dot(local_normal, local_normal) == 0.0) local_normal = vec3(0.0, 1.0, 0.0);
        local_position = local_normal * combined_radius;
    }} else {{
        return false;
    }}
    corrected_world_position = inx_particle_affine_point(collider.collider_to_world, local_position);
    world_normal = inx_collision_world_normal(collider, local_normal);
    return true;
}}

bool inx_collision_capsule(InxParticleCollider collider, vec3 previous_world_position,
                           vec3 world_position, float world_radius,
                           out vec3 corrected_world_position, out vec3 world_normal) {{
    vec3 local_position = inx_particle_affine_point(collider.world_to_collider, world_position);
    vec3 local_previous =
        inx_particle_affine_point(collider.previous_world_to_collider, previous_world_position);
    uint axis = min(uint(max(collider.shape.z, 0.0)), 2u);
    float radius = max(collider.shape.x, 0.0)
                 + inx_collision_local_radius(collider.world_to_collider, world_radius);
    float half_segment = max(collider.shape.y * 0.5 - collider.shape.x, 0.0);
    vec3 closest = vec3(0.0);
    closest[axis] = clamp(local_position[axis], -half_segment, half_segment);
    vec3 delta = local_position - closest;
    float distance_value = length(delta);
    vec3 local_normal = vec3(0.0);
    vec3 previous_closest = vec3(0.0);
    previous_closest[axis] = clamp(local_previous[axis], -half_segment, half_segment);
    bool start_inside = dot(local_previous - previous_closest,
                            local_previous - previous_closest) <= radius * radius;
    float hit_time = 0.0;
    if (!start_inside &&
        inx_sweep_capsule(local_previous, local_position, axis, half_segment, radius,
                          hit_time, local_normal)) {{
        local_position = mix(local_previous, local_position, hit_time);
    }} else if (distance_value < radius) {{
        local_normal = distance_value > 1.0e-6 ? delta / distance_value : vec3(0.0, 1.0, 0.0);
        local_position = closest + local_normal * radius;
    }} else {{
        return false;
    }}
    corrected_world_position = inx_particle_affine_point(collider.collider_to_world, local_position);
    world_normal = inx_collision_world_normal(collider, local_normal);
    return true;
}}

bool inx_collide_scene(inout vec3 simulation_position, inout vec3 simulation_velocity,
                       float particle_radius, uint layer_mask, bool include_triggers,
                       float restitution_scale, float friction_scale,
                       out vec3 simulation_collision_normal) {{
    simulation_collision_normal = vec3(0.0);
    if (inx_collision_scene.collider_count == 0u || layer_mask == 0u) return false;

    vec3 world_position = (transforms.simulation_to_world * vec4(simulation_position, 1.0)).xyz;
    vec3 world_velocity = (transforms.simulation_to_world * vec4(simulation_velocity, 0.0)).xyz;
    bool any_hit = false;
    vec3 last_world_normal = vec3(0.0);
    float simulation_scale = max(length(transforms.simulation_to_world[0].xyz),
                                 max(length(transforms.simulation_to_world[1].xyz),
                                     length(transforms.simulation_to_world[2].xyz)));
    float world_radius = max(particle_radius, 0.0) * simulation_scale;
    vec3 previous_world_position = world_position - world_velocity * pc.delta_time;
    ivec3 query_min = inx_collision_grid_cell(min(previous_world_position, world_position) - vec3(world_radius));
    ivec3 query_max = inx_collision_grid_cell(max(previous_world_position, world_position) + vec3(world_radius));

    for (int z = query_min.z; z <= query_max.z; ++z) {{
        for (int y = query_min.y; y <= query_max.y; ++y) {{
            for (int x = query_min.x; x <= query_max.x; ++x) {{
                ivec3 cell = ivec3(x, y, z);
                uint cell_index = inx_collision_grid_index(cell);
                uint first = particle_collision_grid_offsets[cell_index];
                uint last = particle_collision_grid_offsets[cell_index + 1u];
                for (uint reference = first; reference < last; ++reference) {{
                    uint collider_index = particle_collision_grid_collider_indices[reference];
                    if (collider_index >= inx_collision_scene.collider_count) continue;
                    InxParticleCollider collider = particle_colliders[collider_index];
                    if ((collider.metadata.y & layer_mask) == 0u) continue;
                    if (!include_triggers && (collider.metadata.z & 1u) != 0u) continue;

                    vec3 collider_swept_min =
                        min(collider.previous_world_aabb_min.xyz, collider.world_aabb_min.xyz);
                    vec3 collider_swept_max =
                        max(collider.previous_world_aabb_max.xyz, collider.world_aabb_max.xyz);
                    ivec3 collider_min_cell = inx_collision_grid_cell(collider_swept_min);
                    if (any(notEqual(cell, max(query_min, collider_min_cell)))) continue;
                    vec3 particle_swept_min =
                        min(previous_world_position, world_position) - vec3(world_radius);
                    vec3 particle_swept_max =
                        max(previous_world_position, world_position) + vec3(world_radius);
                    if (any(lessThan(particle_swept_max, collider_swept_min)) ||
                        any(greaterThan(particle_swept_min, collider_swept_max)))
                        continue;

                    vec3 corrected_world_position = world_position;
                    vec3 world_normal = vec3(0.0, 1.0, 0.0);
                    bool hit = collider.metadata.x == 0u
                                   ? inx_collision_box(collider, previous_world_position,
                                                       world_position, world_radius,
                                                       corrected_world_position, world_normal)
                               : collider.metadata.x == 1u
                                   ? inx_collision_sphere(collider, previous_world_position,
                                                          world_position, world_radius,
                                                          corrected_world_position, world_normal)
                               : collider.metadata.x == 2u
                                   ? inx_collision_capsule(collider, previous_world_position,
                                                           world_position, world_radius,
                                                           corrected_world_position, world_normal)
                               : collider.metadata.x == 3u
                                   ? inx_collision_mesh(collider, previous_world_position,
                                                        world_position, world_radius,
                                                        corrected_world_position, world_normal)
                                   : false;
                    if (!hit) continue;

                    any_hit = true;
                    last_world_normal = world_normal;
                    world_position = corrected_world_position;
                    vec3 relative_velocity = world_velocity - collider.linear_velocity.xyz;
                    float normal_speed = dot(relative_velocity, world_normal);
                    if (normal_speed < 0.0) {{
                        vec3 tangent_velocity = relative_velocity - world_normal * normal_speed;
                        float friction = clamp(collider.material.x * max(friction_scale, 0.0), 0.0, 1.0);
                        float restitution = clamp(collider.material.y * max(restitution_scale, 0.0), 0.0, 1.0);
                        relative_velocity = tangent_velocity * (1.0 - friction)
                                          - world_normal * normal_speed * restitution;
                        world_velocity = relative_velocity + collider.linear_velocity.xyz;
                    }}
                }}
            }}
        }}
    }}
    simulation_position = (transforms.world_to_simulation * vec4(world_position, 1.0)).xyz;
    simulation_velocity = (transforms.world_to_simulation * vec4(world_velocity, 0.0)).xyz;
    if (any_hit) {{
        vec3 transformed_normal =
            transpose(mat3(transforms.simulation_to_world)) * last_world_normal;
        float normal_length = length(transformed_normal);
        simulation_collision_normal = normal_length > 1.0e-6
                                          ? transformed_normal / normal_length
                                          : vec3(0.0, 1.0, 0.0);
    }}
    return any_hit;
}}

vec3 inx_shape_random(uvec3 slots, uint particle_id, uint generation) {{
    return vec3(inx_random01(0u, slots.x, particle_id, generation),
                inx_random01(0u, slots.y, particle_id, generation),
                inx_random01(0u, slots.z, particle_id, generation));
}}

vec3 inx_shape_direction(uint kind, float angle_degrees, uvec3 slots, uint particle_id, uint generation) {{
    if (kind == 0u) return vec3(0.0, 0.0, 1.0);
    vec3 random_value = inx_shape_random(slots, particle_id, generation);
    float cosine_limit = kind == 3u ? cos(radians(angle_degrees)) : -1.0;
    float z = mix(cosine_limit, 1.0, random_value.x);
    float phi = random_value.y * 6.283185307179586;
    float radial = sqrt(max(0.0, 1.0 - z * z));
    return vec3(cos(phi) * radial, sin(phi) * radial, z);
}}

vec3 inx_sample_shape_direction(uint kind, float radius, float angle_degrees, vec3 dimensions, uvec3 slots, uint particle_id, uint generation) {{
    return inx_shape_direction(kind, angle_degrees, slots, particle_id, generation);
}}

vec3 inx_sample_shape_position(uint kind, float radius, float angle_degrees, vec3 dimensions, uvec3 slots, uint particle_id, uint generation) {{
    vec3 random_value = inx_shape_random(slots, particle_id, generation);
    if (kind == 0u) return vec3(0.0);
    if (kind == 2u) return (random_value - vec3(0.5)) * dimensions;
    if (kind == 3u) {{
        float radial = sqrt(random_value.x) * radius;
        float phi = random_value.y * 6.283185307179586;
        return vec3(cos(phi) * radial, sin(phi) * radial, 0.0);
    }}
    return inx_shape_direction(kind, angle_degrees, slots, particle_id, generation) * (pow(random_value.z, 1.0 / 3.0) * radius);
}}

bool inx_finite(float value) {{ return !isnan(value) && !isinf(value); }}
bool inx_finite(vec2 value) {{ return !any(isnan(value)) && !any(isinf(value)); }}
bool inx_finite(vec3 value) {{ return !any(isnan(value)) && !any(isinf(value)); }}
bool inx_finite(vec4 value) {{ return !any(isnan(value)) && !any(isinf(value)); }}
"""


def _bootstrap_main() -> str:
    return """
void main() {
    uint index = gl_GlobalInvocationID.x;
    if (index >= pc.capacity) return;
    states[index].alive = 0u;
    states[index].spawn_generation = 0u;
    free_slots[index] = index;
    if (index == 0u) {
        counters.free_count = pc.capacity;
        counters.visible_count = 0u;
        counters.dropped_count = 0u;
        counters.reserved_count = 0u;
        indirect_args.vertex_count = 6u;
        indirect_args.instance_count = 0u;
        indirect_args.first_vertex = 0u;
        indirect_args.first_instance = 0u;
    }
}
"""


def _init_main(
    body: str,
    event_body: str,
    emitter: ParticleEmitterKernelIR,
    fields: tuple[tuple[str, TypeRef, str], ...],
) -> str:
    id_field = next(field for stable, _type, field in fields if stable == "builtin.id")
    finite = _finite_state_check(emitter.init, fields)
    return f"""
void main() {{
    if (simulation_control.simulation_allowed == 0u) return;
    uint invocation = gl_GlobalInvocationID.x;
    if (invocation >= pc.invocation_count) return;
    uint particle_index = inx_pop_free();
    if (particle_index == INX_INVALID_INDEX) {{ atomicAdd(counters.dropped_count, 1u); return; }}
    ParticleState state = states[particle_index];
    state.alive = 1u;
    uint particle_id = pc.spawn_base_id + invocation;
    state.{id_field} = particle_id;
    state.spawn_generation = pc.spawn_generation + uint(particle_id < pc.spawn_base_id);
    bool particle_alive = true;
{body}
    particle_alive = particle_alive && ({finite});
    state.alive = particle_alive ? 1u : 0u;
{event_body}
    states[particle_index] = state;
    if (!particle_alive) inx_push_free(particle_index);
}}
"""


def _event_init_bindings() -> str:
    return """
struct ParticleEventChannel {
    uint record_base_words;
    uint record_stride_words;
    uint capacity;
    uint source_emitter_index;
    uint target_emitter_index;
    uint event_type_index;
    uint payload_stride_words;
    uint spawn_count;
    uint spawn_base_indices;
    uint target_capacity;
    uint reserved0;
    uint reserved1;
};
layout(std430, set = 3, binding = 0) readonly buffer ParticleEventChannels {
    ParticleEventChannel event_channels[];
};
layout(std430, set = 3, binding = 1) readonly buffer ParticleEventRecords {
    uint event_record_words[];
};
layout(std430, set = 3, binding = 2) readonly buffer ParticleEventCounters {
    uvec4 event_counters[];
};
layout(std430, set = 3, binding = 3) readonly buffer ParticleEventSpawnIndices {
    uint event_spawn_indices[];
};
"""


def _event_output_bindings(set_index: int) -> str:
    return f"""
struct ParticleEventOutputChannel {{
    uint record_base_words;
    uint record_stride_words;
    uint capacity;
    uint source_emitter_index;
    uint target_emitter_index;
    uint event_type_index;
    uint payload_stride_words;
    uint spawn_count;
    uint spawn_base_indices;
    uint target_capacity;
    uint reserved0;
    uint reserved1;
}};
layout(std430, set = {set_index}, binding = 0) readonly buffer ParticleEventOutputChannels {{
    ParticleEventOutputChannel event_output_channels[];
}};
layout(std430, set = {set_index}, binding = 1) buffer ParticleEventOutputRecords {{
    uint event_output_record_words[];
}};
layout(std430, set = {set_index}, binding = 2) buffer ParticleEventOutputCounters {{
    uvec4 event_output_counters[];
}};
"""


def _event_init_main(
    body: str,
    event_body: str,
    emitter: ParticleEmitterKernelIR,
    fields: tuple[tuple[str, TypeRef, str], ...],
) -> str:
    id_field = next(field for stable, _type, field in fields if stable == "builtin.id")
    finite = _finite_state_check(emitter.init, fields)
    return f"""
void main() {{
    uint invocation = gl_GlobalInvocationID.x;
    uint channel_index = pc.spawn_base_id;
    ParticleEventChannel channel = event_channels[channel_index];
    uint accepted_count = event_counters[channel_index].z;
    uint total_spawn_count = accepted_count * channel.spawn_count;
    if (invocation >= total_spawn_count) return;
    uint particle_index = event_spawn_indices[channel.spawn_base_indices + invocation];
    if (particle_index == INX_INVALID_INDEX || particle_index >= pc.capacity) return;
    uint event_index = invocation / channel.spawn_count;
    uint record_base = channel.record_base_words + event_index * channel.record_stride_words;
    uint source_particle_id = event_record_words[record_base + 2u];
    uint source_generation = event_record_words[record_base + 3u];
    uint route_seed = inx_random_u32(channel_index, channel.source_emitter_index,
                                     channel.target_emitter_index, channel.event_type_index);
    uint particle_id = inx_random_u32(route_seed, source_particle_id, source_generation, invocation);
    ParticleState state = states[particle_index];
    state.alive = 1u;
    state.{id_field} = particle_id;
    state.spawn_generation = inx_random_u32(route_seed ^ 0x9e3779b9u, source_generation,
                                            source_particle_id, invocation);
    bool particle_alive = true;
{body}
    particle_alive = particle_alive && ({finite});
    state.alive = particle_alive ? 1u : 0u;
{event_body}
    states[particle_index] = state;
    if (!particle_alive) inx_push_free(particle_index);
}}
"""


def _update_main(
    body: str,
    event_body: str,
    emitter: ParticleEmitterKernelIR,
    fields: tuple[tuple[str, TypeRef, str], ...],
) -> str:
    finite = _finite_state_check(emitter.update, fields)
    return f"""
void main() {{
    if (simulation_control.simulation_allowed == 0u) return;
    uint particle_index = gl_GlobalInvocationID.x;
    if (particle_index >= pc.capacity || states[particle_index].alive == 0u) return;
    ParticleState state = states[particle_index];
    bool particle_alive = true;
{body}
    particle_alive = particle_alive && ({finite});
    state.alive = particle_alive ? 1u : 0u;
{event_body}
    states[particle_index] = state;
    if (!particle_alive) inx_push_free(particle_index);
}}
"""


def _render_reset_main() -> str:
    return """
void main() {
    if (simulation_control.simulation_allowed == 0u) return;
    if (gl_GlobalInvocationID.x != 0u) return;
    counters.visible_count = 0u;
    indirect_args.vertex_count = 6u;
    indirect_args.instance_count = 0u;
    indirect_args.first_vertex = 0u;
    indirect_args.first_instance = 0u;
}
"""


def _rendering_main(body: str, event_body: str, exports: dict[str, str]) -> str:
    position = exports["builtin.position"]
    size = exports["builtin.size"]
    color = exports["builtin.color"]
    rotation = exports["builtin.rotation"]
    age = exports["builtin.age"]
    lifetime = exports["builtin.lifetime"]
    orientation = exports.get("builtin.orientation", "vec3(0.0)")
    scale = exports.get("builtin.scale", "vec3(1.0)")
    particle_id = exports["builtin.id"]
    ribbon_strip_id = exports.get("builtin.ribbon_strip_id", "0u")
    ribbon_order = exports.get("builtin.ribbon_order", particle_id)
    ribbon_break = exports.get("builtin.ribbon_break", "false")
    flipbook_frame = exports.get("builtin.flipbook_frame", "0.0")
    velocity = exports["builtin.velocity"]
    world_position = f"(transforms.simulation_to_world * vec4({position}, 1.0)).xyz"
    world_velocity = f"(transforms.simulation_to_world * vec4({velocity}, 0.0)).xyz"
    world_scale = (
        "vec3(length(transforms.simulation_to_world[0].xyz), "
        "length(transforms.simulation_to_world[1].xyz), "
        "length(transforms.simulation_to_world[2].xyz))"
    )
    finite = " && ".join(
        (_finite_expression(position, TypeRef(ValueType.VEC3)),
         _finite_expression(size, TypeRef(ValueType.F32)),
         _finite_expression(color, TypeRef(ValueType.COLOR)),
         _finite_expression(rotation, TypeRef(ValueType.F32)),
         _finite_expression(age, TypeRef(ValueType.F32)),
         _finite_expression(lifetime, TypeRef(ValueType.F32)),
         _finite_expression(orientation, TypeRef(ValueType.VEC3)),
         _finite_expression(scale, TypeRef(ValueType.VEC3)),
         _finite_expression(flipbook_frame, TypeRef(ValueType.F32)))
    )
    return f"""
void main() {{
    if (simulation_control.simulation_allowed == 0u) return;
    uint particle_index = gl_GlobalInvocationID.x;
    if (particle_index >= pc.capacity || states[particle_index].alive == 0u) return;
    ParticleState state = states[particle_index];
    bool particle_alive = true;
{body}
    if (!({finite})) {{
        state.alive = 0u;
        states[particle_index] = state;
        inx_push_free(particle_index);
        return;
    }}
{event_body}
    uint output_index = atomicAdd(counters.visible_count, 1u);
    if (output_index >= pc.capacity) return;
    ParticleRenderInstance previous_instance = instances[particle_index];
    bool history_valid = previous_instance.ribbon_data.w == {particle_id} &&
        floatBitsToUint(previous_instance.previous_position_history.w) == state.spawn_generation;
    instances[particle_index].position_size = vec4({world_position}, {size});
    instances[particle_index].color = {color};
    instances[particle_index].rotation_custom = vec4({rotation}, {orientation});
    float normalized_age = clamp(({age}) / max(({lifetime}), 0.000001), 0.0, 1.0);
    instances[particle_index].scale_custom = vec4(({scale}) * {world_scale}, normalized_age);
    instances[particle_index].ribbon_data = uvec4(
        {ribbon_strip_id}, {ribbon_order}, ({ribbon_break}) ? 1u : 0u, {particle_id});
    instances[particle_index].custom_data = vec4({flipbook_frame}, {world_velocity});
    instances[particle_index].previous_position_history = vec4(
        history_valid ? previous_instance.position_size.xyz : {world_position},
        uintBitsToFloat(state.spawn_generation));
    render_indices[output_index] = particle_index;
    atomicAdd(indirect_args.instance_count, 1u);
}}
"""


def _finite_state_check(
    function: ParticleKernelFunction,
    fields: tuple[tuple[str, TypeRef, str], ...],
) -> str:
    schema = {stable: (value_type, field) for stable, value_type, field in fields}
    checks = []
    for stable_id in function.written_attributes:
        value_type, field = schema[stable_id]
        checks.append(_finite_expression(f"state.{field}", value_type))
    return " && ".join(checks) or "true"


def _finite_expression(expression: str, value_type: TypeRef) -> str:
    kind = value_type.value_type
    if kind in {ValueType.BOOL, ValueType.I32, ValueType.U32}:
        return "true"
    if kind in {ValueType.F32, ValueType.VEC2, ValueType.VEC3, ValueType.VEC4, ValueType.COLOR}:
        return f"inx_finite({expression})"
    if kind is ValueType.MAT3:
        return " && ".join(f"inx_finite(({expression})[{index}])" for index in range(3))
    if kind is ValueType.MAT4:
        return " && ".join(f"inx_finite(({expression})[{index}])" for index in range(4))
    raise GpuParticleCompileError(f"GPU finite check does not support {kind.value}")


def _space_conversion(expression: str, result_type: TypeRef | None, immediate: dict[str, Any]) -> str:
    if result_type is None or result_type.value_type is not ValueType.VEC3:
        raise GpuParticleCompileError("GPU space conversion currently requires vec3")
    source = CoordinateSpace(immediate["from"])
    target = CoordinateSpace(immediate["to"])
    if source is CoordinateSpace.NONE or target is CoordinateSpace.NONE:
        return expression
    supported = {CoordinateSpace.EMITTER_LOCAL, CoordinateSpace.SIMULATION, CoordinateSpace.WORLD}
    if source not in supported or target not in supported:
        raise GpuParticleCompileError(
            f"GPU space conversion {source.value} -> {target.value} is not portable yet"
        )
    w = "1.0" if immediate["semantic"] == "position" else "0.0"
    world = expression
    if source is CoordinateSpace.EMITTER_LOCAL:
        world = f"(transforms.emitter_to_world * vec4({expression}, {w})).xyz"
    elif source is CoordinateSpace.SIMULATION:
        world = f"(transforms.simulation_to_world * vec4({expression}, {w})).xyz"
    if target is CoordinateSpace.EMITTER_LOCAL:
        return f"(transforms.world_to_emitter * vec4({world}, {w})).xyz"
    if target is CoordinateSpace.SIMULATION:
        return f"(transforms.world_to_simulation * vec4({world}, {w})).xyz"
    return world


def _glsl_type(value_type: TypeRef | None) -> str:
    if value_type is None:
        raise GpuParticleCompileError("GPU value is missing its type")
    try:
        return {
            ValueType.BOOL: "bool",
            ValueType.I32: "int",
            ValueType.U32: "uint",
            ValueType.F32: "float",
            ValueType.VEC2: "vec2",
            ValueType.VEC3: "vec3",
            ValueType.VEC4: "vec4",
            ValueType.COLOR: "vec4",
            ValueType.MAT3: "mat3",
            ValueType.MAT4: "mat4",
        }[value_type.value_type]
    except KeyError as exc:
        raise GpuParticleCompileError(
            f"GPU backend does not support {value_type.value_type.value} values"
        ) from exc


def _numeric_resize_glsl(
    expression: str,
    source_type: TypeRef,
    target_type: TypeRef | None,
) -> str:
    if target_type is None:
        raise GpuParticleCompileError("numeric resize is missing its result type")
    dimensions = {
        ValueType.I32: 1,
        ValueType.U32: 1,
        ValueType.F32: 1,
        ValueType.VEC2: 2,
        ValueType.VEC3: 3,
        ValueType.VEC4: 4,
        ValueType.COLOR: 4,
    }
    try:
        source_dimension = dimensions[source_type.value_type]
        target_dimension = dimensions[target_type.value_type]
    except KeyError as exc:
        raise GpuParticleCompileError(
            "numeric resize only supports scalar and vector values"
        ) from exc

    if target_dimension == 1:
        value = expression if source_dimension == 1 else f"({expression}).x"
        if target_type.value_type is ValueType.F32 and source_type.value_type in {
            ValueType.I32,
            ValueType.U32,
        }:
            return f"float({value})"
        return value

    target_glsl = _glsl_type(target_type)
    if source_dimension == 1:
        scalar = expression
        if source_type.value_type in {ValueType.I32, ValueType.U32}:
            scalar = f"float({scalar})"
        return f"{target_glsl}({scalar})"
    if source_dimension == target_dimension:
        return expression
    if source_dimension > target_dimension:
        return f"({expression}).{'xyzw'[:target_dimension]}"
    zero_tail = ", ".join("0.0" for _ in range(target_dimension - source_dimension))
    return f"{target_glsl}({expression}, {zero_tail})"


def _parameter_load_glsl(slot: int, value_type: TypeRef | None) -> str:
    if value_type is None:
        raise GpuParticleCompileError("parameter load is missing its result type")
    words = f"parameter_words[{int(slot)}]"
    kind = value_type.value_type
    if kind is ValueType.BOOL:
        return f"({words}.x != 0u)"
    if kind is ValueType.I32:
        return f"int({words}.x)"
    if kind is ValueType.U32:
        return f"{words}.x"
    if kind is ValueType.F32:
        return f"uintBitsToFloat({words}.x)"
    swizzles = {
        ValueType.VEC2: "xy",
        ValueType.VEC3: "xyz",
        ValueType.VEC4: "xyzw",
        ValueType.COLOR: "xyzw",
    }
    swizzle = swizzles.get(kind)
    if swizzle is None:
        raise GpuParticleCompileError(
            f"GPU parameters do not support {kind.value!r}"
        )
    return f"uintBitsToFloat({words}.{swizzle})"


def pack_gpu_particle_parameters(
    parameters: tuple[KernelParameter, ...],
    overrides: Mapping[str, Any] | None = None,
) -> tuple[int, ...]:
    """Pack one deterministic 16-byte slot per graph parameter."""
    overrides = dict(overrides or {})
    known = {parameter.stable_id for parameter in parameters}
    unknown = set(overrides) - known
    if unknown:
        raise GpuParticleCompileError(
            f"particle parameter overrides reference unknown ids: {sorted(unknown)}"
        )
    words: list[int] = []
    for parameter in parameters:
        value = overrides.get(parameter.stable_id, parameter.default)
        kind = parameter.value_type.value_type
        if kind is ValueType.TEXTURE2D:
            try:
                AssetReference.from_dict(value)
            except (TypeError, ValueError) as exc:
                raise GpuParticleCompileError(
                    f"particle parameter {parameter.name!r} requires a Texture2D asset reference"
                ) from exc
            encoded = []
        elif kind is ValueType.BOOL:
            if type(value) is not bool:
                raise GpuParticleCompileError(
                    f"particle parameter {parameter.name!r} requires a bool"
                )
            encoded = [1 if value else 0]
        elif kind in {ValueType.I32, ValueType.U32}:
            if type(value) is not int:
                raise GpuParticleCompileError(
                    f"particle parameter {parameter.name!r} requires an integer"
                )
            encoded = [int(value) & 0xFFFFFFFF]
        else:
            dimension = {
                ValueType.F32: 1,
                ValueType.VEC2: 2,
                ValueType.VEC3: 3,
                ValueType.VEC4: 4,
                ValueType.COLOR: 4,
            }.get(kind)
            values = [value] if dimension == 1 else value
            if (
                dimension is None
                or not isinstance(values, (list, tuple))
                or len(values) != dimension
                or any(type(item) not in {int, float} for item in values)
            ):
                raise GpuParticleCompileError(
                    f"particle parameter {parameter.name!r} has an invalid value"
                )
            encoded = [
                struct.unpack("<I", struct.pack("<f", float(item)))[0]
                for item in values
            ]
        words.extend(encoded)
        words.extend([0] * (4 - len(encoded)))
    return tuple(words)


def pack_gpu_particle_event_payload(
    event_type,
    values: Mapping[str, Any] | None = None,
) -> tuple[int, ...]:
    """Pack one external event payload using the compiled GPU event ABI."""
    values = dict(values or {})
    fields = tuple(getattr(event_type, "fields", ()))
    by_key: dict[str, Any] = {}
    ambiguous: set[str] = set()
    for field in fields:
        for key in (field.stable_id, getattr(field, "name", "")):
            if not key:
                continue
            if key in by_key and by_key[key] is not field:
                ambiguous.add(key)
            else:
                by_key[key] = field
    unknown = set(values) - set(by_key)
    if unknown:
        raise GpuParticleCompileError(
            f"particle event payload references unknown fields: {sorted(unknown)}"
        )
    if ambiguous.intersection(values):
        raise GpuParticleCompileError(
            "particle event payload field names are ambiguous; use stable field ids"
        )

    supplied: dict[str, Any] = {}
    for key, value in values.items():
        stable_id = by_key[key].stable_id
        if stable_id in supplied:
            raise GpuParticleCompileError(
                f"particle event field {stable_id!r} was supplied more than once"
            )
        supplied[stable_id] = value

    words: list[int] = []
    for field in fields:
        value = supplied.get(field.stable_id, field.default)
        encoded = _pack_gpu_particle_event_value(field.value_type, value)
        if len(encoded) != field.word_count or len(words) != field.word_offset:
            raise GpuParticleCompileError(
                f"particle event field {field.stable_id!r} does not match its compiled ABI"
            )
        words.extend(encoded)
    if len(words) != int(getattr(event_type, "payload_stride_words", -1)):
        raise GpuParticleCompileError("particle event payload does not match its compiled stride")
    return tuple(words)


def _pack_gpu_particle_event_value(value_type: TypeRef, value: Any) -> list[int]:
    kind = value_type.value_type
    if kind is ValueType.BOOL:
        if type(value) is not bool:
            raise GpuParticleCompileError("particle event bool field requires a bool")
        return [1 if value else 0]
    if kind in {ValueType.I32, ValueType.U32}:
        if type(value) is not int:
            raise GpuParticleCompileError("particle event integer field requires an integer")
        if kind is ValueType.U32 and not 0 <= value <= 0xFFFFFFFF:
            raise GpuParticleCompileError("particle event uint field is out of range")
        if kind is ValueType.I32 and not -(1 << 31) <= value < (1 << 31):
            raise GpuParticleCompileError("particle event int field is out of range")
        return [value & 0xFFFFFFFF]

    dimensions = {
        ValueType.F32: 1,
        ValueType.VEC2: 2,
        ValueType.VEC3: 3,
        ValueType.VEC4: 4,
        ValueType.COLOR: 4,
        ValueType.MAT3: 9,
        ValueType.MAT4: 16,
    }
    dimension = dimensions.get(kind)
    source = [value] if dimension == 1 else value
    if (
        dimension is None
        or not isinstance(source, (list, tuple))
        or len(source) != dimension
        or any(type(item) not in {int, float} or not math.isfinite(float(item)) for item in source)
    ):
        raise GpuParticleCompileError(
            f"particle event {kind.value} field has an invalid value"
        )
    encoded = [struct.unpack("<I", struct.pack("<f", float(item)))[0] for item in source]
    if kind is ValueType.MAT3:
        encoded = [
            word
            for column in range(3)
            for word in (*encoded[column * 3 : column * 3 + 3], 0)
        ]
    return encoded


def _storage_type(value_type: TypeRef) -> str:
    return "uint" if value_type.value_type is ValueType.BOOL else _glsl_type(value_type)


def _value_name(value_id: str) -> str:
    if not value_id.startswith("%") or not value_id[1:].isdigit():
        raise GpuParticleCompileError(f"invalid SSA value id {value_id!r}")
    return "v" + value_id[1:]


def _glsl_literal(value: Any, value_type: TypeRef | None) -> str:
    if value_type is None:
        raise GpuParticleCompileError("constant is missing its type")
    kind = value_type.value_type
    if kind is ValueType.BOOL:
        return "true" if value else "false"
    if kind is ValueType.I32:
        return str(int(value))
    if kind is ValueType.U32:
        return f"{int(value)}u"
    if kind is ValueType.F32:
        return _float_literal(value)
    component_count = {
        ValueType.VEC2: 2,
        ValueType.VEC3: 3,
        ValueType.VEC4: 4,
        ValueType.COLOR: 4,
        ValueType.MAT3: 9,
        ValueType.MAT4: 16,
    }.get(kind)
    if component_count is None:
        raise GpuParticleCompileError(f"GPU literal does not support {kind.value}")
    return f"{_glsl_type(value_type)}(" + ", ".join(_float_literal(item) for item in value) + ")"


def _event_payload_word_expressions(expression: str, value_type: TypeRef) -> list[str]:
    kind = value_type.value_type
    if kind is ValueType.BOOL:
        return [f"(({expression}) ? 1u : 0u)"]
    if kind is ValueType.I32:
        return [f"uint({expression})"]
    if kind is ValueType.U32:
        return [expression]
    if kind is ValueType.F32:
        return [f"floatBitsToUint({expression})"]
    component_count = {
        ValueType.VEC2: 2,
        ValueType.VEC3: 3,
        ValueType.VEC4: 4,
        ValueType.COLOR: 4,
    }.get(kind)
    if component_count is not None:
        components = "xyzw"
        return [
            f"floatBitsToUint(({expression}).{components[index]})"
            for index in range(component_count)
        ]
    if kind is ValueType.MAT3:
        words = []
        for column in range(3):
            words.extend(
                f"floatBitsToUint(({expression})[{column}][{row}])"
                for row in range(3)
            )
            words.append("0u")
        return words
    if kind is ValueType.MAT4:
        return [
            f"floatBitsToUint(({expression})[{column}][{row}])"
            for column in range(4)
            for row in range(4)
        ]
    raise GpuParticleCompileError(
        f"GPU event payload does not support {kind.value}"
    )


def _event_payload_glsl_expression(
    words: tuple[str, ...], value_type: TypeRef
) -> str:
    kind = value_type.value_type
    if kind is ValueType.BOOL:
        return f"({words[0]} != 0u)"
    if kind is ValueType.I32:
        return f"int({words[0]})"
    if kind is ValueType.U32:
        return words[0]
    if kind is ValueType.F32:
        return f"uintBitsToFloat({words[0]})"
    component_count = {
        ValueType.VEC2: 2,
        ValueType.VEC3: 3,
        ValueType.VEC4: 4,
        ValueType.COLOR: 4,
    }.get(kind)
    if component_count is not None:
        values = ", ".join(
            f"uintBitsToFloat({word})" for word in words[:component_count]
        )
        return f"{_glsl_type(value_type)}({values})"
    if kind is ValueType.MAT3:
        columns = []
        for offset in (0, 4, 8):
            values = ", ".join(
                f"uintBitsToFloat({word})" for word in words[offset : offset + 3]
            )
            columns.append(f"vec3({values})")
        return f"mat3({', '.join(columns)})"
    if kind is ValueType.MAT4:
        values = ", ".join(f"uintBitsToFloat({word})" for word in words)
        return f"mat4({values})"
    raise GpuParticleCompileError(
        f"GPU event payload does not support {kind.value}"
    )


def _float_literal(value: Any) -> str:
    result = format(float(value), ".9g")
    if "." not in result and "e" not in result.lower():
        result += ".0"
    return result


def _vector_literal(values: Any, count: int) -> str:
    return f"vec{count}(" + ", ".join(_float_literal(item) for item in values) + ")"


def _shape_kind(value: str) -> int:
    try:
        return {"point": 0, "sphere": 1, "box": 2, "cone": 3, "mesh": 4}[value]
    except KeyError as exc:
        raise GpuParticleCompileError(f"unsupported particle shape {value!r}") from exc


__all__ = [
    "GpuParticleCompileError",
    "GpuParticleContinuationSource",
    "GpuParticleEmitterSource",
    "GpuParticleGlslLowerer",
    "GpuParticleProgramSource",
    "build_gpu_particle_migration",
    "compile_gpu_particle_spirv",
    "decode_gpu_particle_spirv",
    "pack_gpu_particle_event_payload",
    "pack_gpu_particle_parameters",
    "validate_gpu_particle_spirv",
]
