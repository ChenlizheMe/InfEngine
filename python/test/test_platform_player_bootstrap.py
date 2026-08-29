from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from Infernux.engine.platform_player_bootstrap import prepare_platform_player
from Infernux.engine.player_package_native import read_manifest, write_pack


def _platform_package(tmp_path: Path) -> Path:
    data_root = tmp_path / "Game_Data"
    data_root.mkdir(parents=True)
    source = tmp_path / "BuildSettings.json"
    source.write_text('{"scenes": ["Assets/Scenes/Start.scene"]}\n', encoding="utf-8")
    guid_map = tmp_path / "_script_guid_map.json"
    guid_map.write_text('{"scripts": {}}\n', encoding="utf-8")
    builtin_shader = tmp_path / "standard.vert"
    builtin_shader.write_text("void main() {}\n", encoding="utf-8")
    archive = data_root / "Content.inxpkg"
    write_pack(
        (
            ("ProjectSettings/BuildSettings.json", source),
            ("_script_guid_map.json", guid_map),
            ("Infernux/resources/shaders/standard.vert", builtin_shader),
        ),
        archive,
    )
    manifest = read_manifest(archive)
    (data_root / "PackageIndex.inxmanifest").write_text(
        "INFERNUX_PLAYER_PACKAGE_INDEX_V1\n"
        f"content\t{manifest['archive_sha256']}\t{manifest['archive_bytes']}\n",
        encoding="ascii",
    )
    (data_root / "Player.inxmanifest").write_text(
        json.dumps({"product": {"flavor": "PlayerDebug"}}) + "\n",
        encoding="utf-8",
    )
    (data_root / "BuildManifest.json").write_text(
        json.dumps({"game_name": "Game"}), encoding="utf-8"
    )
    return data_root


def test_platform_player_prepares_validated_content_cache(monkeypatch, tmp_path):
    data_root = _platform_package(tmp_path)
    cache_root = tmp_path / "cache"
    for name in (
        "_INFERNUX_PLAYER_MODE",
        "_INFERNUX_PLAYER_DATA_ROOT",
        "_INFERNUX_PLAYER_CONTENT_ARCHIVE_SHA256",
        "_INFERNUX_PLAYER_CONTENT_ARCHIVE_BYTES",
        "_INFERNUX_PLAYER_DEBUG_BUILD",
        "_INFERNUX_PLAYER_LOG",
        "PYTHONDONTWRITEBYTECODE",
    ):
        monkeypatch.setenv(name, "__pytest_restore__")

    project_root = Path(
        prepare_platform_player(str(data_root.parent), str(cache_root))
    )

    assert (project_root / "ProjectSettings/BuildSettings.json").is_file()
    assert (project_root / "_script_guid_map.json").is_file()
    assert (
        project_root / "Infernux/resources/shaders/standard.vert"
    ).read_text(encoding="utf-8") == "void main() {}\n"
    assert json.loads((project_root / "BuildManifest.json").read_text(encoding="utf-8"))[
        "game_name"
    ] == "Game"
    assert Path(prepare_platform_player(str(data_root), str(cache_root))) == project_root
    assert Path(os.environ["_INFERNUX_PLAYER_LOG"]) == cache_root / "Logs/player.log"
    assert os.environ["_INFERNUX_PLAYER_DEBUG_BUILD"] == "1"


def test_platform_player_asset_root_requires_one_cooked_data_directory(tmp_path):
    first = _platform_package(tmp_path / "first")
    second = tmp_path / "first" / "Other_Data"
    second.mkdir()
    (second / "Player.inxmanifest").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="exactly one cooked Data directory"):
        prepare_platform_player(str(first.parent), str(tmp_path / "cache"))
