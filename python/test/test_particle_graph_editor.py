from __future__ import annotations

import json
import os
import struct
from types import SimpleNamespace

import pytest

from Infernux.engine.project_context import clear_panel_tracking
from Infernux.engine.ui.graph_document_authoring import (
    GraphDocumentAuthoringModel,
    ParticleEmitterGraphAuthoringModel,
    particle_stage_definition_filter,
)
from Infernux.graph.types import TypeRef, ValueType
from Infernux.particle.asset import (
    EmitterSettings,
    ParticleEmitterAsset,
    ParticleEventField,
    ParticleEventFlow,
    ParticleEventType,
    ParticleGraphAsset,
    ParticleParameter,
    particle_attribute_cache_id,
)
from Infernux.particle.nodes import (
    PARTICLE_EVENT_ACTIVE_TYPE_ID,
    PARTICLE_EVENT_TRIGGER_TYPE_ID,
    particle_event_payload_port_id,
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
    assert "particle.context.delta_time" not in {
        definition.type_id for definition in model.registered_types()
    }
    assert "particle.attribute.velocity" in {
        definition.type_id for definition in model.registered_types()
    }
    velocity = model.add_node("particle.attribute.velocity", 240.0, 20.0)
    velocity.data["value"] = [1.0, 2.0, 3.0]
    assert model.add_link("init.velocity", "out", velocity.uid, "in") is not None

    restored = model.to_document()
    assert restored.domain == "particle.init"
    authored = next(node for node in restored.nodes if node.uid == velocity.uid)
    assert authored.position == (240.0, 20.0)
    assert authored.properties["value"] == [1.0, 2.0, 3.0]
    assert all(link.kind.value == "exec" for link in restored.links)


def test_particle_update_palette_exposes_explicit_delta_time_value():
    document = ParticleGraphAsset().emitters[0].update
    model = _stage_model(document)

    assert "particle.context.delta_time" in {
        definition.type_id for definition in model.registered_types()
    }


def test_particle_rendering_palette_exposes_independent_wait_and_until_nodes():
    document = ParticleGraphAsset().emitters[0].rendering
    model = _stage_model(document)
    type_ids = {definition.type_id for definition in model.registered_types()}

    assert "particle.control.wait_frames" in type_ids
    assert "particle.control.wait_seconds" in type_ids
    assert "particle.control.until_frames" in type_ids
    assert "particle.control.until_seconds" in type_ids


def test_particle_wait_nodes_are_exposed_after_gpu_resume_is_available():
    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    registered = model.registered_types()
    visible_types = {definition.type_id for definition in registered}

    assert "particle.control.wait_frames" in visible_types
    assert "particle.control.wait_seconds" in visible_types
    assert "particle.control.until_frames" in visible_types
    assert "particle.control.until_seconds" in visible_types
    assert not any("per_second" in type_id for type_id in visible_types)
    assert not any("per second" in definition.label.lower() for definition in registered)
    assert "particle.update.acceleration" not in visible_types

    model.set_authoring_stage("update")
    model.prepare_node_creation("update")
    assert model.add_node("particle.control.wait_frames", 240.0, 20.0) is not None
    assert model.authoring_stage == "update"

    model.set_authoring_stage("rendering")
    model.prepare_node_creation("rendering")
    assert model.add_node("particle.control.wait_seconds", 240.0, 20.0) is not None
    assert model.authoring_stage == "rendering"


def test_optional_collision_lanes_never_capture_ordinary_node_creation():
    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    model.set_authoring_stage("update")
    model.prepare_node_creation("collision_enter")

    ordinary = model.add_node("particle.attribute.size", 240.0, 460.0)
    assert model.stage_for_uid(ordinary.uid) == "update"

    enabled, reason = model.node_creation_state("particle.root.collision_enter")
    assert enabled is False
    assert "Enable Collision" in reason

    model.set_collision_enabled(True)
    root = model.add_node("particle.root.collision_enter", 40.0, 460.0)
    model.set_authoring_stage("collision_enter")
    model.prepare_node_creation("collision_enter")
    collision_node = model.add_node("particle.attribute.size", 260.0, 460.0)
    assert model.stage_for_uid(root.uid) == "collision_enter"
    assert model.stage_for_uid(collision_node.uid) == "collision_enter"

    model.set_collision_enabled(False)
    unavailable = model.get_node_type(root)
    assert "Unavailable" in unavailable.label
    assert unavailable.header_color[0] > unavailable.header_color[1]


def test_particle_data_and_attribute_nodes_are_creatable_in_every_particle_stage():
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
        "particle.attribute.get",
        "particle.vector_field.sample",
        "particle.attribute.lifetime",
        "particle.attribute.normalized_age",
    ):
        assert type_id in init_types
        assert type_id in update_types
        assert type_id in rendering_types

    assert "particle.attribute.velocity" in rendering_types
    assert "particle.output.sprite" not in update_types

    model = ParticleEmitterGraphAuthoringModel(emitter)
    model.prepare_node_creation("update")
    position = model.add_node("particle.attribute.get", 200.0, 230.0)
    assert position.uid.startswith("update::")


def test_get_attribute_exec_output_is_revealed_and_cleared_with_exec_input():
    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    model.prepare_node_creation("update")
    sample = model.add_node("particle.attribute.get", 240.0, 230.0)
    tail = model.add_node("particle.attribute.size", 480.0, 230.0)

    assert {pin.id for pin in model.get_node_type(sample).output_pins()} == {"value"}
    incoming = model.add_link("update::root.update", "out", sample.uid, "in")
    assert incoming is not None
    assert {pin.id for pin in model.get_node_type(sample).output_pins()} == {
        "value",
        "out",
    }
    outgoing = model.add_link(sample.uid, "out", tail.uid, "in")
    assert outgoing is not None

    assert model.remove_link(incoming.uid) is True
    assert model.find_link(outgoing.uid) is None
    assert {pin.id for pin in model.get_node_type(sample).output_pins()} == {"value"}


def test_attribute_cache_node_owns_storage_and_infers_its_input_type():
    model = ParticleEmitterGraphAuthoringModel(ParticleEmitterAsset())
    model.set_authoring_stage("update")
    model.prepare_node_creation("update")

    node = model.add_node("particle.attribute.cache", 240.0, 250.0)
    source = model.add_node(
        "common.constant.vec3", 20.0, 250.0, value=[1.0, 2.0, 3.0]
    )
    assert model.add_link(source.uid, "value", node.uid, "value") is not None
    definition = model.get_node_type(node)

    assert node.data["name"] == "Attribute Cache"
    assert node.data["value_type"] == "vec3"
    assert node.data["value"] == [0.0, 0.0, 0.0]
    value_pin = next(pin for pin in definition.input_pins() if pin.id == "value")
    assert value_pin.data_type == "vec3"
    assert model.stage_for_uid(node.uid) == "update"
    expected_id = particle_attribute_cache_id(
        "update", model._document_uid(node.uid)
    )
    assert expected_id in {value for _label, value in model.attribute_cache_entries()}
    assert model.node_creation_state("particle.attribute.cache") == (True, "")


def test_attribute_cache_infers_texture_handle_and_forces_set_composition():
    asset = ParticleGraphAsset(
        parameters=(
            ParticleParameter(
                "smoke-texture",
                "Smoke Texture",
                TypeRef(ValueType.TEXTURE2D),
                {"guid": "texture-guid", "path_hint": "Assets/Smoke.png"},
            ),
        )
    )
    definitions = particle_graph_node_definitions(asset)
    model = ParticleEmitterGraphAuthoringModel(
        asset.emitters[0], definition_set=definitions
    )
    model.prepare_node_creation("init")
    cache = model.add_node(
        "particle.attribute.cache", 240.0, 250.0, composition="multiply"
    )
    model.prepare_node_creation("init")
    parameter = model.add_node(
        "particle.parameter.get",
        20.0,
        250.0,
        parameter="smoke-texture",
    )

    assert model.add_link(parameter.uid, "value", cache.uid, "value") is not None

    assert cache.data["value_type"] == "texture2d"
    assert cache.data["value"] == {"guid": "", "path_hint": ""}
    assert cache.data["composition"] == "set"


def test_particle_attribute_node_title_tracks_composition_mode():
    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    model.prepare_node_creation("update")
    velocity = model.add_node("particle.attribute.velocity", 240.0, 220.0)
    definition = model.definition_for_type(velocity.type_id)
    assert definition is not None
    composition = definition.property("composition")
    assert composition is not None
    assert composition.choices == (
        ("Set", "set"),
        ("Add", "add"),
        ("Multiply", "multiply"),
    )
    assert model.get_node_type(velocity).label == "Set Velocity"
    velocity.data["composition"] = "add"
    assert model.get_node_type(velocity).label == "Add Velocity"
    velocity.data["composition"] = "multiply"
    assert model.get_node_type(velocity).label == "Multiply Velocity"


def test_collision_lifecycle_roots_require_setting_and_are_unique():
    disabled = ParticleEmitterGraphAuthoringModel(ParticleEmitterAsset())
    type_ids = {definition.type_id for definition in disabled.registered_types()}
    assert {
        "particle.root.collision_enter",
        "particle.root.collision_stay",
        "particle.root.collision_exit",
    } <= type_ids
    assert disabled.node_creation_state("particle.root.collision_enter") == (
        False,
        "Enable Collision in Emitter Settings first",
    )
    with pytest.raises(ValueError, match="Enable Collision"):
        disabled.add_node("particle.root.collision_enter", 0.0, 230.0)

    enabled = ParticleEmitterGraphAuthoringModel(
        ParticleEmitterAsset(settings=EmitterSettings(collision_enabled=True))
    )
    enabled.prepare_node_creation("collision_enter")
    root = enabled.add_node("particle.root.collision_enter", 0.0, 230.0)
    assert root.uid.startswith("collision_enter::")
    documents = enabled.to_documents()
    assert documents["collision_enter"].domain == "particle.collision_enter"
    assert documents["collision_stay"] is None
    assert enabled.node_creation_state("particle.root.collision_enter")[0] is False
    with pytest.raises(ValueError, match="already exists"):
        enabled.add_node("particle.root.collision_enter", 0.0, 230.0)
    assert enabled.remove_node(root.uid) is True
    assert enabled.node_creation_state("particle.root.collision_enter") == (True, "")


def test_get_attribute_uses_dropdown_and_instance_typed_port_colors():
    emitter = ParticleGraphAsset().emitters[0]
    definitions = particle_graph_node_definitions(ParticleGraphAsset(emitters=(emitter,)))
    registered = {item.type_id for item in definitions.registry.definitions()}
    assert "particle.attribute.get" in registered
    assert not any(type_id.startswith("particle.attribute.read_") for type_id in registered)

    model = ParticleEmitterGraphAuthoringModel(emitter, definition_set=definitions)
    model.prepare_node_creation("update")
    node = model.add_node("particle.attribute.get", 200.0, 230.0)
    position_type = model.get_node_type(node)
    position_pin = next(pin for pin in position_type.pins if pin.id == "value")
    attribute_field = next(
        field for field in position_type.inline_fields if field.id == "attribute"
    )

    assert position_pin.data_type == "vec3"
    assert "builtin.position" in attribute_field.enum_values
    assert "Position" in attribute_field.enum_labels
    assert "builtin.collision_point" in attribute_field.enum_values
    assert "builtin.collision_relative_velocity" in attribute_field.enum_values
    assert "builtin.collision_penetration" in attribute_field.enum_values
    assert "builtin.collision_is_trigger" in attribute_field.enum_values
    assert "builtin.collision_material" in attribute_field.enum_values
    assert not any(
        value.startswith("internal.collision_")
        for value in attribute_field.enum_values
    )

    node.data["attribute"] = "builtin.age"
    age_type = model.get_node_type(node)
    age_pin = next(pin for pin in age_type.pins if pin.id == "value")
    assert age_pin.data_type == "f32"
    assert age_pin.color != position_pin.color


def test_get_parameter_uses_parameter_identity_as_title_and_typed_output():
    asset = ParticleGraphAsset(
        parameters=(
            ParticleParameter(
                "wind",
                "Wind",
                TypeRef(ValueType.VEC3),
                [0.0, 1.0, 0.0],
            ),
            ParticleParameter(
                "intensity",
                "Intensity",
                TypeRef(ValueType.F32),
                1.0,
            ),
        )
    )
    definitions = particle_graph_node_definitions(asset)
    model = ParticleEmitterGraphAuthoringModel(
        asset.emitters[0], definition_set=definitions
    )
    model.prepare_node_creation("update")
    node = model.add_node("particle.parameter.get", 200.0, 230.0)

    wind_type = model.get_node_type(node)
    wind_pin = next(pin for pin in wind_type.pins if pin.id == "value")
    assert node.data["parameter"] == "wind"
    assert wind_type.label == "Wind"
    assert wind_pin.data_type == "vec3"
    assert not any(field.id == "parameter" for field in wind_type.inline_fields)

    node.data["parameter"] = "intensity"
    intensity_type = model.get_node_type(node)
    intensity_pin = next(pin for pin in intensity_type.pins if pin.id == "value")
    assert intensity_type.label == "Intensity"
    assert intensity_pin.data_type == "f32"
    assert intensity_pin.color != wind_pin.color


def test_get_attribute_disconnects_links_made_invalid_by_attribute_change():
    emitter = ParticleGraphAsset().emitters[0]
    model = ParticleEmitterGraphAuthoringModel(emitter)
    model.prepare_node_creation("update")
    attribute = model.add_node("particle.attribute.get", 200.0, 180.0)
    vector_field = model.add_node("particle.vector_field.sample", 440.0, 180.0)
    link = model.add_link(attribute.uid, "value", vector_field.uid, "position")
    assert link is not None

    attribute.data["attribute"] = "builtin.age"
    assert model.remove_invalid_links_for_node(attribute.uid) == (link.uid,)
    assert all(existing.uid != link.uid for existing in model.links)


def test_vector_field_sample_connects_to_simulation_space_acceleration():
    emitter = ParticleGraphAsset().emitters[0]
    model = ParticleEmitterGraphAuthoringModel(emitter)
    model.prepare_node_creation("update")

    position = model.add_node("particle.attribute.get", 200.0, 180.0)
    vector_field = model.add_node("particle.vector_field.sample", 440.0, 180.0)
    acceleration = model.add_node("particle.attribute.velocity", 680.0, 180.0)

    assert model.add_link(position.uid, "value", vector_field.uid, "position") is not None
    assert model.add_link(vector_field.uid, "value", acceleration.uid, "value") is not None


def test_particle_trigger_event_is_available_after_event_flow_is_implemented():
    from Infernux.particle import default_event_graph

    source = ParticleEmitterAsset(
        stable_id="source",
        name="Source",
        event_flows=(ParticleEventFlow("event", default_event_graph("event")),),
    )
    asset = ParticleGraphAsset(
        emitters=(source,),
        event_types=(
            ParticleEventType(
                "event",
                "Event",
                32,
                (ParticleEventField("amount", "Amount", TypeRef(ValueType.F32), 1.0),),
            ),
        ),
    )
    model = ParticleEmitterGraphAuthoringModel(
        source, definition_set=particle_graph_node_definitions(asset)
    )
    registered = {definition.type_id for definition in model.registered_types()}

    type_id = PARTICLE_EVENT_TRIGGER_TYPE_ID
    assert type_id in registered
    for stage in ("init", "update", "rendering", "event.event"):
        model.prepare_node_creation(stage)
        node = model.add_node(type_id, 200.0, 230.0, event="event")
        assert model.stage_for_uid(node.uid) == stage
    assert any(
        node.type_id == PARTICLE_EVENT_ACTIVE_TYPE_ID for node in model.nodes
    )


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
        "particle.attribute.lifetime",
        "particle.attribute.velocity",
        "particle.root.update",
        "particle.attribute.velocity",
        "particle.root.rendering",
        "particle.output.sprite",
    ]
    assert model.remove_node("init::root.init") is False

    velocity = model.add_node("particle.attribute.velocity", 220.0, 0.0)
    acceleration = model.add_node("particle.attribute.velocity", 220.0, 230.0)
    assert model.add_link("init::init.velocity", "out", velocity.uid, "in") is not None
    assert model.add_link("update::update.velocity", "out", acceleration.uid, "in") is not None
    assert not model.validate_link(velocity.uid, "out", acceleration.uid, "in")

    documents = model.to_documents()
    assert [node.type_id for node in documents["init"].nodes] == [
        "particle.root.init",
        "particle.attribute.lifetime",
        "particle.attribute.velocity",
        "particle.attribute.velocity",
    ]
    assert [node.type_id for node in documents["update"].nodes] == [
        "particle.root.update",
        "particle.attribute.velocity",
        "particle.attribute.velocity",
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


def test_particle_graph_blackboard_api_updates_and_removes_reference_nodes():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    parameter = panel.add_authoring_parameter(
        "Wind", "vec3", [0.0, 1.0, 0.0]
    )
    node = panel.add_authoring_node("update", "particle.parameter.get", 200.0, 230.0)
    assert node["properties"]["parameter"] == parameter["stable_id"]

    changed = panel.update_authoring_parameter(
        parameter["stable_id"],
        {"name": "Wind Strength", "default": [1.0, 2.0, 3.0]},
    )
    assert changed["name"] == "Wind Strength"
    assert changed["changed"] is True

    panel.remove_authoring_parameter(parameter["stable_id"])
    assert panel.asset.parameters == ()
    assert not any(
        record.type_id == "particle.parameter.get"
        for record in panel.asset.emitters[0].update.nodes
    )


def test_particle_parameter_canvas_drop_creates_the_selected_parameter_node():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    parameter = panel.add_authoring_parameter(
        "Wind", "vec3", [0.0, 1.0, 0.0]
    )

    panel._on_canvas_drop(
        "PARTICLE_PARAMETER", parameter["stable_id"], 320.0, 230.0
    )

    nodes = [
        node
        for node in panel.asset.emitters[0].update.nodes
        if node.type_id == "particle.parameter.get"
    ]
    assert len(nodes) == 1
    assert nodes[0].properties["parameter"] == parameter["stable_id"]
    assert panel.authoring_snapshot()["parameters"] == [
        {
            "stable_id": parameter["stable_id"],
            "name": "Wind",
            "type": {"value_type": "vec3", "space": "none"},
            "default": [0.0, 1.0, 0.0],
            "exposed": True,
            "category": "",
            "tooltip": "",
        }
    ]
    canvas_node = next(
        node
        for node in panel._model.nodes
        if node.type_id == "particle.parameter.get"
        and node.data["parameter"] == parameter["stable_id"]
    )
    assert panel._model.get_node_type(canvas_node).label == "Wind"


def test_removing_attribute_cache_node_removes_dependent_get_nodes():
    model = ParticleEmitterGraphAuthoringModel(ParticleEmitterAsset())
    model.prepare_node_creation("update")
    owner = model.add_node("particle.attribute.cache", 240.0, 230.0)
    cache_id = particle_attribute_cache_id(
        "update", model._document_uid(owner.uid)
    )
    model.prepare_node_creation("rendering")
    reader = model.add_node(
        "particle.attribute.get", 320.0, 1150.0, attribute=cache_id
    )

    assert model.find_node(reader.uid) is not None
    assert model.remove_node(owner.uid) is True
    assert model.find_node(reader.uid) is None


def test_get_attribute_dropdown_discovers_node_owned_cache_type():
    model = ParticleEmitterGraphAuthoringModel(ParticleEmitterAsset())
    model.prepare_node_creation("update")
    owner = model.add_node(
        "particle.attribute.cache",
        240.0,
        230.0,
        name="Heat",
        value_type="f32",
    )
    cache_id = particle_attribute_cache_id(
        "update", model._document_uid(owner.uid)
    )
    model.prepare_node_creation("rendering")
    reader = model.add_node(
        "particle.attribute.get", 320.0, 1150.0, attribute=cache_id
    )

    assert ("Heat", cache_id) in model.attribute_cache_entries()
    value_pin = next(
        pin for pin in model.get_node_type(reader).output_pins() if pin.id == "value"
    )
    assert value_pin.data_type == "f32"


def test_particle_graph_editor_authors_texture2d_parameter_node():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    parameter = panel.add_authoring_parameter("Smoke Texture", "texture2d")
    node = panel.add_authoring_parameter_node(
        parameter["stable_id"], 340.0, 420.0, stage="update"
    )

    assert parameter["default"] == {"guid": "", "path_hint": ""}
    assert node["properties"]["parameter"] == parameter["stable_id"]
    canvas_node = panel._model.find_node(node["uid"])
    port = panel._definition_for_type("particle.parameter.get").port("value")
    assert panel._model._effective_port_type(canvas_node, port) == TypeRef(
        ValueType.TEXTURE2D
    )


def test_live_draft_compile_failure_stays_in_the_graph_editor(monkeypatch, tmp_path):
    import Infernux.engine.ui.particle_graph_editor_panel as module

    panel = module.ParticleGraphEditorPanel()
    panel._file_path = str(tmp_path / "Draft.particlegraph")
    panel._draft_compile_due_at = 1.0
    logged_errors = []

    def _reject_draft(_asset, _path):
        raise ValueError("Join All needs another input")

    monkeypatch.setattr(module.time, "monotonic", lambda: 2.0)
    monkeypatch.setattr(
        module.ParticleArtifactRegistry, "publish_graph_asset", _reject_draft
    )
    monkeypatch.setattr(module.Debug, "log_error", logged_errors.append)

    panel._publish_live_draft_if_due()

    assert panel._draft_compile_error == "Join All needs another input"
    assert logged_errors == []

    monkeypatch.setattr(
        module.ParticleArtifactRegistry,
        "publish_graph_asset",
        lambda _asset, _path: object(),
    )
    panel._draft_compile_due_at = 1.0
    panel._publish_live_draft_if_due()

    assert panel._draft_compile_error == ""


def test_particle_emitter_row_defers_model_rebind_until_list_render_finishes(
    monkeypatch,
):
    import Infernux.engine.ui.particle_graph_editor_panel as module

    panel = module.ParticleGraphEditorPanel()
    panel._asset = ParticleGraphAsset(
        emitters=(
            ParticleEmitterAsset(stable_id="first", name="First"),
            ParticleEmitterAsset(stable_id="second", name="Second"),
        )
    )
    panel._bind_stage()
    rendered_rows = []
    selected = []
    original_select = panel._select_emitter

    def _begin(_ctx, entry_id, _selected):
        rendered_rows.append(entry_id)
        return entry_id.endswith("second"), (0.0, 0.0, 100.0, 28.0)

    def _select(index):
        assert rendered_rows == ["particle_emitter_first", "particle_emitter_second"]
        selected.append(index)
        original_select(index)

    monkeypatch.setattr(module, "render_workspace_add_header", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "begin_workspace_entry", _begin)
    monkeypatch.setattr(module, "paint_workspace_entry", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "finish_workspace_entry", lambda *_a, **_k: None)
    monkeypatch.setattr(panel, "_select_emitter", _select)
    ctx = SimpleNamespace(
        semantic_capture_enabled=False,
        begin_popup_context_item=lambda _item_id: False,
    )

    panel._render_emitter_page(ctx)

    assert selected == [1]
    assert panel._emitter_index == 1
    assert panel._selected_emitter().stable_id == "second"


def test_particle_parameter_rename_state_uses_the_existing_undoable_api():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    parameter = panel.add_authoring_parameter("Intensity", "f32", 1.0)

    panel._request_parameter_rename(parameter["stable_id"])
    panel._parameter_rename_buffer = "Emission Strength"

    assert panel._commit_parameter_rename() is True
    assert panel.asset.parameters[0].name == "Emission Strength"
    assert panel._renaming_parameter_id == ""


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
    velocity = panel._on_node_add("particle.attribute.velocity", 220.0, 0.0)
    panel._on_link_created("init::init.velocity", "out", velocity.uid, "in")
    panel._select_stage("rendering")

    restored = ParticleGraphEditorPanel()
    restored.load_state(panel.save_state())

    assert restored._dirty is True
    assert restored._stage == "rendering"
    assert [node.type_id for node in restored.asset.emitters[0].init.nodes] == [
        "particle.root.init",
        "particle.attribute.lifetime",
        "particle.attribute.velocity",
        "particle.attribute.velocity",
    ]
    assert [node.type_id for node in restored._model.nodes] == [
        "particle.root.init",
        "particle.attribute.lifetime",
        "particle.attribute.velocity",
        "particle.attribute.velocity",
        "particle.root.update",
        "particle.attribute.velocity",
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


def test_particle_graph_editor_ignores_float32_widget_round_trip_noise():
    from Infernux.engine.ui.inspector_utils import preserve_ui_float_precision
    from Infernux.particle.asset import EmitterSettings

    original = EmitterSettings(spawn_rate=3.7)
    float32 = lambda value: struct.unpack("f", struct.pack("f", value))[0]
    widget_value = EmitterSettings(spawn_rate=float32(original.spawn_rate))

    assert preserve_ui_float_precision(widget_value, original) == original
    changed = EmitterSettings(spawn_rate=4.0)
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


def test_particle_graph_alignment_property_publishes_combo_semantic():
    from Infernux.engine.ui.particle_graph_editor_panel import (
        _record_scalar_node_property_semantics,
    )

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
        key="alignment",
        label="Billboard Alignment",
        value_type=ValueType.STRING,
        value="velocity",
    )

    assert ctx.items == [
        (
            (
                "combo",
                "Billboard Alignment",
                True,
                "particle_graph.node.rendering::output.sprite.property.alignment",
            ),
            {"string_value": "velocity"},
        )
    ]


def test_particle_graph_alignment_axis_is_only_visible_in_axis_mode():
    from Infernux.engine.ui.particle_graph_editor_panel import (
        _node_property_is_visible,
    )

    node = SimpleNamespace(data={})
    assert _node_property_is_visible(node, "alignment_axis") is False
    node.data["alignment"] = "velocity"
    assert _node_property_is_visible(node, "alignment_axis") is False
    node.data["alignment"] = "axis"
    assert _node_property_is_visible(node, "alignment_axis") is True
    assert _node_property_is_visible(node, "material") is True


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


def test_particle_graph_editor_authors_typed_sdf_data_interface(tmp_path, monkeypatch):
    from Infernux.engine.ui import particle_graph_editor_panel as module

    sdf_path = tmp_path / "Collision.inxsdf"
    sdf_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "_asset_guid_from_path", lambda _path: "sdf-guid")
    monkeypatch.setattr(
        module,
        "_portable_asset_path_hint",
        lambda _path: "Assets/VFX/Collision.inxsdf",
    )

    panel = module.ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    emitter_id = panel.asset.emitters[0].stable_id
    interface = panel.add_authoring_data_interface(
        emitter_id, "sdf_volume", "Collision Volume"
    )
    interface_id = interface["stable_id"]

    bound = panel.set_authoring_data_interface_asset(
        emitter_id, interface_id, str(sdf_path)
    )
    patched = panel.patch_authoring_data_interface(
        emitter_id,
        interface_id,
        {
            "space": "emitter_local",
            "distance_scale": 2.5,
            "filtering": "nearest",
        },
    )
    collision = panel.add_authoring_node(
        "update", "particle.update.collide_sdf", 220.0, 380.0
    )
    selected = panel.set_node_property(collision["uid"], "interface", interface_id)

    assert bound["texture"] == {
        "guid": "sdf-guid",
        "path_hint": "Assets/VFX/Collision.inxsdf",
    }
    assert patched["space"] == "emitter_local"
    assert patched["distance_scale"] == 2.5
    assert patched["filtering"] == "nearest"
    assert selected["value"] == interface_id
    snapshot = panel.authoring_snapshot()
    emitter_snapshot = snapshot["emitters"][0]
    assert emitter_snapshot["data_interfaces"][0]["stable_id"] == interface_id

    with pytest.raises(ValueError, match="still referenced"):
        panel.remove_authoring_data_interface(emitter_id, interface_id)
    panel.set_node_property(collision["uid"], "interface", "")
    removed = panel.remove_authoring_data_interface(emitter_id, interface_id)
    assert removed["changed"] is True
    assert panel.authoring_snapshot()["emitters"][0]["data_interfaces"] == []


def test_particle_graph_editor_rejects_wrong_data_interface_kind():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    emitter_id = panel.asset.emitters[0].stable_id
    vector_field = panel.add_authoring_data_interface(
        emitter_id, "vector_field", "Wind"
    )
    collision = panel.add_authoring_node(
        "update", "particle.update.collide_sdf", 220.0, 380.0
    )

    with pytest.raises(ValueError, match="requires a SdfVolume"):
        panel.set_node_property(
            collision["uid"], "interface", vector_field["stable_id"]
        )


def test_particle_graph_editor_semantic_authoring_edits_orientation_exec_chains():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None

    initial = panel.add_authoring_node(
        "init", "particle.attribute.orientation", 240.0, 40.0
    )
    changed = panel.set_node_property(
        initial["uid"], "degrees", [15.0, 30.0, 45.0]
    )
    initial_link = panel.connect_exec("init::root.init", initial["uid"])
    angular = panel.add_authoring_node(
        "update", "particle.attribute.orientation", 240.0, 400.0
    )
    panel.set_node_property(
        angular["uid"], "degrees", [130.0, 220.0, 310.0]
    )
    update_link = panel.connect_exec("update::root.update", angular["uid"])

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
    assert nodes[angular["uid"]]["properties"]["degrees"] == [
        130.0,
        220.0,
        310.0,
    ]

    with pytest.raises(ValueError, match="cross_stage"):
        panel.connect_exec(initial["uid"], angular["uid"])


def test_particle_graph_editor_public_api_disconnects_exec_and_value_links():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    lifetime = panel.add_authoring_node(
        "init", "particle.attribute.lifetime", 240.0, 40.0
    )
    connected = panel.connect_exec("init::root.init", lifetime["uid"])

    disconnected = panel.disconnect_link(connected["link_uid"])

    assert disconnected == {
        "link_uid": connected["link_uid"],
        "source_node_uid": "init::root.init",
        "source_port": "out",
        "target_node_uid": lifetime["uid"],
        "target_port": "in",
        "changed": True,
    }
    assert all(
        link["uid"] != connected["link_uid"]
        for link in panel.authoring_snapshot()["links"]
    )
    with pytest.raises(KeyError, match="link not found"):
        panel.disconnect_link(connected["link_uid"])

    size = panel.add_authoring_node("init", "particle.attribute.size", 480.0, 40.0)
    constant = panel.add_authoring_node("init", "common.constant.f32", 240.0, 180.0)
    value_link = panel.connect_value(constant["uid"], "value", size["uid"], "value")
    value_disconnected = panel.disconnect_link(value_link["link_uid"])
    assert value_disconnected["source_port"] == "value"
    assert value_disconnected["target_port"] == "value"


def test_particle_graph_editor_type_catalog_is_searchable_and_paged():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    catalog = panel.authoring_type_catalog(query="ribbon", offset=0, limit=1)

    assert catalog["offset"] == 0
    assert catalog["limit"] == 1
    assert catalog["total"] >= 1
    assert len(catalog["types"]) == 1
    assert "ribbon" in catalog["types"][0]["type_id"]


def test_particle_graph_editor_exposes_asset_local_burst_node():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    panel.add_authoring_emitter("Burst Target")
    panel.select_authoring_emitter(panel._asset.emitters[0].stable_id)

    catalog = panel.authoring_type_catalog(query="burst")

    assert [item["type_id"] for item in catalog["types"]] == [
        "particle.emitter.burst"
    ]
    created = panel.add_authoring_node(
        "update", "particle.emitter.burst", 320.0, 260.0
    )
    assert created["properties"]["emitter"] in {
        emitter.stable_id for emitter in panel._asset.emitters
    }


def test_particle_graph_editor_edits_unlinked_value_input_defaults():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    noise = panel.add_authoring_node(
        "update", "common.noise.vector3d", 220.0, 360.0
    )

    changed = panel.set_node_property(noise["uid"], "frequency", 2.5)

    assert changed == {
        "node_uid": noise["uid"],
        "property_name": "frequency",
        "value": 2.5,
        "changed": True,
    }
    saved = next(
        node
        for node in panel.authoring_snapshot()["nodes"]
        if node["uid"] == noise["uid"]
    )
    assert saved["properties"]["frequency"] == 2.5


def test_particle_graph_editor_rejects_default_edit_for_linked_value_input():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    frequency = panel.add_authoring_node(
        "update", "common.constant.f32", 120.0, 360.0
    )
    noise = panel.add_authoring_node(
        "update", "common.noise.vector3d", 320.0, 360.0
    )
    panel.connect_value(frequency["uid"], "value", noise["uid"], "frequency")

    with pytest.raises(ValueError, match="is driven by a value link"):
        panel.set_node_property(noise["uid"], "frequency", 2.5)


def test_particle_graph_editor_rejects_camera_sort_for_ribbon_output():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    ribbon = panel.add_authoring_node(
        "rendering", "particle.output.ribbon", 520.0, 430.0
    )

    assert ribbon["properties"]["sort"] == "none"
    with pytest.raises(ValueError, match="requires sort='none'"):
        panel.set_node_property(ribbon["uid"], "sort", "back_to_front")
    snapshot = panel.authoring_snapshot()
    saved = next(node for node in snapshot["nodes"] if node["uid"] == ribbon["uid"])
    assert saved["properties"]["sort"] == "none"


def test_particle_graph_editor_exposes_vector_components_and_dimension_policies():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    definitions = {
        item["type_id"]: item
        for item in panel.authoring_snapshot(include_registered_types=True)[
            "registered_types"
        ]
    }

    compose = definitions["common.vector.compose3"]
    assert [
        (port["id"], port["display_name"], port["direction"])
        for port in compose["ports"]
    ] == [
        ("x", "X", "input"),
        ("y", "Y", "input"),
        ("z", "Z", "input"),
        ("value", "", "output"),
    ]
    multiply = definitions["common.math.multiply"]
    assert [port["dimension_policy"] for port in multiply["ports"][:2]] == [
        "promote",
        "promote",
    ]
    noise = definitions["common.noise.value3d"]
    policies = {port["id"]: port["dimension_policy"] for port in noise["ports"]}
    assert policies == {"position": "fixed", "frequency": "exact", "seed": "exact", "value": "exact"}


def test_particle_graph_editor_authors_trigger_and_independent_event_flow():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.particle import ParticleGraphCompiler, ParticleKernelLowerer

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    assert not hasattr(panel, "add_event_route")
    assert not hasattr(panel, "add_event_output_node")
    assert not hasattr(panel, "add_event_payload_node")
    event_type = panel.add_event_type(
        "Impact",
        4,
        [{"name": "Weight", "type": TypeRef(ValueType.F32).to_dict(), "default": 1.25}],
    )
    event_id = event_type["stable_id"]
    field_id = event_type["fields"][0]["stable_id"]
    first_flow = panel.add_authoring_event_flow(event_id)
    second_flow = panel.add_authoring_event_flow(event_id)
    assert first_flow["created"] is True
    assert second_flow["created"] is True
    assert first_flow["flow_id"] != second_flow["flow_id"]

    trigger = panel.add_authoring_node(
        "update", PARTICLE_EVENT_TRIGGER_TYPE_ID, 260.0, 230.0
    )
    panel.set_node_property(trigger["uid"], "event", event_id)
    panel.connect_exec("update::root.update", trigger["uid"])
    event_stage = f"event.{first_flow['flow_id']}"
    size = panel.add_authoring_node(
        event_stage, "particle.attribute.size", 360.0, 0.0
    )
    root_uid = f"{event_stage}::root.event"
    panel.connect_exec(root_uid, size["uid"])
    panel.connect_value(
        root_uid,
        particle_event_payload_port_id(field_id),
        size["uid"],
        "value",
    )

    kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(panel.asset)
    )
    opcodes = [item.opcode for item in kernel.emitters[0].update.instructions]
    assert "event_enqueue" in opcodes
    assert "event_payload" in opcodes
    assert "event_begin" in opcodes
    assert "event_complete" in opcodes


def test_particle_graph_editor_dragged_event_root_uses_canvas_position():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    event_type = panel.add_event_type("Pulse", 8, [])
    event_id = event_type["stable_id"]

    created = panel.add_authoring_event_flow(event_id, 420.0, 760.0)

    assert created["event_id"] == event_id
    assert created["created"] is True
    flow_id = created["flow_id"]
    root = next(
        node
        for node in panel.authoring_snapshot()["nodes"]
        if node["uid"] == f"event.{flow_id}::root.event"
    )
    assert root["position"] == pytest.approx([420.0, 760.0])
    root_definition = panel._model.get_node_type(
        panel._model.find_node(root["uid"])
    )
    assert root_definition.visual_style == "context"
    assert root_definition.deletable is False
    strict_definition = panel._definition_for_node(
        panel._model.find_node(root["uid"])
    )
    assert all(port.direction.value == "output" for port in strict_definition.ports)
    assert [port.id for port in strict_definition.ports][0] == "out"
    duplicate = panel.add_authoring_event_flow(event_id, 20.0, 30.0)
    assert duplicate["created"] is True
    assert duplicate["flow_id"] != flow_id
    duplicate_root = next(
        node
        for node in panel.authoring_snapshot()["nodes"]
        if node["uid"] == f"event.{duplicate['flow_id']}::root.event"
    )
    assert duplicate_root["position"] == pytest.approx([20.0, 30.0])
    assert panel._selected_node_uid == duplicate_root["uid"]
    assert panel._view.selected_nodes == [duplicate_root["uid"]]


def test_particle_graph_defers_canvas_drop_target_until_after_floating_sources():
    import inspect

    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    source = inspect.getsource(ParticleGraphEditorPanel.on_render_content)
    graph_render = source.index("defer_canvas_drop_target=True")
    left_overlay = source.index("self._left_overlay", graph_render)
    right_overlay = source.index("self._right_overlay", left_overlay)
    drop_target = source.index("render_canvas_drop_target", right_overlay)

    assert graph_render < left_overlay < right_overlay < drop_target


def test_particle_event_canvas_drop_creates_an_independent_event_flow():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    event_type = panel.add_event_type("Pulse", 8, [])
    event_id = event_type["stable_id"]

    panel._on_canvas_drop("PARTICLE_EVENT", event_id, 420.0, 760.0)

    assert [flow.event_id for flow in panel.asset.emitters[0].event_flows] == [
        event_id
    ]
    flow_id = panel.asset.emitters[0].event_flows[0].stable_id
    root = next(
        node
        for node in panel.authoring_snapshot()["nodes"]
        if node["uid"] == f"event.{flow_id}::root.event"
    )
    assert root["position"] == pytest.approx([420.0, 760.0])


def test_particle_event_row_binds_drag_source_before_context_menu(monkeypatch):
    import Infernux.engine.ui.particle_graph_editor_panel as module

    panel = module.ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    event_type = panel.add_event_type("Pulse", 8, [])
    calls = []

    monkeypatch.setattr(module, "render_workspace_add_header", lambda *_a, **_k: None)
    monkeypatch.setattr(
        module,
        "begin_workspace_entry",
        lambda *_a, **_k: (False, (0.0, 0.0, 100.0, 28.0)),
    )
    monkeypatch.setattr(module, "paint_workspace_entry", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "finish_workspace_entry", lambda *_a, **_k: None)
    monkeypatch.setattr(panel, "_workspace_shortcut_pressed", lambda *_a: False)

    class Context:
        semantic_capture_enabled = False

        @staticmethod
        def begin_drag_drop_source():
            calls.append("drag")
            return True

        @staticmethod
        def set_drag_drop_payload_str(payload_type, payload):
            calls.append((payload_type, payload))

        @staticmethod
        def label(_value):
            pass

        @staticmethod
        def end_drag_drop_source():
            pass

        @staticmethod
        def begin_popup_context_item(_item_id):
            calls.append("context")
            return False

    panel._render_event_page(Context())

    assert calls == [
        "drag",
        ("PARTICLE_EVENT", event_type["stable_id"]),
        "context",
    ]


def test_particle_graph_editor_event_schema_edit_prunes_only_invalid_payload_links():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    event_type = panel.add_event_type(
        "Impact",
        8,
        [{"name": "Weight", "type": TypeRef(ValueType.F32).to_dict(), "default": 1.0}],
    )
    event_id = event_type["stable_id"]
    field_id = event_type["fields"][0]["stable_id"]
    flow = panel.add_authoring_event_flow(event_id)
    event_stage = f"event.{flow['flow_id']}"
    size = panel.add_authoring_node(event_stage, "particle.attribute.size", 360.0, 0.0)
    root_uid = f"{event_stage}::root.event"
    panel.connect_exec(root_uid, size["uid"])
    panel.connect_value(
        root_uid, particle_event_payload_port_id(field_id), size["uid"], "value"
    )

    updated = panel.update_event_type(
        event_id,
        "Impact Renamed",
        16,
        [{
            "stable_id": field_id,
            "name": "Impulse",
            "type": TypeRef(ValueType.F32).to_dict(),
            "default": 2.0,
        }],
    )
    assert updated["stable_id"] == event_id
    assert updated["queue_capacity"] == 16
    assert any(
        link.source_port == particle_event_payload_port_id(field_id)
        for link in panel.asset.emitters[0].event_flows[0].graph.links
    )

    panel.update_event_type(
        event_id,
        "Impact Renamed",
        16,
        [{
            "stable_id": field_id,
            "name": "Impulse",
            "type": TypeRef(ValueType.VEC3).to_dict(),
            "default": [0.0, 0.0, 0.0],
        }],
    )
    links = panel.asset.emitters[0].event_flows[0].graph.links
    assert any(link.kind.value == "exec" for link in links)
    assert not any(
        link.source_port == particle_event_payload_port_id(field_id) for link in links
    )


def test_particle_graph_editor_event_schema_edit_migrates_trigger_literals_atomically():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    event_type = panel.add_event_type(
        "Impact",
        8,
        [
            {
                "name": "Direction",
                "type": TypeRef(ValueType.VEC3).to_dict(),
                "default": [0.0, 1.0, 0.0],
            }
        ],
    )
    event_id = event_type["stable_id"]
    field_id = event_type["fields"][0]["stable_id"]
    panel.add_authoring_event_flow(event_id)
    trigger = panel.add_authoring_node(
        "update", PARTICLE_EVENT_TRIGGER_TYPE_ID, 260.0, 230.0
    )
    panel.set_node_property(trigger["uid"], "event", event_id)
    payload_port = particle_event_payload_port_id(field_id)
    trigger_node = panel._model.find_node(trigger["uid"])
    assert trigger_node.data[payload_port] == [0.0, 1.0, 0.0]

    panel.update_event_type(
        event_id,
        "Impact",
        8,
        [
            {
                "stable_id": field_id,
                "name": "Weight",
                "type": TypeRef(ValueType.F32).to_dict(),
                "default": 2.5,
            },
            {
                "stable_id": "new-color-field",
                "name": "Tint",
                "type": TypeRef(ValueType.COLOR).to_dict(),
                "default": [1.0, 0.25, 0.0, 1.0],
            },
        ],
    )

    node = next(
        item
        for item in panel.asset.emitters[0].update.nodes
        if item.type_id == PARTICLE_EVENT_TRIGGER_TYPE_ID
        and item.properties.get("event") == event_id
    )
    assert node.properties[payload_port] == 2.5
    assert node.properties[particle_event_payload_port_id("new-color-field")] == [
        1.0,
        0.25,
        0.0,
        1.0,
    ]
    canvas_node = panel._model.find_node(f"update::{node.uid}")
    canvas_type = panel._model.get_node_type(canvas_node)
    inline_types = {field.id: field.data_type for field in canvas_type.inline_fields}
    assert inline_types[payload_port] == "f32"
    assert inline_types[particle_event_payload_port_id("new-color-field")] == "color"


def test_particle_graph_editor_removing_event_clears_reusable_event_nodes():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    event_type = panel.add_event_type("Death", 4, [])
    event_id = event_type["stable_id"]
    panel.add_authoring_event_flow(event_id)
    trigger = panel.add_authoring_node(
        "update", PARTICLE_EVENT_TRIGGER_TYPE_ID, 260.0, 230.0
    )
    panel.set_node_property(trigger["uid"], "event", event_id)
    panel.connect_exec("update::root.update", trigger["uid"])

    removed = panel.remove_event_type(event_id)
    assert removed["stable_id"] == event_id
    assert panel.asset.event_types == ()
    assert len(panel.asset.emitters[0].event_flows) == 1
    assert panel.asset.emitters[0].event_flows[0].event_id == ""
    trigger_node = next(
        node
        for node in panel.asset.emitters[0].update.nodes
        if node.type_id == PARTICLE_EVENT_TRIGGER_TYPE_ID
    )
    assert trigger_node.properties["event"] == ""

    replacement = panel.add_event_type("Respawn", 8, [])
    replacement_id = replacement["stable_id"]
    flow = panel.asset.emitters[0].event_flows[0]
    trigger_uid = f"update::{trigger_node.uid}"
    active_uid = f"event.{flow.stable_id}::root.event"
    panel.set_node_property(trigger_uid, "event", replacement_id)
    panel.set_node_property(active_uid, "event", replacement_id)
    assert panel._model.get_node_type(panel._model.find_node(trigger_uid)).label == (
        "Trigger Event: Respawn"
    )
    assert panel._model.get_node_type(panel._model.find_node(active_uid)).label == (
        "Active Event: Respawn"
    )


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


def test_particle_node_numeric_semantics_are_bound_during_widget_submission():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    class Context:
        semantic_capture_enabled = True

        def __init__(self):
            self.semantic_id = ""

        @staticmethod
        def label(_label):
            pass

        @staticmethod
        def separator():
            pass

        def input_int_semantic(self, _label, _value, semantic_id):
            self.semantic_id = semantic_id
            return 5

        @staticmethod
        def record_semantic_item(*_args, **_kwargs):
            raise AssertionError("numeric semantic aliases must not be recorded after the widget")

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    panel._on_node_creation_requested({"source_node": "", "gy": 230.0})
    node = panel._on_node_add("particle.control.wait_frames", 400.0, 230.0)
    panel._selected_node_uid = node.uid
    ctx = Context()

    panel._render_node_properties(ctx)

    assert node.data["frames"] == 5
    assert ctx.semantic_id == f"particle_graph.node.{node.uid}.property.frames"


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

    ParticleArtifactRegistry.clear()
    monkeypatch.setattr(AssetManager, "reimport_asset", classmethod(lambda cls, _path: None))

    path = tmp_path / "Smoke.particlegraph"
    panel = ParticleGraphEditorPanel()
    assert panel._save_to(str(path)) is True
    artifact = ParticleArtifactRegistry.get(str(path))
    assert artifact is not None
    assert artifact.source_kind == "graph"
    assert artifact.hir["name"] == "Smoke"
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

    ParticleArtifactRegistry.clear()

    ok, error = create_particlegraph(str(tmp_path), "Fire")

    assert ok is True, error
    path = tmp_path / "Fire.particlegraph"
    graph = ParticleGraphAsset.load(str(path))
    assert graph.name == "Fire"
    assert len(graph.emitters) == 1
    emitter = graph.emitters[0]
    assert [node.type_id for node in emitter.init.nodes[1:]] == [
        "particle.attribute.lifetime",
        "particle.attribute.velocity",
    ]
    assert [node.type_id for node in emitter.update.nodes[1:]] == [
        "particle.attribute.velocity"
    ]
    assert emitter.update.nodes[1].properties["composition"] == "add"
    artifact = ParticleArtifactRegistry.get(str(path))
    assert artifact is not None
    assert artifact.source_kind == "graph"
    assert artifact.hir["name"] == "Fire"
    assert json.loads(path.read_text(encoding="utf-8"))["$schema"] == "infernux.particle_graph"


def test_particle_graph_document_state_does_not_serialize_stale_model(monkeypatch):
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._file_path = "Assets/VFX/Legacy.particlegraph"
    panel._dirty = False
    monkeypatch.setattr(
        panel,
        "_sync_model_to_asset",
        lambda: (_ for _ in ()).throw(AssertionError("must not serialize the graph")),
    )

    assert panel.authoring_document_state() == {
        "file_path": "Assets/VFX/Legacy.particlegraph",
        "dirty": False,
    }


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
    assert set(fields) == {
        "graph",
        "simulation_speed",
        "play_on_awake",
        "random_seed",
        "prewarm",
        "offscreen_policy",
        "bounds_mode",
        "manual_bounds_center",
        "manual_bounds_size",
    }
    assert fields["graph"].display_name_key == "particle_system.graph"
    assert fields["simulation_speed"].display_name_key == "particle_system.simulation_speed"
    assert fields["play_on_awake"].display_name_key == "particle_system.play_on_awake"
    assert fields["random_seed"].display_name_key == "particle_system.random_seed"
    assert fields["prewarm"].display_name_key == "particle_system.prewarm"
    assert (
        fields["offscreen_policy"].display_name_key
        == "particle_system.offscreen_policy"
    )
    assert fields["bounds_mode"].display_name_key == "particle_system.bounds_mode"
    assert (
        fields["manual_bounds_center"].display_name_key
        == "particle_system.manual_bounds_center"
    )
    assert (
        fields["manual_bounds_size"].display_name_key
        == "particle_system.manual_bounds_size"
    )
