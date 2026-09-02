import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

import hub_updater
from hub_updater import HubUpdate, check_for_update, stage_update
from incremental_update import create_manifest


def _asset(name: str, size: int = 1) -> dict:
    return {"name": name, "browser_download_url": f"https://example.invalid/{name}", "size": size}


def test_packaged_hub_version_requires_the_current_document(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hub_updater, "is_frozen", lambda: True)
    monkeypatch.setattr(hub_updater, "get_app_dir", lambda: str(tmp_path))
    (tmp_path / "hub-version.json").write_text(
        '{"version": "0.4.0", "old_version": "0.3.7"}', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="current schema"):
        hub_updater.current_hub_version()


def test_check_prefers_exact_incremental_asset(monkeypatch):
    current = "1.0.0"
    target = "1.1.0"
    patch = f"InfernuxHub-{current}-to-{target}-windows-x64.patch.zip"
    full = f"InfernuxHub-{target}-windows-x64-full.zip"
    manifest = "InfernuxHub-manifest.json"
    checksums = f"{'1' * 64}  {patch}\n{'2' * 64}  {full}\n{'3' * 64}  {manifest}\n"
    release = {
        "tag_name": f"v{target}",
        "html_url": "https://example.invalid/release",
        "assets": [_asset("SHA256SUMS.txt"), _asset(patch), _asset(full), _asset(manifest)],
    }
    responses = iter([json.dumps(release).encode(), checksums.encode()])
    monkeypatch.setattr(hub_updater, "_request_bytes", lambda *args, **kwargs: next(responses))
    update = check_for_update(current)
    assert update is not None
    assert update.incremental is True
    assert update.asset_name == patch
    assert update.required is False


def test_check_falls_back_to_full_asset(monkeypatch):
    target = "1.1.0"
    full = f"InfernuxHub-{target}-windows-x64-full.zip"
    manifest = "InfernuxHub-manifest.json"
    checksums = f"{'2' * 64}  {full}\n{'3' * 64}  {manifest}\n"
    release = {
        "tag_name": f"v{target}",
        "assets": [_asset("SHA256SUMS.txt"), _asset(full), _asset(manifest)],
    }
    responses = iter([json.dumps(release).encode(), checksums.encode()])
    monkeypatch.setattr(hub_updater, "_request_bytes", lambda *args, **kwargs: next(responses))
    update = check_for_update("1.0.0")
    assert update is not None
    assert update.incremental is False
    assert update.asset_name == full


def test_update_into_versioned_runtime_hub_is_required(monkeypatch):
    target = "0.4.0"
    full = f"InfernuxHub-{target}-windows-x64-full.zip"
    manifest = "InfernuxHub-manifest.json"
    checksums = f"{'2' * 64}  {full}\n{'3' * 64}  {manifest}\n"
    release = {
        "tag_name": f"v{target}",
        "assets": [_asset("SHA256SUMS.txt"), _asset(full), _asset(manifest)],
    }
    responses = iter([json.dumps(release).encode(), checksums.encode()])
    monkeypatch.setattr(hub_updater, "_request_bytes", lambda *args, **kwargs: next(responses))

    update = check_for_update("0.3.7")

    assert update is not None
    assert update.required is True


def test_updates_after_runtime_catalog_migration_are_optional(monkeypatch):
    target = "0.4.1"
    full = f"InfernuxHub-{target}-windows-x64-full.zip"
    manifest = "InfernuxHub-manifest.json"
    checksums = f"{'2' * 64}  {full}\n{'3' * 64}  {manifest}\n"
    release = {
        "tag_name": f"v{target}",
        "assets": [_asset("SHA256SUMS.txt"), _asset(full), _asset(manifest)],
    }
    responses = iter([json.dumps(release).encode(), checksums.encode()])
    monkeypatch.setattr(hub_updater, "_request_bytes", lambda *args, **kwargs: next(responses))

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
    monkeypatch.setattr(hub_updater, "get_app_dir", lambda: str(tmp_path / "installed"))

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


def test_stage_full_update_verifies_every_file(tmp_path: Path, monkeypatch):
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

    update = HubUpdate(
        current_version="1.0.0",
        target_version="1.1.0",
        release_url="",
        asset_name=archive_path.name,
        asset_url="",
        sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        size=archive_path.stat().st_size,
        incremental=False,
        manifest_url="",
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(
        hub_updater,
        "_download",
        lambda _update, destination, _progress=None: shutil.copy2(archive_path, destination),
    )
    monkeypatch.setattr(hub_updater, "_request_bytes", lambda *args, **kwargs: manifest_bytes)

    staged = stage_update(update)
    assert (staged / "stage" / "Infernux Hub.exe").read_bytes() == b"new executable"
    assert (staged / "stage" / "lib.dll").read_bytes() == b"new library"
    assert (staged / "stage" / "InfernuxHub-manifest.json").read_bytes() == manifest_bytes
    metadata = json.loads((staged / "hub-update.json").read_text(encoding="utf-8"))
    assert metadata["target_version"] == "1.1.0"
    assert {entry["path"] for entry in metadata["files"]} == {
        "Infernux Hub.exe", "lib.dll", "InfernuxHub-manifest.json",
    }
