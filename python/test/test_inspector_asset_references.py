import copy
import json

from Infernux.core.assets import AssetManager
from Infernux.core.asset_ref import RenderEffectRef
from Infernux.engine.ui._inspector_references import (
    _asset_guid_from_path,
    _portable_asset_path_hint,
)
from Infernux.renderstack.effect_slot import EffectSlot


def test_asset_guid_lookup_falls_back_to_adjacent_current_meta(monkeypatch, tmp_path):
    asset = tmp_path / "Post.effectgroup"
    asset.write_text("{}", encoding="ascii")
    (tmp_path / "Post.effectgroup.meta").write_text(
        json.dumps(
            {
                "metadata": {
                    "guid": {"type": "string", "value": "effect-guid"},
                }
            }
        ),
        encoding="utf-8",
    )

    class EmptyLookup:
        @staticmethod
        def get_guid_from_path(_path):
            return ""

    monkeypatch.setattr(AssetManager, "_asset_database", EmptyLookup())

    assert _asset_guid_from_path(str(asset)) == "effect-guid"


def test_inspector_asset_path_hint_is_project_relative(monkeypatch, tmp_path):
    from Infernux.engine import project_context

    project = tmp_path / "PortableProject"
    asset = project / "Assets" / "VFX" / "Smoke.particlegraph"
    asset.parent.mkdir(parents=True)
    asset.write_text("{}", encoding="ascii")
    monkeypatch.setattr(project_context, "_project_root", str(project))

    assert _portable_asset_path_hint(str(asset)) == "Assets/VFX/Smoke.particlegraph"


def test_serializable_object_copy_preserves_raw_asset_reference():
    slot = EffectSlot(
        stage_id="final",
        effect=RenderEffectRef(
            guid="effect-guid",
            path_hint="Assets/Rendering/Post.effectgroup",
        ),
    )

    copied = copy.deepcopy(slot)

    assert copied is not slot
    assert copied.effect_ref is not slot.effect_ref
    assert copied.effect_ref.guid == "effect-guid"
    assert copied.effect_ref.path_hint == "Assets/Rendering/Post.effectgroup"
