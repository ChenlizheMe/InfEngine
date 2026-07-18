"""AOT lowering from portable particle Kernel IR to Vulkan GLSL compute sources."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import re
from typing import Any
import zlib

from Infernux.graph.types import CoordinateSpace, TypeRef, ValueType

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


@dataclass(frozen=True)
class GpuParticleEmitterSource:
    stable_id: str
    kernel_hash: str
    attribute_fields: tuple[tuple[str, str, str], ...]
    state_stride: int
    bootstrap: str
    init: str
    update: str
    render_reset: str
    rendering: str

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
                {"stable_id": stable_id, "field": field, "glsl_type": glsl_type}
                for stable_id, field, glsl_type in self.attribute_fields
            ],
            "state_stride": self.state_stride,
            "stages": self.stages(),
        }


@dataclass(frozen=True)
class GpuParticleProgramSource:
    kernel_hash: str
    emitters: tuple[GpuParticleEmitterSource, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "$schema": "infernux.particle_gpu_glsl",
            "$version": 1,
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
        prelude = _shader_prelude(fields, emitter.random_seed)
        bootstrap = prelude + _bootstrap_main()
        init_body, _ = _StageCompiler(emitter, fields).compile(emitter.init)
        update_body, _ = _StageCompiler(emitter, fields).compile(emitter.update)
        rendering_body, exports = _StageCompiler(emitter, fields).compile(
            emitter.rendering
        )
        required = {"builtin.position", "builtin.size", "builtin.color"}
        if not required.issubset(exports):
            missing = ", ".join(sorted(required - set(exports)))
            raise GpuParticleCompileError(
                f"particle rendering stage does not export {missing}"
            )
        return GpuParticleEmitterSource(
            emitter.stable_id,
            kernel_hash,
            tuple(
                (stable_id, field, _glsl_type(value_type))
                for stable_id, value_type, field in fields
            ),
            _std430_state_stride(fields),
            bootstrap,
            prelude + _init_main(init_body, emitter, fields),
            prelude + _update_main(update_body, emitter, fields),
            prelude + _render_reset_main(),
            prelude + _rendering_main(rendering_body, exports),
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
    return {
        "$schema": "infernux.particle_gpu_spirv",
        "$version": 1,
        "target": "vulkan1.2-spirv1.5",
        "kernel_hash": program.kernel_hash,
        "emitters": emitters,
    }


def validate_gpu_particle_spirv(
    value: Any, program: GpuParticleProgramSource
) -> dict[str, Any]:
    """Strictly validate a persisted GPU binary payload without recompiling it."""
    expected_top = {"$schema", "$version", "target", "kernel_hash", "emitters"}
    if type(value) is not dict or set(value) != expected_top:
        raise GpuParticleCompileError("particle GPU SPIR-V payload is invalid")
    if (
        value["$schema"] != "infernux.particle_gpu_spirv"
        or value["$version"] != 1
        or value["target"] != "vulkan1.2-spirv1.5"
        or value["kernel_hash"] != program.kernel_hash
        or type(value["emitters"]) is not list
        or len(value["emitters"]) != len(program.emitters)
    ):
        raise GpuParticleCompileError("particle GPU SPIR-V header is incompatible")
    for encoded, source in zip(value["emitters"], program.emitters):
        if type(encoded) is not dict or set(encoded) != {"stable_id", "stages"}:
            raise GpuParticleCompileError("particle GPU emitter binary entry is invalid")
        stages = encoded["stages"]
        if encoded["stable_id"] != source.stable_id or type(stages) is not dict:
            raise GpuParticleCompileError("particle GPU emitter binary identity is invalid")
        if set(stages) != set(source.stages()):
            raise GpuParticleCompileError("particle GPU emitter binary stages are incomplete")
        for stage, descriptor in stages.items():
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
    return value


class _StageCompiler:
    def __init__(
        self,
        emitter: ParticleEmitterKernelIR,
        fields: tuple[tuple[str, TypeRef, str], ...],
    ) -> None:
        self._emitter = emitter
        self._fields = {stable_id: (value_type, field) for stable_id, value_type, field in fields}
        self._values: dict[str, str] = {}
        self._exports: dict[str, str] = {}
        self._lines: list[str] = []

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
        elif opcode == "multiply":
            expression = f"({operands[0]} * {operands[1]})"
        elif opcode == "less_than":
            expression = f"({operands[0]} < {operands[1]})"
        elif opcode == "normalize":
            expression = f"inx_safe_normalize({operands[0]})"
        elif opcode == "random_f32":
            expression = (
                f"inx_random_range({operands[0]}, {operands[1]}, {operands[2]}, "
                f"{int(immediate['random_slot'])}u, state.{self._field('builtin.id')[1]}, "
                "state.spawn_generation)"
            )
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
        elif opcode == "convert_space":
            expression = _space_conversion(operands[0], result_type, immediate)
        elif opcode == "store_attribute":
            value_type, field = self._field(immediate["attribute"])
            value = operands[0]
            if value_type.value_type is ValueType.BOOL:
                value = f"({value} ? 1u : 0u)"
            self._lines.append(f"state.{field} = {value};")
            return
        elif opcode == "set_alive":
            self._lines.append(f"particle_alive = {operands[0]};")
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


def _std430_state_stride(
    fields: tuple[tuple[str, TypeRef, str], ...],
) -> int:
    offset = 8  # alive + spawn_generation
    struct_alignment = 4
    layout = {
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
    for stable_id, value_type, _field in fields:
        try:
            alignment, byte_size = layout[value_type.value_type]
        except KeyError as exc:
            raise GpuParticleCompileError(
                f"attribute {stable_id!r} has no std430 storage layout"
            ) from exc
        offset = _align_up(offset, alignment) + byte_size
        struct_alignment = max(struct_alignment, alignment)
    return _align_up(offset, struct_alignment)


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _shader_prelude(
    fields: tuple[tuple[str, TypeRef, str], ...], emitter_seed: int
) -> str:
    state_fields = "\n".join(
        f"    {_storage_type(value_type)} {field};"
        for _stable_id, value_type, field in fields
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
    finite = " && ".join(
        (_finite_expression(position, TypeRef(ValueType.VEC3)),
         _finite_expression(size, TypeRef(ValueType.F32)),
         _finite_expression(color, TypeRef(ValueType.COLOR)))
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
    instances[output_index].position_size = vec4({position}, {size});
    instances[output_index].color = {color};
    instances[output_index].rotation_custom = vec4(0.0);
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
    "compile_gpu_particle_spirv",
    "validate_gpu_particle_spirv",
]
