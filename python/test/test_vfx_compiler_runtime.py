from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import numpy as np
import pytest

from Infernux.components.particle_system import ParticleSystem
from Infernux.core.asset_ref import ParticleGraphRef
from Infernux.core.assets import AssetManager
from Infernux.core.material import Material
from Infernux.debug import Debug
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
    VectorField,
)
from Infernux.lib import AssetRegistry


particle_system_module = importlib.import_module("Infernux.components.particle_system")


def test_gpu_particle_default_material_state_matches_output_geometry(monkeypatch):
    fallback_sprite = SimpleNamespace(
        render_queue=3000,
        blend_enable=True,
        depth_test_enable=True,
        depth_write_enable=False,
        native=object(),
    )
    requested_builtins: list[str] = []

    def get_builtin(name: str):
        requested_builtins.append(name)
        return fallback_sprite

    monkeypatch.setattr(Material, "get", staticmethod(get_builtin))
    sprite = SimpleNamespace(output_type="sprite", material=AssetReference())
    mesh = SimpleNamespace(output_type="mesh", material=AssetReference())

    sprite_state = ParticleSystem._gpu_material_binding(sprite)
    mesh_state = ParticleSystem._gpu_material_binding(mesh)

    assert requested_builtins == ["ParticleSpriteMaterial"]
    assert sprite_state == {
        "render_queue": 3000,
        "blend_enabled": True,
        "depth_test_enabled": True,
        "depth_write_enabled": False,
        "native": fallback_sprite.native,
    }
    assert mesh_state == {
        "render_queue": 2000,
        "blend_enabled": False,
        "depth_test_enabled": True,
        "depth_write_enabled": True,
        "native": None,
    }


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


def test_particle_system_editor_preview_reuses_runtime_without_play_mode(
    scene, engine, monkeypatch
):
    component = ParticleSystem()
    component.graph = ParticleGraphAsset(stable_id="editor-preview")
    game_object = scene.create_game_object("ParticlePreviewProbe")
    game_object.add_py_component(component)
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: engine))

    assert component.editor_preview_begin() is True
    assert component._editor_preview_active is True
    assert component.editor_preview_update(0.25, 0.5) is True

    step = component._runtimes[0].simulation_step
    assert component.editor_preview_pause() is True
    component.editor_preview_update(0.25)
    assert component._runtimes[0].simulation_step == step

    assert component.editor_preview_play() is True
    assert component.editor_preview_stop() is True
    assert component._runtimes == []
    assert component._gpu_controllers == []
    component.editor_preview_end()
    assert component._editor_preview_active is False
    component._remove_native_batch()


def test_particle_system_throttles_repeated_compile_failures(scene, monkeypatch):
    component = ParticleSystem()
    component.graph = ParticleGraphAsset(stable_id="compile-failure-throttle")
    game_object = scene.create_game_object("CompileFailureThrottleProbe")
    game_object.add_py_component(component)
    component.awake()

    now = [100.0]
    attempts = []
    errors = []
    monkeypatch.setattr(particle_system_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        component,
        "_compile_particle_graph",
        lambda graph: attempts.append(graph) or False,
    )
    monkeypatch.setattr(Debug, "log_error", staticmethod(errors.append))

    assert component._compile_asset() is False
    assert len(attempts) == 1
    now[0] = 100.5
    assert component._compile_asset() is False
    assert len(attempts) == 1
    now[0] = 101.0
    assert component._compile_asset() is False
    assert len(attempts) == 2

    component._report_compile_failure(RuntimeError("invalid particle material"))
    component._report_compile_failure(RuntimeError("invalid particle material"))
    assert len(errors) == 1
    now[0] = 106.0
    component._report_compile_failure(RuntimeError("invalid particle material"))
    assert len(errors) == 2


class _MixedParticleNative:
    def __init__(self):
        self.program_batches = []
        self.frames = []
        self.removed_batches = []
        self.reset_emitters = []

    def _replace_gpu_particle_graph(self, graph_instance_id, programs, removed):
        self.program_batches.append((programs, removed, graph_instance_id))
        return ""

    def _begin_gpu_particle_batch(self, graph_instance_id, items):
        self.frames.append((graph_instance_id, items))
        return True

    def _reset_gpu_particle_emitter(self, emitter_id):
        self.reset_emitters.append(emitter_id)
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
    game_object.layer = 3
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
    assert native.program_batches[-1][0][0]["owner_object_id"] == int(game_object.id)
    assert native.program_batches[-1][0][0]["owner_layer_mask"] == 1 << 3
    assert native.program_batches[-1][0][0]["graph_instance_id"] == component._batch_id
    assert native.program_batches[-1][2] == component._batch_id
    assert len(native.frames) == 1
    assert native.frames[0][0] == component._batch_id
    assert [item["emitter_id"] for item in native.frames[0][1]] == component._gpu_emitter_ids

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
    published_gpu_ids = list(component._gpu_emitter_ids)
    component._remove_native_batch()
    assert native.program_batches[-1] == ([], published_gpu_ids, component._batch_id)


def test_local_particle_instances_follow_emitter_transform_and_scale():
    instances = np.array(
        [[1.0, 2.0, 3.0, 0.5, 1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0]],
        dtype=np.float32,
    )
    emitter_to_world = np.array(
        [
            [2.0, 0.0, 0.0, 10.0],
            [0.0, 3.0, 0.0, 20.0],
            [0.0, 0.0, 4.0, 30.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float32,
    )

    transformed = ParticleSystem._local_instances_to_world(instances, emitter_to_world)

    assert transformed[0, :3].tolist() == pytest.approx([12.0, 26.0, 42.0])
    assert transformed[0, 3] == pytest.approx(0.5)
    assert transformed[0, 9:12].tolist() == pytest.approx([2.0, 3.0, 4.0])
    assert instances[0, :4].tolist() == pytest.approx([1.0, 2.0, 3.0, 0.5])


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
                    target=ExecutionTarget.CPU,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 1),),
                ),
            ),
        ),
    )
    first.save(str(source))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
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
                    target=ExecutionTarget.CPU,
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
                target=ExecutionTarget.CPU,
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
                target=ExecutionTarget.CPU,
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
        "cast_shadows": False,
        "soft_particles": False,
        "soft_distance": 1.0,
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
    override_source = tmp_path / "OverridePoints.pointcache"
    override_document = json.loads(point_cache_source.read_text(encoding="utf-8"))
    override_document["stable_id"] = "gpu-override-points"
    override_document["name"] = "GPU Override Points"
    override_document["channels"][0]["data"] = [
        [10.0, 20.0, 30.0],
        [40.0, 50.0, 60.0],
    ]
    override_source.write_text(json.dumps(override_document), encoding="utf-8")
    override_imported = AssetManager.import_asset(
        str(override_source), database=engine.get_asset_database()
    )
    assert override_imported, override_imported.error

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
    assert component.set_data_interface_asset(
        "spawn-points",
        guid=override_imported.guid,
        path_hint=str(override_source),
    )
    assert component.data_interface_asset("spawn-points") == AssetReference(
        override_imported.guid, str(override_source)
    )
    component.start()
    component.update(0.0)

    active_layout = component._gpu_data_interface_layout(
        component._particle_kernel.emitters[0], component._particle_gpu_layouts[0]
    )
    assert active_layout["point_caches"][0]["native"].guid == override_imported.guid
    assert (
        component._resolve_point_cache(
            component._particle_kernel.emitters[0].data_interfaces[0]
        ).guid
        == override_imported.guid
    )
    assert component.clear_data_interface_asset("spawn-points")
    assert component.data_interface_asset("spawn-points") is None
    default_layout = component._gpu_data_interface_layout(
        component._particle_kernel.emitters[0], component._particle_gpu_layouts[0]
    )
    assert default_layout["point_caches"][0]["native"].guid == imported.guid
    assert not component.set_data_interface_asset(
        "spawn-points", guid="missing-point-cache-guid"
    )
    assert component.data_interface_asset("spawn-points") is None

    emitter_id = component._gpu_emitter_ids[0]
    assert engine._gpu_particle_artifact_revision(emitter_id) == component._artifact_revision
    assert component._gpu_controllers[0].simulation_step == 1
    initial_artifact_revision = component._artifact_revision
    initial_generation = engine._gpu_particle_point_cache_generation(emitter_id, 0)
    assert initial_generation > 0
    assert not component.set_data_interface_asset(
        "missing-interface", guid=override_imported.guid
    )

    stress_step = component._gpu_controllers[0].simulation_step
    for index in range(12):
        if index % 2 == 0:
            assert component.set_data_interface_asset(
                "spawn-points",
                guid=override_imported.guid,
                path_hint=str(override_source),
            )
        else:
            assert component.clear_data_interface_asset("spawn-points")
        component.update(0.0)
        stress_step += 1
        assert component._gpu_controllers[0].simulation_step == stress_step
        assert component._artifact_revision == initial_artifact_revision
        assert engine._gpu_particle_point_cache_generation(emitter_id, 0) > 0

    updated_cache = json.loads(point_cache_source.read_text(encoding="utf-8"))
    updated_cache["channels"][0]["data"][0] = [9.0, 8.0, 7.0]
    point_cache_source.write_text(json.dumps(updated_cache), encoding="utf-8")
    reimported = AssetManager.reimport_asset(
        str(point_cache_source), database=engine.get_asset_database()
    )
    assert reimported, reimported.error
    component.update(0.0)

    assert component._artifact_revision == initial_artifact_revision
    assert component._gpu_controllers[0].simulation_step == stress_step + 1
    assert (
        engine._gpu_particle_point_cache_generation(emitter_id, 0)
        == initial_generation + 1
    )
    component._remove_native_batch()
    assert engine._gpu_particle_artifact_revision(emitter_id) == 0


def test_saved_gpu_particle_graph_binds_vector_field_texture3d_through_rhi(
    scene, engine, monkeypatch, tmp_path
):
    vector_field_source = tmp_path / "Wind.inxvfield"

    def document(vectors):
        return {
            "$schema": "infernux.vector_field",
            "dimensions": [2, 1, 1],
            "storage_order": "x_fastest",
            "bake_basis": [
                1.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 1.0,
            ],
            "vectors": vectors,
        }

    vector_field_source.write_text(
        json.dumps(document([[1.0, 0.0, 0.0], [0.0, 2.0, 0.0]])),
        encoding="utf-8",
    )
    imported = AssetManager.import_asset(
        str(vector_field_source), database=engine.get_asset_database()
    )
    assert imported, imported.error
    texture = AssetRegistry.instance().load_texture_by_guid(imported.guid)
    assert texture is not None
    assert texture.dimension == "3d"
    assert texture.semantic == "vector_field"

    update = GraphDocument(
        "particle.update",
        (
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("acceleration", "particle.update.acceleration"),
            GraphNodeRecord("position", "particle.attribute.read_vec3"),
            GraphNodeRecord(
                "sample.wind",
                "particle.vector_field.sample",
                properties={"interface": "wind"},
            ),
        ),
        (
            GraphLinkRecord(
                "root-to-acceleration",
                "root.update",
                "out",
                "acceleration",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "position-to-sample",
                "position",
                "value",
                "sample.wind",
                "position",
                PortKind.VALUE,
            ),
            GraphLinkRecord(
                "sample-to-acceleration",
                "sample.wind",
                "value",
                "acceleration",
                "value",
                PortKind.VALUE,
            ),
        ),
    )
    source = tmp_path / "GpuVectorField.particlegraph"
    ParticleGraphAsset(
        stable_id="gpu-vector-field-system",
        emitters=(
            ParticleEmitterAsset(
                stable_id="gpu-vector-field-emitter",
                settings=EmitterSettings(
                    target=ExecutionTarget.GPU,
                    capacity=32,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 2),),
                ),
                data_interfaces=(
                    VectorField(
                        stable_id="wind",
                        texture=AssetReference(imported.guid, str(vector_field_source)),
                    ),
                ),
                update=update,
            ),
        ),
    ).save(str(source))

    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    game_object = scene.create_game_object("GpuVectorFieldProbe")
    game_object.add_py_component(component)
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: engine))

    component.awake()
    component.start()
    component.update(0.0)

    emitter_id = component._gpu_emitter_ids[0]
    initial_artifact_revision = component._artifact_revision
    initial_generation = engine._gpu_particle_vector_field_generation(emitter_id, 0)
    assert initial_generation > 0
    assert component._gpu_controllers[0].simulation_step == 1

    vector_field_source.write_text(
        json.dumps(document([[3.0, 4.0, 5.0], [6.0, 7.0, 8.0]])),
        encoding="utf-8",
    )
    reimported = AssetManager.reimport_asset(
        str(vector_field_source), database=engine.get_asset_database()
    )
    assert reimported, reimported.error
    for _ in range(4):
        component.update(0.0)
        if engine._gpu_particle_vector_field_generation(emitter_id, 0) > initial_generation:
            break

    assert component._artifact_revision == initial_artifact_revision
    assert engine._gpu_particle_vector_field_generation(emitter_id, 0) == initial_generation + 1
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
