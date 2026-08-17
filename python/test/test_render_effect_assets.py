import json

import pytest

from Infernux.core.asset_ref import RenderEffectRef
from Infernux.core.assets import AssetManager
from Infernux.renderstack.effect_stage import EffectResourceContract, EffectScope, EffectStage
from Infernux.renderstack.render_effect_asset import (
    EffectAssetReference,
    RenderEffectAsset,
    RenderEffectGroupAsset,
    RenderEffectGroupEntry,
    direct_effect_dependencies,
    dump_render_effect_document,
    parse_render_effect_document,
)
from Infernux.renderstack.render_effect import EditableRenderEffectGroup


def test_effect_stage_has_stable_identity_scope_and_contract():
    stage = EffectStage(
        "opaque.toon_finish",
        EffectScope.ROUTE,
        contract=EffectResourceContract(
            inputs={"color", "depth"},
            outputs={"color"},
            capabilities={"hdr", "isolated_target"},
        ),
    )

    assert stage.display_name == "Opaque.Toon Finish"
    assert stage.stable_id == "opaque.toon_finish"
    assert stage.contract.inputs == frozenset({"color", "depth"})


@pytest.mark.parametrize("stable_id", ["", "Final", "1final", "final stage", "final-"])
def test_effect_stage_rejects_unstable_identifiers(stable_id):
    with pytest.raises(ValueError):
        EffectStage(stable_id, EffectScope.COMPOSITE)


def test_render_effect_round_trips_as_deterministic_json():
    document = RenderEffectAsset(
        feature_type="infernux.post.bloom",
        parameters={"threshold": 1.0, "intensity": 0.6},
        dependencies=(EffectAssetReference(path_hint="Assets/Materials/Bloom.mat"),),
    )

    encoded = dump_render_effect_document(document)
    decoded = parse_render_effect_document(encoded)

    assert decoded == document
    assert encoded.endswith("\n")
    assert list(json.loads(encoded)) == [
        "$schema",
        "dependencies",
        "feature_type",
        "parameters",
    ]


def test_render_effect_group_preserves_order_and_direct_dependencies():
    bloom = EffectAssetReference(guid="bloom-guid", path_hint="RenderEffects/Bloom.effect")
    tone = EffectAssetReference(guid="tone-guid", path_hint="RenderEffects/Tonemapping.effect")
    group = RenderEffectGroupAsset(
        entries=(
            RenderEffectGroupEntry("bloom", bloom, overrides={"intensity": 0.8}),
            RenderEffectGroupEntry("tonemapping", tone),
        )
    )

    restored = parse_render_effect_document(dump_render_effect_document(group))

    assert restored == group
    assert direct_effect_dependencies(restored) == (bloom, tone)


def test_editable_render_effect_group_replaces_its_strict_document(tmp_path):
    initial = RenderEffectGroupAsset()
    resource = EditableRenderEffectGroup(
        initial,
        file_path=str(tmp_path / "Post.effectgroup"),
        guid="group-guid",
    )
    document = RenderEffectGroupAsset(
        entries=(
            RenderEffectGroupEntry(
                "bloom",
                EffectAssetReference(guid="bloom-guid", path_hint="Assets/Bloom.effect"),
            ),
        )
    ).to_dict()

    assert resource.deserialize_document(document)
    assert resource.serialize_document() == document
    assert resource.entries[0].entry_id == "bloom"


def test_render_effect_reference_resolves_effect_group_as_a_live_resource(tmp_path):
    path = tmp_path / "Default Post Processing.effectgroup"
    path.write_text(
        dump_render_effect_document(RenderEffectGroupAsset()),
        encoding="utf-8",
    )

    assert AssetManager._type_from_extension(".effectgroup").__name__ == "RenderEffect"
    resource = RenderEffectRef(path_hint=str(path)).resolve()

    assert isinstance(resource, EditableRenderEffectGroup)
    assert resource.file_path == str(path)
    assert resource.entries == ()


def test_render_effect_documents_reject_unknown_fields_and_duplicate_entry_ids():
    with pytest.raises(ValueError, match="unknown"):
        parse_render_effect_document(
            {
                "$schema": "infernux.render_effect",
                "feature_type": "infernux.post.bloom",
                "parameters": {},
                "dependencies": [],
                "scope": "final",
            }
        )

    reference = EffectAssetReference(guid="same")
    with pytest.raises(ValueError, match="unique"):
        RenderEffectGroupAsset(
            entries=(
                RenderEffectGroupEntry("duplicate", reference),
                RenderEffectGroupEntry("duplicate", reference),
            )
        )


def test_render_effect_asset_does_not_encode_mount_scope():
    effect = RenderEffectAsset(feature_type="project.effects.grayscale")
    serialized = effect.to_dict()

    assert "scope" not in serialized
    assert "stage" not in serialized
    assert "queue" not in serialized


def test_direct_construction_rejects_untyped_dependency_values():
    with pytest.raises(TypeError, match="EffectAssetReference"):
        RenderEffectAsset(
            feature_type="infernux.post.bloom",
            dependencies=({"guid": "not-a-reference"},),
        )

    with pytest.raises(TypeError, match="RenderEffectGroupEntry"):
        RenderEffectGroupAsset(entries=({"entry_id": "not-an-entry"},))
