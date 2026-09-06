"""Runtime update discovery and staging for the packaged Infernux Hub."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable

from hub_utils import get_app_dir, is_frozen
from hub_release import (
    MANIFEST_SCHEMA,
    PRODUCT_NAME,
    host_platform_id,
    load_manifest,
    manifest_asset_name,
    validate_manifest,
)
from style import StyleManager


HUB_CATALOG_URL = "https://infernux-engine.com/hub-catalog.json"
HUB_CATALOG_SCHEMA = "infernux.hub_catalog"
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class HubUpdateStatus(str, Enum):
    UP_TO_DATE = "up-to-date"
    UPDATE_AVAILABLE = "update-available"
    UNSUPPORTED_CURRENT_VERSION = "unsupported-current-version"
    NETWORK_UNAVAILABLE = "network-unavailable"
    CATALOG_INVALID = "catalog-invalid"


@dataclass(frozen=True)
class HubUpdate:
    current_version: str
    target_version: str
    release_url: str
    asset_name: str
    asset_url: str
    size: int
    manifest_url: str
    platform: str


@dataclass(frozen=True)
class HubUpdateCheck:
    status: HubUpdateStatus
    current_version: str
    latest_version: str = ""
    update: HubUpdate | None = None
    installer_url: str = ""
    detail: str = ""


def _version_key(value: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", value)
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def current_hub_version() -> str:
    if is_frozen():
        candidate = Path(get_app_dir()) / "hub-version.json"
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"version"}:
            raise ValueError("hub-version.json does not match the current schema")
        version = str(payload["version"])
    else:
        candidate = Path(__file__).resolve().parents[1] / "pyproject.toml"
        version_lines = [
            line.split("=", 1)[1].strip().strip('"')
            for line in candidate.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("version =")
        ]
        if len(version_lines) != 1:
            raise ValueError("pyproject.toml must declare exactly one Hub version")
        version = version_lines[0]
    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"Invalid Infernux Hub version: {version!r}")
    return version


def _request_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "InfernuxHub-Updater",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


def _catalog_release(document: object) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != {
        "$schema",
        "stable",
        "releases",
    }:
        raise ValueError("Hub release catalog does not match the current contract")
    stable = document["stable"]
    releases = document["releases"]
    if (
        document["$schema"] != HUB_CATALOG_SCHEMA
        or not isinstance(stable, str)
        or not _VERSION_PATTERN.fullmatch(stable)
        or not isinstance(releases, list)
    ):
        raise ValueError("Hub release catalog does not match the current contract")
    matching = [
        item
        for item in releases
        if isinstance(item, dict) and item.get("version") == stable
    ]
    if len(matching) != 1:
        raise ValueError("Hub release catalog must contain its stable release exactly once")
    release = matching[0]
    if set(release) != {
        "version",
        "channel",
        "published_at",
        "release_url",
        "minimum_updatable_version",
        "platforms",
    } or release["channel"] != "stable":
        raise ValueError("Stable Hub release does not match the current contract")
    if (
        (release["published_at"] is not None and (
            not isinstance(release["published_at"], str) or not release["published_at"]
        ))
        or not isinstance(release["release_url"], str)
        or not release["release_url"]
        or not isinstance(release["minimum_updatable_version"], str)
        or not _VERSION_PATTERN.fullmatch(release["minimum_updatable_version"])
        or not isinstance(release["platforms"], dict)
    ):
        raise ValueError("Stable Hub release platforms must be an object")
    return release


def check_for_update(
    current_version: str | None = None,
    *,
    platform_id: str | None = None,
) -> HubUpdateCheck:
    current = current_version or current_hub_version()
    try:
        catalog_bytes = _request_bytes(HUB_CATALOG_URL)
    except OSError as exc:
        return HubUpdateCheck(
            HubUpdateStatus.NETWORK_UNAVAILABLE,
            current,
            detail=str(exc),
        )
    try:
        release = _catalog_release(json.loads(catalog_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return HubUpdateCheck(
            HubUpdateStatus.CATALOG_INVALID,
            current,
            detail=str(exc),
        )
    target = str(release["version"])
    if release["published_at"] is None:
        return HubUpdateCheck(HubUpdateStatus.UP_TO_DATE, current)
    if _version_key(target) <= _version_key(current):
        return HubUpdateCheck(HubUpdateStatus.UP_TO_DATE, current, target)
    target_platform = platform_id or host_platform_id()
    platform_release = release["platforms"].get(target_platform)
    if not isinstance(platform_release, dict) or set(platform_release) != {
        "installer",
        "update",
        "manifest",
    }:
        return HubUpdateCheck(
            HubUpdateStatus.CATALOG_INVALID,
            current,
            target,
            detail=f"Infernux Hub {target} has no {target_platform} release",
        )
    full_name = f"InfernuxHub-{target}-{target_platform}-full.zip"
    manifest_name = manifest_asset_name(target_platform)
    installer_name = (
        f"InfernuxHubInstaller-{target}-windows-x64.exe"
        if target_platform == "windows-x64"
        else f"InfernuxHubInstaller-{target}-linux-x64"
    )
    installer_asset = platform_release["installer"]
    if (
        not isinstance(installer_asset, dict)
        or set(installer_asset) != {"name", "url"}
        or not isinstance(installer_asset.get("url"), str)
        or not installer_asset["url"]
    ):
        return HubUpdateCheck(
            HubUpdateStatus.CATALOG_INVALID,
            current,
            target,
            detail=f"Hub release catalog has an invalid {target_platform} installer",
        )
    minimum = str(release["minimum_updatable_version"])
    if _version_key(current) < _version_key(minimum):
        return HubUpdateCheck(
            HubUpdateStatus.UNSUPPORTED_CURRENT_VERSION,
            current,
            target,
            installer_url=installer_asset["url"],
            detail=f"Infernux Hub {minimum} or newer is required for in-app update",
        )
    update_asset = platform_release["update"]
    manifest_asset = platform_release["manifest"]
    if (
        installer_asset.get("name") != installer_name
        or not isinstance(update_asset, dict)
        or set(update_asset) != {"name", "url", "size"}
        or update_asset.get("name") != full_name
        or not isinstance(update_asset.get("url"), str)
        or not update_asset["url"]
        or not isinstance(update_asset.get("size"), int)
        or update_asset["size"] <= 0
        or not isinstance(manifest_asset, dict)
        or set(manifest_asset) != {"name", "url"}
        or manifest_asset.get("name") != manifest_name
        or not isinstance(manifest_asset.get("url"), str)
        or not manifest_asset["url"]
    ):
        return HubUpdateCheck(
            HubUpdateStatus.CATALOG_INVALID,
            current,
            target,
            detail=f"Hub release catalog has an invalid {target_platform} update pair",
        )
    update = HubUpdate(
        current_version=current,
        target_version=target,
        release_url=str(release["release_url"]),
        asset_name=full_name,
        asset_url=update_asset["url"],
        size=update_asset["size"],
        manifest_url=manifest_asset["url"],
        platform=target_platform,
    )
    return HubUpdateCheck(
        HubUpdateStatus.UPDATE_AVAILABLE,
        current,
        target,
        update=update,
        installer_url=installer_asset["url"],
    )


def _safe_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"Unsafe update path: {value!r}")
    if tuple(part.casefold() for part in path.parts[:2]) == ("infernuxhubdata", "shared"):
        raise ValueError("Hub updates cannot own user shared resources")
    return path


def _download(
    update: HubUpdate,
    destination: Path,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    request = urllib.request.Request(update.asset_url, headers={"User-Agent": "InfernuxHub-Updater"})
    received = 0
    with urllib.request.urlopen(request) as response, destination.open("wb") as stream:
        total = int(response.headers.get("Content-Length", update.size or 0))
        while True:
            chunk = response.read(1024 * 512)
            if not chunk:
                break
            stream.write(chunk)
            received += len(chunk)
            if progress:
                progress(received, total)
    if update.size and received != update.size:
        destination.unlink(missing_ok=True)
        raise ValueError(
            f"Downloaded update is incomplete: expected {update.size} bytes, received {received}"
        )


def stage_update(
    update: HubUpdate,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    from hub_utils import get_hub_shared_data_dir

    base = Path(get_hub_shared_data_dir()) / "Updates" / update.target_version
    if base.exists():
        shutil.rmtree(base)
    stage = base / "stage"
    stage.mkdir(parents=True)
    archive_path = base / update.asset_name
    _download(update, archive_path, progress)

    manifest_name = manifest_asset_name(update.platform)
    manifest_path = base / manifest_name
    manifest_bytes = _request_bytes(update.manifest_url)
    manifest_path.write_bytes(manifest_bytes)
    target_manifest = validate_manifest(json.loads(manifest_bytes.decode("utf-8")))
    if target_manifest["version"] != update.target_version:
        raise ValueError("Hub update manifest does not match the target release")
    if target_manifest["platform"] != update.platform:
        raise ValueError("Hub update manifest does not match the target platform")

    local_manifest = load_manifest(Path(get_app_dir()) / manifest_name)
    if local_manifest["version"] != update.current_version:
        raise ValueError("Installed Hub manifest does not match the running version")
    if local_manifest["platform"] != update.platform:
        raise ValueError("Installed Hub manifest does not match the running platform")
    old_paths = {entry["path"] for entry in local_manifest["files"]}
    target_entries = list(target_manifest["files"])
    target_paths = {entry["path"] for entry in target_entries}
    metadata = {
        "$schema": MANIFEST_SCHEMA,
        "product": PRODUCT_NAME,
        "base_version": update.current_version,
        "target_version": update.target_version,
        "files": target_entries,
        "delete": sorted(old_paths - target_paths),
    }

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        if names != target_paths:
            missing = sorted(target_paths - names)
            unexpected = sorted(names - target_paths)
            raise ValueError(
                "Hub update archive does not match its manifest: "
                f"missing={missing}, unexpected={unexpected}"
            )
        for entry in target_entries:
            relative = _safe_path(entry["path"])
            destination = stage.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            archive_entry = archive.getinfo(relative.as_posix())
            with archive.open(archive_entry) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            archived_mode = (archive_entry.external_attr >> 16) & 0o777
            if archived_mode:
                destination.chmod(archived_mode)
        for relative in metadata["delete"]:
            _safe_path(relative)

    shutil.copy2(manifest_path, stage / manifest_name)
    metadata["files"] = target_entries + [{"path": manifest_name}]

    metadata_path = base / "hub-update.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return base


def launch_external_updater(staged_root: str | Path, *, is_dark: bool) -> None:
    if not is_frozen():
        raise RuntimeError("Automatic replacement requires a packaged Infernux Hub")
    root = Path(staged_root).resolve()
    if sys.platform == "win32":
        script = root / "apply-update.ps1"
        script.write_text(_powershell_updater_script(is_dark), encoding="utf-8-sig")
        _launch_elevated_powershell(
            script,
            [
                "-ParentPid", str(os.getpid()),
                "-InstallDir", str(Path(get_app_dir()).resolve()),
                "-StageDir", str((root / "stage").resolve()),
                "-MetadataPath", str((root / "hub-update.json").resolve()),
            ],
            root,
        )
        return
    if sys.platform == "linux":
        from python_runtime import PythonRuntimeManager

        python_executable = PythonRuntimeManager().get_runtime_path()
        if not python_executable:
            raise RuntimeError("The managed Python runtime required by the Hub updater is missing")
        script = Path(get_app_dir()) / "InfernuxHubData" / "updater" / "hub_update_apply.py"
        if not script.is_file():
            raise RuntimeError(f"The packaged Linux Hub updater is missing: {script}")
        subprocess.Popen(
            [
                python_executable,
                os.fspath(script),
                "--parent-pid",
                str(os.getpid()),
                "--install-dir",
                os.fspath(Path(get_app_dir()).resolve()),
                "--stage-dir",
                os.fspath((root / "stage").resolve()),
                "--metadata",
                os.fspath((root / "hub-update.json").resolve()),
            ],
            cwd=root,
            start_new_session=True,
        )
        return
    raise RuntimeError(f"Infernux Hub has no updater contract for {sys.platform}")


def _launch_elevated_powershell(
    script: Path,
    arguments: list[str],
    working_directory: Path,
) -> None:
    """Launch the staged replacement step with the installer's privileges."""
    import ctypes

    parameters = subprocess.list2cmdline(
        [
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", str(script),
            *arguments,
        ]
    )
    # The installer defaults to Program Files.  A normally launched Hub can
    # download and verify an update there, but it cannot replace its installed
    # files without a UAC grant.  SW_HIDE suppresses the console window; the
    # updater script presents its own progress window.
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        "powershell.exe",
        parameters,
        str(working_directory),
        0,
    )
    if int(result) <= 32:
        raise OSError(int(result), "Could not start the elevated Hub updater")


def _powershell_updater_script(is_dark: bool) -> str:
    palette = StyleManager.palette(is_dark)
    return (
        _POWERSHELL_UPDATER_TEMPLATE.replace("@BG_BASE@", palette.bg_base)
        .replace("@TEXT_PRIMARY@", palette.text_primary)
        .replace("@TEXT_SECONDARY@", palette.text_secondary)
        .replace("@ACCENT@", palette.accent)
        .replace("@DANGER@", palette.danger)
    )


_POWERSHELL_UPDATER_TEMPLATE = r'''param(
    [int]$ParentPid,
    [string]$InstallDir,
    [string]$StageDir,
    [string]$MetadataPath
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object Windows.Forms.Form
$form.Text = "Infernux Hub Update"
$form.Size = New-Object Drawing.Size(460,170)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.BackColor = [Drawing.ColorTranslator]::FromHtml("@BG_BASE@")
$label = New-Object Windows.Forms.Label
$label.Text = "INSTALLING INFERNUX HUB UPDATE"
$label.ForeColor = [Drawing.ColorTranslator]::FromHtml("@TEXT_PRIMARY@")
$label.Location = New-Object Drawing.Point(24,24)
$label.Size = New-Object Drawing.Size(400,24)
$label.Font = New-Object Drawing.Font("Segoe UI",11,[Drawing.FontStyle]::Bold)
$bar = New-Object Windows.Forms.Panel
$bar.Location = New-Object Drawing.Point(24,68)
$bar.Size = New-Object Drawing.Size(396,4)
$bar.BackColor = [Drawing.ColorTranslator]::FromHtml("@ACCENT@")
$status = New-Object Windows.Forms.Label
$status.Text = "Waiting for Infernux Hub to close..."
$status.ForeColor = [Drawing.ColorTranslator]::FromHtml("@TEXT_SECONDARY@")
$status.Location = New-Object Drawing.Point(24,92)
$status.Size = New-Object Drawing.Size(396,24)
$form.Controls.AddRange(@($label,$bar,$status))
$form.Show()
[Windows.Forms.Application]::DoEvents()
$backup = Join-Path (Split-Path $MetadataPath) "backup"
$applied = New-Object System.Collections.Generic.List[string]
try {
    while (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
        Start-Sleep -Milliseconds 100
        [Windows.Forms.Application]::DoEvents()
    }
    $status.Text = "Replacing application files..."
    [Windows.Forms.Application]::DoEvents()
    $metadata = Get-Content -LiteralPath $MetadataPath -Raw | ConvertFrom-Json
    foreach ($file in $metadata.files) {
        $source = Join-Path $StageDir $file.path
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Staged file is missing: $($file.path)" }
    }
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    $affected = @($metadata.files.path) + @($metadata.delete)
    foreach ($relative in $affected) {
        $live = Join-Path $InstallDir $relative
        if (Test-Path -LiteralPath $live -PathType Leaf) {
            $saved = Join-Path $backup $relative
            New-Item -ItemType Directory -Force -Path (Split-Path $saved) | Out-Null
            Copy-Item -LiteralPath $live -Destination $saved -Force
        }
    }
    foreach ($file in $metadata.files) {
        $source = Join-Path $StageDir $file.path
        $destination = Join-Path $InstallDir $file.path
        New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
        $applied.Add([string]$file.path)
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
    foreach ($relative in $metadata.delete) {
        $target = Join-Path $InstallDir $relative
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            $applied.Add([string]$relative)
            Remove-Item -LiteralPath $target -Force
        }
    }
    $status.Text = "Update complete. Restarting..."
    [Windows.Forms.Application]::DoEvents()
    Start-Sleep -Milliseconds 500
    Start-Process -FilePath (Join-Path $InstallDir "Infernux Hub.exe") -WorkingDirectory $InstallDir
} catch {
    foreach ($relative in $applied) {
        $live = Join-Path $InstallDir $relative
        $saved = Join-Path $backup $relative
        if (Test-Path -LiteralPath $saved -PathType Leaf) {
            New-Item -ItemType Directory -Force -Path (Split-Path $live) | Out-Null
            Copy-Item -LiteralPath $saved -Destination $live -Force
        } elseif (Test-Path -LiteralPath $live -PathType Leaf) {
            Remove-Item -LiteralPath $live -Force
        }
    }
    $bar.BackColor = [Drawing.ColorTranslator]::FromHtml("@DANGER@")
    $status.Text = "Update failed: $($_.Exception.Message)"
    [Windows.Forms.MessageBox]::Show($status.Text,"Infernux Hub Update") | Out-Null
    Start-Sleep -Seconds 2
}
$form.Close()
'''


__all__ = [
    "HubUpdate",
    "HubUpdateCheck",
    "HubUpdateStatus",
    "check_for_update",
    "current_hub_version",
    "launch_external_updater",
    "stage_update",
]
