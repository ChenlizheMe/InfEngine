import json
import shutil
import zipfile
from pathlib import Path

import pytest

import hub_updater
from hub_release import create_manifest, write_manifest
from hub_updater import HubUpdate, check_for_update, stage_update


def _asset(name: str, size: int = 1) -> dict:
    return {
        "name": name,
        "browser_download_url": f"https://example.invalid/{name}",
        "size": size,
    }


def _release(target: str) -> dict:
    full = f"InfernuxHub-{target}-windows-x64-full.zip"
    return {
        "tag_name": f"v{target}",
        "html_url": "https://example.invalid/release",
        "assets": [_asset(full), _asset("InfernuxHub-manifest.json")],
    }


def test_packaged_hub_version_requires_the_current_document(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(hub_updater, "is_frozen", lambda: True)
    monkeypatch.setattr(hub_updater, "get_app_dir", lambda: str(tmp_path))
    (tmp_path / "hub-version.json").write_text(
        '{"version": "0.4.0", "old_version": "0.3.7"}', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="current schema"):
        hub_updater.current_hub_version()


def test_check_selects_the_standalone_update(monkeypatch):
    release = _release("1.1.0")
    monkeypatch.setattr(
        hub_updater,
        "_request_bytes",
        lambda *_args, **_kwargs: json.dumps(release).encode(),
    )

    update = check_for_update("1.0.0")

    assert update is not None
    assert update.asset_name == "InfernuxHub-1.1.0-windows-x64-full.zip"
    assert update.asset_url.endswith(update.asset_name)
    assert update.manifest_url.endswith("InfernuxHub-manifest.json")
    assert update.required is False


def test_check_requires_the_current_release_pair(monkeypatch):
    release = _release("1.1.0")
    release["assets"] = [release["assets"][0]]
    monkeypatch.setattr(
        hub_updater,
        "_request_bytes",
        lambda *_args, **_kwargs: json.dumps(release).encode(),
    )

    assert check_for_update("1.0.0") is None


def test_update_into_versioned_runtime_hub_is_required(monkeypatch):
    release = _release("0.4.0")
    monkeypatch.setattr(
        hub_updater,
        "_request_bytes",
        lambda *_args, **_kwargs: json.dumps(release).encode(),
    )

    update = check_for_update("0.3.7")

    assert update is not None
    assert update.required is True


def test_updates_after_runtime_catalog_migration_are_optional(monkeypatch):
    release = _release("0.4.1")
    monkeypatch.setattr(
        hub_updater,
        "_request_bytes",
        lambda *_args, **_kwargs: json.dumps(release).encode(),
    )

    update = check_for_update("0.4.0")

    assert update is not None
    assert update.required is False


def test_packaged_updater_requests_elevation(tmp_path: Path, monkeypatch):
    staged = tmp_path / "update"
    (staged / "stage").mkdir(parents=True)
    (staged / "hub-update.json").write_text("{}", encoding="utf-8")
    observed = {}

    monkeypatch.setattr(hub_updater.sys, "platform", "win32")
    monkeypatch.setattr(hub_updater, "is_frozen", lambda: True)
    monkeypatch.setattr(
        hub_updater, "get_app_dir", lambda: str(tmp_path / "installed")
    )

    def launch(script, arguments, working_directory):
        observed["script"] = script
        observed["arguments"] = arguments
        observed["working_directory"] = working_directory

    monkeypatch.setattr(hub_updater, "_launch_elevated_powershell", launch)

    hub_updater.launch_external_updater(staged)

    assert observed["script"] == staged / "apply-update.ps1"
    assert observed["working_directory"] == staged
    assert "-InstallDir" in observed["arguments"]
    assert "-StageDir" in observed["arguments"]
    assert "-MetadataPath" in observed["arguments"]


def test_stage_update_uses_the_archive_and_current_manifests(
    tmp_path: Path, monkeypatch
):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "Infernux Hub.exe").write_bytes(b"new executable")
    (payload / "lib.dll").write_bytes(b"new library")
    manifest = create_manifest(payload, "1.1.0")
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
    archive_path = tmp_path / "full.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for file in payload.iterdir():
            archive.write(file, file.name)

    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "Infernux Hub.exe").write_bytes(b"old executable")
    (installed / "removed.dll").write_bytes(b"removed")
    write_manifest(
        create_manifest(installed, "1.0.0"),
        installed / "InfernuxHub-manifest.json",
    )
    update = HubUpdate(
        current_version="1.0.0",
        target_version="1.1.0",
        release_url="",
        asset_name=archive_path.name,
        asset_url="",
        size=archive_path.stat().st_size,
        manifest_url="",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(hub_updater, "get_app_dir", lambda: str(installed))
    monkeypatch.setattr(
        hub_updater,
        "_download",
        lambda _update, destination, _progress=None: shutil.copy2(
            archive_path, destination
        ),
    )
    monkeypatch.setattr(
        hub_updater, "_request_bytes", lambda *_args, **_kwargs: manifest_bytes
    )

    staged = stage_update(update)

    assert (staged / "stage" / "Infernux Hub.exe").read_bytes() == b"new executable"
    assert (staged / "stage" / "lib.dll").read_bytes() == b"new library"
    assert (
        staged / "stage" / "InfernuxHub-manifest.json"
    ).read_bytes() == manifest_bytes
    metadata = json.loads(
        (staged / "hub-update.json").read_text(encoding="utf-8")
    )
    assert metadata["target_version"] == "1.1.0"
    assert metadata["delete"] == ["removed.dll"]
    assert {entry["path"] for entry in metadata["files"]} == {
        "Infernux Hub.exe",
        "lib.dll",
        "InfernuxHub-manifest.json",
    }


def test_stage_update_rejects_an_unowned_archive_member(tmp_path: Path, monkeypatch):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "Infernux Hub.exe").write_bytes(b"new executable")
    manifest = create_manifest(payload, "1.1.0")
    manifest_bytes = json.dumps(manifest).encode()
    archive_path = tmp_path / "full.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(payload / "Infernux Hub.exe", "Infernux Hub.exe")
        archive.writestr("unexpected.dll", b"unexpected")

    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "Infernux Hub.exe").write_bytes(b"old executable")
    write_manifest(
        create_manifest(installed, "1.0.0"),
        installed / "InfernuxHub-manifest.json",
    )
    update = HubUpdate(
        current_version="1.0.0",
        target_version="1.1.0",
        release_url="",
        asset_name=archive_path.name,
        asset_url="",
        size=archive_path.stat().st_size,
        manifest_url="",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(hub_updater, "get_app_dir", lambda: str(installed))
    monkeypatch.setattr(
        hub_updater,
        "_download",
        lambda _update, destination, _progress=None: shutil.copy2(
            archive_path, destination
        ),
    )
    monkeypatch.setattr(
        hub_updater, "_request_bytes", lambda *_args, **_kwargs: manifest_bytes
    )

    with pytest.raises(ValueError, match="does not match its manifest"):
        stage_update(update)
