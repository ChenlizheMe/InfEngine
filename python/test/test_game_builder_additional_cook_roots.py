from __future__ import annotations

import hashlib
import json

import pytest

from Infernux.engine.game_builder import GameBuilder
from Infernux.engine.interaction.project_settings import normalize_build_settings
from Infernux.engine.runtime_artifact_catalog import load_asset_index
from Infernux.engine.runtime_artifact_catalog import unix_ns_to_filetime_ticks


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
        current["content_hash"] = hashlib.sha256(source.read_bytes()).hexdigest()
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
        "content_hash": "a" * 64,
        "dependencies": list(dependencies),
    }


def test_build_settings_normalizes_additional_cook_roots_without_legacy_aliases():
    settings = normalize_build_settings(
        {
            "scenes": ["Assets/Main.scene"],
            "additional_cook_roots": [
                "Assets/Runtime\\Configs",
                "assets/runtime/configs",
            ],
        }
    )

    assert settings["additional_cook_roots"] == ["Assets/Runtime/Configs"]
    assert "runtime_resource_groups" not in settings
    assert "additional_cook_roots_v1" not in settings


@pytest.mark.parametrize(
    "legacy_field",
    (
        "runtime_resource_groups",
        "additional_cook_roots_v1",
        "additional_cook_roots_v2",
    ),
)
def test_build_settings_rejects_legacy_runtime_root_fields(legacy_field):
    with pytest.raises(ValueError, match="unknown build settings fields"):
        normalize_build_settings(
            {
                "scenes": ["Assets/Main.scene"],
                legacy_field: ["Assets/Runtime"],
            }
        )


def test_additional_cook_root_must_exist_inside_assets(tmp_path):
    project = tmp_path / "Project"
    (project / "Assets" / "Runtime").mkdir(parents=True)
    builder = GameBuilder(
        str(project),
        str(tmp_path / "Build"),
        additional_cook_roots=["Assets/Runtime"],
    )

    assert builder._resolve_additional_cook_roots() == [
        str((project / "Assets" / "Runtime").resolve())
    ]

    with pytest.raises(ValueError, match="inside the project Assets folder"):
        builder._resolve_additional_cook_root("ProjectSettings")
    with pytest.raises(FileNotFoundError, match="does not exist"):
        builder._resolve_additional_cook_root("Assets/Missing")


def test_additional_cook_root_joins_assetindex_dependency_closure(tmp_path):
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
        json.dumps(
            {
                "scenes": ["Assets/Main.scene"],
                "additional_cook_roots": ["Assets/Runtime"],
            }
        ),
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

    assert set(selected) == {"scene", "config", "material"}


def test_cook_stages_only_assetindex_entries_from_declared_root(tmp_path):
    project = tmp_path / "Project"
    assets = project / "Assets"
    (assets / "Runtime").mkdir(parents=True)
    (assets / "Main.scene").write_text("{}", encoding="utf-8")
    (assets / "Runtime" / "runtime.bin").write_bytes(b"runtime")
    (assets / "Unused.mat").write_text("unused", encoding="utf-8")
    (project / "ProjectSettings").mkdir(parents=True)
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps(
            {
                "scenes": ["Assets/Main.scene"],
                "additional_cook_roots": ["Assets/Runtime"],
            }
        ),
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


def test_cook_rejects_unindexed_additional_root_files(tmp_path):
    project = tmp_path / "Project"
    assets = project / "Assets"
    runtime = assets / "Runtime"
    runtime.mkdir(parents=True)
    (assets / "Main.scene").write_text("{}", encoding="utf-8")
    (runtime / "indexed.bin").write_bytes(b"indexed")
    (runtime / "unindexed.bin").write_bytes(b"unindexed")
    (project / "ProjectSettings").mkdir(parents=True)
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps(
            {
                "scenes": ["Assets/Main.scene"],
                "additional_cook_roots": ["Assets/Runtime"],
            }
        ),
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

    with pytest.raises(RuntimeError, match="absent from the current"):
        builder._copy_cooked_assets(str(data_dir))


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
