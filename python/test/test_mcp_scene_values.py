from Infernux.lib import Vector3
from Infernux.mcp.tools.scene import (
    _coerce_component_property_value,
    _coerce_property_value,
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
