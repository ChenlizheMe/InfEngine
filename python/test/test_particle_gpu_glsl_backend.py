from __future__ import annotations

import copy
from pathlib import Path
import re
import shutil
import struct
import subprocess

import pytest

import Infernux.particle.gpu_glsl_backend as gpu_backend

from Infernux.lib import _Infernux as native
from Infernux.particle import (
    EmitterSettings,
    EmitterShape,
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
    ParticleParameter,
    ParticleKernelLowerer,
    MeshEmissionMode,
    SdfVolume,
    VectorField,
    build_gpu_particle_migration,
    compile_gpu_particle_spirv,
    standard_particle_attributes,
    validate_gpu_particle_spirv,
    pack_gpu_particle_parameters,
    pack_gpu_particle_event_payload,
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


def _scene_collision_asset():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "scene-collision",
                "particle.update.collide_scene",
                properties={
                    "particle_radius": 0.125,
                    "layer_mask": 5,
                    "include_triggers": True,
                    "restitution_scale": 0.75,
                    "friction_scale": 0.5,
                },
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream",
                "root.update",
                "out",
                "scene-collision",
                "in",
                PortKind.STREAM,
            ),
        ),
    )
    return ParticleGraphAsset(
        stable_id="scene-collision-gpu",
        emitters=(ParticleEmitterAsset(update=update),),
    )


def _if_asset():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("condition", "common.constant.bool", properties={"value": True}),
            GraphNodeRecord("if", "particle.control.if"),
            GraphNodeRecord("true-size", "particle.attribute.set_size", properties={"value": 2.0}),
            GraphNodeRecord("false-size", "particle.attribute.set_size", properties={"value": 0.5}),
        ),
        links=(
            GraphLinkRecord("root-if", "root.update", "out", "if", "in", PortKind.STREAM),
            GraphLinkRecord("condition-if", "condition", "value", "if", "condition"),
            GraphLinkRecord("if-true", "if", "true", "true-size", "in", PortKind.STREAM),
            GraphLinkRecord("if-false", "if", "false", "false-size", "in", PortKind.STREAM),
        ),
    )
    return ParticleGraphAsset(
        stable_id="if-gpu",
        emitters=(ParticleEmitterAsset(update=update),),
    )


def _collision_lifecycle_asset():
    collision_enter = GraphDocument(
        "particle.collision_enter",
        nodes=(
            GraphNodeRecord("root.collision_enter", "particle.root.collision_enter"),
            GraphNodeRecord(
                "enter-size",
                "particle.attribute.set_size",
                properties={"value": 2.0},
            ),
        ),
        links=(
            GraphLinkRecord(
                "enter-exec",
                "root.collision_enter",
                "out",
                "enter-size",
                "in",
                PortKind.STREAM,
            ),
        ),
    )
    return ParticleGraphAsset(
        stable_id="collision-lifecycle-gpu",
        emitters=(
            ParticleEmitterAsset(
                settings=EmitterSettings(collision_enabled=True),
                collision_enter=collision_enter,
            ),
        ),
    )


def _wait_asset(*, fork: bool = False):
    nodes = [
        GraphNodeRecord("root.update", "particle.root.update"),
        GraphNodeRecord(
            "frames", "common.constant.i32", properties={"value": 3}
        ),
        GraphNodeRecord("wait", "particle.control.wait_frames"),
        GraphNodeRecord(
            "tail", "particle.attribute.set_size", properties={"value": 2.0}
        ),
    ]
    links = [
        GraphLinkRecord(
            "root-wait", "root.update", "out", "wait", "in", PortKind.STREAM
        ),
        GraphLinkRecord(
            "frames-wait", "frames", "value", "wait", "frames", PortKind.VALUE
        ),
        GraphLinkRecord(
            "wait-tail", "wait", "out", "tail", "in", PortKind.STREAM
        ),
    ]
    if fork:
        nodes.append(
            GraphNodeRecord(
                "sibling",
                "particle.attribute.set_position",
                properties={"value": [1.0, 2.0, 3.0]},
            )
        )
        links.append(
            GraphLinkRecord(
                "root-sibling",
                "root.update",
                "out",
                "sibling",
                "in",
                PortKind.STREAM,
            )
        )
    return ParticleGraphAsset(
        stable_id="wait-gpu-fork" if fork else "wait-gpu",
        emitters=(
            ParticleEmitterAsset(
                settings=EmitterSettings(capacity=512),
                update=GraphDocument(
                    "particle.update", nodes=tuple(nodes), links=tuple(links)
                ),
            ),
        ),
    )


def _two_wait_asset():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "frames", "common.constant.i32", properties={"value": 2}
            ),
            GraphNodeRecord(
                "seconds", "common.constant.f32", properties={"value": 0.25}
            ),
            GraphNodeRecord("wait.frames", "particle.control.wait_frames"),
            GraphNodeRecord("wait.seconds", "particle.control.wait_seconds"),
            GraphNodeRecord("tail", "particle.attribute.set_size"),
        ),
        links=(
            GraphLinkRecord(
                "root-frames",
                "root.update",
                "out",
                "wait.frames",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "frames-value",
                "frames",
                "value",
                "wait.frames",
                "frames",
                PortKind.VALUE,
            ),
            GraphLinkRecord(
                "frames-seconds",
                "wait.frames",
                "out",
                "wait.seconds",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "seconds-value",
                "seconds",
                "value",
                "wait.seconds",
                "seconds",
                PortKind.VALUE,
            ),
            GraphLinkRecord(
                "seconds-tail",
                "wait.seconds",
                "out",
                "tail",
                "in",
                PortKind.STREAM,
            ),
        ),
    )
    return ParticleGraphAsset(
        stable_id="two-waits-gpu",
        emitters=(ParticleEmitterAsset(update=update),),
    )


def _wait_join_asset():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("frames", "common.constant.i32", properties={"value": 2}),
            GraphNodeRecord("wait", "particle.control.wait_frames"),
            GraphNodeRecord(
                "left",
                "particle.attribute.set_size",
                properties={"value": 2.0},
            ),
            GraphNodeRecord(
                "right",
                "particle.attribute.set_position",
                properties={"value": [1.0, 2.0, 3.0]},
            ),
            GraphNodeRecord("join", "particle.control.join_all"),
            GraphNodeRecord(
                "tail",
                "particle.attribute.set_color",
                properties={"value": [0.25, 0.5, 0.75, 1.0]},
            ),
        ),
        links=(
            GraphLinkRecord("root-wait", "root.update", "out", "wait", "in", PortKind.STREAM),
            GraphLinkRecord("frames-wait", "frames", "value", "wait", "frames", PortKind.VALUE),
            GraphLinkRecord("wait-left", "wait", "out", "left", "in", PortKind.STREAM),
            GraphLinkRecord("left-join", "left", "out", "join", "in0", PortKind.STREAM),
            GraphLinkRecord("root-right", "root.update", "out", "right", "in", PortKind.STREAM),
            GraphLinkRecord("right-join", "right", "out", "join", "in1", PortKind.STREAM),
            GraphLinkRecord("join-tail", "join", "out", "tail", "in", PortKind.STREAM),
        ),
    )
    return ParticleGraphAsset(
        stable_id="wait-join-gpu",
        emitters=(ParticleEmitterAsset(settings=EmitterSettings(capacity=512), update=update),),
    )


def _dual_wait_join_asset():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("frames", "common.constant.i32", properties={"value": 2}),
            GraphNodeRecord("seconds", "common.constant.f32", properties={"value": 0.25}),
            GraphNodeRecord("wait.left", "particle.control.wait_frames"),
            GraphNodeRecord("wait.right", "particle.control.wait_seconds"),
            GraphNodeRecord("left", "particle.attribute.set_size", properties={"value": 2.0}),
            GraphNodeRecord(
                "right",
                "particle.attribute.set_position",
                properties={"value": [1.0, 2.0, 3.0]},
            ),
            GraphNodeRecord("join", "particle.control.join_all"),
            GraphNodeRecord(
                "tail",
                "particle.attribute.set_color",
                properties={"value": [0.25, 0.5, 0.75, 1.0]},
            ),
        ),
        links=(
            GraphLinkRecord("root-left", "root.update", "out", "wait.left", "in", PortKind.STREAM),
            GraphLinkRecord("frames-left", "frames", "value", "wait.left", "frames", PortKind.VALUE),
            GraphLinkRecord("left-tail", "wait.left", "out", "left", "in", PortKind.STREAM),
            GraphLinkRecord("left-join", "left", "out", "join", "in0", PortKind.STREAM),
            GraphLinkRecord("root-right", "root.update", "out", "wait.right", "in", PortKind.STREAM),
            GraphLinkRecord("seconds-right", "seconds", "value", "wait.right", "seconds", PortKind.VALUE),
            GraphLinkRecord("right-tail", "wait.right", "out", "right", "in", PortKind.STREAM),
            GraphLinkRecord("right-join", "right", "out", "join", "in1", PortKind.STREAM),
            GraphLinkRecord("join-tail", "join", "out", "tail", "in", PortKind.STREAM),
        ),
    )
    return ParticleGraphAsset(
        stable_id="dual-wait-join-gpu",
        emitters=(ParticleEmitterAsset(settings=EmitterSettings(capacity=512), update=update),),
    )


def _nested_wait_join_asset():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("frames.first", "common.constant.i32", properties={"value": 2}),
            GraphNodeRecord("frames.second", "common.constant.i32", properties={"value": 3}),
            GraphNodeRecord("wait.first", "particle.control.wait_frames"),
            GraphNodeRecord("first.left", "particle.attribute.set_size", properties={"value": 2.0}),
            GraphNodeRecord(
                "first.right",
                "particle.attribute.set_position",
                properties={"value": [1.0, 2.0, 3.0]},
            ),
            GraphNodeRecord("join.first", "particle.control.join_all"),
            GraphNodeRecord("wait.second", "particle.control.wait_frames"),
            GraphNodeRecord(
                "second.left",
                "particle.attribute.set_velocity",
                properties={"value": [0.0, 4.0, 0.0]},
            ),
            GraphNodeRecord(
                "second.right",
                "particle.attribute.set_color",
                properties={"value": [0.2, 0.4, 0.8, 1.0]},
            ),
            GraphNodeRecord("join.second", "particle.control.join_all"),
            GraphNodeRecord(
                "tail",
                "particle.attribute.set_rotation",
                properties={"value": 0.75},
            ),
        ),
        links=(
            GraphLinkRecord(
                "root-first-wait",
                "root.update",
                "out",
                "wait.first",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "first-frames-value",
                "frames.first",
                "value",
                "wait.first",
                "frames",
                PortKind.VALUE,
            ),
            GraphLinkRecord(
                "first-wait-left",
                "wait.first",
                "out",
                "first.left",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "first-left-join",
                "first.left",
                "out",
                "join.first",
                "in0",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "root-first-right",
                "root.update",
                "out",
                "first.right",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "first-right-join",
                "first.right",
                "out",
                "join.first",
                "in1",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "first-join-second-wait",
                "join.first",
                "out",
                "wait.second",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "second-frames-value",
                "frames.second",
                "value",
                "wait.second",
                "frames",
                PortKind.VALUE,
            ),
            GraphLinkRecord(
                "second-wait-left",
                "wait.second",
                "out",
                "second.left",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "second-left-join",
                "second.left",
                "out",
                "join.second",
                "in0",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "first-join-second-right",
                "join.first",
                "out",
                "second.right",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "second-right-join",
                "second.right",
                "out",
                "join.second",
                "in1",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "second-join-tail",
                "join.second",
                "out",
                "tail",
                "in",
                PortKind.STREAM,
            ),
        ),
    )
    return ParticleGraphAsset(
        stable_id="nested-wait-join-gpu",
        emitters=(
            ParticleEmitterAsset(
                settings=EmitterSettings(capacity=512),
                update=update,
            ),
        ),
    )


@pytest.mark.parametrize("fork", [False, True])
def test_gpu_wait_emits_bounded_continuation_program_and_valid_spirv(fork):
    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(_wait_asset(fork=fork))
    )
    source = GpuParticleGlslLowerer().lower(kernel)
    emitter = source.emitters[0]
    continuation = emitter.continuation

    assert continuation is not None
    assert continuation.record_stride == 64
    assert continuation.lane_count == 1
    assert continuation.join_count == 0
    assert "layout(std430, set = 5, binding = 0)" in emitter.update
    assert "inx_suspend_frames(" in emitter.update
    assert "case 1u:" in continuation.dispatch
    assert "state.a_builtin_size =" in continuation.dispatch
    if fork:
        assert "state.a_builtin_position =" in emitter.update
        assert emitter.update.count("_active") >= 2

    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)
    encoded = compiled["emitters"][0]["continuation"]
    assert encoded is not None
    assert set(encoded["stages"]) == {"prepare", "classify", "dispatch"}
    decoded = gpu_backend.decode_gpu_particle_spirv(compiled, 0)["continuation"]
    assert decoded is not None
    assert decoded["record_stride"] == 64
    assert all(decoded["stages"][stage] for stage in encoded["stages"])


def test_gpu_sequential_wait_reuses_the_current_continuation_record():
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(
            ParticleGraphCompiler().compile(_two_wait_asset())
        )
    )
    continuation = source.emitters[0].continuation
    assert continuation is not None
    assert continuation.lane_count == 1
    assert "case 1u:" in continuation.dispatch
    assert "case 2u:" in continuation.dispatch
    assert "inx_continuation_record_index" in continuation.dispatch
    assert "inx_continuation_resuspended = true" in continuation.dispatch
    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def test_gpu_wait_crosses_join_with_persistent_branch_arrival_state():
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(
            ParticleGraphCompiler().compile(_wait_join_asset())
        )
    )
    emitter = source.emitters[0]
    continuation = emitter.continuation

    assert continuation is not None
    assert continuation.lane_count == 1
    assert continuation.join_count == 1
    assert "inx_continuation_lane_pending" in emitter.update
    assert "inx_continuation_join_has_arrived" in emitter.update
    assert "inx_continuation_join_arrive" in emitter.update
    assert "continuation_record_words[base + 8u] = branch_token" in emitter.update
    assert "inx_continuation_record_branch_token" in continuation.dispatch
    assert "inx_continuation_record_join_index" in continuation.dispatch
    assert "inx_continuation_join_arrive" in continuation.dispatch
    assert "state.a_builtin_color =" in continuation.dispatch

    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def test_gpu_join_resumes_only_after_both_waiting_branches_arrive():
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(
            ParticleGraphCompiler().compile(_dual_wait_join_asset())
        )
    )
    emitter = source.emitters[0]
    continuation = emitter.continuation

    assert continuation is not None
    assert continuation.lane_count == 2
    assert continuation.join_count == 1
    assert continuation.dispatch.count("case ") == 2
    assert continuation.dispatch.count("inx_continuation_join_arrive(") >= 3
    assert "? 1u : 0u" in continuation.dispatch
    assert "? 2u : 0u" in continuation.dispatch

    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def test_gpu_nested_wait_joins_move_the_continuation_to_the_next_join():
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(
            ParticleGraphCompiler().compile(_nested_wait_join_asset())
        )
    )
    emitter = source.emitters[0]
    continuation = emitter.continuation

    assert continuation is not None
    assert continuation.lane_count == 2
    assert continuation.join_count == 2
    assert continuation.dispatch.count("case ") == 2
    assert "state.a_builtin_velocity =" in continuation.dispatch
    assert "state.a_builtin_rotation =" in continuation.dispatch
    assert "inx_continuation_record_join_index == 0u" in continuation.dispatch
    assert "inx_continuation_record_join_index == 1u" in continuation.dispatch

    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def test_particle_if_lowers_to_guarded_gpu_branches_and_valid_spirv():
    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(_if_asset()))
    source = GpuParticleGlslLowerer().lower(kernel)

    update = source.emitters[0].stages()["update"]
    assert update.count("if (") >= 2
    assert update.count("state.a_builtin_size =") == 2
    assert "// true-size" in update
    assert "// false-size" in update
    compiled = compile_gpu_particle_spirv(source)
    assert compiled["emitters"][0]["stages"]["update"]["byte_size"] > 0


def test_collision_lifecycle_root_lowers_to_gpu_mask_and_valid_spirv():
    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(_collision_lifecycle_asset())
    )
    source = GpuParticleGlslLowerer().lower(kernel)
    update = source.emitters[0].stages()["update"]

    assert "inx_collide_scene(" in update
    assert "state.a_builtin_collision_active" in update
    assert "// enter-size" in update
    assert "if (" in update
    compiled = compile_gpu_particle_spirv(source)
    assert compiled["emitters"][0]["stages"]["update"]["byte_size"] > 0


def test_scene_collision_uses_shared_grid_abi_and_compiles_to_spirv():
    hir = ParticleGraphCompiler().compile(_scene_collision_asset())
    operation = hir.emitters[0].update.operations[-1]
    assert operation.opcode == "collision.scene"
    assert operation.parameter_dict() == {
        "friction_scale": 0.5,
        "include_triggers": True,
        "layer_mask": 5,
        "particle_radius": 0.125,
        "restitution_scale": 0.75,
    }

    kernel = ParticleKernelLowerer().lower(hir)
    instruction = next(
        value
        for value in kernel.emitters[0].update.instructions
        if value.opcode == "collide_scene"
    )
    assert instruction.result_id == ""
    assert [operand.value_type.value_type for operand in instruction.operands] == [
        ValueType.VEC3,
        ValueType.VEC3,
        ValueType.F32,
        ValueType.U32,
        ValueType.BOOL,
        ValueType.F32,
        ValueType.F32,
    ]
    assert instruction.immediate_dict() == {
        "hit_attribute": "",
        "normal_attribute": "",
        "position_attribute": "builtin.position",
        "velocity_attribute": "builtin.velocity",
    }

    source = GpuParticleGlslLowerer().lower(kernel)
    update_source = source.emitters[0].update
    assert "struct InxParticleAffine" in update_source
    assert "vec4 row0;" in update_source
    assert "vec4 row1;" in update_source
    assert "vec4 row2;" in update_source
    assert "InxParticleAffine collider_to_world;" in update_source
    assert "InxParticleAffine world_to_collider;" in update_source
    assert "InxParticleAffine previous_world_to_collider;" in update_source
    assert "uvec4 geometry;" in update_source
    assert "mat4 collider_to_world;" not in update_source
    assert "mat4 world_to_collider;" not in update_source
    assert "inx_particle_affine_point" in update_source
    assert "inx_particle_affine_linear" in update_source
    collider_fields = (
        "vec4 material;",
        "vec4 world_aabb_min;",
        "vec4 world_aabb_max;",
        "uvec4 metadata;",
        "uvec4 identity;",
    )
    assert [update_source.index(field) for field in collider_fields] == sorted(
        update_source.index(field) for field in collider_fields
    )
    assert "binding = 10" in update_source
    assert "binding = 11" in update_source
    assert "binding = 12" in update_source
    assert "binding = 13" in update_source
    assert "binding = 14" in update_source
    assert "inx_collide_scene(" in update_source
    assert "bool inx_collide_scene(" in update_source
    assert "out vec3 simulation_collision_normal" in update_source
    assert "particle_collision_grid_offsets[cell_index + 1u]" in update_source
    assert "bool inx_sweep_box(" in update_source
    assert "bool inx_sweep_sphere(" in update_source
    assert "bool inx_sweep_capsule(" in update_source
    assert "bool inx_sweep_triangle(" in update_source
    assert "bool inx_collision_mesh(" in update_source
    assert "particle_collision_mesh_bvh[node_index]" in update_source
    assert "collider.metadata.x == 3u" in update_source
    assert "vec3 collider_swept_min" in update_source
    assert "collider.previous_world_aabb_min.xyz" in update_source
    assert "collider.previous_world_aabb_max.xyz" in update_source
    assert "vec3 particle_swept_min" in update_source
    assert "collider.previous_world_to_collider" in update_source
    assert "previous_world_position + collider_displacement" not in update_source
    assert update_source.index("inx_sweep_box(local_previous") < update_source.index(
        "else if (all(lessThanEqual(abs(local_position), half_extent)))"
    )

    payload = compile_gpu_particle_spirv(source)
    assert validate_gpu_particle_spirv(payload, source) is payload


def test_scene_collision_event_payload_reads_post_collision_state():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("collision", "particle.update.collide_scene"),
            GraphNodeRecord(
                "position",
                "particle.attribute.get",
                properties={"attribute": "builtin.position"},
            ),
            GraphNodeRecord(
                "collision.hit",
                "particle.attribute.get",
                properties={"attribute": "builtin.collision_hit"},
            ),
            GraphNodeRecord(
                "collision.normal",
                "particle.attribute.get",
                properties={"attribute": "builtin.collision_normal"},
            ),
            GraphNodeRecord(
                "impact.output",
                particle_event_output_type_id("impact-route", "update"),
                properties={"condition": True},
            ),
        ),
        links=(
            GraphLinkRecord(
                "collision.stream",
                "root.update",
                "out",
                "collision",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "event.stream",
                "collision",
                "out",
                "impact.output",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "event.condition",
                "collision.hit",
                "value",
                "impact.output",
                "condition",
                PortKind.VALUE,
            ),
            GraphLinkRecord(
                "event.position",
                "position",
                "value",
                "impact.output",
                particle_event_payload_port_id("position"),
                PortKind.VALUE,
            ),
            GraphLinkRecord(
                "event.normal",
                "collision.normal",
                "value",
                "impact.output",
                particle_event_payload_port_id("normal"),
                PortKind.VALUE,
            ),
        ),
    )
    source_emitter = ParticleEmitterAsset(stable_id="source", update=update)
    target_emitter = ParticleEmitterAsset(stable_id="target")
    event_type = ParticleEventType(
        "impact",
        "Impact",
        64,
        (
            ParticleEventField(
                "position",
                "Position",
                TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
                [0.0, 0.0, 0.0],
            ),
            ParticleEventField(
                "normal",
                "Normal",
                TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
                [0.0, 1.0, 0.0],
            ),
        ),
    )
    graph = ParticleGraphAsset(
        stable_id="collision-event-order",
        emitters=(source_emitter, target_emitter),
        event_types=(event_type,),
        event_routes=(
            ParticleEventRoute(
                "impact-route",
                "impact",
                "source",
                "update",
                "target",
            ),
        ),
    )

    hir = ParticleGraphCompiler().compile(graph)
    assert [operation.opcode for operation in hir.emitters[0].update.operations] == [
        "collision.scene",
        "event.emit",
    ]
    kernel = ParticleKernelLowerer().lower(hir)
    attribute_ids = {attribute[0] for attribute in kernel.emitters[0].attributes}
    assert "builtin.collision_hit" in attribute_ids
    assert "builtin.collision_normal" in attribute_ids
    instructions = kernel.emitters[0].update.instructions
    collision_index = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opcode == "collide_scene"
    )
    event_index = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opcode == "event_append"
    )
    post_collision_position_loads = [
        index
        for index, instruction in enumerate(instructions)
        if instruction.opcode == "load_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.position"
        and collision_index < index < event_index
    ]
    assert post_collision_position_loads
    post_collision_hit_loads = [
        index
        for index, instruction in enumerate(instructions)
        if instruction.opcode == "load_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.collision_hit"
        and collision_index < index < event_index
    ]
    post_collision_normal_loads = [
        index
        for index, instruction in enumerate(instructions)
        if instruction.opcode == "load_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.collision_normal"
        and collision_index < index < event_index
    ]
    assert post_collision_hit_loads
    assert post_collision_normal_loads
    collision_instruction = instructions[collision_index]
    assert collision_instruction.immediate_dict()["hit_attribute"] == "builtin.collision_hit"
    assert (
        collision_instruction.immediate_dict()["normal_attribute"]
        == "builtin.collision_normal"
    )
    round_trip = type(kernel).from_dict(kernel.to_dict())
    assert round_trip == kernel
    source = GpuParticleGlslLowerer().lower(kernel).emitters[0].update
    assert source.index("inx_collide_scene(") < source.index(
        "event_output_record_words["
    )
    assert "bool inx_scene_hit_" in source
    assert "!= 0u || inx_scene_hit_" in source
    assert "if (inx_scene_hit_" in source


def test_gpu_parameters_use_one_stable_uvec4_slot_and_typed_loads():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "wind",
                "particle.parameter.get",
                properties={"parameter": "wind"},
            ),
            GraphNodeRecord("accelerate", "particle.update.acceleration"),
        ),
        links=(
            GraphLinkRecord("stream", "root.update", "out", "accelerate", "in", PortKind.STREAM),
            GraphLinkRecord("value", "wind", "value", "accelerate", "value", PortKind.VALUE),
        ),
    )
    asset = ParticleGraphAsset(
        parameters=(
            ParticleParameter(
                "wind", "Wind", TypeRef(ValueType.VEC3), [1.0, 2.0, 3.0]
            ),
        ),
        emitters=(ParticleEmitterAsset(update=update),),
    )
    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    source = GpuParticleGlslLowerer().lower(kernel)

    assert len(pack_gpu_particle_parameters(kernel.parameters)) == 4
    assert pack_gpu_particle_parameters(
        kernel.parameters, {"wind": [4.0, 5.0, 6.0]}
    ) != pack_gpu_particle_parameters(kernel.parameters)
    assert "binding = 7" in source.emitters[0].update
    assert "uintBitsToFloat(parameter_words[0].xyz)" in source.emitters[0].update


def test_external_event_payload_uses_compiled_field_word_layout():
    asset = ParticleGraphAsset(
        event_types=(
            ParticleEventType(
                "impact",
                "Impact",
                4,
                (
                    ParticleEventField(
                        "enabled", "Enabled", TypeRef(ValueType.BOOL), True
                    ),
                    ParticleEventField(
                        "direction",
                        "Direction",
                        TypeRef(ValueType.VEC3),
                        [0.0, 1.0, 0.0],
                    ),
                ),
            ),
        ),
        event_routes=(
            ParticleEventRoute(
                "impact-route", "impact", "source", "update", "target", 1
            ),
        ),
        emitters=(
            ParticleEmitterAsset(stable_id="source"),
            ParticleEmitterAsset(stable_id="target"),
        ),
    )
    event_type = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(asset)
    ).events.event_types[0]
    words = pack_gpu_particle_event_payload(
        event_type,
        {"enabled": False, "direction": [1.0, 2.0, 3.0]},
    )
    assert words[0] == 0
    assert words[1:] == tuple(
        struct.unpack("<I", struct.pack("<f", value))[0]
        for value in (1.0, 2.0, 3.0)
    )
    with pytest.raises(GpuParticleCompileError, match="unknown fields"):
        pack_gpu_particle_event_payload(event_type, {"missing": 1.0})


def test_gpu_texture2d_parameter_lowers_to_rhi_resource_and_sample():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-color", "particle.attribute.set_color"),
            GraphNodeRecord(
                "texture",
                "particle.parameter.get",
                properties={"parameter": "smoke-texture"},
            ),
            GraphNodeRecord(
                "uv",
                "common.constant.vec2",
                properties={"value": [0.25, 0.75]},
            ),
            GraphNodeRecord("sample", "common.texture.sample2d"),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "set-color", "in", PortKind.STREAM
            ),
            GraphLinkRecord(
                "texture-value", "texture", "value", "sample", "texture", PortKind.VALUE
            ),
            GraphLinkRecord("uv-value", "uv", "value", "sample", "uv", PortKind.VALUE),
            GraphLinkRecord(
                "sample-color", "sample", "color", "set-color", "value", PortKind.VALUE
            ),
        ),
    )
    default = AssetReference("smoke-guid", "Assets/VFX/Smoke.png")
    asset = ParticleGraphAsset(
        parameters=(
            ParticleParameter(
                "smoke-texture",
                "Smoke Texture",
                TypeRef(ValueType.TEXTURE2D),
                default.to_dict(),
            ),
        ),
        emitters=(ParticleEmitterAsset(update=update),),
    )
    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    source = GpuParticleGlslLowerer().lower(kernel)
    emitter = source.emitters[0]

    assert pack_gpu_particle_parameters(kernel.parameters) == (0, 0, 0, 0)
    assert emitter.data_interface_layout["texture2d_parameters"] == [
        {
            "stable_id": "smoke-texture",
            "name": "Smoke Texture",
            "parameter_slot": 0,
            "resource_index": 0,
            "texture_binding": 1,
            "default": default.to_dict(),
        }
    ]
    assert (
        "layout(set = 2, binding = 1) uniform sampler2D inx_parameter_texture_0;"
        in emitter.update
    )
    assert "texture(inx_parameter_texture_0," in emitter.update
    payload = compile_gpu_particle_spirv(source)
    assert validate_gpu_particle_spirv(payload, source) is payload


def _kill_if_gpu_source():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("kill", "particle.update.kill_if"),
            GraphNodeRecord(
                "age",
                "particle.attribute.get",
                properties={"attribute": "builtin.age"},
            ),
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


def test_gpu_backend_lowers_vector_compose_and_zero_extended_math_inputs():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "xy", "common.vector.compose2", properties={"x": 1.0, "y": 2.0}
            ),
            GraphNodeRecord(
                "position",
                "particle.attribute.get",
                properties={"attribute": "builtin.position"},
            ),
            GraphNodeRecord("add", "common.math.add"),
            GraphNodeRecord("acceleration", "particle.update.acceleration"),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "acceleration", "in", PortKind.STREAM
            ),
            GraphLinkRecord("xy", "xy", "value", "add", "a"),
            GraphLinkRecord("position", "position", "value", "add", "b"),
            GraphLinkRecord("result", "add", "result", "acceleration", "value"),
        ),
    )
    asset = ParticleGraphAsset(
        emitters=(ParticleEmitterAsset(stable_id="vector-resize", update=update),)
    )
    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    opcodes = [instruction.opcode for instruction in kernel.emitters[0].update.instructions]

    assert "compose_vec2" in opcodes
    assert "numeric_resize" in opcodes
    assert "add" in opcodes

    source = GpuParticleGlslLowerer().lower(kernel).emitters[0].update
    assert re.search(r"vec2\([^\n]+\)", source)
    assert re.search(r"vec3\(v\d+, 0\.0\)", source)


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
    assert "layout(std430, set = 4, binding = 1)" in source.update
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
            GraphNodeRecord(
                "position",
                "particle.attribute.get",
                properties={"attribute": "builtin.position"},
            ),
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
    assert "instances[particle_index].ribbon_data = uvec4(" in emitter.rendering
    assert "state.a_builtin_ribbon_strip_id" in emitter.rendering
    assert "state.a_builtin_ribbon_order" in emitter.rendering
    assert "state.a_builtin_ribbon_break" in emitter.rendering
    payload = compile_gpu_particle_spirv(source)
    assert validate_gpu_particle_spirv(payload, source) is payload


def test_gpu_sprite_flipbook_exports_frame_and_remaps_atlas_uvs():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "flipbook",
                "particle.attribute.set_flipbook_frame",
                properties={"value": 5.0},
            ),
        ),
        links=(
            GraphLinkRecord(
                "init-stream", "root.init", "out", "flipbook", "in", PortKind.STREAM
            ),
        ),
    )
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord(
                "sprite",
                "particle.output.sprite",
                properties={
                    "flipbook_columns": 4,
                    "flipbook_rows": 2,
                    "alignment": "velocity",
                },
            ),
        ),
        links=(
            GraphLinkRecord(
                "render-stream", "root.rendering", "out", "sprite", "in", PortKind.STREAM
            ),
        ),
    )
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(init=init, rendering=rendering),))
    )
    source = GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))
    emitter = source.emitters[0]

    assert "a_builtin_flipbook_frame" in emitter.init
    assert "instances[particle_index].custom_data = vec4(" in emitter.rendering
    assert "instance.custom_data.x" in gpu_backend._BILLBOARD_VERTEX_GLSL
    assert "instance.custom_data.yzw" in gpu_backend._BILLBOARD_VERTEX_GLSL
    assert "view.alignment_reference.w" in gpu_backend._BILLBOARD_VERTEX_GLSL
    assert "state.a_builtin_velocity" in emitter.rendering
    assert "render_indices[output_index] = particle_index" in emitter.rendering
    assert "transforms.simulation_to_world * vec4(" in emitter.rendering
    assert "view.rendering_control.zw" in gpu_backend._BILLBOARD_VERTEX_GLSL
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




def _vector_field_gpu_source(*, boundary="zero", filtering="linear"):
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("acceleration", "particle.update.acceleration"),
            GraphNodeRecord(
                "position",
                "particle.attribute.get",
                properties={"attribute": "builtin.position"},
            ),
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


def _sdf_gpu_source():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "collision",
                "particle.update.collide_sdf",
                properties={
                    "interface": "collision-field",
                    "particle_radius": 0.05,
                    "restitution": 0.5,
                    "friction": 0.2,
                },
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.STREAM
            ),
        ),
    )
    emitter = ParticleEmitterAsset(
        stable_id="sdf-emitter",
        update=update,
        data_interfaces=(
            SdfVolume(
                stable_id="collision-field",
                texture=AssetReference(guid="sdf-texture-guid"),
            ),
        ),
    )
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(stable_id="sdf-gpu", emitters=(emitter,))
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


def test_gpu_lowerer_emits_normalized_age_lerp_rotation_and_attribute_stores():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-color", "particle.attribute.set_color"),
            GraphNodeRecord("set-size", "particle.attribute.set_size"),
            GraphNodeRecord("set-rotation", "particle.attribute.set_rotation"),
            GraphNodeRecord("normalized-age", "particle.attribute.normalized_age"),
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
            GraphLinkRecord("color-a", "start-color", "value", "color-over-life", "a"),
            GraphLinkRecord("color-b", "end-color", "value", "color-over-life", "b"),
            GraphLinkRecord("color-t", "normalized-age", "value", "color-over-life", "t"),
            GraphLinkRecord("size-t", "normalized-age", "value", "size-over-life", "t"),
            GraphLinkRecord(
                "rotation-t", "normalized-age", "value", "rotation-over-life", "t"
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

    assert "clamp(" in source
    assert " / max(" in source
    assert "mix(" in source
    assert ".a_builtin_color = " in source
    assert ".a_builtin_size = " in source
    assert ".a_builtin_rotation = " in source

    rendering = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(hir)
    ).emitters[0].rendering
    assert "rotation_custom = vec4(" in rendering
    assert ".a_builtin_rotation" in rendering
    assert "float normalized_age = clamp(" in rendering
    assert ".a_builtin_age" in rendering
    assert ".a_builtin_lifetime" in rendering
    assert "scale_custom = vec4(" in rendering
    assert ", normalized_age);" in rendering


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
            GraphNodeRecord(
                "age",
                "particle.attribute.get",
                properties={"attribute": "builtin.age"},
            ),
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




@pytest.mark.parametrize("mode", tuple(MeshEmissionMode))
def test_gpu_mesh_shape_uses_shared_mesh_buffers_and_compiles(mode):
    mesh = AssetReference(
        guid="mesh-guid", path_hint="Assets/Models/emission-source.fbx"
    )
    asset = ParticleGraphAsset(
        emitters=(
            ParticleEmitterAsset(
                stable_id="mesh-shape",
                settings=EmitterSettings(
                    shape=EmitterShape(kind="mesh", mesh=mesh, mesh_mode=mode)
                ),
            ),
        )
    )
    hir = ParticleGraphCompiler().compile(asset)
    emitter = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(hir)
    ).emitters[0]

    assert emitter.data_interface_layout["mesh_shape"] == {
        "mesh": mesh.to_dict(),
        "mode": mode.value,
        "metadata_offset": 0,
        "vertex_binding": 14,
        "triangle_binding": 15,
    }
    assert "set = 1, binding = 14" in emitter.init
    assert "set = 1, binding = 15" in emitter.init
    assert "inx_sample_mesh_shape_position" in emitter.init
    if mode is MeshEmissionMode.SURFACE:
        assert "uintBitsToFloat" in emitter.init
    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), f"particle-mesh-shape-{mode.value}"
    )
    assert set(compiled) == set(emitter.stages())


def test_gpu_vector_field_lowering_emits_rhi_set_two_layout_and_valid_spirv():
    emitter = _vector_field_gpu_source().emitters[0]
    layout = emitter.data_interface_layout

    assert layout["volume_metadata_binding"] == 0
    assert layout["volume_stride_words"] == 32
    assert layout["volume_interfaces"] == [
        {
            "kind": "vector_field",
            "stable_id": "wind",
            "interface_index": 0,
            "texture_binding": 1,
            "boundary": "zero",
            "filtering": "linear",
        }
    ]
    assert "set = 2, binding = 0" in emitter.update
    assert "set = 2, binding = 1" in emitter.update
    assert "uniform sampler3D inx_volume_texture_0" in emitter.update
    assert "any(lessThan(uvw" in emitter.update
    assert "inx_sample_vector_field_0" in emitter.update

    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-vector-field-test"
    )
    assert set(compiled) == set(emitter.stages())


def test_gpu_vector_field_repeat_nearest_policy_is_preserved_for_rhi_sampler():
    emitter = _vector_field_gpu_source(boundary="repeat", filtering="nearest").emitters[0]
    interface = emitter.data_interface_layout["volume_interfaces"][0]

    assert interface["boundary"] == "repeat"
    assert interface["filtering"] == "nearest"
    assert "any(lessThan(uvw" not in emitter.update


def test_gpu_sdf_collision_uses_shared_volume_set_and_compiles_to_spirv():
    emitter = _sdf_gpu_source().emitters[0]
    layout = emitter.data_interface_layout

    assert layout["volume_interfaces"] == [
        {
            "kind": "sdf",
            "stable_id": "collision-field",
            "interface_index": 0,
            "texture_binding": 1,
            "filtering": "linear",
        }
    ]
    assert "inx_sample_sdf_0" in emitter.update
    assert "inx_collide_sdf_position_0" in emitter.update
    assert "textureSize(inx_volume_texture_0" in emitter.update
    assert "field_position + vec3(0.5)" in emitter.update
    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-sdf-test"
    )
    assert set(compiled) == set(emitter.stages())


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
