import json
import zipfile
from pathlib import Path

import pytest

from hub_release import (
    build_release_artifacts,
    create_manifest,
    load_manifest,
    write_manifest,
)


def test_release_contains_one_archive_and_current_manifest(tmp_path: Path):
    hub = tmp_path / "hub"
    output = tmp_path / "release"
    (hub / "runtime").mkdir(parents=True)
    (hub / "Infernux Hub.exe").write_bytes(b"executable")
    (hub / "runtime" / "library.dll").write_bytes(b"library")

    archive_path, manifest_path = build_release_artifacts(hub, "0.4.0", output)

    assert archive_path.name == "InfernuxHub-0.4.0-windows-x64-full.zip"
    assert manifest_path.name == "InfernuxHub-manifest.json"
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {
            "Infernux Hub.exe",
            "runtime/library.dll",
        }
    assert load_manifest(manifest_path) == {
        "$schema": "infernux.hub_update",
        "product": "InfernuxHub",
        "version": "0.4.0",
        "platform": "windows-x64",
        "files": [
            {"path": "Infernux Hub.exe"},
            {"path": "runtime/library.dll"},
        ],
    }


def test_manifest_entries_are_paths(tmp_path: Path):
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "Infernux Hub.exe").write_bytes(b"executable")

    manifest = create_manifest(hub, "0.4.0")

    assert manifest["files"] == [{"path": "Infernux Hub.exe"}]


def test_manifest_rejects_parent_traversal(tmp_path: Path):
    manifest = {
        "$schema": "infernux.hub_update",
        "product": "InfernuxHub",
        "version": "0.4.0",
        "platform": "windows-x64",
        "files": [{"path": "../outside"}],
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsafe update path"):
        load_manifest(path)


def test_manifest_rejects_unknown_fields(tmp_path: Path):
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "Infernux Hub.exe").write_bytes(b"executable")
    manifest = create_manifest(hub, "0.4.0")
    manifest["files"][0]["old_field"] = True

    with pytest.raises(ValueError, match="file entry"):
        write_manifest(manifest, tmp_path / "manifest.json")
