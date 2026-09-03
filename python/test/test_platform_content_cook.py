from __future__ import annotations

import json
from pathlib import Path

import pytest

from Infernux.engine.build import BuildProfile, BuildRequest
from Infernux.engine.build.host_player_export import execute_host_player_build
from Infernux.engine.platform_content_cook import (
    build_settings_for_request,
    cook_platform_content,
    read_cooked_player_icon,
)
from Infernux.engine.player_package_native import read_manifest, write_pack


def _seal_build_manifest(data: Path, source_root: Path, document: dict) -> None:
    manifest_source = source_root / "BuildManifest.json"
    manifest_source.write_text(json.dumps(document), encoding="utf-8")
    catalog_source = source_root / "RuntimeAssetCatalog.json"
    catalog_source.write_text(
        json.dumps(
            {
                "$schema": "infernux.runtime_asset_catalog",
                "player_host": {},
                "packages": [],
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    catalog = data / "AssetCatalog.inxcat"
    write_pack(
        (
            ("RuntimeAssetCatalog.json", catalog_source),
            ("BuildManifest.json", manifest_source),
        ),
        catalog,
    )
    identity = read_manifest(catalog)
    (data / "PackageIndex.inxmanifest").write_text(
        "INFERNUX_PLAYER_PACKAGE_INDEX\n"
        f"catalog\t{identity['archive_sha256']}\t{identity['archive_bytes']}\n",
        encoding="ascii",
    )


def test_cooked_player_icon_is_read_from_the_sealed_content_package(tmp_path):
    data = tmp_path / "Game_Data"
    data.mkdir()
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"configured-icon")
    write_pack((("Branding/icon.png", icon),), data / "Content.inxpkg")
    _seal_build_manifest(data, tmp_path, {"icon_path": "Branding/icon.png"})

    assert read_cooked_player_icon(data, default_icon=tmp_path / "unused.png") == b"configured-icon"


def test_cooked_player_icon_uses_explicit_default_only_when_manifest_is_empty(tmp_path):
    data = tmp_path / "Game_Data"
    data.mkdir()
    default = tmp_path / "default.png"
    default.write_bytes(b"default-icon")
    _seal_build_manifest(data, tmp_path, {"icon_path": ""})

    assert read_cooked_player_icon(data, default_icon=default) == b"default-icon"


def test_cooked_player_icon_rejects_manifest_path_escape(tmp_path):
    data = tmp_path / "Game_Data"
    data.mkdir()
    default = tmp_path / "default.png"
    default.write_bytes(b"default-icon")
    _seal_build_manifest(data, tmp_path, {"icon_path": "../icon.png"})

    with pytest.raises(ValueError, match="escapes"):
        read_cooked_player_icon(data, default_icon=default)


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


def test_platform_cook_releases_headless_catalog_host_before_builder(
    monkeypatch, tmp_path
):
    project = tmp_path / "Project"
    (project / "Assets").mkdir(parents=True)
    (project / "ProjectSettings").mkdir()
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        '{"game_name":"Project","scenes":[]}\n', encoding="utf-8"
    )
    output = tmp_path / "Cook"
    lifecycle = []

    def publish(_root):
        lifecycle.append("publish")
        return {"entries": [{"guid": "b" * 32}]}

    class _Builder:
        def __init__(self, *_args, **_kwargs):
            lifecycle.append("builder")

        def freeze_asset_index_entries(self, entries):
            assert entries == [{"guid": "b" * 32}]

        def cook_platform_content(self, package_root, **_kwargs):
            data = Path(package_root) / "Project_Data"
            data.mkdir(parents=True)
            return str(data)

    monkeypatch.setattr(
        "Infernux.engine.platform_content_cook.publish_player_asset_catalog_for_host",
        publish,
    )
    monkeypatch.setattr("Infernux.engine.platform_content_cook.GameBuilder", _Builder)

    result = cook_platform_content(
        BuildRequest(str(project), "web-wasm32", str(tmp_path / "Published")),
        output,
        platform_host={"identity": "fixture"},
    )

    assert result.data_directory == output / "Project_Data"
    assert lifecycle == ["publish", "builder"]


def test_build_request_reads_and_normalizes_project_settings_strictly(tmp_path):
    project = tmp_path / "Project"
    settings_dir = project / "ProjectSettings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "BuildSettings.json").write_text(
        json.dumps(
            {
                "display_mode": "windowed",
                "window_width": 1280,
                "window_height": 720,
                "scenes": [],
            }
        ),
        encoding="utf-8",
    )

    settings = build_settings_for_request(
        BuildRequest(str(project), "windows-x64", str(tmp_path / "Build"))
    )

    assert settings["display_mode"] == "windowed"
    assert settings["window_width"] == 1280
    assert settings["window_height"] == 720
    assert settings["window_resizable"] is True


def test_build_request_rejects_missing_or_malformed_project_settings(tmp_path):
    project = tmp_path / "Project"
    settings_dir = project / "ProjectSettings"
    settings_dir.mkdir(parents=True)
    request = BuildRequest(
        str(project), "windows-x64", str(tmp_path / "Build")
    )

    with pytest.raises(FileNotFoundError, match="BuildSettings.json"):
        build_settings_for_request(request)

    (settings_dir / "BuildSettings.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        build_settings_for_request(request)


def test_build_request_rejects_invalid_explicit_snapshot_without_disk_fallback(
    tmp_path,
):
    project = tmp_path / "Project"
    settings_dir = project / "ProjectSettings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "BuildSettings.json").write_text(
        '{"display_mode":"windowed","scenes":[]}\n', encoding="utf-8"
    )
    request = BuildRequest(
        str(project),
        "windows-x64",
        str(tmp_path / "Build"),
        BuildProfile(options={"build_settings": "not-a-document"}),
    )

    with pytest.raises(TypeError, match="must be a mapping"):
        build_settings_for_request(request)


def test_build_request_rejects_non_positive_render_dimensions(tmp_path):
    project = tmp_path / "Project"
    (project / "ProjectSettings").mkdir(parents=True)
    request = BuildRequest(
        str(project),
        "windows-x64",
        str(tmp_path / "Build"),
        BuildProfile(
            options={
                "build_settings": {
                    "window_width": 0,
                    "window_height": 720,
                    "scenes": [],
                }
            }
        ),
    )

    with pytest.raises(ValueError, match="window_width must be positive"):
        build_settings_for_request(request)


def test_host_player_uses_the_shared_authoritative_build_settings(
    monkeypatch, tmp_path
):
    project = tmp_path / "Project"
    (project / "Assets").mkdir(parents=True)
    settings_dir = project / "ProjectSettings"
    settings_dir.mkdir()
    (settings_dir / "BuildSettings.json").write_text(
        json.dumps(
            {
                "game_name": "WindowedGame",
                "display_mode": "windowed",
                "window_width": 1280,
                "window_height": 720,
                "window_resizable": False,
                "scenes": [],
            }
        ),
        encoding="utf-8",
    )
    observed = {}

    class _Builder:
        def __init__(self, project_root, output_dir, **kwargs):
            observed["project_root"] = project_root
            observed["output_dir"] = output_dir
            observed.update(kwargs)

        def freeze_asset_index_entries(self, entries):
            observed["entries"] = entries

        def _validate_output_directory(self):
            pass

        def build(self, **_kwargs):
            output = tmp_path / "Build" / "WindowedGame"
            output.mkdir(parents=True)
            return str(output)

    monkeypatch.setattr("Infernux.engine.game_builder.GameBuilder", _Builder)
    request = BuildRequest(
        str(project),
        "windows-x64",
        str(tmp_path / "Build"),
        asset_catalog_entries=({"guid": "c" * 32},),
    )

    result = execute_host_player_build(request, object())

    assert result.success
    assert observed["game_name"] == "WindowedGame"
    assert observed["display_mode"] == "windowed"
    assert observed["window_width"] == 1280
    assert observed["window_height"] == 720
    assert observed["window_resizable"] is False


def test_host_player_does_not_recreate_missing_normalized_settings(
    monkeypatch, tmp_path
):
    from Infernux.engine.interaction.project_settings import normalize_build_settings

    settings = normalize_build_settings({})
    settings.pop("display_mode")
    monkeypatch.setattr(
        "Infernux.engine.platform_content_cook.build_settings_for_request",
        lambda _request: settings,
    )
    request = BuildRequest(
        str(tmp_path / "Project"),
        "windows-x64",
        str(tmp_path / "Build"),
        asset_catalog_entries=({"guid": "d" * 32},),
    )

    with pytest.raises(KeyError, match="display_mode"):
        execute_host_player_build(request, object())


def test_platform_cook_does_not_recreate_missing_normalized_settings(
    monkeypatch, tmp_path
):
    from Infernux.engine.interaction.project_settings import normalize_build_settings

    settings = normalize_build_settings({})
    settings.pop("window_width")
    monkeypatch.setattr(
        "Infernux.engine.platform_content_cook.build_settings_for_request",
        lambda _request: settings,
    )
    monkeypatch.setattr(
        "Infernux.engine.platform_content_cook.GameBuilder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("GameBuilder must not receive an incomplete contract")
        ),
    )
    request = BuildRequest(
        str(tmp_path / "Project"),
        "web-wasm32",
        str(tmp_path / "Build"),
        asset_catalog_entries=({"guid": "e" * 32},),
    )

    with pytest.raises(KeyError, match="window_width"):
        cook_platform_content(
            request,
            tmp_path / "Cook",
            platform_host={"identity": "fixture"},
        )
