"""AOT lowering from portable particle Kernel IR to Vulkan GLSL compute sources."""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
import hashlib
import re
import struct
from typing import Any
import zlib

from Infernux.graph.types import CoordinateSpace, TypeRef, ValueType
from Infernux.graph.ramp import Curve, Gradient

from .data_interface import PointCache, VectorField
from .kernel_ir import (
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
};

layout(set = 0, binding = 0, std430) readonly buffer Instances {
    ParticleInstance instances[];
};
layout(set = 0, binding = 1, std430) readonly buffer RenderIndices {
    uint render_indices[];
};

layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
} view;

layout(location = 0) out vec4 out_color;
layout(location = 1) out vec2 out_uv;
layout(location = 2) out vec3 out_world_position;
layout(location = 3) out vec3 out_world_normal;
layout(location = 4) out float out_view_depth;

const vec2 corners[6] = vec2[](
    vec2(-1.0, -1.0), vec2(-1.0, 1.0), vec2(1.0, 1.0),
    vec2(-1.0, -1.0), vec2(1.0, 1.0), vec2(1.0, -1.0)
);

const vec2 uvs[6] = vec2[](
    vec2(0.0, 1.0), vec2(0.0, 0.0), vec2(1.0, 0.0),
    vec2(0.0, 1.0), vec2(1.0, 0.0), vec2(1.0, 1.0)
);

void main() {
    uint particle_index = view.lighting_control.y > 0.5
        ? render_indices[gl_InstanceIndex]
        : gl_InstanceIndex;
    ParticleInstance instance = instances[particle_index];
    vec2 corner = corners[gl_VertexIndex % 6];
    float cosine = cos(instance.rotation_custom.x);
    float sine = sin(instance.rotation_custom.x);
    corner = mat2(cosine, -sine, sine, cosine) * corner;
    vec3 world_position = instance.position_size.xyz +
        (view.camera_right.xyz * corner.x + view.camera_up.xyz * corner.y) * instance.position_size.w;
    gl_Position = view.view_projection * vec4(world_position, 1.0);
    out_color = instance.color;
    out_uv = uvs[gl_VertexIndex % 6];
    out_world_position = world_position;
    out_world_normal = normalize(cross(view.camera_right.xyz, view.camera_up.xyz));
    out_view_depth = gl_Position.w;
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
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
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

struct ParticleDirectionalLightData {
    vec4 direction;
    vec4 color;
    vec4 shadow_params;
};
struct ParticlePointLightData {
    vec4 position;
    vec4 color;
    vec4 attenuation;
};
struct ParticleSpotLightData {
    vec4 position;
    vec4 direction;
    vec4 color;
    vec4 spot_params;
    vec4 attenuation;
};
struct CanonicalLightData {
    vec4 position_range;
    vec4 direction_spot;
    vec4 color_intensity;
    vec4 attenuation;
    uvec4 metadata;
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
    mat4 light_vp[4];
    vec4 shadow_cascade_splits;
    vec4 shadow_map_params;
} particle_lighting;

vec2 inx_particle_shadow_tile(int cascade_index) {
    return vec2(float(cascade_index & 1), float(cascade_index >> 1)) * 0.5;
}

float inx_particle_shadow(vec3 world_position, vec3 normal, float view_depth) {
    if (view.rendering_control.x < 0.5 || particle_lighting.shadow_map_params.y < 0.5 ||
        particle_lighting.light_counts.x <= 0) return 1.0;
    int cascade_count = clamp(int(particle_lighting.shadow_map_params.z), 1, 4);
    int cascade_index = 0;
    if (cascade_count > 1 && view_depth > particle_lighting.shadow_cascade_splits.x) cascade_index = 1;
    if (cascade_count > 2 && view_depth > particle_lighting.shadow_cascade_splits.y) cascade_index = 2;
    if (cascade_count > 3 && view_depth > particle_lighting.shadow_cascade_splits.z) cascade_index = 3;

    vec4 shadow_params = particle_lighting.directional_lights[0].shadow_params;
    if (shadow_params.w < 0.5 || shadow_params.x <= 0.0) return 1.0;
    vec3 biased_position = world_position + normal * shadow_params.z;
    vec4 clip = particle_lighting.light_vp[cascade_index] * vec4(biased_position, 1.0);
    vec3 ndc = clip.xyz / max(clip.w, 0.00001);
    vec2 tile_min = inx_particle_shadow_tile(cascade_index);
    vec2 atlas_uv = tile_min + (ndc.xy * 0.5 + 0.5) * 0.5;
    float current_depth = ndc.z - shadow_params.y;
    if (current_depth <= 0.0 || current_depth >= 1.0) return 1.0;

    float visibility = 0.0;
    float texel = 1.0 / max(particle_lighting.shadow_map_params.x, 1.0);
    int radius = shadow_params.w > 1.5 ? 1 : 0;
    for (int y = -radius; y <= radius; ++y) {
        for (int x = -radius; x <= radius; ++x) {
            vec2 sample_uv = clamp(atlas_uv + vec2(x, y) * texel, tile_min + vec2(texel),
                                   tile_min + vec2(0.5 - texel));
            visibility += current_depth <= texture(particle_shadow_map, sample_uv).r ? 1.0 : 0.0;
        }
    }
    float sample_count = float((radius * 2 + 1) * (radius * 2 + 1));
    return mix(1.0, visibility / sample_count, shadow_params.x);
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
        float shadow = index == 0u ? inx_particle_shadow(world_position, normal, view_depth) : 1.0;
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
            if ((light.metadata.y & object_layer_mask) == 0u) continue;
            vec3 light_vector = light.position_range.xyz - world_position;
            float distance_to_light = length(light_vector);
            float range = max(light.position_range.w, 0.0001);
            if (distance_to_light <= 0.00001 || distance_to_light >= range) continue;
            vec3 direction = light_vector / distance_to_light;
            float normalized_distance = distance_to_light / range;
            float range_window = max(1.0 - pow(normalized_distance, 4.0), 0.0);
            float falloff = (range_window * range_window) / max(distance_to_light * distance_to_light, 0.01);
            if (light.metadata.x == 2u) {
                float cone = dot(direction, -normalize(light.direction_spot.xyz));
                falloff *= smoothstep(light.direction_spot.w, light.attenuation.w, cone);
            }
            float ndotl = dot(normal, direction);
            ndotl = two_sided ? abs(ndotl) : max(ndotl, 0.0);
            result += albedo * light.color_intensity.rgb * light.color_intensity.w * falloff * ndotl;
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
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
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
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
} view;

layout(location = 0) out vec4 out_color;
layout(location = 1) out vec3 out_normal;
layout(location = 2) out vec2 out_uv;
layout(location = 3) out vec3 out_world_position;
layout(location = 4) out float out_view_depth;

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
    vec3 world_position = instance.position_size.xyz +
        rotation * vertex.position.xyz * instance.position_size.w;
    gl_Position = view.view_projection * vec4(world_position, 1.0);
    out_color = instance.color * vertex.color;
    out_normal = normalize(rotation * vertex.normal.xyz);
    out_uv = vertex.uv.xy;
    out_world_position = world_position;
    out_view_depth = gl_Position.w;
}
"""

_MESH_FRAGMENT_GLSL = """#version 450

layout(location = 0) in vec4 in_color;
layout(location = 1) in vec3 in_normal;
layout(location = 2) in vec2 in_uv;
layout(location = 0) out vec4 out_color;

layout(push_constant) uniform ViewConstants {
    mat4 view_projection;
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
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
    vec4 camera_right;
    vec4 camera_up;
    vec4 material_tint;
    vec4 depth_reconstruct;
    vec4 lighting_control;
    vec4 rendering_control;
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
class GpuParticleEmitterSource:
    stable_id: str
    kernel_hash: str
    attribute_fields: tuple[tuple[str, str, str, int, int], ...]
    state_stride: int
    bootstrap: str
    init: str
    update: str
    render_reset: str
    rendering: str
    data_interfaces: tuple[dict[str, Any], ...] = ()
    data_interface_layout: dict[str, Any] = field(default_factory=dict)

    def stages(self) -> dict[str, str]:
        return {
            "bootstrap": self.bootstrap,
            "init": self.init,
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
            "data_interfaces": [dict(value) for value in self.data_interfaces],
            "data_interface_layout": dict(self.data_interface_layout),
            "stages": self.stages(),
        }


@dataclass(frozen=True)
class GpuParticleProgramSource:
    kernel_hash: str
    emitters: tuple[GpuParticleEmitterSource, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "$schema": "infernux.particle_gpu_glsl",
            "kernel_hash": self.kernel_hash,
            "emitters": [emitter.to_dict() for emitter in self.emitters],
        }


class GpuParticleGlslLowerer:
    """Generate portable GLSL 450 consumed by the Vulkan RHI backend."""

    def lower(self, program: ParticleKernelProgram) -> GpuParticleProgramSource:
        if not isinstance(program, ParticleKernelProgram):
            raise TypeError("GPU particle lowering requires ParticleKernelProgram")
        return GpuParticleProgramSource(
            program.kernel_hash,
            tuple(self._lower_emitter(program.kernel_hash, emitter) for emitter in program.emitters),
        )

    def _lower_emitter(
        self, kernel_hash: str, emitter: ParticleEmitterKernelIR
    ) -> GpuParticleEmitterSource:
        fields = _attribute_fields(emitter)
        attribute_layout, state_stride = _std430_attribute_layout(fields)
        data_interface_layout = _data_interface_layout(emitter)
        prelude = _shader_prelude(fields, emitter.random_seed, data_interface_layout)
        bootstrap = prelude + _bootstrap_main()
        init_body, _ = _StageCompiler(emitter, fields, data_interface_layout).compile(emitter.init)
        update_body, _ = _StageCompiler(emitter, fields, data_interface_layout).compile(emitter.update)
        rendering_body, exports = _StageCompiler(emitter, fields, data_interface_layout).compile(
            emitter.rendering
        )
        required = {"builtin.position", "builtin.size", "builtin.color", "builtin.rotation"}
        if not required.issubset(exports):
            missing = ", ".join(sorted(required - set(exports)))
            raise GpuParticleCompileError(
                f"particle rendering stage does not export {missing}"
            )
        return GpuParticleEmitterSource(
            emitter.stable_id,
            kernel_hash,
            tuple(
                (stable_id, field, _glsl_type(value_type), offset, byte_size)
                for stable_id, value_type, field, offset, byte_size in attribute_layout
            ),
            state_stride,
            bootstrap,
            prelude + _init_main(init_body, emitter, fields),
            prelude + _update_main(update_body, emitter, fields),
            prelude + _render_reset_main(),
            prelude + _rendering_main(rendering_body, exports),
            tuple(interface.to_dict() for interface in emitter.data_interfaces),
            data_interface_layout,
        )


def compile_gpu_particle_spirv(program: GpuParticleProgramSource) -> dict[str, Any]:
    """Compile and compress all generated stages using the engine glslang service."""
    from Infernux.lib import _Infernux as native

    emitters = []
    for emitter in program.emitters:
        sources = emitter.stages()
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
        stages = {}
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
            stages[stage] = dict(descriptor)
        emitters.append({"stable_id": emitter.stable_id, "stages": stages})
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

    return {
        "$schema": "infernux.particle_gpu_spirv",
        "target": "vulkan1.2-spirv1.5",
        "kernel_hash": program.kernel_hash,
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
        or type(value["emitters"]) is not list
        or len(value["emitters"]) != len(program.emitters)
    ):
        raise GpuParticleCompileError("particle GPU SPIR-V header is incompatible")
    billboard = value["billboard"]
    if type(billboard) is not dict or set(billboard) != {
        "vertex", "fragment", "forward_plus_fragment", "picking_fragment"
    }:
        raise GpuParticleCompileError("particle GPU billboard binary is incomplete")
    mesh = value["mesh"]
    if type(mesh) is not dict or set(mesh) != {
        "vertex", "fragment", "forward_plus_fragment", "picking_fragment"
    }:
        raise GpuParticleCompileError("particle GPU mesh binary is incomplete")
    for encoded, source in zip(value["emitters"], program.emitters):
        if type(encoded) is not dict or set(encoded) != {"stable_id", "stages"}:
            raise GpuParticleCompileError("particle GPU emitter binary entry is invalid")
        stages = encoded["stages"]
        if encoded["stable_id"] != source.stable_id or type(stages) is not dict:
            raise GpuParticleCompileError("particle GPU emitter binary identity is invalid")
        if set(stages) != set(source.stages()):
            raise GpuParticleCompileError("particle GPU emitter binary stages are incomplete")
        for stage, descriptor in stages.items():
            _validate_spirv_descriptor(descriptor, stage)
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
    billboard = value.get("billboard") if type(value) is dict else None
    mesh = value.get("mesh") if type(value) is dict else None
    if (
        type(emitters) is not list
        or emitter_index >= len(emitters)
        or type(emitters[emitter_index]) is not dict
        or type(emitters[emitter_index].get("stages")) is not dict
        or type(billboard) is not dict
        or type(mesh) is not dict
    ):
        raise GpuParticleCompileError("particle GPU emitter payload is invalid")

    def decode(descriptor: Any, stage: str) -> bytes:
        _validate_spirv_descriptor(descriptor, stage)
        return zlib.decompress(
            base64.b64decode(descriptor["zlib_base64"], validate=True)
        )

    emitter = emitters[emitter_index]
    return {
        "stable_id": emitter.get("stable_id", ""),
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
    ) -> None:
        self._emitter = emitter
        self._fields = {stable_id: (value_type, field) for stable_id, value_type, field in fields}
        self._values: dict[str, str] = {}
        self._exports: dict[str, str] = {}
        self._lines: list[str] = []
        self._point_cache_samples = {
            (
                sample["interface"],
                sample["channel"],
                sample["value_type"],
                sample["lookup"],
                sample["semantic"],
            ): sample["sample_index"]
            for interface in data_interface_layout.get("point_caches", ())
            for sample in interface["samples"]
        }
        self._vector_field_samples = {
            interface["stable_id"]: interface["interface_index"]
            for interface in data_interface_layout.get("vector_fields", ())
        }

    def compile(self, function: ParticleKernelFunction) -> tuple[str, dict[str, str]]:
        for instruction in function.instructions:
            self._compile_instruction(instruction)
        return "\n".join(f"    {line}" for line in self._lines), dict(self._exports)

    def _compile_instruction(self, instruction: KernelInstruction) -> None:
        opcode = instruction.opcode
        immediate = instruction.immediate_dict()
        operands = [self._values[item.value_id] for item in instruction.operands]
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
        elif opcode == "add":
            expression = f"({operands[0]} + {operands[1]})"
        elif opcode == "subtract":
            expression = f"({operands[0]} - {operands[1]})"
        elif opcode == "multiply":
            expression = f"({operands[0]} * {operands[1]})"
        elif opcode == "divide":
            expression = f"({operands[0]} / {operands[1]})"
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
        elif opcode == "logical_not":
            expression = f"!({operands[0]})"
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
        elif opcode == "value_noise_3d":
            expression = f"inx_value_noise_3d({operands[0]}, {operands[1]}, {operands[2]})"
        elif opcode == "vector_noise_3d":
            expression = f"inx_vector_noise_3d({operands[0]}, {operands[1]}, {operands[2]})"
        elif opcode.startswith("sample_shape_"):
            mode = "position" if opcode.endswith("position") else "direction"
            slots = immediate["random_slots"]
            expression = (
                f"inx_sample_shape_{mode}({_shape_kind(immediate['shape'])}u, "
                f"{_float_literal(immediate['radius'])}, "
                f"{_float_literal(immediate['angle_degrees'])}, "
                f"{_vector_literal(immediate['dimensions'], 3)}, "
                f"uvec3({int(slots[0])}u, {int(slots[1])}u, {int(slots[2])}u), "
                f"state.{self._field('builtin.id')[1]}, state.spawn_generation)"
            )
        elif opcode == "sample_point_cache":
            key = (
                immediate["interface"],
                immediate["channel"],
                result_type.value_type.value,
                immediate["lookup"],
                immediate["semantic"],
            )
            try:
                sample_index = self._point_cache_samples[key]
            except KeyError as exc:
                raise GpuParticleCompileError(
                    f"GPU point cache sample layout is missing {key!r}"
                ) from exc
            expression = f"inx_sample_point_cache_{sample_index}({operands[0]})"
        elif opcode == "sample_vector_field":
            stable_id = immediate["interface"]
            try:
                sample_index = self._vector_field_samples[stable_id]
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


def _data_interface_layout(emitter: ParticleEmitterKernelIR) -> dict[str, Any]:
    layout = _point_cache_layout(emitter)
    layout.update(_vector_field_layout(emitter))
    return layout


def _point_cache_layout(emitter: ParticleEmitterKernelIR) -> dict[str, Any]:
    interfaces = {
        interface.stable_id: interface
        for interface in emitter.data_interfaces
        if isinstance(interface, PointCache)
    }
    samples: dict[str, set[tuple[str, str, str, str]]] = {}
    for function in (emitter.init, emitter.update, emitter.rendering):
        for instruction in function.instructions:
            if instruction.opcode != "sample_point_cache":
                continue
            immediate = instruction.immediate_dict()
            stable_id = immediate["interface"]
            if stable_id not in interfaces:
                raise GpuParticleCompileError(
                    f"GPU kernel references unknown point cache interface {stable_id!r}"
                )
            samples.setdefault(stable_id, set()).add(
                (
                    immediate["channel"],
                    instruction.result_type.value_type.value,
                    immediate["lookup"],
                    immediate["semantic"],
                )
            )

    if len(samples) > 7:
        raise GpuParticleCompileError(
            "GPU particle emitters currently support at most seven sampled PointCache interfaces"
        )
    point_caches = []
    sample_index = 0
    for interface_index, stable_id in enumerate(sorted(samples)):
        encoded_samples = []
        for channel, value_type, lookup, semantic in sorted(samples[stable_id]):
            encoded_samples.append(
                {
                    "sample_index": sample_index,
                    "interface": stable_id,
                    "channel": channel,
                    "value_type": value_type,
                    "lookup": lookup,
                    "semantic": semantic,
                }
            )
            sample_index += 1
        point_caches.append(
            {
                "stable_id": stable_id,
                "interface_index": interface_index,
                "data_binding": 1 + interface_index * 2,
                "lookup_binding": 2 + interface_index * 2,
                "samples": encoded_samples,
            }
        )
    return {
        "version": 1,
        "metadata_binding": 0,
        "interface_stride_words": 32,
        "sample_stride_words": 4,
        "sample_count": sample_index,
        "point_caches": point_caches,
    }


def _vector_field_layout(emitter: ParticleEmitterKernelIR) -> dict[str, Any]:
    interfaces = {
        interface.stable_id: interface
        for interface in emitter.data_interfaces
        if isinstance(interface, VectorField)
    }
    sampled: set[str] = set()
    for function in (emitter.init, emitter.update, emitter.rendering):
        for instruction in function.instructions:
            if instruction.opcode != "sample_vector_field":
                continue
            stable_id = instruction.immediate_dict()["interface"]
            if stable_id not in interfaces:
                raise GpuParticleCompileError(
                    f"GPU kernel references unknown vector field interface {stable_id!r}"
                )
            sampled.add(stable_id)
    if len(sampled) > 15:
        raise GpuParticleCompileError(
            "GPU particle emitters currently support at most fifteen sampled VectorField interfaces"
        )
    return {
        "vector_field_metadata_binding": 0,
        "vector_field_stride_words": 32,
        "vector_fields": [
            {
                "stable_id": stable_id,
                "interface_index": index,
                "texture_binding": index + 1,
                "boundary": interfaces[stable_id].boundary.value,
                "filtering": interfaces[stable_id].filtering.value,
            }
            for index, stable_id in enumerate(sorted(sampled))
        ],
    }


def _attribute_fields(
    emitter: ParticleEmitterKernelIR,
) -> tuple[tuple[str, TypeRef, str], ...]:
    used: set[str] = set()
    result = []
    for stable_id, value_type, _default in emitter.attributes:
        if value_type.value_type in {ValueType.STRING, ValueType.ASSET_REF}:
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


def _point_cache_glsl(layout: dict[str, Any]) -> str:
    point_caches = layout.get("point_caches", ())
    if not point_caches:
        return ""
    interface_stride = int(layout["interface_stride_words"])
    sample_stride = int(layout["sample_stride_words"])
    sample_base = len(point_caches) * interface_stride
    lines = [
        "layout(std430, set = 1, binding = 0) readonly buffer InxPointCacheMetadata { uint inx_pc_meta[]; };"
    ]
    for interface in point_caches:
        interface_index = int(interface["interface_index"])
        lines.extend(
            (
                f"layout(std430, set = 1, binding = {int(interface['data_binding'])}) "
                f"readonly buffer InxPointCacheData{interface_index} {{ uint inx_pc_data_{interface_index}[]; }};",
                f"layout(std430, set = 1, binding = {int(interface['lookup_binding'])}) "
                f"readonly buffer InxPointCacheLookup{interface_index} {{ uvec2 inx_pc_lookup_{interface_index}[]; }};",
            )
        )
        base = interface_index * interface_stride
        lines.extend(
            (
                f"uint inx_pc_resolve_{interface_index}(uint key) {{",
                f"    uint point_count = inx_pc_meta[{base + 28}u];",
                f"    uint lookup_mode = inx_pc_meta[{base + 30}u];",
                "    if (lookup_mode == 0u) return key < point_count ? key : 0xffffffffu;",
                f"    uint mask = inx_pc_meta[{base + 29}u];",
                "    uint slot = (key * 0x9e3779b1u) & mask;",
                "    for (uint probe = 0u; probe <= mask; ++probe) {",
                f"        uvec2 entry = inx_pc_lookup_{interface_index}[slot];",
                "        if (entry.y == 0xffffffffu) return 0xffffffffu;",
                "        if (entry.x == key) return entry.y;",
                "        slot = (slot + 1u) & mask;",
                "    }",
                "    return 0xffffffffu;",
                "}",
                f"mat4 inx_pc_matrix_{interface_index}() {{",
                "    return mat4(",
                *(
                    "        vec4("
                    + ", ".join(
                        f"uintBitsToFloat(inx_pc_meta[{base + column * 4 + row}u])"
                        for row in range(4)
                    )
                    + (")," if column < 3 else ")")
                    for column in range(4)
                ),
                "    );",
                "}",
                f"mat3 inx_pc_normal_matrix_{interface_index}() {{",
                "    return mat3(",
                *(
                    "        vec3("
                    + ", ".join(
                        f"uintBitsToFloat(inx_pc_meta[{base + 16 + column * 4 + row}u])"
                        for row in range(3)
                    )
                    + (")," if column < 2 else ")")
                    for column in range(3)
                ),
                "    );",
                "}",
            )
        )

        for sample in interface["samples"]:
            index = int(sample["sample_index"])
            metadata = sample_base + index * sample_stride
            kind = ValueType(sample["value_type"])
            glsl_type = _glsl_type(TypeRef(kind))
            zero = "0u" if kind is ValueType.U32 else f"{glsl_type}(0.0)"
            word = f"inx_pc_meta[{metadata}u] + point_index * inx_pc_meta[{metadata + 1}u]"
            if kind is ValueType.U32:
                loaded = f"inx_pc_data_{interface_index}[word]"
            elif kind is ValueType.F32:
                loaded = f"uintBitsToFloat(inx_pc_data_{interface_index}[word])"
            else:
                components = {
                    ValueType.VEC2: 2,
                    ValueType.VEC3: 3,
                    ValueType.VEC4: 4,
                    ValueType.COLOR: 4,
                }[kind]
                loaded = (
                    f"{glsl_type}("
                    + ", ".join(
                        f"uintBitsToFloat(inx_pc_data_{interface_index}[word + {component}u])"
                        for component in range(components)
                    )
                    + ")"
                )
            resolve = (
                f"(key < inx_pc_meta[{base + 28}u] ? key : 0xffffffffu)"
                if sample["lookup"] == "index"
                else f"inx_pc_resolve_{interface_index}(key)"
            )
            semantic = sample["semantic"]
            if semantic == "position":
                transformed = f"(inx_pc_matrix_{interface_index}() * vec4(value, 1.0)).xyz"
            elif semantic in {"direction", "vector"}:
                transformed = f"mat3(inx_pc_matrix_{interface_index}()) * value"
            elif semantic == "normal":
                transformed = f"inx_pc_normal_matrix_{interface_index}() * value"
            else:
                transformed = "value"
            lines.extend(
                (
                    f"{glsl_type} inx_sample_point_cache_{index}(uint key) {{",
                    f"    uint point_index = {resolve};",
                    f"    if (point_index == 0xffffffffu) return {zero};",
                    f"    uint word = {word};",
                    f"    {glsl_type} value = {loaded};",
                    f"    return {transformed};",
                    "}",
                )
            )
    return "\n".join(lines)


def _vector_field_glsl(layout: dict[str, Any]) -> str:
    vector_fields = layout.get("vector_fields", ())
    if not vector_fields:
        return ""
    stride = int(layout["vector_field_stride_words"])
    lines = [
        "layout(std430, set = 2, binding = 0) readonly buffer InxVectorFieldMetadata { uint inx_vf_meta[]; };"
    ]
    for interface in vector_fields:
        index = int(interface["interface_index"])
        base = index * stride
        lines.extend(
            (
                f"layout(set = 2, binding = {int(interface['texture_binding'])}) uniform sampler3D inx_vf_texture_{index};",
                f"mat4 inx_vf_simulation_to_field_{index}() {{",
                "    return mat4(",
                *(
                    "        vec4("
                    + ", ".join(
                        f"uintBitsToFloat(inx_vf_meta[{base + column * 4 + row}u])"
                        for row in range(4)
                    )
                    + (")," if column < 3 else ")")
                    for column in range(4)
                ),
                "    );",
                "}",
                f"mat3 inx_vf_field_to_simulation_{index}() {{",
                "    return mat3(",
                *(
                    "        vec3("
                    + ", ".join(
                        f"uintBitsToFloat(inx_vf_meta[{base + 16 + column * 4 + row}u])"
                        for row in range(3)
                    )
                    + (")," if column < 2 else ")")
                    for column in range(3)
                ),
                "    );",
                "}",
                f"vec3 inx_sample_vector_field_{index}(vec3 simulation_position) {{",
                f"    vec3 uvw = (inx_vf_simulation_to_field_{index}() * vec4(simulation_position, 1.0)).xyz;",
            )
        )
        if interface["boundary"] == "zero":
            lines.append(
                "    if (any(lessThan(uvw, vec3(0.0))) || any(greaterThan(uvw, vec3(1.0)))) return vec3(0.0);"
            )
        lines.extend(
            (
                f"    vec3 value = texture(inx_vf_texture_{index}, uvw).xyz;",
                f"    float scale = uintBitsToFloat(inx_vf_meta[{base + 28}u]);",
                f"    return inx_vf_field_to_simulation_{index}() * value * scale;",
                "}",
            )
        )
    return "\n".join(lines)


def _shader_prelude(
    fields: tuple[tuple[str, TypeRef, str], ...],
    emitter_seed: int,
    data_interface_layout: dict[str, Any],
) -> str:
    state_fields = "\n".join(
        f"    {_storage_type(value_type)} {field};"
        for _stable_id, value_type, field in fields
    )
    data_interface_glsl = "\n".join(
        part
        for part in (
            _point_cache_glsl(data_interface_layout),
            _vector_field_glsl(data_interface_layout),
        )
        if part
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
{data_interface_glsl}
layout(push_constant) uniform ParticlePushConstants {{
    uint capacity;
    uint invocation_count;
    uint spawn_base_id;
    uint spawn_generation;
    uint system_seed;
    uint simulation_step;
    float delta_time;
    uint reserved;
}} pc;

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
    emitter: ParticleEmitterKernelIR,
    fields: tuple[tuple[str, TypeRef, str], ...],
) -> str:
    id_field = next(field for stable, _type, field in fields if stable == "builtin.id")
    finite = _finite_state_check(emitter.init, fields)
    return f"""
void main() {{
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
    states[particle_index] = state;
    if (!particle_alive) inx_push_free(particle_index);
}}
"""


def _update_main(
    body: str,
    emitter: ParticleEmitterKernelIR,
    fields: tuple[tuple[str, TypeRef, str], ...],
) -> str:
    finite = _finite_state_check(emitter.update, fields)
    return f"""
void main() {{
    uint particle_index = gl_GlobalInvocationID.x;
    if (particle_index >= pc.capacity || states[particle_index].alive == 0u) return;
    ParticleState state = states[particle_index];
    bool particle_alive = true;
{body}
    particle_alive = particle_alive && ({finite});
    state.alive = particle_alive ? 1u : 0u;
    states[particle_index] = state;
    if (!particle_alive) inx_push_free(particle_index);
}}
"""


def _render_reset_main() -> str:
    return """
void main() {
    if (gl_GlobalInvocationID.x != 0u) return;
    counters.visible_count = 0u;
    indirect_args.vertex_count = 6u;
    indirect_args.instance_count = 0u;
    indirect_args.first_vertex = 0u;
    indirect_args.first_instance = 0u;
}
"""


def _rendering_main(body: str, exports: dict[str, str]) -> str:
    position = exports["builtin.position"]
    size = exports["builtin.size"]
    color = exports["builtin.color"]
    rotation = exports["builtin.rotation"]
    orientation = exports.get("builtin.orientation", "vec3(0.0)")
    world_position = f"(transforms.simulation_to_world * vec4({position}, 1.0)).xyz"
    world_scale = (
        "max(length(transforms.simulation_to_world[0].xyz), "
        "max(length(transforms.simulation_to_world[1].xyz), "
        "length(transforms.simulation_to_world[2].xyz)))"
    )
    finite = " && ".join(
        (_finite_expression(position, TypeRef(ValueType.VEC3)),
         _finite_expression(size, TypeRef(ValueType.F32)),
         _finite_expression(color, TypeRef(ValueType.COLOR)),
         _finite_expression(rotation, TypeRef(ValueType.F32)),
         _finite_expression(orientation, TypeRef(ValueType.VEC3)))
    )
    return f"""
void main() {{
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
    uint output_index = atomicAdd(counters.visible_count, 1u);
    if (output_index >= pc.capacity) return;
    instances[output_index].position_size = vec4({world_position}, ({size}) * {world_scale});
    instances[output_index].color = {color};
    instances[output_index].rotation_custom = vec4({rotation}, {orientation});
    render_indices[output_index] = output_index;
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


def _float_literal(value: Any) -> str:
    result = format(float(value), ".9g")
    if "." not in result and "e" not in result.lower():
        result += ".0"
    return result


def _vector_literal(values: Any, count: int) -> str:
    return f"vec{count}(" + ", ".join(_float_literal(item) for item in values) + ")"


def _shape_kind(value: str) -> int:
    try:
        return {"point": 0, "sphere": 1, "box": 2, "cone": 3}[value]
    except KeyError as exc:
        raise GpuParticleCompileError(f"unsupported particle shape {value!r}") from exc


__all__ = [
    "GpuParticleCompileError",
    "GpuParticleEmitterSource",
    "GpuParticleGlslLowerer",
    "GpuParticleProgramSource",
    "build_gpu_particle_migration",
    "compile_gpu_particle_spirv",
    "decode_gpu_particle_spirv",
    "validate_gpu_particle_spirv",
]
