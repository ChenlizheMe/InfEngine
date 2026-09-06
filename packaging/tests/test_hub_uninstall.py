from pathlib import Path
import os
import subprocess
import shutil
import sys
import time

import pytest

from hub_uninstall import remove_application
from installer_safety import is_recognized_install_dir, write_install_marker
from installer.install_application import HubInstallTransaction
from installer.payload import HUB_EXECUTABLE


def test_uninstall_removes_program_and_preserves_shared_and_reinstall(tmp_path):
    root = tmp_path / "Hub"
    write_install_marker(str(root))
    (root / HUB_EXECUTABLE).write_bytes(b"old")
    (root / "lib").mkdir()
    (root / "lib/library.dll").write_bytes(b"library")
    shared = root / "InfernuxHubData/Shared"
    shared.mkdir(parents=True)
    resource = shared / "plugin.inxpkg"
    resource.write_bytes(b"user package")
    (root / "InfernuxHubData/bundled.bin").write_bytes(b"bundle")

    remove_application(root)

    assert not (root / HUB_EXECUTABLE).exists()
    assert not (root / "lib").exists()
    assert not (root / "InfernuxHubData/bundled.bin").exists()
    assert resource.read_bytes() == b"user package"
    assert is_recognized_install_dir(str(root))
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / HUB_EXECUTABLE).write_bytes(b"new")
    with HubInstallTransaction(payload, root) as installation:
        installation.prepare()
        installation.activate()
        installation.commit()
    assert resource.read_bytes() == b"user package"
    assert (root / HUB_EXECUTABLE).read_bytes() == b"new"


def test_uninstall_rejects_project_even_with_install_marker(tmp_path):
    write_install_marker(str(tmp_path))
    (tmp_path / "Assets").mkdir()
    (tmp_path / "ProjectSettings").mkdir()
    with pytest.raises(ValueError, match="project directory"):
        remove_application(tmp_path)
    assert (tmp_path / "Assets").is_dir()


def test_uninstall_requires_marker_and_propagates_deletion_errors(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError):
        remove_application(tmp_path)
    write_install_marker(str(tmp_path))
    (tmp_path / HUB_EXECUTABLE).write_bytes(b"locked")
    monkeypatch.setattr(Path, "unlink", lambda _path: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(PermissionError, match="locked"):
        remove_application(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native uninstall helper")
@pytest.mark.parametrize("wait_for_parent", [False, True])
def test_windows_uninstaller_preserves_shared_and_directory_links(tmp_path, wait_for_parent):
    root = tmp_path / "Hub"
    write_install_marker(str(root))
    (root / "Hub.exe").write_bytes(b"program")
    shared = root / "InfernuxHubData/Shared"
    shared.mkdir(parents=True)
    (shared / "plugin.inxpkg").write_bytes(b"user")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"outside")
    powershell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    # Directory junctions need no symlink privilege and must not be traversed.
    for link in (root / "link", root / "lib/nested-link"):
        link.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            powershell, "-NoProfile", "-Command",
            "New-Item -ItemType Junction -Path $env:INX_TEST_LINK -Target $env:INX_TEST_TARGET | Out-Null",
        ], check=True, capture_output=True, env={
            **os.environ, "INX_TEST_LINK": str(link), "INX_TEST_TARGET": str(outside),
        })
    helper = root / "InfernuxHubData/uninstaller/hub_uninstall.ps1"
    helper.parent.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / "hub_uninstall.ps1", helper)
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"]) if wait_for_parent else None
    process = subprocess.Popen([
        powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper),
        "-InstallDir", str(root), "-ParentPid", str(parent.pid if parent else 0), "-Quiet",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    if parent:
        time.sleep(0.3)
        assert parent.poll() is None
        assert (root / "Hub.exe").exists()
    stdout, stderr = process.communicate(timeout=15)
    if parent:
        parent.wait(timeout=5)
    assert process.returncode == 0, stdout + stderr
    assert not (root / "Hub.exe").exists()
    assert not helper.exists()
    assert (shared / "plugin.inxpkg").read_bytes() == b"user"
    assert (outside / "keep.txt").read_bytes() == b"outside"


def test_windows_dispatch_uses_system_powershell_not_mutable_python(tmp_path, monkeypatch):
    import ctypes
    from types import SimpleNamespace
    import launcher

    install = tmp_path / "Hub"
    helper = install / "InfernuxHubData/uninstaller/hub_uninstall.ps1"
    helper.parent.mkdir(parents=True)
    helper.write_text("# helper")
    calls = []

    def launch(*args):
        calls.append(args)
        return 33

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace(ShellExecuteW=launch)), raising=False)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.setattr(launcher, "get_app_dir", lambda: str(install))
    monkeypatch.setattr(launcher, "is_frozen", lambda: True)
    launcher._schedule_windows_application_removal(str(install))
    assert calls[0][1] == "runas"
    assert calls[0][2] == str(tmp_path / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    assert str(helper) in calls[0][3]
    assert "-ParentPid" in calls[0][3]
    assert calls[0][-1] == 0
