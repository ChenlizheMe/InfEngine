from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import hub_update_apply
import hub_updater


def _fixture(tmp_path, existing):
    install = tmp_path / "installed"
    stage = tmp_path / "stage"
    install.mkdir()
    stage.mkdir()
    (install / "Infernux Hub").write_bytes(b"existing executable")
    (stage / "payload.bin").write_bytes(b"complete new payload")
    if existing:
        (install / "payload.bin").write_bytes(b"original payload")
    metadata = tmp_path / "hub-update.json"
    metadata.write_text(json.dumps({
        "$schema": "infernux.hub_update", "product": "InfernuxHub",
        "base_version": "0.4.0", "target_version": "0.4.1",
        "files": [{"path": "payload.bin"}], "delete": [],
    }), encoding="utf-8")
    return install, stage, metadata


def _assert_restored(install, existing):
    if existing:
        assert (install / "payload.bin").read_bytes() == b"original payload"
    else:
        assert not (install / "payload.bin").exists()
    assert (install / "Infernux Hub").read_bytes() == b"existing executable"


@pytest.mark.parametrize("existing", (False, True))
def test_linux_update_restores_the_file_whose_copy_failed(tmp_path, monkeypatch, existing):
    install, stage, metadata = _fixture(tmp_path, existing)
    copy = shutil.copy2

    def partial_copy(source, destination, *args, **kwargs):
        if Path(source) == stage / "payload.bin":
            Path(destination).write_bytes(b"partial payload")
            raise OSError("copy failed after truncation")
        return copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(hub_update_apply.shutil, "copy2", partial_copy)
    monkeypatch.setattr(hub_update_apply, "_wait_for_exit", lambda pid: None)
    with pytest.raises(OSError, match="copy failed after truncation"):
        hub_update_apply.apply_update(parent_pid=1, install_dir=install,
                                     stage_dir=stage, metadata_path=metadata)
    _assert_restored(install, existing)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell update transaction")
@pytest.mark.parametrize("existing", (False, True))
def test_windows_update_rolls_back_a_nonterminating_copy_error(tmp_path, existing):
    install, stage, metadata = _fixture(tmp_path, existing)
    script = hub_updater._powershell_updater_script(True)
    # Execute the generated filesystem transaction, omitting only UI and delay
    # statements. No replacement implementation of apply/rollback is used.
    prefix = script[:script.index("Add-Type -AssemblyName")]
    body = script[script.index('$backup = Join-Path'):script.index('$form.Close()')]
    body = "\n".join(line for line in body.splitlines() if not any(
        marker in line for marker in ("[Windows.Forms.", "$bar.BackColor", "Start-Sleep")
    ))
    injection = r'''
$status = [pscustomobject]@{Text=""}
function Copy-Item {
    [CmdletBinding()]
    param([string]$LiteralPath, [string]$Destination, [switch]$Force)
    if ($LiteralPath -eq (Join-Path $StageDir "payload.bin")) {
        [IO.File]::WriteAllText($Destination, "partial payload")
        Write-Error "copy failed after truncation"
        return
    }
    Microsoft.PowerShell.Management\Copy-Item -LiteralPath $LiteralPath -Destination $Destination -Force
}
function Start-Process { Write-Output "UNEXPECTED_RESTART" }
'''
    driver = tmp_path / "update-transaction.ps1"
    driver.write_text(prefix + injection + body + '\nWrite-Output $status.Text\n', encoding="utf-8-sig")
    result = subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(driver), "-ParentPid", "2147483647", "-InstallDir", str(install),
        "-StageDir", str(stage), "-MetadataPath", str(metadata),
    ], capture_output=True, text=True, errors="replace", timeout=30, creationflags=0x08000000)
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_restored(install, existing)
    assert "copy failed after truncation" in result.stdout
    assert "UNEXPECTED_RESTART" not in result.stdout
