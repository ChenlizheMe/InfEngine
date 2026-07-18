import json

import pytest

from Infernux.components.serialized_field import FieldType, get_raw_field_value, get_serialized_fields
from Infernux.core.asset_ref import RenderEffectRef
from Infernux.renderstack.effect_slot import EffectSlot
from Infernux.renderstack.render_effect import RenderEffect
from Infernux.renderstack.render_effect_asset import (
    EffectAssetReference,
    RenderEffectAsset,
    RenderEffectGroupAsset,
    RenderEffectGroupEntry,
    dump_render_effect_document,
)
from Infernux.renderstack.render_effect_compiler import (
    RenderEffectCompileError,
    expand_render_effect_reference,
)
from Infernux.renderstack.render_stack import RenderStack
from Infernux.renderstack.render_pipeline import RenderPipeline


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


def test_default_pipeline_declares_effect_stages_in_topology_order():
    stack = RenderStack()

    assert [stage.stable_id for stage in stack.effect_stages] == [
        "after_opaque",
        "after_sky",
        "after_transparent",
        "final",
    ]

    topology = stack._build_full_topology_probe().topology_sequence
    assert topology.index(("effect_stage", "final")) < topology.index(
        ("pass", "_ScreenUI_Camera")
    )


def test_render_stack_rejects_undeclared_stage_but_preserves_orphan_slots():
    stack = RenderStack()
    orphan = EffectSlot(stage_id="removed_stage")
    stack.effect_slots = [orphan]

    assert stack.orphan_effect_slots == (orphan,)
    with pytest.raises(ValueError, match="does not declare EffectStage"):
        stack.add_effect_slot("removed_stage")
    assert stack.effect_slots == [orphan]


def test_render_stack_canonicalizes_declared_stage_aliases():
    class RenamedStagePipeline(RenderPipeline):
        name = "Renamed Stage"

        def define_topology(self, graph):
            graph.create_texture("color", camera_target=True)
            with graph.add_pass("Opaque") as render_pass:
                render_pass.write_color("color")
                render_pass.draw_renderers()
            graph.effects("final", aliases=("old_final",))
            graph.set_output("color")

    stack = RenderStack()
    stack._pipeline = RenamedStagePipeline()
    slot = EffectSlot(stage_id="old_final")
    stack.effect_slots = [slot]

    stack._canonicalize_effect_stage_aliases()

    assert slot.stage_id == "final"
    assert stack.get_effect_stage_slots("old_final") == (slot,)
    assert stack.orphan_effect_slots == ()


def test_render_stack_can_explicitly_remap_preserved_orphan_slots():
    stack = RenderStack()
    first = EffectSlot(stage_id="removed_stage")
    second = EffectSlot(stage_id="removed_stage")
    stack.effect_slots = [first, second]

    assert stack.remap_orphan_effect_stage("removed_stage", "final") == 2
    assert stack.get_effect_stage_slots("final") == (first, second)
    assert stack.orphan_effect_slots == ()


def test_effect_stage_inspector_adapter_routes_list_edits_to_render_stack():
    from Infernux.engine.ui.inspector_renderstack import _EffectStageSlotsAdapter

    stack = RenderStack()
    adapter = _EffectStageSlotsAdapter(stack, "final")
    first = EffectSlot(stage_id="")
    second = EffectSlot(stage_id="")

    adapter.slots = [first, second]
    assert first.stage_id == "final"
    assert second.stage_id == "final"
    assert adapter.slots == [first, second]

    adapter.slots = [second]
    assert stack.get_effect_stage_slots("final") == (second,)


def test_render_effect_picker_accepts_effect_groups():
    from Infernux.core.asset_ref import get_asset_type_config

    config = get_asset_type_config("RenderEffect")
    assert config["extensions"] == ("*.effect", "*.effectgroup", "*.effectstack")


def test_render_stack_compiles_effect_stage_to_scoped_dynamic_blocks():
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.tonemapping",
            parameters={"exposure": 1.25},
        )
    )
    stack = RenderStack()
    slot = stack.add_effect_slot("final", RenderEffectRef(effect=effect))

    description = stack.build_graph()
    effect_pass = next(
        render_pass
        for render_pass in description.passes
        if render_pass.name.endswith("ToneMap_Apply")
    )
    command = effect_pass.commands[0]

    assert f"final/{slot.slot_id}/0" in effect_pass.name
    assert command.parameter_block.startswith(f"effect/final/{slot.slot_id}/0/")
    assert dict(command.push_constants)["exposure"] == pytest.approx(1.25)
    assert any(render_pass.name.endswith("final/Commit") for render_pass in description.passes)
    assert stack.effect_compile_errors == ()


def test_render_stack_batches_only_changed_effect_parameters_without_rebuild():
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.tonemapping",
            parameters={"exposure": 1.0},
        )
    )
    stack = RenderStack()
    stack.add_effect_slot("final", RenderEffectRef(effect=effect))
    stack._graph_desc = stack.build_graph()

    class Context:
        graph_instance_id = 17

    requires_rebuild, initial = stack._collect_effect_parameter_updates(Context())
    assert requires_rebuild is False
    assert initial
    assert stack._collect_effect_parameter_updates(Context()) == (False, [])

    effect.set_float("exposure", 2.0)
    requires_rebuild, updates = stack._collect_effect_parameter_updates(Context())

    assert requires_rebuild is False
    assert any(dict(update.values).get("exposure") == 2.0 for update in updates)


def test_render_stack_rebuilds_when_effect_topology_parameter_changes():
    effect = RenderEffect(
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            parameters={"max_iterations": 2},
        )
    )
    stack = RenderStack()
    stack.add_effect_slot("final", RenderEffectRef(effect=effect))
    stack._graph_desc = stack.build_graph()

    class Context:
        graph_instance_id = 23

    assert stack._collect_effect_parameter_updates(Context())[0] is False
    effect.set_int("max_iterations", 3)
    assert stack._collect_effect_parameter_updates(Context()) == (True, [])


def test_effect_group_expands_in_order_with_non_destructive_overrides(tmp_path):
    bloom_path = tmp_path / "Bloom.effect"
    tone_path = tmp_path / "Tone.effect"
    bloom_path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.post.bloom",
                parameters={"intensity": 0.5, "max_iterations": 2},
            )
        ),
        encoding="utf-8",
    )
    tone_path.write_text(
        dump_render_effect_document(
            RenderEffectAsset(
                feature_type="infernux.post.tonemapping",
                parameters={"exposure": 1.0},
            )
        ),
        encoding="utf-8",
    )
    group_path = tmp_path / "Post.effectgroup"
    group_path.write_text(
        dump_render_effect_document(
            RenderEffectGroupAsset(
                entries=(
                    RenderEffectGroupEntry(
                        "bloom",
                        EffectAssetReference(path_hint=bloom_path.name),
                        overrides={"intensity": 0.9},
                    ),
                    RenderEffectGroupEntry(
                        "tone",
                        EffectAssetReference(path_hint=tone_path.name),
                    ),
                )
            )
        ),
        encoding="utf-8",
    )

    effects = expand_render_effect_reference(RenderEffectRef(path_hint=str(group_path)))

    assert [effect.feature_type for effect in effects] == [
        "infernux.post.bloom",
        "infernux.post.tonemapping",
    ]
    assert effects[0].get_float("intensity") == pytest.approx(0.9)


def test_effect_group_cycle_is_rejected(tmp_path):
    first = tmp_path / "First.effectgroup"
    second = tmp_path / "Second.effectgroup"
    first.write_text(
        dump_render_effect_document(
            RenderEffectGroupAsset(
                entries=(
                    RenderEffectGroupEntry(
                        "second",
                        EffectAssetReference(path_hint=second.name),
                    ),
                )
            )
        ),
        encoding="utf-8",
    )
    second.write_text(
        dump_render_effect_document(
            RenderEffectGroupAsset(
                entries=(
                    RenderEffectGroupEntry(
                        "first",
                        EffectAssetReference(path_hint=first.name),
                    ),
                )
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(RenderEffectCompileError, match="cycle"):
        expand_render_effect_reference(RenderEffectRef(path_hint=str(first)))


def test_failed_effect_compile_rolls_back_partial_graph_mutation():
    from Infernux.renderstack.fullscreen_effect import FullScreenEffect
    from Infernux.renderstack.render_effect_compiler import register_render_effect_feature

    class BrokenEffect(FullScreenEffect):
        name = "Broken Test Effect"
        injection_point = "after_post_process"
        default_order = 1

        def setup_passes(self, graph, bus):
            graph.create_texture("partial")
            with graph.add_pass("Partial") as render_pass:
                render_pass.write_color("partial")
                render_pass.fullscreen_quad("fullscreen_blit")
            raise ValueError("intentional compile failure")

    register_render_effect_feature("tests.post.broken", BrokenEffect)
    stack = RenderStack()
    stack.add_effect_slot(
        "final",
        RenderEffectRef(effect=RenderEffect(RenderEffectAsset("tests.post.broken"))),
    )

    description = stack.build_graph()

    assert not any("Partial" in render_pass.name for render_pass in description.passes)
    assert any("intentional compile failure" in error for error in stack.effect_compile_errors)
