from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from Infernux.engine.platform_player_bootstrap import prepare_platform_player
from Infernux.engine.player_package_native import read_manifest, write_pack


_PLAYER_ENVIRONMENT = (
    "_INFERNUX_PLAYER_MODE",
    "_INFERNUX_PLAYER_DATA_ROOT",
    "_INFERNUX_PLAYER_PERSISTENT_DATA_ROOT",
    "_INFERNUX_PLAYER_CONTENT_ARCHIVE_SHA256",
    "_INFERNUX_PLAYER_CONTENT_ARCHIVE_BYTES",
    "_INFERNUX_PLAYER_DEBUG_BUILD",
    "_INFERNUX_PLAYER_LOG",
    "PYTHONDONTWRITEBYTECODE",
)


@pytest.fixture(autouse=True)
def _restore_player_environment(monkeypatch):
    """Keep platform bootstrap environment writes inside each test."""
    for name in _PLAYER_ENVIRONMENT:
        monkeypatch.setenv(name, "__pytest_restore__")
        monkeypatch.delenv(name)


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
    catalog_source = tmp_path / "RuntimeAssetCatalog.json"
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
    build_manifest_source = tmp_path / "BuildManifest.json"
    build_manifest_source.write_text(
        json.dumps({"game_name": "Game"}), encoding="utf-8"
    )
    catalog_archive = data_root / "AssetCatalog.inxcat"
    write_pack(
        (
            ("RuntimeAssetCatalog.json", catalog_source),
            ("BuildManifest.json", build_manifest_source),
        ),
        catalog_archive,
    )
    catalog_manifest = read_manifest(catalog_archive)
    (data_root / "PackageIndex.inxmanifest").write_text(
        "INFERNUX_PLAYER_PACKAGE_INDEX\n"
        f"content\t{manifest['archive_sha256']}\t{manifest['archive_bytes']}\n"
        f"catalog\t{catalog_manifest['archive_sha256']}\t"
        f"{catalog_manifest['archive_bytes']}\n",
        encoding="ascii",
    )
    (data_root / "Player.inxmanifest").write_text(
        json.dumps({"product": {"flavor": "PlayerDebug"}}) + "\n",
        encoding="utf-8",
    )
    return data_root


@pytest.mark.parametrize("flavor,debug_flag", [("PlayerDebug", "1"), ("PlayerRelease", "0")])
def test_platform_player_prepares_validated_content_cache(monkeypatch, tmp_path, flavor, debug_flag):
    data_root = _platform_package(tmp_path)
    (data_root / "Player.inxmanifest").write_text(
        json.dumps({"product": {"flavor": flavor}}), encoding="utf-8"
    )
    monkeypatch.setenv("_INFERNUX_PLAYER_DEBUG_BUILD", "0" if debug_flag == "1" else "1")
    cache_root = tmp_path / "cache"

    project_root = Path(
        prepare_platform_player(str(data_root), str(cache_root))
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
    assert Path(os.environ["_INFERNUX_PLAYER_PERSISTENT_DATA_ROOT"]) == tmp_path / "Data"
    assert (tmp_path / "Data").is_dir()
    assert os.environ["_INFERNUX_PLAYER_DEBUG_BUILD"] == debug_flag


def test_platform_player_keeps_only_the_active_content_generation(tmp_path):
    data_root = _platform_package(tmp_path)
    cache_root = tmp_path / "cache"
    active = Path(prepare_platform_player(str(data_root), str(cache_root)))
    now = time.time_ns()
    stale_generations = []
    for index in range(3):
        generation = cache_root / f"content-{index + 1:024x}"
        generation.mkdir()
        (generation / ".ready").write_text("f" * 64 + "\n", encoding="ascii")
        timestamp = now - ((index + 1) * 1_000_000_000)
        os.utime(generation, ns=(timestamp, timestamp))
        stale_generations.append(generation)
    incomplete = cache_root / f"content-{'a' * 24}"
    incomplete.mkdir()
    unrelated = cache_root / "content-user-data"
    unrelated.mkdir()

    assert Path(prepare_platform_player(str(data_root), str(cache_root))) == active

    assert active.is_dir()
    assert not stale_generations[0].exists()
    assert not stale_generations[1].exists()
    assert not stale_generations[2].exists()
    assert not incomplete.exists()
    assert unrelated.is_dir()


def test_platform_player_asset_root_must_be_the_cooked_data_directory(tmp_path):
    first = _platform_package(tmp_path / "first")
    second = tmp_path / "first" / "Other_Data"
    second.mkdir()
    (second / "Player.inxmanifest").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="must be the cooked Data directory"):
        prepare_platform_player(str(first.parent), str(tmp_path / "cache"))
