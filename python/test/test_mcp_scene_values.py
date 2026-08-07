from enum import IntEnum

from Infernux.components import InxComponent, serialized_field
from Infernux.lib import Vector3
from Infernux.mcp.tools.scene import (
    _add_component_through_editor_transaction,
    _component_snapshot,
    _coerce_component_property_value,
    _coerce_property_value,
    _find_component,
    _remove_component_through_editor_transaction,
    _set_component_fields_through_editor_transaction,
    _resolved_component_field_metadata,
)
from Infernux.renderstack.effect_slot import EffectSlot
from Infernux.renderstack.render_stack import RenderStack


def test_property_coercion_uses_current_vector3_type_for_array_input():
    current = Vector3(1.0, 2.0, 3.0)

    value = _coerce_property_value("size", [4.0, 5.0, 6.0], current)

    assert isinstance(value, Vector3)
    assert (value.x, value.y, value.z) == (4.0, 5.0, 6.0)


def test_component_coercion_builds_typed_render_effect_slots():
    stack = RenderStack()

    value = _coerce_component_property_value(
        stack,
        "effect_slots",
        [{
            "slot_id": "post",
            "stage_id": "final",
            "effect": {
                "guid": "effect-guid",
                "path_hint": "Assets/Rendering/Post.effectgroup",
            },
            "enabled": True,
        }],
        [],
    )

    assert len(value) == 1
    assert isinstance(value[0], EffectSlot)
    assert value[0].effect_ref.guid == "effect-guid"
    stack.effect_slots = value
    document = stack._serialize_fields_document()
    assert document["effect_slots"][0]["$type"] == "serializable_object"
    assert document["effect_slots"][0]["fields"]["effect"] == {
        "$type": "asset_ref",
        "asset_type": "RenderEffect",
        "guid": "effect-guid",
        "path_hint": "Assets/Rendering/Post.effectgroup",
    }


def test_component_coercion_rejects_incomplete_serializable_objects():
    stack = RenderStack()

    try:
        _coerce_component_property_value(
            stack,
            "effect_slots",
            [{"stage_id": "final"}],
            [],
        )
    except ValueError as exc:
        assert "serialized fields mismatch" in str(exc)
    else:
        raise AssertionError("incomplete EffectSlot input must be rejected")


def test_component_lookup_prefers_public_wrapper_over_same_native_component():
    class NativeMeshRenderer:
        type_name = "MeshRenderer"
        component_id = 17

    class MeshRenderer:
        type_name = "MeshRenderer"
        component_id = 17
        material_guid = "public-wrapper-field"

    class Object:
        @staticmethod
        def get_py_components():
            return [MeshRenderer()]

        @staticmethod
        def get_components():
            return [NativeMeshRenderer()]

    component = _find_component(Object(), "MeshRenderer", 0)

    assert type(component).__name__ == "MeshRenderer"
    assert component.material_guid == "public-wrapper-field"
    assert _find_component(Object(), "MeshRenderer", 1) is None


def test_component_lookup_promotes_registered_native_component(monkeypatch):
    from Infernux.components import BuiltinComponent

    class NativeProbe:
        type_name = "McpNativeProbe"
        component_id = 23

    class PublicProbe:
        type_name = "McpNativeProbe"
        component_id = 23
        public_value = "wrapped"

    class WrapperFactory:
        @staticmethod
        def _get_or_create_wrapper(component, game_object):
            assert isinstance(component, NativeProbe)
            assert isinstance(game_object, Object)
            return PublicProbe()

    class Object:
        @staticmethod
        def get_py_components():
            return []

        @staticmethod
        def get_components():
            return [NativeProbe()]

    monkeypatch.setitem(
        BuiltinComponent._builtin_registry, "McpNativeProbe", WrapperFactory
    )

    component = _find_component(Object(), "McpNativeProbe", 0)

    assert isinstance(component, PublicProbe)
    assert component.public_value == "wrapped"


def test_component_snapshot_uses_roundtrip_enum_document():
    class Mode(IntEnum):
        OFF = 0
        ON = 1

    class Probe(InxComponent):
        mode: Mode = serialized_field(default=Mode.ON)

    class Object:
        id = 41
        name = "Probe"

    snapshot = _component_snapshot(Object(), Probe())

    assert snapshot["fields"]["mode"] == {
        "$type": "enum",
        "enum_type": "test_component_snapshot_uses_roundtrip_enum_document.<locals>.Mode",
        "name": "ON",
    }


def test_builtin_lazy_enum_metadata_resolves_to_public_enum():
    from Infernux.components import Light
    from Infernux.components.serialized_field import get_serialized_fields
    from Infernux.lib import LightShadows

    metadata = get_serialized_fields(Light)["shadows"]
    resolved = _resolved_component_field_metadata(metadata)

    assert metadata.enum_type == "LightShadows"
    assert resolved.enum_type is LightShadows

    from Infernux.components.value_codec import VALUE_CODECS

    decoded = VALUE_CODECS.decode(
        {"$type": "enum", "enum_type": "LightShadows", "name": "Soft"},
        resolved,
        "Light.shadows",
    )
    assert decoded is LightShadows.Soft


def test_mcp_component_add_fields_are_one_editor_transaction(scene):
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager

    owner = scene.create_game_object("McpComponentAddTransaction")
    previous_manager = UndoManager._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    try:
        light = _add_component_through_editor_transaction(
            owner,
            "Light",
            fields={"intensity": 3.25},
        )
        component_id = int(light.component_id)
        assert light.intensity == 3.25

        manager.undo()
        assert owner.get_component("Light") is None
        assert not manager.can_undo

        manager.redo()
        restored = owner.get_component("Light")
        assert restored is not None
        assert restored.component_id == component_id
        assert restored.intensity == 3.25
    finally:
        core.shutdown()
        UndoManager._instance = previous_manager


def test_mcp_component_multi_field_edit_is_one_editor_transaction(scene):
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager

    owner = scene.create_game_object("McpComponentFieldsTransaction")
    light = owner.add_component("Light")
    old_intensity = float(light.intensity)
    old_range = float(light.range)
    previous_manager = UndoManager._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    try:
        changed = _set_component_fields_through_editor_transaction(
            light,
            "Light",
            {"intensity": 3.5, "range": 17.0},
        )
        assert changed == {"intensity": 3.5, "range": 17.0}
        assert len(manager.action_journal.applied_entries()) == 1
        manager.undo()
        assert float(light.intensity) == old_intensity
        assert float(light.range) == old_range
        assert not manager.can_undo
        manager.redo()
        assert float(light.intensity) == 3.5
        assert float(light.range) == 17.0
    finally:
        core.shutdown()
        UndoManager._instance = previous_manager


def test_mcp_component_remove_reports_constraint_rejection(scene):
    from Infernux.components.decorators import require_component
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager

    class McpDependency(InxComponent):
        pass

    @require_component(McpDependency)
    class McpConsumer(InxComponent):
        pass

    owner = scene.create_game_object("McpConstraintAwareRemove")
    consumer = owner.add_component(McpConsumer)
    dependency = owner.get_component(McpDependency)
    assert consumer is not None
    assert dependency is not None

    previous_manager = UndoManager._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    try:
        assert _remove_component_through_editor_transaction(
            owner, dependency
        ) is False
        assert owner.get_component(McpDependency) is dependency
        assert not manager.can_undo
    finally:
        core.shutdown()
        UndoManager._instance = previous_manager
