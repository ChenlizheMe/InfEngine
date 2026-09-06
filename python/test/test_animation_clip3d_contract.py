from __future__ import annotations

import pytest

from Infernux.core.animation_clip3d import (
    AnimationClip3D,
    is_asset_guid_string,
    resolve_disk_path_for_guid_string,
)


def _document(guid: str) -> dict:
    return {
        "name": "Walk",
        "source_model_guid": guid,
        "source_model_path": "",
        "take_name": "Walk",
        "bind_pose_bone_names": [],
        "duration_hint": 1.0,
        "events": [],
    }


def test_animation_clip_uses_current_asset_guid_contract(tmp_path) -> None:
    guid = "a" * 32
    model = tmp_path / "Robot.fbx"
    model.write_bytes(b"model")

    class AssetDatabase:
        calls: list[str] = []

        @classmethod
        def get_path_from_guid(cls, value: str) -> str:
            cls.calls.append(value)
            return str(model)

    assert is_asset_guid_string(guid)
    assert resolve_disk_path_for_guid_string(AssetDatabase(), guid) == str(model)
    assert AssetDatabase.calls == [guid]
    assert AnimationClip3D.from_dict(_document(guid)).source_model_guid == guid


@pytest.mark.parametrize(
    "guid",
    [
        "A" * 32,
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "model-guid",
        "a" * 31,
    ],
)
def test_animation_clip_rejects_non_current_guid_forms(guid: str) -> None:
    assert not is_asset_guid_string(guid)
    with pytest.raises(ValueError, match="source_model_guid"):
        AnimationClip3D.from_dict(_document(guid))


def test_animation_clip_does_not_query_invalid_guid_forms() -> None:
    class AssetDatabase:
        @staticmethod
        def get_path_from_guid(_value: str) -> str:
            raise AssertionError("invalid GUID must not reach AssetDatabase")

    assert resolve_disk_path_for_guid_string(AssetDatabase(), "A" * 32) is None


def test_animation_clip_save_rejects_invalid_runtime_state(tmp_path) -> None:
    clip = AnimationClip3D(source_model_guid="model-guid")

    with pytest.raises(ValueError, match="source_model_guid"):
        clip.save(str(tmp_path / "invalid.animclip3d"))
