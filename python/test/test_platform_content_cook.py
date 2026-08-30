from __future__ import annotations

from pathlib import Path

from Infernux.engine.build import BuildRequest
from Infernux.engine.platform_content_cook import cook_platform_content


def test_platform_cook_consumes_editor_catalog_snapshot_without_rescanning(
    monkeypatch, tmp_path
):
    project = tmp_path / "Project"
    (project / "Assets").mkdir(parents=True)
    (project / "ProjectSettings").mkdir()
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        '{"game_name":"SnapshotGame","scenes":["Assets/Main.scene"]}\n',
        encoding="utf-8",
    )
    output = tmp_path / "Cook"
    captured = {}

    class _Builder:
        def __init__(self, *_args, **_kwargs):
            pass

        def freeze_asset_index_entries(self, entries):
            captured["entries"] = entries

        def cook_platform_content(self, package_root, **_kwargs):
            data = Path(package_root) / "SnapshotGame_Data"
            data.mkdir(parents=True)
            return str(data)

    monkeypatch.setattr("Infernux.engine.platform_content_cook.GameBuilder", _Builder)
    monkeypatch.setattr(
        "Infernux.engine.platform_content_cook.publish_player_asset_catalog_for_host",
        lambda _root: (_ for _ in ()).throw(AssertionError("unexpected rescan")),
    )
    request = BuildRequest(
        str(project),
        "web-wasm32",
        str(tmp_path / "Published"),
        asset_catalog_entries=({"guid": "a" * 32},),
    )

    result = cook_platform_content(
        request,
        output,
        platform_host={
            "identity": "fixture",
            "entry_point": "index.html",
            "platform": "web",
            "architecture": "wasm32",
        },
    )

    assert result.game_name == "SnapshotGame"
    assert captured["entries"] == [{"guid": "a" * 32}]
    assert result.data_directory == output / "SnapshotGame_Data"
