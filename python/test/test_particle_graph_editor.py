from __future__ import annotations

import json
import os
import struct

import pytest

from Infernux.engine.project_context import clear_panel_tracking
from Infernux.engine.ui.graph_document_authoring import (
    GraphDocumentAuthoringModel,
    ParticleEmitterGraphAuthoringModel,
    particle_stage_definition_filter,
)
from Infernux.graph.types import TypeRef, ValueType
from Infernux.particle.asset import (
    ParticleEmitterAsset,
    ParticleEventField,
    ParticleEventRoute,
    ParticleEventType,
    ParticleGraphAsset,
)
from Infernux.particle.nodes import (
    particle_event_output_type_id,
    particle_event_payload_port_id,
    particle_event_payload_type_id,
    particle_graph_node_definitions,
)


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


def test_particle_data_expression_nodes_are_creatable_in_simulation_stages():
    emitter = ParticleGraphAsset().emitters[0]
    init_types = {
        definition.type_id for definition in _stage_model(emitter.init).registered_types()
    }
    update_types = {
        definition.type_id for definition in _stage_model(emitter.update).registered_types()
    }
    rendering_types = {
        definition.type_id
        for definition in _stage_model(emitter.rendering).registered_types()
    }

    for type_id in (
        "particle.attribute.read_vec3",
        "particle.point_cache.sample_position",
        "particle.vector_field.sample",
    ):
        assert type_id in init_types
        assert type_id in update_types
        assert type_id not in rendering_types

    model = ParticleEmitterGraphAuthoringModel(emitter)
    model.prepare_node_creation("update")
    position = model.add_node("particle.attribute.read_vec3", 200.0, 230.0)
    assert position.uid.startswith("update::")


def test_particle_event_output_is_available_in_every_emitter_stage():
    source = ParticleEmitterAsset(stable_id="source", name="Source")
    target = ParticleEmitterAsset(stable_id="target", name="Target")
    routes = tuple(
        ParticleEventRoute(
            f"route-{stage}", "event", "source", stage, "target"
        )
        for stage in ("init", "update", "rendering")
    )
    asset = ParticleGraphAsset(
        emitters=(source, target),
        event_types=(
            ParticleEventType(
                "event",
                "Event",
                32,
                (ParticleEventField("amount", "Amount", TypeRef(ValueType.F32), 1.0),),
            ),
        ),
        event_routes=routes,
    )
    model = ParticleEmitterGraphAuthoringModel(
        source, definition_set=particle_graph_node_definitions(asset)
    )
    registered = {definition.type_id for definition in model.registered_types()}

    for route in routes:
        type_id = particle_event_output_type_id(route.stable_id, route.source_stage)
        assert type_id in registered
        model.prepare_node_creation(route.source_stage)
        node = model.add_node(type_id, 200.0, 230.0)
        assert model.stage_for_uid(node.uid) == route.source_stage

    target_model = ParticleEmitterGraphAuthoringModel(
        target, definition_set=particle_graph_node_definitions(asset)
    )
    target_types = {definition.type_id for definition in target_model.registered_types()}
    assert not target_types.intersection(
        particle_event_output_type_id(route.stable_id, route.source_stage)
        for route in routes
    )
    for route in routes:
        payload_type_id = particle_event_payload_type_id(route.stable_id)
        assert payload_type_id in target_types
        target_model.prepare_node_creation("init")
        node = target_model.add_node(payload_type_id, 200.0, 230.0)
        assert target_model.stage_for_uid(node.uid) == "init"


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


def test_particle_common_node_creation_keeps_the_requested_stage():
    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    model.prepare_node_creation("update")

    noise = model.add_node("common.noise.vector3d", 200.0, 460.0)

    assert noise.uid.startswith("update::")
    assert model.authoring_stage == "update"


def test_particle_graph_palette_request_freezes_source_or_canvas_stage():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    panel._on_node_creation_requested(
        {"source_node": "update::root.update", "gy": 460.0}
    )
    linked_creation = panel._on_node_add("common.noise.vector3d", 200.0, 460.0)
    panel._on_node_creation_requested({"source_node": "", "gy": 230.0})
    canvas_creation = panel._on_node_add("common.compare.greater_than", 400.0, 230.0)

    assert linked_creation.uid.startswith("update::")
    assert canvas_creation.uid.startswith("update::")
    assert panel._stage == "update"


def test_shared_palette_notifies_host_with_the_complete_creation_request():
    from Infernux.engine.ui.node_graph_view import NodeGraphView, PinKind

    view = NodeGraphView()
    requests = []
    view.on_node_creation_requested = requests.append

    view._request_node_creation(
        12.0,
        34.0,
        "update::root.update",
        "out",
        PinKind.OUTPUT,
    )

    assert requests == [
        {
            "gx": 12.0,
            "gy": 34.0,
            "source_node": "update::root.update",
            "source_pin": "out",
            "source_kind": PinKind.OUTPUT,
        }
    ]


def test_curve_and_gradient_use_inspector_properties_and_round_trip_canvas_data():
    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    curve_type = model.get_type("common.curve.sample")
    gradient_type = model.get_type("common.gradient.sample")

    assert [field.id for field in curve_type.inline_fields] == ["t"]
    assert [field.id for field in gradient_type.inline_fields] == ["t"]

    curve = model.add_node("common.curve.sample", 240.0, 230.0)
    gradient = model.add_node("common.gradient.sample", 480.0, 230.0)
    curve.data["curve"]["keys"][1]["value"] = 2.0
    gradient.data["gradient"]["keys"][0]["color"] = [2.0, 0.5, 0.0, 1.0]

    documents = model.to_documents()
    update_nodes = {node.uid: node for node in documents["update"].nodes}
    assert update_nodes[curve.uid.split("::", 1)[1]].properties["curve"]["keys"][1]["value"] == 2.0
    assert update_nodes[gradient.uid.split("::", 1)[1]].properties["gradient"]["keys"][0]["color"] == [2.0, 0.5, 0.0, 1.0]


def test_particle_value_input_connection_can_be_replaced_atomically():
    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    first = model.add_node("common.curve.sample", 240.0, 230.0)
    second = model.add_node("common.curve.sample", 480.0, 230.0)
    add = model.add_node("common.math.add", 720.0, 230.0)
    original = model.add_link(first.uid, "value", add.uid, "a")

    replaced = model.replace_link(
        original.uid, second.uid, "value", add.uid, "a"
    )

    assert replaced is original
    assert replaced.source_node == second.uid
    assert len(
        [link for link in model.links if link.target_node == add.uid and link.target_pin == "a"]
    ) == 1


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


def test_particle_graph_editor_discards_incompatible_transient_draft(tmp_path):
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.particle import ParticleGraphAsset

    target = tmp_path / "Current.particlegraph"
    ParticleGraphAsset(stable_id="current-graph", name="Current").save(str(target))
    panel = ParticleGraphEditorPanel()
    state = panel.save_state()
    state["file_path"] = str(target)
    state["dirty"] = True
    stale_draft = dict(state["draft"])
    stale_draft.pop("event_types")
    stale_draft.pop("event_routes")
    state["draft"] = stale_draft

    restored = ParticleGraphEditorPanel()
    restored.load_state(state)

    assert restored._dirty is False
    assert os.path.normcase(restored._file_path) == os.path.normcase(str(target.resolve()))
    assert restored.asset.stable_id == "current-graph"


def test_particle_graph_editor_explicitly_discards_an_unsaved_memory_document():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    panel.add_authoring_emitter("Temporary")

    result = panel.discard_unsaved_changes()

    assert result == {
        "discarded": True,
        "previous_file_path": "",
        "file_path": "",
        "dirty": False,
    }
    assert len(panel.asset.emitters) == 1
    assert panel.asset.event_types == ()
    assert panel.asset.event_routes == ()


def test_particle_graph_editor_ignores_float32_widget_round_trip_noise():
    from Infernux.engine.ui.inspector_utils import preserve_ui_float_precision
    from Infernux.particle.asset import EmitterSettings

    original = EmitterSettings(gravity=(0.0, -9.81, 0.0), spawn_rate=3.7)
    float32 = lambda value: struct.unpack("f", struct.pack("f", value))[0]
    widget_value = EmitterSettings(
        gravity=tuple(float32(value) for value in original.gravity),
        spawn_rate=float32(original.spawn_rate),
    )

    assert preserve_ui_float_precision(widget_value, original) == original
    changed = EmitterSettings(gravity=(0.0, -8.5, 0.0), spawn_rate=4.0)
    assert preserve_ui_float_precision(changed, original) == changed


def test_particle_graph_save_replaces_persisted_dirty_draft(tmp_path, monkeypatch):
    from Infernux.engine.ui import panel_state
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    layout = tmp_path / "layout"
    panel_state.init(str(layout))
    target = tmp_path / "Sparks.particlegraph"
    panel = ParticleGraphEditorPanel()
    panel._file_path = str(target)
    panel._dirty = True
    panel._persist_panel_state()
    assert panel_state.get("panel:particle_graph_editor")["dirty"] is True

    def save_asset(asset, path):
        target.write_text(json.dumps(asset.to_dict()), encoding="utf-8")

    monkeypatch.setattr(ParticleGraphAsset, "save", save_asset)

    assert panel._save_to(str(target)) is True
    persisted = panel_state.get("panel:particle_graph_editor")
    assert persisted["dirty"] is False
    assert "draft" not in persisted


def test_particle_graph_scalar_properties_publish_stable_semantic_ids():
    from Infernux.engine.ui.particle_graph_editor_panel import (
        _record_scalar_node_property_semantics,
    )
    from Infernux.graph.types import ValueType

    class Context:
        semantic_capture_enabled = True

        def __init__(self):
            self.items = []

        def record_semantic_item(self, *args, **kwargs):
            self.items.append((args, kwargs))

    ctx = Context()
    _record_scalar_node_property_semantics(
        ctx,
        node_uid="rendering::output.sprite",
        key="soft_particles",
        label="Soft Particles",
        value_type=ValueType.BOOL,
        value=True,
    )
    _record_scalar_node_property_semantics(
        ctx,
        node_uid="rendering::output.sprite",
        key="soft_distance",
        label="Fade Distance",
        value_type=ValueType.F32,
        value=0.5,
    )

    assert ctx.items == [
        (
            (
                "checkbox",
                "Soft Particles",
                True,
                "particle_graph.node.rendering::output.sprite.property.soft_particles",
            ),
            {"bool_value": True},
        ),
        (
            (
                "drag_float",
                "Fade Distance",
                True,
                "particle_graph.node.rendering::output.sprite.property.soft_distance",
            ),
            {"numeric_value": 0.5},
        ),
    ]


def test_particle_graph_editor_sets_mesh_asset_through_live_authoring_model(
    tmp_path, monkeypatch
):
    from Infernux.engine.ui import particle_graph_editor_panel as module

    mesh_path = tmp_path / "ParticleShard.obj"
    mesh_path.write_text("o ParticleShard\nv 0 0 0\n", encoding="utf-8")
    monkeypatch.setattr(module, "_asset_guid_from_path", lambda _path: "mesh-guid")
    monkeypatch.setattr(
        module, "_portable_asset_path_hint", lambda _path: "Assets/Models/ParticleShard.obj"
    )

    panel = module.ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    panel._model.prepare_node_creation("rendering")
    node = panel._on_node_add("particle.output.mesh", 540.0, 460.0)

    reference = panel.set_node_asset_reference(node.uid, "mesh", str(mesh_path))
    routed = panel.set_rendering_output(node.uid)

    assert reference == {
        "guid": "mesh-guid",
        "path_hint": "Assets/Models/ParticleShard.obj",
    }
    assert node.data["mesh"] == reference
    assert panel._selected_node_uid == node.uid
    assert panel._view.selected_nodes == [node.uid]
    assert panel._dirty is True
    snapshot = panel.authoring_snapshot()
    assert snapshot["panel_id"] == "particle_graph_editor"
    saved_node = next(item for item in snapshot["nodes"] if item["uid"] == node.uid)
    assert saved_node["properties"]["mesh"] == reference
    assert routed["changed"] is True
    output_links = [
        link
        for link in snapshot["links"]
        if link["source_node"] == "rendering::root.rendering"
        and link["source_port"] == "out"
    ]
    assert output_links == [
        {
            "uid": routed["link_uid"],
            "source_node": "rendering::root.rendering",
            "source_port": "out",
            "target_node": node.uid,
            "target_port": "in",
        }
    ]


def test_particle_graph_editor_rejects_wrong_asset_kind(tmp_path):
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    texture_path = tmp_path / "not-a-mesh.png"
    texture_path.write_bytes(b"png")
    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    panel._model.prepare_node_creation("rendering")
    node = panel._on_node_add("particle.output.mesh", 540.0, 460.0)

    with pytest.raises(ValueError, match="requires a model asset"):
        panel.set_node_asset_reference(node.uid, "mesh", str(texture_path))


def test_particle_graph_editor_semantic_authoring_edits_orientation_streams():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None

    initial = panel.add_authoring_node(
        "init", "particle.attribute.set_orientation", 240.0, 40.0
    )
    changed = panel.set_node_property(
        initial["uid"], "degrees", [15.0, 30.0, 45.0]
    )
    initial_link = panel.connect_stream("init::root.init", initial["uid"])
    angular = panel.add_authoring_node(
        "update", "particle.update.rotate_orientation", 240.0, 400.0
    )
    panel.set_node_property(
        angular["uid"], "degrees_per_second", [130.0, 220.0, 310.0]
    )
    update_link = panel.connect_stream("update::root.update", angular["uid"])

    assert changed == {
        "node_uid": initial["uid"],
        "property_name": "degrees",
        "value": [15.0, 30.0, 45.0],
        "changed": True,
    }
    assert initial_link["changed"] is True
    assert update_link["changed"] is True
    assert panel._dirty is True
    snapshot = panel.authoring_snapshot()
    nodes = {node["uid"]: node for node in snapshot["nodes"]}
    assert nodes[initial["uid"]]["properties"]["degrees"] == [15.0, 30.0, 45.0]
    assert nodes[angular["uid"]]["properties"]["degrees_per_second"] == [
        130.0,
        220.0,
        310.0,
    ]

    with pytest.raises(ValueError, match="cross_stage"):
        panel.connect_stream(initial["uid"], angular["uid"])


def test_particle_graph_editor_public_api_authors_a_typed_event_route():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.particle import ParticleGraphCompiler, ParticleKernelLowerer

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    source_id = panel._asset.emitters[0].stable_id
    target = panel.add_authoring_emitter("Event Target")
    target_id = target["stable_id"]
    target_settings = target["settings"]
    target_settings["spawn_rate"] = 0.0
    changed = panel.set_authoring_emitter_settings(target_id, target_settings)
    assert changed["changed"] is True
    event_type = panel.add_event_type(
        "Impact",
        64,
        [
            {
                "name": "Weight",
                "type": TypeRef(ValueType.F32).to_dict(),
                "default": 1.25,
            }
        ],
    )
    route = panel.add_event_route(
        event_type["stable_id"], source_id, "update", target_id, 2
    )

    panel.select_authoring_emitter(source_id)
    snapshot = panel.authoring_snapshot()
    event_output_definition = next(
        value
        for value in snapshot["registered_types"]
        if value["type_id"]
        == particle_event_output_type_id(route["stable_id"], "update")
    )
    assert any(
        port["display_name"] == "Weight"
        and port["type"] == TypeRef(ValueType.F32).to_dict()
        for port in event_output_definition["ports"]
    )
    output = panel.add_authoring_node(
        "update",
        particle_event_output_type_id(route["stable_id"], "update"),
        260.0,
        230.0,
    )
    panel.connect_stream("update::root.update", output["uid"])

    panel.select_authoring_emitter(target_id)
    payload = panel.add_authoring_node(
        "init",
        particle_event_payload_type_id(route["stable_id"]),
        160.0,
        0.0,
    )
    size = panel.add_authoring_node(
        "init", "particle.attribute.set_size", 420.0, 0.0
    )
    panel.connect_stream("init::root.init", size["uid"])
    panel.connect_value(
        payload["uid"],
        particle_event_payload_port_id(event_type["fields"][0]["stable_id"]),
        size["uid"],
        "value",
    )
    replacement_payload = panel.add_authoring_node(
        "init",
        particle_event_payload_type_id(route["stable_id"]),
        160.0,
        120.0,
    )
    replaced = panel.connect_value(
        replacement_payload["uid"],
        particle_event_payload_port_id(event_type["fields"][0]["stable_id"]),
        size["uid"],
        "value",
    )
    assert replaced["changed"] is True
    assert [
        link.source_node
        for link in panel._model.links
        if link.target_node == size["uid"] and link.target_pin == "value"
    ] == [replacement_payload["uid"]]

    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(panel._asset)
    )
    assert kernel.events.routes[0].spawn_count == 2
    assert any(
        instruction.opcode == "event_payload"
        for instruction in kernel.emitters[1].init.instructions
    )


def test_particle_graph_editor_semantic_event_helpers_patch_and_route_nodes():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    source = panel.asset.emitters[0]
    original_settings = source.settings.to_dict()

    patched = panel.patch_authoring_emitter_settings(
        source.stable_id,
        {"capacity": 64, "spawn_rate": 120.0},
    )

    assert patched["settings"]["capacity"] == 64
    assert patched["settings"]["spawn_rate"] == 120.0
    assert patched["settings"]["lifetime"] == original_settings["lifetime"]
    with pytest.raises(ValueError, match="unknown emitter settings"):
        panel.patch_authoring_emitter_settings(source.stable_id, {"legacy": True})

    target_id = panel.add_authoring_emitter("Event Target")["stable_id"]
    event_type = panel.add_event_type(
        "Impact",
        4,
        [
            {
                "name": "Weight",
                "type": TypeRef(ValueType.F32).to_dict(),
                "default": 1.0,
            }
        ],
    )
    route = panel.add_event_route(
        event_type["stable_id"], source.stable_id, "update", target_id, 3
    )

    output = panel.add_event_output_node(route["stable_id"], 280.0, 220.0)
    assert panel.asset.emitters[panel._emitter_index].stable_id == source.stable_id
    assert output["stage"] == "update"
    assert output["type_id"] == particle_event_output_type_id(
        route["stable_id"], "update"
    )

    payload = panel.add_event_payload_node(route["stable_id"], 160.0, 40.0)
    assert panel.asset.emitters[panel._emitter_index].stable_id == target_id
    assert payload["stage"] == "init"
    assert payload["type_id"] == particle_event_payload_type_id(route["stable_id"])


def test_particle_graph_editor_updates_event_identity_in_place():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    source_id = panel.asset.emitters[0].stable_id
    target_id = panel.add_authoring_emitter("Target")["stable_id"]
    event_type = panel.add_event_type(
        "Impact",
        32,
        [{"name": "Weight", "type": TypeRef(ValueType.F32).to_dict(), "default": 1.0}],
    )
    route = panel.add_event_route(
        event_type["stable_id"], source_id, "update", target_id, 2
    )

    panel.select_authoring_emitter(source_id)
    output = panel.add_authoring_node(
        "update",
        particle_event_output_type_id(route["stable_id"], "update"),
        240.0,
        230.0,
    )
    panel.connect_stream("update::root.update", output["uid"])
    panel.select_authoring_emitter(target_id)
    payload = panel.add_authoring_node(
        "init", particle_event_payload_type_id(route["stable_id"]), 160.0, 0.0
    )
    size = panel.add_authoring_node(
        "init", "particle.attribute.set_size", 420.0, 0.0
    )
    panel.connect_stream("init::root.init", size["uid"])
    payload_port = particle_event_payload_port_id(
        event_type["fields"][0]["stable_id"]
    )
    panel.connect_value(payload["uid"], payload_port, size["uid"], "value")

    updated_type = panel.update_event_type(
        event_type["stable_id"],
        "Impact Renamed",
        8,
        [
            {
                "stable_id": event_type["fields"][0]["stable_id"],
                "name": "Impulse",
                "type": TypeRef(ValueType.F32).to_dict(),
                "default": 2.0,
            }
        ],
    )
    assert updated_type["stable_id"] == event_type["stable_id"]
    assert updated_type["fields"][0]["stable_id"] == event_type["fields"][0]["stable_id"]
    assert updated_type["capacity_per_step"] == 8
    assert updated_type["changed"] is True
    assert any(
        link.source_port == payload_port for link in panel.asset.emitters[1].init.links
    )

    updated_route = panel.update_event_route(
        route["stable_id"],
        event_type["stable_id"],
        source_id,
        "update",
        target_id,
        7,
    )
    assert updated_route["stable_id"] == route["stable_id"]
    assert updated_route["spawn_count"] == 7
    assert updated_route["route_nodes_removed"] is False
    assert any(
        node.type_id == particle_event_output_type_id(route["stable_id"], "update")
        for node in panel.asset.emitters[0].update.nodes
    )


def test_particle_graph_editor_prunes_only_invalid_event_edit_context():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    source_id = panel.asset.emitters[0].stable_id
    target_id = panel.add_authoring_emitter("Target")["stable_id"]
    event_type = panel.add_event_type(
        "Impact",
        32,
        [{"name": "Weight", "type": TypeRef(ValueType.F32).to_dict(), "default": 1.0}],
    )
    route = panel.add_event_route(
        event_type["stable_id"], source_id, "update", target_id, 2
    )
    field_id = event_type["fields"][0]["stable_id"]
    payload_port = particle_event_payload_port_id(field_id)

    panel.select_authoring_emitter(source_id)
    output = panel.add_authoring_node(
        "update",
        particle_event_output_type_id(route["stable_id"], "update"),
        240.0,
        230.0,
    )
    panel.connect_stream("update::root.update", output["uid"])
    constant = panel.add_authoring_node("update", "common.constant.f32", 80.0, 0.0)
    panel.connect_value(constant["uid"], "value", output["uid"], payload_port)

    panel.update_event_type(
        event_type["stable_id"],
        "Impact",
        32,
        [
            {
                "stable_id": field_id,
                "name": "Weight",
                "type": TypeRef(ValueType.VEC3).to_dict(),
                "default": [1.0, 1.0, 1.0],
            }
        ],
    )
    source = panel.asset.emitters[0]
    assert any(link.target_port == "in" for link in source.update.links)
    assert not any(link.target_port == payload_port for link in source.update.links)

    moved = panel.update_event_route(
        route["stable_id"],
        event_type["stable_id"],
        source_id,
        "init",
        target_id,
        2,
    )
    assert moved["route_nodes_removed"] is True
    route_types = {
        particle_event_output_type_id(route["stable_id"], stage)
        for stage in ("init", "update", "rendering")
    }
    route_types.add(particle_event_payload_type_id(route["stable_id"]))
    assert not any(
        node.type_id in route_types
        for emitter in panel.asset.emitters
        for stage in (emitter.init, emitter.update, emitter.rendering)
        for node in stage.nodes
    )


def test_particle_graph_editor_removes_event_route_nodes_transactionally():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    source_id = panel.asset.emitters[0].stable_id
    target_id = panel.add_authoring_emitter("Target")["stable_id"]
    event_type = panel.add_event_type(
        "Impact",
        32,
        [
            {
                "name": "Weight",
                "type": TypeRef(ValueType.F32).to_dict(),
                "default": 1.0,
            }
        ],
    )
    route = panel.add_event_route(
        event_type["stable_id"], source_id, "update", target_id, 2
    )

    panel.select_authoring_emitter(source_id)
    output = panel.add_authoring_node(
        "update",
        particle_event_output_type_id(route["stable_id"], "update"),
        240.0,
        230.0,
    )
    panel.connect_stream("update::root.update", output["uid"])
    panel.select_authoring_emitter(target_id)
    panel.add_authoring_node(
        "init",
        particle_event_payload_type_id(route["stable_id"]),
        160.0,
        0.0,
    )

    removed = panel.remove_event_route(route["stable_id"])

    assert removed == route
    assert panel.asset.event_routes == ()
    removed_types = {
        particle_event_output_type_id(route["stable_id"], "update"),
        particle_event_payload_type_id(route["stable_id"]),
    }
    assert not any(
        node.type_id in removed_types
        for emitter in panel.asset.emitters
        for stage in (emitter.init, emitter.update, emitter.rendering)
        for node in stage.nodes
    )
    ParticleGraphAsset.from_dict(panel.asset.to_dict())


def test_particle_graph_editor_removing_emitter_cascades_event_routes():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    source_id = panel.asset.emitters[0].stable_id
    target_id = panel.add_authoring_emitter("Target")["stable_id"]
    event_type = panel.add_event_type("Death", 16, [])
    panel.add_event_route(
        event_type["stable_id"], source_id, "rendering", target_id, 1
    )

    panel.select_authoring_emitter(target_id)
    panel._remove_selected_emitter()

    assert [emitter.stable_id for emitter in panel.asset.emitters] == [source_id]
    assert panel.asset.event_routes == ()
    assert [value.stable_id for value in panel.asset.event_types] == [
        event_type["stable_id"]
    ]
    ParticleGraphAsset.from_dict(panel.asset.to_dict())


def test_particle_node_inspector_edits_unconnected_value_input_defaults():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    class Context:
        semantic_capture_enabled = True

        def __init__(self):
            self.items = []

        def label(self, _label):
            pass

        def separator(self):
            pass

        def drag_float(self, label, value, _speed, _minimum, _maximum):
            return 1.5 if label.startswith("B##") else value

        def record_semantic_item(self, *args, **kwargs):
            self.items.append((args, kwargs))

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    panel._on_node_creation_requested({"source_node": "", "gy": 230.0})
    node = panel._on_node_add("common.compare.greater_than", 400.0, 230.0)
    panel._selected_node_uid = node.uid
    ctx = Context()

    panel._render_node_properties(ctx)

    assert node.data["b"] == 1.5
    semantic_ids = {args[3] for args, _kwargs in ctx.items}
    assert semantic_ids == {
        f"particle_graph.node.{node.uid}.property.a",
        f"particle_graph.node.{node.uid}.property.b",
    }


def test_particle_gradient_editor_uses_hdr_and_channel_semantics():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.graph.ramp import Gradient

    class Context:
        def __init__(self):
            self.hdr = False
            self.items = []

        def combo(self, _label, value, _options, _popup_height):
            return value

        def separator(self):
            pass

        def label(self, _label):
            pass

        def drag_float(self, _label, value, _speed, _minimum, _maximum):
            return value

        def color_edit(self, _label, *color, hdr=False):
            self.hdr = hdr
            return color

        def button(self, _label):
            return False

        def record_semantic_item(self, *args, **kwargs):
            self.items.append((args, kwargs))

    ctx = Context()
    result = ParticleGraphEditorPanel._render_gradient_property(
        ctx,
        "update::sample",
        "gradient",
        Gradient().to_dict(),
    )

    assert result == Gradient().to_dict()
    assert ctx.hdr is True
    semantic_ids = {args[3] for args, _kwargs in ctx.items}
    assert "particle_graph.node.update::sample.property.gradient.key.0.time" in semantic_ids
    assert (
        "particle_graph.node.update::sample.property.gradient.key.0.color.r"
        in semantic_ids
    )
    assert (
        "particle_graph.node.update::sample.property.gradient.key.1.color.a"
        in semantic_ids
    )


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
    assert reopened.reload_from_disk() is True
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
