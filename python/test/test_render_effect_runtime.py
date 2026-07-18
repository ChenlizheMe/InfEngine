import json

import pytest

from Infernux.components.serialized_field import FieldType, get_raw_field_value, get_serialized_fields
from Infernux.core.asset_ref import RenderEffectRef
from Infernux.renderstack.effect_slot import EffectSlot
from Infernux.renderstack.render_effect import RenderEffect
from Infernux.renderstack.render_effect_asset import RenderEffectAsset
from Infernux.renderstack.render_stack import RenderStack


def test_render_effect_has_material_like_typed_parameter_api():
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"intensity": 0.5},
        )
    )

    effect.set_float("intensity", 1.25)
    effect.set_int("samples", 8)
    effect.set_color("tint", 1.0, 0.5, 0.25, 1.0)

    assert effect.get_float("intensity") == pytest.approx(1.25)
    assert effect.get_int("samples") == 8
    assert effect.get_color("tint") == pytest.approx((1.0, 0.5, 0.25, 1.0))
    assert effect.revision == 3
    assert effect.feature_type == "infernux.post.bloom"


def test_render_effect_clone_is_runtime_only_and_parameter_isolated():
    shared = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"intensity": 0.5},
        ),
        file_path="Assets/Effects/Bloom.effect",
        guid="bloom-guid",
    )

    instance = shared.clone()
    instance.set_float("intensity", 2.0)

    assert shared.get_float("intensity") == pytest.approx(0.5)
    assert instance.get_float("intensity") == pytest.approx(2.0)
    assert instance.guid == ""
    assert instance.file_path == ""


def test_render_effect_save_and_load_round_trip(tmp_path):
    path = tmp_path / "Bloom.effect"
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"threshold": 1.0},
        )
    )
    assert effect.save(str(path))

    loaded = RenderEffect.load(str(path))

    assert loaded is not None
    assert loaded.feature_type == "infernux.post.bloom"
    assert loaded.get_float("threshold") == pytest.approx(1.0)


def test_render_stack_effect_slots_use_structured_serialized_list():
    fields = get_serialized_fields(RenderStack)
    metadata = fields["effect_slots"]

    assert metadata.field_type is FieldType.LIST
    assert metadata.element_type is FieldType.SERIALIZABLE_OBJECT
    assert metadata.element_class is EffectSlot

    stack = RenderStack()
    slot = stack.add_effect_slot(
        "final",
        RenderEffectRef(guid="bloom-guid", path_hint="Assets/Effects/Bloom.effect"),
    )
    document = stack._serialize_fields_document()

    assert isinstance(document["effect_slots"], list)
    assert document["effect_slots"][0]["$type"] == "serializable_object"
    assert "effect_stage_bindings_json" in document
    assert document["effect_stage_bindings_json"] == ""
    assert slot.effect_ref.guid == "bloom-guid"


def test_render_stack_structured_slots_round_trip_without_hidden_json():
    stack = RenderStack()
    stack.add_effect_slot(
        "after_sky",
        RenderEffectRef(guid="fog-guid", path_hint="Assets/Effects/Fog.effect"),
        enabled=False,
    )

    restored = RenderStack()
    restored._deserialize_fields_document(stack._serialize_fields_document())

    slots = restored.get_effect_stage_slots("after_sky")
    assert len(slots) == 1
    assert slots[0].enabled is False
    assert slots[0].effect_ref.guid == "fog-guid"
    assert restored.effect_stage_bindings_json == ""


def test_render_stack_migrates_legacy_json_binding_once():
    stack = RenderStack()
    stack.effect_stage_bindings_json = json.dumps(
        {
            "$schema": "infernux.render_stack_effect_bindings",
            "$version": 1,
            "stages": {
                "final": [
                    {
                        "slot_id": "legacy-slot",
                        "asset": {
                            "guid": "legacy-guid",
                            "path_hint": "Assets/Effects/Legacy.effect",
                        },
                        "enabled": True,
                        "overrides": {"intensity": 0.5},
                    }
                ]
            },
        }
    )

    stack.on_after_deserialize()

    slots = stack.get_effect_stage_slots("final")
    assert len(slots) == 1
    assert slots[0].slot_id == "legacy-slot"
    assert slots[0].effect_ref.guid == "legacy-guid"
    assert stack.effect_stage_bindings_json == ""


def test_slot_effect_property_resolves_to_mutable_runtime_asset(tmp_path):
    path = tmp_path / "Bloom.effect"
    path.write_text(
        json.dumps(
            {
                "$schema": "infernux.render_effect",
                "$version": 1,
                "feature_type": "infernux.post.bloom",
                "parameters": {"intensity": 0.5},
                "dependencies": [],
            }
        ),
        encoding="utf-8",
    )
    slot = EffectSlot(stage_id="final", effect=RenderEffectRef(path_hint=str(path)))
    raw_ref = get_raw_field_value(slot, "effect")

    effect = slot.effect
    assert effect is not None
    effect._suppress_auto_save = True
    effect.set_float("intensity", 1.5)

    assert raw_ref.resolve().get_float("intensity") == pytest.approx(1.5)
