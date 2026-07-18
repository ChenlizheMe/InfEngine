import json

import pytest

from Infernux.renderstack.effect_binding import (
    EffectBindingDocument,
    EffectSlotBinding,
    dump_effect_binding_document,
    parse_effect_binding_document,
)
from Infernux.renderstack.render_effect_asset import EffectAssetReference


def test_stage_slots_round_trip_with_empty_and_missing_references():
    missing = EffectAssetReference(
        guid="deleted-effect-guid",
        path_hint="Assets/RenderEffects/Missing.effect",
    )
    document = EffectBindingDocument(
        stages={
            "final": (
                EffectSlotBinding("empty-slot"),
                EffectSlotBinding("missing-slot", missing, overrides={"intensity": 0.5}),
            )
        }
    )

    encoded = dump_effect_binding_document(document)
    restored = parse_effect_binding_document(encoded)

    assert restored == document
    assert restored.slots("final")[0].asset is None
    assert restored.slots("final")[1].asset == missing


def test_stage_order_and_slot_identity_are_preserved():
    first = EffectSlotBinding("first")
    second = EffectSlotBinding("second")
    document = EffectBindingDocument().with_stage("opaque.toon_finish", (second, first))

    restored = parse_effect_binding_document(dump_effect_binding_document(document))

    assert [slot.slot_id for slot in restored.slots("opaque.toon_finish")] == ["second", "first"]


def test_duplicate_slot_ids_are_rejected_across_stages():
    with pytest.raises(ValueError, match="duplicate"):
        EffectBindingDocument(
            stages={
                "final": (EffectSlotBinding("same"),),
                "after_sky": (EffectSlotBinding("same"),),
            }
        )


def test_binding_document_rejects_unknown_fields_and_non_finite_overrides():
    raw = {
        "$schema": "infernux.render_stack_effect_bindings",
        "$version": 1,
        "stages": {},
        "topology": [],
    }
    with pytest.raises(ValueError, match="unknown"):
        parse_effect_binding_document(raw)

    with pytest.raises(TypeError, match="finite JSON"):
        EffectSlotBinding("bad", overrides={"value": float("nan")})


def test_compact_scene_encoding_is_deterministic():
    document = EffectBindingDocument(stages={"final": (EffectSlotBinding("slot"),)})
    encoded = dump_effect_binding_document(document)

    assert "\n" not in encoded
    assert json.loads(encoded)["stages"]["final"][0]["slot_id"] == "slot"
