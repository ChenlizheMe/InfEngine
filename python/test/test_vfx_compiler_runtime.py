from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import numpy as np
import pytest

from Infernux.components.particle_system import (
    ParticleBoundsMode,
    ParticleOffscreenPolicy,
    ParticleSystem,
)
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
    TypeRef,
    ValueType,
)
from Infernux.particle import (
    EmitterSettings,
    ParticleBurst,
    ParticleEmitterAsset,
    ParticleEventField,
    ParticleEventRoute,
    ParticleEventType,
    ParticleGraphAsset,
    ParticleParameter,
    ParticleRuntimeCompatibility,
    SdfVolume,
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
    sprite = SimpleNamespace(output_id="sprite", output_type="sprite", material=AssetReference())
    mesh = SimpleNamespace(output_id="mesh", output_type="mesh", material=AssetReference())

    component = ParticleSystem()
    sprite_state = component._gpu_material_binding(sprite)
    mesh_state = component._gpu_material_binding(mesh)

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


def test_gpu_soft_particle_material_state_is_transparent(monkeypatch):
    opaque_material = SimpleNamespace(
        render_queue=2000,
        blend_enable=False,
        depth_test_enable=True,
        depth_write_enable=True,
        native=object(),
    )
    monkeypatch.setattr(Material, "load", staticmethod(lambda _path: opaque_material))
    output = SimpleNamespace(
        output_id="soft-sprite",
        output_type="sprite",
        material=AssetReference(path_hint="Assets/VFX/Opaque.mat"),
        soft_particles=True,
    )

    component = ParticleSystem()
    assert component._gpu_material_binding(output) == {
        "render_queue": 2501,
        "blend_enabled": True,
        "depth_test_enabled": True,
        "depth_write_enabled": False,
        "native": opaque_material.native,
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


class _GpuParticleNative:
    def __init__(self):
        self.program_batches = []
        self.frames = []
        self.removed_batches = []
        self.reset_emitters = []
        self.event_domains = []
        self.parameter_updates = []
        self.external_events = []

    def _replace_gpu_particle_graph(
        self, graph_instance_id, programs, removed, event_domain=None
    ):
        self.program_batches.append((programs, removed, graph_instance_id))
        self.event_domains.append(event_domain)
        return ""

    def _begin_gpu_particle_batch(self, graph_instance_id, items):
        self.frames.append((graph_instance_id, items))
        return True

    def _update_gpu_particle_parameters(self, graph_instance_id, parameter_words):
        self.parameter_updates.append((graph_instance_id, list(parameter_words)))
        return ""

    def _queue_gpu_particle_events(
        self, graph_instance_id, channel_index, record_words, record_count
    ):
        self.external_events.append(
            (graph_instance_id, channel_index, list(record_words), record_count)
        )
        return ""

    def _reset_gpu_particle_emitter(self, emitter_id):
        self.reset_emitters.append(emitter_id)
        return True

    def _gpu_particle_artifact_revision(self, emitter_id):
        return 17

    def _gpu_particle_state_was_preserved(self, emitter_id):
        return True

    def _gpu_particle_event_abi_hash(self, graph_instance_id):
        return 41

    def _gpu_particle_event_domain_serial(self, graph_instance_id):
        return 7

    def _request_gpu_particle_diagnostics(self, graph_instance_id):
        self.diagnostic_graph_instance_id = graph_instance_id
        return 91

    def _poll_gpu_particle_diagnostics(self, request_id):
        return {
            "request_id": request_id,
            "graph_instance_id": self.diagnostic_graph_instance_id,
            "status": "completed",
            "error": "",
            "emitters": [
                {
                    "emitter_id": 17,
                    "emitter_index": 1,
                    "capacity": 8,
                    "free_count": 5,
                    "alive_count": 3,
                    "visible_count": 3,
                    "dropped_count": 0,
                    "bounds_mode": "manual",
                    "bounds_valid": True,
                    "bounds_lower": [-10.0, -6.0, -10.0],
                    "bounds_upper": [10.0, 6.0, 10.0],
                }
            ],
            "events": [],
        }

    def _request_gpu_particle_view_diagnostics(self, graph_instance_id, view):
        self.view_diagnostic_graph_instance_id = graph_instance_id
        self.view_diagnostic_view = view
        return 92

    def _poll_gpu_particle_view_diagnostics(self, view, request_id):
        return {
            "request_id": request_id,
            "graph_instance_id": self.view_diagnostic_graph_instance_id,
            "view": view,
            "status": "completed",
            "error": "",
            "outputs": [
                {
                    "output_id": 23,
                    "output_stable_id": "ribbon-output",
                    "emitter_id": 17,
                    "emitter_index": 1,
                    "capacity": 8,
                    "source_count": 5,
                    "visible_count": 3,
                    "draw_vertex_count": 6,
                    "draw_instance_count": 3,
                    "bounds_valid": True,
                    "coarse_rejected": False,
                    "cull_mode": "ribbon_segments",
                }
            ],
        }


def test_particle_system_sends_typed_external_events_to_gpu_ring(
    scene, monkeypatch, tmp_path
):
    source = tmp_path / "RuntimeEvents.particlegraph"
    ParticleGraphAsset(
        stable_id="runtime-event-system",
        event_types=(
            ParticleEventType(
                "impact",
                "Impact",
                8,
                (
                    ParticleEventField(
                        "strength", "Strength", TypeRef(ValueType.F32), 1.0
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
                "impact-to-sparks",
                "impact",
                "source",
                "update",
                "target",
                2,
            ),
        ),
        emitters=(
            ParticleEmitterAsset(stable_id="source", name="Source"),
            ParticleEmitterAsset(stable_id="target", name="Sparks"),
        ),
    ).save(str(source))
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    game_object = scene.create_game_object("RuntimeEventProbe")
    game_object.add_py_component(component)

    component.awake()
    component.start()
    schema = component.runtime_event_schema()
    assert len(schema) == 1
    assert schema[0]["stable_id"] == "impact-to-sparks"
    assert schema[0]["event_type_name"] == "Impact"
    assert schema[0]["target_emitter_name"] == "Sparks"
    assert component.send_event(
        "Impact",
        {"Strength": 2.5, "direction": [1.0, 2.0, 3.0]},
        2,
        target="Sparks",
    )

    graph_id, channel, words, count = native.external_events[-1]
    assert graph_id == component._batch_id
    assert channel == 0
    assert count == 2
    assert len(words) == 16
    assert words[:4] == [0, 0, 0xFFFFFFFF, 0]
    assert words[:8] == words[8:]
    with pytest.raises(KeyError, match="unknown fields"):
        component.send_event("Impact", {"missing": 1.0})


def test_particle_system_exposed_parameter_updates_live_gpu_block(
    scene, monkeypatch, tmp_path
):
    source = tmp_path / "GpuParameters.particlegraph"
    ParticleGraphAsset(
        stable_id="gpu-parameter-component",
        parameters=(
            ParticleParameter(
                "smoke-density",
                "Density",
                TypeRef(ValueType.F32),
                0.25,
                True,
                "Smoke",
                "Controls the smoke density.",
            ),
        ),
        emitters=(
            ParticleEmitterAsset(
                stable_id="gpu-smoke",
                settings=EmitterSettings(capacity=8),
            ),
        ),
    ).save(str(source))
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    game_object = scene.create_game_object("GpuParticleParameterProbe")
    game_object.add_py_component(component)

    component.awake()
    component.start()
    component.set_float("Density", 0.75)

    assert component.has_parameter("smoke-density")
    assert component.get_float("Density") == pytest.approx(0.75)
    assert component.exposed_parameter_schema() == [
        {
            "stable_id": "smoke-density",
            "name": "Density",
            "type": "f32",
            "default": 0.25,
            "value": 0.75,
            "category": "Smoke",
            "tooltip": "Controls the smoke density.",
        }
    ]
    assert component.runtime_diagnostics()["parameters"][0]["value"] == 0.75
    assert json.loads(component._parameter_overrides_json) == {
        "smoke-density": 0.75
    }
    assert len(native.parameter_updates) == 1
    graph_instance_id, words = native.parameter_updates[0]
    assert graph_instance_id == component._batch_id
    assert len(words) == 4
    assert words[1:] == [0, 0, 0]
    with pytest.raises(TypeError, match="not vec3"):
        component.set_vector3("Density", 1.0, 2.0, 3.0)


def test_particle_system_has_no_instance_resource_override_contract():
    assert not hasattr(ParticleSystem, "resource_schema")
    assert not hasattr(ParticleSystem, "set_resource")
    assert not hasattr(ParticleSystem, "set_data_interface_asset")


def test_particle_system_manual_bounds_are_local_and_transform_to_world_aabb():
    from Infernux.lib import Vector3

    component = ParticleSystem()
    component.bounds_mode = ParticleBoundsMode.MANUAL
    component.manual_bounds_center = Vector3(1.0, 2.0, 3.0)
    component.manual_bounds_size = Vector3(-4.0, 2.0, 6.0)
    emitter_to_world = np.asarray(
        [
            [-2.0, 0.0, 0.0, 10.0],
            [0.0, 3.0, 0.0, -5.0],
            [0.0, 0.0, 4.0, 2.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float32,
    )

    mode, lower, upper = component._gpu_bounds_request(emitter_to_world)

    assert mode == "manual"
    assert lower == pytest.approx([4.0, -2.0, 2.0])
    assert upper == pytest.approx([12.0, 4.0, 26.0])

    component.bounds_mode = ParticleBoundsMode.AUTOMATIC
    assert component._gpu_bounds_request(emitter_to_world) == (
        "automatic",
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
    )


def test_particle_emitter_lifecycle_is_serialized_on_the_component_instance(
    scene, monkeypatch
):
    class _Runtime:
        def __init__(self):
            self.actions = []

        def play(self):
            self.actions.append("play")

        def pause(self):
            self.actions.append("pause")

        def reset(self, *, playing):
            self.actions.append(("reset", playing))

    component = ParticleSystem()
    scene.create_game_object("EmitterInstancePolicy").add_py_component(component)
    component.awake()
    component._particle_metadata = SimpleNamespace(
        parameters=(),
        emitters=(
            SimpleNamespace(
                stable_id="smoke",
                name="Smoke",
                enabled=False,
                play_on_start=False,
            ),
        ),
    )
    runtime = _Runtime()
    component._gpu_emitter_indices = [0]
    component._gpu_controllers = [runtime]
    monkeypatch.setattr(component, "_reset_gpu_emitters", lambda *_args: None)

    assert component.emitter_instance_schema() == [
        {
            "index": 0,
            "stable_id": "smoke",
            "name": "Smoke",
            "enabled": True,
            "play_on_start": True,
        }
    ]
    assert component.set_emitter_options("Smoke", play_on_start=False)
    assert runtime.actions == ["pause"]
    assert json.loads(component._emitter_overrides_json) == {
        "smoke": {"enabled": True, "play_on_start": False}
    }
    assert component.set_emitter_options("smoke", enabled=False)
    assert runtime.actions[-1] == ("reset", False)
    assert component._emitter_is_enabled(0) is False


def test_particle_system_exposes_texture2d_as_a_typed_parameter(scene):
    default = AssetReference("default-texture", "Assets/VFX/DefaultSmoke.png")
    override = AssetReference("override-texture", "Assets/VFX/Smoke.png")
    metadata = SimpleNamespace(
        parameters=(
            SimpleNamespace(
                stable_id="smoke-texture",
                name="Smoke Texture",
                value_type=TypeRef(ValueType.TEXTURE2D),
                default=default.to_dict(),
                exposed=True,
                category="Smoke",
                tooltip="Smoke sprite sheet.",
            ),
        ),
        emitters=(),
    )
    component = ParticleSystem()
    scene.create_game_object("TextureParameter").add_py_component(component)
    component.awake()
    component._particle_metadata = metadata

    assert component.exposed_parameter_schema() == [
        {
            "stable_id": "smoke-texture",
            "name": "Smoke Texture",
            "type": "texture2d",
            "default": default.to_dict(),
            "value": default.to_dict(),
            "category": "Smoke",
            "tooltip": "Smoke sprite sheet.",
        }
    ]
    component.set_texture("Smoke Texture", override)
    assert component.get_texture("smoke-texture") == override
    assert json.loads(component._parameter_overrides_json) == {
        "smoke-texture": override.to_dict()
    }
    assert component.reset_parameter("Smoke Texture")
    assert component.get_texture("smoke-texture") == default
    assert component._parameter_overrides_json == "{}"


def test_live_texture_parameter_rebuilds_binding_and_rolls_back_on_failure(
    scene, monkeypatch
):
    default = AssetReference("default-texture", "Assets/VFX/DefaultSmoke.png")
    override = AssetReference("override-texture", "Assets/VFX/Smoke.png")
    parameter = SimpleNamespace(
        stable_id="smoke-texture",
        name="Smoke Texture",
        value_type=TypeRef(ValueType.TEXTURE2D),
        default=default.to_dict(),
        exposed=True,
        category="",
        tooltip="",
    )
    component = ParticleSystem()
    scene.create_game_object("LiveTextureParameter").add_py_component(component)
    component.awake()
    component._particle_metadata = SimpleNamespace(parameters=(parameter,), emitters=())
    monkeypatch.setattr(component, "_has_runtime", lambda: True)
    rebuilds = []
    monkeypatch.setattr(
        component,
        "_compile_asset",
        lambda *, force=False: rebuilds.append(force) or False,
    )

    with pytest.raises(RuntimeError, match="could not rebuild"):
        component.set_texture("Smoke Texture", override)

    assert rebuilds == [True]
    assert component.get_texture("Smoke Texture") == default
    assert component._parameter_overrides_json == "{}"


def test_particle_system_runs_gpu_emitters_by_active_index(
    scene, monkeypatch, tmp_path
):
    source = tmp_path / "GpuTargets.particlegraph"
    graph = ParticleGraphAsset(
        stable_id="gpu-target-component",
        emitters=(
            ParticleEmitterAsset(
                stable_id="gpu-smoke",
                name="Smoke",
                settings=EmitterSettings(
                    capacity=8,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 1),),
                ),
            ),
            ParticleEmitterAsset(
                stable_id="gpu-sparks",
                name="Sparks",
                settings=EmitterSettings(
                    capacity=8,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 2),),
                ),
            ),
        ),
    )
    graph.save(str(source))
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    game_object = scene.create_game_object("GpuParticleGraphProbe")
    game_object.layer = 3
    game_object.add_py_component(component)

    component.awake()
    component.start()
    component.update(0.0)

    assert component._gpu_emitter_indices == [0, 1]
    assert [runtime.simulation_step for runtime in component._gpu_controllers] == [1, 1]
    assert len(native.program_batches[-1][0]) == 2
    assert all(
        program["owner_object_id"] == int(game_object.id)
        for program in native.program_batches[-1][0]
    )
    assert all(
        program["owner_layer_mask"] == 1 << 3
        for program in native.program_batches[-1][0]
    )
    assert native.program_batches[-1][0][0]["graph_instance_id"] == component._batch_id
    assert native.program_batches[-1][2] == component._batch_id
    assert len(native.frames) == 1
    assert native.frames[0][0] == component._batch_id
    assert [item["emitter_id"] for item in native.frames[0][1]] == component._gpu_emitter_ids
    assert all(
        item["bounds_mode"] == "automatic"
        and item["offscreen_policy"] == "always_simulate"
        and item["force_simulation"] is False
        and item["manual_bounds_lower"] == [0.0, 0.0, 0.0]
        and item["manual_bounds_upper"] == [0.0, 0.0, 0.0]
        for item in native.frames[0][1]
    )
    assert all(item["simulation_time_ticks"] == 0 for item in native.frames[0][1])
    assert all(program["continuation"] is None for program in native.program_batches[-1][0])
    component.update(0.125)
    assert all(
        item["simulation_time_ticks"] == 125_000_000
        for item in native.frames[-1][1]
    )
    component.pause()
    component.update(0.5)
    assert all(
        item["simulation_time_ticks"] == 125_000_000
        for item in native.frames[-1][1]
    )
    component.play()
    component.offscreen_policy = ParticleOffscreenPolicy.PAUSE_WHEN_OFFSCREEN
    component.update(0.0)
    assert all(
        item["offscreen_policy"] == "pause_when_offscreen"
        for item in native.frames[-1][1]
    )
    component._editor_preview_active = True
    assert component.editor_preview_set_emitter_muted(1, True) is True
    component.update(0.0)
    assert native.frames[-1][1][0]["render"] is True
    assert native.frames[-1][1][1]["render"] is False
    assert component.editor_preview_set_emitter_muted(1, False) is True
    component._editor_preview_active = False
    diagnostics = component.runtime_diagnostics()
    assert diagnostics["event_abi_hash"] == 41
    assert diagnostics["event_domain_serial"] == 7
    assert diagnostics["play_requested"] is True
    assert diagnostics["resident"] is True
    assert diagnostics["playing"] is True
    assert "runtime_target" not in diagnostics
    assert all("target" not in emitter for emitter in diagnostics["emitters"])
    assert all(emitter["play_requested"] is True for emitter in diagnostics["emitters"])
    assert all(emitter["resident"] is True for emitter in diagnostics["emitters"])
    assert all(emitter["playing"] is True for emitter in diagnostics["emitters"])
    assert diagnostics["emitters"][1]["artifact_revision"] == 17
    assert diagnostics["emitters"][1]["state_preserved"] is True

    artifact_revision = native._gpu_particle_artifact_revision
    native._gpu_particle_artifact_revision = lambda _emitter_id: 0
    inactive = component.runtime_diagnostics()
    assert inactive["play_requested"] is True
    assert inactive["resident"] is False
    assert inactive["playing"] is False
    assert all(emitter["play_requested"] is True for emitter in inactive["emitters"])
    assert all(emitter["resident"] is False for emitter in inactive["emitters"])
    assert all(emitter["playing"] is False for emitter in inactive["emitters"])
    native._gpu_particle_artifact_revision = artifact_revision

    diagnostic_request = component.request_gpu_diagnostics()
    gpu_diagnostics = component.poll_gpu_diagnostics(diagnostic_request)
    assert diagnostic_request == 91
    assert gpu_diagnostics["status"] == "completed"
    assert gpu_diagnostics["emitters"][0]["stable_id"] == "gpu-sparks"
    assert gpu_diagnostics["emitters"][0]["alive_count"] == 3
    assert gpu_diagnostics["emitters"][0]["bounds_mode"] == "manual"
    assert gpu_diagnostics["emitters"][0]["bounds_valid"] is True
    assert gpu_diagnostics["emitters"][0]["bounds_lower"] == [-10.0, -6.0, -10.0]
    view_request = component.request_gpu_view_diagnostics("GAME")
    view_diagnostics = component.poll_gpu_view_diagnostics("game", view_request)
    assert view_request == 92
    assert native.view_diagnostic_view == "game"
    assert view_diagnostics["outputs"][0]["emitter_stable_id"] == "gpu-sparks"
    assert view_diagnostics["outputs"][0]["visible_count"] == 3
    assert view_diagnostics["outputs"][0]["cull_mode"] == "ribbon_segments"
    with pytest.raises(ValueError, match="scene.*game"):
        component.request_gpu_view_diagnostics("preview")

    first_step = component._gpu_controllers[0].simulation_step
    second_step = component._gpu_controllers[1].simulation_step
    assert component.pause_emitter("Sparks") is True
    assert component.pause_emitter("Missing") is False
    assert component.pause_emitter(99) is False
    component.update(0.25)
    assert component._gpu_controllers[0].simulation_step == first_step + 1
    assert component._gpu_controllers[1].simulation_step == second_step

    assert component.terminate_emitter("Smoke") is True
    assert component.terminate_emitter("gpu-sparks") is True
    assert native.reset_emitters == component._gpu_emitter_ids
    assert component.start_emitter("Sparks") is True
    component.update(0.0)
    assert component._gpu_controllers[0].simulation_step == 0
    assert component._gpu_controllers[1].simulation_step == 1

    preserved_step = component._gpu_controllers[1].simulation_step
    assert component.editor_preview_pause() is False
    component._editor_preview_active = True
    assert component.editor_preview_pause() is True
    assert component.editor_preview_begin() is True
    assert component._gpu_controllers[1].simulation_step == preserved_step
    published_gpu_ids = list(component._gpu_emitter_ids)
    component._remove_native_batch()
    assert native.program_batches[-1] == ([], published_gpu_ids, component._batch_id)


def test_particle_system_publishes_saved_event_domain_with_complete_gpu_graph(
    scene, monkeypatch, tmp_path
):
    source = tmp_path / "GpuEvents.particlegraph"
    graph = ParticleGraphAsset(
        stable_id="gpu-event-component",
        emitters=(
            ParticleEmitterAsset(
                stable_id="source",
                settings=EmitterSettings(capacity=32),
            ),
            ParticleEmitterAsset(
                stable_id="target",
                settings=EmitterSettings(capacity=64),
            ),
        ),
        event_types=(ParticleEventType("impact", "Impact", 128),),
        event_routes=(
            ParticleEventRoute(
                "source-impact-target",
                "impact",
                "source",
                "update",
                "target",
                2,
            ),
        ),
    )
    graph.save(str(source))
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    game_object = scene.create_game_object("GpuEventGraphProbe")
    game_object.add_py_component(component)

    component.awake()
    component.start()

    assert len(native.program_batches[-1][0]) == 2
    event_domain = native.event_domains[-1]
    assert event_domain["event_abi_hash"] != 0
    assert event_domain["channels"][0]["stable_event_type_hash"] != 0
    assert {
        key: value
        for key, value in event_domain["channels"][0].items()
        if key != "stable_event_type_hash"
    } == {
        "source_emitter_index": 0,
        "target_emitter_index": 1,
        "event_type_index": 0,
        "payload_stride_words": 0,
        "capacity": 128,
        "spawn_count": 2,
    }
    component._remove_native_batch()
    assert native.event_domains[-1] is None


def test_saved_particle_graph_uses_real_gpu_runtime_control_path(
    scene, engine, monkeypatch, tmp_path
):
    source = tmp_path / "GpuSmoke.particlegraph"
    material_path = tmp_path / "GpuSmoke.mat"
    material = Material.create_unlit("Gpu Smoke")
    material.vert_shader_name = "Particle Sprite"
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
            GraphNodeRecord(
                "position",
                "particle.attribute.get",
                properties={"attribute": "builtin.position"},
            ),
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


def test_saved_gpu_particle_graph_binds_sdf_texture3d_through_rhi(
    scene, engine, monkeypatch, tmp_path
):
    sdf_source = tmp_path / "Collision.inxsdf"

    def document(distances):
        return {
            "$schema": "infernux.sdf",
            "dimensions": [2, 2, 2],
            "storage_order": "x_fastest",
            "distance_unit": "field",
            "bake_basis": [
                1.0, 0.0, 0.0, 0.0,
                0.0, 1.0, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 1.0,
            ],
            "distances": distances,
        }

    sdf_source.write_text(
        json.dumps(document([-0.2, 0.2, -0.2, 0.2, -0.2, 0.2, -0.2, 0.2])),
        encoding="utf-8",
    )
    imported = AssetManager.import_asset(
        str(sdf_source), database=engine.get_asset_database()
    )
    assert imported, imported.error
    texture = AssetRegistry.instance().load_texture_by_guid(imported.guid)
    assert texture is not None
    assert texture.dimension == "3d"
    assert texture.semantic == "signed_distance_field"

    update = GraphDocument(
        "particle.update",
        (
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "collision.sdf",
                "particle.update.collide_sdf",
                properties={"interface": "collision-field", "particle_radius": 0.02},
            ),
        ),
        (
            GraphLinkRecord(
                "root-to-sdf",
                "root.update",
                "out",
                "collision.sdf",
                "in",
                PortKind.STREAM,
            ),
        ),
    )
    source = tmp_path / "GpuSdfCollision.particlegraph"
    ParticleGraphAsset(
        stable_id="gpu-sdf-system",
        emitters=(
            ParticleEmitterAsset(
                stable_id="gpu-sdf-emitter",
                settings=EmitterSettings(
                    capacity=32,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 2),),
                ),
                data_interfaces=(
                    SdfVolume(
                        stable_id="collision-field",
                        texture=AssetReference(imported.guid, str(sdf_source)),
                    ),
                ),
                update=update,
            ),
        ),
    ).save(str(source))

    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    game_object = scene.create_game_object("GpuSdfProbe")
    game_object.add_py_component(component)
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: engine))

    component.awake()
    component.start()
    component.update(0.0)

    emitter_id = component._gpu_emitter_ids[0]
    generation = engine._gpu_particle_vector_field_generation(emitter_id, 0)
    assert generation > 0
    assert component._gpu_controllers[0].simulation_step == 1
    component._remove_native_batch()
    assert engine._gpu_particle_artifact_revision(emitter_id) == 0


def test_particle_system_requires_a_saved_gpu_artifact_and_graphical_renderer(
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

    assert component._has_runtime() is False
    assert "AOT artifact" in component._last_compile_error
