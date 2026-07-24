from __future__ import annotations

import copy
from pathlib import Path
import shutil
import struct
import subprocess

import pytest

import Infernux.particle.gpu_glsl_backend as gpu_backend

from Infernux.lib import _Infernux as native
from Infernux.particle import (
    GpuParticleGlslLowerer,
    GpuParticleCompileError,
    KernelCompileError,
    ParticleAttribute,
    ParticleEmitterAsset,
    ParticleEventField,
    ParticleEventRoute,
    ParticleEventType,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleKernelLowerer,
    PointCache,
    VectorField,
    build_gpu_particle_migration,
    compile_gpu_particle_spirv,
    standard_particle_attributes,
    validate_gpu_particle_spirv,
)
from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
from Infernux.graph.types import AssetReference, CoordinateSpace, TypeRef, ValueType
from Infernux.particle.nodes import (
    particle_event_output_type_id,
    particle_event_payload_port_id,
    particle_event_payload_type_id,
)


def _gpu_source():
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(stable_id="gpu-particle")
    )
    kernel = ParticleKernelLowerer().lower(hir)
    return GpuParticleGlslLowerer().lower(kernel)


def _kill_if_gpu_source():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("kill", "particle.update.kill_if"),
            GraphNodeRecord("age", "particle.attribute.read_f32"),
            GraphNodeRecord("limit", "common.constant.f32", properties={"value": 0.5}),
            GraphNodeRecord("older", "common.compare.greater_than"),
        ),
        links=(
            GraphLinkRecord("stream", "root.update", "out", "kill", "in", PortKind.STREAM),
            GraphLinkRecord("a", "age", "value", "older", "a", PortKind.VALUE),
            GraphLinkRecord("b", "limit", "value", "older", "b", PortKind.VALUE),
            GraphLinkRecord("condition", "older", "result", "kill", "condition", PortKind.VALUE),
        ),
    )
    emitter = ParticleEmitterAsset(stable_id="kill", update=update)
    hir = ParticleGraphCompiler().compile(ParticleGraphAsset(emitters=(emitter,)))
    return GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))


def _event_output_program():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "impact.position",
                "common.constant.vec3",
                properties={"value": [4.0, 5.0, 6.0]},
            ),
            GraphNodeRecord(
                "impact.output",
                particle_event_output_type_id("impact-route", "update"),
                properties={"condition": True},
            ),
        ),
        links=(
            GraphLinkRecord(
                "event.stream",
                "root.update",
                "out",
                "impact.output",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "event.position",
                "impact.position",
                "value",
                "impact.output",
                particle_event_payload_port_id("position"),
                PortKind.VALUE,
            ),
        ),
    )
    source = ParticleEmitterAsset(stable_id="source", update=update)
    target_init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "impact.payload",
                particle_event_payload_type_id("impact-route"),
            ),
            GraphNodeRecord("impact.weight", "particle.attribute.set_size"),
        ),
        links=(
            GraphLinkRecord(
                "target.stream",
                "root.init",
                "out",
                "impact.weight",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "target.weight",
                "impact.payload",
                particle_event_payload_port_id("weight"),
                "impact.weight",
                "value",
                PortKind.VALUE,
            ),
        ),
    )
    target = ParticleEmitterAsset(stable_id="target", init=target_init)
    impact = ParticleEventType(
        "impact",
        "Impact",
        64,
        (
            ParticleEventField(
                "position",
                "Position",
                TypeRef(ValueType.VEC3),
                [1.0, 2.0, 3.0],
            ),
            ParticleEventField(
                "kind",
                "Kind",
                TypeRef(ValueType.U32),
                7,
            ),
            ParticleEventField(
                "weight",
                "Weight",
                TypeRef(ValueType.F32),
                2.5,
            ),
        ),
    )
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(
            stable_id="event-output-graph",
            emitters=(source, target),
            event_types=(impact,),
            event_routes=(
                ParticleEventRoute(
                    "impact-route",
                    "impact",
                    "source",
                    "update",
                    "target",
                    2,
                ),
            ),
        )
    )
    kernel = ParticleKernelLowerer().lower(hir)
    return kernel, GpuParticleGlslLowerer().lower(kernel)


def test_gpu_event_payload_round_trips_from_stage_output_to_event_init():
    kernel, gpu = _event_output_program()
    instruction = next(
        instruction
        for instruction in kernel.emitters[0].update.instructions
        if instruction.opcode == "event_append"
    )
    immediate = instruction.immediate_dict()
    assert immediate["channel_index"] == 0
    assert [operand.value_type for operand in instruction.operands] == [
        TypeRef(ValueType.BOOL),
        TypeRef(ValueType.VEC3),
        TypeRef(ValueType.U32),
        TypeRef(ValueType.F32),
    ]
    assert [field["stable_id"] for field in immediate["payload_layout"]] == [
        "position",
        "kind",
        "weight",
    ]
    assert [field["word_offset"] for field in immediate["payload_layout"]] == [0, 3, 4]
    assert kernel.events.event_types[0].payload_stride_words == 5
    assert kernel.events.event_types[0].fields[0].value_type == TypeRef(ValueType.VEC3)
    assert kernel.events.routes[0].source_stage.value == "update"
    target_payload = next(
        instruction
        for instruction in kernel.emitters[1].init.instructions
        if instruction.opcode == "event_payload"
    )
    assert target_payload.result_type == TypeRef(ValueType.F32)
    assert target_payload.immediate_dict() == {
        "channel_index": 0,
        "word_offset": 4,
        "word_count": 1,
        "default": 2.5,
    }

    source = gpu.emitters[0]
    assert source.event_output_stages == ("update",)
    assert "layout(std430, set = 3, binding = 1)" in source.update
    assert "atomicAdd(event_output_counters" in source.update
    assert "state.spawn_generation" in source.update
    assert "event_output_record_words" in source.update
    assert "vec3(4.0, 5.0, 6.0)" in source.update
    assert "floatBitsToUint" in source.update
    assert "ParticleEventOutputChannels" not in source.init
    target_source = gpu.emitters[1]
    assert "uintBitsToFloat" not in target_source.init
    assert "= 2.5;" in target_source.init
    assert "channel_index == 0u" in target_source.event_init
    assert "event_record_words[record_base + 8u]" in target_source.event_init
    restored = type(kernel).from_dict(kernel.to_dict())
    assert GpuParticleGlslLowerer().lower(restored).emitters[0].update == source.update
    corrupted = copy.deepcopy(kernel.to_dict())
    corrupted["events"]["routes"][0]["source_stage"] = "init"
    with pytest.raises(KernelCompileError, match="does not match its source route"):
        type(kernel).from_dict(corrupted)
    compiled = compile_gpu_particle_spirv(gpu)
    assert set(compiled["emitters"][0]["stages"]) == set(source.stages())


def _noise_gpu_source():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("acceleration", "particle.update.acceleration"),
            GraphNodeRecord("position", "particle.attribute.read_vec3"),
            GraphNodeRecord(
                "noise",
                "common.noise.vector3d",
                properties={"frequency": 2.0, "seed": 17},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "acceleration", "in", PortKind.STREAM
            ),
            GraphLinkRecord("position", "position", "value", "noise", "position"),
            GraphLinkRecord("noise", "noise", "value", "acceleration", "value"),
        ),
    )
    emitter = ParticleEmitterAsset(stable_id="noise", update=update)
    hir = ParticleGraphCompiler().compile(ParticleGraphAsset(emitters=(emitter,)))
    return GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))


def test_gpu_kill_if_accumulates_death_and_feeds_the_existing_free_list():
    source = _kill_if_gpu_source().emitters[0].update

    assert "particle_alive = particle_alive && !(" in source
    assert " > " in source
    assert "inx_push_free" in source


def test_gpu_vector_noise_uses_the_portable_hash_and_compiles_to_spirv():
    source = _noise_gpu_source()
    update = source.emitters[0].update

    assert "inx_noise_hash" in update
    assert "inx_vector_noise_3d" in update
    payload = compile_gpu_particle_spirv(source)
    assert validate_gpu_particle_spirv(payload, source) is payload


def test_geometry_and_particle_shadow_receivers_use_stable_pcf_without_blocker_search():
    python_root = Path(__file__).resolve().parents[1]
    shader_path = python_root / "Infernux" / "resources" / "shaders" / "lighting.glsl"
    geometry_source = shader_path.read_text(encoding="utf-8")
    particle_source = (python_root / "Infernux" / "particle" / "gpu_glsl_backend.py").read_text(encoding="utf-8")
    lighting_ubo = (
        python_root / "Infernux" / "resources" / "shaders" / "_templates" / "lighting_ubo.glsl"
    ).read_text(encoding="utf-8")

    for source in (geometry_source, particle_source):
        assert "interleavedGradientNoise" not in source
        assert "vogelDiskSample" not in source
        assert "1.0 + slope" not in source
        assert "1.0 + n_dot_l" not in source
        assert "textureGather" in source
        assert "pcss" not in source.lower()
        assert "blockerDisk" not in source and "blocker_disk" not in source
    assert "step(vec4(receiverDepth), depths)" in geometry_source
    assert "shadowTentWeight" in geometry_source
    assert "for (int y = 0; y < 4; ++y)" in geometry_source
    assert "shadowParams.z * worldTexel" in geometry_source
    assert "dFdx" in particle_source and "dFdy" in particle_source
    assert "receiver_plane_gradient" in particle_source
    assert "step(receiver_depths, depths)" in particle_source
    assert "0.002 * atlas_size" in particle_source
    assert "clamp(gradient" in particle_source
    assert "filter_disk" in particle_source
    assert "kernel_rotation" in particle_source
    assert "sampler2D shadowMap" in lighting_ubo
    assert "sampler2D particle_shadow_map" in particle_source


def test_shadow_vertex_keeps_directional_bias_out_of_caster_geometry():
    python_root = Path(__file__).resolve().parents[1]
    template_root = python_root / "Infernux" / "resources" / "shaders" / "_templates"
    builtins = (template_root / "shadow_vertex_builtins.glsl").read_text(encoding="utf-8")
    vertex = (template_root / "shadow_vertex_main.glsl").read_text(encoding="utf-8")

    assert "vec4 light_vector" in builtins
    assert "vec4 bias" in builtins
    assert "transpose(inverse(mat3(instModel)))" in vertex
    assert "worldPos.xyz -=" not in vertex
    assert "shadowUBO.bias." not in vertex
    assert "if (shadowUBO.light_vector.w < 0.5)" in vertex
    assert "gl_Position.z = max(gl_Position.z, 0.0)" in vertex


def test_geometry_shadow_filter_applies_receiver_bias_before_stable_tent_pcf():
    python_root = Path(__file__).resolve().parents[1]
    source = (python_root / "Infernux" / "resources" / "shaders" / "lighting.glsl").read_text(
        encoding="utf-8"
    )

    normal_bias = "biasedPosition += N * (shadowParams.z * worldTexel"
    light_bias = "biasedPosition += L * (shadowParams.y * worldTexel)"
    projection = "shadowView.viewProjection * vec4(biasedPosition, 1.0)"
    assert normal_bias in source
    assert light_bias in source
    assert projection in source
    assert source.index(normal_bias) < source.index(projection)
    assert source.index(light_bias) < source.index(projection)
    assert "vec4 comparison = step(vec4(receiverDepth), depths)" in source
    assert "float stepSize = filterTexels / 1.5" in source
    assert "return visibility / max(totalWeight, 1.0)" in source


def test_gpu_ribbon_render_instance_exports_full_width_topology_key():
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord("ribbon", "particle.output.ribbon"),
        ),
        links=(
            GraphLinkRecord("render", "root.rendering", "out", "ribbon", "in", PortKind.STREAM),
        ),
    )
    asset = ParticleGraphAsset(
        stable_id="gpu-ribbon",
        emitters=(ParticleEmitterAsset(stable_id="trail", rendering=rendering),),
    )
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    )
    emitter = source.emitters[0]

    assert "uvec4 ribbon_data" in emitter.rendering
    assert "instances[output_index].ribbon_data = uvec4(" in emitter.rendering
    assert "state.a_builtin_ribbon_strip_id" in emitter.rendering
    assert "state.a_builtin_ribbon_order" in emitter.rendering
    assert "state.a_builtin_ribbon_break" in emitter.rendering
    payload = compile_gpu_particle_spirv(source)
    assert validate_gpu_particle_spirv(payload, source) is payload


def test_gpu_plane_collision_uses_portable_post_integration_helpers_and_compiles():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "collision",
                "particle.update.collide_plane",
                properties={"radius": 0.2, "restitution": 0.65, "friction": 0.15},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.STREAM
            ),
        ),
    )
    asset = ParticleGraphAsset(
        stable_id="gpu-plane-collision",
        emitters=(ParticleEmitterAsset(stable_id="sparks", update=update),),
    )
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    )
    update_source = source.emitters[0].update

    assert "inx_collide_plane_position" in update_source
    assert "inx_collide_plane_velocity" in update_source
    assert update_source.index("update.integrate_position") < update_source.index(
        "// collision"
    )
    payload = compile_gpu_particle_spirv(source)
    assert validate_gpu_particle_spirv(payload, source) is payload


def test_gpu_sphere_collision_uses_portable_post_integration_helpers_and_compiles():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "collision",
                "particle.update.collide_sphere",
                properties={"sphere_radius": 1.5, "particle_radius": 0.1},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.STREAM
            ),
        ),
    )
    asset = ParticleGraphAsset(
        stable_id="gpu-sphere-collision",
        emitters=(ParticleEmitterAsset(stable_id="sparks", update=update),),
    )
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    )
    update_source = source.emitters[0].update

    assert "inx_collide_sphere_position" in update_source
    assert "inx_collide_sphere_velocity" in update_source
    assert update_source.index("update.integrate_position") < update_source.index(
        "// collision"
    )
    payload = compile_gpu_particle_spirv(source)
    assert validate_gpu_particle_spirv(payload, source) is payload


def _point_cache_gpu_source():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord("velocity", "particle.init.set_velocity"),
            GraphNodeRecord("particle_id", "particle.attribute.read_u32"),
            GraphNodeRecord(
                "sample",
                "particle.point_cache.sample_position",
                properties={
                    "interface": "spawn-points",
                    "channel": "$position",
                    "lookup": "stable_id",
                    "semantic": "position",
                },
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.init", "out", "velocity", "in", PortKind.STREAM
            ),
            GraphLinkRecord("id", "particle_id", "value", "sample", "index"),
            GraphLinkRecord("value", "sample", "value", "velocity", "value"),
        ),
    )
    emitter = ParticleEmitterAsset(
        stable_id="point-cache-emitter",
        init=init,
        data_interfaces=(
            PointCache(
                stable_id="spawn-points",
                cache=AssetReference(guid="point-cache-guid"),
                space=CoordinateSpace.WORLD,
                id_channel="stable_id",
            ),
        ),
    )
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(stable_id="point-cache-gpu", emitters=(emitter,))
    )
    return GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))


def _vector_field_gpu_source(*, boundary="zero", filtering="linear"):
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("acceleration", "particle.update.acceleration"),
            GraphNodeRecord("position", "particle.attribute.read_vec3"),
            GraphNodeRecord(
                "sample",
                "particle.vector_field.sample",
                properties={"interface": "wind"},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "acceleration", "in", PortKind.STREAM
            ),
            GraphLinkRecord("position", "position", "value", "sample", "position"),
            GraphLinkRecord("value", "sample", "value", "acceleration", "value"),
        ),
    )
    emitter = ParticleEmitterAsset(
        stable_id="vector-field-emitter",
        update=update,
        data_interfaces=(
            VectorField(
                stable_id="wind",
                texture=AssetReference(guid="wind-texture-guid"),
                boundary=boundary,
                filtering=filtering,
            ),
        ),
    )
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(stable_id="vector-field-gpu", emitters=(emitter,))
    )
    return GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))


def test_gpu_lowerer_emits_resident_compute_lifecycle_and_indirect_output():
    program = _gpu_source()
    assert len(program.emitters) == 1
    emitter = program.emitters[0]

    assert set(emitter.stages()) == {
        "bootstrap",
        "init",
        "event_init",
        "update",
        "render_reset",
        "rendering",
    }
    assert "buffer ParticleStates" in emitter.update
    assert "inx_pop_free" in emitter.init
    assert "layout(std430, set = 3, binding = 3)" in emitter.event_init
    assert "event_spawn_indices[channel.spawn_base_indices + invocation]" in emitter.event_init
    assert "uint source_particle_id = event_record_words[record_base + 2u];" in emitter.event_init
    assert "uint route_seed = inx_random_u32(channel_index" in emitter.event_init
    assert "state.spawn_generation = inx_random_u32(" in emitter.event_init
    assert "states[index].spawn_generation = 0u;" in emitter.bootstrap
    assert "atomicAdd(indirect_args.instance_count, 1u)" in emitter.rendering
    assert "layout(push_constant)" in emitter.rendering
    assert "Vk" not in "\n".join(emitter.stages().values())
    assert {
        stable_id for stable_id, _field, _type, _offset, _size in emitter.attribute_fields
    } >= {
        "builtin.position",
        "builtin.velocity",
        "builtin.color",
        "builtin.id",
    }
    assert emitter.state_stride == 80
    document = emitter.to_dict()
    assert document["state_stride"] == 80
    assert all(
        field["offset"] >= 8 and field["byte_size"] % 4 == 0
        for field in document["attribute_fields"]
    )


def test_gpu_lowerer_emits_lifecycle_divide_lerp_rotation_and_attribute_stores():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-color", "particle.attribute.set_color"),
            GraphNodeRecord("set-size", "particle.attribute.set_size"),
            GraphNodeRecord("set-rotation", "particle.attribute.set_rotation"),
            GraphNodeRecord("age", "particle.attribute.read_f32"),
            GraphNodeRecord(
                "lifetime",
                "particle.attribute.read_f32",
                properties={"attribute": "builtin.lifetime"},
            ),
            GraphNodeRecord("normalized-age", "common.math.divide"),
            GraphNodeRecord("start-color", "common.constant.color"),
            GraphNodeRecord(
                "end-color",
                "common.constant.color",
                properties={"value": [0.0, 0.0, 0.0, 0.0]},
            ),
            GraphNodeRecord("color-over-life", "common.math.lerp"),
            GraphNodeRecord("size-over-life", "common.math.lerp", properties={"a": 1.0, "b": 0.0}),
            GraphNodeRecord(
                "rotation-over-life",
                "common.math.lerp",
                properties={"a": 0.0, "b": 3.141592653589793},
            ),
        ),
        links=(
            GraphLinkRecord("stream-color", "root.update", "out", "set-color", "in", PortKind.STREAM),
            GraphLinkRecord("stream-size", "set-color", "out", "set-size", "in", PortKind.STREAM),
            GraphLinkRecord(
                "stream-rotation", "set-size", "out", "set-rotation", "in", PortKind.STREAM
            ),
            GraphLinkRecord("age-divide", "age", "value", "normalized-age", "a"),
            GraphLinkRecord("life-divide", "lifetime", "value", "normalized-age", "b"),
            GraphLinkRecord("color-a", "start-color", "value", "color-over-life", "a"),
            GraphLinkRecord("color-b", "end-color", "value", "color-over-life", "b"),
            GraphLinkRecord("color-t", "normalized-age", "result", "color-over-life", "t"),
            GraphLinkRecord("size-t", "normalized-age", "result", "size-over-life", "t"),
            GraphLinkRecord(
                "rotation-t", "normalized-age", "result", "rotation-over-life", "t"
            ),
            GraphLinkRecord("set-color-value", "color-over-life", "result", "set-color", "value"),
            GraphLinkRecord("set-size-value", "size-over-life", "result", "set-size", "value"),
            GraphLinkRecord(
                "set-rotation-value",
                "rotation-over-life",
                "result",
                "set-rotation",
                "value",
            ),
        ),
    )
    asset = ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    hir = ParticleGraphCompiler().compile(asset)
    source = GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir)).emitters[0].update

    assert " / " in source
    assert "mix(" in source
    assert ".a_builtin_color = " in source
    assert ".a_builtin_size = " in source
    assert ".a_builtin_rotation = " in source

    rendering = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(hir)
    ).emitters[0].rendering
    assert "rotation_custom = vec4(" in rendering
    assert ".a_builtin_rotation" in rendering


def test_gpu_mesh_orientation_and_nonuniform_scale_use_current_instance_abi():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "orientation",
                "particle.attribute.set_orientation",
                properties={"degrees": [10.0, 20.0, 30.0]},
            ),
            GraphNodeRecord(
                "scale",
                "particle.attribute.set_scale",
                properties={"value": [2.0, 0.5, 1.5]},
            ),
        ),
        links=(
            GraphLinkRecord("init-stream", "root.init", "out", "orientation", "in", PortKind.STREAM),
            GraphLinkRecord("scale-stream", "orientation", "out", "scale", "in", PortKind.STREAM),
        ),
    )
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "rotate",
                "particle.update.rotate_orientation",
                properties={"degrees_per_second": [90.0, 180.0, 270.0]},
            ),
        ),
        links=(GraphLinkRecord("update-stream", "root.update", "out", "rotate", "in", PortKind.STREAM),),
    )
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord(
                "mesh",
                "particle.output.mesh",
                properties={"mesh": AssetReference(guid="mesh-guid").to_dict()},
            ),
        ),
        links=(GraphLinkRecord("render-stream", "root.rendering", "out", "mesh", "in", PortKind.STREAM),),
    )
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(
            emitters=(ParticleEmitterAsset(init=init, update=update, rendering=rendering),)
        )
    )
    program = GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))
    emitter = program.emitters[0]

    assert _gpu_source().emitters[0].state_stride == 80
    assert emitter.state_stride == 112
    assert ".a_builtin_orientation" in emitter.init
    assert ".a_builtin_orientation" in emitter.update
    assert "rotation_custom = vec4(" in emitter.rendering
    assert ".a_builtin_orientation" in emitter.rendering
    assert ".a_builtin_scale" in emitter.rendering
    assert "scale_custom = vec4(" in emitter.rendering
    assert "instance.rotation_custom.yzw" in gpu_backend._MESH_VERTEX_GLSL
    assert "instance.scale_custom.xyz" in gpu_backend._MESH_VERTEX_GLSL
    assert "rotation_z * rotation_y * rotation_x" in gpu_backend._MESH_VERTEX_GLSL
    assert "view.rendering_control.y > 0.5" in gpu_backend._MESH_VERTEX_GLSL
    assert "world_position -= to_light * (view.camera_up.x * world_texel)" in gpu_backend._MESH_VERTEX_GLSL
    assert "world_position -= out_normal * (view.camera_up.y * world_texel * normal_scale)" in gpu_backend._MESH_VERTEX_GLSL
    assert "gl_Position.z = max(gl_Position.z, 0.0)" in gpu_backend._MESH_VERTEX_GLSL
    assert "ParticleTileLightMasks" in gpu_backend._PARTICLE_FORWARD_PLUS_LIGHTING_GLSL
    assert "findLSB(light_mask)" in gpu_backend._PARTICLE_FORWARD_PLUS_LIGHTING_GLSL
    assert "tile_indices" not in gpu_backend._PARTICLE_FORWARD_PLUS_LIGHTING_GLSL

    payload = compile_gpu_particle_spirv(program)
    assert validate_gpu_particle_spirv(payload, program) is payload


def test_gpu_curve_and_gradient_sampling_emit_valid_vulkan_glsl():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-size", "particle.attribute.set_size"),
            GraphNodeRecord("set-color", "particle.attribute.set_color"),
            GraphNodeRecord("age", "particle.attribute.read_f32"),
            GraphNodeRecord(
                "curve",
                "common.curve.sample",
                properties={
                    "curve": {
                        "keys": [
                            {"time": 0.0, "value": 0.0, "in_tangent": 0.0, "out_tangent": 1.0},
                            {"time": 1.0, "value": 1.0, "in_tangent": 1.0, "out_tangent": 0.0},
                        ],
                        "pre_wrap": "ping_pong",
                        "post_wrap": "repeat",
                    }
                },
            ),
            GraphNodeRecord("gradient", "common.gradient.sample"),
        ),
        links=(
            GraphLinkRecord("stream-size", "root.update", "out", "set-size", "in", PortKind.STREAM),
            GraphLinkRecord("stream-color", "set-size", "out", "set-color", "in", PortKind.STREAM),
            GraphLinkRecord("age-curve", "age", "value", "curve", "t"),
            GraphLinkRecord("age-gradient", "age", "value", "gradient", "t"),
            GraphLinkRecord("curve-size", "curve", "value", "set-size", "value"),
            GraphLinkRecord("gradient-color", "gradient", "color", "set-color", "value"),
        ),
    )
    asset = ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    hir = ParticleGraphCompiler().compile(asset)
    emitter = GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir)).emitters[0]

    assert "_time =" in emitter.update
    assert "mix(vec4(" in emitter.update
    assert "mod(" in emitter.update
    assert "abs(" in emitter.update
    compiled = native._compile_compute_glsl_batch({"update": emitter.update}, "particle-curve-gradient-test")
    assert set(compiled) == {"update"}


def test_gpu_layout_migration_descriptor_copies_stable_fields_and_packs_defaults():
    stable_id = "layout-emitter"
    previous_asset = ParticleGraphAsset(
        stable_id="previous-layout",
        emitters=(ParticleEmitterAsset(stable_id=stable_id),),
    )
    next_asset = ParticleGraphAsset(
        stable_id="next-layout",
        emitters=(
            ParticleEmitterAsset(
                stable_id=stable_id,
                attributes=(
                    *standard_particle_attributes(),
                    ParticleAttribute(
                        "custom.temperature",
                        "temperature",
                        TypeRef(ValueType.F32),
                        3.5,
                    ),
                ),
            ),
        ),
    )
    previous_kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(previous_asset)
    )
    next_kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(next_asset)
    )
    previous_layout = GpuParticleGlslLowerer().lower(previous_kernel).emitters[0].to_dict()
    next_layout = GpuParticleGlslLowerer().lower(next_kernel).emitters[0].to_dict()

    migration = build_gpu_particle_migration(
        previous_layout,
        next_layout,
        next_kernel.emitters[0],
    )

    assert migration["source_stride"] == previous_layout["state_stride"]
    assert migration["destination_stride"] == next_layout["state_stride"]
    assert len(migration["copy_ranges"]) == len(standard_particle_attributes())
    temperature = next(
        field
        for field in next_layout["attribute_fields"]
        if field["stable_id"] == "custom.temperature"
    )
    default_word = migration["default_state_words"][temperature["offset"] // 4]
    assert struct.unpack("<f", struct.pack("<I", default_word))[0] == pytest.approx(3.5)
    assert all(
        item["source_offset"] % 4 == 0
        and item["destination_offset"] % 4 == 0
        and item["byte_size"] % 4 == 0
        for item in migration["copy_ranges"]
    )

    stale_layout = copy.deepcopy(previous_layout)
    stale_layout["attribute_fields"][0].pop("offset")
    with pytest.raises(GpuParticleCompileError, match="layout entry"):
        build_gpu_particle_migration(stale_layout, next_layout, next_kernel.emitters[0])


def test_gpu_point_cache_lowering_emits_stable_set_one_layout_and_valid_spirv():
    emitter = _point_cache_gpu_source().emitters[0]
    layout = emitter.data_interface_layout

    assert layout["version"] == 1
    assert layout["sample_count"] == 1
    assert layout["point_caches"][0]["stable_id"] == "spawn-points"
    assert layout["point_caches"][0]["data_binding"] == 1
    assert layout["point_caches"][0]["lookup_binding"] == 2
    sample = layout["point_caches"][0]["samples"][0]
    assert sample == {
        "sample_index": 0,
        "interface": "spawn-points",
        "channel": "$position",
        "value_type": "vec3",
        "lookup": "stable_id",
        "semantic": "position",
    }
    assert "set = 1, binding = 0" in emitter.init
    assert "inx_pc_resolve_0" in emitter.init
    assert "inx_sample_point_cache_0" in emitter.init

    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-point-cache-test"
    )
    assert set(compiled) == set(emitter.stages())


def test_gpu_vector_field_lowering_emits_rhi_set_two_layout_and_valid_spirv():
    emitter = _vector_field_gpu_source().emitters[0]
    layout = emitter.data_interface_layout

    assert layout["vector_field_metadata_binding"] == 0
    assert layout["vector_field_stride_words"] == 32
    assert layout["vector_fields"] == [
        {
            "stable_id": "wind",
            "interface_index": 0,
            "texture_binding": 1,
            "boundary": "zero",
            "filtering": "linear",
        }
    ]
    assert "set = 2, binding = 0" in emitter.update
    assert "set = 2, binding = 1" in emitter.update
    assert "uniform sampler3D inx_vf_texture_0" in emitter.update
    assert "any(lessThan(uvw" in emitter.update
    assert "inx_sample_vector_field_0" in emitter.update

    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-vector-field-test"
    )
    assert set(compiled) == set(emitter.stages())


def test_gpu_vector_field_repeat_nearest_policy_is_preserved_for_rhi_sampler():
    emitter = _vector_field_gpu_source(boundary="repeat", filtering="nearest").emitters[0]
    interface = emitter.data_interface_layout["vector_fields"][0]

    assert interface["boundary"] == "repeat"
    assert interface["filtering"] == "nearest"
    assert "any(lessThan(uvw" not in emitter.update


def test_generated_gpu_particle_kernels_compile_to_vulkan_spirv(tmp_path):
    validator = shutil.which("glslangValidator")
    if validator is None:
        pytest.skip("Vulkan SDK glslangValidator is unavailable")

    emitter = _gpu_source().emitters[0]
    for stage, source in emitter.stages().items():
        source_path = tmp_path / f"{stage}.comp"
        output_path = tmp_path / f"{stage}.spv"
        source_path.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [validator, "-V", str(source_path), "-o", str(output_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert output_path.stat().st_size > 20


def test_engine_compute_compiler_batches_generated_particle_stages():
    emitter = _gpu_source().emitters[0]
    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-test"
    )

    assert set(compiled) == set(emitter.stages())
    for spirv in compiled.values():
        assert isinstance(spirv, bytes)
        assert len(spirv) > 20
        assert int.from_bytes(spirv[:4], "little") == 0x07230203


def test_persisted_particle_spirv_is_compressed_and_integrity_checked():
    source = _gpu_source()
    payload = compile_gpu_particle_spirv(source)

    assert validate_gpu_particle_spirv(payload, source) is payload
    descriptor = payload["emitters"][0]["stages"]["update"]
    assert descriptor["byte_size"] > 20
    assert len(descriptor["sha256"]) == 64
    assert len(descriptor["zlib_base64"]) < descriptor["byte_size"] * 2

    corrupted = copy.deepcopy(payload)
    corrupted["emitters"][0]["stages"]["update"]["sha256"] = "0" * 64
    with pytest.raises(GpuParticleCompileError, match="integrity validation"):
        validate_gpu_particle_spirv(corrupted, source)
