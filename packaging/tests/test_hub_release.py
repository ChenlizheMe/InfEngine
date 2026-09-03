import json
import zipfile
from pathlib import Path

import pytest

from hub_release import (
    build_release_artifacts,
    create_manifest,
    host_platform_id,
    load_manifest,
    manifest_asset_name,
    write_manifest,
)


def test_release_contains_one_archive_and_current_manifest(tmp_path: Path):
    hub = tmp_path / "hub"
    output = tmp_path / "release"
    (hub / "runtime").mkdir(parents=True)
    (hub / "Infernux Hub.exe").write_bytes(b"executable")
    (hub / "runtime" / "library.dll").write_bytes(b"library")

    archive_path, manifest_path = build_release_artifacts(
        hub, "0.4.0", output, "windows-x64"
    )

    assert archive_path.name == "InfernuxHub-0.4.0-windows-x64-full.zip"
    assert manifest_path.name == "InfernuxHub-windows-x64-manifest.json"
    installed_manifest = hub / "InfernuxHub-windows-x64-manifest.json"
    assert installed_manifest.read_bytes() == manifest_path.read_bytes()
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {
            "Infernux Hub.exe",
            "runtime/library.dll",
        }
        assert "InfernuxHub-windows-x64-manifest.json" not in archive.namelist()
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

    manifest = create_manifest(hub, "0.4.0", "windows-x64")

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
    manifest = create_manifest(hub, "0.4.0", "windows-x64")
    manifest["files"][0]["old_field"] = True

    with pytest.raises(ValueError, match="file entry"):
        write_manifest(manifest, tmp_path / "manifest.json")


def test_linux_release_uses_platform_scoped_assets(tmp_path: Path):
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "Infernux Hub").write_bytes(b"executable")

    archive, manifest = build_release_artifacts(
        hub, "0.4.0", tmp_path / "release", "linux-x64"
    )

    assert archive.name == "InfernuxHub-0.4.0-linux-x64-full.zip"
    assert manifest.name == "InfernuxHub-linux-x64-manifest.json"
    assert load_manifest(manifest)["platform"] == "linux-x64"
    assert (hub / manifest.name).is_file()


def test_existing_release_manifest_is_not_application_payload(tmp_path: Path):
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "Infernux Hub").write_bytes(b"executable")
    (hub / "InfernuxHub-windows-x64-manifest.json").write_text(
        "stale", encoding="utf-8"
    )

    archive, manifest = build_release_artifacts(
        hub, "0.4.0", tmp_path / "release", "linux-x64"
    )

    assert load_manifest(manifest)["files"] == [{"path": "Infernux Hub"}]
    with zipfile.ZipFile(archive) as release_archive:
        assert release_archive.namelist() == ["Infernux Hub"]


def test_host_platform_id_is_an_exact_os_and_architecture_contract():
    assert host_platform_id(system="Windows", machine="AMD64") == "windows-x64"
    assert host_platform_id(system="Linux", machine="x86_64") == "linux-x64"
    assert manifest_asset_name("linux-x64") == "InfernuxHub-linux-x64-manifest.json"
    with pytest.raises(RuntimeError, match="architecture"):
        host_platform_id(system="Linux", machine="aarch64")
    with pytest.raises(RuntimeError, match="host system"):
        host_platform_id(system="Darwin", machine="x86_64")
