from __future__ import annotations

import json

import pytest

from Infernux.engine.project_context import clear_panel_tracking
from Infernux.engine.ui.graph_document_authoring import (
    GraphDocumentAuthoringModel,
    ParticleEmitterGraphAuthoringModel,
    particle_stage_definition_filter,
)
from Infernux.particle.asset import ParticleGraphAsset


@pytest.fixture(autouse=True)
def _isolate_particle_graph_panel_dirty_tracking():
    clear_panel_tracking("particle_graph_editor")
    try:
        yield
    finally:
        clear_panel_tracking("particle_graph_editor")


def _stage_model(document):
    return GraphDocumentAuthoringModel(
        document,
        definition_filter=particle_stage_definition_filter(document.domain),
    )


def test_particle_document_authoring_round_trip_keeps_strict_roots():
    document = ParticleGraphAsset().emitters[0].init
    model = _stage_model(document)

    assert model.remove_node("root.init") is False
    assert "particle.init.set_velocity" in {
        definition.type_id for definition in model.registered_types()
    }
    assert "particle.update.acceleration" not in {
        definition.type_id for definition in model.registered_types()
    }

    velocity = model.add_node("particle.init.set_velocity", 240.0, 20.0)
    velocity.data["value"] = [1.0, 2.0, 3.0]
    assert model.add_link("root.init", "out", velocity.uid, "in") is not None

    restored = model.to_document()
    assert restored.domain == "particle.init"
    assert restored.nodes[1].position == (240.0, 20.0)
    assert restored.nodes[1].properties["value"] == [1.0, 2.0, 3.0]
    assert restored.links[0].kind.value == "stream"


def test_default_rendering_stage_opens_without_overlapping_output():
    rendering = ParticleGraphAsset().emitters[0].rendering
    positions = {node.uid: node.position for node in rendering.nodes}

    assert positions["root.rendering"] == (0.0, 0.0)
    assert positions["output.sprite"] == (280.0, 0.0)


def test_particle_emitter_authoring_combines_stages_but_keeps_chains_isolated():
    emitter = ParticleGraphAsset().emitters[0]
    model = ParticleEmitterGraphAuthoringModel(emitter)

    assert [node.type_id for node in model.nodes] == [
        "particle.root.init",
        "particle.root.update",
        "particle.root.rendering",
        "particle.output.sprite",
    ]
    assert model.remove_node("init::root.init") is False

    velocity = model.add_node("particle.init.set_velocity", 220.0, 0.0)
    acceleration = model.add_node("particle.update.acceleration", 220.0, 230.0)
    assert model.add_link("init::root.init", "out", velocity.uid, "in") is not None
    assert model.add_link("update::root.update", "out", acceleration.uid, "in") is not None
    assert not model.validate_link(velocity.uid, "out", acceleration.uid, "in")

    documents = model.to_documents()
    assert [node.type_id for node in documents["init"].nodes] == [
        "particle.root.init",
        "particle.init.set_velocity",
    ]
    assert [node.type_id for node in documents["update"].nodes] == [
        "particle.root.update",
        "particle.update.acceleration",
    ]


def test_particle_graph_editor_restores_single_canvas_dirty_draft():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    panel._on_node_add("particle.init.set_velocity", 220.0, 0.0)
    velocity = next(
        node for node in panel._model.nodes
        if node.type_id == "particle.init.set_velocity"
    )
    panel._on_link_created("init::root.init", "out", velocity.uid, "in")
    panel._select_stage("rendering")

    restored = ParticleGraphEditorPanel()
    restored.load_state(panel.save_state())

    assert restored._dirty is True
    assert restored._stage == "rendering"
    assert [node.type_id for node in restored.asset.emitters[0].init.nodes] == [
        "particle.root.init",
        "particle.init.set_velocity",
    ]
    assert [node.type_id for node in restored._model.nodes] == [
        "particle.root.init",
        "particle.init.set_velocity",
        "particle.root.update",
        "particle.root.rendering",
        "particle.output.sprite",
    ]


def test_particle_graph_editor_save_aot_compiles_and_reopens(tmp_path, monkeypatch):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.particle.artifact import ParticleArtifactRegistry

    compiled = []
    monkeypatch.setattr(
        ParticleArtifactRegistry,
        "compile_path",
        classmethod(lambda cls, path, **_kwargs: compiled.append(path)),
    )
    monkeypatch.setattr(AssetManager, "reimport_asset", classmethod(lambda cls, _path: None))

    path = tmp_path / "Smoke.particlegraph"
    panel = ParticleGraphEditorPanel()
    assert panel._save_to(str(path)) is True
    assert compiled == [str(path.resolve())]
    assert panel._dirty is False

    reopened = ParticleGraphEditorPanel()
    assert reopened._open_particlegraph(str(path)) is True
    assert reopened.asset.name == "Smoke"
    assert reopened._dirty is False


def test_project_create_particlegraph_writes_loadable_asset(tmp_path, monkeypatch):
    from Infernux.engine.ui.project_file_ops import create_particlegraph
    from Infernux.particle.artifact import ParticleArtifactRegistry

    compiled = []
    monkeypatch.setattr(
        ParticleArtifactRegistry,
        "compile_path",
        classmethod(lambda cls, path, **_kwargs: compiled.append(path)),
    )

    ok, error = create_particlegraph(str(tmp_path), "Fire")

    assert ok is True, error
    path = tmp_path / "Fire.particlegraph"
    graph = ParticleGraphAsset.load(str(path))
    assert graph.name == "Fire"
    assert len(graph.emitters) == 1
    assert compiled == [str(path)]
    assert json.loads(path.read_text(encoding="utf-8"))["$schema"] == "infernux.particle_graph"


def test_particle_graph_live_draft_publishes_without_overwriting_source(tmp_path):
    from dataclasses import replace
    from Infernux.particle.artifact import ParticleArtifactRegistry

    path = tmp_path / "LiveSmoke.particlegraph"
    original = ParticleGraphAsset(stable_id="live-smoke")
    original.save(str(path))
    source_before = path.read_text(encoding="utf-8")
    first = ParticleArtifactRegistry.get(str(path))

    emitter = original.emitters[0]
    draft = replace(
        original,
        emitters=(
            replace(
                emitter,
                settings=replace(emitter.settings, spawn_rate=321.0),
            ),
        ),
    )
    published = ParticleArtifactRegistry.publish_graph_asset(draft, str(path))

    assert published.revision > first.revision
    assert ParticleArtifactRegistry.get(str(path)) is published
    assert path.read_text(encoding="utf-8") == source_before


def test_particle_system_inspector_metadata_is_localizable_and_backend_is_emitter_owned():
    from Infernux.components.particle_system import ParticleSystem
    from Infernux.components.serialized_field import get_serialized_fields

    fields = get_serialized_fields(ParticleSystem)
    assert set(fields) == {"graph", "simulation_speed", "play_on_awake"}
    assert fields["graph"].display_name_key == "particle_system.graph"
    assert fields["simulation_speed"].display_name_key == "particle_system.simulation_speed"
    assert fields["play_on_awake"].display_name_key == "particle_system.play_on_awake"
