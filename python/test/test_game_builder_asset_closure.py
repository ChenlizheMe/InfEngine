from __future__ import annotations

import json

import pytest

from Infernux.engine.game_builder import GameBuilder
from Infernux.engine.runtime_artifact_catalog import load_asset_index
from Infernux.engine.runtime_artifact_catalog import unix_ns_to_filetime_ticks


def _content_hash(payload: bytes) -> str:
    value = 14695981039346656037
    for byte in payload:
        value ^= byte
        value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def _write_asset_index(project, entries):
    current_entries = []
    for entry in entries:
        current = dict(entry)
        source = project / current["normalized_path"]
        stat = source.stat()
        current["source"] = {
            "size": stat.st_size,
            "modified_ns": unix_ns_to_filetime_ticks(stat.st_mtime_ns),
        }
        current["content_hash"] = _content_hash(source.read_bytes())
        current_entries.append(current)
    library = project / "Library"
    library.mkdir(parents=True, exist_ok=True)
    (library / "AssetIndex.json").write_text(
        json.dumps({"entries": current_entries}),
        encoding="utf-8",
    )


def _entry(guid, path, dependencies=()):
    return {
        "guid": guid,
        "normalized_path": path,
        "source": {"size": 1, "modified_ns": 1},
        "content_hash": "a" * 16,
        "dependencies": list(dependencies),
    }


def test_all_imported_assets_join_runtime_product_closure(tmp_path):
    project = tmp_path / "Project"
    assets = project / "Assets"
    (assets / "Runtime").mkdir(parents=True)
    (assets / "Main.scene").write_text("{}", encoding="utf-8")
    (assets / "Runtime" / "config.json").write_text("{}", encoding="utf-8")
    (assets / "Materials" / "Extra.mat").parent.mkdir(parents=True)
    (assets / "Materials" / "Extra.mat").write_text("{}", encoding="utf-8")
    (assets / "Unused.mat").write_text("{}", encoding="utf-8")
    (project / "ProjectSettings").mkdir(parents=True)
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )
    _write_asset_index(
        project,
        [
            _entry("scene", "Assets/Main.scene"),
            _entry("config", "Assets/Runtime/config.json", ["material"]),
            _entry("material", "Assets/Materials/Extra.mat"),
            _entry("unused", "Assets/Unused.mat"),
        ],
    )

    builder = GameBuilder(str(project), str(tmp_path / "Build"))
    selected = builder._collect_library_asset_entries(load_asset_index(str(project)))

    assert set(selected) == {"scene", "config", "material", "unused"}


def test_cook_stages_all_imported_assets_but_not_unindexed_sources(tmp_path):
    project = tmp_path / "Project"
    assets = project / "Assets"
    (assets / "Runtime").mkdir(parents=True)
    (assets / "Main.scene").write_text("{}", encoding="utf-8")
    (assets / "Runtime" / "runtime.bin").write_bytes(b"runtime")
    (assets / "Unused.mat").write_text("unused", encoding="utf-8")
    (project / "ProjectSettings").mkdir(parents=True)
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )
    _write_asset_index(
        project,
        [
            _entry("scene", "Assets/Main.scene"),
            _entry("runtime", "Assets/Runtime/runtime.bin"),
        ],
    )

    builder = GameBuilder(str(project), str(tmp_path / "Build"))
    data_dir = tmp_path / "Staged" / "Data"
    data_dir.mkdir(parents=True)
    builder._copy_cooked_assets(str(data_dir))

    assert (data_dir / "Assets" / "Main.scene").is_file()
    assert (data_dir / "Assets" / "Runtime" / "runtime.bin").is_file()
    assert not (data_dir / "Assets" / "Unused.mat").exists()


def test_cook_uses_current_assetindex_as_the_imported_assets_snapshot(tmp_path):
    project = tmp_path / "Project"
    assets = project / "Assets"
    runtime = assets / "Runtime"
    runtime.mkdir(parents=True)
    (assets / "Main.scene").write_text("{}", encoding="utf-8")
    (runtime / "indexed.bin").write_bytes(b"indexed")
    (runtime / "unindexed.bin").write_bytes(b"unindexed")
    (project / "ProjectSettings").mkdir(parents=True)
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )
    _write_asset_index(
        project,
        [
            _entry("scene", "Assets/Main.scene"),
            _entry("indexed", "Assets/Runtime/indexed.bin"),
        ],
    )
    builder = GameBuilder(str(project), str(tmp_path / "Build"))
    data_dir = tmp_path / "Staged" / "Data"

    builder._copy_cooked_assets(str(data_dir))

    assert (data_dir / "Assets" / "Runtime" / "indexed.bin").is_file()
    assert not (data_dir / "Assets" / "Runtime" / "unindexed.bin").exists()


def test_cook_rejects_build_scene_absent_from_current_assetindex(tmp_path):
    project = tmp_path / "Project"
    scene = project / "Assets" / "Main.scene"
    scene.parent.mkdir(parents=True)
    scene.write_text("{}", encoding="utf-8")
    (project / "ProjectSettings").mkdir(parents=True)
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )
    _write_asset_index(project, [])
    builder = GameBuilder(str(project), str(tmp_path / "Build"))

    with pytest.raises(RuntimeError, match="BuildSettings scene is absent"):
        builder._collect_library_asset_entries(load_asset_index(str(project)))


def test_cook_rejects_dependency_absent_from_current_assetindex(tmp_path):
    project = tmp_path / "Project"
    scene = project / "Assets" / "Main.scene"
    scene.parent.mkdir(parents=True)
    scene.write_text("{}", encoding="utf-8")
    (project / "ProjectSettings").mkdir(parents=True)
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": ["Assets/Main.scene"]}),
        encoding="utf-8",
    )
    _write_asset_index(
        project,
        [_entry("scene", "Assets/Main.scene", ["missing-material"])],
    )
    builder = GameBuilder(str(project), str(tmp_path / "Build"))

    with pytest.raises(RuntimeError, match="AssetIndex dependency is absent"):
        builder._collect_library_asset_entries(load_asset_index(str(project)))
