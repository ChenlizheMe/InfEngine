from __future__ import annotations

import json

import pytest

from Infernux.core.animation_clip import AnimationClip, AnimationFrame
from Infernux.core.asset_types import SpriteFrame, TextureImportSettings, TextureType
from Infernux.mcp.tools import assets as assets_module


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        name = str(kwargs.get("name") or (args[0] if args else ""))

        def _register(fn):
            self.tools[name] = fn
            return fn

        return _register


class _AssetDatabase:
    def __init__(self, mappings: dict[str, tuple[str, str]]) -> None:
        self._by_guid = {guid: path for guid, (path, _type) in mappings.items()}
        self._by_path = {path: guid for guid, (path, _type) in mappings.items()}

    def get_path_from_guid(self, guid: str) -> str:
        return self._by_guid.get(guid, "")

    def get_guid_from_path(self, path: str) -> str:
        return self._by_path.get(path, "")


def _write_texture_meta(path, frames: list[SpriteFrame]) -> None:
    settings = TextureImportSettings(texture_type=TextureType.SPRITE, sprite_frames=frames)
    metadata = {}
    for key, value in settings.to_dict().items():
        if isinstance(value, bool):
            tag = "bool"
        elif isinstance(value, int):
            tag = "int"
        elif isinstance(value, list):
            tag = "json_array"
        else:
            tag = "string"
        metadata[key] = {"type": tag, "value": value}
    path.with_name(path.name + ".meta").write_text(
        json.dumps({"metadata": metadata}), encoding="utf-8"
    )


@pytest.fixture
def sprite_asset_tools(tmp_path, monkeypatch):
    assets = tmp_path / "Assets"
    assets.mkdir()
    texture = assets / "characters.png"
    texture.write_bytes(b"texture")
    clip_path = assets / "walk.animclip2d"
    texture_guid = "1" * 32
    clip_guid = "2" * 32
    database = _AssetDatabase({
        texture_guid: (str(texture.resolve()), "TEXTURE"),
        clip_guid: (str(clip_path.resolve()), "ANIMATION_CLIP_2D"),
    })
    monkeypatch.setattr(assets_module, "get_asset_database", lambda: database)
    monkeypatch.setattr(
        assets_module,
        "main_thread",
        lambda _operation, callback, **_kwargs: callback(),
    )
    mcp = _FakeMcp()
    assets_module.register_asset_tools(mcp, str(tmp_path))
    return mcp.tools, texture, clip_path, texture_guid, clip_guid


def test_sprite_frame_ids_round_trip_through_animation_clip_contract(sprite_asset_tools):
    tools, texture, clip_path, texture_guid, clip_guid = sprite_asset_tools
    first = SpriteFrame(stable_id="a" * 32, name="Idle", x=0, y=0, w=32, h=32)
    second = SpriteFrame(stable_id="b" * 32, name="Step", x=32, y=0, w=32, h=32)
    _write_texture_meta(texture, [first, second])
    occurrences = [
        AnimationFrame(stable_id="c" * 32, sprite_frame_id=second.stable_id),
        AnimationFrame(stable_id="d" * 32, sprite_frame_id=first.stable_id),
    ]
    clip = AnimationClip(
        name="Walk",
        authoring_texture_guid=texture_guid,
        authoring_texture_path="Assets/characters.png",
        frames=occurrences,
    )
    clip_path.write_text(json.dumps(clip.to_dict()), encoding="utf-8")

    listed = tools["asset_list_sprite_frames"](guid=texture_guid)
    inspected = tools["asset_inspect_animation_clip_2d"](guid=clip_guid)

    assert listed["identity_field"] == "sprite_frame_id"
    assert [frame["sprite_frame_id"] for frame in listed["frames"]] == [
        first.stable_id,
        second.stable_id,
    ]
    assert [frame["animation_frame_id"] for frame in inspected["frames"]] == [
        occurrences[0].stable_id,
        occurrences[1].stable_id,
    ]
    assert [frame["sprite_frame_id"] for frame in inspected["frames"]] == [
        second.stable_id,
        first.stable_id,
    ]
    assert [frame["source_frame"]["sprite_frame_id"] for frame in inspected["frames"]] == [
        second.stable_id,
        first.stable_id,
    ]
    assert inspected["valid"] is True
    assert inspected["diagnostics"] == []

    _write_texture_meta(texture, [second, first])
    reordered = tools["asset_inspect_animation_clip_2d"](path="Assets/walk.animclip2d")
    assert [frame["sprite_frame_id"] for frame in reordered["frames"]] == [
        second.stable_id,
        first.stable_id,
    ]
    assert reordered["frames"][0]["source_frame"]["index"] == 0
    assert reordered["frames"][1]["source_frame"]["index"] == 1


def test_animation_clip_contract_reports_missing_sprite_frame_subresource(sprite_asset_tools):
    tools, texture, clip_path, texture_guid, _clip_guid = sprite_asset_tools
    available = SpriteFrame(stable_id="a" * 32, name="Available", w=32, h=32)
    missing_id = "f" * 32
    occurrence_id = "e" * 32
    _write_texture_meta(texture, [available])
    clip = AnimationClip(
        name="Broken",
        authoring_texture_guid=texture_guid,
        authoring_texture_path="Assets/characters.png",
        frames=[AnimationFrame(stable_id=occurrence_id, sprite_frame_id=missing_id)],
    )
    clip_path.write_text(json.dumps(clip.to_dict()), encoding="utf-8")

    inspected = tools["asset_inspect_animation_clip_2d"](
        path="Assets/walk.animclip2d"
    )

    assert inspected["valid"] is False
    assert inspected["missing_sprite_frame_ids"] == [missing_id]
    assert inspected["frames"] == [{
        "animation_frame_id": occurrence_id,
        "sprite_frame_id": missing_id,
        "index": 0,
        "resolved": False,
        "source_frame": None,
    }]
    missing = [
        item for item in inspected["diagnostics"]
        if item["code"] == "sprite_frame_missing"
    ]
    assert missing == [{
        "severity": "error",
        "code": "sprite_frame_missing",
        "message": "Animation frame references a missing SpriteFrame subresource.",
        "animation_frame_id": occurrence_id,
        "sprite_frame_id": missing_id,
        "index": 0,
    }]
