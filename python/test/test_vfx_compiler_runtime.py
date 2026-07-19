from __future__ import annotations

import json

import numpy as np
import pytest

from Infernux.core.vfx_system import VfxEmitter
from Infernux.core.vfx_system import VfxSystem
from Infernux.components.particle_system import ParticleSystem
from Infernux.core.asset_ref import ParticleGraphRef, VfxSystemRef
from Infernux.core.assets import AssetManager
from Infernux.core.material import Material
from Infernux.graph import (
    AssetReference,
    GraphDocument,
    GraphLinkRecord,
    GraphNodeRecord,
    PortKind,
)
from Infernux.particle import (
    EmitterSettings,
    ExecutionTarget,
    ParticleBurst,
    ParticleEmitterAsset,
    ParticleGraphAsset,
    ParticleRuntimeCompatibility,
    PointCache,
)
from Infernux.lib import AssetRegistry, SceneManager
from Infernux.vfx import CpuParticleRuntime, VfxCompileError, VfxGraphCompiler


def _two_output_rendering_graph(
    material: AssetReference | None = None,
) -> GraphDocument:
    rendering = ParticleEmitterAsset().rendering
    return GraphDocument(
        rendering.domain,
        (
            *rendering.nodes,
            GraphNodeRecord(
                "output.secondary",
                "particle.output.sprite",
                (280.0, 140.0),
                {"material": material.to_dict()} if material is not None else {},
            ),
        ),
        (
            *rendering.links,
            GraphLinkRecord(
                "root-to-secondary",
                "root.rendering",
                "out",
                "output.secondary",
                "in",
                PortKind.STREAM,
            ),
        ),
        rendering.metadata,
    )


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


class _MixedParticleNative:
    def __init__(self):
        self.program_batches = []
        self.frames = []
        self.removed_batches = []
        self.reset_emitters = []

    def _replace_gpu_particle_emitters(self, programs, removed):
        self.program_batches.append((programs, removed))
        return ""

    def _begin_gpu_particle_frame(self, emitter_id, *request):
        self.frames.append((emitter_id, request))
        return True

    def _reset_gpu_particle_emitter(self, emitter_id):
        self.reset_emitters.append(emitter_id)
        return True

    def _remove_gpu_particle_emitter(self, emitter_id):
        return True

    def submit_particle_instances(self, batch_id, *args, **kwargs):
        return None

    def remove_particle_batch(self, batch_id):
        self.removed_batches.append(batch_id)


def test_particle_system_runs_mixed_cpu_gpu_emitters_by_active_index(
    scene, monkeypatch, tmp_path
):
    source = tmp_path / "MixedTargets.particlegraph"
    graph = ParticleGraphAsset(
        stable_id="mixed-target-component",
        emitters=(
            ParticleEmitterAsset(
                stable_id="cpu-smoke",
                settings=EmitterSettings(
                    target=ExecutionTarget.CPU,
                    capacity=8,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 1),),
                ),
            ),
            ParticleEmitterAsset(
                stable_id="gpu-sparks",
                settings=EmitterSettings(
                    target=ExecutionTarget.GPU,
                    capacity=8,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 2),),
                ),
            ),
        ),
    )
    graph.save(str(source))
    native = _MixedParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    game_object = scene.create_game_object("MixedParticleGraphProbe")
    game_object.add_py_component(component)

    component.awake()
    component.start()
    component.update(0.0)

    assert component._runtime_target is ExecutionTarget.AUTO
    assert component._cpu_emitter_indices == [0]
    assert component._gpu_emitter_indices == [1]
    assert component.emitter_runtime_target(0) is ExecutionTarget.CPU
    assert component.emitter_runtime_target(1) is ExecutionTarget.GPU
    assert component.emitter_runtime_target(-1) is None
    assert component.emitter_runtime_target(True) is None
    assert component._runtimes[0].particle_count == 1
    assert component._gpu_controllers[0].simulation_step == 1
    assert len(native.program_batches[-1][0]) == 1

    cpu_step = component._runtimes[0].simulation_step
    gpu_step = component._gpu_controllers[0].simulation_step
    assert component.pause_emitter(1) is True
    assert component.pause_emitter(99) is False
    component.update(0.25)
    assert component._runtimes[0].simulation_step == cpu_step + 1
    assert component._gpu_controllers[0].simulation_step == gpu_step

    assert component.terminate_emitter(0) is True
    assert component._runtimes[0].particle_count == 0
    assert component.terminate_emitter(1) is True
    assert native.reset_emitters == [component._gpu_emitter_ids[0]]
    assert component.start_emitter(1) is True
    component.update(0.0)
    assert component._gpu_controllers[0].simulation_step == 1
    component._remove_native_batch()


def test_particle_system_only_persists_emitter_index_for_legacy_vfx_assets():
    modern = ParticleSystem()
    modern.graph = ParticleGraphRef(path_hint="Assets/Smoke.particlegraph")
    modern.emitter_index = 7

    modern_document = modern._serialize_fields_document()

    assert "emitter_index" not in modern_document

    legacy = ParticleSystem()
    legacy.system = VfxSystemRef(path_hint="Assets/Smoke.vfxsystem")
    legacy.emitter_index = 3
    legacy_document = legacy._serialize_fields_document()

    assert legacy_document["emitter_index"] == 3
    restored = ParticleSystem()
    restored._deserialize_fields_document(legacy_document)
    assert restored.emitter_index == 3
    assert "emitter_index" in restored._serialize_fields_document()


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


def test_particle_system_hot_reload_reorders_emitters_by_stable_id(
    scene, engine, monkeypatch, tmp_path
):
    source = tmp_path / "Reordered.particlegraph"

    def emitter(stable_id: str, count: int) -> ParticleEmitterAsset:
        return ParticleEmitterAsset(
            stable_id=stable_id,
            settings=EmitterSettings(
                capacity=8,
                spawn_rate=0.0,
                bursts=(ParticleBurst(0.0, count),),
            ),
        )

    smoke = emitter("smoke", 1)
    sparks = emitter("sparks", 2)
    ParticleGraphAsset(
        stable_id="reordered-system",
        emitters=(smoke, sparks),
    ).save(str(source))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    component.simulation_target = ExecutionTarget.CPU
    game_object = scene.create_game_object("ReorderedParticleGraphProbe")
    game_object.add_py_component(component)
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: engine))

    component.awake()
    component.start()
    component.update(0.0)
    assert component.pause_emitter(0) is True
    assert [runtime.particle_count for runtime in component._runtimes] == [1, 2]
    assert [runtime.simulation_step for runtime in component._runtimes] == [1, 1]

    ParticleGraphAsset(
        stable_id="reordered-system",
        emitters=(sparks, smoke),
    ).save(str(source))
    component.update(0.0)

    assert component._particle_metadata.schedule == ("sparks", "smoke")
    assert [runtime.particle_count for runtime in component._runtimes] == [2, 1]
    assert [runtime.simulation_step for runtime in component._runtimes] == [2, 1]
    assert [runtime.is_playing for runtime in component._runtimes] == [True, False]
    assert component.emitter_reload_compatibility(0) is ParticleRuntimeCompatibility.PARAMETER_ONLY
    assert component.emitter_reload_compatibility(1) is ParticleRuntimeCompatibility.PARAMETER_ONLY
    assert component.start_emitter(1) is True
    component.update(0.0)
    assert [runtime.simulation_step for runtime in component._runtimes] == [3, 2]
    component._remove_native_batch()


def test_particle_system_hot_reload_adds_and_removes_emitters_by_stable_id(
    scene, engine, monkeypatch, tmp_path
):
    source = tmp_path / "ChangedEmitterSet.particlegraph"

    def emitter(stable_id: str, count: int) -> ParticleEmitterAsset:
        return ParticleEmitterAsset(
            stable_id=stable_id,
            settings=EmitterSettings(
                capacity=8,
                spawn_rate=0.0,
                bursts=(ParticleBurst(0.0, count),),
            ),
        )

    smoke = emitter("smoke", 1)
    sparks = emitter("sparks", 2)
    mist = emitter("mist", 3)
    ParticleGraphAsset(
        stable_id="changed-emitter-set",
        emitters=(smoke, sparks),
    ).save(str(source))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    component.simulation_target = ExecutionTarget.CPU
    game_object = scene.create_game_object("ChangedEmitterSetProbe")
    game_object.add_py_component(component)
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: engine))

    component.awake()
    component.start()
    component.update(0.0)
    assert component.pause_emitter(1) is True
    assert [runtime.particle_count for runtime in component._runtimes] == [1, 2]

    ParticleGraphAsset(
        stable_id="changed-emitter-set",
        emitters=(sparks, mist),
    ).save(str(source))
    component.update(0.0)

    assert component._particle_metadata.schedule == ("sparks", "mist")
    assert [runtime.program.stable_id for runtime in component._runtimes] == [
        "sparks",
        "mist",
    ]
    assert [runtime.particle_count for runtime in component._runtimes] == [2, 3]
    assert [runtime.simulation_step for runtime in component._runtimes] == [1, 1]
    assert [runtime.is_playing for runtime in component._runtimes] == [False, True]
    assert (
        component.emitter_reload_compatibility(0)
        is ParticleRuntimeCompatibility.PARAMETER_ONLY
    )
    assert component.emitter_reload_compatibility(1) is None
    assert component.start_emitter(0) is True
    component.update(0.0)
    assert [runtime.simulation_step for runtime in component._runtimes] == [2, 2]
    component._remove_native_batch()


def test_saved_particle_graph_uses_real_gpu_runtime_control_path(
    scene, engine, monkeypatch, tmp_path
):
    source = tmp_path / "GpuSmoke.particlegraph"
    material_path = tmp_path / "GpuSmoke.mat"
    material = Material.create_unlit("Gpu Smoke")
    material.vert_shader_name = "particle_sprite"
    material.render_queue = 3075
    material.blend_enable = True
    material.depth_write_enable = False
    assert material.save(str(material_path))
    imported_material = engine.get_asset_database().import_asset(str(material_path))
    assert imported_material.guid
    material_ref = AssetReference(imported_material.guid, str(material_path))
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
                rendering=_two_output_rendering_graph(material_ref),
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
    assert engine._gpu_particle_output_count(emitter_id) == 2
    secondary_output_id = component._gpu_output_id("smoke", "output.secondary")
    assert engine._gpu_particle_output_semantics(emitter_id, secondary_output_id) == {
        "receive_scene_lighting": False,
        "receive_shadows": False,
        "sort_mode": "back_to_front",
    }
    assert (
        engine._gpu_particle_output_render_queue(emitter_id, secondary_output_id)
        == 3075
    )
    live_material = Material.load(str(material_path))
    assert live_material is not None
    live_material.render_queue = 3090
    assert (
        engine._gpu_particle_output_render_queue(emitter_id, secondary_output_id)
        == 3090
    )

    component.update(0.0)
    assert component._gpu_controllers[0].simulation_step == 1
    assert component.pause_emitter(999) is False
    assert component.pause_emitter(0) is True
    component.update(1.0)
    assert component._gpu_controllers[0].simulation_step == 1

    compatible_graph = ParticleGraphAsset(
        stable_id="gpu-smoke-component",
        emitters=(
            ParticleEmitterAsset(
                stable_id="smoke",
                settings=EmitterSettings(
                    target=ExecutionTarget.GPU,
                    capacity=32,
                    spawn_rate=4.0,
                    bursts=(ParticleBurst(0.0, 4),),
                ),
                rendering=_two_output_rendering_graph(material_ref),
            ),
        ),
    )
    compatible_revision = component._artifact_revision
    compatible_graph.save(str(source))
    component.update(0.0)
    assert component._artifact_revision > compatible_revision
    assert component._gpu_controllers[0].is_playing is False
    assert component._gpu_controllers[0].simulation_step == 1
    assert (
        component.emitter_reload_compatibility(0)
        is ParticleRuntimeCompatibility.PARAMETER_ONLY
    )
    assert engine._gpu_particle_state_was_preserved(emitter_id) is True

    resized_graph = ParticleGraphAsset(
        stable_id="gpu-smoke-component",
        emitters=(
            ParticleEmitterAsset(
                stable_id="smoke",
                settings=EmitterSettings(
                    target=ExecutionTarget.GPU,
                    capacity=64,
                    spawn_rate=4.0,
                    bursts=(ParticleBurst(0.0, 4),),
                ),
                rendering=_two_output_rendering_graph(material_ref),
            ),
        ),
    )
    resized_revision = component._artifact_revision
    resized_graph.save(str(source))
    component.update(0.0)
    assert component._artifact_revision > resized_revision
    assert component._gpu_controllers[0].is_playing is False
    assert component._gpu_controllers[0].simulation_step == 1
    assert (
        component.emitter_reload_compatibility(0)
        is ParticleRuntimeCompatibility.LAYOUT_MIGRATABLE
    )
    assert engine._gpu_particle_state_was_preserved(emitter_id) is True

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
                rendering=_two_output_rendering_graph(material_ref),
            ),
        ),
    )
    revised_graph.save(str(source))
    component.update(0.0)
    assert component._artifact_revision > previous_revision
    assert component._gpu_controllers[0].is_playing is False
    assert component._gpu_controllers[0].simulation_step == 0
    assert (
        component.emitter_reload_compatibility(0)
        is ParticleRuntimeCompatibility.EMITTER_RESTART
    )
    assert engine._gpu_particle_state_was_preserved(emitter_id) is False
    assert engine._gpu_particle_artifact_revision(emitter_id) == component._artifact_revision
    assert engine._gpu_particle_output_count(emitter_id) == 2
    assert (
        engine._gpu_particle_output_render_queue(emitter_id, secondary_output_id)
        == 3090
    )
    assert component.terminate_emitter(0) is True
    assert component.restart(0) is True

    component._remove_native_batch()
    assert engine._gpu_particle_artifact_revision(emitter_id) == 0
    assert engine._gpu_particle_output_count(emitter_id) == 0


def test_saved_gpu_particle_graph_binds_point_cache_through_rhi(
    scene, engine, monkeypatch, tmp_path
):
    point_cache_source = tmp_path / "SpawnPoints.pointcache"
    point_cache_source.write_text(
        json.dumps(
            {
                "$schema": "infernux.point_cache",
                "$version": 1,
                "stable_id": "gpu-spawn-points",
                "name": "GPU Spawn Points",
                "bake_basis": "right_handed_y_up",
                "point_count": 2,
                "channels": [
                    {
                        "name": "position",
                        "semantic": "position",
                        "type": "vec3",
                        "data": [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]],
                    },
                    {
                        "name": "id",
                        "semantic": "id",
                        "type": "u32",
                        "data": [0, 1],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    imported = AssetManager.import_asset(
        str(point_cache_source), database=engine.get_asset_database()
    )
    assert imported, imported.error
    assert imported.guid
    assert engine.get_asset_database().contains_path(str(point_cache_source))
    assert AssetRegistry.instance().load_point_cache_by_guid(imported.guid) is not None

    base_init = ParticleEmitterAsset().init
    init = GraphDocument(
        base_init.domain,
        (
            *base_init.nodes,
            GraphNodeRecord(
                "sample.spawn.position",
                "particle.point_cache.sample_position",
                (40.0, 120.0),
                {"interface": "spawn-points", "lookup": "index"},
            ),
            GraphNodeRecord(
                "set.spawn.velocity",
                "particle.init.set_velocity",
                (300.0, 0.0),
                {},
            ),
        ),
        (
            GraphLinkRecord(
                "root-to-set-velocity",
                "root.init",
                "out",
                "set.spawn.velocity",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "sample-to-velocity",
                "sample.spawn.position",
                "value",
                "set.spawn.velocity",
                "value",
                PortKind.VALUE,
            ),
        ),
        base_init.metadata,
    )
    source = tmp_path / "GpuPointCache.particlegraph"
    ParticleGraphAsset(
        stable_id="gpu-point-cache-system",
        emitters=(
            ParticleEmitterAsset(
                stable_id="gpu-point-cache-emitter",
                settings=EmitterSettings(
                    target=ExecutionTarget.GPU,
                    capacity=32,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 2),),
                ),
                data_interfaces=(
                    PointCache(
                        stable_id="spawn-points",
                        cache=AssetReference(imported.guid, str(point_cache_source)),
                    ),
                ),
                init=init,
            ),
        ),
    ).save(str(source))

    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    game_object = scene.create_game_object("GpuPointCacheProbe")
    game_object.add_py_component(component)
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: engine))

    component.awake()
    component.start()
    component.update(0.0)

    emitter_id = component._gpu_emitter_ids[0]
    assert engine._gpu_particle_artifact_revision(emitter_id) == component._artifact_revision
    assert component._gpu_controllers[0].simulation_step == 1
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
