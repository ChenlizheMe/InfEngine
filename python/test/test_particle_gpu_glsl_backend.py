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
    ParticleEventFlow,
    ParticleEventType,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleScriptCompiler,
    ParticleParameter,
    ParticleKernelLowerer,
    MeshEmissionMode,
    SdfVolume,
    VectorField,
    build_gpu_particle_migration,
    compile_gpu_particle_spirv,
    default_event_graph,
    standard_particle_attributes,
    validate_gpu_particle_spirv,
    pack_gpu_particle_parameters,
)
from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
from Infernux.graph.types import AssetReference, CoordinateSpace, TypeRef, ValueType
from Infernux.graph.ramp import Curve, CurveKey, Gradient, GradientKey
from Infernux.particle.nodes import (
    PARTICLE_EVENT_ACTIVE_TYPE_ID,
    PARTICLE_EVENT_TRIGGER_TYPE_ID,
    particle_event_payload_port_id,
)
from Infernux.particle.asset import particle_attribute_cache_id


def _gpu_source():
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(stable_id="gpu-particle")
    )
    kernel = ParticleKernelLowerer().lower(hir)
    return GpuParticleGlslLowerer().lower(kernel)


def _assert_generated_values_are_declared(glsl: str) -> None:
    value_names = set(re.findall(r"\bv\d+\b", glsl))
    declarations = set(
        re.findall(
            r"\b(?:bool|int|uint|float|[biu]?vec[234]|mat[234])\s+(v\d+)\b",
            glsl,
        )
    )
    assert value_names <= declarations


def _assert_all_generated_values_are_declared(source) -> None:
    for emitter in source.emitters:
        for glsl in emitter.stages().values():
            _assert_generated_values_are_declared(glsl)
        if emitter.continuation is not None:
            for glsl in emitter.continuation.stages().values():
                _assert_generated_values_are_declared(glsl)


def test_gpu_fused_update_rendering_stage_has_structured_symbols_and_workgroup_compaction():
    emitter = _gpu_source().emitters[0]
    fused = emitter.stages()["update_rendering_fused"]

    assert emitter.update_render_fusion["eligible"] is True
    assert emitter.update_render_fusion["fused_stage"] == "update_rendering_fused"
    assert set(emitter.stages()) == {
        "bootstrap",
        "init",
        "update",
        "contact_prepare",
        "contact_solve",
        "contact_dispatch",
        "render_reset",
        "rendering",
        "update_rendering_fused",
    }
    assert "shared uint inx_particle_render_local_count;" in fused
    assert "shared uint inx_particle_render_group_base;" in fused
    assert len(re.findall(r"atomicAdd\s*\(\s*counters\.visible_count", fused)) == 1
    assert len(re.findall(r"atomicAdd\s*\(\s*indirect_args\.instance_count", fused)) == 1
    # Alive-list and render export share one subgroup prefix pass.  Keeping
    # this at two barriers is the performance contract for the fused kernel.
    fused_main = fused.rsplit("void main()", 1)[1]
    assert fused_main.count("barrier();") == 2
    assert "subgroupBallot(inx_particle_active_candidate)" in fused
    assert "subgroupBallot(inx_particle_render_candidate)" in fused
    assert "subgroupBallotExclusiveBitCount" in fused
    assert "inx_alive_index(pc.alive_read_slot, invocation)" in fused
    assert "inx_store_alive(pc.alive_write_slot" in fused
    assert "update_v" in fused and "render_v" in fused
    assert fused.index("update_v") < fused.index("render_v")
    assert "ParticleVisibilityInstance" in fused
    assert "visibility[particle_index].position_radius = vec4(" in fused

    render_body_start = fused.index("render_v")
    render_finite = fused.rindex("particle_alive = particle_alive &&")
    assert "states[particle_index] = state;" not in fused[render_body_start:render_finite]
    assert fused.count("inx_push_free(particle_index)") == 1


def test_gpu_noneligible_emitter_keeps_only_fallback_stages():
    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(_scene_collision_asset())
    )
    emitter = GpuParticleGlslLowerer().lower(kernel).emitters[0]

    assert emitter.update_render_fusion["eligible"] is False
    assert emitter.update_render_fusion["fused_stage"] == ""
    assert "update_rendering_fused" not in emitter.stages()


def test_gpu_nonfused_particle_recycling_has_one_stage_owner():
    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(_scene_collision_asset())
    )
    emitter = GpuParticleGlslLowerer().lower(kernel).emitters[0]

    # Contact and Rendering can invalidate a particle after Update has already
    # compacted the current alive list. The following Update owns recycling so
    # the same slot cannot be pushed twice by independent stages.
    assert "state.lifecycle_flags &= ~INX_PARTICLE_ALIVE" in emitter.contact_dispatch
    assert "if (!particle_alive && (particle_was_alive || pc.use_alive_list != 0u))" in emitter.update
    assert emitter.update.count("inx_push_free(particle_index)") == 1
    assert "inx_push_free(particle_index)" not in emitter.rendering


def test_particle_script_until_compiles_to_valid_gpu_continuations():
    source_text = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class TimedMotion(ParticleScript):
    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_lifetime(8.0)

        def update(self, ctx, particles):
            particles.add_velocity((0.0, -1.0, 0.0))
            ctx.until_seconds(3.0)
            particles.add_velocity((1.0, 1.0, 0.0))
            ctx.until_frames(5)

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    hir = ParticleScriptCompiler().compile(
        source_text,
        source_name="TimedMotion.particle.py",
    )
    kernel = ParticleKernelLowerer().lower(hir)
    source = GpuParticleGlslLowerer().lower(kernel)
    assert source.emitters[0].collision_enabled is False
    update = source.emitters[0].update

    assert "inx_until_seconds(" in update
    assert "inx_until_frames(" in update
    assert update.count("state.a_builtin_velocity =") >= 2
    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def test_target_position_motion_compiles_to_gpu_without_hidden_target_storage():
    source_text = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class TargetMotion(ParticleScript):
    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_lifetime(4.0)

        def update(self, ctx, particles):
            particles.target_position(
                (1.0, 2.0, 3.0),
                speed=7.0,
                responsiveness=9.0,
                arrival_radius=0.5,
            )

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(
            ParticleScriptCompiler().compile(
                source_text,
                source_name="TargetMotion.particle.py",
            )
        )
    )

    update = source.emitters[0].update
    assert "inx_target_position_velocity(" in update
    assert "a_target" not in update
    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def test_rendering_wait_uses_an_independent_gpu_timeline_and_keeps_exporting():
    source_text = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class RenderingTimeline(ParticleScript):
    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_lifetime(8.0)

        def update(self, ctx, particles):
            particles.add_velocity((0.0, 1.0, 0.0))

        def rendering(self, ctx, particles):
            particles.set_size(0.5)
            ctx.until_seconds(1.0)
            ctx.wait_frames(2)
            particles.sprite()
'''
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(
            ParticleScriptCompiler().compile(
                source_text,
                source_name="RenderingTimeline.particle.py",
            )
        )
    )
    emitter = source.emitters[0]

    assert "inx_until_seconds(" in emitter.rendering
    assert "inx_suspend_frames(" in emitter.rendering
    assert "state.rendering_resume_step != pc.simulation_step" in emitter.rendering
    assert "atomicAdd(counters.visible_count," in emitter.rendering
    assert "inx_particle_render_local_count" in emitter.rendering
    assert emitter.continuation is not None
    assert "state.rendering_resume_step = pc.simulation_step" in emitter.continuation.dispatch
    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def test_init_wait_gates_update_and_rendering_until_all_init_lanes_finish():
    source_text = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class DelayedBirth(ParticleScript):
    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_lifetime(8.0)
            ctx.wait_frames(3)
            particles.set_size(2.0)

        def update(self, ctx, particles):
            particles.add_velocity((0.0, 1.0, 0.0))

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(
            ParticleScriptCompiler().compile(
                source_text,
                source_name="DelayedBirth.particle.py",
            )
        )
    )
    emitter = source.emitters[0]

    assert "state.lifecycle_flags = INX_PARTICLE_ALIVE" in emitter.init
    assert "if (!inx_stage_suspended) state.lifecycle_flags |= INX_PARTICLE_INIT_COMPLETE" in emitter.init
    assert "(state.lifecycle_flags & INX_PARTICLE_INIT_COMPLETE) != 0u" in emitter.update
    assert "inx_store_alive(pc.alive_write_slot" in emitter.update
    assert "INX_PARTICLE_INIT_COMPLETE) != 0u" in emitter.rendering
    assert emitter.continuation is not None
    assert "state.lifecycle_flags |= INX_PARTICLE_INIT_COMPLETE" in emitter.continuation.dispatch
    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def test_simultaneous_init_wait_lanes_serialize_completion_and_release_gate():
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(
            ParticleGraphCompiler().compile(_dual_init_wait_asset())
        )
    )
    emitter = source.emitters[0]
    continuation = emitter.continuation

    assert continuation is not None
    assert continuation.lane_count == 2
    assert continuation.dispatch.count("case ") == 2
    assert continuation.dispatch.count("inx_continuation_lane_pending(") >= 2
    assert continuation.dispatch.count("state.lifecycle_flags |= INX_PARTICLE_INIT_COMPLETE") == 2
    assert "INX_PARTICLE_CONTINUATION_LOCK" in continuation.dispatch
    assert "inx_continuation_append_active(inx_continuation_record_index)" in continuation.dispatch
    _assert_all_generated_values_are_declared(source)

    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def test_particle_script_delta_time_compiles_to_explicit_gpu_uniform_math():
    source_text = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class Gravity(ParticleScript):
    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_lifetime(8.0)

        def update(self, ctx, particles):
            particles.add_velocity((0.0, -9.8, 0.0) * ctx.delta_time)

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(
            ParticleScriptCompiler().compile(
                source_text,
                source_name="Gravity.particle.py",
            )
        )
    )
    update = source.emitters[0].update
    assert "pc.delta_time" in update
    assert "state.a_builtin_velocity =" in update
    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def test_particle_script_if_else_with_wait_compiles_to_valid_gpu_continuations():
    source_text = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class ConditionalMotion(ParticleScript):
    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_lifetime(8.0)

        def update(self, ctx, particles):
            if particles.age < 1.0:
                particles.add_velocity((0.0, 1.0, 0.0))
                ctx.wait_frames(2)
            else:
                particles.multiply_size(0.5)
            particles.add_color((0.1, 0.2, 0.3, 0.0))

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    hir = ParticleScriptCompiler().compile(
        source_text,
        source_name="ConditionalMotion.particle.py",
    )
    kernel = ParticleKernelLowerer().lower(hir)
    source = GpuParticleGlslLowerer().lower(kernel)
    update = source.emitters[0].update

    assert "inx_suspend_frames(" in update
    assert "state.a_builtin_velocity =" in update
    assert "state.a_builtin_size =" in update
    assert update.count("state.a_builtin_color =") >= 2
    assert source.emitters[0].continuation is not None
    _assert_all_generated_values_are_declared(source)
    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def _scene_collision_asset():
    return ParticleGraphAsset(
        stable_id="scene-collision-gpu",
        emitters=(
            ParticleEmitterAsset(
                settings=EmitterSettings(collision_enabled=True),
            ),
        ),
    )


def _if_asset():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("condition", "common.constant.bool", properties={"value": True}),
            GraphNodeRecord("if", "particle.control.if"),
            GraphNodeRecord("true-size", "particle.attribute.size", properties={"value": 2.0}),
            GraphNodeRecord("false-size", "particle.attribute.size", properties={"value": 0.5}),
        ),
        links=(
            GraphLinkRecord("root-if", "root.update", "out", "if", "in", PortKind.EXEC),
            GraphLinkRecord("condition-if", "condition", "value", "if", "condition"),
            GraphLinkRecord("if-true", "if", "true", "true-size", "in", PortKind.EXEC),
            GraphLinkRecord("if-false", "if", "false", "false-size", "in", PortKind.EXEC),
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
                "particle.attribute.size",
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
                PortKind.EXEC,
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


def _collision_wait_asset():
    collision_enter = GraphDocument(
        "particle.collision_enter",
        nodes=(
            GraphNodeRecord("root.collision_enter", "particle.root.collision_enter"),
            GraphNodeRecord(
                "frames", "common.constant.i32", properties={"value": 3}
            ),
            GraphNodeRecord("wait", "particle.control.wait_frames"),
            GraphNodeRecord(
                "enter-size",
                "particle.attribute.size",
                properties={"value": 2.0},
            ),
        ),
        links=(
            GraphLinkRecord(
                "enter-wait",
                "root.collision_enter",
                "out",
                "wait",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "frames-wait",
                "frames",
                "value",
                "wait",
                "frames",
                PortKind.VALUE,
            ),
            GraphLinkRecord(
                "wait-size",
                "wait",
                "out",
                "enter-size",
                "in",
                PortKind.EXEC,
            ),
        ),
    )
    return ParticleGraphAsset(
        stable_id="collision-wait-gpu",
        emitters=(
            ParticleEmitterAsset(
                settings=EmitterSettings(collision_enabled=True),
                collision_enter=collision_enter,
            ),
        ),
    )


def _collision_wait_join_asset():
    collision_enter = GraphDocument(
        "particle.collision_enter",
        nodes=(
            GraphNodeRecord("root.collision_enter", "particle.root.collision_enter"),
            GraphNodeRecord("frames", "common.constant.i32", properties={"value": 3}),
            GraphNodeRecord("wait", "particle.control.wait_frames"),
            GraphNodeRecord(
                "delayed-size",
                "particle.attribute.size",
                properties={"value": 2.0},
            ),
            GraphNodeRecord(
                "immediate-position",
                "particle.attribute.position",
                properties={"value": [1.0, 2.0, 3.0]},
            ),
            GraphNodeRecord("join", "particle.control.join_all"),
            GraphNodeRecord(
                "tail-color",
                "particle.attribute.color",
                properties={"value": [0.25, 0.5, 0.75, 1.0]},
            ),
        ),
        links=(
            GraphLinkRecord(
                "root-wait", "root.collision_enter", "out", "wait", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "frames-wait", "frames", "value", "wait", "frames", PortKind.VALUE
            ),
            GraphLinkRecord(
                "wait-size", "wait", "out", "delayed-size", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "size-join", "delayed-size", "out", "join", "in0", PortKind.EXEC
            ),
            GraphLinkRecord(
                "root-position",
                "root.collision_enter",
                "out",
                "immediate-position",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "position-join",
                "immediate-position",
                "out",
                "join",
                "in1",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "join-tail", "join", "out", "tail-color", "in", PortKind.EXEC
            ),
        ),
    )
    return ParticleGraphAsset(
        stable_id="collision-wait-join-gpu",
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
            "tail", "particle.attribute.size", properties={"value": 2.0}
        ),
    ]
    links = [
        GraphLinkRecord(
            "root-wait", "root.update", "out", "wait", "in", PortKind.EXEC
        ),
        GraphLinkRecord(
            "frames-wait", "frames", "value", "wait", "frames", PortKind.VALUE
        ),
        GraphLinkRecord(
            "wait-tail", "wait", "out", "tail", "in", PortKind.EXEC
        ),
    ]
    if fork:
        nodes.append(
            GraphNodeRecord(
                "sibling",
                "particle.attribute.position",
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
                PortKind.EXEC,
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


def _wait_acceleration_asset():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "wait", "particle.control.wait_frames", properties={"frames": 1}
            ),
            GraphNodeRecord(
                "accelerate",
                "particle.attribute.velocity",
                properties={"value": [0.0, -9.81, 0.0]},
            ),
        ),
        links=(
            GraphLinkRecord(
                "root-wait", "root.update", "out", "wait", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "wait-accelerate", "wait", "out", "accelerate", "in", PortKind.EXEC
            ),
        ),
    )
    return ParticleGraphAsset(
        stable_id="wait-acceleration-gpu",
        emitters=(ParticleEmitterAsset(update=update),),
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
            GraphNodeRecord("tail", "particle.attribute.size"),
        ),
        links=(
            GraphLinkRecord(
                "root-frames",
                "root.update",
                "out",
                "wait.frames",
                "in",
                PortKind.EXEC,
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
                PortKind.EXEC,
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
                PortKind.EXEC,
            ),
        ),
    )
    return ParticleGraphAsset(
        stable_id="two-waits-gpu",
        emitters=(ParticleEmitterAsset(update=update),),
    )


def _terminal_wait_acceleration_asset():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "accelerate.down",
                "particle.attribute.velocity",
                properties={"value": [0.0, -9.8, 0.0]},
            ),
            GraphNodeRecord(
                "wait.three",
                "particle.control.wait_seconds",
                properties={"seconds": 3.0},
            ),
            GraphNodeRecord(
                "accelerate.up_right",
                "particle.attribute.velocity",
                properties={"value": [9.8, 9.8, 0.0]},
            ),
            GraphNodeRecord(
                "wait.five",
                "particle.control.wait_seconds",
                properties={"seconds": 5.0},
            ),
        ),
        links=(
            GraphLinkRecord(
                "root-down",
                "root.update",
                "out",
                "accelerate.down",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "down-wait",
                "accelerate.down",
                "out",
                "wait.three",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "wait-up",
                "wait.three",
                "out",
                "accelerate.up_right",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "up-terminal",
                "accelerate.up_right",
                "out",
                "wait.five",
                "in",
                PortKind.EXEC,
            ),
        ),
    )
    return ParticleGraphAsset(
        stable_id="terminal-wait-acceleration-gpu",
        emitters=(ParticleEmitterAsset(update=update),),
    )


def _until_velocity_asset():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "velocity.down",
                "particle.attribute.velocity",
                properties={"composition": "add", "value": [0.0, -1.0, 0.0]},
            ),
            GraphNodeRecord(
                "until.down",
                "particle.control.until_seconds",
                properties={"seconds": 3.0},
            ),
            GraphNodeRecord(
                "velocity.side",
                "particle.attribute.velocity",
                properties={"composition": "add", "value": [1.0, 0.0, 0.0]},
            ),
            GraphNodeRecord(
                "until.side",
                "particle.control.until_frames",
                properties={"frames": 5},
            ),
        ),
        links=(
            GraphLinkRecord(
                "root-down", "root.update", "out", "velocity.down", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "down-until", "velocity.down", "out", "until.down", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "until-side", "until.down", "out", "velocity.side", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "side-until", "velocity.side", "out", "until.side", "in", PortKind.EXEC
            ),
        ),
    )
    return ParticleGraphAsset(
        stable_id="until-velocity-gpu",
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
                "particle.attribute.size",
                properties={"value": 2.0},
            ),
            GraphNodeRecord(
                "right",
                "particle.attribute.position",
                properties={"value": [1.0, 2.0, 3.0]},
            ),
            GraphNodeRecord("join", "particle.control.join_all"),
            GraphNodeRecord(
                "tail",
                "particle.attribute.color",
                properties={"value": [0.25, 0.5, 0.75, 1.0]},
            ),
        ),
        links=(
            GraphLinkRecord("root-wait", "root.update", "out", "wait", "in", PortKind.EXEC),
            GraphLinkRecord("frames-wait", "frames", "value", "wait", "frames", PortKind.VALUE),
            GraphLinkRecord("wait-left", "wait", "out", "left", "in", PortKind.EXEC),
            GraphLinkRecord("left-join", "left", "out", "join", "in0", PortKind.EXEC),
            GraphLinkRecord("root-right", "root.update", "out", "right", "in", PortKind.EXEC),
            GraphLinkRecord("right-join", "right", "out", "join", "in1", PortKind.EXEC),
            GraphLinkRecord("join-tail", "join", "out", "tail", "in", PortKind.EXEC),
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
            GraphNodeRecord("left", "particle.attribute.size", properties={"value": 2.0}),
            GraphNodeRecord(
                "right",
                "particle.attribute.position",
                properties={"value": [1.0, 2.0, 3.0]},
            ),
            GraphNodeRecord("join", "particle.control.join_all"),
            GraphNodeRecord(
                "tail",
                "particle.attribute.color",
                properties={"value": [0.25, 0.5, 0.75, 1.0]},
            ),
        ),
        links=(
            GraphLinkRecord("root-left", "root.update", "out", "wait.left", "in", PortKind.EXEC),
            GraphLinkRecord("frames-left", "frames", "value", "wait.left", "frames", PortKind.VALUE),
            GraphLinkRecord("left-tail", "wait.left", "out", "left", "in", PortKind.EXEC),
            GraphLinkRecord("left-join", "left", "out", "join", "in0", PortKind.EXEC),
            GraphLinkRecord("root-right", "root.update", "out", "wait.right", "in", PortKind.EXEC),
            GraphLinkRecord("seconds-right", "seconds", "value", "wait.right", "seconds", PortKind.VALUE),
            GraphLinkRecord("right-tail", "wait.right", "out", "right", "in", PortKind.EXEC),
            GraphLinkRecord("right-join", "right", "out", "join", "in1", PortKind.EXEC),
            GraphLinkRecord("join-tail", "join", "out", "tail", "in", PortKind.EXEC),
        ),
    )
    return ParticleGraphAsset(
        stable_id="dual-wait-join-gpu",
        emitters=(ParticleEmitterAsset(settings=EmitterSettings(capacity=512), update=update),),
    )


def _dual_init_wait_asset():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "wait.left",
                "particle.control.wait_frames",
                properties={"frames": 2},
            ),
            GraphNodeRecord(
                "wait.right",
                "particle.control.wait_frames",
                properties={"frames": 2},
            ),
        ),
        links=(
            GraphLinkRecord(
                "root-left", "root.init", "out", "wait.left", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "root-right", "root.init", "out", "wait.right", "in", PortKind.EXEC
            ),
        ),
    )
    return ParticleGraphAsset(
        stable_id="dual-init-wait-gpu",
        emitters=(ParticleEmitterAsset(init=init),),
    )


def _nested_wait_join_asset():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("frames.first", "common.constant.i32", properties={"value": 2}),
            GraphNodeRecord("frames.second", "common.constant.i32", properties={"value": 3}),
            GraphNodeRecord("wait.first", "particle.control.wait_frames"),
            GraphNodeRecord("first.left", "particle.attribute.size", properties={"value": 2.0}),
            GraphNodeRecord(
                "first.right",
                "particle.attribute.position",
                properties={"value": [1.0, 2.0, 3.0]},
            ),
            GraphNodeRecord("join.first", "particle.control.join_all"),
            GraphNodeRecord("wait.second", "particle.control.wait_frames"),
            GraphNodeRecord(
                "second.left",
                "particle.attribute.velocity",
                properties={"value": [0.0, 4.0, 0.0]},
            ),
            GraphNodeRecord(
                "second.right",
                "particle.attribute.color",
                properties={"value": [0.2, 0.4, 0.8, 1.0]},
            ),
            GraphNodeRecord("join.second", "particle.control.join_all"),
            GraphNodeRecord(
                "tail",
                "particle.attribute.rotation",
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
                PortKind.EXEC,
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
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "first-left-join",
                "first.left",
                "out",
                "join.first",
                "in0",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "root-first-right",
                "root.update",
                "out",
                "first.right",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "first-right-join",
                "first.right",
                "out",
                "join.first",
                "in1",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "first-join-second-wait",
                "join.first",
                "out",
                "wait.second",
                "in",
                PortKind.EXEC,
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
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "second-left-join",
                "second.left",
                "out",
                "join.second",
                "in0",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "first-join-second-right",
                "join.first",
                "out",
                "second.right",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "second-right-join",
                "second.right",
                "out",
                "join.second",
                "in1",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "second-join-tail",
                "join.second",
                "out",
                "tail",
                "in",
                PortKind.EXEC,
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


def test_gpu_wait_resume_rebuilds_external_ssa_dependencies():
    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(_wait_acceleration_asset())
    )
    source = GpuParticleGlslLowerer().lower(kernel)
    continuation = source.emitters[0].continuation

    assert continuation is not None
    assert "pc.delta_time" in continuation.dispatch
    assert "state.a_builtin_velocity =" in continuation.dispatch
    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


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


def test_gpu_terminal_wait_finishes_continuation_and_compiles_valid_spirv():
    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(_terminal_wait_acceleration_asset())
    )
    emitter = kernel.emitters[0]
    first_wait, terminal_wait = emitter.suspensions

    assert first_wait.resume_node_uid == "accelerate.up_right"
    assert terminal_wait.resume_node_uid == ""
    assert terminal_wait.resume_instruction_index == -1

    source = GpuParticleGlslLowerer().lower(kernel)
    gpu_emitter = source.emitters[0]
    continuation = gpu_emitter.continuation
    assert continuation is not None
    assert continuation.dispatch.count("case ") == 2
    assert "inx_finish_continuation(" in continuation.dispatch
    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def test_gpu_until_repeats_preceding_attribute_operation_and_compiles_valid_spirv():
    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(_until_velocity_asset())
    )
    emitter = kernel.emitters[0]
    assert [item.resume_node_uid for item in emitter.suspensions] == [
        "velocity.down",
        "velocity.side",
    ]

    source = GpuParticleGlslLowerer().lower(kernel)
    continuation = source.emitters[0].continuation
    assert continuation is not None
    assert "inx_until_seconds(" in source.emitters[0].update
    assert "inx_until_frames(" in source.emitters[0].update
    assert "INX_CONTINUATION_FLAG_UNTIL_SECONDS" in source.emitters[0].update
    assert "INX_CONTINUATION_FLAG_UNTIL_FRAMES" in source.emitters[0].update
    assert continuation.dispatch.count("case ") == 2
    assert continuation.dispatch.count("state.a_builtin_velocity =") >= 2
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
    assert "INX_PARTICLE_CONTINUATION_LOCK" in continuation.dispatch
    assert "atomicOr(" in continuation.dispatch
    assert (
        "inx_continuation_append_active(inx_continuation_record_index)"
        in continuation.dispatch
    )
    assert "atomicAnd(" in continuation.dispatch
    assert "~INX_PARTICLE_CONTINUATION_LOCK" in continuation.dispatch

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
    contact = source.emitters[0].stages()["contact_dispatch"]

    assert "inx_collide_scene(" in update
    assert "if ((pc.diagnostic_flags & 1u) != 0u) {" in update
    assert "atomicAdd(counters.collision_hit_count, 1u);" in update
    assert "atomicAdd(counters.collision_response_count, 1u);" in update
    assert "atomicAdd(counters.collision_trigger_count, 1u);" in update
    assert "atomicAdd(counters.collision_enter_count, 1u);" in contact
    assert "atomicAdd(counters.collision_stay_count, 1u);" in contact
    assert "atomicAdd(counters.collision_exit_count, 1u);" in contact
    assert "atomicMax(counters.collision_max_outward_speed_bits" in update
    assert "atomicMax(counters.collision_max_tangent_speed_bits" in update
    assert "// enter-size" in contact
    assert "inx_contact_lifecycle == 0u" in contact
    compiled = compile_gpu_particle_spirv(source)
    assert compiled["emitters"][0]["stages"]["update"]["byte_size"] > 0
    assert compiled["emitters"][0]["stages"]["contact_dispatch"]["byte_size"] > 0


def test_contact_wait_uses_contact_scoped_snapshot_and_valid_spirv():
    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(_collision_wait_asset())
    )
    source = GpuParticleGlslLowerer().lower(kernel)
    emitter = source.emitters[0]
    contact = emitter.contact_dispatch
    continuation = emitter.continuation

    assert continuation is not None
    assert "inx_continuation_context_kind = INX_CONTINUATION_CONTEXT_CONTACT" in contact
    assert "contact_continuation_snapshot_words[base + 23u]" in contact
    assert "bool uses_lane_slot = inx_continuation_context_kind == 0u" in contact
    assert "uint snapshot_base = inx_continuation_record_index * 24u" in continuation.dispatch
    assert "Contact invocation completed independently" in continuation.dispatch
    assert "state.update_resume_step = pc.simulation_step" not in continuation.dispatch
    compiled = compile_gpu_particle_spirv(source)
    assert compiled["emitters"][0]["stages"]["contact_dispatch"]["byte_size"] > 0
    assert (
        compiled["emitters"][0]["continuation"]["stages"]["dispatch"]["byte_size"]
        > 0
    )


def test_contact_wait_join_uses_full_contact_identity_and_valid_spirv():
    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(_collision_wait_join_asset())
    )
    source = GpuParticleGlslLowerer().lower(kernel)
    emitter = source.emitters[0]
    continuation = emitter.continuation

    assert continuation is not None
    assert continuation.join_count == 1
    assert "ParticleContactContinuationJoinStates" in emitter.contact_dispatch
    assert "inx_contact_continuation_join_begin" in emitter.contact_dispatch
    assert "inx_continuation_context_identity_low = contact.identity.z" in emitter.contact_dispatch
    assert "inx_continuation_context_identity_high = contact.identity.w" in emitter.contact_dispatch
    assert "inx_contact_continuation_join_arrive" in continuation.dispatch
    assert "contact_continuation_snapshot_words[snapshot_base + 2u]" in continuation.dispatch
    assert "contact_continuation_snapshot_words[snapshot_base + 3u]" in continuation.dispatch
    assert "contact Wait/Until branches cannot Join" not in continuation.dispatch

    compiled = compile_gpu_particle_spirv(source)
    validate_gpu_particle_spirv(compiled, source)


def test_scene_collision_uses_shared_grid_abi_and_compiles_to_spirv():
    hir = ParticleGraphCompiler().compile(_scene_collision_asset())
    assert not any(
        operation.opcode == "collision.scene"
        for operation in hir.emitters[0].update.flow.iter_operations()
    )

    kernel = ParticleKernelLowerer().lower(hir)
    collision_instructions = [
        value for value in kernel.emitters[0].update.instructions
        if value.opcode == "collide_scene"
    ]
    assert len(collision_instructions) == 1
    assert not any(
        value.opcode == "record_collision_diagnostics"
        for value in kernel.emitters[0].update.instructions
    )
    instruction = collision_instructions[0]
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
        "collider_id_high_attribute": "builtin.collision_collider_id_high",
        "collider_id_low_attribute": "builtin.collision_collider_id_low",
        "hit_attribute": "builtin.collision_hit",
        "material_attribute": "builtin.collision_material",
        "normal_attribute": "builtin.collision_normal",
        "penetration_attribute": "builtin.collision_penetration",
        "point_attribute": "builtin.collision_point",
        "position_attribute": "builtin.position",
        "relative_velocity_attribute": "builtin.collision_relative_velocity",
        "trigger_attribute": "builtin.collision_is_trigger",
        "velocity_attribute": "builtin.velocity",
    }

    source = GpuParticleGlslLowerer().lower(kernel)
    assert source.emitters[0].collision_enabled is True
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
    for binding in range(7):
        assert f"set = 6, binding = {binding}" in update_source
    assert "inx_collide_scene(" in update_source
    assert "bool inx_collide_scene(" in update_source
    assert "out vec3 simulation_collision_normal" in update_source
    assert "out vec3 simulation_contact_point" in update_source
    assert "out vec3 simulation_relative_velocity" in update_source
    assert "out float simulation_penetration" in update_source
    assert "out bool collision_is_trigger" in update_source
    assert "out vec4 collision_material" in update_source
    assert "out uvec2 collision_collider_id" in update_source
    assert "primary_collider_id = collider_id" in update_source
    assert "if (is_trigger) continue;" in update_source
    assert update_source.index("atomicAdd(counters.collision_trigger_count, 1u);") < update_source.index(
        "if (is_trigger) continue;"
    )
    assert "state.a_builtin_collision_collider_id_low" in update_source
    assert "state.a_builtin_collision_collider_id_high" in update_source
    assert "uint collision_hit_count;" in update_source
    assert "uint collision_exit_count;" in update_source
    assert "uint collision_max_outward_speed_bits;" in update_source
    assert "uint collision_max_tangent_speed_bits;" in update_source
    assert "uint collision_candidate_overflow_count;" in update_source
    assert "uint candidate_indices[16];" in update_source
    assert "particle_colliders[existing_index].identity.xy" in update_source
    assert "atomicAdd(counters.collision_candidate_overflow_count, 1u);" in update_source
    assert update_source.index("uint candidate_indices[16];") < update_source.index(
        "for (uint candidate = 0u; candidate < candidate_count; ++candidate)"
    )
    render_reset_source = source.emitters[0].render_reset
    assert "(pc.diagnostic_flags & 2u) != 0u" in render_reset_source
    assert "counters.collision_hit_count = 0u;" in render_reset_source
    assert "counters.collision_max_outward_speed_bits = 0u;" in render_reset_source
    assert "counters.collision_max_tangent_speed_bits = 0u;" in render_reset_source
    assert "counters.collision_candidate_overflow_count = 0u;" in render_reset_source
    assert render_reset_source.index("counters.collision_hit_count = 0u;") < render_reset_source.index(
        "simulation_control.simulation_allowed == 0u"
    )
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

    contact_prepare_source = source.emitters[0].contact_prepare
    for binding in range(8):
        assert f"set = 7, binding = {binding}" in contact_prepare_source
    assert "const uint INX_CONTACTS_PER_PARTICLE = 8u;" in contact_prepare_source
    assert "uint current_page = pc.simulation_step & 1u;" in contact_prepare_source
    assert "previous_state.y + 1u == pc.simulation_step" in contact_prepare_source
    assert "contact_particle_record_indices[current_base + slot]" in contact_prepare_source
    assert "contact_counters.contact_current_record_count = 0u;" in contact_prepare_source
    assert "contact_counters.contact_max_per_particle = 0u;" in contact_prepare_source
    assert "contact_counters.multi_contact_particle_count = 0u;" in contact_prepare_source
    assert "contact_counters.contact_retained_order_hash = 0u;" in contact_prepare_source
    assert "contact_counters.contact_dropped_order_hash = 0u;" in contact_prepare_source
    assert "contact_counters.contact_min_particle_index = INX_CONTACT_INVALID_INDEX;" in contact_prepare_source
    assert "contact_counters.contact_max_particle_index = 0u;" in contact_prepare_source
    assert "contact_hash_slots[hash_slot].key = uvec4(INX_CONTACT_INVALID_INDEX);" in contact_prepare_source
    assert "bool reset_all = (pc.diagnostic_flags & 4u) != 0u;" in contact_prepare_source
    assert "for (uint page = 0u; page < 2u; ++page)" in contact_prepare_source
    assert "contact_particle_states[particle_index] =\n                uvec4(INX_CONTACT_INVALID_INDEX" in contact_prepare_source
    assert "(pc.capacity + 255u) / 256u" in contact_prepare_source
    assert "particle_index * INX_CONTACTS_PER_PARTICLE * 2u" not in contact_prepare_source
    assert "inx_store_contact(" in update_source
    assert "atomicAdd(contact_counters.contact_current_record_count, 1u)" in update_source
    assert "particle_index * INX_CONTACTS_PER_PARTICLE + contact_slot" not in update_source
    assert "bounded global sparse pool" in update_source
    assert "privileges particle indices" in update_source
    assert "first eight actual" in update_source
    assert "particle_state = uvec4(generation, pc.simulation_step, 0u, 0u);" in update_source
    assert "atomicAdd(contact_counters.contact_current_record_count, 1u);" in update_source
    assert "(pc.diagnostic_flags & 1u) != 0u" in update_source
    assert "atomicMax(contact_counters.contact_max_per_particle, contact_slot + 1u);" in update_source
    assert "atomicAdd(contact_counters.multi_contact_particle_count, 1u);" in update_source
    assert "atomicAdd(contact_counters.contact_retained_order_hash" in update_source
    assert "atomicAdd(contact_counters.contact_dropped_order_hash" in update_source
    assert "uvec4(collider_id, contact_slot, 0u)" in update_source
    assert "uvec4(collider_id, contact_order, 1u)" in update_source
    assert "atomicMin(contact_counters.contact_min_particle_index, particle_index);" in update_source
    assert "atomicMax(contact_counters.contact_max_particle_index, particle_index);" in update_source
    assert "gl_GlobalInvocationID.x, contact_hit_order, collider_id" in update_source
    assert "struct InxParticleContactRecord" in update_source

    contact_solve_source = source.emitters[0].contact_solve
    assert "uint hash_slot = inx_contact_hash(key) & hash_mask;" in contact_solve_source
    assert "atomicCompSwap(" in contact_solve_source
    assert "contact_hash_slots[physical_hash_slot].value = uvec4(" in contact_solve_source
    assert "uint inx_find_contact_record(uint page, uvec4 key)" in contact_solve_source
    assert "for (uint lifecycle = 0u; lifecycle < 2u; ++lifecycle)" in contact_solve_source
    assert "contact_work_items[work_index].dispatch = uvec4(" in contact_solve_source
    assert "2u, record_index, pc.simulation_step," in contact_solve_source
    assert "contact_records[record_index].metadata.w" in contact_solve_source
    assert "atomicAdd(contact_counters.contact_work_item_count, work_count);" in contact_solve_source
    assert "uint work_capacity = record_capacity * 2u;" in contact_solve_source
    assert "one contiguous" in contact_solve_source
    assert "particle_index * INX_CONTACTS_PER_PARTICLE * 2u" not in contact_solve_source

    contact_dispatch_source = source.emitters[0].contact_dispatch
    assert "packed_work_range >> INX_CONTACT_WORK_COUNT_BITS" in contact_dispatch_source
    assert "local_work < work_count" in contact_dispatch_source

    payload = compile_gpu_particle_spirv(source)
    assert validate_gpu_particle_spirv(payload, source) is payload


def test_scene_collision_event_payload_reads_post_collision_state():
    collision_enter = GraphDocument(
        "particle.collision_enter",
        nodes=(
            GraphNodeRecord(
                "root.collision_enter", "particle.root.collision_enter"
            ),
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
                "impact.trigger",
                PARTICLE_EVENT_TRIGGER_TYPE_ID,
                properties={"event": "impact", "condition": True},
            ),
        ),
        links=(
            GraphLinkRecord(
                "event.stream",
                "root.collision_enter",
                "out",
                "impact.trigger",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "event.condition",
                "collision.hit",
                "value",
                "impact.trigger",
                "condition",
                PortKind.VALUE,
            ),
            GraphLinkRecord(
                "event.position",
                "position",
                "value",
                "impact.trigger",
                particle_event_payload_port_id("position"),
                PortKind.VALUE,
            ),
            GraphLinkRecord(
                "event.normal",
                "collision.normal",
                "value",
                "impact.trigger",
                particle_event_payload_port_id("normal"),
                PortKind.VALUE,
            ),
        ),
    )
    event_graph = GraphDocument(
        "particle.event",
        nodes=(
            GraphNodeRecord(
                "root.event",
                PARTICLE_EVENT_ACTIVE_TYPE_ID,
                properties={"event": "impact"},
            ),
            GraphNodeRecord("set.velocity", "particle.attribute.velocity"),
        ),
        links=(
            GraphLinkRecord(
                "event.exec", "root.event", "out", "set.velocity", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "event.normal",
                "root.event",
                particle_event_payload_port_id("normal"),
                "set.velocity",
                "value",
                PortKind.VALUE,
            ),
        ),
    )
    source_emitter = ParticleEmitterAsset(
        stable_id="source",
        settings=EmitterSettings(collision_enabled=True),
        collision_enter=collision_enter,
        event_flows=(ParticleEventFlow("impact", event_graph),),
    )
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
        emitters=(source_emitter,),
        event_types=(event_type,),
    )

    hir = ParticleGraphCompiler().compile(graph)
    assert [
        operation.opcode
        for operation in hir.emitters[0].collision_enter.flow.iter_operations()
    ] == ["event.trigger"]
    kernel = ParticleKernelLowerer().lower(hir)
    attribute_ids = {attribute[0] for attribute in kernel.emitters[0].attributes}
    assert "builtin.collision_hit" in attribute_ids
    assert "builtin.collision_normal" in attribute_ids
    update_instructions = kernel.emitters[0].update.instructions
    contact_instructions = kernel.emitters[0].contact.instructions
    collision_index = next(
        index
        for index, instruction in enumerate(update_instructions)
        if instruction.opcode == "collide_scene"
    )
    event_index = next(
        index
        for index, instruction in enumerate(contact_instructions)
        if instruction.opcode == "event_enqueue"
    )
    post_collision_position_loads = [
        index
        for index, instruction in enumerate(contact_instructions)
        if instruction.opcode == "load_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.position"
        and index < event_index
    ]
    assert post_collision_position_loads
    post_collision_hit_loads = [
        index
        for index, instruction in enumerate(contact_instructions)
        if instruction.opcode == "load_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.collision_hit"
        and index < event_index
    ]
    post_collision_normal_loads = [
        index
        for index, instruction in enumerate(contact_instructions)
        if instruction.opcode == "load_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.collision_normal"
        and index < event_index
    ]
    assert post_collision_hit_loads
    assert post_collision_normal_loads
    collision_instruction = update_instructions[collision_index]
    assert collision_instruction.immediate_dict()["hit_attribute"] == "builtin.collision_hit"
    assert (
        collision_instruction.immediate_dict()["normal_attribute"]
        == "builtin.collision_normal"
    )
    round_trip = type(kernel).from_dict(kernel.to_dict())
    assert round_trip == kernel
    source = GpuParticleGlslLowerer().lower(kernel).emitters[0].update
    assert source.index("inx_collide_scene(") < source.index(
        "inx_event_"
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
                "particle.parameter",
                properties={"parameter": "wind"},
            ),
            GraphNodeRecord("accelerate", "particle.attribute.velocity"),
        ),
        links=(
            GraphLinkRecord("stream", "root.update", "out", "accelerate", "in", PortKind.EXEC),
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
    assert "set = 3, binding = 2" in source.emitters[0].update
    assert "uintBitsToFloat(parameter_words[0].xyz)" in source.emitters[0].update


def test_gpu_writable_parameter_emits_typed_shared_buffer_store():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "set-color",
                "particle.parameter.set",
                properties={
                    "parameter": "shared-color",
                    "value": [0.25, 0.5, 0.75, 1.0],
                },
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream",
                "root.update",
                "out",
                "set-color",
                "in",
                PortKind.EXEC,
            ),
        ),
    )
    asset = ParticleGraphAsset(
        parameters=(
            ParticleParameter(
                "shared-color",
                "Shared Color",
                TypeRef(ValueType.COLOR),
                [1.0, 1.0, 1.0, 1.0],
                writable=True,
            ),
        ),
        emitters=(ParticleEmitterAsset(update=update),),
    )
    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    emitter = GpuParticleGlslLowerer().lower(kernel).emitters[0]

    assert "set = 3, binding = 2" in emitter.update
    assert "parameter_words[0] = floatBitsToUint(" in emitter.update
    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-writable-shared-parameter"
    )
    assert set(compiled) == set(emitter.stages())


def test_gpu_set_emitter_playing_uses_graph_state_request_buffers():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "pause-impact",
                "particle.emitter.playing",
                properties={"emitter": "impact", "playing": False},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream",
                "root.update",
                "out",
                "pause-impact",
                "in",
                PortKind.EXEC,
            ),
        ),
    )
    asset = ParticleGraphAsset(
        emitters=(
            ParticleEmitterAsset(stable_id="meteor", update=update),
            ParticleEmitterAsset(stable_id="impact"),
        )
    )
    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    emitter = GpuParticleGlslLowerer().lower(kernel).emitters[0]

    assert "set = 3, binding = 3" in emitter.update
    assert "set = 3, binding = 4" in emitter.update
    assert "bool v3 = false;" in emitter.update
    assert "inx_request_emitter_playing(1u, v3);" in emitter.update
    assert "&& inx_current_emitter_playing();" in emitter.update
    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-emitter-playing"
    )
    assert set(compiled) == set(emitter.stages())


def test_per_particle_event_queue_uses_compiled_field_word_layout():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "trigger", PARTICLE_EVENT_TRIGGER_TYPE_ID,
                properties={
                    "event": "impact",
                    "condition": True,
                    particle_event_payload_port_id("enabled"): False,
                    particle_event_payload_port_id("direction"): [1.0, 2.0, 3.0],
                },
            ),
        ),
        links=(
            GraphLinkRecord(
                "trigger.exec", "root.update", "out", "trigger", "in", PortKind.EXEC
            ),
        ),
    )
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
        emitters=(
            ParticleEmitterAsset(
                stable_id="source",
                update=update,
                event_flows=(
                    ParticleEventFlow("impact", default_event_graph("impact")),
                ),
            ),
        ),
    )
    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(asset)
    )
    event_type = kernel.events.event_types[0]
    assert [(field.word_offset, field.word_count) for field in event_type.fields] == [
        (0, 1),
        (1, 3),
    ]
    enqueue = next(
        item for item in kernel.emitters[0].update.instructions
        if item.opcode == "event_enqueue"
    )
    assert [operand.value_type for operand in enqueue.operands] == [
        TypeRef(ValueType.BOOL),
        TypeRef(ValueType.BOOL),
        TypeRef(ValueType.VEC3),
    ]
    attribute_ids = {item[0] for item in kernel.emitters[0].attributes}
    assert "internal.event.0.head" in attribute_ids
    assert "internal.event.0.tail" in attribute_ids
    assert "internal.event.0.count" in attribute_ids
    assert "internal.event.0.active" in attribute_ids


def test_gpu_texture2d_parameter_lowers_to_rhi_resource_and_sample():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-color", "particle.attribute.color"),
            GraphNodeRecord(
                "texture",
                "particle.parameter",
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
                "stream", "root.update", "out", "set-color", "in", PortKind.EXEC
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
    texture_handle = gpu_backend._texture_resource_handle("smoke-texture")
    assert f"inx_sample_parameter_texture({texture_handle}u," in emitter.update
    payload = compile_gpu_particle_spirv(source)
    assert validate_gpu_particle_spirv(payload, source) is payload


@pytest.mark.parametrize("value_type", (ValueType.TEXTURE2D, ValueType.MESH))
def test_particle_resources_cannot_be_stored_as_per_particle_attributes(value_type):
    with pytest.raises(ValueError, match="must use a GPU-storable type"):
        ParticleAttribute(
            "resource.attribute",
            "Resource Attribute",
            TypeRef(value_type),
            AssetReference().to_dict(),
        )


def _kill_if_gpu_source():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("kill", "particle.lifecycle.kill_if"),
            GraphNodeRecord(
                "age",
                "particle.attribute.get",
                properties={"attribute": "builtin.age"},
            ),
            GraphNodeRecord("limit", "common.constant.f32", properties={"value": 0.5}),
            GraphNodeRecord("older", "common.compare.greater_than"),
        ),
        links=(
            GraphLinkRecord("stream", "root.update", "out", "kill", "in", PortKind.EXEC),
            GraphLinkRecord("a", "age", "value", "older", "a", PortKind.VALUE),
            GraphLinkRecord("b", "limit", "value", "older", "b", PortKind.VALUE),
            GraphLinkRecord("condition", "older", "result", "kill", "condition", PortKind.VALUE),
        ),
    )
    emitter = ParticleEmitterAsset(stable_id="kill", update=update)
    hir = ParticleGraphCompiler().compile(ParticleGraphAsset(emitters=(emitter,)))
    return GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))


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
            GraphNodeRecord("acceleration", "particle.attribute.velocity"),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "acceleration", "in", PortKind.EXEC
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


def _event_queue_program():
    impact = ParticleEventType(
        "impact",
        "Impact",
        4,
        (
            ParticleEventField("position", "Position", TypeRef(ValueType.VEC3), [1.0, 2.0, 3.0]),
            ParticleEventField("kind", "Kind", TypeRef(ValueType.U32), 7),
            ParticleEventField("weight", "Weight", TypeRef(ValueType.F32), 2.5),
        ),
    )
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "trigger",
                PARTICLE_EVENT_TRIGGER_TYPE_ID,
                properties={
                    "event": "impact",
                    "condition": True,
                    particle_event_payload_port_id("position"): [4.0, 5.0, 6.0],
                },
            ),
        ),
        links=(GraphLinkRecord("trigger.exec", "root.update", "out", "trigger", "in", PortKind.EXEC),),
    )
    event_graph = GraphDocument(
        "particle.event",
        nodes=(
            GraphNodeRecord(
                "root.event",
                PARTICLE_EVENT_ACTIVE_TYPE_ID,
                properties={"event": "impact"},
            ),
            GraphNodeRecord("set.size", "particle.attribute.size"),
        ),
        links=(
            GraphLinkRecord("event.exec", "root.event", "out", "set.size", "in", PortKind.EXEC),
            GraphLinkRecord(
                "event.weight",
                "root.event",
                particle_event_payload_port_id("weight"),
                "set.size",
                "value",
                PortKind.VALUE,
            ),
        ),
    )
    emitter = ParticleEmitterAsset(
        stable_id="source",
        update=update,
        event_flows=(ParticleEventFlow("impact", event_graph),),
    )
    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(stable_id="event-queue-graph", emitters=(emitter,), event_types=(impact,))
        )
    )
    return kernel, GpuParticleGlslLowerer().lower(kernel)


def test_gpu_event_payload_round_trips_through_the_per_particle_fifo():
    kernel, gpu = _event_queue_program()
    instruction = next(
        instruction
        for instruction in kernel.emitters[0].update.instructions
        if instruction.opcode == "event_enqueue"
    )
    immediate = instruction.immediate_dict()
    assert immediate["event_type_index"] == 0
    assert immediate["queue_capacity"] == 4
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
    event_payload = next(
        instruction
        for instruction in kernel.emitters[0].update.instructions
        if instruction.opcode == "event_payload"
    )
    assert event_payload.result_type == TypeRef(ValueType.F32)
    assert event_payload.immediate_dict() == {
        "event_type_index": 0,
        "field_stable_id": "weight",
        "word_offset": 4,
        "word_count": 1,
        "default": 2.5,
    }

    source = gpu.emitters[0]
    assert source.event_type_count == 1
    assert source.to_dict()["event_type_count"] == 1
    assert "internal_event_0_tail" in source.update
    assert "internal_event_0_count" in source.update
    assert "vec3(4.0, 5.0, 6.0)" in source.update
    assert "internal_event_0_active" in source.update
    assert (
        "if (state.a_internal_event_0_count + "
        "state.a_internal_event_0_active < 4u)"
    ) in source.update
    assert "uint event_counters[];" in source.update
    assert "atomicAdd(counters.event_counters[0u], 1u);" in source.update
    assert "atomicAdd(counters.event_counters[1u], 1u);" in source.update
    assert "atomicAdd(counters.event_counters[2u], 1u);" in source.update
    assert "if (index < 3u) counters.event_counters[index] = 0u;" in source.bootstrap
    restored = type(kernel).from_dict(kernel.to_dict())
    assert GpuParticleGlslLowerer().lower(restored).emitters[0].update == source.update
    compiled = compile_gpu_particle_spirv(gpu)
    assert set(compiled["emitters"][0]["stages"]) == set(source.stages())


def test_gpu_event_wait_keeps_one_fifo_invocation_active_until_resume():
    source_text = '''\
from Infernux.particle import (
    ParticleScript, ParticleEmitter, EmitterSettings, EventField, EventType, event,
)

class QueuedImpact(ParticleScript):
    event_types = (
        EventType(
            stable_id="impact",
            name="Impact",
            queue_capacity=8,
            fields=(EventField("weight", "Weight", "f32", 1.0),),
        ),
    )

    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            pass

        def update(self, ctx, particles):
            particles.trigger_event(
                event="impact", payload={"weight": particles.size}
            )
            particles.trigger_event(
                event="impact", payload={"weight": particles.size}
            )

        @event("impact")
        def on_impact(self, ctx, particles):
            particles.set_size(ctx.event_payload(field="weight"))
            ctx.wait_frames(3)
            particles.set_color((1.0, 0.0, 0.0, 1.0))

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    hir = ParticleScriptCompiler().compile(
        source_text, source_name="QueuedImpact.particle.py"
    )
    kernel = ParticleKernelLowerer().lower(hir)
    update_instructions = kernel.emitters[0].update.instructions
    assert sum(
        item.opcode == "event_enqueue" for item in update_instructions
    ) == 2
    assert sum(
        item.opcode == "event_begin" for item in update_instructions
    ) == 1
    assert sum(
        item.opcode == "event_complete" for item in update_instructions
    ) == 1
    suspension = next(
        item for item in kernel.emitters[0].suspensions
        if item.lifecycle_stage.value == "event"
    )
    assert suspension.flow_id == "impact"
    update = GpuParticleGlslLowerer().lower(kernel).emitters[0].update
    assert "internal_event_0_active" in update
    assert "internal_event_0_head" in update
    assert "internal_event_0_tail" in update
    assert "internal_event_0_count" in update
    assert "state.a_internal_event_0_active == 0u" in update
    assert "inx_continuation_lane_pending" in update
    assert (
        "bool inx_lane_update_0_0_active = "
        "state.update_resume_step != pc.simulation_step;" in update
    )
    event_lane = next(
        line for line in update.splitlines()
        if "bool inx_lane_event_1_0_active" in line
    )
    assert "state.update_resume_step" not in event_lane
    gpu = GpuParticleGlslLowerer().lower(kernel)
    continuation = gpu.emitters[0].continuation.stages()["dispatch"]
    assert "bool inx_event_begin_" not in continuation
    assert "if (true)" in continuation
    assert "state.a_builtin_color =" in continuation
    assert "a_internal_event_0_active = 0u" in continuation
    _assert_all_generated_values_are_declared(gpu)
    assert validate_gpu_particle_spirv(
        compile_gpu_particle_spirv(gpu), gpu
    )


def test_gpu_event_fanout_stays_non_reentrant_until_every_waiting_branch_finishes():
    event_type = ParticleEventType("pulse", "Pulse", queue_capacity=8)
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "trigger",
                PARTICLE_EVENT_TRIGGER_TYPE_ID,
                properties={"event": "pulse"},
            ),
        ),
        links=(
            GraphLinkRecord(
                "trigger.exec",
                "root.update",
                "out",
                "trigger",
                "in",
                PortKind.EXEC,
            ),
        ),
    )
    event_graph = GraphDocument(
        "particle.event",
        nodes=(
            GraphNodeRecord(
                "root.event",
                PARTICLE_EVENT_ACTIVE_TYPE_ID,
                properties={"event": "pulse"},
            ),
            GraphNodeRecord(
                "wait.left",
                "particle.control.wait_frames",
                properties={"frames": 2},
            ),
            GraphNodeRecord(
                "wait.right",
                "particle.control.wait_frames",
                properties={"frames": 3},
            ),
            GraphNodeRecord(
                "set.size",
                "particle.attribute.size",
                properties={"value": 2.0},
            ),
            GraphNodeRecord(
                "set.color",
                "particle.attribute.color",
                properties={"value": [1.0, 0.0, 1.0, 1.0]},
            ),
        ),
        links=(
            GraphLinkRecord(
                "event.left",
                "root.event",
                "out",
                "wait.left",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "event.right",
                "root.event",
                "out",
                "wait.right",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "left.tail",
                "wait.left",
                "out",
                "set.size",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "right.tail",
                "wait.right",
                "out",
                "set.color",
                "in",
                PortKind.EXEC,
            ),
        ),
    )
    emitter = ParticleEmitterAsset(
        stable_id="event-fanout",
        update=update,
        event_flows=(ParticleEventFlow("pulse", event_graph),),
    )
    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(
                stable_id="event-fanout-graph",
                emitters=(emitter,),
                event_types=(event_type,),
            )
        )
    )

    event_suspensions = [
        item
        for item in kernel.emitters[0].suspensions
        if item.lifecycle_stage.value == "event" and item.flow_id == "pulse"
    ]
    assert len(event_suspensions) == 2
    assert len({item.lane_stable_id for item in event_suspensions}) == 2

    gpu = GpuParticleGlslLowerer().lower(kernel)
    update_source = gpu.emitters[0].update
    continuation_source = gpu.emitters[0].continuation.stages()["dispatch"]
    assert update_source.count("!inx_continuation_lane_pending(particle_index") >= 2
    assert continuation_source.count(
        "if (!inx_continuation_resuspended && !inx_continuation_lane_pending"
    ) == 2
    assert continuation_source.count("a_internal_event_0_active = 0u") == 2
    assert validate_gpu_particle_spirv(
        compile_gpu_particle_spirv(gpu), gpu
    )


def _noise_gpu_source():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("acceleration", "particle.attribute.velocity"),
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
                "stream", "root.update", "out", "acceleration", "in", PortKind.EXEC
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
            GraphLinkRecord("render", "root.rendering", "out", "ribbon", "in", PortKind.EXEC),
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
                "particle.attribute.flipbook_frame",
                properties={"value": 5.0},
            ),
        ),
        links=(
            GraphLinkRecord(
                "init-stream", "root.init", "out", "flipbook", "in", PortKind.EXEC
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
                "render-stream", "root.rendering", "out", "sprite", "in", PortKind.EXEC
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
    assert "layout(location = 0) out vec3 out_world_position;" in gpu_backend._BILLBOARD_VERTEX_GLSL
    assert "layout(location = 10) out vec2 out_particle_next_uv;" in gpu_backend._BILLBOARD_VERTEX_GLSL
    assert "layout(location = 14) out float out_particle_alpha;" in gpu_backend._BILLBOARD_VERTEX_GLSL
    assert "layout(location = 15) flat out uint out_layer_mask;" in gpu_backend._BILLBOARD_VERTEX_GLSL
    payload = compile_gpu_particle_spirv(source)
    assert validate_gpu_particle_spirv(payload, source) is payload


def test_gpu_plane_collision_uses_portable_post_integration_helpers_and_compiles():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "collision",
                "particle.collision.plane",
                properties={"radius": 0.2, "restitution": 0.65, "friction": 0.15},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.EXEC
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
                "particle.collision.sphere",
                properties={"sphere_radius": 1.5, "particle_radius": 0.1},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.EXEC
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
            GraphNodeRecord("acceleration", "particle.attribute.velocity"),
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
                "stream", "root.update", "out", "acceleration", "in", PortKind.EXEC
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
                "particle.collision.sdf",
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
                "stream", "root.update", "out", "collision", "in", PortKind.EXEC
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


def _sdf_sample_gpu_source():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "position",
                "particle.attribute.get",
                properties={"attribute": "builtin.position"},
            ),
            GraphNodeRecord(
                "distance",
                "particle.sdf.sample_distance",
                properties={"interface": "sample-field"},
            ),
            GraphNodeRecord(
                "gradient",
                "particle.sdf.sample_gradient",
                properties={"interface": "sample-field"},
            ),
            GraphNodeRecord("size", "particle.attribute.size"),
            GraphNodeRecord("velocity", "particle.attribute.velocity"),
        ),
        links=(
            GraphLinkRecord("root-size", "root.update", "out", "size", "in", PortKind.EXEC),
            GraphLinkRecord("size-velocity", "size", "out", "velocity", "in", PortKind.EXEC),
            GraphLinkRecord("position-distance", "position", "value", "distance", "position"),
            GraphLinkRecord("position-gradient", "position", "value", "gradient", "position"),
            GraphLinkRecord("distance-size", "distance", "distance", "size", "value"),
            GraphLinkRecord("gradient-velocity", "gradient", "gradient", "velocity", "value"),
        ),
    )
    emitter = ParticleEmitterAsset(
        stable_id="sdf-sample-emitter",
        update=update,
        data_interfaces=(
            SdfVolume(
                stable_id="sample-field",
                texture=AssetReference(guid="sdf-texture-guid"),
            ),
        ),
    )
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(stable_id="sdf-sample-gpu", emitters=(emitter,))
    )
    return GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))


def _sdf_shape_gpu_source(mode: str):
    interface = SdfVolume(
        stable_id="spawn-field",
        texture=AssetReference(guid="sdf-texture-guid"),
    )
    emitter = ParticleEmitterAsset(
        stable_id=f"sdf-{mode}-emitter",
        settings=EmitterSettings(
            shape=EmitterShape(
                kind="sdf",
                sdf_interface=interface.stable_id,
                sdf_mode=mode,
            )
        ),
        data_interfaces=(interface,),
    )
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(stable_id=f"sdf-{mode}-gpu", emitters=(emitter,))
    )
    return GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))


def test_gpu_lowerer_emits_resident_compute_lifecycle_and_indirect_output():
    program = _gpu_source()
    assert len(program.emitters) == 1
    emitter = program.emitters[0]

    assert set(emitter.stages()) == {
        "bootstrap",
        "init",
        "update",
        "contact_prepare",
        "contact_solve",
        "contact_dispatch",
        "render_reset",
        "rendering",
        "update_rendering_fused",
    }
    assert "buffer ParticleStates" in emitter.update
    assert "inx_pop_free" in emitter.init
    assert "states[index].spawn_generation = 0u;" in emitter.bootstrap
    assert "atomicAdd(indirect_args.instance_count, committed_count)" in emitter.rendering
    assert "ParticleVisibilityInstance" in emitter.rendering
    assert "visibility[particle_index].position_radius = vec4(" in emitter.rendering
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


def test_gpu_init_reserves_one_free_list_block_per_workgroup_and_compiles():
    init = _gpu_source().emitters[0].init
    main = init.rsplit("void main()", 1)[1]
    before_barrier = main.split("barrier();", 1)[0]

    assert "shared uint inx_particle_init_old_free_count;" in init
    assert "shared uint inx_particle_init_accepted_count;" in init
    assert "uint inx_reserve_free_block(" in init
    assert "attempt < attempt_limit" in init
    assert main.count("inx_reserve_free_block(") == 1
    assert "inx_pop_free();" not in main
    assert "return" not in before_barrier
    assert main.count("barrier();") == 1
    assert "emitter_spawn.spawn_count - group_first_invocation" in main
    assert "simulation_control.simulation_allowed" not in before_barrier
    assert "inx_current_emitter_playing()" not in before_barrier
    assert re.search(
        r"free_slots\[\s*inx_particle_init_old_free_count\s*-\s*1u\s*"
        r"-\s*gl_LocalInvocationIndex\s*\]",
        main,
    )
    assert "requested_count - inx_particle_init_accepted_count" in main
    assert "atomicAdd(counters.reserved_count, inx_particle_init_accepted_count)" in main
    assert "atomicAdd(counters.dropped_count, dropped_count)" in main
    assert "if (!initialized_state_finite) atomicAdd(counters.dropped_count, 1u);" in main

    compiled = native._compile_compute_glsl_batch(
        {"init": init}, "particle-init-free-block-test"
    )
    assert set(compiled) == {"init"}


def test_gpu_lowerer_emits_normalized_age_lerp_rotation_and_attribute_stores():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-color", "particle.attribute.color"),
            GraphNodeRecord("set-size", "particle.attribute.size"),
            GraphNodeRecord("set-rotation", "particle.attribute.rotation"),
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
            GraphLinkRecord("stream-color", "root.update", "out", "set-color", "in", PortKind.EXEC),
            GraphLinkRecord("stream-size", "set-color", "out", "set-size", "in", PortKind.EXEC),
            GraphLinkRecord(
                "stream-rotation", "set-size", "out", "set-rotation", "in", PortKind.EXEC
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


def test_gpu_lowerer_emits_foundational_common_math_expressions():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-velocity", "particle.attribute.velocity"),
            GraphNodeRecord(
                "a", "common.constant.vec3", properties={"value": [1.0, 2.0, 3.0]}
            ),
            GraphNodeRecord(
                "b", "common.constant.vec3", properties={"value": [3.0, 2.0, 1.0]}
            ),
            GraphNodeRecord("cross", "common.vector.cross"),
            GraphNodeRecord("sine", "common.math.sine"),
            GraphNodeRecord(
                "clamp",
                "common.math.clamp",
                properties={"minimum": -0.5, "maximum": 0.5},
            ),
        ),
        links=(
            GraphLinkRecord(
                "exec", "root.update", "out", "set-velocity", "in", PortKind.EXEC
            ),
            GraphLinkRecord("a-cross", "a", "value", "cross", "a"),
            GraphLinkRecord("b-cross", "b", "value", "cross", "b"),
            GraphLinkRecord("cross-sine", "cross", "result", "sine", "value"),
            GraphLinkRecord("sine-clamp", "sine", "result", "clamp", "value"),
            GraphLinkRecord(
                "velocity-value", "clamp", "result", "set-velocity", "value"
            ),
        ),
    )
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    )
    source = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(hir)
    ).emitters[0].update

    assert "cross(" in source
    assert "sin(" in source
    assert "clamp(" in source
    assert ".a_builtin_velocity = " in source
    compiled = native._compile_compute_glsl_batch(
        {"update": source}, "particle-common-foundational-math"
    )
    assert set(compiled) == {"update"}


def test_gpu_mesh_orientation_and_nonuniform_scale_use_current_instance_abi():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "orientation",
                "particle.attribute.orientation",
                properties={"degrees": [10.0, 20.0, 30.0]},
            ),
            GraphNodeRecord(
                "scale",
                "particle.attribute.scale",
                properties={"value": [2.0, 0.5, 1.5]},
            ),
        ),
        links=(
            GraphLinkRecord("init-stream", "root.init", "out", "orientation", "in", PortKind.EXEC),
            GraphLinkRecord("scale-stream", "orientation", "out", "scale", "in", PortKind.EXEC),
        ),
    )
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "rotate",
                "particle.attribute.orientation",
                properties={"degrees": [90.0, 180.0, 270.0]},
            ),
        ),
        links=(GraphLinkRecord("update-stream", "root.update", "out", "rotate", "in", PortKind.EXEC),),
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
        links=(GraphLinkRecord("render-stream", "root.rendering", "out", "mesh", "in", PortKind.EXEC),),
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
    assert (
        "world_position -= surface_normal * (view.camera_up.y * world_texel * normal_scale)"
        in gpu_backend._MESH_VERTEX_GLSL
    )
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
            GraphNodeRecord("set-size", "particle.attribute.size"),
            GraphNodeRecord("set-color", "particle.attribute.color"),
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
            GraphLinkRecord("stream-size", "root.update", "out", "set-size", "in", PortKind.EXEC),
            GraphLinkRecord("stream-color", "set-size", "out", "set-color", "in", PortKind.EXEC),
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


def test_gpu_curve_and_gradient_parameters_use_fixed_hot_update_layouts():
    curve = Curve(
        (
            CurveKey(0.0, 0.25, 0.0, 1.0),
            CurveKey(1.0, 2.0, -0.5, 0.0),
        ),
        "repeat",
        "ping_pong",
    )
    gradient = Gradient(
        (
            GradientKey(0.0, (2.0, 0.0, 0.0, 1.0)),
            GradientKey(1.0, (0.0, 0.5, 1.0, 0.0)),
        ),
        "linear",
    )
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-size", "particle.attribute.size"),
            GraphNodeRecord("set-color", "particle.attribute.color"),
            GraphNodeRecord(
                "curve-parameter",
                "particle.parameter",
                properties={"parameter": "size-over-life"},
            ),
            GraphNodeRecord(
                "gradient-parameter",
                "particle.parameter",
                properties={"parameter": "color-over-life"},
            ),
            GraphNodeRecord("age", "particle.attribute.normalized_age"),
            GraphNodeRecord("curve", "common.curve.sample"),
            GraphNodeRecord("gradient", "common.gradient.sample"),
        ),
        links=(
            GraphLinkRecord(
                "stream-size",
                "root.update",
                "out",
                "set-size",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "stream-color",
                "set-size",
                "out",
                "set-color",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord("curve-input", "curve-parameter", "value", "curve", "curve"),
            GraphLinkRecord(
                "gradient-input",
                "gradient-parameter",
                "value",
                "gradient",
                "gradient",
            ),
            GraphLinkRecord("curve-time", "age", "value", "curve", "t"),
            GraphLinkRecord("gradient-time", "age", "value", "gradient", "t"),
            GraphLinkRecord("curve-size", "curve", "value", "set-size", "value"),
            GraphLinkRecord("gradient-color", "gradient", "color", "set-color", "value"),
        ),
    )
    asset = ParticleGraphAsset(
        parameters=(
            ParticleParameter(
                "size-over-life",
                "Size Over Life",
                TypeRef(ValueType.CURVE),
                curve.to_dict(),
            ),
            ParticleParameter(
                "color-over-life",
                "Color Over Life",
                TypeRef(ValueType.GRADIENT),
                gradient.to_dict(),
            ),
        ),
        emitters=(ParticleEmitterAsset(update=update),),
    )
    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    words = pack_gpu_particle_parameters(kernel.parameters)
    changed_words = pack_gpu_particle_parameters(
        kernel.parameters,
        {
            "size-over-life": Curve((CurveKey(0.0, 4.0),)).to_dict(),
            "color-over-life": Gradient(
                (GradientKey(0.0, (0.0, 1.0, 0.0, 1.0)),),
                "fixed",
            ).to_dict(),
        },
    )

    assert len(words) == (17 + 33) * 4
    assert len(changed_words) == len(words)
    assert changed_words != words
    source = GpuParticleGlslLowerer().lower(kernel).emitters[0].update
    # Parameter ABI order is canonical by stable ID: color first, then size.
    assert "inx_sample_gradient_parameter(0u," in source
    assert "inx_sample_curve_parameter(33u," in source
    assert f"index < {gpu_backend.MAX_RAMP_KEYS}u" in source
    compiled = native._compile_compute_glsl_batch(
        {"update": source},
        "particle-dynamic-curve-gradient-test",
    )
    assert set(compiled) == {"update"}


def test_gpu_layout_migration_descriptor_copies_stable_fields_and_packs_defaults():
    stable_id = "layout-emitter"
    cache_node_uid = "cache.temperature"
    cache_id = particle_attribute_cache_id("init", cache_node_uid)
    previous_asset = ParticleGraphAsset(
        stable_id="previous-layout",
        emitters=(ParticleEmitterAsset(stable_id=stable_id),),
    )
    next_asset = ParticleGraphAsset(
        stable_id="next-layout",
        emitters=(
            ParticleEmitterAsset(
                stable_id=stable_id,
                init=GraphDocument(
                    "particle.init",
                    nodes=(
                        GraphNodeRecord("root.init", "particle.root.init"),
                        GraphNodeRecord(
                            cache_node_uid,
                            "particle.attribute.cache",
                            properties={
                                "name": "Temperature",
                                "value_type": "f32",
                                "value_space": "none",
                                "composition": "set",
                                "value": 3.5,
                            },
                        ),
                    ),
                    links=(
                        GraphLinkRecord(
                            "root-cache",
                            "root.init",
                            "out",
                            cache_node_uid,
                            "in",
                            PortKind.EXEC,
                        ),
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
        if field["stable_id"] == cache_id
    )
    default_word = migration["default_state_words"][temperature["offset"] // 4]
    assert struct.unpack("<f", struct.pack("<I", default_word))[0] == pytest.approx(0.0)
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

    assert emitter.data_interface_layout["mesh_interfaces"] == [
        {
            "stable_id": "__emitter_shape_mesh",
            "name": "Emitter Shape Mesh",
            "mesh": mesh.to_dict(),
            "space": "emitter_local",
            "mesh_to_space": [
                1.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 1.0,
            ],
            "interface_index": 0,
            "metadata_offset": 0,
            "vertex_binding": 1,
            "triangle_binding": 2,
            "influence_binding": 3,
            "palette_binding": 4,
            "shape_mode": mode.value,
        }
    ]
    assert "set = 1, binding = 1" in emitter.init
    assert "set = 1, binding = 2" in emitter.init
    assert "set = 1, binding = 3" in emitter.init
    assert "set = 1, binding = 4" in emitter.init
    assert "InxMeshSkinInfluence" in emitter.init
    assert "uint bone_count" in emitter.init
    assert "inx_sample_mesh_shape_position" in emitter.init
    if mode is MeshEmissionMode.SURFACE:
        assert "uintBitsToFloat" in emitter.init
    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), f"particle-mesh-shape-{mode.value}"
    )
    assert set(compiled) == set(emitter.stages())


def test_gpu_mesh_parameter_exposes_all_static_mesh_attributes_and_compiles():
    source = '''\
from Infernux.particle import AssetReference, EmitterSettings, Parameter, ParticleEmitter, ParticleScript

class MeshSampling(ParticleScript):
    parameters = (
        Parameter(
            "surface-mesh",
            "Surface Mesh",
            "mesh",
            AssetReference(path_hint="Assets/Models/surface.fbx"),
        ),
    )

    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_position(ctx.sample_mesh_position(ctx.parameter("Surface Mesh"), (0.1, 0.2, 0.3)))
            particles.set_velocity(ctx.sample_mesh_normal(ctx.parameter("Surface Mesh"), (0.2, 0.3, 0.4), mode="edge"))
            particles.set_attribute("Mesh UV", ctx.sample_mesh_uv(ctx.parameter("Surface Mesh"), (0.3, 0.4, 0.5), mode="vertex"))
            particles.set_attribute("Mesh Barycentric", ctx.sample_mesh_barycentric(ctx.parameter("Surface Mesh"), (0.4, 0.5, 0.6)))

        def update(self, ctx, particles):
            particles.set_velocity(ctx.sample_mesh_tangent(ctx.parameter("Surface Mesh"), (0.5, 0.6, 0.7)))

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    hir = ParticleScriptCompiler().compile(source, source_name="MeshSampling.particle.py")
    emitter = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(hir)
    ).emitters[0]

    mesh_interfaces = emitter.data_interface_layout["mesh_interfaces"]
    assert len(mesh_interfaces) == 1
    assert mesh_interfaces[0]["stable_id"].startswith("sample.mesh.")
    assert mesh_interfaces[0]["mesh_parameter"] == "surface-mesh"
    assert mesh_interfaces[0]["mesh"] == AssetReference(
        path_hint="Assets/Models/surface.fbx"
    ).to_dict()
    combined = emitter.init + emitter.update
    assert ".position" in combined
    assert ".normal" in combined
    assert ".tangent" in combined
    assert ".uv" in combined
    assert ".barycentric" in combined
    assert "INX_MESH_SAMPLE_VERTEX" in combined
    assert "INX_MESH_SAMPLE_EDGE" in combined
    assert "INX_MESH_SAMPLE_SURFACE" in combined
    assert "edge_count" in combined
    assert "triangle_count +" in combined
    assert "transformed_tangent -= result.normal" in combined
    assert "inx_mesh_vertex_0" in combined
    assert "inx_mesh_palette_0" in combined
    assert "vec4 tangent" in combined
    assert "tangent_handedness" in combined
    assert "determinant(mat3(mesh_to_simulation))" in combined
    assert combined.count("readonly buffer InxMeshVertices0") == 2
    assert "InxMeshVertices1" not in combined
    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-mesh-data-interface"
    )
    assert set(compiled) == set(emitter.stages())


def test_gpu_mesh_sample_seed_hashes_particle_slot_and_explicit_input_overrides_it():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "seeded",
                "particle.mesh.sample",
                properties={
                    "mesh": AssetReference(guid="surface-mesh").to_dict(),
                    "seed": 123,
                },
            ),
            GraphNodeRecord(
                "explicit",
                "particle.mesh.sample",
                properties={
                    "mesh": AssetReference(guid="surface-mesh").to_dict(),
                    "seed": 999,
                },
            ),
            GraphNodeRecord(
                "coordinate",
                "common.constant.vec3",
                properties={"value": [0.1, 0.2, 0.3]},
            ),
            GraphNodeRecord("write.position", "particle.attribute.position"),
            GraphNodeRecord("write.velocity", "particle.attribute.velocity"),
        ),
        links=(
            GraphLinkRecord("exec.position", "root.update", "out", "write.position", "in", PortKind.EXEC),
            GraphLinkRecord("exec.velocity", "root.update", "out", "write.velocity", "in", PortKind.EXEC),
            GraphLinkRecord("seeded.position", "seeded", "position", "write.position", "value", PortKind.VALUE),
            GraphLinkRecord("coordinate.sample", "coordinate", "value", "explicit", "sample", PortKind.VALUE),
            GraphLinkRecord("explicit.normal", "explicit", "normal", "write.velocity", "value", PortKind.VALUE),
        ),
    )
    emitter = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(
            ParticleGraphCompiler().compile(
                ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
            )
        )
    ).emitters[0]
    source = emitter.update

    assert "inx_random01(123u, 0u, particle_index, 0u)" in source
    assert "inx_random01(123u, 1u, particle_index, 0u)" in source
    assert "inx_random01(123u, 2u, particle_index, 0u)" in source
    assert "inx_random01(999u" not in source
    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-mesh-seeded-sample"
    )
    assert set(compiled) == set(emitter.stages())


def test_gpu_mesh_parameter_words_accept_skinned_renderer_reference():
    parameter = gpu_backend.KernelParameter(
        stable_id="skinned-source",
        name="Skinned Source",
        value_type=TypeRef(ValueType.MESH),
        default=AssetReference(path_hint="Assets/Models/source.fbx").to_dict(),
        exposed=True,
        writable=False,
        slot=0,
    )
    source = {
        "$type": "component_ref",
        "game_object_id": 42,
        "component_type": "SkinnedMeshRenderer",
    }

    assert pack_gpu_particle_parameters(
        (parameter,), {"skinned-source": source}
    ) == (0, 0, 0, 0)
    with pytest.raises(
        GpuParticleCompileError,
        match="Mesh asset or SkinnedMeshRenderer",
    ):
        pack_gpu_particle_parameters(
            (parameter,),
            {
                "skinned-source": {
                    "$type": "component_ref",
                    "game_object_id": 42,
                    "component_type": "MeshRenderer",
                }
            },
        )


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


def test_gpu_sdf_distance_and_gradient_samples_share_one_volume_binding_and_compile():
    emitter = _sdf_sample_gpu_source().emitters[0]

    assert emitter.data_interface_layout["volume_interfaces"] == [
        {
            "kind": "sdf",
            "stable_id": "sample-field",
            "interface_index": 0,
            "texture_binding": 1,
            "filtering": "linear",
        }
    ]
    assert "float inx_sample_sdf_distance_0" in emitter.update
    assert "vec3 inx_sample_sdf_gradient_0" in emitter.update
    assert "inx_sample_sdf_distance_0(" in emitter.update
    assert "inx_sample_sdf_gradient_0(" in emitter.update

    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-sdf-sample-test"
    )
    assert set(compiled) == set(emitter.stages())


@pytest.mark.parametrize("mode,surface_literal", (("surface", "true"), ("volume", "false")))
def test_gpu_sdf_emitter_shape_uses_bounded_spawn_only_sampling_and_valid_spirv(
    mode, surface_literal
):
    emitter = _sdf_shape_gpu_source(mode).emitters[0]

    assert emitter.data_interface_layout["volume_interfaces"] == [
        {
            "kind": "sdf",
            "stable_id": "spawn-field",
            "interface_index": 0,
            "texture_binding": 1,
            "filtering": "linear",
        }
    ]
    assert "inx_sample_sdf_shape_position_0" in emitter.init
    assert f", {surface_literal})" in emitter.init
    assert "attempt < 16" in emitter.init
    assert "iteration < 8" in emitter.init
    assert emitter.init.count("inx_sample_sdf_shape_position_0") == 2
    assert emitter.update.count("inx_sample_sdf_shape_position_0") == 1

    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), f"particle-sdf-{mode}-shape-test"
    )
    assert set(compiled) == set(emitter.stages())


def test_generated_gpu_particle_kernels_compile_to_vulkan_spirv(tmp_path):
    validator = shutil.which("glslangValidator")
    if validator is None:
        pytest.skip("Vulkan SDK glslangValidator is unavailable")

    emitter = _gpu_source().emitters[0]
    assert "update_rendering_fused" in emitter.stages()
    for stage, source in emitter.stages().items():
        source_path = tmp_path / f"{stage}.comp"
        output_path = tmp_path / f"{stage}.spv"
        source_path.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [
                validator,
                "-V",
                "--target-env",
                "vulkan1.2",
                str(source_path),
                "-o",
                str(output_path),
            ],
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
    assert payload["emitters"][0]["update_render_fusion"] == source.emitters[0].update_render_fusion
    decoded = gpu_backend.decode_gpu_particle_spirv(payload, 0)
    assert decoded["update_render_fusion"] == source.emitters[0].update_render_fusion
    assert set(decoded["stages"]) == set(source.emitters[0].stages())
    descriptor = payload["emitters"][0]["stages"]["update"]
    assert descriptor["byte_size"] > 20
    assert len(descriptor["sha256"]) == 64
    assert len(descriptor["zlib_base64"]) < descriptor["byte_size"] * 2

    corrupted = copy.deepcopy(payload)
    corrupted["emitters"][0]["stages"]["update"]["sha256"] = "0" * 64
    with pytest.raises(GpuParticleCompileError, match="integrity validation"):
        validate_gpu_particle_spirv(corrupted, source)

    stale_graphics = copy.deepcopy(payload)
    stale_graphics["graphics_abi_hash"] = "0" * 64
    with pytest.raises(GpuParticleCompileError, match="header is incompatible"):
        validate_gpu_particle_spirv(stale_graphics, source)
