from __future__ import annotations

import json
import os
import struct
from dataclasses import replace
from types import SimpleNamespace

import pytest

from Infernux.engine.i18n import t
from Infernux.engine.interaction import DocumentRegistry
from Infernux.engine.ui.graph_document_authoring import (
    GraphDocumentAuthoringModel,
    ParticleEmitterGraphAuthoringModel,
    particle_stage_definition_filter,
)
from Infernux.graph import GraphDocument, GraphNodeRecord
from Infernux.graph.types import CoordinateSpace, TypeRef, ValueType
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
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager.instance()
    UndoManager()
    DocumentRegistry.instance().close_view("particle_graph_editor")
    try:
        yield
    finally:
        DocumentRegistry.instance().close_view("particle_graph_editor")
        UndoManager._instance = previous_manager


def _stage_model(document):
    return GraphDocumentAuthoringModel(
        document,
        definition_filter=particle_stage_definition_filter(document.domain),
    )


def _finish_particle_graph_save(panel, path) -> None:
    from Infernux.core.document_store import DocumentStore

    DocumentStore.flush(str(path))
    panel._authoring_document_controller.poll_pending_writes()


def _save_particle_graph(panel, path) -> bool:
    registry = DocumentRegistry.instance()
    document = registry.require(panel.document_id)
    target = os.path.abspath(str(path))
    current = os.path.abspath(document.resource_path) if document.resource_path else ""
    ticket = registry.begin_save(
        document.document_id,
        save_as=not current or os.path.normcase(target) != os.path.normcase(current),
    )
    accepted = panel._save_to(str(path), ticket_id=ticket.ticket_id)
    if accepted:
        _finish_particle_graph_save(panel, path)
    return accepted


def test_particle_document_authoring_round_trip_keeps_strict_roots():
    document = ParticleGraphAsset().emitters[0].init
    model = _stage_model(document)


    assert model.remove_node("root.init") is False
    assert "particle.context.delta_time" in {
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


def test_particle_emitter_canvas_loads_vector_properties_named_x_y_z():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "vector",
                "common.vector.compose3",
                (120.0, 240.0),
                {"x": 1.0, "y": 2.0, "z": 3.0},
            ),
        ),
    )

    model = ParticleEmitterGraphAuthoringModel(
        ParticleEmitterAsset(update=update)
    )
    vector = model.find_node("update::vector")

    assert vector is not None
    assert (vector.pos_x, vector.pos_y) == (120.0, 470.0)
    assert vector.data == {"x": 1.0, "y": 2.0, "z": 3.0}


def test_particle_update_palette_exposes_explicit_delta_time_value():
    document = ParticleGraphAsset().emitters[0].update
    model = _stage_model(document)

    assert "particle.context.delta_time" in {
        definition.type_id for definition in model.registered_types()
    }


def test_target_position_is_a_lifecycle_operation_not_a_particle_attribute():
    asset = ParticleGraphAsset()
    emitter = asset.emitters[0]
    update_types = {
        definition.type_id for definition in _stage_model(emitter.update).registered_types()
    }
    init_types = {
        definition.type_id for definition in _stage_model(emitter.init).registered_types()
    }
    rendering_types = {
        definition.type_id
        for definition in _stage_model(emitter.rendering).registered_types()
    }

    assert "particle.motion.target_position" in update_types
    assert "particle.motion.target_position" in init_types
    assert "particle.motion.target_position" in rendering_types
    definition = particle_graph_node_definitions(asset).registry.get(
        "particle.motion.target_position"
    )
    assert definition is not None
    assert {port.id for port in definition.ports} == {
        "in",
        "out",
        "target",
        "speed",
        "responsiveness",
        "arrival_radius",
    }
    assert "target" not in {
        attribute.stable_id for attribute in emitter.attributes
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
        "particle.sdf.sample_distance",
        "particle.sdf.sample_gradient",
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
    assert model.attribute_id_for_cache_node(node.uid) == expected_id
    assert model.attribute_id_for_cache_node(source.uid) == ""
    assert expected_id in {value for _label, value in model.attribute_cache_entries()}
    assert model.node_creation_state("particle.attribute.cache") == (True, "")


def test_attribute_cache_type_change_rebuilds_dependent_get_nodes():
    model = ParticleEmitterGraphAuthoringModel(ParticleEmitterAsset())
    model.set_authoring_stage("update")
    model.prepare_node_creation("update")
    cache = model.add_node("particle.attribute.cache", 240.0, 250.0)
    scalar = model.add_node("common.constant.f32", 20.0, 200.0, value=1.0)
    vector = model.add_node(
        "common.constant.vec3", 20.0, 300.0, value=[1.0, 2.0, 3.0]
    )
    incoming = model.add_link(scalar.uid, "value", cache.uid, "value")
    cache_id = particle_attribute_cache_id(
        "update", model._document_uid(cache.uid)
    )
    dependent = model.add_node(
        "particle.attribute.get",
        480.0,
        250.0,
        attribute=cache_id,
    )

    scalar_output = next(
        pin for pin in model.get_node_type(dependent).output_pins() if pin.id == "value"
    )
    assert scalar_output.data_type == "f32"

    assert model.replace_link(
        incoming.uid,
        vector.uid,
        "value",
        cache.uid,
        "value",
    ) is not None

    vector_output = next(
        pin for pin in model.get_node_type(dependent).output_pins() if pin.id == "value"
    )
    assert cache.data["value_type"] == "vec3"
    assert vector_output.data_type == "vec3"


def test_unlinked_attribute_cache_uses_its_declared_vector_type_in_the_canvas():
    model = ParticleEmitterGraphAuthoringModel(ParticleEmitterAsset())
    model.set_authoring_stage("update")
    model.prepare_node_creation("update")

    node = model.add_node(
        "particle.attribute.cache",
        240.0,
        250.0,
        name="Heat",
        value_type="vec3",
        value_space="none",
        value=[1.0, 2.0, 3.0],
    )
    definition = model.get_node_type(node)

    value_pin = next(pin for pin in definition.input_pins() if pin.id == "value")
    value_field = next(field for field in definition.inline_fields if field.id == "value")
    assert value_pin.data_type == "vec3"
    assert value_field.data_type == "vec3"


@pytest.mark.parametrize("resource_type", (ValueType.TEXTURE2D, ValueType.MESH))
def test_attribute_cache_rejects_graph_resources(resource_type):
    default = (
        {"guid": "texture-guid", "path_hint": "Assets/Smoke.png"}
        if resource_type is ValueType.TEXTURE2D
        else {"guid": "mesh-guid", "path_hint": "Assets/Smoke.fbx"}
    )
    asset = ParticleGraphAsset(
        parameters=(
            ParticleParameter(
                "resource",
                "Resource",
                TypeRef(resource_type),
                default,
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
        "particle.parameter",
        20.0,
        250.0,
        parameter="resource",
    )

    validation = model.validate_link(parameter.uid, "value", cache.uid, "value")
    assert validation.is_valid is False
    assert validation.code == "unsupported_cache_type"
    assert model.add_link(parameter.uid, "value", cache.uid, "value") is None


def test_particle_registry_has_no_resource_attribute_nodes():
    definitions = particle_graph_node_definitions(ParticleGraphAsset())
    type_ids = {item.type_id for item in definitions.registry.definitions()}

    assert "particle.attribute.mesh" not in type_ids
    assert "particle.attribute.texture2d" not in type_ids
    assert all("set mesh" not in item.lower() for item in type_ids)
    assert all("get mesh" not in item.lower() for item in type_ids)


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
    assert "builtin.collision_collider_id_low" in attribute_field.enum_values
    assert "builtin.collision_collider_id_high" in attribute_field.enum_values
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
    node = model.add_node("particle.parameter", 200.0, 230.0)

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


def test_set_parameter_only_offers_writable_parameters_and_uses_typed_input():
    asset = ParticleGraphAsset(
        parameters=(
            ParticleParameter(
                "read-only",
                "Read Only",
                TypeRef(ValueType.F32),
                1.0,
            ),
            ParticleParameter(
                "shared-velocity",
                "Shared Velocity",
                TypeRef(ValueType.VEC3),
                [0.0, 0.0, 0.0],
                writable=True,
            ),
        )
    )
    model = ParticleEmitterGraphAuthoringModel(
        asset.emitters[0], definition_set=particle_graph_node_definitions(asset)
    )
    model.prepare_node_creation("update")
    node = model.add_node("particle.parameter.set", 200.0, 230.0)
    node_type = model.get_node_type(node)
    value_pin = next(pin for pin in node_type.pins if pin.id == "value")
    parameter_field = next(
        field for field in node_type.inline_fields if field.id == "parameter"
    )

    assert node.data["parameter"] == "shared-velocity"
    assert node_type.label == "Set Parameter: Shared Velocity"
    assert value_pin.data_type == "vec3"
    assert parameter_field.enum_values == ("shared-velocity",)


def test_set_parameter_color_inline_follows_hdr_attribute():
    from Infernux.graph.parameters import GRAPH_PARAMETER_HDR_ATTRIBUTE

    def value_field(attributes=()):
        asset = ParticleGraphAsset(
            parameters=(
                ParticleParameter(
                    "paper",
                    "Paper",
                    TypeRef(ValueType.COLOR),
                    [1.0, 1.0, 1.0, 1.0],
                    writable=True,
                    attributes=attributes,
                ),
            )
        )
        model = ParticleEmitterGraphAuthoringModel(
            asset.emitters[0],
            definition_set=particle_graph_node_definitions(asset),
        )
        model.prepare_node_creation("update")
        node = model.add_node("particle.parameter.set", 200.0, 230.0)
        return next(
            field
            for field in model.get_node_type(node).inline_fields
            if field.id == "value"
        )

    assert value_field().hdr is False
    assert value_field((GRAPH_PARAMETER_HDR_ATTRIBUTE,)).hdr is True


def test_color_parameter_hdr_toggle_writes_the_hdr_attribute(monkeypatch):
    from Infernux.engine.interaction import GraphElementKind, GraphElementRef
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.graph.parameters import GRAPH_PARAMETER_HDR_ATTRIBUTE

    captured = {}
    monkeypatch.setattr(
        "Infernux.engine.ui.node_graph_editor_panel.render_color_value_bar",
        lambda _ctx, _widget_id, value, **kwargs: captured.update(kwargs) or list(value),
    )
    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    parameter = panel.add_authoring_parameter(
        "Paper", "color", [0.97, 0.93, 0.84, 1.0]
    )
    panel._graph_selection.select(
        (GraphElementRef(GraphElementKind.PARAMETER, parameter["stable_id"]),),
        reason="test",
        record_history=False,
    )

    class Context:
        @staticmethod
        def label(*_args):
            pass

        @staticmethod
        def separator():
            pass

        @staticmethod
        def set_next_item_width(*_args):
            pass

        @staticmethod
        def text_input(_label, value, _length):
            return value

        @staticmethod
        def combo(_label, index, *_args):
            return index

        @staticmethod
        def checkbox(label, value):
            return True if "graph_parameter_hdr" in label else value

        @staticmethod
        def begin_disabled(_disabled=True):
            pass

        @staticmethod
        def end_disabled():
            pass

    assert panel._render_node_graph_parameter_detail(Context())
    stored = next(
        item
        for item in panel.asset.parameters
        if item.stable_id == parameter["stable_id"]
    )
    encoded = stored.to_dict()
    assert stored.attributes == (GRAPH_PARAMETER_HDR_ATTRIBUTE,)
    assert "hdr" not in encoded
    assert encoded["attributes"] == ["hdr"]
    assert captured["allow_hdr"] is True
    assert captured["default_hdr_enabled"] is True


def test_set_emitter_playing_dropdown_excludes_the_owning_emitter():
    asset = ParticleGraphAsset(
        emitters=(
            ParticleEmitterAsset(stable_id="meteor", name="Meteor"),
            ParticleEmitterAsset(stable_id="trail", name="Trail"),
            ParticleEmitterAsset(stable_id="impact", name="Impact"),
        )
    )
    model = ParticleEmitterGraphAuthoringModel(
        asset.emitters[0], definition_set=particle_graph_node_definitions(asset)
    )
    catalog_type = next(
        item
        for item in model.registered_types()
        if item.type_id == "particle.emitter.playing"
    )
    catalog_field = next(
        field for field in catalog_type.inline_fields if field.id == "emitter"
    )
    model.prepare_node_creation("update")
    node = model.add_node("particle.emitter.playing", 200.0, 230.0)
    node_type = model.get_node_type(node)
    emitter_field = next(
        field for field in node_type.inline_fields if field.id == "emitter"
    )

    assert catalog_field.enum_values == ("trail", "impact")
    assert node.data["emitter"] == "trail"
    assert node_type.label == "Set Emitter Playing: Trail"
    assert emitter_field.enum_values == ("trail", "impact")

    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._asset = asset
    panel._emitter_index = 0
    panel._bind_stage()
    catalog = panel.authoring_type_catalog(query="playing")
    catalog_choices = next(
        item for item in catalog["types"]
        if item["type_id"] == "particle.emitter.playing"
    )["properties"][0]["choices"]
    assert [item["value"] for item in catalog_choices] == ["trail", "impact"]


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


def test_transform_position_connects_to_set_position_without_matching_target_space():
    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    model.prepare_node_creation("update")
    transform = model.add_node("common.space.transform_position", 240.0, 180.0)
    setter = model.add_node("particle.attribute.position", 480.0, 180.0)
    constant = model.add_node("common.constant.vec3", 40.0, 180.0)

    assert transform.data.get("target_space", "world") == "world"
    assert model.validate_link(constant.uid, "value", transform.uid, "input")
    assert model.validate_link(transform.uid, "value", setter.uid, "value")
    assert model.add_link(constant.uid, "value", transform.uid, "input") is not None
    assert model.add_link(transform.uid, "value", setter.uid, "value") is not None


def test_numeric_ports_accept_convertible_shapes_across_common_and_set_nodes():
    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    model.prepare_node_creation("update")
    scalar = model.add_node("common.constant.f32", 40.0, 180.0)
    integer = model.add_node("common.constant.i32", 40.0, 260.0)
    vector = model.add_node("common.constant.vec3", 40.0, 340.0)
    position = model.add_node("particle.attribute.position", 480.0, 180.0)
    size = model.add_node("particle.attribute.size", 480.0, 260.0)
    compare = model.add_node("common.compare.less_than", 480.0, 340.0)

    assert model.validate_link(scalar.uid, "value", position.uid, "value")
    assert model.validate_link(vector.uid, "value", size.uid, "value")
    assert model.validate_link(integer.uid, "value", size.uid, "value")
    assert model.validate_link(integer.uid, "value", compare.uid, "a")
    assert model.validate_link(vector.uid, "value", compare.uid, "b")
    assert model.add_link(scalar.uid, "value", position.uid, "value") is not None
    assert model.add_link(vector.uid, "value", size.uid, "value") is not None
    assert model.add_link(integer.uid, "value", compare.uid, "a") is not None


def test_transform_position_palette_can_create_set_position_from_its_output():
    from Infernux.core.node_graph import PinKind

    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    model.prepare_node_creation("update")
    transform = model.add_node("common.space.transform_position", 240.0, 180.0)
    pin = model.compatible_creation_pin(
        "particle.attribute.position",
        {
            "source_node": transform.uid,
            "source_pin": "value",
            "source_kind": PinKind.OUTPUT,
        },
    )

    assert pin is not None
    assert pin.id == "value"


def test_particle_common_node_creation_keeps_the_requested_stage():
    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    model.prepare_node_creation("update")

    noise = model.add_node("common.noise.vector3d", 200.0, 460.0)

    assert noise.uid.startswith("update::")
    assert model.authoring_stage == "update"


def test_particle_common_operator_chain_follows_the_lifecycle_node_it_feeds():
    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    model.prepare_node_creation("init")
    random_value = model.add_node("common.random.f32", 360.0, 80.0)
    model.prepare_node_creation("update")
    vector = model.add_node("common.vector.compose3", 560.0, 260.0)
    original_vector_uid = vector.uid
    original_vector_position = (vector.pos_x, vector.pos_y)
    before_connection = model.capture_authoring_state()

    validation = model.validate_link(
        random_value.uid, "value", vector.uid, "x"
    )
    assert validation
    assert model.add_link(random_value.uid, "value", vector.uid, "x") is not None

    migrated_vector_uid = model._canvas_uid(
        "init", model._document_uid(original_vector_uid)
    )
    assert model.find_node(original_vector_uid) is None
    migrated_vector = model.find_node(migrated_vector_uid)
    assert migrated_vector is not None
    assert (migrated_vector.pos_x, migrated_vector.pos_y) == original_vector_position
    assert model.add_link(
        migrated_vector_uid,
        "value",
        "init::init.velocity",
        "value",
    ) is not None

    documents = model.to_documents()
    assert any(
        node.type_id == "common.random.f32" for node in documents["init"].nodes
    )
    assert any(
        node.type_id == "common.vector.compose3" for node in documents["init"].nodes
    )
    assert not any(
        node.type_id == "common.vector.compose3" for node in documents["update"].nodes
    )

    after_connection = model.capture_authoring_state()
    mutations = model.diff_authoring_states(before_connection, after_connection)
    model.apply_authoring_mutations(model.invert_authoring_mutations(mutations))
    assert model.capture_authoring_state() == before_connection
    model.apply_authoring_mutations(mutations)
    assert model.capture_authoring_state() == after_connection


def test_particle_port_drag_rehomes_and_connects_a_common_operator():
    from Infernux.engine.ui.node_graph_view import NodeGraphView, PinKind

    model = ParticleEmitterGraphAuthoringModel(ParticleGraphAsset().emitters[0])
    model.prepare_node_creation("init")
    random_value = model.add_node("common.random.f32", 360.0, 80.0)
    model.prepare_node_creation("update")
    vector = model.add_node("common.vector.compose3", 560.0, 260.0)

    view = NodeGraphView()
    view.bind_graph(model, preserve_selection=False)
    view._origin_x = 0.0
    view._origin_y = 0.0
    view._compute_layouts()
    vector_layout = view.get_layout(vector.uid)
    target_pin = next(
        pin for pin in vector_layout.input_pins if pin.pin_def.id == "x"
    )
    view._dragging_pin = True
    view._drag_src_node = random_value.uid
    view._drag_src_pin = "value"
    view._drag_src_kind = PinKind.OUTPUT
    view.on_link_created = lambda *endpoints: model.add_link(*endpoints)

    view._try_complete_link(target_pin.cx, target_pin.cy)

    assert len(
        [
            link
            for link in model.links
            if link.source_pin == "value" and link.target_pin == "x"
        ]
    ) == 1
    assert all(
        model.stage_for_uid(link.source_node)
        == model.stage_for_uid(link.target_node)
        for link in model.links
    )


def test_particle_link_topology_edits_never_move_authoring_nodes():
    from dataclasses import replace

    emitter = ParticleGraphAsset().emitters[0]
    model = ParticleEmitterGraphAuthoringModel(emitter)
    model.prepare_node_creation("init")
    random_value = model.add_node("common.random.f32", 371.0, 117.0)
    model.prepare_node_creation("update")
    vector = model.add_node("common.vector.compose3", 619.0, 287.0)

    def positions_by_raw_id(graph):
        return {
            graph._document_uid(node.uid): (node.pos_x, node.pos_y)
            for node in graph.nodes
        }

    original = positions_by_raw_id(model)
    first = model.add_link(random_value.uid, "value", vector.uid, "x")
    assert first is not None
    assert positions_by_raw_id(model) == original

    vector_uid = model._canvas_uid("init", model._document_uid(vector.uid))
    second = model.add_link(
        vector_uid,
        "value",
        "init::init.velocity",
        "value",
    )
    assert second is not None
    assert positions_by_raw_id(model) == original

    replaced = model.replace_link(
        first.uid,
        random_value.uid,
        "value",
        vector_uid,
        "y",
    )
    assert replaced is not None
    assert positions_by_raw_id(model) == original

    assert model.remove_link(second.uid)
    assert positions_by_raw_id(model) == original

    # Once the pure island is detached it may be consumed by another
    # lifecycle. Re-inference changes only compiler ownership, never layout.
    third = model.add_link(
        vector_uid,
        "value",
        "update::update.velocity",
        "value",
    )
    assert third is not None
    assert positions_by_raw_id(model) == original

    documents = model.to_documents()
    reopened_emitter = replace(
        emitter,
        init=documents["init"],
        update=documents["update"],
        collision_enter=documents["collision_enter"],
        collision_stay=documents["collision_stay"],
        collision_exit=documents["collision_exit"],
        rendering=documents["rendering"],
    )
    reopened = ParticleEmitterGraphAuthoringModel(reopened_emitter)
    assert positions_by_raw_id(reopened) == original


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
    node = panel.add_authoring_node("update", "particle.parameter", 200.0, 230.0)
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
        record.type_id == "particle.parameter"
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
        if node.type_id == "particle.parameter"
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
                "writable": False,
                "category": "",
            "tooltip": "",
            "attributes": [],
        }
    ]
    canvas_node = next(
        node
        for node in panel._model.nodes
        if node.type_id == "particle.parameter"
        and node.data["parameter"] == parameter["stable_id"]
    )
    assert panel._model.get_node_type(canvas_node).label == "Wind"


def test_particle_parameter_canvas_drop_ignores_inactive_collision_lanes():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    parameter = panel.add_authoring_parameter(
        "Paper Color", "color", [0.97, 0.93, 0.84, 1.0]
    )

    panel._on_canvas_drop(
        "PARTICLE_PARAMETER", parameter["stable_id"], 320.0, 460.0
    )

    nodes = [
        node
        for node in panel.asset.emitters[0].update.nodes
        if node.type_id == "particle.parameter"
    ]
    assert len(nodes) == 1
    assert nodes[0].properties["parameter"] == parameter["stable_id"]
    assert not any(
        node.type_id == "particle.parameter"
        for stage in ("collision_enter", "collision_stay", "collision_exit")
        for document in (getattr(panel.asset.emitters[0], stage),)
        if document is not None
        for node in document.nodes
    )


def test_particle_vector_parameter_adopts_fixed_attribute_space_and_compiles():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.particle import ParticleGraphCompiler, ParticleKernelLowerer

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    parameter = panel.add_authoring_parameter("Wind", "vec3", [0.0, 1.0, 0.0])
    source = panel.add_authoring_parameter_node(
        parameter["stable_id"], 180.0, 360.0, stage="update"
    )
    velocity_uid = "update::update.velocity"

    link = panel.connect_value(source["uid"], "value", velocity_uid, "value")
    program = ParticleGraphCompiler().compile(panel.asset)

    assert link["changed"] is True
    converted = next(
        item
        for item in ParticleKernelLowerer().lower(program).emitters[0].update.instructions
        if item.opcode == "convert_space"
    )
    assert converted.result_type == TypeRef(
        ValueType.VEC3, CoordinateSpace.SIMULATION
    )


def test_world_position_parameter_requires_and_preserves_explicit_space_transform():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.particle import (
        GpuParticleGlslLowerer,
        ParticleGraphCompiler,
        ParticleKernelLowerer,
    )

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    parameter = panel.add_authoring_parameter(
        "Shared Position",
        "vec3",
        [0.0, 0.0, 0.0],
        space="world",
    )
    panel.update_authoring_parameter(parameter["stable_id"], {"writable": True})
    source = panel.add_authoring_node("update", "particle.attribute.get", 160.0, 360.0)
    panel._model.find_node(source["uid"]).data["attribute"] = "builtin.position"
    transform = panel.add_authoring_node(
        "update", "common.space.transform_position", 360.0, 360.0
    )
    store = panel.add_authoring_node("update", "particle.parameter.set", 560.0, 360.0)

    panel.connect_value(source["uid"], "value", transform["uid"], "input")
    panel.connect_value(transform["uid"], "value", store["uid"], "value")
    panel.connect_exec("update::update.velocity", store["uid"])
    panel._sync_model_to_asset()

    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(panel.asset))
    conversion = next(
        instruction
        for instruction in kernel.emitters[0].update.instructions
        if instruction.opcode == "convert_space"
        and instruction.immediate_dict().get("semantic") == "position"
    )
    assert conversion.result_type == TypeRef(ValueType.VEC3, CoordinateSpace.WORLD)
    assert conversion.immediate_dict() == {
        "from": "simulation",
        "to": "world",
        "semantic": "position",
    }
    update_glsl = GpuParticleGlslLowerer().lower(kernel).emitters[0].update
    assert "simulation_to_world * vec4(" in update_glsl
    assert ", 1.0)).xyz" in update_glsl


def test_particle_parameters_reject_emitter_relative_coordinate_spaces():
    ParticleParameter(
        "world-position",
        "World Position",
        TypeRef(ValueType.VEC3, CoordinateSpace.WORLD),
        [0.0, 0.0, 0.0],
    )

    with pytest.raises(ValueError, match="world-space vec3"):
        ParticleParameter(
            "simulation-position",
            "Simulation Position",
            TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
            [0.0, 0.0, 0.0],
        )


def test_world_direction_transform_uses_vector_semantics_without_translation():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.particle import (
        GpuParticleGlslLowerer,
        ParticleGraphCompiler,
        ParticleKernelLowerer,
    )

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    parameter = panel.add_authoring_parameter(
        "Shared Direction", "vec3", [0.0, 0.0, 1.0], space="world"
    )
    panel.update_authoring_parameter(parameter["stable_id"], {"writable": True})
    source = panel.add_authoring_node("update", "particle.attribute.get", 160.0, 360.0)
    panel._model.find_node(source["uid"]).data["attribute"] = "builtin.velocity"
    transform = panel.add_authoring_node(
        "update", "common.space.transform_direction", 360.0, 360.0
    )
    store = panel.add_authoring_node("update", "particle.parameter.set", 560.0, 360.0)
    panel.connect_value(source["uid"], "value", transform["uid"], "input")
    panel.connect_value(transform["uid"], "value", store["uid"], "value")
    panel.connect_exec("update::update.velocity", store["uid"])
    panel._sync_model_to_asset()

    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(panel.asset))
    conversion = next(
        instruction
        for instruction in kernel.emitters[0].update.instructions
        if instruction.opcode == "convert_space"
        and instruction.immediate_dict().get("semantic") == "direction"
    )
    assert conversion.result_type == TypeRef(ValueType.VEC3, CoordinateSpace.WORLD)
    update_glsl = GpuParticleGlslLowerer().lower(kernel).emitters[0].update
    assert "normalize(transforms.simulation_to_world[0].xyz)" in update_glsl


def test_direction_space_conversion_does_not_apply_emitter_scale():
    from Infernux.particle.gpu_glsl_backend import _space_conversion

    glsl = _space_conversion(
        "local_velocity",
        TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
        {
            "from": CoordinateSpace.EMITTER_LOCAL.value,
            "to": CoordinateSpace.SIMULATION.value,
            "semantic": "direction",
        },
    )
    assert "transforms.world_to_simulation * transforms.emitter_to_world" in glsl
    assert "vec4(" not in glsl


def test_position_and_vector_space_conversion_keep_affine_semantics():
    from Infernux.particle.gpu_glsl_backend import _space_conversion

    position = _space_conversion(
        "local_point",
        TypeRef(ValueType.VEC3, CoordinateSpace.WORLD),
        {
            "from": CoordinateSpace.EMITTER_LOCAL.value,
            "to": CoordinateSpace.WORLD.value,
            "semantic": "position",
        },
    )
    vector = _space_conversion(
        "local_offset",
        TypeRef(ValueType.VEC3, CoordinateSpace.WORLD),
        {
            "from": CoordinateSpace.EMITTER_LOCAL.value,
            "to": CoordinateSpace.WORLD.value,
            "semantic": "vector",
        },
    )
    assert "transforms.emitter_to_world * vec4(local_point, 1.0)" in position
    assert "transforms.emitter_to_world * vec4(local_offset, 0.0)" in vector


def test_particle_compose4_connects_directly_to_color_attribute_and_compiles():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.particle import ParticleGraphCompiler

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    compose = panel.add_authoring_node("update", "common.vector.compose4", 180.0, 360.0)
    color = panel.add_authoring_node("update", "particle.attribute.color", 480.0, 360.0)

    link = panel.connect_value(compose["uid"], "value", color["uid"], "value")

    assert link["changed"] is True
    ParticleGraphCompiler().compile(panel.asset)


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
    port = panel._definition_for_type("particle.parameter").port("value")
    assert panel._model._effective_port_type(canvas_node, port) == TypeRef(
        ValueType.TEXTURE2D
    )


def test_unsaved_particle_graph_edits_do_not_publish_runtime_artifacts(
    monkeypatch, tmp_path
):
    import Infernux.engine.ui.particle_graph_editor_panel as module

    panel = module.ParticleGraphEditorPanel()
    panel._file_path = str(tmp_path / "Draft.particlegraph")
    published = []
    monkeypatch.setattr(
        module.ParticleArtifactRegistry,
        "save_graph_asset",
        lambda *_args, **_kwargs: published.append(True),
    )

    panel.add_authoring_parameter("Draft Value", "f32", 1.0)

    assert panel._document_is_dirty() is True
    assert published == []


def _particle_panel_with_command_core():
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.ui.particle_graph_editor_panel import (
        ParticleGraphEditorPanel,
    )
    from Infernux.engine.undo import UndoManager

    core = EditorInteractionCore()
    manager = UndoManager(core.action_journal)
    panel = ParticleGraphEditorPanel()
    core.panels.register_type(panel.panel_type_id, panel.PANEL_INTERACTION)
    core.panels.bind_view(panel.window_id, panel.panel_type_id, panel)
    panel._graph_selection.bind(core.selection)
    core.focus.activate_panel(
        panel.panel_type_id,
        view_id=panel.window_id,
        document_id=panel.document_id,
        child_context_id=panel.current_child_context_id(),
        record_history=False,
    )
    return panel, core, manager


def test_particle_emitter_row_defers_model_rebind_until_list_render_finishes(
    monkeypatch,
):
    import Infernux.engine.ui.particle_graph_editor_panel as module
    import Infernux.engine.ui.node_graph_editor_panel as shared_module

    panel, _core, _manager = _particle_panel_with_command_core()
    panel._asset = ParticleGraphAsset(
        emitters=(
            ParticleEmitterAsset(stable_id="first", name="First"),
            ParticleEmitterAsset(stable_id="second", name="Second"),
        )
    )
    panel._bind_stage()
    rendered_rows = []
    selected = []
    original_activate = panel._node_graph_workspace_activate

    def _begin(_ctx, entry_id, _selected):
        rendered_rows.append(entry_id)
        return entry_id.endswith("second"), (0.0, 0.0, 100.0, 28.0)

    def _activate(element):
        assert rendered_rows == [
            "graph_workspace_particle_emitter_emitter_first",
            "graph_workspace_particle_emitter_emitter_second",
        ]
        selected.append(element.stable_id)
        return original_activate(element)

    monkeypatch.setattr(
        shared_module, "render_workspace_add_header", lambda *_a, **_k: None
    )
    monkeypatch.setattr(shared_module, "begin_workspace_entry", _begin)
    monkeypatch.setattr(
        shared_module, "paint_workspace_entry", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        shared_module, "finish_workspace_entry", lambda *_a, **_k: None
    )
    monkeypatch.setattr(panel, "_node_graph_workspace_activate", _activate)
    ctx = SimpleNamespace(
        semantic_capture_enabled=False,
        begin_popup_context_item=lambda _item_id: False,
    )

    panel._render_emitter_page(ctx)

    assert selected == ["second"]
    assert panel._emitter_index == 1
    assert panel._selected_emitter().stable_id == "second"


def test_particle_workspace_defers_add_until_all_rows_finish(monkeypatch):
    import Infernux.engine.ui.node_graph_editor_panel as shared_module
    from Infernux.engine.ui.particle_graph_editor_panel import (
        ParticleGraphEditorPanel,
    )

    panel, core, _manager = _particle_panel_with_command_core()
    rendered_rows = []
    dispatched = []

    def _header(_ctx, _title, _section_id, **kwargs):
        kwargs["on_add"]()

    def _begin(_ctx, entry_id, _selected):
        rendered_rows.append(entry_id)
        return False, (0.0, 0.0, 100.0, 28.0)

    def _add(section_id, action_id):
        assert rendered_rows == [
            "graph_workspace_particle_emitter_emitter_"
            + panel.asset.emitters[0].stable_id
        ]
        dispatched.append((section_id, action_id))
        return True

    monkeypatch.setattr(shared_module, "render_workspace_add_header", _header)
    monkeypatch.setattr(shared_module, "begin_workspace_entry", _begin)
    monkeypatch.setattr(
        shared_module, "paint_workspace_entry", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        shared_module, "finish_workspace_entry", lambda *_a, **_k: None
    )
    monkeypatch.setattr(panel, "_node_graph_workspace_add", _add)
    ctx = SimpleNamespace(begin_popup_context_item=lambda _item_id: False)

    panel._render_emitter_page(ctx)

    assert dispatched == [("particle_emitter", "default")]
    assert core.commands.can_execute(
        "graph.workspace.add",
        core.commands.context(
            payload={
                "section_id": "particle_emitter",
                "action_id": "default",
            }
        ),
    )
    assert not core.commands.can_execute(
        "graph.workspace.add",
        core.commands.context(
            payload={
                "section_id": "particle_emitter",
                "action_id": "missing",
            }
        ),
    )


def test_particle_parameter_workspace_rename_uses_the_existing_undoable_api():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    parameter = panel.add_authoring_parameter("Intensity", "f32", 1.0)
    sections = []
    panel._render_graph_workspace_section = (
        lambda _ctx, section: sections.append(section)
    )

    panel._render_parameter_page(SimpleNamespace())
    entry = sections[0].entries[0]

    assert entry.element.stable_id == parameter["stable_id"]
    assert (
        panel._node_graph_workspace_rename(
            entry.element, "Emission Strength"
        )
        is not False
    )
    assert panel.asset.parameters[0].name == "Emission Strength"


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


def test_particle_graph_editor_restores_single_canvas_dirty_document_session():
    from Infernux.engine.interaction import DocumentRegistry
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    velocity = panel._on_node_add("particle.attribute.velocity", 220.0, 0.0)
    panel._on_link_created("init::init.velocity", "out", velocity.uid, "in")
    panel._select_stage("rendering")
    view_state = panel.save_state()
    session_state = DocumentRegistry.instance().capture_session_state()

    restored_registry = DocumentRegistry()
    assert restored_registry.queue_session_restore(session_state) == 1
    restored = ParticleGraphEditorPanel()
    assert restored.restore_persisted_session_document()
    restored.load_state(view_state)

    assert restored._document_is_dirty() is True
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


def test_particle_graph_editor_discards_incompatible_document_session(tmp_path):
    from Infernux.engine.interaction import DocumentRegistry
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.particle import ParticleGraphAsset

    target = tmp_path / "Current.particlegraph"
    ParticleGraphAsset(stable_id="current-graph", name="Current").save(str(target))
    panel = ParticleGraphEditorPanel()
    panel.open_document_resource_immediate(str(target))
    session_state = DocumentRegistry.instance().capture_session_state()
    stale_asset = session_state["documents"][0]["restore_state"]["asset"]
    stale_asset.pop("event_types")

    restored_registry = DocumentRegistry()
    assert restored_registry.queue_session_restore(session_state) == 1
    restored = ParticleGraphEditorPanel()
    assert restored.restore_persisted_session_document()

    assert restored._document_is_dirty() is False
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


def test_particle_graph_save_publishes_view_state_without_global_session_flush(tmp_path, monkeypatch):
    from Infernux.engine.ui import panel_state
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    layout = tmp_path / "layout"
    panel_state.init(str(layout))
    sentinel_session = {"documents": [{"document_id": "bootstrap-owned"}]}
    panel_state.put("document_session", sentinel_session)
    target = tmp_path / "Sparks.particlegraph"
    panel = ParticleGraphEditorPanel()
    panel._file_path = str(target)
    panel._persist_panel_state()
    view_state = panel_state.get("panel:particle_graph_editor")
    assert "dirty" not in view_state
    assert "draft" not in view_state
    assert panel_state.get("document_session") == sentinel_session

    def save_asset(asset, path):
        target.write_text(json.dumps(asset.to_dict()), encoding="utf-8")

    monkeypatch.setattr(ParticleGraphAsset, "save", save_asset)

    assert _save_particle_graph(panel, target) is True
    persisted_view = panel_state.get("panel:particle_graph_editor")
    assert "dirty" not in persisted_view
    assert "draft" not in persisted_view
    assert panel_state.get("document_session") == sentinel_session


def test_particle_graph_save_surfaces_compile_errors(tmp_path, monkeypatch):
    import Infernux.engine.ui.particle_graph_editor_panel as module
    from Infernux.particle.artifact import ParticleArtifactError

    panel = module.ParticleGraphEditorPanel()
    target = tmp_path / "Broken.particlegraph"

    def boom(*_args, **_kwargs):
        raise ParticleArtifactError(
            "particle graph AOT compile failed: kernel space conversion metadata does not match its types"
        )

    monkeypatch.setattr(module.ParticleArtifactRegistry, "prepare_graph_asset", boom)
    with pytest.raises(ParticleArtifactError, match="kernel space conversion"):
        panel.capture_authoring_save_snapshot(str(target))
    assert "kernel space conversion" in panel._draft_compile_error


def test_particle_graph_save_compiles_untyped_transform_direction(tmp_path):
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    compose = panel.add_authoring_node("init", "common.vector.compose3", 180.0, 360.0)
    transform = panel.add_authoring_node(
        "init", "common.space.transform_direction", 360.0, 360.0
    )
    panel.set_node_property(transform["uid"], "target_space", "simulation")
    panel.connect_value(compose["uid"], "value", transform["uid"], "input")
    panel.connect_value(transform["uid"], "value", "init::init.velocity", "value")

    snapshot = panel.capture_authoring_save_snapshot(str(tmp_path / "Aim.particlegraph"))
    assert snapshot.source_text
    assert panel._draft_compile_error == ""


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
    assert _node_property_is_visible(node, "shader") is True


def test_particle_output_exposes_selected_shader_properties_as_node_inputs():
    from Infernux.engine.ui.particle_graph_editor_panel import (
        ParticleGraphEditorPanel,
    )

    panel = ParticleGraphEditorPanel()
    output = panel._model.find_node("rendering::output.sprite")
    definition = panel._definition_for_node(output)

    assert definition is not None
    assert {item.id for item in definition.properties} >= {"shader"}
    assert "material" not in {item.id for item in definition.properties}
    shader_ports = {
        item.id: item.value_type.value_type
        for item in definition.ports
        if item.id.startswith("shader.") and item.value_type is not None
    }
    assert shader_ports == {
        "shader.baseColor": ValueType.COLOR,
        "shader.texSampler": ValueType.TEXTURE2D,
    }


@pytest.mark.parametrize(
    "type_id",
    (
        "particle.output.sprite",
        "particle.output.mesh",
        "particle.output.ribbon",
    ),
)
def test_every_particle_output_uses_the_same_shader_property_contract(type_id):
    from Infernux.engine.ui.particle_graph_editor_panel import (
        ParticleGraphEditorPanel,
    )

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    node = (
        panel._model.find_node("rendering::output.sprite")
        if type_id == "particle.output.sprite"
        else panel._on_node_add(type_id, 540.0, 460.0)
    )
    definition = panel._definition_for_node(node)

    assert definition is not None
    assert "shader" in {item.id for item in definition.properties}
    assert "material" not in {item.id for item in definition.properties}
    assert {
        item.id
        for item in definition.ports
        if item.id.startswith("shader.")
    } == {
        "shader.baseColor",
        "shader.texSampler",
    }

    if type_id == "particle.output.mesh":
        mesh_port = definition.port("mesh")
        assert mesh_port is not None
        assert mesh_port.value_type == TypeRef(ValueType.MESH)
        assert mesh_port.required is False
        assert "mesh" not in {item.id for item in definition.properties}


def test_particle_output_texture_port_uses_live_asset_reference_path(
    tmp_path, monkeypatch
):
    from Infernux.engine.ui import particle_graph_editor_panel as module

    texture_path = tmp_path / "Smoke.png"
    texture_path.write_bytes(b"png")
    monkeypatch.setattr(module, "_asset_guid_from_path", lambda _path: "smoke-guid")
    monkeypatch.setattr(
        module,
        "_portable_asset_path_hint",
        lambda _path: "Assets/VFX/Smoke.png",
    )

    panel = module.ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    output = panel._model.find_node("rendering::output.sprite")
    reference = panel.set_node_asset_reference(
        output.uid, "shader.texSampler", str(texture_path)
    )

    assert reference == {
        "guid": "smoke-guid",
        "path_hint": "Assets/VFX/Smoke.png",
    }
    assert panel._model.find_node(output.uid).data["shader.texSampler"] == reference


def test_particle_inline_texture_field_uses_structured_picker_and_clear_contract(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from Infernux.engine.ui import igui
    from Infernux.engine.ui import node_graph_view as graph_view
    from Infernux.engine.ui import particle_graph_editor_panel as module

    texture_path = tmp_path / "Smoke.png"
    texture_path.write_bytes(b"png")
    monkeypatch.setattr(module, "_asset_guid_from_path", lambda _path: "smoke-guid")
    monkeypatch.setattr(
        module,
        "_portable_asset_path_hint",
        lambda _path: "Assets/VFX/Smoke.png",
    )

    panel, manager = _particle_panel_with_history()
    output = panel._model.find_node("rendering::output.sprite")
    layout = graph_view._NodeLayout(
        node=output,
        typedef=panel._definition_for_node(output),
    )
    field = SimpleNamespace(id="shader.texSampler", label="Texture")
    calls = []

    def choose(_ctx, _field_id, display, _type_hint, **kwargs):
        calls.append((display, kwargs))
        kwargs["on_assign"](
            {
                "asset_type": "Texture",
                "builtin": "",
                "guid": "smoke-guid",
                "path_hint": str(texture_path),
            }
        )
        return False

    monkeypatch.setattr(igui.IGUI, "asset_reference_field", choose)
    manager.clear()
    panel._view._render_inline_asset_reference(
        object(), layout, field, "Texture", None, 180.0
    )

    assert panel._model.find_node(output.uid).data["shader.texSampler"] == {
        "guid": "smoke-guid",
        "path_hint": "Assets/VFX/Smoke.png",
    }
    assert calls[-1][0] == t("igui.none")
    assert calls[-1][1]["has_value"] is False
    assert manager.can_undo

    def clear(_ctx, _field_id, display, _type_hint, **kwargs):
        calls.append((display, kwargs))
        kwargs["on_clear"]()
        return False

    monkeypatch.setattr(igui.IGUI, "asset_reference_field", clear)
    panel._view._render_inline_asset_reference(
        object(),
        layout,
        field,
        "Texture",
        panel._model.find_node(output.uid).data["shader.texSampler"],
        180.0,
    )

    assert panel._model.find_node(output.uid).data["shader.texSampler"] == {
        "guid": "",
        "path_hint": "",
    }
    assert calls[-1][0] == "Smoke.png"
    assert calls[-1][1]["has_value"] is True


def test_particle_node_inspector_texture_field_uses_live_asset_reference_path(
    tmp_path, monkeypatch
):
    from Infernux.engine.ui import particle_graph_editor_panel as module

    texture_path = tmp_path / "Smoke.png"
    texture_path.write_bytes(b"png")
    monkeypatch.setattr(module, "_asset_guid_from_path", lambda _path: "smoke-guid")
    monkeypatch.setattr(
        module,
        "_portable_asset_path_hint",
        lambda _path: "Assets/VFX/Smoke.png",
    )

    class Context:
        semantic_capture_enabled = False

        @staticmethod
        def label(_label):
            pass

        @staticmethod
        def separator():
            pass

    calls = []

    def choose(_ctx, _field_id, _display, _type_hint, **kwargs):
        calls.append(kwargs)
        kwargs["on_assign"](
            {
                "asset_type": "Texture",
                "guid": "smoke-guid",
                "path_hint": str(texture_path),
            }
        )
        return False

    monkeypatch.setattr(module, "render_asset_reference_field", choose)
    monkeypatch.setattr(
        module,
        "_node_property_is_visible",
        lambda _node, key: key == "shader.texSampler",
    )
    panel, manager = _particle_panel_with_history()
    output = panel._model.find_node("rendering::output.sprite")
    panel._selected_node_uid = output.uid
    manager.clear()

    panel._render_node_properties(Context())

    assert panel._model.find_node(output.uid).data["shader.texSampler"] == {
        "guid": "smoke-guid",
        "path_hint": "Assets/VFX/Smoke.png",
    }
    assert calls[-1]["reference_value"]["asset_type"] == "Texture"
    assert manager.can_undo


def test_particle_node_inspector_texture_clear_uses_live_asset_reference_path(
    monkeypatch,
):
    from Infernux.engine.ui import particle_graph_editor_panel as module

    class Context:
        semantic_capture_enabled = False

        @staticmethod
        def label(_label):
            pass

        @staticmethod
        def separator():
            pass

    def clear(_ctx, _field_id, _display, _type_hint, **kwargs):
        kwargs["on_clear"]()
        return False

    monkeypatch.setattr(module, "render_asset_reference_field", clear)
    monkeypatch.setattr(
        module,
        "_node_property_is_visible",
        lambda _node, key: key == "shader.texSampler",
    )
    panel, manager = _particle_panel_with_history()
    output = panel._model.find_node("rendering::output.sprite")
    output.data["shader.texSampler"] = {
        "guid": "smoke-guid",
        "path_hint": "Assets/VFX/Smoke.png",
    }
    panel._selected_node_uid = output.uid
    manager.clear()

    panel._render_node_properties(Context())

    assert panel._model.find_node(output.uid).data["shader.texSampler"] == {
        "guid": "",
        "path_hint": "",
    }
    assert manager.can_undo


def test_particle_inline_mesh_field_offers_builtin_meshes():
    from Infernux.engine.ui.particle_graph_editor_panel import (
        ParticleGraphEditorPanel,
    )

    panel = ParticleGraphEditorPanel()
    panel._install_node_graph_view_callbacks()

    provider = panel._view.on_node_asset_reference_items

    assert callable(provider)
    assert (
        "Built-in/Cube",
        {
            "asset_type": "Mesh",
            "builtin": "Cube",
            "guid": "",
            "path_hint": "",
        },
    ) in provider("node", "mesh", "Mesh", "cube")
    assert provider("node", "texture", "Texture", "") == ()


def test_particle_output_shader_schema_hot_reload_preserves_selection(monkeypatch):
    from Infernux.engine.ui import particle_graph_editor_panel as module

    panel = module.ParticleGraphEditorPanel()
    output_uid = "rendering::output.sprite"
    panel._selected_node_uid = output_uid
    panel._view.selected_nodes = [output_uid]
    next_generation = panel._shader_definition_generation + 1
    monkeypatch.setattr(
        module, "get_shader_property_generation", lambda: next_generation
    )

    panel._refresh_shader_definitions_if_needed()

    assert panel._shader_definition_generation == next_generation
    assert panel._selected_node_uid == output_uid
    assert panel._view.selected_nodes == [output_uid]


def test_particle_output_shader_switch_preserves_compatible_ports_and_links():
    from Infernux.engine.ui.particle_graph_editor_panel import (
        ParticleGraphEditorPanel,
    )

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    output_uid = "rendering::output.sprite"
    parameter = panel.add_authoring_parameter(
        "Tint", "color", [0.2, 0.4, 0.8, 1.0]
    )
    source = panel.add_authoring_parameter_node(
        parameter["stable_id"], 180.0, 500.0, stage="rendering"
    )
    panel.connect_value(source["uid"], "value", output_uid, "shader.baseColor")

    panel.set_node_property(output_uid, "shader", "Particle Six-Way Smoke")
    panel.set_node_property(output_uid, "shader.alphaScale", 1.75)
    panel.set_node_property(output_uid, "shader", "Particle Unlit")

    output = panel._model.find_node(output_uid)
    assert output is not None
    assert "shader.alphaScale" not in output.data
    assert any(
        link.source_node == source["uid"]
        and link.target_node == output_uid
        and link.target_pin == "shader.baseColor"
        for link in panel._model.links
    )


def _particle_panel_with_history():
    from Infernux.engine.interaction import (
        DocumentRegistry,
        EditorContextSnapshot,
        SelectionService,
    )
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.engine.undo import UndoManager

    DocumentRegistry()
    selection = SelectionService()
    manager = UndoManager()
    manager.set_context_hooks(
        lambda: EditorContextSnapshot(selection=selection.snapshot),
        lambda context, phase: (
            selection.apply_snapshot(
                context.selection,
                reason=phase,
                record_history=False,
            ),
            True,
        )[1],
    )
    panel = ParticleGraphEditorPanel()
    panel._graph_selection.bind(selection)
    return panel, manager


def test_six_way_output_exposes_texture_ports_but_not_internal_controls():
    from Infernux.engine.ui.particle_graph_editor_panel import (
        ParticleGraphEditorPanel,
    )

    panel = ParticleGraphEditorPanel()
    output = panel._model.find_node("rendering::output.sprite")
    panel.set_node_property(output.uid, "shader", "Particle Six-Way Smoke")
    definition = panel._definition_for_node(output)
    shader_ports = {
        item.id for item in definition.ports if item.id.startswith("shader.")
    }

    assert {"shader.positiveAxesMap", "shader.negativeAxesMap"} <= shader_ports
    assert "shader.flipbookColumns" not in shader_ports
    assert "shader.densityClipThreshold" not in shader_ports


def test_particle_output_compiler_strips_stale_internal_shader_properties():
    from Infernux.engine.ui.particle_graph_editor_panel import (
        ParticleGraphEditorPanel,
    )
    from Infernux.particle import ParticleGraphCompiler

    panel = ParticleGraphEditorPanel()
    panel.set_node_property(
        "rendering::output.sprite", "shader", "Particle Six-Way Smoke"
    )
    panel._sync_model_to_asset()
    emitter = panel.asset.emitters[0]
    rendering = emitter.rendering
    nodes = tuple(
        replace(
            node,
            properties={
                **node.properties,
                "shader.densityClipThreshold": 0.4,
                "shader.fadeOutStart": 0.9,
            },
        )
        if node.uid == "output.sprite"
        else node
        for node in rendering.nodes
    )
    asset = replace(
        panel.asset,
        emitters=(
            replace(emitter, rendering=replace(rendering, nodes=nodes)),
        ),
    )

    output = ParticleGraphCompiler().compile(asset).emitters[0].render_plan.outputs[0]

    names = {item.name for item in output.shader_properties}
    assert "densityClipThreshold" not in names
    assert "fadeOutStart" not in names


def test_particle_graph_parameter_connects_directly_to_output_shader_property():
    from Infernux.engine.ui.particle_graph_editor_panel import (
        ParticleGraphEditorPanel,
    )
    from Infernux.particle import ParticleGraphCompiler

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    parameter = panel.add_authoring_parameter(
        "Tint", "color", [0.2, 0.4, 0.8, 1.0]
    )
    source = panel.add_authoring_parameter_node(
        parameter["stable_id"], 180.0, 500.0, stage="rendering"
    )
    panel.connect_value(
        source["uid"],
        "value",
        "rendering::output.sprite",
        "shader.baseColor",
    )

    output = ParticleGraphCompiler().compile(panel.asset).emitters[0].render_plan.outputs[0]
    base_color = next(
        item for item in output.shader_properties if item.name == "baseColor"
    )
    assert base_color.parameter_id == parameter["stable_id"]


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
    assert panel._document_is_dirty() is True
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

    with pytest.raises(ValueError, match="Mesh reference rejects"):
        panel.set_node_asset_reference(node.uid, "mesh", str(texture_path))


def test_particle_graph_editor_hides_internal_volume_authoring():
    from Infernux.engine.ui import particle_graph_editor_panel as module

    panel = module.ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    emitter_id = panel.asset.emitters[0].stable_id

    for kind in ("vector_field", "sdf_volume"):
        with pytest.raises(ValueError, match="authoring are not available"):
            panel.add_authoring_data_interface(emitter_id, kind, "Internal")
    for type_id in (
        "particle.vector_field.sample",
        "particle.collision.sdf",
        "particle.sdf.sample_distance",
        "particle.sdf.sample_gradient",
    ):
        with pytest.raises(ValueError, match="authoring are not available"):
            panel.add_authoring_node("update", type_id, 220.0, 380.0)

    catalog = panel.authoring_type_catalog(limit=200)
    assert not {
        item["type_id"] for item in catalog["types"]
    }.intersection(panel._HIDDEN_INTERNAL_RESOURCE_NODE_TYPES)
    shape = panel.asset.emitters[0].settings.shape.to_dict()
    shape.update(kind="sdf", sdf_interface="hidden", sdf_mode="volume")
    with pytest.raises(ValueError, match="SDF authoring is not available"):
        panel.patch_authoring_emitter_settings(emitter_id, {"shape": shape})


def test_particle_graph_editor_authors_mesh_input_as_constant_or_parameter(
    tmp_path, monkeypatch
):
    from Infernux.engine.ui import particle_graph_editor_panel as module

    mesh_path = tmp_path / "Surface.fbx"
    mesh_path.write_bytes(b"fbx")
    monkeypatch.setattr(module, "_asset_guid_from_path", lambda _path: "mesh-guid")
    monkeypatch.setattr(
        module,
        "_portable_asset_path_hint",
        lambda _path: "Assets/Models/Surface.fbx",
    )

    panel = module.ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    node = panel.add_authoring_node("update", "particle.mesh.sample", 220.0, 380.0)
    bound = panel.set_node_asset_reference(node["uid"], "mesh", str(mesh_path))

    assert bound == {
        "guid": "mesh-guid",
        "path_hint": "Assets/Models/Surface.fbx",
    }
    snapshot = panel.authoring_snapshot()
    sampled = next(item for item in snapshot["nodes"] if item["uid"] == node["uid"])
    assert sampled["properties"]["mesh"] == bound

    parameter = panel.add_authoring_parameter("Surface Mesh", "mesh", bound)
    source = panel.add_authoring_parameter_node(
        parameter["stable_id"], 20.0, 380.0, stage="update"
    )
    link = panel.connect_value(source["uid"], "value", node["uid"], "mesh")

    snapshot = panel.authoring_snapshot()
    assert snapshot["emitters"][0]["data_interfaces"] == []
    assert any(item["uid"] == link["link_uid"] for item in snapshot["links"])


def test_particle_graph_editor_accepts_builtin_mesh_references():
    from Infernux.engine.ui import particle_graph_editor_panel as module

    panel = module.ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    node = panel.add_authoring_node(
        "update", "particle.mesh.sample", 220.0, 380.0
    )

    bound = panel.set_node_asset_reference(node["uid"], "mesh", "Cube")

    assert bound == {"guid": "builtin-mesh:Cube", "path_hint": ""}
    snapshot = panel.authoring_snapshot()
    sampled = next(item for item in snapshot["nodes"] if item["uid"] == node["uid"])
    assert sampled["properties"]["mesh"] == bound


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
    assert panel._document_is_dirty() is True
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


def test_particle_graph_editor_switches_emitter_without_old_selection_winning():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    second = panel.add_authoring_emitter("Second")
    first_id = panel._asset.emitters[0].stable_id

    panel.select_authoring_emitter(first_id)
    selected = panel.select_authoring_emitter(second["stable_id"])

    assert selected["index"] == 1
    assert panel.authoring_snapshot()["emitter_index"] == 1
    assert panel._selected_emitter().stable_id == second["stable_id"]


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
    velocity = definitions["particle.attribute.velocity"]
    assert next(port for port in velocity["ports"] if port["id"] == "value")[
        "dimension_policy"
    ] == "fixed"


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

    from Infernux.engine.ui.node_graph_editor_panel import NodeGraphEditorPanel
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    source = inspect.getsource(NodeGraphEditorPanel._render_node_graph_workspace)
    graph_render = source.index("self._view.render(")
    left_overlay = source.index("self._left_overlay", graph_render)
    right_overlay = source.index("self._right_overlay", left_overlay)
    drop_target = source.index("render_canvas_drop_target", right_overlay)

    assert ParticleGraphEditorPanel()._node_graph_defer_canvas_drop_target()
    assert graph_render < left_overlay < right_overlay < drop_target


def test_particle_graph_uses_the_shared_editor_shell_and_node_graph_model():
    from Infernux.engine.ui.graph_document_authoring import (
        ParticleEmitterGraphAuthoringModel,
    )
    from Infernux.engine.ui.node_graph_editor_panel import NodeGraphEditorPanel
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()

    assert isinstance(panel, NodeGraphEditorPanel)
    assert isinstance(panel._model, ParticleEmitterGraphAuthoringModel)
    assert panel._view.on_link_created == panel._on_link_created
    assert panel._view.on_nodes_deleted == panel._on_nodes_deleted
    assert panel._view.on_node_data_changed == panel._on_node_data_changed


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
    import Infernux.engine.ui.node_graph_editor_panel as shared_module

    panel, _core, _manager = _particle_panel_with_command_core()
    panel._record = lambda *_args: None
    event_type = panel.add_event_type("Pulse", 8, [])
    calls = []

    monkeypatch.setattr(
        shared_module, "render_workspace_add_header", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        shared_module,
        "begin_workspace_entry",
        lambda *_a, **_k: (False, (0.0, 0.0, 100.0, 28.0)),
    )
    monkeypatch.setattr(
        shared_module, "paint_workspace_entry", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        shared_module, "finish_workspace_entry", lambda *_a, **_k: None
    )

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


def test_particle_node_color_inputs_use_shared_color_bar(monkeypatch):
    import Infernux.engine.ui.particle_graph_editor_panel as particle_editor

    class Context:
        semantic_capture_enabled = False

        @staticmethod
        def label(_label):
            pass

        @staticmethod
        def separator():
            pass

        @staticmethod
        def combo(_label, value, _options, _popup_height):
            return value

        @staticmethod
        def drag_float(*_args, **_kwargs):
            raise AssertionError("Color inputs must not render as XYZW drag fields")

    calls = []

    def render_color(_ctx, widget_id, value, **kwargs):
        calls.append((widget_id, list(value), kwargs))
        return list(value)

    monkeypatch.setattr(particle_editor, "render_color_value_bar", render_color)
    panel = particle_editor.ParticleGraphEditorPanel()
    panel._record = lambda *_args: None
    node = panel._on_node_add("particle.attribute.color", 400.0, 230.0)
    panel._selected_node_uid = node.uid

    panel._render_node_properties(Context())

    assert calls == [
        (
            f"##particle_node_{node.uid}_value",
            [1.0, 1.0, 1.0, 1.0],
            {"allow_hdr": True, "default_hdr_enabled": True},
        )
    ]


def test_color_node_can_drive_sprite_output_and_compile_to_gpu_glsl():
    from dataclasses import replace

    from Infernux.engine.ui.graph_document_authoring import (
        ParticleEmitterGraphAuthoringModel,
    )
    from Infernux.particle import (
        GpuParticleGlslLowerer,
        ParticleGraphCompiler,
        ParticleKernelLowerer,
    )
    from Infernux.particle.asset import ParticleGraphAsset
    from Infernux.particle.nodes import particle_graph_node_definitions

    asset = ParticleGraphAsset()
    emitter = asset.emitters[0]
    definitions = particle_graph_node_definitions(asset)
    model = ParticleEmitterGraphAuthoringModel(
        emitter,
        definition_set=definitions,
    )
    model.set_authoring_stage("rendering")
    model.prepare_node_creation("rendering")
    color = model.add_node(
        "common.constant.color",
        180.0,
        model._STAGE_Y["rendering"] + 100.0,
        value=[2.0, 0.5, 0.25, 1.0],
    )
    link = model.add_link(
        color.uid,
        "value",
        "rendering::output.sprite",
        "shader.baseColor",
    )
    assert link is not None

    documents = model.to_documents()
    emitter = replace(emitter, rendering=documents["rendering"])
    asset = replace(asset, emitters=(emitter,))
    compiled = ParticleGraphCompiler().compile(asset)
    output = compiled.emitters[0].render_plan.outputs[0]
    base_color = next(
        item for item in output.shader_properties if item.name == "baseColor"
    )

    assert base_color.default == [2.0, 0.5, 0.25, 1.0]
    assert base_color.parameter_id == ""
    kernel = ParticleKernelLowerer().lower(compiled)
    gpu_program = GpuParticleGlslLowerer().lower(kernel)
    assert gpu_program.emitters


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
    assert _save_particle_graph(panel, path) is True
    artifact = ParticleArtifactRegistry.get(str(path))
    assert artifact is not None
    assert artifact.source_kind == "graph"
    assert artifact.hir["name"] == "Smoke"
    assert panel._document_is_dirty() is False

    reopened = ParticleGraphEditorPanel()
    assert reopened.open_document_resource_immediate(str(path)) is True
    assert reopened.asset.name == "Smoke"
    assert reopened._document_is_dirty() is False
    assert reopened.reload_from_disk() is True
    assert reopened._document_is_dirty() is False


def test_particle_graph_close_reopen_preserves_direct_save_target(tmp_path, monkeypatch):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction import DocumentRegistry
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    DocumentRegistry()
    monkeypatch.setattr(AssetManager, "reimport_asset", classmethod(lambda cls, _path: None))
    path = tmp_path / "New_Particle_Graph.particlegraph"
    panel = ParticleGraphEditorPanel()
    panel.on_enable()
    assert _save_particle_graph(panel, path) is True
    original_document_id = panel.document_id

    panel.unbind_document()
    assert panel.document_id == ""
    panel.open()

    assert panel.document_id == original_document_id
    assert panel._file_path == str(path.resolve())
    panel.add_authoring_parameter("Intensity")
    assert panel._document_is_dirty() is True
    monkeypatch.setattr(
        panel,
        "_show_save_as_dialog",
        lambda: (_ for _ in ()).throw(AssertionError("Save unexpectedly became Save As")),
    )

    assert DocumentRegistry.instance().request_save(panel.document_id).accepted
    _finish_particle_graph_save(panel, path)
    assert panel._file_path == str(path.resolve())
    assert panel._document_is_dirty() is False
    assert DocumentRegistry.instance().require(panel.document_id).is_dirty is False
    panel.on_disable()


def test_particle_graph_deferred_save_stays_clean_after_same_frame_edit(
    tmp_path, monkeypatch
):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction import DocumentRegistry
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    registry = DocumentRegistry()
    monkeypatch.setattr(AssetManager, "reimport_asset", classmethod(lambda cls, _path: None))
    path = tmp_path / "Deferred.particlegraph"
    panel = ParticleGraphEditorPanel()
    panel.on_enable()
    assert _save_particle_graph(panel, path) is True
    panel.add_authoring_parameter("BeforeSave")

    result = registry.defer_save(panel.document_id)
    panel.add_authoring_parameter("CommittedLaterInFrame")
    assert result.accepted
    registry.process_deferred_saves()
    _finish_particle_graph_save(panel, path)
    document = registry.require(panel.document_id)
    assert document.is_dirty is False
    assert panel._document_is_dirty() is False
    reopened = ParticleGraphAsset.load(str(path))
    assert [parameter.name for parameter in reopened.parameters] == [
        "BeforeSave",
        "CommittedLaterInFrame",
    ]
    panel.on_disable()


def test_particle_graph_pending_save_as_clears_the_serialized_revision(
    tmp_path, monkeypatch
):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction import DocumentRegistry
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    registry = DocumentRegistry()
    monkeypatch.setattr(AssetManager, "reimport_asset", classmethod(lambda cls, _path: None))
    path = tmp_path / "PendingSaveAs.particlegraph"
    panel = ParticleGraphEditorPanel()
    panel.on_enable()
    document = registry.require(panel.document_id)
    ticket = registry.begin_save(document.document_id, save_as=True)
    panel._pending_save_ticket_id = ticket.ticket_id
    panel.add_authoring_parameter("CommittedWhileDialogWasOpen")

    assert panel._save_to(str(path), ticket_id=ticket.ticket_id) is True
    assert document.resource_path == ""
    _finish_particle_graph_save(panel, path)

    document = registry.require(panel.document_id)
    assert document.resource_path == str(path.resolve())
    assert document.is_dirty is False
    assert panel._document_is_dirty() is False
    assert panel._window_title_suffix() == ""
    panel.on_disable()


def test_particle_graph_async_save_keeps_edits_after_capture_dirty(
    tmp_path, monkeypatch
):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction import DocumentRegistry
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    registry = DocumentRegistry()
    monkeypatch.setattr(AssetManager, "reimport_asset", classmethod(lambda cls, _path: None))
    path = tmp_path / "SnapshotIsolation.particlegraph"
    panel = ParticleGraphEditorPanel()
    panel.on_enable()
    panel.add_authoring_parameter("Captured")
    document = registry.require(panel.document_id)
    ticket = registry.begin_save(document.document_id, save_as=True)

    assert panel._save_to(str(path), ticket_id=ticket.ticket_id) is True
    panel.add_authoring_parameter("AfterCapture")
    _finish_particle_graph_save(panel, path)

    first_snapshot = ParticleGraphAsset.load(str(path))
    assert [parameter.name for parameter in first_snapshot.parameters] == ["Captured"]
    assert document.is_dirty is True

    assert _save_particle_graph(panel, path) is True
    second_snapshot = ParticleGraphAsset.load(str(path))
    assert [parameter.name for parameter in second_snapshot.parameters] == [
        "Captured",
        "AfterCapture",
    ]
    assert document.is_dirty is False
    panel.on_disable()


def test_particle_graph_save_absorbs_synchronous_reimport_bookkeeping_revision(
    tmp_path, monkeypatch
):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction import DocumentRegistry
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    registry = DocumentRegistry()
    path = tmp_path / "ReimportBookkeeping.particlegraph"
    panel = ParticleGraphEditorPanel()
    panel.on_enable()
    panel.add_authoring_parameter("Intensity")
    document = registry.require(panel.document_id)

    def reimport_asset(_cls, _path):
        registry.mark_changed(document.document_id)
        return True

    monkeypatch.setattr(AssetManager, "reimport_asset", classmethod(reimport_asset))

    ticket = registry.begin_save(document.document_id, save_as=True)
    assert panel._save_to(str(path), ticket_id=ticket.ticket_id) is True
    _finish_particle_graph_save(panel, path)

    assert document.revision == document.saved_revision
    assert document.is_dirty is False
    assert panel._document_is_dirty() is False
    assert panel._window_title_suffix() == ""
    panel.on_disable()


def test_particle_graph_document_revision_and_selection_are_globally_authoritative(
    tmp_path, monkeypatch
):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction import (
        DocumentRegistry,
        GraphElementKind,
        SelectionService,
    )
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    DocumentRegistry()
    selection = SelectionService()
    monkeypatch.setattr(AssetManager, "reimport_asset", classmethod(lambda cls, _path: None))
    panel = ParticleGraphEditorPanel()
    panel.on_enable()
    path = tmp_path / "Authority.particlegraph"

    assert _save_particle_graph(panel, path) is True
    document = DocumentRegistry.instance().require(panel.document_id)
    assert document.is_dirty is False

    parameter = panel.add_authoring_parameter("Intensity")

    assert document.is_dirty is True
    assert panel._document_is_dirty() is True
    assert selection.snapshot.primary.document_id == document.document_id
    assert selection.snapshot.primary.sub_kind == GraphElementKind.PARAMETER.value
    assert selection.snapshot.primary.target_id == parameter["stable_id"]

    assert _save_particle_graph(panel, path) is True
    assert document.is_dirty is False
    assert panel._document_is_dirty() is False
    panel.on_disable()


def test_particle_parameter_insert_and_update_use_precise_graph_diffs():
    from Infernux.engine.interaction import GraphElementKind

    panel, manager = _particle_panel_with_history()
    document = panel._particle_document()
    initial_revision = document.revision

    created = panel.add_authoring_parameter("Intensity")
    parameter_id = created["stable_id"]
    insert_revision = document.revision

    assert insert_revision > initial_revision
    assert panel._graph_selection.primary_id(GraphElementKind.PARAMETER) == parameter_id

    updated = panel.update_authoring_parameter(
        parameter_id,
        {"name": "Smoke Intensity", "default": 2.0},
    )
    update_revision = document.revision
    assert updated["name"] == "Smoke Intensity"
    assert update_revision > insert_revision

    manager.undo()
    parameter = next(item for item in panel.asset.parameters if item.stable_id == parameter_id)
    assert parameter.name == "Intensity"
    assert document.revision == insert_revision

    manager.undo()
    assert all(item.stable_id != parameter_id for item in panel.asset.parameters)
    assert document.revision == initial_revision

    manager.redo()
    manager.redo()
    parameter = next(item for item in panel.asset.parameters if item.stable_id == parameter_id)
    assert parameter.name == "Smoke Intensity"
    assert document.revision == update_revision


def test_particle_structural_parameter_edit_restores_affected_graph_in_one_undo():
    panel, manager = _particle_panel_with_history()
    parameter = panel.add_authoring_parameter("Scale", "f32", 1.0)
    source = panel.add_authoring_parameter_node(
        parameter["stable_id"], 220.0, 40.0, stage="init"
    )
    target = panel.add_authoring_node(
        "init", "particle.attribute.size", 480.0, 40.0
    )
    link = panel.connect_value(source["uid"], "value", target["uid"], "value")
    manager.clear()

    panel.update_authoring_parameter(
        parameter["stable_id"],
        {"type": TypeRef(ValueType.VEC3).to_dict()},
    )

    assert panel._model.find_link(link["link_uid"]) is None
    manager.undo()
    restored = next(
        item
        for item in panel.asset.parameters
        if item.stable_id == parameter["stable_id"]
    )
    assert restored.value_type == TypeRef(ValueType.F32)
    assert panel._model.find_link(link["link_uid"]) is not None
    manager.redo()
    assert panel._model.find_link(link["link_uid"]) is None


def test_particle_emitter_settings_use_one_precise_undo_action():
    panel, manager = _particle_panel_with_history()
    emitter = panel.asset.emitters[0]
    settings = emitter.settings.to_dict()
    settings["spawn_rate"] = 73.0
    manager.clear()

    panel.set_authoring_emitter_settings(emitter.stable_id, settings)

    assert panel.asset.emitters[0].settings.spawn_rate == 73.0
    manager.undo()
    assert panel.asset.emitters[0].settings.spawn_rate == emitter.settings.spawn_rate
    manager.redo()
    assert panel.asset.emitters[0].settings.spawn_rate == 73.0


def test_particle_data_interface_edit_uses_one_precise_undo_action():
    from Infernux.particle.data_interface import VectorField

    panel, manager = _particle_panel_with_history()
    emitter = panel.asset.emitters[0]
    interface = VectorField(stable_id="wind-field", name="Wind Field")
    panel._replace_emitter(
        replace(emitter, data_interfaces=(interface,))
    )
    panel._bind_stage()
    manager.clear()

    updated = panel.patch_authoring_data_interface(
        emitter.stable_id,
        interface.stable_id,
        {"vector_scale": 2.5},
    )

    assert updated["vector_scale"] == 2.5
    assert manager.undo_description == "Edit Particle Graph Data Interface"
    manager.undo()
    assert panel.asset.emitters[0].data_interfaces[0].vector_scale == 1.0
    manager.redo()
    assert panel.asset.emitters[0].data_interfaces[0].vector_scale == 2.5


def test_particle_event_type_and_flow_use_precise_domain_diffs():
    panel, manager = _particle_panel_with_history()

    event_type = panel.add_event_type("Impact", 8, [])
    event_id = event_type["stable_id"]
    manager.undo()
    assert panel.asset.event_types == ()


    manager.redo()
    assert panel.asset.event_types[0].stable_id == event_id

    manager.clear()
    flow = panel.add_authoring_event_flow(event_id, 420.0, 760.0)
    flow_id = flow["flow_id"]
    assert panel.asset.emitters[0].event_flows[0].stable_id == flow_id
    manager.undo()
    assert panel.asset.emitters[0].event_flows == ()
    manager.redo()
    assert panel.asset.emitters[0].event_flows[0].stable_id == flow_id
    assert panel._model.find_node(f"event.{flow_id}::root.event") is not None

    manager.clear()
    panel.remove_event_type(event_id)
    assert panel.asset.event_types == ()
    assert panel.asset.emitters[0].event_flows[0].event_id == ""
    manager.undo()
    assert panel.asset.event_types[0].stable_id == event_id
    assert panel.asset.emitters[0].event_flows[0].event_id == event_id
    manager.redo()
    assert panel.asset.event_types == ()
    assert panel.asset.emitters[0].event_flows[0].event_id == ""


def test_particle_graph_disk_discard_reconciles_document_revision(tmp_path):
    from Infernux.engine.interaction import DocumentActionStatus, DocumentRegistry
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.particle import ParticleGraphAsset

    target = tmp_path / "Saved.particlegraph"
    ParticleGraphAsset(stable_id="saved-graph", name="Saved").save(str(target))
    panel = ParticleGraphEditorPanel()
    assert panel.open_document_resource_immediate(str(target))
    registry = DocumentRegistry.instance()
    document = registry.require(panel.document_id)
    registry.mark_changed(document.document_id)

    result = registry.request_discard(document.document_id)

    assert result.status is DocumentActionStatus.APPLIED
    assert registry.require(panel.document_id).is_dirty is False
    assert panel._document_is_dirty() is False


def test_particle_node_drag_records_only_stable_node_positions():
    panel, manager = _particle_panel_with_history()
    node_data = panel.add_authoring_node(
        "update",
        "common.noise.vector3d",
        240.0,
        460.0,
    )
    node_uid = node_data["uid"]
    node = panel._model.find_node(node_uid)
    before = (node.pos_x, node.pos_y)
    manager.clear()

    panel._view.project_selection((node_uid,), "")
    panel._on_node_drag_start(node_uid)
    node.pos_x += 37.0
    node.pos_y -= 19.0
    after = (node.pos_x, node.pos_y)
    panel._on_node_drag_end(node_uid)

    assert (panel._model.find_node(node_uid).pos_x, panel._model.find_node(node_uid).pos_y) == after
    manager.undo()
    assert (
        panel._model.find_node(node_uid).pos_x,
        panel._model.find_node(node_uid).pos_y,
    ) == before
    manager.redo()
    assert (
        panel._model.find_node(node_uid).pos_x,
        panel._model.find_node(node_uid).pos_y,
    ) == after


def test_connect_vector3_to_set_velocity_never_moves_nodes_through_panel_history():
    """The real panel transaction must preserve layout across connect/undo/redo."""
    panel, manager = _particle_panel_with_history()
    model = panel._model
    model.prepare_node_creation("init")
    vector = model.add_node("common.vector.compose3", 619.0, 287.0)
    velocity = model.find_node("init::init.velocity")
    assert velocity is not None

    def positions_by_raw_id():
        return {
            model._document_uid(node.uid): (node.pos_x, node.pos_y)
            for node in model.nodes
        }

    expected = positions_by_raw_id()
    manager.clear()
    panel._on_link_created(vector.uid, "value", velocity.uid, "value")

    assert positions_by_raw_id() == expected
    assert any(
        link.source_node == vector.uid
        and link.source_pin == "value"
        and link.target_node == velocity.uid
        and link.target_pin == "value"
        for link in model.links
    )
    manager.undo()
    assert positions_by_raw_id() == expected
    manager.redo()
    assert positions_by_raw_id() == expected


def test_particle_node_drag_keeps_exact_cross_panel_history_order():
    """A graph drag must remain between the two Scene editing sessions."""
    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import (
        ContextRestoreStatus,
        EditorInteractionCore,
        PanelInteractionDescriptor,
        SelectionDomain,
        SelectionTarget,
    )
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.engine.undo import SetPropertyCommand, UndoManager

    core = EditorInteractionCore()
    manager = UndoManager(core.action_journal)
    manager.set_context_hooks(
        core.capture_context,
        lambda context, _phase: (
            core.focus.apply_snapshot(context.focus, record_history=False),
            core.selection.apply_snapshot(
                context.selection,
                reason="cross_panel_test_restore",
                record_history=False,
            ),
            ContextRestoreStatus.READY,
        )[-1],
    )
    bootstrap = BootstrapSelectionMixin()
    bootstrap.interaction_core = core
    bootstrap._present_selection_snapshot = lambda _snapshot: None
    core.focus.add_change_listener(bootstrap._on_global_focus_changed)
    core.selection.add_listener(bootstrap._on_global_selection_changed)

    panel = ParticleGraphEditorPanel()
    core.panels.register_type(
        panel.panel_type_id,
        panel.PANEL_INTERACTION,
    )
    core.panels.bind_view(panel.window_id, panel.panel_type_id, panel)
    core.panels.register_type(
        "scene_view",
        PanelInteractionDescriptor(
            owned_selection_domains=frozenset({SelectionDomain.SCENE_OBJECT}),
        ),
    )
    panel._graph_selection.bind(core.selection)
    node_data = panel.add_authoring_node(
        "update",
        "common.noise.vector3d",
        240.0,
        460.0,
    )
    node_uid = node_data["uid"]
    manager.clear()
    camera = SimpleNamespace(position=0.0)

    core.focus.activate_panel("scene_view", view_id="scene_view")
    core.selection.select(
        SelectionTarget.scene_object(5),
        owner_id="scene_view",
    )
    manager.execute(SetPropertyCommand(camera, "position", 0.0, 1.0, "Move camera"))

    core.focus.activate_panel(
        panel.panel_type_id,
        view_id=panel.window_id,
        document_id=panel.document_id,
        child_context_id=panel.current_child_context_id(),
    )
    panel._select_canvas_node(node_uid, record_history=True)
    panel._on_node_drag_start(node_uid)
    node = panel._model.find_node(node_uid)
    node.pos_x += 37.0
    panel._on_node_drag_end(node_uid)

    core.focus.activate_panel(
        "scene_view",
        view_id="scene_view",
        document_id="",
        child_context_id="",
    )
    core.selection.select(
        SelectionTarget.scene_object(5),
        owner_id="scene_view",
    )
    manager.execute(SetPropertyCommand(camera, "position", 1.0, 2.0, "Move camera"))
    core.selection.clear(reason="scene_pick_clear")

    assert [entry.action.description for entry in manager.action_journal.entries] == [
        "Focus scene_view",
        "Change Selection",
        "Move camera",
        f"Focus {panel.panel_type_id}",
        "Change Selection",
        "Move Particle Graph node",
        "Focus scene_view",
        "Change Selection",
        "Move camera",
        "Change Selection",
    ]

    for _ in range(4):
        manager.undo()
    assert manager.undo_description == "Move Particle Graph node"
    manager.undo()
    assert panel._model.find_node(node_uid).pos_x == 240.0


def test_particle_node_and_link_deletion_is_one_precise_undo_action():
    panel, manager = _particle_panel_with_history()
    panel._stage = "update"
    panel._model.set_authoring_stage("update")
    panel._model.prepare_node_creation("update")
    node = panel._on_node_add("particle.attribute.velocity", 260.0, 230.0)
    assert node is not None
    panel._on_link_created("update::root.update", "out", node.uid, "in")
    link = next(
        item
        for item in panel._model.links
        if item.target_node == node.uid and item.target_pin == "in"
    )
    node_uid = node.uid
    link_uid = link.uid
    manager.clear()

    panel._on_nodes_deleted((node_uid,))

    assert panel._model.find_node(node_uid) is None
    assert panel._model.find_link(link_uid) is None

    manager.undo()
    assert panel._model.find_node(node_uid) is not None
    assert panel._model.find_link(link_uid) is not None

    manager.redo()
    assert panel._model.find_node(node_uid) is None
    assert panel._model.find_link(link_uid) is None


def test_particle_graph_uses_shared_typed_subgraph_clipboard_and_undo():
    from Infernux.engine.interaction import (
        ClipboardDomain,
        ClipboardService,
        GraphElementKind,
    )

    panel, manager = _particle_panel_with_history()
    panel._model.set_authoring_stage("update")
    panel._model.prepare_node_creation("update")
    first = panel._on_node_add("particle.control.wait_frames", 260.0, 230.0)
    panel._model.prepare_node_creation("update")
    second = panel._on_node_add("particle.control.wait_seconds", 520.0, 230.0)
    assert first is not None and second is not None
    panel._on_link_created(first.uid, "out", second.uid, "in")
    assert any(
        link.source_node == first.uid and link.target_node == second.uid
        for link in panel._model.links
    )
    panel._graph_selection.select(
        (
            panel._graph_element_from_view(GraphElementKind.NODE, first.uid),
            panel._graph_element_from_view(GraphElementKind.NODE, second.uid),
        ),
        record_history=False,
    )
    manager.clear()

    panel._on_graph_copy()
    payload = ClipboardService.instance().peek(ClipboardDomain.GRAPH_ELEMENT)
    assert payload is not None
    assert payload.items[0].sub_kind == "node_graph_subgraph"
    assert len(payload.items[0].data.state.nodes) == 2
    assert len(payload.items[0].data.state.links) == 1

    panel._on_graph_paste()
    pasted = panel._graph_selection.selected_ids(GraphElementKind.NODE)
    assert len(pasted) == 2
    assert all(stable_id not in {first.uid, second.uid} for stable_id in pasted)
    assert manager.undo_description == "Paste graph nodes"
    copied_count = len(panel._model.nodes)

    manager.undo()
    assert len(panel._model.nodes) == copied_count - 2
    manager.redo()
    assert len(panel._model.nodes) == copied_count


def test_particle_graph_shared_commands_duplicate_and_cut_atomically():
    from Infernux.engine.interaction import GraphElementKind

    panel, manager = _particle_panel_with_history()
    panel._model.set_authoring_stage("update")
    panel._model.prepare_node_creation("update")
    first = panel._on_node_add("particle.control.wait_frames", 260.0, 230.0)
    panel._model.prepare_node_creation("update")
    second = panel._on_node_add("particle.control.wait_seconds", 520.0, 230.0)
    assert first is not None and second is not None
    panel._on_link_created(first.uid, "out", second.uid, "in")
    panel._graph_selection.select(
        (
            panel._graph_element_from_view(GraphElementKind.NODE, first.uid),
            panel._graph_element_from_view(GraphElementKind.NODE, second.uid),
        ),
        record_history=False,
    )
    manager.clear()

    assert panel.command_edit_duplicate()
    duplicated_count = len(panel._model.nodes)
    assert manager.undo_description == "Duplicate graph nodes"
    manager.undo()
    assert len(panel._model.nodes) == duplicated_count - 2

    panel._graph_selection.select(
        (
            panel._graph_element_from_view(GraphElementKind.NODE, first.uid),
            panel._graph_element_from_view(GraphElementKind.NODE, second.uid),
        ),
        record_history=False,
    )
    manager.clear()
    assert panel.command_edit_cut()
    assert panel._model.find_node(first.uid) is None
    assert panel._model.find_node(second.uid) is None
    assert manager.undo_description == "Delete Particle Graph nodes"
    manager.undo()
    assert panel._model.find_node(first.uid) is not None
    assert panel._model.find_node(second.uid) is not None


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
    monkeypatch.setattr(
        panel,
        "_sync_model_to_asset",
        lambda: (_ for _ in ()).throw(AssertionError("must not serialize the graph")),
    )

    assert panel.authoring_document_state() == {
        "file_path": "Assets/VFX/Legacy.particlegraph",
        "dirty": False,
    }


def test_particle_system_inspector_metadata_is_localizable_and_backend_is_emitter_owned():
    from Infernux.components.particle_system import ParticleSystem
    from Infernux.components.fields import get_serialized_fields

    fields = get_serialized_fields(ParticleSystem)
    assert {
        name for name, metadata in fields.items() if not metadata.hidden
    } == {
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
    assert {
        name for name, metadata in fields.items() if metadata.hidden
    } == {
        "_parameter_overrides_json",
        "_emitter_overrides_json",
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
def test_particle_graph_workspace_child_context_is_stable_and_restorable():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    panel = ParticleGraphEditorPanel()
    panel._workspace_tab_index = 1
    assert panel.current_child_context_id() == "particle_graph.workspace.parameters"

    assert panel.restore_child_context("particle_graph.workspace.events")
    assert panel._workspace_tab_index == 2
    assert not panel.restore_child_context("particle_graph.workspace.unknown")
