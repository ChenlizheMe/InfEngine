from __future__ import annotations

import numpy as np
import pytest

from Infernux.core.vfx_system import VfxEmitter
from Infernux.core.vfx_system import VfxSystem
from Infernux.components.particle_system import ParticleSystem
from Infernux.core.asset_ref import ParticleGraphRef
from Infernux.particle import (
    EmitterSettings,
    ExecutionTarget,
    ParticleBurst,
    ParticleEmitterAsset,
    ParticleGraphAsset,
)
from Infernux.lib import SceneManager
from Infernux.vfx import CpuParticleRuntime, VfxCompileError, VfxGraphCompiler


def _compiled_emitter(*, rate: float = 4.0, capacity: int = 32):
    emitter = VfxEmitter(capacity=capacity)
    graph = emitter.graph
    rate_value = graph.add_node("vfx_float", uid="rate_value", value=rate)
    spawn = graph.add_node("vfx_spawn_rate", uid="spawn")
    velocity = graph.add_node("vfx_set_velocity", uid="velocity", value=[0.0, 2.0, 0.0])
    lifetime = graph.add_node("vfx_set_lifetime", uid="lifetime", value=2.0)
    gravity = graph.add_node("vfx_gravity", uid="gravity", strength=-1.0)
    output = graph.add_node("vfx_billboard_output", uid="output")

    assert graph.add_link(rate_value.uid, "value", spawn.uid, "rate")
    assert graph.add_link(spawn.uid, "exec_out", velocity.uid, "exec_in")
    assert graph.add_link(velocity.uid, "exec_out", lifetime.uid, "exec_in")
    assert graph.add_link(lifetime.uid, "exec_out", gravity.uid, "exec_in")
    assert graph.add_link(gravity.uid, "exec_out", output.uid, "exec_in")
    return emitter, VfxGraphCompiler().compile(emitter)


def test_vfx_compiler_freezes_stage_order_and_linked_constant():
    emitter, artifact = _compiled_emitter(rate=12.0, capacity=64)

    assert artifact.capacity == 64
    assert [item.opcode for item in artifact.spawn] == ["spawn_rate"]
    assert artifact.spawn[0].parameter_dict()["rate"] == 12.0
    assert [item.opcode for item in artifact.initialize] == ["set_velocity", "set_lifetime"]
    assert [item.opcode for item in artifact.update] == ["gravity"]
    assert [item.opcode for item in artifact.output] == ["billboard_output"]
    assert {name for name, _, _ in artifact.attributes} >= {
        "position", "velocity", "color", "size", "age", "lifetime"
    }
    assert emitter.graph.find_node("spawn").data == {}


def test_vfx_compiler_requires_one_output_and_forward_stage_flow():
    emitter = VfxEmitter()
    with pytest.raises(VfxCompileError, match="exactly one Billboard Output"):
        VfxGraphCompiler().compile(emitter)


@pytest.mark.parametrize(
    ("type_id", "data", "message"),
    [
        ("vfx_set_velocity", {"value": [1.0, 2.0]}, "exactly 3 numbers"),
        ("vfx_float", {"value": float("nan")}, "must be finite"),
        ("vfx_set_size", {"value": -1.0}, "must be non-negative"),
        ("vfx_gravity", {"typo": 1.0}, "unknown parameter"),
    ],
)
def test_vfx_compiler_rejects_invalid_node_parameters(type_id, data, message):
    emitter = VfxEmitter()
    node = emitter.graph.add_node(type_id, uid="invalid", **data)
    output = emitter.graph.add_node("vfx_billboard_output", uid="output")
    if node.type_id != "vfx_float":
        assert emitter.graph.add_link(node.uid, "exec_out", output.uid, "exec_in")
    with pytest.raises(VfxCompileError, match=message):
        VfxGraphCompiler().compile(emitter)


def test_vfx_compiler_converts_vec3_constant_to_color():
    emitter = VfxEmitter()
    color = emitter.graph.add_node("vfx_vec3", uid="color", value=[0.2, 0.4, 0.6])
    initialize = emitter.graph.add_node("vfx_set_color", uid="initialize")
    output = emitter.graph.add_node("vfx_billboard_output", uid="output")
    assert emitter.graph.add_link(color.uid, "value", initialize.uid, "value")
    assert emitter.graph.add_link(initialize.uid, "exec_out", output.uid, "exec_in")

    artifact = VfxGraphCompiler().compile(emitter)

    assert artifact.initialize[0].parameter_dict()["value"] == [0.2, 0.4, 0.6, 1.0]

    spawn = emitter.graph.add_node("vfx_spawn_rate", uid="spawn")
    output = emitter.graph.add_node("vfx_billboard_output", uid="output")
    assert emitter.graph.add_link(output.uid, "exec_out", spawn.uid, "exec_in")
    with pytest.raises(VfxCompileError, match="cannot move from output back to spawn"):
        VfxGraphCompiler().compile(emitter)


def test_cpu_particle_runtime_emits_updates_and_returns_contiguous_instances():
    _, artifact = _compiled_emitter(rate=4.0)
    runtime = CpuParticleRuntime(artifact)

    instances = runtime.tick(0.5)

    assert runtime.particle_count == 2
    assert instances.shape == (2, 9)
    assert instances.dtype == np.float32
    assert instances.flags.c_contiguous
    assert np.allclose(instances[:, 1], 0.75)
    assert np.allclose(instances[:, 3], 1.0)
    assert np.allclose(instances[:, 4:8], 1.0)


def test_cpu_particle_runtime_burst_runs_once_and_reuses_instance_storage():
    emitter = VfxEmitter(capacity=8)
    burst = emitter.graph.add_node("vfx_burst", uid="burst", count=3)
    output = emitter.graph.add_node("vfx_billboard_output", uid="output")
    assert emitter.graph.add_link(burst.uid, "exec_out", output.uid, "exec_in")
    runtime = CpuParticleRuntime(VfxGraphCompiler().compile(emitter))

    first = runtime.tick(0.0)
    first_pointer = first.__array_interface__["data"][0]
    second = runtime.tick(0.0)

    assert runtime.particle_count == 3
    assert second.shape == (3, 9)
    assert second.__array_interface__["data"][0] == first_pointer


def test_particle_system_component_runs_in_scene_play_mode(scene, engine, monkeypatch):
    emitter, _ = _compiled_emitter(rate=8.0, capacity=32)
    component = ParticleSystem()
    component.system = VfxSystem(name="Play Mode VFX", emitters=[emitter])
    game_object = scene.create_game_object("ParticleSystemProbe")
    game_object.add_py_component(component)
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: engine))

    manager = SceneManager.instance()
    try:
        manager.play()
        assert manager.is_playing()
        component.awake()
        component.start()
        for _ in range(3):
            component.update(0.25)
        assert engine.gpu_residency_snapshot["particle_bytes"] > 0
    finally:
        if manager.is_playing():
            manager.stop()
        component._remove_native_batch()
    assert engine.gpu_residency_snapshot["particle_bytes"] == 0


def test_particle_system_runs_multi_emitter_graph_and_controls_each_emitter(
    scene, engine, monkeypatch
):
    graph = ParticleGraphAsset(
        stable_id="multi-emitter-component",
        emitters=(
            ParticleEmitterAsset(
                stable_id="smoke",
                settings=EmitterSettings(
                    capacity=8,
                    spawn_rate=1.0,
                    bursts=(ParticleBurst(0.0, 1),),
                ),
            ),
            ParticleEmitterAsset(
                stable_id="sparks",
                settings=EmitterSettings(
                    capacity=8,
                    spawn_rate=2.0,
                    bursts=(ParticleBurst(0.0, 2),),
                ),
            ),
        ),
    )
    component = ParticleSystem()
    component.graph = graph
    game_object = scene.create_game_object("ParticleGraphProbe")
    game_object.add_py_component(component)
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: engine))

    component.awake()
    component.start()
    component.update(0.0)

    assert [runtime.particle_count for runtime in component._runtimes] == [1, 2]
    first_step = component._runtimes[0].simulation_step
    second_step = component._runtimes[1].simulation_step

    component.pause(0)
    component.play(999)
    component.update(0.5)

    assert component._runtimes[0].simulation_step == first_step
    assert component._runtimes[1].simulation_step == second_step + 1

    component.stop(1)
    component.stop(-1)

    assert component._runtimes[1].particle_count == 0
    component._remove_native_batch()


def test_particle_system_hot_switches_to_new_published_artifact_revision(
    scene, engine, monkeypatch, tmp_path
):
    source = tmp_path / "HotSmoke.particlegraph"
    first = ParticleGraphAsset(
        stable_id="hot-smoke",
        emitters=(
            ParticleEmitterAsset(
                stable_id="smoke",
                settings=EmitterSettings(
                    capacity=8,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 1),),
                ),
            ),
        ),
    )
    first.save(str(source))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    component.simulation_target = ExecutionTarget.CPU
    game_object = scene.create_game_object("HotParticleGraphProbe")
    game_object.add_py_component(component)
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: engine))

    component.awake()
    component.start()
    component.update(0.0)
    first_revision = component._artifact_revision
    assert component._runtimes[0].particle_count == 1

    second = ParticleGraphAsset(
        stable_id="hot-smoke",
        emitters=(
            ParticleEmitterAsset(
                stable_id="smoke",
                settings=EmitterSettings(
                    capacity=8,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 4),),
                ),
            ),
        ),
    )
    second.save(str(source))
    component.update(0.0)

    assert component._artifact_revision > first_revision
    assert component._runtimes[0].particle_count == 4
    component._remove_native_batch()


def test_saved_particle_graph_uses_real_gpu_runtime_control_path(
    scene, engine, monkeypatch, tmp_path
):
    source = tmp_path / "GpuSmoke.particlegraph"
    graph = ParticleGraphAsset(
        stable_id="gpu-smoke-component",
        emitters=(
            ParticleEmitterAsset(
                stable_id="smoke",
                settings=EmitterSettings(
                    target=ExecutionTarget.GPU,
                    capacity=32,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 4),),
                ),
            ),
        ),
    )
    graph.save(str(source))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    game_object = scene.create_game_object("GpuParticleGraphProbe")
    game_object.add_py_component(component)
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: engine))

    component.awake()
    component.start()

    assert component._runtime_target is ExecutionTarget.GPU
    assert component._runtimes == []
    assert len(component._gpu_controllers) == 1
    emitter_id = component._gpu_emitter_ids[0]
    assert engine._gpu_particle_artifact_revision(emitter_id) == component._artifact_revision

    component.update(0.0)
    assert component._gpu_controllers[0].simulation_step == 1
    assert component.pause_emitter(999) is False
    assert component.pause_emitter(0) is True
    component.update(1.0)
    assert component._gpu_controllers[0].simulation_step == 1
    previous_revision = component._artifact_revision
    revised_graph = ParticleGraphAsset(
        stable_id="gpu-smoke-component",
        emitters=(
            ParticleEmitterAsset(
                stable_id="smoke",
                settings=EmitterSettings(
                    target=ExecutionTarget.GPU,
                    capacity=64,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 8),),
                ),
            ),
        ),
    )
    revised_graph.save(str(source))
    component.update(0.0)
    assert component._artifact_revision > previous_revision
    assert component._gpu_controllers[0].is_playing is False
    assert engine._gpu_particle_artifact_revision(emitter_id) == component._artifact_revision
    assert component.terminate_emitter(0) is True
    assert component.restart(0) is True

    component._remove_native_batch()
    assert engine._gpu_particle_artifact_revision(emitter_id) == 0


def test_particle_system_simulation_does_not_depend_on_a_graphical_renderer(
    scene, monkeypatch
):
    graph = ParticleGraphAsset(
        stable_id="logic-only-particles",
        emitters=(
            ParticleEmitterAsset(
                settings=EmitterSettings(
                    capacity=4,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 2),),
                ),
            ),
        ),
    )
    component = ParticleSystem()
    component.graph = graph
    game_object = scene.create_game_object("LogicOnlyParticleGraphProbe")
    game_object.add_py_component(component)
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: None))

    component.awake()
    component.start()
    component.update(0.0)

    assert component._runtimes[0].particle_count == 2
    assert component._runtimes[0].simulation_step == 1


@pytest.mark.parametrize("delta_time", [-0.1, float("nan"), float("inf")])
def test_cpu_particle_runtime_rejects_invalid_delta_time(delta_time):
    _, artifact = _compiled_emitter()
    with pytest.raises(ValueError, match="finite and non-negative"):
        CpuParticleRuntime(artifact).tick(delta_time)
