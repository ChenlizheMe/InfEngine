import copy
import json

from Infernux.core.assets import AssetManager
from Infernux.core.asset_ref import RenderEffectRef
from Infernux.engine.ui._inspector_references import (
    _asset_guid_from_path,
    _is_project_asset_path,
    _portable_asset_path_hint,
    ping_asset_in_project,
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


def test_builtin_asset_is_resolvable_but_not_project_navigable(monkeypatch, tmp_path):
    from Infernux.engine import project_context

    project = tmp_path / "PortableProject"
    (project / "Assets").mkdir(parents=True)
    builtin = tmp_path / "Infernux" / "lib" / "shaders" / "standard.vert"
    builtin.parent.mkdir(parents=True)
    builtin.write_text("builtin", encoding="ascii")
    monkeypatch.setattr(project_context, "_project_root", str(project))

    assert not _is_project_asset_path(str(builtin))
    assert ping_asset_in_project(str(builtin)) is False


def test_project_asset_ping_uses_the_assets_boundary(monkeypatch, tmp_path):
    from Infernux.engine import project_context

    project = tmp_path / "PortableProject"
    asset = project / "Assets" / "Materials" / "Test.mat"
    asset.parent.mkdir(parents=True)
    asset.write_text("{}", encoding="ascii")
    monkeypatch.setattr(project_context, "_project_root", str(project))

    assert _is_project_asset_path(str(asset))


def test_generated_project_shader_is_not_revealed_outside_assets(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from Infernux.engine import project_context
    from Infernux.engine.interaction import EditorInteractionCore

    project = tmp_path / "PortableProject"
    shader = project / "Library" / "Resources" / "shaders" / "standard.vert"
    shader.parent.mkdir(parents=True)
    shader.write_text("shader", encoding="ascii")
    located = []
    core = SimpleNamespace(
        navigation=SimpleNamespace(
            locate=lambda target, **kwargs: located.append((target, kwargs)) or True
        )
    )
    monkeypatch.setattr(project_context, "_project_root", str(project))
    monkeypatch.setattr(
        EditorInteractionCore,
        "instance",
        classmethod(lambda _cls: core),
    )

    assert not _is_project_asset_path(str(shader))
    assert ping_asset_in_project(str(shader)) is False
    assert located == []


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
