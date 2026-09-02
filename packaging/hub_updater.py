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
from pathlib import Path, PurePosixPath
from typing import Callable

from hub_utils import get_app_dir, is_frozen
from hub_release import MANIFEST_SCHEMA, PRODUCT_NAME, load_manifest, validate_manifest


GITHUB_LATEST_RELEASE = "https://api.github.com/repos/ChenlizheMe/Infernux/releases/latest"
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
# Hub 0.4 introduces the versioned Python runtime catalog. Earlier Hub builds
# can discover the release, but cannot provision its cp313 runtime.
_VERSIONED_RUNTIME_HUB_VERSION = "0.4.0"


@dataclass(frozen=True)
class HubUpdate:
    current_version: str
    target_version: str
    release_url: str
    asset_name: str
    asset_url: str
    size: int
    manifest_url: str
    required: bool = False


def _version_key(value: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", value)
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def _update_is_required(current_version: str, target_version: str) -> bool:
    boundary = _version_key(_VERSIONED_RUNTIME_HUB_VERSION)
    return _version_key(current_version) < boundary <= _version_key(target_version)


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


def _request_bytes(url: str, timeout: int = 20) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "InfernuxHub-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def check_for_update(current_version: str | None = None) -> HubUpdate | None:
    current = current_version or current_hub_version()
    release = json.loads(_request_bytes(GITHUB_LATEST_RELEASE).decode("utf-8"))
    target = str(release.get("tag_name", "")).removeprefix("v")
    if not _VERSION_PATTERN.match(target) or _version_key(target) <= _version_key(current):
        return None

    assets = {asset["name"]: asset for asset in release.get("assets", [])}
    manifest_name = "InfernuxHub-manifest.json"
    manifest_asset = assets.get(manifest_name)
    full_name = f"InfernuxHub-{target}-windows-x64-full.zip"
    asset = assets.get(full_name)
    if not manifest_asset or not asset:
        return None
    return HubUpdate(
        current_version=current,
        target_version=target,
        release_url=release.get("html_url", ""),
        asset_name=full_name,
        asset_url=asset["browser_download_url"],
        size=int(asset.get("size", 0)),
        manifest_url=manifest_asset["browser_download_url"],
        required=_update_is_required(current, target),
    )


def _safe_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"Unsafe update path: {value!r}")
    return path


def _download(
    update: HubUpdate,
    destination: Path,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    request = urllib.request.Request(update.asset_url, headers={"User-Agent": "InfernuxHub-Updater"})
    received = 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as stream:
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
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "InfernuxHub" / "updates" / update.target_version
    if base.exists():
        shutil.rmtree(base)
    stage = base / "stage"
    stage.mkdir(parents=True)
    archive_path = base / update.asset_name
    _download(update, archive_path, progress)

    manifest_path = base / "InfernuxHub-manifest.json"
    manifest_bytes = _request_bytes(update.manifest_url, timeout=60)
    manifest_path.write_bytes(manifest_bytes)
    target_manifest = validate_manifest(json.loads(manifest_bytes.decode("utf-8")))
    if target_manifest["version"] != update.target_version:
        raise ValueError("Hub update manifest does not match the target release")

    local_manifest = load_manifest(Path(get_app_dir()) / "InfernuxHub-manifest.json")
    if local_manifest["version"] != update.current_version:
        raise ValueError("Installed Hub manifest does not match the running version")
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
            with archive.open(relative.as_posix()) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
        for relative in metadata["delete"]:
            _safe_path(relative)

    shutil.copy2(manifest_path, stage / "InfernuxHub-manifest.json")
    metadata["files"] = target_entries + [{"path": "InfernuxHub-manifest.json"}]

    metadata_path = base / "hub-update.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return base


def launch_external_updater(staged_root: str | Path) -> None:
    if sys.platform != "win32" or not is_frozen():
        raise RuntimeError("Automatic replacement is only available in the packaged Windows Hub")
    root = Path(staged_root).resolve()
    script = root / "apply-update.ps1"
    script.write_text(_POWERSHELL_UPDATER, encoding="utf-8-sig")
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


_POWERSHELL_UPDATER = r'''param(
    [int]$ParentPid,
    [string]$InstallDir,
    [string]$StageDir,
    [string]$MetadataPath
)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object Windows.Forms.Form
$form.Text = "Infernux Hub Update"
$form.Size = New-Object Drawing.Size(460,170)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.BackColor = [Drawing.Color]::FromArgb(10,12,17)
$label = New-Object Windows.Forms.Label
$label.Text = "INSTALLING INFERNUX HUB UPDATE"
$label.ForeColor = [Drawing.Color]::FromArgb(243,238,226)
$label.Location = New-Object Drawing.Point(24,24)
$label.Size = New-Object Drawing.Size(400,24)
$label.Font = New-Object Drawing.Font("Segoe UI",11,[Drawing.FontStyle]::Bold)
$bar = New-Object Windows.Forms.Panel
$bar.Location = New-Object Drawing.Point(24,68)
$bar.Size = New-Object Drawing.Size(396,4)
$bar.BackColor = [Drawing.Color]::FromArgb(235,87,87)
$status = New-Object Windows.Forms.Label
$status.Text = "Waiting for Infernux Hub to close..."
$status.ForeColor = [Drawing.Color]::FromArgb(170,177,188)
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
        Copy-Item -LiteralPath $source -Destination $destination -Force
        $applied.Add([string]$file.path)
    }
    foreach ($relative in $metadata.delete) {
        $target = Join-Path $InstallDir $relative
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            Remove-Item -LiteralPath $target -Force
            $applied.Add([string]$relative)
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
    $bar.BackColor = [Drawing.Color]::FromArgb(184,49,59)
    $status.Text = "Update failed: $($_.Exception.Message)"
    [Windows.Forms.MessageBox]::Show($status.Text,"Infernux Hub Update") | Out-Null
    Start-Sleep -Seconds 2
}
$form.Close()
'''


__all__ = [
    "HubUpdate",
    "check_for_update",
    "current_hub_version",
    "launch_external_updater",
    "stage_update",
]
