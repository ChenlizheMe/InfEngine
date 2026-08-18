from __future__ import annotations

import importlib
import json
import struct
import time
from types import SimpleNamespace

import numpy as np
import pytest

from Infernux.components.particle_system import (
    ParticleBoundsMode,
    ParticleOffscreenPolicy,
    ParticleSystem,
    _normalize_mesh_source_value,
)
from Infernux.components.ref_wrappers import ComponentRef
from Infernux.core.asset_ref import ParticleGraphRef
from Infernux.core.assets import AssetManager
from Infernux.core.material import Material
from Infernux.debug import Debug
from Infernux.application import Application
from Infernux.engine.undo import PythonComponentDocumentCommand
from Infernux.graph import (
    AssetReference,
    GraphDocument,
    GraphLinkRecord,
    GraphNodeRecord,
    PortKind,
    TypeRef,
    ValueType,
)
from Infernux.graph.ramp import Curve, CurveKey, Gradient, GradientKey
from Infernux.particle import (
    EmitterSettings,
    ParticleBurst,
    ParticleEmitterAsset,
    ParticleEventField,
    ParticleEventFlow,
    ParticleEventType,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleKernelLowerer,
    ParticleParameter,
    ParticleRuntimeCompatibility,
    SdfVolume,
    VectorField,
    default_event_graph,
)
from Infernux.lib import AssetRegistry, GameObject


particle_system_module = importlib.import_module("Infernux.components.particle_system")


def _particle_artifact_load_probe(monkeypatch, tmp_path, *, editor: bool):
    source = tmp_path / "Recovery.particlegraph"
    source.write_text("{}", encoding="utf-8")
    artifact = SimpleNamespace(
        hir={},
        kernel_ir={},
        gpu_glsl={"emitters": []},
        gpu_spirv={},
        revision=1,
        source_key=str(source),
    )
    calls = []
    monkeypatch.setattr(Application, "is_editor", staticmethod(lambda: editor))
    monkeypatch.setattr(
        particle_system_module.ParticleArtifactRegistry,
        "get",
        staticmethod(lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        particle_system_module.ParticleArtifactRegistry,
        "load_runtime_reference",
        staticmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("stale artifact"))),
    )
    monkeypatch.setattr(
        particle_system_module.ParticleArtifactRegistry,
        "compile_path",
        staticmethod(lambda path, **kwargs: calls.append((path, kwargs)) or artifact),
    )
    component = ParticleSystem()
    monkeypatch.setattr(
        component,
        "_publish_gpu_particle_graph",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        particle_system_module,
        "decode_particle_runtime_metadata",
        lambda _hir: SimpleNamespace(parameters=(), emitters=()),
    )
    monkeypatch.setattr(
        particle_system_module.ParticleKernelProgram,
        "from_dict",
        staticmethod(lambda _value: SimpleNamespace(emitters=())),
    )
    assert component._load_particle_graph_artifact(
        ParticleGraphRef(path_hint=str(source))
    ) is editor
    return calls


def test_editor_recovers_invalid_particle_artifact_from_authoring_source(
    monkeypatch, tmp_path
):
    calls = _particle_artifact_load_probe(monkeypatch, tmp_path, editor=True)

    assert len(calls) == 1
    assert calls[0][0] == str(tmp_path / "Recovery.particlegraph")


def test_editor_recompiles_after_loaded_artifact_fails_current_publish(
    monkeypatch, tmp_path
):
    source = tmp_path / "PublishRecovery.particlegraph"
    source.write_text("{}", encoding="utf-8")
    stale_artifact = SimpleNamespace(
        hir={},
        kernel_ir={},
        gpu_glsl={"emitters": []},
        gpu_spirv={},
        revision=1,
        source_key=str(source),
    )
    rebuilt_artifact = SimpleNamespace(
        hir={},
        kernel_ir={},
        gpu_glsl={"emitters": []},
        gpu_spirv={},
        revision=2,
        source_key=str(source),
    )
    compile_calls = []
    publish_calls = []
    monkeypatch.setattr(Application, "is_editor", staticmethod(lambda: True))
    monkeypatch.setattr(
        particle_system_module.ParticleArtifactRegistry,
        "get",
        staticmethod(lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(
        particle_system_module.ParticleArtifactRegistry,
        "load_runtime_reference",
        staticmethod(lambda *_args, **_kwargs: stale_artifact),
    )
    monkeypatch.setattr(
        particle_system_module.ParticleArtifactRegistry,
        "compile_path",
        staticmethod(
            lambda path, **kwargs: compile_calls.append((path, kwargs))
            or rebuilt_artifact
        ),
    )
    monkeypatch.setattr(
        particle_system_module,
        "decode_particle_runtime_metadata",
        lambda _hir: SimpleNamespace(parameters=(), emitters=()),
    )
    monkeypatch.setattr(
        particle_system_module.ParticleKernelProgram,
        "from_dict",
        staticmethod(lambda _value: SimpleNamespace(emitters=())),
    )
    component = ParticleSystem()

    def publish(*_args, **_kwargs):
        publish_calls.append(True)
        if len(publish_calls) == 1:
            raise RuntimeError("missing update_render_fusion")

    monkeypatch.setattr(component, "_publish_gpu_particle_graph", publish)

    assert component._load_particle_graph_artifact(
        ParticleGraphRef(path_hint=str(source))
    ) is True
    assert len(compile_calls) == 1
    assert len(publish_calls) == 2


def test_player_rejects_invalid_particle_artifact_without_source_compilation(
    monkeypatch, tmp_path
):
    calls = _particle_artifact_load_probe(monkeypatch, tmp_path, editor=False)

    assert calls == []


def test_particle_runtime_batch_ids_do_not_alias_reused_scene_ids(monkeypatch):
    owner = SimpleNamespace(id=28, layer=0)
    first = ParticleSystem()
    second = ParticleSystem()
    first._component_id = 90
    second._component_id = 90
    monkeypatch.setattr(first, "_try_get_game_object", lambda: owner)
    monkeypatch.setattr(second, "_try_get_game_object", lambda: owner)

    first._initialize_runtime_state(True)
    second._initialize_runtime_state(True)

    assert first._batch_id > 0
    assert second._batch_id > 0
    assert first._batch_id != second._batch_id


def test_inactive_particle_runtime_allocates_graph_identity_on_first_preview_access():
    component = ParticleSystem()
    component._gpu_controllers = []
    component._gpu_emitter_ids = []
    component._batch_id = 0

    component._ensure_runtime_state()

    assert component._batch_id > 0
    assert component._gpu_controllers == []
    assert component._gpu_emitter_ids == []


def test_particle_override_sync_is_dirty_driven(monkeypatch):
    component = ParticleSystem()
    component._initialize_runtime_state(False)

    module = particle_system_module
    original_get_raw = module.get_raw_field_value
    reads = []

    def tracked_get_raw(owner, name):
        reads.append(name)
        return original_get_raw(owner, name)

    monkeypatch.setattr(module, "get_raw_field_value", tracked_get_raw)
    component._sync_serialized_instance_overrides()
    assert reads == []

    component._parameter_overrides_json = '{"density":0.5}'
    component._sync_serialized_instance_overrides()

    assert reads == [
        "_parameter_overrides_json",
        "_parameter_overrides_json",
        "_emitter_overrides_json",
    ]
    assert component._instance_overrides_dirty is False
    assert component._parameter_overrides == {"density": 0.5}


def test_gpu_particle_default_material_state_matches_output_geometry(monkeypatch):
    def runtime_material(*, transparent: bool):
        value = SimpleNamespace(
            render_queue=3000 if transparent else 2000,
            blend_enable=transparent,
            depth_test_enable=True,
            depth_write_enable=not transparent,
            native=SimpleNamespace(),
            frag_shader_name="Particle Unlit",
            vert_shader_name="",
        )
        value.clone = lambda: runtime_material(transparent=transparent)
        return value

    fallback_sprite = runtime_material(transparent=True)
    requested_builtins: list[str] = []

    def get_builtin(name: str):
        requested_builtins.append(name)
        return fallback_sprite

    monkeypatch.setattr(Material, "get", staticmethod(get_builtin))
    monkeypatch.setattr(
        Material,
        "create_unlit",
        staticmethod(lambda _name: runtime_material(transparent=False)),
    )
    sprite = SimpleNamespace(
        output_id="sprite",
        output_type="sprite",
        shader="Particle Unlit",
        shader_properties=(),
    )
    mesh = SimpleNamespace(
        output_id="mesh",
        output_type="mesh",
        shader="Particle Unlit",
        shader_properties=(),
    )

    component = ParticleSystem()
    component._output_materials = {}
    component._parameter_overrides = {}
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
        "native": component._output_materials[("", "mesh")].native,
    }
    assert component._output_materials[("", "sprite")].vert_shader_name == "Particle Sprite"
    assert component._output_materials[("", "mesh")].vert_shader_name == "Particle Sprite"


def test_gpu_soft_particle_material_state_is_transparent(monkeypatch):
    opaque_material = SimpleNamespace(
        render_queue=2000,
        blend_enable=False,
        depth_test_enable=True,
        depth_write_enable=True,
        native=object(),
    )
    opaque_material.frag_shader_name = "Particle Unlit"
    opaque_material.vert_shader_name = ""
    monkeypatch.setattr(
        Material, "create_unlit", staticmethod(lambda _name: opaque_material)
    )
    monkeypatch.setattr(Material, "get", staticmethod(lambda _name: None))
    output = SimpleNamespace(
        output_id="soft-sprite",
        output_type="sprite",
        shader="Particle Unlit",
        shader_properties=(),
        soft_particles=True,
    )

    component = ParticleSystem()
    component._output_materials = {}
    component._parameter_overrides = {}
    assert component._gpu_material_binding(output) == {
        "render_queue": 2501,
        "blend_enabled": True,
        "depth_test_enabled": True,
        "depth_write_enabled": False,
        "native": opaque_material.native,
    }


def _two_output_rendering_graph(
    shader: str = "Particle Unlit",
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
                {"shader": shader},
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
                PortKind.EXEC,
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
        "_load_particle_graph_artifact",
        lambda graph: attempts.append(graph) or False,
    )
    monkeypatch.setattr(Debug, "log_error", staticmethod(errors.append))

    assert component._load_saved_artifact() is False
    assert len(attempts) == 1
    now[0] = 100.5
    assert component._load_saved_artifact() is False
    assert len(attempts) == 1
    now[0] = 101.0
    assert component._load_saved_artifact() is False
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
        self.playing_updates = []
        self.parameter_updates = []
        self.diagnostic_state_samples = []
        self.accept_batches = True

    def _replace_gpu_particle_graph(self, graph_instance_id, programs, removed):
        self.program_batches.append((programs, removed, graph_instance_id))
        return ""

    def _begin_gpu_particle_batch(self, graph_instance_id, items):
        self.frames.append((graph_instance_id, items))
        return self.accept_batches

    def _update_gpu_particle_parameters(self, graph_instance_id, parameter_words):
        self.parameter_updates.append((graph_instance_id, list(parameter_words)))
        return ""

    def _reset_gpu_particle_emitter(self, emitter_id):
        self.reset_emitters.append(emitter_id)
        return True

    def _set_gpu_particle_emitter_playing(self, emitter_id, playing):
        self.playing_updates.append((emitter_id, bool(playing)))
        return True

    def _gpu_particle_artifact_revision(self, emitter_id):
        return 17

    def _gpu_particle_state_was_preserved(self, emitter_id):
        return True

    def _request_gpu_particle_diagnostics(
        self, graph_instance_id, sample_frames=60, state_sample_count=0
    ):
        self.diagnostic_graph_instance_id = graph_instance_id
        self.diagnostic_sample_frames = sample_frames
        self.diagnostic_state_sample_count = state_sample_count
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
                    "collision_hit_count": 17,
                    "collision_response_count": 13,
                    "collision_trigger_count": 4,
                    "collision_enter_count": 5,
                    "collision_stay_count": 11,
                    "collision_exit_count": 3,
                    "collision_max_outward_speed": 4.5,
                    "collision_max_tangent_speed": 2.25,
                    "event_overflow_counts": [],
                    "event_enqueue_counts": [],
                    "event_complete_counts": [],
                    "bounds_mode": "manual",
                    "bounds_valid": True,
                    "bounds_lower": [-10.0, -6.0, -10.0],
                    "bounds_upper": [10.0, 6.0, 10.0],
                    "state_samples": list(self.diagnostic_state_samples),
                }
            ],
            "events": [],
        }

    def _request_gpu_particle_view_diagnostics(
        self, graph_instance_id, view, camera_component_id=0
    ):
        self.view_diagnostic_graph_instance_id = graph_instance_id
        self.view_diagnostic_view = view
        self.view_diagnostic_camera_component_id = camera_component_id
        return 92

    def _poll_gpu_particle_view_diagnostics(
        self, view, request_id, camera_component_id=0
    ):
        return {
            "request_id": request_id,
            "graph_instance_id": self.view_diagnostic_graph_instance_id,
            "view": view,
            "camera_component_id": camera_component_id,
            "render_view_id": 701,
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
                    "sort_mode": "none",
                    "sort_group_count_x": 1,
                    "sorter_allocated": False,
                    "bounds_valid": True,
                    "coarse_rejected": False,
                    "cull_mode": "ribbon_segments",
                }
            ],
        }


def test_particle_system_exposes_graph_defined_event_schema_without_external_routes(
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
        emitters=(
            ParticleEmitterAsset(
                stable_id="source",
                name="Source",
                event_flows=(
                    ParticleEventFlow("impact", default_event_graph("impact")),
                ),
            ),
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
    assert schema[0]["stable_id"] == "impact"
    assert schema[0]["name"] == "Impact"
    assert schema[0]["queue_capacity"] == 8
    assert [field["stable_id"] for field in schema[0]["fields"]] == [
        "strength",
        "direction",
    ]
    assert not hasattr(component, "send_event")


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
                category="Smoke",
                tooltip="Controls the smoke density.",
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
            "hdr": False,
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


def _instantiate_particle_graph(tmp_path, name="InstantiateParameters"):
    source = tmp_path / f"{name}.particlegraph"
    ParticleGraphAsset(
        stable_id=name.lower(),
        parameters=(
            ParticleParameter(
                "smoke-density",
                "Density",
                TypeRef(ValueType.F32),
                1.0,
                True,
            ),
            ParticleParameter(
                "impact-scale",
                "ImpactScale",
                TypeRef(ValueType.F32),
                1.0,
                True,
            ),
        ),
        emitters=(
            ParticleEmitterAsset(
                stable_id="impact",
                name="Impact",
                settings=EmitterSettings(capacity=8),
            ),
        ),
    ).save(str(source))
    return source


def test_particle_system_serialize_flushes_live_instance_overrides(
    scene, monkeypatch, tmp_path
):
    source = _instantiate_particle_graph(tmp_path, "SerializeFlush")
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    scene.create_game_object("SerializeFlush").add_py_component(component)
    component.awake()
    component.start()
    component.set_float("Density", 0.75)
    component.set_emitter_options("Impact", play_on_start=False)
    component._parameter_overrides["impact-scale"] = 0.2
    component._emitter_overrides["impact"] = {
        "enabled": True,
        "play_on_start": False,
    }

    document = component._serialize_fields_document()

    assert json.loads(document["_parameter_overrides_json"]) == {
        "impact-scale": 0.2,
        "smoke-density": 0.75,
    }
    assert json.loads(document["_emitter_overrides_json"]) == {
        "impact": {"enabled": True, "play_on_start": False}
    }


def test_particle_system_keeps_instance_overrides_when_runtime_schema_is_empty(
    scene, monkeypatch, tmp_path
):
    source = _instantiate_particle_graph(tmp_path, "EmptySchema")
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    scene.create_game_object("EmptySchema").add_py_component(component)
    component.awake()
    component._parameter_overrides = {"impact-scale": 0.2}
    component._emitter_overrides = {
        "impact": {"enabled": True, "play_on_start": False}
    }
    component._store_parameter_overrides()
    component._store_emitter_overrides()

    component._reconcile_parameter_overrides(())
    component._reconcile_emitter_overrides(())

    assert component._parameter_overrides == {"impact-scale": 0.2}
    assert component._emitter_overrides == {
        "impact": {"enabled": True, "play_on_start": False}
    }
    assert json.loads(component._parameter_overrides_json) == {"impact-scale": 0.2}


def test_game_object_instantiate_copies_particle_instance_state(
    scene, monkeypatch, tmp_path
):
    source = _instantiate_particle_graph(tmp_path)
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    game_object = scene.create_game_object("ImpactSparkTemplate")
    game_object.add_py_component(component)
    component.awake()
    component.start()
    component.play_on_awake = False
    component.simulation_speed = 2.5
    component.set_float("Density", 0.75)
    component.set_float("ImpactScale", 0.2)
    component.set_emitter_options("Impact", play_on_start=False)
    component.stop()
    game_object.active = False

    parent = scene.create_game_object("ImpactSparkPool")
    clone = GameObject.instantiate(
        game_object,
        parent=parent,
        instantiate_in_world_space=False,
    )

    assert clone is not None
    assert clone.get_parent() is parent
    assert clone.active is False
    restored = clone.get_py_component(ParticleSystem)
    assert restored is not None
    assert restored is not component
    assert restored.play_on_awake is False
    assert restored.simulation_speed == pytest.approx(2.5)
    assert restored.get_float("Density") == pytest.approx(0.75)
    assert restored.get_float("ImpactScale") == pytest.approx(0.2)
    assert json.loads(restored._parameter_overrides_json) == {
        "impact-scale": 0.2,
        "smoke-density": 0.75,
    }
    assert json.loads(restored._emitter_overrides_json) == {
        "impact": {"enabled": True, "play_on_start": False}
    }
    assert restored.emitter_instance_schema()[0]["play_on_start"] is False
    assert getattr(restored, "_playing", False) is False


def test_game_object_instantiate_copies_live_overrides_ahead_of_json(
    scene, monkeypatch, tmp_path
):
    source = _instantiate_particle_graph(tmp_path, "LiveAhead")
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    game_object = scene.create_game_object("LiveAheadTemplate")
    game_object.add_py_component(component)
    component.awake()
    component.start()
    component.set_float("ImpactScale", 1.0)
    component._parameter_overrides["impact-scale"] = 0.2

    clone = GameObject.instantiate(game_object)
    restored = clone.get_py_component(ParticleSystem)

    assert restored.get_float("ImpactScale") == pytest.approx(0.2)
    assert json.loads(restored._parameter_overrides_json) == {"impact-scale": 0.2}


def test_particle_system_deserialize_repairs_missing_runtime_override_cache(
    scene, monkeypatch, tmp_path
):
    source = tmp_path / "UndoRepair.particlegraph"
    ParticleGraphAsset(
        stable_id="undo-repair",
        parameters=(
            ParticleParameter(
                "density", "Density", TypeRef(ValueType.F32), 0.25, True
            ),
        ),
        emitters=(ParticleEmitterAsset(stable_id="emitter"),),
    ).save(str(source))
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    game_object = scene.create_game_object("UndoRepair")
    game_object.add_py_component(component)
    component.awake()
    component.start()
    component.set_float("Density", 0.75)
    old_document = component._serialize_fields_document()
    component.set_float("Density", 0.5)
    new_document = component._serialize_fields_document()
    assert json.loads(old_document["_parameter_overrides_json"])["density"] == 0.75
    assert json.loads(new_document["_parameter_overrides_json"])["density"] == 0.5

    del component._parameter_overrides
    assert hasattr(component, "_gpu_controllers")

    command = PythonComponentDocumentCommand(
        component, old_document, new_document, "Edit ParticleSystem"
    )
    assert command._live() is component
    command.undo()

    assert component.get_float("Density") == pytest.approx(0.75)
    assert component._parameter_overrides == {"density": 0.75}


def test_particle_system_inspector_document_edits_undo_fields_parameters_and_emitters(
    scene, monkeypatch, tmp_path
):
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.ui._inspector_undo import (
        _record_python_component_document_edit,
    )
    from Infernux.engine.undo import UndoManager

    source = tmp_path / "InspectorUndo.particlegraph"
    ParticleGraphAsset(
        stable_id="inspector-undo",
        parameters=(
            ParticleParameter(
                "density", "Density", TypeRef(ValueType.F32), 0.25, True
            ),
        ),
        emitters=(ParticleEmitterAsset(stable_id="smoke", name="Smoke"),),
    ).save(str(source))
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    owner = scene.create_game_object("InspectorUndo")
    owner.add_py_component(component)
    component.awake()
    component.start()

    previous_manager = UndoManager.instance()
    manager = UndoManager()
    core = EditorInteractionCore()
    try:
        _record_python_component_document_edit(
            component,
            lambda: setattr(component, "simulation_speed", 2.0),
            "Set simulation_speed",
            edit_key="simulation_speed",
            validate=True,
        )
        assert component.simulation_speed == pytest.approx(2.0)
        manager.undo()
        assert component.simulation_speed == pytest.approx(1.0)
        manager.redo()
        assert component.simulation_speed == pytest.approx(2.0)

        _record_python_component_document_edit(
            component,
            lambda: component.set_float("Density", 0.75),
            "Set Density",
            edit_key="parameter:density",
        )
        assert component.get_float("Density") == pytest.approx(0.75)
        manager.undo()
        assert component.get_float("Density") == pytest.approx(0.25)
        manager.redo()
        assert component.get_float("Density") == pytest.approx(0.75)

        _record_python_component_document_edit(
            component,
            lambda: component.set_emitter_options("smoke", enabled=False),
            "Set Smoke enabled",
            edit_key="emitter:smoke:enabled",
        )
        assert component.emitter_instance_schema()[0]["enabled"] is False
        manager.undo()
        assert component.emitter_instance_schema()[0]["enabled"] is True
        manager.redo()
        assert component.emitter_instance_schema()[0]["enabled"] is False
    finally:
        core.shutdown()
        UndoManager._instance = previous_manager


def test_particle_system_curve_and_gradient_parameters_hot_update_fixed_gpu_block(
    scene, monkeypatch, tmp_path
):
    default_curve = Curve()
    default_gradient = Gradient()
    source = tmp_path / "GpuRampParameters.particlegraph"
    ParticleGraphAsset(
        stable_id="gpu-ramp-parameter-component",
        parameters=(
            ParticleParameter(
                "size-over-life",
                "Size Over Life",
                TypeRef(ValueType.CURVE),
                default_curve.to_dict(),
            ),
            ParticleParameter(
                "color-over-life",
                "Color Over Life",
                TypeRef(ValueType.GRADIENT),
                default_gradient.to_dict(),
            ),
        ),
        emitters=(ParticleEmitterAsset(stable_id="gpu-smoke"),),
    ).save(str(source))
    native_runtime = _GpuParticleNative()
    monkeypatch.setattr(
        ParticleSystem,
        "_native_engine",
        staticmethod(lambda: native_runtime),
    )
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    scene.create_game_object("GpuRampParameterProbe").add_py_component(component)
    component.awake()
    component.start()

    curve = Curve((CurveKey(0.0, 1.0), CurveKey(1.0, 3.0)))
    gradient = Gradient(
        (
            GradientKey(0.0, (1.0, 0.0, 0.0, 1.0)),
            GradientKey(1.0, (0.0, 0.0, 1.0, 0.0)),
        ),
        "fixed",
    )
    component.set_curve("Size Over Life", curve)
    component.set_gradient("color-over-life", gradient.to_dict())

    assert component.get_curve("size-over-life") == curve
    assert component.get_gradient("Color Over Life") == gradient
    assert len(native_runtime.parameter_updates) == 2
    assert len(native_runtime.parameter_updates[-1][1]) == (17 + 33) * 4
    assert component._artifact_revision > 0


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
    assert "active" not in component.emitter_instance_schema()[0]
    assert component.set_emitter_options("Smoke", play_on_start=False)
    assert runtime.actions == []
    assert json.loads(component._emitter_overrides_json) == {
        "smoke": {"enabled": True, "play_on_start": False}
    }
    assert "active" not in json.loads(component._emitter_overrides_json)["smoke"]
    assert component.set_emitter_options("smoke", enabled=False)
    assert runtime.actions == []
    assert component._emitter_is_enabled(0) is False
    assert component.play("smoke")
    assert runtime.actions == ["play"]


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
            "hdr": False,
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


def test_particle_system_exposes_color_parameter_hdr(scene):
    metadata = SimpleNamespace(
        parameters=(
            SimpleNamespace(
                stable_id="paper-color",
                name="Paper",
                value_type=TypeRef(ValueType.COLOR),
                default=[1.0, 1.0, 1.0, 1.0],
                exposed=True,
                category="",
                tooltip="",
                hdr=True,
            ),
        ),
        emitters=(),
    )
    component = ParticleSystem()
    scene.create_game_object("ColorParameter").add_py_component(component)
    component.awake()
    component._particle_metadata = metadata

    assert component.exposed_parameter_schema() == [
        {
            "stable_id": "paper-color",
            "name": "Paper",
            "type": "color",
            "default": [1.0, 1.0, 1.0, 1.0],
            "value": [1.0, 1.0, 1.0, 1.0],
            "category": "",
            "tooltip": "",
            "hdr": True,
        }
    ]


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
        "_load_saved_artifact",
        lambda *, force=False: rebuilds.append(force) or False,
    )

    with pytest.raises(RuntimeError, match="could not rebuild"):
        component.set_texture("Smoke Texture", override)

    assert rebuilds == [True]
    assert component.get_texture("Smoke Texture") == default
    assert component._parameter_overrides_json == "{}"


def test_particle_system_exposes_mesh_only_as_a_generic_typed_parameter(scene):
    default = AssetReference("default-mesh", "Assets/Models/Default.fbx")
    override = AssetReference("override-mesh", "Assets/Models/Override.fbx")
    metadata = SimpleNamespace(
        parameters=(
            SimpleNamespace(
                stable_id="surface-mesh",
                name="Surface Mesh",
                value_type=TypeRef(ValueType.MESH),
                default=default.to_dict(),
                exposed=True,
                category="Sampling",
                tooltip="Mesh sampled by the graph.",
            ),
        ),
        emitters=(),
    )
    component = ParticleSystem()
    scene.create_game_object("MeshParameter").add_py_component(component)
    component.awake()
    component._particle_metadata = metadata

    assert not hasattr(component, "set_mesh")
    assert not hasattr(component, "get_mesh")
    component.set_parameter("Surface Mesh", override)
    assert component.get_parameter("surface-mesh") == override.to_dict()
    assert json.loads(component._parameter_overrides_json) == {
        "surface-mesh": override.to_dict()
    }
    assert component.reset_parameter("Surface Mesh")
    assert component.get_parameter("surface-mesh") == default.to_dict()


def test_mesh_parameter_accepts_skinned_renderer_reference_without_becoming_state():
    source = ComponentRef(go_id=42, component_type="SkinnedMeshRenderer")

    assert _normalize_mesh_source_value(source, "Animated Surface") == {
        "$type": "component_ref",
        "game_object_id": 42,
        "component_type": "SkinnedMeshRenderer",
    }
    with pytest.raises(TypeError, match="Mesh asset or SkinnedMeshRenderer"):
        _normalize_mesh_source_value(
            ComponentRef(go_id=42, component_type="MeshRenderer"),
            "Animated Surface",
        )


def test_live_mesh_parameter_rebuilds_binding_and_rolls_back_on_failure(
    scene, monkeypatch
):
    default = AssetReference("default-mesh", "Assets/Models/Default.fbx")
    override = AssetReference("override-mesh", "Assets/Models/Override.fbx")
    parameter = SimpleNamespace(
        stable_id="surface-mesh",
        name="Surface Mesh",
        value_type=TypeRef(ValueType.MESH),
        default=default.to_dict(),
        exposed=True,
        category="",
        tooltip="",
    )
    component = ParticleSystem()
    scene.create_game_object("LiveMeshParameter").add_py_component(component)
    component.awake()
    component._particle_metadata = SimpleNamespace(parameters=(parameter,), emitters=())
    monkeypatch.setattr(component, "_has_runtime", lambda: True)
    rebuilds = []
    monkeypatch.setattr(
        component,
        "_load_saved_artifact",
        lambda *, force=False: rebuilds.append(force) or False,
    )

    with pytest.raises(RuntimeError, match="could not rebuild"):
        component.set_parameter("Surface Mesh", override)

    assert rebuilds == [True]
    assert component.get_parameter("Surface Mesh") == default.to_dict()
    assert component._parameter_overrides_json == "{}"


def test_live_mesh_parameter_publishes_each_distinct_override_once(
    scene, monkeypatch
):
    first = AssetReference("mesh-a", "Assets/Models/A.obj")
    second = AssetReference("mesh-b", "Assets/Models/B.obj")
    parameter = SimpleNamespace(
        stable_id="surface-mesh",
        name="Surface Mesh",
        value_type=TypeRef(ValueType.MESH),
        default=first.to_dict(),
        exposed=True,
        category="",
        tooltip="",
    )
    component = ParticleSystem()
    scene.create_game_object("LiveMeshPublication").add_py_component(component)
    component.awake()
    component._particle_metadata = SimpleNamespace(parameters=(parameter,), emitters=())
    monkeypatch.setattr(component, "_has_runtime", lambda: True)
    publications = []
    monkeypatch.setattr(
        component,
        "_load_saved_artifact",
        lambda *, force=False: publications.append(force) or True,
    )

    for index in range(40):
        component.set_parameter("Surface Mesh", second if index % 2 == 0 else first)

    assert publications == [True] * 40
    assert component.get_parameter("Surface Mesh") == first.to_dict()
    assert json.loads(component._parameter_overrides_json) == {
        "surface-mesh": first.to_dict()
    }

    component.set_parameter("Surface Mesh", first)
    assert publications == [True] * 40


def test_mesh_output_resolves_its_typed_parameter_override(monkeypatch):
    default = AssetReference("default-mesh", "Assets/Models/Default.fbx")
    override = AssetReference("override-mesh", "Assets/Models/Override.fbx")
    parameter = SimpleNamespace(
        stable_id="output-mesh",
        name="Output Mesh",
        value_type=TypeRef(ValueType.MESH),
        default=default.to_dict(),
    )
    component = ParticleSystem()
    component._particle_metadata = SimpleNamespace(parameters=(parameter,))
    component._parameter_overrides = {"output-mesh": override.to_dict()}
    resolved = []
    monkeypatch.setattr(
        component,
        "_resolve_mesh_reference",
        lambda reference, purpose: resolved.append((reference, purpose)) or "native-mesh",
    )

    native = component._gpu_mesh_binding(
        SimpleNamespace(
            output_type="mesh",
            mesh=AssetReference(),
            mesh_parameter="output-mesh",
        ),
        (parameter,),
    )

    assert native == "native-mesh"
    assert resolved == [(override, "ParticleGraph Mesh Output")]


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
    native.accept_batches = False
    component.update(0.5)
    assert component._graph_simulation_time_ticks == 125_000_000
    assert {
        runtime.simulation_time_ticks for runtime in component._gpu_controllers
    } == {125_000_000}
    native.accept_batches = True
    component.update(0.5)
    assert component._graph_simulation_time_ticks == 625_000_000
    assert {
        runtime.simulation_time_ticks for runtime in component._gpu_controllers
    } == {625_000_000}
    component.pause()
    component.update(0.5)
    assert all(
        item["simulation_time_ticks"] == 625_000_000
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
    assert diagnostics["graph_simulation_time_ticks"] == 625_000_000
    assert {
        emitter["simulation_time_ticks"]
        for emitter in diagnostics["emitters"]
    } == {625_000_000}
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
    assert native.diagnostic_sample_frames == 60
    assert native.diagnostic_state_sample_count == 0
    sampled_request = component.request_gpu_diagnostics(12, 4)
    assert sampled_request == 91
    assert native.diagnostic_sample_frames == 12
    assert native.diagnostic_state_sample_count == 4
    with pytest.raises(ValueError, match="sample_frames"):
        component.request_gpu_diagnostics(0)
    with pytest.raises(ValueError, match="sample_frames"):
        component.request_gpu_diagnostics(4097)
    with pytest.raises(ValueError, match="state_sample_count"):
        component.request_gpu_diagnostics(1, -1)
    with pytest.raises(ValueError, match="state_sample_count"):
        component.request_gpu_diagnostics(1, 65)
    layout = component._particle_gpu_layouts[1]
    state_bytes = bytearray(layout["state_stride"])
    struct.pack_into("<II", state_bytes, 0, 3, 7)
    position_field = next(
        field
        for field in layout["attribute_fields"]
        if field["stable_id"] == "builtin.position"
    )
    struct.pack_into("<3f", state_bytes, position_field["offset"], 1.25, 2.5, -3.0)
    native.diagnostic_state_samples = [
        {
            "slot_index": 6,
            "lifecycle_flags": 3,
            "spawn_generation": 7,
            "raw_words": list(
                struct.unpack(f"<{len(state_bytes) // 4}I", state_bytes)
            ),
        }
    ]
    gpu_diagnostics = component.poll_gpu_diagnostics(diagnostic_request)
    assert diagnostic_request == 91
    assert gpu_diagnostics["status"] == "completed"
    assert gpu_diagnostics["emitters"][0]["stable_id"] == "gpu-sparks"
    assert gpu_diagnostics["emitters"][0]["alive_count"] == 3
    assert gpu_diagnostics["emitters"][0]["collision_hit_count"] == 17
    assert gpu_diagnostics["emitters"][0]["collision_response_count"] == 13
    assert gpu_diagnostics["emitters"][0]["collision_trigger_count"] == 4
    assert gpu_diagnostics["emitters"][0]["collision_enter_count"] == 5
    assert gpu_diagnostics["emitters"][0]["collision_stay_count"] == 11
    assert gpu_diagnostics["emitters"][0]["collision_exit_count"] == 3
    assert gpu_diagnostics["emitters"][0]["collision_max_outward_speed"] == 4.5
    assert gpu_diagnostics["emitters"][0]["collision_max_tangent_speed"] == 2.25
    assert gpu_diagnostics["emitters"][0]["bounds_mode"] == "manual"
    assert gpu_diagnostics["emitters"][0]["bounds_valid"] is True
    assert gpu_diagnostics["emitters"][0]["bounds_lower"] == [-10.0, -6.0, -10.0]
    assert gpu_diagnostics["emitters"][0]["event_diagnostics"] == []
    state_sample = gpu_diagnostics["emitters"][0]["state_samples"][0]
    assert state_sample["slot_index"] == 6
    assert state_sample["spawn_generation"] == 7
    assert state_sample["attributes"]["builtin.position"] == pytest.approx(
        (1.25, 2.5, -3.0)
    )
    assert "raw_words" not in state_sample
    view_request = component.request_gpu_view_diagnostics("GAME", 44)
    view_diagnostics = component.poll_gpu_view_diagnostics("game", view_request, 44)
    assert view_request == 92
    assert native.view_diagnostic_view == "game"
    assert native.view_diagnostic_camera_component_id == 44
    assert view_diagnostics["camera_component_id"] == 44
    assert view_diagnostics["render_view_id"] == 701
    assert view_diagnostics["outputs"][0]["emitter_stable_id"] == "gpu-sparks"
    assert view_diagnostics["outputs"][0]["visible_count"] == 3
    assert view_diagnostics["outputs"][0]["sort_mode"] == "none"
    assert view_diagnostics["outputs"][0]["sort_group_count_x"] == 1
    assert view_diagnostics["outputs"][0]["sorter_allocated"] is False
    assert view_diagnostics["outputs"][0]["cull_mode"] == "ribbon_segments"
    with pytest.raises(ValueError, match="scene.*game"):
        component.request_gpu_view_diagnostics("preview")
    with pytest.raises(ValueError, match="camera_component_id"):
        component.request_gpu_view_diagnostics("scene", 44)

    first_step = component._gpu_controllers[0].simulation_step
    second_step = component._gpu_controllers[1].simulation_step
    assert component.pause_emitter("Sparks") is True
    assert component.pause_emitter("Missing") is False
    assert component.pause_emitter(99) is False
    component.update(0.25)
    assert component._gpu_controllers[0].simulation_step == first_step + 1
    # CPU scheduling follows the graph clock even while the graph-owned
    # playing state gates this emitter on the GPU.
    assert component._gpu_controllers[1].simulation_step == second_step + 1
    assert native.playing_updates[-1] == (
        component._gpu_emitter_ids[1],
        False,
    )

    assert component.terminate_emitter("Smoke") is True
    assert component.terminate_emitter("gpu-sparks") is True
    assert native.reset_emitters == component._gpu_emitter_ids
    assert component.start_emitter("Sparks") is True
    component.update(0.0)
    assert component._gpu_controllers[0].simulation_step == 1
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



def test_particle_system_keeps_event_queues_inside_each_gpu_emitter(
    scene, monkeypatch, tmp_path
):
    source = tmp_path / "GpuEvents.particlegraph"
    graph = ParticleGraphAsset(
        stable_id="gpu-event-component",
        emitters=(
            ParticleEmitterAsset(
                stable_id="source",
                settings=EmitterSettings(capacity=32),
                event_flows=(
                    ParticleEventFlow("impact", default_event_graph("impact")),
                ),
            ),
        ),
        event_types=(ParticleEventType("impact", "Impact", 32),),
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

    assert len(native.program_batches[-1][0]) == 1
    assert component._particle_gpu_layouts[0]["event_type_count"] == 1
    attribute_ids = {
        item[0] for item in component._particle_kernel.emitters[0].attributes
    }
    assert {
        "internal.event.0.head",
        "internal.event.0.tail",
        "internal.event.0.count",
        "internal.event.0.active",
    }.issubset(attribute_ids)
    native._poll_gpu_particle_diagnostics = lambda request_id: {
        "request_id": request_id,
        "graph_instance_id": component._batch_id,
        "status": "completed",
        "error": "",
        "emitters": [
            {
                "emitter_id": component._gpu_emitter_ids[0],
                "emitter_index": 0,
                "capacity": 32,
                "free_count": 31,
                "alive_count": 1,
                "visible_count": 1,
                "dropped_count": 0,
                "collision_hit_count": 0,
                "collision_response_count": 0,
                "collision_trigger_count": 0,
                "collision_enter_count": 0,
                "collision_stay_count": 0,
                "collision_exit_count": 0,
                "event_overflow_counts": [7],
                "event_enqueue_counts": [11],
                "event_complete_counts": [9],
                "bounds_mode": "automatic",
                "bounds_valid": False,
                "bounds_lower": [0.0, 0.0, 0.0],
                "bounds_upper": [0.0, 0.0, 0.0],
            }
        ],
    }
    diagnostics = component.poll_gpu_diagnostics(component.request_gpu_diagnostics())
    assert diagnostics["emitters"][0]["event_diagnostics"] == [
        {
            "event_type_index": 0,
            "stable_id": "impact",
            "name": "Impact",
            "queue_capacity": 32,
            "enqueue_count": 11,
            "complete_count": 9,
            "overflow_count": 7,
        }
    ]
    native._poll_gpu_particle_diagnostics = lambda request_id: {
        "request_id": request_id,
        "graph_instance_id": component._batch_id,
        "status": "completed",
        "error": "",
        "emitters": [
            {
                "emitter_id": component._gpu_emitter_ids[0],
                "emitter_index": 0,
                "event_overflow_counts": [0],
                "event_enqueue_counts": [],
                "event_complete_counts": [0],
            }
        ],
    }
    with pytest.raises(
        RuntimeError,
        match=(
            "emitter index 0: expected 1 event slots, "
            "got overflow=1, enqueue=0, complete=1"
        ),
    ):
        component.poll_gpu_diagnostics(component.request_gpu_diagnostics())
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
                    capacity=32,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 4),),
                ),
                rendering=_two_output_rendering_graph(),
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
        == 3000
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
                rendering=_two_output_rendering_graph(),
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
                rendering=_two_output_rendering_graph(),
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

    event_graph = ParticleGraphAsset(
        stable_id="gpu-smoke-component",
        emitters=(
            ParticleEmitterAsset(
                stable_id="smoke",
                settings=EmitterSettings(
                    capacity=64,
                    spawn_rate=4.0,
                    bursts=(ParticleBurst(0.0, 4),),
                ),
                rendering=_two_output_rendering_graph(),
            ),
        ),
        event_types=(ParticleEventType("impact", "Impact", 8),),
    )
    event_revision = component._artifact_revision
    event_graph.save(str(source))
    component.update(0.0)
    assert component._artifact_revision > event_revision
    assert component._gpu_controllers[0].simulation_step == 0
    assert (
        component.emitter_reload_compatibility(0)
        is ParticleRuntimeCompatibility.EMITTER_RESTART
    )
    assert component._particle_gpu_layouts[0]["event_type_count"] == 1
    assert engine._gpu_particle_state_was_preserved(emitter_id) is False

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
                rendering=_two_output_rendering_graph(),
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
        == 3000
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
            GraphNodeRecord("acceleration", "particle.attribute.velocity"),
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
                PortKind.EXEC,
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
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        component.update(0.0)
        if engine._gpu_particle_vector_field_generation(emitter_id, 0) > initial_generation:
            break
        time.sleep(0.001)

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
                "particle.collision.sdf",
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
                PortKind.EXEC,
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


def test_particle_system_prewarm_uses_one_transactional_fixed_step_gpu_sequence(
    scene, monkeypatch, tmp_path
):
    source = tmp_path / "GpuPrewarm.particlegraph"
    ParticleGraphAsset(
        stable_id="gpu-prewarm-component",
        emitters=(
            ParticleEmitterAsset(
                stable_id="looping-smoke",
                name="Looping Smoke",
                settings=EmitterSettings(
                    capacity=32,
                    spawn_rate=60.0,
                    duration=0.05,
                    loop=True,
                ),
            ),
            ParticleEmitterAsset(
                stable_id="one-shot-sparks",
                name="One Shot Sparks",
                settings=EmitterSettings(
                    capacity=32,
                    spawn_rate=60.0,
                    duration=0.05,
                    loop=False,
                ),
            ),
        ),
    ).save(str(source))
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    component.prewarm = True
    game_object = scene.create_game_object("GpuPrewarmProbe")
    game_object.add_py_component(component)

    component.awake()
    component.start()
    component.update(0.0)

    frame_items = native.frames[-1][1]
    looping_steps = frame_items[0]["preroll_steps"]
    assert len(looping_steps) == 3
    assert sum(step["delta_time"] for step in looping_steps) == pytest.approx(0.05)
    assert [step["simulation_step"] for step in looping_steps] == [0, 1, 2]
    assert frame_items[0]["simulation_step"] == 3
    assert component._gpu_controllers[0].simulation_step == 4
    assert len(frame_items[1]["preroll_steps"]) == 3
    assert component._gpu_controllers[1].simulation_step == 4
    assert {
        item["simulation_time_ticks"] for item in frame_items
    } == {50_000_000}
    assert {
        controller.simulation_time_ticks
        for controller in component._gpu_controllers
    } == {50_000_000}

    for delta_time in (0.0, 1.0 / 144.0, 1.0 / 59.0, 0.125):
        component.update(delta_time)
        assert len(
            {item["simulation_time_ticks"] for item in native.frames[-1][1]}
        ) == 1
        assert len(
            {
                controller.simulation_time_ticks
                for controller in component._gpu_controllers
            }
        ) == 1
    assert all(item["preroll_steps"] == [] for item in native.frames[-1][1])

    # Runtime time remains integer-authoritative even after nanosecond ticks
    # exceed the exact-integer range of an IEEE-754 double.
    long_running_ticks = (1 << 53) + 17
    component._graph_simulation_time_ticks = long_running_ticks
    component.update(1.0 / 144.0)
    expected_ticks = long_running_ticks + int(round((1.0 / 144.0) * 1_000_000_000.0))
    assert {
        item["simulation_time_ticks"] for item in native.frames[-1][1]
    } == {expected_ticks}
    assert {
        controller.simulation_time_ticks
        for controller in component._gpu_controllers
    } == {expected_ticks}

    assert component.restart("Looping Smoke") is True
    component.update(0.0)
    assert all(item["preroll_steps"] == [] for item in native.frames[-1][1])

    assert component.restart() is True
    component.update(0.0)
    assert all(len(item["preroll_steps"]) == 3 for item in native.frames[-1][1])


def test_particle_system_play_consumes_saved_aot_without_source_compilation(
    scene, monkeypatch, tmp_path
):
    source = tmp_path / "SavedAotOnly.particlegraph"
    ParticleGraphAsset(stable_id="saved-aot-only").save(str(source))
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))

    def _unexpected_compile(*_args, **_kwargs):
        raise AssertionError("Play must not compile ParticleGraph source")

    monkeypatch.setattr(ParticleGraphCompiler, "compile", _unexpected_compile)
    monkeypatch.setattr(ParticleKernelLowerer, "lower", _unexpected_compile)
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    scene.create_game_object("SavedAotOnlyProbe").add_py_component(component)

    component.awake()
    assert component.play() is True
    component.update(0.0)

    assert component._gpu_runtime_resident()
    assert native.program_batches


def test_single_emitter_restart_resets_authoritative_graph_clock(
    scene, monkeypatch, tmp_path
):
    source = tmp_path / "SingleBurstRestart.particlegraph"
    ParticleGraphAsset(stable_id="single-burst-restart").save(str(source))
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    scene.create_game_object("SingleBurstRestartProbe").add_py_component(component)

    component.awake()
    component.start()
    component.update(2.0)
    assert component._graph_simulation_time_ticks == 2_000_000_000

    assert component.restart(0) is True
    component.update(1.0 / 60.0)

    assert component._graph_simulation_time_ticks == int(round(1_000_000_000.0 / 60.0))
    assert native.frames[-1][1][0]["simulation_time_ticks"] == int(
        round(1_000_000_000.0 / 60.0)
    )


def test_particle_preview_reuses_saved_aot_native_runtime_and_graph_clock(
    scene, monkeypatch, tmp_path
):
    source = tmp_path / "SavedPreviewAotOnly.particlegraph"
    ParticleGraphAsset(
        stable_id="saved-preview-aot-only",
        emitters=(
            ParticleEmitterAsset(stable_id="first", name="First"),
            ParticleEmitterAsset(stable_id="second", name="Second"),
        ),
    ).save(str(source))
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))

    def _unexpected_compile(*_args, **_kwargs):
        raise AssertionError("Preview must not compile ParticleGraph source")

    monkeypatch.setattr(ParticleGraphCompiler, "compile", _unexpected_compile)
    monkeypatch.setattr(ParticleKernelLowerer, "lower", _unexpected_compile)
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    scene.create_game_object("SavedPreviewAotOnlyProbe").add_py_component(component)

    component.awake()
    assert component.editor_preview_begin() is True
    component.editor_preview_update(1.0 / 60.0)

    assert component._gpu_runtime_resident()
    assert len(native.program_batches[-1][0]) == 2
    assert len({item["graph_instance_id"] for item in native.program_batches[-1][0]}) == 1
    assert {
        controller.simulation_time_ticks
        for controller in component._gpu_controllers
    } == {component._graph_simulation_time_ticks}

    # The overlay remains a view of the graph clock even if stale diagnostic
    # controller data is observed while a native publication is in flight.
    component._graph_simulation_time_ticks = (1 << 53) + 19
    component._gpu_controllers[0]._simulation_time_ticks = 1
    component._gpu_controllers[1]._simulation_time_ticks = 2
    assert component.editor_preview_time_seconds() == pytest.approx(
        ((1 << 53) + 19) / 1_000_000_000.0
    )


def test_particle_system_seek_replays_from_zero_and_preserves_pause_state(
    scene, monkeypatch, tmp_path
):
    source = tmp_path / "GpuSeek.particlegraph"
    ParticleGraphAsset(
        stable_id="gpu-seek-component",
        emitters=(
            ParticleEmitterAsset(
                stable_id="seek-smoke",
                name="Seek Smoke",
                settings=EmitterSettings(
                    capacity=32,
                    spawn_rate=60.0,
                    duration=1.0,
                    loop=True,
                    seed=17,
                ),
            ),
        ),
    ).save(str(source))
    native = _GpuParticleNative()
    monkeypatch.setattr(ParticleSystem, "_native_engine", staticmethod(lambda: native))
    component = ParticleSystem()
    component.graph = ParticleGraphRef(path_hint=str(source))
    component.random_seed = 123
    game_object = scene.create_game_object("GpuSeekProbe")
    game_object.add_py_component(component)

    component.awake()
    component.start()
    component.update(0.0)
    assert component.pause("Seek Smoke") is True
    revision = scene.temporal_discontinuity_revision
    assert component.seek(0.05, "Seek Smoke") is True
    assert scene.temporal_discontinuity_revision == revision + 1
    assert native.reset_emitters[-1] == component._gpu_emitter_ids[0]

    component.update(1.0)
    item = native.frames[-1][1][0]
    assert len(item["preroll_steps"]) == 3
    assert [step["simulation_step"] for step in item["preroll_steps"]] == [0, 1, 2]
    assert sum(step["delta_time"] for step in item["preroll_steps"]) == pytest.approx(0.05)
    assert {step["system_seed"] for step in item["preroll_steps"]} == {123}
    assert item["simulate"] is False
    assert item["render"] is True
    assert item["force_simulation"] is True
    assert item["simulation_step"] == 3
    assert component._gpu_controllers[0].is_playing is False
    assert component._gpu_controllers[0].simulation_step == 3
    assert component.runtime_diagnostics()["random_seed"] == 123
    assert component.runtime_diagnostics()["emitters"][0]["simulation_time_seconds"] == pytest.approx(0.05)

    assert component.play("Seek Smoke") is True
    revision = scene.temporal_discontinuity_revision
    assert component.seek(0.05, "Seek Smoke") is True
    assert scene.temporal_discontinuity_revision == revision + 1
    component.update(0.0)
    replay = native.frames[-1][1][0]
    assert replay["preroll_steps"] == item["preroll_steps"]
    assert replay["simulation_step"] == 3
    assert replay["simulate"] is True
    assert replay["force_simulation"] is False
    assert component._gpu_controllers[0].simulation_step == 4

    assert component.reset_simulation("Seek Smoke") is True
    component.pause("Seek Smoke")
    component.update(0.0)
    reset_item = native.frames[-1][1][0]
    assert reset_item["preroll_steps"] == []
    assert reset_item["simulation_step"] == 0
